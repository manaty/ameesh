# SPDX-License-Identifier: AGPL-3.0-only
"""L11 — vitesse (0018) : interruption prioritaire, rotation, déplacement."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

from ameesh import mail, registry
from ameesh.runner import AgentWorker, Runner, write_worktree_marker

from .support import FAKEBIN, PgTestCase


class InterruptionTest(PgTestCase):
    def _cwd(self, name: str) -> str:
        path = os.path.join(self.tmp, "work", name)
        os.makedirs(path, exist_ok=True)
        return path

    def _worker(self, name: str = "l11", *, senders=("orch",)):
        cfg = dataclasses.replace(self.cfg, interrupt_senders=tuple(senders))
        runner = Runner(cfg, self.db, once=True)
        cwd = self._cwd(name)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        return runner, worker

    def test_urgent_autorise_passe_en_tete(self):
        runner, worker = self._worker()
        registry.set_pending_prompt(self.db, "l11", "consigne en attente")
        mail.send(self.db, "orch", "l11", "incident prioritaire", kind="event",
                  payload={"urgent": True})
        spec = worker.pick()
        self.assertEqual(spec["kind"], "urgent")
        self.assertIn("incident prioritaire", spec["prompt"])
        self.assertIn("prioritaire", spec["prompt"].lower())

    def test_urgent_non_autorise_reste_un_message(self):
        runner, worker = self._worker(senders=("orch",), name="l11b")
        mail.send(self.db, "inconnu", "l11b", "faux urgent", kind="event",
                  payload={"urgent": True})
        spec = worker.pick()
        self.assertIsNotNone(spec)
        self.assertNotEqual(spec["kind"], "urgent")
        self.assertEqual(len(mail.unread(self.db, "l11b")), 1)

    def test_interruption_arrete_le_tour_et_restaure_la_consigne(self):
        runner, worker = self._worker()
        self.register("l11", "claude", cwd=self._cwd("l11"), prompt="tour long")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "5"}
        spec = worker.pick()
        self.assertEqual(spec["kind"], "prompt")
        resultat: list = []
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                thread = threading.Thread(
                    target=lambda: resultat.append(worker.run_turn(spec)), daemon=True)
                thread.start()
                time.sleep(0.4)
                worker.request_preempt()
                thread.join(timeout=6)
            self.assertEqual(resultat, [False])
            row = registry.get(self.db, "l11")
            self.assertEqual(row["pending_prompt"], "tour long")
            self.assertEqual(row["status"], "queued")
            self.assertFalse(worker.lease_lost.is_set())
        finally:
            worker.watchdog_stop.set()

    def test_dispatch_prioritaire_selon_l_habilititation(self):
        runner, worker = self._worker()
        with runner.lock:
            runner.workers["l11"] = worker
        mail.send(self.db, "inconnu", "l11", "faux", kind="event",
                  payload={"urgent": True})
        faux = mail.unread(self.db, "l11")[0]["id"]
        runner.dispatch({"channel": "agent_mail",
                         "payload": json.dumps({"to": "l11", "id": faux})})
        self.assertFalse(worker.preempting.is_set())
        mail.send(self.db, "orch", "l11", "vrai", kind="event",
                  payload={"urgent": True})
        vrai = [m["id"] for m in mail.unread(self.db, "l11")][-1]
        runner.dispatch({"channel": "agent_mail",
                         "payload": json.dumps({"to": "l11", "id": vrai})})
        self.assertTrue(worker.preempting.is_set())


class RotationTest(PgTestCase):
    def _worker(self, name: str = "rot"):
        cfg = dataclasses.replace(self.cfg, session_min_turns=1,
                                  session_max_turn_seconds=0.0, session_max_tokens=0.0)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        return runner, worker

    def test_rotation_resume_puis_session_neuve(self):
        runner, worker = self._worker()
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "premier tour", "ids": []}))
                self.assertEqual(registry.get(self.db, "rot")["session_id"], "claude-sess-1")
                self.assertTrue(worker.rotation_due())
                self.assertTrue(worker.maybe_rotate())
                self.assertIsNone(registry.get(self.db, "rot")["session_id"])
                self.assertTrue(worker.resume_summary)
                historique = os.path.join(worker.state_dir, "session-history.jsonl")
                with open(historique, encoding="utf-8") as fh:
                    ligne = json.loads(fh.readline())
                self.assertEqual(ligne["session"], "claude-sess-1")
                self.assertTrue(ligne["resume"])
                # le tour suivant, en session neuve, ouvre sur le résumé
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "deuxième tour", "ids": []}))
            self.assertIn("Reprise de session après rotation",
                          " ".join(self.turns()[-1]["argv"]))
        finally:
            worker.watchdog_stop.set()

    def test_rotation_jamais_pendant_un_tour(self):
        runner, worker = self._worker()
        worker.session_turns = 5
        proc = subprocess.Popen(["sleep", "60"])
        worker.proc = proc
        try:
            self.assertFalse(worker.rotation_due())
        finally:
            proc.kill()
            proc.wait()
        worker.proc = None
        self.assertTrue(worker.rotation_due())


class DeplacementTest(PgTestCase):
    def _git(self, *args: str, cwd: str):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)

    def _depot_et_worktree(self):
        racine = os.path.join(self.tmp, "racine")
        depot = os.path.join(racine, "depot")
        os.makedirs(depot)
        self._git("init", "-q", cwd=depot)
        self._git("config", "user.email", "banc@example.invalid", cwd=depot)
        self._git("config", "user.name", "banc", cwd=depot)
        with open(os.path.join(depot, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        self._git("add", ".", cwd=depot)
        self._git("commit", "-q", "-m", "init", cwd=depot)
        wt = os.path.join(racine, "wt")
        self._git("worktree", "add", "-q", "-b", "lot/x", wt, cwd=depot)
        return racine, wt

    def test_adoption_d_un_worktree_deplace(self):
        racine, wt = self._depot_et_worktree()
        cfg = dataclasses.replace(self.cfg, worktree_roots=(racine,))
        self.register("dep", "claude", cwd=wt)
        write_worktree_marker(cfg, "dep", wt)
        runner = Runner(cfg, self.db, once=True)
        lease = registry.claim(self.db, "dep", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "dep"), lease)
        deplace = os.path.join(racine, "wt-renomme")
        os.rename(wt, deplace)
        # un autre agent occupe déjà le candidat : adoption refusée
        self.register("autre", "claude", cwd=deplace)
        self.assertIsNone(worker.adopt_moved_worktree())
        self.db.execute("UPDATE agent_registry SET cwd = NULL WHERE name = 'autre'")
        self.assertEqual(worker.adopt_moved_worktree(), deplace)
        self.assertEqual(registry.get(self.db, "dep")["cwd"], deplace)

    def test_aucun_candidat_si_le_dossier_na_pas_bouge(self):
        racine, wt = self._depot_et_worktree()
        cfg = dataclasses.replace(self.cfg, worktree_roots=(racine,))
        self.register("dep", "claude", cwd=wt)
        write_worktree_marker(cfg, "dep", wt)
        runner = Runner(cfg, self.db, once=True)
        lease = registry.claim(self.db, "dep", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "dep"), lease)
        # le dossier est toujours là : aucun candidat (l'ancien est exclu)
        self.assertIsNone(worker.adopt_moved_worktree())


if __name__ == "__main__":
    unittest.main()
