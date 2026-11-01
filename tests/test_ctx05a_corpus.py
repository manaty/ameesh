# SPDX-License-Identifier: AGPL-3.0-only
"""#5134 (CTX-05a) — corpus de rejeu ``ameesh-context-corpus/1``.

Le corpus est reconstruit **en lecture seule** depuis le grand livre des tours
(``turn_costs``) et les événements ``turn.context`` publiés par CTX-02b. Une
ligne par tour, compteurs seuls : aucune décision n'est prise ici, et un
champ absent reste ``None`` avec sa raison, jamais ``0`` (contrat
``ameesh-context/1`` §2.4).

Les cinq reproductions R1 à R5 de l'étude CTX-01 sont versionnées dans
``tests/fixtures/ctx05/`` et se relisent avec :func:`context_corpus.read_corpus`.
Le corpus réel n'est jamais versionné : l'écriture refuse un chemin dans un
arbre Git et pose le fichier en ``0600``.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import unittest

from ameesh import context_corpus, cost, events, storage

from .support import PgTestCase

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ctx05")
FIXTURE_NAMES = (
    "r1_model_usage_order",
    "r2_cache_creation",
    "r3_codex_ten_calls",
    "r4_cumulative_budget",
    "r5_session_guard",
)


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name + ".jsonl")


def measure(value, quality="measured", reasons=(), granularity="call",
            source="stream"):
    return {
        "value": value,
        "quality": quality,
        "reasons": list(reasons),
        "granularity": granularity,
        "source": source,
    }


def legacy(**overrides):
    values = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": None,
        "output_tokens": 0,
        "reread_tokens": 0,
        "context_tokens": None,
        "subagent_reread_tokens": 0,
        "session_tokens": 0,
        "session_turns": 1,
        "context_max_tokens": 15000000,
        "session_max_tokens": 150000,
    }
    values.update(overrides)
    return values


def core(c_last, o_last, r_turn, r_session, o, *, reasons=(),
         r_session_quality="measured"):
    return {
        "C_last": measure(c_last, reasons=reasons),
        "O_last": measure(o_last, reasons=reasons),
        "R_turn": measure(r_turn, reasons=reasons),
        "R_session": measure(r_session, r_session_quality, reasons),
        "O": measure(o, reasons=reasons),
    }


def payload(*, turn_id="t1", agent="ctx05-agent", harness="claude",
            model="claude-opus-5-5", mode="non-resident", role="build",
            session="session-1", measures=None, cache=None,
            legacy_values=None, decision=None, policy=None, started=1000.0,
            ended=1010.0, context_extra=None, partial=False):
    context = {
        "schema": "ameesh-context/1",
        "harness": harness,
        "model": model,
        "mode": mode,
        "role": role,
        "session": session,
        "turn": turn_id,
        "source": "stream",
        "partial": partial,
        "measures": measures if measures is not None else core(
            1000, 100, 1000, 1000, 100),
        "cache": cache if cache is not None else {
            "read_tokens": measure(0), "write_tokens": measure(0)},
        "subagents": {"calls": 0},
        "counts": {"calls": 1, "subagent_calls": 0, "totals": 0,
                   "duplicates": 0, "replaced": 0, "dropped": 0},
        "reasons": [],
        "finished": True,
    }
    if context_extra:
        context.update(context_extra)
    return {
        "schema": "ameesh-context/1",
        "observation_id": "turn:" + turn_id,
        "agent": agent,
        "host": "ameesh-agents-1",
        "turn_id": turn_id,
        "turn": "messages",
        "started_ts": started,
        "ended_ts": ended,
        "session": session,
        "harness": harness,
        "model": model,
        "mode": mode,
        "role": role,
        "policy": policy if policy is not None else {
            "contract": "ameesh-context/1",
            "session": "par-lot",
            "context_max_tokens": 15000000,
            "session_max_tokens": 150000,
        },
        "decision": decision if decision is not None else {
            "close": None, "rotation": None},
        "legacy": legacy_values if legacy_values is not None else legacy(),
        "context": context,
    }


def build(**overrides):
    return context_corpus.corpus_line(payload(**overrides))


class CorpusLineTest(unittest.TestCase):
    """Le constructeur d'une ligne, sans base."""

    def test_liste_blanche_et_champs_obligatoires(self):
        line = build()
        self.assertEqual(line["schema"], context_corpus.SCHEMA)
        self.assertLessEqual(set(line), context_corpus.LINE_KEYS)
        self.assertLessEqual(context_corpus.REQUIRED_LINE_KEYS, set(line))
        self.assertEqual(context_corpus.validate_line(line), line)
        # Un champ hors liste blanche est refusé, à la racine comme dans une
        # mesure : c'est le témoin d'exhaustivité du schéma.
        broken = json.loads(json.dumps(line))
        broken["prompt"] = "secret"
        with self.assertRaises(context_corpus.CorpusError):
            context_corpus.validate_line(broken)
        broken = json.loads(json.dumps(line))
        broken["C_last"]["tool_result"] = "secret"
        with self.assertRaises(context_corpus.CorpusError):
            context_corpus.validate_line(broken)

    def test_aucun_texte_de_la_charge_ne_fuit(self):
        source = payload()
        source["prompt"] = "ne doit jamais entrer dans le corpus"
        source["context"]["tool_result"] = "ne doit jamais entrer"
        source["context"]["mail_body"] = "ne doit jamais entrer"
        line = context_corpus.corpus_line(source)
        encoded = json.dumps(line)
        self.assertNotIn("ne doit jamais entrer", encoded)
        self.assertNotIn("prompt", encoded)
        self.assertNotIn("tool_result", encoded)
        self.assertNotIn("mail_body", encoded)

    def test_absence_reste_none_avec_raison_jamais_zero(self):
        line = build(measures={}, cache={}, legacy_values={})
        for name in ("C_last", "O_last", "R_turn", "R_session",
                     "R_session_base", "O", "cache_read_tokens",
                     "cache_write_tokens", "S_hist", "C_next_input", "C_next",
                     "W"):
            with self.subTest(measure=name):
                self.assertIsNone(line[name]["value"])
                self.assertTrue(line[name]["reasons"])
                self.assertIn(line[name]["quality"], ("unknown", "partial",
                                                      "fallback"))
        self.assertEqual(line["quality"], "unknown")
        self.assertEqual(line["decision"]["observed"], "none")
        context_corpus.validate_line(line)

    def test_session_opaque_et_stable(self):
        first = build()
        second = build()
        self.assertEqual(first["session"], second["session"])
        self.assertNotIn("session-1", json.dumps(first))
        self.assertTrue(first["session"].startswith("sha256:"))
        other = build(agent="autre-agent")
        self.assertNotEqual(first["session"], other["session"])

    def test_w_suit_le_modele_principal_pas_l_auxiliaire(self):
        line = build(context_extra={"model_usage": {
            "claude-haiku-4-5": {"contextWindow": 200000},
            "claude-opus-5-5": {"contextWindow": 1000000},
        }})
        self.assertEqual(line["W"]["value"], 1_000_000)
        self.assertEqual(line["W"]["quality"], "measured")
        self.assertEqual(line["model_quality"], "measured")
        self.assertEqual(line["auxiliary_models"], ["claude-haiku-4-5"])
        self.assertNotEqual(line["W"]["value"], 200000)

    def test_profil_ctx03_prime_et_garde_sa_provenance(self):
        line = build(context_extra={"profile": {
            "schema": "ameesh-context/1",
            "model_effective": "claude-opus-5-5",
            "model_quality": "measured",
            "window_tokens": 1_000_000,
            "window_quality": "measured",
            "window_source": "runtime:modelUsage.contextWindow",
            "reasons": [],
            "auxiliary_models": ["claude-haiku-4-5"],
        }})
        self.assertEqual(line["W"]["value"], 1_000_000)
        self.assertEqual(line["W"]["source"],
                         "runtime:modelUsage.contextWindow")
        self.assertEqual(line["model_quality"], "measured")
        self.assertEqual(line["auxiliary_models"], ["claude-haiku-4-5"])

    def test_sans_profil_le_nom_demande_reste_requested(self):
        # Un tour configuré avec un alias, sans profil ni model_usage publié :
        # le corpus ne peut pas affirmer une mesure runtime du modèle.
        line = build(model="alias-demande")
        self.assertEqual(line["model"], "alias-demande")
        self.assertEqual(line["model_quality"], "requested")
        context_corpus.validate_line(line)

    def test_sans_nom_la_qualite_reste_inconnue(self):
        line = build(model=None)
        self.assertIsNone(line["model"])
        self.assertEqual(line["model_quality"], "unknown")
        context_corpus.validate_line(line)

    def test_qualite_publiee_par_le_producteur_est_reprise(self):
        # Le producteur distingue une annonce du flux (`measured`) du seul nom
        # demandé (`requested`) ; le corpus garde cette provenance.
        for quality in ("measured", "requested"):
            with self.subTest(quality=quality):
                source = payload()
                source["context"]["model_quality"] = quality
                line = context_corpus.corpus_line(source)
                self.assertEqual(line["model_quality"], quality)

    def test_qualite_de_racine_est_reprise(self):
        source = payload()
        source["model_quality"] = "measured"
        line = context_corpus.corpus_line(source)
        self.assertEqual(line["model_quality"], "measured")

    def test_qualite_inconnue_ne_devient_pas_mesuree(self):
        source = payload()
        source["context"]["model_quality"] = "unknown"
        line = context_corpus.corpus_line(source)
        self.assertEqual(line["model_quality"], "unknown")

    def test_profil_sans_modele_effectif_ne_promouvoit_pas_le_nom(self):
        line = build(context_extra={"profile": {
            "schema": "ameesh-context/1",
            "model_quality": "measured",
            "window_tokens": 1_000_000,
            "window_quality": "measured",
        }})
        self.assertEqual(line["model"], "claude-opus-5-5")
        self.assertEqual(line["model_quality"], "requested")
        context_corpus.validate_line(line)

    def test_qualite_hors_vocabulaire_retombe_sur_requested(self):
        source = payload()
        source["context"]["model_quality"] = "presque-mesure"
        line = context_corpus.corpus_line(source)
        self.assertEqual(line["model_quality"], "requested")
        context_corpus.validate_line(line)

    def test_fenetre_zero_reste_inconnue(self):
        line = build(context_extra={"profile": {
            "schema": "ameesh-context/1",
            "model_effective": "claude-opus-5-5",
            "window_tokens": 0,
            "window_quality": "measured",
            "window_source": "runtime:modelUsage.contextWindow",
            "reasons": [],
            "auxiliary_models": [],
        }})
        self.assertIsNone(line["W"]["value"])
        self.assertEqual(line["W"]["quality"], "unknown")
        self.assertIn(context_corpus.REASON_WINDOW_UNKNOWN,
                      line["W"]["reasons"])

    def test_cache_claude_ecrit_dans_c_last_pas_dans_r_turn(self):
        line = build(
            measures=core(24000, 200, 21000, 21000, 200),
            cache={"read_tokens": measure(20000),
                   "write_tokens": measure(3000)},
            legacy_values=legacy(input_tokens=1000, cached_input_tokens=20000,
                                 cache_write_tokens=3000, output_tokens=200,
                                 reread_tokens=21000, context_tokens=24000,
                                 session_tokens=21000),
        )
        self.assertEqual(line["C_last"]["value"], 24000)
        self.assertEqual(line["R_turn"]["value"], 21000)
        self.assertEqual(line["cache_write_tokens"]["value"], 3000)
        self.assertEqual(line["legacy"]["cache_write_tokens"], 3000)

    def test_codex_ne_somme_pas_les_appels_dans_c_last(self):
        line = build(
            harness="codex", model="gpt-6.1-sol",
            measures=core(60000, 1000, 600000, 600000, 10000),
            cache={"read_tokens": measure(59000),
                   "write_tokens": measure(
                       0, "derived", ["harness_without_cache_write"])},
            legacy_values=legacy(input_tokens=600000, cached_input_tokens=59000,
                                 output_tokens=10000, reread_tokens=600000,
                                 context_tokens=60000, session_tokens=600000),
        )
        self.assertEqual(line["C_last"]["value"], 60000)
        self.assertEqual(line["R_turn"]["value"], 600000)
        self.assertEqual(line["C_last"]["value"] * 10,
                         line["R_turn"]["value"])

    def test_budget_cumule_n_est_pas_une_fenetre(self):
        line = build(
            measures=core(50000, 1000, 50000, 400000, 1000),
            legacy_values=legacy(input_tokens=50000, output_tokens=1000,
                                 reread_tokens=50000, context_tokens=50000,
                                 session_tokens=400000),
            context_extra={"profile": {
                "schema": "ameesh-context/1",
                "model_effective": "claude-opus-5-5",
                "window_tokens": 1000000,
                "window_quality": "measured",
                "window_source": "runtime:modelUsage.contextWindow",
                "reasons": [],
                "auxiliary_models": [],
            }},
        )
        self.assertEqual(line["R_session"]["value"], 400000)
        self.assertEqual(line["C_last"]["value"], 50000)
        self.assertEqual(line["W"]["value"], 1000000)
        self.assertEqual(line["decision"]["observed"], "none")

    def test_garde_150k_conventions_de_cache_distinctes(self):
        claude = build(
            session="s-claude",
            measures=core(50000, 0, 150000, 150000, 0),
            cache={"read_tokens": measure(50000),
                   "write_tokens": measure(0)},
            legacy_values=legacy(input_tokens=100000, cached_input_tokens=50000,
                                 cache_write_tokens=0, output_tokens=0,
                                 reread_tokens=150000, context_tokens=50000,
                                 session_tokens=150000, session_turns=3),
        )
        codex = build(
            agent="ctx05-codex", harness="codex", model="gpt-6.1-sol",
            session="s-codex",
            measures=core(50000, 0, 150000, 150000, 0),
            cache={"read_tokens": measure(50000),
                   "write_tokens": measure(
                       0, "derived", ["harness_without_cache_write"])},
            legacy_values=legacy(input_tokens=150000, cached_input_tokens=50000,
                                 output_tokens=0, reread_tokens=150000,
                                 context_tokens=50000, session_tokens=150000,
                                 session_turns=3),
        )
        self.assertEqual(claude["S_hist"]["value"], 150000)
        self.assertEqual(codex["S_hist"]["value"], 150000)
        self.assertEqual(claude["legacy"]["input_tokens"], 100000)
        self.assertEqual(codex["legacy"]["input_tokens"], 150000)
        self.assertEqual(codex["cache_write_tokens"]["value"], 0)
        self.assertEqual(codex["cache_write_tokens"]["quality"], "derived")

    def test_rotation_et_session_precedente_opaque(self):
        line = build(decision={
            "close": None,
            "rotation": {"kind": "context_cap", "old_session": "old-session",
                          "reason": "context cap: 15000000 tokens"},
        })
        self.assertEqual(line["decision"]["observed"], "rotation")
        self.assertEqual(line["decision"]["cause"], "context_cap")
        self.assertEqual(
            line["decision"]["rotation"]["previous_session"],
            context_corpus.opaque_session("ctx05-agent", "old-session"))
        self.assertNotIn("old-session", json.dumps(line))
        self.assertNotIn("context cap:", json.dumps(line))

    def test_compaction_demandee_n_est_pas_reussie(self):
        line = build(decision={
            "close": None,
            "rotation": None,
            "window": {"decision": "compact", "W": 1000000,
                       "unknowns": []},
        })
        self.assertEqual(line["decision"]["observed"], "compaction")
        self.assertIsNone(line["decision"]["compaction"]["post_tokens"])
        self.assertTrue(line["decision"]["compaction"]["requested"])
        self.assertIsNone(line["decision"]["compaction"]["observed"])
        self.assertIsNone(line["decision"]["compacted"])
        self.assertIn(context_corpus.REASON_COMPACTION_REQUESTED,
                      line["decision"]["compaction"]["reasons"])

    def test_compaction_observee_est_visible_a_part(self):
        line = build(context_extra={"compaction": {
            "pre": 100000, "post": 40000, "quality": "measured",
            "reasons": [], "trigger": "auto"}})
        self.assertEqual(line["decision"]["observed"], "compaction")
        self.assertEqual(line["decision"]["compacted"], True)
        self.assertEqual(line["decision"]["compaction"]["observed"], True)
        self.assertEqual(line["decision"]["compaction"]["pre_tokens"], 100000)
        self.assertEqual(line["decision"]["compaction"]["post_tokens"], 40000)

    def test_compaction_non_publiee_reste_inconnue(self):
        line = build()
        self.assertIsNone(line["decision"]["compacted"])
        self.assertIsNone(line["decision"]["compaction"]["observed"])
        self.assertFalse(line["decision"]["compaction"]["requested"])
        self.assertIn(context_corpus.REASON_COMPACTION_NOT_PUBLISHED,
                      line["decision"]["compaction"]["reasons"])

    def test_projection_publiee_est_derivee(self):
        line = build(decision={
            "close": None,
            "rotation": None,
            "window": {"C_last": 50000, "O_last": 1000, "W": 1000000,
                       "C_next_input": 51000, "C_next": 56000,
                       "O_reserve_next": 5000, "share": 0.5, "near": False,
                       "decision": "", "unknowns": []},
        })
        self.assertEqual(line["C_next_input"]["value"], 51000)
        self.assertEqual(line["C_next"]["value"], 56000)
        self.assertEqual(line["C_next_input"]["quality"], "derived")
        self.assertEqual(line["decision"]["window"]["C_next"], 56000)
        self.assertIsNone(line["decision"]["window"]["decision"])

    def test_projection_absente_nommee(self):
        line = build()
        self.assertIsNone(line["C_next_input"]["value"])
        self.assertIn(context_corpus.REASON_PROJECTION_NOT_PUBLISHED,
                      line["C_next_input"]["reasons"])


