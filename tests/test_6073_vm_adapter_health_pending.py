# SPDX-License-Identifier: AGPL-3.0-only
"""#6073: health answers 0/75/other; a probe turn in flight keeps the return."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from tests.test_5925_vm_adapter import policy
from ameesh import storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue

FRESH = mock.patch('ameesh.vm_adapter.host_release._fresh_reading', return_value={'sampled_ts': 1})


class EnrollmentTest(unittest.TestCase):
    def test_health_deadline_is_explicit_and_bounded(self):
        self.assertEqual(vm.enrollment(policy(), local_host='controller')['health_deadline_s'], 900)
        missing = policy()
        del missing['health_deadline_s']
        for value in (missing, policy(health_deadline_s=True), policy(health_deadline_s=0),
                      policy(health_deadline_s=10), policy(health_deadline_s=3601),
                      policy(health_deadline_s=float('inf'))):
            with self.assertRaisesRegex(vm.Refused, 'health deadline'):
                vm.enrollment(value, local_host='controller')

    def test_only_health_reads_75_as_pending(self):
        with mock.patch('ameesh.vm_adapter.subprocess.run', return_value=SimpleNamespace(returncode=75)):
            self.assertEqual(vm.run_probe(['/health'], 10, pending=True), 'pending')
            self.assertIs(vm.run_probe(['/reconcile'], 10), False)
        with mock.patch('ameesh.vm_adapter.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            self.assertIs(vm.run_probe(['/health'], 10, pending=True), False)


class HealthPendingTest(PgTestCase):
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
        operations = mock.Mock()
        operations.listing.return_value = []
        operations.open_lots_activity.return_value = []
        hosts = mock.Mock()
        hosts.holds.return_value = []
        self.store = SimpleNamespace(host_lifecycle=storage.of(self.db).host_lifecycle,
                                     operations=operations, hosts=hosts)
        self.probe = mock.Mock(return_value='pending')
        control = self.q.control('burst')
        self.q.request('burst', control['generation'], control['release_epoch'], 'busy')
        self.q.observe('burst', 'running', 90)
        self.db.execute("UPDATE vm_adapter_observations SET on_since=clock_timestamp()-interval '2 hours'")

    def tick(self):
        with mock.patch('ameesh.vm_adapter.storage.of', return_value=self.store):
            return vm.tick_host(self.cfg, self.db, policy(), 'burst', self.provider, probe=self.probe,
                                evidence=lambda *a, **k: {'idle_since': k['now'] - 7200})

    def control(self):
        control = self.q.control('burst')
        return control['mode'], control['claims_open'], self.q.return_probe('burst')

    def busy(self):
        return self.db.query("SELECT * FROM vm_adapter_requests WHERE state='busy' "
                             "ORDER BY generation DESC LIMIT 1")[0]

    def test_75_keeps_returning_without_refusal(self):
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            admitted = self.q.probe_admitted(self.busy())
            self.assertIsNotNone(admitted)
            for _ in range(3):
                self.assertEqual(self.tick(), 'health pending')
                self.assertEqual(self.control(), ('returning', False, 'burst-probe'))
        self.assertEqual(self.q.probe_admitted(self.busy()), admitted)
        self.assertEqual(self.busy()['status'], 'pending')
        self.assertEqual(self.probe.call_count, 3)
        self.assertEqual(self.probe.call_args.kwargs, {'pending': True})

    def test_deadline_passed_returns_to_draining_never_lost(self):
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
            self.db.execute("UPDATE vm_adapter_requests SET "
                            "updated_at=clock_timestamp()-interval '901 seconds'")
            self.assertEqual(self.tick(), 'health deadline passed')
            self.assertEqual(self.control(), ('draining', False, None))
            self.assertEqual(self.busy()['status'], 'pending')
            self.assertIsNone(self.q.probe_admitted(self.busy()))
            # The return is still due: a new admission restarts the deadline.
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')

    def test_generation_change_during_wait_is_obsolete(self):
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
            old = self.busy()
            self.db.execute("UPDATE host_lifecycle SET generation=generation+1 WHERE host='burst'")
            self.assertEqual(self.tick(), 'obsolete')
        self.assertEqual(self.db.query(
            "SELECT status FROM vm_adapter_requests WHERE generation=%s AND state='busy'",
            (old['generation'],))[0]['status'], 'obsolete')
        self.assertIsNone(self.q.return_probe('burst'))
        self.assertFalse(self.q.control('burst')['claims_open'])
        self.provider.action.assert_not_called()

    def test_pending_then_healthy_and_reconciled_is_ready(self):
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health pending')
            self.probe.side_effect = [True, False]
            self.assertEqual(self.tick(), 'reconciliation pending')
            self.probe.side_effect = None
            self.probe.return_value = True
            self.assertEqual(self.tick(), 'ready')
        self.assertEqual(self.busy()['status'], 'done')
        self.assertFalse(self.q.control('burst')['claims_open'])

    def test_other_code_still_fails_at_once(self):
        self.probe.return_value = False
        with FRESH:
            self.assertEqual(self.tick(), 'probe admitted')
            self.assertEqual(self.tick(), 'health failed')
        self.assertEqual(self.control(), ('draining', False, None))


if __name__ == '__main__':
    unittest.main()
