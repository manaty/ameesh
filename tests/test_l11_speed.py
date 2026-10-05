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

    def test_interruption_ne_bloque_pas_sur_un_journal(self):
        """Verdict L11 B1 : le moniteur de préemption coupe **avant** de
        journaliser (un tube saturé ne doit pas retarder l'arrêt)."""
        runner, worker = self._worker()
        self.register("l11", "claude", cwd=self._cwd("l11"), prompt="tour long")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "8"}
        spec = worker.pick()
        resultat: list = []

        def faux_log(message, *args, **kwargs):
            if "prioritaire" in str(message):
                time.sleep(30)  # simule un tube plein

        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch("ameesh.runner.log", side_effect=faux_log):
                thread = threading.Thread(
                    target=lambda: resultat.append(worker.run_turn(spec)), daemon=True)
                thread.start()
                time.sleep(0.4)
                worker.request_preempt()
                thread.join(timeout=6)
            self.assertFalse(thread.is_alive(), "l'arrêt a été bloqué par un journal")
            self.assertEqual(resultat, [False])
        finally:
            worker.watchdog_stop.set()

    def test_preemption_pendant_la_publication_est_rattrapee(self):
        """Verdict L11 B2 : la préemption arrivée avant la publication du `Popen`
        est rattrapée par `kill_if_preempted` (même classe que B5a-P)."""
        runner, worker = self._worker()
        proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
        worker.proc = proc
        worker.pgid = proc.pid
        worker.preempting.set()
        try:
            self.assertTrue(worker.kill_if_preempted())
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            worker.proc = None
            worker.preempting.clear()

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

    def test_dispatch_prioritaire_ne_bloque_pas_sur_un_journal(self):
        """Verdict L11 B1 (dispatch) : le signal de préemption part avant le
        journal ; un pipe saturé ne doit pas l'empêcher."""
        runner, worker = self._worker()
        with runner.lock:
            runner.workers["l11"] = worker
        mail.send(self.db, "orch", "l11", "vrai", kind="event",
                  payload={"urgent": True})
        mid = mail.unread(self.db, "l11")[-1]["id"]

        def bloque(message, *args, **kwargs):
            time.sleep(30)

        thread = threading.Thread(
            target=lambda: runner.dispatch({"channel": "agent_mail",
                                            "payload": json.dumps({"to": "l11", "id": mid})}),
            daemon=True)
        with mock.patch("ameesh.runner.log", side_effect=bloque):
            thread.start()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "le dispatch a été bloqué par un journal")
        self.assertTrue(worker.preempting.is_set())

    def test_reprise_apres_interruption_traite_l_urgent(self):
        """Verdict L11 B5 : le latch d'arrêt ne doit pas tuer la reprise — le
        tour prioritaire suivant traite bien le message."""
        runner, worker = self._worker()
        self.register("l11", "claude", cwd=self._cwd("l11"), prompt="tour long")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "5"}
        spec = worker.pick()
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
            mail.send(self.db, "orch", "l11", "incident prioritaire", kind="event",
                      payload={"urgent": True})
            urgent = worker.pick()
            self.assertEqual(urgent["kind"], "urgent")
            env2 = dict(env)
            env2["AMEESH_TEST_SLEEP"] = "0"
            with mock.patch.dict(os.environ, env2, clear=False):
                self.assertTrue(worker.run_turn(urgent),
                                "le tour prioritaire a été tué par le latch d'arrêt")
            self.assertEqual(mail.unread(self.db, "l11"), [])
            self.assertFalse(worker.stop_requested.is_set())
        finally:
            worker.watchdog_stop.set()


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

    def test_rotation_annulee_si_le_resume_echoue(self):
        """Verdict L11 B3 : un tour de résumé en échec (bail perdu, remplaçant)
        ne doit ni tourner ni effacer la session du remplaçant."""
        runner, worker = self._worker()
        registry.set_session(self.db, "rot", "session-du-remplacant")
        worker.agent = registry.get(self.db, "rot")
        worker.session_turns = 5
        try:
            with mock.patch.object(worker, "run_turn", return_value=False):
                self.assertFalse(worker.maybe_rotate())
        finally:
            worker.watchdog_stop.set()
        self.assertEqual(registry.get(self.db, "rot")["session_id"],
                         "session-du-remplacant")

    def test_clear_session_refuse_un_autre_bail(self):
        """Verdict L11 B3 : l'effacement de session est fencé par le bail."""
        runner, worker = self._worker()
        registry.set_session(self.db, "rot", "session-protegee")
        self.assertFalse(registry.clear_session(self.db, "rot", "autre-runner",
                                                int(worker.epoch)))
        self.assertEqual(registry.get(self.db, "rot")["session_id"], "session-protegee")
        self.assertTrue(registry.clear_session(self.db, "rot", runner.runner_id,
                                               int(worker.epoch)))
        self.assertIsNone(registry.get(self.db, "rot")["session_id"])

    def test_clear_session_refuse_un_bail_expire(self):
        """Verdict L11 B3 : même owner et même epoch, un bail expiré ne peut
        rien effacer (la session d'un remplaçant reste intacte)."""
        runner, worker = self._worker()
        registry.set_session(self.db, "rot", "session-protegee")
        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at = now() - interval '1 second' "
            "WHERE name = 'rot'")
        self.assertFalse(registry.clear_session(self.db, "rot", runner.runner_id,
                                                int(worker.epoch)))
        self.assertEqual(registry.get(self.db, "rot")["session_id"], "session-protegee")


