# SPDX-License-Identifier: AGPL-3.0-only
"""L232a: durable moves against simulated hosts; never SSH or real credentials."""
import os
import subprocess
import tempfile
import threading
import json
import unittest
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

    def reconcile_effect(self, op):
        # Fake hosts have no surviving async job; a real helper must fence
        # and quiesce this epoch before providing the same proof.
        self.calls.append("reconcile_effect")
        return {"ok": True, "quiesced": True, "epoch": op["evidence"]["pending_effect"]["epoch"]}

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
        return self.result(op, "verify", commit=COMMIT, turn_completed=True, authenticated=True, caps_ready=True)


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

    def reading(self, host, memory, **fields):
        return self.api.hosts.record({"host": host, "mem_available_bytes": memory,
                                     "swap_used_bytes": 0, "cpu_count": 8, "load1": 0.0,
                                     "disk_free_bytes": 100*GIB, "turns_in_progress": 0, **fields})

    def test_withdrawing_destination_is_not_planned(self):
        self.assertIsNotNone(self.candidate())
        for state in ("draining", "stopped"):
            self.reading("target", 24*GIB, gate_state=state)
            self.assertIsNone(self.candidate())
        self.reading("target", 24*GIB, gate_state="available")
        self.assertIsNotNone(self.candidate())

    def test_gate_closing_after_activation_prevents_enable(self):
        op = self.request()
        for _ in range(7): op = self.advance(op)
        self.assertEqual(op["state"], "activated")
        self.reading("target", 24*GIB, gate_state="draining")
        held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertNotIn("enable", self.adapter.calls)

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
        actual_run = subprocess.run
        def git_remote(argv, *args, **kwargs):
            if argv[0] == "git":
                return SimpleNamespace(stdout="git@forge:acme/app.git")
            return actual_run(argv, *args, **kwargs)
        with mock.patch("ameesh.runner.subprocess.run", side_effect=git_remote):
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

    def test_controller_lock_excludes_concurrent_step_and_cancel(self):
        op = self.drain()
        with self.api.handoffs.controller(op["id"]) as locked:
            self.assertIsNotNone(locked)
            with self.assertRaisesRegex(handoffs.HandoffError, "another controller"):
                self.advance(op)
            with self.assertRaisesRegex(handoffs.HandoffError, "another controller"):
                handoffs.cancel(self.db, op["id"], self.adapter)
        self.assertEqual(self.advance(op)["state"], "published")

    def test_lost_lock_during_restore_blocks_activation_until_quiesced(self):
        op = self.drain()
        def restore(intent):
            self.assertTrue(intent["evidence"]["cancel_requested"])
            self.assertEqual(intent["evidence"]["pending_effect"]["name"], "restore_source")
            # Lose only the independent advisory-lock backend while the
            # controller's journal connection is still usable.
            pid = self.db.query(
                "SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted"
                " AND classid=((hashtextextended(%s,0)>>32)&4294967295)::oid"
                " AND objid=(hashtextextended(%s,0)&4294967295)::oid AND objsubid=1",
                ("handoff-operation:"+op["id"],)*2)[0]["pid"]
            self.db.query("SELECT pg_terminate_backend(%s)", (pid,))
            with self.assertRaisesRegex(handoffs.HandoffError, "unfinished host effect"):
                self.advance(op)
            self.assertNotIn("enable", self.adapter.calls)
            return True
        with mock.patch.object(self.adapter, "restore_source", side_effect=restore):
            with self.assertRaisesRegex(handoffs.HandoffError, "ownership lost"):
                handoffs.cancel(self.db, op["id"], self.adapter)
        pending = self.api.handoffs.get(op["id"])
        self.assertEqual(pending["state"], "drained")
        self.assertTrue(pending["fenced"])
        self.assertEqual(pending["evidence"]["pending_effect"]["name"], "restore_source")
        with mock.patch.object(self.adapter, "reconcile_effect", return_value={"ok": True, "quiesced": False}):
            with self.assertRaisesRegex(handoffs.HandoffError, "not quiesced"):
                handoffs.retry(self.db, op["id"], self.adapter)
        reconciled = handoffs.retry(self.db, op["id"], self.adapter)
        self.assertEqual(reconciled["state"], "held")
        self.assertNotIn("pending_effect", reconciled["evidence"])
        # Quiescing the old effect must not change cancellation into a move.
        self.advance(op)
        self.assertNotIn("enable", self.adapter.calls)
        self.assertEqual(handoffs.cancel(self.db, op["id"], self.adapter)["state"], "cancelled")
        self.assertEqual(registry.get(self.db, "dev")["host"], "source")

    def test_stale_controller_epoch_cannot_write_journal(self):
        from ameesh.db import DbError
        op = self.request()
        with self.api.handoffs.controller(op["id"]) as old:
            stale = storage.of(old).handoffs
        with self.api.handoffs.controller(op["id"]):
            with self.assertRaises(DbError):
                stale.transition(op["id"], "planned", "prepared")
        self.assertEqual(self.api.handoffs.get(op["id"])["state"], "planned")

    def test_admission_changed_during_admit_cannot_release_fence(self):
        op = self.request()
        for _ in range(6): op = self.advance(op)
        self.assertEqual(op["state"], "switched")
        def revoke(op):
            self.db.execute("UPDATE agent_registry SET admitted_hosts=ARRAY['source'] WHERE name='dev'")
            return self.adapter.prepare(op)
        with mock.patch.object(self.adapter, "admit", side_effect=revoke):
            held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertNotIn("enable", self.adapter.calls)
        self.assertIsNone(registry.claim(self.db, "dev", "other", ttl_seconds=300))

    def test_reservation_expiring_during_admit_cannot_release_fence(self):
        op = self.request()
        for _ in range(6): op = self.advance(op)
        def admit(intent):
            self.db.execute("UPDATE host_handoffs SET reserved_until=now()-interval '1 second' WHERE id=%s", (op["id"],))
            return self.adapter.prepare(intent)
        with mock.patch.object(self.adapter, "admit", side_effect=admit):
            held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertNotIn("enable", self.adapter.calls)

    def test_reservation_expiring_after_unfence_never_enables(self):
        from ameesh.storage.postgres.handoffs import Handoffs
        op = self.request()
        for _ in range(7): op = self.advance(op)
        original = Handoffs.unfence
        def expire(api, operation, expected, state):
            result = original(api, operation, expected, state)
            if result is not None:
                self.db.execute("UPDATE host_handoffs SET reserved_until=now()-interval '1 second' WHERE id=%s", (op["id"],))
            return result
        with mock.patch.object(Handoffs, "unfence", new=expire):
            held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertNotIn("enable", self.adapter.calls)

    def test_changed_canon_or_pause_refuses_destination_retry(self):
        op = self.request()
        for _ in range(7): op = self.advance(op)
        with mock.patch.object(self.adapter, "enable", return_value=False):
            held = self.advance(op)
        self.assertEqual(held["state"], "held")
        self.db.execute("UPDATE canon_state SET last_good_commit='changed' WHERE host='target'")
        handoffs.retry(self.db, op["id"], self.adapter)
        self.assertEqual(self.advance(op)["state"], "held")
        self.db.execute("UPDATE canon_state SET last_good_commit=%s WHERE host='target'", (COMMIT,))
        self.db.execute("UPDATE agent_registry SET status='stopped' WHERE name='dev'")
        handoffs.retry(self.db, op["id"], self.adapter)
        self.assertEqual(self.advance(op)["state"], "held")
        self.assertEqual(registry.get(self.db, "dev")["status"], "stopped")
        self.assertEqual(self.adapter.calls.count("enable"), 0)

    def test_enable_sees_committed_fence_release_and_pending_turn_is_not_failed(self):
        from ameesh import db as db_mod
        op = self.request()
        for _ in range(7): op = self.advance(op)
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        def enable(op):
            agent = registry.get(other, "dev")
            self.assertFalse(other.query("SELECT handoff_fenced FROM agent_registry WHERE name='dev'")[0]["handoff_fenced"])
            self.assertEqual(agent["host"], "target")
            return True
        with mock.patch.object(self.adapter, "enable", side_effect=enable), mock.patch.object(
                self.adapter, "verify", return_value={"ok": True, "pending": True}):
            waiting = self.advance(op)
        self.assertEqual((waiting["state"], waiting["fenced"]), ("activated", False))
        self.assertEqual(self.advance(op)["state"], "verified")

    def test_changed_agent_canon_before_switch_holds_source(self):
        op = self.request()
        for _ in range(5): op = self.advance(op)
        self.assertEqual(op["state"], "session_ready")
        self.db.execute("UPDATE agent_registry SET canon='other' WHERE name='dev'")
        held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertEqual(registry.get(self.db, "dev")["host"], "source")
        self.assertNotIn("enable", self.adapter.calls)

    def test_expired_destination_requires_explicit_reconciled_renewal(self):
        op = self.request()
        for _ in range(7): op = self.advance(op)
        self.db.execute("UPDATE host_handoffs SET reserved_until=now()-interval '1 second' WHERE id=%s", (op["id"],))
        held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        with self.assertRaises(handoffs.HandoffError):
            handoffs.retry(self.db, op["id"], self.adapter)
        self.db.execute("UPDATE host_resources SET sampled_at=now()-interval '2 minutes'")
        with self.assertRaises(handoffs.HandoffError):
            handoffs.retry(self.db, op["id"], self.adapter, renew_ttl=300)
        self.reading("target", 24*GIB)
        with mock.patch.object(self.adapter, "revalidate", return_value=False):
            with self.assertRaises(handoffs.HandoffError):
                handoffs.retry(self.db, op["id"], self.adapter, renew_ttl=300)
        op = handoffs.retry(self.db, op["id"], self.adapter, renew_ttl=300)
        self.assertEqual(op["state"], "activated")
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")
        self.assertEqual(self.advance(op)["state"], "verified")
        self.assertNotIn("restore_source", self.adapter.calls)

    def test_cli_show_reads_journal_without_activation(self):
        op = self.drain()
        proc = self.mesh("handoff", "show", op["id"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["operation"]["state"], "drained")
        self.assertEqual(result["events"][-1]["state"], "drained")
        self.assertEqual(registry.get(self.db, "dev")["host"], "source")
        missing = self.mesh("handoff", "show", "missing")
        self.assertNotEqual(missing.returncode, 0)

    def test_repository_credentials_cannot_enter_reservation(self):
        for repository in ("https://user:secret@forge/app.git", "https://forge/app.git?token=secret"):
            with self.assertRaises(handoffs.HandoffError):
                handoffs.request(self.db, self.candidate(), repository=repository)
        self.assertIsNone(self.api.handoffs.active("dev"))

    def test_legacy_canon_move_cannot_bypass_active_handoff(self):
        op = self.request()
        self.api.canon.move_host("dev", "source", target="target", canon_ref="card",
                                 diagnostic="legacy", ref="placement")
        self.assertEqual(registry.get(self.db, "dev")["host"], "source")
        self.assertEqual(self.api.handoffs.get(op["id"])["state"], "planned")

    def test_admission_changed_after_activation_never_enables_destination(self):
        op = self.request()
        for _ in range(7): op = self.advance(op)
        self.db.execute("UPDATE agent_registry SET admitted_hosts=ARRAY['source'] WHERE name='dev'")
        held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertNotIn("enable", self.adapter.calls)

    def test_stale_canon_writer_cannot_restore_source_after_switch(self):
        values = dict(registry.get(self.db, "dev"))
        values.update(placement_ok=True, placement_diagnostic="", placement_ref="placement", canon_ref="card")
        op = self.request()
        for _ in range(6): op = self.advance(op)
        self.assertEqual(op["state"], "switched")
        self.assertFalse(self.api.canon.write_declared("dev", values))
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")
        self.assertEqual(registry.get(self.db, "dev")["cwd"], "/work/agent")

    def test_stale_canon_writer_cannot_restore_source_after_verified(self):
        values = dict(registry.get(self.db, "dev"))
        values.update(placement_ok=True, placement_diagnostic="", placement_ref="placement", canon_ref="card")
        op = self.request()
        for _ in range(8): op = self.advance(op)
        self.assertEqual(op["state"], "verified")
        self.assertFalse(self.api.canon.write_declared("dev", values))
        self.assertEqual(registry.get(self.db, "dev")["host"], "target")
        self.assertEqual(registry.get(self.db, "dev")["cwd"], "/work/agent")
        # The epoch also closes an ABA placement race on the same host.
        self.db.execute("UPDATE agent_registry SET host='source',cwd='/work/new' WHERE name='dev'")
        self.assertFalse(self.api.canon.write_declared("dev", values))
        self.assertEqual(registry.get(self.db, "dev")["cwd"], "/work/new")

    def test_changed_release_and_unauthenticated_turn_hold_operation(self):
        op = self.request()
        for _ in range(6): op = self.advance(op)
        changed = self.adapter.prepare(op)
        changed["release_revision"] = "b"*40
        with mock.patch.object(self.adapter, "admit", return_value=changed):
            self.assertEqual(self.advance(op)["state"], "held")
        self.assertNotIn("enable", self.adapter.calls)
        handoffs.retry(self.db, op["id"], self.adapter)
        op = self.advance(op)
        with mock.patch.object(self.adapter, "verify", return_value={"ok": True, "commit": COMMIT,
                "turn_completed": True, "authenticated": False, "caps_ready": True}):
            self.assertEqual(self.advance(op)["state"], "held")

    def test_malformed_adapter_proof_is_held(self):
        op = self.request()
        malformed = self.adapter.prepare(op)
        malformed["release_revision"] = 123
        with mock.patch.object(self.adapter, "prepare", return_value=malformed):
            self.assertEqual(self.advance(op)["state"], "held")
        self.assertNotIn("123", str(self.api.handoffs.get(op["id"])["evidence"]))

    def test_simultaneous_planners_reserve_one_physical_slot(self):
        from concurrent.futures import ThreadPoolExecutor
        from ameesh import db as db_mod
        self.limits["max_agents"] = 1
        registry.upsert(self.db, "dev2", harness="codex", host="source")
        self.db.execute("UPDATE agent_registry SET admitted_hosts=ARRAY['source','target'] WHERE name='dev2'")
        registry.claim(self.db, "dev2", "runner2", ttl_seconds=300)
        candidates = [self.candidate(name) for name in ("dev", "dev2")]
        ready = threading.Barrier(2)
        def reserve(candidate):
            other = db_mod.connect(self.cfg)
            try:
                ready.wait(timeout=10)
                try:
                    return handoffs.request(other, candidate, repository="git@forge:acme/app.git")["id"]
                except handoffs.HandoffError:
                    return None
            finally:
                other.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(reserve, candidates))
        self.assertEqual(sum(value is not None for value in result), 1)

    def test_waiting_begin_turn_observes_committed_boundary_fence(self):
        from ameesh import db as db_mod
        op = self.request()
        self.advance(op)
        started = threading.Event()
        result = []
        def begin_turn():
            other = db_mod.connect(self.cfg)
            try:
                started.set()
                result.append(registry.begin_turn(other, "dev", "runner", self.lease["lease_epoch"], "racing"))
            finally:
                other.close()
        thread = threading.Thread(target=begin_turn)
        try:
            with self.db.transaction() as tx:
                tx.query("SELECT name FROM agent_registry WHERE name='dev' FOR UPDATE")
                thread.start()
                self.assertTrue(started.wait(10))
                thread.join(0.1)
                self.assertTrue(thread.is_alive())
                self.assertEqual(storage.of(tx).handoffs.drain(op["id"])["state"], "drained")
        finally:
            if thread.ident is not None:
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])

    def test_agent_named_canon_is_used_instead_of_default_host_canon(self):
        self.db.execute("UPDATE agent_registry SET canon='other' WHERE name='dev'")
        self.assertIsNone(self.candidate())
        for host in ("source", "target"):
            self.api.canon.record_state(host, "ok", root="/other", source="local", commit=COMMIT,
                                        good=True, diagnostic="", canon="other")
        op = self.request()
        self.assertEqual(op["plan"]["canon"], "other")
        for _ in range(8): op = self.advance(op)
        self.assertEqual(op["state"], "verified")

    def test_destination_checkout_path_changed_before_enable_holds(self):
        op = self.request()
        for _ in range(7): op = self.advance(op)
        self.db.execute("UPDATE agent_registry SET cwd='/other/worktree' WHERE name='dev'")
        held = self.advance(op)
        self.assertEqual((held["state"], held["fenced"]), ("held", True))
        self.assertNotIn("enable", self.adapter.calls)


