# SPDX-License-Identifier: AGPL-3.0-only
"""L221 — un `result` Claude sans usage ne fait pas du cumul le coût du tour.

Constat (issue #173) : le tour 2260 de mesh-design est inscrit à 0 jeton pour
510,19 USD. Son `result` portait `total_cost_usd` mais un usage vide : la liste
`[{}]` était vraie pour Python, le repli L105 (appel par appel, au barème) était
sauté, et le coût du tour devenait tout le cumul du processus. Désormais un
`result` sans usage se compte comme un tour sans `result` ; son cumul reste le
repère du tour suivant. Postgres réel ; journaux factices.
"""
from __future__ import annotations

import unittest

from .support import PgTestCase
from .test_cost import Sandbox

# barème de `gpt-6.1-sol` (DEFAULT_PRICES) : entrée, relu, sortie, par M jetons
PIN, PCACHE, POUT = 1.25, 0.125, 10.0


def _appel(ident: str, entree: int, relus: int, sortie: int, bloc: str = "text") -> dict:
    return {"type": "assistant", "session_id": "s1", "message": {
        "id": ident, "content": [{"type": bloc}],
        "usage": {"input_tokens": entree, "cache_read_input_tokens": relus,
                  "output_tokens": sortie}}}


class ResultSansUsageTest(Sandbox, PgTestCase):
    def _tour(self, usage) -> list:
        result = {"type": "result", "session_id": "s1", "total_cost_usd": 510.19}
        if usage is not None:
            result["usage"] = usage
        return [
            {"type": "system", "subtype": "init", "session_id": "s1"},
            _appel("m1", 1_000, 2_000_000, 500, "tool_use"),
            _appel("m1", 1_000, 2_000_000, 500, "text"),   # même message : compté une fois
            _appel("m2", 3_000, 6_000_000, 1_500),
            result,
        ]

    def _attendu(self) -> float:
        return (4_000 * PIN + 8_000_000 * PCACHE + 2_000 * POUT) / 1e6

    def test_usage_vide_ou_absent_repli_au_bareme(self):
        for n, usage in enumerate(({}, None, {"input_tokens": 0, "output_tokens": 0})):
            with self.subTest(usage=usage):
                self.db.execute("DELETE FROM turn_costs")
                nom = self.agent("a%d" % n, "claude", "gpt-6.1-sol")
                self.events(nom, *self._tour(usage))
                tour = self.book().record(nom)
                self.assertEqual(tour.input_tokens, 4_000)
                self.assertEqual(tour.cached_input_tokens, 8_000_000)
                self.assertEqual(tour.output_tokens, 2_000)
                self.assertAlmostEqual(tour.usd, self._attendu())
                self.assertLess(tour.usd, 510.19)   # jamais le cumul du processus

    def test_le_cumul_reste_le_repere_du_tour_suivant(self):
        self.agent("a1", "claude", "gpt-6.1-sol")
        book = self.book()
        self.events("a1", *self._tour({}))
        premier = book.record("a1")
        self.assertAlmostEqual(premier.usd, self._attendu())
        row = self.db.query("SELECT cum_usd FROM turn_costs WHERE agent = 'a1'")[0]
        self.assertAlmostEqual(float(row["cum_usd"]), 510.19)
        # Tour suivant, même session, `result` complet : seule la différence.
        self.events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 512.19,
                           "usage": {"input_tokens": 10, "output_tokens": 5}})
        second = book.record("a1", start=5)
        self.assertAlmostEqual(second.usd, 2.0)
        self.assertEqual(second.input_tokens, 10)

    def test_le_result_principal_final_decide(self):
        """Un `result` avec usage (sous-agent, ou principal plus tôt) ne fait pas
        sauter le repli si le `result` principal final n'en a pas."""
        avant = {"input_tokens": 7, "output_tokens": 3}
        for n, precedent in enumerate((
                {"type": "result", "session_id": "s1", "parent_tool_use_id": "outil-1",
                 "total_cost_usd": 0.20, "usage": avant},
                {"type": "result", "session_id": "s1", "total_cost_usd": 500.0,
                 "usage": avant})):
            with self.subTest(precedent=precedent):
                self.db.execute("DELETE FROM turn_costs")
                nom = self.agent("p%d" % n, "claude", "gpt-6.1-sol")
                tour = self._tour({})
                self.events(nom, *(tour[:-1] + [precedent, tour[-1]]))
                usage = self.book().record(nom)
                self.assertAlmostEqual(usage.usd, self._attendu())
                self.assertEqual(usage.cached_input_tokens, 8_000_000)

    def test_un_result_de_sous_agent_n_est_ni_repere_ni_session(self):
        """Un `result` enfant après celui du parent : ni son cumul (0,20) ni sa
        session ne deviennent ceux du tour."""
        self.agent("a1", "claude", "gpt-6.1-sol")
        book = self.book()
        tour = self._tour({"input_tokens": 4_000, "output_tokens": 2_000})
        enfant = {"type": "result", "session_id": "s-enfant", "parent_tool_use_id": "outil-1",
                  "total_cost_usd": 0.20, "usage": {"input_tokens": 5}}
        self.events("a1", *(tour + [enfant]))
        premier = book.record("a1")
        self.assertAlmostEqual(premier.usd, 510.19)       # session neuve : le cumul EST le tour
        row = self.db.query("SELECT cum_usd, session FROM turn_costs WHERE agent = 'a1'")[0]
        self.assertAlmostEqual(float(row["cum_usd"]), 510.19)
        self.assertEqual(row["session"], "s1")
        self.events("a1", {"type": "result", "session_id": "s1", "total_cost_usd": 512.19,
                           "usage": {"input_tokens": 10, "output_tokens": 5}})
        second = book.record("a1", start=len(tour) + 1)
        self.assertAlmostEqual(second.usd, 2.0)


if __name__ == "__main__":
    unittest.main()
