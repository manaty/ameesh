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

from ameesh import adapters, context_metrics, cost, mechanics
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
        worker = object.__new__(AgentWorker)
        worker._context_observation = None
        with mock.patch("ameesh.runner.events_mod.record") as record:
            AgentWorker._publier_contexte(worker, mock.Mock(), {})
        record.assert_not_called()

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
