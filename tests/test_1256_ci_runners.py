# SPDX-License-Identifier: AGPL-3.0-only
"""GitHub Actions runners of the ameesh guest on a lending host (lot #1256):
the runner unit, the gate wrapper that keeps them behind the guard's state,
the installer's dry runs, and the isolation it promises to the host's own
application (never `actions.runner.*`, never `/home/runner`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

import yaml

from ameesh import capacity_guard

from .support import REPO

GUEST = os.path.join(REPO, "deploy", "guest")
SCRIPT = os.path.join(GUEST, "ci-runners.sh")
UNIT = os.path.join(GUEST, "ameesh-guest-ci@.service")
PLAN = os.path.join(GUEST, "PILOT-ci-nexlink.md")
WRAPPER = os.path.join(GUEST, "ci-runner-gate.py")
SRC = os.path.join(REPO, "src")


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
        # the gate (R1 #1256): the unit runs the wrapper, which obeys the
        # guard's state, not run.sh directly
        self.assertIn("/srv/ameesh-guest/bin/ci-runner-gate", u["ExecStart"][0])
        self.assertIn("--dir /srv/ameesh-guest/ci/r%i", u["ExecStart"][0])
        self.assertEqual(env["AMEESH_HOST_GATE_PATH"],
                         "/run/ameesh-gate/state/state.json")
        self.assertNotIn("run.sh", u["ExecStart"][0])
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


def gate_doc(state="available", mode="enforce", *, ttl=90.0, seq=1):
    """A guard's state document (`ameesh-host-state/1`, L159f)."""
    return {"schema": "ameesh-host-state/1", "state": state, "seq": seq,
            "expires_ts": time.time() + ttl, "guard": {"mode": mode}}


