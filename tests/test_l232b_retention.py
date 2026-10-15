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
import time
import unittest
from unittest import mock

from tests import support  # Isolate runner environment before using the bench.


PATH = Path(__file__).resolve().parents[1] / "deploy/mise-a-jour/retention-privee.py"
SPEC = importlib.util.spec_from_file_location("private_retention", PATH)
retention = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retention)


class RemoteAdapter:
    """Simulated immutable object store with an atomic digest precondition."""
    def __init__(self, objects):
        self.objects = dict(objects)
        self.calls = []

    def inspect(self, object_id):
        self.calls.append(("inspect", object_id))
        return self.objects.get(object_id)

    def delete_if_digest(self, object_id, sha256):
        self.calls.append(("delete", object_id))
        if self.objects.get(object_id) not in (None, sha256):
            raise retention.RetentionError("remote digest precondition failed")
        self.objects.pop(object_id, None)
        return dict(object_id=object_id, sha256=sha256, absent=True)


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

    def daily(self, name, *, age, content=b"daily dump", now=2_000_000):
        path = self.root / name
        path.write_bytes(content)
        path.chmod(0o600)
        stamp = now - age
        os.utime(path, (stamp, stamp))
        return path

    def test_unregistered_daily_expiry_spares_catalogued_sets_and_recent_dumps(self):
        self.artifact(rollback=0)
        retention.protect(self.root, "old", accepted=True)
        stale = self.daily("ameesh-host-2026-01-01-030000.dump", age=14 * 24 * 3600)
        recent = self.daily("ameesh-host-2026-01-02-030000.dump", age=3600)
        other = self.root / "notes.txt"
        other.write_bytes(b"not a dump")
        result = retention.expire_unregistered(self.root, now=2_000_000, max_age=14 * 24 * 3600)
        self.assertEqual(result, {"deleted": ["ameesh-host-2026-01-01-030000.dump"], "refused": []})
        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.root / "old.dump").exists(), "a catalogued set is never expired by age")

    def test_expire_unregistered_command_fails_when_a_candidate_is_refused(self):
        # Un refus n'est pas silencieux : la commande sort non nulle et le
        # fichier, qui n'expirera jamais, reste visible dans le JSON.
        public = self.daily("ameesh-public.dump", age=15 * 24 * 3600)
        public.chmod(0o644)
        result = subprocess.run([sys.executable, str(PATH), "--root", str(self.root),
                                 "expire-unregistered", "--now", "2000000",
                                 "--max-age", str(14 * 24 * 3600)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["refused"], ["ameesh-public.dump"])
        self.assertIn("refused", result.stderr)
        self.assertTrue(public.exists())

    def test_unregistered_daily_expiry_refuses_unsafe_candidates(self):
        target = self.root / "target"
        target.write_bytes(b"private target")
        target.chmod(0o600)
        link = self.root / "ameesh-link.dump"
        link.symlink_to(target)
        public = self.daily("ameesh-public.dump", age=14 * 24 * 3600)
        public.chmod(0o644)
        old = self.daily("ameesh-old.dump", age=14 * 24 * 3600)
        result = retention.expire_unregistered(self.root, now=2_000_000, max_age=14 * 24 * 3600)
        self.assertEqual(result["deleted"], ["ameesh-old.dump"])
        self.assertEqual(sorted(result["refused"]), ["ameesh-link.dump", "ameesh-public.dump"])
        self.assertTrue(link.is_symlink())
        self.assertTrue(public.exists())
        self.assertTrue(target.exists())
        self.assertFalse(old.exists())

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
        unregistered = self.daily("ameesh-host-2026-01-01-030000.dump", age=14 * 24 * 3600,
                                  now=time.time())
        result = subprocess.run(["sh", "-c", self.server_script()], capture_output=True,
                                env=dict(os.environ, BACKUP_FAIL="1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / "old.dump").exists())
        self.assertTrue(unregistered.exists(), "a failed backup never starts expiry")

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

    def test_server_completed_backup_expires_stale_unregistered_dumps(self):
        stale = self.daily("ameesh-host-2026-01-01-030000.dump", age=15 * 24 * 3600, now=time.time())
        fresh = self.daily("ameesh-host-2026-01-02-030000.dump", age=3600, now=time.time())
        result = subprocess.run(["sh", "-c", self.server_script()], capture_output=True,
                                env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(len(list(self.root.glob("ameesh-*.dump"))), 2, "fresh and new dumps survive")

    def test_server_refused_unregistered_dump_fails_the_run_after_prune(self):
        # Un dump non privé n'expire jamais : la course quotidienne doit sortir
        # non nulle, sans empêcher prune de tourner.
        self.artifact(rollback=0)
        retention.protect(self.root, "old", accepted=True)
        public = self.daily("ameesh-public.dump", age=15 * 24 * 3600, now=time.time())
        public.chmod(0o644)
        result = subprocess.run(["sh", "-c", self.server_script()], capture_output=True,
                                env=dict(os.environ))
        self.assertNotEqual(result.returncode, 0, "a refused candidate must fail the daily run")
        self.assertTrue(public.exists())
        self.assertFalse((self.root / "old.dump").exists(), "prune still ran")

    def remote_artifact(self):
        path = self.root / "remote.dump"
        path.write_bytes(b"synthetic remote recovery")
        path.chmod(0o600)
        sha = retention.private_digest(path)
        objects = {"host-1/remote.dump": sha, "host-2/remote.dump": sha}
        retention.register(self.root, "remote", ["remote.dump"], expires_at=100, rollback_until=200,
                           remote=[dict(object_id=key, sha256=value) for key, value in objects.items()])
        return RemoteAdapter(objects)

    def test_remote_protection_blocks_adapter_and_missing_adapter_blocks_local(self):
        adapter = self.remote_artifact()
        self.assertEqual(retention.prune(self.root, now=1000, remote_adapter=adapter), [])
        self.assertEqual(adapter.calls, [])
        retention.protect(self.root, "remote", accepted=True, hold=True)
        self.assertEqual(retention.prune(self.root, now=1000, remote_adapter=adapter), [])
        self.assertEqual(adapter.calls, [])
        retention.protect(self.root, "remote", hold=False)
        with self.assertRaises(retention.RetentionError):
            retention.prune(self.root, now=1000)
        self.assertTrue((self.root / "remote.dump").exists())

    def test_remote_deletion_is_confirmed_before_local_deletion(self):
        adapter = self.remote_artifact()
        retention.protect(self.root, "remote", accepted=True)
        delete = adapter.delete_if_digest

        def delete_while_local_retained(object_id, sha):
            self.assertTrue((self.root / "remote.dump").exists())
            record = json.loads((self.root / ".retention.json").read_text())["remote"]
            self.assertEqual(record["state"], "deleting")
            return delete(object_id, sha)

        with mock.patch.object(adapter, "delete_if_digest", side_effect=delete_while_local_retained):
            self.assertEqual(retention.prune(self.root, now=1000, remote_adapter=adapter), ["remote"])
        self.assertEqual(adapter.objects, {})
        self.assertFalse((self.root / "remote.dump").exists())

    def test_remote_changed_or_missing_before_intent_keeps_entire_batch(self):
        for altered in (None, "f" * 64):
            with self.subTest(altered=altered):
                if not (self.root / ".retention.json").exists():
                    adapter = self.remote_artifact()
                    self.artifact("local")
                    retention.protect(self.root, "remote", accepted=True)
                    retention.protect(self.root, "local", accepted=True)
                adapter.objects["host-2/remote.dump"] = altered
                with self.assertRaises(retention.RetentionError) as caught:
                    retention.prune(self.root, now=1000, remote_adapter=adapter)
                # L'erreur nomme l'ensemble et l'objet à réconcilier.
                self.assertIn("remote", str(caught.exception))
                self.assertIn("host-2/remote.dump", str(caught.exception))
                self.assertFalse(any(call[0] == "delete" for call in adapter.calls))
                self.assertTrue((self.root / "local.dump").exists())
                self.assertTrue((self.root / "remote.dump").exists())

    def test_remote_failure_after_one_confirmation_resumes_without_losing_local(self):
        adapter = self.remote_artifact()
        retention.protect(self.root, "remote", accepted=True)
        delete = adapter.delete_if_digest

        def fail_second(object_id, sha):
            if object_id.startswith("host-2/"):
                raise OSError("synthetic storage outage")
            return delete(object_id, sha)

        with mock.patch.object(adapter, "delete_if_digest", side_effect=fail_second), self.assertRaises(OSError):
            retention.prune(self.root, now=1000, remote_adapter=adapter)
        record = json.loads((self.root / ".retention.json").read_text())["remote"]
        self.assertEqual([item["deleted"] for item in record["remote"]], [True, False])
        self.assertTrue((self.root / "remote.dump").exists())
        adapter.calls.clear()
        self.assertEqual(retention.prune(self.root, now=1001, remote_adapter=adapter), ["remote"])
        self.assertEqual(adapter.calls, [("inspect", "host-2/remote.dump"), ("delete", "host-2/remote.dump")])

    def test_lost_remote_receipt_replays_absence_only_after_intent(self):
        adapter = self.remote_artifact()
        retention.protect(self.root, "remote", accepted=True)
        delete = adapter.delete_if_digest

        def lose_receipt(object_id, sha):
            delete(object_id, sha)
            return dict(object_id="wrong/object", sha256=sha, absent=True)

        with mock.patch.object(adapter, "delete_if_digest", side_effect=lose_receipt), self.assertRaises(retention.RetentionError):
            retention.prune(self.root, now=1000, remote_adapter=adapter)
        self.assertTrue((self.root / "remote.dump").exists())
        self.assertEqual(retention.prune(self.root, now=1001, remote_adapter=adapter), ["remote"])

    def test_remote_object_changed_after_inspect_is_not_deleted(self):
        adapter = self.remote_artifact()
        retention.protect(self.root, "remote", accepted=True)
        delete = adapter.delete_if_digest

        def race(object_id, sha):
            adapter.objects[object_id] = "f" * 64
            return delete(object_id, sha)

        with mock.patch.object(adapter, "delete_if_digest", side_effect=race), self.assertRaises(retention.RetentionError):
            retention.prune(self.root, now=1000, remote_adapter=adapter)
        self.assertTrue((self.root / "remote.dump").exists())
        self.assertEqual(adapter.objects["host-1/remote.dump"], "f" * 64)

    def test_cli_remote_expiry_fails_closed_without_backend(self):
        self.remote_artifact()
        retention.protect(self.root, "remote", accepted=True)
        result = subprocess.run([sys.executable, str(PATH), "--root", str(self.root),
                                 "prune", "--now", "1000"], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"host-1", result.stderr)
        self.assertTrue((self.root / "remote.dump").exists())

    def test_remote_reference_and_digest_validation_preserves_catalog(self):
        adapter = self.remote_artifact()
        before = (self.root / ".retention.json").read_bytes()
        candidate = self.root / "candidate.dump"
        candidate.write_bytes(b"synthetic remote recovery")
        candidate.chmod(0o600)
        sha = retention.private_digest(candidate)
        for object_id, digest in (("host/../escape", sha), ("host/object", "f" * 64),
                                  ("host-1/remote.dump", sha)):
            with self.subTest(object_id=object_id), self.assertRaises(retention.RetentionError):
                retention.register(self.root, "candidate", ["candidate.dump"], expires_at=1, rollback_until=2,
                                   remote=[dict(object_id=object_id, sha256=digest)])
            self.assertEqual((self.root / ".retention.json").read_bytes(), before)
        self.assertEqual(adapter.calls, [])


if __name__ == "__main__":
    unittest.main()
