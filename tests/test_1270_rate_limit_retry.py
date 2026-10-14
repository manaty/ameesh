# SPDX-License-Identifier: AGPL-3.0-only
"""#1270 : un 429 de débit passager n'arrête pas l'exécuteur.

Le tour qui prend un 429 attend puis reprend seul, à délais cadencés
(1, 2, 5, 10 min), et ne compte pas comme un échec rapide (L48). Un quota
épuisé est distingué du débit passager ; l'état vit dans `status_text` et
survit au redémarrage. L'alerte #1268 n'est levée qu'après 15 min d'un épisode
de débit entier — un 429 d'une minute ne réveille personne.
"""
from __future__ import annotations

import time
import unittest

from ameesh import exploitation, rate_limit, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_l30_accounts import _Base


class ClassifyTest(unittest.TestCase):
    def test_debit_passager(self):
        for texte in ("rate_limit_error: 429 Too Many Requests",
                      "HTTP 429: rate limit exceeded",
                      "overloaded_error: Overloaded",
                      "upstream: retry-after 30"):
            with self.subTest(texte=texte):
                self.assertEqual(rate_limit.classify(error=texte),
                                 rate_limit.RATE_LIMITED)
                self.assertEqual(rate_limit.classify(result=texte),
                                 rate_limit.RATE_LIMITED)
                self.assertEqual(rate_limit.classify(stderr_tail="bruit\n" + texte),
                                 rate_limit.RATE_LIMITED)

    def test_quota_est_distingue_et_prime(self):
        for texte in ("insufficient_quota: you exceeded your current quota",
                      "You've hit your usage limit",
                      "429 Too Many Requests: insufficient_quota",
                      "Go to your billing page: credit balance is too low"):
            with self.subTest(texte=texte):
                self.assertEqual(rate_limit.classify(error=texte), rate_limit.QUOTA)

    def test_aucune_conclusion_sur_un_texte_illisible(self):
        # ni vide, ni erreur sans rapport ne concluent quoi que ce soit sur le
        # budget : la garde comptable (L226b) reste seule juge.
        for texte in ("", "the harness returned code 1", "400 model not found"):
            with self.subTest(texte=texte):
                self.assertIsNone(rate_limit.classify(error=texte))


class DelaiTest(unittest.TestCase):
    def test_cadence_1_2_5_10_min_puis_palier(self):
        self.assertEqual([rate_limit.delay(n) for n in (1, 2, 3, 4, 5, 99)],
                         [60.0, 120.0, 300.0, 600.0, 600.0, 600.0])


class EtatTest(unittest.TestCase):
    def test_aller_retour(self):
        since = time.time() - 30
        texte = rate_limit.status_text("mail in 3s (failed)", rate_limit.RATE_LIMITED,
                                       2, since, 120.0)
        etat = rate_limit.state(texte)
        self.assertEqual((etat["kind"], etat["streak"], etat["wait"]),
                         (rate_limit.RATE_LIMITED, 2, 120.0))
        self.assertAlmostEqual(etat["since"], since, delta=1.0)

    def test_marqueur_derriere_la_serie_1268(self):
        texte = exploitation.turn_failure_status_text(
            rate_limit.status_text("x (failed)", rate_limit.QUOTA, 1, time.time(), 60.0),
            1, critical=True)
        self.assertEqual(rate_limit.state(texte)["kind"], rate_limit.QUOTA)
        self.assertEqual(exploitation.critical_failure_streak(texte), 1)

    def test_texte_sans_marqueur(self):
        for texte in ("", "mail in 20s", "critical failures x2: mail in 3s (failed)"):
            with self.subTest(texte=texte):
                self.assertIsNone(rate_limit.state(texte))

    def test_alerte_taise_puis_autorisee(self):
        now = time.time()
        etat = {"kind": rate_limit.RATE_LIMITED, "streak": 2, "since": now - 300,
                "wait": 120.0}
        self.assertTrue(rate_limit.alert_suppressed(etat, 2, now=now))
        # causes mêlées : l'épisode ne couvre pas toute la série -> alerte
        self.assertFalse(rate_limit.alert_suppressed(etat, 3, now=now))
        vieux = dict(etat, since=now - rate_limit.PERSIST_S - 1)
        self.assertFalse(rate_limit.alert_suppressed(vieux, 2, now=now))


