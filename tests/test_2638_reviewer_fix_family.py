# SPDX-License-Identifier: AGPL-3.0-only
"""#2638 — a reviewer's first-round correction does not make their family an author.

Witnesses of observation #2602: the frozen range carries the three families
(the lot author and the two designated reviewers), but the small corrections
pushed by the reviewers must not remove their families from the designation.
Above the §2 threshold (~50 changed lines) or for a design change, the reviewer
becomes an author of the head; a third party's commit always does.
"""
from __future__ import annotations

import dataclasses
import os

from ameesh import registry
from ameesh import review_designation as rd
from ameesh import review_designation_pg as pg

from .support import PgTestCase
from .test_2447_review_designation_activation import FEDERATION, _ActivationFixture, _Canon


class ReviewerFixFamilyTest(_ActivationFixture, PgTestCase):
    """The reference author is the assignee; a small reviewer fix keeps its family."""

    # -- helpers ----------------------------------------------------------
    def commit(self, actor, harness, text, path="src/ameesh/db.py"):
        """A new frozen head whose exact receipt names `actor` / `harness`."""
        self.repo.write(path, text)
        sha = self.repo.commit("%s: %s" % (actor, path))
        self.receipts(actor=actor, harness=harness, commits=[sha])
        self.frozen = sha
        return sha

    def authors(self):
        """The authors recorded by the latest designation mark."""
        mark = [pg.designation_of(note) for note in self.marks()
                if note.startswith(pg.MARK_PREFIX)][-1]
        return {row["name"]: row["family"] for row in mark.get("authors") or ()}

    def first_freeze(self):
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1", "r2"])
        return out

    def one_review_canon(self):
        """The #2602 policy: one independent review for `src/ameesh/**`."""
        canon = _Canon(os.path.join(self.tmp, "canon-one"),
                       federation=FEDERATION.replace("reviews: 2", "reviews: 1"))
        self.cfg = dataclasses.replace(self.cfg, canon=canon.root, canon_ref="HEAD")
        self.canon = canon

    def status(self, name, status):
        registry.upsert(self.db, name, harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status=status)

    # -- witnesses --------------------------------------------------------
    def test_2602_chain_keeps_a_reviewer_retired_by_a_later_freeze(self):
        # The exact #2602 sequence with one review: dev1 (Claude) reviews,
        # pushes its small fix, then a later freeze designates dev5 (DeepSeek)
        # and retires dev1 from the column. Both corrections are in the range
        # of the third freeze, where the old code forbade all three families.
        self.one_review_canon()
        _, first = self.freeze()
        self.assertEqual([r["name"] for r in first["reviewers"]], ["r1"])
        self.commit("r1", "claude", "lot\nr1 small fix\n")
        self.status("r1", "stopped")           # the later freeze cannot mail r1
        _, second = self.freeze()
        self.assertEqual(second["state"], rd.DESIGNATED, second)
        self.assertEqual([r["name"] for r in second["reviewers"]], ["r2"])
        self.commit("r2", "codex", "lot\nr1 small fix\nr2 small fix\n")
        self.status("r1", "idle")              # dev1 is back for the third freeze
        _, third = self.freeze()
        self.assertEqual(third["state"], rd.DESIGNATED, third)
        self.assertEqual([r["name"] for r in third["reviewers"]], ["r1"])
        self.assertEqual(self.authors(), {"author": "deepseek"})

    def test_2602_three_families_with_small_reviewer_fixes_still_designate(self):
        # #2602: cx2 (Codex) authors, dev1 (Claude) then dev5 (DeepSeek) push
        # their first-round correction; the next freeze must still designate.
        self.first_freeze()
        self.commit("r1", "claude", "lot\nr1 small fix\n")
        self.commit("r2", "codex", "lot\nr1 small fix\nr2 small fix\n")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1", "r2"])
        # the three families are in the range, but only the lot author is one
        self.assertEqual(self.authors(), {"author": "deepseek"})
        # the same reviewer-correctors are designated again on the new head
        self.assertEqual([r["family"] for r in out["reviewers"]],
                         ["claude", "codex"])

    def test_a_reviewer_fix_over_fifty_lines_makes_that_family_an_author(self):
        self.first_freeze()
        big = "lot\n" + "".join("extra %02d\n" % index for index in range(60))
        self.commit("r1", "claude", big)
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("forbidden claude, deepseek", out["why"])
        self.assertEqual(self.authors(), {"author": "deepseek", "r1": "claude"})

    def test_a_reviewer_design_change_excludes_the_family_even_when_small(self):
        # canon §2: a correction "> ~50 lignes ou de conception" goes back to
        # the author; the small size does not buy the exception.
        self.first_freeze()
        self.commit("r1", "claude", "study\n", path="docs/design/etudes/x.md")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("forbidden claude, deepseek", out["why"])
        self.assertEqual(self.authors(), {"author": "deepseek", "r1": "claude"})

    def test_a_third_party_commit_excludes_its_family(self):
        # A non-reviewer never gets the §2 first-round correction allowance:
        # their small commit makes their family an author's family.
        self.first_freeze()
        registry.upsert(self.db, "third", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("third",))
        self.commit("third", "claude", "lot\nthird small fix\n")
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.WAITING, out)
        self.assertIn("forbidden claude, deepseek", out["why"])
        self.assertEqual(self.authors(),
                         {"author": "deepseek", "third": "claude"})
