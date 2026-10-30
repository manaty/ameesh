# SPDX-License-Identifier: AGPL-3.0-only
"""Repeated cutoff failures must reach the operator each cutoff."""
import tempfile
import unittest
from unittest import mock
from ameesh import config, release, release_cutoff


class ErrorReportTest(unittest.TestCase):
    def test_same_failure_is_reported_once_per_cutoff(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config.Config(state_dir=folder)
            project = release_cutoff.ProjectRelease(
                name="ameesh", repo=folder, target="main", report="bureau")
            with mock.patch.object(release_cutoff, "compiled_rules", return_value=(None, None)), \
                    mock.patch.object(release_cutoff, "cutoff_slot",
                                      side_effect=[(100, "a"), (100, "a"), (200, "b")]), \
                    mock.patch.object(release_cutoff, "send_report") as send:
                for now in (101, 102, 201):
                    release._report_error(cfg, object(), project,
                                          release_cutoff.ReleaseError("major"), now)
                self.assertEqual(send.call_count, 2)
            slot = release_cutoff.load_state(cfg)["projects"]["ameesh"]
            self.assertEqual(slot["last_error_at"], 201)
            self.assertEqual(slot["last_error_cutoff"], 200)


    def test_late_tick_keeps_the_failure_from_its_window(self):
        from types import SimpleNamespace
        for error_at, expected in ((101, True), (99, False)):
            with self.subTest(error_at=error_at), tempfile.TemporaryDirectory() as folder:
                cfg = config.Config(state_dir=folder)
                project = release_cutoff.ProjectRelease(
                    name="ameesh", repo=folder, target="main", report="bureau")
                release_cutoff.save_state(cfg, {"schema": release_cutoff.SCHEMA, "projects": {"ameesh": {
                    "last_error": "major", "last_error_at": error_at}}})
                with mock.patch.object(release_cutoff, "compiled_rules",
                                       return_value=(None, SimpleNamespace(commit="canon"))), \
                        mock.patch.object(release_cutoff, "cutoff_slot", return_value=(100, "a")), \
                        mock.patch.object(release_cutoff, "due", return_value={"missed": True}), \
                        mock.patch.object(release_cutoff, "cutoff_date", return_value="date"), \
                        mock.patch.object(release_cutoff, "fetch"), \
                        mock.patch.object(release_cutoff, "recent_tag", return_value=None), \
                        mock.patch.object(release_cutoff, "send_report"):
                    report = release_cutoff.execute(cfg, object(), project, now=2000)
                self.assertEqual("attempt_error" in report, expected)
                if expected:
                    self.assertIn("major", report["message"])


class CandidateAnalyzerTest(unittest.TestCase):
    def test_analyzer_comes_from_target_even_with_stale_checkout(self):
        import pathlib
        import subprocess
        with tempfile.TemporaryDirectory() as folder:
            def git(*args):
                return subprocess.run(["git", *args], cwd=folder, check=True,
                                      capture_output=True, text=True)
            git("init", "-q")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "test")
            script = pathlib.Path(folder) / "scripts/version-level.py"
            script.parent.mkdir()
            script.write_text("print('minor: candidate analyzer')\n")
            git("add", ".")
            git("commit", "-qm", "candidate")
            script.write_text("print('major: stale analyzer')\n")
            self.assertEqual(release_cutoff.version_floor(folder, "HEAD", "HEAD"),
                             ("minor", "candidate analyzer"))

    def test_missing_candidate_analyzer_is_unknown(self):
        import subprocess
        with mock.patch.object(release_cutoff, "_git",
                               return_value=subprocess.CompletedProcess([], 1, "", "missing")):
            self.assertEqual(release_cutoff.version_floor("/unused", "base", "target")[0],
                             "unknown")
