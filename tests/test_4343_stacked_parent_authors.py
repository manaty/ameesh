# SPDX-License-Identifier: AGPL-3.0-only
"""#4343 — un lot empilé sur un prérequis déclaré ne relit pas les commits du parent.

Constat du 28/10 : #4166 (deepseek) empilé sur #4164 (claude) voit dans sa
plage `main..tête` les commits de son parent ; il ne reste alors qu'une famille
éligible (codex) pour deux avis, et le gel est refusé (#3742). #4329, empilé sur
#4237, échoue de même sur une provenance inconnue du parent.

La tête gelée d'un prérequis déclaré (`work dependencies`) est la borne de la
lecture d'auteurs de l'enfant : les commits du parent sont relus dans le
parent, leur provenance inconnue ne bloque pas l'enfant, et les familles
éligibles redeviennent celles de la politique moins l'auteur de l'enfant.
"""
from __future__ import annotations

import unittest

from ameesh import freeze_families as ff
from ameesh import registry, review_designation_activation as act
from ameesh import storage, work, work_dependencies
from ameesh.work_reviewers import designated

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture


class StackedParentTest(_ActivationFixture, PgTestCase):
    """Une tête enfant deepseek posée sur une tête parent claude gelée."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "parent-author", harness="claude",
                        host=self.cfg.host, cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = %s", ("parent-author",))
        self.parent = int(work.add(
            self.db, title="Parent empilé", app="ameesh",
            assignee="parent-author")["id"])

    # -- helpers ----------------------------------------------------------
    def _receipt(self, sha: str, actor: str, harness: str, lot: int) -> None:
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness, lot))

    def _stack(self, *, parent_receipt: bool = True,
               side_parent: bool = False) -> str:
        """Base → parent claude → enfant deepseek, et la dépendance déclarée."""
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/parent.py", "parent\n")
        parent_frozen = self.repo.commit("parent work", agent="parent-author")
        if parent_receipt:
            self._receipt(parent_frozen, "parent-author", "claude", self.parent)
        declared = parent_frozen
        if side_parent:
            # Une tête gelée du parent sur une branche sœur : pas un ancêtre de
            # l'enfant, donc elle ne prouve aucun retrait de la plage.
            self.repo.git("checkout", "-q", "-b", "side-parent", self.base)
            self.repo.write("src/ameesh/side.py", "side\n")
            declared = self.repo.commit("side work", agent="parent-author")
            self.repo.git("checkout", "-q", "-")
        self.repo.write("src/ameesh/mail.py", "child\n")
        self.frozen = self.repo.commit("child work", agent="author")
        self._receipt(self.frozen, "author", "deepseek", self.lot)
        storage.of(self.db).work.add_milestone(
            self.parent, "frozen", sha=declared, actor="parent-author",
            verdict=None, note="")
        work_dependencies.add(self.db, self.lot, self.parent, actor="author")
        return parent_frozen

    def _inputs(self):
        # `freeze` provisoire : la lecture est la même que dans le hook, sans
        # écrire le jalon (les tests de refus gardent la frise vide).
        return act.prepare(self.cfg, self.db, self.lot,
                           freeze={"id": 7, "sha": self.frozen}, fetch=False)

    # -- témoins ----------------------------------------------------------
    def test_the_parent_commits_leave_the_childs_author_reading(self):
        self._stack()
        inputs = self._inputs()
        self.assertIsInstance(inputs, act.Inputs)
        self.assertEqual([row["name"] for row in inputs.authors], ["author"])
        self.assertEqual(inputs.unknown_authors, [])
        verdict = ff.assess(inputs)
        self.assertEqual(verdict["state"], "ok")
        self.assertEqual(verdict["forbidden"], ["deepseek"])
        self.assertEqual(verdict["eligible"], ["claude", "codex"])
        # Et la désignation aboutit sur les deux familles rendues éligibles.
        mark, out = self.freeze()
        self.assertEqual(out["state"], "designated")
        self.assertEqual(set(designated(work.get(self.db, self.lot))),
                         {"r1", "r2"})
        self.assertIn(mark["id"], [row["id"] for row in
                                   work.milestones(self.db, self.lot)])

    def test_the_parents_unknown_provenance_does_not_block_the_child(self):
        self._stack(parent_receipt=False)
        inputs = self._inputs()
        self.assertIsInstance(inputs, act.Inputs)
        self.assertEqual(inputs.unknown_authors, [])
        self.assertEqual([row["name"] for row in inputs.authors], ["author"])
        _mark, out = self.freeze()
        self.assertEqual(out["state"], "designated")

    def test_without_the_dependency_the_parent_commits_stay_authors(self):
        self._stack()
        self.db.execute("DELETE FROM work_item_dependencies"
                        " WHERE work_item_id = %s AND blocker_id = %s",
                        (self.lot, self.parent))
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("freeze refused (author-families)", text)
        self.assertIn("claude", text)
        self.assertIn("deepseek", text)
        self.assertEqual([row for row in work.milestones(self.db, self.lot)
                          if row["kind"] == "frozen"], [])

    def test_a_parent_head_outside_the_childs_history_hides_nothing(self):
        # Le prérequis est déclaré, mais sa tête gelée n'est pas un ancêtre :
        # elle ne prouve rien du contenu de l'enfant.
        self._stack(side_parent=True)
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        self.assertIn("freeze refused (author-families)", str(caught.exception))
        self.assertEqual([row for row in work.milestones(self.db, self.lot)
                          if row["kind"] == "frozen"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
