# SPDX-License-Identifier: AGPL-3.0-only
"""#4174: project phases, readiness, recomputation and legacy date command."""
import argparse
import contextlib
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests.support import PgTestCase
from ameesh import project_plan as plan, storage, work, plan_cli, backlog
from tests.test_l165_prise_lots import _Lots


def lot(number, **values):
    return dict(id=number, state="intake", type="evolution", title="lot",
                lot_team="acme", priority=2, created_ts=number, **values)


class PlanTest(unittest.TestCase):
    def report(self, rows, **kwargs):
        return plan.evaluate(rows, project="acme", now=1800000000, **kwargs)

    def test_bug_preempts_next_dispatch_without_interrupting_build(self):
        evolution = lot(1)
        self.assertEqual(self.report([evolution])["ready"], [1])
        bug = lot(2)
        bug["type"] = "bug"
        in_progress = lot(3)
        in_progress.update(state="build", assignee="author")
        report = self.report([evolution, bug, in_progress])
        self.assertEqual(report["phase"], "bugs")
        self.assertEqual(report["ready"], [2])
        self.assertEqual(in_progress["state"], "build")
        for state in ("waiting_human", "blocked", "qa", "merged", "promoted"):
            bug["state"] = state
            self.assertEqual(self.report([evolution, bug])["ready"], [1])

    def test_blocked_bugs_do_not_close_evolution_phase(self):
        for values in ({"pending_decision": True}, {"blocking_dependencies": [99]},
                       {"open_conflicts": [99]}, {"planned_start": "2099-01-01"},
                       {"external_executor": "external"}, {"assignee": "agent"},
                       {"required_capabilities": ["production"]}):
            bug = lot(2)
            bug.update(type="bug", **values)
            with self.subTest(values=values):
                self.assertEqual(self.report([lot(1), bug])["ready"], [1])

    def test_every_readiness_gate_explained_and_spec_is_project_owned(self):
        row = lot(1, body="draft", blocking_dependencies=[2], open_conflicts=[3],
                  pending_decision=True)
        rule = {"mode": "marker", "field": "body", "value": "Spec: accepted"}
        out = self.report([row], rule=rule)
        self.assertEqual(out["ready"], [])
        self.assertEqual(len(out["lots"][0]["waiting"]), 4)
        row.update(body="Spec: accepted", blocking_dependencies=[], open_conflicts=[],
                   pending_decision=False)
        self.assertEqual(self.report([row], rule=rule)["ready"], [1])
        for invalid in ({"mode": "unknown"}, {}, [], False, "intake"):
            with self.subTest(rule=invalid):
                self.assertFalse(self.report([row], rule=invalid)["ready"])

    def test_dependency_order_priority_age_and_dates(self):
        rows = [lot(1, blocking_dependencies=[2]), lot(2), lot(3)]
        rows[2]["priority"] = 1
        self.assertEqual([r["id"] for r in self.report(rows)["lots"]], [3, 2, 1])
        rows = [lot(4, planned_start="2020-01-01"), lot(3)]
        self.assertEqual(self.report(rows)["ready"], [4, 3])

    def test_hold_and_spec_missing_bug_do_not_preempt(self):
        bug = lot(2)
        bug.update(type="bug", body="draft")
        self.assertEqual(self.report([lot(1), bug], holds={2: {"since": 1}})["ready"], [1])
        self.assertEqual(self.report([lot(1, body="accepted"), bug],
            rule={"mode": "marker", "field": "body", "value": "accepted"})["ready"], [1])

    def test_dispatch_snapshot_read_once_and_re_evaluated_per_agent(self):
        from unittest.mock import Mock
        db = Mock(cfg=None)
        rows = [dict(lot(1), app="acme"), dict(lot(2), app="acme", type="bug")]
        snapshots = {}
        with patch.object(plan.storage, "of") as of, \
                patch.object(backlog, "_take_holds", return_value={}) as holds:
            of.return_value.work.project_plan_rows.return_value = rows
            self.assertEqual([r["id"] for r in plan.eligible(
                db, rows, snapshots=snapshots, can_take=lambda r: r["id"] != 2)], [1])
            self.assertEqual([r["id"] for r in plan.eligible(
                db, rows, snapshots=snapshots)], [2])
            plan.mark_taken(snapshots, 2)
            self.assertEqual([r["id"] for r in plan.eligible(
                db, rows, snapshots=snapshots)], [1])
            of.return_value.work.project_plan_rows.assert_called_once_with("acme")
            holds.assert_called_once()

    def test_cli_keeps_legacy_target_and_read_only_project(self):
        parser = argparse.ArgumentParser()
        plan_cli.add_parsers(parser.add_subparsers(dest="work_command"), lambda: None)
        args = parser.parse_args(["plan", "12", "--start", "2026-10-28"])
        with patch("ameesh.roadmap.plan", return_value={"target": 12, "kind": "task", "plan": {}}) as legacy:
            with contextlib.redirect_stdout(io.StringIO()):
                plan_cli._plan(None, args)
            legacy.assert_called_once_with(None, "12", source=None, actor="", start="2026-10-28")
        args = parser.parse_args(["plan", "--project", "acme", "--json"])
        with patch.object(plan, "report", return_value=self.report([lot(1)])):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                plan_cli._plan(None, args)
            self.assertEqual(json.loads(output.getvalue())["ready"], [1])
        args = parser.parse_args(["plan", "12", "--project", "acme"])
        with self.assertRaises(work.WorkError):
            plan_cli._plan(None, args)


