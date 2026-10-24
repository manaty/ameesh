# SPDX-License-Identifier: AGPL-3.0-only
"""#3402: a candidate without the owner's route must not fail the staging check.

#3383 blocked the `alerts` pass on a candidate whose `notify` key pushes to
nobody, and counted it as a problem: `role timers --check` and `--install`
exited 1, so `vm.sh agents controler` failed and a staging deploy of the VMs
rolled back while the owner's secret lived on the post only. Canon §3: an
absent credential is not a negative result. Witnesses: the blocked `alerts`
pass is still named (report, warning), exits 0; a blocked `github.projects`
pass (#3327) still fails.
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import role_timers

NO_ROUTE = "human:owner has no route on this host"
WATCH = SimpleNamespace(project="ameesh", repo="manaty/ameesh", interval=900)


def row():
    return {"name": "mesh-design", "releve": "election", "host": "agents-a",
            "candidats": ["agents-a", "agents-b"], "responsible": "human:owner"}


def plan(projects=True, **kw):
    settings = SimpleNamespace(projects={"ameesh": "manaty/ameesh"} if projects else {},
                               interval=900)
    return role_timers.plan(host="agents-a", role="mesh-design", row=row(),
                            settings=settings, watch_intervals=[900], **kw)


class AdvisoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="3402-")
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_route_is_advisory_and_check_stays_green(self):
        report = plan(notify_humans=[], notify_error=NO_ROUTE)
        alerts = next(item for item in report["timers"] if item["name"] == "alerts")
        self.assertEqual((alerts["blocked"], alerts["advisory"]), (NO_ROUTE, True))
        role_timers.install(report, directory=self.dir, apply=False)
        self.assertEqual(role_timers.check(report, directory=self.dir, apply=False), [])
        self.assertEqual(len(role_timers.warnings(report)), 1)

    def test_a_routed_pass_is_not_advisory(self):
        report = plan(notify_humans=["human:owner"])
        self.assertEqual(role_timers.warnings(report), [])
        self.assertFalse(any(item["advisory"] for item in report["timers"]))

    def test_github_projects_missing_still_fails(self):
        report = plan(projects=False, notify_humans=[], notify_error=NO_ROUTE)
        role_timers.install(report, directory=self.dir, apply=False)
        problems = role_timers.check(report, directory=self.dir, apply=False)
        self.assertTrue(any(p.startswith("issues: github.projects missing")
                            for p in problems), problems)
        self.assertFalse(any(p.startswith("alerts:") for p in problems), problems)

    def test_non_candidate_has_no_warning(self):
        report = role_timers.plan(host="elsewhere", role="mesh-design", row=row(),
                                  settings=None, watch_intervals=())
        self.assertEqual(role_timers.warnings(report), [])


class MainExitTest(unittest.TestCase):
    """What vm.sh sees: `timers` and `controler` exit codes."""

    def run_main(self, argv, *, projects=True):
        cfg = SimpleNamespace(host="agents-a", notify={}, config_path="", config_base="",
                              github={"projects": {"ameesh": "manaty/ameesh"}})
        settings = SimpleNamespace(projects={"ameesh": "manaty/ameesh"} if projects else {},
                                   interval=900)
        err, out = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory(prefix="3402-main-") as tmp, \
                mock.patch.object(role_timers.config_mod, "load", return_value=cfg), \
                mock.patch.object(role_timers.db_mod, "connect", return_value=mock.Mock()), \
                mock.patch.object(role_timers.registry, "get", return_value=row()), \
                mock.patch.object(role_timers, "unit_env", return_value={}), \
                mock.patch.object(role_timers.plan_github, "settings_of",
                                  return_value=settings), \
                mock.patch.object(role_timers, "_watch_declarations",
                                  return_value=([WATCH], "")), \
                redirect_stderr(err), redirect_stdout(out):
            code = role_timers.main(argv + ["--no-apply", "--no-sync", "--systemd-dir", tmp])
        return code, err.getvalue()

    def test_install_and_check_exit_0_without_the_owner_route(self):
        code, err = self.run_main(["--install"])
        self.assertEqual(code, 0, err)
        self.assertIn("role timers: warning: alerts not installed", err)
        self.assertNotIn("role timers: alerts not installed", err)

    def test_check_names_the_gap_without_failing_on_it(self):
        self.run_main(["--install"])
        with mock.patch.object(role_timers, "check", return_value=[]):
            code, err = self.run_main(["--check"])
        self.assertEqual(code, 0, err)
        self.assertIn("role timers: warning: alerts: ", err)

    def test_a_real_problem_still_fails_check_and_keeps_the_warning(self):
        with mock.patch.object(role_timers, "check", return_value=["ci: missing x"]):
            code, err = self.run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("role timers: ci: missing x", err)
        self.assertIn("role timers: warning: alerts: ", err)


if __name__ == "__main__":
    unittest.main()
