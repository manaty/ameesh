# SPDX-License-Identifier: AGPL-3.0-only
"""#7319: recover a failed listener, preserve stops, jobs and drain exclusion."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from . import support  # isolate host configuration

PATH = Path(__file__).resolve().parents[1] / "deploy/ci-flotte/ci-runner-recover.py"
spec = importlib.util.spec_from_file_location("ci_recovery", PATH)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
UNIT = "actions.runner.example-ci.reserve.service"


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.host = recovery.Host(cgroup_root=self.root, lock=str(self.root / "lock"))
        self.host.units = lambda: [UNIT]
        self.state = {"ActiveState": "failed", "LoadState": "loaded", "Job": "",
                      "ControlGroup": "/system.slice/reserve.service",
                      "Result": "signal", "ExecMainCode": "2", "ExecMainStatus": "9"}
        self.host.properties = mock.Mock(side_effect=lambda unit: dict(self.state))
        self.host.start = mock.Mock(side_effect=self.start)
        self.logs = []

    def start(self, unit):
        self.state["ActiveState"] = "active"

    def run_pass(self):
        return recovery.recover(self.host, self.logs.append)

    def test_failed_sigkill_is_reported_then_recovered(self):
        self.assertEqual(self.run_pass(), 0)
        self.host.start.assert_called_once_with(UNIT)
        self.assertIn("result=signal code=2 status=9", self.logs[0])
        self.assertIn("recovered", self.logs[-1])
        self.assertEqual(self.run_pass(), 0)
        self.host.start.assert_called_once_with(UNIT)

    def test_manual_stop_and_busy_active_services_are_never_started(self):
        for state in ("inactive", "active", "activating", "deactivating", "reloading"):
            with self.subTest(state=state):
                self.state["ActiveState"] = state
                self.assertEqual(self.run_pass(), 0)
                self.host.start.assert_not_called()

    def test_failed_listener_with_surviving_worker_is_not_restarted(self):
        group = self.root / "system.slice/reserve.service"
        group.mkdir(parents=True)
        (group / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        self.assertEqual(self.run_pass(), 1)
        self.host.start.assert_not_called()
        self.assertIn("still has processes", self.logs[-1])

    def test_stopped_during_read_or_pending_stop_is_not_rearmed(self):
        for changed in ({"ActiveState": "inactive"}, {"Job": "123 /job/123"}):
            with self.subTest(changed=changed):
                self.host.properties.side_effect = [self.state, dict(self.state, **changed)]
                self.assertEqual(self.run_pass(), 0)
                self.host.start.assert_not_called()

    def test_unreadable_or_malformed_cgroup_fails_closed(self):
        for error in (PermissionError("denied"), OSError("bad counter")):
            with self.subTest(error=error):
                with mock.patch.object(self.host, "populated", side_effect=error):
                    self.assertEqual(self.run_pass(), 1)
                self.host.start.assert_not_called()

    def test_start_failure_is_reported_and_other_units_can_recover(self):
        self.host.units = lambda: [UNIT, UNIT.replace("reserve", "second")]
        def start(unit):
            if unit == UNIT:
                raise subprocess.CalledProcessError(1, ["systemctl"])
            self.state["ActiveState"] = "active"
        self.host.start.side_effect = start
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.host.start.call_count, 2)
        self.assertIn("recovered", self.logs[-1])

    def test_recovery_excludes_a_concurrent_drain_during_state_read(self):
        self.assertEqual(recovery.LOCK, "/run/lock/ameesh-ci-runner-drain.lock")
        def properties(unit):
            with open(self.host.lock, "w") as contender:
                with self.assertRaises(BlockingIOError):
                    recovery.fcntl.flock(contender, recovery.fcntl.LOCK_EX |
                                         recovery.fcntl.LOCK_NB)
            return dict(self.state)
        self.host.properties.side_effect = properties
        self.assertEqual(self.run_pass(), 0)

    def test_cgroup_paths_and_counter_are_checked(self):
        for path in ("/", "relative", "/../escape"):
            with self.subTest(path=path):
                with self.assertRaises(OSError):
                    self.host.populated(path)
        group = self.root / "empty"
        group.mkdir()
        (group / "cgroup.events").write_text("frozen 0\n")
        with self.assertRaises(OSError):
            self.host.populated("/empty")
        (group / "cgroup.events").write_text("populated 0\n")
        self.assertFalse(self.host.populated("/empty"))

    def test_start_does_not_replace_an_operators_job(self):
        with mock.patch.object(self.host, "run") as run:
            recovery.Host.start(self.host, UNIT)
            run.assert_called_once_with("start", "--job-mode=fail", UNIT)

    def test_unit_listing_excludes_foreign_or_invalid_names(self):
        with mock.patch.object(self.host, "run", return_value=(
                f"{UNIT} loaded failed failed reserve\n"
                "ssh.service loaded active running SSH\n"
                "actions.runner.bad.service/evil loaded failed failed X\n")):
            self.assertEqual(recovery.Host.units(self.host), [UNIT])
