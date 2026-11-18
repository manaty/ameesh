# SPDX-License-Identifier: AGPL-3.0-only
"""#7769: a registry row with `admitted_hosts` NULL must not break the tick.

`admitted_hosts` is declarative and nullable (0029): rows registered outside
`canon sync` (externals, ephemerals, hook registrations) carry NULL. `get`
returned None and the set comprehension raised TypeError inside `wake_due`;
the observation written earlier in the same transaction was rolled back, so
the draining host produced no observation at all.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from ameesh import storage, vm_adapter as vm


HOST = 'burst'
TARGET = dict(zone='fr-par-1', server_id='11111111-1111-4111-8111-111111111111',
              project_id='22222222-2222-4222-8222-222222222222', timezone='Europe/Paris',
              release_windows=['11:00', '23:00'], health_argv=['/health'],
              reconcile_argv=['/reconcile'], quiesce_argv=['/quiesce'],
              halt_argv=['/halt'], halted_argv=['/halted'], probe_agent='burst-probe')


def policy(**over):
    values = dict(enabled=True, controller_host='controller', min_on_s=3600,
                  min_off_s=60, wake_delay_s=900, observation_gap_s=90,
                  io_timeout_s=10, health_deadline_s=900, halt_deadline_s=300,
                  targets={HOST: dict(TARGET)})
    values.update(over)
    return values


def legacy_agent(**over):
    """Shape of `store.operations.listing()` for a row outside canon sync."""
    row = dict(name='legacy', host=HOST, admitted_hosts=None, releve='hote',
               mode='execute', status='idle')
    row.update(over)
    return row


class WakeDueNullTest(unittest.TestCase):
    def test_null_is_no_admission_and_does_not_mask_another_agent(self):
        now = datetime(2026, 11, 5, 9, 45, tzinfo=timezone.utc).timestamp()  # Paris 10:45
        control = dict(host=HOST, mode='draining')
        controls = {HOST: control}
        args = dict(now=now, horizon=900, target=TARGET)
        self.assertFalse(vm.wake_due(HOST, control, [legacy_agent()], [], controls, {}, **args))
        listing = [legacy_agent(),
                   dict(name='agent', host=HOST, admitted_hosts=[HOST],
                        mode='execute', status='idle')]
        self.assertTrue(vm.wake_due(HOST, control, listing, [], controls, {}, **args))

    def test_null_does_not_break_the_shared_host_exclusion(self):
        now = datetime(2026, 11, 5, 9, 45, tzinfo=timezone.utc).timestamp()
        control = dict(host=HOST, mode='draining')
        controls = {HOST: control, 'other': dict(host='other', mode='execute')}
        listing = [legacy_agent(),
                   dict(name='agent', host=HOST, admitted_hosts=[HOST, 'other'],
                        mode='execute', status='idle')]
        self.assertFalse(vm.wake_due(HOST, control, listing, [], controls, {},
                                     now=now, horizon=900, target=TARGET))


class NullAdmittedHostsTickTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM vm_adapter_requests')
        self.db.execute('DELETE FROM vm_adapter_observations')
        self.db.execute('DELETE FROM host_lifecycle')
        self.db.execute("INSERT INTO host_lifecycle(host,mode,actor,claims_open,"
                        "release_state,hook_state) VALUES (%s,'draining','tester',"
                        "false,'draining','draining')", (HOST,))
        self.cfg = replace(self.cfg, idle_after_s=1800)
        self.provider = mock.Mock()
        self.provider.state.return_value = 'running'
        self.operations = mock.Mock()
        self.operations.listing.return_value = [legacy_agent()]
        self.operations.open_lots_activity.return_value = []
        self.hosts = mock.Mock()
        self.hosts.holds.return_value = []
        real = storage.of(self.db)
        self.store = SimpleNamespace(host_lifecycle=real.host_lifecycle,
                                     operations=self.operations, hosts=self.hosts)

    def test_draining_tick_commits_the_observation(self):
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            result = vm.tick_host(self.cfg, self.db, policy(), HOST, self.provider,
                                  probe=mock.Mock(return_value=True))
        self.assertEqual(result, 'no request')
        observations = self.db.query(
            'SELECT host, provider_state FROM vm_adapter_observations WHERE host=%s',
            (HOST,))
        self.assertEqual(observations, [{'host': HOST, 'provider_state': 'running'}])


if __name__ == '__main__':
    unittest.main()
