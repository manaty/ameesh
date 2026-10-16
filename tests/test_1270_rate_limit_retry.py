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

from ameesh import adapters, db as db_mod, exploitation, rate_limit, registry
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase
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
                      "DeepSeek Messages SSE event type mismatch",
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

    def test_pannes_passageres_5xx_et_deepseek(self):
        # bureau 15/10 : la reprise couvre aussi les 5xx explicites et les
        # erreurs passagères DeepSeek (`dsh: <CODE>: …` sur stderr), pas
        # seulement le 429 Codex — sur les canaux du fournisseur seulement.
        for texte in ("dsh: MALFORMED_RESPONSE: DeepSeek Messages SSE event type mismatch",
                      "dsh: SERVER: DeepSeek Messages request failed (503)",
                      "dsh: RATE_LIMIT: DeepSeek Messages request failed (429)",
                      "dsh: STREAM_CLOSED: DeepSeek Messages stream ended before message_stop",
                      "exceeded retry limit, last status: 503 Service Unavailable",
                      "unexpected status 502 Bad Gateway: upstream connect error",
                      'API Error: 500 {"type":"error","error":{"type":"api_error"}}',
                      "HTTP/1.1 504 Gateway Timeout",
                      "500 Internal Server Error",
                      "stream disconnected before completion: error decoding response body"):
            with self.subTest(texte=texte):
                self.assertEqual(rate_limit.classify(error=texte), rate_limit.RATE_LIMITED)
                self.assertEqual(rate_limit.classify(stderr_tail="bruit\n" + texte),
                                 rate_limit.RATE_LIMITED)
                self.assertIsNone(rate_limit.classify(result=texte))

    def test_5xx_et_codes_dsh_sans_signature_ne_classent_pas(self):
        for texte in ('  File "request.py", line 503, in send',
                      "(/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js:500:15)",
                      "listening on port 5003",
                      "HTTP 501 Not Implemented",
                      "assert response.status_code == 500",
                      "MALFORMED_RESPONSE dans mes notes",
                      "dsh: AUTH: invalid credential",
                      "dsh: CONTEXT_WINDOW_EXCEEDED: prompt too long",
                      # transport MCP (SDK) et serveurs tiers : ni débit ni
                      # panne passagère du fournisseur (R1 dev2 du 15/10)
                      "SSE error: Non-200 status code (401)",
                      'MCP server "figma": SSE connection closed',
                      "remote: Internal Server Error",
                      "MCP server x: Service Unavailable"):
            with self.subTest(texte=texte):
                self.assertIsNone(rate_limit.classify(error=texte))
                self.assertIsNone(rate_limit.classify(stderr_tail=texte))
        # le solde DeepSeek épuisé (402) est un quota, pas une panne passagère
        self.assertEqual(rate_limit.classify(stderr_tail="dsh: QUOTA: Insufficient Balance"),
                         rate_limit.QUOTA)


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

    def test_quota_dans_un_result_marque_is_error(self):
        # #1397 (NO-GO dev1) : Claude ne dit un quota que dans le `result`
        # marqué `is_error` — ce texte est un canal du CLI, donc classé quota.
        # Le sort de L48 pour un quota est la décision de #1270 (R1 dev2 :
        # quota = cadence lente mais L48 reste armé) ; ce témoin ne fixe que
        # le classement, commun aux deux têtes.
        _, worker = self._worker("rlq", "claude", {})
        try:
            self.assertIsNotNone(self._tour(
                worker, AMEESH_TEST_RESULT="You've hit your usage limit",
                AMEESH_TEST_IS_ERROR="1", AMEESH_TEST_EXIT="1"))
            ligne = registry.get(self.db, "rlq")
            etat = rate_limit.state(ligne["status_text"])
            self.assertEqual(etat["kind"], rate_limit.QUOTA)
            self.assertEqual(worker.rate_limit, rate_limit.QUOTA)
        finally:
            worker.watchdog_stop.set()

    def test_result_libre_non_marque_ne_classe_pas_un_quota(self):
        # #1397 : sans `is_error`, le même texte libre ne conclut rien — le
        # tour reste un échec rapide ordinaire (L48 armé).
        _, worker = self._worker("rlq2", "claude", {})
        try:
            self.assertIsNotNone(self._tour(
                worker, AMEESH_TEST_RESULT="not near the usage limit",
                AMEESH_TEST_EXIT="1"))
            self.assertIsNone(worker.rate_limit)
            self.assertTrue(worker.fast_failure)
            self.assertIsNone(rate_limit.state(
                registry.get(self.db, "rlq2")["status_text"]))
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

    def test_tour_sans_consigne_repris_a_l_echeance(self):
        # relecture bureau : un tour de courrier ou d'événements en échec ne
        # laisse aucune consigne en attente (courrier soldé au lancement) ; à
        # l'échéance, `pick` rend un tour de reprise au lieu de rien.
        _, worker = self._worker("rl10", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.DEBIT))
            registry.set_pending_prompt(self.db, "rl10", None)  # rien de remis
            worker.rate_limit_retry_at = time.time() - 1
            with mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                              "AMEESH_TEST_LOG": self.turns_log}):
                spec = worker.pick()
            self.assertIsNotNone(spec)
            self.assertEqual((spec["kind"], spec["prompt"]),
                             ("idle", adapters.RETRY_PROMPT))
        finally:
            worker.watchdog_stop.set()

    QUOTA = {"AMEESH_TEST_STDERR": "Error: insufficient_quota: you exceeded your "
                                   "current quota\n",
             "AMEESH_TEST_EXIT": "1"}

    def test_quota_reste_compte_par_l48(self):
        # relecture bureau : un quota épuisé suit la cadence mais reste un échec
        # rapide — L48 l'arrête au bout de cinq refus, pas de reprise sans fin.
        _, worker = self._worker("rl11", "claude", {})
        try:
            self.assertIsNotNone(self._tour(worker, **self.QUOTA))
            self.assertEqual(worker.rate_limit, rate_limit.QUOTA)
            self.assertTrue(worker.fast_failure)
            self.assertAlmostEqual(worker.failure_wait(), 60.0, delta=2.0)
            worker.rate_limit_retry_at = time.time() - 1
            self.assertFalse(worker.retry_due())
        finally:
            worker.watchdog_stop.set()


