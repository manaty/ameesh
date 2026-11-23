# SPDX-License-Identifier: AGPL-3.0-only
"""A dated manual drain can halt while ordinary active mail waits."""
import dataclasses
import unittest
from unittest import mock

from ameesh import host_idle, vm_adapter as vm
from ameesh.config import Config


class HaltMailTest(unittest.TestCase):
    def check_stop(self, control, agent):
        now = 10000
        reading = dict(sampled_ts=now, turns_in_progress=0, cpu_count=8,
                       mem_total_bytes=16000, mem_available_bytes=12000,
                       app_cpu=0.25, cgroup={'container': False, 'limits_readable': True})
        history = [dict(reading, sampled_ts=t) for t in (now, now-60, now-120)]
        store = mock.Mock()
        store.hosts.latest.return_value = reading
        store.operations.listing.return_value = [agent]
        cfg = dataclasses.replace(Config(), resource_interval=60, idle_after_s=60,
                                  host_release_settle=0)

        def snapshot(cfg, db, canons, readings, *, listing, **kwargs):
            fp = host_idle.project(reading, history, listing, [], now=now,
                                   idle_after_s=60, fresh_within=180,
                                   runs_declared=(host_idle.RUNS_NONE, 'test'))
            return {'vm': fp}

        with mock.patch.object(vm.storage, 'of', return_value=store), \
             mock.patch.object(vm.host_release, '_canon_read', return_value=([object()], False)), \
             mock.patch.object(vm.host_release, 'ci_wake_evidence', return_value={'state': 'inert'}), \
             mock.patch.object(vm.host_idle, 'snapshot', side_effect=snapshot), \
             mock.patch.object(vm.host_lifecycle, 'critical_names', return_value=[]):
            return vm.stop_evidence(cfg, mock.Mock(), 'vm', control, now=now)

    def test_manual_drain_and_other_work(self):
        control = dict(host='vm', mode='draining', automatic=False,
                       return_ts=11000, requested_ts=9000)
        agent = dict(name='worker', host='vm', status='idle', lease_live=False,
                     actionable_lots=0, unread=1, urgent_unread=0,
                     passive_unread=0, status_since_ts=7000)
        self.assertEqual(self.check_stop(control, agent)['state'], 'idle')
        self.assertEqual(agent['unread'], 1)
        for changes in (dict(urgent_unread=1), dict(actionable_lots=1),
                        dict(has_pending_prompt=True), dict(passive_unread=1),
                        dict(lease_live=True), dict(urgent_unread=None)):
            with self.subTest(changes=changes), self.assertRaises(vm.Refused):
                self.check_stop(control, dict(agent, **changes))
        for changes in (dict(automatic=True), dict(return_ts=None),
                        dict(mode='returning')):
            with self.subTest(changes=changes), self.assertRaises(vm.Refused):
                self.check_stop(dict(control, **changes), agent)
