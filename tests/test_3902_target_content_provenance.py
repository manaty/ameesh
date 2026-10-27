# SPDX-License-Identifier: AGPL-3.0-only
"""#3902 — un commit d'un gel antérieur déjà dans la cible sous un autre SHA
ne compte ni comme auteur ni comme inconnu.

Cas réel : #3519, gel #20698 (83de9ae5) dont la plage ne contient que les deux
commits de l'assigné, mais dont les gels antérieurs #18919/#18915 portent des
copies de l'ancienne pile H7/H8 livrée dans la cible par h-30 sous d'autres
SHA. `_survives_rewrite` ne testait que l'ascendance par SHA : il retrouvait
leur contenu dans la tête (donc dans la cible) et les gardait comme inconnus.

Attendu : un commit d'un gel antérieur dont le patch est déjà dans la cible
(patch-id, ou contenu présent dans la cible avant la tête) ne compte ni comme
auteur ni comme inconnu ; le doute garde. Témoin : un lot empilé, son parent
fusionné dans la cible sous des SHA réécrits, puis regel — la lecture passe
sans `--supersedes`.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import registry, review_authors, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")


class _StackedRepo:
    """Base, pile parente (P) puis enfant (C), cible réécrite (S), tête (C')."""

    def __init__(self, root: str):
        self.repo = _Repo(root)
        self.repo.write("base.txt", "base\n")
        self.repo.write("feature.py", "old_line();\n")
        self.base = self.repo.commit("base")
        # -- pile du lot parent, empilée sur la base -----------------------
        self.repo.write("feature.py", "parent_work();\n")
        self.parent_commit = self.repo.commit("parent lot", agent="parent")
        # -- enfant empilé sur le parent, puis gelé -------------------------
        self.repo.write("child.py", "child_work();\n")
        self.child_commit = self.repo.commit("child lot", agent="impl")
        self.old_head = self.repo.git("rev-parse", "HEAD")
        # -- la cible intègre le parent sous un autre SHA (squash + évolution)
        self.repo.git("checkout", "-q", "-b", "target", self.base)
        self.repo.write("feature.py", "parent_work();\nextra_line();\n")
        self.target_if_integrated = self.repo.commit("target integrates parent",
                                                     agent="other")
        # -- l'enfant est rejoué sur la cible, puis regelé ------------------
        self.repo.git("checkout", "-q", "-b", "rebased",
                      self.target_if_integrated)
        self.repo.write("child.py", "child_work();\n")
        self.head = self.repo.commit("child lot rebased", agent="impl")

    def retarget(self, sha: str) -> None:
        """Repointe la cible et rejoue l'enfant dessus (témoin négatif)."""
        self.repo.git("checkout", "-q", "rebased")
        self.repo.git("reset", "--hard", sha)
        self.repo.write("child.py", "child_work();\n")
        self.head = self.repo.commit("child lot rebased", agent="impl")
        self.target_if_integrated = sha


class ContentBeforeHeadTest(unittest.TestCase):
    """#3902 : la preuve de contenu précède l'ascendance par SHA."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3902-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fixture = _StackedRepo(os.path.join(self.tmp, "repo"))
        self.repo = self.fixture.repo.root

    def remaining(self, target=None, head=None):
        return review_authors._not_in_target(
            self.repo, [self.fixture.parent_commit],
            target or self.fixture.target_if_integrated,
            head=self.fixture.head if head is None else head)

    def test_parent_integrated_under_another_sha_is_not_counted(self):
        """Le parent est dans la cible par contenu, pas par SHA (patch-id)."""
        self.assertEqual(self.remaining(), [])

    def test_parent_content_absent_from_target_keeps_the_unknown(self):
        # la cible avance sans le contenu du parent : le doute garde
        self.fixture.retarget(self.fixture.base)
        self.assertEqual(self.remaining(), [self.fixture.parent_commit])

    def test_reverted_parent_content_keeps_the_unknown(self):
        # la cible a intégré puis annulé le contenu du parent
        repo = self.fixture.repo
        repo.git("checkout", "-q", "-b", "reverted",
                 self.fixture.target_if_integrated)
        repo.write("feature.py", "old_line();\n")
        reverted = repo.commit("revert parent", agent="other")
        repo.git("checkout", "-q", "-b", "on-reverted", reverted)
        repo.write("child.py", "child_work();\n")
        head = repo.commit("child on reverted", agent="impl")
        self.assertEqual(
            review_authors._not_in_target(
                self.repo, [self.fixture.parent_commit], reverted, head=head),
            [self.fixture.parent_commit])

    def test_unreadable_target_content_keeps_the_unknown(self):
        with mock.patch.object(review_authors, "_path_line_counts",
                               return_value=None):
            self.assertEqual(self.remaining(),
                             [self.fixture.parent_commit])

    def test_survives_rewrite_reads_the_target_content_too(self):
        self.assertFalse(review_authors._survives_rewrite(
            self.repo, self.fixture.head, self.fixture.parent_commit, {},
            self.fixture.target_if_integrated,
            self.fixture.target_if_integrated))

    def test_survives_rewrite_keeps_the_unknown_outside_target(self):
        # le contenu du parent est réintroduit par la tête, absent de la
        # cible : le commit reste lu (aucune preuve de contenu dans la cible).
        repo = self.fixture.repo
        repo.git("checkout", "-q", "-b", "reintroduced",
                 self.fixture.base)
        repo.git("cherry-pick", self.fixture.parent_commit)
        repo.write("child.py", "child_work();\n")
        head = repo.commit("reintroduce parent", agent="impl")
        self.assertTrue(review_authors._survives_rewrite(
            self.repo, head, self.fixture.parent_commit, {},
            self.fixture.base, self.fixture.base))

    def test_blank_line_reintroduction_does_not_retain_an_integrated_patch(self):
        """#3781 : une ligne vide du landing n'est pas une réintroduction."""
        repo = self.fixture.repo
        repo.git("checkout", "-q", "-b", "blank-base", self.fixture.base)
        repo.write("code.py", "one();\n")
        base = repo.commit("base code", agent="impl")
        repo.write("code.py", "one();\nblank_probe();\n\n")
        earlier = repo.commit("parent adds a blank line", agent="parent")
        repo.git("checkout", "-q", "-b", "blank-target", base)
        repo.git("cherry-pick", earlier)
        repo.write("code.py", "one();\nblank_probe();\nevolved();\n")
        target = repo.commit("target evolves around the patch", agent="other")
        repo.git("checkout", "-q", "-b", "blank-head", target)
        repo.write("code.py", "one();\nblank_probe();\nevolved();\n\n")
        head = repo.commit("own landing adds another blank line", agent="impl")
        # une ligne vide du landing ne retient pas un patch déjà intégré
        self.assertEqual(review_authors._not_in_target(
            self.repo, [earlier], target, head=head), [])
        self.assertFalse(review_authors._survives_rewrite(
            self.repo, head, earlier, {}, target, target))

    def test_inherited_line_reintroduced_does_not_retain_integrated_patch(self):
        """Une ligne déjà portée par le parent n'est pas la contribution."""
        repo = self.fixture.repo
        repo.git("checkout", "-q", "-b", "own-base", self.fixture.base)
        repo.write("a.py", "import struct\n")
        base = repo.commit("base import", agent="impl")
        repo.write("b.py", "own_probe();\nimport struct\n")
        earlier = repo.commit("parent adds the same import", agent="parent")
        repo.git("checkout", "-q", "-b", "own-target", base)
        repo.git("cherry-pick", earlier)
        repo.write("b.py", "own_probe();\nimport struct\nevolved_b();\n")
        target = repo.commit("target evolves", agent="other")
        repo.git("checkout", "-q", "-b", "own-head", target)
        repo.write("b.py", "own_probe();\nimport struct\nevolved_b();\n"
                          "import struct\n")
        head = repo.commit("own landing repeats the import", agent="impl")
        # une ligne héritée du parent n'est pas la contribution du commit
        self.assertEqual(review_authors._not_in_target(
            self.repo, [earlier], target, head=head), [])
        self.assertFalse(review_authors._survives_rewrite(
            self.repo, head, earlier, {}, target, target))


class RewrittenParentDesignationTest(PgTestCase):
    """La lecture partagée (désignation et porte) ne voit plus le parent."""

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        for name, harness in (("impl", "deepseek"), ("parent", "codex"),
                              ("other", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.item = work.add(self.db, title="cible réécrite #3902",
                             app="ameesh", assignee="impl")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['other']"
                        " WHERE id = %s", (self.lot,))
        self.repo.write("base.txt", "base\n")
        self.repo.write("feature.py", "old_line();\n")
        self.base = self.repo.commit("base")
        self.db.execute("UPDATE work_items SET branch_target = %s"
                        " WHERE id = %s", (self.base, self.lot))
        # pile : parent (codex) puis enfant (deepseek), gel de l'ancienne tête
        self.repo.write("feature.py", "parent_work();\n")
        self.parent_commit = self.repo.commit("parent lot", agent="parent")
        self.repo.write("child.py", "child_work();\n")
        self.child_commit = self.repo.commit("child lot", agent="impl")
        self.receipt("impl", "deepseek", [self.child_commit])
        self.old_head = self.repo.git("rev-parse", "HEAD")
        self.old_freeze = self.freeze(self.old_head)
        # cible : le parent est livré sous un autre SHA (squash + évolution)
        self.repo.git("checkout", "-q", "-b", "target", self.base)
        self.repo.write("feature.py", "parent_work();\nextra_line();\n")
        self.target = self.repo.commit("target integrates parent", agent="other")
        # enfant rejoué sur la cible, regel
        self.repo.git("checkout", "-q", "-b", "rebased", self.target)
        self.repo.write("child.py", "child_work();\n")
        self.head = self.repo.commit("child lot rebased", agent="impl")
        self.receipt("impl", "deepseek", [self.head])
        self.new_freeze = self.freeze(self.head)

    def receipt(self, actor, harness, commits):
        for sha in commits:
            self.db.execute(
                "INSERT INTO provenance_receipts"
                " (subject, sha, actor, session, turn, harness, work_item_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (subject) DO NOTHING",
                ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
                 self.lot))

    def freeze(self, sha):
        return work.milestone(self.db, self.lot, "frozen", sha=sha,
                              actor="impl", repo=self.repo.root)

    def reading(self, *, target=None):
        freeze = {"id": int(self.new_freeze["id"]),
                  "sha": self.head, "actor": "impl",
                  "at_ts": self.new_freeze.get("at_ts")}
        return review_authors.reading(
            self.db, work.get(self.db, self.lot), freeze,
            [self.head], self.repo.root, FAMILIES, strict=True,
            base=self.target, head=self.head,
            target=self.target if target is None else target)

    def names(self, authors):
        return {row["name"]: row["family"] for row in authors}

    def test_integrated_parent_is_neither_author_nor_unknown(self):
        authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [])
        self.assertNotIn("parent", self.names(authors))
        self.assertEqual(self.names(authors), {"impl": "deepseek"})


if __name__ == "__main__":
    unittest.main()