class TourDeRepriseTest(unittest.TestCase):
    """Relecture bureau (#1270) : sans base. Un tour sans consigne à remettre
    en attente (courrier, événements, reprise) repart à l'échéance par un tour
    de reprise ; sinon l'agent restait `blocked` jusqu'au courrier suivant.
    Worker factice : les gardes de `_pick` (base, budget, hôte, compte) sont
    neutralisées, rien n'est en attente."""

    def _worker(self, genre: str, echeance: float):
        w = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(w, name="rlx", daemon=True)  # jamais démarré
        w.db, w.epoch = object(), 1
        w.runner = SimpleNamespace(runner_id="r", idle_nudge=1200.0,
                                   gate_holds=lambda: False, host_pressure=lambda: {})
        w.rate_limit, w.rate_limit_streak = genre, 1
        w.rate_limit_since, w.rate_limit_retry_at = time.time() - 60, echeance
        w.nudged, w.last_activity = True, time.monotonic()
        w._session_en_attente, w.suite, w._reprise_attach = None, None, False
        return w

    def _pick(self, w):
        oui, non = mock.Mock(return_value=True), mock.Mock(return_value=False)
        with mock.patch.multiple(w, _marque_base=non, maybe_relocate=non, budget_ok=oui,
                                 _held_by_pressure=non, _host_pressure_release=mock.Mock(),
                                 host_ready=oui, workdir_ready=oui, auth_ready=oui,
                                 _lot_wake_spec=mock.Mock(return_value=None)), \
                mock.patch("ameesh.runner.registry.get", return_value={"status": "blocked"}), \
                mock.patch("ameesh.runner.registry.take_pending_prompt", return_value=None), \
                mock.patch("ameesh.runner.mail.unread_urgent", return_value=[]), \
                mock.patch("ameesh.runner.mail.unread", return_value=[]), \
                mock.patch("ameesh.runner.account_turn.continuity", return_value=True):
            return w._pick()

    def test_echeance_passee_lance_un_tour_de_reprise(self):
        w = self._worker(rate_limit.RATE_LIMITED, time.time() - 1)
        self.assertTrue(w.retry_due())
        spec = self._pick(w)
        self.assertEqual((spec["kind"], spec["prompt"]), ("idle", adapters.RETRY_PROMPT))

    def test_avant_l_echeance_rien(self):
        w = self._worker(rate_limit.RATE_LIMITED, time.time() + 60)
        self.assertFalse(w.retry_due())
        self.assertIsNone(self._pick(w))

    def test_un_quota_n_est_pas_repris_sans_fin(self):
        w = self._worker(rate_limit.QUOTA, time.time() - 1)
        self.assertFalse(w.retry_due())
        self.assertIsNone(self._pick(w))


