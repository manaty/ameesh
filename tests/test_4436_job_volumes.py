# SPDX-License-Identifier: AGPL-3.0-only
"""Job cleanup and inode exhaustion regressions; no host containers touched."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from . import support
from ameesh import hostcheck

ROOT = Path(__file__).resolve().parents[1]


class JobVolumesTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "job_launcher_4436", ROOT / "deploy/guest/ameesh-job-launcher.py")
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)

    def test_close_without_after_hook_removes_container_and_prunes_safely(self):
        commands = []
        def run(argv, **kwargs):
            commands.append(argv)
            self.assertEqual(kwargs["timeout"], 30)
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        with mock.patch.object(self.launcher.subprocess, "run", side_effect=run), \
                mock.patch.object(self.launcher, "systemctl") as ctl:
            self.assertEqual(self.launcher.cmd_close(
                ["--job", "20261029-010000-abcdef"]), 0)
        self.assertEqual(commands[0], ["/usr/bin/podman", "rm", "-f", "-v",
                                     "-t", "2", "ameesh-job-20261029-010000-abcdef-pg"])
        self.assertEqual(commands[1], ["/usr/bin/podman", "volume", "inspect", "--all"])
        self.assertEqual(ctl.call_args_list[-1].args[0], "revert")

    def test_stuck_or_absent_container_does_not_skip_prune_or_caps(self):
        for error in (subprocess.TimeoutExpired("podman", 30),
                      FileNotFoundError("podman")):
            with self.subTest(error=error), \
                    mock.patch.object(self.launcher.subprocess, "run", side_effect=error) as run, \
                    mock.patch.object(self.launcher, "systemctl") as ctl:
                self.assertEqual(self.launcher.cmd_close(
                    ["--job", "20261029-010000-abcdef"]), 0)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(ctl.call_args_list[-1].args[0], "revert")

    def test_open_sweeps_even_without_memory_caps(self):
        with mock.patch.object(self.launcher, "load_config", return_value={}), \
                mock.patch.object(self.launcher, "prune_job_volumes") as prune:
            self.assertEqual(self.launcher.cmd_open(
                ["--job", "20261029-010000-abcdef"]), 0)
            prune.assert_called_once_with()

    def test_only_old_explicitly_anonymous_volumes_are_removed_without_force(self):
        inventory = [
            {"Name": "orphan", "Anonymous": True, "CreatedAt": "2020-01-01T00:00:00Z"},
            {"Name": "live", "Anonymous": True, "CreatedAt": "2020-01-01T00:00:00Z"},
            {"Name": "named", "Anonymous": False, "CreatedAt": "2020-01-01T00:00:00Z"},
            {"Name": "unknown", "CreatedAt": "2020-01-01T00:00:00Z"},
            {"Name": "recent", "Anonymous": True, "CreatedAt": "2030-01-01T00:00:00Z"},
            {"Name": "bad-date", "Anonymous": True, "CreatedAt": "unreadable"},
            {"Name": "naive", "Anonymous": True, "CreatedAt": "2020-01-01T00:00:00"},
        ]
        # A live volume can appear in inspect; the remove must leave Podman's
        # reference check enabled, including for attachments after inventory.
        with mock.patch.object(self.launcher, "podman_cleanup", return_value=json.dumps(inventory)) as run, \
                mock.patch.object(self.launcher.time, "time", return_value=1800000000):
            self.launcher.prune_job_volumes()
        self.assertEqual(run.call_args_list, [
            mock.call("volume", "inspect", "--all"),
            mock.call("volume", "rm", "orphan", "live")])

    def test_unreadable_inventory_never_removes_a_volume(self):
        for raw in (None, "not json", "{}", "[]"):
            with self.subTest(raw=raw), \
                    mock.patch.object(self.launcher, "podman_cleanup", return_value=raw) as run:
                self.launcher.prune_job_volumes()
                self.assertEqual(run.call_count, 1)

    def test_successful_prune_logs_only_confirmed_removed_names(self):
        inventory = [{"Name": "orphan", "Anonymous": True,
                      "CreatedAt": "2020-01-01T00:00:00Z"}]
        with mock.patch.object(self.launcher, "podman_cleanup", side_effect=[
                json.dumps(inventory), "orphan\nunexpected private output\n"]), \
                mock.patch.object(self.launcher.sys, "stderr") as stderr:
            self.launcher.prune_job_volumes()
        stderr.write.assert_called_once_with(
            "ameesh-job-launcher: removed 1 stale anonymous volume(s): orphan\n")

    def test_postgres_hooks_pass_tmpfs_and_remove_volumes_even_if_absent(self):
        # Execute both actual hooks with a recording Podman, including down
        # returning 'container absent'. PGDATA is never requested as a volume.
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "podman"
            log = Path(tmp) / "commands"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$COMMAND_LOG\"\n"
                            "case $1 in port) echo 127.0.0.1:12345;; rm) exit 1;; esac\n")
            fake.chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                       COMMAND_LOG=str(log), OFFLOAD_JOB="20261029-010000-abcdef")
            script = ROOT / "deploy/guest/job-postgres.sh"
            for verb in ("up", "down", "down"):
                result = subprocess.run(["sh", str(script), verb], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            commands = log.read_text().splitlines()
            self.assertIn("--image-volume=tmpfs", commands[0])
            self.assertEqual(sum(c.startswith("rm -f -v -t 2 ") for c in commands), 2)


class InodeWarningTest(unittest.TestCase):
    def test_boundary_and_disk_with_no_inode_limit(self):
        for free, warning in ((21, False), (20, True), (0, True)):
            with self.subTest(free=free):
                lines = hostcheck.inode_lines(stat=lambda p: SimpleNamespace(
                    f_files=100, f_ffree=free))
                self.assertEqual(bool(lines), warning)
        self.assertEqual(hostcheck.inode_lines(stat=lambda p: SimpleNamespace(
            f_files=0, f_ffree=0)), [])

    def test_mounts_deduplicated_and_failure_visible(self):
        def stat(path):
            if path == "/missing":
                raise OSError("unreadable")
            return SimpleNamespace(f_files=100, f_ffree=10, f_fsid=42)
        lines = hostcheck.inode_lines(("/", "/work", "/missing"), stat=stat)
        self.assertEqual(len(lines), 2)
        self.assertIn("90.0% used", lines[0])
        self.assertIn("unreadable", lines[1])
