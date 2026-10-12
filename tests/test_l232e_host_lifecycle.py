# SPDX-License-Identifier: AGPL-3.0-only
"""Durable withdrawal blocks new turns and gates, preserving current work."""
import threading

from ameesh import db as db_mod, registry, storage
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

    def test_waiting_turn_observes_committed_drain_without_agent_row_update(self):
        started = threading.Event()
        result, errors = [], []

        def begin_turn():
            other = db_mod.connect(self.cfg)
            try:
                started.set()
                result.append(registry.begin_turn(other, "dev", "runner", self.lease["lease_epoch"], "racing"))
            except Exception as exc:
                errors.append(exc)
            finally:
                other.close()

        thread = threading.Thread(target=begin_turn, daemon=True)
        try:
            with self.db.transaction() as tx:
                tx.query("SELECT name FROM agent_registry WHERE name='dev' FOR UPDATE")
                thread.start()
                self.assertTrue(started.wait(5))
                thread.join(0.1)
                self.assertTrue(thread.is_alive())
                storage.of(tx).host_lifecycle.drain("source", actor="orch")
        finally:
            if thread.ident is not None:
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [False])
        self.assertEqual(registry.get(self.db, "dev")["status"], "idle")

    def test_withdrawal_waits_for_enable_gate_boundary(self):
        started = threading.Event()
        result, errors = [], []

        def withdraw():
            other = db_mod.connect(self.cfg)
            try:
                started.set()
                result.append(storage.of(other).host_lifecycle.drain("source", actor="orch"))
            except Exception as exc:
                errors.append(exc)
            finally:
                other.close()

        thread = threading.Thread(target=withdraw, daemon=True)
        try:
            with self.api.hosts.gate_guard("source") as hosts:
                thread.start()
                self.assertTrue(started.wait(5))
                thread.join(0.1)
                self.assertTrue(thread.is_alive())
                self.assertIsNone(self.api.host_lifecycle.get("source"))
                self.assertEqual(hosts.latest("source")["gate_state"], "available")
        finally:
            if thread.ident is not None:
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["mode"], "draining")
        self.assertEqual(self.api.hosts.latest("source")["gate_state"], "draining")

    def age_source(self):
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-interval '10 minutes' "
                        "WHERE host='source'")

    def detect_loss(self):
        return self.api.host_lifecycle.detect_losses(actor="remote", older_than=180, critical=("dev",))

    def test_loss_requires_stale_heartbeat_and_no_live_lease(self):
        self.age_source()
        self.assertEqual(self.detect_loss(), [])
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        found = self.detect_loss()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["mode"], "lost")
        self.assertIsNone(registry.claim(self.db, "dev", "new", ttl_seconds=300))
        self.assertEqual(self.detect_loss(), [])
        self.assertEqual(self.api.host_lifecycle.agents("source", found[0]["generation"])[0]["critical"], True)

    def test_fresh_or_unobserved_host_is_not_lost(self):
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        self.assertEqual(self.detect_loss(), [])
        self.db.execute("DELETE FROM host_resources WHERE host='source'")
        self.assertEqual(self.detect_loss(), [])
        self.assertIsNone(self.api.host_lifecycle.get("source"))

    def test_loss_does_not_claim_quiescence_or_change_an_expired_running_turn(self):
        self.assertTrue(registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "active"))
        self.age_source()
        self.db.execute("UPDATE agent_registry SET lease_expires_at=clock_timestamp()-interval '1 second' "
                        "WHERE name='dev'")
        before = registry.get(self.db, "dev")
        self.assertEqual(self.detect_loss()[0]["mode"], "lost")
        after = registry.get(self.db, "dev")
        for key in ("status", "lease_owner", "lease_epoch", "lease_expires_ts", "handoff_fenced", "host"):
            self.assertEqual(after[key], before[key])
        self.assertIsNone(self.api.handoffs.active("dev"))
        self.assertIsNone(registry.claim(self.db, "dev", "remote", ttl_seconds=300))

    def test_returning_heartbeat_does_not_reopen_lost_host(self):
        self.age_source()
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        found = self.detect_loss()[0]
        self.api.hosts.record({"host": "source", "gate_state": "available"})
        self.assertEqual(self.api.hosts.latest("source")["gate_state"], "draining")
        self.assertEqual(self.api.host_lifecycle.get("source"), found)
        self.assertIsNone(registry.claim(self.db, "dev", "old", ttl_seconds=300))
        with self.assertRaisesRegex(ValueError, "verified recovery"):
            self.api.host_lifecycle.undrain("source", actor="orch")
        self.assertEqual(self.api.host_lifecycle.get("source"), found)

    def test_loss_preserves_drain_origins_and_cancels_automatic_return(self):
        first = self.drain()
        self.age_source()
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        found = self.detect_loss()[0]
        self.assertEqual(found["generation"], first["generation"])
        self.assertIsNone(found["return_ts"])
        self.assertEqual(len(self.api.host_lifecycle.agents("source", first["generation"])), 1)
        self.assertEqual(self.api.host_lifecycle.expire_returns(), [])

    def test_loss_rechecks_heartbeat_after_waiting_for_gate_lock(self):
        self.age_source()
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        started = threading.Event()
        result, errors = [], []

        def detect():
            other = db_mod.connect(self.cfg)
            try:
                started.set()
                result.extend(storage.of(other).host_lifecycle.detect_losses(actor="remote", older_than=180))
            except Exception as exc:
                errors.append(exc)
            finally:
                other.close()

        thread = threading.Thread(target=detect, daemon=True)
        try:
            with self.db.transaction() as tx:
                tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("host-gate:source",))
                thread.start()
                self.assertTrue(started.wait(5))
                thread.join(0.1)
                self.assertTrue(thread.is_alive())
                storage.of(tx).hosts.record({"host": "source", "gate_state": "available"})
        finally:
            if thread.ident is not None:
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [])
        self.assertIsNone(self.api.host_lifecycle.get("source"))
