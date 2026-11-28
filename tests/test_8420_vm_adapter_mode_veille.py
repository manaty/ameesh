# SPDX-License-Identifier: AGPL-3.0-only
"""#8420: mode-veille.sh entrer before a replica halt, sortir after both return.

#8459: a failed sortir stays durable (alert, replay at each idle tick) and the
enrollment bound keeps a margin under the tick's idle-in-transaction timeout.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from tests.test_5925_vm_adapter import TARGET, policy
from ameesh import host_release, storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue

FRESH = mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1})
ROOT = Path(__file__).resolve().parents[1]


def replica(**over):
    return dict(TARGET, replica=True, standby_enter_argv=['/entrer'],
                standby_exit_argv=['/sortir'], **over)


def replicas(**over):
    """Two replica targets (the two agent VMs), standby bound enrolled."""
    other = replica(server_id='33333333-3333-4333-8333-333333333333',
                    probe_agent='other-probe')
    return policy(**dict(dict(standby_timeout_s=60, targets={'burst': replica(), 'other': other}),
                         **over))


class EnrollmentTest(unittest.TestCase):
    def test_replica_requires_both_commands_and_a_bounded_timeout(self):
        value = vm.enrollment(replicas(), local_host='controller')
        self.assertIs(value['targets']['burst']['replica'], True)
        for key in ('standby_enter_argv', 'standby_exit_argv'):
            for argv in (None, [], ['relative/mode-veille'], ['/x', '']):
                bad = replicas()
                bad['targets']['burst'][key] = argv
                with self.assertRaisesRegex(vm.Refused, 'fixed absolute'):
                    vm.enrollment(bad, local_host='controller')
        missing = replicas()
        del missing['standby_timeout_s']
        for bad in (missing, replicas(standby_timeout_s=True), replicas(standby_timeout_s=5),
                    replicas(standby_timeout_s=121), replicas(standby_timeout_s=float('nan'))):
            with self.assertRaisesRegex(vm.Refused, 'standby timeout'):
                vm.enrollment(bad, local_host='controller')

    def test_standby_timeout_stays_strictly_under_the_idle_transaction_timeout(self):
        """#8459: `entrer` runs inside the tick transaction, whose
        `idle_in_transaction_session_timeout` is 120 s; a bound of 120 s (or
        more) can be cut mid-call by the database."""
        self.assertLess(vm.STANDBY_TIMEOUT_MAX_S, 120)
        self.assertEqual(vm.enrollment(replicas(standby_timeout_s=90),
                                       local_host='controller')['standby_timeout_s'], 90)
        for bad in (90.5, 91, 119, 120):
            with self.assertRaisesRegex(vm.Refused, 'standby timeout'):
                vm.enrollment(replicas(standby_timeout_s=bad), local_host='controller')

    def test_standby_commands_without_replica_are_refused(self):
        self.assertNotIn('replica', vm.enrollment(policy(), local_host='controller')['targets']['burst'])
        for flag in ({}, {'replica': False}):
            bad = policy()
            bad['targets']['burst'].update(flag, standby_enter_argv=['/entrer'])
            with self.assertRaisesRegex(vm.Refused, 'replica: true'):
                vm.enrollment(bad, local_host='controller')
        bad = policy()
        bad['targets']['burst']['replica'] = 'yes'
        with self.assertRaisesRegex(vm.Refused, 'replica must be'):
            vm.enrollment(bad, local_host='controller')

    def test_example_declares_a_replica_with_mode_veille(self):
        enrolled = json.loads((ROOT / 'deploy/vm-adapter/enrollment.example.json').read_text())
        enrolled.update(enabled=True)
        value = vm.enrollment(enrolled, local_host=enrolled['controller_host'])
        target = value['targets']['replica-host']
        self.assertIs(target['replica'], True)
        self.assertEqual(target['standby_enter_argv'][-1], 'entrer')
        self.assertEqual(target['standby_exit_argv'][-1], 'sortir')
        self.assertNotIn('replica', value['targets']['burst-host'])


class Base(PgTestCase):
    request_state = None

    def setUp(self):
        super().setUp()
        for table in ('vm_adapter_requests', 'vm_adapter_observations', 'host_lifecycle'):
            self.db.execute('DELETE FROM ' + table)
        # ameesh_events keeps 30 days (retention): read only this test's rows.
        self.since = self.db.query("SELECT coalesce(max(id), 0) AS id FROM ameesh_events")[0]['id']
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,release_state,hook_state) "
                        "VALUES ('burst','draining','tester',false,'draining','draining')")
        self.q = Queue(self.db)
        self.cfg = replace(self.cfg, idle_after_s=1800, host_release_adapter_stall=900)
        self.provider = mock.Mock()
        self.provider.state.return_value = 'running'
        self.provider.allowed.return_value = {'poweron', 'poweroff'}
        operations = mock.Mock()
        operations.listing.return_value = []
        operations.open_lots_activity.return_value = []
        hosts = mock.Mock()
        hosts.holds.return_value = []
        self.store = SimpleNamespace(host_lifecycle=storage.of(self.db).host_lifecycle,
                                     operations=operations, hosts=hosts)
        self.probe = mock.Mock(return_value=True)
        control = self.q.control('burst')
        self.q.request('burst', control['generation'], control['release_epoch'], self.request_state)
        self.q.observe('burst', 'running', 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours'")
        self.policy = replicas()

    def tick(self):
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            return vm.tick_host(self.cfg, self.db, self.policy, 'burst', self.provider,
                                probe=self.probe,
                                evidence=lambda *a, **k: {'idle_since': k['now'] - 7200})

    def argvs(self):
        return [c.args[0] for c in self.probe.call_args_list]

    def request(self, state):
        return self.db.query("SELECT * FROM vm_adapter_requests WHERE state=%s "
                             "ORDER BY generation DESC LIMIT 1", (state,))[0]


class EnterTest(Base):
    request_state = 'idle-drained'

    def answer(self, refused):
        self.probe.side_effect = lambda argv, *a, **k: argv != refused

    def test_enter_runs_before_the_halt_intent_and_before_the_halt(self):
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        self.assertEqual(self.tick(), 'halt sent')
        self.assertEqual(self.argvs(), [['/quiesce'], ['/entrer'], ['/quiesce'], ['/entrer'], ['/halt']])
        self.assertEqual(self.policy['standby_timeout_s'], self.probe.call_args_list[1].args[1])

    def test_enter_refused_keeps_the_vm_up_without_halt_intent(self):
        self.answer(['/entrer'])
        self.assertEqual(self.tick(), 'stop prepared')
        for _ in range(3):
            self.assertEqual(self.tick(), 'standby enter refused')
        self.assertEqual(self.request('idle-drained')['result'], vm.STOP_PREPARED)
        self.assertNotIn(['/halt'], self.argvs())
        # Once the hub enters standby mode, the halt phase resumes.
        self.answer(None)
        self.assertEqual(self.tick(), 'halt prepared')

    def test_enter_refused_after_the_intent_sends_no_halt(self):
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        self.answer(['/entrer'])
        self.assertEqual(self.tick(), 'standby enter refused')
        self.assertEqual(self.request('idle-drained')['result'], vm.HALT_INTENT)
        self.assertNotIn(['/halt'], self.argvs())
        self.provider.action.assert_not_called()

    def test_a_target_without_replica_never_calls_mode_veille(self):
        self.policy = policy()
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        self.assertEqual(self.tick(), 'halt sent')
        self.assertEqual(self.argvs(), [['/quiesce'], ['/quiesce'], ['/halt']])

    def test_enter_records_a_durable_standby_debt(self):
        """#8459 B1: committed with the stop intent, a tick before `entrer`."""
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertIsNotNone(self.q.control('burst')['standby_entered_ts'])
        self.assertEqual(self.tick(), 'halt prepared')
        self.assertIsNotNone(self.q.control('burst')['standby_entered_ts'])

    def test_a_killed_worker_after_enter_keeps_the_debt(self):
        """#8459 B1 (cx2): the debt is committed before the external call, so
        a worker killed after `entrer` and before the tick commits still owes
        `sortir`; the rolled-back probe cannot erase the earlier commit."""
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertIsNotNone(self.q.control('burst')['standby_entered_ts'])
        entered = []

        def killed(argv, *args, **kwargs):
            if argv == ['/entrer']:
                entered.append(argv)
                raise RuntimeError('worker killed after entrer')
            return True

        self.probe.side_effect = killed
        with self.assertRaisesRegex(RuntimeError, 'killed after entrer'):
            self.tick()
        self.assertEqual(entered, [['/entrer']])
        # The probe transaction rolled back; the committed debt stands.
        self.assertIsNotNone(self.q.control('burst')['standby_entered_ts'])
        self.probe.side_effect = None
        self.probe.return_value = True
        self.assertEqual(self.tick(), 'halt prepared')

    def test_refused_enter_still_records_the_debt(self):
        self.answer(['/entrer'])
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'standby enter refused')
        self.assertIsNotNone(self.q.control('burst')['standby_entered_ts'])
        self.assertIsNone(self.q.control('burst')['standby_exit_failed_ts'])


