# SPDX-License-Identifier: AGPL-3.0-only
"""An installed Python package must not leave the guest's root scripts stale."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "guest_runtime_4793", ROOT / "deploy/mise-a-jour/guest-runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class GuestRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.lib = self.root / "lib"
        self.config = self.root / "offload.json"

    def populate(self, *, legacy=False):
        self.lib.mkdir()
        self.lib.chmod(0o755)
        self.config.write_text('{"slots": 3}')
        for relative, name, mode in runtime.FILES:
            origin = self.source / relative
            origin.parent.mkdir(parents=True, exist_ok=True)
            origin.write_bytes(("new " + name).encode())
            if legacy and "launcher" in name:
                continue
            target = self.lib / name
            target.write_bytes(("old " + name).encode())
            target.chmod(mode)

    def refresh(self, **kwargs):
        return runtime.refresh(self.source, lib=self.lib, config=self.config,
                               owner=os.geteuid(), **kwargs)

    def test_no_guest_does_not_provision_anything(self):
        self.assertEqual(self.refresh()["guest"], "absent")
        self.assertFalse(self.lib.exists())

    def test_install_verify_and_rollback_to_an_older_tree(self):
        self.populate()
        with self.assertRaisesRegex(ValueError, "differs"):
            self.refresh(check=True)
        self.assertEqual((self.lib / "job-postgres").read_bytes(), b"old job-postgres")
        report = self.refresh()
        self.assertEqual(report["changed"], 4)
        self.assertEqual(len(report["files"]), 4)
        self.assertEqual(self.refresh(check=True)["changed"], 0)
        self.assertEqual(self.config.read_text(), '{"slots": 3}')
        # Rollback uses the operator's helper with an older source tree;
        # that tree need not contain guest-runtime.py.
        (self.source / "deploy/guest/job-postgres.sh").write_bytes(b"previous hook")
        self.assertEqual(self.refresh()["changed"], 1)
        self.assertEqual((self.lib / "job-postgres").read_bytes(), b"previous hook")

    def test_legacy_guest_does_not_gain_a_job_user_launcher(self):
        self.populate(legacy=True)
        self.assertEqual(self.refresh()["changed"], 2)
        self.assertFalse((self.lib / "ameesh-job-launcher").exists())

    def test_missing_source_or_half_installed_launcher_changes_nothing(self):
        for missing in ("source", "launcher"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                self.source = Path(tmp) / "source"
                self.lib = Path(tmp) / "lib"
                self.populate()
                if missing == "source":
                    (self.source / runtime.FILES[-1][0]).unlink()
                else:
                    (self.lib / "ameesh-job-launcher.py").unlink()
                with self.assertRaises(OSError):
                    self.refresh()
                self.assertEqual((self.lib / "job-postgres").read_bytes(), b"old job-postgres")

    def test_failed_second_rename_restores_the_first_file(self):
        self.populate()
        real = runtime.replace
        count = 0

        def fail_once(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected disk failure")
            return real(*args)

        with mock.patch.object(runtime, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.refresh()
        for _relative, name, _mode in runtime.FILES:
            self.assertEqual((self.lib / name).read_bytes(), ("old " + name).encode())
        self.assertFalse(list(self.lib.glob(".runtime-*")))

    def test_symlink_or_writable_installed_file_is_refused_before_mutation(self):
        self.populate()
        target = self.lib / "job-postgres"
        target.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.refresh()
        target.unlink()
        target.symlink_to(self.config)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.refresh()
        self.assertEqual(self.config.read_text(), '{"slots": 3}')
        self.assertEqual((self.lib / "ameesh-offload-shell").read_bytes(), b"old ameesh-offload-shell")


if __name__ == "__main__":
    unittest.main()
