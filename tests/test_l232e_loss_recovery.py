# SPDX-License-Identifier: AGPL-3.0-only
"""Lost-host recovery needs scoped quiescence and a published checkpoint."""
from unittest import mock
from contextlib import contextmanager

from ameesh import handoffs, host_lifecycle, registry
from . import test_l232e_drain_handoffs as drain_fixtures
from .test_l232a_handoffs import COMMIT


class RecoveryAdapter(drain_fixtures.Adapter):
    fields = {}

    def recover_source(self, op):
        return self.result(op, "recover_source", **dict(
            {"source": op["source"], "agent": op["agent"], "lease_epoch": op["plan"]["epoch"],
             "generation": op["plan"]["lifecycle_generation"], "source_fenced": True,
             "quiesced": True, "unpublished_work": False, "repository": op["plan"]["repository"],
             "branch": "lot/work", "commit": COMMIT, "remote_commit": COMMIT, "clean": True},
            **self.fields))


class LossRecoveryTest(drain_fixtures.DrainHandoffTest):
    def gate_failure_cancel(self, phase):
        from ameesh.storage.postgres.hosts import Hosts
        self.adapter = RecoveryAdapter()
        self.api.host_lifecycle.drain("source", actor="orch", critical=("dev",))
        for _ in range(3):
            op = self.step()[0]["operation"]
        self.assertEqual(op["state"], "drained")
        self.assertTrue(op["fenced"])
        self.db.execute("UPDATE agent_registry SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE name='dev'")
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-interval '20 minutes' WHERE host='source'")
        original_guard = Hosts.gate_guard
        pids = []

        @contextmanager
        def record_gate(hosts, host):
            with original_guard(hosts, host) as locked:
                pids.append(locked.db.query("SELECT pg_backend_pid() AS pid")[0]["pid"])
                yield locked
                if phase == "commit":
                    disconnect()

        def disconnect():
            self.db.query("SELECT pg_terminate_backend(%s)", (pids[-1],))
            changed = self.api.host_lifecycle.detect_losses(actor="remote", older_than=180, critical=("dev",))
            self.assertEqual(changed[0]["mode"], "lost")

        def restore(intent):
            self.assertEqual(intent["evidence"]["pending_effect"]["name"], "restore_source")
            if phase == "restore":
                disconnect()
            return True

        with mock.patch.object(Hosts, "gate_guard", record_gate), \
                mock.patch.object(self.adapter, "restore_source", side_effect=restore):
            with self.assertRaises(handoffs.HandoffError):
                handoffs.cancel(self.db, op["id"], self.adapter)
        uncertain = self.api.handoffs.get(op["id"])
        self.assertEqual(uncertain["state"], "drained")
        self.assertTrue(uncertain["fenced"])
        self.assertTrue(registry.get(self.db, "dev")["handoff_fenced"])
        self.assertEqual(uncertain["evidence"]["pending_effect"]["name"], "restore_source")
        result = self.step()[0]
        self.assertEqual(result["missing"], ["handoff_requires_reconciliation"])
        self.assertNotIn("enable", self.adapter.calls)

    def test_gate_disconnect_during_restore_keeps_cancellation_uncertain_and_fenced(self):
        self.gate_failure_cancel("restore")

    def test_gate_disconnect_before_commit_rolls_back_completion_and_fence_release(self):
        self.gate_failure_cancel("commit")

    def lose_prepared_drain(self):
        self.adapter = RecoveryAdapter()
        self.api.host_lifecycle.drain("source", actor="orch", critical=("dev",))
        self.step()
        op = self.step()[0]["operation"]
        self.assertEqual(op["state"], "prepared")
        self.db.execute("UPDATE agent_registry SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE name='dev'")
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-interval '20 minutes' WHERE host='source'")
        self.api.host_lifecycle.detect_losses(actor="remote", older_than=180, critical=("dev",))
        return op

    def test_cancel_planned_drain_refuses_source_that_is_now_lost(self):
        op = self.lose_prepared_drain()
        with self.assertRaisesRegex(handoffs.HandoffError, "lost source"):
            handoffs.cancel(self.db, op["id"], self.adapter)
        self.assertNotIn("restore_source", self.adapter.calls)
        self.assertNotEqual(self.api.handoffs.get(op["id"])["state"], "cancelled")

    def test_prepared_drain_enters_loss_recovery_and_reaches_verified(self):
        op = self.lose_prepared_drain()
        for _ in range(6):
            result = self.step()[0]
        self.assertEqual(result["operation"]["id"], op["id"])
        self.assertEqual(result["operation"]["state"], "verified")
        self.assertEqual(result["operation"]["plan"]["reason"], "host_loss")
        self.assertIn("recover_source", self.adapter.calls)
        self.assertNotIn("restore_source", self.adapter.calls)

    def test_held_drain_retry_converts_current_lost_source_before_revalidation(self):
        op = self.lose_prepared_drain()
        self.api.handoffs.hold(op["id"], "prepared")
        wrapped = host_lifecycle._ReadyAdapter(self.db, self.adapter, self.requirements)
        retried = handoffs.retry(self.db, op["id"], wrapped)
        self.assertEqual(retried["state"], "prepared")
        self.assertEqual(retried["plan"]["reason"], "host_loss")
        self.assertTrue(retried["fenced"])
        for _ in range(6):
            result = self.step()[0]
        self.assertEqual(result["operation"]["state"], "verified")

    def test_uncertain_old_drain_effect_must_be_reconciled_before_loss_conversion(self):
        op = self.lose_prepared_drain()
        with self.api.handoffs.controller(op["id"]) as journal:
            from ameesh import storage
            storage.of(journal).handoffs.begin_effect(op["id"], "prepared", "restore_source")
        result = self.step()[0]
        self.assertEqual(result["missing"], ["handoff_requires_reconciliation"])
        self.assertEqual(result["operation"]["plan"]["reason"], "host_drain")
        self.assertNotIn("recover_source", self.adapter.calls)
        wrapped = host_lifecycle._ReadyAdapter(self.db, self.adapter, self.requirements)
        retried = handoffs.retry(self.db, op["id"], wrapped)
        self.assertEqual(retried["plan"]["reason"], "host_loss")
        self.assertNotIn("pending_effect", retried["evidence"])
        self.assertNotIn("cancel_requested", retried["evidence"])
        for _ in range(6):
            result = self.step()[0]
        self.assertEqual(result["operation"]["state"], "verified")

    def lose_source(self, *, critical=True, minutes=20):
        self.adapter = RecoveryAdapter()
        self.assertTrue(registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "running"))
        self.db.execute("UPDATE agent_registry SET lease_expires_at=clock_timestamp()-interval '1 second' "
                        "WHERE name='dev'")
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-make_interval(mins=>%s) "
                        "WHERE host='source'", (minutes,))
        return self.api.host_lifecycle.detect_losses(actor="remote", older_than=180,
                                                     critical=("dev",) if critical else ())[0]

    def test_expired_running_source_recovers_without_source_heartbeat_or_fake_lease(self):
        self.lose_source()
        for _ in range(8):
            result = self.step()[0]
        op = result["operation"]
        self.assertEqual(op["state"], "verified")
        self.assertTrue(op["evidence"]["source_recovered"])
        self.assertNotIn("stop_source", self.adapter.calls)
        self.assertNotIn("publish", self.adapter.calls)
        self.assertEqual(self.adapter.calls.count("enable"), 1)
        row = registry.get(self.db, "dev")
        self.assertEqual(row["host"], "target")
        self.assertGreater(row["lease_epoch"], self.lease["lease_epoch"])
        self.assertIsNone(registry.renew(self.db, "dev", "runner", self.lease["lease_epoch"], ttl_seconds=300))
        self.assertFalse(registry.end_turn(self.db, "dev", "runner", self.lease["lease_epoch"], status="idle"))
        self.assertIsNotNone(registry.claim(self.db, "dev", "target-runner", ttl_seconds=300))

    def test_noncritical_waits_longer_than_critical(self):
        self.lose_source(critical=False, minutes=5)
        result = self.step()[0]
        self.assertIsNone(result["operation"])
        self.assertEqual(result["missing"], ["source_loss_grace"])
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-interval '20 minutes' "
                        "WHERE host='source'")
        self.assertEqual(self.step()[0]["operation"]["state"], "planned")

    def test_bad_or_unpublished_proof_never_transfers_or_revokes_running_state(self):
        self.lose_source()
        self.step()
        self.step()
        op = self.api.handoffs.active("dev")
        for fields in ({"quiesced": False}, {"source_fenced": False}, {"unpublished_work": True},
                       {"remote_commit": "b"*40}, {"agent": "other"}, {"generation": 0},
                       {"lease_epoch": 0}, {"source": "other"}):
            self.adapter.fields = fields
            with self.assertRaises(handoffs.HandoffError):
                handoffs._proof("recover_source", self.adapter.recover_source(op), op)
        result = self.step()[0]
        self.assertEqual(result["operation"]["state"], "held")
        self.assertIn("source_quiescence_and_published_checkpoint", result["missing"])
        row = registry.get(self.db, "dev")
        self.assertEqual((row["host"], row["status"], row["lease_epoch"]),
                         ("source", "running", self.lease["lease_epoch"]))
        self.assertNotIn("transfer", self.adapter.calls)
        self.assertNotIn("enable", self.adapter.calls)

    def test_returning_source_heartbeat_invalidates_unreserved_stale_candidate(self):
        self.lose_source()
        agent = registry.get(self.db, "dev")
        demand = self.api.host_lifecycle.demand("dev", "source")
        candidate = handoffs.plan(self.db, agent, demand=demand, limits_for=lambda h: self.limits,
                                  readiness_for=lambda a, h: {"ready": True})
        self.api.hosts.record({"host": "source", "gate_state": "available"})
        with self.assertRaises(handoffs.HandoffError):
            handoffs.request(self.db, candidate, repository=self.adapter.repository(agent))
        self.assertIsNone(self.api.handoffs.active("dev"))

    def test_uncertain_recovery_requires_reconciliation_before_retry(self):
        self.lose_source()
        self.step()
        self.step()
        with mock.patch.object(self.adapter, "recover_source", side_effect=RuntimeError("private diagnostic")):
            result = self.step()[0]
        op = result["operation"]
        self.assertEqual(op["state"], "held")
        self.assertEqual(op["evidence"]["pending_effect"]["name"], "recover_source")
        with self.assertRaises(handoffs.HandoffError):
            self.step()
        self.assertNotIn("private diagnostic", str(self.api.handoffs.events(op["id"])))
        wrapped = host_lifecycle._ReadyAdapter(self.db, self.adapter, self.requirements)
        handoffs.retry(self.db, op["id"], wrapped)
        for _ in range(6):
            result = self.step()[0]
        self.assertEqual(result["operation"]["state"], "verified")

    def test_normal_cancel_cannot_restore_a_lost_source(self):
        self.lose_source()
        op = self.step()[0]["operation"]
        with self.assertRaisesRegex(handoffs.HandoffError, "ordinary cancellation"):
            handoffs.cancel(self.db, op["id"], self.adapter)
        self.assertNotIn("restore_source", self.adapter.calls)
