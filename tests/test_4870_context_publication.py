# SPDX-License-Identifier: AGPL-3.0-only
"""#4870 (CTX-02b) — publier les mesures de contexte à côté du legacy.

Le contrat ``ameesh-context/1`` (docs/design/ctx-01-compteurs-contexte.md §2.2,
§5.3, §7.3) normalise les compteurs ; ce lot les **publie** pour chaque tour,
étiquetés, à côté des valeurs legacy qui continuent de décider (seuil 150 000,
plafond de contexte). Aucune décision n'est changée : les tests vérifient
l'invariance de la ligne ``turn_costs`` et du contexte legacy, l'absence de
prompt / résultat d'outil / secret dans la publication, et la qualité des
mesures par harnais (cache écrit exclu de ``R_turn``, Codex sans double
comptage, absence en ``null``).

La publication est l'événement ``turn.context`` du journal partagé
(``ameesh_events``) : une ligne par tour compté, écrite après sa ligne
``turn_costs``. Les tests d'intégration utilisent les flux synthétiques du
banc (``tests/fakebin``), jamais un harnais réel.
"""
from __future__ import annotations

import copy
import json
import os
import unittest
from unittest import mock

from ameesh import adapters, context_metrics, cost, events, mechanics
from ameesh.runner import AgentWorker

from .support import PgTestCase

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "context")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


class Reader:
    def __init__(self, harness: str, reader):
        self.harness = harness
        self.reader = reader

    def parse(self, line: str) -> dict:
        return self.reader.parse(line)


CLAUDE = Reader("claude", adapters.ClaudeStream())
CODEX = Reader("codex", adapters.CodexStream())
DEEPSEEK = Reader("deepseek", adapters.DshStream())
ACP = Reader("acp", adapters.AcpStream())


