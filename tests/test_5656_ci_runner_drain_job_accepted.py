# SPDX-License-Identifier: AGPL-3.0-only
"""#5656: a job the listener accepted but has not given to a Worker is busy.

The listener prints `Running job: …` before it spawns Runner.Worker; in that
window the cgroup has no Worker yet, so the drain must read the listener's
current `_diag/Runner_*.log` (under the freeze) and leave the runner alone.
A missing, unreadable or marker-less log is busy too (fail closed). A runner
started less than the cooldown ago is not restarted again: a job process left
in the unit cgroup by the upstream unit's `KillMode=process` keeps needrestart
listing the unit, hence the second pass of the incident.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from . import test_3312_ci_runner_drain as original

drain = original.load_drain()
R1 = original.R1

HELLO = (
    "[2026-11-03 13:00:00Z INFO Runner] Runner is built for Linux (X64)\n"
    "[2026-11-03 13:00:00Z INFO Terminal] WRITE LINE: "
    "2026-11-03 13:00:00Z: Listening for Jobs\n")
START = (
    "[2026-11-03 13:32:18Z INFO Terminal] WRITE LINE: "
    "2026-11-03 13:32:18Z: Running job: psql 13/16\n")
DONE = (
    "[2026-11-03 13:40:00Z INFO Terminal] WRITE LINE: "
    "2026-11-03 13:40:00Z: Job psql 13/16 completed with result: Failed\n")


class DiagLogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.work = self.root / "runner"
        (self.work / "_diag").mkdir(parents=True)
        self.log = self.work / "_diag" / "Runner_20261103-130000-utc.log"
        self.log.write_text("")
        self.host = drain.Host(str(self.root / "cg"), str(self.root / "proc"),
                               str(self.root / "drain.lock"))
        self.host._run = lambda argv: str(self.work) + "\n"
        self.host.process_names = lambda unit: ["Runner.Listener"]

    def write(self, text, name="Runner_20261103-130000-utc.log"):
        (self.work / "_diag" / name).write_text(text)

    def test_job_accepted_without_worker_is_busy(self):
        self.write(HELLO + START)
        self.assertTrue(self.host.job_in_flight(R1))
        self.assertTrue(drain.busy(self.host, R1))

    def test_completed_job_is_idle(self):
        self.write(HELLO + START + DONE)
        self.assertFalse(self.host.job_in_flight(R1))
        self.assertFalse(drain.busy(self.host, R1))

    def test_idle_listener_is_idle(self):
        self.write(HELLO)
        self.assertFalse(self.host.job_in_flight(R1))

    def test_last_marker_wins(self):
        self.write(HELLO + START + DONE + START)
        self.assertTrue(self.host.job_in_flight(R1))

    def test_paged_log_keeps_a_job_started_on_the_previous_page(self):
        self.write(HELLO + START, "Runner_20261103-130000-utc.log")
        self.write("[2026-11-03 13:35:00Z INFO JobDispatcher] waiting\n",
                   "Runner_20261103-133500-utc.log")
        self.assertTrue(self.host.job_in_flight(R1))

    def test_restarted_listener_starts_a_new_session(self):
        # The job cut by a previous restart has no completion line; the new
        # listener's session marker is what proves that job is gone.
        self.write(HELLO + START, "Runner_20261103-130000-utc.log")
        self.write(HELLO, "Runner_20261103-140000-utc.log")
        self.assertFalse(self.host.job_in_flight(R1))

    def test_no_log_is_busy(self):
        os.unlink(self.log)
        self.assertTrue(self.host.job_in_flight(R1))

    def test_missing_diag_directory_is_busy(self):
        os.unlink(self.log)
        os.rmdir(self.work / "_diag")
        self.assertTrue(self.host.job_in_flight(R1))

    def test_unreadable_log_is_busy(self):
        os.unlink(self.log)
        self.log.mkdir()  # open() raises IsADirectoryError, an OSError
        self.assertTrue(self.host.job_in_flight(R1))

    def test_empty_log_is_busy(self):
        self.write("")
        self.assertTrue(self.host.job_in_flight(R1))

    def test_missing_working_directory_is_busy(self):
        self.host._run = lambda argv: "-\n"
        self.assertTrue(self.host.job_in_flight(R1))

    def test_job_marker_found_across_read_chunks(self):
        self.host.log_chunk = 17  # smaller than the longest marker
        self.write(HELLO + START + DONE + START)
        self.assertTrue(self.host.job_in_flight(R1))

    def test_marker_split_by_a_chunk_boundary(self):
        # The tail chunk starts five bytes inside the "Running job:" marker;
        # only the overlap kept between chunks can rebuild it.
        line = "Running job: psql 13/16\n"
        chunk = 64
        self.host.log_chunk = chunk
        self.write(HELLO + "y" * 100 + line + "z" * (chunk + 5 - len(line)))
        self.assertTrue(self.host.job_in_flight(R1))

    def test_last_marker_of_a_markerless_file_is_none(self):
        self.write("nothing here\n")
        self.assertIsNone(drain.last_marker(str(self.log)))

    def test_active_seconds_reads_systemd_monotonic(self):
        host = drain.Host()
        host._run = lambda argv: "2000000\n"  # 2 s since boot, in microseconds
        with mock.patch.object(drain.time, "monotonic", return_value=100.0):
            self.assertAlmostEqual(host.active_seconds(R1), 98.0)

    def test_unknown_active_time_is_none(self):
        host = drain.Host()
        host._run = lambda argv: "0\n"
        self.assertIsNone(host.active_seconds(R1))


class CooldownTest(unittest.TestCase):
    """The second pass of the incident must not restart the same runner."""

    def setUp(self):
        self.host = original.FakeHost(outdated=[R1])

    def pass_(self, cooldown):
        lines = []
        left = drain.one_pass(self.host, False, "restart", False, lines.append,
                              cooldown=cooldown)
        return left, lines

    def test_one_pass_keeps_a_job_accepted_without_worker(self):
        self.host.in_flight.add(R1)
        lines = []
        left = drain.one_pass(self.host, False, "restart", False, lines.append,
                              cooldown=0)
        self.assertEqual(self.host.acts, [])
        self.assertEqual(left, [R1])
        self.assertIn("busy, kept until its job ends: %s" % R1, lines)

    def test_recent_restart_is_not_repeated(self):
        self.host.active_age = 120
        left, lines = self.pass_(900)
        self.assertEqual(self.host.acts, [])
        self.assertEqual(left, [R1])
        self.assertIn("active for 120 s (< 900 s cooldown), "
                      "not restarted again: %s" % R1, lines)

    def test_old_restart_is_drained(self):
        self.host.active_age = 901
        left, _ = self.pass_(900)
        self.assertEqual(self.host.acts, [("restart", R1)])
        self.assertEqual(left, [])

    def test_cooldown_zero_always_acts(self):
        self.host.active_age = 1
        left, _ = self.pass_(0)
        self.assertEqual(self.host.acts, [("restart", R1)])
        self.assertEqual(left, [])

    def test_unknown_age_does_not_block(self):
        self.host.active_age = None
        left, _ = self.pass_(900)
        self.assertEqual(self.host.acts, [("restart", R1)])
        self.assertEqual(left, [])

    def test_planned_stop_ignores_the_cooldown(self):
        self.host.active_age = 1
        self.host.outdated = []
        self.host.active = [R1]
        left = drain.one_pass(self.host, True, "stop", False,
                              lambda line: None, cooldown=900)
        self.assertEqual(self.host.acts, [("stop", R1)])
        self.assertEqual(left, [])

    def test_default_cooldown_is_applied_by_main(self):
        self.host.active_age = 120
        code, lines = original.run([], self.host)
        self.assertEqual(self.host.acts, [])
        self.assertEqual(code, drain.BUSY_EXIT)
        self.assertTrue(any("not restarted again: %s" % R1 in line
                            for line in lines))

    def test_negative_cooldown_is_refused(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as refused:
                drain.main(["--cooldown", "-1"], host=original.FakeHost())
        self.assertEqual(refused.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