class PublishedCheckoutTest(unittest.TestCase):
    def test_actual_remote_identity_clean_exact_pushed_commit(self):
        with tempfile.TemporaryDirectory(prefix="handoff-git-") as root:
            remote = os.path.join(root, "origin.git")
            checkout = os.path.join(root, "checkout")
            def git(*args):
                return subprocess.run(["git", *args], text=True, capture_output=True, check=True).stdout.strip()
            git("init", "--bare", remote)
            git("init", "-b", "lot/work", checkout)
            git("-C", checkout, "config", "user.name", "Test")
            git("-C", checkout, "config", "user.email", "test@example.invalid")
            git("-C", checkout, "commit", "--allow-empty", "-m", "published")
            git("-C", checkout, "remote", "add", "origin", remote)
            git("-C", checkout, "push", "origin", "lot/work")
            proof = handoffs.published_checkout(checkout, remote)
            self.assertEqual(proof["commit"], git("-C", checkout, "rev-parse", "HEAD"))
            with self.assertRaisesRegex(handoffs.HandoffError, "identity mismatch"):
                handoffs.published_checkout(checkout, remote + "-other")
            with open(os.path.join(checkout, "untracked"), "w") as file:
                file.write("local work")
            with self.assertRaisesRegex(handoffs.HandoffError, "dirty"):
                handoffs.published_checkout(checkout, remote)
            os.unlink(os.path.join(checkout, "untracked"))
            git("-C", checkout, "commit", "--allow-empty", "-m", "unpublished")
            with self.assertRaisesRegex(handoffs.HandoffError, "remote commit differs"):
                handoffs.published_checkout(checkout, remote)
