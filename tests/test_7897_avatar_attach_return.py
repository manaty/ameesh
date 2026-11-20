# SPDX-License-Identifier: AGPL-3.0-only
"""Attach is the explicit workstation return gesture; switch stays at boundary."""
from __future__ import annotations

import io
import itertools
import os
import dataclasses
import contextlib
from unittest import mock

from ameesh import avatar_attach as A, conversation_move as M, registry, storage
from .test_7052_avatar_b2 import Base, ready


class ReturnTest(Base):
    def setUp(self):
        super().setUp()
        self.hold(self.CLOUD)

    def test_healthy_attach_plans_once_and_preserves_source_session_until_switch(self):
        first = A.plan_return(self.db, "pilote", self.POSTE)
        second = A.plan_return(self.db, "pilote", self.POSTE)
        self.assertIsNotNone(first)
        self.assertEqual(first["move_id"], second["move_id"])
        self.assertEqual(first["reason"], "manual")
        self.assertEqual(first["requested_by"], "attach:" + self.POSTE)
        self.assertEqual(self.row()["host"], self.CLOUD)
        self.assertEqual(self.row()["session_id"], "sess-source")
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: "/work/local")
        self.assertEqual(self.row()["host"], self.POSTE)
        self.assertEqual(self.moves().get(first["move_id"])["state"], "switched")

    def moves(self):
        return storage.of(self.db).conversation_moves

    def test_attach_joins_preference_during_cloud_turn(self):
        self.db.execute("UPDATE agent_registry SET status = 'running',"
                        " current_prompt = 'cloud turn' WHERE name = 'pilote'")
        move = M.plan(self.db, "pilote", self.POSTE,
                      requested_by="runner", reason="preference")
        joined = A.plan_return(self.db, "pilote", self.POSTE)
        self.assertEqual(joined["move_id"], move["move_id"])
        self.db.execute("UPDATE agent_registry SET status = 'idle', current_prompt = NULL"
                        " WHERE name = 'pilote'")
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
        self.assertEqual(self.row()["host"], self.POSTE)
        self.assertIn("Restore the conversation", self.row()["pending_prompt"])

    def test_attach_joins_preference_after_switch(self):
        move = M.plan(self.db, "pilote", self.POSTE,
                      requested_by="runner", reason="preference")
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
        self.assertIsNone(self.row()["pending_prompt"])
        joined = A.plan_return(self.db, "pilote", self.POSTE)
        self.assertEqual(joined["move_id"], move["move_id"])
        self.assertIn("Restore the conversation", self.row()["pending_prompt"])

    def test_attach_after_switch_preserves_waiting_input(self):
        M.plan(self.db, "pilote", self.POSTE, requested_by="runner", reason="preference")
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
        registry.set_pending_prompt(self.db, "pilote", "human input")
        self.assertIsNotNone(A.plan_return(self.db, "pilote", self.POSTE))
        self.assertEqual(self.row()["pending_prompt"], "human input")

    def test_attach_joins_if_switch_wins_the_registry_lock(self):
        M.plan(self.db, "pilote", self.POSTE, requested_by="runner", reason="preference")
        real_plan = M.plan
        def switch_first(*args, **kwargs):
            M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
            return real_plan(*args, **kwargs)
        with mock.patch.object(M, "plan", side_effect=switch_first):
            joined = A.plan_return(self.db, "pilote", self.POSTE)
        self.assertIsNotNone(joined)
        self.assertIn("Restore the conversation", self.row()["pending_prompt"])

    def test_unhealthy_workstation_does_not_return(self):
        self.standing("pilote", self.POSTE, 10, ready=False)
        self.assertIsNone(A.plan_return(self.db, "pilote", self.POSTE))
        self.assertIsNone(self.moves().open_for("pilote"))
        self.assertEqual(self.row()["host"], self.CLOUD)

    def test_cloud_attach_is_not_a_workstation_gesture(self):
        self.hold(self.THIRD)
        self.assertIsNone(A.plan_return(self.db, "pilote", self.CLOUD))

    def test_running_turn_defers_switch_until_its_end(self):
        self.db.execute("UPDATE agent_registry SET status = 'running',"
                        " current_prompt = 'cloud turn' WHERE name = 'pilote'")
        move = A.plan_return(self.db, "pilote", self.POSTE)
        result = M.advance(self.db, self.POSTE, probes=ready,
                           cwd_for=lambda m: "/work/local")
        self.assertIn((move["move_id"], "waiting"), result)
        self.assertEqual(self.row()["host"], self.CLOUD)
        self.assertEqual(self.row()["current_prompt"], "cloud turn")
        self.db.execute("UPDATE agent_registry SET status = 'idle', current_prompt = NULL"
                        " WHERE name = 'pilote'")
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: "/work/local")
        self.assertEqual(self.row()["host"], self.POSTE)

    def test_presence_after_failover_without_attach_does_not_return(self):
        self._failover_origin()
        self.assertIsNone(M.plan(self.db, "pilote", self.POSTE,
                                 requested_by="runner", reason="preference"))
        self.assertIsNone(self.moves().open_for("pilote"))
        self.assertEqual(self.row()["host"], self.CLOUD)
        self.assertIsNotNone(A.plan_return(self.db, "pilote", self.POSTE))

    def _failover_origin(self):
        self.hold(self.POSTE)
        move = M.plan(self.db, "pilote", self.CLOUD,
                      requested_by="runner", reason="failover")
        M.advance(self.db, self.CLOUD, probes=ready, cwd_for=lambda m: "/work/cloud")
        # A served cloud turn closed the failover attempt; keep its origin.
        self.db.execute("UPDATE conversation_moves SET state = 'failed',"
                        " failed_at = now(), error = 'test: served cloud turn'"
                        " WHERE move_id = %s::uuid", (move["move_id"],))
        self.hold(self.CLOUD)

    def test_health_rechecked_under_registry_lock(self):
        real_plan = M.plan

        def degrade(*args, **kwargs):
            self.standing("pilote", self.POSTE, 10, ready=False)
            return real_plan(*args, **kwargs)

        with mock.patch.object(M, "plan", side_effect=degrade):
            self.assertIsNone(A.plan_return(self.db, "pilote", self.POSTE))
        self.assertIsNone(self.moves().open_for("pilote"))

    def test_wait_does_not_finish_at_switch_before_target_rehydration(self):
        move = A.plan_return(self.db, "pilote", self.POSTE)
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: "/work/local")
        with mock.patch.object(A.time, "monotonic", side_effect=itertools.chain([0, 0], itertools.repeat(2))):
            with mock.patch.object(A.time, "sleep"):
                self.assertFalse(A.wait_return(self.db, move, max_seconds=1, out=io.StringIO()))

    def test_switch_queues_restoration_only_on_target_without_overwriting_input(self):
        move = A.plan_return(self.db, "pilote", self.POSTE)
        self.assertIsNone(self.row()["pending_prompt"])
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
        prompt = self.row()["pending_prompt"]
        self.assertIn("Restore the conversation", prompt)
        self.assertIsNotNone(self.row()["pending_prompt_id"])
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
        self.assertEqual(self.row()["pending_prompt"], prompt)

    def test_idle_source_with_expired_lease_also_gets_restoration_request(self):
        self.hold(self.CLOUD, expires_in=-1)
        move = A.plan_return(self.db, "pilote", self.POSTE)
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
        self.assertEqual(self.moves().get(move["move_id"])["state"], "recovered")
        self.assertIn("Restore the conversation", self.row()["pending_prompt"])

    def test_switch_preserves_an_existing_request(self):
        registry.set_pending_prompt(self.db, "pilote", "existing human input")
        A.plan_return(self.db, "pilote", self.POSTE)
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
        self.assertEqual(self.row()["pending_prompt"], "existing human input")

    def test_recheck_uses_the_active_transaction_on_both_drivers(self):
        from ameesh import db as db_mod
        reads = []
        original = A._refusal

        def check(conn, row, host, **kwargs):
            reads.append(db_mod.in_transaction(conn))
            return original(conn, row, host, **kwargs)

        with mock.patch.object(A, "_refusal", side_effect=check):
            self.assertIsNotNone(A.plan_return(self.db, "pilote", self.POSTE))
        self.assertEqual(reads, [False, True])

    def test_idle_avatar_is_picked_and_verified_by_target_worker_without_mail(self):
        from ameesh import adapters, harnesses, runner
        from .support import FAKEBIN
        cfg = dataclasses.replace(self.cfg, host=self.POSTE)
        self.db.execute("UPDATE agent_candidates SET cwd = %s WHERE name = 'pilote'"
                        " AND host = %s", (self.tmp, self.POSTE))
        adapter = adapters.HarnessAdapter(harnesses.get("claude"),
                                          os.path.join(FAKEBIN, "claude"))
        preference = M.plan(self.db, "pilote", self.POSTE,
                            requested_by="runner", reason="preference")
        move = A.plan_return(self.db, "pilote", self.POSTE)
        self.assertEqual(move["move_id"], preference["move_id"])
        self.assertIsNone(self.row()["pending_prompt"])
        self.assertIsNone(self.row()["current_prompt"])
        self.assertEqual(storage.of(self.db).mailbox.unread("pilote", limit=10), [])
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: self.tmp)
        target = runner.Runner(cfg, self.db)
        lease = registry.claim(self.db, "pilote", target.runner_id, 120, host=self.POSTE)
        worker = runner.AgentWorker(target, self.row(), lease)
        self.addCleanup(worker.watchdog_stop.set)
        self.addCleanup(worker.release_lease)
        with mock.patch.object(adapters, "adapter_for", return_value=adapter):
            with mock.patch.dict(os.environ, {"AMEESH_TEST_LOG": self.turns_log}):
                spec = worker.pick()
                self.assertIsNotNone(spec)
                self.assertIn("Restore the conversation", spec["prompt"])
                self.assertTrue(worker.run_turn(spec))
                self.assertTrue(A.wait_return(self.db, move, max_seconds=1,
                                              out=io.StringIO()))
                self.assertEqual(runner.run_attach(cfg, self.db, "pilote",
                                                   ttl=30, max_seconds=10), 0)
        self.assertEqual(self.moves().get(move["move_id"])["state"], "verified")
        self.assertNotEqual(self.row()["session_id"], "sess-source")
        self.assertTrue(A.wait_return(self.db, move, max_seconds=1, out=io.StringIO()))

    def test_attach_waits_for_verified_target_session(self):
        move = A.plan_return(self.db, "pilote", self.POSTE)
        M.advance(self.db, self.POSTE, probes=ready, cwd_for=lambda m: "/work/local")
        lease = registry.claim(self.db, "pilote", "runner-local", 120, host=self.POSTE)
        self.assertIsNotNone(lease)
        first = M.open_first_turn(
            self.db, self.cfg, os.path.join(self.tmp, "return-state"),
            agent="pilote", host=self.POSTE, owner="runner-local",
            epoch=lease["lease_epoch"], secrets=[])
        self.assertIsNotNone(first)
        self.assertEqual(first.finish(True, session="local-session", account=None, harness="claude",
                                      cwd="/work/local")["state"], "verified")
        self.assertTrue(A.wait_return(self.db, move, max_seconds=1, out=io.StringIO()))
        self.assertEqual(self.row()["session_id"], "local-session")


