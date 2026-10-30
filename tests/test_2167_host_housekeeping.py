# SPDX-License-Identifier: AGPL-3.0-only
"""Host housekeeping is shared by runners; retained paths need no Git scan."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from . import support  # isolate the host's configuration before exercising the runner
from ameesh import housekeeping, platform, runner


class HostPassTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-2167-")
        self.addCleanup(temporary.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=temporary.name)
        self.policy = housekeeping.Policy()

    def passage(self, **kwargs):
        return housekeeping.run_pass(self.cfg, object(), self.policy,
                                     host="pc", actor="test", **kwargs)

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_two_runners_share_one_pass_with_the_cli(self):
        def make_runner():
            return types.SimpleNamespace(
                db=None, _housekeeping_lock=threading.Lock(),
                _housekeeping_pass=lambda **kwargs: self.passage(**kwargs))

        first, second = make_runner(), make_runner()

        def during_first(*args, **kwargs):
            other = runner.Runner.housekeeping_once(second)
            cli = self.passage(dry_run=True)
            for report in (other, cli):
                self.assertEqual(report["plan"], [])
                self.assertIn("another housekeeping pass", report["skipped"][0])
            return {"entries": []}

        with mock.patch.object(housekeeping, "_run_pass", side_effect=during_first) as run:
            runner.Runner.housekeeping_once(first)
        run.assert_called_once()

    def test_host_interval_prevents_staggered_runners_repeating_the_scan(self):
        with mock.patch.object(housekeeping, "_run_pass", return_value={}) as run:
            self.passage(now=1000, min_interval_s=600)
            skipped = self.passage(now=1001, min_interval_s=600)
            self.assertEqual(skipped["status"], "skipped")
            self.assertIn("already ran", skipped["skipped"][0])
            run.assert_called_once()
            self.passage(now=1600, min_interval_s=600)
        self.assertEqual(run.call_count, 2)

    def test_a_skipped_pass_prints_skipped_never_executed_or_dry_run(self):
        """#2173: the host lock skipped the pass, so nothing ran and nothing
        is "nothing to do" — the concurrent pass is doing the work."""
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                seen = {}

                def during_first(*args, **kwargs):
                    report = self.passage(dry_run=dry_run)
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        housekeeping.print_report(report)
                    seen["report"], seen["text"] = report, out.getvalue()
                    return {"entries": []}

                with mock.patch.object(housekeeping, "_run_pass",
                                       side_effect=during_first):
                    self.passage()
                self.assertEqual(seen["report"]["status"], "skipped")
                self.assertEqual(seen["report"]["skipped"],
                                 ["another housekeeping pass is running on this host"])
                self.assertEqual(
                    seen["text"],
                    "housekeeping of pc — skipped — another housekeeping pass is "
                    "running on this host\n")
                for lie in ("executed", "nothing to do", "DRY RUN"):
                    self.assertNotIn(lie, seen["text"])

    def test_pass_exclusion_is_visible_in_another_process(self):
        script = """
import json, sys, types
from ameesh import housekeeping
def forbidden(*args, **kwargs):
    raise AssertionError('concurrent housekeeping')
housekeeping._run_pass = forbidden
cfg = types.SimpleNamespace(state_dir=sys.argv[1])
print(json.dumps(housekeeping.run_pass(cfg, None, housekeeping.Policy(),
                                     host='pc', actor='other-runner')))
