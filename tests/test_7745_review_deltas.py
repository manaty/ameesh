# SPDX-License-Identifier: AGPL-3.0-only
"""#7745 — la règle d'indépendance par delta d'auteur.

Témoins du lot :
- une tête à deux familles d'auteurs (codex + deepseek) est validable avec les
  trois familles : les avis claude+deepseek couvrent le delta codex,
  claude+codex couvrent le delta deepseek ;
- une tête où un delta n'a qu'un avis d'une autre famille reste refusée ;
- une famille globale (commit réattribué, #7324 ; contenu d'un gel antérieur
  réécrit, #2924) reste interdite pour **tous** les deltas.
"""
from __future__ import annotations

import unittest

from ameesh import review_deltas as rdl

FAMILIES = ("claude", "codex", "deepseek")
CODEX = "a" * 40
DEEPSEEK = "b" * 40
CLAUDE = "c" * 40


class DeltaFamiliesTest(unittest.TestCase):

    def test_each_commit_keeps_its_own_author_families(self):
        deltas = rdl.delta_families(
            [CODEX, DEEPSEEK],
            {CODEX: {"codex"}, DEEPSEEK: {"deepseek"}})
        self.assertEqual(deltas, [{"codex"}, {"deepseek"}])

    def test_a_global_family_is_added_to_every_delta(self):
        # #7324 : le commit réattribué sous une autre identité garde la
        # famille de l'acteur réel ; elle ne se lave pas par le découpage.
        deltas = rdl.delta_families(
            [CODEX, DEEPSEEK],
            {CODEX: {"codex"}, DEEPSEEK: {"deepseek"}},
            global_families={"claude"})
        self.assertEqual(deltas, [{"claude", "codex"}, {"claude", "deepseek"}])

    def test_a_commit_without_a_reading_is_unknown(self):
        self.assertIsNone(rdl.delta_families([CODEX], {}))

    def test_a_human_delta_has_no_family(self):
        deltas = rdl.delta_families([CODEX], {CODEX: set()})
        self.assertEqual(deltas, [set()])
        self.assertTrue(rdl.covered(deltas, {"claude", "codex"}, 2))


class CoverageTest(unittest.TestCase):
    """La borne d'un delta est sa propre famille, pas celle de toute la tête."""

    def test_two_author_families_are_valid_with_three_families(self):
        deltas = [{"codex"}, {"deepseek"}]
        self.assertFalse(rdl.covered(deltas, {"claude"}, 2))
        self.assertFalse(rdl.covered(deltas, {"claude", "codex"}, 2))
        self.assertTrue(rdl.covered(deltas, {"claude", "codex", "deepseek"}, 2))
        # Le témoin nomme la couverture : claude+deepseek pour codex,
        # claude+codex pour deepseek.
        self.assertTrue(rdl.covered(deltas, ["claude", "deepseek", "codex"], 2))

    def test_one_author_family_leaves_two_other_families(self):
        self.assertTrue(rdl.covered([{"claude"}], {"codex", "deepseek"}, 2))

    def test_a_delta_with_only_one_other_family_is_refused(self):
        # Deux familles en tout, deux avis : le delta claude n'a que codex.
        self.assertFalse(rdl.covered([{"claude"}], {"claude", "codex"}, 2))
        missing = rdl.uncovered([{"claude"}], {"claude", "codex"}, 2)
        self.assertEqual(missing[0]["eligible"], ["codex"])
        self.assertEqual(missing[0]["missing"], 1)

    def test_a_shared_global_family_closes_every_delta(self):
        deltas = [{"claude", "codex"}, {"claude", "deepseek"}]
        self.assertFalse(rdl.covered(deltas, {"claude", "codex", "deepseek"}, 2))
        missing = rdl.uncovered(deltas, {"claude", "codex", "deepseek"}, 2)
        self.assertEqual([row["index"] for row in missing], [0, 1])
        self.assertEqual(missing[0]["eligible"], ["deepseek"])
        self.assertEqual(missing[1]["eligible"], ["codex"])

    def test_an_ambiguous_delta_excludes_each_proven_family(self):
        self.assertFalse(rdl.covered([{"claude", "codex"}],
                                     {"claude", "codex", "deepseek"}, 2))
        self.assertTrue(rdl.covered([{"claude", "codex"}],
                                    {"codex", "deepseek"}, 1))

    def test_distinct_families_off_lets_one_family_cover_both_reviews(self):
        deltas = [{"claude"}, {"codex"}]
        self.assertFalse(rdl.covered(deltas, ["deepseek"], 2))
        self.assertTrue(rdl.covered(deltas, ["deepseek", "deepseek"], 2,
                                    distinct=False))
        # Les deux avis distincts de la même famille restent insuffisants
        # quand la politique exige des familles distinctes.
        self.assertFalse(rdl.covered(deltas, ["deepseek", "deepseek"], 2))