class CliReturnTest(Base):
    def test_attach_routes_locally_after_return_even_with_relay_configured(self):
        from ameesh import runner
        self.hold(self.CLOUD)
        cfg = dataclasses.replace(self.cfg, host=self.POSTE)
        # Keep the real CLI routing and deferred plan; emulate the target
        # runner having completed its move before interactive lease access.
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(runner, "load_config", return_value=cfg))
            stack.enter_context(mock.patch.object(runner, "_harden_executor_environment", return_value=True))
            stack.enter_context(mock.patch.object(runner.adapters, "configure"))
            stack.enter_context(mock.patch.object(runner.db_mod, "connect", return_value=self.db))
            stack.enter_context(mock.patch.object(self.db, "close"))
            stack.enter_context(mock.patch.object(runner, "_sortie_sure"))
            waited = stack.enter_context(mock.patch.object(A, "wait_return", return_value=True))
            local = stack.enter_context(mock.patch.object(runner, "run_attach", return_value=0))
            remote = stack.enter_context(mock.patch.object(runner, "_attach_distant_main"))
            self.assertEqual(runner.attach_main(["pilote", "--relay", "wss://relay.example"]), 0)
            waited.assert_called_once()
            local.assert_called_once()
            self.assertTrue(local.call_args.kwargs["wait"])
            remote.assert_not_called()

    def test_unhealthy_poste_keeps_distant_attach(self):
        from ameesh import runner
        self.hold(self.CLOUD)
        self.standing("pilote", self.POSTE, 10, ready=False)
        cfg = dataclasses.replace(self.cfg, host=self.POSTE)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(runner, "load_config", return_value=cfg))
            stack.enter_context(mock.patch.object(runner, "_harden_executor_environment", return_value=True))
            stack.enter_context(mock.patch.object(runner.adapters, "configure"))
            stack.enter_context(mock.patch.object(runner.db_mod, "connect", return_value=self.db))
            stack.enter_context(mock.patch.object(self.db, "close"))
            stack.enter_context(mock.patch.object(runner, "_sortie_sure"))
            local = stack.enter_context(mock.patch.object(runner, "run_attach"))
            remote = stack.enter_context(mock.patch.object(runner, "_attach_distant_main", return_value=0))
            self.assertEqual(runner.attach_main(["pilote", "--relay", "wss://relay.example"]), 0)
            remote.assert_called_once()
            local.assert_not_called()
            self.assertIsNone(storage.of(self.db).conversation_moves.open_for("pilote"))
