# SPDX-License-Identifier: AGPL-3.0-only
"""E3b (#7675): the native qualification witness, without IMA's code.

Two qualifications, both deterministic and independent of a running host:

* **Pressure (`own_cgroup`).** The thresholds IMA's private
  `document-container-pressure.py` applies (0.90 CPU, 0.85 memory, 0.25
  throttled periods) are replayed on a synthetic cgroup v2 tree. The native
  measures and verdicts are compared with IMA's formulas. The CPU and
  throttled-period gates are *gaps*: no existing native key catches them, so
  the tranche adds `max_cgroup_cpu_used` and `max_cgroup_throttled_periods`
  (no default; absent means unwatched). Memory already has
  `max_cgroup_memory_used`. Unknown readings never become a breach, and the
  request/limit split changes no threshold.
* **Admission.** IMA's `PriorityAdmission` gives a permanent document class
  priority over roles/background. The native `admission_lock.PriorityRLock`
  has no permanent class: awakened/control work goes first, FIFO within a
  class, with aging and a bounded control burst. The scenarios below label a
  woken document CONTROL and role/background passes NORMAL and witness the
  latency/fairness acceptance case, including the bounded background latency
  of `BOOST_BURST` grants. This lot adds no document class and no lock
  replacement.

The IMA source is the frozen inventory SHA `23e0a944` (mapping #7664); the
thresholds are the chart values (cpu 0.90, memory 0.85, throttled 0.25,
requests 500m/1Gi, limits 4/4Gi).
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import admission_lock, resources

GIB = 1024 ** 3
#: IMA chart values, pinned by the mapping (#7664) at 23e0a944.
IMA_CPU = 0.90
IMA_MEMORY = 0.85
IMA_THROTTLED = 0.25


def write_cgroup(root: str, *, usage_usec: int = 0,
                 throttled_usec: int = 0, nr_periods: int = 100,
                 nr_throttled: int = 0, current: int = 3 * GIB,
                 maximum: int | None = 4 * GIB, quota: str = "400000",
                 period: str = "100000", cpu_psi: float = 0.0,
                 mem_psi: float = 0.0) -> str:
    """A cgroup v2 tree with the files the native meter reads."""
    os.makedirs(root, exist_ok=True)

    def psi(path, some):
        with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
            fh.write("some avg10=0.00 avg60=%.2f avg300=0.00 total=0\n"
                     "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n" % some)

    psi("cpu.pressure", cpu_psi)
    psi("memory.pressure", mem_psi)
    with open(os.path.join(root, "cpu.stat"), "w", encoding="utf-8") as fh:
        fh.write("usage_usec %d\nnr_periods %d\nnr_throttled %d\n"
                 "throttled_usec %d\n"
                 % (usage_usec, nr_periods, nr_throttled, throttled_usec))
    with open(os.path.join(root, "cpu.max"), "w", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (quota, period))
    with open(os.path.join(root, "memory.current"), "w", encoding="utf-8") as fh:
        fh.write("%d\n" % current)
    with open(os.path.join(root, "memory.max"), "w", encoding="utf-8") as fh:
        fh.write("max\n" if maximum is None else "%d\n" % maximum)
    return root


class PressureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="7675-cgroup-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "cg")

    def sample(self, first: dict, second: dict, *, start: float = 100.0,
               end: float = 102.0) -> dict:
        """Two readings two seconds apart; the counters are deltas."""
        write_cgroup(self.root, **first)
        meter = resources.CgroupMeter()
        meter.read(self.root, now=start)
        write_cgroup(self.root, **second)
        return meter.read(self.root, now=end)

    def ima(self, cgroup: dict) -> dict:
        """IMA's `ContainerPressure.sample/verdict` formulas on the reading."""
        quota = cgroup.get("cpu_quota")
        cpu = cgroup.get("cpu_used")
        memory = cgroup.get("mem_used")
        return {
            # IMA divides the usage rate by min(cpu_limit, quota/period); the
            # downward-API limit is the cgroup quota here.
            "cpu": None if quota in (None, 0) or cpu is None else cpu / quota,
            "memory": memory,
            "throttled": cgroup.get("throttled_periods"),
        }

    def limits(self, **declared) -> dict:
        return resources.thresholds({"resources": dict(
            {"pressure_scope": "own_cgroup"}, **declared)})

    def breach_keys(self, reading: dict, limits: dict) -> list[str]:
        return [b["key"] for b in resources.breaches(reading, limits)]

    def test_cpu_used_is_the_ima_cpu_gate_and_needs_the_new_key(self):
        cgroup = self.sample(
            {"usage_usec": 0, "nr_periods": 100, "nr_throttled": 0,
             "current": 3 * GIB},
            {"usage_usec": 7_400_000, "nr_periods": 100, "nr_throttled": 0,
             "current": 3 * GIB})
        self.assertEqual(cgroup["cpu_used"], 3.7)
        self.assertEqual(cgroup["cpu_quota"], 4.0)
        self.assertEqual(cgroup["cpu_used_ratio"], 0.925)
        self.assertEqual(self.ima(cgroup)["cpu"], cgroup["cpu_used_ratio"])
        reading = {"cgroup": cgroup}

        # gap: IMA's CPU gate (0.90) is crossed, no existing native key sees it
        old = self.limits(max_cgroup_throttled=IMA_THROTTLED,
                          max_cpu_pressure=20.0)
        keys = self.breach_keys(reading, old)
        self.assertNotIn("max_cgroup_cpu_used", keys)
        self.assertEqual([k for k in keys if k.startswith("max_cgroup")], [])

        # the new declarative key carries the same verdict as IMA
        new = self.limits(max_cgroup_cpu_used="90%")
        keys = self.breach_keys(reading, new)
        self.assertIn("max_cgroup_cpu_used", keys)
        self.assertEqual(new["max_cgroup_cpu_used"], IMA_CPU)
        self.assertEqual(
            next(b for b in resources.breaches(reading, new)
                 if b["key"] == "max_cgroup_cpu_used")["value"],
            self.ima(cgroup)["cpu"])

        # at or under the threshold there is no breach; above it is critical
        # halfway to 1.0, like the other cgroup ceilings.
        under = self.limits(max_cgroup_cpu_used="95%")
        self.assertNotIn("max_cgroup_cpu_used", self.breach_keys(reading, under))
        self.assertEqual(self.limits()["max_cgroup_cpu_used"], None)

    def test_memory_matches_ima_and_keeps_its_default(self):
        cgroup = self.sample(
            {"current": 2 * GIB}, {"current": 4 * GIB * 85 // 100})
        self.assertEqual(cgroup["mem_used"], IMA_MEMORY)
        self.assertEqual(self.ima(cgroup)["memory"], IMA_MEMORY)
        at_threshold = self.limits(max_cgroup_memory_used="85%")
        self.assertNotIn("max_cgroup_memory_used",
                         self.breach_keys({"cgroup": cgroup}, at_threshold))
        self.assertEqual(self.limits()["max_cgroup_memory_used"], 0.9)
        over = self.limits(max_cgroup_memory_used="80%")
        self.assertIn("max_cgroup_memory_used",
                      self.breach_keys({"cgroup": cgroup}, over))

    def test_throttled_periods_and_wall_time_are_distinct_and_both_kept(self):
        # 30 % of periods throttled, but only 10 % of wall time: IMA's gate
        # (periods) is crossed while the existing wall-time key is not.
        cgroup = self.sample(
            {"usage_usec": 1_000_000, "nr_periods": 100, "nr_throttled": 5,
             "throttled_usec": 0},
            {"usage_usec": 3_000_000, "nr_periods": 300, "nr_throttled": 65,
             "throttled_usec": 200_000})
        self.assertEqual(cgroup["throttled_periods"], 0.3)
        self.assertEqual(cgroup["throttled"], 0.1)
        self.assertEqual(self.ima(cgroup)["throttled"], 0.3)
        reading = {"cgroup": cgroup}

        old = self.limits(max_cgroup_throttled="50%")
        keys = self.breach_keys(reading, old)
        self.assertNotIn("max_cgroup_throttled", keys)
        self.assertNotIn("max_cgroup_throttled_periods", keys)

        new = self.limits(max_cgroup_throttled="50%",
                          max_cgroup_throttled_periods="25%")
        keys = self.breach_keys(reading, new)
        self.assertIn("max_cgroup_throttled_periods", keys)
        self.assertNotIn("max_cgroup_throttled", keys)
        self.assertEqual(new["max_cgroup_throttled_periods"], IMA_THROTTLED)
        self.assertEqual(new["max_cgroup_throttled"], 0.5)

    def test_unknown_readings_are_never_a_breach_and_requests_are_not_limits(self):
        cgroup = self.sample(
            {"maximum": None, "quota": "max", "nr_periods": 100,
             "nr_throttled": 5},
            {"maximum": None, "quota": "max", "nr_periods": 300,
             "nr_throttled": 65})
        self.assertIsNone(cgroup["mem_used"])
        self.assertIsNone(cgroup["cpu_quota"])
        self.assertIsNone(cgroup["cpu_used_ratio"])
        # the period ratio does not need a finite quota
        self.assertEqual(cgroup["throttled_periods"], 0.3)
        declared = dict(max_cgroup_memory_used="80%",
                        max_cgroup_cpu_used="80%",
                        max_cgroup_throttled_periods="90%")
        keys = self.breach_keys({"cgroup": cgroup}, self.limits(**declared))
        self.assertNotIn("max_cgroup_memory_used", keys)
        self.assertNotIn("max_cgroup_cpu_used", keys)

        # no cgroup measure at all: visible fallback to host, never a blind
        # `own_cgroup` with no guard.
        verdict = resources.pressure(
            {"cgroup": {"container": True, "bounded": False}},
            limits=self.limits(max_cgroup_cpu_used="80%"))
        self.assertEqual(verdict["scope"], "host")
        self.assertIn("scope_fallback", verdict)
        self.assertFalse(verdict["blocked"])

        # requests are visible in IMA's reading but are not thresholds: the
        # native policy only names limits, and a request-only change moves no
        # verdict. `thresholds` has no request key.
        self.assertNotIn("cpu_request", self.limits())
        self.assertNotIn("memory_request", self.limits())

    def test_the_new_keys_accept_fractions_like_the_other_ceilings(self):
        limits = self.limits(max_cgroup_cpu_used="90%",
                             max_cgroup_throttled_periods=0.25)
        self.assertEqual(limits["max_cgroup_cpu_used"], 0.9)
        self.assertEqual(limits["max_cgroup_throttled_periods"], 0.25)
        # absent/unreadable stays unwatched, not a prudent invented default
        self.assertIsNone(self.limits()["max_cgroup_cpu_used"])
        self.assertIsNone(self.limits()["max_cgroup_throttled_periods"])


def _wait_queued(lock: admission_lock.PriorityRLock, count: int,
                 kind: int = admission_lock.NORMAL, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with lock._condition:
            if len(lock._queues[kind]) >= count:
                return
        time.sleep(0.002)
    raise AssertionError("lock queue never reached %d/%d" % (count, kind))


class AdmissionTest(unittest.TestCase):
    """The awakened-document vs role/background acceptance case."""

    def setUp(self):
        admission_lock.reset()

    def tearDown(self):
        admission_lock.reset()

    def test_an_awakened_document_goes_before_role_and_background_passes(self):
        lock = admission_lock.PriorityRLock(clock=lambda: 1000.0)
        lock.acquire()
        plan = [
            (admission_lock.NORMAL, "role-pass-0"),
            (admission_lock.NORMAL, "background-1"),
            (admission_lock.NORMAL, "role-pass-2"),
            (admission_lock.CONTROL, "document-woken"),
        ]
        # one shared order, appended under the lock
        order = []
        threads = []
        counts = [0, 0]
        for kind, label in plan:
            thread = threading.Thread(
                target=lambda k=kind, n=label: (
                    admission_lock.set_priority(k), lock.acquire(),
                    order.append(n), lock.release()))
            thread.start()
            counts[kind] += 1
            _wait_queued(lock, counts[kind], kind)
            threads.append(thread)
        lock.release()
        for thread in threads:
            thread.join(timeout=3)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(order[0], "document-woken")
        self.assertEqual(order[1:], ["role-pass-0", "background-1", "role-pass-2"])

    def test_an_awakened_document_and_role_are_fifo(self):
        lock = admission_lock.PriorityRLock(clock=lambda: 1000.0)
        lock.acquire()
        order = []
        threads = []
        for label in ("document-woken", "role-woken"):
            thread = threading.Thread(
                target=lambda n=label: (
                    admission_lock.set_priority(admission_lock.CONTROL),
                    lock.acquire(), order.append(n), lock.release()))
            thread.start()
            _wait_queued(lock, len(threads) + 1, admission_lock.CONTROL)
            threads.append(thread)
        lock.release()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(order, ["document-woken", "role-woken"])

    def test_background_latency_is_bounded_by_the_control_burst(self):
        # No aging (clock frozen), so only the burst bound remains. Under a
        # continuous stream of awakened work a role/background pass waits at
        # most BOOST_BURST grants: bounded, never starved.
        lock = admission_lock.PriorityRLock(clock=lambda: 1000.0)
        with mock.patch.object(admission_lock, "AGING_S", 1e9):
            lock.acquire()
            order = []
            threads = []
            for index in range(20):
                label = "awakened-%d" % index
                thread = threading.Thread(
                    target=lambda n=label: (
                        admission_lock.set_priority(admission_lock.CONTROL),
                        lock.acquire(), order.append(n), lock.release()))
                thread.start()
                _wait_queued(lock, index + 1, admission_lock.CONTROL)
                threads.append(thread)
            background = threading.Thread(
                target=lambda: (
                    admission_lock.set_priority(admission_lock.NORMAL),
                    lock.acquire(), order.append("background"),
                    lock.release()))
            background.start()
            _wait_queued(lock, 1, admission_lock.NORMAL)
            lock.release()
            for thread in threads + [background]:
                thread.join(timeout=5)
            self.assertFalse(background.is_alive())
        self.assertEqual(order.index("background"), admission_lock.BOOST_BURST)

    def test_aging_lets_an_old_background_pass_go_first(self):
        now = [100.0]
        lock = admission_lock.PriorityRLock(clock=lambda: now[0])
        lock.acquire()
        order = []
        normal = threading.Thread(
            target=lambda: (admission_lock.set_priority(admission_lock.NORMAL),
                            lock.acquire(), order.append("background"),
                            lock.release()))
        normal.start()
        _wait_queued(lock, 1, admission_lock.NORMAL)
        now[0] = 105.0
        control = threading.Thread(
            target=lambda: (admission_lock.set_priority(admission_lock.CONTROL),
                            lock.acquire(), order.append("awakened"),
                            lock.release()))
        control.start()
        _wait_queued(lock, 1, admission_lock.CONTROL)
        lock.release()
        normal.join(timeout=3)
        control.join(timeout=3)
        self.assertEqual(order, ["background", "awakened"])


if __name__ == "__main__":  # pragma: no cover - unittest discovery
    unittest.main()
