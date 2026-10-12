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

    def recovery_fixture(self):
        credentials = self.root / "recovery.json"
        credentials.write_text(json.dumps({"access_key": "RECOVERY-ACCESS", "secret_key": "RECOVERY-SECRET",
                                           "bucket": "acme", "region": "fr-par"}))
        credentials.chmod(0o600)
        archive = self.root / "source.dump"
        archive.write_bytes(b"archive")
        return credentials, backup.digest(archive)

    def test_retrieval_uses_separate_private_identity_and_pinned_origin(self):
        credentials, sha = self.recovery_fixture()
        seen = []

        def child(args, *, env=None):
            self.assertFalse(self.output.exists())
            self.assertNotIn("RECOVERY-SECRET", str(args))
            self.assertNotIn("SCW_SECRET_KEY", env)
            self.assertNotIn("SCW_ACCESS_KEY", env)
            if args[0] == "curl":
                self.assertEqual(args[:2], ["curl", "--disable"])
                self.assertNotIn("--location", args)
                self.assertEqual(args[-1], "https://acme.s3.fr-par.scw.cloud/host-1/remote.dump")
                config = Path(args[3])
                self.assertIn("RECOVERY-SECRET", config.read_text())
                self.assertNotIn("UPLOAD-SECRET", config.read_text())
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
                seen.append(config)
                Path(args[args.index("--output") + 1]).write_bytes(b"archive")
            else:
                self.assertEqual(args[:2], ["pg_restore", "--list"])
        with mock.patch.dict(os.environ, {"SCW_ACCESS_KEY": "UPLOAD-ACCESS", "SCW_SECRET_KEY": "UPLOAD-SECRET"}), \
                mock.patch.object(backup, "run", side_effect=child):
            proof = backup.retrieve(credentials, "host-1", "remote.dump", sha, self.output)
        self.assertEqual(proof, {"artifact_sha256": sha, "retrieved": True})
        self.assertEqual(self.output.read_bytes(), b"archive")
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertFalse(seen[0].exists())

    def test_failed_retrieval_never_publishes_an_artifact(self):
        credentials, sha = self.recovery_fixture()
        for failure in ("download", "digest", "catalogue", "empty"):
            def child(args, *, env=None):
                if args[0] == "curl":
                    Path(args[args.index("--output") + 1]).write_bytes(
                        b"wrong" if failure == "digest" else b"" if failure == "empty" else b"archive")
                if (args[0] == "curl" and failure == "download"
                        or args[0] == "pg_restore" and failure == "catalogue"):
                    raise backup.BackupError("failed")
            with self.subTest(failure=failure), mock.patch.object(backup, "run", side_effect=child):
                with self.assertRaises(backup.BackupError):
                    backup.retrieve(credentials, "host-1", "remote.dump", sha, self.output)
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_retrieval_refuses_replacement_even_of_a_dangling_link(self):
        credentials, sha = self.recovery_fixture()
        self.output.parent.mkdir()
        self.output.symlink_to(self.root / "absent")
        with mock.patch.object(backup, "run") as child, self.assertRaises(backup.BackupError):
            backup.retrieve(credentials, "host-1", "remote.dump", sha, self.output)
        child.assert_not_called()
        self.assertTrue(self.output.is_symlink())

    def test_retrieval_refuses_public_credentials_or_unpinned_locations(self):
        credentials, sha = self.recovery_fixture()
        cases = [("host-1", "../remote.dump", sha), ("host-1", "remote.dump", ""),
                 ("bad/host", "remote.dump", sha)]
        with mock.patch.object(backup, "run") as child:
            for host, name, digest in cases:
                with self.subTest(host=host, name=name), self.assertRaises(backup.BackupError):
                    backup.retrieve(credentials, host, name, digest, self.output)
            credentials.chmod(0o644)
            with self.assertRaises(backup.BackupError):
                backup.retrieve(credentials, "host-1", "remote.dump", sha, self.output)
        child.assert_not_called()

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

    def snapshot_fixture(self, *, versions=((1, "checksum"),)):
        source, context = mock.MagicMock(), mock.MagicMock()
        context.__enter__.return_value = source
        cursors = [mock.MagicMock() for _ in range(5)]
        cursors[1].fetchone.return_value = ("00000003-000000AE-1",)
        cursors[2].fetchall.return_value = list(versions)
        cursors[3].fetchone.return_value = (5,)
        cursors[4].fetchone.return_value = (2,)
        source.execute.side_effect = cursors
        return source, context, cursors

    def test_inventory_and_dump_share_live_exported_snapshot(self):
        source, context, cursors = self.snapshot_fixture()
        inventory = self.output.with_suffix(".inventory.json")
        def child(args, *, env=None):
            context.__exit__.assert_not_called()
            self.assertNotIn("CANARY-PASSWORD", str(args))
            if args[0] == "pg_dump":
                self.assertEqual(args[args.index("--snapshot") + 1], "00000003-000000AE-1")
                # Subsequent source changes cannot change the expected snapshot
                # counts already captured by this transaction.
                cursors[4].fetchone.return_value = (999,)
                Path(args[-1]).write_bytes(b"snapshot archive")
        with mock.patch("psycopg.connect", return_value=context), mock.patch.object(backup, "run", side_effect=child):
            proof = backup.snapshot_dump(self.config, self.output, inventory)
        expected = json.loads(inventory.read_text())
        self.assertEqual(expected["row_counts"], {"agent_mailbox": 5, "agent_registry": 2})
        self.assertEqual(expected["migrations"], [[1, "checksum"]])
        self.assertEqual(expected["artifact_sha256"], backup.digest(self.output))
        self.assertEqual(proof["artifact_sha256"], expected["artifact_sha256"])
        self.assertEqual(stat.S_IMODE(inventory.stat().st_mode), 0o600)
        self.assertNotIn("CANARY", inventory.read_text())
        context.__exit__.assert_called_once()

    def test_failed_snapshot_dump_never_publishes_expected_inventory(self):
        _, context, _ = self.snapshot_fixture()
        inventory = self.output.with_suffix(".inventory.json")
        with mock.patch("psycopg.connect", return_value=context), \
                mock.patch.object(backup, "run", side_effect=backup.BackupError("dump failed")):
            with self.assertRaises(backup.BackupError):
                backup.snapshot_dump(self.config, self.output, inventory)
        self.assertFalse(inventory.exists())
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.iterdir()), [])
        context.__exit__.assert_called_once()

    def test_empty_source_migrations_block_snapshot_before_dump(self):
        _, context, _ = self.snapshot_fixture(versions=())
        inventory = self.output.with_suffix(".inventory.json")
        with mock.patch("psycopg.connect", return_value=context), mock.patch.object(backup, "run") as child:
            with self.assertRaises(backup.BackupError):
                backup.snapshot_dump(self.config, self.output, inventory)
        child.assert_not_called()
        self.assertFalse(inventory.exists())

    def test_existing_inventory_is_retained_without_source_connection(self):
        self.output.parent.mkdir()
        inventory = self.output.with_suffix(".inventory.json")
        inventory.write_text("old inventory")
        with mock.patch("psycopg.connect") as connect, self.assertRaises(backup.BackupError):
            backup.snapshot_dump(self.config, self.output, inventory)
        connect.assert_not_called()
        self.assertEqual(inventory.read_text(), "old inventory")
