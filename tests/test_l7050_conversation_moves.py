# SPDX-License-Identifier: AGPL-3.0-only
"""#7050 (avatar A2, design 5322 §2.3-§2.4): conversation_moves — state
machine, claim of the move, provisional session, segment on a private
channel, failure back to the source, idempotence, two holders never."""
from __future__ import annotations

import dataclasses
import os
import stat
import threading
import uuid

from ameesh import (conversation as C, conversation_move as M,
                    conversation_segment as S, registry, storage, turn_context)
from tests.support import PgTestCase

SECRET = "sk-test-0123456789abcdef-SECRET"
READY = {c: True for c in M.CHECKS}


def ready(_move):
    return READY


class Base(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM conversation_moves; DELETE FROM conversation_log;"
                        " DELETE FROM conversation_heads")
        self.cfg = dataclasses.replace(
            self.cfg, threads_dir=os.path.join(self.tmp, self._testMethodName))
        registry.upsert(self.db, "avatar", harness="claude", host="source",
                        cwd="/src/avatar", session_id="sess-source")
        self.lease = registry.claim(self.db, "avatar", "runner-src", ttl_seconds=300,
                                    host="source")
        self.epoch = self.lease["lease_epoch"]
        self.moves = storage.of(self.db).conversation_moves
        self.state = os.path.join(self.tmp, self._testMethodName, "state")
        log = C.Log(self.db, self.cfg, agent="avatar", project="acme", lot="7050")
        log.append("human_message", "quelle est la couleur du vélo ?",
                   author="human:alice", turn="t1")
        log.append("turn_end", "le vélo est vert", turn="t1",
                   meta={"status": "completed"})

    # -- helpers -----------------------------------------------------------
    def plan(self, **kw):
        return M.plan(self.db, "avatar", "target", requested_by="human:alice", **kw)

    def advance(self):
        return M.advance(self.db, "target", probes=ready, cwd_for=lambda m: "/tgt/avatar")

    def target_claim(self, owner="runner-tgt"):
        return registry.claim(self.db, "avatar", owner, ttl_seconds=300, host="target")

    def first_turn(self, lease, owner="runner-tgt", **kw):
        return M.open_first_turn(self.db, self.cfg, self.state, agent="avatar",
                                 host="target", owner=owner, epoch=lease["lease_epoch"],
                                 secrets=kw.pop("secrets", [SECRET]), **kw)

    def begin(self, owner="runner-src", epoch=None):
        return registry.begin_turn_diagnosed(
            self.db, "avatar", owner, self.epoch if epoch is None else epoch, "turn")

    def row(self):
        return registry.get(self.db, "avatar")



