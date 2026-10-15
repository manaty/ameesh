# SPDX-License-Identifier: AGPL-3.0-only
"""#1270 : un 429 de débit passager n'arrête pas l'exécuteur.

Le tour qui prend un 429 attend puis reprend seul, à délais cadencés
(1, 2, 5, 10 min), et ne compte pas comme un échec rapide (L48). Un quota
épuisé est distingué du débit passager ; l'état vit dans `status_text` et
survit au redémarrage. L'alerte #1268 n'est levée qu'après 15 min d'un épisode
de débit entier — un 429 d'une minute ne réveille personne.
"""
from __future__ import annotations

import dataclasses
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import exploitation, rate_limit, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_l30_accounts import _Base


def attendre(cond, delai=10.0, pas=0.02) -> bool:
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(pas)
    return cond()


class ClassifyTest(unittest.TestCase):
    def test_debit_passager(self):
        for texte in ("rate_limit_error: 429 Too Many Requests",
                      "HTTP 429: rate limit exceeded",
                      "overloaded_error: Overloaded",
                      "upstream: retry-after 30"):
            with self.subTest(texte=texte):
                self.assertEqual(rate_limit.classify(error=texte),
                                 rate_limit.RATE_LIMITED)
                self.assertEqual(rate_limit.classify(stderr_tail="bruit\n" + texte),
                                 rate_limit.RATE_LIMITED)

    def test_un_texte_libre_qui_parle_de_debit_ne_classe_pas(self):
        # P2 dev1 (14/10) : le résultat libre du tour (dernier message du
        # modèle) peut *parler* d'un débit sans l'avoir subi — il ne désarme
        # pas L48 et ne tait pas l'alerte #1268.
        for texte in ("handled the rate limit exceeded case and too many requests retries",
                      "rate_limit_error: 429 Too Many Requests",
                      "429",
                      "Error code: 429 - rate limit reached"):
            with self.subTest(texte=texte):
                self.assertIsNone(rate_limit.classify(result=texte))

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

    def test_quota_prime_entre_les_canaux_fournisseur(self):
        # R1 cx3 B2 : le quota prime sur le débit, parmi les textes du
        # fournisseur ; le résultat libre ne départage rien (#1397).
        self.assertEqual(rate_limit.classify(
            error="HTTP 429 Too Many Requests", stderr_tail="insufficient_quota"),
            rate_limit.QUOTA)
        self.assertEqual(rate_limit.classify(
            error="429", result="you have hit your weekly limit"),
            rate_limit.RATE_LIMITED)

    def test_quota_seulement_sur_les_canaux_fournisseur(self):
        # #1397 (P3 dev1) : le résultat libre du tour peut *parler* d'un quota
        # sans en subir — « not near the usage limit » ne classe pas un quota
        # (sinon une phrase du modèle suffit à déclencher la pause de quota).
        # Les deux cas : résultat libre ignoré, canaux fournisseur classés.
        for texte in ("You've hit your usage limit",
                      "not near the usage limit",
                      "insufficient_quota",
                      "credit balance is too low"):
            with self.subTest(texte=texte, canal="result"):
                self.assertIsNone(rate_limit.classify(result=texte))
        for texte in ("insufficient_quota: you exceeded your current quota",
                      "You've hit your usage limit",
                      "credit balance is too low"):
            with self.subTest(texte=texte, canal="error"):
                self.assertEqual(rate_limit.classify(error=texte), rate_limit.QUOTA)
            with self.subTest(texte=texte, canal="stderr"):
                self.assertEqual(rate_limit.classify(stderr_tail=texte),
                                 rate_limit.QUOTA)

    def test_aucune_conclusion_sur_un_texte_illisible(self):
        # ni vide, ni erreur sans rapport ne concluent quoi que ce soit sur le
        # budget : la garde comptable (L226b) reste seule juge.
        for texte in ("", "the harness returned code 1", "400 model not found"):
            with self.subTest(texte=texte):
                self.assertIsNone(rate_limit.classify(error=texte))

    def test_debit_seulement_sur_signature_fournisseur(self):
        # P2 dev1/cx3 (14/10) : un numéro de ligne « 429 » (trace Node,
        # request.py, api/client.py), le sujet « rate limit » ou l'adjectif
        # « overloaded » d'un texte libre ne sont pas des signatures de débit
        # — sinon un tour réellement en échec repart toutes les 10 min (L48
        # désarmé) et l'alerte #1268 est tue.
        for texte in ("Traceback (most recent call last):\n"
                      "  File \"runner.py\", line 429, in run\n"
                      "ValueError: boom",
                      "Traceback (most recent call last):\n"
                      "  File \"request.py\", line 429, in send\n"
                      "ValueError: boom",
                      "File \"/srv/app/api/client.py\", line 429",
                      "(/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js:429:15)",
                      "runner.py:429: in rate_limit",
                      "j'ai corrigé le handling du rate limit dans rate_limit.py",
                      "le parseur est overloaded par les options",
                      "voir la ligne 429 du journal"):
            with self.subTest(texte=texte):
                self.assertIsNone(rate_limit.classify(error=texte))
                self.assertIsNone(rate_limit.classify(result=texte))
                self.assertIsNone(rate_limit.classify(stderr_tail=texte))

    def test_signatures_ancrees_du_debit(self):
        # les formes fournisseur réelles restent des signatures : en-tête HTTP,
        # statut explicite, phrase 429, ou statut seul.
        for texte in ("Error code: 429 - rate limit reached",
                      "HTTP 429 Too Many Requests",
                      "HTTP/1.1 429 Too Many Requests",
                      "last status: 429",
                      "status_code=429",
                      "429",
                      "429 Client Error: Too Many Requests for url",
                      "RateLimitError: too many requests",
                      "The engine is currently overloaded, please try again later",
                      "Rate limit exceeded"):
            with self.subTest(texte=texte):
                self.assertEqual(rate_limit.classify(error=texte),
                                 rate_limit.RATE_LIMITED)
                self.assertEqual(rate_limit.classify(stderr_tail=texte),
                                 rate_limit.RATE_LIMITED)

    def test_pannes_passageres_deepseek(self):
        # Complément bureau du 15/10 03:06 : la reprise bornée #1270 couvre
        # aussi les pannes passagères du fournisseur DeepSeek — flux SSE
        # malformé, service surchargé, passerelle 5xx. Formes réelles relevées
        # dans les `stderr.log` des exécuteurs :
        # « dsh: MALFORMED_RESPONSE: DeepSeek Messages SSE event type
        # mismatch » et « Service is too busy. We advise users to temporarily
        # switch to alternative LLM API service providers. ».
        for texte in ("dsh: MALFORMED_RESPONSE: DeepSeek Messages SSE event type mismatch",
                      "MALFORMED_RESPONSE",
                      "DeepSeek Messages SSE event type mismatch",
                      "SSE error: stream closed unexpectedly",
                      "Service is too busy. We advise users to temporarily switch to "
                      "alternative LLM API service providers.",
                      "HTTP 502 Bad Gateway",
                      "status: 503",
                      "Error code: 504",
                      "502 Bad Gateway",
                      "503 Service Unavailable",
                      "504 Gateway Timeout",
                      "500 Internal Server Error"):
            with self.subTest(texte=texte):
                self.assertEqual(rate_limit.classify(stderr_tail=texte),
                                 rate_limit.RATE_LIMITED)
                self.assertEqual(rate_limit.classify(error=texte),
                                 rate_limit.RATE_LIMITED)

    def test_un_5xx_de_trace_ne_classe_pas(self):
        # même exigence d'ancrage que le 429 : un numéro de ligne « 5xx » ou un
        # mot libre ne sont pas des signatures de passerelle.
        for texte in ("Traceback (most recent call last):\n"
                      "  File \"runner.py\", line 502, in run\n"
                      "ValueError: boom",
                      "File \"/srv/app/api/client.py\", line 503",
                      "voir la ligne 504 du journal",
                      "le flux a été malformed puis réessayé"):
            with self.subTest(texte=texte):
                self.assertIsNone(rate_limit.classify(error=texte))
                self.assertIsNone(rate_limit.classify(result=texte))
                self.assertIsNone(rate_limit.classify(stderr_tail=texte))


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
    """Un vrai tour qui prend un 429 : attente cadencée, reprise, pas d'arrêt.

    Le débit est écrit sur le **stderr du fournisseur** (le vrai canal d'un
    429) ; le résultat libre du tour ne conclut plus rien sur un débit (P2
    dev1 du 14/10)."""

    DEBIT = {"AMEESH_TEST_STDERR": "rate_limit_error: 429 Too Many Requests\n",
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

    FAUX_429 = ("Traceback (most recent call last):\n"
                "  File \"runner.py\", line 429, in run\n"
                "ValueError: boom")

    def test_une_ligne_429_ne_desarme_pas_l48(self):
        # P2 dev1/cx3 (14/10) : un texte qui contient « line 429 » n'est pas un
        # 429 fournisseur — le tour compte comme un échec rapide (L48 armé) et
        # aucun épisode de débit n'est ouvert. Les deux canaux lus pour un débit
        # sont couverts : stderr du fournisseur et résultat libre du modèle.
        for canal, env in (("stderr", {"AMEESH_TEST_STDERR": self.FAUX_429}),
                           ("result", {"AMEESH_TEST_RESULT": self.FAUX_429})):
            with self.subTest(canal=canal):
                _, worker = self._worker("rl8" + canal[0], "claude", {})
                try:
                    self.assertIsNotNone(
                        self._tour(worker, **env, AMEESH_TEST_EXIT="1"))
                    self.assertIsNone(worker.rate_limit)
                    self.assertTrue(worker.fast_failure)
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


class BailTest(_Base):
    """R1 cx3 B1 (#1270) : le bail est renouvelé **avant** son échéance pendant
    une reprise plus longue que lui.

    Dormir jusqu'à la deadline (borne de `wait_timeout`) faisait refuser le
    renouvellement au réveil (`deadline_passed`) : le worker rendait le bail au
    lieu de reprendre. Témoins de service, sans courrier, reprise > bail."""

    TTL = 2.0
    REPRISE = 4.0

    def _worker_court(self, name: str):
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=10.0,
                                  budget_check_interval=0.0, poll=0.05)
        runner = Runner(cfg, self.db, once=False)
        runner.lease_ttl = self.TTL
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, self.TTL)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        self.addCleanup(lambda: (worker.stopping.set(), worker.wake.set(),
                                 worker.join(timeout=5)))
        return worker

    def _espionne(self, worker):
        """Compte les renouvellements et la marge restante à chaque appel."""
        renews, tours = [], []
        vrai_renew = worker.renew

        def espion():
            renews.append(worker.lease_deadline - time.time())
            return vrai_renew()

        worker.renew = espion
        return renews, tours

    def _lance(self, worker):
        """Démarre la boucle de service en isolant les entrées/sorties du tour."""
        return mock.patch.multiple(
            worker,
            pick=mock.Mock(return_value={"kind": "consigne"}),
            rotate_for_lot=mock.Mock(side_effect=lambda s: s),
            maybe_rotate=mock.Mock(),
            apply_restart_if_requested=mock.Mock())

    def _verifie_bail(self, worker, renews, tours, echeance):
        self.assertFalse(worker.lease_lost.is_set(),
                         "le bail a été perdu pendant la reprise")
        self.assertTrue(all(marge > 0 for marge in renews),
                        "renouvellement refusé (échéance atteinte) : %r" % (renews,))
        self.assertGreaterEqual(len(renews), 3,
                                "le bail ne se renouvelle pas pendant l'attente : %r"
                                % (renews,))
        self.assertGreaterEqual(tours[-1], echeance - 0.05,
                                "un tour a été lancé avant l'échéance de reprise")

    def test_reprise_plus_longue_que_le_bail(self):
        # aucun échec : l'épisode est déjà durable au démarrage du service.
        worker = self._worker_court("rl6")
        renews, tours = self._espionne(worker)
        echeance = time.time() + self.REPRISE
        worker.rate_limit = rate_limit.RATE_LIMITED
        worker.rate_limit_streak = 1
        worker.rate_limit_since = time.time()
        worker.rate_limit_retry_at = echeance
        worker.run_turn = lambda spec: (tours.append(time.time()),
                                        worker.stopping.set(), True)[-1]
        with self._lance(worker):
            worker.start()
            self.assertTrue(attendre(lambda: tours, delai=10.0),
                            "le worker doit reprendre après l'échéance, bail tenu")
        self._verifie_bail(worker, renews, tours, echeance)

    def test_attente_apres_echec_renouvelle_le_bail(self):
        # un vrai échec de tour (429) : l'attente post-échec est plus longue
        # que le bail et doit se découper, pas dormir jusqu'à la deadline.
        worker = self._worker_court("rl7")
        renews, tours = self._espionne(worker)
        echeance = time.time() + self.REPRISE

        def tour(_spec):
            tours.append(time.time())
            if len(tours) == 1:
                worker.rate_limit = rate_limit.RATE_LIMITED
                worker.rate_limit_streak = 1
                worker.rate_limit_since = time.time()
                worker.rate_limit_retry_at = echeance
                return False
            worker.stopping.set()
            return True

        worker.run_turn = tour
        with self._lance(worker):
            worker.start()
            self.assertTrue(attendre(lambda: len(tours) >= 2, delai=10.0),
                            "un échec de débit ne doit pas faire rendre le bail")
        self.assertEqual(len(tours), 2, "aucun tour ne doit partir avant l'échéance")
        self._verifie_bail(worker, renews, tours, echeance)


