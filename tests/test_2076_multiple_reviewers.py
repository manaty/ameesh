# SPDX-License-Identifier: AGPL-3.0-only
"""Multiple designations, frozen-head verdicts and rolling runner upgrades."""
from __future__ import annotations

import concurrent.futures
import threading
import unittest
from pathlib import Path

from .support import PgTestCase, REPO
from ameesh import alert_briefs, progress, stagnation, storage, work


class ReviewWaitingTest(unittest.TestCase):
    def marks(self, *verdicts, frozen="a" * 40):
        item = {"state": "qa", "assignee": "author", "reviewer": "second",
                "reviewers": ["first", "second"]}
        rows = [{"kind": "frozen", "at_ts": 1, "sha": frozen}]
        rows += [{"kind": "verdict", "at_ts": i + 2, "actor": actor,
                  "verdict": verdict, "sha": sha}
                 for i, (actor, verdict, sha) in enumerate(verdicts)]
        return item, progress._work_item_milestones(item, [], [], rows)

    def test_each_reviewer_owes_a_current_verdict(self):
        a = "a" * 40
        item, marks = self.marks(("first", "ok", a))
        self.assertEqual(stagnation.waiting(item, marks)["who"], "second")
        self.assertEqual(progress.lot_state("qa", marks), "review")
        item, marks = self.marks(("first", "ok", a), ("second", "ok", "b" * 40))
        self.assertEqual(stagnation.waiting(item, marks)["who"], "second")
        item, marks = self.marks(("first", "ok", a), ("second", "ok", a))
        self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")
        self.assertEqual(progress.lot_state("qa", marks), "approved")
        item, marks = self.marks(("first", "ok", a), ("second", "ok", a), frozen="c" * 40)
        self.assertEqual(stagnation.waiting(item, marks)["who"], "first, second")

    def test_later_go_does_not_erase_another_reviewers_blocker(self):
        a = "a" * 40
        item, marks = self.marks(("first", "blocked", a), ("second", "ok", a))
        self.assertEqual(stagnation.waiting(item, marks)["what"], "fix")
        self.assertEqual(progress.lot_state("qa", marks), "review")
        item, marks = self.marks(("first", "blocked", a), ("second", "ok", a),
                                 ("first", "ok", a))
        self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")

    def test_both_pending_reviewers_receive_the_alert(self):
        alert = {"type": "stale_lot", "agent": "author", "lot": 42,
                 "title": "review", "lot_state": "qa", "value": 25000,
                 "project": "ameesh", "orchestrators": [],
                 "waiting": {"what": "verdict", "who": "first, second",
                             "label": "verdict by first, second"}}
        briefs = alert_briefs.briefs(alert, [{"name": n, "team": "ameesh"}
                                            for n in ("author", "first", "second")])
        self.assertIn("first", briefs)
        self.assertIn("second", briefs)

    def test_waiver_is_not_a_reviewer_and_light_lots_can_merge(self):
        from ameesh import lot_class
        item, marks = self.marks()
        self.assertEqual(stagnation.waiting(item, marks, review_class=lot_class.LIGHT)["what"],
                         "merge")
        item.update(reviewer=work.REVIEWER_NONE, reviewers=[work.REVIEWER_NONE])
        marks = progress._work_item_milestones(item, [], [], [])
        self.assertEqual(marks["reviewers"], [])
        self.assertEqual(marks["pending_reviewers"], [])
        self.assertFalse(marks["verdict_required"])
        self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")
        self.assertIsNone(progress.build_lot(dict(item, id=42), [], [])["reviewer"])


class MultipleReviewersTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("author", "first", "second", "chief"):
            self.register(name, "codex", cwd=self.tmp)
        self.lot = work.add(self.db, title="two reviews", assignee="author", app="ameesh")
        work.move(self.db, self.lot["id"], "build")
        work.move(self.db, self.lot["id"], "qa")

    def designate(self, name, **kwargs):
        return storage.of(self.db).work.designate_reviewer(self.lot["id"], name,
                                                          actor="chief", **kwargs)

    def test_accumulation_cli_removal_and_assignment(self):
        self.designate("first")
        self.designate("second")
        self.designate("second")
        row = work.get(self.db, self.lot["id"])
        self.assertEqual(row["reviewers"], ["first", "second"])
        shown = self.mesh("work", "show", str(self.lot["id"]))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("reviewer : first, second", shown.stdout)
        self.assertIn("verdict by first, second", shown.stdout)
        proc = self.mesh("work", "reviewer", str(self.lot["id"]), "first", "--remove",
                         "--actor", "chief", env=self.env(AMEESH_ALERT_ORCHESTRATORS="chief"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])
        self.assertTrue(any("reviewer first removed" in e["note"]
                            for e in work.events(self.db, self.lot["id"])))
        # Assignment uses the legacy SQL path; the trigger removes the new
        # author atomically and keeps all unrelated designations.
        self.designate("first")
        work.assign(self.db, self.lot["id"], "second", actor="chief", cfg=self.cfg)
        row = work.get(self.db, self.lot["id"])
        self.assertEqual(row["reviewers"], ["first"])
        self.assertEqual(row["reviewer"], "first")

    def test_legacy_writer_accumulates_and_cannot_reintroduce_author(self):
        for name in ("first", "second", "first"):
            self.db.execute("UPDATE work_items SET reviewer = %s WHERE id = %s",
                            (name, self.lot["id"]))
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["first", "second"])
        self.db.execute("UPDATE work_items SET assignee = 'first' WHERE id = %s",
                        (self.lot["id"],))
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])
        self.assertIsNone(self.designate("first"))

    def test_global_removal_and_waiver_replace_all_designations(self):
        for flag in ("--remove", "--no-verdict"):
            with self.subTest(flag=flag):
                self.designate("first")
                self.designate("second")
                proc = self.mesh("work", "reviewer", str(self.lot["id"]), flag,
                                 "--actor", "chief",
                                 env=self.env(AMEESH_ALERT_ORCHESTRATORS="chief"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                row = work.get(self.db, self.lot["id"])
                self.assertEqual(row["reviewers"],
                                 [work.REVIEWER_NONE] if flag == "--no-verdict" else [])
                marks = progress._work_item_milestones(row, [], [], [])
                self.assertEqual(marks["pending_reviewers"], [])
                self.assertEqual(marks["verdict_required"], flag != "--no-verdict")
                # A fresh designation clears the waiver, and accumulates again.
                self.designate("first")
                self.designate("second")
                self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"],
                                 ["first", "second"])

    def test_legacy_waiver_is_replaced_by_a_new_designation(self):
        self.designate("first")
        self.designate("second")
        self.db.execute("UPDATE work_items SET reviewer = %s WHERE id = %s",
                        (work.REVIEWER_NONE, self.lot["id"]))
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], [work.REVIEWER_NONE])
        self.db.execute("UPDATE work_items SET reviewer = 'first' WHERE id = %s",
                        (self.lot["id"],))
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["first"])

    def test_delegation_and_return_preserve_other_reviewers(self):
        self.designate("first")
        self.designate("second")
        delegated = storage.of(self.db).work.delegate(
            self.lot["id"], "first", current="author", delegated_by="author",
            within_s=1800, note="delegate", actor="chief")
        self.assertEqual(delegated["item"]["reviewers"], ["second"])
        self.designate("author")
        returned = work.expire_delegations(self.db, now=delegated["delegation"]["due_ts"] + 1)
        self.assertEqual(returned[0]["outcome"], "rendue")
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])

    def test_removal_keeps_the_designation_authority_check(self):
        self.designate("first")
        proc = self.mesh("work", "reviewer", str(self.lot["id"]), "first", "--remove",
                         "--actor", "author")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("designation refused", proc.stderr)
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["first"])

    def test_concurrent_designations_keep_both_names(self):
        barrier = threading.Barrier(2)

        def add(name):
            db = self.connect()
            try:
                barrier.wait(timeout=10)
                return storage.of(db).work.designate_reviewer(self.lot["id"], name,
                                                              actor="chief")
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(add, ["first", "second"]))
        self.assertTrue(all(rows))
        self.assertEqual(set(work.get(self.db, self.lot["id"])["reviewers"]), {"first", "second"})

    def test_pre_migration_reads_backfill_and_live_connection(self):
        from ameesh import db as db_mod
        migration = Path(REPO, "src/ameesh/migrations/0456_work_item_reviewers.sql").read_text()
        self.db.execute("DROP TRIGGER work_item_reviewers_sync ON work_items")
        self.db.execute("ALTER TABLE work_items DROP COLUMN reviewers")
        self.addCleanup(self.db.execute, migration)
        self.db.__dict__.pop("_has_reviewers", None)
        self.db.execute("UPDATE work_items SET reviewer = 'first' WHERE id = %s",
                        (self.lot["id"],))
        row = work.get(self.db, self.lot["id"])
        self.assertIsNone(row["reviewers"])
        self.assertEqual(row["reviewer"], "first")
        with self.assertRaisesRegex(db_mod.DbError, "0456"):
            self.designate("second")
        work.move(self.db, self.lot["id"], "build")
        with self.assertRaisesRegex(work.WorkError, "0456"):
            work.move(self.db, self.lot["id"], "qa", reviewer="second", actor="human:chief")
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "build")
        self.db.execute(migration)
        work.move(self.db, self.lot["id"], "qa")
        # A negative catalogue lookup was not cached by the live connection.
        self.assertEqual(self.designate("second")["reviewers"], ["first", "second"])
        self.designate("first", remove=True)
        self.db.execute(migration)
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])
