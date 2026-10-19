# SPDX-License-Identifier: AGPL-3.0-only
"""#2729 : le latch de préemption est consommé à chaque tour, et un agent qui
enchaîne N tours préemptés sans en finir lève l'alerte `preemption_streak`.

Incident du 19/10 : après un message prioritaire, mty-amh-impl-01 a enchaîné
185 tours arrêtés par « harness stopped (preemption) » en une heure — le
drapeau n'était remis à zéro qu'au tour `urgent`, jamais au tour ordinaire
suivant. Le correctif est le `clear()` du début de tour (témoin dans
`test_l11_speed`) ; ce fichier couvre la série, son marqueur durable et
l'alerte qui la rend visible.
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

from ameesh import exploitation, registry
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class MarqueurTest(unittest.TestCase):
    def test_le_marqueur_porte_la_serie_et_le_debut_d_episode(self):
        since = time.time() - 120
        texte = exploitation.preemption_status_text(
            "turn interrupted: priority message", 5, since=since)
        self.assertTrue(texte.startswith("turn interrupted: priority message ["))
        self.assertIn("preemptions x5 since ", texte)
        self.assertEqual(exploitation.preemption_streak(texte), 5)
        self.assertAlmostEqual(exploitation.preemption_since(texte), since, delta=1.0)

    def test_le_marqueur_ne_masque_pas_les_prefixes_d_echec(self):
        # #2729 : le marqueur de préemption est un suffixe justement pour que
        # les marqueurs d'échec et de débit, relus en tête, restent lisibles.
        echec = exploitation.turn_failure_status_text(
            "mail in 3s (failed)", 2, critical=True)
        texte = exploitation.preemption_status_text(echec, 3)
        self.assertEqual(exploitation.critical_failure_streak(texte), 2)
        self.assertEqual(exploitation.failure_streak(texte), 2)
        self.assertEqual(exploitation.preemption_streak(texte), 3)

    def test_un_statut_ordinaire_ne_porte_pas_de_serie(self):
        for texte in ("", "mail in 30s", "turn interrupted: priority message"):
            with self.subTest(texte=texte):
                self.assertEqual(exploitation.preemption_streak(texte), 0)
                self.assertEqual(exploitation.preemption_since(texte), 0.0)


class AlerteTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "l11", harness="claude", host=self.cfg.host)

    def _alertes(self):
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "preemption_streak"]

    def test_cinq_preemptions_d_affilee_levent_l_alerte(self):
        since = time.time() - 300
        registry.set_status(
            self.db, "l11", "queued",
            status_text=exploitation.preemption_status_text(
                "turn interrupted: priority message",
                exploitation.PREEMPTIONS_ALERT, since=since))
        [alerte] = self._alertes()
        self.assertEqual(alerte["agent"], "l11")
        self.assertEqual(alerte["value"], exploitation.PREEMPTIONS_ALERT)
        self.assertEqual(alerte["threshold"], exploitation.PREEMPTIONS_ALERT)
        self.assertAlmostEqual(alerte["since"], since, delta=1.0)

    def test_quatre_preemptions_ne_levent_pas_l_alerte(self):
        registry.set_status(
            self.db, "l11", "queued",
            status_text=exploitation.preemption_status_text(
                "turn interrupted: priority message",
                exploitation.PREEMPTIONS_ALERT - 1))
        self.assertEqual(self._alertes(), [])

    def test_un_tour_qui_finit_resout_l_alerte(self):
        registry.set_status(
            self.db, "l11", "queued",
            status_text=exploitation.preemption_status_text(
                "turn interrupted: priority message",
                exploitation.PREEMPTIONS_ALERT))
        self.assertEqual(len(self._alertes()), 1)
        registry.set_status(self.db, "l11", "idle", status_text="prompt in 4s")
        self.assertEqual(self._alertes(), [])


class SerieTest(PgTestCase):
    """La série vit dans la ligne : elle survit au redémarrage de l'exécuteur."""

    def _worker(self, status_text: str) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "l11")
        os.makedirs(cwd, exist_ok=True)
        self.register("l11", "claude", cwd=cwd)
        registry.set_status(self.db, "l11", "queued", status_text=status_text)
        lease = registry.claim(self.db, "l11", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "l11"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_la_serie_est_relue_et_preservee_pendant_le_tour(self):
        since = time.time() - 60
        worker = self._worker(exploitation.preemption_status_text(
            "turn interrupted: priority message", 4, since=since))
        self.assertEqual(worker.preemption_streak, 4)
        texte = worker._marqueurs_durables("mail turn (claude)")
        self.assertEqual(exploitation.preemption_streak(texte), 4)
        self.assertAlmostEqual(exploitation.preemption_since(texte), since, delta=1.0)


class FenetreDeSelectionTest(PgTestCase):
    """#2729 B1 : une préemption reçue APRÈS la sélection, pendant la
    préparation (potentiellement lente) est un arrêt pour le tour sélectionné.
    La consommation du début de tour ne doit pas l'effacer comme un résidu."""

    def _worker(self) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "l11")
        os.makedirs(cwd, exist_ok=True)
        self.register("l11", "claude", cwd=cwd, prompt="tour long")
        lease = registry.claim(self.db, "l11", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "l11"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_une_preemption_pendant_la_preparation_interrompt_le_tour(self):
        worker = self._worker()
        spec = worker.pick()  # sélection AVANT la demande
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "prompt")

        # Barrière dans la préparation, après la sélection et avant le clear
        # du début de tour : `_ouverture` est appelée par `_run_turn`.
        preparation_atteinte = threading.Event()
        liberer = threading.Event()
        ouverture = worker._ouverture

        def ouverture_lente():
            preparation_atteinte.set()
            self.assertTrue(liberer.wait(5), "barrière de préparation non libérée")
            return ouverture()

        resultat: list = []
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "5"}
        fil = None
        try:
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(worker, "_ouverture", side_effect=ouverture_lente):
                fil = threading.Thread(
                    target=lambda: resultat.append(worker.run_turn(spec)), daemon=True)
                fil.start()
                self.assertTrue(preparation_atteinte.wait(5),
                                "préparation du tour non atteinte")
                # Ordre déterministe de la relecture : pick() -> préparation ->
                # request_preempt() (dispatch) -> le début de tour ne doit pas
                # effacer cette NOUVELLE demande.
                worker.request_preempt()
                self.assertTrue(worker.preempting.is_set())
                liberer.set()
                fil.join(timeout=10)
            self.assertFalse(fil.is_alive())
            self.assertEqual(resultat, [False],
                             "la préemption reçue pendant la préparation a été effacée")
            self.assertTrue(worker.preempting.is_set())

            # Reprise normale : aucun nouveau signal ; la demande, désormais
            # antérieure à la nouvelle sélection, est bien consommée.
            reprise = worker.pick()
            self.assertIsNotNone(reprise)
            env["AMEESH_TEST_SLEEP"] = "0"
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(reprise),
                                "le tour de reprise a été tué par le latch")
            self.assertFalse(worker.preempting.is_set())
        finally:
            liberer.set()
            worker.watchdog_stop.set()


