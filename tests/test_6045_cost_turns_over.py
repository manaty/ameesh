# SPDX-License-Identifier: AGPL-3.0-only
"""#6045 — `cost turns --over` : tours au-dessus d'un seuil d'entrée, avec
leur raison normalisée, leur pic et la politique en vigueur.

Lecture seule du grand livre et des événements `turn.context` (CTX-02b) ;
une mesure inconnue est null avec sa raison, jamais 0. Postgres réel.
"""
from __future__ import annotations

import json
import unittest

from ameesh import cost, events, storage

from .support import PgTestCase
from .test_ctx05a_corpus import core, legacy, measure, payload

AGENT = "over-a"


class ParseTokenCountTest(unittest.TestCase):

    def test_suffixes(self):
        self.assertEqual(cost.parse_token_count("2M"), 2_000_000)
        self.assertEqual(cost.parse_token_count("1.5m"), 1_500_000)
        self.assertEqual(cost.parse_token_count("500k"), 500_000)
        self.assertEqual(cost.parse_token_count("1200"), 1200)
        for bad in ("", "deux", "-1", "2G"):
            with self.subTest(bad=bad), self.assertRaises(cost.CostError):
                cost.parse_token_count(bad)


class CostTurnsOverTest(PgTestCase):

    def setUp(self):
        super().setUp()
        self.db.execute(
            "TRUNCATE turn_costs, spend_pending, ameesh_events,"
            " ameesh_events_pruned CASCADE")

    def _ledger(self, turn_id, *, input_tokens, cached=0, harness="claude",
                agent=AGENT):
        storage.of(self.db).turn_costs.insert(
            agent=agent, harness=harness, turn="messages", model=None,
            session="s", usd=1.0, input_tokens=input_tokens,
            cached_input_tokens=cached, output_tokens=10, cum_usd=None,
            cum_input_tokens=None, cum_cached_input_tokens=None,
            cum_output_tokens=None, turn_id=turn_id)

    def _event(self, turn_id, *, decision=None, policy=None, measures=None,
               prompt=None):
        body = payload(agent=AGENT, session="s", turn_id=turn_id,
                       measures=measures, decision=decision, policy=policy,
                       legacy_values=legacy())
        if prompt is not None:
            # du texte libre de harnais ne doit jamais ressortir
            body["context"]["prompt"] = prompt
            body["decision"]["reason"] = prompt
        events.record(self.db, cost.CONTEXT_EVENT, "agent:" + AGENT,
                      project="ameesh", **body)

    def _over(self, *extra):
        proc = self.mesh("cost", "turns", "--over", "2M", "--agent", AGENT,
                         "--json", *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["schema"], "ameesh-turns-over/1")
        self.assertEqual(out["over"], 2_000_000)
        return {turn["turn_id"]: turn for turn in out["turns"]}, proc.stdout

    def test_tour_clos_par_budget(self):
        self._ledger("budget", input_tokens=500_000, cached=2_000_000)
        measures = core(180_000, 900, 2_500_000, 2_500_000, 900)
        measures["C_peak"] = measure(190_000)
        self._event("budget", measures=measures, decision={
            "close": {"kind": "reread_budget", "measure": "R_turn",
                      "value": 2_500_000, "budget": 2_000_000},
            "rotation": None, "compacted": False,
        }, policy={"contract": "ameesh-context/1", "session": "par-lot",
                   "context_max_tokens": 15_000_000,
                   "session_max_tokens": 150_000,
                   "turn_reread_budget": 2_000_000,
                   "compact_tokens": 160_000},
            prompt="SECRET prompt text")
        turns, raw = self._over()
        turn = turns["budget"]
        self.assertEqual((turn["reason"], turn["cause"]),
                         ("close", "reread_budget"))
        self.assertEqual(turn["close"]["budget"], 2_000_000)
        self.assertEqual(turn["close"]["measure"], "R_turn")
        self.assertEqual(turn["reread_tokens"], 2_500_000)
        self.assertEqual(turn["R_turn"]["value"], 2_500_000)
        self.assertEqual(turn["C_last"]["value"], 180_000)
        self.assertEqual(turn["C_peak"]["value"], 190_000)
        self.assertEqual(turn["policy"], {"turn_reread_budget": 2_000_000,
                                          "compact_tokens": 160_000})
        self.assertNotIn("SECRET", raw)

    def test_tour_sans_garde_unbounded_et_pic_inconnu(self):
        self._ledger("libre", input_tokens=3_000_000)
        self._event("libre", measures=core(150_000, 100, 3_000_000,
                                           3_000_000, 100))
        turn = self._over()[0]["libre"]
        self.assertEqual((turn["reason"], turn["cause"]), ("unbounded", None))
        # événement d'avant #6044 : le pic est inconnu, jamais recopié
        self.assertIsNone(turn["C_peak"]["value"])
        self.assertTrue(turn["C_peak"]["reasons"])
        # politique publiée sans opt-in : null, pas 0
        self.assertEqual(turn["policy"], {"turn_reread_budget": None,
                                          "compact_tokens": None})

    def test_tour_sous_le_seuil_exclu_et_codex(self):
        self._ledger("petit", input_tokens=1_000_000, cached=900_000)
        # Codex compte le cache dans son entrée : 2,5 M relus, pas 4,5 M
        self._ledger("codex-pile", input_tokens=2_000_000, cached=2_000_000,
                     harness="codex")
        self._ledger("codex-gros", input_tokens=2_500_000, cached=2_000_000,
                     harness="codex")
        turns, _raw = self._over()
        self.assertEqual(set(turns), {"codex-gros"})
        self.assertEqual(turns["codex-gros"]["reread_tokens"], 2_500_000)

    def test_evenement_absent_not_published(self):
        self._ledger("muet", input_tokens=2_100_000)
        turn = self._over()[0]["muet"]
        self.assertEqual(turn["reason"], "not_published")
        self.assertIsNone(turn["policy"])
        for name in cost.OVER_MEASURES:
            self.assertEqual(turn[name], {"value": None, "quality": "unknown",
                                          "reasons": ["not_published"]})

    def test_sortie_texte(self):
        self._ledger("budget", input_tokens=2_100_000)
        self._event("budget", decision={
            "close": {"kind": "reread_budget"}, "rotation": None})
        self._ledger("muet", input_tokens=2_200_000)
        proc = self.mesh("cost", "turns", "--over", "2M", "--agent", AGENT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("C_PEAK", lines[0])
        self.assertIn("close:reread_budget", proc.stdout)
        self.assertIn("not_published", proc.stdout)

    def test_seuil_illisible(self):
        proc = self.mesh("cost", "turns", "--over", "beaucoup")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("threshold", proc.stderr)


if __name__ == "__main__":
    unittest.main()
