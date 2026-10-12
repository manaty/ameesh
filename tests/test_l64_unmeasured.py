# SPDX-License-Identifier: AGPL-3.0-only
"""L64: the host measures this OS cannot give are said, never left silent."""
from __future__ import annotations

import dataclasses
import tempfile
import unittest
from unittest import mock

from ameesh import cli, config, resources

FULL = {"mem_available_bytes": 1, "swap_used_bytes": 0, "load1": 0.5, "cpu_count": 4,
        "disk_free_bytes": 10, "tmp_fstype": "tmpfs", "psi_cpu_some": 0.0,
        "psi_mem_some": 0.0, "psi_io_some": 0.0, "guest_mem_bytes": 0}


class UnmeasuredTest(unittest.TestCase):
    def test_full_reading(self):
        self.assertEqual(resources.unmeasured(FULL), [])
        self.assertEqual(resources.unmeasured(None), [])

    def test_zero_is_a_measure_none_is_not(self):
        macos = dict(FULL, load1=None, psi_cpu_some=None, psi_mem_some=None,
                     psi_io_some=None, swap_used_bytes=0)
        self.assertEqual(resources.unmeasured(macos),
                         ["load", "CPU pressure (PSI)", "memory pressure (PSI)",
                          "I/O pressure (PSI)"])

    def test_absent_key_counts_as_unmeasured(self):
        partial = {k: v for k, v in FULL.items() if k != "guest_mem_bytes"}
        self.assertEqual(resources.unmeasured(partial), ["guest memory (cgroup)"])

    def test_every_measure_named_once(self):
        keys = [k for k, _ in resources.MEASURES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("app_cpu", keys)  # needs two readings


class DoctorLineTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dataclasses.replace(config.load(), state_dir=tempfile.gettempdir())

    def test_line_lists_the_missing_measures(self):
        line = cli.host_measures_line(self.cfg, dict(FULL, load1=None))
        self.assertIn("not given on this host", line)
        self.assertIn("load", line)
        self.assertIn("every host measure is read",
                      cli.host_measures_line(self.cfg, FULL))

    def test_fresh_sample_when_no_reading(self):
        with mock.patch.object(resources, "sample", return_value=dict(FULL, psi_io_some=None)):
            self.assertIn("I/O pressure (PSI)", cli.host_measures_line(self.cfg))

    def test_a_failed_reading_never_breaks_the_doctor(self):
        with mock.patch.object(resources, "sample", side_effect=OSError("boom")):
            self.assertIn("host not read (boom)", cli.host_measures_line(self.cfg))


if __name__ == "__main__":
    unittest.main()