def observe_all(reader: Reader, name: str, **kwargs) -> dict:
    normalizer = context_metrics.TurnNormalizer(reader.harness, **kwargs)
    with open(fixture(name), encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                normalizer.observe(reader.parse(line))
    return normalizer.finish()


def legacy_block(**overrides) -> dict:
    """Les onze champs legacy, tous présents (None = mesure absente)."""
    values = {name: None for name in cost.CONTEXT_LEGACY_FIELDS}
    values.update(overrides)
    return values


def publication(document: dict, **overrides) -> dict:
    values = {
        "legacy": legacy_block(input_tokens=1, cached_input_tokens=2,
                               output_tokens=3, reread_tokens=3,
                               session_turns=1),
        "decision": {"policy": {"contract": context_metrics.SCHEMA,
                                "session": "par-lot"},
                     "close": None, "rotation": None},
        "agent": "agent-contexte",
        "host": "ameesh-agents-1",
    }
    values.update(overrides)
    return cost.context_publication(document, **values)


class BuilderTest(unittest.TestCase):
    """La charge publiée elle-même, sans base ni exécuteur."""

    def test_document_clos_et_compteurs_legacy_publication(self):
        document = observe_all(DEEPSEEK, "deepseek_stream.jsonl",
                               session="ctx-dsh-1", session_reread=0)
        payload = publication(document)
        encoded = json.dumps(payload)
        self.assertIn("ameesh-context/1", encoded)
        self.assertEqual(payload["schema"], context_metrics.SCHEMA)
        self.assertEqual(payload["context"], document)
        self.assertEqual(payload["agent"], "agent-contexte")
        self.assertEqual(payload["host"], "ameesh-agents-1")
        self.assertIsNone(payload["observation_id"])
        self.assertEqual(set(payload["legacy"]), set(cost.CONTEXT_LEGACY_FIELDS))
        self.assertEqual(payload["legacy"]["input_tokens"], 1)
        self.assertEqual(payload["policy"]["session"], "par-lot")
        self.assertIsNone(payload["decision"]["close"])
        # le document normalisé reste le seul porteur des mesures étiquetées
        self.assertEqual(payload["context"]["measures"]["C_last"]["value"], 22500)
        self.assertEqual(payload["context"]["measures"]["R_turn"]["value"], 43500)

    def test_le_document_n_est_pas_modifie(self):
        document = observe_all(CLAUDE, "claude_stream.jsonl",
                               session="ctx-claude-1", session_reread=0)
        before = copy.deepcopy(document)
        publication(document)
        self.assertEqual(document, before)

    def test_refuse_un_document_non_clos(self):
        document = observe_all(DEEPSEEK, "deepseek_stream.jsonl",
                               session="ctx-dsh-1", session_reread=0)
        interrupted = dict(document, finished=False)
        with self.assertRaises(cost.CostError):
            publication(interrupted)
        with self.assertRaises(cost.CostError):
            publication(dict(document, schema="ameesh-context/2"))

    def test_refuse_un_compteur_legacy_manquant(self):
        document = observe_all(DEEPSEEK, "deepseek_stream.jsonl",
                               session="ctx-dsh-1", session_reread=0)
        legacy = legacy_block(input_tokens=1)
        legacy.pop("session_max_tokens")
        with self.assertRaises(cost.CostError):
            publication(document, legacy=legacy)

    def test_refuse_une_politique_absente(self):
        document = observe_all(DEEPSEEK, "deepseek_stream.jsonl",
                               session="ctx-dsh-1", session_reread=0)
        with self.assertRaises(cost.CostError):
            publication(document, decision={"close": None})

    def test_aucun_prompt_outil_ni_secret(self):
        # Le flux de test porte des textes libres ; la publication ne retient
        # que les dictionnaires d'usage.
        document = observe_all(CLAUDE, "claude_stream.jsonl",
                               session="ctx-claude-1", session_reread=0)
        encoded = json.dumps(publication(document))
        self.assertNotIn('"display"', encoded)
        self.assertNotIn("toolu-", encoded)
        self.assertNotIn("tool_result", encoded)
        self.assertNotIn('"text"', encoded)

    def test_publication_reprend_la_qualite_publiee_du_modele(self):
        document = observe_all(DEEPSEEK, "deepseek_stream.jsonl",
                               session="ctx-dsh-1", session_reread=0)
        self.assertEqual(document["model"], "deepseek-flash")
        self.assertEqual(document["model_quality"], context_metrics.MEASURED)
        payload = publication(document)
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["context"]["model_quality"],
                         context_metrics.MEASURED)

    def test_nom_demande_sans_annonce_reste_requested(self):
        document = context_metrics.TurnNormalizer(
            "deepseek", session="ctx-dsh-1", model="alias-demande").finish()
        self.assertEqual(document["model_quality"], context_metrics.REQUESTED)
        payload = publication(document)
        self.assertEqual(payload["model"], "alias-demande")
        self.assertEqual(payload["context"]["model_quality"],
                         context_metrics.REQUESTED)

    def test_document_anterieur_sans_qualite_reste_publicable(self):
        # Les événements déjà écrits n'ont pas `model_quality` : la
        # publication reste compatible et ne suppose aucune mesure.
        document = observe_all(DEEPSEEK, "deepseek_stream.jsonl",
                               session="ctx-dsh-1", session_reread=0)
        document.pop("model_quality")
        payload = publication(document)
        self.assertIsNone(payload["context"].get("model_quality"))

    def test_document_et_legacy_serialisables(self):
        for reader, name in ((CLAUDE, "claude_stream.jsonl"),
                             (CODEX, "codex_exec.jsonl"),
                             (DEEPSEEK, "deepseek_stream.jsonl"),
                             (ACP, "acp_stream.jsonl")):
            with self.subTest(harness=reader.harness):
                document = observe_all(reader, name, session="s", session_reread=0)
                json.dumps(publication(document))


