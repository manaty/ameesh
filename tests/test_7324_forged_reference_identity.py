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

from ameesh import freeze_families, registry, review_authors, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo

FAMILIES = ("claude", "codex", "deepseek")
AUTHOR = "impl7324"                    # claude, l'assigné en pause
FORGER = "dsk7324"                     # deepseek, l'exécutant réel


class _Range(PgTestCase):

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



class LongTurnTest(_Range):
    """B 7bd2c991/75d35268 : un tour long ou en cours ne ferme rien.

    La garde par fenêtre de tours est retirée : seul le reçu du gel prouve un
    autre acteur. Un commit de l'assigné loin de tout tour fini reste le sien."""

    def test_a_commit_far_from_any_finished_turn_stays_the_assignee(self):
        self.turn(self.stamp - 3 * 3600)
        authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"] for row in authors}, {AUTHOR})

    def test_a_turn_ending_forty_minutes_later_keeps_the_commit(self):
        self.turn(self.stamp - 3 * 3600)
        self.turn(self.stamp + 40 * 60)
        authors, unknown, _correctors = self.reading()
        self.assertEqual(unknown, [])
        self.assertEqual({row["name"] for row in authors}, {AUTHOR})


class FreezerReceiptTest(_Range):
    """Témoin (b) du lot : commits au nom de X, gel prouvé de Y → {X, Y}.

    X est en tour au même instant (tours simultanés, date recopiée) : la
    garde du journal ne voit rien, seul le reçu du gel prouve Y."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, FORGER, harness="deepseek", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.turn(self.stamp + 60)     # X tournait vraiment à cet instant

    def freeze_by(self, actor, harness):
        ops = __import__("ameesh.storage", fromlist=["of"]).of(self.db).work
        row = ops.add_milestone(self.lot, "frozen", sha=self.head, actor=actor,
                                verdict=None, note="")
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, 's', 't', %s, %s)",
            ("milestone:%d" % int(row["id"]), self.head, actor, harness, self.lot))
        return dict(row)

    def read_at(self, milestone):
        return review_authors.reading(
            self.db, work.get(self.db, self.lot), milestone, [self.head],
            self.repo.root, FAMILIES, strict=True, base=self.base,
            head=self.head, target=self.base)

    def families(self, authors):
        out = {}
        for row in authors:
            out[row["name"]] = set(row.get("families") or [row["family"]])
        return out

    def test_a_freeze_by_another_family_makes_it_an_author(self):
        authors, unknown, _ = self.read_at(self.freeze_by(FORGER, "deepseek"))
        self.assertEqual(unknown, [])
        found = self.families(authors)
        self.assertEqual(found.get(FORGER), {"deepseek"}, authors)
        self.assertIn(AUTHOR, found)

    def test_a_freeze_by_the_assignee_adds_nobody(self):
        authors, unknown, _ = self.read_at(self.freeze_by(AUTHOR, "claude"))
        self.assertEqual(unknown, [])
        self.assertEqual(set(self.families(authors)), {AUTHOR})

    def test_an_empty_pre_insert_proof_never_reads_a_provisional_milestone(self):
        # #3948 : `{}` = gel hors tour ; l'id provisoire peut nommer le jalon
        # d'un autre lot, il ne se relit pas.
        other = self.freeze_by(FORGER, "deepseek")
        authors, _unknown, _ = review_authors.reading(
            self.db, work.get(self.db, self.lot), other, [self.head],
            self.repo.root, FAMILIES, strict=True, base=self.base,
            head=self.head, target=self.base, milestone_proof={})
        self.assertNotIn(FORGER, self.families(authors))

    def test_the_freezer_carries_no_turn_journal_trace(self):
        authors, _unknown, _ = review_authors.reading(
            self.db, work.get(self.db, self.lot), self.freeze_by(FORGER, "deepseek"),
            [self.head], self.repo.root, FAMILIES, strict=True, base=self.base,
            head=self.head, target=self.base, trace=True)
        row = next(row for row in authors if row["name"] == FORGER)
        self.assertNotIn("proof", row)

    def test_the_pre_insert_proof_counts_as_the_freeze_receipt(self):
        authors, _unknown, _ = review_authors.reading(
            self.db, work.get(self.db, self.lot), None, [self.head],
            self.repo.root, FAMILIES, strict=True, base=self.base,
            head=self.head, target=self.base,
            milestone_proof={"actor": FORGER, "harness": "deepseek"})
        self.assertEqual(self.families(authors).get(FORGER), {"deepseek"})

    def test_an_exact_commit_receipt_does_not_add_the_freezer(self):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, 's', 't', 'claude', %s)",
            ("commit:" + self.head, self.head, AUTHOR, self.lot))
        authors, _unknown, _ = self.read_at(self.freeze_by(FORGER, "deepseek"))
        self.assertNotIn(FORGER, self.families(authors))


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

    def test_an_empty_assignee_reads_once(self):
        verdict = {"state": "short", "required": 2, "need": 2,
                   "eligible": [], "forbidden": ["claude"],
                   "families": ["claude", "codex"], "authors": [],
                   "manual_seats": [], "unknown_authors": [],
                   "assignee": "", "why": "x"}
        text = freeze_families.message(None, verdict)
        self.assertIn("The assignee (or", text)
        self.assertNotIn("The the", text)
