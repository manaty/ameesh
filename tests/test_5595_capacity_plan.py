# SPDX-License-Identifier: AGPL-3.0-only
"""Capacity, graph, reservation and event forecast contracts (#4201)."""
import argparse
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ameesh import capacity_plan as plan, plan_cli, work


def lot(i, **kw):
    return dict(id=i, **dict({"estimate_minutes": 60, "created_ts": i,
                             "project": "acme", "repository": "acme/repo",
                             "footprint_paths": ["src/module%s" % i]}, **kw))


def snapshot(rows, slots=2, **kw):
    return dict(now=1800000000, hosts=[{"name": "worker", "slots": slots}], lots=rows, **kw)


def by_id(out):
    return {r["id"]: r for r in out["lots"]}


class CapacityTest(unittest.TestCase):
    def test_31_lots_5_epics_more_capacity_moves_named_lots(self):
        # Generic layered graph; fixture deliberately contains no customer data.
        rows = [lot(i, epic="epic%s" % ((i - 1) % 5),
                    depends_on=[i - 15] if i > 15 else []) for i in range(1, 32)]
        eight = plan.forecast(snapshot(rows, 8))
        twelve = plan.forecast(snapshot(rows, 12), previous=eight)
        self.assertLess(twelve["end_ts"], eight["end_ts"])
        self.assertEqual(len(eight["lots"]), 31)
        self.assertEqual(len(eight["epics"]), 5)
        self.assertTrue(twelve["moved"])
        self.assertTrue(eight["critical_path"])
        for out in (eight, twelve):
            entries = by_id(out)
            for row in rows:
                for dep in row.get("depends_on", []):
                    self.assertGreaterEqual(entries[row["id"]]["start_ts"], entries[dep]["end_ts"])
            for at in {r["start_ts"] for r in out["lots"]}:
                active = [r for r in out["lots"] if r["start_ts"] <= at < r["end_ts"]]
                self.assertLessEqual(len(active), out["slots"])
                self.assertEqual(len({(r["host"], r["slot"]) for r in active}), len(active))

    def test_deadline_capacity_and_serial_impossibility(self):
        data = snapshot([lot(i) for i in range(1, 7)], 2, deadline_ts=1800003600)
        result = plan.forecast(data)["deadline"]
        self.assertFalse(result["met"])
        self.assertEqual(result["required_slots"], 6)
        self.assertEqual(result["additional_slots"], 4)
        data["lots"] = [lot(1), lot(2, depends_on=[1])]
        result = plan.forecast(data)["deadline"]
        self.assertIsNone(result["required_slots"])
        self.assertIn("dependencies", result["reason"])

    def test_actuals_cohort_and_late_estimate_exclusion(self):
        history = [dict(type="evolution", estimate_minutes=240, actual_minutes=357),
                   dict(type="bug", estimate_minutes=10, actual_minutes=1000),
                   dict(type="evolution", estimate_minutes=1, actual_minutes=1000, late=True)]
        out = plan.forecast(snapshot([lot(1, type="evolution")], history=history))
        row = out["lots"][0]
        self.assertEqual(row["calibration_ratio"], 357 / 240)
        self.assertEqual(row["calibration_samples"], 1)
        self.assertEqual(row["end_ts"] - row["start_ts"], 60 * 60 * 357 / 240)

    def test_priority_class_then_creation_without_repository_cap(self):
        rows = [lot(1, created_ts=3), lot(2, priority_class=1, created_ts=2),
                lot(3, priority_class=1, created_ts=1)]
        out = plan.forecast(snapshot(rows, 1))
        self.assertEqual([r["id"] for r in out["lots"]], [3, 2, 1])
        out = plan.forecast(snapshot(rows, 3))
        self.assertEqual(len(out["waves"]), 1)

    def test_project_class_and_dedicated_reservations(self):
        data = snapshot([lot(1, project="other"), lot(2), lot(3)])
        data["hosts"][0]["reservations"] = {"acme": 1}
        data["project_classes"] = {"acme": 1}
        rows = by_id(plan.forecast(data))
        self.assertEqual(rows[1]["reservation"], None)
        self.assertEqual(rows[2]["reservation"], "acme")
        self.assertEqual(rows[3]["reservation"], None)
        self.assertGreaterEqual(rows[1]["start_ts"], rows[3]["end_ts"])

    def test_footprints_typed_dependencies_conflicts_and_shared_paths(self):
        rows = [lot(1, footprint_paths=["src/a"]), lot(2, footprint_paths=["src/a/b.py"]),
                lot(3, repository="acme/other", footprint_paths=["src/a"])]
        out = by_id(plan.forecast(snapshot(rows, 3)))
        self.assertEqual(out[2]["start_ts"], out[1]["end_ts"])
        self.assertEqual(out[3]["start_ts"], out[1]["start_ts"])
        data = snapshot(rows, 3, policies={"acme/repo": {"shared_paths": ["src/a"]}})
        self.assertEqual(len(plan.forecast(data)["waves"]), 1)
        data["links"] = [dict(source=3, target=1, kind="depends_on"),
                         dict(source=2, target=1, kind="conflicts_with")]
        out = by_id(plan.forecast(data))
        self.assertEqual(out[3]["start_ts"], out[1]["end_ts"])
        self.assertEqual(out[2]["start_ts"], out[1]["end_ts"])

    def test_gate_wait_does_not_occupy_ai_slot_and_answer_replans(self):
        data = snapshot([lot(1, gates=[{"approver": "approver"}]), lot(2)], 1,
                        approver_minutes={"approver": 120})
        first = plan.forecast(data)
        rows = by_id(first)
        self.assertEqual(rows[1]["start_ts"], data["now"] + 7200)
        self.assertEqual(rows[2]["start_ts"], data["now"])
        data["lots"][0]["gates"][0]["answered_ts"] = data["now"]
        second = plan.forecast(data, previous=first)
        self.assertEqual(by_id(second)[1]["start_ts"], data["now"])
        self.assertEqual({r["id"] for r in second["moved"]}, {1, 2})

    def test_elapsed_gate_wait_is_subtracted(self):
        data = snapshot([lot(1, gates=[{"requested_ts": 1799996400,
                                      "response_minutes": 120}])], 1)
        out = plan.forecast(data)
        self.assertEqual(out["lots"][0]["start_ts"], data["now"] + 3600)

    def test_merging_early_or_late_and_capacity_change_recompute(self):
        data = snapshot([lot(1), lot(2, depends_on=[1]), lot(3)], 1)
        before = plan.forecast(data)
        data["now"] += 1800
        data["lots"][0]["completed_ts"] = data["now"]
        after = plan.forecast(data, previous=before)
        self.assertEqual(by_id(after)[2]["start_ts"], data["now"])
        self.assertLess(after["end_ts"], before["end_ts"])
        data["hosts"][0]["slots"] = 2
        more = plan.forecast(data, previous=after)
        self.assertLess(more["end_ts"], after["end_ts"])
        data["now"] += 7200
        data["lots"][0]["completed_ts"] = data["now"]
        late = plan.forecast(data, previous=more)
        self.assertGreater(late["end_ts"], more["end_ts"])

    def test_gap_is_filled_and_resource_chain_explains_finish(self):
        data = snapshot([lot(1, not_before_ts=1800007200), lot(2), lot(3)], 1)
        out = plan.forecast(data)
        self.assertEqual([r["id"] for r in out["lots"]], [2, 3, 1])
        out = plan.forecast(snapshot([lot(1), lot(2), lot(3)], 1))
        self.assertEqual(out["critical_path"], [1, 2, 3])

    def test_reject_invalid_graph_estimate_capacity_or_gate(self):
        cases = [snapshot([lot(1), lot(1)]), snapshot([lot(1, depends_on=[2])]),
                 snapshot([lot(1, depends_on=[2]), lot(2, depends_on=[1])]),
                 snapshot([lot(1, estimate_minutes=None)]), snapshot([lot(1)], 0),
                 snapshot([lot(1, estimate_minutes=float("nan"))]),
                 snapshot([lot(1, gates=[{"approver": "unknown"}])]),
                 snapshot([lot(1)], 1.5),
                 snapshot([lot(1)], 1)]
        cases[-1]["hosts"][0]["reservations"] = {"acme": 2}
        for data in cases:
            with self.subTest(data=data), self.assertRaises(plan.PlanningError):
                plan.forecast(data)

    def test_host_constraint_unknown_footprint_and_purity(self):
        data = snapshot([lot(1, footprint_paths=[]), lot(2)], 2,
                        policies={"acme/repo": {"unknown_footprint": "repository"}})
        original = deepcopy(data)
        out = plan.forecast(data)
        self.assertEqual(len(out["waves"]), 2)
        self.assertEqual(data, original)
        data["lots"][0]["host"] = "missing"
        with self.assertRaisesRegex(plan.PlanningError, "no eligible slot"):
            plan.forecast(data)

    def test_cli_publishes_projection_and_rejects_date_mutation(self):
        parser = argparse.ArgumentParser()
        plan_cli.add_parsers(parser.add_subparsers(dest="work_command"), lambda: None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot([lot(1)])))
            args = parser.parse_args(["plan", "--capacity-plan", str(path), "--json"])
            output = io.StringIO()
            db = Mock()
            with contextlib.redirect_stdout(output):
                self.assertEqual(plan_cli.run(db, args), 0)
            self.assertEqual(json.loads(output.getvalue())["schema"], plan.SCHEMA)
            self.assertEqual(db.mock_calls, [])
            args.debut = "2026-11-03"
            with self.assertRaises(work.WorkError):
                plan_cli.run(db, args)