class PublicationRobustnessTest(unittest.TestCase):
    """La publication ne retient jamais un tour et ne republie pas un trou."""

    def test_sans_observation_rien_n_est_ecrit(self):
        worker = _context_worker()
        worker._context_observation = None
        with mock.patch("ameesh.runner.events_mod.record") as record:
            AgentWorker._publier_contexte(worker, mock.Mock(),
                                          {"session": "s", "turn": "consigne"})
        record.assert_not_called()
        self.assertIsNone(worker._context_session_reread)

    def test_un_echec_d_ecriture_est_avale(self):
        document = observe_all(DEEPSEEK, "deepseek_stream.jsonl",
                               session="ctx-dsh-1", session_reread=0)
        worker = mock.Mock()
        worker._context_observation = document
        worker._turn_close_observation = None
        worker._last_rotation_observation = None
        worker.name = "agent-contexte"
        worker.runner.host = "ameesh-agents-1"
        worker.agent = {"team": "ameesh"}
        worker.session_turns = 0
        worker.session_tokens = 0.0
        worker.last_turn_context = None
        worker.last_turn_subagent_reread = 0
        worker.context_max_tokens.return_value = 1
        worker.session_max_tokens.return_value = 1.0
        worker.session_policy.return_value = "par-lot"
        with mock.patch.object(cost, "context_publication",
                               wraps=cost.context_publication), \
                mock.patch("ameesh.runner.events_mod.record",
                           side_effect=RuntimeError("base absente")), \
                mock.patch("ameesh.runner.log_async"):
            AgentWorker._publier_contexte(worker, cost.TurnUsage(
                "agent-contexte", "deepseek", "deepseek-flash", 0.0,
                input_tokens=1, cached_input_tokens=2, output_tokens=3,
                session="ctx-dsh-1"), {"turn": "consigne", "turn_id": "t1",
                                       "created_ts": 1.0})
        self.assertIsNone(worker._context_observation)


def _context_worker() -> AgentWorker:
    """Un worker minimal pour la machine d'état de la base R_session."""
    worker = object.__new__(AgentWorker)
    # AgentWorker hérite de Thread : les méthodes exercées ici ne lisent que
    # `name`, pas l'état du fil (jamais démarré).
    worker._initialized = True
    worker._name = "agent-contexte"
    worker.db = None
    worker._context_session_id = None
    worker._context_session_reread = None
    worker._context_session_quality = context_metrics.UNKNOWN
    worker._context_session_reasons = ()
    worker._context_baseline = None
    worker._context_baseline_quality = context_metrics.UNKNOWN
    worker._context_baseline_reasons = ()
    worker._context_session_lost = None
    return worker


def _turn_document(session: str, value, quality: str, reasons=()) -> dict:
    return {"session": session, "reasons": list(reasons),
            "measures": {"R_turn": {"value": value, "quality": quality,
                                    "reasons": list(reasons)}}}


class SessionBaselineQualityTest(unittest.TestCase):
    """La qualité d'une base partielle ne se perd pas dans le normalisateur."""

    def test_base_partielle_reste_partielle_apres_un_tour_complet(self):
        normalizer = context_metrics.TurnNormalizer(
            "deepseek", session="s", session_reread=100,
            session_reread_quality=context_metrics.PARTIAL,
            session_reread_reasons=("input_unknown",))
        normalizer.call({"inputTokens": 20, "cacheReadTokens": 10,
                         "outputTokens": 1}, "c1")
        measure = normalizer.finish()["measures"]["R_session"]
        self.assertEqual(measure["value"], 130)
        self.assertEqual(measure["quality"], context_metrics.PARTIAL)
        self.assertIn("input_unknown", measure["reasons"])

    def test_base_inconnue_garde_ses_raisons(self):
        normalizer = context_metrics.TurnNormalizer(
            "deepseek", session="s", session_reread=None,
            session_reread_reasons=(context_metrics.REASON_COLLECTION_LOST,))
        normalizer.call({"inputTokens": 20, "cacheReadTokens": 10,
                         "outputTokens": 1}, "c1")
        measure = normalizer.finish()["measures"]["R_session"]
        self.assertIsNone(measure["value"])
        self.assertEqual(measure["quality"], context_metrics.UNKNOWN)
        self.assertIn(context_metrics.REASON_SESSION_BASELINE_UNKNOWN,
                      measure["reasons"])
        self.assertIn(context_metrics.REASON_COLLECTION_LOST, measure["reasons"])

    def test_qualite_de_base_invalide_refusee(self):
        with self.assertRaises(context_metrics.ContextMetricsError):
            context_metrics.TurnNormalizer(
                "deepseek", session="s", session_reread=1,
                session_reread_quality="presque")