class ReproductionsTest(unittest.TestCase):
    """Les cinq fixtures versionnées se relisent et disent R1 à R5."""

    def test_les_cinq_fixtures_se_relisent(self):
        for name in FIXTURE_NAMES:
            with self.subTest(fixture=name):
                lines = context_corpus.read_corpus(fixture(name))
                self.assertTrue(lines)
                for line in lines:
                    self.assertEqual(line["schema"], context_corpus.SCHEMA)

    def test_r1_modele_auxiliaire_en_premier(self):
        lines = context_corpus.read_corpus(fixture("r1_model_usage_order"))
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["W"]["value"], 1_000_000)
        self.assertEqual(line["auxiliary_models"], ["claude-haiku-4-5"])
        self.assertNotEqual(line["W"]["value"], 200_000)

    def test_r2_ecriture_de_cache(self):
        line = context_corpus.read_corpus(fixture("r2_cache_creation"))[0]
        self.assertEqual(line["C_last"]["value"], 24000)
        self.assertEqual(line["R_turn"]["value"], 21000)
        self.assertEqual(line["cache_write_tokens"]["value"], 3000)
        self.assertNotEqual(line["C_last"]["value"], line["R_turn"]["value"])

    def test_r3_dix_appels_codex(self):
        line = context_corpus.read_corpus(fixture("r3_codex_ten_calls"))[0]
        self.assertEqual(line["harness"], "codex")
        self.assertEqual(line["C_last"]["value"], 60000)
        self.assertEqual(line["R_turn"]["value"], 600000)
        self.assertEqual(line["C_last"]["value"] * 10,
                         line["R_turn"]["value"])

    def test_r4_400k_cumules_pour_50k_de_contexte(self):
        lines = context_corpus.read_corpus(fixture("r4_cumulative_budget"))
        self.assertEqual(len(lines), 3)
        self.assertEqual([line["session_seq"] for line in lines], [1, 2, 3])
        self.assertEqual(lines[-1]["R_session"]["value"], 400000)
        for line in lines:
            self.assertEqual(line["C_last"]["value"], 50000)
            self.assertEqual(line["W"]["value"], 1_000_000)
            self.assertEqual(line["decision"]["observed"], "none")

    def test_r5_meme_150k_conventions_distinctes(self):
        lines = context_corpus.read_corpus(fixture("r5_session_guard"))
        self.assertEqual(len(lines), 2)
        claude, codex = lines
        self.assertEqual(claude["S_hist"]["value"], 150000)
        self.assertEqual(codex["S_hist"]["value"], 150000)
        self.assertEqual(claude["legacy"]["input_tokens"], 100000)
        self.assertEqual(codex["legacy"]["input_tokens"], 150000)
        self.assertEqual(codex["cache_write_tokens"]["quality"], "derived")
        self.assertEqual(claude["session_seq"], 1)
        self.assertEqual(codex["session_seq"], 1)


