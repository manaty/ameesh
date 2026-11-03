# SPDX-License-Identifier: AGPL-3.0-only
"""Automatic forecast publication and silent replays."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from ameesh import capacity_replan as replan
from tests.test_5595_capacity_plan import lot, snapshot, by_id


class ReplanTest(unittest.TestCase):
    def setUp(self):
        self.saved = None
        self.store = Mock()
        self.db = MagicMock()
        self.db.transaction.return_value.__enter__.return_value = self.db
        self.store.events.recent_type.side_effect = lambda *a, **kw: (
            [{"data": deepcopy(self.saved)}] if self.saved else [])
        self.store.events.record.side_effect = self.record
        self.plans = {i: {"id": i, "planned_start": None, "planned_end": None,
                          "planned_source": None} for i in range(1, 4)}
        self.store.roadmap.item_plans.side_effect = lambda ids: [self.plans[i] for i in ids]
        self.store.roadmap.plan_item.side_effect = self.write
        self.patches = [patch.object(replan.storage, "of", return_value=self.store),
                        patch.object(replan.storage, "is_remote", return_value=False),
                        patch.object(replan.work_dependencies, "lock"),
                        patch.object(replan.work, "note")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.data = snapshot([lot(1), lot(2, depends_on=[1]), lot(3)], 1)
        self.live = patch.object(replan, "live_snapshot", side_effect=lambda *a: deepcopy(self.data))
        self.live.start()
        self.addCleanup(self.live.stop)

    def record(self, kind, obj, project, data):
        self.saved = deepcopy(data)

    def write(self, ident, dates, **kw):
        self.plans[ident].update(planned_start=dates["start"], planned_end=dates["end"],
                                 planned_source=kw["source"])
        return self.plans[ident]

    def run_plan(self):
        return replan.reconcile(self.db, "acme", {}, now=self.data["now"])

    def test_replay_and_clock_tick_do_not_publish_or_write(self):
        self.assertEqual(self.run_plan()["published"], [1, 2, 3])
        self.store.reset_mock()
        replan.work.note.reset_mock()
        self.data["now"] += 60
        self.assertEqual(self.run_plan(), {"changed": False, "published": []})
        self.store.events.record.assert_not_called()
        self.store.roadmap.plan_item.assert_not_called()
        replan.work.note.assert_not_called()

    def test_changed_input_with_identical_forecast_does_not_publish(self):
        self.run_plan()
        self.store.reset_mock()
        replan.work.note.reset_mock()
        self.data["approver_minutes"] = {"unused": 120}
        self.assertFalse(self.run_plan()["changed"])
        self.store.events.record.assert_not_called()
        replan.work.note.assert_not_called()

    def test_early_and_late_merge_move_remaining_lots(self):
        self.run_plan()
        before = self.saved["forecast"]
        self.data["now"] += 1800
        self.data["lots"][0]["completed_ts"] = self.data["now"]
        self.assertIn(2, self.run_plan()["published"])
        self.assertLess(by_id(self.saved["forecast"])[2]["end_ts"], by_id(before)[2]["end_ts"])
        self.data["now"] += 7200
        self.data["lots"][0]["completed_ts"] = self.data["now"]
        self.assertIn(2, self.run_plan()["published"])
        self.assertGreater(by_id(self.saved["forecast"])[2]["end_ts"], by_id(before)[2]["end_ts"])

    def test_gate_answer_and_capacity_move_dates(self):
        self.data = snapshot([lot(1, gates=[{"response_minutes": 120}]), lot(2)], 1)
        self.run_plan()
        self.data["lots"][0]["gates"][0]["answered_ts"] = self.data["now"]
        self.assertEqual(set(self.run_plan()["published"]), {1, 2})
        self.data["hosts"][0]["slots"] = 2
        self.assertEqual(self.run_plan()["published"], [2])
        self.store.reset_mock()
        self.assertFalse(self.run_plan()["changed"])
        self.store.events.record.assert_not_called()

    def test_manual_dates_are_preserved_but_forecast_is_visible(self):
        self.plans[1].update(planned_start="2027-01-01", planned_end="2027-01-02",
                             planned_source="set by human:planner")
        self.run_plan()
        self.assertEqual(self.plans[1]["planned_start"], "2027-01-01")
        self.assertIn(1, by_id(self.saved["forecast"]))
        self.assertTrue(any("manual dates kept" in c.args[2] for c in replan.work.note.call_args_list))

    def test_failure_to_write_dates_does_not_checkpoint(self):
        self.store.roadmap.plan_item.side_effect = lambda *a, **kw: None
        with self.assertRaisesRegex(replan.capacity_plan.PlanningError, "concurrently"):
            self.run_plan()
        self.assertIsNone(self.saved)


class LiveInputsTest(unittest.TestCase):
    def test_answer_is_read_from_decision_and_completed_overrides_removed(self):
        settings = snapshot([lot(1, gates=[{"decision_id": 9, "response_minutes": 60}]),
                             lot(2)], 1)
        with patch.object(replan.storage, "of") as store, \
                patch("ameesh.decisions.get", return_value={"lot": 1, "state": "answered",
                                                            "answer": {"ts": 1800000010}}), \
                patch.object(replan.capacity_plan, "project_snapshot", side_effect=lambda d,p,s:s):
            store.return_value.work.project_plan_rows.return_value = [{"id": 1}]
            store.return_value.work.get.return_value = {"id": 2, "state": "delivered"}
            out = replan.live_snapshot(Mock(), "acme", settings, 1800000020)
        self.assertEqual(len(out["lots"]), 1)
        self.assertEqual(out["lots"][0]["gates"][0]["answered_ts"], 1800000010)
        self.assertNotIn("answered_ts", settings["lots"][0]["gates"][0])

    def test_unknown_or_other_project_override_is_rejected(self):
        for item in (None, {"id": 2, "state": "build"}):
            with self.subTest(item=item), patch.object(replan.storage, "of") as store:
                store.return_value.work.project_plan_rows.return_value = [{"id": 1}]
                store.return_value.work.get.return_value = item
                with self.assertRaisesRegex(replan.capacity_plan.PlanningError, "outside project"):
                    replan.live_snapshot(Mock(), "acme", snapshot([lot(2)]), 1800000000)

    def test_issues_pass_replans_without_github_configuration(self):
        from ameesh.notify import Notifier
        notifier = Notifier.__new__(Notifier)
        notifier.cfg = SimpleNamespace(github={}, project_planning={})
        notifier.dry_run = False
        notifier.log = Mock()
        with patch.object(replan, "tick") as tick:
            notifier._github_issues(Mock(), 1800000000)
        tick.assert_called_once()
        self.assertEqual(tick.call_args.kwargs["now"], 1800000000)

    def test_cli_reads_saved_forecast_without_replanning(self):
        import argparse
        import contextlib
        import io
        import json
        from ameesh import plan_cli
        parser = argparse.ArgumentParser()
        plan_cli.add_parsers(parser.add_subparsers(dest="work_command"), lambda: None)
        args = parser.parse_args(["plan", "--project", "acme", "--json"])
        forecast = replan.capacity_plan.forecast(snapshot([lot(1)]))
        output = io.StringIO()
        with patch("ameesh.project_plan.report", return_value={"phase": "evolution", "lots": []}), \
                patch.object(replan, "latest", return_value={"forecast": forecast}), \
                patch.object(replan, "reconcile") as reconcile, contextlib.redirect_stdout(output):
            self.assertEqual(plan_cli.run(Mock(), args), 0)
        self.assertEqual(json.loads(output.getvalue())["capacity_forecast"], forecast)
        reconcile.assert_not_called()

    def test_tick_reads_capacity_file_again_and_isolates_bad_projects(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capacity.json"
            cfg = SimpleNamespace(project_planning={"broken": {"capacity_plan": str(path)+"-missing"},
                                                    "acme": {"capacity_plan": str(path)}})
            with patch.object(replan, "reconcile") as reconcile:
                path.write_text(json.dumps(snapshot([], 1)))
                replan.tick(cfg, Mock(), now=1)
                self.assertEqual(reconcile.call_args.args[2]["hosts"][0]["slots"], 1)
                path.write_text(json.dumps(snapshot([], 2)))
                replan.tick(cfg, Mock(), now=2)
                self.assertEqual(reconcile.call_args.args[2]["hosts"][0]["slots"], 2)
                reconcile.reset_mock()
                replan.tick(cfg, Mock(), now=3, dry_run=True)
                reconcile.assert_not_called()


from tests.support import PgTestCase
from ameesh import storage, work, work_dependencies


class PublicationDbTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.project = "acme-" + self._testMethodName
        self.first = work.add(self.db, title="First forecast fixture", app=self.project, estimate="1h")
        self.second = work.add(self.db, title="Dependent forecast fixture", app=self.project, estimate="1h")
        work_dependencies.add(self.db, self.second["id"], self.first["id"], actor="ameesh")
        self.settings = {"hosts": [{"name": "worker", "slots": 1}]}
        self.now = 1800000000

    def test_real_publication_then_silent_replay_and_merge(self):
        first = replan.reconcile(self.db, self.project, self.settings, now=self.now)
        self.assertEqual(set(first["published"]), {self.first["id"], self.second["id"]})
        saved = replan.latest(self.db, self.project)
        self.assertEqual(len(saved["forecast"]["lots"]), 2)
        before = work.events(self.db, self.second["id"])
        self.assertFalse(replan.reconcile(self.db, self.project, self.settings, now=self.now+60)["changed"])
        self.assertEqual(work.events(self.db, self.second["id"]), before)
        # Model the same durable close that the merge observer writes; the
        # planner rereads the remaining graph rather than retaining old lots.
        work.close_merged(self.db, self.first["id"], sha="a" * 40, actor="ameesh", unfrozen=True)
        replan.reconcile(self.db, self.project, self.settings, now=self.now+1800)
        after = replan.latest(self.db, self.project)["forecast"]
        self.assertEqual([r["id"] for r in after["lots"]], [self.second["id"]])
        self.assertLess(after["end_ts"], saved["forecast"]["end_ts"])
        self.assertTrue(any("Capacity forecast:" in row.get("note", "")
                            for row in work.events(self.db, self.second["id"])))

    def test_note_failure_rolls_back_dates_and_checkpoint(self):
        with patch.object(replan.work, "note", side_effect=RuntimeError("injected journal failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                replan.reconcile(self.db, self.project, self.settings, now=self.now)
        self.assertIsNone(replan.latest(self.db, self.project))
        self.assertIsNone(storage.of(self.db).roadmap.item_plans([self.first["id"]])[0]["planned_start"])

    def test_latest_checkpoint_survives_event_retention(self):
        for _ in range(2):
            self.db.execute("INSERT INTO ameesh_events (type, object, project, data, created_at)"
                            " VALUES ('capacity.plan', %s, %s, %s::jsonb, now() - interval '400 days')",
                            (self.project, self.project, '{"fingerprint":"fixture"}'))
        storage.of(self.db).events.prune(365, 1000)
        rows = storage.of(self.db).events.recent_type(replan.EVENT, None, 10, object=self.project)
        self.assertEqual(len(rows), 1)
        self.assertEqual(replan.latest(self.db, self.project), {"fingerprint": "fixture"})

    def test_two_projectors_publish_one_checkpoint(self):
        import threading
        from ameesh import db as db_mod
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run():
            connection = db_mod.connect(self.cfg)
            try:
                barrier.wait(10)
                results.append(replan.reconcile(connection, self.project, self.settings, now=self.now))
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(result["changed"] for result in results), [False, True])
        self.assertEqual(len(storage.of(self.db).events.recent_type(
            replan.EVENT, None, 10, object=self.project)), 1)
