# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit evacuation uses ready admitted hosts without pressure or lot completion."""
import time

from ameesh import handoffs, host_lifecycle, host_preflight, registry, resources, storage, work
from . import test_l232a_handoffs as fixtures
from .support import PgTestCase


class Adapter(fixtures.HostAdapter):
    def inventory(self, host):
        return {"host": host, "checked_ts": time.time(), "connected": True,
                "tools": {"gh": True, "gcloud": True}, "secrets": {"mesh/api": True}}

    def repository(self, agent):
        return "git@forge:manaty/app.git"


class DrainHandoffTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle,host_resources,credential_health CASCADE")
        self.api = storage.of(self.db)
        registry.upsert(self.db, "dev", harness="codex", host="source", cwd="/work/source")
        self.db.execute("UPDATE agent_registry SET admitted_hosts=ARRAY['source','target'] WHERE name='dev'")
        self.lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        for host in ("source", "target"):
            self.api.canon.record_state(host, "ok", root="/canon", source="local", commit=fixtures.COMMIT,
                                        good=True, diagnostic="")
            for _ in range(3):
                self.api.hosts.record({"host": host, "mem_available_bytes": 24*fixtures.GIB,
                                      "swap_used_bytes": 0, "cpu_count": 8, "load1": 0.0,
                                      "disk_free_bytes": 100*fixtures.GIB, "gate_state": "available"})
            for harness in ("codex", "gh", "gcloud"):
                self.api.credentials.report(host, harness, "", "healthy", source="manual")
        self.limits = dict(resources.thresholds({"resources": {"min_mem_available": "4GiB",
                                                               "min_disk_free": "1GiB"}}), max_agents=4)
        self.adapter = Adapter()

    def requirements(self, agent, target):
        return host_preflight.Requirements(target, agent["harness"], secret_refs=("mesh/api",))

    def step(self):
        return host_lifecycle.advance_host(self.db, "source", limits_for=lambda host: self.limits,
                                           adapter=self.adapter, requirements_for=self.requirements)

    def test_drain_moves_active_lot_at_turn_boundary_without_source_pressure(self):
        row = registry.get(self.db, "dev")
        self.assertIsNone(handoffs.plan(self.db, row, limits_for=lambda host: self.limits))
        lot = work.add(self.db, title="Ongoing", assignee="dev", app="p")
        work.move(self.db, lot["id"], "build")
        self.api.host_lifecycle.drain("source", actor="orch", critical=("dev",))
        for _ in range(9):
            result = self.step()[0]
        self.assertEqual(result["operation"]["state"], "verified")
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")
        self.assertEqual(work.get(self.db, lot["id"])["state"], "build")
        self.assertEqual(self.adapter.calls.count("enable"), 1)

    def test_running_turn_finishes_before_source_fence(self):
        self.assertTrue(registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "active"))
        self.api.host_lifecycle.drain("source", actor="orch")
        for _ in range(3):
            result = self.step()[0]
        self.assertEqual(result["operation"]["state"], "prepared")
        self.assertNotIn("stop_source", self.adapter.calls)
        registry.end_turn(self.db, "dev", "runner", self.lease["lease_epoch"], status="idle")
        self.assertEqual(self.step()[0]["operation"]["state"], "drained")

    def test_missing_helper_and_secret_requirements_are_named_without_host_effects(self):
        self.api.host_lifecycle.drain("source", actor="orch")
        result = host_lifecycle.advance_host(self.db, "source", limits_for=lambda host: self.limits)
        self.assertEqual(result[0]["missing"], ["helper:L232b"])
        result = host_lifecycle.readiness(self.db, registry.get(self.db, "dev"), "target", adapter=self.adapter)
        self.assertIn("requirements:L232c", result["missing"])
        self.assertIsNone(self.api.handoffs.active("dev"))
        self.assertEqual(self.adapter.calls, [])

    def test_failed_receiving_host_login_refuses_before_reservation(self):
        self.api.credentials.report("target", "codex", "", "failing", source="manual")
        self.api.host_lifecycle.drain("source", actor="orch")
        result = self.step()[0]
        self.assertEqual(result["missing"], ["login:codex/default"])
        self.assertIsNone(result["operation"])
        self.assertEqual(self.adapter.calls, [])

    def test_request_superseded_before_reservation_is_rejected(self):
        self.api.host_lifecycle.drain("source", actor="orch")
        row = registry.get(self.db, "dev")
        demand = self.api.host_lifecycle.demand("dev", "source")
        plan = handoffs.plan(self.db, row, limits_for=lambda host: self.limits, demand=demand,
                             readiness_for=lambda agent, target: {"ready": True})
        self.api.host_lifecycle.undrain("source", actor="orch")
        with self.assertRaisesRegex(handoffs.HandoffError, "changed"):
            handoffs.request(self.db, plan, repository=self.adapter.repository(row))
        self.assertIsNone(self.api.handoffs.active("dev"))

    def test_undrain_brings_agent_back_despite_same_capacity_and_cooldown(self):
        self.api.host_lifecycle.drain("source", actor="orch")
        for _ in range(9):
            self.step()
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")
        self.assertIsNotNone(registry.claim(self.db, "dev", "target-runner", ttl_seconds=300))
        self.api.host_lifecycle.undrain("source", actor="orch")
        for _ in range(9):
            result = self.step()[0]
        self.assertEqual(result["operation"]["state"], "verified")
        self.assertEqual(registry.get(self.db, "dev")["host"], "source")
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "available")
        self.assertEqual(self.adapter.calls.count("enable"), 2)

    def test_undrain_cancels_prepared_evacuation_before_source_transfer(self):
        self.api.host_lifecycle.drain("source", actor="orch")
        self.step()
        self.assertEqual(self.step()[0]["operation"]["state"], "prepared")
        self.api.host_lifecycle.undrain("source", actor="orch")
        result = self.step()[0]
        self.assertEqual(result["operation"]["state"], "cancelled")
        self.assertEqual(registry.get(self.db, "dev")["host"], "source")
        self.assertNotIn("enable", self.adapter.calls)
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "available")

    def test_elapsed_duration_requests_return_without_forging_host_reading(self):
        self.api.host_lifecycle.drain("source", actor="orch", duration=30)
        before = self.api.hosts.latest("source")["sampled_ts"]
        self.db.execute("UPDATE host_lifecycle SET return_at=clock_timestamp()-interval '1 second'")
        self.step()
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "available")
        self.assertEqual(self.api.hosts.latest("source")["sampled_ts"], before)