class StopTriggersTest(PgTestCase):
    """Suite **paramétrée** de tous les déclencheurs d'arrêt (verdicts codex3 L11).

    Chaque déclencheur rejoue les mêmes scénarios : harnais publié, journal
    saturé, `Popen` publié en retard. Un nouveau chemin d'arrêt (garde de budget
    L13, demain) hérite de ces sondes en s'ajoutant à `TRIGGERS`.
    """

    TRIGGERS = (
        ("bail-perdu", lambda w: w.lease_lost.set()),
        ("preemption", lambda w: w.preempting.set()),
        ("arret", lambda w: w.stopping.set()),
        ("echeance", lambda w: setattr(w, "lease_deadline", time.time() - 1.0)),
        ("stop-demande", lambda w: w.stop_requested.set()),
        # L26 : `ameesh restart` passe par le même point de passage
        ("redemarrage", lambda w: w.restarting.set()),
    )

    def _worker(self, name: str):
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return AgentWorker(runner, registry.get(self.db, name), lease)

    @staticmethod
    def _proc() -> subprocess.Popen:
        return subprocess.Popen(["sleep", "60"], start_new_session=True)

    def _publie(self, worker, proc):
        worker.proc = proc
        worker.pgid = proc.pid

    def test_chaque_declencheur_coupe_le_harnais(self):
        for label, declenche in self.TRIGGERS:
            with self.subTest(declencheur=label):
                worker = self._worker("st-%s" % label)
                proc = self._proc()
                self._publie(worker, proc)
                try:
                    declenche(worker)
                    self.assertTrue(worker.kill_if_stop_requested())
                    proc.wait(timeout=5)
                    self.assertIsNotNone(proc.poll())
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    worker.proc = None

    def test_chaque_declencheur_coupe_meme_journal_bloque(self):
        def bloque(message, *args, **kwargs):
            time.sleep(30)

        for label, declenche in self.TRIGGERS:
            with self.subTest(declencheur=label):
                worker = self._worker("sb-%s" % label)
                proc = self._proc()
                self._publie(worker, proc)
                try:
                    with mock.patch("ameesh.runner.log", side_effect=bloque):
                        declenche(worker)
                        debut = time.monotonic()
                        self.assertTrue(worker.kill_if_stop_requested())
                        proc.wait(timeout=5)
                        ecoule = time.monotonic() - debut
                    self.assertIsNotNone(proc.poll())
                    self.assertLess(ecoule, 2.0,
                                    "l'arrêt dépend d'une E/S sur le chemin (%s)" % label)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    worker.proc = None

    def test_chaque_declencheur_est_rattrape_apres_publication(self):
        for label, declenche in self.TRIGGERS:
            with self.subTest(declencheur=label):
                worker = self._worker("sp-%s" % label)
                # le déclencheur part avant la publication du Popen
                worker.proc = None
                declenche(worker)
                proc = self._proc()
                self._publie(worker, proc)
                try:
                    self.assertTrue(worker.kill_if_stop_requested())
                    proc.wait(timeout=5)
                    self.assertIsNotNone(proc.poll())
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    worker.proc = None


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
        wt = os.path.join(racine, "manaty", "wt")
        os.makedirs(os.path.dirname(wt), exist_ok=True)
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
        deplace = os.path.join(racine, "manaty", "wt-renomme")
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


