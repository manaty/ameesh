# SPDX-License-Identifier: AGPL-3.0-only
"""#5853: role timers survive a restart of user@, and `--check` sees a dead one.

On AG2, restarting user@1001 left six role timers "active (elapsed)" with no
next elapse: `OnBootSec=` was long past and the service had never run under
the new manager, so `OnUnitActiveSec=` had no base. `--check` said OK because
`is-active` is true for an elapsed timer. The witness: the rendered timers
(role passes, release tick, deploy/systemd copies) count from `OnActiveSec=`,
and `check` names an elapsed timer with the command that rearms it.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import role_timers

ROOT = Path(__file__).resolve().parents[1]
BIN = "/opt/ameesh/bin/ameesh"


def report():
    return role_timers.plan(
        host="agents-a", role="mesh-design",
        row={"name": "mesh-design", "releve": "election", "host": "agents-a",
             "candidats": ["agents-a", "agents-b"]},
        settings=SimpleNamespace(projects={"ameesh": "manaty/ameesh"}, interval=900),
        watch_intervals=(900,), notify_humans=["human:owner"])


def systemctl(substate, realtime="", monotonic="infinity"):
    def run(*args, timeout=30.0):
        if args[0] == "show":
            return mock.Mock(returncode=0, stderr="", stdout=(
                "SubState=%s\nNextElapseUSecRealtime=%s\nNextElapseUSecMonotonic=%s\n"
                % (substate, realtime, monotonic)))
        return mock.Mock(returncode=0, stdout="", stderr="")
    return run


class RenderTest(unittest.TestCase):
    def test_no_rendered_timer_counts_from_boot(self):
        specs = [role_timers.TimerSpec(item["name"], item["only"],
                                       int(item["interval_s"]), item["description"])
                 for item in report()["timers"]]
        specs.append(role_timers.TimerSpec(role_timers.RELEASE_PASS, "", 60, ""))
        for spec in specs:
            text = role_timers.render_timer(spec)
            self.assertNotIn("OnBootSec", text, spec.name)
            if "OnUnitActiveSec" in text:
                self.assertIn("OnActiveSec=", text, spec.name)

    def test_deploy_user_timers_count_from_activation(self):
        for name in ("credentials", "host-publish", "release", "release-nexlink",
                     "staging"):
            text = (ROOT / ("deploy/systemd/ameesh-%s.timer" % name)).read_text(
                encoding="utf-8")
            self.assertNotIn("OnBootSec", text, name)
            self.assertIn("OnActiveSec=", text, name)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="5853-units-")
        self.dir = Path(self.tmp.name)
        self.report = report()
        role_timers.install(self.report, directory=self.dir, bin_path=BIN, apply=False)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, run):
        with mock.patch.object(role_timers, "_systemctl", side_effect=run):
            return role_timers.check(self.report, directory=self.dir, bin_path=BIN)

    def test_an_elapsed_timer_is_a_problem_with_its_rearm_command(self):
        problems = self.check(systemctl("elapsed"))
        self.assertEqual(len(problems), len(self.report["timers"]))
        self.assertIn("github-watch: ameesh-role-github-watch.timer is active but "
                      "has no next elapse (elapsed); rearm it: systemctl --user "
                      "restart ameesh-role-github-watch.timer", problems)

    def test_waiting_without_any_deadline_is_a_problem(self):
        self.assertTrue(self.check(systemctl("waiting", "", "infinity")))

    def test_a_waiting_or_running_timer_passes(self):
        self.assertEqual(self.check(systemctl("waiting", "", "1min 30s")), [])
        self.assertEqual(self.check(systemctl("waiting", "Thu 2026-11-05 02:00:00 UTC")), [])
        # the service is running: OnUnitInactiveSec has no deadline yet
        self.assertEqual(self.check(systemctl("running")), [])

    def test_the_alerts_gate_names_an_elapsed_timer(self):
        with mock.patch.object(role_timers, "_systemctl", side_effect=systemctl("elapsed")):
            text = role_timers.alerts_timer_gap(role="mesh-design", directory=self.dir,
                                               bin_path=BIN)
        self.assertIn("ameesh-role-alerts.timer is active but has no next elapse", text)

    def test_install_restarts_a_timer_left_elapsed(self):
        calls = []

        def run(*args, timeout=30.0):
            calls.append(args)
            return systemctl("elapsed")(*args, timeout=timeout)
        with mock.patch.object(role_timers, "_systemctl", side_effect=run):
            role_timers._apply("install", ["ameesh-role-ci.timer"])
        self.assertIn(("restart", "ameesh-role-ci.timer"), calls)


if __name__ == "__main__":
    unittest.main()
