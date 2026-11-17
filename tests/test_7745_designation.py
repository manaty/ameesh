# SPDX-License-Identifier: AGPL-3.0-only
"""#7745 — la désignation lit l'indépendance par delta d'auteur.

Témoins du lot, sans base : la sélection seule et le plancher manuel.

(1) une tête à deux familles d'auteurs (codex + deepseek) est couverte par les
    trois familles : claude+deepseek pour le delta codex, claude+codex pour le
    delta deepseek (#7678) ; la règle historique, elle, n'en désignait qu'une
    et attendait ;
(2) s'il n'y a que deux familles, un delta reste découvert et la sélection
    attend en nommant le delta ;
(3) un siège manuel de la famille d'un auteur d'un autre delta couvre les
    deltas qu'elle n'a pas écrits ;
(4) une famille globale (#7324) ne couvre aucun delta ;
(5) le plancher manuel (`_manual_covers`) juge la même couverture.
"""
from __future__ import annotations

import unittest

from ameesh import review_designation as rd
from ameesh import review_designation_activation as act

FAMILIES = ("claude", "codex", "deepseek")
CODEX = {"name": "codex-author", "family": "codex"}
DEEPSEEK = {"name": "ds-author", "family": "deepseek"}
TWO_DELTAS = [{"codex"}, {"deepseek"}]


def row(name, harness="deepseek"):
    """Une ligne de registre de relecteur (capacité et rôle prouvés, #4779)."""
    return {"name": name, "team": "ameesh", "harness": harness,
            "session_harness": "", "status": "idle", "mode": "execute",
            "status_text": "", "capabilities": ["read", "prepare", "propose"],
            "roles": ["implementation"]}


def candidates(*names, loads=None):
    return rd.candidates_from_registry(
        [row(name, harness=name.split("-")[0]) for name in names],
        families=FAMILIES, project="ameesh", loads=loads)


class SelectByDeltaTest(unittest.TestCase):

    def select(self, candidate_names, **kw):
        authors = kw.pop("authors", (CODEX, DEEPSEEK))
        return rd.select(required=kw.pop("required", 2), authors=authors,
                         candidates=candidates(*candidate_names),
                         families=FAMILIES, **kw)

    def test_two_author_families_take_the_three_families(self):
        out = self.select(("claude-r", "codex-r", "deepseek-r"),
                          deltas=TWO_DELTAS)
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["missing"], 0)
        self.assertEqual({seat["family"] for seat in out["reviewers"]},
                         {"claude", "codex", "deepseek"})

    def test_the_historical_rule_still_waits_on_the_same_head(self):
        # Sans lecture par delta (provenance illisible), la porte garde la
        # règle historique : les familles codex et deepseek sont exclues en
        # bloc, seul claude reste, il manque un avis.
        out = self.select(("claude-r", "codex-r", "deepseek-r"))
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["missing"], 1)
        self.assertEqual([seat["family"] for seat in out["reviewers"]],
                         ["claude"])

    def test_two_families_leave_a_delta_uncovered(self):
        out = self.select(("claude-r", "codex-r"), deltas=TWO_DELTAS)
        self.assertEqual(out["state"], rd.WAITING, out)
        # Le delta codex n'a que claude, il lui manque deepseek. Le manque se
        # lit sur le pire delta, avec les familles candidates disponibles.
        self.assertEqual(out["missing"], 1)
        self.assertIn("1 (author families: codex; 1 missing)", out["why"])

    def test_a_manual_seat_of_another_delta_counts(self):
        # codex1 a écrit le delta codex : son siège ne couvre pas ce delta,
        # mais il couvre le delta deepseek, qui demande pourtant deux autres
        # familles. La sélection ajoute donc claude et deepseek, et garde le
        # siège manuel au lieu de l'écarter en bloc.
        out = self.select(
            ("claude-r", "deepseek-r"),
            already=[{"name": "codex-r", "family": "codex"}],
            deltas=TWO_DELTAS)
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([seat["name"] for seat in out["manual"]], ["codex-r"])
        self.assertEqual(
            {seat["family"] for seat in out["manual"] + out["reviewers"]},
            {"claude", "codex", "deepseek"})

    def test_a_global_family_covers_no_delta(self):
        # Commit réattribué (#7324) : codex et deepseek ont tous deux écrit le
        # seul delta (familles globales), seul claude reste recevable.
        out = self.select(("claude-r", "codex-r", "deepseek-r"),
                          deltas=[{"codex", "deepseek"}],
                          global_families={"codex", "deepseek"})
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertEqual(out["missing"], 1)
        self.assertEqual(out["reviewers"], [])

    def test_an_unknown_author_family_still_waits(self):
        out = self.select(("claude-r", "codex-r", "deepseek-r"),
                          authors=[CODEX, {"name": "ghost", "family": ""}],
                          deltas=TWO_DELTAS)
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("author family unknown: ghost", out["why"])


class ManualFloorTest(unittest.TestCase):
    """#7745 : le plancher manuel juge la couverture par delta, pas le compte."""

    def inputs(self, seats, commit_families, global_families=()):
        policy = act.Policy(rules=(), families=FAMILIES, risk="sensitive",
                            required=2, ref="", commit="", source="")
        return act.Inputs(
            item={}, freeze={}, repo="", base="", paths=[], commits=[],
            policy=policy, authors=[CODEX, DEEPSEEK], unknown_authors=[],
            correctors=[], candidates=[], notify="",
            manual=[seat["name"] for seat in seats], manual_seats=list(seats),
            commit_families=dict(commit_families),
            global_families=set(global_families))

    def test_one_manual_family_does_not_cover_two_author_families(self):
        inputs = self.inputs([{"name": "codex-r", "family": "codex"}],
                             {"a": {"codex"}, "b": {"deepseek"}})
        self.assertFalse(act._manual_covers(inputs))

    def test_the_three_manual_families_cover_the_head(self):
        inputs = self.inputs(
            [{"name": "claude-r", "family": "claude"},
             {"name": "codex-r", "family": "codex"},
             {"name": "deepseek-r", "family": "deepseek"}],
            {"a": {"codex"}, "b": {"deepseek"}})
        self.assertTrue(act._manual_covers(inputs))

    def test_without_a_delta_reading_the_old_count_applies(self):
        # Le chemin historique fait confiance aux sièges déjà filtrés par
        # `_manual_seats` (familles d'auteurs écartées en bloc) : deux sièges
        # comptent deux avis. La lecture par delta, elle, voit que codex n'a
        # pas écrit le delta deepseek mais a écrit l'autre.
        seats = [{"name": "claude-r", "family": "claude"},
                 {"name": "codex-r", "family": "codex"}]
        self.assertTrue(act._manual_covers(self.inputs(seats, {})))
        self.assertFalse(act._manual_covers(
            self.inputs(seats, {"a": {"codex"}, "b": {"deepseek"}})))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
