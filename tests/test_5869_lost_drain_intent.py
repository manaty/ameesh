# SPDX-License-Identifier: AGPL-3.0-only
"""#5869: a manual drain still running survives a loss and its reconciliation."""

from ameesh import registry, storage
from .support import PgTestCase


class LostDrainIntentTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle,host_resources,credential_health CASCADE")
        self.api = storage.of(self.db)
        registry.upsert(self.db, "dev", harness="codex", host="source", cwd="/work/source")
        self.lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        self.api.hosts.record({"host": "source", "gate_state": "available"})

    def drain(self, *, duration=3600, automatic=False):
        return self.api.host_lifecycle.drain("source", actor="pilot", duration=duration,
                                             automatic=automatic)

    def lose(self):
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()"
                        "-interval '1000 seconds' WHERE host='source'")
        lost = self.api.host_lifecycle.detect_losses(actor="remote", older_than=180)
        self.db.execute("UPDATE host_lifecycle SET updated_at=clock_timestamp()"
                        "-interval '900 seconds' WHERE host='source'")
        return lost

    def come_back(self):
        self.db.execute(
            "INSERT INTO host_resources(host, sampled_at, gate_state) VALUES "
            "('source', clock_timestamp()-interval '200 seconds', 'available'), "
            "('source', clock_timestamp()-interval '100 seconds', 'available')")
        self.api.hosts.record({"host": "source", "gate_state": "available"})
        return self.api.host_lifecycle.reconcile_loss(
            "source", actor="runner-peer@target", fresh_within=180)

    def test_loss_of_a_manual_drain_keeps_its_deadline(self):
        drained = self.drain()
        self.assertEqual(len(self.lose()), 1)
        row = self.api.host_lifecycle.get("source")
        self.assertEqual(row["mode"], "lost")
        self.assertTrue(row["lost_from_drain"])
        self.assertAlmostEqual(row["return_ts"], drained["return_ts"], delta=1)
        self.assertEqual(row["generation"], drained["generation"])

    def test_reconcile_returns_a_running_manual_drain_to_draining(self):
        drained = self.drain()
        self.lose()
        result = self.come_back()
        self.assertTrue(result["reconciled"], result)
        self.assertEqual(result["mode"], "draining")
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["generation"]), ("draining", drained["generation"]))
        self.assertAlmostEqual(row["return_ts"], drained["return_ts"], delta=1)
        self.assertFalse(row["lost_from_drain"])
        self.assertIn("drain_intent_kept", result["evidence"]["checks"])
        # The operator's undrain stays the way out, as planned.
        back = self.api.host_lifecycle.undrain("source", actor="pilot")
        self.assertEqual(back["mode"], "returning")
        # A second pass finds nothing lost: no reopening behind the operator.
        again = self.api.host_lifecycle.reconcile_loss(
            "source", actor="runner-peer@target", fresh_within=180)
        self.assertFalse(again["reconciled"])

    def test_an_expired_drain_reopens_as_before(self):
        self.drain()
        self.lose()
        self.db.execute("UPDATE host_lifecycle SET return_at=clock_timestamp()"
                        "-interval '1 second' WHERE host='source'")
        result = self.come_back()
        self.assertEqual(result["mode"], "available", result)
        self.assertFalse(self.api.host_lifecycle.get("source")["lost_from_drain"])

    def test_an_automatic_drain_reopens_as_before(self):
        self.drain(automatic=True)
        self.lose()
        self.assertEqual(self.come_back()["mode"], "available")

    def test_an_indefinite_manual_drain_is_kept(self):
        self.drain(duration=None)
        self.lose()
        result = self.come_back()
        self.assertEqual(result["mode"], "draining", result)
        self.assertIsNone(self.api.host_lifecycle.get("source")["return_ts"])

    def test_a_loss_without_drain_reopens_as_before(self):
        self.lose()
        self.assertFalse(self.api.host_lifecycle.get("source")["lost_from_drain"])
        self.assertEqual(self.come_back()["mode"], "available")

    def test_refusals_still_apply_before_the_drain_is_restored(self):
        self.drain()
        self.lose()
        self.db.execute("UPDATE agent_registry SET handoff_fenced=true WHERE name='dev'")
        result = self.come_back()
        self.assertFalse(result["reconciled"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")