class ProjectSnapshotTest(unittest.TestCase):
    def test_reads_recorded_actuals_and_live_graph_each_time(self):
        rows = [dict(lot(1), app="acme/repo", blocking_dependencies=[], state="intake"),
                dict(lot(2), app="acme/repo", blocking_dependencies=[1], state="intake")]
        settings = snapshot([], 2)
        with patch.object(plan, "project_snapshot", wraps=plan.project_snapshot), \
                patch("ameesh.storage.of") as store, \
                patch("ameesh.work_dependencies.links", return_value={}), \
                patch("ameesh.estimates.report", return_value={"lots": [
                    {"estimate_minutes": 60, "actual_minutes": 90}]}) as history:
            store.return_value.work.project_plan_rows.return_value = rows
            data = plan.project_snapshot(Mock(), "acme", settings)
            first = plan.forecast(data)
            self.assertEqual(first["end_ts"], settings["now"] + 10800)
            self.assertEqual(first["lots"][0]["calibration_ratio"], 1.5)
            rows.pop(0)
            rows[0]["blocking_dependencies"] = []
            second = plan.forecast(plan.project_snapshot(Mock(), "acme", settings), previous=first)
            self.assertEqual(second["end_ts"], settings["now"] + 5400)
            self.assertEqual(history.call_count, 2)

    def test_unmodelled_human_wait_is_not_silently_ignored(self):
        with patch("ameesh.storage.of") as store, \
                patch("ameesh.work_dependencies.links", return_value={}):
            store.return_value.work.project_plan_rows.return_value = [
                dict(lot(1), state="waiting_human")]
            with self.assertRaisesRegex(plan.PlanningError, "explicit gate"):
                plan.project_snapshot(Mock(), "acme", snapshot([]))

    def test_in_progress_consumes_capacity_before_new_admission(self):
        data = snapshot([lot(1, priority_class=0),
                         lot(2, started_ts=1799998200, host="worker", slot=1)], 1)
        rows = by_id(plan.forecast(data))
        self.assertEqual(rows[2]["start_ts"], data["now"])
        self.assertEqual(rows[2]["end_ts"], data["now"] + 1800)
        self.assertEqual(rows[1]["start_ts"], rows[2]["end_ts"])
        data["lots"][0]["started_ts"] = data["now"]
        with self.assertRaisesRegex(plan.PlanningError, "in-progress"):
            plan.forecast(data)
