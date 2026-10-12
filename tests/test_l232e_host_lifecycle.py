# SPDX-License-Identifier: AGPL-3.0-only
"""Durable withdrawal blocks new turns and gates, preserving current work."""
from ameesh import registry, storage
from .support import PgTestCase


class HostLifecycleTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle CASCADE")
        self.api = storage.of(self.db)
        registry.upsert(self.db, "dev", harness="codex", host="source", cwd="/work/source")
        self.lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        self.reading = self.api.hosts.record({"host": "source", "gate_state": "available"})

    def drain(self):
        return self.api.host_lifecycle.drain("source", actor="orch", duration=3600, critical=("dev",))

    def test_drain_preserves_running_turn_and_lease_but_refuses_next_turn(self):
        self.assertTrue(registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "active"))
        before = registry.get(self.db, "dev")
        self.drain()
        after = registry.get(self.db, "dev")
        for key in ("status", "lease_owner", "lease_epoch", "lease_expires_ts", "host"):
            self.assertEqual(after[key], before[key])
        self.assertFalse(registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "new"))

    def test_drain_prevents_prompt_consumption_and_claims(self):
        registry.set_pending_prompt(self.db, "dev", "keep this work")
        self.drain()
        self.assertIsNone(registry.take_pending_prompt(self.db, "dev", "runner", self.lease["lease_epoch"]))
        self.assertEqual(registry.get(self.db, "dev")["pending_prompt"], "keep this work")
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        self.assertIsNone(registry.claim(self.db, "dev", "other", ttl_seconds=300))
        self.assertEqual(registry.claimable(self.db, "source"), [])

    def test_control_survives_new_available_readings_without_forging_heartbeat(self):
        self.drain()
        latest = self.api.hosts.latest("source")
        self.assertEqual(latest["sampled_ts"], self.reading["sampled_ts"])
        self.assertEqual(latest["gate_state"], "draining")
        self.assertEqual(self.api.hosts.record({"host": "source", "gate_state": "available"})["gate_state"], "draining")
        self.assertEqual(self.api.hosts.history("source", 2)[-1]["gate_state"], "draining")
        self.assertEqual(self.api.hosts.current("source")[0]["gate_state"], "draining")

    def test_idempotent_drain_retains_origins_and_critical_order(self):
        first = self.drain()
        self.assertEqual(self.drain(), first)
        rows = self.api.host_lifecycle.agents("source", first["generation"])
        self.assertEqual([(r["agent"], r["critical"]) for r in rows], [("dev", True)])

    def test_undrain_requests_return_and_reopens_existing_claim(self):
        first = self.drain()
        returned = self.api.host_lifecycle.undrain("source", actor="orch")
        self.assertEqual((returned["mode"], returned["generation"]), ("returning", first["generation"]))
        self.assertIsNone(returned["return_ts"])
        self.assertEqual(self.api.hosts.latest("source")["gate_state"], "available")
        self.assertTrue(registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "new"))
        self.assertEqual(self.api.host_lifecycle.agents("source", first["generation"])[0]["agent"], "dev")

    def test_undrain_does_not_override_physical_stopped_gate(self):
        self.drain()
        self.api.hosts.record({"host": "source", "gate_state": "stopped"})
        self.api.host_lifecycle.undrain("source", actor="orch")
        self.assertEqual(self.api.hosts.latest("source")["gate_state"], "stopped")