class MinimalSeatsTest(unittest.TestCase):
    """Le gel désigne le plus petit ensemble de familles, jamais une dispense."""

    def test_two_outside_families_are_enough_for_one_author(self):
        self.assertEqual(
            rdl.minimal_seats([{"claude"}], ["codex", "deepseek"], 2), ["codex", "deepseek"])

    def test_a_second_author_family_takes_the_third_seat(self):
        self.assertEqual(
            rdl.minimal_seats([{"codex"}, {"deepseek"}], FAMILIES, 2),
            ["claude", "codex", "deepseek"])

    def test_a_manual_seat_of_an_author_family_counts_for_the_other_deltas(self):
        # Un siège deepseek ne couvre pas le delta codex (il lui manque un
        # second avis) : les deux autres familles restent à désigner.
        self.assertEqual(
            rdl.minimal_seats([{"codex"}, {"deepseek"}], FAMILIES, 2,
                              already=["deepseek"]),
            ["claude", "codex"])

    def test_no_target_when_the_policy_lacks_families(self):
        self.assertIsNone(rdl.minimal_seats([{"claude"}], ["claude", "codex"], 2))

    def test_a_globally_forbidden_family_is_never_a_target(self):
        deltas = [{"claude", "codex"}, {"claude", "deepseek"}]
        self.assertIsNone(rdl.minimal_seats(deltas, FAMILIES, 2))

    def test_an_already_covered_head_needs_no_seat(self):
        self.assertEqual(
            rdl.minimal_seats([{"codex"}], FAMILIES, 1,
                              already=["claude", "deepseek"]), [])


class CompletedTsTest(unittest.TestCase):
    """#2965 + #7745 : la période ouverte ne se date qu'une fois **chaque**
    delta couvert ; une tête à deux familles d'auteurs en demande trois."""

    def marks(self):
        return [dict(id=1, actor="claude-r", verdict="ok", at_ts=1.0),
                dict(id=2, actor="codex-r", verdict="ok", at_ts=2.0),
                dict(id=3, actor="deepseek-r", verdict="ok", at_ts=3.0)]

    def test_a_third_family_reopens_only_the_delta_it_covers(self):
        from ameesh import merge_queue
        eligible = {"claude-r": "claude", "codex-r": "codex",
                    "deepseek-r": "deepseek"}
        deltas = [{"codex"}, {"deepseek"}]
        # Sans lecture par commit (repli historique), deux familles suffisent
        # à la tête entière : la porte se date au second avis.
        self.assertEqual(merge_queue._gate_completed_ts(
            self.marks()[:2], eligible, required=2, waived=False,
            distinct_families=True), 2.0)
        # Avec les deltas, le delta codex n'a que claude : la porte reste
        # fermée jusqu'au troisième avis, celui de deepseek.
        self.assertIsNone(merge_queue._gate_completed_ts(
            self.marks()[:2], eligible, required=2, waived=False,
            distinct_families=True, deltas=deltas))
        self.assertEqual(merge_queue._gate_completed_ts(
            self.marks(), eligible, required=2, waived=False,
            distinct_families=True, deltas=deltas), 3.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
