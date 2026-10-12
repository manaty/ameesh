# SPDX-License-Identifier: AGPL-3.0-only
"""Unleased diagnostics, claim clearing, and operational alerts (#501)."""
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from .support import PgTestCase
from ameesh import exploitation, registry, storage
from ameesh.runner import Runner


class UnleasedRegistryTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("waiting", "claude", cwd=self.tmp, prompt="work")
        self.error = "lease_not_taken: host gate: draining (app_cpu)"

    def note(self, host=None):
        return storage.of(self.db).agents.note_unleased("waiting", host or self.cfg.host, self.error)

    def alerts(self):
        return [a for a in exploitation.alerts(self.cfg, self.db) if a["type"] == "lease_not_taken"]

    def test_note_keeps_status_and_successful_claim_clears_alert(self):
        before = registry.get(self.db, "waiting")
        self.assertTrue(self.note())
        self.assertFalse(self.note())  # no timestamp churn for unchanged error
        row = registry.get(self.db, "waiting")
        self.assertEqual(row["status"], before["status"])
        self.assertEqual(row["last_error"], self.error)
        self.assertEqual([a["agent"] for a in self.alerts()], ["waiting"])
        self.assertTrue(registry.claim(self.db, "waiting", "runner", 60))
        self.assertIsNone(registry.get(self.db, "waiting")["last_error"])
        self.assertEqual(self.alerts(), [])
        self.assertFalse(self.note())

    def test_live_lease_other_host_and_blocked_status_are_preserved(self):
        self.assertFalse(self.note(host="other-host"))
        registry.set_status(self.db, "waiting", "blocked", error="authentication failing")
        self.assertFalse(self.note())
        self.assertEqual(registry.get(self.db, "waiting")["last_error"], "authentication failing")
        self.assertTrue(registry.claim(self.db, "waiting", "runner", 60))
        self.assertFalse(self.note())
        self.assertEqual(registry.get(self.db, "waiting")["last_error"], "authentication failing")

    def test_closed_gate_sweep_records_reason_without_claiming(self):
        runner = Runner(replace(self.cfg, poll=1), self.db, agents=["waiting"])
        runner._gate_settled.set()
        runner.host_gate = SimpleNamespace(holds_turns=lambda: True,
                                           describe=lambda: "stopped (ack permission denied)",
                                           gate=SimpleNamespace(ack_error="permission denied"))
        with mock.patch.object(runner, "expire_delegations_once"), \
                mock.patch.object(registry, "claim") as claim:
            runner.sweep()
        claim.assert_not_called()
        row = registry.get(self.db, "waiting")
        self.assertTrue(row["last_error"].startswith("lease_not_taken: host gate:"))
        self.assertIn("permission denied", row["last_error"])
        self.assertIsNone(row["lease_owner"])
        self.assertEqual([a["agent"] for a in self.alerts()], ["waiting"])

    def test_expired_lease_can_be_diagnosed_without_changing_owner(self):
        lease = registry.claim(self.db, "waiting", "expired", 60)
        self.assertTrue(lease)
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 second'"
                        " WHERE name = 'waiting'")
        self.assertTrue(self.note())
        self.assertEqual(registry.get(self.db, "waiting")["lease_owner"], "expired")

    def test_waiting_note_cannot_overwrite_a_lease_claimed_under_row_lock(self):
        import threading
        import time
        if self.db.name != "psycopg":
            self.skipTest("row-lock contention requires a persistent connection")
        blocker = self.connect()
        other = self.connect()
        started = threading.Event()
        result, errors = [], []
        def note():
            started.set()
            try:
                result.append(storage.of(other).agents.note_unleased(
                    "waiting", self.cfg.host, self.error))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=note)
        try:
            blocker.execute("BEGIN")
            blocker.query("SELECT name FROM agent_registry WHERE name = 'waiting' FOR UPDATE")
            worker.start()
            self.assertTrue(started.wait(2))
            time.sleep(.1)
            self.assertTrue(worker.is_alive())
            self.assertTrue(registry.claim(blocker, "waiting", "winner", 60))
            blocker.execute("COMMIT")
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, [False])
            row = registry.get(self.db, "waiting")
            self.assertEqual(row["lease_owner"], "winner")
            self.assertIsNone(row["last_error"])
        finally:
            blocker.execute("ROLLBACK")
            worker.join(timeout=5)
            other.close()
            blocker.close()
