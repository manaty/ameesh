# SPDX-License-Identifier: AGPL-3.0-only
"""#3317: a listener cannot start a worker after the last idle reading."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from . import test_3312_ci_runner_drain as original

drain = original.load_drain()
R1 = original.R1


class FrozenDrainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.group = self.root / "cg" / "system.slice" / R1
        self.group.mkdir(parents=True)
        (self.group / "cgroup.events").write_text("frozen 1\npopulated 1\n")
        (self.group / "cgroup.freeze").write_text("0")
        (self.group / "cgroup.kill").write_text("")
        (self.group / "cgroup.procs").write_text("10\n")
        self.proc = self.root / "proc"
        self.process("10", "Runner.Listener")
        self.host = drain.Host(str(self.root / "cg"), str(self.proc),
                               str(self.root / "drain.lock"))
        self.events = []
        self.frozen = False
        self.alive = True
        self.restart = "no"
        self.forced = ""
        self.host._run = self.command
        self.host._set_freezer = self.set_freezer
        self.host.act = self.act
        self.host._kill_frozen = mock.Mock(side_effect=self.kill)

    def process(self, pid, comm):
        path = self.proc / pid
        path.mkdir(parents=True)
        (path / "comm").write_text(comm)

    def command(self, argv):
        if argv[3] == "RestartForceExitStatus":
            return self.forced
        return self.restart if argv[3] == "Restart" else "/system.slice/" + R1

    def set_freezer(self, group, value):
        drain.Host._set_freezer(self.host, group, value)
        self.events.append("freeze" if value == "1" else "thaw")
        self.frozen = value == "1"

    def kill(self, group):
        self.assertTrue(self.frozen)
        self.assertEqual(group, str(self.group))
        self.events.append("kill")
        self.alive = False

    def act(self, verb, unit):
        # systemctl implicitly thaws on stop/restart. An alive listener
        # could now start a worker: it must already have been removed.
        self.frozen = False
        self.assertFalse(self.alive, "listener could accept a job at restart")
        self.events.append(verb)

    def test_idle_listener_removed_before_implicit_thaw(self):
        for verb in ("stop", "restart"):
            with self.subTest(verb=verb):
                self.events.clear()
                self.alive = True
                self.assertTrue(self.host.act_if_idle(verb, R1))
                self.assertEqual(self.events, ["freeze", "kill", "thaw", verb])

    def test_job_appearing_during_freeze_is_thawed_and_kept(self):
        set_freezer = self.set_freezer

        def job_before_freeze(group, value):
            if value == "1":
                self.process("11", "Runner.Worker")
                (self.group / "cgroup.procs").write_text("10\n11\n")
            return set_freezer(group, value)

        self.host._set_freezer = job_before_freeze
        self.assertFalse(self.host.act_if_idle("restart", R1))
        self.assertEqual(self.events, ["freeze", "thaw"])
        self.assertTrue(self.alive)
        self.host._kill_frozen.assert_not_called()

    def test_worker_in_descendant_cgroup_is_kept(self):
        child = self.group / "job"
        child.mkdir()
        (child / "cgroup.procs").write_text("11\n")
        self.process("11", "Runner.Worker")
        self.assertFalse(self.host.act_if_idle("stop", R1))
        self.assertEqual(self.events, ["freeze", "thaw"])
        self.host._kill_frozen.assert_not_called()

    def test_automatic_restart_is_refused_before_freezing(self):
        self.restart = "always"
        with self.assertRaisesRegex(OSError, "Restart=no"):
            self.host.act_if_idle("restart", R1)
        self.assertEqual(self.events, [])
        self.host._kill_frozen.assert_not_called()

    def test_forced_restart_is_also_refused(self):
        self.forced = "9"
        with self.assertRaisesRegex(OSError, "RestartForceExitStatus"):
            self.host.act_if_idle("restart", R1)
        self.assertEqual(self.events, [])

    def test_already_frozen_unit_is_left_alone(self):
        (self.group / "cgroup.freeze").write_text("1")
        with self.assertRaisesRegex(OSError, "already frozen"):
            self.host.act_if_idle("restart", R1)
        self.assertEqual(self.events, [])
        self.assertEqual((self.group / "cgroup.freeze").read_text(), "1")

    def test_incomplete_freeze_is_thawed_without_kill(self):
        (self.group / "cgroup.events").write_text("frozen 0\npopulated 1\n")
        with mock.patch.object(drain.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(OSError, "frozen=1"):
                self.host.act_if_idle("restart", R1)
        self.assertEqual(self.events, ["freeze", "thaw"])
        self.host._kill_frozen.assert_not_called()

    def test_read_error_never_means_idle_and_always_thaws(self):
        with mock.patch.object(self.host, "process_names", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.host.act_if_idle("restart", R1)
        self.assertEqual(self.events, ["freeze", "thaw"])
        self.host._kill_frozen.assert_not_called()

    def test_failed_freeze_is_cleaned_up(self):
        set_freezer = self.set_freezer

        def fail(group, value):
            result = set_freezer(group, value)
            if value == "1":
                raise subprocess.TimeoutExpired(["freeze"], 60)
            return result

        self.host._set_freezer = fail
        with self.assertRaises(subprocess.TimeoutExpired):
            self.host.act_if_idle("restart", R1)
        self.assertEqual(self.events, ["freeze", "thaw"])
        self.host._kill_frozen.assert_not_called()

    def test_kill_failure_or_interruption_thaws(self):
        for error in (OSError("kill unavailable"), KeyboardInterrupt(), SystemExit(2)):
            with self.subTest(error=type(error).__name__):
                self.events.clear()
                self.host._kill_frozen.side_effect = error
                with self.assertRaises(type(error)):
                    self.host.act_if_idle("restart", R1)
                self.assertEqual(self.events, ["freeze", "thaw"])

    def test_cgroup_without_process_list_is_an_error(self):
        os.unlink(self.group / "cgroup.procs")
        with self.assertRaises(FileNotFoundError):
            self.host.act_if_idle("restart", R1)
        self.assertEqual(self.events, ["freeze", "thaw"])

    def test_dry_run_never_freezes_or_kills(self):
        self.host.active_units = lambda: [R1]
        self.assertEqual(drain.main(["--all", "--dry-run"], host=self.host,
                                    log=lambda line: None), 0)
        self.assertEqual(self.events, [])
        self.host._kill_frozen.assert_not_called()

    def test_kill_waits_until_no_process_can_resume(self):
        # Ordinary files stand in for the kernel interface. The first poll
        # still has a live process; the second confirms SIGKILL completed.
        def empty(_):
            (self.group / "cgroup.events").write_text("frozen 1\npopulated 0\n")

        with mock.patch.object(drain.time, "sleep", side_effect=empty) as sleep:
            drain.Host._kill_frozen(self.host, str(self.group))
        self.assertEqual((self.group / "cgroup.kill").read_text(), "1")
        sleep.assert_called_once()

    def test_kill_timeout_is_an_error(self):
        with mock.patch.object(drain.time, "monotonic", side_effect=[0, 6]):
            with self.assertRaisesRegex(OSError, "populated=0"):
                drain.Host._kill_frozen(self.host, str(self.group))

    def test_missing_kill_interface_is_refused(self):
        os.unlink(self.group / "cgroup.kill")
        with self.assertRaises(FileNotFoundError):
            drain.Host._kill_frozen(self.host, str(self.group))
        self.assertFalse((self.group / "cgroup.kill").exists())


if __name__ == "__main__":
    unittest.main()
