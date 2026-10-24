# SPDX-License-Identifier: AGPL-3.0-only
"""#3335 — l'exclusion d'un correcteur suit la survie de son contenu.

Un relecteur de premier tour dont le correctif ne survit plus dans la tête
gelée n'est plus un auteur de cette tête (#3220) ; sa famille doit donc
redevenir éligible, même quand le correctif a été retiré par un commit
postérieur de la même plage. Une ligne qui survit suffit à garder l'exclusion ;
une lecture de contenu impossible la garde aussi (le doute ne retire jamais
une famille).
"""
from __future__ import annotations

import os

from .test_2924_history_rewrite_provenance import _RewriteFixture


class _RemovedCorrectorFixture(_RewriteFixture):
    """Gel A avec le correctif de cx3, puis un commit qui le retire."""

    def remove_corrector(self, keep: str = ""):
        self.repo.write("src/ameesh/runner.py", "base\nlot\n" + keep)
        witness = os.path.join(self.repo.root, "tests", "test_runner.py")
        if os.path.exists(witness):
            os.remove(witness)
        self.head_b = self.repo.commit(
            "impl: retire la correction de cx3", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        self.milestone = {"id": int(self.freeze_b["id"]), "sha": self.head_b,
                          "actor": "impl", "at_ts": self.freeze_b.get("at_ts")}
        return self.head_b

    def reading_all(self):
        return self.reading(
            self.milestone,
            [self.author, self.fix, self.witness, self.head_b],
            self.base, self.head_b)


class RemovedCorrectorTest(_RemovedCorrectorFixture):
    """Aucune ligne du correctif ne survit : la famille codex est éligible."""

    def setUp(self):
        super().setUp()
        self.remove_corrector()

    def test_the_removed_corrector_leaves_the_authors(self):
        authors, unknown, correctors = self.reading_all()
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek"})
        self.assertEqual(correctors, [])

    def test_the_designation_can_designate_the_codex_family_again(self):
        from ameesh import review_designation as rd

        authors, _unknown, correctors = self.reading_all()
        out = rd.select(
            required=1, authors=authors, fixers=correctors,
            candidates=[rd.Candidate("cx3", "codex", True, 0)],
            families=("claude", "codex", "deepseek"))
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([row["family"] for row in out["reviewers"]],
                         ["codex"], out)

    def test_the_gate_accepts_the_codex_family_again(self):
        from ameesh import merge_queue, work

        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/runner.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/runner.py"]},
                           {"path": "tests/test_runner.py",
                            "class": "sensitive",
                            "rule": ["tests/test_runner.py"]}],
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


class PartlySurvivingCorrectorTest(_RemovedCorrectorFixture):
    """Une ligne propre du correctif survit : le correcteur reste exclu."""

    def setUp(self):
        super().setUp()
        self.remove_corrector(keep="if not admitted:\n")

    def test_one_surviving_line_keeps_the_corrector(self):
        authors, unknown, correctors = self.reading_all()
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertEqual([row["name"] for row in correctors], ["cx3"])


class UnreadableCorrectorContentTest(_RewriteFixture):
    """La lecture de contenu échoue : le correcteur reste exclu."""

    def test_an_unreadable_head_keeps_the_corrector(self):
        # Une tête illisible rend `_commit_survives` favorable (doute) : le
        # correcteur de la plage n'est pas retiré de l'exclusion.
        missing = "0" * 40
        freeze = self.freeze(self.head_a)
        milestone = {"id": int(freeze["id"]), "sha": missing,
                     "actor": "impl", "at_ts": freeze.get("at_ts")}
        authors, unknown, correctors = self.reading(
            milestone, [self.author, self.fix, self.witness],
            self.base, missing)
        self.assertEqual(unknown, [])
        self.assertIn("cx3", {row["name"] for row in authors})
        self.assertIn("cx3", {row["name"] for row in correctors})