class RepriseTest(_Base):
    """Un vrai tour qui prend un 429 : attente cadencée, reprise, pas d'arrêt."""

    DEBIT = {"AMEESH_TEST_RESULT": "rate_limit_error: 429 Too Many Requests",
             "AMEESH_TEST_EXIT": "1"}

    def test_429_attend_et_reprend_sans_arret(self):
        _, worker = self._worker("rl", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.DEBIT))
            ligne = registry.get(self.db, "rl")
            self.assertEqual(ligne["status"], "blocked")
            etat = rate_limit.state(ligne["status_text"])
            self.assertEqual((etat["kind"], etat["streak"], etat["wait"]),
                             (rate_limit.RATE_LIMITED, 1, 60.0))
            # pas un échec rapide : L48 ne peut pas arrêter l'agent
            self.assertFalse(worker.fast_failure)
            self.assertAlmostEqual(worker.failure_wait(), 60.0)
            # deuxième tour, même épisode : la cadence monte
            self.assertIsNotNone(self._tour(worker, **self.DEBIT))
            etat = rate_limit.state(registry.get(self.db, "rl")["status_text"])
            self.assertEqual(etat["streak"], 2)
            self.assertAlmostEqual(worker.failure_wait(), 120.0)
            self.assertFalse(worker.fast_failure)
        finally:
            worker.watchdog_stop.set()

    def test_un_succes_efface_l_episode(self):
        _, worker = self._worker("rl2", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.DEBIT))
            self.assertEqual(worker.rate_limit, rate_limit.RATE_LIMITED)
            self.assertIsNotNone(self._tour(worker))  # tour réussi (défaut)
            ligne = registry.get(self.db, "rl2")
            self.assertEqual(ligne["status"], "idle")
            self.assertIsNone(rate_limit.state(ligne["status_text"]))
            self.assertIsNone(worker.rate_limit)
            self.assertEqual(worker.failure_streak, 0)
            self.assertAlmostEqual(worker.failure_wait(), worker.runner.failure_backoff)
        finally:
            worker.watchdog_stop.set()

    def test_l_episode_survit_au_redemarrage(self):
        _, worker = self._worker("rl3", "claude", {})
        self.assertIsNotNone(self._tour(worker, **self.DEBIT))
        worker.watchdog_stop.set()
        worker.release_lease()
        runner2 = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "rl3", runner2.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker2 = AgentWorker(runner2, registry.get(self.db, "rl3"), lease)
        self.addCleanup(worker2.watchdog_stop.set)
        self.assertEqual(worker2.rate_limit, rate_limit.RATE_LIMITED)
        self.assertEqual(worker2.rate_limit_streak, 1)
        self.assertAlmostEqual(worker2.failure_wait(), 60.0)


class AlerteTest(PgTestCase):
    """L'alerte #1268 est taisée tant que le débit est passager et récent."""

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "orch", harness="codex", host=self.cfg.host)

    def _alerte(self, texte: str):
        registry.set_status(self.db, "orch", "blocked", status_text=texte,
                            error="rate_limit_error: 429 Too Many Requests")
        return [a for a in exploitation.alerts(self.cfg, self.db)
                if a["type"] == "critical_turn_failures"]

    def _statut(self, streak: int, since: float) -> str:
        return exploitation.turn_failure_status_text(
            rate_limit.status_text("mail in 3s (failed)", rate_limit.RATE_LIMITED,
                                   streak, since, rate_limit.delay(streak)),
            streak, critical=True)

    def test_debit_passager_ne_reveille_pas(self):
        self.assertEqual(self._alerte(self._statut(2, time.time() - 300)), [])

    def test_debit_qui_persiste_alerte(self):
        [alerte] = self._alerte(self._statut(3, time.time() - rate_limit.PERSIST_S - 61))
        self.assertIn("rate-limited", alerte["detail"])
        self.assertIn("429", alerte["detail"])

    def test_causes_melees_alertent_comme_avant(self):
        texte = rate_limit.status_text("mail in 3s (failed)", rate_limit.RATE_LIMITED,
                                       1, time.time() - 10, 60.0)
        [alerte] = self._alerte(exploitation.turn_failure_status_text(
            texte, 3, critical=True))
        self.assertEqual(alerte["value"], 3)


if __name__ == "__main__":
    unittest.main()