class ProjectSnapshotTest(PgTestCase):
    def setUp(self):
        self.db.execute("TRUNCATE work_items CASCADE")
        self.a = work.add(self.db, title="evolution", app="acme")["id"]
        self.b = work.add(self.db, title="bug", type="bug", app="acme")["id"]

    def report(self):
        return plan.report(self.db, "acme", now=1800000000)

    def test_real_snapshot_recomputes_after_state_dependency_and_conflict_changes(self):
        self.assertEqual(self.report()["ready"], [self.b])
        self.db.execute("UPDATE work_items SET state = 'waiting_human' WHERE id = %s", (self.b,))
        self.assertEqual(self.report()["ready"], [self.a])
        deps = storage.of(self.db).work_dependencies
        deps.add_link(self.a, self.b, "conflicts_with", actor="test")
        self.assertEqual(self.report()["ready"], [])
        # A recorded conflict decision is still an open conflicts_with edge.
        deps.decide_link(self.a, self.b, decision="rescope", actor="test")
        self.assertEqual(self.report()["ready"], [])
        work.close(self.db, self.b, abandoned=True)
        self.assertEqual(self.report()["ready"], [self.a])
        self.assertEqual(len(self.report()["lots"]), 1)

    def test_bug_beyond_old_candidate_window_is_not_starved(self):
        self.db.execute("INSERT INTO work_items(title, app, priority)"
                        " SELECT 'evolution ' || n, 'acme', 1 FROM generate_series(1, 205) n")
        self.db.execute("UPDATE work_items SET priority = 3 WHERE id = %s", (self.b,))
        candidates = storage.of(self.db).work.unassigned_lots(None)
        self.assertGreater(len(candidates), 200)
        self.assertEqual([r["id"] for r in plan.eligible(self.db, candidates)], [self.b])

    def test_locked_snapshot_only_reads_target_and_unassigned_bugs(self):
        other = work.add(self.db, title="unrelated evolution", app="acme")["id"]
        rows = storage.of(self.db).work.project_plan_rows("acme", assignment_id=self.a)
        self.assertEqual({r["id"] for r in rows}, {self.a, self.b})
        self.db.execute("UPDATE work_items SET assignee = 'elsewhere' WHERE id = %s", (self.b,))
        rows = storage.of(self.db).work.project_plan_rows("acme", assignment_id=self.a)
        self.assertEqual({r["id"] for r in rows}, {self.a})
        self.assertNotIn(other, {r["id"] for r in rows})

    def test_project_isolation_and_scheduled_dates(self):
        self.db.execute("UPDATE work_items SET app = 'elsewhere' WHERE id = %s", (self.b,))
        self.assertEqual(self.report()["ready"], [self.a])
        self.db.execute("UPDATE work_items SET planned_start = '2099-01-01' WHERE id = %s", (self.a,))
        row = self.report()["lots"][0]
        self.assertIn("planned start: 2099-01-01", row["waiting"])

    def test_plan_lists_bugs_and_evolutions_not_the_improvement_queue(self):
        improvement = storage.of(self.db).work.backlog_add(
            title="basket line", body="", source="", expected_value="value",
            value_score=10, priority=3, team=None, required_capabilities=None,
            package_id=None, package_parent=None, note="basket", actor="test",
            app="acme")["id"]
        self.assertNotIn(improvement, [int(r["id"]) for r in self.report()["lots"]])
        self.assertIn(self.b, [int(r["id"]) for r in self.report()["lots"]])


