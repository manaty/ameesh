# SPDX-License-Identifier: AGPL-3.0-only
"""#6074: halt the OS, prove it (stopped in place + muted SSH), then archive."""
from __future__ import annotations

from dataclasses import replace
import json
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from tests.test_5925_vm_adapter import TARGET, policy
from ameesh import host_release, storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue

FRESH = mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1})


class EnrollmentTest(unittest.TestCase):
    def test_halt_deadline_and_commands_are_explicit(self):
        self.assertEqual(vm.enrollment(policy(), local_host='controller')['halt_deadline_s'], 300)
        missing = policy()
        del missing['halt_deadline_s']
        for value in (missing, policy(halt_deadline_s=True), policy(halt_deadline_s=10),
                      policy(halt_deadline_s=3601), policy(halt_deadline_s=float('nan'))):
            with self.assertRaisesRegex(vm.Refused, 'halt deadline'):
                vm.enrollment(value, local_host='controller')
        for key in ('halt_argv', 'halted_argv'):
            for argv in (None, [], ['relative/halt'], ['/x', '']):
                bad = policy()
                bad['targets']['burst'][key] = argv
                with self.assertRaisesRegex(vm.Refused, 'fixed absolute'):
                    vm.enrollment(bad, local_host='controller')

    def test_markers_match_the_alert_reader(self):
        self.assertEqual(set(host_release.ADAPTER_HALTING), set(vm.HALTING))
        self.assertLessEqual(set(host_release.ADAPTER_AGING), set(vm.PHASES))
        self.assertLessEqual(set(host_release.ADAPTER_IMMEDIATE), set(vm.HALTING))
        self.assertEqual(host_release.ADAPTER_POWERON_REFUSED, vm.POWERON_REFUSED)

    def test_provider_reads_standby_spelling_and_allowed_actions(self):
        def api(server):
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = json.dumps({'server': dict(
                id=TARGET['server_id'], project=TARGET['project_id'], **server)}).encode()
            opener = mock.Mock()
            opener.open.return_value = response
            return vm.Scaleway('secret-token-123456', timeout=10, opener=opener)
        for spelling in ('stopped in place', 'stopped_in_place'):
            self.assertEqual(api({'state': spelling}).state(TARGET), 'stopped in place')
        self.assertEqual(api({'state': 'stopped in place', 'allowed_actions': [
            'poweron', 'poweroff']}).allowed(TARGET), {'poweron', 'poweroff'})
        # Absent or malformed: nothing is deduced as allowed.
        self.assertEqual(api({'state': 'stopped in place'}).allowed(TARGET), set())
        self.assertEqual(api({'allowed_actions': 'poweroff'}).allowed(TARGET), set())
        with self.assertRaises(vm.Refused):
            api({'state': 'stop_in_place'}).action(TARGET, 'stop_in_place')


class HaltTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM vm_adapter_requests')
        self.db.execute('DELETE FROM vm_adapter_observations')
        self.db.execute('DELETE FROM host_lifecycle')
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
        self.q.request('burst', control['generation'], control['release_epoch'], 'idle-drained')
        self.q.observe('burst', 'running', 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours'")

    def tick(self):
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            return vm.tick_host(self.cfg, self.db, policy(), 'burst', self.provider, probe=self.probe,
                                evidence=lambda *a, **k: {'idle_since': k['now'] - 7200})

    def stop(self):
        return self.db.query("SELECT * FROM vm_adapter_requests WHERE state='idle-drained' "
                             "ORDER BY generation DESC LIMIT 1")[0]

    def argvs(self):
        return [c.args[0] for c in self.probe.call_args_list]

    def halt_sent(self):
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        self.assertEqual(self.stop()['result'], 'halt intent')
        self.assertEqual(self.tick(), 'halt sent')
        self.assertEqual(self.argvs().count(['/halt']), 1)
        self.assertEqual(self.stop()['result'], 'halt sent')

    def age(self, seconds):
        self.db.execute("UPDATE vm_adapter_requests SET updated_at=clock_timestamp()"
                        "-make_interval(secs=>%s)", (float(seconds),))

    def alerts(self):
        return [(a['type'], a['reason'], a['host'], a['generation'])
                for a in host_release.alerts(self.cfg, self.db, time.time())]

    def native(self):
        control = self.q.control('burst')
        return control['mode'], control['claims_open'], control['hook_attempt_state']

    def test_two_phases_halt_then_proof_then_archive(self):
        self.halt_sent()
        # Quiesce runs before each halt-phase effect; never halt twice.
        self.assertEqual(self.argvs(), [['/quiesce'], ['/quiesce'], ['/halt']])
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'halt proven')
        self.assertEqual(self.argvs()[-1], ['/halted'])
        self.provider.action.assert_not_called()
        self.assertEqual(self.tick(), 'archiving')
        self.provider.action.assert_called_once_with(TARGET, 'poweroff')
        self.assertEqual(self.stop()['result'], 'poweroff sent')
        # A long archive of a local volume is progress, not a stall.
        self.provider.state.return_value = 'stopping'
        self.age(2000)
        self.assertEqual(self.tick(), 'provider transition')
        self.assertEqual(self.alerts(), [])
        self.provider.state.return_value = 'stopped'
        self.assertEqual(self.tick(), 'stopped')
        self.assertEqual(self.stop()['status'], 'done')
        self.assertEqual(self.provider.action.call_count, 1)

    def test_muted_ssh_while_running_never_powers_off(self):
        self.halt_sent()
        for _ in range(5):
            self.assertEqual(self.tick(), 'halt pending')
        # The SSH witness is never consulted while the provider says running.
        self.assertNotIn(['/halted'], self.argvs())
        self.provider.action.assert_not_called()
        self.assertEqual(self.native(), ('draining', False, 'idle-drained'))

    def test_deadline_alerts_names_host_and_generation_never_lost(self):
        self.halt_sent()
        self.age(301)
        self.assertEqual(self.tick(), 'halt deadline passed')
        self.assertEqual(self.tick(), 'halt deadline passed')
        self.provider.action.assert_not_called()
        self.assertEqual(self.native(), ('draining', False, 'idle-drained'))
        generation = self.stop()['generation']
        self.assertEqual(self.alerts(), [('host_release_adapter_stalled', 'halt_deadline',
                                          'burst', generation)])
        # A halt finally observed after the deadline is still our halt.
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'halt proven')

    def test_deadline_bounds_a_halt_intent_whose_quiesce_keeps_failing(self):
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        self.probe.return_value = False
        self.assertEqual(self.tick(), 'quiesce pending')
        self.age(301)
        self.assertEqual(self.tick(), 'halt deadline passed')
        self.assertNotIn(['/halt'], self.argvs())

    def test_ssh_answering_in_standby_is_no_proof(self):
        self.halt_sent()
        self.provider.state.return_value = 'stopped in place'
        self.probe.return_value = False
        self.assertEqual(self.tick(), 'halt witness pending')
        self.provider.action.assert_not_called()
        self.assertEqual(self.stop()['result'], 'halt sent')
        self.assertEqual(self.alerts(), [])
        # ameesh-cx1 B on 90572aaab: the deadline bounds the whole proof,
        # standby included; no archive without the witness.
        self.age(301)
        for _ in range(2):
            self.assertEqual(self.tick(), 'halt deadline passed')
            self.assertEqual(self.stop()['result'], 'halt deadline passed')
        self.provider.action.assert_not_called()
        self.provider.allowed.assert_not_called()
        self.assertEqual(self.alerts(), [('host_release_adapter_stalled', 'halt_deadline',
                                          'burst', self.stop()['generation'])])
        self.assertEqual(self.native(), ('draining', False, 'idle-drained'))
        # A late but complete proof is still our halt.
        self.probe.return_value = True
        self.assertEqual(self.tick(), 'halt proven')
        self.assertEqual(self.alerts(), [])

    def test_deadline_in_standby_with_complete_proof_proceeds_at_once(self):
        self.halt_sent()
        self.age(301)
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'halt proven')
        self.assertEqual(self.stop()['result'], 'archive intent')

    def test_interruption_between_phases_resumes_from_the_intent(self):
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'halt prepared')
        # halt_argv ran but its transaction was lost: only the intent remains.
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'halt proven')
        # Provider poweroff accepted, result lost: the next GET reconciles.
        self.provider.state.return_value = 'stopping'
        self.age(2000)
        self.assertEqual(self.tick(), 'halt pending')
        self.provider.action.assert_not_called()
        self.assertEqual(self.alerts(), [])
        self.provider.state.return_value = 'stopped'
        self.assertEqual(self.tick(), 'stopped')
        self.assertEqual(self.stop()['status'], 'done')

    def test_archive_not_allowed_by_server_get_alerts_without_action(self):
        self.halt_sent()
        self.provider.state.return_value = 'stopped in place'
        self.provider.allowed.return_value = {'poweron'}
        self.assertEqual(self.tick(), 'archive not allowed')
        self.assertEqual(self.tick(), 'archive not allowed')
        self.provider.action.assert_not_called()
        self.assertEqual([a[:2] for a in self.alerts()],
                         [('host_release_adapter_stalled', 'archive_refused')])
        # Allowed again (re-proven): the archive goes on, after its intent.
        self.provider.allowed.return_value = {'poweroff'}
        self.assertEqual(self.tick(), 'halt proven')
        self.assertEqual(self.tick(), 'archiving')

    def test_allowed_revoked_between_intent_and_effect(self):
        self.halt_sent()
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'halt proven')
        self.provider.allowed.return_value = set()
        self.assertEqual(self.tick(), 'archive not allowed')
        self.provider.action.assert_not_called()

    def test_return_during_halt_phase_powers_standby_back_on(self):
        self.halt_sent()
        self.q.return_intent('burst')
        # The busy request goes first; the OS halting reports nothing.
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value=None):
            self.assertEqual(self.tick(), 'fresh reading pending')
        self.assertNotIn(['/halted'], self.argvs())
        self.provider.state.return_value = 'stopped in place'
        self.provider.allowed.return_value = {'poweroff'}
        self.assertEqual(self.tick(), 'poweron not allowed')
        self.provider.action.assert_not_called()
        self.provider.allowed.return_value = {'poweron', 'poweroff'}
        self.assertEqual(self.tick(), 'starting')
        self.provider.action.assert_called_once_with(TARGET, 'poweron')
        self.assertFalse(self.q.control('burst')['claims_open'])
        # Claims still wait for running, a fresh reading, health, reconcile.
        self.provider.state.return_value = 'running'
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
        self.assertFalse(self.q.control('burst')['claims_open'])

    def test_return_blocked_in_standby_alerts_past_the_stall_bound(self):
        # #6080: standby, return due, `poweron` not allowed (or the field is
        # absent): never silent, the host is named, claims stay closed.
        self.halt_sent()
        self.q.return_intent('burst')
        self.provider.state.return_value = 'stopped in place'
        self.provider.allowed.return_value = set()
        busy = lambda: self.db.query("SELECT * FROM vm_adapter_requests "
                                     "WHERE state='busy'")[0]
        self.assertEqual(self.tick(), 'poweron not allowed')
        self.assertEqual(busy()['result'], 'poweron refused')
        self.assertEqual(self.alerts(), [])
        self.age(901)
        for _ in range(3):
            self.assertEqual(self.tick(), 'poweron not allowed')
        self.provider.action.assert_not_called()
        # The refusal is not re-committed: the row keeps aging.
        self.assertEqual(self.alerts(), [('host_release_adapter_stalled', 'poweron_refused',
                                          'burst', busy()['generation'])])
        self.assertFalse(self.q.control('burst')['claims_open'])
        # Allowed again: normal start, the marker is replaced, alert gone.
        self.provider.allowed.return_value = {'poweron'}
        self.assertEqual(self.tick(), 'starting')
        self.provider.action.assert_called_once_with(TARGET, 'poweron')
        self.assertEqual(busy()['result'], 'poweron sent')
        self.assertFalse(self.q.control('burst')['claims_open'])
        self.assertEqual(self.alerts(), [])

    def test_stopped_in_place_without_halt_intent_is_alerted_not_archived(self):
        self.assertEqual(self.tick(), 'stop prepared')
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'stopped in place without halt')
        self.assertEqual(self.tick(), 'stopped in place without halt')
        self.provider.action.assert_not_called()
        self.provider.allowed.assert_not_called()
        self.assertNotIn(['/halted'], self.argvs())
        self.assertEqual([a[:3] for a in self.alerts()],
                         [('host_release_adapter_stalled', 'stopped_in_place', 'burst')])

    def test_locked_provider_names_the_host_never_lost(self):
        self.halt_sent()
        self.provider.state.return_value = 'locked'
        self.assertEqual(self.tick(), 'halt pending')
        self.provider.action.assert_not_called()
        self.assertEqual([a[:3] for a in self.alerts()],
                         [('host_release_adapter_stalled', 'provider_locked', 'burst')])
        self.assertEqual(self.native(), ('draining', False, 'idle-drained'))

    def test_generation_change_between_phases_is_obsolete(self):
        self.halt_sent()
        self.db.execute("UPDATE host_lifecycle SET generation=generation+1 WHERE host='burst'")
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'obsolete')
        self.assertEqual(self.db.query("SELECT status FROM vm_adapter_requests")[0]['status'],
                         'obsolete')
        self.provider.action.assert_not_called()
        self.assertNotIn(['/halted'], self.argvs())


if __name__ == '__main__':
    unittest.main()
