# SPDX-License-Identifier: AGPL-3.0-only
"""#3986: QA without a declared freeze still requires the author's work."""
from ameesh import lot_class, plan, stagnation, storage, work
from .support import PgTestCase


class QaFreezeTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("author", "codex", cwd=self.tmp)
        self.item = work.add(self.db, title="freeze owed", assignee="author")
        work.move(self.db, self.item["id"], "build", actor="author")
        work.move(self.db, self.item["id"], "qa", actor="author")

    def test_without_freeze_views_capacity_and_wake_agree(self):
        rows = work.list_items(self.db)
        plan.annotate(self.db, rows, cfg=self.cfg)
        self.assertEqual(rows[0]["waiting_for"],
                         {"what": "freeze", "who": "author", "label": "freeze by author"})
        listing = storage.of(self.db).operations.listing()
        self.assertEqual(next(r for r in listing if r["name"] == "author")["actionable_lots"], 1)
        due = storage.of(self.db).lot_sessions.actionable("author")
        self.assertEqual([r["id"] for r in due], [self.item["id"]])
        detail = stagnation.with_availability(rows[0]["waiting_for"], {"author": "stopped"})
        self.assertEqual(detail["who_state"], "arrêté")

    def test_declared_freeze_without_reviewer_keeps_verdict(self):
        work.milestone(self.db, self.item["id"], "frozen", sha="a" * 40, actor="author")
        rows = work.list_items(self.db)
        plan.annotate(self.db, rows, cfg=self.cfg)
        self.assertEqual(rows[0]["waiting_for"]["label"],
                         "verdict from a reviewer (none designated)")
        self.assertEqual(storage.of(self.db).lot_sessions.actionable("author"), [])

    def test_light_and_waived_lots_keep_merge_wait(self):
        item = {"state": "qa", "assignee": "author"}
        for kw, jalons in (({"review_class": lot_class.LIGHT}, {}),
                           ({}, {"verdict_required": False})):
            self.assertEqual(stagnation.waiting(item, jalons, **kw)["what"], "merge")

    def test_waiver_does_not_wake_or_count_author(self):
        self.db.execute("UPDATE work_items SET reviewer = %s WHERE id = %s",
                        (work.REVIEWER_NONE, self.item["id"]))
        self.assertEqual(storage.of(self.db).lot_sessions.actionable("author"), [])
        listing = storage.of(self.db).operations.listing()
        self.assertEqual(next(r for r in listing if r["name"] == "author")["actionable_lots"], 0)
