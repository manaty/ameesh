# SPDX-License-Identifier: AGPL-3.0-only
"""#3220 — le contenu d'un gel antérieur supprimé ne garde pas son auteur.

Cas réel Nexlink #3216 : le gel #16865 (`3e9a87709`) portait un document de
relecture de codex2 (`b72181370`, `docs/review/…`). Après rebase, la tête
`b2f9b52f5` (gel #16874) ne contient plus ce fichier ; les reçus exacts de la
plage courante n'en parlent plus. Le report #2924 gardait pourtant codex2
comme auteur de la tête, donc sa famille restait exclue et la porte n'avait
plus aucune famille éligible.

La règle est la survie du **contenu** (#2977), pas l'ascendance du gel : un
auteur d'un gel antérieur n'est reporté que si une partie de ce qu'il a écrit
subsiste dans la tête gelée. Un fichier supprimé (ou dont toutes les lignes
significatives ont disparu) ne le garde pas ; un contenu identique réécrit par
un autre le garde (#2924 inchangé), et un déplacement aussi (#2924 B8).
"""
from __future__ import annotations

from ameesh import merge_queue, review_authors, work

from .test_2924_history_rewrite_provenance import _RewriteFixture

DOC = "docs/review/2026-10-23-3211-codex2-r2.md"
DOC_TEXT = ("# #3211 — avis Codex R2 : OK\n"
            "ligne unique du relecteur codex2 sur la garde DSN\n"
            "seconde ligne propre au document de relecture\n")


class _DeletedFileFixture(_RewriteFixture):
    """Gel A : l'auteur deepseek puis un document (fichier) de codex2."""

    def _build_head_a(self):
        self.repo.write("src/ameesh/runner.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/runner.py", "base\nlot\n")
        self.author = self.repo.commit("impl: lot", agent="impl")
        self.receipt("impl", "deepseek", [self.author])
        self.repo.write(DOC, DOC_TEXT)
        self.review_doc = self.repo.commit("cx3: avis #3211", agent="cx3")
        self.receipt("cx3", "codex", [self.review_doc])
        self.head_a = self.review_doc
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)


class DeletedPriorContentTest(_DeletedFileFixture):
    """La tête B repart du commit de l'auteur : le document de codex2 est absent."""

    def setUp(self):
        super().setUp()
        # Le rebase rejoue le commit de l'auteur sur la base, sans le document.
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py", "base\nlot\n")
        self.head_b = self.repo.commit("impl: lot rejoué", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_deleted_file_does_not_carry_its_author(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})
        self.assertEqual(correctors, [])

    def test_the_gate_can_read_the_codex_family_again(self):
        # codex2 n'est plus auteur : la famille codex redevient éligible, et
        # deux avis de familles distinctes (codex + claude) passent la porte.
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("cx3", "dev1"):
            self.verdict(actor, self.head_b)
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertTrue(out["ok"], out["why"])
        self.assertEqual({entry["actor"] for entry in out["accepted"]},
                         {"cx3", "dev1"})
        self.assertNotIn("cx3", {row["name"] for row in out["authors"]})


class IdenticalContentStillCarriedTest(_DeletedFileFixture):
    """Témoin #2924 : un contenu identique réécrit par un autre garde l'auteur."""

    def setUp(self):
        super().setUp()
        # Même arbre que le gel A, commit unique signé par l'auteur : squash.
        tree = self.repo.git("rev-parse", self.head_a + "^{tree}")
        parent = self.repo.git("rev-parse", self.head_a + "^")
        self.head_b = self.squash(tree, parent, "impl: contenu consolidé")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_corrector_stays_an_author(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})


