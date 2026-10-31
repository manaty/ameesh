# SPDX-License-Identifier: AGPL-3.0-only
"""A one-shot wake keeps its deadline across installation and timer reloads."""
from __future__ import annotations

from .entrypoint import run as isolated_entrypoint

import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import watch

from . import support  # isolate the host's configuration and environment


class DeadlineTest(unittest.TestCase):
    def install(self, args, *, connect_delay=90, sub_state="waiting"):
        """Run the CLI and its real arm path; systemctl and the database are fake."""
        self.clock = datetime.datetime(2026, 10, 15, 23, 45,
                                       tzinfo=datetime.timezone.utc).timestamp()
        self.calls = []

        def connect(cfg):
            self.clock += connect_delay
            return mock.Mock()

        def run(argv, **kwargs):
            self.calls.append(argv[2:])
            value = ""
            if argv[2:4] == ["show", "-p"]:
                value = {"Environment": "AGENT_MAIL_NAME=deadline-agent",
                         "ExecStart": "env AGENT_MAIL_NAME=deadline-agent /a watch fire",
                         "ActiveState": "active", "SubState": sub_state}[argv[4]]
            return subprocess.CompletedProcess(argv, 0, value, "")

        directory = tempfile.TemporaryDirectory(prefix="ameesh-1737-")
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        real_arm = watch.arm

        def arm(*args, **kwargs):
            return real_arm(*args, **kwargs, run=run,
                            env={watch.UNIT_DIR_ENV: self.directory, "PATH": "/bin"})

        with mock.patch.object(watch.time, "time", side_effect=lambda: self.clock), \
                mock.patch.object(watch.platform, "is_linux", return_value=True), \
                mock.patch.object(watch.config_mod, "load",
                                  return_value=SimpleNamespace(host="test")), \
                mock.patch.object(watch.db_mod, "connect", side_effect=connect), \
                mock.patch.object(watch.db_mod, "require_schema"), \
                mock.patch.object(watch.registry, "get", return_value={"name": "deadline-agent"}), \
                mock.patch.object(watch, "authorize", return_value="human:test"), \
                mock.patch.object(watch, "arm", side_effect=arm), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return isolated_entrypoint(watch.main, ["at", "--agent", "deadline-agent", "--message", "check",
                               *args])

    def timer(self):
        with open(os.path.join(self.directory,
                               watch.wake_name("deadline-agent", "wake", "timer")),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_cli_preserves_absolute_and_relative_deadlines_during_installation(self):
        for args in (["--at", "2026-10-16T02:15:00.250000+02:00"],
                     ["--in", "30m"]):
            with self.subTest(args=args):
                self.assertEqual(self.install(args), 0)
                expected = "2026-10-16 00:15:00.250000 UTC" if args[0] == "--at" \
                    else "2026-10-16 00:15:00.000000 UTC"
                self.assertIn("OnCalendar=" + expected + "\n", self.timer())
                self.assertNotIn("OnActiveSec=", self.timer())
                with open(os.path.join(self.directory,
                                       watch.wake_name("deadline-agent", "wake", "wake.json")),
                          encoding="utf-8") as fh:
                    state = json.load(fh)
                self.assertEqual(state["armed_ts"], self.clock)
                self.assertEqual(state["deadline_ts"] - state["armed_ts"],
                                 1710.25 if args[0] == "--at" else 1710)

    def test_deadline_passed_during_connection_is_refused_without_installing(self):
        self.assertEqual(self.install(["--in", "30m"], connect_delay=1801), 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(os.listdir(self.directory), [])

    def test_deadline_passed_before_timer_start_is_refused_and_disarmed(self):
        # systemd leaves a past OnCalendar= timer active/elapsed: it never fires.
        self.assertEqual(self.install(["--in", "30m"], sub_state="elapsed"), 1)
        self.assertEqual(os.listdir(self.directory), [])

    @unittest.skipUnless(shutil.which("systemd-analyze"), "needs the systemd calendar parser")
    def test_systemd_recomputes_the_same_deadline_after_later_reloads(self):
        self.assertEqual(self.install(["--at", "2026-10-16T00:15:00Z"]), 0)
        calendar = next(line.removeprefix("OnCalendar=") for line in self.timer().splitlines()
                        if line.startswith("OnCalendar="))
        # systemd reparses the same installed calendar at each reload. Ask its
        # real parser for the next event at the reported incident's reload times.
        for reference in ("2026-10-15 16:42:47 UTC", "2026-10-15 16:59:30 UTC",
                          "2026-10-15 23:59:59 UTC"):
            result = subprocess.run(["systemd-analyze", "calendar", "--base-time=" + reference,
                                     calendar], text=True, capture_output=True, timeout=10,
                                    env=dict(os.environ, LC_ALL="C", TZ="UTC"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Next elapse: Fri 2026-10-16 00:15:00 UTC", result.stdout)


if __name__ == "__main__":
    unittest.main()