class CanonSyncTest(PgTestCase):
    """Spec §4.4 : l'exécuteur lance `canon sync` au démarrage puis périodiquement."""

    def _runner(self, *, canon: str = "", interval: float = 300.0) -> Runner:
        cfg = dataclasses.replace(self.cfg, canon=canon, canon_sync_interval=interval)
        return Runner(cfg, self.db, once=True)

    def test_sans_canon_aucun_sync(self):
        runner = self._runner()
        with mock.patch("ameesh.canon_sync.sync") as sync:
            self.assertTrue(runner.canon_sync_once())
        sync.assert_not_called()

    def test_sync_appelle_le_canon_de_l_hote(self):
        runner = self._runner(canon="/tmp/canon-fictif")
        appels: list = []

        def faux_sync(db, canon, host):
            appels.append((canon, host))
            return type("R", (), {"status": "ok"})()

        with mock.patch("ameesh.canon.from_config", return_value="canon-fictif"), \
                mock.patch("ameesh.canon_sync.sync", side_effect=faux_sync):
            self.assertTrue(runner.canon_sync_once())
        self.assertEqual(appels, [("canon-fictif", self.cfg.host)])

    def test_sync_en_echec_ne_leve_pas(self):
        runner = self._runner(canon="/tmp/canon-fictif")
        with mock.patch("ameesh.canon.from_config", side_effect=OSError("canon absent")):
            self.assertFalse(runner.canon_sync_once())

    def test_one_shot_synchronise_au_demarrage(self):
        runner = self._runner(canon="/tmp/canon-fictif")
        appels: list = []
        with mock.patch.object(Runner, "canon_sync_once",
                               side_effect=lambda: appels.append(1) or True):
            runner.start_canon_sync()
        self.assertEqual(appels, [1])  # en ligne, pas de fil pour un one-shot
        self.assertIsNone(runner.canon_thread)

    def test_intervalle_nul_synchronise_seulement_au_demarrage(self):
        runner = self._runner(canon="/tmp/canon-fictif", interval=0.0)
        appels: list = []
        with mock.patch.object(Runner, "canon_sync_once",
                               side_effect=lambda: appels.append(1) or True):
            runner.start_canon_sync()
        self.assertEqual(appels, [1])
        self.assertIsNone(runner.canon_thread)

    def test_boucle_periodique_demarree_puis_arretee(self):
        runner = self._runner(canon="/tmp/canon-fictif", interval=3600.0)
        runner.once = False
        appels: list = []
        with mock.patch.object(Runner, "canon_sync_once",
                               side_effect=lambda: appels.append(1) or True):
            runner.start_canon_sync()
            self.wait_for(lambda: appels, timeout=3)
            self.assertIsNotNone(runner.canon_thread)
            self.assertTrue(runner.canon_thread.is_alive())
            runner.stop.set()
            runner.canon_thread.join(timeout=5)
            self.assertFalse(runner.canon_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
