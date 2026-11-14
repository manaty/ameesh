# SPDX-License-Identifier: AGPL-3.0-only
"""#7324 — un commit signé au nom d'un agent qui ne tournait pas reste inconnu.

Constat nexlink #7272 (14/11 03:4xZ) : le gel refusé par author-families
disait « Return these commits to the assignee ». deepseek4 a réécrit sept
commits sous l'identité `claude1@ameesh.invalid` (auteur et committer)
pendant la pause de claude1 ; sans reçu exact, le repli du committer (#2721)
lisait la famille au dernier tour de claude1, vieux de trois heures, et ne
voyait que claude.

Attendu :
1. un commit de l'assigné sans reçu, sans tour qui le couvre et sans tour en
   cours, est d'origine inconnue (la porte refuse, la désignation attend) ;
2. le même commit couvert par un tour de l'assigné reste le sien ;
3. le refus de gel dit de refaire les commits, jamais de réécrire l'auteur.
"""
from __future__ import annotations

import os
import time

from ameesh import freeze_families, registry, review_authors, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
AUTHOR = "impl7324"                    # claude, l'assigné en pause


class ForgedReferenceTest(PgTestCase):

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        registry.upsert(self.db, AUTHOR, harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.repo.write("src/moves.py", "base\n")
        self.base = self.repo.commit("base")
        self.item = work.add(self.db, title="identité réécrite #7324",
                             app="ameesh", assignee=AUTHOR)
        self.lot = int(self.item["id"])
        self.repo.write("src/moves.py", "renumbered = 799\n")
        self.head = self.repo.commit("chore: renumber", agent=AUTHOR)
        self.stamp = float(self.repo.git("log", "-1", "--format=%ct", self.head))

    def turn(self, at: float):
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES (%s, 'claude', 's', to_timestamp(%s))", (AUTHOR, at))

    def reading(self):
        return review_authors.reading(
            self.db, work.get(self.db, self.lot), None, [self.head],
            self.repo.root, FAMILIES, strict=True, base=self.base,
            head=self.head, target=self.base)

    def test_a_commit_outside_any_turn_of_the_assignee_is_unknown(self):
        # Le dernier tour de l'assigné s'est fini trois heures avant.
        self.turn(self.stamp - 3 * 3600)
        _authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [self.head[:12]])

    def test_a_commit_inside_a_turn_of_the_assignee_stays_his(self):
        self.turn(self.stamp - 3 * 3600)
        self.turn(self.stamp + 60)
        authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"]: row["family"] for row in authors},
                         {AUTHOR: "claude"})

    def test_turn_absent_reads_a_turn_in_progress(self):
        self.turn(self.stamp - 3 * 3600)
        self.assertTrue(review_authors._turn_absent(self.db, AUTHOR, self.stamp))
        self.db.execute("UPDATE agent_registry SET status = 'running'"
                        " WHERE name = %s", (AUTHOR,))
        from unittest import mock
        with mock.patch.object(registry, "turn_in_progress", return_value=True):
            self.assertFalse(review_authors._turn_absent(
                self.db, AUTHOR, time.time()))

    def test_an_agent_without_any_turn_keeps_the_previous_reading(self):
        self.assertFalse(review_authors._turn_absent(self.db, AUTHOR, self.stamp))


class RefusalWordingTest(PgTestCase):

    def test_the_refusal_says_redo_never_rewrite(self):
        verdict = {"state": "short", "required": 2, "need": 2,
                   "eligible": ["codex"], "forbidden": ["claude", "deepseek"],
                   "families": ["claude", "codex", "deepseek"],
                   "authors": [], "manual_seats": [], "unknown_authors": [],
                   "assignee": "claude1", "why": "x"}
        text = freeze_families.message(None, verdict)
        self.assertIn("must redo these commits", text)
        self.assertIn("forbidden", text)
        self.assertNotIn("Return these commits", text)
