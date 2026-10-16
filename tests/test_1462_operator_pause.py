# SPDX-License-Identifier: AGPL-3.0-only
"""L87b: holds survive turns/leases; resume affects only the operator flag."""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import threading
import unittest
from unittest import mock

from ameesh import agent_pause, progress, registry, reprise, serve, storage
from ameesh.runner import AgentWorker

from .support import PgTestCase
from .test_l87_serve_actions import PostMixin


class PauseTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "worker", harness="deepseek", host=self.cfg.host,
                        cwd=self.tmp)

    def change(self, paused=True, **kw):
        args = dict(paused=paused, reason="operator maintenance", actor="human:alice")
        args.update(kw)
        return agent_pause.change(self.db, "worker", **args)

    def events(self):
        return self.db.query("SELECT * FROM operator_pause_events ORDER BY id")

    def claim(self):
        lease = registry.claim(self.db, "worker", "runner-test", 3600)
        self.assertIsNotNone(lease)
        return lease["lease_epoch"]

    def test_repeated_transition_is_a_noop_and_audits_actor_principal(self):
        self.assertTrue(self.change(principal="human:bob")["changed"])
        self.assertFalse(self.change(reason="another reason")["changed"])
        row = registry.get(self.db, "worker")
        self.assertEqual(row["operator_pause_reason"], "operator maintenance")
        self.assertEqual((row["operator_pause_by"], row["operator_pause_for"]),
                         ("human:alice", "human:bob"))
        self.assertTrue(self.change(False, reason="maintenance done")["changed"])
        self.assertFalse(self.change(False)["changed"])
        self.assertEqual([(e["paused"], e["actor"], e["principal"]) for e in self.events()],
                         [(True, "human:alice", "human:bob"),
                          (False, "human:alice", "human:alice")])

    def test_pending_prompt_is_not_consumed_or_replaced(self):
        registry.set_pending_prompt(self.db, "worker", "keep this instruction")
        epoch = self.claim()
        self.change()
        self.assertIsNone(registry.take_pending_prompt(self.db, "worker", "runner-test", epoch))
        self.assertFalse(registry.begin_turn(self.db, "worker", "runner-test", epoch, "mail"))
        self.change(False)
        self.assertEqual(registry.take_pending_prompt(self.db, "worker", "runner-test", epoch),
                         "keep this instruction")

    def test_active_turn_finishes_but_the_next_turn_waits(self):
        registry.set_pending_prompt(self.db, "worker", "active instruction")
        epoch = self.claim()
        registry.take_pending_prompt(self.db, "worker", "runner-test", epoch)
        before = registry.get(self.db, "worker")
        self.change()
        during = registry.get(self.db, "worker")
        for field in ("status", "status_text", "current_prompt", "session_id",
                      "lease_owner", "lease_epoch", "lease_expires_ts"):
            self.assertEqual(during[field], before[field], field)
        self.assertTrue(registry.end_turn(self.db, "worker", "runner-test", epoch,
                                         status="idle", status_text="turn finished"))
        self.assertTrue(registry.get(self.db, "worker")["operator_paused"])
        self.assertFalse(registry.begin_turn(self.db, "worker", "runner-test", epoch, "mail"))
        self.change(False)
        self.assertTrue(registry.begin_turn(self.db, "worker", "runner-test", epoch, "mail"))

    def test_resume_preserves_budget_block_and_stopped_agent(self):
        for status, text in (("blocked", "paused: budget"), ("stopped", "owner stop")):
            with self.subTest(status=status):
                registry.set_status(self.db, "worker", status, status_text=text)
                self.change()
                self.change(False)
                row = registry.get(self.db, "worker")
                self.assertEqual((row["status"], row["status_text"]), (status, text))
                self.assertIsNone(row["pending_prompt"])

    def test_lease_replacement_does_not_lift_the_hold(self):
        self.change()
        epoch = self.claim()
        registry.release(self.db, "worker", "runner-test", epoch)
        epoch = self.claim()
        self.assertTrue(registry.get(self.db, "worker")["operator_paused"])
        self.assertFalse(registry.begin_turn(self.db, "worker", "runner-test", epoch, "mail"))

    def test_concurrent_pauses_produce_one_transition(self):
        barrier = threading.Barrier(2)

        def pause():
            db = self.connect()
            try:
                barrier.wait(timeout=10)
                return agent_pause.change(db, "worker", paused=True, reason="maintenance",
                                          actor="human:alice")["changed"]
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(pause) for _ in range(2)]
            self.assertEqual(sorted(t.result(timeout=20) for t in tasks), [False, True])
        self.assertEqual(len(self.events()), 1)

    def test_audit_and_flag_roll_back_together(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.transaction() as tx:
                agent_pause.change(tx, "worker", paused=True, reason="maintenance",
                                   actor="human:alice")
                raise RuntimeError("rollback")
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])
        self.assertEqual(self.events(), [])

    def test_sql_entrypoint_has_no_public_execution_or_definer_privilege(self):
        row = self.db.query(
            "SELECT prosecdef, EXISTS (SELECT 1 FROM aclexplode(proacl) a "
            "WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') AS public_exec "
            "FROM pg_proc WHERE oid = "
            "'ameesh_operator_pause(text,boolean,text,text,text)'::regprocedure")[0]
        self.assertEqual(row, {"prosecdef": False, "public_exec": False})

    def test_operator_resume_wakes_the_runner_after_commit(self):
        name = self.schema
        registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host)
        agent_pause.change(self.db, name, paused=True, reason="maintenance", actor="human:alice")
        listener = storage.of(self.db).wakeups.subscribe(["agent_lease"])
        self.assertIsNotNone(listener)
        self.addCleanup(listener.close)
        agent_pause.change(self.db, name, paused=False, reason="done", actor="human:alice")
        for _ in range(20):
            event = listener.wait(timeout=0.5)
            if event and event.get("channel") == "agent_lease" \
                    and json.loads(event["payload"]).get("agent") == name:
                break
        else:
            self.fail("operator resume did not wake the runner")

    def test_unknown_external_and_invalid_requests_have_no_effect(self):
        registry.set_mode(self.db, "worker", "externe")
        with self.assertRaises(agent_pause.PauseRefused):
            self.change()
        for fields in ({"actor": "agent:worker"}, {"reason": " "},
                       {"reason": "x" * 2001}, {"principal": "agent:worker"}):
            with self.subTest(fields=fields), self.assertRaises(agent_pause.PauseRefused):
                self.change(**fields)
        with self.assertRaises(agent_pause.PauseRefused):
            agent_pause.change(self.db, "unknown", paused=True, reason="maintenance",
                               actor="human:alice")
        self.assertEqual(self.events(), [])

    def test_runner_pick_does_not_consume_mail_or_check_budget_while_paused(self):
        self.change()
        worker = object.__new__(AgentWorker)
        threading.Thread.__init__(worker)
        worker.db, worker.name = self.db, "worker"
        worker.stopping = threading.Event()
        with mock.patch.object(worker, "budget_ok", side_effect=AssertionError("budget called")):
            self.assertIsNone(worker._pick())
        self.assertFalse(worker.stopping.is_set())

    def test_pause_between_pick_and_run_restores_the_prompt(self):
        epoch = self.claim()
        registry.set_pending_prompt(self.db, "worker", "picked prompt")
        registry.take_pending_prompt(self.db, "worker", "runner-test", epoch)
        self.change()
        worker = object.__new__(AgentWorker)
        threading.Thread.__init__(worker)
        worker.db, worker.name, worker.epoch = self.db, "worker", epoch
        worker.state_dir, worker.resident = self.tmp, None
        worker.runner = mock.Mock(runner_id="runner-test")
        with mock.patch.object(worker, "_run_turn", side_effect=AssertionError("turn launched")):
            self.assertFalse(worker.run_turn({"kind": "prompt", "prompt": "picked prompt"}))
        row = registry.get(self.db, "worker")
        self.assertEqual(row["pending_prompt"], "picked prompt")
        self.assertTrue(row["operator_paused"])
        self.assertEqual(row["status"], "queued")

    def test_due_rotation_waits_while_paused_without_cancelling(self):
        self.change()
        worker = object.__new__(AgentWorker)
        threading.Thread.__init__(worker)
        worker.db, worker.name, worker.proc = self.db, "worker", None
        worker.lock, worker.resident = threading.Lock(), None
        worker.rotation_forcee = "forced by restart"
        worker.runner = mock.Mock()
        with mock.patch.object(worker, "_held_by_pressure", return_value=False), \
                mock.patch.object(worker, "_rotate", side_effect=AssertionError("rotated")), \
                mock.patch.object(worker, "budget_ok", side_effect=AssertionError("budget")):
            self.assertFalse(worker.maybe_rotate())
        self.assertEqual(worker.rotation_forcee, "forced by restart")

    def test_historical_resume_cannot_queue_over_operator_pause(self):
        self.change()
        with self.assertRaisesRegex(reprise.RepriseError, "--operator"):
            reprise.resume(self.cfg, self.db, "worker")
        self.assertIsNone(registry.get(self.db, "worker")["pending_prompt"])

    def test_progress_and_registry_distinguish_operator_pause(self):
        self.change()
        row = next(r for r in storage.of(self.db).progress.agents(None) if r["name"] == "worker")
        view = progress.build_agent(row, 100)
        self.assertEqual(view["state"], "paused")
        self.assertEqual(view["operator_pause"]["reason"], "operator maintenance")
        row.update(status="running", lease_live=True)
        view = progress.build_agent(row, 100)
        self.assertEqual(view["state"], "working")
        self.assertTrue(view["operator_pause"]["after_turn"])
        overview = next(r for r in registry.overview(self.db) if r["name"] == "worker")
        self.assertTrue(overview["operator_paused"])

    def test_cli_pause_and_operator_resume_are_idempotent(self):
        for argv in (("pause", "worker"), ("pause", "worker"),
                     ("resume", "worker", "--operator"), ("resume", "worker", "--operator")):
            out = self.mesh(*argv, "--human", "human:alice", "--reason", "maintenance", "--json")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(len(self.events()), 2)
        self.assertIsNone(registry.get(self.db, "worker")["pending_prompt"])

    def test_cli_agent_identity_cannot_become_a_human_even_with_a_stale_lease(self):
        out = self.mesh("pause", "worker", "--reason", "maintenance", "--human", "human:alice",
                        env=self.env(AGENT_MAIL_NAME="worker", AMEESH_RUNNER_ID="stale-runner",
                                     AMEESH_LEASE_EPOCH="99"))
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])

    def test_real_runner_keeps_work_queued_then_runs_after_operator_resume(self):
        registry.set_pending_prompt(self.db, "worker", "do the queued work")
        self.change()
        out = self.runner("--once", "--agents", "worker", "--wait", "0")
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertEqual(self.turns(), [])
        self.assertEqual(registry.get(self.db, "worker")["pending_prompt"], "do the queued work")
        self.change(False)
        out = self.runner("--once", "--agents", "worker", "--wait", "0")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(len(self.turns()), 1)