class CorpusFileTest(unittest.TestCase):
    """Lecture, liste blanche et écriture hors dépôt en 0600."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctx05-corpus-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lecture_refuse_une_cle_hors_liste_blanche(self):
        line = build()
        line["prompt"] = "secret"
        path = os.path.join(self.tmp, "bad.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(line) + "\n")
        with self.assertRaises(context_corpus.CorpusError):
            context_corpus.read_corpus(path)

    def test_ecriture_0600_et_force(self):
        path = os.path.join(self.tmp, "corpus.jsonl")
        target = context_corpus.write_corpus(path, [build()])
        self.assertEqual(target, os.path.abspath(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(len(context_corpus.read_corpus(path)), 1)
        with self.assertRaises(context_corpus.CorpusError):
            context_corpus.write_corpus(path, [build()])
        context_corpus.write_corpus(
            path, [build(turn_id="t2")], force=True)
        self.assertEqual(len(context_corpus.read_corpus(path)), 1)

    def test_refuse_un_corpus_dans_le_depot(self):
        path = os.path.join(os.path.dirname(__file__), ".ctx05-inside.jsonl")
        try:
            with self.assertRaises(context_corpus.CorpusError):
                context_corpus.write_corpus(path, [build()])
            self.assertFalse(os.path.exists(path))
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_une_ligne_invalide_n_ecrit_rien(self):
        path = os.path.join(self.tmp, "corpus.jsonl")
        broken = build()
        broken["prompt"] = "secret"
        with self.assertRaises(context_corpus.CorpusError):
            context_corpus.write_corpus(path, [broken])
        self.assertFalse(os.path.exists(path))


class ExportCorpusTest(PgTestCase):
    """L'export réel : journal + grand livre, borné par temps et harnais."""

    def setUp(self):
        super().setUp()
        self.db.execute(
            "TRUNCATE turn_costs, spend_pending, ameesh_events,"
            " ameesh_events_pruned CASCADE")

    def _ledger(self, *, agent="ctx05-a", harness="claude", session="s",
                turn_id, input_tokens=100, cached=0, output=10,
                cache_write=None):
        storage.of(self.db).turn_costs.insert(
            agent=agent, harness=harness, turn="messages", model=None,
            session=session, usd=0.0, input_tokens=input_tokens,
            cached_input_tokens=cached, output_tokens=output,
            cum_usd=None, cum_input_tokens=None,
            cum_cached_input_tokens=None, cum_output_tokens=None,
            cache_write_tokens=cache_write, turn_id=turn_id)

    def _event(self, *, agent="ctx05-a", harness="claude", session="s",
               turn_id, started=1000.0, ended=1010.0, session_tokens=100,
               measures=None, cache=None, decision=None, policy=None,
               schema="ameesh-context/1", context_extra=None):
        body = payload(
            agent=agent, harness=harness, session=session, turn_id=turn_id,
            started=started, ended=ended,
            measures=measures or core(1000, 100, 1000, 1000, 100),
            cache=cache or {"read_tokens": measure(0),
                            "write_tokens": measure(0)},
            legacy_values=legacy(input_tokens=100, output_tokens=10,
                                 reread_tokens=100, context_tokens=1000,
                                 session_tokens=session_tokens),
            decision=decision, policy=policy, context_extra=context_extra,
        )
        body["schema"] = schema
        body["observation_id"] = "turn:" + turn_id
        events.record(self.db, cost.CONTEXT_EVENT, "agent:" + agent,
                      project="ameesh", **body)

    def _export(self, **kwargs):
        now = time.time()
        return context_corpus.export_corpus(
            self.db, since_ts=kwargs.pop("since_ts", now - 60),
            until_ts=kwargs.pop("until_ts", now + 60), **kwargs)

    def test_export_lit_le_grand_livre_et_les_evenements(self):
        self._ledger(turn_id="t1", input_tokens=100, output=10)
        self._ledger(turn_id="t2", input_tokens=200, output=20)
        self._event(turn_id="t1", started=1000.0, session_tokens=110,
                    measures=core(1000, 100, 1000, 1000, 100))
        self._event(turn_id="t2", started=2000.0, session_tokens=330,
                    measures=core(1000, 100, 1000, 1100, 100))
        result = self._export()
        self.assertEqual(result.turns, 2)
        self.assertEqual(result.sessions, 1)
        first, second = result.lines
        self.assertEqual(first["S_hist"]["value"], 110)
        self.assertEqual(second["S_hist"]["value"], 330)
        self.assertEqual(first["S_hist"]["source"], "ledger")
        self.assertEqual(first["S_hist"]["quality"], "measured")
        self.assertEqual([line["session_seq"] for line in result.lines],
                         [1, 2])
        self.assertEqual(first["R_session_base"]["value"], 0)
        self.assertEqual(second["R_session_base"]["value"], 100)
        self.assertEqual(second["R_session_base"]["source"], "ledger")
        self.assertEqual(second["R_session_base"]["quality"], "measured")
        self.assertIsNotNone(first["source"]["ledger_id"])
        self.assertNotIn("session-1", json.dumps(first))
        self.assertEqual(first["quality"], "measured")

    def test_export_borne_par_harnais(self):
        self._ledger(turn_id="t1", harness="claude")
        self._ledger(agent="ctx05-b", harness="codex", turn_id="t2")
        self._event(turn_id="t1", harness="claude")
        self._event(agent="ctx05-b", harness="codex", turn_id="t2")
        result = self._export(harness="claude")
        self.assertEqual(result.turns, 1)
        self.assertEqual(result.lines[0]["harness"], "claude")

    def test_export_borne_par_fenetre(self):
        self._event(turn_id="t1")
        future = self._export(since_ts=time.time() + 3600,
                              until_ts=time.time() + 7200)
        self.assertEqual(future.turns, 0)
        self.assertEqual(future.sessions, 0)

    def test_export_ecarte_un_evenement_hors_schema(self):
        self._event(turn_id="t1")
        self._event(turn_id="bad", schema="autre-schema/1")
        result = self._export()
        self.assertEqual(result.turns, 1)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["reason"],
                         context_corpus.REASON_EVENT_SCHEMA_MISMATCH)

    def test_export_ecarte_un_evenement_hors_contrat(self):
        self._event(turn_id="bad", context_extra={"compaction": {
            "pre": 1, "post": 2, "quality": "unknown"}})
        result = self._export()
        self.assertEqual(result.turns, 0)
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0]["reason"],
                         context_corpus.REASON_EVENT_INVALID)

    def test_export_signale_un_ecart_de_session_tokens(self):
        self._ledger(turn_id="t1", input_tokens=100, output=10)
        self._ledger(turn_id="t2", input_tokens=100, output=10)
        self._event(turn_id="t1", started=1000.0, session_tokens=110)
        self._event(turn_id="t2", started=2000.0, session_tokens=110)
        result = self._export()
        second = result.lines[1]
        self.assertEqual(second["S_hist"]["value"], 220)
        self.assertEqual(second["S_hist"]["quality"], "partial")
        self.assertIn(context_corpus.REASON_SESSION_TOKENS_MISMATCH,
                      second["S_hist"]["reasons"])
        self.assertEqual(second["quality"], "partial")