class GateWrapperTest(unittest.TestCase):
    """deploy/guest/ci-runner-gate.py: the GitHub runner (a fake run.sh here)
    runs only while the guard publishes a fresh `available` out of `enforce`,
    and is stopped when the state closes, disappears or goes stale (R1
    #1256)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.run_dir = os.path.join(self.dir, "r1")
        os.makedirs(self.run_dir)
        self.events = os.path.join(self.dir, "events")
        self.gate = os.path.join(self.dir, "state.json")
        self.write(os.path.join(self.run_dir, "run.sh"),
                   "#!/bin/sh\n"
                   "trap 'echo term >> %s; exit 0' TERM\n"
                   "echo start >> %s\n"
                   "while :; do sleep 0.1; done\n" % (self.events, self.events),
                   mode=0o755)
        self.procs = []

    def write(self, path, content, mode=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        if mode is not None:
            os.chmod(path, mode)

    def set_gate(self, **kwargs):
        self.write(self.gate, json.dumps(gate_doc(**kwargs)))

    def start(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        proc = subprocess.Popen(
            [sys.executable, WRAPPER, "--dir", self.run_dir, "--gate", self.gate,
             "--interval", "0.1", "--grace", "3"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        self.procs.append(proc)
        self.addCleanup(self.stop, proc)
        return proc

    def stop(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()

    def events_now(self):
        try:
            with open(self.events, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def wait_event(self, text, timeout=6.0):
        return self.wait_count(text, 1, timeout=timeout)

    def wait_count(self, text, count, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.events_now().count(text) >= count:
                return True
            time.sleep(0.05)
        return False

    def runner_alive(self):
        res = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True)
        return any(self.run_dir + "/run.sh" in line for line in res.stdout.splitlines())

    def test_the_runner_runs_only_on_a_fresh_enforcing_available_state(self):
        proc = self.start()
        # no document: a lending host is closed by default
        time.sleep(0.5)
        self.assertNotIn("start", self.events_now())
        # an observing guard stops nothing: the guests stay off (plan §6)
        self.set_gate(mode="observe")
        time.sleep(0.5)
        self.assertNotIn("start", self.events_now())
        # available out of enforce: the runner starts
        self.set_gate()
        self.assertTrue(self.wait_event("start"), self.events_now())
        self.assertTrue(self.runner_alive())
        # closed: the runner is stopped and no new one starts
        self.set_gate(state="stopped", seq=2)
        self.assertTrue(self.wait_event("term"), self.events_now())
        self.assertFalse(self.runner_alive())
        self.assertEqual(self.events_now().count("start"), 1)
        # reopened: it starts again, same process, no systemd restart needed
        self.set_gate(seq=3)
        self.assertTrue(self.wait_count("start", 2), self.events_now())
        self.assertEqual(self.events_now().count("start"), 2)
        # SIGTERM: the wrapper forwards it and exits cleanly
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(self.events_now().count("term"), 2)
        self.assertFalse(self.runner_alive())

    def test_a_stale_state_stops_the_runner(self):
        # the guard died or hung: its state expires, so the guest stops itself
        self.set_gate()
        self.start()
        self.assertTrue(self.wait_event("start"), self.events_now())
        self.write(self.gate, json.dumps(gate_doc(ttl=-1.0)))
        self.assertTrue(self.wait_event("term"), self.events_now())
        self.assertFalse(self.runner_alive())

    def test_a_missing_runner_is_a_configuration_refusal(self):
        os.remove(os.path.join(self.run_dir, "run.sh"))
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 78)
        self.assertIn("missing or not executable", proc.stdout.read())


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


    def test_prepare_requires_the_guard(self):
        # #1256 R1 : sans garde (ou sans couverture), rien n'arrête les
        # invités quand l'application a besoin de l'hôte : la préparation
        # refuse au lieu d'avertir
        os.remove(os.path.join(self.host, "etc/ameesh/guard.json"))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("guard.json", res.stderr)
        self.assertIn("install-pilot.sh first", res.stderr)

    def test_enable_refuses_while_the_guard_observes(self):
        # #1256 R1 : activation fermée par défaut, enforce avant le premier
        # job ; en observation les invités restent éteints
        for n in (1, 2):
            self.write("srv/ameesh-guest/ci/r%d/.runner" % n, "{}\n")
        res = self.run_script("--enable")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("observation", res.stderr)
        self.assertNotIn("systemctl enable --now", res.stdout)
        # enforce : l'activation passe
        self.write("etc/ameesh/guard.env", "AMEESH_GUARD_ARGS=\n")
        res = self.run_script("--enable")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl enable --now ameesh-guest-ci@1.service", res.stdout)

    def test_the_cached_tarball_is_verified_before_extraction(self):
        # #1256 R1 : le cache est sous $ROOT/ci, que l'invité peut écrire ;
        # son digest est vérifié à chaque fois avant l'extraction root
        self.write("srv/ameesh-guest/ci/cache/actions-runner-linux-x64-2.338.0.tar.gz",
                   "not the tarball\n")
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("cached tarball reused", res.stdout)
        self.assertIn("af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32",
                      res.stdout)
        self.assertIn("sha256sum -c -", res.stdout)
        self.assertNotIn("curl -fL", res.stdout)

    def test_prepare_installs_the_gate_wrapper(self):
        # #1256 R1 : l'unité ne lance pas run.sh directement ; --prepare pose
        # le garde-barrière qui obéit à la porte
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DRY write /srv/ameesh-guest/bin/ci-runner-gate (0755 root:root)",
                      res.stdout)
        self.assertIn("capacity_guard", res.stdout)   # le contrat, pas un doublon
        self.assertIn("gate available: starting", res.stdout)

    def test_remove_returns_the_gate_wrapper(self):
        self.write("etc/ameesh/ci-runners.state",
                   "host ci-3\ncount 1\n"
                   "created /srv/ameesh-guest/bin/ci-runner-gate\n")
        res = self.run_script("--remove", "--count", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("rm -f", res.stdout)
        self.assertIn("srv/ameesh-guest/bin/ci-runner-gate", res.stdout)

    def test_check_reports_the_gate_and_the_wrapper(self):
        for n in (1, 2):
            self.write("srv/ameesh-guest/ci/r%d/.runner" % n,
                       json.dumps({"agentName": "ameesh-nxl-ci-3-%d" % n,
                                   "serverUrl": "https://github.com/manaty/ameesh"}))
            self.write("srv/ameesh-guest/ci/r%d/.credentials" % n, "{}\n", mode=0o600)
        res = self.run_script("--check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("state: stopped (no document yet", res.stdout)
        self.assertIn("wrapper: /srv/ameesh-guest/bin/ci-runner-gate is missing",
                      res.stdout)
        self.assertIn("check: every line ok", res.stdout)

    def test_the_documented_offer_counts_jobs(self):
        # le brouillon de fiche d'hôte du plan est lu par le contrat : le
        # signal de jobs y est `busy_tasks`, pas le nombre d'écouteurs
        with open(PLAN, encoding="utf-8") as fh:
            block = fh.read().split("```yaml", 1)[1].split("```", 1)[0]
        body = "\n".join(line for line in textwrap.dedent(block).splitlines()
                         if line.strip() != "---")
        lend = yaml.safe_load(body)["policy"]["lend"]
        offer, problems, unknown = capacity_guard.parse_lend(lend)
        self.assertEqual(problems, [])
        # `restart_after_seconds` est la clé propre au garde (L159e4), lue par
        # `guard.parse_offer` : le lecteur du contrat la signale, pas plus
        self.assertEqual(unknown, ["restart_after_seconds"])
        reclaim = offer["reclaim"]
        self.assertEqual(reclaim["busy_tasks"], ["Runner.Worker"])
        self.assertEqual(reclaim["busy_units"], ["actions.runner.*"])
        # l'application garde au moins un runner : l'invité s'arrête avant
        self.assertLess(reclaim["busy_max"], 4)


if __name__ == "__main__":
    unittest.main()
