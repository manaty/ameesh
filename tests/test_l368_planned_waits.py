# SPDX-License-Identifier: AGPL-3.0-only
"""L368: planned waits suppress stale alerts consistently across all views."""
from __future__ import annotations

import datetime
import time
import unittest

from ameesh import (plan, planned_waits as W, progress, roadmap, stagnation, storage,
                    work, work_dependencies)
from tests.support import PgTestCase


class UnitTest(unittest.TestCase):
    now = plan.day_start_ts("2026-10-12") + 12 * 3600
    rows = [{"id": 1, "state": "build", "package_id": "child"}]
    packages = {"child": {"id": "child", "parent": "epic", "present": True},
                "epic": {"id": "epic", "present": True},
                "prerequisite": {"id": "prerequisite", "present": True}}

    def resolve(self, *, dates=None, commitments=(), items=(), rows=None, now=None, landings=()):
        return W.resolve(rows or self.rows, dates or {}, commitments, self.packages,
                         items, now=self.now if now is None else now, landings=landings)

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

    def test_external_gate_holds_until_the_end_of_the_due_day(self):
        commitment = {"id": 7, "status": "open", "work_item_id": 1, "due_on": "2026-10-13",
                      "depends_on": ["gate:liquichain-window"]}
        expected = {1: {"what": "hold", "gate": "liquichain-window", "until": "2026-10-13",
                        "commitment": 7}}
        self.assertEqual(self.resolve(commitments=[commitment]), expected)
        for alias in ("external:liquichain-window", "GATE:liquichain-window"):
            self.assertEqual(self.resolve(commitments=[dict(commitment, depends_on=[alias])]),
                             expected)
        end = plan.day_start_ts("2026-10-14")
        self.assertEqual(self.resolve(commitments=[commitment], now=end - 1), expected)
        self.assertEqual(self.resolve(commitments=[commitment], now=end), {})

    def test_external_gate_on_a_package_holds_its_descendants(self):
        commitment = {"id": 8, "status": "open", "package_id": "epic", "due_on": "2026-10-13",
                      "depends_on": ["gate:epic-window"]}
        self.assertEqual(self.resolve(commitments=[commitment]),
                         {1: {"what": "hold", "gate": "epic-window", "until": "2026-10-13",
                              "commitment": 8}})
        rows = [{"id": 4, "state": "build", "package_id": "other"}]
        self.assertEqual(self.resolve(commitments=[commitment], rows=rows), {})

    def test_unrepresentable_hold_end_does_not_break_other_waits(self):
        rows = [{"id": 1, "state": "build"}, {"id": 2, "state": "build"}]
        commitments = [
            {"status": "open", "work_item_id": 1, "due_on": "9999-12-31",
             "depends_on": ["gate:out-of-range"]},
            {"status": "open", "work_item_id": 2, "due_on": "2026-10-13",
             "depends_on": ["gate:valid"]},
        ]
        self.assertEqual(self.resolve(rows=rows, commitments=commitments),
                         {2: {"what": "hold", "gate": "valid", "until": "2026-10-13"}})

    def test_malformed_finished_or_undated_gate_does_not_suppress(self):
        base = {"id": 7, "status": "open", "work_item_id": 1, "due_on": "2026-10-13"}
        for reference in ("gate:", "gate", "external", "gate liquichain", "hold:liquichain",
                          "after the window", None):
            with self.subTest(reference=reference):
                self.assertEqual(self.resolve(commitments=[dict(base, depends_on=[reference])]), {})
        for status in ("proposed", "done", "cancelled"):
            self.assertEqual(self.resolve(commitments=[dict(base, status=status,
                                                            depends_on=["gate:x"])]), {})
        for due in (None, "", "someday"):
            self.assertEqual(self.resolve(commitments=[dict(base, due_on=due,
                                                            depends_on=["gate:x"])]), {})
        self.assertEqual(self.resolve(commitments=[dict(base, due_on="2026-10-11",
                                                        depends_on=["gate:x"])]), {})
        # a lot dependency stays a dependency, and wins over a gate on the same lot
        both = dict(base, depends_on=["gate:x", "#2"])
        self.assertEqual(self.resolve(commitments=[both], items=[{"id": 2, "state": "build"}]),
                         {1: {"what": "dependency", "lots": [2]}})

    # -- #1945 : atterrissage planifié sur un véhicule de paquet -------------

    def landing(self, **kw):
        row = {"work_item_id": 1, "vehicle_id": 2, "until_on": "2026-10-13",
               "open": True, "vehicle_state": "qa", "head": "b" * 40}
        row.update(kw)
        return row

    def test_landing_waits_while_vehicle_open_until_the_end_of_its_day(self):
        expected = {1: {"what": "landing", "vehicle": 2, "step": "qa",
                        "head": "b" * 40, "until": "2026-10-13"}}
        self.assertEqual(self.resolve(landings=[self.landing()]), expected)
        end = plan.day_start_ts("2026-10-14")
        self.assertEqual(self.resolve(landings=[self.landing()], now=end - 1), expected)
        # la journée compte en entier : le lendemain, le signal stale revient
        self.assertEqual(self.resolve(landings=[self.landing()], now=end), {})
        for broken in ({"open": False}, {"until_on": None}, {"until_on": ""},
                       {"until_on": "someday"}, {"until_on": "2026-10-11"}, {"vehicle_id": 1}):
            with self.subTest(broken=broken):
                self.assertEqual(self.resolve(landings=[self.landing(**broken)]), {})
        # sans drapeau `open` (résolveur pur), le véhicule doit être un lot ouvert connu
        fallback = self.landing()
        fallback.pop("open")
        self.assertEqual(self.resolve(landings=[fallback]), {})
        self.assertEqual(self.resolve(landings=[fallback], items=[{"id": 2, "state": "qa"}]),
                         expected)

    def test_explicit_waits_and_gates_stay_ahead_of_a_landing(self):
        landing = self.landing()
        self.assertEqual(self.resolve(dates={1: {"planned_start": "2026-10-13"}},
                                      landings=[landing]),
                         {1: {"what": "scheduled", "start": "2026-10-13"}})
        self.assertEqual(self.resolve(commitments=[{"status": "open", "work_item_id": 1,
                                                    "depends_on": ["#2"]}],
                                      items=[{"id": 2, "state": "build"}], landings=[landing]),
                         {1: {"what": "dependency", "lots": [2]}})
        self.assertEqual(self.resolve(commitments=[{"status": "open", "work_item_id": 1,
                                                    "due_on": "2026-10-13",
                                                    "depends_on": ["gate:x"]}],
                                      landings=[landing]),
                         {1: {"what": "hold", "gate": "x", "until": "2026-10-13"}})

    def test_landing_label_names_vehicle_step_head_and_deadline(self):
        wait = {"what": "landing", "vehicle": 42, "step": "qa",
                "head": "b" * 40, "until": "2026-10-18"}
        self.assertEqual(work_dependencies.wait_label(wait),
                         "landing on vehicle #42 (qa, head %s) until 2026-10-18" % ("b" * 12))


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

    def test_external_hold_defers_stagnation_and_ends_at_its_due_day(self):
        waiting = self.item("blocked by an external gate")
        due = (datetime.date.fromtimestamp(self.now)
               + datetime.timedelta(days=1)).isoformat()
        commitment = roadmap.add_commitment(
            self.db, "hold until the liquichain window", due=due, lot=waiting["id"],
            depends_on=["gate:liquichain-window"])
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"],
                         {"what": "hold", "gate": "liquichain-window", "until": due,
                          "commitment": commitment["id"]})
        self.assertIsNone(row["stale"])
        self.assertEqual(work_dependencies.wait_label(row["planned_wait"]),
                         "external hold until %s (liquichain-window)" % due)
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        self.assertIsNone(next(r for r in snap["lots"] if r["id"] == waiting["id"])["stale"])
        # the due day counts in full: the ordinary rule applies again the next day
        after = plan.day_start_ts(
            (datetime.date.fromtimestamp(self.now) + datetime.timedelta(days=2)).isoformat())
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=after)],
                         [waiting["id"]])
        # cancelling the commitment releases the lot immediately
        storage.of(self.db).roadmap.set_commitment(
            commitment["id"], values={"status": "cancelled"}, current=["open"])
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)],
                         [waiting["id"]])

    def test_declared_landing_defers_stagnation_until_cleared_closed_or_expired(self):
        waiting = work.add(self.db, title="frozen, carried by a package vehicle", app="acme")
        work.move(self.db, waiting["id"], "build", actor="test")
        work.move(self.db, waiting["id"], "qa", actor="test")
        head = "c" * 40
        work.milestone(self.db, waiting["id"], "frozen", sha=head, actor="test")
        work.milestone(self.db, waiting["id"], "verdict", verdict="ok", sha=head, actor="reviewer")
        self.old(waiting)
        # sans déclaration, c'est bien le faux stale générique qui tire
        stale_row = next(r for r in stagnation.stagnant_lots(self.db, now=self.now)
                         if r["id"] == waiting["id"])
        self.assertEqual(stale_row["waiting_for"], "fusion")
        self.assertEqual(stale_row["waiting"]["label"], "merge (executor not designated)")
        vehicle = work.add(self.db, title="package vehicle", app="acme")
        vehicle_head = "e" * 40
        work.milestone(self.db, vehicle["id"], "frozen", sha=vehicle_head, actor="test")
        today = datetime.date.fromtimestamp(self.now)
        until = (today + datetime.timedelta(days=2)).isoformat()
        work_dependencies.land(self.db, waiting["id"], vehicle["id"], until_on=until,
                               actor="mesh-design")
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        label = "landing on vehicle #%d (intake, head %s) until %s" % (
            vehicle["id"], vehicle_head[:12], until)
        self.assertEqual(row["planned_wait"],
                         {"what": "landing", "vehicle": vehicle["id"], "step": "intake",
                          "head": vehicle_head, "until": until})
        self.assertIsNone(row["stale"])
        self.assertEqual(row["waiting_for"]["label"], label)
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=self.now, gantt=False)
        self.assertIsNone(next(r for r in snap["lots"] if r["id"] == waiting["id"])["stale"])
        # un nouveau gel du véhicule remplace la tête affichée, sans geste
        newer = "f" * 40
        work.milestone(self.db, vehicle["id"], "frozen", sha=newer, actor="test")
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertEqual(row["planned_wait"]["head"], newer)
        self.assertIn(newer[:12], row["waiting_for"]["label"])
        # le retrait rend le signal stale immédiatement
        self.assertTrue(work_dependencies.clear_landing(self.db, waiting["id"], actor="pilot"))
        row = plan.annotate(self.db, [work.get(self.db, waiting["id"])], now=self.now)[0]
        self.assertIsNone(row["planned_wait"])
        self.assertEqual(row["waiting_for"]["label"], "merge (executor not designated)")
        self.old(waiting)   # le geste lui-même est une activité ; on repart de l'âge d'avant
        self.assertIn(waiting["id"], [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # véhicule fermé : le signal revient
        work_dependencies.land(self.db, waiting["id"], vehicle["id"], until_on=until, actor="pilot")
        self.assertEqual(stagnation.stagnant_lots(self.db, now=self.now), [])
        work.close(self.db, vehicle["id"], abandoned=True)
        self.old(waiting)
        self.assertIn(waiting["id"], [r["id"] for r in stagnation.stagnant_lots(self.db, now=self.now)])
        # échéance dépassée : la journée compte en entier, puis le signal revient
        vehicle2 = work.add(self.db, title="second vehicle", app="acme")
        work_dependencies.land(self.db, waiting["id"], vehicle2["id"], until_on=until, actor="pilot")
        end_until = plan.day_start_ts(
            (datetime.date.fromisoformat(until) + datetime.timedelta(days=1)).isoformat())
        self.assertNotIn(waiting["id"],
                         [r["id"] for r in stagnation.stagnant_lots(self.db, now=end_until - 1)])
        self.assertIn(waiting["id"],
                      [r["id"] for r in stagnation.stagnant_lots(self.db, now=end_until)])
