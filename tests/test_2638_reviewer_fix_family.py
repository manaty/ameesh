# SPDX-License-Identifier: AGPL-3.0-only
"""#2638/#2888 — a reviewer's correction authors the head, whatever its size.

Witnesses of observation #2602: the frozen range carries the three families
(the lot author and the two designated reviewers). Canon §2 (home#127) has no
size exception: every reviewer correction makes its author an author of the
head, even under the ~50-line threshold. The threshold only says whether the
reviewer should have pushed the correction or returned it to the author; it
never keeps a family eligible. A design change or a third party's commit also
makes its family an author's family.

Depuis #3742, quand ces corrections ne laissent plus assez de familles
indépendantes pour les avis requis, le gel refuse la tête (« freeze refused
(author-families) »), nomme les commits à renvoyer à l'assigné et n'écrit
aucune marque de désignation : la tête ne part plus en attente illisible.
"""
from __future__ import annotations

import dataclasses
import os
import tempfile
from unittest import TestCase

from ameesh import registry
from ameesh import review_authors, review_designation_activation as act
from ameesh import review_designation as rd
from ameesh import review_designation_pg as pg
from ameesh import work

from .support import PgTestCase
from .test_2447_review_designation_activation import (
    FEDERATION, _ActivationFixture, _Canon, _Repo,
)


class DesignPathTest(TestCase):
    def test_git_quoted_design_paths_still_count_as_design(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as root:
            repo = _Repo(root)
            repo.write("base", "base\n")
            repo.commit("base")
            for path in ("docs/design/étude.md", "docs/design/tab\tline\n.md"):
                with self.subTest(path=path):
                    repo.write(path, "design\n")
                    sha = repo.commit("design")
                    self.assertEqual(review_authors.change(root, sha), (1, True))



class ReviewerFixFamilyTest(_ActivationFixture, PgTestCase):
    """The reference author is the assignee; a reviewer fix authors the head."""

    # -- helpers ----------------------------------------------------------
    def commit(self, actor, harness, text, path="src/ameesh/mail.py"):
        """A new frozen head whose exact receipt names `actor` / `harness`.

        `mail.py` reste sensible hors du périmètre critique : #1515 y a fait
        entrer `work.py`, qui servait de chemin neutre à ces témoins."""
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

    def agent(self, name, harness, status="idle"):
        """Another agent of a family, eligible when the corrector is not."""
        registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                        cwd=self.repo.root, status=status)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        (name,))

    # -- witnesses --------------------------------------------------------
    def test_2602_chain_keeps_a_reviewer_retired_by_a_later_freeze(self):
        # The exact #2602 sequence with one review: dev1 (Claude) reviews and
        # pushes its small fix. The next freeze treats dev1 as an author of the
        # head (its family is excluded), so dev5 (DeepSeek) takes the other
        # family's seat. Both corrections are in the range of the third freeze:
        # the three families are authors and no seat can be proven independent.
        self.one_review_canon()
        _, first = self.freeze()
        self.assertEqual([r["name"] for r in first["reviewers"]], ["r1"])
        r1_sha = self.commit("r1", "claude", "lot\nr1 small fix\n")
        self.status("r1", "stopped")           # the later freeze cannot mail r1
        _, second = self.freeze()
        self.assertEqual(second["state"], rd.DESIGNATED, second)
        self.assertEqual([r["name"] for r in second["reviewers"]], ["r2"])
        # #2888: r1 is an author of the head even for a small correction.
        self.assertEqual(self.authors(), {"author": "deepseek", "r1": "claude"})
        r2_sha = self.commit("r2", "codex", "lot\nr1 small fix\nr2 small fix\n")
        self.status("r1", "idle")              # dev1 is back for the third freeze
        # #2739/#2888: r1 and r2 are both authors now; no independent family
        # remains, even for another Claude agent: the correction was not
        # receivable and must go back to the author. Since #3742 the freeze
        # itself refuses the head, names both commits and writes no third
        # designation mark.
        self.agent("r1b", "claude")
        marks = self.marks()
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("freeze refused (author-families)", text)
        self.assertIn("0 eligible family(ies)", text)
        self.assertIn("forbidden: claude, codex, deepseek", text)
        self.assertIn(r1_sha[:12], text)
        self.assertIn(r2_sha[:12], text)
        self.assertIn("(reviewer correction)", text)
        self.assertEqual(self.marks(), marks)

    def test_2602_three_families_with_small_reviewer_fixes_refuse_the_freeze(self):
        # #2602: cx2 (Codex) authors, dev1 (Claude) then dev5 (DeepSeek) push
        # their first-round correction. Canon §2 has no size exception: the
        # three families are authors of the head and no independent seat
        # remains for two required opinions. Since #3742 the freeze refuses
        # the head instead of freezing an unreadable waiting designation.
        self.first_freeze()
        r1_sha = self.commit("r1", "claude", "lot\nr1 small fix\n")
        r2_sha = self.commit("r2", "codex", "lot\nr1 small fix\nr2 small fix\n")
        self.agent("r1b", "claude")
        self.agent("r2b", "codex")
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("2 required review(s)", text)
        self.assertIn("0 eligible family(ies)", text)
        self.assertIn("forbidden: claude, codex, deepseek", text)
        self.assertIn(r1_sha[:12], text)
        self.assertIn(r2_sha[:12], text)

    def test_a_reviewer_fix_over_fifty_lines_makes_that_family_an_author(self):
        self.first_freeze()
        big = "lot\n" + "".join("extra %02d\n" % index for index in range(60))
        sha = self.commit("r1", "claude", big)
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("1 eligible family(ies)", text)
        self.assertIn("forbidden: claude, deepseek", text)
        self.assertIn(sha[:12], text)

    def test_a_reviewer_design_change_excludes_the_family_even_when_small(self):
        # canon §2: a correction "> ~50 lignes ou de conception" goes back to
        # the author; the small size does not buy the exception.
        self.first_freeze()
        sha = self.commit("r1", "claude", "study\n",
                          path="docs/design/etudes/x.md")
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("1 eligible family(ies)", text)
        self.assertIn("forbidden: claude, deepseek", text)
        self.assertIn(sha[:12], text)

    def test_a_third_party_commit_excludes_its_family(self):
        # A non-reviewer never gets the §2 first-round correction allowance:
        # their small commit makes their family an author's family.
        self.first_freeze()
        registry.upsert(self.db, "third", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("third",))
        sha = self.commit("third", "claude", "lot\nthird small fix\n")
        with self.assertRaises(work.WorkError) as caught:
            self.freeze()
        text = str(caught.exception)
        self.assertIn("1 eligible family(ies)", text)
        self.assertIn("forbidden: claude, deepseek", text)
        self.assertIn(sha[:12], text)