class StorageReadTest(PgTestCase):
    """Les deux lectures nommées qui alimentent l'export."""

    def setUp(self):
        super().setUp()
        self.db.execute(
            "TRUNCATE turn_costs, spend_pending, ameesh_events,"
            " ameesh_events_pruned CASCADE")

    def test_context_events_ordre_curseur_et_harnais(self):
        for turn_id, harness in (("t1", "claude"), ("t2", "codex"),
                                 ("t3", "claude")):
            events.record(self.db, cost.CONTEXT_EVENT, "agent:a",
                          project="ameesh", schema="ameesh-context/1",
                          agent="a", harness=harness, turn_id=turn_id,
                          session="s", legacy={}, context={})
        store = storage.of(self.db).events
        now = time.time()
        rows = store.context_events(now - 60, now + 60, 10)
        self.assertEqual([row["data"]["turn_id"] for row in rows],
                         ["t1", "t2", "t3"])
        after = store.context_events(now - 60, now + 60, 10,
                                     after_id=rows[0]["id"])
        self.assertEqual([row["data"]["turn_id"] for row in after],
                         ["t2", "t3"])
        codex = store.context_events(now - 60, now + 60, 10,
                                     harness="codex")
        self.assertEqual([row["data"]["turn_id"] for row in codex], ["t2"])

    def test_session_rows_ordre_et_champs(self):
        costs = storage.of(self.db).turn_costs
        for index, turn_id in enumerate(("t1", "t2", "t3"), 1):
            costs.insert(
                agent="a", harness="claude", turn=None, model=None,
                session="s", usd=0.0, input_tokens=index * 100,
                cached_input_tokens=index, output_tokens=index,
                cum_usd=None, cum_input_tokens=None,
                cum_cached_input_tokens=None, cum_output_tokens=None,
                cache_write_tokens=index, turn_id=turn_id)
        rows = costs.session_rows("a", "s")
        self.assertEqual([row["turn_id"] for row in rows], ["t1", "t2", "t3"])
        self.assertEqual([row["input_tokens"] for row in rows], [100, 200, 300])
        self.assertEqual([row["cache_write_tokens"] for row in rows], [1, 2, 3])
        self.assertIsNotNone(rows[0]["recorded_ts"])
        self.assertEqual(costs.session_rows("a", "absent"), [])
