# SPDX-License-Identifier: AGPL-3.0-only
"""L368: planned waits suppress stale alerts consistently across all views."""
from __future__ import annotations

import datetime
import time
import unittest

from ameesh import plan, planned_waits as W, progress, roadmap, stagnation, storage, work
from tests.support import PgTestCase


class UnitTest(unittest.TestCase):
    now = plan.day_start_ts("2026-10-12") + 12 * 3600
    rows = [{"id": 1, "state": "build", "package_id": "child"}]
    packages = {"child": {"id": "child", "parent": "epic", "present": True},
                "epic": {"id": "epic", "present": True},
                "prerequisite": {"id": "prerequisite", "present": True}}

    def resolve(self, *, dates=None, commitments=(), items=(), rows=None, now=None):
        return W.resolve(rows or self.rows, dates or {}, commitments, self.packages,
                         items, now=self.now if now is None else now)

    def test_future_start_ends_at_local_midnight_without_changing_activity(self):
        dates = {1: {"planned_start": "2026-10-13"}}
        self.assertEqual(self.resolve(dates=dates), {1: {"what": "scheduled", "start": "2026-10-13"}})
        self.assertEqual(self.resolve(dates=dates, now=plan.day_start_ts("2026-10-13")), {})
        for start in (None, "", "invalid", "2026-10-12", "2026-10-11"):
            self.assertEqual(self.resolve(dates={1: {"planned_start": start}}), {})

    def test_only_explicit_open_lots_block(self):
        for reference in ("#2", "2", "lot 2", "lot #2", "task #2", "TASK 2"):
            with self.subTest(reference=reference):
                commitment = {"status": "open", "work_item_id": 1, "depends_on": [reference]}
                self.assertEqual(self.resolve(commitments=[commitment], items=[{"id": 2, "state": "qa"}]),
                                 {1: {"what": "dependency", "lots": [2]}})
                for state in ("merged", "promoted", "closed"):
                    self.assertEqual(self.resolve(commitments=[commitment], items=[{"id": 2, "state": state}]), {})
        for reference in ("after #2 merges", "main carries abc123", "#999", "#1", "e2", None):
            self.assertEqual(self.resolve(commitments=[{"status": "open", "work_item_id": 1,
                                                       "depends_on": [reference]}],
                                          items=[{"id": 1, "state": "build"}, {"id": 2, "state": "qa"}]), {})

    def test_proposals_cancelled_and_unrelated_commitments_do_not_suppress(self):
        for status, lot in (("proposed", 1), ("cancelled", 1), ("done", 1), ("open", 3)):
            self.assertEqual(self.resolve(commitments=[{"status": status, "work_item_id": lot,
                                                       "depends_on": ["#2"]}],
                                          items=[{"id": 2, "state": "build"}]), {})

    def test_plan_dependency_inherits_through_ancestor_and_resolves_package(self):
        c = {"status": "open", "package_id": "epic", "depends_on": ["prerequisite"]}
        items = [{"id": 2, "state": "build", "package_id": "prerequisite"},
                 {"id": 3, "state": "merged", "package_id": "prerequisite"}]
        self.assertEqual(self.resolve(commitments=[c], items=items), {1: {"what": "dependency", "lots": [2]}})
        for reference in ("lot prerequisite", "package prerequisite", "WorkPackage prerequisite"):
            self.assertEqual(self.resolve(commitments=[dict(c, depends_on=[reference])], items=items),
                             {1: {"what": "dependency", "lots": [2]}})
        self.assertEqual(self.resolve(commitments=[c], items=[]), {})
        self.assertEqual(self.resolve(commitments=[c], items=items,
                                      rows=[{"id": 4, "state": "build", "package_id": "other"}]), {})
        packages = {"a": {"parent": "b"}, "b": {"parent": "a"}}
        self.assertEqual(list(W.ancestors("a", packages)), ["a", "b"])
        self.assertEqual(list(W.ancestors("retired", {"retired": {"present": False}})), [])

    def test_plan_start_inherits_and_explicit_lot_start_overrides_it(self):
        packages = {k: dict(v) for k, v in self.packages.items()}
        packages["epic"]["planned_start"] = "2026-10-13"
        self.assertEqual(W.resolve(self.rows, {}, [], packages, [], now=self.now),
            {1: {"what": "scheduled", "start": "2026-10-13", "package": "epic"}})
        self.assertEqual(W.resolve(self.rows, {1: {"planned_start": "2026-10-12"}}, [],
                                    packages, [], now=self.now), {})


