# SPDX-License-Identifier: AGPL-3.0-only
"""Idle is evidence over time, never the absence of a current measurement."""
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import config, host_idle, storage

NOW = 10000


class HostIdleTest(unittest.TestCase):
    def reading(self, **extra):
        return dict(dict(sampled_ts=NOW, turns_in_progress=0, cpu_count=8,
                         mem_total_bytes=16000, mem_available_bytes=12000,
                         app_cpu=0.25, cgroup={'container': False, 'limits_readable': True}), **extra)

    def agent(self, **extra):
        return dict(dict(name='worker', host='vm', status='idle', lease_live=True,
                         session_lot_state=None, actionable_lots=0, unread=0,
                         passive_unread=0, status_since_ts=7000, last_turn_ts=7000), **extra)

    def project(self, *, reading=None, agents=None, lots=(), history=None, **kw):
        return host_idle.project(
            self.reading() if reading is None else reading,
            [self.reading(sampled_ts=t) for t in range(7600, NOW + 1, 60)]
            if history is None else history,
            [self.agent()] if agents is None else agents, lots, now=NOW, **kw)

    def test_idle_with_footprint_and_price(self):
        out = self.project(cost_eur_h=0.08)
        self.assertEqual(out['state'], 'idle')
        self.assertEqual(out['idle_since'], 7600)
        self.assertEqual(out['reserved_cpu'], 8)
        self.assertEqual(out['used_mem_bytes'], 4000)
        self.assertEqual(out['leases'], 0)  # a quiet runner is not a lot lease
        self.assertIn('0.08 EUR/h', host_idle.line(out))
        self.assertEqual(out['runs'], 0)
        self.assertIn('aucune interface', out['runs_source'])

    def test_each_work_condition_prevents_idle(self):
        for change in ({'status': 'running'}, {'turn_started_ts': 9990},
                       {'session_lot_state': 'build'}, {'actionable_lots': 1},
                       {'unread': 1}, {'passive_unread': 1}, {'has_pending_prompt': True}):
            with self.subTest(change=change):
                self.assertEqual(self.project(agents=[self.agent(**change)])['state'], 'busy')
        self.assertEqual(self.project(reading=self.reading(turns_in_progress=1))['state'], 'busy')
        for wake in (NOW - 1, NOW, NOW + 1799):
            self.assertEqual(self.project(lots=[dict(state='blocked', wake_ts=wake)])['state'], 'busy')
        self.assertEqual(self.project(lots=[dict(state='blocked', wake_ts=NOW + 1800)])['state'], 'idle')
        self.assertEqual(self.project(lots=[dict(state='blocked', wake_ts=NOW + 1801)])['state'], 'idle')
        self.assertEqual(self.project(lots=[dict(state='closed', wake_ts=NOW - 1)])['state'], 'idle')

    def test_held_nonactionable_session_does_not_reserve_host(self):
        lot = dict(id=42, state='build', assignee='worker', updated_ts=7000)
        agent = self.agent(session_work_item='42', session_lot_state='build',
                           actionable_lots=1)
        out = self.project(agents=[agent], lots=[lot], holds={42: dict(since=7000)})
        self.assertEqual(out['leases'], 0)
        self.assertEqual(out['state'], 'idle')
        self.assertEqual(self.project(agents=[agent], lots=[lot])['state'], 'busy')
        # A second actionable lot, mail or a near wake still requires the host.
        self.assertEqual(self.project(agents=[dict(agent, actionable_lots=2)],
                                     lots=[lot], holds={42: dict(since=7000)})['state'], 'busy')
        self.assertEqual(self.project(agents=[dict(agent, unread=1)], lots=[lot],
                                     holds={42: dict(since=7000)})['state'], 'busy')
        for wake in (NOW - 1, NOW + 60):
            self.assertEqual(self.project(agents=[agent], lots=[dict(lot, wake_ts=wake)],
                                         holds={42: dict(since=7000)})['state'], 'busy')

    def test_future_wake_releases_parked_session_until_near_deadline(self):
        lot = dict(id=42, state='build', assignee='worker', updated_ts=7000,
                   wake_ts=NOW + 3600)
        agent = self.agent(session_work_item='42', session_lot_state='build')
        out = self.project(agents=[agent], lots=[lot])
        self.assertEqual(out['leases'], 0)
        self.assertEqual(out['state'], 'idle')
        for wake in (NOW + 60, NOW):
            self.assertEqual(self.project(agents=[agent], lots=[dict(lot, wake_ts=wake)])['state'],
                             'busy')
        self.assertEqual(self.project(agents=[dict(agent, status='running')], lots=[lot])['state'],
                         'busy')

    def test_recent_activity_resets_the_window(self):
        for key in ('status_since_ts', 'last_turn_ts', 'last_turn_recorded_ts'):
            self.assertEqual(self.project(agents=[self.agent(**{key: NOW - 100})])['state'], 'busy')
        self.assertEqual(self.project(lots=[dict(updated_ts=NOW - 60, state='closed')])['state'], 'busy')
        self.assertEqual(self.project(idle_after_s=3600)['state'], 'busy')

    def test_unknown_or_stale_is_never_idle(self):
        for reading in (self.reading(sampled_ts=NOW - 181),
                        self.reading(sampled_ts=NOW + 1),
                        self.reading(turns_in_progress=None),
                        self.reading(cgroup={}), self.reading(cpu_count=None)):
            out = self.project(reading=reading)
            self.assertEqual(out['state'], 'busy')
            self.assertTrue(out['unknown'])
        self.assertEqual(self.project(agents=[self.agent(lease_live=None)])['state'], 'busy')
        self.assertEqual(self.project(lots=None)['state'], 'busy')
        out = self.project(reading=self.reading(cpu_count=None))
        self.assertIn('reserved CPU unknown', host_idle.line(out))

    def test_history_gaps_and_previous_turn_reset_window(self):
        for history in ([], [self.reading()],
                        [self.reading(sampled_ts=7600), self.reading()]):
            self.assertEqual(self.project(history=history)['state'], 'busy')
        history = [self.reading(sampled_ts=t, turns_in_progress=int(t == 9100))
                   for t in range(7600, NOW + 1, 60)]
        self.assertEqual(self.project(history=history)['state'], 'busy')

    def test_baseline_guard_and_explicit_cards(self):
        def canon(**cards):
            return NS(hosts=[NS(title=n, fiche=NS(data=d)) for n, d in cards.items()])
        caps, costs = host_idle.capacities([canon(vm={'tarif_eur_h': 0.2})],
                                          ['vm', 'leader', 'reserve'],
                                          protected=['leader', 'reserve'])
        self.assertEqual(caps, dict(vm='burst', leader='baseline', reserve='baseline'))
        self.assertEqual(costs['vm'], 0.2)
        caps, _ = host_idle.capacities([canon(a={'capacity': 'baseline'}, b={'capacity': 'baseline'})],
                                       ['a', 'b', 'c'], protected=['c'])
        self.assertEqual(caps['c'], 'burst')

    def test_snapshot_reads_only_and_failure_is_conservative(self):
        store = mock.MagicMock()
        store.operations.listing.return_value = [self.agent()]
        store.work.items.return_value = []
        store.hosts.since.return_value = [self.reading(sampled_ts=t)
                                           for t in range(7600, NOW + 1, 60)]
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()):
            self.assertEqual(host_idle.snapshot(cfg, 'db', [], {'vm': self.reading()}, now=NOW)['vm']['state'], 'idle')
            # A reading that can still prove idle keeps the full lot read and
            # the quiet history: both can shorten the interval (VM witness).
            store.work.items.assert_called_once_with(state=None, assignee='worker', limit=1000)
            self.assertEqual([call.args[0] for call in store.hosts.since.call_args_list], ['vm'])
            store.operations.listing.side_effect = RuntimeError('unavailable')
            out = host_idle.snapshot(cfg, 'db', [], {'vm': self.reading()}, now=NOW)['vm']
            self.assertEqual(out['state'], 'busy')
            self.assertTrue(out['unknown'])
        self.assertFalse(store.host_lifecycle.mock_calls)

    def test_snapshot_skips_lot_reads_where_the_reading_is_busy(self):
        store = mock.MagicMock()
        store.operations.listing.return_value = [self.agent(), self.agent(name='other', host='busy')]
        store.work.items.return_value = []
        store.hosts.since.return_value = [self.reading(sampled_ts=t)
                                           for t in range(7600, NOW + 1, 60)]
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()):
            out = host_idle.snapshot(cfg, 'db', [], {'vm': self.reading(),
                                                     'busy': self.reading(turns_in_progress=2)},
                                     now=NOW)
        self.assertEqual(out['vm']['state'], 'idle')
        self.assertEqual(out['busy']['state'], 'busy')
        self.assertEqual([call.kwargs['assignee'] for call in store.work.items.call_args_list],
                         ['worker'])
        # The busy host cannot be idle whatever its history says: no read.
        self.assertEqual([call.args[0] for call in store.hosts.since.call_args_list], ['vm'])

    def test_busy_host_keeps_hold_aware_leases_without_full_lot_reads(self):
        """The hold/lease witness must survive the fast path (#4178)."""
        store = mock.MagicMock()
        store.operations.listing.return_value = [
            self.agent(name='worker', host='busy', session_work_item='42',
                       session_lot_state='build', actionable_lots=1)]
        store.work.get.return_value = dict(id=42, state='build', assignee='worker',
                                           updated_ts=7000)
        store.progress.lot_hold_events.return_value = [
            dict(id=1, work_item_id=42, state='build', actor='human:smichea',
                 note='hold: attente owner', created_ts=7000)]
        store.hosts.since.return_value = []
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()):
            out = host_idle.snapshot(cfg, 'db', [], {'busy': self.reading(turns_in_progress=2)},
                                     now=NOW)['busy']
        # The reading is busy, but the session lot still feeds the lease count:
        # the held lot is not a lot lease, exactly as with a full read.
        self.assertEqual(out['state'], 'busy')
        self.assertEqual(out['leases'], 0)
        self.assertNotIn('agent/lot activity unreadable', out['unknown'])
        store.work.items.assert_not_called()
        store.work.get.assert_called_once_with(42)
        store.progress.lot_hold_events.assert_called_once()

    def test_snapshot_seeds_the_canon_window_for_hold_authority(self):
        """#4178: the canons already read for the view are not read per author."""
        from ameesh import canon as canon_mod

        store = mock.MagicMock()
        store.operations.listing.return_value = [
            self.agent(name='worker', host='busy', session_work_item='42',
                       session_lot_state='build')]
        store.work.get.return_value = dict(id=42, state='build', assignee='worker')
        store.progress.lot_hold_events.return_value = [
            dict(id=1, work_item_id=42, state='build', actor='ameesh-cx3',
                 note='hold: attente owner', created_ts=7000)]
        store.hosts.since.return_value = []
        cfg = config.Config(resource_interval=60)

        def orchestrator(*args, **kwargs):
            # Same delegation as the real check, without a registry read.
            canon_mod.load_configured(cfg)
            return True

        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()), \
                mock.patch.object(canon_mod, 'load') as load, \
                mock.patch('ameesh.assignments.is_orchestrator', side_effect=orchestrator):
            out = host_idle.snapshot(
                cfg, 'db', [], {'busy': self.reading(turns_in_progress=2)}, now=NOW)['busy']
        load.assert_not_called()
        self.assertEqual(out['leases'], 0)

    def test_unknown_reading_skips_lots_but_keeps_the_unknown_witness(self):
        store = mock.MagicMock()
        store.operations.listing.return_value = [self.agent()]
        store.work.items.return_value = []
        store.hosts.since.return_value = []
        cfg = config.Config(resource_interval=60)
        with mock.patch.object(storage, 'of', return_value=store), \
                mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()):
            out = host_idle.snapshot(cfg, 'db', [], {'vm': self.reading(cgroup={})},
                                     now=NOW)['vm']
        self.assertEqual(out['state'], 'busy')
        self.assertIn('executor cgroup unreadable', out['unknown'])
        store.work.items.assert_not_called()
        store.hosts.since.assert_not_called()