class MoveTest(Base):
    def test_full_move_session_provisional_until_verified(self):
        move = self.plan()
        self.assertTrue(move["created"])
        self.assertEqual((move["state"], move["source_host"], move["source_session"]),
                         ("planned", "source", "sess-source"))
        # the source opens no new turn once the move is planned
        self.assertEqual(self.begin(), M.BEGIN_REFUSAL)
        self.assertEqual(self.advance(), [(move["move_id"], "prepared"),
                                          (move["move_id"], "switched")])
        row = self.row()
        self.assertEqual((row["host"], row["cwd"]), ("target", "/tgt/avatar"))
        self.assertIsNone(row["lease_owner"])
        self.assertGreater(row["lease_epoch"], self.epoch)
        # provisional: the registry still names the source session
        self.assertEqual(row["session_id"], "sess-source")
        # the old source lease is fenced out
        self.assertNotEqual(self.begin(), "started")
        lease = self.target_claim()
        self.assertIsNotNone(lease)
        first = self.first_turn(lease)
        self.assertEqual(first.move["state"], "rehydrating")
        self.assertEqual(self.row()["session_id"], "sess-source")
        first.launched("sess-target")
        self.assertEqual(self.row()["session_id"], "sess-source")
        done = first.finish(True, session="sess-target", account="acct", harness="claude",
                            cwd="/tgt/avatar")
        self.assertEqual((done["state"], done["source_cache"]), ("verified", "stale-cache"))
        row = self.row()
        self.assertEqual((row["session_id"], row["session_account"]), ("sess-target", "acct"))
        self.assertFalse(os.path.exists(first.path))
        self.assertIsNone(self.moves.open_for("avatar"))

    def test_segment_file_private_and_never_in_prompt_or_row(self):
        C.Log(self.db, self.cfg, agent="avatar", project="acme").append(
            "human_message", "mon jeton est " + SECRET, author="human:alice", turn="t2")
        C.Log(self.db, self.cfg, agent="avatar", project="acme").append(
            "turn_end", "noté", turn="t2", meta={"status": "completed"})
        self.plan()
        self.advance()
        first = self.first_turn(self.target_claim())
        mode = stat.S_IMODE(os.stat(first.path).st_mode)
        self.assertEqual(mode, 0o600)
        with open(first.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("le vélo est vert", text)
        self.assertNotIn(SECRET, text)
        self.assertIn(S.REDACTED, text)
        # the command-line note names the file indirectly, never the conversation
        note = first.prompt_note()
        self.assertNotIn("vélo", note)
        self.assertNotIn(first.path, note)
        # the attempt row and the turn context carry bounds and digest only
        row = self.moves.get(first.move_id)
        flat = repr(row)
        self.assertNotIn("vélo", flat)
        self.assertNotIn(SECRET, flat)
        self.assertEqual(row["digest"], first.context["digest"])
        document = turn_context.build(
            turn_id="t", agent="avatar", host="target", kind="prompt", started_ts=1.0,
            session=None, work_item_id=None, lease_owner="runner-tgt", lease_epoch=1,
            lease_expires_ts=None, requests=(), harness="claude", model=None,
            budget_usd=None, spent_usd=0.0, spend_key=None, marker=None,
            reservation=None, conversation=first.context)
        self.assertEqual(set(document["conversation"]), set(turn_context.CONVERSATION_KEYS))
        self.assertNotIn("vélo", repr(document))
        # the segment is never written back into the journal
        entries = storage.of(self.db).conversations.read("avatar")["entries"]
        self.assertEqual(sum("vélo est vert" in e["text"] for e in entries), 1)

    def test_idempotent_plan_bind_and_verify(self):
        move = self.plan(move_id=str(uuid.uuid4()))
        again = self.plan(move_id=move["move_id"])
        other = self.plan()
        self.assertFalse(again["created"])
        self.assertEqual(again["move_id"], move["move_id"])
        self.assertEqual(other["move_id"], move["move_id"])
        self.advance()
        self.assertEqual(self.advance(), [])  # nothing left for the target to do
        lease = self.target_claim()
        first = self.first_turn(lease)
        replay = self.first_turn(lease)
        self.assertEqual(replay.context["digest"], first.context["digest"])
        ok = dict(session="sess-target", account=None, harness="claude", cwd="/tgt")
        self.assertEqual(first.finish(True, **ok)["state"], "verified")
        self.assertEqual(self.moves.verify(first.move_id, owner="runner-tgt",
                                           epoch=lease["lease_epoch"], **ok)["state"],
                         "verified")

    def test_plan_through_an_alias_rereads_the_open_attempt(self):
        # #7093 (relecture dev3) : un plan nommé par un alias résout le nom
        # primaire comme le déclencheur d'écriture ; sinon il ne relit ni la
        # ligne de registre ni la tentative ouverte.
        self.db.execute("UPDATE agent_registry SET aliases = ARRAY['avatar-old']"
                        " WHERE name = 'avatar'")
        first = M.plan(self.db, "avatar-old", "target", requested_by="human:alice")
        self.assertIsNotNone(first)
        self.assertEqual(first["agent"], "avatar")
        again = M.plan(self.db, "avatar-old", "target", requested_by="human:alice")
        self.assertEqual(again["move_id"], first["move_id"])
        self.assertFalse(again["created"])

    def test_prepare_failure_source_keeps_serving(self):
        move = self.plan()
        steps = M.advance(self.db, "target", cwd_for=lambda m: "/tgt",
                          probes=lambda m: dict(READY, binary=False))
        self.assertEqual(steps, [(move["move_id"], "failed")])
        self.assertIn("binary", self.moves.get(move["move_id"])["error"])
        row = self.row()
        self.assertEqual((row["host"], row["lease_owner"], row["lease_epoch"],
                          row["session_id"]),
                         ("source", "runner-src", self.epoch, "sess-source"))
        self.assertEqual(self.begin(), "started")

    def test_first_turn_failure_returns_to_source_with_notice(self):
        self.plan()
        self.advance()
        lease = self.target_claim()
        first = self.first_turn(lease)
        first.launched("sess-target")
        done = first.finish(False, session=None, account=None, harness="claude",
                            cwd="/tgt", error="harness exit 1")
        self.assertEqual(done["state"], "failed")
        row = self.row()
        self.assertEqual((row["host"], row["cwd"], row["session_id"]),
                         ("source", "/src/avatar", "sess-source"))
        self.assertIsNone(row["lease_owner"])
        self.assertGreater(row["lease_epoch"], lease["lease_epoch"])
        self.assertIn("part of that turn may have run", row["pending_prompt"])
        # the source reclaims and serves its own native session
        back = registry.claim(self.db, "avatar", "runner-src", ttl_seconds=300,
                                     host="source")
        self.assertIsNotNone(back)
        self.assertEqual(registry.get(self.db, "avatar")["session_id"], "sess-source")

    def test_no_switch_during_a_source_turn_and_never_two_holders(self):
        move = self.plan()
        # the source was already in its turn when the move was planned
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'avatar'")
        self.assertEqual(self.advance(), [(move["move_id"], "prepared"),
                                          (move["move_id"], "waiting")])
        self.assertEqual(self.row()["lease_owner"], "runner-src")
        self.assertIsNone(self.target_claim())  # not on the target host yet
        self.db.execute("UPDATE agent_registry SET status = 'idle' WHERE name = 'avatar'")
        results = []

        def go():
            results.append(self.moves.switch(move["move_id"], target_host="target"))

        threads = [threading.Thread(target=go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(r and r["state"] == "switched" for r in results))
        self.assertEqual(self.row()["lease_epoch"], self.epoch + 1)  # one release only
        self.assertIsNone(registry.claim(self.db, "avatar", "runner-src", ttl_seconds=300,
                                     host="source"))
        self.assertIsNotNone(self.target_claim())
        self.assertIsNone(self.target_claim("runner-other"))

    def test_interactive_session_holds_the_source(self):
        move = self.plan()
        self.db.execute("UPDATE agent_registry SET lease_owner = 'attach:alice@source'"
                        " WHERE name = 'avatar'")
        self.assertEqual(self.advance()[-1], (move["move_id"], "waiting"))
        self.assertEqual(self.row()["host"], "source")

    def test_recovered_when_the_source_is_gone(self):
        move = self.plan()
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 s'"
                        " WHERE name = 'avatar'")
        self.assertEqual(self.advance()[-1], (move["move_id"], "recovered"))
        first = self.first_turn(self.target_claim())
        self.assertEqual(first.move["state"], "rehydrating")

    def test_restarted_target_fails_back_to_source(self):
        self.plan()
        self.advance()
        lease = self.target_claim()
        self.first_turn(lease).launched("sess-target")
        # the target runner dies; another target runner claims after expiry
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 s'"
                        " WHERE name = 'avatar'")
        relaunch = self.target_claim("runner-tgt-2")
        with self.assertRaises(M.MoveError):
            self.first_turn(relaunch, owner="runner-tgt-2")
        row = self.row()
        self.assertEqual((row["host"], row["session_id"]), ("source", "sess-source"))
        self.assertIn("part of that turn may have run", row["pending_prompt"])

    def test_stale_plan_releases_the_source(self):
        move = self.plan()
        self.db.execute("UPDATE conversation_moves SET updated_at = now() - interval '1 hour'")
        self.assertEqual(self.begin(), "started")
        self.assertEqual(self.advance(), [(move["move_id"], "failed")])

    def test_long_source_turn_delays_the_switch_without_failing_it(self):
        # #7093 (relecture dev3) : au-delà de PLAN_TTL_S une tentative déjà
        # préparée attend la fin du tour source ; elle n'est pas abandonnée
        # (design 5322 §2.6 : un tour long retarde, il ne tue pas).
        move = self.plan()
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'avatar'")
        self.assertEqual(self.advance(), [(move["move_id"], "prepared"),
                                          (move["move_id"], "waiting")])
        self.db.execute("UPDATE conversation_moves SET updated_at = now() - interval '1 hour'")
        self.db.execute("UPDATE agent_registry SET status = 'idle' WHERE name = 'avatar'")
        self.assertEqual(self.advance(), [(move["move_id"], "switched")])
        self.assertEqual(self.row()["host"], "target")

    def test_late_failure_guard_does_not_undo_a_switch(self):
        # #7093 (relecture dev3) : la passe cible ne défait pas une tentative
        # qu'une autre passe vient de préparer ou de basculer — `fail` limité
        # à `planned` n'agit pas.
        move = self.plan()
        self.advance()  # prepared then switched in the same pass
        self.assertEqual(self.moves.get(move["move_id"])["state"], "switched")
        self.assertIsNone(self.moves.fail(move["move_id"], error="prepare: expired",
                                          only=("planned",)))
        self.assertEqual(self.moves.get(move["move_id"])["state"], "switched")
        self.assertEqual(self.row()["host"], "target")

    def test_stale_plan_holds_nothing_and_can_be_planned_again(self):
        # B (relecture 3) : au-delà de PLAN_TTL_S, la tentative ne retient
        # plus rien (même règle que begin_turn) ; un nouveau plan, même sur
        # le même epoch, ouvre une tentative neuve
        old = self.plan()
        self.assertTrue(self.moves.holds("avatar"))
        self.db.execute("UPDATE conversation_moves SET updated_at = now() - interval '1 hour'")
        self.assertFalse(self.moves.holds("avatar"))
        again = self.plan()
        self.assertTrue(again["created"])
        self.assertNotEqual(again["move_id"], old["move_id"])
        self.assertEqual(again["epoch_source"], old["epoch_source"])
        self.assertEqual(self.moves.get(old["move_id"])["state"], "failed")
        self.assertTrue(self.moves.holds("avatar"))
        # the replay of the live attempt still rereads it
        self.assertFalse(self.plan(move_id=again["move_id"])["created"])

    def test_plan_refused_on_same_host_or_external(self):
        self.assertIsNone(M.plan(self.db, "avatar", "source", requested_by="human:alice"))
        with self.assertRaises(ValueError):
            self.plan(reason="whim")

    def test_checks_are_booleans_only(self):
        move = self.plan()
        with self.assertRaises(ValueError):
            self.moves.prepare(move["move_id"], target_host="target", target_cwd="/tgt",
                               checks={"account": "/home/x/.claude/credentials.json"})