class _ConflictedFixture(_RewriteFixture):
    """La cible avancée et le gel antérieur modifient la même ligne : conflit."""

    def _build_head_a(self):
        self.repo.write("shared.txt", "line one\nline two\n")
        self.base = self.repo.commit("base")
        self.repo.write("shared.txt", "line one\nA side\n")
        self.author = self.repo.commit("impl: côté A", agent="impl")
        self.receipt("impl", "deepseek", [self.author])
        self.repo.write(DOC, DOC_TEXT)
        self.review_doc = self.repo.commit("cx3: avis #3211", agent="cx3")
        self.receipt("cx3", "codex", [self.review_doc])
        self.head_a = self.review_doc
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("shared.txt", "line one\nB side\n")
        self.target = self.repo.commit("main: côté B")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)


class ConflictedRewriteTest(_ConflictedFixture):
    """Le conflit n'est pas une preuve de conservation : le contenu décide."""

    def setUp(self):
        super().setUp()
        # Le rebase résout le conflit côté A mais ne rejoue pas le document.
        self.repo.git("reset", "--hard", self.target)
        self.repo.write("shared.txt", "line one\nA side\n")
        self.head_b = self.repo.commit("impl: résolution côté A", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_conflicting_merge_is_not_taken_as_preservation(self):
        self.assertIsNone(review_authors._merged_tree(
            self.repo.root, self.head_a, self.target))
        self.assertFalse(review_authors._freeze_content_preserved(
            self.repo.root, self.head_a, self.head_b, self.target))

    def test_the_deleted_file_does_not_carry_its_author(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.target, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})
        self.assertEqual(correctors, [])


class _SurvivingRemovalFixture(_RewriteFixture):
    """Gel A : le correcteur retire une garde et ajoute un document."""

    def _build_head_a(self):
        self.repo.write("src/ameesh/gate.py",
                        "allow_all_users_without_check()\n"
                        "require_session_identity()\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/gate.py", "require_session_identity()\n")
        self.repo.write(DOC, DOC_TEXT)
        self.review_doc = self.repo.commit(
            "cx3: retire la garde et documente", agent="cx3")
        self.receipt("cx3", "codex", [self.review_doc])
        self.head_a = self.review_doc
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)


class SurvivingRemovalTest(_SurvivingRemovalFixture):
    """B1 (revue cx2) : la suppression du correcteur subsiste, son document non."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/gate.py", "require_session_identity()\n")
        self.head_b = self.repo.commit(
            "impl: garde retirée, document omis", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_surviving_removal_keeps_the_corrector(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})

    def test_the_gate_refuses_the_corrector_with_a_surviving_removal(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/gate.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/gate.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("cx3", "dev1"):
            self.verdict(actor, self.head_b)
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"], out)
        self.assertIn("cx3", {row["name"] for row in out["authors"]})


class EditedThenDeletedTest(_SurvivingRemovalFixture):
    """B2 (revue dev2) : l'édition d'un fichier ensuite supprimé ne le garde pas."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.git("rm", "-q", "src/ameesh/gate.py")
        self.head_b = self.repo.commit(
            "impl: fichier édité puis supprimé", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_edit_in_the_deleted_file_does_not_carry_the_corrector(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})
        self.assertEqual(correctors, [])


class DeletedPriorContentCommitTest(_DeletedFileFixture):
    """La preuve de contenu, commit par commit, sur le fichier supprimé."""

    def test_the_deleted_document_does_not_survive(self):
        self.repo.git("reset", "--hard", self.author)
        self.assertFalse(review_authors._commit_survives(
            self.repo.root, self.author, self.review_doc, {}, self.base))


class _MixedCopyFixture(_RewriteFixture):
    """La garde copiée par le correcteur existe dans policy.py et dans gate.py."""

    def _build_head_a(self):
        self.repo.write("src/ameesh/policy.py",
                        "require_session_identity()\n")
        self.repo.write("src/ameesh/gate.py", "# gate\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/gate.py",
                        "# gate\nrequire_session_identity()\n")
        self.repo.write(DOC, DOC_TEXT)
        self.review_doc = self.repo.commit(
            "cx3: copie la garde et documente", agent="cx3")
        self.receipt("cx3", "codex", [self.review_doc])
        self.head_a = self.review_doc
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)


