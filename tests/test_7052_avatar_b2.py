# SPDX-License-Identifier: AGPL-3.0-only
"""#7052 (avatar B2, design 5322 §3.2): return to the preferred host.

Dwell before the return, holdoff after it (the old holder never takes the
conversation back at once), short avatar lease so an absent workstation
frees the conversation in two minutes, replay of the interrupted turn once,
and never two holders.
"""
from __future__ import annotations

import dataclasses
import os
import time
import unittest
import uuid
from types import SimpleNamespace

from ameesh import (conversation as C, conversation_move as M, election, registry,
                    storage)

from .test_l165_ep2_election_score import _RoleCase

READY = {c: True for c in M.CHECKS}


def ready(_move):
    return READY


class Base(_RoleCase):
    POSTE, CLOUD, THIRD = "atelier", "banc", "troisieme"

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM conversation_moves")
        self.role_row = self.role(hosts=(self.POSTE, self.CLOUD, self.THIRD))
        self.db.execute("UPDATE agent_registry SET releve = 'preference'"
                        " WHERE name = 'pilote'")
        for rank, host in enumerate((self.POSTE, self.CLOUD, self.THIRD)):
            self.ready_host("pilote", host)
            self.standing("pilote", host, 10.0)
            self.db.execute("UPDATE agent_candidates SET preference_rank = %s"
                            " WHERE name = 'pilote' AND host = %s", (rank, host))

    # -- fixtures ----------------------------------------------------------
    def hold(self, host: str, *, expires_in: float = 300.0) -> dict:
        self.db.execute(
            "UPDATE agent_registry SET host = %s, lease_owner = 'runner-src',"
            " lease_epoch = 3, lease_expires_at = clock_timestamp()"
            " + make_interval(secs => %s), session_id = 'sess-source',"
            " status = 'idle', current_prompt = NULL, current_prompt_id = NULL"
            " WHERE name = 'pilote'", (host, expires_in))
        return registry.get(self.db, "pilote")

    def row(self) -> dict:
        return registry.get(self.db, "pilote")

    def runner(self, host: str | None = None) -> SimpleNamespace:
        from ameesh.runner import Runner
        host = host or self.POSTE
        runner = SimpleNamespace(
            db=self.db, host=host, runner_id="runner-%s" % host,
            dry_run=False, mediated=False, agents_filter=None,
            clock=SimpleNamespace(monotonic=time.monotonic),
            _preference_next=0.0, _preference_seen_since={},
            _preference_plan_next={}, _preference_recovery_next={},
            _preference_error=None)
        for name in ("preference_return_pass", "advance_conversation_moves",
                     "election_brief_for", "_preference_claim_or_recovery",
                     "_load_handover_state", "_note_unleased"):
            setattr(runner, name, getattr(Runner, name).__get__(runner))
        return runner

    def claimable(self, host: str) -> dict:
        rows = [r for r in registry.claimable(self.db, host, ["pilote"])
                if r["name"] == "pilote"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def move(self) -> dict:
        return M.handover(self.db, "pilote") or {}

    def attempt(self, move_id: str) -> dict:
        return storage.of(self.db).conversation_moves.get(move_id)

    def open_move(self) -> dict:
        return storage.of(self.db).conversation_moves.open_for("pilote") or {}

    def backdate(self, seconds: float) -> None:
        self.db.execute(
            "UPDATE conversation_moves SET switched_at ="
            " clock_timestamp() - make_interval(secs => %s) WHERE agent = %s",
            (seconds, "pilote"))


class ReturnTest(Base):
    def test_dwell_before_the_return(self):
        self.hold(self.CLOUD)
        verdict = election.preference_return(self.db, self.row(), self.POSTE)
        self.assertEqual(verdict["state"], "dwell")
        self.assertAlmostEqual(verdict["remaining_s"], election.PREFERENCE_DWELL_S,
                               delta=1.0)
        # present but not long enough yet: still no move
        verdict = election.preference_return(
            self.db, self.row(), self.POSTE,
            seen_since=time.monotonic() - election.PREFERENCE_DWELL_S / 2)
        self.assertEqual(verdict["state"], "dwell")
        self.assertFalse(M.holds(self.db, "pilote"))

    def test_dwell_done_the_preferred_host_plans_and_takes_the_lease(self):
        self.hold(self.CLOUD)
        verdict = election.preference_return(
            self.db, self.row(), self.POSTE,
            seen_since=time.monotonic() - election.PREFERENCE_DWELL_S - 1.0)
        self.assertEqual((verdict["state"], verdict["recovery"]), ("plan", False))
        runner = self.runner()
        runner._preference_seen_since["pilote"] = (
            time.monotonic() - election.PREFERENCE_DWELL_S - 1.0)
        self.assertEqual(runner.preference_return_pass(), ["pilote"])
        move = self.open_move()
        self.assertEqual((move["state"], move["target_host"]),
                         ("planned", self.POSTE))
        # prepared and switched on the target's pass, claimed in the same sweep
        self.assertEqual(
            [step for _ident, step in M.advance(
                self.db, self.POSTE, probes=ready, cwd_for=lambda m: "/tgt/pilote")],
            ["prepared", "switched"])
        lease = registry.claim(self.db, "pilote", "runner-poste", 120,
                               host=self.POSTE)
        self.assertIsNotNone(lease)
        row = self.row()
        self.assertEqual((row["host"], row["session_id"]), (self.POSTE, "sess-source"))
        # the old holder never claims while the target holds a live lease
        self.assertIsNone(registry.claim(self.db, "pilote", "runner-src", 120,
                                         host=self.CLOUD))

    def test_absent_holder_is_recovered_at_once(self):
        self.hold(self.CLOUD)
        # the workstation's last standing is an hour old: it is not present
        self.db.execute(
            "UPDATE role_standings SET evaluated_at = clock_timestamp()"
            " - interval '1 hour' WHERE name = 'pilote' AND host = %s",
            (self.CLOUD,))
        verdict = election.preference_return(self.db, self.row(), self.POSTE)
        self.assertEqual((verdict["state"], verdict["recovery"]), ("plan", True))

    def test_expired_lease_is_recovered_without_dwell(self):
        self.hold(self.CLOUD, expires_in=-5)
        verdict = election.preference_return(self.db, self.row(), self.POSTE)
        self.assertEqual((verdict["state"], verdict["recovery"]), ("plan", True))

    def test_the_old_holder_waits_the_holdoff_then_reclaims(self):
        self.hold(self.CLOUD)
        move = M.plan(self.db, "pilote", self.POSTE, requested_by="runner-poste",
                      reason="preference")
        M.advance(self.db, self.POSTE, probes=ready,
                  cwd_for=lambda m: "/tgt/pilote")
        now = time.monotonic()
        allowed, reason, _ = election.claim_wait(
            self.db, self.row(), self.CLOUD, now - 1000.0, now=now)
        self.assertFalse(allowed)
        self.assertIn("preference holdoff", reason)
        self.assertIn(self.POSTE, reason)
        # still not claimable: no lease moved, the target may still take it
        self.assertIsNone(registry.claim(self.db, "pilote", "runner-src", 120,
                                         host=self.CLOUD))
        # the preferred host never came: after the margin, the source closes
        # the attempt and resumes its own session, locally
        self.backdate(election.PREFERENCE_HOLDOFF_S + 60.0)
        allowed, reason, _ = election.claim_wait(
            self.db, self.row(), self.CLOUD, now - 1000.0, now=now)
        self.assertFalse(allowed)
        self.assertIn("preference reclaim", reason)
        closed = M.reclaim(self.db, "pilote", self.CLOUD,
                           holdoff_s=election.PREFERENCE_HOLDOFF_S + 60.0)
        self.assertIsNotNone(closed)
        # #7680 : le libellé nomme la cible réelle, pas un « hôte préféré ».
        self.assertIn("preference handover", closed["error"])
        self.assertIn(self.POSTE, closed["error"])
        row = self.row()
        self.assertEqual((row["host"], row["session_id"]),
                         (self.CLOUD, "sess-source"))
        self.assertEqual(self.attempt(move["move_id"])["state"], "failed")
        lease = registry.claim(self.db, "pilote", "runner-src", 300,
                               host=self.CLOUD)
        self.assertIsNotNone(lease)

    def test_a_host_under_pressure_does_not_close_the_handover(self):
        # #7680 : une source sous pression ne peut pas servir le retour
        # qu'elle annoncerait ; la tentative reste ouverte à sa cible.
        self.hold(self.CLOUD)
        M.plan(self.db, "pilote", self.POSTE, requested_by="runner-poste",
               reason="preference")
        M.advance(self.db, self.POSTE, probes=ready,
                  cwd_for=lambda m: "/tgt/pilote")
        self.backdate(election.PREFERENCE_HOLDOFF_S + 60.0)
        now = time.monotonic()
        allowed, reason, _ = election.claim_wait(
            self.db, self.row(), self.CLOUD, now - 1000.0, now=now,
            hold_turns=True)
        self.assertFalse(allowed)
        self.assertIn("deferred", reason)
        self.assertIn("pressure", reason)
        self.assertEqual(self.open_move()["state"], "switched")
        # sans pression, le même instant est bien un reclaim
        allowed, reason, _ = election.claim_wait(
            self.db, self.row(), self.CLOUD, now - 1000.0, now=now)
        self.assertFalse(allowed)
        self.assertIn("preference reclaim", reason)
        self.assertEqual(self.open_move()["state"], "switched")

    def test_the_runner_defers_the_reclaim_under_pressure(self):
        from ameesh.runner import Runner
        self.hold(self.CLOUD)
        move = M.plan(self.db, "pilote", self.POSTE, requested_by="runner-poste",
                      reason="preference")
        M.advance(self.db, self.POSTE, probes=ready,
                  cwd_for=lambda m: "/tgt/pilote")
        self.backdate(election.PREFERENCE_HOLDOFF_S + 60.0)
        runner = self.runner(self.CLOUD)
        runner._handover_loaded = True
        runner._handover_released = {}
        runner._handover_failed = {}
        runner._handover_retry_at = 0.0
        runner._handover_error = None
        runner._election_free_since = {"pilote": time.monotonic() - 1000.0}
        runner._preference_plan_next = {}
        runner._role_error = None
        runner.pressure_holds_turn = lambda agent, pressure=None: True
        runner.election_claim_allowed = getattr(
            Runner, "election_claim_allowed").__get__(runner)
        allowed, reason, _ = runner.election_claim_allowed(self.row())
        self.assertFalse(allowed)
        self.assertIn("deferred", reason)
        self.assertEqual(self.open_move()["state"], "switched")
        self.assertEqual(self.row()["host"], self.POSTE)
        # la pression passée, le même appel referme la tentative et réclame
        runner.pressure_holds_turn = lambda agent, pressure=None: False
        runner._preference_plan_next = {}
        allowed, reason, _ = runner.election_claim_allowed(self.row())
        self.assertTrue(allowed, reason)
        self.assertEqual(self.attempt(move["move_id"])["state"], "failed")
        self.assertEqual(self.row()["host"], self.CLOUD)

    def test_another_candidate_waits_for_the_designated_target(self):
        self.hold(self.CLOUD)
        M.plan(self.db, "pilote", self.POSTE, requested_by="runner-poste",
               reason="preference")
        M.advance(self.db, self.POSTE, probes=ready,
                  cwd_for=lambda m: "/tgt/pilote")
        now = time.monotonic()
        allowed, reason, _ = election.claim_wait(
            self.db, self.row(), self.THIRD, now - 1000.0, now=now)
        self.assertFalse(allowed)
        self.assertIn("handed to %s" % self.POSTE, reason)
        # the designated target itself is allowed (no brief: its own session)
        allowed, _, _ = election.claim_wait(
            self.db, self.row(), self.POSTE, now - 1000.0, now=now)
        self.assertTrue(allowed)

    def test_a_live_target_first_turn_is_never_superseded(self):
        from ameesh.storage.conversation_moves import HANDOVER_TTL_S
        self.hold(self.CLOUD)
        first = M.plan(self.db, "pilote", self.POSTE,
                       requested_by="runner-poste", reason="preference")
        M.advance(self.db, self.POSTE, probes=ready,
                  cwd_for=lambda m: "/tgt/pilote")
        lease = registry.claim(self.db, "pilote", "runner-poste", 120,
                               host=self.POSTE)
        self.db.execute(
            "UPDATE conversation_moves SET state = 'rehydrating',"
            " target_owner = 'runner-poste', target_epoch = %s, digest = %s"
            " WHERE move_id = %s::uuid",
            (lease["lease_epoch"], "0" * 64, first["move_id"]))
        self.backdate(HANDOVER_TTL_S + 60.0)
        # the target's first turn may run long: its live lease protects it
        kept = M.plan(self.db, "pilote", self.THIRD,
                      requested_by="runner-troisieme", reason="preference")
        self.assertFalse(kept["created"])
        self.assertEqual((kept["state"], kept["target_host"]),
                         ("rehydrating", self.POSTE))
        # the target is gone: the attempt is now stale and can be superseded
        self.assertTrue(registry.release(self.db, "pilote", "runner-poste",
                                         lease["lease_epoch"]))
        moved = M.plan(self.db, "pilote", self.THIRD,
                       requested_by="runner-troisieme", reason="preference")
        self.assertTrue(moved["created"])
        self.assertEqual(moved["target_host"], self.THIRD)

    def test_a_preference_claim_never_builds_an_election_brief(self):
        self.hold(self.CLOUD)
        M.plan(self.db, "pilote", self.POSTE, requested_by="runner-poste",
               reason="preference")
        agent = dict(self.row(), previous_host=self.CLOUD)
        runner = self.runner()
        runner.cfg = self.cfg
        from ameesh.runner import Runner
        # #7680 : la ligne est encore sur l'autre hôte ; aucun brief n'est
        # construit, et la passe ne réclame pas (""), le déplacement la
        # basculera ici.
        self.assertEqual(Runner.election_brief_for(runner, agent), "")

    def test_a_handover_nobody_claimed_is_superseded(self):
        # the reviewer's case (B/P2 #40199): after A→B, both A and B are gone
        # and C alone is ready. The runner's real path must stale the handover
        # (not skip `plan` on `holds`) and C must take the conversation.
        from ameesh.storage.conversation_moves import HANDOVER_TTL_S
        self.hold(self.CLOUD)
        first = M.plan(self.db, "pilote", self.POSTE,
                       requested_by="runner-poste", reason="preference")
        M.advance(self.db, self.POSTE, probes=ready,
                  cwd_for=lambda m: "/tgt/pilote")
        self.db.execute(
            "UPDATE role_standings SET evaluated_at = clock_timestamp()"
            " - interval '1 hour' WHERE name = 'pilote' AND host = ANY(%s)",
            ([self.CLOUD, self.POSTE],))
        self.backdate(HANDOVER_TTL_S + 60.0)
        runner = self.runner(self.THIRD)
        self.assertEqual(runner.preference_return_pass(), ["pilote"])
        second = self.open_move()
        self.assertEqual((second["target_host"], second["source_host"]),
                         (self.THIRD, self.POSTE))
        self.assertEqual(self.attempt(first["move_id"])["state"], "failed")
        self.assertEqual(
            [step for _i, step in M.advance(
                self.db, self.THIRD, probes=ready,
                cwd_for=lambda m: "/tgt/pilote")],
            ["prepared", "switched"])
        self.assertIsNotNone(registry.claim(self.db, "pilote", "runner-troisieme",
                                            120, host=self.THIRD))


class RecoveryTest(Base):
    """#7680: no election brief for `preference`, even without an open move.

    The host that would take the agent plans the recovery move instead; the
    target's first turn receives the journal segment, never a resume brief.
    """

    def test_without_a_move_the_host_plans_a_recovery_with_a_segment(self):
        from ameesh.runner import Runner
        self.db.execute("DELETE FROM conversation_log;"
                        " DELETE FROM conversation_heads")
        self.cfg = dataclasses.replace(
            self.cfg, threads_dir=os.path.join(self.tmp, self._testMethodName))
        self.db.execute(
            "UPDATE agent_registry SET host = %s, lease_owner = NULL,"
            " lease_expires_at = NULL, session_id = 'sess-source',"
            " status = 'idle', status_text = '', cwd = '/src/pilote'"
            " WHERE name = 'pilote'", (self.CLOUD,))
        log = C.Log(self.db, self.cfg, agent="pilote", project="acme", lot="7680")
        log.append("human_message", "garde bien la conversation",
                   author="human:alice", turn="t1")
        log.append("turn_end", "je la garde, avec son extrait",
                   author="agent:pilote", turn="t1",
                   meta={"status": "completed"})
        runner = self.runner(self.POSTE)
        runner.cfg = self.cfg
        # la ligne est libre sur l'autre hôte : la passe ne réclame pas, elle
        # planifie la reprise (aucun brief d'élection n'est construit).
        self.assertEqual(Runner.election_brief_for(runner, self.row()), "")
        move = self.open_move()
        self.assertEqual(
            (move["state"], move["source_host"], move["target_host"],
             move["source_session"]),
            ("planned", self.CLOUD, self.POSTE, "sess-source"))
        # la cible prépare et bascule, puis la réclamation est locale
        self.assertEqual(
            [step for _ident, step in M.advance(
                self.db, self.POSTE, probes=ready,
                cwd_for=lambda _move: "/tgt/pilote")],
            ["prepared", "switched"])
        lease = registry.claim(self.db, "pilote", "runner-poste", 120,
                               host=self.POSTE)
        self.assertIsNotNone(lease)
        row = self.row()
        self.assertEqual((row["host"], row["session_id"]),
                         (self.POSTE, "sess-source"))
        state = os.path.join(self.tmp, self._testMethodName, "state")
        first = M.open_first_turn(
            self.db, self.cfg, state, agent="pilote", host=self.POSTE,
            owner="runner-poste", epoch=lease["lease_epoch"], secrets=[])
        self.assertIsNotNone(first)
        self.assertEqual(first.move["move_id"], move["move_id"])
        with open(first.path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("je la garde, avec son extrait", text)

    def test_a_local_preference_claim_keeps_its_session(self):
        # la ligne est déjà sur cet hôte : pas de brief, la session locale
        # reste (une reprise de session locale est normale).
        from ameesh.runner import Runner
        runner = self.runner(self.POSTE)
        runner.cfg = self.cfg
        agent = dict(self.row(), previous_host=self.CLOUD)
        self.assertIsNone(Runner.election_brief_for(runner, agent))


class InterruptedTurnTest(Base):
    def test_the_interrupted_turn_is_replayed_once_with_its_identity(self):
        prompt_id = str(uuid.uuid4())
        self.hold(self.CLOUD, expires_in=-5)
        self.db.execute(
            "UPDATE agent_registry SET current_prompt = 'traite le lot 42',"
            " current_prompt_id = %s WHERE name = 'pilote'", (prompt_id,))
        move = M.plan(self.db, "pilote", self.POSTE, requested_by="runner-poste",
                      reason="preference")
        steps = M.advance(self.db, self.POSTE, probes=ready,
                          cwd_for=lambda m: "/tgt/pilote")
        self.assertEqual([step for _i, step in steps], ["prepared", "recovered"])
        row = self.row()
        self.assertIn("interrupted", row["current_prompt"])
        self.assertIn("traite le lot 42", row["current_prompt"])
        self.assertIn("host %s was interrupted" % self.CLOUD, row["current_prompt"])
        lease = registry.claim(self.db, "pilote", "runner-poste", 120,
                               host=self.POSTE)
        self.assertIsNotNone(lease)
        row = self.row()
        self.assertIsNone(row["current_prompt"])
        self.assertIn("traite le lot 42", row["pending_prompt"])
        self.assertIn("interrupted", row["pending_prompt"])
        self.assertEqual(row["pending_prompt_id"], prompt_id)
        self.assertEqual(self.attempt(move["move_id"])["state"], "recovered")


class LeaseTtlTest(unittest.TestCase):
    def test_a_preference_avatar_gets_the_short_ttl(self):
        from ameesh.runner import AgentWorker, preference_lease_ttl
        self.assertEqual(preference_lease_ttl({"releve": "preference"}, 300), 120.0)
        self.assertEqual(preference_lease_ttl({"releve": "preference"}, 60), 60.0)
        self.assertEqual(preference_lease_ttl({"releve": "preference"}, 0), 120.0)
        self.assertEqual(preference_lease_ttl({"releve": "election"}, 300), 300.0)
        self.assertEqual(preference_lease_ttl({"releve": "hote"}, 300), 300.0)
        worker = SimpleNamespace(agent={"releve": "preference"},
                                 runner=SimpleNamespace(lease_ttl=300.0))
        self.assertEqual(AgentWorker.lease_ttl.__get__(worker), 120.0)
        worker.agent = {"releve": "hote"}
        self.assertEqual(AgentWorker.lease_ttl.__get__(worker), 300.0)


if __name__ == "__main__":
    unittest.main()
