# SPDX-License-Identifier: AGPL-3.0-only
"""#4829 (CTX-02a) — compteurs de contexte ``ameesh-context/1``, observation seule.

Le contrat (docs/design/ctx-01-compteurs-contexte.md §2.2, §5.3) fixe la lecture
par harnais : Claude sépare l'entrée, le cache lu et le cache écrit ; Codex
compte le cache dans l'entrée ; DeepSeek publie des clés camelCase ; l'ACP
n'a pas de sortie fiable (le pont publie ``output_tokens: 0``). Les tests
partent des **flux synthétiques** du dossier ``tests/fixtures/context`` (jamais
un journal réel) et vérifient : la normalisation, la déduplication, les
sous-agents, les cumuls Codex en delta, l'absence en ``null`` (jamais 0) et
l'invariance exacte des compteurs legacy.

Aucune décision de rotation n'est exercée ici : le normalisateur ne lit aucun
seuil. Le seuil legacy de 150 000 reste celui de ``mechanics``.
"""
from __future__ import annotations

import itertools
import json
import os
import unittest

from ameesh import adapters, context_metrics

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "context")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def parse_lines(reader, name: str) -> list[dict]:
    """Les événements analysés d'un flux JSONL, dans l'ordre du fichier."""
    events = []
    with open(fixture(name), encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                parsed = reader.parse(line)
                if parsed:
                    events.append(parsed)
    return events


def observe_all(reader, name: str, **kwargs) -> dict:
    normalizer = context_metrics.TurnNormalizer(reader.harness, **kwargs)
    for parsed in parse_lines(reader, name):
        normalizer.observe(parsed)
    return normalizer.finish()


class Reader:
    """Un lecteur de flux et le nom de harnais associé."""

    def __init__(self, harness: str, reader):
        self.harness = harness
        self.reader = reader

    def parse(self, line: str) -> dict:
        return self.reader.parse(line)


CLAUDE = Reader("claude", adapters.ClaudeStream())
CODEX = Reader("codex", adapters.CodexStream())
DEEPSEEK = Reader("deepseek", adapters.DshStream())
ACP = Reader("acp", adapters.AcpStream())


class LegacyInvarianceTest(unittest.TestCase):
    """Les compteurs legacy gardent exactement leurs valeurs (§5.3)."""

    @staticmethod
    def legacy_usage_tokens(usage):
        def lu(*keys):
            for key in keys:
                value = (usage or {}).get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return max(int(value), 0)
            return 0
        return (lu("input_tokens", "inputTokens"),
                lu("cache_read_input_tokens", "cached_input_tokens", "cacheReadTokens"),
                lu("output_tokens", "outputTokens"))

    @classmethod
    def legacy_reread(cls, harness, usage):
        entree, cache, _ = cls.legacy_usage_tokens(usage)
        return max(entree, cache) if harness == "codex" else entree + cache

    @classmethod
    def legacy_context(cls, harness, usage):
        entree, cache, _ = cls.legacy_usage_tokens(usage)
        if harness == "codex":
            return max(entree, cache)
        ecrits = 0
        value = (usage or {}).get("cache_creation_input_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            ecrits = max(int(value), 0)
        return entree + cache + ecrits

    SAMPLES = (
        None,
        {},
        {"input_tokens": 5, "cache_read_input_tokens": 7, "output_tokens": 2},
        {"input_tokens": 9, "cached_input_tokens": 6, "output_tokens": 1},
        {"inputTokens": 3, "cacheReadTokens": 8, "outputTokens": 4},
        {"inputTokens": "x"},
        {"input_tokens": "", "inputTokens": 7, "outputTokens": "8"},
        {"input_tokens": -3, "cache_read_input_tokens": 7.9, "output_tokens": True},
        {"input_tokens": 10, "cached_input_tokens": 20},
        {"cache_creation_input_tokens": 3, "cache_read_input_tokens": 4,
         "input_tokens": 5},
        {"cache_creation_input_tokens": -3, "input_tokens": 1},
        {"used": 12, "inputTokens": 12, "cacheReadTokens": 0, "outputTokens": 0},
    )

    def test_usage_tokens_inchange(self):
        for harness, usage in itertools.product(
                ("claude", "codex", "deepseek", "acp", "inconnu"), self.SAMPLES):
            with self.subTest(harness=harness, usage=usage):
                self.assertEqual(adapters.usage_tokens(usage),
                                 self.legacy_usage_tokens(usage))

    def test_reread_et_context_inchanges(self):
        for harness, usage in itertools.product(
                ("claude", "codex", "deepseek", "acp", "inconnu"), self.SAMPLES):
            with self.subTest(harness=harness, usage=usage):
                self.assertEqual(adapters.reread_tokens(harness, usage),
                                 self.legacy_reread(harness, usage))
                self.assertEqual(adapters.context_tokens(harness, usage),
                                 self.legacy_context(harness, usage))

    def test_cache_write_tokens(self):
        self.assertEqual(adapters.cache_write_tokens({"input_tokens": 1}), 0)
        self.assertEqual(adapters.cache_write_tokens(
            {"cache_creation_input_tokens": 3000}), 3000)
        self.assertEqual(adapters.cache_write_tokens(
            {"cache_creation_input_tokens": -4}), 0)
        self.assertEqual(adapters.cache_write_tokens(
            {"cache_creation_input_tokens": True}), 0)


class ClaudeTest(unittest.TestCase):
    def setUp(self):
        self.result = observe_all(CLAUDE, "claude_stream.jsonl",
                                  session="ctx-claude-1", session_reread=100000)
        self.measures = self.result["measures"]

    def test_dernier_appel_avec_cache_lu_et_ecrit(self):
        self.assertEqual(self.measures["C_last"]["value"], 23500)
        self.assertEqual(self.measures["C_last"]["quality"], "measured")
        self.assertEqual(self.measures["O_last"]["value"], 300)

    def test_relecture_du_tour_sans_cache_ecrit(self):
        # msg-1 : 1 000 + 20 000 (cache écrit 3 000 exclu) = 21 000
        # msg-2 : 500 + 23 000 = 23 500
        self.assertEqual(self.measures["R_turn"]["value"], 44500)
        self.assertEqual(self.measures["R_turn"]["quality"], "measured")

    def test_sortie_du_tour_distincte_de_la_derniere_sortie(self):
        self.assertEqual(self.measures["O"]["value"], 500)
        self.assertEqual(self.measures["O_last"]["value"], 300)

    def test_cache_lu_et_ecrit(self):
        self.assertEqual(self.result["cache"]["read_tokens"]["value"], 43000)
        self.assertEqual(self.result["cache"]["write_tokens"]["value"], 3000)
        self.assertFalse(self.result["cache"]["input_includes_read"])
        self.assertTrue(self.result["cache"]["write_in_context"])
        self.assertFalse(self.result["cache"]["write_in_reread"])

    def test_blocs_repete_du_meme_message_compte_une_fois(self):
        # msg-1 apparaît deux fois (texte puis outil) avec le même usage
        self.assertEqual(self.result["counts"]["calls"], 2)
        self.assertEqual(self.result["counts"]["duplicates"], 1)

    def test_sous_agent_mesure_a_part(self):
        self.assertEqual(self.result["counts"]["subagent_calls"], 1)
        sub = self.result["subagents"]
        self.assertEqual(sub["C_last"]["value"], 5000)  # 4 000 + 0 + 1 000
        self.assertEqual(sub["R_turn"]["value"], 4000)  # écrit exclu
        self.assertEqual(sub["O"]["value"], 50)
        # et jamais dans le fil principal
        self.assertEqual(self.measures["R_turn"]["value"], 44500)

    def test_cumul_de_session(self):
        self.assertEqual(self.measures["R_session"]["value"], 144500)
        self.assertEqual(self.measures["R_session"]["quality"], "measured")

    def test_tour_complet(self):
        self.assertFalse(self.result["partial"])


class CodexTest(unittest.TestCase):
    def test_cumul_de_fil_ramene_au_delta_du_tour(self):
        result = observe_all(
            CODEX, "codex_exec.jsonl", session="ctx-codex-1",
            cumulative={"input_tokens": 540000, "cached_input_tokens": 90000,
                        "output_tokens": 2500},
            session_reread=500000)
        measures = result["measures"]
        # 600 000 - 540 000, cache inclus : jamais input + cached
        self.assertEqual(measures["R_turn"]["value"], 60000)
        self.assertEqual(measures["R_turn"]["granularity"], "turn")
        self.assertEqual(measures["R_turn"]["source"], "total")
        self.assertEqual(measures["O"]["value"], 500)
        self.assertEqual(result["cache"]["read_tokens"]["value"], 10000)
        self.assertEqual(result["cache"]["write_tokens"]["value"], 0)
        self.assertEqual(result["cache"]["write_tokens"]["quality"], "derived")
        self.assertEqual(measures["R_session"]["value"], 560000)
        # Codex exec seul ne publie pas le dernier appel : inconnu, pas 0
        self.assertIsNone(measures["C_last"]["value"])
        self.assertEqual(measures["C_last"]["quality"], "unknown")
        self.assertTrue(result["partial"])

    def test_appel_de_journal_pour_c_last_sans_doubler_r_turn(self):
        with open(fixture("codex_journal.json"), encoding="utf-8") as fh:
            journal = json.load(fh)
        info = journal["payload"]["info"]
        normalizer = context_metrics.TurnNormalizer(
            "codex", session="ctx-codex-1", session_reread=500000,
            cumulative={"input_tokens": 540000, "cached_input_tokens": 90000,
                        "output_tokens": 2500})
        for parsed in parse_lines(CODEX, "codex_exec.jsonl"):
            normalizer.observe(parsed)
        normalizer.call(info["last_token_usage"], "codex-call-1", source="journal")
        result = normalizer.finish()
        measures = result["measures"]
        self.assertEqual(measures["C_last"]["value"], 60000)
        self.assertEqual(measures["C_last"]["source"], "journal")
        self.assertEqual(measures["O_last"]["value"], 500)
        # le total du tour reste la source de R_turn : 60 000, pas 120 000
        self.assertEqual(measures["R_turn"]["value"], 60000)
        self.assertEqual(measures["O"]["value"], 500)
        self.assertFalse(result["partial"])

    def test_source_calls_ignore_le_cumul(self):
        normalizer = context_metrics.TurnNormalizer(
            "codex", session="s", reread_source="calls",
            cumulative={"input_tokens": 0, "cached_input_tokens": 0,
                        "output_tokens": 0})
        normalizer.call({"input_tokens": 60000, "cached_input_tokens": 10000,
                         "output_tokens": 500}, "c1", source="journal")
        normalizer.total({"input_tokens": 55000, "cached_input_tokens": 9000,
                          "output_tokens": 400})
        measures = normalizer.finish()["measures"]
        self.assertEqual(measures["R_turn"]["value"], 60000)
        self.assertEqual(measures["O"]["value"], 500)
        self.assertEqual(measures["R_turn"]["source"], "journal")

    def test_base_inconnue_reste_inconnue_puis_delta(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.total({"input_tokens": 600000, "output_tokens": 3000})
        first = normalizer.finish()
        self.assertIsNone(first["measures"]["R_turn"]["value"])
        self.assertEqual(first["measures"]["R_turn"]["quality"], "unknown")
        self.assertIn("cumulative_baseline_unknown",
                      first["measures"]["R_turn"]["reasons"])

        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.total({"input_tokens": 600000, "output_tokens": 3000})
        normalizer.total({"input_tokens": 660000, "output_tokens": 3500})
        second = normalizer.finish()
        self.assertEqual(second["measures"]["R_turn"]["value"], 60000)
        self.assertEqual(second["measures"]["R_turn"]["quality"], "partial")
        self.assertIn("cumulative_baseline_unknown",
                      second["measures"]["R_turn"]["reasons"])
        self.assertEqual(second["measures"]["O"]["value"], 500)
        self.assertTrue(second["partial"])

    def test_baseline_explicite_et_usage_total(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.baseline({"input_tokens": 100, "output_tokens": 1})
        normalizer.observe({"session": "s", "usage_total": {
            "input_tokens": 160, "output_tokens": 3}})
        measures = normalizer.finish()["measures"]
        self.assertEqual(measures["R_turn"]["value"], 60)
        self.assertEqual(measures["R_turn"]["quality"], "measured")
        self.assertEqual(measures["O"]["value"], 2)

    def test_cumul_qui_recule_ne_compte_pas_negativement(self):
        normalizer = context_metrics.TurnNormalizer(
            "codex", session="s",
            cumulative={"input_tokens": 600000, "output_tokens": 3000})
        normalizer.total({"input_tokens": 500000, "output_tokens": 2800})
        normalizer.total({"input_tokens": 510000, "output_tokens": 2900})
        measures = normalizer.finish()["measures"]
        # le recul est nommé, jamais soustrait ; le delta suivant reste valide
        self.assertEqual(measures["R_turn"]["value"], 10000)
        self.assertEqual(measures["R_turn"]["quality"], "partial")
        self.assertIn("cumulative_decreased",
                      measures["R_turn"]["reasons"])
        self.assertEqual(measures["O"]["value"], 100)

    def test_fil_neuf_declare_zero(self):
        result = observe_all(
            CODEX, "codex_exec.jsonl", session="ctx-codex-1",
            cumulative={"input_tokens": 0, "cached_input_tokens": 0,
                        "output_tokens": 0})
        self.assertEqual(result["measures"]["R_turn"]["value"], 600000)
        self.assertEqual(result["measures"]["R_turn"]["quality"], "measured")


class DeepSeekTest(unittest.TestCase):
    def test_convention_dsh(self):
        result = observe_all(DEEPSEEK, "deepseek_stream.jsonl",
                             session="ctx-dsh-1", session_reread=0)
        measures = result["measures"]
        self.assertEqual(measures["C_last"]["value"], 22500)  # 500 + 22 000
        self.assertEqual(measures["R_turn"]["value"], 43500)
        self.assertEqual(measures["O_last"]["value"], 200)
        self.assertEqual(measures["O"]["value"], 300)
        self.assertEqual(result["cache"]["read_tokens"]["value"], 42000)
        self.assertEqual(result["cache"]["write_tokens"]["value"], 0)
        self.assertEqual(result["cache"]["write_tokens"]["quality"], "derived")
        self.assertFalse(result["partial"])


class AcpTest(unittest.TestCase):
    def test_zero_de_sortie_du_pont_n_est_pas_une_mesure(self):
        result = observe_all(ACP, "acp_stream.jsonl", session="ctx-acp-1",
                             session_reread=0)
        measures = result["measures"]
        self.assertEqual(measures["C_last"]["value"], 12000)  # `used`
        self.assertEqual(measures["R_turn"]["value"], 12000)
        self.assertIsNone(measures["O_last"]["value"])
        self.assertEqual(measures["O_last"]["quality"], "unknown")
        self.assertIn("output_unknown", measures["O_last"]["reasons"])
        self.assertIsNone(measures["O"]["value"])
        self.assertTrue(result["partial"])

    def test_used_prioritaire_sur_inputtokens(self):
        measured = context_metrics.call_measure(
            "acp", {"used": 9000, "inputTokens": 12000, "cacheReadTokens": 0,
                    "outputTokens": 0})
        self.assertEqual(measured.c_last.value, 9000)

    def test_used_seul_suffit_a_l_entree(self):
        measured = context_metrics.call_measure("acp", {"used": 7000})
        # sans `cacheReadTokens`, le cache lu reste inconnu : C_last est null,
        # la relecture garde la partie connue en `partial`.
        self.assertIsNone(measured.c_last.value)
        self.assertEqual(measured.c_last.quality, "unknown")
        self.assertEqual(measured.reread.value, 7000)
        self.assertEqual(measured.reread.quality, "partial")
        with_cache = context_metrics.call_measure(
            "acp", {"used": 7000, "cacheReadTokens": 0})
        self.assertEqual(with_cache.c_last.value, 7000)
        self.assertEqual(with_cache.c_last.quality, "measured")


class AbsenceTest(unittest.TestCase):
    def test_cle_absente_n_est_pas_zero(self):
        measured = context_metrics.call_measure(
            "claude", {"output_tokens": 5})
        self.assertIsNone(measured.c_last.value)
        self.assertEqual(measured.c_last.quality, "unknown")
        self.assertIn("context_unknown", measured.c_last.reasons)
        self.assertIsNone(measured.reread.value)
        self.assertIsNone(measured.cache_read.value)
        self.assertIsNone(measured.cache_write.value)
        self.assertEqual(measured.o_last.value, 5)

    def test_entree_seule_reste_partielle(self):
        measured = context_metrics.call_measure(
            "deepseek", {"inputTokens": 10})
        self.assertIsNone(measured.c_last.value)  # cache lu inconnu
        self.assertEqual(measured.reread.value, 10)
        self.assertEqual(measured.reread.quality, "partial")
        self.assertIn("cache_read_unknown", measured.reread.reasons)

    def test_codex_entree_manquante_replie_sur_le_cache(self):
        measured = context_metrics.call_measure(
            "codex", {"cached_input_tokens": 100})
        self.assertEqual(measured.c_last.value, 100)
        self.assertEqual(measured.c_last.quality, "fallback")
        self.assertIn("input_unknown", measured.c_last.reasons)
        self.assertEqual(measured.reread.value, 100)

    def test_dernier_appel_inconnu_ne_reprend_pas_le_precedent(self):
        normalizer = context_metrics.TurnNormalizer("deepseek", session="s")
        normalizer.call({"inputTokens": 10, "cacheReadTokens": 90,
                         "outputTokens": 1}, "a")
        normalizer.call({"outputTokens": 1}, "b")
        measures = normalizer.finish()["measures"]
        self.assertIsNone(measures["C_last"]["value"])
        self.assertEqual(measures["C_last"]["quality"], "unknown")
        # R_turn garde la partie connue, explicitement partielle
        self.assertEqual(measures["R_turn"]["value"], 100)
        self.assertEqual(measures["R_turn"]["quality"], "partial")

    def test_tour_interrompu_marque_partiel(self):
        normalizer = context_metrics.TurnNormalizer("deepseek", session="s")
        normalizer.call({"inputTokens": 10, "cacheReadTokens": 90,
                         "outputTokens": 1}, "a")
        result = normalizer.finish(interrupted=True)
        self.assertTrue(result["partial"])
        self.assertEqual(result["measures"]["C_last"]["quality"], "partial")
        self.assertIn("turn_interrupted",
                      result["measures"]["C_last"]["reasons"])


class DedupTest(unittest.TestCase):
    def test_meme_appel_identique_compte_une_fois(self):
        normalizer = context_metrics.TurnNormalizer("deepseek", session="s")
        usage = {"inputTokens": 10, "cacheReadTokens": 90, "outputTokens": 5}
        normalizer.call(usage, "a")
        normalizer.call(usage, "a")
        result = normalizer.finish()
        self.assertEqual(result["counts"]["calls"], 1)
        self.assertEqual(result["counts"]["duplicates"], 1)
        self.assertEqual(result["measures"]["R_turn"]["value"], 100)

    def test_mise_a_jour_du_meme_appel_remplace(self):
        normalizer = context_metrics.TurnNormalizer("deepseek", session="s")
        normalizer.call({"inputTokens": 10, "cacheReadTokens": 90,
                         "outputTokens": 5}, "a")
        normalizer.call({"inputTokens": 20, "cacheReadTokens": 180,
                         "outputTokens": 7}, "a")
        result = normalizer.finish()
        self.assertEqual(result["counts"]["calls"], 1)
        self.assertEqual(result["counts"]["replaced"], 1)
        self.assertEqual(result["measures"]["C_last"]["value"], 200)
        self.assertEqual(result["measures"]["R_turn"]["value"], 200)
        self.assertEqual(result["measures"]["O_last"]["value"], 7)

    def test_meme_identifiant_principal_et_sous_agent(self):
        normalizer = context_metrics.TurnNormalizer("deepseek", session="s")
        normalizer.call({"inputTokens": 10, "cacheReadTokens": 0,
                         "outputTokens": 1}, "a")
        normalizer.call({"inputTokens": 20, "cacheReadTokens": 0,
                         "outputTokens": 2}, "a", subagent=True)
        result = normalizer.finish()
        self.assertEqual(result["counts"]["calls"], 1)
        self.assertEqual(result["counts"]["subagent_calls"], 1)
        self.assertEqual(result["measures"]["R_turn"]["value"], 10)
        self.assertEqual(result["subagents"]["R_turn"]["value"], 20)

    def test_call_id_absent_est_nomme_pour_claude(self):
        normalizer = context_metrics.TurnNormalizer("claude", session="s")
        normalizer.call({"input_tokens": 1, "cache_read_input_tokens": 2,
                         "cache_creation_input_tokens": 3, "output_tokens": 4})
        result = normalizer.finish()
        self.assertEqual(result["measures"]["R_turn"]["quality"], "partial")
        self.assertIn("call_id_unknown",
                      result["measures"]["R_turn"]["reasons"])

    def test_borne_de_memoire_du_tour(self):
        normalizer = context_metrics.TurnNormalizer(
            "deepseek", session="s", max_calls=2)
        for index in range(3):
            normalizer.call({"inputTokens": 10, "cacheReadTokens": 0,
                             "outputTokens": 1}, "c%d" % index)
        result = normalizer.finish()
        self.assertEqual(result["counts"]["calls"], 2)
        self.assertEqual(result["counts"]["dropped"], 1)
        self.assertTrue(result["partial"])
        self.assertIn("call_bound_reached", result["reasons"])


class SessionTest(unittest.TestCase):
    def test_releve_d_une_autre_session_ignore(self):
        normalizer = context_metrics.TurnNormalizer("claude", session="s1")
        normalizer.observe({"session": "s2", "call_usage": {
            "input_tokens": 1, "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 3, "output_tokens": 4},
            "call_id": "a"})
        result = normalizer.finish()
        self.assertEqual(result["counts"]["calls"], 0)
        self.assertTrue(result["partial"])
        self.assertIn("session_mismatch", result["reasons"])

    def test_base_de_session_non_declaree_reste_inconnue(self):
        normalizer = context_metrics.TurnNormalizer("deepseek", session="s")
        normalizer.call({"inputTokens": 10, "cacheReadTokens": 90,
                         "outputTokens": 1}, "a")
        measures = normalizer.finish()["measures"]
        self.assertIsNone(measures["R_session"]["value"])
        self.assertEqual(measures["R_session"]["quality"], "unknown")
        self.assertIn("session_baseline_unknown",
                      measures["R_session"]["reasons"])

    def test_session_incoherente_n_avance_pas_le_cumul(self):
        normalizer = context_metrics.TurnNormalizer(
            "claude", session="s1", session_reread=10)
        normalizer.observe({"session": "s2"})
        normalizer.call({"input_tokens": 1, "cache_read_input_tokens": 2,
                         "cache_creation_input_tokens": 3, "output_tokens": 4},
                        "a")
        normalizer.finish()
        self.assertEqual(normalizer.session_reread, 10)

    def test_sans_session_le_cumul_de_session_est_inconnu(self):
        normalizer = context_metrics.TurnNormalizer("deepseek")
        normalizer.call({"inputTokens": 10, "cacheReadTokens": 90,
                         "outputTokens": 1}, "a")
        measures = normalizer.finish()["measures"]
        self.assertIsNone(measures["R_session"]["value"])
        self.assertEqual(measures["R_session"]["quality"], "unknown")
        self.assertIn("no_session", measures["R_session"]["reasons"])

    def test_fin_idempotente_ne_somme_pas_deux_fois(self):
        normalizer = context_metrics.TurnNormalizer(
            "deepseek", session="s", session_reread=1000)
        normalizer.call({"inputTokens": 10, "cacheReadTokens": 90,
                         "outputTokens": 1}, "a")
        first = normalizer.finish()
        second = normalizer.finish()
        self.assertEqual(first, second)
        self.assertEqual(normalizer.session_reread, 1100)


class SchemaTest(unittest.TestCase):
    def test_document_serialisable_et_complet(self):
        result = observe_all(CLAUDE, "claude_stream.jsonl",
                             session="ctx-claude-1", session_reread=0)
        encoded = json.dumps(result)
        self.assertIn("ameesh-context/1", encoded)
        self.assertEqual(result["schema"], context_metrics.SCHEMA)
        self.assertEqual(
            set(result["measures"]),
            {"C_last", "O_last", "R_turn", "R_session", "O"})
        for measure in result["measures"].values():
            self.assertIn(measure["quality"], context_metrics.QUALITIES)
        self.assertEqual(
            set(result["cache"]),
            {"read_tokens", "write_tokens", "input_includes_read",
             "write_in_context", "write_in_reread"})
        self.assertIn("subagents", result)
        self.assertIn("cache", result["subagents"])
        self.assertIn("counts", result)
        # aucune donnée de prompt, de résultat d'outil ou de secret
        self.assertNotIn("prompt", encoded)
        self.assertNotIn("tool_result", encoded)

    def test_convention_par_harnais(self):
        self.assertTrue(context_metrics.convention("codex").input_includes_cache)
        self.assertTrue(context_metrics.convention("claude").publishes_cache_write)
        self.assertTrue(context_metrics.convention("acp").output_zero_unknown)
        # alias de format de flux
        self.assertEqual(context_metrics.convention("dsh-json").harness,
                         "deepseek")
        self.assertEqual(context_metrics.convention("claude-stream-json").harness,
                         "claude")
        self.assertFalse(context_metrics.convention("gemini").input_includes_cache)


if __name__ == "__main__":
    unittest.main()
