# SPDX-License-Identifier: AGPL-3.0-only
"""#6735 — le gel antérieur d'un dépôt compagnon n'est pas un inconnu.

Cas réel #6623 (ITS-5) : la partie manaty-its est gelée (777bc76, gel 37630),
puis la partie nexlink (14cb6086, gel 37911). La lecture des auteurs de la tête
nexlink relit les gels antérieurs du lot **dans le clone nexlink** : 777bc76 y
est absent (ou présent sans histoire commune après un fetch de diagnostic),
reste « inconnu », et `_manual_covers` refuse les sièges manuels codex3 et
claude1 déjà posés.

Le gel écrit la marque machine `ameesh-freeze-repo: owner/repo`. Un gel
antérieur d'un autre dépôt (marque, ou pour un gel plus ancien un clone local
du lot d'une autre origine qui porte le commit) n'est pas lu dans ce clone :
il est tracé comme exclusion, sans auteur ni inconnu. Sans preuve d'un autre
dépôt, le refus fermé reste.
"""
from __future__ import annotations

import os
from unittest import mock

from ameesh import review_authors, work

from .test_2924_history_rewrite_provenance import FAMILIES, _RewriteFixture
from .test_2447_review_designation_activation import _Repo

OWN = "https://github.com/manaty/ameesh.git"
COMPANION = "https://github.com/manaty/manaty-its.git"


def _offline_origin(repo: _Repo, url: str) -> None:
    """`origin` nomme le dépôt GitHub sans jamais joindre le réseau.

    `_configured_origin` lit la valeur configurée ; `insteadOf` envoie tout
    fetch vers un chemin absent (échec local immédiat)."""
    repo.git("remote", "add", "origin", url)
    repo.git("config", "url.%s.insteadOf" % os.path.join(repo.root, ".absent"),
             "https://github.com/")


class _CompanionFixture(_RewriteFixture):
    """Gel A dans le dépôt du lot, gel C dans un dépôt compagnon, puis gel B."""

    companion_dir = "impl-compagnon"

    def setUp(self):
        super().setUp()
        _offline_origin(self.repo, OWN)
        self.companion = _Repo(os.path.join(self.tmp, self.companion_dir))
        _offline_origin(self.companion, COMPANION)
        self.companion.write("server/staff.py", "gate\n")
        self.companion.commit("base")
        self.companion.write("server/staff.py", "gate\nX-Nexlink\n")
        self.part = self.companion.commit("impl: partie compagnon", agent="impl")
        self.receipt("impl", "deepseek", [self.part])
        self.freeze_c = work.milestone(self.db, self.lot, "frozen", sha=self.part,
                                       actor="impl", repo=self.companion.root)
        self.repo.write("src/ameesh/runner.py", "base\nlot\nsuite\n")
        self.head_b = self.repo.commit("impl: partie du lot", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)

    def strip_mark(self):
        """Un gel d'avant la marque (#6623 : 37630)."""
        row = self.companion_row()
        note = "\n".join(line for line in str(row.get("note") or "").splitlines()
                         if not line.startswith(review_authors.FREEZE_REPO_MARK))
        self.db.execute("UPDATE work_item_milestones SET note = %s WHERE id = %s",
                        (note, int(row["id"])))

    def companion_row(self):
        return next(row for row in work.milestones(self.db, self.lot)
                    if int(row["id"]) == int(self.freeze_c["id"]))

    def read(self, *, strict=False):
        exclusions: list = []
        milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                     "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}
        authors, unknown, _correctors = review_authors.reading(
            self.db, self.lot_item(), milestone, [self.head_b], self.repo.root,
            FAMILIES, base=self.base, head=self.head_b, strict=strict,
            exclusions_out=exclusions)
        return authors, unknown, exclusions