class DispatchTest(_Lots):
    def test_dispatch_bug_before_higher_priority_evolution_and_resume(self):
        self.agent("claude1", team="nexlink")
        evo = self.lot("enhancement", priority=1)
        bug = self.lot("bug", type="bug", priority=3)
        self.assertEqual([t["item"]["id"] for t in self.take()], [bug["id"]])
        self.assertIsNone(work.get(self.db, evo["id"])["assignee"])
        # No interrupt: the assigned bug remains assigned, and next dispatch
        # sees no unassigned bug. Another agent can take the enhancement.
        self.agent("claude2", team="nexlink")
        self.assertEqual([t["item"]["id"] for t in self.take()], [evo["id"]])
        self.assertEqual(work.get(self.db, bug["id"])["assignee"], "claude1")

    def test_untakeable_bug_does_not_block_enhancement(self):
        self.agent("claude1", team="nexlink")
        evo = self.lot("enhancement")
        bug = self.lot("device bug", type="bug")
        self.db.execute("UPDATE work_items SET required_capabilities = '{ios-device}' WHERE id = %s",
                        (bug["id"],))
        self.assertEqual([t["item"]["id"] for t in self.take()], [evo["id"]])
        self.assertIsNone(work.get(self.db, bug["id"])["assignee"])

    def test_agent_filter_keeps_bug_for_capable_agent(self):
        self.agent("claude1", team="nexlink")
        self.agent("claude2", team="nexlink", capabilities=["ios-device"])
        evo = self.lot("enhancement")
        bug = self.lot("device bug", type="bug")
        self.db.execute("UPDATE work_items SET required_capabilities = '{ios-device}' WHERE id = %s",
                        (bug["id"],))
        self.assertEqual({t["item"]["id"] for t in self.take()}, {evo["id"], bug["id"]})
        self.assertEqual(work.get(self.db, bug["id"])["assignee"], "claude2")

    def test_directory_refusal_does_not_block_enhancement(self):
        self.agent("claude1", team="nexlink")
        evo = self.lot("enhancement")
        bug = self.lot("bug without directory", type="bug")
        original = backlog._directory_ready

        def directory(cfg, db, agent, item, project, **kwargs):
            return item["id"] != bug["id"] and original(
                cfg, db, agent, item, project, **kwargs)

        with patch.object(backlog, "_directory_ready", side_effect=directory):
            self.assertEqual([t["item"]["id"] for t in self.take()], [evo["id"]])
        self.assertIsNone(work.get(self.db, bug["id"])["assignee"])

    def test_footprint_blocked_bug_keeps_evolution_dispatchable(self):
        from ameesh import work_footprints
        self.agent("claude1", team="nexlink")
        occupant = self.lot("occupant")
        evolution = self.lot("enhancement")
        bug = self.lot("overlapping bug", type="bug")
        footprints = storage.of(self.db).work_footprints
        for item in (occupant, bug):
            footprints.replace(item["id"], "manual",
                               [work_footprints.normalize("a.py")], actor="test")
        self.db.execute("UPDATE work_items SET assignee = 'human:proprio' WHERE id = %s",
                        (occupant["id"],))
        report = plan.report(self.db, "nexlink")
        blocked = next(row for row in report["lots"] if row["id"] == bug["id"])
        self.assertIn("footprint overlaps an occupying lot", blocked["waiting"])
        self.assertEqual([taken["item"]["id"] for taken in self.take()], [evolution["id"]])
        self.assertIsNone(work.get(self.db, bug["id"])["assignee"])

    def test_new_untakeable_bug_does_not_rollback_enhancement(self):
        self.agent("claude1", team="nexlink")
        evo = self.lot("enhancement")
        check = work.check_assignee
        inserted = []

        def add_bug(*args, **kwargs):
            if not inserted:
                inserted.append(self.lot("device bug", type="bug")["id"])
                self.db.execute("UPDATE work_items SET required_capabilities = '{ios-device}' WHERE id = %s",
                                (inserted[0],))
            return check(*args, **kwargs)

        with patch.object(work, "check_assignee", side_effect=add_bug):
            self.assertEqual([t["item"]["id"] for t in self.take()], [evo["id"]])

    def test_new_bug_after_selection_rolls_back_evolution_assignment(self):
        self.agent("claude1", team="nexlink")
        evo = self.lot("enhancement")
        check = work.check_assignee
        inserted = []

        def add_bug(*args, **kwargs):
            if not inserted:
                inserted.append(self.lot("new bug", type="bug")["id"])
            return check(*args, **kwargs)

        with patch.object(work, "check_assignee", side_effect=add_bug):
            self.assertEqual(self.take(), [])
        self.assertIsNone(work.get(self.db, evo["id"])["assignee"])
        self.assertFalse(any(e["note"].startswith(backlog.LOT_TAKE_NOTE)
                             for e in work.events(self.db, evo["id"])))
        self.assertEqual([t["item"]["id"] for t in self.take()], inserted)

    def test_malformed_project_does_not_abort_other_project_dispatch(self):
        self.agent("claude1", team="nexlink")
        self.agent("claude2", team="acme")
        blocked = self.lot("bad config", app="acme")
        ready = self.lot("valid project")
        self.cfg = replace(self.cfg, project_planning={"acme": "invalid"})
        self.assertEqual([t["item"]["id"] for t in self.take()], [ready["id"]])
        self.assertIsNone(work.get(self.db, blocked["id"])["assignee"])
        report = plan.report(self.db, "acme", cfg=self.cfg)
        self.assertIn("invalid project spec_acceptance rule", report["lots"][0]["waiting"])
        self.assertFalse(plan.assignment_ready(self.db, blocked, blocked, cfg=self.cfg))
        for settings in (None, [], False):
            with self.subTest(settings=settings):
                cfg = replace(self.cfg, project_planning=settings)
                self.assertEqual(plan.report(self.db, "acme", cfg=cfg)["ready"], [])

    def test_project_spec_marker_controls_actual_dispatch(self):
        self.agent("claude1", team="nexlink")
        evo = self.lot("enhancement", body="draft")
        self.cfg = replace(self.cfg, project_planning={"nexlink": {"spec_acceptance": {
            "mode": "marker", "field": "body", "value": "Spec: accepted"}}})
        self.assertEqual(self.take(), [])
        self.db.execute("UPDATE work_items SET body = 'Spec: accepted' WHERE id = %s", (evo["id"],))
        self.assertEqual([t["item"]["id"] for t in self.take()], [evo["id"]])