class CadenceTest(unittest.TestCase):
    """Sonde déterministe (horloge simulée) de `renew_wait` et de la cadence.

    Les témoins de service ci-dessus dépendent de l'ordonnancement réel ; ici
    aucune attente réelle : la tranche vaut `ttl/3`, sans plancher dépassant
    l'échéance, et `_attendre_reprise` renouvelle à ce pas (R1 cx3 B1)."""

    def _bail(self, ttl: float, deadline: float):
        return SimpleNamespace(lease_deadline=deadline,
                               runner=SimpleNamespace(lease_ttl=ttl))

    def test_tranche_au_tiers_du_bail(self):
        self.assertAlmostEqual(
            AgentWorker.renew_wait(self._bail(300.0, 0.0), 600.0), 100.0)
        # une échéance lointaine ne raccourcit pas la tranche sous ttl/3
        self.assertAlmostEqual(
            AgentWorker.renew_wait(self._bail(300.0, time.time() + 10_000.0),
                                   600.0), 100.0)

    def test_echeance_proche_sans_plancher(self):
        with mock.patch("ameesh.runner.time.time", return_value=100.0):
            # 5 ms avant l'échéance : la tranche vaut ces 5 ms, pas un plancher
            self.assertAlmostEqual(
                AgentWorker.renew_wait(self._bail(300.0, 100.005), 600.0), 0.005)
            # échéance atteinte : 0 exactement, `renew()` coupe
            self.assertEqual(
                AgentWorker.renew_wait(self._bail(300.0, 99.0), 600.0), 0.0)

    def test_attente_de_reprise_renouvelle_toutes_les_ttl_sur_3(self):
        faux, horloge, renews = self._faux()
        with mock.patch("ameesh.runner.time.monotonic", side_effect=lambda: horloge["t"]), \
                mock.patch("ameesh.runner.time.time", side_effect=lambda: horloge["t"]):
            AgentWorker._attendre_reprise(faux, 900.0)
        self.assertEqual(renews, [100.0 * n for n in range(1, 10)])

    def _faux(self):
        """Un worker factice à horloge simulée : `wake.wait` avance le temps."""
        horloge = {"t": 0.0}
        renews: list[float] = []
        faux = SimpleNamespace()
        faux.stopping = threading.Event()
        faux.runner = SimpleNamespace(stop=threading.Event(), draining=threading.Event(),
                                      gate_holds=lambda: False, lease_ttl=300.0)
        # échéance réelle (pas une constante arbitraire) : `renew_wait` lit
        # `time.time`, donc une échéance déjà passée rendrait la tranche nulle
        # et la boucle tournerait à vide.
        faux.lease_deadline = time.time() + 1e6
        faux.renew_wait = lambda cap: AgentWorker.renew_wait(faux, cap)
        faux.renew = lambda: (renews.append(round(horloge["t"], 6)), True)[-1]
        faux.wake = SimpleNamespace(
            wait=lambda timeout: (horloge.__setitem__("t", horloge["t"] + timeout),
                                  False)[-1],
            clear=lambda: None)
        return faux, horloge, renews

    def test_attente_apres_echec_honore_la_duree_entiere(self):
        # 250 s > ttl/3 = 100 s : l'attente se découpe (100, 100, 50) et va
        # jusqu'au bout — pas de troncature à une tranche (L48).
        faux, horloge, renews = self._faux()
        with mock.patch("ameesh.runner.time.monotonic", side_effect=lambda: horloge["t"]):
            AgentWorker._attendre_apres_echec(faux, 250.0)
        self.assertEqual(horloge["t"], 250.0)
        self.assertEqual(renews, [100.0, 200.0])

    def test_attente_courte_apres_echec_en_un_seul_wait(self):
        # sous le pas de renouvellement : un seul wait, comme avant.
        faux, horloge, renews = self._faux()
        with mock.patch("ameesh.runner.time.monotonic", side_effect=lambda: horloge["t"]), \
                mock.patch("ameesh.runner.time.time", side_effect=lambda: horloge["t"]):
            AgentWorker._attendre_apres_echec(faux, 30.0)
        self.assertEqual(horloge["t"], 30.0)
        self.assertEqual(renews, [])


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