class MixedCopyTest(_MixedCopyFixture):
    """B/P1 cx2 : une copie encore à son chemin garde l'auteur (#2977)."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/gate.py",
                        "# gate\nrequire_session_identity()\n")
        self.head_b = self.repo.commit(
            "impl: copie conservée, document omis", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_copy_still_at_its_path_keeps_the_corrector(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})

    def test_the_gate_refuses_the_corrector_with_a_surviving_copy(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/gate.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/gate.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("cx3", "dev1"):
            self.verdict(actor, self.head_b)
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"], out)
        self.assertIn("cx3", {row["name"] for row in out["authors"]})


class _WholeFileDeletionFixture(_RewriteFixture):
    """Le correcteur supprime un fichier entier et ajoute un document."""

    def _build_head_a(self):
        self.repo.write("src/ameesh/legacy_bypass.py",
                        "allow_all_users_without_check()\n")
        self.repo.write("src/ameesh/gate.py", "# gate\n")
        self.base = self.repo.commit("base")
        self.repo.git("rm", "-q", "src/ameesh/legacy_bypass.py")
        self.repo.write(DOC, DOC_TEXT)
        self.review_doc = self.repo.commit(
            "cx3: retire le contournement et documente", agent="cx3")
        self.receipt("cx3", "codex", [self.review_doc])
        self.head_a = self.review_doc
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)


class DeletedWholeFileTest(_WholeFileDeletionFixture):
    """B/P1 dev2 : la suppression du fichier par le correcteur subsiste."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.git("rm", "-q", "src/ameesh/legacy_bypass.py")
        self.head_b = self.repo.commit(
            "impl: suppression conservée, document omis", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_corrector_deletion_still_counts(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})


class _InheritedHeadingFixture(_RewriteFixture):
    """Un autre document de la base porte la ligne d'en-tête du document ajouté."""

    def _build_head_a(self):
        self.repo.write("src/ameesh/runner.py", "base\n")
        self.repo.write("docs/review/other.md",
                        "# #3211 — avis Codex R2 : OK\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/runner.py", "base\nlot\n")
        self.author = self.repo.commit("impl: lot", agent="impl")
        self.receipt("impl", "deepseek", [self.author])
        self.repo.write(DOC, DOC_TEXT)
        self.review_doc = self.repo.commit("cx3: avis #3211", agent="cx3")
        self.receipt("cx3", "codex", [self.review_doc])
        self.head_a = self.review_doc
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)