class PgTest(PgTestCase):
    def setUp(self):
        self.db.execute("TRUNCATE work_items, commitments, work_packages CASCADE")
        self.now = time.time()

    def old(self, item):
        for sql in ("UPDATE work_items SET updated_at = to_timestamp(%s), created_at = to_timestamp(%s) WHERE id = %s",
                    "UPDATE work_item_events SET created_at = to_timestamp(%s) WHERE work_item_id = %s",
                    "UPDATE work_item_milestones SET at = to_timestamp(%s) WHERE work_item_id = %s"):
            args = (self.now - 10 * 3600, self.now - 11 * 3600, item["id"]) if "created_at = to_timestamp(%s) WHERE id" in sql else (self.now - 10 * 3600, item["id"])
            self.db.execute(sql, args)

    def item(self, title):
        item = work.add(self.db, title=title, app="acme")
        self.old(item)
        return item

    def test_planned_start_in_alert_work_and_progress_and_limit_after_filter(self):
        future = self.item("planned tomorrow")
        actionable = self.item("unexplained wait")
        tomorrow = (datetime.date.fromtimestamp(self.now) + datetime.timedelta(days=1)).isoformat()
        storage.of(self.db).roadmap.plan_item(future["id"], {"start": tomorrow}, source="test", actor="test")
        alerts = stagnation.stagnant_lots(self.db, now=self.now, limit=1)
        self.assertEqual([a["id"] for a in alerts], [actionable["id"]])
        annotated = plan.annotate(self.db, [work.get(self.db, future["id"])], now=self.now)[0]
        self.assertIsNone(annotated["stale"])
        self.assertEqual(annotated["planned_wait"]["start"], tomorrow)
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        lots = {r["id"]: r for r in snap["lots"]}
        self.assertIsNone(lots[future["id"]]["stale"])
        self.assertIsNotNone(lots[actionable["id"]]["stale"])
        after_start = stagnation.stagnant_lots(self.db, now=plan.day_start_ts(tomorrow))
        self.assertIn(future["id"], [r["id"] for r in after_start])

    def test_open_blocker_beyond_render_limit_and_release_when_closed(self):
        waiting = self.item("wait for a real lot")
        blocker = work.add(self.db, title="blocker", app="other-project")
        roadmap.add_commitment(self.db, "release", due="2999-01-01", lot=waiting["id"],
                               depends_on=["task #%d" % blocker["id"]])
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now, limit=1), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"]["lots"], [blocker["id"]])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, max_lots=1, gantt=False)
        self.assertIsNone(snap["lots"][0]["stale"])
        work.close(self.db, blocker["id"], abandoned=True)
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])

    def test_cancelled_commitment_or_prose_does_not_mask_stagnation(self):
        waiting = self.item("wait in prose only")
        blocker = work.add(self.db, title="blocker")
        commitment = roadmap.add_commitment(self.db, "release", due="2999-01-01", lot=waiting["id"],
                                             depends_on=["after #%d merges" % blocker["id"]])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])
        storage.of(self.db).roadmap.set_commitment(commitment["id"],
            values={"depends_on": ["#%d" % blocker["id"]], "status": "cancelled"}, current=["open"])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)], [waiting["id"]])

    def test_package_plan_dependency_is_read_from_storage(self):
        st = storage.of(self.db)
        for ident, kind, parent in (("epic", "epic", None), ("child", "lot", "epic"),
                                    ("prerequisite", "lot", None)):
            st.packages.upsert({"id": ident, "kind": kind, "title": ident, "parent": parent,
                                "canon_ref": "test", "present": True})
        waiting = work.add(self.db, title="waiting", app="acme", package="child")
        self.old(waiting)
        blocker = work.add(self.db, title="blocker", app="other", package="prerequisite")
        roadmap.add_commitment(self.db, "epic release", due="2999-01-01", package="epic",
                               depends_on=["prerequisite"])
        rows = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)
        self.assertEqual(rows[0]["planned_wait"]["lots"], [blocker["id"]])
        self.assertIsNone(rows[0]["stale"])
