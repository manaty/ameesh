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
        with mock.patch.object(review_authors, "_path_lines",
                               return_value=None):
            self.assertEqual(self.remaining(),
                             [self.fixture.parent_commit])

    def test_unreadable_hunks_keep_the_unknown(self):
        with mock.patch.object(review_authors, "_commit_hunks",
                               return_value=None):
            self.assertFalse(review_authors._content_before_head(
                self.repo, self.fixture.target_if_integrated,
                self.fixture.parent_commit))

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

    def test_reordered_calls_with_delivered_marker_keep_the_unknown(self):
        """#3902 B/P2 : un réordre + un ajout déjà livré n'est pas « déjà là ».

        Les comptes de lignes dépouillées voyaient le marqueur présent et
        ignoraient l'ordre : le commit perdait son auteur alors que son
        réordre restait à livrer.
        """
        repo = _Repo(os.path.join(self.tmp, "reorder"))
        repo.write("feature.py",
                   "def operation():\n"
                   "    validate_access()\n"
                   "    perform_action()\n")
        base = repo.commit("base")
        repo.write("feature.py",
                   "def operation():\n"
                   "    perform_action()\n"
                   "    validate_access()\n"
                   "    # parent_marker_is_present\n")
        earlier = repo.commit("parent reorders and marks", agent="parent")
        repo.git("checkout", "-q", "-b", "target", base)
        repo.write("feature.py",
                   "def operation():\n"
                   "    validate_access()\n"
                   "    perform_action()\n"
                   "    # parent_marker_is_present\n")
        target = repo.commit("target integrates the marker only", agent="other")
        repo.git("checkout", "-q", "-b", "head", target)
        repo.write("feature.py",
                   "def operation():\n"
                   "    perform_action()\n"
                   "    validate_access()\n"
                   "    # parent_marker_is_present\n")
        head = repo.commit("head delivers the reorder", agent="impl")
        self.assertFalse(review_authors._content_before_head(
            repo.root, target, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], target, head=head), [earlier])
        self.assertTrue(review_authors._survives_rewrite(
            repo.root, head, earlier, {}, target, target))

    def test_reindented_contribution_with_delivered_marker_keeps_the_unknown(self):
        """#3902 B/P2 : une ligne dépouillée ignore aussi l'indentation."""
        repo = _Repo(os.path.join(self.tmp, "reindent"))
        repo.write("feature.py",
                   "def operation():\n"
                   "    perform_action()\n")
        base = repo.commit("base")
        repo.write("feature.py",
                   "def operation():\n"
                   "    with lock:\n"
                   "            perform_action()\n"
                   "    # parent_marker_is_present\n")
        earlier = repo.commit("parent indents and marks", agent="parent")
        repo.git("checkout", "-q", "-b", "target", base)
        repo.write("feature.py",
                   "def operation():\n"
                   "    with lock:\n"
                   "        perform_action()\n"
                   "    # parent_marker_is_present\n")
        target = repo.commit("target integrates the marker only", agent="other")
        repo.git("checkout", "-q", "-b", "head", target)
        repo.write("feature.py",
                   "def operation():\n"
                   "    with lock:\n"
                   "            perform_action()\n"
                   "    # parent_marker_is_present\n")
        head = repo.commit("head delivers the indent", agent="impl")
        self.assertFalse(review_authors._content_before_head(
            repo.root, target, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], target, head=head), [earlier])

    def test_deletion_is_proven_only_when_applied(self):
        """La suppression d'une ligne n'est prouvée que dans son contexte."""
        repo = _Repo(os.path.join(self.tmp, "deletion"))
        repo.write("feature.py",
                   "keep_line()\n"
                   "remove_me()\n"
                   "end_line()\n")
        base = repo.commit("base")
        repo.write("feature.py",
                   "keep_line()\n"
                   "end_line()\n")
        earlier = repo.commit("parent removes a line", agent="parent")
        # cible où le retrait n'est pas appliqué (seul un marqueur arrive)
        repo.git("checkout", "-q", "-b", "kept", base)
        repo.write("feature.py",
                   "keep_line()\n"
                   "remove_me()\n"
                   "end_line()\n"
                   "# target_marker\n")
        kept = repo.commit("target keeps the line", agent="other")
        self.assertFalse(review_authors._content_before_head(
            repo.root, kept, earlier))
        # cible où le retrait est appliqué
        repo.git("checkout", "-q", "-b", "removed", base)
        repo.git("cherry-pick", earlier)
        repo.write("anchor.txt", "target advanced\n")
        removed = repo.commit("target applies the deletion", agent="other")
        self.assertTrue(review_authors._content_before_head(
            repo.root, removed, earlier))

    def test_removal_at_end_still_present_keeps_the_unknown(self):
        """Une suppression en bout de fichier n'est pas prouvée par son seul
        contexte : la ligne retirée doit avoir disparu de la cible."""
        repo = _Repo(os.path.join(self.tmp, "tail-removal"))
        repo.write("feature.py", "keep()\ndrop_me()\n")
        base = repo.commit("base")
        repo.write("feature.py", "keep()\n")
        earlier = repo.commit("parent deletes the last line", agent="parent")
        # cible qui n'applique pas le retrait et avance ailleurs
        repo.git("checkout", "-q", "-b", "tail-kept", base)
        repo.write("anchor.txt", "target advanced\n")
        kept = repo.commit("target keeps the line", agent="other")
        self.assertFalse(review_authors._content_before_head(
            repo.root, kept, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], kept, head=kept), [earlier])
        self.assertTrue(review_authors._survives_rewrite(
            repo.root, kept, earlier, {}, kept, kept))
        # cible qui applique le retrait
        repo.git("checkout", "-q", "-b", "tail-delivered", base)
        repo.git("cherry-pick", earlier)
        repo.write("anchor.txt", "target advanced\n")
        delivered = repo.commit("target deletes the line", agent="other")
        self.assertTrue(review_authors._content_before_head(
            repo.root, delivered, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], delivered, head=delivered), [])

    def test_removal_at_start_still_present_keeps_the_unknown(self):
        """Même preuve pour une suppression en tête de fichier."""
        repo = _Repo(os.path.join(self.tmp, "head-removal"))
        repo.write("feature.py", "drop_me()\nkeep()\n")
        base = repo.commit("base")
        repo.write("feature.py", "keep()\n")
        earlier = repo.commit("parent deletes the first line", agent="parent")
        repo.git("checkout", "-q", "-b", "head-kept", base)
        repo.write("anchor.txt", "target advanced\n")
        kept = repo.commit("target keeps the line", agent="other")
        self.assertFalse(review_authors._content_before_head(
            repo.root, kept, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], kept, head=kept), [earlier])
        repo.git("checkout", "-q", "-b", "head-delivered", base)
        repo.git("cherry-pick", earlier)
        repo.write("anchor.txt", "target advanced\n")
        delivered = repo.commit("target deletes the line", agent="other")
        self.assertTrue(review_authors._content_before_head(
            repo.root, delivered, earlier))

    def test_duplicate_context_elsewhere_does_not_prove_a_removal(self):
        """#3902 B/P2 (cx2) : le bloc postérieur d'une suppression peut
        exister tel quel dans une autre fonction ; la preuve ne vaut que si
        l'image antérieure n'y est pas plus présente que dans la post-image."""
        repo = _Repo(os.path.join(self.tmp, "duplicate-context"))
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    remove_me()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    end_line()\n")
        base = repo.commit("base")
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    end_line()\n")
        earlier = repo.commit("parent removes the protected line",
                              agent="parent")
        # cible : base inchangée, marqueur en fin de fichier
        repo.git("checkout", "-q", "-b", "duplicate-target", base)
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    remove_me()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "# target_marker\n")
        target = repo.commit("target keeps the removal undone", agent="other")
        self.assertFalse(review_authors._content_before_head(
            repo.root, target, earlier))
        # tête : la cible + la suppression, donc la contribution reste à livrer
        repo.git("checkout", "-q", "-b", "duplicate-head", target)
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "# target_marker\n")
        head = repo.commit("head delivers the removal", agent="impl")
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], target, head=head), [earlier])
        self.assertTrue(review_authors._survives_rewrite(
            repo.root, head, earlier, {}, target, target))
        # cible où la suppression est réellement appliquée : preuve positive
        repo.git("checkout", "-q", "-b", "duplicate-delivered", base)
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "# target_marker\n")
        delivered = repo.commit("target applies the removal", agent="other")
        self.assertTrue(review_authors._content_before_head(
            repo.root, delivered, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], delivered, head=head), [])

    def test_removal_applied_in_the_other_function_does_not_prove_the_hunk(self):
        """#3902 B/P2 cx2 (gel #21778) : un retrait appliqué dans l'autre
        fonction laisse le compte global égal ; seul l'ancrage sur
        l'occurrence d'origine garde le doute."""
        repo = _Repo(os.path.join(self.tmp, "permuted-removal"))
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    remove_me()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    remove_me()\n"
                   "    end_line()\n")
        base = repo.commit("base")
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    remove_me()\n"
                   "    end_line()\n")
        earlier = repo.commit("parent removes the protected line",
                              agent="parent")
        # cible : le retrait est appliqué dans other(), pas dans protected()
        repo.git("checkout", "-q", "-b", "permuted-target", base)
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    remove_me()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "# target_marker\n")
        target = repo.commit("target removes elsewhere", agent="other")
        self.assertFalse(review_authors._content_before_head(
            repo.root, target, earlier))
        repo.git("checkout", "-q", "-b", "permuted-head", target)
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "# target_marker\n")
        head = repo.commit("head delivers the protected removal", agent="impl")
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], target, head=head), [earlier])
        self.assertTrue(review_authors._survives_rewrite(
            repo.root, head, earlier, {}, target, target))
        # positif : le retrait est appliqué dans protected(), other() garde
        # le sien ; le compte global reste égal, l'ancrage doit prouver.
        repo.git("checkout", "-q", "-b", "permuted-delivered", base)
        repo.write("feature.py",
                   "def protected():\n"
                   "    keep_line()\n"
                   "    end_line()\n"
                   "\n"
                   "def other():\n"
                   "    keep_line()\n"
                   "    remove_me()\n"
                   "    end_line()\n"
                   "# target_marker\n")
        delivered = repo.commit("target applies the right removal",
                                agent="other")
        self.assertTrue(review_authors._content_before_head(
            repo.root, delivered, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], delivered, head=head), [])

    def test_mode_change_not_delivered_keeps_the_unknown(self):
        """#3902 B/P2 (cx1) : un mode encore absent de la cible est un doute,
        même quand le texte du même commit y est déjà."""
        repo = _Repo(os.path.join(self.tmp, "mode-change"))
        repo.write("script.sh", "#!/bin/sh\necho old\n")
        base = repo.commit("base")
        repo.write("script.sh", "#!/bin/sh\necho new\n")
        os.chmod(os.path.join(repo.root, "script.sh"), 0o755)
        earlier = repo.commit("parent changes text and mode", agent="parent")
        # cible : texte livré, mode encore 100644
        repo.git("checkout", "-q", "-b", "mode-target", base)
        repo.write("script.sh", "#!/bin/sh\necho new\n")
        target = repo.commit("target applies text only", agent="other")
        self.assertFalse(review_authors._content_before_head(
            repo.root, target, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], target, head=earlier), [earlier])
        self.assertTrue(review_authors._survives_rewrite(
            repo.root, earlier, earlier, {}, target, target))
        # cible où texte et mode sont livrés
        repo.git("checkout", "-q", "-b", "mode-delivered", base)
        repo.write("script.sh", "#!/bin/sh\necho new\n")
        os.chmod(os.path.join(repo.root, "script.sh"), 0o755)
        delivered = repo.commit("target applies text and mode", agent="other")
        self.assertTrue(review_authors._content_before_head(
            repo.root, delivered, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], delivered, head=earlier), [])
        self.assertFalse(review_authors._survives_rewrite(
            repo.root, earlier, earlier, {}, delivered, delivered))

    def test_mode_only_path_in_a_text_commit_not_delivered(self):
        """Un chemin à mode seul dans un commit qui change du texte ailleurs
        ne peut pas être écarté par la preuve de texte."""
        repo = _Repo(os.path.join(self.tmp, "mode-only-path"))
        repo.write("code.py", "old();\n")
        repo.write("tool.sh", "#!/bin/sh\necho tool\n")
        base = repo.commit("base")
        repo.write("code.py", "new();\n")
        os.chmod(os.path.join(repo.root, "tool.sh"), 0o755)
        earlier = repo.commit("parent changes text and a mode-only path",
                              agent="parent")
        # cible : le texte est livré, le mode de tool.sh ne l'est pas
        repo.git("checkout", "-q", "-b", "mode-only-target", base)
        repo.write("code.py", "new();\n")
        target = repo.commit("target applies text only", agent="other")
        self.assertFalse(review_authors._content_before_head(
            repo.root, target, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], target, head=earlier), [earlier])
        # cible où le mode du chemin à mode seul est aussi livré
        repo.git("checkout", "-q", "-b", "mode-only-delivered", base)
        repo.write("code.py", "new();\n")
        os.chmod(os.path.join(repo.root, "tool.sh"), 0o755)
        delivered = repo.commit("target applies text and mode", agent="other")
        self.assertTrue(review_authors._content_before_head(
            repo.root, delivered, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], delivered, head=earlier), [])

    def test_mode_only_commit_is_proven_by_its_delivered_mode(self):
        """Un commit à mode seul est une contribution : livré, il s'écarte ;
        non livré, il garde son auteur."""
        repo = _Repo(os.path.join(self.tmp, "mode-only-commit"))
        repo.write("script.sh", "#!/bin/sh\necho x\n")
        base = repo.commit("base")
        os.chmod(os.path.join(repo.root, "script.sh"), 0o755)
        earlier = repo.commit("parent changes the mode only", agent="parent")
        repo.git("checkout", "-q", "-b", "mode-only-kept", base)
        repo.write("anchor.txt", "target advanced\n")
        kept = repo.commit("target keeps the mode", agent="other")
        self.assertFalse(review_authors._content_before_head(
            repo.root, kept, earlier))
        repo.git("checkout", "-q", "-b", "mode-only-applied", base)
        os.chmod(os.path.join(repo.root, "script.sh"), 0o755)
        repo.write("anchor.txt", "target advanced\n")
        delivered = repo.commit("target applies the mode", agent="other")
        self.assertTrue(review_authors._content_before_head(
            repo.root, delivered, earlier))

    def test_delivered_reorder_is_proven(self):
        """La preuve stricte accepte un réordre réellement livré."""
        repo = _Repo(os.path.join(self.tmp, "reorder-delivered"))
        repo.write("feature.py",
                   "def operation():\n"
                   "    validate_access()\n"
                   "    perform_action()\n")
        base = repo.commit("base")
        repo.write("feature.py",
                   "def operation():\n"
                   "    perform_action()\n"
                   "    validate_access()\n"
                   "    # parent_marker_is_present\n")
        earlier = repo.commit("parent reorders", agent="parent")
        repo.git("checkout", "-q", "-b", "target", base)
        repo.git("cherry-pick", earlier)
        repo.write("anchor.txt", "target advanced\n")
        target = repo.commit("target integrates the reorder", agent="other")
        repo.git("checkout", "-q", "-b", "head", target)
        repo.write("other.py", "unrelated();\n")
        head = repo.commit("head evolves elsewhere", agent="impl")
        self.assertTrue(review_authors._content_before_head(
            repo.root, target, earlier))
        self.assertEqual(review_authors._not_in_target(
            repo.root, [earlier], target, head=head), [])


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