class ContextSessionStateTest(unittest.TestCase):
    """La base du worker : partiel→complet, absent→complet, collecte perdue."""

    def setUp(self):
        self.worker = _context_worker()
        patcher = mock.patch("ameesh.runner.cost_mod.session_reread",
                             return_value={"tokens": 100, "turns": 1,
                                           "last_ts": 1.0})
        self.addCleanup(patcher.stop)
        self.session_reread = patcher.start()
        proof = mock.patch.object(
            AgentWorker, "_context_resume_state",
            return_value=(100, context_metrics.MEASURED, ()))
        self.addCleanup(proof.stop)
        proof.start()

    def test_partiel_puis_complet_reste_partiel(self):
        self.assertEqual(self.worker._context_session_baseline("s"), 100)
        self.worker._remember_context_session(
            _turn_document("s", 20, context_metrics.PARTIAL, ("input_unknown",)),
            "s")
        self.assertEqual(self.worker._context_session_reread, 120)
        self.assertEqual(self.worker._context_session_quality,
                         context_metrics.PARTIAL)
        # tour 2 complet : la base partielle le reste, avec sa raison
        self.assertEqual(self.worker._context_session_baseline("s"), 120)
        self.worker._remember_context_session(
            _turn_document("s", 30, context_metrics.MEASURED), "s")
        self.assertEqual(self.worker._context_session_reread, 150)
        self.assertEqual(self.worker._context_session_quality,
                         context_metrics.PARTIAL)
        self.assertIn("input_unknown", self.worker._context_session_reasons)

    def test_normalisateur_r_session_apres_partiel_puis_complet(self):
        # Sonde B1 : base 100, tour 1 partiel (20), tour 2 complet (30).
        self.assertEqual(self.worker._context_session_baseline("s"), 100)
        first = context_metrics.TurnNormalizer(
            "deepseek", session="s",
            session_reread=self.worker._context_baseline,
            session_reread_quality=self.worker._context_baseline_quality,
            session_reread_reasons=self.worker._context_baseline_reasons)
        first.call({"inputTokens": 20, "outputTokens": 1}, "c1")
        document = first.finish()
        self.assertEqual(document["measures"]["R_session"]["value"], 120)
        self.assertEqual(document["measures"]["R_session"]["quality"],
                         context_metrics.PARTIAL)
        self.worker._remember_context_session(document, "s")

        second = context_metrics.TurnNormalizer(
            "deepseek", session="s",
            session_reread=self.worker._context_session_baseline("s"),
            session_reread_quality=self.worker._context_session_quality,
            session_reread_reasons=self.worker._context_session_reasons)
        second.call({"inputTokens": 30, "cacheReadTokens": 0,
                     "outputTokens": 1}, "c2")
        measure = second.finish()["measures"]["R_session"]
        self.assertEqual(measure["value"], 150)
        self.assertEqual(measure["quality"], context_metrics.PARTIAL)
        self.assertIn("cache_read_unknown", measure["reasons"])

    def test_absent_invalide_la_base(self):
        self.assertEqual(self.worker._context_session_baseline("s"), 100)
        self.worker._remember_context_session(
            _turn_document("s", None, context_metrics.UNKNOWN, ("no_call",)), "s")
        self.assertIsNone(self.worker._context_session_reread)
        self.assertEqual(self.worker._context_session_quality,
                         context_metrics.UNKNOWN)
        self.assertIn("no_call", self.worker._context_session_reasons)
        # un tour complet suivant ne reconstruit pas un trou : la base reste
        # inconnue, avec la raison du trou
        self.assertIsNone(self.worker._context_session_baseline("s"))
        self.worker._remember_context_session(
            _turn_document("s", 30, context_metrics.MEASURED), "s")
        self.assertIsNone(self.worker._context_session_reread)
        self.assertEqual(self.worker._context_session_quality,
                         context_metrics.UNKNOWN)

    def test_collecte_perdue_invalide_la_base(self):
        self.assertEqual(self.worker._context_session_baseline("s"), 100)
        self.worker._context_observation = None
        AgentWorker._publier_contexte(self.worker, mock.Mock(),
                                      {"session": "s", "turn": "consigne"})
        self.assertIsNone(self.worker._context_session_reread)
        self.assertEqual(self.worker._context_session_quality,
                         context_metrics.UNKNOWN)
        self.assertIn(context_metrics.REASON_COLLECTION_LOST,
                      self.worker._context_session_reasons)
        self.assertIsNone(self.worker._context_session_baseline("s"))

    def test_session_neuve_explicite_repart_de_zero(self):
        self.worker._invalidate_context_session(
            None, (context_metrics.REASON_COLLECTION_LOST,))
        self.assertIsNone(self.worker._context_session_baseline(None))
        self.worker._context_session_forget()
        self.assertEqual(self.worker._context_session_baseline(None), 0)
        self.assertEqual(self.worker._context_baseline_quality,
                         context_metrics.MEASURED)


