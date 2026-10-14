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

    def test_signature_fournisseur_positive_seule(self):
        # R1 cx3 B2 : « billing » (ou « plafond ») dans un texte libre ne
        # classe pas un quota — le mot seul ne désarme pas L48.
        for texte in ("I updated the billing page",
                      "the harness returned code 1: see the billing page",
                      "plafond atteint dans le cache local"):
            with self.subTest(texte=texte):
                self.assertIsNone(rate_limit.classify(error=texte))
                self.assertIsNone(rate_limit.classify(result=texte))

    def test_quota_prime_entre_les_textes(self):
        # R1 cx3 B2 : le quota prime sur le débit, tous textes confondus.
        self.assertEqual(rate_limit.classify(
            error="HTTP 429 Too Many Requests", stderr_tail="insufficient_quota"),
            rate_limit.QUOTA)
        self.assertEqual(rate_limit.classify(
            error="429", result="you have hit your weekly limit"),
            rate_limit.QUOTA)

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

    def test_l_echeance_durable_est_relue(self):
        # #1270 B1 : `retry at` est absolue et relue telle quelle ; elle ne se
        # recalcule pas (une tentative interrompue ne repousse pas la reprise).
        since = time.time() - 30
        retry_at = time.time() + 90
        texte = rate_limit.status_text("mail in 3s (failed)", rate_limit.QUOTA, 2,
                                       since, 120.0, retry_at=retry_at)
        etat = rate_limit.state(texte)
        self.assertEqual(etat["kind"], rate_limit.QUOTA)
        self.assertAlmostEqual(etat["retry_at"], retry_at, delta=1.0)
        self.assertAlmostEqual(rate_limit.remaining(etat), 90.0, delta=2.0)
        # marqueur d'avant le champ : repli sur le délai nominal
        ancien = "[rate_limited x1 since %s, retry in 60s] x" % rate_limit._stamp(since)
        self.assertEqual(rate_limit.state(ancien)["retry_at"], 0.0)
        self.assertAlmostEqual(rate_limit.remaining(rate_limit.state(ancien)), 60.0)

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
            self.assertAlmostEqual(worker.failure_wait(), 60.0, delta=2.0)
            # deuxième tour, même épisode : la cadence monte
            self.assertIsNotNone(self._tour(worker, **self.DEBIT))
            etat = rate_limit.state(registry.get(self.db, "rl")["status_text"])
            self.assertEqual(etat["streak"], 2)
            self.assertAlmostEqual(worker.failure_wait(), 120.0, delta=2.0)
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
        echeance = worker.rate_limit_retry_at
        self.assertGreater(echeance, time.time())
        worker.watchdog_stop.set()
        worker.release_lease()
        runner2 = Runner(self.cfg, self.db, once=True)
        lease = registry.claim(self.db, "rl3", runner2.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker2 = AgentWorker(runner2, registry.get(self.db, "rl3"), lease)
        self.addCleanup(worker2.watchdog_stop.set)
        self.assertEqual(worker2.rate_limit, rate_limit.RATE_LIMITED)
        self.assertEqual(worker2.rate_limit_streak, 1)
        # #1270 B1 : l'échéance absolue est relue telle quelle, jamais repoussée
        self.assertAlmostEqual(worker2.rate_limit_retry_at, echeance, delta=1.0)
        self.assertGreater(worker2.retry_remaining(), 0.0)
        self.assertAlmostEqual(worker2.failure_wait(), 60.0, delta=5.0)

    def test_begin_turn_preserve_l_echeance(self):
        # #1270 B1 : le statut d'un tour en cours garde l'épisode et son
        # échéance — un redémarrage pendant la tentative ne la repousse pas.
        _, worker = self._worker("rl4", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.DEBIT))
            echeance = worker.rate_limit_retry_at
            texte = worker._marqueurs_durables("mail turn (claude)")
            etat = rate_limit.state(texte)
            self.assertEqual(etat["kind"], rate_limit.RATE_LIMITED)
            self.assertEqual(etat["streak"], 1)
            self.assertAlmostEqual(etat["retry_at"], echeance, delta=1.0)
            self.assertIn("mail turn (claude)", texte)
        finally:
            worker.watchdog_stop.set()

    def test_aucun_lancement_avant_l_echeance(self):
        # #1270 B1 : même avec un réveil déjà posé, le mode banc ne lance pas
        # de tour avant l'échéance durable.
        _, worker = self._worker("rl5", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.DEBIT))
            tours = len(self.turns())
            worker.wake.set()  # un courrier/réveil en attente
            self.assertFalse(worker.process_once(0.0))
            self.assertEqual(len(self.turns()), tours)
            self.assertGreater(worker.retry_remaining(), 0.0)
        finally:
            worker.watchdog_stop.set()


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