class CgroupReadabilityTest(unittest.TestCase):
    def test_unlimited_is_readable_but_missing_limits_are_not(self):
        import os
        import tempfile
        from ameesh import resources
        from tests.test_2814_pressure_cgroup_capacity import write_cgroup
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(resources.platform.host, 'in_container', return_value=True):
            write_cgroup(root, quota='max', maximum=None)
            self.assertTrue(resources.CgroupMeter(root).read()['limits_readable'])
            with open(os.path.join(root, 'cpu.max'), 'w') as stream:
                stream.write('garbage 100000')
            self.assertFalse(resources.CgroupMeter(root).read()['limits_readable'])
            os.unlink(os.path.join(root, 'memory.max'))
            self.assertFalse(resources.CgroupMeter(root).read()['limits_readable'])

    def test_invalid_numeric_mesh_settings_keep_defaults(self):
        for value in ('nan', 'inf', '-1', '0', 'garbage'):
            self.assertEqual(config._as_bounded_positive(value, 1800), 1800)
        self.assertEqual(config._as_bounded_positive('900', 1800), 900)


    def test_vm_without_cpu_controller_reserves_the_machine(self):
        import os
        import tempfile
        from ameesh import resources
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(resources.platform.host, 'in_container', return_value=False):
            with open(os.path.join(root, 'memory.max'), 'w') as stream:
                stream.write('max')
            cg = resources.CgroupMeter(root).read()
            self.assertTrue(cg['limits_readable'])
            self.assertFalse(cg['container'])
            reading = dict(sampled_ts=NOW, cpu_count=8, mem_total_bytes=16000,
                           turns_in_progress=0, cgroup=cg)
            history = [dict(reading, sampled_ts=t) for t in range(7600, NOW + 1, 60)]
            out = host_idle.project(reading, history, [], [], now=NOW)
            self.assertEqual(out['state'], 'idle')
            self.assertEqual(out['reserved_cpu'], 8)
            self.assertEqual(out['reserved_mem_bytes'], 16000)

    def test_real_vm_meter_does_not_require_delegated_cpu(self):
        from ameesh import resources
        cg = resources.CgroupMeter().read()
        if cg['container']:
            self.skipTest('real VM witness requires a VM')
        self.assertTrue(cg['limits_readable'])