class ContextResumeProofTest(PgTestCase):
    """Worker neuf sur la même session : la qualité ne remonte jamais à
    ``measured`` sans preuve publiée (#4870, B cx3)."""

    def setUp(self):
        self.worker = _context_worker()
        self.worker.db = self.db
        # Le schéma PgTestCase est partagé par la classe : un agent par test
        # évite qu'un tour d'un autre test entre dans la base relue.
        self.agent = "ctx-resume-" + self._testMethodName
        self.worker._name = self.agent

    def _insert_turn(self, session, input_tokens=100, cached=0):
        cost.storage.of(self.db).turn_costs.insert(
            agent=self.agent, harness="deepseek", turn=None, model=None,
            session=session, usd=0.0, input_tokens=input_tokens,
            cached_input_tokens=cached, output_tokens=0,
            cum_usd=None, cum_input_tokens=None,
            cum_cached_input_tokens=None, cum_output_tokens=None)

    def _publish(self, session, value, quality, reasons=()):
        events.record(
            self.db, cost.CONTEXT_EVENT, "agent:" + self.agent,
            project="ameesh", session=session,
            legacy={"session_turns": 1},
            context={"session": session,
                     "measures": {"R_session": {
                         "value": value, "quality": quality,
                         "reasons": list(reasons)}}})

    def test_reprise_partielle_reste_partielle(self):
        self._insert_turn("s")
        self._publish("s", 100, context_metrics.PARTIAL,
                      ("cache_read_unknown",))
        self.assertEqual(self.worker._context_session_baseline("s"), 100)
        self.assertEqual(self.worker._context_baseline_quality,
                         context_metrics.PARTIAL)
        self.assertIn("cache_read_unknown",
                      self.worker._context_baseline_reasons)
        # Un tour complet suivant ne promeut pas la base.
        self.worker._remember_context_session(
            _turn_document("s", 20, context_metrics.MEASURED), "s")
        self.assertEqual(self.worker._context_session_reread, 120)
        self.assertEqual(self.worker._context_session_quality,
                         context_metrics.PARTIAL)
        self.assertIn("cache_read_unknown",
                      self.worker._context_session_reasons)

    def test_reprise_complete_reste_mesuree(self):
        self._insert_turn("s")
        self._publish("s", 100, context_metrics.MEASURED)
        self.assertEqual(self.worker._context_session_baseline("s"), 100)
        self.assertEqual(self.worker._context_baseline_quality,
                         context_metrics.MEASURED)

    def test_sans_evenement_reste_inconnue(self):
        self._insert_turn("s")
        self.assertIsNone(self.worker._context_session_baseline("s"))
        self.assertEqual(self.worker._context_baseline_quality,
                         context_metrics.UNKNOWN)
        self.assertIn(context_metrics.REASON_SESSION_BASELINE_UNKNOWN,
                      self.worker._context_baseline_reasons)

    def test_tour_inscrit_depuis_la_publication_reste_inconnue(self):
        self._insert_turn("s")
        self._publish("s", 100, context_metrics.MEASURED)
        self._insert_turn("s", input_tokens=50)
        self.assertIsNone(self.worker._context_session_baseline("s"))
        self.assertEqual(self.worker._context_baseline_quality,
                         context_metrics.UNKNOWN)

    def test_evenement_sans_r_session_reste_inconnu(self):
        self._insert_turn("s")
        self._publish("s", None, context_metrics.UNKNOWN,
                      (context_metrics.REASON_TURN_NOT_MEASURED,))
        self.assertIsNone(self.worker._context_session_baseline("s"))
        self.assertEqual(self.worker._context_baseline_quality,
                         context_metrics.UNKNOWN)


