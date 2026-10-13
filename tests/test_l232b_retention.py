# SPDX-License-Identifier: AGPL-3.0-only
"""Expired recovery sets remain protected until release acceptance/rollback."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import support  # Isolate runner environment before using the bench.


PATH = Path(__file__).resolve().parents[1] / "deploy/mise-a-jour/retention-privee.py"
SPEC = importlib.util.spec_from_file_location("private_retention", PATH)
retention = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retention)


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-retention-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)

    def artifact(self, name="old", rollback=200):
        for suffix in (".dump", ".inventory.json"):
            path = self.root / (name + suffix)
            path.write_bytes(b"synthetic private backup")
            path.chmod(0o600)
        retention.register(self.root, name, [name + ".dump", name + ".inventory.json"],
                           expires_at=100, rollback_until=rollback)

    def test_acceptance_and_rollback_window_both_required(self):
        self.artifact()
        self.assertEqual(retention.prune(self.root, now=1000), [])
        retention.protect(self.root, "old", accepted=True)
        self.assertEqual(retention.prune(self.root, now=199), [])
        self.assertTrue((self.root / "old.dump").exists())
        self.assertEqual(retention.prune(self.root, now=200), ["old"])
        self.assertFalse((self.root / "old.dump").exists())
        self.assertFalse((self.root / "old.inventory.json").exists())
        self.assertEqual(retention.prune(self.root, now=201), [])
        record = json.loads((self.root / ".retention.json").read_text())["old"]
        self.assertEqual(record["state"], "deleted")

    def test_explicit_hold_and_unknown_backups_survive(self):
        self.artifact()
        unknown = self.root / "ameesh-old-unregistered.dump"
        unknown.write_bytes(b"unknown")
        retention.protect(self.root, "old", accepted=True, hold=True)
        self.assertEqual(retention.prune(self.root, now=1000), [])
        retention.protect(self.root, "old", hold=False)
        self.assertEqual(retention.prune(self.root, now=1000), ["old"])
        self.assertEqual(unknown.read_bytes(), b"unknown")

    def test_changed_candidate_blocks_whole_batch(self):
        self.artifact("first")
        self.artifact("second")
        for name in ("first", "second"):
            retention.protect(self.root, name, accepted=True)
        (self.root / "second.dump").write_bytes(b"changed")
        with self.assertRaises(retention.RetentionError):
            retention.prune(self.root, now=1000)
        self.assertTrue((self.root / "first.dump").exists())
        self.assertTrue((self.root / "second.dump").exists())

    def test_interruption_replays_durable_deletion_intent(self):
        self.artifact()
        retention.protect(self.root, "old", accepted=True)
        unlink = Path.unlink

        def interrupted(path, *args, **kwargs):
            if path.name == "old.inventory.json":
                raise OSError("synthetic interruption")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", interrupted), self.assertRaises(OSError):
            retention.prune(self.root, now=1000)
        self.assertFalse((self.root / "old.dump").exists())
        record = json.loads((self.root / ".retention.json").read_text())["old"]
        self.assertEqual(record["state"], "deleting")
        with self.assertRaises(retention.RetentionError):
            retention.protect(self.root, "old", hold=True)
        self.assertEqual(retention.prune(self.root, now=1000), ["old"])
        self.assertFalse((self.root / "old.inventory.json").exists())

    def test_missing_file_without_intent_is_refused(self):
        self.artifact()
        retention.protect(self.root, "old", accepted=True)
        (self.root / "old.dump").unlink()
        with self.assertRaises(OSError):
            retention.prune(self.root, now=1000)
        self.assertTrue((self.root / "old.inventory.json").exists())

    def test_symlink_hardlink_public_file_and_traversal_refused(self):
        self.artifact()
        original = self.root / "old.dump"
        original.unlink()
        target = self.root / "target"
        target.write_bytes(b"private target")
        target.chmod(0o600)
        original.symlink_to(target)
        with self.assertRaises(OSError):
            retention.register(self.root, "linked", ["old.dump"], expires_at=1, rollback_until=2)
        original.unlink()
        os.link(target, original)
        with self.assertRaises(retention.RetentionError):
            retention.register(self.root, "linked", ["old.dump"], expires_at=1, rollback_until=2)
        original.unlink()
        target.chmod(0o644)
        with self.assertRaises(retention.RetentionError):
            retention.register(self.root, "public", ["target"], expires_at=1, rollback_until=2)
        with self.assertRaises(retention.RetentionError):
            retention.register(self.root, "escape", ["../target"], expires_at=1, rollback_until=2)

    def test_bad_catalog_cannot_authorize_deletion(self):
        self.artifact()
        retention.protect(self.root, "old", accepted=True)
        path = self.root / ".retention.json"
        catalog = json.loads(path.read_text())
        for field, value in (("accepted", "yes"), ("expires_at", float("nan")), ("hold", 0)):
            broken = json.loads(json.dumps(catalog))
            broken["old"][field] = value
            path.write_text(json.dumps(broken))
            with self.assertRaises(retention.RetentionError):
                retention.prune(self.root, now=1000)
            self.assertTrue((self.root / "old.dump").exists())

    def test_empty_registry_deletes_nothing(self):
        unknown = self.root / "ameesh-old.dump"
        unknown.write_bytes(b"old")
        self.assertEqual(retention.prune(self.root, now=1000), [])
        self.assertTrue(unknown.exists())

    def server_script(self):
        helper = self.root / "fake-backup.py"
        helper.write_text("import os, sys\nfrom pathlib import Path\n"
                          "if os.environ.get('BACKUP_FAIL'): sys.exit(7)\n"
                          "Path(sys.argv[sys.argv.index('--output') + 1]).write_bytes(b'new dump')\n")
        script = PATH.parents[1] / "serveur/sauvegarder-base.sh"
        return (script.read_text()
                .replace("d=/home/ameesh/sauvegardes", "d=" + shlex.quote(str(self.root)))
                .replace("python=/opt/ameesh/venv/bin/python", "python=" + shlex.quote(sys.executable))
                .replace("helper=/usr/local/sbin/ameesh-sauvegarder-prive.py", "helper=" + shlex.quote(str(helper)))
                .replace("/etc/ameesh/sauvegarde.env", str(self.root / "absent-storage.env"))
                .replace("/usr/local/sbin/ameesh-retention-privee.py", shlex.quote(str(PATH))))

    def test_server_failed_backup_never_starts_expiry(self):
        self.artifact(rollback=0)
        retention.protect(self.root, "old", accepted=True)
        result = subprocess.run(["sh", "-c", self.server_script()], capture_output=True,
                                env=dict(os.environ, BACKUP_FAIL="1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "old.dump").exists())

    def test_server_completed_backup_expires_only_unprotected_sets(self):
        self.artifact(rollback=0)
        self.artifact("held", rollback=0)
        retention.protect(self.root, "old", accepted=True)
        retention.protect(self.root, "held", accepted=True, hold=True)
        result = subprocess.run(["sh", "-c", self.server_script()], capture_output=True,
                                env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "old.dump").exists())
        self.assertTrue((self.root / "held.dump").exists())
        self.assertEqual(len(list(self.root.glob("ameesh-*.dump"))), 1)


if __name__ == "__main__":
    unittest.main()
