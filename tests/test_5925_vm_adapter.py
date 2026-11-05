# SPDX-License-Identifier: AGPL-3.0-only
"""#5925: simulated cloud, generation fencing, hysteresis, wake and health."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from ameesh import config, host_release, storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue


TARGET = dict(zone='fr-par-1', server_id='11111111-1111-4111-8111-111111111111',
              project_id='22222222-2222-4222-8222-222222222222', timezone='Europe/Paris',
              release_windows=['11:00', '23:00'], health_argv=['/health'],
              reconcile_argv=['/reconcile'], quiesce_argv=['/quiesce'])


def policy(**over):
    return dict(dict(enabled=True, controller_host='controller', min_on_s=3600,
                     min_off_s=60, wake_delay_s=900, observation_gap_s=90,
                     io_timeout_s=10, targets={'burst': dict(TARGET)}), **over)


class DecisionTest(unittest.TestCase):
    def test_enrollment_is_explicit_single_host_and_no_self_shutdown(self):
        self.assertEqual(vm.enrollment(policy(), local_host='controller')['wake_delay_s'], 900)
        for value in (policy(enabled=False), policy(controller_host='elsewhere'),
                      policy(io_timeout_s=11), policy(min_on_s=True),
                      policy(min_off_s=float('nan')), policy(targets={'controller': TARGET})):
            with self.assertRaises(vm.Refused):
                vm.enrollment(value, local_host='controller')
        bad = policy()
        bad['targets']['burst']['health_argv'] = ['relative/script']
        with self.assertRaises(vm.Refused):
            vm.enrollment(bad, local_host='controller')
        default = policy()
        del default['wake_delay_s']
        self.assertEqual(vm.enrollment(default, local_host='controller')['wake_delay_s'], 900)

    def test_private_files_reject_links_and_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secret'
            path.write_text('{"secret_key":"never print"}')
            path.chmod(0o600)
            self.assertEqual(vm.private_json(path)['secret_key'], 'never print')
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                vm.private_json(link)
            path.chmod(0o644)
            with self.assertRaises(vm.Refused):
                vm.private_json(path)

    def test_http_actions_only_poweron_poweroff_identity_bound_and_errors_redacted(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'server': dict(id=TARGET['server_id'],
            project=TARGET['project_id'], state='stopped')}).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        api = vm.Scaleway('secret-token-123456', timeout=10, opener=opener)
        self.assertEqual(api.state(TARGET), 'stopped')
        api.action(TARGET, 'poweroff')
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), {'action': 'poweroff'})
        self.assertIn('/instance/v1/zones/fr-par-1/servers/', request.full_url)
        for action in ('terminate', 'stop_in_place', 'reboot'):
            with self.assertRaises(vm.Refused):
                api.action(TARGET, action)
        opener.open.side_effect = RuntimeError('secret-token-123456')
        with self.assertRaises(vm.Refused) as error:
            api.state(TARGET)
        self.assertNotIn('secret-token', str(error.exception))

    def test_probe_inherits_no_cloud_credential_and_suppresses_output(self):
        with mock.patch.dict(os.environ, {'SCW_SECRET_KEY': 'secret'}), \
                mock.patch('ameesh.vm_adapter.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(vm.run_probe(['/health', 'fixed-host'], 10))
        self.assertNotIn('SCW_SECRET_KEY', run.call_args.kwargs['env'])
        self.assertNotIn('CREDENTIALS_DIRECTORY', run.call_args.kwargs['env'])
        self.assertEqual(run.call_args.args[0], ['/health', 'fixed-host'])
        self.assertEqual(run.call_args.kwargs['stdout'], vm.subprocess.DEVNULL)

    def test_leader_and_ready_reserve_are_protected_even_with_declared_baseline(self):
        fp = dict(capacity='burst', unknown=[], state='idle')
        store = mock.Mock()
        store.operations.listing.return_value = []
        with mock.patch('ameesh.vm_adapter.host_release._canon_read', return_value=([object()], False)), \
                mock.patch('ameesh.vm_adapter.storage.of', return_value=store), \
                mock.patch('ameesh.vm_adapter.host_idle.snapshot', return_value={'burst': fp}), \
                mock.patch('ameesh.vm_adapter.host_lifecycle.critical_names', return_value=['role']), \
                mock.patch('ameesh.vm_adapter.election.status', return_value={
                    'holder': 'leader', 'candidates': [{'host': 'burst', 'ready': True}]}):
            with self.assertRaisesRegex(vm.Refused, 'reserve protected'):
                vm.stop_evidence(config.Config(), mock.Mock(), 'burst', {}, now=1000)

    def test_adapter_hysteresis_does_not_change_native_drain_timeout(self):
        cfg = config.Config()
        control = dict(host='burst', requested_ts=1000)
        reading = dict(sampled_ts=5000, turns_in_progress=0)
        fp = dict(runs=0, runs_source='host measure')
        self.assertEqual(vm.host_release.release_blocker(control, [], reading,
            footprint=fp, cfg=cfg, now=5000), 'drain is overdue')
        self.assertIsNone(vm.host_release.release_blocker(control, [], reading,
            footprint=fp, cfg=cfg, now=5000, adapter_wait=True))

    def test_wake_before_lot_and_release_but_not_when_work_can_run_elsewhere(self):
        now = datetime(2026, 11, 5, 9, 45, tzinfo=timezone.utc).timestamp()  # Paris 10:45
        control = dict(host='burst', mode='draining')
        controls = {'burst': control}
        listing = [dict(name='agent', host='burst', admitted_hosts=['burst'], mode='execute', status='idle')]
        args = dict(now=now, horizon=900, target=TARGET)
        self.assertTrue(vm.wake_due('burst', control, listing, [], controls, {}, **args))
        target = dict(TARGET, release_windows=[])
        args['target'] = target
        lots = [dict(assignee='agent', state='intake', wake_ts=now+899)]
        self.assertTrue(vm.wake_due('burst', control, listing, lots, controls, {}, **args))
        self.assertFalse(vm.wake_due('burst', control, listing, lots, controls, {'burst': {}}, **args))
        listing[0]['admitted_hosts'].append('other')
        self.assertFalse(vm.wake_due('burst', control, listing, lots, controls, {}, **args))
        listing[0]['admitted_hosts'] = ['burst']
        listing[0]['actionable_lots'] = 1
        self.assertTrue(vm.wake_due('burst', control, listing, [], controls, {}, **args))


class AdapterTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM vm_adapter_requests')
        self.db.execute('DELETE FROM vm_adapter_observations')
        self.db.execute('DELETE FROM host_lifecycle')
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,release_state,hook_state) "
                        "VALUES ('burst','draining','tester',false,'draining','draining')")
        self.q = Queue(self.db)
        self.cfg = replace(self.cfg, idle_after_s=1800)
        self.provider = mock.Mock()
        self.provider.state.return_value = 'running'
        self.operations = mock.Mock()
        self.operations.listing.return_value = []
        self.operations.open_lots_activity.return_value = []
        self.hosts = mock.Mock()
        self.hosts.holds.return_value = []
        real = storage.of(self.db)
        self.store = SimpleNamespace(host_lifecycle=real.host_lifecycle,
                                     operations=self.operations, hosts=self.hosts)
        self.probe = mock.Mock(return_value=True)

    def request(self, state='idle-drained'):
        control = self.q.control('burst')
        return self.q.request('burst', control['generation'], control['release_epoch'], state)

    def tick(self, **extra):
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            return vm.tick_host(self.cfg, self.db, policy(), 'burst', self.provider,
                               probe=self.probe, evidence=lambda *a, **k: {'idle_since': k['now']-7200}, **extra)

    def observe_old(self, state='running'):
        self.q.observe('burst', state, 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours',"
                        "off_since=clock_timestamp()-interval '2 hours'")

    def test_queue_idempotence_and_generation_fence(self):
        self.assertEqual(self.request(), 'pending')
        self.assertEqual(self.request(), 'pending')
        self.assertEqual(len(self.db.query('SELECT * FROM vm_adapter_requests')), 1)
        self.db.execute("UPDATE host_lifecycle SET generation=generation+1 WHERE host='burst'")
        self.assertEqual(self.tick(), 'obsolete')
        self.provider.action.assert_not_called()
        self.assertEqual(self.db.query('SELECT status FROM vm_adapter_requests')[0]['status'], 'obsolete')

    def test_initial_observation_and_minimum_on_do_not_stop(self):
        self.request()
        self.assertEqual(self.tick(), 'on hysteresis')
        self.provider.action.assert_not_called()
        self.observe_old()
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertEqual(self.tick(), 'stopping')
        self.provider.action.assert_called_once_with(TARGET, 'poweroff')
        self.assertEqual(self.q.pending('burst')[0]['status'], 'pending')
        self.provider.state.return_value = 'stopped'
        self.assertEqual(self.tick(), 'stopped')
        self.assertEqual(self.request(), 'done')

    def test_real_stop_evidence_covers_hysteresis_and_rejects_busy_or_missing_samples(self):
        # Use the real snapshot/project path, not the injected idle_since of tick().
        self.request()
        self.observe_old()
        self.db.execute("UPDATE host_lifecycle SET requested_at=clock_timestamp()-interval '2 hours'")
        self.cfg = replace(self.cfg, baseline_hosts=0, runs_interfaces={'burst': 'none'})
        age = [2400]
        busy = [False]
        gap = [False]

        def reading(stamp):
            return dict(sampled_ts=stamp, turns_in_progress=0, cpu_count=8,
                        mem_total_bytes=16000, mem_available_bytes=12000,
                        app_cpu=0.25, cgroup={'container': False, 'limits_readable': True})

        def history(host, seconds):
            now = self.q.now()
            rows = [reading(now - offset) for offset in range(0, int(min(seconds, age[0])) + 1, 60)]
            if busy[0]:
                rows[10]['turns_in_progress'] = 1
            if gap[0]:
                rows = rows[:10] + rows[15:]
            return rows

        self.hosts.latest.side_effect = lambda host: reading(self.q.now() - 1)
        self.hosts.since.side_effect = history
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store), \
                mock.patch('ameesh.vm_adapter.host_release._canon_read',
                           return_value=([SimpleNamespace(hosts=[])], False)), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=[]):
            self.assertEqual(vm.tick_host(self.cfg, self.db, policy(), 'burst',
                                          self.provider, probe=self.probe), 'on hysteresis')
            self.hosts.since.assert_called_with('burst', 3780)
            self.provider.action.assert_not_called()
            age[0] = 4200
            for flag in (busy, gap):
                flag[0] = True
                with self.assertRaisesRegex(vm.Refused, 'host no longer idle'):
                    vm.tick_host(self.cfg, self.db, policy(), 'burst', self.provider, probe=self.probe)
                self.provider.action.assert_not_called()
                flag[0] = False
            self.assertEqual(vm.tick_host(self.cfg, self.db, policy(), 'burst',
                                          self.provider, probe=self.probe), 'stop prepared')
            self.assertEqual(vm.tick_host(self.cfg, self.db, policy(), 'burst',
                                          self.provider, probe=self.probe), 'stopping')
            self.provider.action.assert_called_once_with(TARGET, 'poweroff')

    def test_off_hysteresis_and_running_requires_fresh_reading_health_reconciliation(self):
        self.request('busy')
        self.provider.state.return_value = 'stopped'
        self.assertEqual(self.tick(), 'off hysteresis')
        self.provider.action.assert_not_called()
        self.observe_old('stopped')
        self.assertEqual(self.tick(), 'starting')
        self.provider.action.assert_called_once_with(TARGET, 'poweron')
        self.provider.state.return_value = 'running'
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value=None):
            self.assertEqual(self.tick(), 'fresh reading pending')
        self.probe.assert_not_called()
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1}):
            self.probe.side_effect = [True, False]
            self.assertEqual(self.tick(), 'reconciliation pending')
            self.assertEqual(self.request('busy'), 'pending')
            self.probe.side_effect = None
            self.assertEqual(self.tick(), 'ready')
        self.assertEqual(self.request('busy'), 'done')
        self.assertFalse(self.q.control('burst')['claims_open'])
        self.assertIsNotNone(self.db.query('SELECT wake_delay_s FROM vm_adapter_observations')[0]['wake_delay_s'])

    def test_operator_return_supersedes_queued_stop(self):
        self.request()
        self.observe_old()
        self.q.return_intent('burst')
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value=None):
            self.assertEqual(self.tick(), 'fresh reading pending')
        self.provider.action.assert_not_called()
        self.assertFalse(self.q.control('burst')['claims_open'])

    def test_holds_and_open_claims_block_every_cloud_effect(self):
        self.request()
        self.hosts.holds.return_value = [{'host': 'burst'}]
        self.assertEqual(self.tick(), 'held')
        self.provider.state.assert_not_called()
        self.hosts.holds.return_value = []
        self.db.execute("UPDATE host_lifecycle SET claims_open=true WHERE host='burst'")
        self.assertEqual(self.tick(), 'inactive')
        self.provider.action.assert_not_called()

    def test_cloud_timeout_does_not_acknowledge_and_next_state_reconciles(self):
        self.request()
        self.observe_old()
        self.assertEqual(self.tick(), 'stop prepared')
        self.provider.action.side_effect = vm.Refused('provider unavailable')
        with self.assertRaises(vm.Refused):
            self.tick()
        self.assertEqual(self.request(), 'pending')
        self.assertFalse(self.q.control('burst')['claims_open'])
        self.provider.state.return_value = 'stopped'
        self.assertEqual(self.tick(), 'stopped')
        self.assertEqual(self.request(), 'done')

    def test_observation_gap_resets_hysteresis(self):
        self.observe_old()
        self.db.execute("UPDATE vm_adapter_observations SET updated_at=clock_timestamp()-interval '5 minutes'")
        self.request()
        self.assertEqual(self.tick(), 'on hysteresis')
        self.provider.action.assert_not_called()

    def test_reconciliation_runs_outside_gate(self):
        self.request('busy')
        self.observe_old()
        other = self.connect()
        locks = []
        def reconcile(argv, timeout):
            with other.transaction() as tx:
                locks.append(tx.query(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok",
                    ('host-gate:burst',))[0]['ok'])
            return True
        self.probe.side_effect = reconcile
        try:
            with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1}):
                self.assertEqual(self.tick(), 'ready')
            self.assertEqual(locks, [True, True])
        finally:
            other.close()

    def test_generation_change_during_health_invalidates_completion(self):
        self.request('busy')
        self.observe_old()
        def changed(argv, timeout):
            self.db.execute("UPDATE host_lifecycle SET generation=generation+1 WHERE host='burst'")
            return True
        self.probe.side_effect = changed
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1}):
            self.assertEqual(self.tick(), 'obsolete')
        self.provider.action.assert_not_called()
        self.assertFalse(self.q.control('burst')['claims_open'])

    def test_real_hook_only_acknowledges_completed_current_attempt(self):
        self.db.execute("UPDATE host_lifecycle SET hook_state='busy' WHERE host='burst'")
        control = self.q.control('burst')
        native = storage.of(self.db).host_lifecycle
        attempt = native.begin_transition('burst', control['generation'], 'draining',
                                          release_epoch=control['release_epoch'], actor='tester',
                                          stale_after=420, retry_after=0)
        self.assertIsNotNone(attempt)
        with mock.patch('ameesh.vm_adapter.config.load', return_value=self.cfg), \
                mock.patch('ameesh.vm_adapter.db_mod.connect', return_value=self.db), \
                mock.patch.object(self.db, 'close'), \
                mock.patch.dict(os.environ, {'AMEESH_HOST_GENERATION': str(control['generation'])}):
            self.assertEqual(vm.main(['request', 'draining', 'burst']),
                             host_release.ADAPTER_WAIT_CODE)
            self.assertEqual(self.tick(), 'native hook in flight')
            # The scanner records the explicit wait; the worker then owns the
            # completion, under the same generation/epoch (#5932).
            waited = native.wait_transition(
                'burst', control['generation'], 'draining',
                release_epoch=control['release_epoch'], actor='tester',
                detail={'command': 'adapter'})
            self.assertEqual(waited['hook_wait_state'], 'draining')
            self.assertIsNone(waited['hook_attempt_state'])
            self.assertEqual(self.tick(), 'draining')
            self.assertEqual(vm.main(['request', 'draining', 'burst']), 0)
            with mock.patch.dict(os.environ, {'AMEESH_HOST_GENERATION': str(control['generation']+1)}):
                self.assertEqual(vm.main(['request', 'draining', 'burst']), 1)
        self.provider.action.assert_not_called()
        self.assertFalse(self.q.control('burst')['claims_open'])

    def test_stop_intent_is_durable_and_completion_updates_native_journal(self):
        self.request()
        self.observe_old()
        self.assertEqual(self.tick(), 'stop prepared')
        self.provider.action.assert_not_called()
        self.assertEqual(self.q.control('burst')['hook_attempt_state'], 'idle-drained')
        self.provider.state.return_value = 'stopping'
        self.assertEqual(self.tick(), 'provider transition')
        self.assertEqual(self.q.control('burst')['hook_attempt_state'], 'idle-drained')
        self.provider.state.return_value = 'stopped'
        self.assertEqual(self.tick(), 'stopped')
        self.assertEqual(self.q.control('burst')['release_state'], 'idle-drained')
        self.assertIsNotNone(self.q.control('burst')['released_ts'])
        rows = self.db.query("SELECT state FROM host_lifecycle_transitions WHERE host='burst'")
        self.assertEqual(rows, [{'state': 'idle-drained'}])

    def test_scanner_does_not_retry_owned_stop_after_attempt_timeout(self):
        self.request()
        self.observe_old()
        self.assertEqual(self.tick(), 'stop prepared')
        self.db.execute("UPDATE host_lifecycle SET hook_attempt_at=clock_timestamp()-interval '5 hours'")
        c = self.q.control('burst')
        attempt = storage.of(self.db).host_lifecycle.begin_transition(
            'burst', c['generation'], 'idle-drained', release_epoch=c['release_epoch'],
            actor='scanner', stale_after=420, retry_after=0)
        self.assertIsNone(attempt)
        self.assertEqual(self.q.control('burst')['hook_attempt_state'], 'idle-drained')
