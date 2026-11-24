# SPDX-License-Identifier: AGPL-3.0-only
"""#8209 — « eligible families » ne nomme plus une famille déjà assise.

Constat du 24/11 (#8198, #8200, #8207) : auteur deepseek, deux avis requis,
codex en pause. La désignation assoit claude puis attend, mais disait
« eligible families: claude ». Le pilote a cherché un second relecteur claude,
que la porte refuse (deux familles distinctes). La sortie dit désormais
qu'aucune famille éligible ne reste, et nomme la famille sans relecteur
disponible (codex).
"""
from __future__ import annotations

import unittest

from ameesh import review_designation as rd

from .test_1617_review_designation import FAMILIES, candidates, row

AUTHORS = [{"name": "author", "family": "deepseek"}]
DELTAS = [["deepseek"]]


class EligibleFamiliesTest(unittest.TestCase):
    def pick(self, *pool, automatic=()):
        return rd.select(families=FAMILIES, required=2, authors=AUTHORS,
                         candidates=candidates(*pool), deltas=DELTAS,
                         automatic=automatic)

    def test_the_family_just_seated_is_not_eligible_again(self):
        picked = self.pick(row("claude-one", harness="claude", session="claude"),
                           row("claude-two", harness="claude", session="claude"))
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertEqual([r["name"] for r in picked["reviewers"]], ["claude-one"])
        self.assertIn("eligible families: none", picked["why"])
        self.assertNotIn("eligible families: claude", picked["why"])
        self.assertIn("families without an available reviewer: codex",
                      picked["why"])

    def test_a_kept_seat_is_not_eligible_again(self):
        picked = self.pick(row("claude-two", harness="claude", session="claude"),
                           automatic=[{"name": "claude-one", "family": "claude"}])
        self.assertEqual(picked["state"], rd.WAITING)
        self.assertIn("eligible families: none", picked["why"])
        self.assertIn("families without an available reviewer: codex",
                      picked["why"])

    def test_covered_designation_is_unchanged(self):
        picked = self.pick(row("claude-one", harness="claude", session="claude"),
                           row("codex-one", harness="codex", session="codex"))
        self.assertEqual(picked["state"], rd.DESIGNATED)
        self.assertNotIn("without an available reviewer", picked["why"])


if __name__ == "__main__":
    unittest.main()