class InheritedHeadingInDeletedDocTest(_InheritedHeadingFixture):
    """B3 garde #3216 : l'en-tête hérité ne garde pas l'auteur du doc supprimé."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py", "base\nlot\n")
        self.head_b = self.repo.commit(
            "impl: document omis", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_inherited_heading_does_not_carry_the_deleted_doc_author(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})
        self.assertEqual(correctors, [])


class MovedRemovalTest(_SurvivingRemovalFixture):
    """B/P1 cx2 : le fichier corrigé est déplacé, le retrait subsiste."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("relocated/gate.py", "require_session_identity()\n")
        self.repo.git("rm", "-q", "src/ameesh/gate.py")
        self.head_b = self.repo.commit(
            "impl: garde corrigée déplacée, document omis", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_moved_file_keeps_the_surviving_removal(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})

    def test_the_gate_refuses_the_corrector_with_a_moved_removal(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "relocated/gate.py",
                            "class": "sensitive",
                            "rule": ["relocated/gate.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("cx3", "dev1"):
            self.verdict(actor, self.head_b)
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"], out)
        self.assertIn("cx3", {row["name"] for row in out["authors"]})


class _RegroupedRemovalFixture(_SurvivingRemovalFixture):
    """B/P1 cx2 (2e témoin B5) : le chemin de reprise existe dès la base."""

    def _build_head_a(self):
        self.repo.write("src/ameesh/gate.py",
                        "allow_all_users_without_check()\n"
                        "require_session_identity()\n")
        # L'ancienne implémentation vit déjà au chemin de reprise : Git verra
        # une modification de ce chemin, pas un renommage depuis gate.py.
        self.repo.write("relocated/gate.py",
                        "allow_all_users_without_check()\n"
                        "require_session_identity()\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/gate.py", "require_session_identity()\n")
        self.repo.write(DOC, DOC_TEXT)
        self.review_doc = self.repo.commit(
            "cx3: retire la garde et documente", agent="cx3")
        self.receipt("cx3", "codex", [self.review_doc])
        self.head_a = self.review_doc
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)


class RegroupedRemovalTest(_RegroupedRemovalFixture):
    """B/P1 cx2 : contenu corrigé repris dans un chemin existant, sans `R`."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("relocated/gate.py",
                        "require_session_identity()\n")
        self.repo.git("rm", "-q", "src/ameesh/gate.py")
        self.head_b = self.repo.commit(
            "impl: garde corrigée regroupée, document omis", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_git_sees_no_rename_for_the_regrouped_content(self):
        # Le témoin ne vaut que si Git ne classe pas le déplacement en `R`.
        status = self.repo.git("diff", "--name-status", "-M",
                               self.review_doc, self.head_b)
        kinds = {line.split("\t", 1)[0][:1]
                 for line in status.splitlines() if line}
        self.assertNotIn("R", kinds)

    def test_the_regrouped_removal_keeps_the_corrector(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})

    def test_the_gate_refuses_the_corrector_with_a_regrouped_removal(self):
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "relocated/gate.py",
                            "class": "sensitive",
                            "rule": ["relocated/gate.py"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        for actor in ("cx3", "dev1"):
            self.verdict(actor, self.head_b)
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)
        self.assertFalse(out.get("error"), out)
        self.assertFalse(out["ok"], out["why"])
        self.assertEqual([entry["actor"] for entry in out["accepted"]],
                         ["dev1"], out)
        self.assertIn("cx3", {row["name"] for row in out["authors"]})


class _RegroupedCopyFixture(_MixedCopyFixture):
    """B/P1 cx2 (2e témoin B5) : la copie est reprise au chemin existant."""

    def _build_head_a(self):
        self.repo.write("src/ameesh/policy.py",
                        "require_session_identity()\n")
        self.repo.write("src/ameesh/gate.py", "# gate\n")
        self.repo.write("relocated/gate.py", "# gate\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/gate.py",
                        "# gate\nrequire_session_identity()\n")
        self.repo.write(DOC, DOC_TEXT)
        self.review_doc = self.repo.commit(
            "cx3: copie la garde et documente", agent="cx3")
        self.receipt("cx3", "codex", [self.review_doc])
        self.head_a = self.review_doc
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)


class RegroupedCopyTest(_RegroupedCopyFixture):
    """B/P1 cx2 : la copie survit dans le fichier existant repris, sans `R`."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("relocated/gate.py",
                        "# gate\nrequire_session_identity()\n")
        self.repo.git("rm", "-q", "src/ameesh/gate.py")
        self.head_b = self.repo.commit(
            "impl: copie regroupée, document omis", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_git_sees_no_rename_for_the_regrouped_copy(self):
        status = self.repo.git("diff", "--name-status", "-M",
                               self.review_doc, self.head_b)
        kinds = {line.split("\t", 1)[0][:1]
                 for line in status.splitlines() if line}
        self.assertNotIn("R", kinds)

    def test_the_regrouped_copy_keeps_the_corrector(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})


class MovedCopyTest(_MixedCopyFixture):
    """B/P1 cx2 : la copie héritée est déplacée avec le fichier corrigé."""

    def setUp(self):
        super().setUp()
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("relocated/gate.py",
                        "# gate\nrequire_session_identity()\n")
        self.repo.git("rm", "-q", "src/ameesh/gate.py")
        self.head_b = self.repo.commit(
            "impl: copie déplacée, document omis", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}

    def test_the_moved_copy_keeps_the_corrector(self):
        authors, unknown, correctors = self.reading(
            self.milestone, [self.head_b], self.base, self.head_b)
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {row["name"] for row in correctors})