"""

        def during_first(*args, **kwargs):
            child = subprocess.run([sys.executable, "-c", script, self.cfg.state_dir],
                                   text=True, capture_output=True, check=True, timeout=10)
            self.assertIn("another housekeeping pass", json.loads(child.stdout)["skipped"][0])
            return {}

        with mock.patch.object(housekeeping, "_run_pass", side_effect=during_first):
            self.passage()

    def test_another_mesh_sharing_the_state_directory_keeps_its_own_schedule(self):
        self.cfg.dsn, self.cfg.schema = "postgresql://localhost/mesh", "first"
        with mock.patch.object(housekeeping, "_run_pass", return_value={}) as run:
            self.passage(now=1000, min_interval_s=600)
            self.cfg.schema = "second"
            self.passage(now=1001, min_interval_s=600)
            self.cfg.schema = "first"
            skipped = self.passage(now=1002, min_interval_s=600)
        self.assertEqual(run.call_count, 2)
        self.assertIn("already ran", skipped["skipped"][0])

    def test_pressure_pass_can_bypass_the_periodic_interval(self):
        with mock.patch.object(housekeeping, "_run_pass", return_value={}) as run:
            self.passage(now=1000, min_interval_s=600)
            self.passage(now=1001)
        self.assertEqual(run.call_count, 2)

    def test_dry_run_does_not_postpone_a_real_pass(self):
        with mock.patch.object(housekeeping, "_run_pass", return_value={}) as run:
            self.passage(now=1000, dry_run=True)
            self.passage(now=1001, min_interval_s=600)
        self.assertEqual(run.call_count, 2)

    def test_failed_pass_releases_the_lock_and_does_not_postpone_a_retry(self):
        with mock.patch.object(housekeeping, "_run_pass",
                               side_effect=[RuntimeError("interrupted"), {}]) as run:
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.passage(now=1000, min_interval_s=600)
            self.passage(now=1001, min_interval_s=600)
        self.assertEqual(run.call_count, 2)

    def test_clock_going_backwards_does_not_disable_housekeeping(self):
        with mock.patch.object(housekeeping, "_run_pass", return_value={}) as run:
            self.passage(now=1000, min_interval_s=600)
            self.passage(now=900, min_interval_s=600)
        self.assertEqual(run.call_count, 2)


class RetainedPathsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-2167-")
        self.addCleanup(temporary.cleanup)
        self.base = temporary.name
        self.path = os.path.join(self.base, "review-copy")
        os.mkdir(self.path)
        self.now = os.stat(self.path).st_mtime + 3 * 86400

    def test_held_or_unreadable_worktrees_do_not_run_git_on_repeated_passes(self):
        row = {"id": 1, "path": self.path, "agent": "quiet", "lot": None,
               "status": "active", "created_ts": self.now - 3 * 86400}
        store = mock.Mock()
        store.housekeeping.worktrees.return_value = [row]
        for holders in ([{"pid": 42}], platform.NotAvailable("holders", "unreadable")):
            with self.subTest(holders=holders), \
                    mock.patch.object(housekeeping.storage, "of", return_value=store), \
                    mock.patch.object(platform, "holders", side_effect=(
                        holders if isinstance(holders, Exception) else lambda *a, **k: holders)), \
                    mock.patch.object(housekeeping, "worktree_state") as git:
                for _ in range(2):
                    entries, plan = housekeeping._worktree_pass(
                        object(), "pc", dry_run=True, protected=set(), now=self.now)
                    self.assertEqual(entries, [])
                    self.assertEqual(plan[0]["action"], "attend")
                git.assert_not_called()

    def test_held_or_unreadable_orphans_do_not_run_git_on_repeated_passes(self):
        row = {"path": self.path, "agent": "quiet", "data": {}}
        for holder in ("held by process 42", housekeeping.HOLDERS_UNREADABLE):
            with self.subTest(holder=holder), \
                    mock.patch.object(housekeeping, "tmp_holder", return_value=holder), \
                    mock.patch.object(housekeeping, "unsaved_git_work") as git:
                for _ in range(2):
                    action, reason = housekeeping._orphan_verdict(
                        row, housekeeping.Policy(), base=self.base, registered={"quiet"},
                        protected=set(), running=set(), now=self.now)
                    self.assertEqual((action, reason), ("attend", holder))
                git.assert_not_called()


class PollTest(unittest.TestCase):
    def test_startup_is_staggered_and_following_passes_share_the_host_interval(self):
        fake = types.SimpleNamespace(
            cfg=types.SimpleNamespace(housekeeping_interval=600), once=False,
            mediated=False, stop=mock.Mock(), housekeeping_once=mock.Mock())
        fake.stop.wait.side_effect = [False, True]

        def start_thread(**kwargs):
            return types.SimpleNamespace(start=kwargs["target"])

        with mock.patch("random.uniform", return_value=123), \
                mock.patch.object(runner.threading, "Thread", side_effect=start_thread):
            runner.Runner.start_housekeeping_poll(fake)
        self.assertEqual(fake.stop.wait.call_args_list, [mock.call(723), mock.call(600)])
        fake.housekeeping_once.assert_called_once_with(min_interval_s=600)


if __name__ == "__main__":
    unittest.main()