class WorkerTest(Base):
    """The runner's hooks: claim of the move, provisional session, verify/fail."""

    def worker(self, lease):
        from types import SimpleNamespace
        from ameesh.runner import AgentWorker
        os.makedirs(self.state, exist_ok=True)
        w = SimpleNamespace(
            name="avatar", db=self.db, cfg=self.cfg, state_dir=self.state,
            epoch=lease["lease_epoch"], lease_lost=threading.Event(),
            runner=SimpleNamespace(host="target", runner_id="runner-tgt", dry_run=False),
            agent=dict(registry.get(self.db, "avatar")), _move_turn=None,
            _turn_launched=False, _path=lambda leaf: os.path.join(self.state, leaf),
            read_session_file=lambda: None, _state_note=lambda *a: True,
            _resume_drop=lambda: None, _installe_hook_preuve=lambda cwd: None,
            write_session_file=lambda sid: None)
        for name in ("_open_move_turn", "_finish_move_turn", "current_session",
                     "_note_session", "_note_move_launched", "_conversation_move_open"):
            setattr(w, name, getattr(AgentWorker, name).__get__(w))
        return w

    def switched(self):
        self.plan()
        self.advance()
        return self.target_claim()

    def test_turn_verifies_and_registry_takes_target_session(self):
        w = self.worker(self.switched())
        self.assertTrue(w._open_move_turn())
        self.assertIsNone(w.current_session())  # a new native session
        w._turn_launched = True
        w._note_session("sess-target", None, "claude", None, "/tgt", "", "claude")
        self.assertEqual(self.row()["session_id"], "sess-source")  # provisional
        self.assertEqual(w.current_session(), "sess-target")
        w._finish_move_turn(True)
        self.assertEqual(self.row()["session_id"], "sess-target")
        self.assertEqual(w.agent["session_id"], "sess-target")
        self.assertFalse(w.lease_lost.is_set())
        self.assertIsNone(w._move_turn)

    def test_turn_not_launched_keeps_the_attempt(self):
        w = self.worker(self.switched())
        self.assertTrue(w._open_move_turn())
        w._finish_move_turn(False)
        self.assertIsNotNone(w._move_turn)
        self.assertEqual(self.moves.open_for("avatar")["state"], "rehydrating")

    def test_failed_turn_returns_to_source(self):
        w = self.worker(self.switched())
        w._open_move_turn()
        w._turn_launched = True
        w._finish_move_turn(False)
        self.assertTrue(w.lease_lost.is_set())
        row = self.row()
        self.assertEqual((row["host"], row["session_id"]), ("source", "sess-source"))

    def test_launched_without_session_warns_the_source(self):
        # B1 (relecture 1/2) : lancé puis en échec avant toute annonce de
        # session — la source est avertie qu'un tour a pu s'exécuter
        w = self.worker(self.switched())
        w._open_move_turn()
        w._turn_launched = True
        w._note_move_launched()
        self.assertTrue(self.moves.open_for("avatar")["target_turn_started"])
        w._finish_move_turn(False)
        row = self.row()
        self.assertEqual((row["host"], row["session_id"]), ("source", "sess-source"))
        self.assertIn("part of that turn may have run", row["pending_prompt"])

    def test_finish_of_a_launched_turn_notes_the_launch(self):
        # B1 : même sans marque au lancement, `finish(launched=True)` la pose
        first = self.first_turn(self.switched())
        first.finish(False, session=None, account=None, harness="claude", cwd=None,
                     launched=True)
        self.assertIn("part of that turn may have run", self.row()["pending_prompt"])

    def test_continuity_held_while_a_move_is_open(self):
        # B2 (relecture 1/2) : ni résumé ni rotation tant que la tentative est
        # ouverte — source retenue, puis cible pas encore vérifiée
        self.plan()
        source = self.worker({"lease_epoch": self.row()["lease_epoch"]})
        self.assertTrue(source._conversation_move_open())
        self.advance()
        w = self.worker(self.target_claim())
        self.assertTrue(w._conversation_move_open())
        w._open_move_turn()
        self.assertTrue(w._conversation_move_open())
        w._turn_launched = True
        w._note_session("sess-target", None, "claude", None, "/tgt", "", "claude")
        w._finish_move_turn(True)
        self.assertFalse(w._conversation_move_open())

    def test_queued_prompt_taken_then_refused_does_not_block_the_switch(self):
        # relecture dev3 (B2) : la vraie séquence du runner — consigne prise
        # (`running`), admission refusée, consigne remise ; le statut ne
        # reste pas `running` et la bascule part
        from types import SimpleNamespace
        from ameesh.runner import AgentWorker
        move = self.plan()
        registry.set_pending_prompt(self.db, "avatar", "une question de l'humain")
        self.assertIsNotNone(registry.take_pending_prompt(
            self.db, "avatar", "runner-src", self.epoch))
        self.assertEqual(self.row()["status"], "running")
        reason = self.begin()
        self.assertEqual(reason, M.BEGIN_REFUSAL)
        w = self.worker({"lease_epoch": self.epoch})
        w.runner = SimpleNamespace(host="source", runner_id="runner-src", dry_run=False,
                                   db_retry_max=1)
        w.wake = threading.Event()
        w.wait_timeout = lambda delay: 0
        w._release_running_for_move = AgentWorker._release_running_for_move.__get__(w)
        AgentWorker._begin_turn_refused(w, reason)
        row = self.row()
        self.assertEqual((row["status"], row["pending_prompt"]),
                         ("queued", "une question de l'humain"))
        self.assertEqual(self.advance(), [(move["move_id"], "prepared"),
                                          (move["move_id"], "switched")])

    def test_stale_source_release_never_touches_another_holder(self):
        # relecture cx2 : CAS sur le bail source — un détenteur périmé (autre
        # epoch, autre hôte) ou un arrêt opérateur ne sont jamais réécrits
        self.db.execute("UPDATE agent_registry SET status = 'running',"
                        " pending_prompt = 'p' WHERE name = 'avatar'")
        self.assertFalse(self.moves.release_refused(
            "avatar", owner="runner-src", epoch=self.epoch - 1, host="source"))
        self.assertFalse(self.moves.release_refused(
            "avatar", owner="runner-src", epoch=self.epoch, host="target"))
        self.assertFalse(self.moves.release_refused(
            "avatar", owner="runner-tgt", epoch=self.epoch, host="source"))
        self.assertEqual(self.row()["status"], "running")
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = 'avatar'")
        self.assertFalse(self.moves.release_refused(
            "avatar", owner="runner-src", epoch=self.epoch, host="source"))
        self.assertEqual(self.row()["status"], "stopped")
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'avatar'")
        self.assertTrue(self.moves.release_refused(
            "avatar", owner="runner-src", epoch=self.epoch, host="source"))
        self.assertEqual(self.row()["status"], "queued")

    def test_target_running_turn_survives_a_late_source_refusal(self):
        # relecture cx2 : la cible a réclamé et tourne ; le refus tardif de
        # la source (bail rendu au switch) ne touche pas son `running`
        from types import SimpleNamespace
        from ameesh.runner import AgentWorker
        self.plan()
        self.advance()
        lease = self.target_claim()
        self.db.execute("UPDATE agent_registry SET status = 'running' WHERE name = 'avatar'")
        w = self.worker({"lease_epoch": self.epoch})
        w.runner = SimpleNamespace(host="source", runner_id="runner-src", dry_run=False)
        AgentWorker._release_running_for_move(w)
        row = self.row()
        self.assertEqual((row["status"], row["lease_owner"], row["lease_epoch"]),
                         ("running", "runner-tgt", lease["lease_epoch"]))

    def test_mediated_executor_has_no_move_hooks(self):
        # B3 (relecture 2/2) : RemoteDb n'a pas `conversation_moves` — no-op
        # explicite, jamais AttributeError, jamais « ouvert pour toujours »
        from ameesh.storage.remote.client import RemoteDb
        self.plan()
        w = self.worker({"lease_epoch": self.row()["lease_epoch"]})
        w.db = RemoteDb(None, object())
        self.assertFalse(w._conversation_move_open())
        self.assertTrue(w._open_move_turn())
        self.assertIsNone(w._move_turn)

    def test_rotation_and_lot_switch_held_while_planned(self):
        # B4 (relecture 2/2) : rotation due et bascule de lot retenues tant
        # qu'une tentative vise l'agent ; session et reset intacts
        from ameesh.runner import AgentWorker
        reset = ("SELECT session_id, session_reset_at FROM agent_registry"
                 " WHERE name = 'avatar'")
        self.plan()
        before = self.db.query(reset)[0]
        w = self.worker({"lease_epoch": self.row()["lease_epoch"]})
        w.rotation_forcee = True
        self.assertFalse(AgentWorker.maybe_rotate(w))
        spec = {"lot": "4242", "prompt": "x"}
        self.assertIs(AgentWorker.rotate_for_lot(w, spec), spec)
        self.assertEqual(self.db.query(reset)[0], before)
        self.assertEqual(before["session_id"], "sess-source")

    def test_lot_switch_of_the_turn_held_while_a_move_is_open(self):
        # B (relecture 3) : la bascule de lot de `_run_turn_sous_battement`
        # (agent multi-projet, session ouverte ailleurs) est retenue — côté
        # source retenue comme au rejeu de la cible
        from unittest import mock
        from ameesh.runner import AgentWorker

        class Stop(Exception):
            pass
        self.plan()
        w = self.worker({"lease_epoch": self.row()["lease_epoch"]})
        switched = []
        w._workdir_lot = None
        w._lot_workdir_secondary = False
        w._resolve_workdir = lambda: self.tmp
        w._switch_lot = lambda *a: switched.append(a) or True
        w._adopt_originless_session = lambda *a: False
        w.agent.update(session_cwd="/elsewhere", session_origin="other",
                       session_work_item="1")

        def stop(spec):
            raise Stop()
        w._with_background_note = stop
        run = AgentWorker._run_turn_sous_battement.__get__(w)
        with mock.patch("ameesh.runner_sandbox.dispatch", return_value=None), \
                mock.patch("ameesh.projects.agent_projects", return_value=["a", "b"]):
            with self.assertRaises(Stop):
                run({"lot": "2", "prompt": "x"})
            self.assertEqual(switched, [])
            # sans tentative, la même bascule part (le chemin est bien celui-ci)
            self.db.execute("DELETE FROM conversation_moves")
            with self.assertRaises(Stop):
                run({"lot": "2", "prompt": "x"})
        self.assertEqual(switched, [("1", "2")])

    def test_switch_refused_when_the_source_session_changed(self):
        # B4 : garde au switch — une session changée depuis le plan échoue
        # la tentative avant tout effet ; la source reste chez elle
        move = self.plan()
        self.db.execute("UPDATE agent_registry SET session_id = 'sess-other'"
                        " WHERE name = 'avatar'")
        steps = self.advance()
        self.assertIn((move["move_id"], "failed"), steps)
        self.assertEqual(self.moves.get(move["move_id"])["error"],
                         "switch: source session changed since the plan")
        row = self.row()
        self.assertEqual((row["host"], row["session_id"]), ("source", "sess-other"))

    def test_switch_refused_when_the_source_lease_changed_since_the_plan(self):
        # #7093 (relecture 1/2) : même nom de détenteur, mais un bail rendu et
        # repris depuis le plan — ce n'est plus le bail que le plan a vu.
        move = self.plan()
        self.assertTrue(registry.release(self.db, "avatar", "runner-src", self.epoch))
        again = registry.claim(self.db, "avatar", "runner-src", ttl_seconds=300,
                               host="source")
        self.assertGreater(again["lease_epoch"], self.epoch)
        steps = self.advance()
        self.assertIn((move["move_id"], "failed"), steps)
        self.assertEqual(self.moves.get(move["move_id"])["error"],
                         "switch: source lease changed since the plan")
        row = self.row()
        self.assertEqual((row["host"], row["session_id"], row["lease_owner"]),
                         ("source", "sess-source", "runner-src"))

    def test_runner_pass_prepares_and_switches(self):
        from types import SimpleNamespace
        from ameesh.runner import Runner
        move = self.plan()
        adapter = SimpleNamespace(binary="/bin/sh", spec=SimpleNamespace(attach=["x"]))
        runner = SimpleNamespace(cfg=self.cfg, db=self.db, host="target",
                                 agents_filter=["avatar"])
        self.db.execute("UPDATE conversation_moves SET source_cwd = %s", (self.tmp,))
        from unittest import mock
        with mock.patch("ameesh.runner.agent_adapter", return_value=adapter), \
                mock.patch("ameesh.accounts.profiles", return_value=[]), \
                mock.patch("ameesh.accounts.default_home", return_value=self.tmp):
            steps = Runner.advance_conversation_moves(runner)
        self.assertEqual(steps, [(move["move_id"], "prepared"), (move["move_id"], "switched")])
        self.assertEqual(self.moves.get(move["move_id"])["checks"], READY)
        # another runner's admission list leaves the attempt alone
        runner.agents_filter = ["someone-else"]
        self.assertEqual(Runner.advance_conversation_moves(runner), [])


class ProbeTest(PgTestCase):
    def test_missing_harness_or_binary(self):
        from types import SimpleNamespace

        def missing(harness, binary):
            raise RuntimeError("no descriptor")
        verdicts = M.host_probes(self.cfg, {"harness": "claude"}, adapter_for=missing)
        self.assertFalse(verdicts["harness"])
        adapter = SimpleNamespace(binary="/nonexistent/claude",
                                  spec=SimpleNamespace(attach=False))
        verdicts = M.host_probes(self.cfg, {"harness": "claude"},
                                 adapter_for=lambda h, b: adapter)
        self.assertTrue(verdicts["harness"])
        self.assertFalse(verdicts["binary"])
        self.assertFalse(verdicts["interactive"])
