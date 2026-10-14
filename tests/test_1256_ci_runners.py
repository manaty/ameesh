# SPDX-License-Identifier: AGPL-3.0-only
"""GitHub Actions runners of the ameesh guest on a lending host (lot #1256):
the runner unit, the installer's dry runs, and the isolation it promises to
the host's own application (never `actions.runner.*`, never `/home/runner`).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

from .support import REPO

GUEST = os.path.join(REPO, "deploy", "guest")
SCRIPT = os.path.join(GUEST, "ci-runners.sh")
UNIT = os.path.join(GUEST, "ameesh-guest-ci@.service")


def unit(name: str) -> dict[str, list[str]]:
    """{key: [values]} of a unit file (keys such as `Environment=` repeat)."""
    out: dict[str, list[str]] = {}
    with open(os.path.join(GUEST, name), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(("#", "[")):
                continue
            key, _, value = line.partition("=")
            out.setdefault(key, []).append(value)
    return out


class GuestCiUnitTest(unittest.TestCase):
    def test_unit_stays_in_the_guest(self):
        u = unit("ameesh-guest-ci@.service")
        self.assertEqual(u["User"], ["ameesh-guest"])
        self.assertEqual(u["Slice"], ["ameesh-guest.slice"])
        self.assertEqual(u["Delegate"], ["yes"])  # rootless Podman's cgroups
        self.assertEqual(u["OOMScoreAdjust"], ["500"])
        self.assertEqual(u["TimeoutStopSec"], ["180"])
        self.assertEqual(u["RequiresMountsFor"], ["/srv/ameesh-guest"])
        env = dict(v.partition("=")[::2] for v in u["Environment"])
        self.assertTrue(env["PATH"].startswith("/srv/ameesh-guest/bin:"))
        self.assertEqual(env["HOME"], "/srv/ameesh-guest/home")
        self.assertIn("/srv/ameesh-guest/ci/r%i/run.sh", u["ExecStart"][0])
        self.assertEqual(u["PrivateTmp"], ["yes"])
        # the unit is not the runner's own `svc.sh` one: no directive ever
        # enters the application's `actions.runner.*` namespace, and the
        # application's Docker daemon stays out of reach (comments may name
        # both, to say why)
        values = [v for vs in unit("ameesh-guest-ci@.service").values() for v in vs]
        for value in values:
            self.assertNotIn("actions.runner", value)
            self.assertNotIn("/run/docker.sock", value)
        # the strict profile of the agent unit would break newuidmap/fuse
        for key in ("NoNewPrivileges", "PrivateDevices", "ProtectSystem"):
            self.assertNotIn(key, u)


class CiRunnersTest(unittest.TestCase):
    """deploy/guest/ci-runners.sh: dry runs only, against a host of its own
    (CI_RUNNERS_TEST_HOST): never the real /etc/ameesh, the real systemd, the
    real users, nor anything else of the machine this test runs on."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.host = os.path.realpath(tmp.name)
        for d in ("etc/systemd/system", "etc/ameesh", "srv/ameesh-guest/bin", "root", "bin"):
            os.makedirs(os.path.join(self.host, d))
        self.write("etc/passwd",
                   "root:x:0:0:root:/root:/bin/bash\n"
                   "ameesh-guest:x:998:998::/srv/ameesh-guest/home:/bin/bash\n")
        self.write("etc/group", "root:x:0:\nameesh-guest:x:998:\n")
        self.write("etc/systemd/system/ameesh-guest.slice", "[Slice]\nCPUWeight=10\n")
        self.write("etc/ameesh/guard.json", json.dumps(
            {"host": "ci-3", "max_agents": 0, "agent_units": "ameesh-guest-*.service",
             "lend": {"cpu_max": 2, "memory_max": "6GiB", "disk_max": "30GiB"}}))
        self.write("srv/ameesh-guest/bin/docker", "#!/bin/sh\n# lot #1256\nexit 0\n", mode=0o755)
        # the tools the preconditions look for, without this machine's
        for name in ("podman", "psql", "git"):
            self.write("bin/%s" % name, "#!/bin/sh\nexit 0\n", mode=0o755)

    def write(self, path, content, mode=None):
        full = os.path.join(self.host, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        if mode is not None:
            os.chmod(full, mode)
        return full

    def run_script(self, *args, busy=False):
        env = dict(os.environ, CI_RUNNERS_TEST_HOST=self.host,
                   PATH=os.path.join(self.host, "bin") + os.pathsep + os.environ["PATH"])
        if busy:
            env["CI_RUNNERS_TEST_BUSY"] = "1"
        return subprocess.run(["bash", SCRIPT, "--dry-run", *args],
                              capture_output=True, text=True, env=env)

    def test_prepare_prints_the_install_and_changes_nothing(self):
        before = sorted(os.path.relpath(os.path.join(d, f), self.host)
                        for d, _, files in os.walk(self.host) for f in files)
        res = self.run_script("--prepare", "--host", "ci-3", "--count", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("actions-runner-linux-x64-2.338.0.tar.gz", out)
        self.assertIn("af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32", out)
        self.assertIn("ci/r1", out)
        self.assertIn("ci/r2", out)
        self.assertIn("DRY write /etc/systemd/system/ameesh-guest-ci@.service", out)
        self.assertIn("systemctl daemon-reload", out)
        # nothing it would run ever names the application's namespace or trees
        for line in out.splitlines():
            if line.startswith("DRY ") or line.startswith("next,"):
                self.assertNotIn("/home/runner", line)
                self.assertNotIn("actions.runner", line)
        after = sorted(os.path.relpath(os.path.join(d, f), self.host)
                       for d, _, files in os.walk(self.host) for f in files)
        self.assertEqual(before, after)

    def test_the_shim_is_rootless_podman_not_the_host_daemon(self):
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("exec podman --cgroup-manager=cgroupfs", res.stdout)
        self.assertNotIn("DOCKER_HOST", res.stdout)

    def test_prepare_refuses_without_the_pilot(self):
        os.remove(os.path.join(self.host, "etc/systemd/system/ameesh-guest.slice"))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("install-pilot.sh first", res.stderr)

    def test_prepare_refuses_a_root_equivalent_guest(self):
        self.write("etc/group", "root:x:0:\ndocker:x:999:ameesh-guest\n")
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("root-equivalent", res.stderr)

    def test_prepare_refuses_a_foreign_shim(self):
        self.write("srv/ameesh-guest/bin/docker", "#!/bin/sh\nexit 0\n", mode=0o755)
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not this script's shim", res.stderr)

    def test_prepare_refuses_a_foreign_unit(self):
        self.write("etc/systemd/system/ameesh-guest-ci@.service", "# another unit\n", mode=0o644)
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("differs from this checkout", res.stderr)

    def test_the_guard_must_cover_the_ci_units(self):
        self.write("etc/ameesh/guard.json", json.dumps(
            {"host": "ci-3", "agent_units": "ameesh-guest-runner@*.service"}))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--agent-units", res.stderr)

    def test_register_never_prints_the_token(self):
        secret = "ghs_" + "x" * 32
        self.write("root/reg.token", secret + "\n", mode=0o600)
        for n in (1, 2):
            self.write("srv/ameesh-guest/ci/r%d/config.sh" % n, "#!/bin/sh\nexit 0\n", mode=0o755)
        res = self.run_script("--register", "--host", "ci-3", "--count", "2",
                              "--token-file", "/root/reg.token")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn(secret, res.stdout + res.stderr)
        self.assertIn("--token <token>", res.stdout)
        self.assertIn("--name ameesh-nxl-ci-3-1", res.stdout)
        self.assertIn("--labels ameesh-ci", res.stdout)

    def test_disable_refuses_a_running_job(self):
        res = self.run_script("--disable", busy=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("terminal", res.stderr)

    def test_wait_terminal_goes_with_disable_too(self):
        # the help and disable() both accept --wait-terminal; the option must
        # not be rejected before the wait is ever tried (r2 fix)
        res = self.run_script("--disable", "--wait-terminal", "30")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl disable --now ameesh-guest-ci@1.service", res.stdout)
        # a dry run never pretends the job ended: the wait still refuses
        res = self.run_script("--disable", "--wait-terminal", "30", busy=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("terminal", res.stderr)

    def test_the_count_prepared_is_adopted_by_the_later_commands(self):
        # --prepare records the runner count: --remove without --count must
        # cover every prepared runner, not fall back to the default of 2
        self.write("etc/ameesh/ci-runners.state",
                   "host ci-3\ncount 3\nversion 2.338.0\n")
        res = self.run_script("--remove")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl disable --now ameesh-guest-ci@3.service", res.stdout)
        # an explicit --count still wins over the recorded one
        res = self.run_script("--remove", "--count", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl disable --now ameesh-guest-ci@1.service", res.stdout)
        self.assertNotIn("ameesh-guest-ci@3.service", res.stdout)

    def test_remove_is_scoped_and_only_after_a_terminal_job(self):
        res = self.run_script("--remove", "--count", "2", busy=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("terminal", res.stderr)
        res = self.run_script("--remove", "--count", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("systemctl disable --now ameesh-guest-ci@1.service", out)
        self.assertIn("systemctl disable --now ameesh-guest-ci@2.service", out)
        self.assertIn("rm -f ", out)
        self.assertIn("etc/systemd/system/ameesh-guest-ci@.service", out)
        self.assertIn("gh api -X DELETE", out)
        for line in out.splitlines():
            if line.startswith("DRY ") or line.startswith("next,"):
                self.assertNotIn("/home/runner", line)
                self.assertNotIn("actions.runner", line)


if __name__ == "__main__":
    unittest.main()
