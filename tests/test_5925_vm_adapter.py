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
from ameesh import config, host_release, registry, storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue


TARGET = dict(zone='fr-par-1', server_id='11111111-1111-4111-8111-111111111111',
              project_id='22222222-2222-4222-8222-222222222222', timezone='Europe/Paris',
              release_windows=['11:00', '23:00'], health_argv=['/health'],
              reconcile_argv=['/reconcile'], quiesce_argv=['/quiesce'],
              halt_argv=['/halt'], halted_argv=['/halted'], probe_agent='burst-probe')


def policy(**over):
    return dict(dict(enabled=True, controller_host='controller', min_on_s=3600,
                     min_off_s=60, wake_delay_s=900, observation_gap_s=90,
                     io_timeout_s=10, health_deadline_s=900, halt_deadline_s=300, targets={'burst': dict(TARGET)}), **over)


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
        for probe in (None, '', '-x', 'a b', 'x' * 65):
            bad = policy()
            bad['targets']['burst']['probe_agent'] = probe
            with self.assertRaisesRegex(vm.Refused, 'probe agent'):
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

    def test_stop_evidence_names_a_missing_canon_apart_from_an_unreadable_one(self):
        with mock.patch('ameesh.vm_adapter.host_release._canon_read', return_value=([], False)):
            with self.assertRaisesRegex(vm.Refused, '^no canon configured$'):
                vm.stop_evidence(config.Config(), mock.Mock(), 'burst', {}, now=1000)
        with mock.patch('ameesh.vm_adapter.host_release._canon_read', return_value=([], True)):
            with self.assertRaisesRegex(vm.Refused, '^canon unavailable$'):
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

    def test_elected_wake_uses_one_admitted_candidate_for_active_work(self):
        role = dict(name='role', releve='election', lease_live=False, status='idle',
                    admitted_hosts=['ag1', 'ag2'], vm_wake_mail_id=17)
        candidates = [dict(host='ag2', declared=True, admitted=True, preference_rank=1),
                      dict(host='ag1', declared=True, admitted=True, preference_rank=0)]
        choose = lambda r, lots=(), holds={}: vm.elected_wake_target(
            r, candidates, lots, {'ag1': {}, 'ag2': {}}, holds, now=1000, horizon=60)
        self.assertEqual(choose(role), 'ag1')
        self.assertEqual(choose(role, holds={'ag1': {}}), 'ag2')
        self.assertIsNone(choose(dict(role, lease_live=True)))
        self.assertIsNone(choose(dict(role, status='stopped')))
        self.assertIsNone(choose(dict(role, retired_ts=1)))
        self.assertIsNone(choose(dict(role, vm_wake_mail_id=None, passive_unread=2)))
        scheduled = [dict(assignee='role', state='build', wake_ts=1050)]
        self.assertEqual(choose(dict(role, vm_wake_mail_id=None), scheduled), 'ag1')
        self.assertIsNone(choose(dict(role, vm_wake_mail_id=None), [dict(scheduled[0], wake_ts=1061)]))
        self.assertIsNone(choose(dict(role, vm_wake_mail_id=None), [dict(scheduled[0], state='closed')]))
        self.assertEqual(vm.elected_wake_target(role, candidates, [],
                         {'ag1': {}, 'ag2': {}}, {}, now=1000, horizon=60,
                         stalled={'ag1'}), 'ag2')

    def test_elected_wake_leaves_a_ready_candidate_open_host_alone(self):
        role = dict(name='role', releve='election', lease_live=False, status='idle',
                    admitted_hosts=['ag1', 'ag2'], vm_wake_mail_id=17)
        vms = [dict(host='ag1', declared=True, admitted=True, preference_rank=0),
               dict(host='ag2', declared=True, admitted=True, preference_rank=1)]
        hub = dict(host='hub', declared=True, admitted=True, ready=True)

        def choose(candidates, **extra):
            return vm.elected_wake_target(role, candidates, [], {'ag1': {}, 'ag2': {}},
                                          {}, now=1000, horizon=60, **extra)

        self.assertEqual(choose(vms + [hub]), 'ag1')
        # #8121: un candidat prêt sur un hôte aux prises ouvertes (le hub,
        # jamais une cible de l'adaptateur) prend le rôle par élection.
        self.assertIsNone(choose(vms + [hub], open_hosts={'hub'}))
        # Seul declared+admitted+ready retient : la place des autres candidats
        # endormis reste l'évidence avant l'allumage.
        for absent in ('declared', 'admitted', 'ready'):
            self.assertEqual(choose(vms + [dict(hub, **{absent: False})],
                                    open_hosts={'hub'}), 'ag1')
        # Un hôte fermé (la cible drainée elle-même) ne retient pas le réveil.
        self.assertEqual(choose(vms + [hub], open_hosts=set()), 'ag1')

    def test_stalled_busy_return_allows_reserve_after_bounded_wait(self):
        queue = mock.Mock()
        queue.pending_all.side_effect = lambda host: ([dict(state='busy', created_ts=850,
                                                             generation=1, release_epoch=1)]
                                                       if host == 'ag1' else [])
        controls = {'ag1': dict(mode='returning', claims_open=False,
                                generation=1, release_epoch=1)}
        self.assertEqual(vm.stalled_role_wakes(queue, ['ag1', 'ag2'], controls, now=999), set())
        self.assertEqual(vm.stalled_role_wakes(queue, ['ag1', 'ag2'], controls, now=1000), {'ag1'})
        controls['ag1']['generation'] = 2
        self.assertEqual(vm.stalled_role_wakes(queue, ['ag1', 'ag2'], controls, now=1000), set())

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
        self.provider.allowed.return_value = {'poweron', 'poweroff'}
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

    def tick(self, target=None, **extra):
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            return vm.tick_host(self.cfg, self.db,
                                policy(targets={'burst': dict(target or TARGET)}), 'burst', self.provider,
                                probe=self.probe, evidence=lambda *a, **k: {'idle_since': k['now']-7200}, **extra)

    def observe_old(self, state='running'):
        self.q.observe('burst', state, 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours',"
                        "off_since=clock_timestamp()-interval '2 hours'")

    def test_active_mail_for_lease_free_role_starts_candidate(self):
        self.operations.listing.return_value = [dict(
            name='role', releve='election', lease_live=False, status='idle',
            admitted_hosts=['burst'], vm_wake_mail_id=17)]
        self.provider.state.return_value = 'stopped'
        self.observe_old('stopped')
        status = dict(holder=None, candidates=[dict(host='burst', declared=True,
                                                    admitted=True, preference_rank=0)])
        with mock.patch('ameesh.vm_adapter.election.status', return_value=status):
            self.assertEqual(self.tick(), 'starting')
        self.provider.action.assert_called_once_with(TARGET, 'poweron')
        event = self.db.query("SELECT data FROM ameesh_events WHERE type='vm.elected_wake' "
                              "AND object='burst' ORDER BY id DESC LIMIT 1")[0]['data']
        self.assertEqual((event['role'], event['mail_id']), ('role', 17))

    def test_ready_hub_candidate_does_not_wake_the_drained_vm(self):
        # #8121: le hub (jamais une cible de l'adaptateur) est prêt et ses
        # prises sont ouvertes ; le candidat de la VM endormie n'a pas de
        # relevé frais, mais son placement déclaré ne doit pas la rallumer.
        self.operations.listing.return_value = [dict(
            name='role', releve='election', lease_live=False, status='idle',
            admitted_hosts=['burst', 'hub'], vm_wake_mail_id=17)]
        self.provider.state.return_value = 'stopped'
        self.observe_old('stopped')
        before = self.db.query("SELECT COALESCE(max(id), 0) AS id FROM ameesh_events")[0]['id']
        status = dict(holder=None, candidates=[
            dict(host='hub', declared=True, admitted=True, ready=True),
            dict(host='burst', declared=True, admitted=True,
                 preference_rank=0, ready=False)])
        with mock.patch('ameesh.vm_adapter.election.status', return_value=status):
            self.assertEqual(self.tick(), 'no request')
        self.provider.action.assert_not_called()
        self.assertEqual(self.q.pending('burst'), [])
        self.assertEqual(self.db.query("SELECT * FROM ameesh_events "
                                       "WHERE type='vm.elected_wake' AND object='burst' "
                                       "AND id > %s", (before,)), [])

    def test_listing_ignores_passive_events_for_vm_wake(self):
        registry.upsert(self.db, 'vm-wake-role', harness='claude', host='burst')
        def deposit(kind, payload):
            return self.db.query("INSERT INTO agent_mailbox(sender,recipient,body,kind,payload) "
                                 "VALUES ('sender','vm-wake-role','test',%s,%s::jsonb) RETURNING id",
                                 (kind, json.dumps(payload)))[0]['id']
        def row():
            return next(r for r in storage.of(self.db).operations.listing()
                        if r['name'] == 'vm-wake-role')
        deposit('event', {})
        deposit('request', {'info': True})
        self.assertIsNone(row()['vm_wake_mail_id'])
        active = deposit('request', {})
        self.assertEqual(row()['vm_wake_mail_id'], active)
        self.db.execute("UPDATE agent_mailbox SET delivered_at=now() WHERE id=%s", (active,))
        urgent = deposit('event', {'urgent': True})
        self.assertEqual(row()['vm_wake_mail_id'], urgent)

    def test_role_return_is_recorded_before_provider_failure(self):
        self.operations.listing.return_value = [dict(
            name='role', releve='election', lease_live=False, status='idle',
            admitted_hosts=['burst'], vm_wake_mail_id=17)]
        self.provider.state.side_effect = vm.Refused('provider unavailable')
        status = dict(holder=None, candidates=[dict(host='burst', declared=True,
                                                    admitted=True, preference_rank=0)])
        with mock.patch('ameesh.vm_adapter.election.status', return_value=status):
            with self.assertRaises(vm.Refused):
                self.tick()
        self.assertEqual(self.q.pending('burst')[0]['state'], 'busy')

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
        # #6074: halt the OS first; the provider poweroff archives only after
        # `stopped in place` and the muted SSH witness prove the halt.
        self.assertEqual(self.tick(), 'halt prepared')
        self.assertEqual(self.tick(), 'halt sent')
        self.probe.assert_called_with(['/halt'], 10)
        self.provider.action.assert_not_called()
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'halt proven')
        self.assertEqual(self.tick(), 'archiving')
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
                                          self.provider, probe=self.probe), 'halt prepared')
            self.assertEqual(vm.tick_host(self.cfg, self.db, policy(), 'burst',
                                          self.provider, probe=self.probe), 'halt sent')
            self.provider.action.assert_not_called()

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
            self.assertEqual(self.tick(), 'probe admitted')
            self.probe.assert_not_called()
            self.probe.side_effect = [True, False]
            self.assertEqual(self.tick(), 'reconciliation pending')
            self.assertEqual(self.request('busy'), 'pending')
            self.probe.side_effect = None
            self.assertEqual(self.tick(), 'ready')
        self.assertEqual(self.request('busy'), 'done')
        self.assertFalse(self.q.control('burst')['claims_open'])
        self.assertIsNotNone(self.db.query('SELECT wake_delay_s FROM vm_adapter_observations')[0]['wake_delay_s'])

    def test_first_tick_reports_exact_short_drain_without_blocking_ack(self):
        self.db.execute("UPDATE host_lifecycle SET return_at=clock_timestamp()+interval '40 minutes' "
                        "WHERE host='burst'")
        self.assertEqual(self.tick(), 'no request')
        control = self.q.control('burst')
        short = self.q.short_drain('burst', control['generation'], control['release_epoch'])
        self.assertEqual(short['result'], 'drain too short for halt: needs 4500 s')
        self.assertEqual(self.q.status('burst', control['generation'], control['release_epoch'],
                                       'draining'), 'done')
        self.assertEqual(self.tick(), 'no request')
        self.assertEqual(self.q.short_drain('burst', control['generation'],
                                            control['release_epoch'])['result'], short['result'])
        self.provider.action.assert_not_called()

    def test_existing_draining_request_keeps_short_warning_when_acknowledged(self):
        self.db.execute("UPDATE host_lifecycle SET return_at=clock_timestamp()+interval '40 minutes' "
                        "WHERE host='burst'")
        self.request('draining')
        self.assertEqual(self.tick(), 'draining')
        control = self.q.control('burst')
        self.assertEqual(self.q.short_drain('burst', control['generation'],
                                            control['release_epoch'])['result'],
                         'drain too short for halt: needs 4500 s')

    def test_forced_drain_records_reason(self):
        row = storage.of(self.db).host_lifecycle.drain(
            'another', actor='human:op', duration=1800, force_short_reason='incident')
        event = self.db.query("SELECT data FROM ameesh_events WHERE type='host.drain_short_forced' "
                              "AND object='host:another' ORDER BY id DESC LIMIT 1")[0]['data']
        self.assertEqual(event['reason'], 'incident')
        self.assertEqual(event['generation'], row['generation'])

    def test_first_tick_accepts_exact_worker_policy(self):
        self.db.execute("UPDATE host_lifecycle SET return_at=clock_timestamp()+interval '2 hours' "
                        "WHERE host='burst'")
        self.assertEqual(self.tick(), 'no request')
        control = self.q.control('burst')
        self.assertIsNone(self.q.short_drain('burst', control['generation'], control['release_epoch']))

    def test_canon_failure_is_recorded_and_cleared_on_recovery(self):
        self.request()
        self.observe_old()
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            self.assertEqual(vm.tick_host(
                self.cfg, self.db, policy(), 'burst', self.provider,
                probe=self.probe, evidence=mock.Mock(side_effect=vm.Refused('canon unavailable'))),
                'canon unavailable')
        self.assertEqual(self.q.pending('burst')[0]['result'], 'canon unavailable')
        self.provider.action.assert_not_called()
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertNotEqual(self.q.pending('burst')[0]['result'], 'canon unavailable')

    def test_missing_canon_is_recorded_distinctly_and_cleared_on_recovery(self):
        self.request()
        self.observe_old()
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            self.assertEqual(vm.tick_host(
                self.cfg, self.db, policy(), 'burst', self.provider,
                probe=self.probe, evidence=mock.Mock(side_effect=vm.Refused('no canon configured'))),
                'no canon configured')
        self.assertEqual(self.q.pending('burst')[0]['result'], 'no canon configured')
        self.provider.action.assert_not_called()
        self.assertEqual(self.tick(), 'stop prepared')
        self.assertNotEqual(self.q.pending('burst')[0]['result'], 'no canon configured')

    def test_deadline_records_why_stop_was_superseded(self):
        self.request()
        self.db.execute("UPDATE host_lifecycle SET return_at=clock_timestamp()+interval '5 minutes' "
                        "WHERE host='burst'")
        self.operations.listing.return_value = [dict(name='worker', admitted_hosts=['burst'],
                                                      releve=None, mode='execute', status='idle')]
        # #8136: the only wake reason must be the return deadline. The enrolled
        # release windows are wall-clock inputs: a run inside the 15 min horizon
        # of 11:00/23:00 Paris made `due` true for a release window too, so the
        # deadline-only branch was skipped and the row stayed pending (CI run
        # 45149593735, 23/11 09:44Z). No window, no measured CI queue here.
        target = dict(TARGET, release_windows=[])
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value=None), \
                mock.patch('ameesh.vm_adapter.host_release.ci_wake_evidence',
                           return_value={'state': 'inert'}):
            self.tick(target=target)
        rows = self.db.query("SELECT status,result FROM vm_adapter_requests WHERE state='idle-drained'")
        self.assertEqual(rows[0]['status'], 'obsolete')
        self.assertEqual(rows[0]['result'], 'return deadline within wake horizon')
        self.provider.action.assert_not_called()

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
        self.assertEqual(self.tick(), 'halt prepared')
        self.assertEqual(self.tick(), 'halt sent')
        self.provider.state.return_value = 'stopped in place'
        self.assertEqual(self.tick(), 'halt proven')
        self.provider.action.side_effect = vm.Refused('provider unavailable')
        with self.assertRaises(vm.Refused):
            self.tick()
        self.assertEqual(self.request(), 'pending')
        self.assertEqual(self.q.pending('burst')[0]['result'], 'archive intent')
        self.assertFalse(self.q.control('burst')['claims_open'])
        self.provider.state.return_value = 'stopped'
        self.assertEqual(self.tick(), 'stopped')
        self.assertEqual(self.request(), 'done')

    def test_observation_gap_resets_hysteresis(self):
        self.observe_old()
        before = self.q.observations()[0]['on_ts']
        self.db.execute("UPDATE vm_adapter_observations SET updated_at=clock_timestamp()-interval '5 minutes'")
        self.request()
        self.assertEqual(self.tick(), 'on hysteresis')
        self.provider.action.assert_not_called()
        self.assertGreater(self.q.observations()[0]['on_ts'], before)

    def test_idle_guard_refusal_keeps_the_tick_observation_and_hysteresis(self):
        # #8382: the idle guard refuses inside the tick's transaction; its
        # rollback used to drop the provider observation of the tick, so a
        # drainage that waited for leases starved the 90 s continuity and
        # restarted `on_since` despite a healthy provider and worker.
        self.request()
        self.observe_old('running')
        before = self.q.observations()[0]
        self.db.execute("UPDATE vm_adapter_observations SET updated_at="
                        "clock_timestamp()-interval '80 seconds' WHERE host='burst'")
        stale = self.q.observations()[0]
        refusal = mock.Mock(side_effect=vm.Refused(
            'host no longer idle: a lease is still held: impl-01'))
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            with self.assertRaisesRegex(vm.Refused, 'host no longer idle'):
                vm.tick_host(self.cfg, self.db, policy(), 'burst', self.provider,
                             probe=self.probe, evidence=refusal)
        after = self.q.observations()[0]
        self.assertEqual(after['provider_state'], 'running')
        # 80 s < observation_gap_s (90 s): the refusal extended the observed
        # interval instead of being treated as a gap.
        self.assertEqual(after['on_ts'], before['on_ts'])
        self.assertGreater(after['seen_ts'], stale['seen_ts'] + 60)
        # No intention, probe or provider order left the rolled-back tick.
        control = self.q.control('burst')
        self.assertIsNone(control['hook_attempt_state'])
        self.assertIsNone(self.q.pending('burst')[0]['result'])
        self.provider.action.assert_not_called()
        self.probe.assert_not_called()
        # The unchanged two-hour `on_since` still carries the hysteresis.
        self.assertEqual(self.tick(), 'stop prepared')
        self.provider.action.assert_not_called()

    def test_reconciliation_runs_outside_gate(self):
        self.request('busy')
        self.observe_old()
        other = self.connect()
        locks = []
        def reconcile(argv, timeout, **_):
            with other.transaction() as tx:
                locks.append(tx.query(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS ok",
                    ('host-gate:burst',))[0]['ok'])
            return True
        self.probe.side_effect = reconcile
        try:
            with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1}):
                self.assertEqual(self.tick(), 'probe admitted')
                self.assertEqual(self.tick(), 'ready')
            self.assertEqual(locks, [True, True])
        finally:
            other.close()

    def test_generation_change_during_health_invalidates_completion(self):
        self.request('busy')
        self.observe_old()
        def changed(argv, timeout, **_):
            self.db.execute("UPDATE host_lifecycle SET generation=generation+1 WHERE host='burst'")
            return True
        self.probe.side_effect = changed
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1}):
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'obsolete')
        self.assertIsNone(self.q.return_probe('burst'))
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

    def admitted(self, agent):
        return self.db.query('SELECT ameesh_agent_claims_open(%s,%s) AS ok',
                             ('burst', agent))[0]['ok']

    def test_6017_wake_admits_probe_alone_then_busy_reopens_and_clears_it(self):
        self.request('busy')
        self.observe_old()
        self.assertFalse(self.admitted('burst-probe'))
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1}):
            self.assertEqual(self.tick(), 'probe admitted')
            self.probe.assert_not_called()
            control = self.q.control('burst')
            self.assertEqual((control['mode'], control['claims_open']), ('returning', False))
            self.assertTrue(self.admitted('burst-probe'))
            self.assertFalse(self.admitted('other-agent'))
            self.assertEqual(self.tick(), 'ready')
        self.assertEqual(self.request('busy'), 'done')
        control = self.q.control('burst')
        reopened = storage.of(self.db).host_lifecycle.reopen_return(
            'burst', generation=control['generation'],
            release_epoch=control['release_epoch'], actor='scanner')
        self.assertTrue(reopened['claims_open'])
        self.assertIsNone(self.q.return_probe('burst'))
        self.assertTrue(self.admitted('other-agent'))

    def test_6017_failed_health_returns_to_draining_without_loss(self):
        self.request('busy')
        self.observe_old()
        self.probe.return_value = False
        with mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1}):
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health failed')
            control = self.q.control('burst')
            self.assertEqual((control['mode'], control['claims_open']), ('draining', False))
            self.assertIsNone(self.q.return_probe('burst'))
            self.assertFalse(self.admitted('burst-probe'))
            self.assertEqual(self.request('busy'), 'pending')
            # The return is still due: the next tick admits the probe again.
            self.assertEqual(self.tick(), 'probe admitted')
        self.probe.assert_called_once()

    def test_6017_draining_lost_and_live_fence_keep_return_probe_closed(self):
        self.db.execute("UPDATE host_lifecycle SET return_probe='burst-probe' WHERE host='burst'")
        self.assertFalse(self.admitted('burst-probe'))  # draining: trigger cleared it
        self.assertIsNone(self.q.return_probe('burst'))
        self.db.execute("UPDATE host_lifecycle SET mode='returning',return_probe='burst-probe' "
                        "WHERE host='burst'")
        self.assertTrue(self.admitted('burst-probe'))
        self.db.execute("INSERT INTO host_release_operations(operation,host,manifest_digest) "
                        "VALUES ('op-6017','burst',repeat('a',64))")
        self.db.execute("INSERT INTO host_release_fences(host,operation,probe) "
                        "VALUES ('burst','op-6017','fence-probe')")
        try:
            self.assertFalse(self.admitted('burst-probe'))
            self.assertTrue(self.admitted('fence-probe'))
        finally:
            self.db.execute("DELETE FROM host_release_fences WHERE host='burst'")
            self.db.execute("DELETE FROM host_release_operations WHERE operation='op-6017'")
        self.assertTrue(self.admitted('burst-probe'))
        self.db.execute("UPDATE host_lifecycle SET mode='lost' WHERE host='burst'")
        self.assertFalse(self.admitted('burst-probe'))
        self.assertIsNone(self.q.return_probe('burst'))
        self.db.execute("UPDATE host_lifecycle SET mode='returning',return_probe='burst-probe' "
                        "WHERE host='burst'")
        self.db.execute("UPDATE host_lifecycle SET release_epoch=release_epoch+1 WHERE host='burst'")
        self.assertIsNone(self.q.return_probe('burst'))