class BoutEnBoutTest(PgTestCase):
    """Le scénario de l'incident, de bout en bout : N préemptions réelles
    lèvent l'alerte, puis un tour ordinaire la résout — sans nouveau
    `request_preempt`, ce qui prouve aussi que le latch est consommé."""

    def _worker(self) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "l11")
        os.makedirs(cwd, exist_ok=True)
        self.register("l11", "claude", cwd=cwd, prompt="tour long")
        lease = registry.claim(self.db, "l11", runner.runner_id, 3600)
        worker = AgentWorker(runner, registry.get(self.db, "l11"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_la_serie_leve_l_alerte_puis_un_tour_la_resout(self):
        worker = self._worker()
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_SLEEP": "5"}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                for rang in range(1, exploitation.PREEMPTIONS_ALERT + 1):
                    spec = worker.pick()
                    self.assertIsNotNone(spec)
                    resultat: list = []
                    thread = threading.Thread(
                        target=lambda: resultat.append(worker.run_turn(spec)),
                        daemon=True)
                    thread.start()
                    time.sleep(0.4)
                    worker.request_preempt()
                    thread.join(timeout=10)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(resultat, [False])
                    self.assertEqual(worker.preemption_streak, rang)
                    ligne = registry.get(self.db, "l11")
                    self.assertEqual(
                        exploitation.preemption_streak(ligne["status_text"]), rang)
            alertes = [a for a in exploitation.alerts(self.cfg, self.db)
                       if a["type"] == "preemption_streak"]
            self.assertEqual(len(alertes), 1)
            self.assertEqual(alertes[0]["value"], exploitation.PREEMPTIONS_ALERT)

            # Aucun nouveau `request_preempt` : sans le clear() du début de
            # tour, ce tour serait tué par le latch resté levé.
            spec = worker.pick()
            self.assertIsNotNone(spec)
            env["AMEESH_TEST_SLEEP"] = "0"
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(spec),
                                "le tour ordinaire a été tué par le latch de préemption")
            self.assertEqual(worker.preemption_streak, 0)
            self.assertFalse(worker.preempting.is_set())
            self.assertEqual([a for a in exploitation.alerts(self.cfg, self.db)
                              if a["type"] == "preemption_streak"], [])
        finally:
            worker.watchdog_stop.set()


if __name__ == "__main__":
    unittest.main()