class BailTest(_Base):
    """R1 cx3 B1 (#1270) : le bail est renouvelé **avant** son échéance pendant
    une reprise plus longue que lui.

    Dormir jusqu'à la deadline (borne de `wait_timeout`) faisait refuser le
    renouvellement au réveil (`deadline_passed`) : le worker rendait le bail au
    lieu de reprendre. Témoins de service, sans courrier, reprise > bail.

    #2139 : la boucle est la vraie (`AgentWorker.run`), mais l'horloge est
    virtuelle et le renouvellement un oracle en mémoire. Le témoin en temps
    réel qui vivait ici dépendait de la latence du banc : sous charge, un
    aller-retour psql de 1,2 s (mesuré) consommait la marge du bail et
    `_renew_call` concluait « renewal unanswered past the lease » — le worker
    s'arrêtait sans qu'aucun défaut du produit ne soit en cause. L'assertion de
    renouvellement est conservée telle quelle : marge strictement positive à
    chaque appel, au moins trois renouvellements, aucun tour avant l'échéance."""

    TTL = 2.0
    REPRISE = 4.0

    def _worker_court(self, name: str, ttl: float | None = None):
        ttl = self.TTL if ttl is None else ttl
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=10.0,
                                  budget_check_interval=0.0, poll=0.05)
        runner = Runner(cfg, self.db, once=False)
        runner.lease_ttl = ttl
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, ttl)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _horloge(self, worker, *, bail_restant: float | None = None,
                 oracle: bool = True):
        """Horloge virtuelle et oracle de bail, branchés sur le worker.

        `renew` enregistre la marge (`lease_deadline - maintenant`) et refuse,
        comme le vrai, dès qu'elle n'est pas strictement positive ; chaque
        renouvellement accepté repousse l'échéance d'un bail. `wake.wait`
        avance l'horloge du délai demandé : aucune attente réelle, aucune
        latence de base — le témoin ne dépend plus de l'ordonnancement.

        `oracle=False` garde le **vrai** `renew()` (et son `_renew_call`,
        bouchonné par le témoin de panne) : l'horloge et le réveil restent
        virtuels."""
        horloge = {"t": 1000.0}
        renews: list[float] = []

        def renew():
            marge = worker.lease_deadline - horloge["t"]
            renews.append(marge)
            if marge <= 0:
                worker.lease_lost.set()
                return False
            worker.lease_deadline = horloge["t"] + worker.runner.lease_ttl
            return True

        if oracle:
            worker.renew = renew
        worker.lease_deadline = horloge["t"] + (
            worker.runner.lease_ttl if bail_restant is None else bail_restant)
        worker.wake = SimpleNamespace(
            wait=lambda timeout: (horloge.__setitem__(
                "t", horloge["t"] + max(0.0, float(timeout))), False)[-1],
            clear=lambda: None,
            set=lambda: None)
        return horloge, renews

    def _lance(self, worker, *, pick=None, avant_reprise=None, renew_call=None):
        """La boucle de service réelle, ses entrées/sorties isolées.

        Le veilleur est neutralisé : il lit l'horloge réelle, pas l'horloge
        virtuelle du témoin (#2139). `avant_reprise` est joué par
        `maybe_rotate`, entre le renouvellement de boucle et la lecture de
        l'échéance de reprise. `renew_call` remplace l'appel de base (panne
        simulée) ; `renew()` reste le vrai."""
        patches = dict(
            ensure_watchdog=mock.Mock(),
            placed_elsewhere=mock.Mock(return_value=""),
            pick=pick or mock.Mock(return_value={"kind": "consigne"}),
            rotate_for_lot=mock.Mock(side_effect=lambda s: s),
            maybe_rotate=(mock.Mock(side_effect=avant_reprise)
                          if avant_reprise else mock.Mock()),
            apply_restart_if_requested=mock.Mock())
        if renew_call is not None:
            patches["_renew_call"] = mock.Mock(side_effect=renew_call)
        return mock.patch.multiple(worker, **patches)

    def _joue(self, worker, horloge, *, pick=None, avant_reprise=None,
              renew_call=None) -> None:
        """Exécute la boucle réelle (`run`) sur l'horloge virtuelle, sans attente."""
        with self._lance(worker, pick=pick, avant_reprise=avant_reprise,
                         renew_call=renew_call), \
                mock.patch("ameesh.runner.time.time",
                           side_effect=lambda: horloge["t"]), \
                mock.patch("ameesh.runner.time.monotonic",
                           side_effect=lambda: horloge["t"]):
            worker.run()

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
        horloge, renews = self._horloge(worker)
        tours: list[float] = []
        echeance = horloge["t"] + self.REPRISE
        worker.rate_limit = rate_limit.RATE_LIMITED
        worker.rate_limit_streak = 1
        worker.rate_limit_since = horloge["t"]
        worker.rate_limit_retry_at = echeance
        worker.run_turn = lambda spec: (tours.append(horloge["t"]),
                                        worker.stopping.set(), True)[-1]
        self._joue(worker, horloge)
        self.assertEqual(len(tours), 1,
                         "le worker doit reprendre après l'échéance, bail tenu "
                         "(renouvellements : %r)" % (renews,))
        self._verifie_bail(worker, renews, tours, echeance)

    def test_attente_apres_echec_renouvelle_le_bail(self):
        # un vrai échec de tour (429) : l'attente post-échec est plus longue
        # que le bail et doit se découper, pas dormir jusqu'à la deadline.
        worker = self._worker_court("rl7")
        horloge, renews = self._horloge(worker)
        tours: list[float] = []
        echeance = horloge["t"] + self.REPRISE

        def tour(_spec):
            tours.append(horloge["t"])
            if len(tours) == 1:
                worker.rate_limit = rate_limit.RATE_LIMITED
                worker.rate_limit_streak = 1
                worker.rate_limit_since = horloge["t"]
                worker.rate_limit_retry_at = echeance
                return False
            worker.stopping.set()
            return True

        worker.run_turn = tour
        self._joue(worker, horloge)
        self.assertEqual(len(tours), 2,
                         "un échec puis la reprise, aucun tour avant l'échéance "
                         "(renouvellements : %r)" % (renews,))
        self._verifie_bail(worker, renews, tours, echeance)

    def test_bail_proche_de_l_echeance_renouvele_sans_attendre(self):
        """#2139 : à moins d'une tranche de l'échéance, l'attente ne planifie
        pas un réveil À l'échéance — elle renouvelle immédiatement.

        Cas réel : un tour long ou un aller-retour de base lent laisse le bail
        presque échu quand la boucle le relit. Borner la tranche au temps
        restant faisait tomber le réveil sur l'échéance même, et le
        renouvellement était refusé (marge nulle) : le worker rendait un bail
        encore valide."""
        worker = self._worker_court("rl8")
        horloge, renews = self._horloge(worker)
        tours: list[float] = []
        echeance = horloge["t"] + self.REPRISE
        reduit: list[bool] = []

        def bail_presque_echu():
            # le bail est presque échu quand la boucle le relit, après le
            # renouvellement de boucle et avant l'attente de reprise.
            if not reduit:
                reduit.append(True)
                worker.lease_deadline = horloge["t"] + 0.1

        worker.rate_limit = rate_limit.RATE_LIMITED
        worker.rate_limit_streak = 1
        worker.rate_limit_since = horloge["t"]
        worker.rate_limit_retry_at = echeance
        worker.run_turn = lambda spec: (tours.append(horloge["t"]),
                                        worker.stopping.set(), True)[-1]
        self._joue(worker, horloge, avant_reprise=bail_presque_echu)
        self.assertEqual(len(tours), 1,
                         "le worker doit reprendre après l'échéance, bail tenu "
                         "(renouvellements : %r)" % (renews,))
        self._verifie_bail(worker, renews, tours, echeance)

    def test_panne_de_base_espace_les_essais_et_reprend(self):
        """#2139 : sur une panne de base, `renew()` rend True **sans prolonger**
        le bail. Les essais doivent être espacés (délai court L72) au lieu de
        boucler à vide, et la cadence nominale reprend au retour de la base.

        Le vrai `renew()` est exercé (`_renew_call` seul est bouchonné) : la
        boucle `run()` réelle, sur l'horloge virtuelle. TTL 300 s, bail à 50 s
        de l'échéance — le cas de la sonde mesh-design (#2139)."""
        worker = self._worker_court("rl9", ttl=300.0)
        horloge, _ = self._horloge(worker, bail_restant=50.0, oracle=False)
        tours: list[float] = []
        appels: list[float] = []
        echeance = horloge["t"] + 400.0
        limite = 12
        pannes = 3
        worker.rate_limit = rate_limit.RATE_LIMITED
        worker.rate_limit_streak = 1
        worker.rate_limit_since = horloge["t"]
        worker.rate_limit_retry_at = echeance
        worker.run_turn = lambda spec: (tours.append(horloge["t"]),
                                        worker.stopping.set(), True)[-1]

        def renew_call():
            appels.append(horloge["t"])
            if len(appels) > limite:  # garde-fou : l'ancien code bouclait à vide
                worker.stopping.set()
                return True, horloge["t"] + worker.runner.lease_ttl, None
            if len(appels) <= pannes:
                return True, None, db_mod.Unavailable("base injoignable (témoin)")
            return True, horloge["t"] + worker.runner.lease_ttl, None

        self._joue(worker, horloge, renew_call=renew_call)
        self.assertLessEqual(len(appels), limite,
                             "les essais de renouvellement bouclent à vide : %r"
                             % (appels,))
        # panne : essais espacés du délai court, jamais nuls
        self.assertEqual([round(b - a, 3) for a, b in zip(appels[:3], appels[1:4])],
                         [2.0, 2.0, 2.0],
                         "les essais en panne ne sont pas espacés : %r" % (appels,))
        # retour de la base : la cadence nominale (ttl/3) reprend
        self.assertEqual(round(appels[4] - appels[3], 3), 100.0,
                         "la cadence nominale ne reprend pas : %r" % (appels,))
        self.assertFalse(worker.lease_lost.is_set(),
                         "le bail a été perdu pendant la panne")
        self.assertEqual(worker.renew_failures, 0)
        self.assertEqual(len(tours), 1)
        self.assertGreaterEqual(tours[-1], echeance - 0.05,
                                "le tour ne part pas à l'échéance de reprise")

    def test_panne_de_base_permanente_s_arrete_a_l_echeance(self):
        """#2139 : panne permanente — les essais restent espacés et s'arrêtent à
        l'échéance du bail, sans relèvement ni plancher après la deadline."""
        worker = self._worker_court("rl10", ttl=300.0)
        horloge, _ = self._horloge(worker, bail_restant=8.0, oracle=False)
        tours: list[float] = []
        appels: list[float] = []
        limite = 12
        deadline = worker.lease_deadline
        echeance = deadline + 400.0
        worker.rate_limit = rate_limit.RATE_LIMITED
        worker.rate_limit_streak = 1
        worker.rate_limit_since = horloge["t"]
        worker.rate_limit_retry_at = echeance
        worker.run_turn = lambda spec: (tours.append(horloge["t"]),
                                        worker.stopping.set(), True)[-1]

        def renew_call():
            appels.append(horloge["t"])
            if len(appels) > limite:  # garde-fou : l'ancien code bouclait à vide
                worker.stopping.set()
                return True, horloge["t"] + worker.runner.lease_ttl, None
            return True, None, db_mod.Unavailable("base injoignable (témoin)")

        self._joue(worker, horloge, renew_call=renew_call)
        self.assertLessEqual(len(appels), 6,
                             "les essais ne s'arrêtent pas à l'échéance : %r"
                             % (appels,))
        self.assertLessEqual(max(appels), deadline,
                             "un essai a lieu après l'échéance du bail : %r"
                             % (appels,))
        self.assertTrue(worker.lease_lost.is_set(),
                        "l'échéance du bail n'a pas arrêté le worker")
        self.assertEqual(tours, [], "aucun tour ne doit partir sur un bail perdu")


