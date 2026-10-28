# SPDX-License-Identifier: AGPL-3.0-only
"""Reserved capacity from the publisher, including unlimited cgroups."""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from . import support  # isolate the runner environment before measuring
from .support import PgTestCase
from .test_2814_pressure_cgroup_capacity import write_cgroup
from ameesh import resources, storage

GIB = 1024 ** 3


class FootprintTest(unittest.TestCase):
    def reading(self, **extra):
        return dict(cpu_count=8, mem_total_bytes=16 * GIB,
                    mem_available_bytes=10 * GIB, app_cpu=1.25,
                    guest_cpu=0.5, **extra)

    def cgroup(self, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        write_cgroup(tmp.name, **kwargs)
        with mock.patch.object(resources.platform.host, "in_container", return_value=True):
            return resources.CgroupMeter(tmp.name).read(now=100)

    def test_container_limits_are_published_reservations(self):
        cg = self.cgroup(quota="250000", maximum=2 * GIB)
        out = resources.measure(self.reading(cgroup=cg, sampled_ts=10))
        self.assertEqual(out["reserved_cpu"], 2.5)
        self.assertEqual(out["reserved_mem_bytes"], 2 * GIB)
        self.assertEqual(out["used_mem_bytes"], 100 * 1024 ** 2)
        self.assertIsNone(out["used_cpu"])  # CPU needs two samples
        self.assertEqual(out["reserved_cpu_source"], "cgroup")
        self.assertEqual(out["ts"], 10)  # never refresh an old reading

    def test_unlimited_container_falls_back_per_resource(self):
        for quota, maximum, cpu, memory in (
                ("max", None, 8, 16 * GIB),
                ("250000", None, 2.5, 16 * GIB),
                ("max", 2 * GIB, 8, 2 * GIB)):
            with self.subTest(quota=quota, maximum=maximum):
                out = resources.footprint(self.reading(cgroup=self.cgroup(
                    quota=quota, maximum=maximum)))
                self.assertEqual((out["reserved_cpu"], out["reserved_mem_bytes"]),
                                 (cpu, memory))

    def test_vm_reserves_machine_even_with_a_bounded_runner_slice(self):
        cg = self.cgroup(quota="100000", maximum=GIB)
        cg["container"] = False
        out = resources.footprint(self.reading(cgroup=cg))
        self.assertEqual(out["reserved_cpu"], 8)
        self.assertEqual(out["reserved_mem_bytes"], 16 * GIB)
        self.assertEqual(out["used_cpu"], 1.75)
        self.assertEqual(out["used_mem_bytes"], 6 * GIB)
        self.assertEqual(out["reserved_mem_source"], "machine")

    def test_absent_measurements_stay_unknown(self):
        out = resources.measure(None)
        for key in ("reserved_cpu", "reserved_mem_bytes", "used_cpu", "used_mem_bytes"):
            self.assertIsNone(out[key])

    def test_container_cpu_usage_is_not_the_host_usage(self):
        out = resources.footprint(self.reading(cgroup={
            "container": True, "cpu_quota": 2, "cpu_used": 0.3,
            "mem_max_bytes": GIB, "mem_current_bytes": 0}))
        self.assertEqual(out["used_cpu"], 0.3)
        self.assertEqual(out["used_mem_bytes"], 0)


class FootprintDbTest(PgTestCase):
    def test_persisted_container_reading_keeps_reservation(self):
        hosts = storage.of(self.db).hosts
        hosts.record({"host": "footprint", "cpu_count": 8,
                      "mem_total_bytes": 16 * GIB,
                      "cgroup": {"container": True, "cpu_quota": 1.5,
                                 "mem_max_bytes": GIB, "mem_current_bytes": 100,
                                 "cpu_used": 0.25}})
        out = resources.measure(hosts.latest("footprint"))
        self.assertEqual(out["reserved_cpu"], 1.5)
        self.assertEqual(out["reserved_mem_bytes"], GIB)
        self.assertEqual(out["used_cpu"], 0.25)
        self.assertEqual(out["used_mem_bytes"], 100)
