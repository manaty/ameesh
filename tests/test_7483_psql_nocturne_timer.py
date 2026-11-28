# SPDX-License-Identifier: AGPL-3.0-only
"""#7483: delayed admissions, elected timer, bounded shared-runner occupancy."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon, psql_nocturne as nocturne, role_timers

REPO = Path(__file__).resolve().parents[1]


def when(hour, minute=0):
    return dt.datetime(2026, 11, 15, hour, minute, tzinfo=dt.timezone.utc)


def cfg():
    return SimpleNamespace(github={"projects": {
        "tool": "acme/tool", "app": {"repo": "acme/app", "base": ["develop"]}}})


class FakeGh:
    def __init__(self, failure=""):
        self.calls = []
        self.failure = failure

    def run(self, args, stdin=None):
        self.calls.append((args, stdin))
        if self.failure in ("read", "post") and ((stdin is None) == (self.failure == "read")):
            return 1, "", "HTTP 500"
        if stdin is None and "acme/app/contents" in args[1]:
            return 1, "", "HTTP 404"
        return 0, "{}", ""


class AdmissionTest(unittest.TestCase):
    def test_window_boundaries_and_delayed_run(self):
        for hour, minute, expected in ((0, 0, True), (1, 47, True),
                                        (5, 59, True), (6, 0, False),
                                        (8, 26, False), (21, 0, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(nocturne.admitted(when(hour, minute)), expected)
        zone = dt.timezone(dt.timedelta(hours=8))
        self.assertTrue(nocturne.admitted(when(1, 47).astimezone(zone)))

    def test_dispatch_and_each_part_use_current_time_not_schedule_time(self):
        gh = FakeGh()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(nocturne.main(["dispatch"], gh=gh, cfg=cfg(), now=when(8)), 0)
            self.assertEqual(nocturne.main(["guard"], now=when(8)), 1)
        self.assertEqual(gh.calls, [])

    def test_skipped_workflow_has_explicit_summary_and_no_false_green_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            output, summary = Path(directory) / "out", Path(directory) / "summary"
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(output),
                                              "GITHUB_STEP_SUMMARY": str(summary)}), \
                    contextlib.redirect_stdout(io.StringIO()) as log:
                self.assertEqual(nocturne.main(["gate"], now=when(8)), 0)
            self.assertEqual(output.read_text(), "lancer=false\n")
            self.assertIn("deferred", summary.read_text())
            self.assertIn("::notice::", log.getvalue())

    def test_guard_failure_is_explicit_without_cancel(self):
        with contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(nocturne.main(["guard"], now=when(6)), 1)
        self.assertIn("::error::Incomplete psql nocturne", log.getvalue())


class DispatchTest(unittest.TestCase):
    def run_dispatch(self, gh, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return nocturne.main(["dispatch", *args], gh=gh, cfg=cfg(), now=when(1, 47))

    def test_only_repositories_with_workflow_are_dispatched_on_declared_base(self):
        gh = FakeGh()
        self.assertEqual(self.run_dispatch(gh), 0)
        posts = [(args, json.loads(body)) for args, body in gh.calls if body]
        self.assertEqual(len(posts), 1)
        self.assertIn("repos/acme/tool/actions/workflows/psql-nocturne.yml/dispatches", posts[0][0])
        self.assertEqual(posts[0][1], {"ref": "main"})
        self.assertTrue(any("?ref=develop" in args[1] for args, _ in gh.calls if _ is None))

    def test_dry_run_never_posts(self):
        gh = FakeGh()
        self.assertEqual(self.run_dispatch(gh, "--dry-run"), 0)
        self.assertFalse(any(body for _, body in gh.calls))

    def test_api_failure_is_not_absent_or_silent(self):
        for failure in ("read", "post"):
            with self.subTest(failure=failure):
                self.assertEqual(self.run_dispatch(FakeGh(failure)), 1)


class WorkflowTest(unittest.TestCase):
    def test_admission_precedes_pool_and_matrix_rechecks_after_checkout(self):
        workflow = canon.load_yaml((REPO / ".github/workflows/psql-nocturne.yml").read_text())
        self.assertNotIn("schedule", workflow.get("on", workflow.get(True)))
        gate = workflow["jobs"]["fenetre"]
        self.assertEqual(gate["runs-on"], "ubuntu-latest")
        self.assertEqual(workflow["jobs"]["suite"]["needs"], "fenetre")
        self.assertIn("outputs.lancer == 'true'", workflow["jobs"]["suite"]["if"])
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        tests = canon.load_yaml((REPO / ".github/workflows/tests.yml").read_text())
        job = tests["jobs"]["psql"]
        self.assertFalse(job["strategy"]["fail-fast"])
        # #8436 : 12 parts psql hors nocturne, 4 au nocturne
        self.assertEqual(job["strategy"]["max-parallel"], "${{ inputs.psql-full && 4 || 12 }}")
        steps = job["steps"]
        guard = next(i for i, step in enumerate(steps) if step.get("run") == "python3 -m ameesh psql-nocturne guard")
        checkout = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/checkout@"))
        self.assertGreater(guard, checkout)
        self.assertEqual(steps[guard]["if"], "inputs.psql-full")
        self.assertLessEqual(nocturne.END_HOUR * 60 + job["timeout-minutes"], 7 * 60)

    def test_daily_timer_installed_under_elected_role_only(self):
        from tests.test_6777_carte_role_timer import plan, row
        report = plan()
        item = next(t for t in report["timers"] if t["name"] == "psql-nocturne")
        self.assertEqual(item["unit"], "ameesh-psql-nocturne")
        spec = role_timers.TimerSpec(role_timers.NOCTURNE_PASS, role_timers.NOCTURNE_PASS,
                                    role_timers.NOCTURNE_INTERVAL_S, role_timers.NOCTURNE_DESCRIPTION)
        self.assertEqual((REPO / "deploy/systemd/ameesh-psql-nocturne.timer").read_text(),
                         role_timers.render_timer(spec))
        self.assertEqual((REPO / "deploy/systemd/ameesh-psql-nocturne.service").read_text(),
                         role_timers.render_service(spec, role="mesh-design"))
        self.assertNotIn("psql-nocturne", [t["name"] for t in plan(
            role="auditeur",
            row_value=dict(row(), name="auditeur", roles=["auditeur"]))["timers"]])
        self.assertNotIn("psql-nocturne", [t["name"] for t in plan(projects={})["timers"]])
        with tempfile.TemporaryDirectory() as directory:
            role_timers.install(report, directory=Path(directory), apply=False, bin_path="/opt/ameesh/bin/ameesh")
            service = (Path(directory) / "ameesh-psql-nocturne.service").read_text()
            timer = (Path(directory) / "ameesh-psql-nocturne.timer").read_text()
            self.assertIn("role run mesh-design -- /opt/ameesh/bin/ameesh psql-nocturne dispatch", service)
            self.assertIn("OnCalendar=*-*-* 01:47:00 UTC", timer)
            self.assertIn("Persistent=true", timer)
