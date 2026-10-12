# SPDX-License-Identifier: AGPL-3.0-only
"""Shared host measure (lot L159d, study L159 §1.4): PSI, application and
guest shares, lendable capacity, `ameesh hosts`, relocation by `lendable`."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import canon, mesh_cli, relocation, resources, storage

from . import test_canon
from .support import PgTestCase
from .test_l31_pressure import set_host_resources

GIB = 1024 ** 3
HZ = float(os.sysconf("SC_CLK_TCK"))


def write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def stat_line(busy_ticks: int, idle_ticks: int = 1000) -> str:
    # user nice system idle iowait irq softirq steal guest guest_nice
    return "cpu  %d 0 0 %d 50 0 0 0 0 0\ncpu0 1 0 0 1 0 0 0 0 0 0\n" % (
        busy_ticks, idle_ticks)


def fiche(lend):
    return SimpleNamespace(policy=SimpleNamespace(lend=lend, resources=None,
                                                  max_agents=None))


class FakeCanon:
    def __init__(self, cid, hosts):
        self.id, self._hosts = cid, hosts

    def host(self, name):
        return self._hosts.get(name)


class MeasureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    # -- PSI ---------------------------------------------------------------

    def test_parse_pressure(self):
        parsed = resources.parse_pressure(
            "some avg10=1.50 avg60=12.25 avg300=3.00 total=123\n"
            "full avg10=0.00 avg60=0.40 avg300=0.10 total=9\n"
            "garbage line\n")
        self.assertEqual(parsed["some"]["avg60"], 12.25)
        self.assertEqual(parsed["full"]["avg60"], 0.40)
        self.assertEqual(resources.parse_pressure(""), {})

    def test_psi_of_the_host(self):
        base = os.path.join(self.dir, "pressure")
        write(os.path.join(base, "cpu"),
              "some avg10=0 avg60=20.50 avg300=0 total=0\n"
              "full avg10=0 avg60=0.00 avg300=0 total=0\n")
        write(os.path.join(base, "memory"),
              "some avg10=0 avg60=3.00 avg300=0 total=0\n"
              "full avg10=0 avg60=1.25 avg300=0 total=0\n")
        out = resources.psi(base)
        self.assertEqual(out, {"psi_cpu_some": 20.5, "psi_mem_some": 3.0,
                               "psi_mem_full": 1.25, "psi_io_some": None})
        write(os.path.join(base, "cpu"), "some avg10=7.50 avg60=20.50 avg300=0 total=0\n")
        fast = resources.psi(base, window="avg10")
        self.assertEqual(set(fast), set(out))
        self.assertEqual(fast["psi_cpu_some"], 7.5)
        with self.assertRaises(ValueError):
            resources.psi(base, window="avg5")

    def test_psi_absent_gives_none(self):
        out = resources.psi(os.path.join(self.dir, "nothing"))
        self.assertEqual(set(out), {c for c, _r, _l in resources.PSI_FIELDS})
        self.assertTrue(all(v is None for v in out.values()))

    # -- CPU shares ----------------------------------------------------------

    def test_host_cpu_seconds(self):
        path = write(os.path.join(self.dir, "stat"),
                     "cpu  100 20 30 5000 70 5 5 10 40 0\n")
        # idle and iowait excluded; guest already counted in user
        self.assertAlmostEqual(resources.host_cpu_seconds(path), 170 / HZ)
        self.assertIsNone(resources.host_cpu_seconds(os.path.join(self.dir, "none")))

    def test_guest_cgroups_found_in_system_and_user_managers(self):
        root = os.path.join(self.dir, "cg")
        os.makedirs(os.path.join(root, "ameesh.slice", "ameesh-guest.slice"))
        os.makedirs(os.path.join(root, "user.slice", "user-1000.slice",
                                 "user@1000.service", "ameesh.slice", "ameesh-agents.slice"))
        # not where systemd puts them: ignored
        os.makedirs(os.path.join(root, "ameesh-agents.slice"))
        os.makedirs(os.path.join(root, "user.slice", "user-1000.slice", "session-2.scope"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(resources.GUEST_CGROUP_ENV, None)
            found = resources.guest_cgroups(root)
        self.assertEqual(found, [
            os.path.join(root, "ameesh.slice", "ameesh-guest.slice"),
            os.path.join(root, "user.slice", "user-1000.slice", "user@1000.service",
                         "ameesh.slice", "ameesh-agents.slice")])

    def test_slice_path(self):
        self.assertEqual(resources.slice_path("ameesh.slice"), "ameesh.slice")
        self.assertEqual(resources.slice_path("ameesh-agents.slice"),
                         os.path.join("ameesh.slice", "ameesh-agents.slice"))
        self.assertEqual(resources.slice_path("a-b-c.slice"),
                         os.path.join("a.slice", "a-b.slice", "a-b-c.slice"))

    def test_guest_cgroups_explicit(self):
        guest = os.path.join(self.dir, "g")
        os.makedirs(guest)
        with mock.patch.dict(os.environ, {resources.GUEST_CGROUP_ENV: guest + ":/no/such"}):
            self.assertEqual(resources.guest_cgroups("/ignored"), [guest])
        with mock.patch.dict(os.environ, {resources.GUEST_CGROUP_ENV: ""}):
            self.assertEqual(resources.guest_cgroups(self.dir), [])

    def test_cpu_meter_splits_application_and_guest(self):
        stat = os.path.join(self.dir, "stat")
        guest = os.path.join(self.dir, "guest")
        meter = resources.CpuMeter()
        write(stat, stat_line(0))
        write(os.path.join(guest, "cpu.stat"), "usage_usec 0\nuser_usec 0\n")
        first = meter.read(stat=stat, cgroups=[guest], now=100.0)
        self.assertEqual(first, {"app_cpu": None, "guest_cpu": None, "cpu_window_s": None})
        # 60 s later: 3 CPU busy on the host (180 s), 1 CPU of it in the guest
        write(stat, stat_line(int(180 * HZ)))
        write(os.path.join(guest, "cpu.stat"), "usage_usec %d\n" % (60 * 10 ** 6))
        second = meter.read(stat=stat, cgroups=[guest], now=160.0)
        self.assertEqual(second["cpu_window_s"], 60.0)
        self.assertAlmostEqual(second["guest_cpu"], 1.0, places=3)
        self.assertAlmostEqual(second["app_cpu"], 2.0, places=3)

    def test_cpu_meter_without_guest(self):
        stat = os.path.join(self.dir, "stat")
        meter = resources.CpuMeter()
        write(stat, stat_line(0))
        meter.read(stat=stat, cgroups=[], now=0.0)
        write(stat, stat_line(int(60 * HZ)))
        out = meter.read(stat=stat, cgroups=[], now=60.0)
        self.assertIsNone(out["guest_cpu"])
        self.assertAlmostEqual(out["app_cpu"], 1.0, places=3)

    def test_cpu_meter_ignores_a_too_short_window(self):
        stat = os.path.join(self.dir, "stat")
        meter = resources.CpuMeter()
        write(stat, stat_line(0))
        meter.read(stat=stat, cgroups=[], now=0.0)
        write(stat, stat_line(1))
        self.assertIsNone(meter.read(stat=stat, cgroups=[], now=0.5)["app_cpu"])
        # the window still starts at the first reading
        write(stat, stat_line(int(2 * HZ)))
        out = meter.read(stat=stat, cgroups=[], now=2.0)
        self.assertEqual(out["cpu_window_s"], 2.0)
        self.assertAlmostEqual(out["app_cpu"], 1.0, places=3)

    def test_cpu_meter_unreadable_guest_is_no_guess(self):
        stat = os.path.join(self.dir, "stat")
        guest = os.path.join(self.dir, "guest")
        os.makedirs(guest)
        meter = resources.CpuMeter()
        write(stat, stat_line(0))
        meter.read(stat=stat, cgroups=[guest], now=0.0)
        write(stat, stat_line(int(60 * HZ)))
        out = meter.read(stat=stat, cgroups=[guest], now=60.0)
        self.assertEqual(out, {"app_cpu": None, "guest_cpu": None, "cpu_window_s": None})

    def test_guest_memory(self):
        a, b = os.path.join(self.dir, "a"), os.path.join(self.dir, "b")
        write(os.path.join(a, "memory.current"), "%d\n" % GIB)
        write(os.path.join(b, "memory.current"), "%d\n" % (2 * GIB))
        self.assertEqual(resources.guest_memory([a, b]), 3 * GIB)
        self.assertEqual(resources.guest_memory([]), 0)
        self.assertIsNone(resources.guest_memory([a, os.path.join(self.dir, "c")]))

    # -- lendable capacity -------------------------------------------------

    def reading(self, **over):
        row = {"cpu_count": 8, "app_cpu": 1.0, "mem_available_bytes": 10 * GIB,
               "guest_mem_bytes": 2 * GIB, "mem_total_bytes": 32 * GIB,
               "disk_free_bytes": 50 * GIB}
        row.update(over)
        return row

    def test_lendable_on_history(self):
        basis = {"span_s": 3 * 86400.0, "min_free_mem_bytes": 8 * GIB, "app_cpu_p95": 4.0}
        out = resources.lendable(self.reading(), basis)
        self.assertEqual(out["lendable_basis"], "history")
        self.assertEqual(out["lendable_memory_bytes"], 4 * GIB)   # 0.5 × M7
        self.assertEqual(out["lendable_cpu"], 2.0)                # 0.5 × (8 − 4)
        self.assertEqual(out["lendable_disk_bytes"], 48 * GIB)    # 50 − 2 GiB

    def test_lendable_counts_the_current_reading(self):
        basis = {"span_s": 3 * 86400.0, "min_free_mem_bytes": 30 * GIB, "app_cpu_p95": 0.0}
        out = resources.lendable(self.reading(), basis)
        # now: 10 GiB available + 2 GiB held by the guest
        self.assertEqual(out["lendable_memory_bytes"], 6 * GIB)

    def test_lendable_is_prudent_during_warmup(self):
        out = resources.lendable(self.reading(mem_available_bytes=60 * GIB,
                                              mem_total_bytes=64 * GIB, app_cpu=0.0), None)
        self.assertEqual(out["lendable_basis"], "warmup")
        self.assertEqual(out["lendable_memory_bytes"], 16 * GIB)  # 25 % of 64 GiB
        self.assertEqual(out["lendable_cpu"], 1.0)

    def test_lendable_unknown_guest_memory_is_not_zero(self):
        basis = {"span_s": 3 * 86400.0, "app_cpu_p95": 0.0}
        out = resources.lendable(self.reading(guest_mem_bytes=None), basis)
        self.assertIsNone(out["lendable_memory_bytes"])
        out = resources.lendable(self.reading(guest_mem_bytes=None),
                                 dict(basis, min_free_mem_bytes=8 * GIB))
        self.assertEqual(out["lendable_memory_bytes"], 4 * GIB)   # history only

    def test_lendable_never_guesses(self):
        out = resources.lendable({}, None)
        self.assertIsNone(out["lendable_cpu"])
        self.assertIsNone(out["lendable_memory_bytes"])
        self.assertIsNone(out["lendable_disk_bytes"])
        busy = resources.lendable(self.reading(app_cpu=9.0, disk_free_bytes=GIB),
                                  {"span_s": 86400.0 * 2})
        self.assertEqual(busy["lendable_cpu"], 0.0)
        self.assertEqual(busy["lendable_disk_bytes"], 0)

    # -- offer -------------------------------------------------------------

    def test_lend_offer(self):
        self.assertIsNone(resources.lend_offer(None))
        self.assertIsNone(resources.lend_offer(SimpleNamespace(lend=None)))
        offer = resources.lend_offer(SimpleNamespace(
            lend={"cpu_max": 3, "memory_max": "14GiB", "disk_max": "80GiB",
                  "windows": ["sat-sun"]}))
        self.assertEqual(offer, {"cpu_max": 3.0, "memory_max": 14 * GIB,
                                 "disk_max": 80 * GIB})
        self.assertEqual(resources.lend_offer({"lend": {"cpu_max": "x"}}),
                         {"cpu_max": None, "memory_max": None, "disk_max": None})

    def test_host_offer_is_the_strictest(self):
        canons = [FakeCanon("a", {"vm": fiche({"cpu_max": 3, "memory_max": "14GiB"})}),
                  FakeCanon("b", {"vm": fiche({"cpu_max": 2, "disk_max": "10GiB"})}),
                  FakeCanon("c", {"vm": fiche(None)})]
        self.assertEqual(resources.host_offer(canons, "vm"),
                         {"cpu_max": 2.0, "memory_max": 14 * GIB, "disk_max": 10 * GIB})
        self.assertIsNone(resources.host_offer(canons, "other"))

    def test_offered_caps_by_offer(self):
        row = {"lendable_cpu": 2.5, "lendable_memory_bytes": 20 * GIB,
               "lendable_disk_bytes": 100 * GIB}
        self.assertIsNone(resources.offered({}, None))
        # no offer: the host lends nothing; an offer without caps: the measure
        self.assertEqual(resources.offered(row, None),
                         {"cpu": 0.0, "memory_bytes": 0, "disk_bytes": 0})
        self.assertEqual(resources.offered(row, {}),
                         {"cpu": 2.5, "memory_bytes": 20 * GIB, "disk_bytes": 100 * GIB})
        self.assertEqual(resources.offered(row, {"cpu_max": 2.0, "memory_max": 14 * GIB,
                                                 "disk_max": None}),
                         {"cpu": 2.0, "memory_bytes": 14 * GIB, "disk_bytes": 100 * GIB})

    # -- the common measure (§1.4) -------------------------------------------

    def test_measure_has_the_study_fields(self):
        row = self.reading(sampled_ts=1000.0, cpu_window_s=60.0, guest_cpu=0.5,
                           swap_used_bytes=0, psi_cpu_some=12.0, psi_mem_some=0.0,
                           psi_mem_full=0.0, psi_io_some=1.5, lendable_cpu=2.0,
                           lendable_memory_bytes=4 * GIB, lendable_disk_bytes=48 * GIB)
        out = resources.measure(row, {"cpu_max": 1.0, "memory_max": None, "disk_max": None})
        self.assertEqual(out["ts"], 1000.0)
        self.assertEqual(out["window_s"], 60.0)
        self.assertEqual(out["psi"], {"cpu_some": 12.0, "mem_some": 0.0,
                                      "mem_full": 0.0, "io_some": 1.5})
        self.assertEqual(out["lendable"], {"cpu": 1.0, "memory_bytes": 4 * GIB,
                                           "disk_bytes": 48 * GIB})
        for key in ("cpu_count", "app_cpu", "guest_cpu", "mem_total_bytes",
                    "mem_available_bytes", "swap_used_bytes", "guest_mem_bytes",
                    "disk_free_bytes", "on_ac", "battery_percent", "app_psi",
                    "app_disk_free_bytes", "metered", "user_idle_s", "busy"):
            self.assertIn(key, out)
        json.dumps(out)

    def test_measure_without_psi(self):
        out = resources.measure({"sampled_ts": 1.0})
        self.assertIsNone(out["psi"])
        self.assertIsNone(out["lendable"])

    # -- CPU pressure threshold ------------------------------------------------

    def test_max_cpu_pressure(self):
        self.assertIsNone(resources.thresholds(None)["max_cpu_pressure"])
        limits = resources.thresholds({"resources": {"max_cpu_pressure": "20%"}})
        self.assertEqual(limits["max_cpu_pressure"], 20.0)
        self.assertEqual(resources.breaches({"psi_cpu_some": 15.0}, limits), [])
        found = resources.breaches({"psi_cpu_some": 30.0}, limits)
        self.assertEqual([(b["key"], b["critical"]) for b in found],
                         [("max_cpu_pressure", False)])
        found = resources.breaches({"psi_cpu_some": 70.0}, limits)
        self.assertTrue(found[0]["critical"])
        # no PSI: nothing watched
        self.assertEqual(resources.breaches({"psi_cpu_some": None}, limits), [])

    # -- `ameesh hosts` and relocation -----------------------------------------

    def test_host_measure_lines(self):
        latest = self.reading(psi_cpu_some=12.0, psi_mem_some=0.5, psi_mem_full=0.0,
                              psi_io_some=None, guest_cpu=0.5, lendable_cpu=2.0,
                              lendable_memory_bytes=4 * GIB, lendable_disk_bytes=None)
        lines = mesh_cli._host_measure_lines(latest, {"cpu_max": 1.0})
        self.assertEqual(len(lines), 3)
        self.assertIn("PSI", lines[0])
        self.assertIn("12.0 %", lines[0])
        self.assertIn("lendable", lines[2])
        self.assertIn("capped by the offer", lines[2])
        self.assertEqual(mesh_cli._host_measure_lines({"mem_available_bytes": 1}, None), [])

    def test_relocation_ranks_a_lending_host_by_lendable(self):
        raw = {"mem_available_bytes": 30 * GIB, "load1": 0.0}
        lending = dict(raw, lendable_memory_bytes=20 * GIB)
        self.assertLess(relocation._score(raw), relocation._score(
            lending, {"cpu_max": None, "memory_max": 4 * GIB, "disk_max": None}))
        # an offer without a lendable measure ranks last
        self.assertEqual(relocation._score(raw, {"memory_max": GIB})[0], 1)


class CanonTest(test_canon._TmpMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.example_copy()

    def _load(self):
        loaded = canon.load(self.root, untrusted=True)
        return loaded, [f.code for f in canon.validate(loaded)]

    def test_max_cpu_pressure_read(self):
        set_host_resources(self.root, max_cpu_pressure="20%")
        loaded, codes = self._load()
        self.assertEqual(loaded.host("atelier").policy.resources,
                         {"max_cpu_pressure": 20.0})
        self.assertNotIn("host-policy-invalid", codes)
        limits = resources.host_limits([loaded], "atelier")
        self.assertEqual(limits["limits"]["max_cpu_pressure"], 20.0)
        self.assertEqual(limits["origin"]["max_cpu_pressure"], loaded.id or "?")

    def test_max_cpu_pressure_invalid(self):
        set_host_resources(self.root, max_cpu_pressure="150%")
        _loaded, codes = self._load()
        self.assertIn("host-policy-invalid", codes)


# ==========================================================================
# storage and CLI (real database)
# ==========================================================================

class MeasureDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def test_record_keeps_the_measure(self):
        hosts = storage.of(self.db).hosts
        hosts.record({"host": "vm", "mem_available_bytes": 8 * GIB, "cpu_count": 4,
                      "mem_total_bytes": 16 * GIB, "psi_cpu_some": 12.5,
                      "psi_mem_some": 0.1, "psi_mem_full": 0.0, "psi_io_some": 2.0,
                      "app_cpu": 1.25, "guest_cpu": 0.5, "cpu_window_s": 60.0,
                      "guest_mem_bytes": GIB, "lendable_cpu": 1.0,
                      "lendable_memory_bytes": 4 * GIB, "lendable_disk_bytes": 9 * GIB,
                      "lendable_basis": "warmup"})
        latest = hosts.latest("vm")
        self.assertEqual(latest["psi_cpu_some"], 12.5)
        self.assertEqual(latest["app_cpu"], 1.25)
        self.assertEqual(latest["guest_mem_bytes"], GIB)
        self.assertEqual(latest["lendable_memory_bytes"], 4 * GIB)
        # an old reading without the measure stays readable
        hosts.record({"host": "old", "mem_available_bytes": GIB})
        self.assertIsNone(hosts.latest("old")["psi_cpu_some"])

    def test_lend_basis(self):
        hosts = storage.of(self.db).hosts
        self.assertEqual(hosts.lend_basis("vm", 3600, 600, 0.95)["samples"], 0)
        for free, guest, cpu in ((8, 1, 1.0), (5, 2, 3.0), (9, 0, None)):
            hosts.record({"host": "vm", "mem_available_bytes": free * GIB,
                          "guest_mem_bytes": guest * GIB, "app_cpu": cpu})
            time.sleep(0.01)
        basis = hosts.lend_basis("vm", 3600, 600, 0.95)
        self.assertEqual(basis["samples"], 3)
        self.assertEqual(basis["min_free_mem_bytes"], 7 * GIB)
        self.assertGreater(basis["span_s"], 0.0)
        self.assertLess(basis["span_s"], 60.0)
        # the readings fall in one or two 10 min slots: their average(s)
        self.assertGreaterEqual(basis["app_cpu_p95"], 1.0)
        self.assertLessEqual(basis["app_cpu_p95"], 3.0)

    def test_lend_basis_ignores_unmeasured_readings(self):
        hosts = storage.of(self.db).hosts
        # legacy readings: no application CPU, no guest memory
        for _ in range(2):
            hosts.record({"host": "old-vm", "mem_available_bytes": GIB})
            time.sleep(0.05)
        hosts.record({"host": "old-vm", "mem_available_bytes": 9 * GIB,
                      "guest_mem_bytes": 0, "app_cpu": 0.5})
        basis = hosts.lend_basis("old-vm", 3600, 600, 0.95)
        self.assertEqual(basis["span_s"], 0.0)          # one valid reading
        self.assertEqual(basis["min_free_mem_bytes"], 9 * GIB)
        # unknown guest memory is not zero
        hosts.record({"host": "vm2", "mem_available_bytes": GIB, "app_cpu": 0.5})
        self.assertIsNone(hosts.lend_basis("vm2", 3600, 600, 0.95)["min_free_mem_bytes"])

    def test_cli_hosts_shows_the_measure(self):
        storage.of(self.db).hosts.record({
            "host": "vm-test", "mem_available_bytes": 8 * GIB, "cpu_count": 4,
            "psi_cpu_some": 12.5, "app_cpu": 1.0, "guest_cpu": 0.5,
            "lendable_cpu": 1.0, "lendable_memory_bytes": 2 * GIB})
        proc = self.mesh("hosts", "vm-test", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        objet = json.loads(proc.stdout.splitlines()[0])
        self.assertIsNone(objet["offer"])
        self.assertEqual(objet["measure"]["psi"]["cpu_some"], 12.5)
        self.assertEqual(objet["measure"]["lendable"]["memory_bytes"], 0)  # no offer
        texte = self.mesh("hosts", "vm-test")
        self.assertEqual(texte.returncode, 0, texte.stderr)
        self.assertIn("PSI", texte.stdout)
        self.assertIn("lendable   CPU 1.00; memory 2.0", texte.stdout)
        self.assertIn("no offer: lends nothing", texte.stdout)


if __name__ == "__main__":
    unittest.main()