class CadenceTest(unittest.TestCase):
    """Sonde déterministe (horloge simulée) de `renew_wait` et de la cadence.

    Les témoins de la boucle ci-dessus sont déterministes eux aussi (#2139) ;
    ici, encore plus bas : la tranche vaut `ttl/3`, réserve une tranche pleine
    avant l'échéance, et `_attendre_reprise` renouvelle à ce pas (R1 cx3 B1)."""

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

    def test_echeance_proche_renouvellement_immediat(self):
        with mock.patch("ameesh.runner.time.time", return_value=100.0):
            # à 150 s de l'échéance (moins de deux tranches), la tranche est
            # bornée pour laisser une tranche pleine de marge au renouvellement
            self.assertAlmostEqual(
                AgentWorker.renew_wait(self._bail(300.0, 250.0), 600.0), 50.0)
            # à 5 ms de l'échéance, il n'y a plus de marge : tranche nulle,
            # l'appelant renouvelle immédiatement (#2139) au lieu de dormir
            # jusqu'à la deadline, où `renew()` serait refusé
            self.assertEqual(
                AgentWorker.renew_wait(self._bail(300.0, 100.005), 600.0), 0.0)
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

    def test_panne_de_base_ne_boucle_pas_sur_un_bail_presque_echu(self):
        for boucle in ("_attendre_reprise", "_attendre_apres_echec"):
            for restant in (1.0, 10.0):
                with self.subTest(boucle=boucle, restant=restant):
                    faux, horloge, _ = self._faux()
                    faux.name = "cadence"
                    faux.lease_deadline = restant
                    faux.renew_failures = 0
                    faux.lease_lost = threading.Event()
                    faux.stop_group_now = mock.Mock()
                    faux.deadline_passed = lambda: AgentWorker.deadline_passed(faux)
                    faux.wait_timeout = lambda cap: AgentWorker.wait_timeout(faux, cap)
                    faux.renew = lambda: AgentWorker.renew(faux)
                    appels = []

                    def indisponible():
                        appels.append(horloge["t"])
                        horloge["t"] += 0.001
                        return True, None, db_mod.Unavailable("connection refused")

                    faux._renew_call = indisponible
                    with mock.patch("ameesh.runner.time.time",
                                    side_effect=lambda: horloge["t"]), \
                            mock.patch("ameesh.runner.time.monotonic",
                                       side_effect=lambda: horloge["t"]), \
                            mock.patch("ameesh.runner.log_async"):
                        getattr(AgentWorker, boucle)(faux, restant + 1.0)
                    self.assertEqual(appels[0], 0.0, "premier essai immédiat")
                    self.assertLessEqual(len(appels), 5,
                                         "une panne ne doit pas boucler sans attente")
                    self.assertTrue(faux.lease_lost.is_set())
                    self.assertEqual(horloge["t"], restant,
                                     "aucune attente au-delà du bail")


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