class PauseApiTest(PostMixin, PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE api_idempotency")
        registry.upsert(self.db, "worker", harness="deepseek", host=self.cfg.host)
        self.start(serve.Context(cfg=dataclasses.replace(self.cfg, read_only=True),
                                 human="human:owner"),
                   serve.db_reader(self.cfg), write=serve.db_writer(self.cfg))

    def test_pause_replay_and_resume_keep_identity_and_pending_prompt(self):
        registry.set_pending_prompt(self.db, "worker", "keep this instruction")
        path = "/api/v1/agents/worker/pause"
        body = {"reason": "maintenance"}
        with mock.patch.dict(os.environ, {"AGENT_MAIL_NAME": "pretend-agent"}):
            status, _h, first = self.post(path, body)
        self.assertEqual(status, 200, first)
        self.assertEqual((first["action"], first["by"], first["for"]),
                         ("pause", "human:owner", "human:owner"))
        status, headers, again = self.post(path, body)
        self.assertEqual((status, again, headers.get("Idempotent-Replayed")), (200, first, "true"))
        status, _h, noop = self.post(path, body, key="pause-another-key")
        self.assertEqual(status, 200, noop)
        self.assertFalse(noop["result"]["changed"])
        events = self.db.query("SELECT actor, principal FROM operator_pause_events")
        self.assertEqual(events, [{"actor": "human:owner", "principal": "human:owner"}])
        status, detail = self.get_json("/api/v1/agents/worker")
        self.assertEqual(status, 200, detail)
        self.assertTrue(detail["agent"]["operator_paused"])
        status, _h, resumed = self.post("/api/v1/agents/worker/resume", body,
                                        key="resume-another-key")
        self.assertEqual(status, 200, resumed)
        row = registry.get(self.db, "worker")
        self.assertFalse(row["operator_paused"])
        self.assertEqual(row["pending_prompt"], "keep this instruction")

    def test_new_routes_require_key_and_cannot_override_the_actor(self):
        path = "/api/v1/agents/worker/pause"
        for fields, key in (({"reason": "maintenance"}, None),
                            ({"reason": "maintenance", "actor": "human:other"}, "actor-key1"),
                            ({"reason": "maintenance", "for": "human:other"}, "actor-key2"),
                            ({}, "empty-key1")):
            with self.subTest(fields=fields):
                status, _h, out = self.post(path, fields, key=key)
                self.assertEqual(status, 400, out)
        self.assertFalse(registry.get(self.db, "worker")["operator_paused"])
        self.assertEqual(self.db.query("SELECT id FROM operator_pause_events"), [])

    def test_resume_without_operator_pause_does_not_restart_a_stopped_agent(self):
        registry.set_status(self.db, "worker", "stopped", status_text="owner stopped it")
        status, _h, out = self.post("/api/v1/agents/worker/resume", {"reason": "checked"})
        self.assertEqual(status, 200, out)
        self.assertFalse(out["result"]["changed"])
        row = registry.get(self.db, "worker")
        self.assertEqual(row["status"], "stopped")
        self.assertIsNone(row["pending_prompt"])

    def test_unknown_and_external_agents_are_refused(self):
        status, _h, out = self.post("/api/v1/agents/ghost/pause", {"reason": "checked"})
        self.assertEqual((status, out["error"]), (404, "not_found"))
        registry.set_mode(self.db, "worker", "externe")
        status, _h, out = self.post("/api/v1/agents/worker/pause", {"reason": "checked"},
                                    key="external-key1")
        self.assertEqual((status, out["error"]), (409, "refused"))


if __name__ == "__main__":
    unittest.main()