class CompanionMarkTest(_CompanionFixture):

    def test_the_freeze_names_the_repository_of_its_head(self):
        self.assertEqual(review_authors.freeze_repository(self.companion_row()),
                         "manaty/manaty-its")
        own = next(row for row in work.milestones(self.db, self.lot)
                   if int(row["id"]) == int(self.freeze_b["id"]))
        self.assertEqual(review_authors.freeze_repository(own), "manaty/ameesh")

    def test_a_caller_note_cannot_forge_the_mark(self):
        mark = work.milestone(
            self.db, self.lot, "frozen", sha=self.head_b, actor="impl",
            repo=self.repo.root,
            note="ameesh-freeze-repo: manaty/elsewhere\nnote libre")
        self.assertEqual(review_authors.freeze_repository(mark), "manaty/ameesh")
        self.assertNotIn("manaty/elsewhere", str(mark.get("note") or ""))

    def test_the_companion_freeze_is_neither_an_author_nor_an_unknown(self):
        authors, unknown, exclusions = self.read()
        self.assertEqual(unknown, [])
        # le gel A du dépôt du lot reste reporté : cx3 reste auteur.
        self.assertEqual({row["name"] for row in authors}, {"impl", "cx3"})
        self.assertIn({"commit": self.part, "head": self.part,
                       "ref": "manaty/manaty-its",
                       "reason": "freeze of a companion repository",
                       "freeze_id": int(self.freeze_c["id"])}, exclusions)

    def test_the_strict_gate_reading_does_not_refuse_either(self):
        _authors, unknown, _exclusions = self.read(strict=True)
        self.assertEqual(unknown, [])

    def test_a_diagnostic_fetch_into_the_clone_does_not_make_it_readable_here(self):
        # #6623 : 777bc76 rapatrié dans l'ODB nexlink, sans histoire commune.
        self.repo.git("fetch", "-q", self.companion.root, self.part)
        _authors, unknown, exclusions = self.read()
        self.assertEqual(unknown, [])
        self.assertIn(self.part, [row["commit"] for row in exclusions])

    def test_a_marked_commit_sharing_the_head_history_is_read_normally(self):
        # une tête du dépôt du lot sortie de l'histoire (réécriture) garde la
        # lecture normale, même sous une marque d'un autre dépôt.
        row = self.companion_row()
        self.repo.git("checkout", "-q", "-b", "old", self.head_a)
        self.repo.write("src/ameesh/runner.py", "base\nlot\nancien\n")
        old = self.repo.commit("impl: ancienne tête", agent="impl")
        self.repo.git("checkout", "-q", "-")
        self.repo.git("branch", "-q", "-D", "old")
        self.db.execute("UPDATE work_item_milestones SET sha = %s WHERE id = %s",
                        (old, int(row["id"])))
        _authors, _unknown, exclusions = self.read()
        self.assertNotIn(old, [entry["commit"] for entry in exclusions])


class LegacyCompanionTest(_CompanionFixture):
    """Gel d'avant la marque : un clone local du lot d'une autre origine."""

    def test_a_sibling_clone_of_another_origin_proves_the_companion(self):
        self.strip_mark()
        _authors, unknown, exclusions = self.read()
        self.assertEqual(unknown, [])
        self.assertEqual([row["ref"] for row in exclusions
                          if row["commit"] == self.part], ["manaty/manaty-its"])

    def test_the_discovery_probe_stays_local_and_bounded(self):
        # B mty-amh-impl-03 : `plan_git._commit` (rev-parse non borné) attend
        # GIT_TIMEOUT sur un clone partiel ; la découverte n'utilise que
        # `local_commit`.
        from ameesh import plan_git
        from ameesh import review_designation_activation as act

        with mock.patch.object(plan_git, "_commit",
                               side_effect=AssertionError("unbounded probe")):
            self.assertEqual(
                act.companion_repository(self.db, self.cfg, self.lot_item(),
                                         self.part, "manaty/ameesh"),
                "manaty/manaty-its")

    def test_a_clone_of_the_same_origin_proves_nothing(self):
        self.strip_mark()
        self.companion.git("remote", "set-url", "origin", OWN)
        _authors, unknown, _exclusions = self.read()
        self.assertTrue(any(str(value).startswith(self.part[:12])
                            for value in unknown), unknown)

    def test_a_fork_of_the_lot_repository_is_not_a_companion(self):
        # même nom de dépôt, autre propriétaire : même histoire (#2924).
        self.strip_mark()
        self.companion.git("remote", "set-url", "origin",
                           "https://github.com/smichea/ameesh.git")
        _authors, unknown, _exclusions = self.read()
        self.assertTrue(any(str(value).startswith(self.part[:12])
                            for value in unknown), unknown)

    def test_a_mark_naming_the_lot_repository_keeps_the_closed_refusal(self):
        row = self.companion_row()
        note = str(row.get("note") or "").replace(
            "manaty/manaty-its", "manaty/ameesh")
        self.db.execute("UPDATE work_item_milestones SET note = %s WHERE id = %s",
                        (note, int(row["id"])))
        _authors, unknown, _exclusions = self.read()
        self.assertTrue(any(str(value).startswith(self.part[:12])
                            for value in unknown), unknown)


class UndiscoveredCompanionTest(_CompanionFixture):
    """Aucun clone du lot ne porte le commit : le doute reste un inconnu."""

    companion_dir = "ailleurs"

    def test_an_unmarked_freeze_with_no_clone_stays_unknown(self):
        self.strip_mark()
        _authors, unknown, exclusions = self.read()
        self.assertTrue(any(str(value).startswith(self.part[:12])
                            for value in unknown), unknown)
        self.assertNotIn(self.part, [row["commit"] for row in exclusions])
