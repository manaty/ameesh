# SPDX-License-Identifier: AGPL-3.0-only
"""Pilot (a1) of a lending host (lot L159g, study L159 §2.1, §3, §4.4):
guest units under a capped slice, and the pilot report."""
from __future__ import annotations

import json
import os
import subprocess
import time
import unittest

from ameesh import pilot, resources, storage

from .support import REPO, PgTestCase

GUEST = os.path.join(REPO, "deploy", "guest")
GIB = 1024 ** 3


def unit(name: str) -> dict[str, list[str]]:
    """{key: [values]} of a unit file, every section together (systemd keys
    such as `Environment=` may repeat)."""
    out: dict[str, list[str]] = {}
    with open(os.path.join(GUEST, name), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(("#", "[")):
                continue
            key, _, value = line.partition("=")
            out.setdefault(key, []).append(value)
    return out


class UnitsTest(unittest.TestCase):
    def test_guest_runner(self):
        u = unit("ameesh-guest-runner@.service")
        self.assertEqual(u["User"], ["ameesh-guest"])
        self.assertEqual(u["Slice"], ["ameesh-guest.slice"])
        self.assertEqual(u["OOMScoreAdjust"], ["500"])
        self.assertEqual(u["TimeoutStopSec"], ["180"])
        # a volume in /etc/fstab that did not mount: no start on the root disk
        self.assertEqual(u["RequiresMountsFor"], ["/srv/ameesh-guest"])
        self.assertTrue(u["MemoryHigh"] and u["MemoryMax"])
        self.assertLess(resources.parse_bytes(u["MemoryHigh"][0]),
                        resources.parse_bytes(u["MemoryMax"][0]))
        env = dict(v.partition("=")[::2] for v in u["Environment"])
        self.assertEqual(env["AMEESH_HOST_GATE"], "file")
        self.assertEqual(env["AMEESH_HOST_GATE_FALLBACK"], "stopped")
        self.assertEqual(env["AMEESH_HOST_GATE_PATH"], "/run/ameesh-gate/state/state.json")
        self.assertEqual(env["AMEESH_HOST_GATE_ACK"], "/run/ameesh-gate/ack/ack.json")
        self.assertEqual(env["AMEESH_DRAIN_SECONDS"], "120")
        self.assertTrue(env["HOME"].startswith("/srv/ameesh-guest/"))
        self.assertIn("--agents %i", u["ExecStart"][0])

    def test_guest_runner_hardening(self):
        u = unit("ameesh-guest-runner@.service")
        for key in ("NoNewPrivileges", "PrivateTmp", "PrivateDevices",
                    "ProtectKernelTunables", "ProtectControlGroups", "RestrictSUIDSGID"):
            self.assertEqual(u.get(key), ["yes"], key)
        self.assertEqual(u["ProtectSystem"], ["strict"])
        self.assertEqual(u["ProtectHome"], ["tmpfs"])
        self.assertEqual(u["ProtectProc"], ["invisible"])
        self.assertEqual(u["ReadWritePaths"], ["/srv/ameesh-guest /run/ameesh-gate/ack"])
        self.assertIn("/run/docker.sock", u["InaccessiblePaths"][0])

    def test_guest_slice_yields_to_the_application(self):
        u = unit("ameesh-guest.slice")
        self.assertEqual(u["CPUWeight"], ["10"])
        self.assertEqual(u["IOWeight"], ["10"])
        self.assertTrue(u["CPUQuota"] and u["MemoryMax"] and u["MemoryHigh"])
        # the whole guest holds at least one agent at its ceiling
        runner = unit("ameesh-guest-runner@.service")
        self.assertGreaterEqual(resources.parse_bytes(u["MemoryMax"][0]),
                                resources.parse_bytes(runner["MemoryMax"][0]))

    def test_parent_slice_yields_to_the_application(self):
        # r1 P2: systemd nests the guest slice under ameesh.slice, so its
        # weights only rank it among siblings; the parent's weight is the one
        # that competes with system.slice and user.slice
        self.assertEqual(resources.slice_path("ameesh-guest.slice").split(os.sep)[0],
                         "ameesh.slice")
        u = unit(os.path.join("ameesh.slice.d", "guest-weight.conf"))
        self.assertEqual(u["CPUWeight"], ["10"])
        self.assertEqual(u["IOWeight"], ["10"])
        with open(os.path.join(GUEST, "ameesh-guest.slice"), encoding="utf-8") as fh:
            self.assertIn("ameesh.slice.d/guest-weight.conf", fh.read())

    def test_guest_slice_is_measured(self):
        self.assertIn("ameesh-guest.slice", resources.GUEST_SLICES)


class InstallPilotTest(unittest.TestCase):
    """deploy/guest/install-pilot.sh: what the owner runs on the host (dry
    runs only: nothing is installed by the tests)."""
    SCRIPT = os.path.join(GUEST, "install-pilot.sh")

    def dry(self, *args, stdin=None):
        return subprocess.run(["bash", self.SCRIPT, "--dry-run", "--host", "vm-test", *args],
                              capture_output=True, text=True, input=stdin)

    def test_guard_unit(self):
        u = unit("ameesh-guard.service")
        self.assertNotIn("User", u)                      # root: set-property, freeze, kill
        self.assertEqual(u["Slice"], ["system.slice"])   # never inside the slice it caps
        self.assertIn("guard run $AMEESH_GUARD_ARGS", u["ExecStart"][0])
        env = dict(v.partition("=")[::2] for v in u["Environment"])
        self.assertEqual(env["AMEESH_GUARD_ARGS"], "--observe")
        self.assertEqual(env["AMEESH_GUARD_CONFIG"], "/etc/ameesh/guard.json")

    def test_gate_directories(self):
        with open(os.path.join(GUEST, "ameesh-gate.tmpfiles.conf"), encoding="utf-8") as fh:
            lines = [l.split() for l in fh if l.strip() and not l.startswith("#")]
        dirs = {l[1]: l[2:5] for l in lines}
        self.assertEqual(dirs["/run/ameesh-gate/state"], ["0755", "root", "root"])
        self.assertEqual(dirs["/run/ameesh-gate/ack"][1:], ["ameesh-guest", "ameesh-guest"])
        # the runner's ReadWritePaths needs the ack directory to exist
        runner = unit("ameesh-guest-runner@.service")
        self.assertIn("/run/ameesh-gate/ack", runner["ReadWritePaths"][0])

    def test_install_dry_run(self):
        subprocess.run(["bash", "-n", self.SCRIPT], check=True)
        res = self.dry("--allow-rootfs", "--agent", "deepseek5")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("useradd --system", out)
        self.assertIn('"host": "vm-test"', out)
        self.assertIn("AMEESH_GUARD_ARGS=--observe", out)
        self.assertIn("/etc/ameesh/guest-deepseek5.env (0640 root:ameesh-guest)", out)
        # the offer is checked by the installed guard before it is enabled
        self.assertLess(out.index("guard check --config /etc/ameesh/guard.json"),
                        out.index("systemctl enable --now ameesh-guard.service"))
        # no guest runner is started by the install
        self.assertNotIn("enable --now ameesh-guest-runner@", out)

    def test_an_invalid_offer_is_refused(self):
        res = self.dry("--allow-rootfs", "--lend", "/dev/stdin", stdin='{"cpu_max": 2}')
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("`memory_max` required", res.stderr)

    def test_arguments_are_checked(self):
        for args in (["--enable", "x;rm"], ["--purge"], ["--set-guard-mode"],
                     ["--set-guard-mode", "on"], ["--root", "srv"]):
            res = self.dry(*args)
            self.assertNotEqual(res.returncode, 0, args)

    def test_enable_and_guard_mode(self):
        res = self.dry("--enable", "deepseek5", "deepseek6")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl enable --now ameesh-guest-runner@deepseek6.service", res.stdout)
        res = self.dry("--set-guard-mode", "enforce")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("AMEESH_GUARD_ARGS=\n", res.stdout)
        self.assertIn("systemctl restart ameesh-guard.service", res.stdout)

    def test_uninstall_follows_the_study_order(self):
        res = self.dry("--uninstall", "--purge")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        # runners and guard stop before their units are removed
        self.assertLess(out.index("disable --now ameesh-guard.service"),
                        out.index("rm -f /etc/systemd/system/ameesh-guest-runner@.service"))
        self.assertIn("rm -f /etc/tmpfiles.d/ameesh-gate.conf", out)
        self.assertIn("rm -f /etc/ameesh/guard.json /etc/ameesh/guest.env", out)
        # only what the script recorded as its own goes (none here)
        self.assertNotIn("userdel", out)
        self.assertNotIn("rm -rf /opt/ameesh/venv", out)

    def test_owner_runbook_commands_run(self):
        # PILOT-ameesh-agents-1.md: each of its script commands, dry run
        import re
        import shlex
        with open(os.path.join(GUEST, "PILOT-ameesh-agents-1.md"), encoding="utf-8") as fh:
            text = re.sub(r"\\\n\s*", "", fh.read())
        commands = []
        for line in text.splitlines():
            m = re.search(r"sudo deploy/guest/install-pilot\.sh([^`#|]*)", line)
            if m:
                commands.append(shlex.split(m.group(1)))
        self.assertGreaterEqual(len(commands), 8)
        for args in commands:
            args = [a for a in args if a != "--dry-run"]
            if "--host" not in args:
                args = ["--host", "vm-test", *args]   # --enable takes the rest
            res = subprocess.run(["bash", self.SCRIPT, "--dry-run", *args],
                                 capture_output=True, text=True)
            if "--check" in args:   # read only: it fails here, nothing being installed
                self.assertNotIn("install-pilot.sh:", res.stderr, args)
            else:
                self.assertEqual(res.returncode, 0, (args, res.stderr))


def reading(ts, **over):
    row = {"sampled_ts": float(ts), "mem_available_bytes": 8 * GIB, "swap_used_bytes": 0,
           "load1": 2.0, "cpu_count": 4, "disk_free_bytes": 100 * GIB,
           "turns_in_progress": 1, "guest_cpu": 0.5, "psi_cpu_some": 5.0,
           "psi_mem_some": 0.2, "psi_mem_full": 0.0, "lendable_cpu": 1.0,
           "lendable_memory_bytes": 2 * GIB}
    row.update(over)
    return row


class ReportTest(unittest.TestCase):
    def limits(self):
        return resources.thresholds(None)

    def test_percentile(self):
        self.assertIsNone(pilot.percentile([], 0.95))
        self.assertEqual(pilot.percentile([None, 3.0], 0.5), 3.0)
        self.assertEqual(pilot.percentile([1, 2, 3, 4, 5], 0.5), 3.0)
        self.assertAlmostEqual(pilot.percentile(range(101), 0.95), 95.0)

    def test_pressure_minutes_and_episodes(self):
        t0 = 1_800_000_000.0
        rows = [reading(t0 + 60 * i) for i in range(10)]
        # minutes 3 and 4: memory below half the floor (critical)
        for i in (3, 4):
            rows[i]["mem_available_bytes"] = 1
        # minute 7: memory below the floor only (blocked)
        rows[7]["mem_available_bytes"] = int(0.9 * GIB)
        rep = pilot.report("vm", rows, self.limits(), days=1)
        total = rep["total"]
        self.assertEqual(total["samples"], 10)
        self.assertEqual(total["measured_s"], 540.0)
        self.assertEqual(total["pressure_min"], 3.0)
        self.assertEqual(total["critical_episodes"], 1)
        self.assertEqual(total["turn_hours"], round(540 / 3600, 2))
        self.assertEqual(total["guest_cpu_hours"], round(0.5 * 540 / 3600, 2))
        self.assertEqual(total["lendable_memory_min"], 2 * GIB)

    def test_a_hole_in_the_measure_counts_for_nothing(self):
        t0 = 1_800_000_000.0
        rows = [reading(t0, mem_available_bytes=1),
                reading(t0 + 3600, mem_available_bytes=1),
                reading(t0 + 3660)]
        rep = pilot.report("vm", rows, self.limits(), days=1)
        self.assertEqual(rep["total"]["measured_s"], 60.0)
        self.assertEqual(rep["total"]["pressure_min"], 1.0)
        # two critical readings separated by a hole: two episodes
        self.assertEqual(rep["total"]["critical_episodes"], 2)

    def test_per_day_and_checks(self):
        t0 = 1_800_000_000.0
        rows = [reading(t0 + 60 * i, load1=10.0 if i == 5 else 2.0) for i in range(30)]
        rows += [reading(t0 + 86400 + 60 * i, tmp_fstype="tmpfs", tmp_size_bytes=100,
                         tmp_used_bytes=85) for i in range(30)]
        rep = pilot.report("vm", rows, self.limits(), days=2)
        self.assertEqual(rep["schema"], "ameesh-pilot-report/1")
        self.assertEqual(len(rep["by_day"]), 2)
        self.assertEqual(sum(d["samples"] for d in rep["by_day"]), 60)
        checks = {c["key"]: c for c in rep["checks"]}
        self.assertEqual(checks["load_p95"]["target"], 6.0)    # 1.5 × 4 CPU
        self.assertTrue(checks["load_p95"]["ok"])
        self.assertEqual(checks["tmpfs_max"]["value"], 0.85)
        self.assertFalse(checks["tmpfs_max"]["ok"])
        self.assertTrue(checks["psi_mem_p95"]["ok"])
        self.assertTrue(checks["critical_episodes"]["ok"])
        json.dumps(rep)

    def test_pressure_target_holds_each_day(self):
        # r1 P2: 15 min on one day and none on the next average 7.5 min/day,
        # yet the first day misses the target
        t0 = time.mktime((2027, 1, 15, 0, 0, 0, 0, 0, -1))
        rows = [reading(t0 + 300 * i) for i in range(2 * 288 + 1)]
        for i in (10, 11, 12):
            rows[i]["mem_available_bytes"] = int(0.9 * GIB)   # blocked, 5 min each
        rep = pilot.report("vm", rows, self.limits(), days=2)
        self.assertEqual([d["pressure_min"] for d in rep["by_day"]], [15.0, 0.0, 0.0])
        check = {c["key"]: c for c in rep["checks"]}["pressure_min_per_day"]
        self.assertEqual(check["value"], 15.0)
        self.assertFalse(check["ok"])
        rows[12]["mem_available_bytes"] = 8 * GIB
        check = {c["key"]: c for c in pilot.report("vm", rows, self.limits(),
                                                     days=2)["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (10.0, False))   # strictly under
        rows[11]["mem_available_bytes"] = 8 * GIB
        check = {c["key"]: c for c in pilot.report("vm", rows, self.limits(),
                                                     days=2)["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (5.0, True))

    def test_guest_cpu_stands_for_the_window_before_its_reading(self):
        # r1 P2: readings at 0/60/120 s, guest CPU None/0/60: the last one is
        # the average over 60–120 s, i.e. 3600 CPU-s (1 h), not over 120 s–
        t0 = 1_800_000_000.0
        rows = [reading(t0, guest_cpu=None), reading(t0 + 60, guest_cpu=0.0),
                reading(t0 + 120, guest_cpu=60.0)]
        self.assertEqual(pilot.report("vm", rows, self.limits(), days=1)
                         ["total"]["guest_cpu_hours"], 1.0)
        # a shorter measured window counts only for itself
        rows[2]["cpu_window_s"] = 30.0
        self.assertEqual(pilot.report("vm", rows, self.limits(), days=1)
                         ["total"]["guest_cpu_hours"], 0.5)
        # a window longer than the gap to the previous reading is capped by it
        rows[2]["cpu_window_s"] = 90.0
        self.assertEqual(pilot.report("vm", rows, self.limits(), days=1)
                         ["total"]["guest_cpu_hours"], 1.0)

    def test_an_interval_across_midnight_is_cut(self):
        # r1 P2: from 23:59 to 00:01, one minute on each day
        midnight = time.mktime((2027, 1, 16, 0, 0, 0, 0, 0, -1))
        blocked = int(0.9 * GIB)
        rows = [reading(midnight - 120, mem_available_bytes=blocked, guest_cpu=None),
                reading(midnight - 60, mem_available_bytes=blocked, guest_cpu=60.0),
                reading(midnight + 60, mem_available_bytes=blocked, guest_cpu=60.0)]
        rep = pilot.report("vm", rows, self.limits(), days=2)
        first, second = rep["by_day"]
        self.assertEqual((first["measured_s"], second["measured_s"]), (120.0, 60.0))
        self.assertEqual((first["pressure_min"], second["pressure_min"]), (2.0, 1.0))
        self.assertEqual((first["samples"], second["samples"]), (2, 1))
        # guest CPU: 60 s before 23:59 and 60 s before midnight on the first
        # day, the 60 s after it on the second
        self.assertEqual((first["guest_cpu_hours"], second["guest_cpu_hours"]), (2.0, 1.0))
        self.assertEqual(rep["total"]["measured_s"], 180.0)
        self.assertEqual(rep["total"]["guest_cpu_hours"], 3.0)

    def test_a_critical_episode_across_midnight_counts_once(self):
        # L159g2: the episode counts on the day it starts, not again after
        # midnight
        midnight = time.mktime((2027, 1, 16, 0, 0, 0, 0, 0, -1))
        rows = [reading(midnight + dt, mem_available_bytes=1) for dt in (-120, -60, 60)]
        rows.append(reading(midnight + 120))
        rep = pilot.report("vm", rows, self.limits(), days=2)
        self.assertEqual([d["critical_episodes"] for d in rep["by_day"]], [1, 0])
        self.assertEqual(rep["total"]["critical_episodes"], 1)

    def test_a_cpu_window_over_the_gap_limit_counts_for_nothing(self):
        # L159g2: readings 1 h apart, each the average of that hour: a hole
        # in the measure, not 300 s of guest CPU each
        t0 = 1_800_000_000.0
        rows = [reading(t0 + 3600 * i, guest_cpu=2.0, cpu_window_s=3600.0)
                for i in range(3)]
        total = pilot.report("vm", rows, self.limits(), days=1)["total"]
        self.assertEqual((total["measured_s"], total["guest_cpu_hours"]), (0.0, 0.0))
        # the same without a measured window: the gap to the previous reading
        for row in rows:
            del row["cpu_window_s"]
        self.assertEqual(pilot.report("vm", rows, self.limits(), days=1)
                         ["total"]["guest_cpu_hours"], 0.0)
        # readings 1 min apart, each averaged over the hour before it: the
        # measured window is judged before it is cut to the 60 s gap
        rows = [reading(t0 + 60 * i, guest_cpu=60.0, cpu_window_s=3600.0)
                for i in range(3)]
        total = pilot.report("vm", rows, self.limits(), days=1)["total"]
        self.assertEqual((total["measured_s"], total["guest_cpu_hours"]), (120.0, 0.0))

    def test_pressure_target_is_judged_unrounded(self):
        # L159g2: 599 s shows as 10.0 min but is under the 10 min target
        t0 = 1_800_000_000.0
        blocked = int(0.9 * GIB)
        rows = [reading(t0, mem_available_bytes=blocked),
                reading(t0 + 300, mem_available_bytes=blocked),
                reading(t0 + 599), reading(t0 + 660)]
        rep = pilot.report("vm", rows, self.limits(), days=1)
        self.assertEqual(rep["total"]["pressure_s"], 599.0)
        check = {c["key"]: c for c in rep["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (10.0, True))
        rows[2]["sampled_ts"] = t0 + 600
        check = {c["key"]: c for c in pilot.report("vm", rows, self.limits(),
                                                     days=1)["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (10.0, False))
        # 599.9999 s rounds to 600.0 in the summary, yet meets the target
        rows[2]["sampled_ts"] = t0 + 599.9999
        rep = pilot.report("vm", rows, self.limits(), days=1)
        self.assertEqual(rep["total"]["pressure_s"], 600.0)
        check = {c["key"]: c for c in rep["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (10.0, True))

    def test_no_reading(self):
        rep = pilot.report("vm", [], self.limits(), days=7)
        self.assertEqual(rep["total"]["samples"], 0)
        self.assertTrue(all(c["ok"] is None for c in rep["checks"]))
        self.assertIn("no reading", "\n".join(pilot.format_report(rep)))

    def test_format_report(self):
        t0 = 1_800_000_000.0
        rep = pilot.report("vm", [reading(t0 + 60 * i) for i in range(5)],
                           self.limits(), days=1)
        text = "\n".join(pilot.format_report(rep))
        self.assertIn("vm — pilot report", text)
        self.assertIn("total", text)
        self.assertIn("lendable", text)
        self.assertIn("pressure_min_per_day", text)


class ReportDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def test_since_and_cli(self):
        hosts = storage.of(self.db).hosts
        for i in range(3):
            hosts.record({"host": "vm-pilot", "mem_available_bytes": 8 * GIB,
                          "load1": 1.0, "cpu_count": 4, "turns_in_progress": i})
        hosts.record({"host": "other", "mem_available_bytes": GIB})
        self.assertEqual(len(hosts.since("vm-pilot", 3600)), 3)
        proc = self.mesh("hosts", "vm-pilot", "--report", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["host"], "vm-pilot")
        self.assertEqual(rep["total"]["samples"], 3)
        text = self.mesh("hosts", "vm-pilot", "--report", "--days", "1")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("pilot report", text.stdout)
        self.assertNotEqual(self.mesh("hosts", "--report").returncode, 0)
        self.assertNotEqual(self.mesh("hosts", "vm-pilot", "--report",
                                      "--days", "30").returncode, 0)


if __name__ == "__main__":
    unittest.main()
