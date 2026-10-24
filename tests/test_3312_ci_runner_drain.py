# SPDX-License-Identifier: AGPL-3.0-only
"""Lot #3312: the runners of a CI VM are never stopped in the middle of a job
by the host's maintenance. needrestart defers them after an unattended
upgrade (needrestart-ameesh-ci.conf), and ameesh-ci-runner-drain restarts or
stops a runner only while no `Runner.Worker` lives in its cgroup.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

from .support import REPO

FLOTTE = os.path.join(REPO, "deploy", "ci-flotte")
DRAIN = os.path.join(FLOTTE, "ci-runner-drain.py")
CONF = os.path.join(FLOTTE, "needrestart-ameesh-ci.conf")
INSTALL = os.path.join(FLOTTE, "install.sh")

R1 = "actions.runner.manaty-ameesh.ameesh-ci-1-1.service"
R2 = "actions.runner.manaty-ameesh.ameesh-ci-1-2.service"


def load_drain():
    spec = importlib.util.spec_from_file_location("ci_runner_drain_3312", DRAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


drain = load_drain()


class FakeHost:
    """`busy` is a list of sets, one per reading: the last one sticks."""

    def __init__(self, outdated=(), active=(), busy=(set(),)):
        self.outdated = list(outdated)
        self.active = list(active)
        self.busy = list(busy)
        self.acts = []

    def outdated_units(self):
        return list(self.outdated)

    def active_units(self):
        return list(self.active)

    def process_names(self, unit):
        now = self.busy[0] if len(self.busy) == 1 else self.busy.pop(0)
        return ["Runner.Listener", "Runner.Worker"] if unit in now else ["Runner.Listener"]

    def act(self, verb, unit):
        self.acts.append((verb, unit))


def run(argv, host, **kw):
    lines = []
    code = drain.main(argv, host=host, sleep=kw.get("sleep", lambda s: None),
                      clock=kw.get("clock", lambda: 0), log=lines.append)
    return code, lines


class DrainTest(unittest.TestCase):
    def test_idle_outdated_runner_restarted_busy_one_kept(self):
        host = FakeHost(outdated=[R1, R2, "docker.service", "ssh.service"],
                        busy=[{R2}])
        code, lines = run([], host)
        self.assertEqual(host.acts, [("restart", R1)])
        self.assertEqual(code, drain.BUSY_EXIT)
        self.assertIn("busy, kept until its job ends: %s" % R2, lines)

    def test_nothing_outdated_is_a_clean_pass(self):
        host = FakeHost(outdated=["docker.service"], active=[R1])
        self.assertEqual(run([], host), (0, []))
        self.assertEqual(host.acts, [])

    def test_all_stop_drains_every_active_runner(self):
        host = FakeHost(active=[R2, R1])
        code, _ = run(["--all", "--stop"], host)
        self.assertEqual(code, 0)
        self.assertEqual(host.acts, [("stop", R1), ("stop", R2)])

    def test_job_taken_between_readings_is_not_cut(self):
        # first reading idle, second (just before acting) busy
        host = FakeHost(outdated=[R1], busy=[set(), {R1}])
        code, _ = run([], host)
        self.assertEqual(host.acts, [])
        self.assertEqual(code, drain.BUSY_EXIT)

    def test_wait_acts_once_the_job_ends(self):
        t = [0]
        slept = []

        def sleep(s):
            slept.append(s)
            t[0] += s

        # pass 1: busy (one reading); pass 2: idle (two readings)
        host = FakeHost(active=[R1], busy=[{R1}, set()])
        code, _ = run(["--all", "--stop", "--wait", "60", "--interval", "10"],
                      host, sleep=sleep, clock=lambda: t[0])
        self.assertEqual(code, 0)
        self.assertEqual(slept, [10])
        self.assertEqual(host.acts, [("stop", R1)])

    def test_wait_deadline_leaves_busy_runner_alone(self):
        t = [0]

        def sleep(s):
            t[0] += s

        host = FakeHost(active=[R1], busy=[{R1}])
        code, _ = run(["--all", "--stop", "--wait", "30", "--interval", "10"],
                      host, sleep=sleep, clock=lambda: t[0])
        self.assertEqual(code, drain.BUSY_EXIT)
        self.assertEqual(host.acts, [])
        self.assertEqual(t[0], 30)

    def test_dry_run_changes_nothing(self):
        host = FakeHost(active=[R1, R2], busy=[{R2}])
        code, lines = run(["--all", "--dry-run"], host)
        self.assertEqual(host.acts, [])
        self.assertIn("would restart %s (idle)" % R1, lines)
        self.assertEqual(code, drain.BUSY_EXIT)

    def test_system_error_is_exit_2(self):
        class Broken(FakeHost):
            def outdated_units(self):
                raise subprocess.CalledProcessError(1, ["needrestart"])

        self.assertEqual(run([], Broken())[0], 2)

    def test_unit_names_are_filtered(self):
        self.assertEqual(
            drain.runner_units([R1, "actions.runner.x.service.d/evil",
                                "ameesh-guest-ci@1.service", R1]),
            [R1])


class HostReadingTest(unittest.TestCase):
    def test_worker_in_the_unit_cgroup_is_busy(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        group = "/system.slice/" + R1
        os.makedirs(root + "/cg" + group)
        with open(root + "/cg" + group + "/cgroup.procs", "w") as f:
            f.write("10\n11\n12\n")
        for pid, comm in (("10", "runsvc.sh"), ("11", "Runner.Listener"),
                          ("12", "Runner.Worker")):
            os.makedirs(os.path.join(root, "proc", pid))
            with open(os.path.join(root, "proc", pid, "comm"), "w") as f:
                f.write(comm + "\n")
        host = drain.Host(cgroup_root=root + "/cg", proc=root + "/proc")
        host._run = lambda argv: group + "\n"
        self.assertTrue(drain.busy(host, R1))
        os.unlink(os.path.join(root, "proc", "12", "comm"))  # job ended
        self.assertFalse(drain.busy(host, R1))

    def test_needrestart_batch_lines(self):
        host = drain.Host()
        host._run = lambda argv: (
            "NEEDRESTART-VER: 3.6\nNEEDRESTART-KSTA: 3\n"
            "NEEDRESTART-SVC: docker.service\nNEEDRESTART-SVC: %s\n" % R1)
        self.assertEqual(host.outdated_units(), ["docker.service", R1])


@unittest.skipUnless(shutil.which("perl"), "perl absent")
class NeedrestartConfTest(unittest.TestCase):
    def verdict(self, unit, *confs):
        script = ('our %nrconf; $nrconf{override_rc} = { qr(^dbus) => 0 };'
                  'for (@ARGV) { do $_; die $@ if $@ }'
                  'my ($k) = grep { $ENV{U} =~ /$_/ } keys %{$nrconf{override_rc}};'
                  'print defined $k ? $nrconf{override_rc}{$k} : "restart"')
        return subprocess.run(["perl", "-e", script, *confs], check=True,
                              text=True, stdout=subprocess.PIPE,
                              env=dict(os.environ, U=unit)).stdout

    def test_runners_are_deferred_other_services_are_not(self):
        self.assertEqual(self.verdict(R1), "restart")
        self.assertEqual(self.verdict(R1, CONF), "0")
        self.assertEqual(self.verdict("ssh.service", CONF), "restart")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.env = dict(os.environ, CI_FLOTTE_TEST_ROOT=self.root)

    def sh(self, *args):
        return subprocess.run(["bash", INSTALL, *args], env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_dry_run_writes_nothing(self):
        out = self.sh("--dry-run", "--install")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("/etc/needrestart/conf.d/ameesh-ci-runners.conf", out.stdout)
        self.assertEqual(os.listdir(self.root), [])

    def test_install_check_remove(self):
        self.assertEqual(self.sh("--check").returncode, 1)
        out = self.sh("--install")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn("KO", out.stdout)
        tool = os.path.join(self.root, "usr/local/sbin/ameesh-ci-runner-drain")
        self.assertTrue(os.access(tool, os.X_OK))
        self.assertEqual(self.sh("--remove").returncode, 0)
        self.assertFalse(os.path.exists(tool))
        self.assertEqual(self.sh("--check").returncode, 1)

    def test_one_mode_required(self):
        self.assertEqual(self.sh().returncode, 2)
        self.assertEqual(self.sh("--install", "--remove").returncode, 2)

    def test_service_runs_the_installed_tool(self):
        with open(os.path.join(FLOTTE, "ameesh-ci-runner-drain.service")) as f:
            unit = f.read()
        self.assertIn("ExecStart=/usr/local/sbin/ameesh-ci-runner-drain --needrestart", unit)
        self.assertIn("SuccessExitStatus=%d" % drain.BUSY_EXIT, unit)


if __name__ == "__main__":
    unittest.main()