class ExitTest(Base):
    request_state = 'busy'

    def other(self, mode, claims_open, busy_done=False):
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,release_state,hook_state) "
                        "VALUES ('other',%s,'tester',%s,'draining','draining')",
                        (mode, claims_open and not busy_done))
        if busy_done:
            control = self.q.control('other')
            self.q.request('other', control['generation'], control['release_epoch'], 'busy')
            row = self.request_of('other')
            self.q.complete(row, 'healthy and reconciled')
            self.db.execute("UPDATE host_lifecycle SET claims_open=%s WHERE host='other'",
                            (claims_open,))

    def request_of(self, host):
        return self.db.query("SELECT * FROM vm_adapter_requests WHERE host=%s AND state='busy'",
                             (host,))[0]

    def returned(self):
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            return self.tick()

    def events(self):
        return self.db.query("SELECT object, data FROM ameesh_events "
                             "WHERE type='vm.standby_exit_failed' AND id>%s", (self.since,))

    def test_exit_waits_for_the_other_replica(self):
        self.other('draining', False)
        self.assertEqual(self.returned(), 'ready; standby exit waits for the other replica')
        self.assertNotIn(['/sortir'], self.argvs())
        self.assertEqual(self.request_of('burst')['status'], 'done')

    def test_exit_after_both_replicas_are_back(self):
        # The native hook may reopen the claims before `complete_return`
        # (review of #8420): a done return counts in both gate states.
        for mode, claims, done in (('available', True, False), ('returning', False, True),
                                   ('returning', True, True)):
            with self.subTest(other=mode, claims_open=claims):
                self.db.execute("DELETE FROM host_lifecycle WHERE host='other'")
                self.db.execute("DELETE FROM vm_adapter_requests WHERE host='other'")
                self.other(mode, claims, done)
                self.db.execute("UPDATE vm_adapter_requests SET status='pending',result=NULL "
                                "WHERE host='burst'")
                self.db.execute("UPDATE host_lifecycle SET mode='draining',return_probe=NULL "
                                "WHERE host='burst'")
                self.probe.reset_mock()
                self.assertEqual(self.returned(), 'ready; standby exited')
                self.assertEqual(self.argvs()[-1], ['/sortir'])
                self.assertEqual(self.probe.call_args.args[1], 60)

    def test_exit_with_no_control_for_the_other_replica(self):
        self.assertEqual(self.returned(), 'ready; standby exited')

    def test_exit_failure_alerts_and_the_return_stands(self):
        self.probe.side_effect = lambda argv, *a, **k: argv != ['/sortir']
        self.assertEqual(self.returned(), 'ready; standby exit failed (synchronous list left empty)')
        self.assertEqual(self.request_of('burst')['status'], 'done')
        events = self.events()
        self.assertEqual([e['object'] for e in events], ['burst'])
        control = self.q.control('burst')
        self.assertEqual(events[0]['data'], {'generation': control['generation'],
                                             'release_epoch': control['release_epoch']})

    # -- #8459: the debt alerts, survives and is replayed ---------------------
    def control_of(self, host='burst'):
        return self.q.control(host) or {}

    def available(self, host='burst'):
        """The scanner's `complete_return` after `ready` (out of this worker)."""
        self.db.execute("UPDATE host_lifecycle SET mode='available',claims_open=true,"
                        "return_probe=NULL WHERE host=%s", (host,))

    def failing_exit(self):
        self.probe.side_effect = lambda argv, *a, **k: argv != ['/sortir']

    def standby_alerts(self):
        return [a for a in host_release.alerts(self.cfg, self.db, time.time())
                if a['type'] == 'host_release_standby_exit_failed']

    def test_exit_failure_is_durable_and_alerted(self):
        self.failing_exit()
        self.assertEqual(self.returned(), 'ready; standby exit failed (synchronous list left empty)')
        control = self.control_of()
        self.assertIsNotNone(control['standby_exit_failed_ts'])
        alerts = self.standby_alerts()
        self.assertEqual([(a['host'], a['since'], a['threshold']) for a in alerts],
                         [('burst', round(control['standby_exit_failed_ts'], 3), 0.0)])
        self.assertIn('synchronous_standby_names', alerts[0]['detail'])
        self.assertEqual(alerts[0]['generation'], control['generation'])

    def test_failed_exit_is_replayed_on_an_idle_tick_then_cleared(self):
        self.failing_exit()
        self.assertEqual(self.returned(), 'ready; standby exit failed (synchronous list left empty)')
        self.assertEqual(len(self.standby_alerts()), 1)
        self.available()
        self.probe.reset_mock()
        self.probe.side_effect = None
        self.probe.return_value = True
        self.assertEqual(self.tick(), 'inactive; standby exited')
        self.assertEqual(self.argvs(), [['/sortir']])
        control = self.control_of()
        self.assertIsNone(control['standby_entered_ts'])
        self.assertIsNone(control['standby_exit_failed_ts'])
        self.assertEqual(self.standby_alerts(), [])

    def test_replay_keeps_one_event_and_the_first_failure_instant(self):
        self.failing_exit()
        self.assertEqual(self.returned(), 'ready; standby exit failed (synchronous list left empty)')
        failed = self.control_of()['standby_exit_failed_ts']
        self.available()
        self.assertEqual(self.tick(),
                         'inactive; standby exit failed (synchronous list left empty)')
        self.assertEqual(self.tick(),
                         'inactive; standby exit failed (synchronous list left empty)')
        self.assertEqual(self.control_of()['standby_exit_failed_ts'], failed)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.standby_alerts()), 1)

    def test_replay_waits_for_a_replica_that_is_not_back(self):
        self.failing_exit()
        self.assertEqual(self.returned(), 'ready; standby exit failed (synchronous list left empty)')
        self.available()
        self.other('draining', False)
        self.probe.reset_mock()
        self.assertEqual(self.tick(), 'inactive; standby exit waits for the other replica')
        self.assertEqual(self.probe.call_args_list, [])
        # Once that replica is back, the next idle tick restores the list.
        self.db.execute("UPDATE host_lifecycle SET mode='available',claims_open=true "
                        "WHERE host='other'")
        self.probe.side_effect = None
        self.probe.return_value = True
        self.assertEqual(self.tick(), 'inactive; standby exited')

    def test_success_clears_the_debt_of_every_replica(self):
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,"
                        "release_state,hook_state) VALUES "
                        "('other','available','tester',true,'busy','busy')")
        self.db.execute("UPDATE host_lifecycle SET standby_entered_at=clock_timestamp(),"
                        "standby_exit_failed_at=clock_timestamp() "
                        "WHERE host IN ('burst','other')")
        self.assertEqual(self.returned(), 'ready; standby exited')
        for host in ('burst', 'other'):
            with self.subTest(host=host):
                control = self.control_of(host)
                self.assertIsNone(control['standby_entered_ts'])
                self.assertIsNone(control['standby_exit_failed_ts'])

    def test_an_n1_writer_row_carries_no_debt(self):
        """#8459: the columns are additive; an older writer leaves them NULL."""
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("DELETE FROM vm_adapter_requests")
        self.previous_insert('host_lifecycle',
                             {'host': 'burst', 'mode': 'available', 'actor': 'n-1',
                              'release_state': 'busy', 'hook_state': 'busy',
                              'claims_open': True},
                             added=('standby_entered_at', 'standby_exit_failed_at'))
        control = self.control_of()
        self.assertIsNone(control['standby_entered_ts'])
        self.assertIsNone(control['standby_exit_failed_ts'])
        self.assertEqual(self.standby_alerts(), [])

    def test_a_target_without_replica_returns_plain_ready(self):
        self.policy = policy()
        self.assertEqual(self.returned(), 'ready')
        self.assertNotIn(['/sortir'], self.argvs())


if __name__ == '__main__':
    unittest.main()