class SessionRereadTest(PgTestCase):
    """La base ``R_session`` se relit du grand livre (lecture seule)."""

    def test_aucun_tour_rend_none(self):
        self.assertIsNone(cost.session_reread(self.db, "inconnu", "session-x"))

    def test_somme_entree_et_cache_lu_selon_le_harnais(self):
        for harness, input_tokens, cached, expected in (
                ("deepseek", 100, 900, 1000),
                ("claude", 100, 900, 1000),
                ("codex", 1000, 700, 1000)):
            with self.subTest(harness=harness):
                cost.storage.of(self.db).turn_costs.insert(
                    agent="a-" + harness, harness=harness, turn=None,
                    model=None, session="s-" + harness, usd=0.0,
                    input_tokens=input_tokens, cached_input_tokens=cached,
                    output_tokens=0, cum_usd=None, cum_input_tokens=None,
                    cum_cached_input_tokens=None, cum_output_tokens=None)
                row = cost.storage.of(self.db).turn_costs.session_reread(
                    "a-" + harness, "s-" + harness)
                self.assertEqual(int(row["tokens"]), expected)


class TurnContextEventTest(PgTestCase):
    """Un tour réel du banc publie ``turn.context`` après son grand livre."""

    def _run(self, name: str, usage: str, prompt: str = "un tour de contexte",
             exit_code: str = "0"):
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "deepseek", cwd=cwd, prompt=prompt)
        env = self.env(AMEESH_TEST_DSH_USAGE=usage,
                       AMEESH_TEST_EXIT=exit_code)
        proc = self.runner("--once", "--agents", name, env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " order by id desc limit 1", (cost.CONTEXT_EVENT,))
        self.assertTrue(rows, "aucun événement turn.context publié")
        return rows[0]["data"]

    def test_mesures_etiquetees_a_cote_du_legacy(self):
        data = self._run("ctx-un", "1000,20000,100;500,22000,200")
        self.assertEqual(data["schema"], context_metrics.SCHEMA)
        self.assertEqual(data["agent"], "ctx-un")
        self.assertEqual(data["harness"], "deepseek")
        self.assertEqual(data["mode"], "non-resident")
        self.assertEqual(data["session"], "dsh-session-1")
        measures = data["context"]["measures"]
        self.assertEqual(measures["C_last"]["value"], 22500)
        self.assertEqual(measures["C_last"]["quality"], "measured")
        self.assertEqual(measures["O_last"]["value"], 200)
        self.assertEqual(measures["R_turn"]["value"], 43500)
        self.assertEqual(measures["R_session"]["value"], 43500)
        self.assertEqual(measures["O"]["value"], 300)
        self.assertFalse(data["context"]["partial"])
        self.assertEqual(data["context"]["cache"]["write_tokens"]["value"], 0)
        # Le faux dsh n'annonce pas de modèle : le nom du lancement reste
        # `requested`, jamais promu `measured` (#5147).
        expected_model_quality = (context_metrics.REQUESTED
                                  if data["context"]["model"]
                                  else context_metrics.UNKNOWN)
        self.assertEqual(data["context"]["model_quality"],
                         expected_model_quality)
        # Legacy : les valeurs qui continuent de décider, dont le seuil 150 k.
        ledger = self.db.query(
            "select input_tokens, cached_input_tokens, output_tokens"
            " from turn_costs where agent = %s order by id desc limit 1",
            ("ctx-un",))[0]
        self.assertEqual(data["legacy"]["input_tokens"], ledger["input_tokens"])
        self.assertEqual(data["legacy"]["cached_input_tokens"],
                         ledger["cached_input_tokens"])
        self.assertEqual(data["legacy"]["output_tokens"], ledger["output_tokens"])
        self.assertEqual(data["legacy"]["reread_tokens"], 43500)
        self.assertEqual(data["legacy"]["context_tokens"], 22500)
        self.assertEqual(data["legacy"]["session_tokens"], 1800)
        self.assertEqual(data["legacy"]["session_turns"], 1)
        self.assertEqual(data["legacy"]["context_max_tokens"],
                         self.cfg.context_max_tokens)
        self.assertEqual(
            data["legacy"]["session_max_tokens"],
            mechanics.session_max_tokens(self.cfg, "deepseek",
                                         self.cfg.session_max_tokens))
        self.assertEqual(data["policy"]["session"], self.cfg.session_policy)
        self.assertIsNone(data["decision"]["close"])
        # Aucun texte libre du tour (prompt, réponse du harnais) ne fuit.
        encoded = json.dumps(data)
        self.assertNotIn("un tour de contexte", encoded)
        self.assertNotIn("bonjour du faux dsh", encoded)

    def test_deuxieme_tour_reprend_le_cumul_de_session(self):
        self._run("ctx-deux", "1000,20000,100;500,22000,200")
        data = self._run("ctx-deux", "2000,10000,50")
        self.assertEqual(data["context"]["measures"]["R_turn"]["value"], 12000)
        self.assertEqual(data["context"]["measures"]["R_session"]["value"], 55500)
        self.assertEqual(data["legacy"]["session_turns"], 2)
        self.assertEqual(data["legacy"]["session_tokens"], 3850)

    def test_tour_echoue_reste_publie_avec_ses_raisons(self):
        data = self._run("ctx-echec", "1000,20000,100", exit_code="1")
        self.assertTrue(data["context"]["partial"])
        self.assertIn("turn_interrupted", data["context"]["reasons"])
        # La mesure lue avant l'échec existe toujours ; l'absence est nommée.
        self.assertEqual(data["context"]["measures"]["R_turn"]["value"], 21000)

    def test_aucune_decision_legacy_change(self):
        # Le plafond de contexte reste le seuil legacy, et la publication ne
        # l'abaisse pas : le tour long n'est pas clos par la mesure normalisée.
        data = self._run("ctx-seuil", "1000,20000,100")
        self.assertEqual(data["legacy"]["context_max_tokens"],
                         self.cfg.context_max_tokens)
        self.assertIsNone(data["decision"]["close"])
        row = self.db.query(
            "select context_max_tokens from agent_registry where name = %s",
            ("ctx-seuil",))[0]
        self.assertIsNone(row["context_max_tokens"])


if __name__ == "__main__":
    unittest.main()
