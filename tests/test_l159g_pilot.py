# SPDX-License-Identifier: AGPL-3.0-only
"""Pilot (a1) of a lending host (lot L159g, study L159 §2.1, §3, §4.4):
guest units under a capped slice, and the pilot report."""
from __future__ import annotations

import json
import os
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

    def test_guest_slice_is_measured(self):
        self.assertIn("ameesh-guest.slice", resources.GUEST_SLICES)


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
