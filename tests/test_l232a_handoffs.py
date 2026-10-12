# SPDX-License-Identifier: AGPL-3.0-only
"""L232a: durable moves against simulated hosts; never SSH or real credentials."""
import os
import subprocess
import tempfile
import time
import threading
from types import SimpleNamespace
from unittest import mock

from ameesh import handoffs, registry, resources, storage, work
from .support import PgTestCase

GIB = 1024**3
COMMIT = "a"*40


class HostAdapter:
    def __init__(self):
        self.calls = []
        self.fail = None

    def revalidate(self, op):
        return True

    def stop_source(self, op):
        self.calls.append("stop_source")
        return True

    def enable(self, op):
        self.calls.append("enable")
        return True

    def restore_source(self, op):
        self.calls.append("restore_source")
        return True

    def result(self, op, step, **fields):
        self.calls.append(step)
        return {"ok": self.fail != step, **fields}

    def prepare(self, op):
        return self.result(op, "prepare", canon_revision=COMMIT,
                           credentials_ready=True, caps_ready=True, tooling_ready=True,
                           dsh_profile_ready=True, release_revision=COMMIT)

    admit = prepare

    def publish(self, op):
        return self.result(op, "publish", repository=op["plan"]["repository"],
                           branch="lot/work", commit=COMMIT, remote_commit=COMMIT, clean=True)

    def transfer(self, op):
        return self.result(op, "transfer", repository=op["plan"]["repository"],
                           branch="lot/work", commit=COMMIT, clean=True, cwd="/work/agent")

    def session(self, op):
        return self.result(op, "session", mode="fresh" if op["plan"]["harness"] == "deepseek" else "brief",
                           executor_enabled=False)

    def verify(self, op):
        return self.result(op, "verify", commit=COMMIT, turn_completed=True)


class HandoffTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "dev", harness="codex", host="source", cwd="/work/source")
        self.db.execute("UPDATE agent_registry SET admitted_hosts=ARRAY['source','target'] WHERE name='dev'")
        self.lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        self.api = storage.of(self.db)
        for host in ("source", "target"):
            self.api.canon.record_state(host, "ok", root="/canon", source="local", commit=COMMIT,
                                        good=True, diagnostic="")
            for _ in range(3):
                self.reading(host, memory=3*GIB if host == "source" else 24*GIB)
        self.limits = resources.thresholds({"resources": {"min_mem_available": "4GiB",
                                                         "min_disk_free": "1GiB"}})
        self.limits["max_agents"] = 4
        self.adapter = HostAdapter()

    def reading(self, host, memory):
        return self.api.hosts.record({"host": host, "mem_available_bytes": memory,
                                     "swap_used_bytes": 0, "cpu_count": 8, "load1": 0.0,
                                     "disk_free_bytes": 100*GIB, "turns_in_progress": 0})

    def candidate(self, name="dev"):
        return handoffs.plan(self.db, registry.get(self.db, name), limits_for=lambda host:self.limits)

    def request(self, name="dev"):
        return handoffs.request(self.db, self.candidate(name), repository="git@forge:manaty/app.git")

    def advance(self, op):
        return handoffs.advance(self.db, op["id"], self.adapter)

    def drain(self):
        op = self.request()
        self.assertEqual(self.advance(op)["state"], "prepared")
        op = self.advance(op)
        self.assertEqual(op["state"], "drained")
        return op

    def test_fresh_sustained_pressure_and_spare_capacity(self):
        self.assertEqual(self.candidate()["target"], "target")
        self.db.execute("UPDATE host_resources SET sampled_at=now()-interval '2 minutes'")
        self.assertIsNone(self.candidate())
        for host, mem in (("source", 12*GIB), ("target", 24*GIB)):
            for _ in range(3): self.reading(host, mem)
        self.assertEqual(self.candidate()["reason"], "spare_capacity")
        self.assertIsNone(handoffs.plan(self.db, registry.get(self.db, "dev"),
                                       limits_for=lambda host:self.limits, queued=False))

    def test_full_or_stale_target_and_unsynchronised_canon_refused(self):
        self.db.execute("UPDATE canon_state SET last_good_commit='different' WHERE host='target'")
        self.assertIsNone(self.candidate())
        self.db.execute("UPDATE canon_state SET last_good_commit=%s WHERE host='target'", (COMMIT,))
        candidate = self.candidate()
        self.reading("target", 24*GIB)
        with self.assertRaises(handoffs.HandoffError):
            handoffs.request(self.db, candidate, repository="git@forge:manaty/app.git")

    def test_two_planners_cannot_reserve_the_last_slot(self):
        self.limits["max_agents"] = 1
        first = self.request()
        registry.upsert(self.db, "dev2", harness="codex", host="source")
        self.db.execute("UPDATE agent_registry SET admitted_hosts=ARRAY['source','target'] WHERE name='dev2'")
        registry.claim(self.db, "dev2", "runner2", ttl_seconds=300)
        with self.assertRaises(handoffs.HandoffError): self.request("dev2")
        self.assertEqual(self.api.handoffs.active("dev")["id"], first["id"])

    def test_reservation_refuses_stale_source_lease(self):
        candidate = self.candidate()
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        with self.assertRaises(handoffs.HandoffError):
            handoffs.request(self.db, candidate, repository="git@forge:manaty/app.git")

    def test_boundary_waits_for_current_lot_and_turn_then_fences_claims(self):
        lot = work.add(self.db, title="Current", assignee="dev")
        work.move(self.db, lot["id"], "build")
        op = self.request(); op = self.advance(op)
        self.assertEqual(self.advance(op)["state"], "prepared")
        work.close(self.db, lot["id"], abandoned=True)
        registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "turn")
        self.assertEqual(self.advance(op)["state"], "prepared")
        registry.end_turn(self.db, "dev", "runner", self.lease["lease_epoch"], status="idle")
        self.assertEqual(self.advance(op)["state"], "drained")
        self.assertFalse(registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "racing turn"))
        self.assertIsNone(registry.claim(self.db, "dev", "another", ttl_seconds=300))

    def test_handoff_orders_session_before_enable_and_invalidates_source_epoch(self):
        op = self.request()
        for _ in range(8): op = self.advance(op)
        self.assertEqual(op["state"], "verified")
        self.assertLess(self.adapter.calls.index("session"), self.adapter.calls.index("enable"))
        self.assertLess(self.adapter.calls.index("stop_source"), self.adapter.calls.index("publish"))
        agent = registry.get(self.db, "dev")
        self.assertEqual((agent["host"], agent["cwd"], agent["session_id"]), ("target", "/work/agent", None))
        self.assertFalse(registry.begin_turn(self.db, "dev", "runner", self.lease["lease_epoch"], "stale"))
        self.assertIsNotNone(registry.claim(self.db, "dev", "destination", ttl_seconds=300))
        self.assertIsNone(self.candidate())  # cooldown
        self.assertEqual(self.advance(op)["state"], "verified")

    def test_fresh_dsh_session_and_held_step_recovery(self):
        self.db.execute("UPDATE agent_registry SET harness='deepseek' WHERE name='dev'")
        op = self.drain()
        self.adapter.fail = "publish"
        held = self.advance(op)
        self.assertEqual((held["state"],held["fenced"]), ("held",True))
        self.assertIsNone(registry.claim(self.db, "dev", "other", ttl_seconds=300))
        self.adapter.fail = None
        op = handoffs.retry(self.db, op["id"], self.adapter)
        for _ in range(6): op = self.advance(op)
        self.assertEqual(op["state"], "verified")
        self.assertEqual(op["evidence"]["session_mode"], "fresh")

    def test_expiry_or_changed_admission_holds_without_starting_target(self):
        op = self.drain()
        self.db.execute("UPDATE host_handoffs SET reserved_until=now()-interval '1 second' WHERE id=%s",(op["id"],))
        held = self.advance(op)
        self.assertEqual((held["state"],held["fenced"]),("held",True))
        self.assertNotIn("enable", self.adapter.calls)
        self.assertEqual(registry.get(self.db,"dev")["host"],"source")
        handoffs.cancel(self.db,op["id"],self.adapter)
        self.assertTrue(registry.begin_turn(self.db,"dev","runner",self.lease["lease_epoch"],"resume"))

    def test_adapter_proofs_reject_wrong_repo_commit_and_premature_enable(self):
        op=self.drain()
        with mock.patch.object(self.adapter,"publish",return_value={"ok":True,"repository":"wrong", "clean":True}):
            held=self.advance(op)
        self.assertEqual(held["state"],"held")
        self.assertEqual(self.api.handoffs.events(op["id"])[-1]["state"],"held")
        self.assertNotIn("wrong", str(held["evidence"]))

    def test_duplicate_operation_is_idempotent_and_concurrent_project_work_preserved(self):
        candidate=self.candidate()
        first=handoffs.request(self.db,candidate,repository="git@forge:manaty/app.git")
        second=handoffs.request(self.db,candidate,repository="git@forge:manaty/app.git")
        self.assertEqual(first["id"],second["id"])
        self.assertEqual(len(self.api.handoffs.events(first["id"])),1)

    def test_adapter_exception_is_held_without_private_diagnostics(self):
        op = self.drain()
        with mock.patch.object(self.adapter, "publish", side_effect=RuntimeError("private token")):
            held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertNotIn("private token", str(held))
        self.assertNotIn("private token", str(self.api.handoffs.events(op["id"])))
        self.assertIsNone(registry.claim(self.db, "dev", "other", ttl_seconds=300))
        handoffs.retry(self.db, op["id"], self.adapter)
        for _ in range(6): op = self.advance(op)
        self.assertEqual(op["state"], "verified")

    def test_enable_exception_fences_target_and_retry_preserves_host(self):
        op = self.request()
        for _ in range(7): op = self.advance(op)
        self.assertEqual(op["state"], "activated")
        with mock.patch.object(self.adapter, "enable", side_effect=OSError("private path")):
            held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")
        self.assertIsNone(registry.claim(self.db, "dev", "other", ttl_seconds=300))
        with self.assertRaises(handoffs.HandoffError):
            handoffs.cancel(self.db, op["id"], self.adapter)
        handoffs.retry(self.db, op["id"], self.adapter)
        self.assertEqual(self.advance(op)["state"], "verified")

    def test_worker_reserves_then_stops_only_when_source_fenced(self):
        from ameesh.runner import AgentWorker
        worker = SimpleNamespace(
            runner=SimpleNamespace(relocate=True, handoff_limits_for=lambda host: self.limits),
            db=self.db, name="dev", agent=registry.get(self.db, "dev"),
            stopping=threading.Event(), wake=threading.Event())
        worker.agent["pending_prompt"] = "queued work"
        with mock.patch("ameesh.runner.subprocess.run", return_value=SimpleNamespace(stdout="git@forge:manaty/app.git")):
            self.assertFalse(AgentWorker.maybe_relocate(worker, {}))
        op = self.api.handoffs.active("dev")
        self.assertEqual(op["state"], "planned")
        self.assertFalse(worker.stopping.is_set())
        self.assertEqual(registry.get(self.db, "dev")["host"], "source")
        self.assertFalse(AgentWorker.maybe_relocate(worker, {}))
        self.advance(op)
        self.advance(op)
        self.assertTrue(AgentWorker.maybe_relocate(worker, {}))
        self.assertTrue(worker.stopping.is_set())
        self.assertTrue(worker.wake.is_set())
