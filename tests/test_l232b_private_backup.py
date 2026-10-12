# SPDX-License-Identifier: AGPL-3.0-only
"""Canary credentials stay private; failed dumps never get their final name."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "deploy/mise-a-jour/sauvegarder-prive.py"
SPEC = importlib.util.spec_from_file_location("private_backup", PATH)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class PrivateBackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-private-backup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"dsn": "postgresql://acme:CANARY-PASSWORD@localhost/db"}))
        self.output = self.root / "backups/archive.dump"

    def test_password_channel_and_atomic_completed_archive(self):
        seen = []

        def child(args, *, env=None):
            self.assertNotIn("CANARY-PASSWORD", str(args))
            if args[0] == "pg_dump":
                passfile = Path(env["PGPASSFILE"])
                self.assertEqual(stat.S_IMODE(passfile.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(passfile.parent.stat().st_mode), 0o700)
                self.assertIn("CANARY-PASSWORD", passfile.read_text())
                self.assertNotIn("PGPASSWORD", env)
                seen.append(passfile)
                Path(args[-1]).write_bytes(b"archive")
                self.assertFalse(self.output.exists())
            else:
                self.assertEqual(args[:2], ["pg_restore", "--list"])
                self.assertFalse(self.output.exists())
        with mock.patch.object(backup, "run", side_effect=child):
            backup.dump(self.config, self.output)
        self.assertEqual(self.output.read_bytes(), b"archive")
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertFalse(seen[0].exists())

    def test_failed_or_unreadable_archive_keeps_final_path_absent(self):
        def failed_check(args, *, env=None):
            if args[0] == "pg_dump":
                Path(args[-1]).write_bytes(b"incomplete")
            else:
                raise backup.BackupError("verification failed")
        for child in (mock.Mock(side_effect=backup.BackupError("dump failed")), failed_check):
            with self.subTest(child=child), mock.patch.object(backup, "run", side_effect=child):
                with self.assertRaises(backup.BackupError):
                    backup.dump(self.config, self.output)
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_existing_backup_cannot_be_replaced(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"old")
        with mock.patch.object(backup, "run") as child, self.assertRaises(backup.BackupError):
            backup.dump(self.config, self.output)
        child.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"old")

    def test_nested_uri_credentials_are_refused_before_any_child(self):
        self.config.write_text(json.dumps({"dsn": "dbname='postgresql://acme:CANARY-PASSWORD@localhost/db'"}))
        with mock.patch.object(backup, "run") as child, self.assertRaises(backup.BackupError):
            backup.dump(self.config, self.output)
        child.assert_not_called()

    def test_upload_uses_private_config_not_argv_or_child_environment(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"archive")
        seen = []

        def child(args, *, env=None):
            self.assertEqual(args[:2], ["curl", "--disable"])
            self.assertNotIn("CANARY-SECRET", str(args))
            self.assertNotIn("SCW_SECRET_KEY", env)
            self.assertNotIn("SCW_ACCESS_KEY", env)
            config = Path(args[3])
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertIn("CANARY-SECRET", config.read_text())
            seen.append(config)
        with mock.patch.dict(os.environ, {"SCW_ACCESS_KEY": "CANARY-ACCESS", "SCW_SECRET_KEY": "CANARY-SECRET",
                                           "SCW_BUCKET": "acme", "SCW_REGION": "fr-par", "TMPDIR": str(self.root)}), \
                mock.patch.object(backup, "run", side_effect=child):
            backup.upload(self.output, "host-1")
        self.assertFalse(seen[0].exists())

    def test_subprocess_private_diagnostic_is_not_exposed(self):
        import subprocess
        with mock.patch.object(backup.subprocess, "run", side_effect=subprocess.CalledProcessError(
                1, "pg_dump", stderr=b"CANARY-PASSWORD")):
            with self.assertRaises(backup.BackupError) as error:
                backup.run(["pg_dump"])
        self.assertNotIn("CANARY-PASSWORD", str(error.exception))

    def restore_fixture(self):
        self.output.parent.mkdir(exist_ok=True)
        self.output.write_bytes(b"archive")
        expected = {"schema": "public", "artifact_sha256": backup.digest(self.output),
                    "migrations": [[1, "checksum"]], "row_counts": {"agent_registry": 2}}
        admin, restored = mock.MagicMock(), mock.MagicMock()
        first, second = mock.MagicMock(), mock.MagicMock()
        first.__enter__.return_value = admin
        second.__enter__.return_value = restored
        restored.execute.return_value.fetchall.return_value = [(1, "checksum")]
        restored.execute.return_value.fetchone.return_value = (2,)
        return expected, admin, restored, [first, second]

    def test_restore_uses_only_new_disposable_database_and_removes_it(self):
        expected, admin, restored, connections = self.restore_fixture()
        seen = []

        def child(args, *, env=None):
            self.assertNotIn("CANARY-PASSWORD", str(args))
            self.assertIn("--exit-on-error", args)
            public_dsn = args[args.index("--dbname") + 1]
            self.assertIn("dbname=ameesh_verify_", public_dsn)
            self.assertNotIn("dbname=db ", public_dsn)
            self.assertTrue(Path(env["PGPASSFILE"]).exists())
            seen.append(Path(env["PGPASSFILE"]))
        with mock.patch("psycopg.connect", side_effect=connections), mock.patch.object(backup, "run", side_effect=child):
            proof = backup.verify_restore(self.config, self.output, expected)
        self.assertTrue(proof["restore_verified"])
        self.assertTrue(proof["disposable_database_removed"])
        self.assertEqual(admin.execute.call_count, 2)
        for call in admin.execute.call_args_list:
            self.assertIn("ameesh_verify_", repr(call.args[0]))
        self.assertFalse(seen[0].exists())

    def test_restore_failure_or_inventory_mismatch_cleans_up_without_proof(self):
        for failure in ("restore", "rows", "migrations"):
            expected, admin, restored, connections = self.restore_fixture()
            if failure == "rows":
                restored.execute.return_value.fetchone.return_value = (99,)
            if failure == "migrations":
                restored.execute.return_value.fetchall.return_value = [(2, "other")]
            with mock.patch("psycopg.connect", side_effect=connections), \
                    mock.patch.object(backup, "run", side_effect=backup.BackupError("restore failed") if failure == "restore" else None):
                with self.assertRaises(backup.BackupError):
                    backup.verify_restore(self.config, self.output, expected)
            self.assertEqual(admin.execute.call_count, 2)
            self.assertIn("DROP DATABASE", repr(admin.execute.call_args.args[0]))

    def test_restore_refuses_wrong_digest_without_opening_database(self):
        expected, _, _, _ = self.restore_fixture()
        expected["artifact_sha256"] = "wrong"
        with mock.patch("psycopg.connect") as connect, self.assertRaises(backup.BackupError):
            backup.verify_restore(self.config, self.output, expected)
        connect.assert_not_called()
