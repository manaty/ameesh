# SPDX-License-Identifier: AGPL-3.0-only
"""L64: the host measures this OS cannot give are said, never left silent."""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from ameesh import cli, config, resources, storage

from .support import PgTestCase

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

    def test_btrfs_volume_without_allocation(self):
        vols = [{"path": "/srv", "fstype": "btrfs", "btrfs_unallocated_bytes": None,
                 "btrfs_metadata_headroom_bytes": None},
                {"path": "/home", "fstype": "btrfs", "btrfs_unallocated_bytes": 1,
                 "btrfs_metadata_headroom_bytes": 1},
                {"path": "/", "fstype": "ext4", "btrfs_unallocated_bytes": None,
                 "btrfs_metadata_headroom_bytes": None}]
        self.assertEqual(resources.unmeasured(dict(FULL, volumes=vols)),
                         ["btrfs allocation (/srv)"])

    def test_volume_records_its_fstype(self):
        mounts = [{"id": 1, "point": "/", "dev": 1, "fstype": "ext4", "source": "x"},
                  {"id": 2, "point": "/srv", "dev": 2, "fstype": "btrfs", "source": "y"}]
        with mock.patch.object(resources.platform, "is_linux", return_value=True), \
                mock.patch.object(resources.platform, "mountinfo", return_value=mounts), \
                mock.patch.object(resources, "volume_key", side_effect=lambda p: p), \
                mock.patch.object(resources, "_anchor", return_value=None):
            vols = resources.volumes(["/srv/a", "/var"])
        self.assertEqual([v["fstype"] for v in vols], ["btrfs", "ext4"])

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


class CommandsTest(PgTestCase):
    """Review #392 r1: the doctor line and the hosts output, at command level."""

    def _doctor(self, cfg, **patch) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(resources, "sample", **patch) as sample, \
                contextlib.redirect_stdout(out):
            code = cli.cmd_doctor(cfg, notify_test=False)
        self.calls = sample.call_args_list
        return code, out.getvalue()

    def test_doctor_samples_the_guard_filesystems(self):
        racine = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, racine)
        cfg = dataclasses.replace(self.cfg, worktree_roots=[racine])
        btrfs = {"path": racine, "fstype": "btrfs", "btrfs_unallocated_bytes": None,
                 "btrfs_metadata_headroom_bytes": None}
        code, text = self._doctor(cfg, return_value=dict(FULL, volumes=[btrfs]))
        paths = self.calls[0].kwargs["paths"]
        self.assertIn(racine, paths)       # a work root, not only the state dir
        self.assertIn(cfg.state_dir, paths)
        self.assertIn("measures   : not given on this host", text)
        self.assertIn("btrfs allocation (%s)" % racine, text)
        # a failed reading: said, and the verdict does not change
        code_full, _ = self._doctor(cfg, return_value=FULL)
        code_fail, text_fail = self._doctor(cfg, side_effect=OSError("boom"))
        self.assertIn("measures   : host not read (boom)", text_fail)
        self.assertEqual(code_fail, code_full)
        self.assertEqual(code, code_full)

    def test_doctor_reads_the_effective_policy(self):
        """Review #392 r2: the cache root of the canon's Host card is sampled
        (the guard's policy); an unreadable canon falls back, said."""
        from ameesh import canon, menage
        cache = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, cache)
        with mock.patch.object(canon, "load_configured", return_value=[]), \
                mock.patch.object(menage, "effective",
                                  return_value=menage.Policy(cache_root=cache)):
            _code, text = self._doctor(self.cfg, return_value=FULL)
        self.assertIn(cache, self.calls[0].kwargs["paths"])
        self.assertNotIn("default policy", text)
        with mock.patch.object(canon, "load_configured", side_effect=OSError("canon")):
            _code, text = self._doctor(self.cfg, return_value=FULL)
        self.assertIn("[default policy: canon unreadable]", text)

    def test_hosts_text_and_json(self):
        self.db.execute("DELETE FROM host_resources")
        storage.of(self.db).hosts.record(dict(
            FULL, host="pc-64", load1=None, psi_io_some=None,
            volumes=[{"path": "/srv", "paths": ["/srv"], "fstype": "btrfs",
                      "disk_free_bytes": 10, "btrfs_unallocated_bytes": None,
                      "btrfs_metadata_headroom_bytes": None}]))
        expected = ["load", "I/O pressure (PSI)", "btrfs allocation (/srv)"]
        proc = self.mesh("hosts", "pc-64", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout.splitlines()[0])["unmeasured"], expected)
        texte = self.mesh("hosts", "pc-64")
        self.assertEqual(texte.returncode, 0, texte.stderr)
        self.assertIn("  unmeasured %s" % ", ".join(expected), texte.stdout)


if __name__ == "__main__":
    unittest.main()
