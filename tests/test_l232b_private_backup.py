# SPDX-License-Identifier: AGPL-3.0-only
"""Canary credentials stay private; failed dumps never get their final name."""
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


# Le canal privé (L232b) passe par psycopg : la part psql de la CI n'en a pas
# (même règle que test_518_dsn_argv) ; ces témoins y sont sautés et couverts par
# la part psycopg.
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None

PATH = Path(__file__).resolve().parents[1] / "deploy/mise-a-jour/sauvegarder-prive.py"
SPEC = importlib.util.spec_from_file_location("private_backup", PATH)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


@unittest.skipUnless(HAVE_PSYCOPG, "psycopg absent (part psql) : canal privé non couvert")
class PrivateBackupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-private-backup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"dsn": "postgresql://acme:CANARY-PASSWORD@localhost/db"}))
        self.output = self.root / "backups/archive.dump"
        # #5189 : the archives and servers here are fakes; one coherent chain
        # of majors (17) keeps the version guard out of these contracts.
        for name, value in (("client_major", lambda name, env=None: 17),
                            ("server_major", lambda connection: 17),
                            ("source_server_major", lambda parameters: 17),
                            ("archive_majors", lambda path: (17, 17))):
            patcher = mock.patch.object(backup, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

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
            header = args[args.index("-H") + 1]
            self.assertEqual(header, "x-amz-content-sha256: " + backup.digest(self.output))
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

    def test_upload_receipt_pins_non_null_version_and_never_replaces(self):
        self.output.parent.mkdir(mode=0o700)
        self.output.write_bytes(b"archive")
        receipt = self.output.with_suffix(".upload.json")
        def child(args, *, env=None):
            self.assertEqual(env, {"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            self.assertNotIn("CANARY-SECRET", str(args))
            Path(args[args.index("--dump-header") + 1]).write_text(
                "HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 200 OK\r\nx-amz-version-id: pinned/+?version\r\n\r\n")
        settings = {"SCW_ACCESS_KEY": "CANARY-ACCESS", "SCW_SECRET_KEY": "CANARY-SECRET",
                    "SCW_BUCKET": "acme", "SCW_REGION": "fr-par", "TMPDIR": str(self.root)}
        with mock.patch.dict(os.environ, settings), mock.patch.object(backup, "run", side_effect=child) as call:
            value = backup.upload(self.output, "host-1", receipt=receipt)
            self.assertEqual(value["version_id"], "pinned/+?version")
            self.assertEqual(value["sha256"], backup.digest(self.output))
            self.assertEqual(json.loads(receipt.read_text()), value)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("CANARY-SECRET", receipt.read_text())
            with self.assertRaises(backup.BackupError):
                backup.upload(self.output, "host-1", receipt=receipt)
            self.assertEqual(call.call_count, 1)

    def test_upload_without_version_or_changed_archive_publishes_no_receipt(self):
        self.output.parent.mkdir(mode=0o700)
        self.output.write_bytes(b"archive")
        receipt = self.output.with_suffix(".upload.json")
        settings = {"SCW_ACCESS_KEY": "CANARY-ACCESS", "SCW_SECRET_KEY": "CANARY-SECRET",
                    "SCW_BUCKET": "acme", "SCW_REGION": "fr-par", "TMPDIR": str(self.root)}
        for header in ("", "x-amz-version-id: null", "x-amz-version-id: v1\r\nx-amz-version-id: v2"):
            def child(args, *, env=None):
                Path(args[args.index("--dump-header") + 1]).write_text("HTTP/1.1 200 OK\r\n" + header + "\r\n\r\n")
            with self.subTest(header=header), mock.patch.dict(os.environ, settings), mock.patch.object(backup, "run", side_effect=child):
                with self.assertRaises(backup.BackupError):
                    backup.upload(self.output, "host-1", receipt=receipt)
                self.assertFalse(receipt.exists())
        def changed(args, *, env=None):
            Path(args[args.index("--dump-header") + 1]).write_text("HTTP/1.1 200 OK\r\nx-amz-version-id: v1\r\n\r\n")
            self.output.write_bytes(b"concurrent change")
        with mock.patch.dict(os.environ, settings), mock.patch.object(backup, "run", side_effect=changed), self.assertRaisesRegex(backup.BackupError, "changed"):
            backup.upload(self.output, "host-1", receipt=receipt)
        self.assertFalse(receipt.exists())

    def test_retrieval_pins_and_attests_exact_version_even_with_same_digest(self):
        credentials, sha = self.recovery_fixture()
        version = "pinned/+?version"
        def child(args, *, env=None):
            if args[0] == "curl":
                self.assertTrue(args[-1].endswith("?versionId=pinned%2F%2B%3Fversion"))
                Path(args[args.index("--output") + 1]).write_bytes(b"archive")
                Path(args[args.index("--dump-header") + 1]).write_text("HTTP/1.1 200 OK\r\nx-amz-version-id: " + version + "\r\n\r\n")
        with mock.patch.object(backup, "run", side_effect=child):
            value = backup.retrieve(credentials, "host-1", "remote.dump", sha, self.output, version_id=version)
        self.assertEqual(value["version_id"], version)
        self.assertEqual(self.output.read_bytes(), b"archive")

    def test_retrieval_wrong_or_null_version_never_publishes(self):
        credentials, sha = self.recovery_fixture()
        def child(args, *, env=None):
            if args[0] == "curl":
                Path(args[args.index("--output") + 1]).write_bytes(b"archive")
                Path(args[args.index("--dump-header") + 1]).write_text("HTTP/1.1 200 OK\r\nx-amz-version-id: different\r\n\r\n")
        with mock.patch.object(backup, "run", side_effect=child) as call:
            with self.assertRaisesRegex(backup.BackupError, "version mismatch"):
                backup.retrieve(credentials, "host-1", "remote.dump", sha, self.output, version_id="pinned")
            self.assertFalse(self.output.exists())
            self.assertEqual(call.call_count, 1)
            for version in ("null", "", "bad\nversion"):
                with self.subTest(version=version), self.assertRaises(backup.BackupError):
                    backup.retrieve(credentials, "host-1", "remote.dump", sha, self.output, version_id=version)
            self.assertEqual(call.call_count, 1)

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

    def test_shared_parent_or_tmpdir_is_refused_not_renarrowed(self):
        # A pre-existing shared directory is not ours to narrow to 0700: dump,
        # retrieve and upload refuse it, without touching its mode.
        shared = self.root / "shared"
        shared.mkdir(mode=0o777)
        shared.chmod(0o1777)
        credentials, sha = self.recovery_fixture()
        settings = {"TMPDIR": str(shared), "SCW_ACCESS_KEY": "CANARY-ACCESS",
                    "SCW_SECRET_KEY": "CANARY-SECRET", "SCW_BUCKET": "acme", "SCW_REGION": "fr-par"}
        with mock.patch.dict(os.environ, settings), mock.patch.object(backup, "run") as child:
            with self.assertRaisesRegex(backup.BackupError, "private owned"):
                backup.dump(self.config, shared / "archive.dump")
            with self.assertRaisesRegex(backup.BackupError, "private owned"):
                backup.upload(self.output, "host-1")
            with self.assertRaisesRegex(backup.BackupError, "private owned"):
                backup.retrieve(credentials, "host-1", "remote.dump", sha, shared / "recovered.dump")
        child.assert_not_called()
        self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o1777)
        # Our own directory, widened by an installer, is tightened instead of
        # refused: the backup of the enrolled host must still run.
        owned = self.root / "owned"
        owned.mkdir(mode=0o755)
        def child(args, *, env=None):
            if args[0] == "pg_dump":
                Path(args[-1]).write_bytes(b"archive")
        with mock.patch.object(backup, "run", side_effect=child):
            backup.dump(self.config, owned / "archive.dump")
        self.assertEqual(stat.S_IMODE(owned.stat().st_mode), 0o700)
        self.assertTrue((owned / "archive.dump").is_file())

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
                self.assertEqual(args[args.index("-H") + 1],
                                 "x-amz-content-sha256: " + backup.EMPTY_SHA256)
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

    def test_killed_verification_is_reaped_before_the_next_restore(self):
        expected, admin, restored, connections = self.restore_fixture()
        leftover = "ameesh_verify_" + "a" * 32
        marker = self.output.parent / (".verify-" + "a" * 32 + ".json")
        marker.write_text(json.dumps({"database": leftover}))
        marker.chmod(0o600)
        with mock.patch("psycopg.connect", side_effect=connections), mock.patch.object(backup, "run"):
            proof = backup.verify_restore(self.config, self.output, expected)
        self.assertTrue(proof["restore_verified"])
        self.assertFalse(marker.exists())
        first = repr(admin.execute.call_args_list[0].args[0])
        self.assertIn("DROP DATABASE IF EXISTS", first)
        self.assertIn(leftover, first)
        # A marker naming anything but this module's disposable database is left
        # alone: a tampered document can never aim a DROP at a live database.
        forged = self.output.parent / (".verify-" + "b" * 32 + ".json")
        forged.write_text(json.dumps({"database": "agent_mesh"}))
        forged.chmod(0o600)
        expected, admin, restored, connections = self.restore_fixture()
        with mock.patch("psycopg.connect", side_effect=connections), mock.patch.object(backup, "run"):
            self.assertTrue(backup.verify_restore(self.config, self.output, expected)["restore_verified"])
        self.assertTrue(forged.exists())
        self.assertIn("CREATE DATABASE", repr(admin.execute.call_args_list[0].args[0]))
        self.assertNotIn("agent_mesh", repr([call.args[0] for call in admin.execute.call_args_list]))

    def test_restore_refuses_wrong_digest_without_opening_database(self):
        expected, _, _, _ = self.restore_fixture()
        expected["artifact_sha256"] = "wrong"
        with mock.patch("psycopg.connect") as connect, self.assertRaises(backup.BackupError):
            backup.verify_restore(self.config, self.output, expected)
        connect.assert_not_called()

    def snapshot_fixture(self, *, versions=((1, "checksum"),)):
        source, context = mock.MagicMock(), mock.MagicMock()
        context.__enter__.return_value = source
        cursors = [mock.MagicMock() for _ in range(7)]
        cursors[2].fetchone.return_value = ("00000003-000000AE-1",)
        cursors[3].fetchall.return_value = list(versions)
        cursors[4].fetchone.return_value = (5,)
        cursors[5].fetchone.return_value = (2,)
        cursors[6].fetchone.return_value = (4096,)
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
                cursors[5].fetchone.return_value = (999,)
                Path(args[-1]).write_bytes(b"snapshot archive")
        with mock.patch("psycopg.connect", return_value=context), mock.patch.object(backup, "run", side_effect=child):
            proof = backup.snapshot_dump(self.config, self.output, inventory)
        expected = json.loads(inventory.read_text())
        self.assertEqual(expected["row_counts"], {"agent_mailbox": 5, "agent_registry": 2})
        self.assertEqual(expected["migrations"], [[1, "checksum"]])
        self.assertEqual(expected["source_bytes"], 4096)
        self.assertEqual(expected["artifact_sha256"], backup.digest(self.output))
        self.assertEqual(proof["artifact_sha256"], expected["artifact_sha256"])
        self.assertEqual(stat.S_IMODE(inventory.stat().st_mode), 0o600)
        self.assertNotIn("CANARY", inventory.read_text())
        # #3836/#3844 : la garde idle-in-transaction ne doit pas tuer l'export
        # de snapshot pendant pg_dump ; l'exemption est locale à la transaction.
        self.assertEqual(source.execute.call_args_list[0].args[0],
                         "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        self.assertEqual(source.execute.call_args_list[1].args[0],
                         "SET LOCAL idle_in_transaction_session_timeout = 0")
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

    # --- #2261 : cause d'un échec de sauvegarde, sans secret --------------
    def test_run_reports_timeout_or_exit_status_without_diagnostic(self):
        with mock.patch.object(backup.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(["pg_dump"], 1800)):
            with self.assertRaises(backup.BackupError) as error:
                backup.run(["pg_dump"], timeout=1800)
        self.assertIn("pg_dump timed out after 1800", str(error.exception))
        with mock.patch.object(backup.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(
                                   7, ["pg_dump"], stderr=b"CANARY-PASSWORD")):
            with self.assertRaises(backup.BackupError) as error:
                backup.run(["pg_dump"])
        self.assertEqual(str(error.exception), "pg_dump exited with status 7")
        self.assertNotIn("CANARY", str(error.exception))

    def test_dump_timeout_cause_names_bytes_written(self):
        def child(args, *, env=None, timeout=1800):
            self.assertEqual(timeout, 1800)
            Path(args[-1]).write_bytes(b"p" * 123)
            raise backup.BackupError("pg_dump timed out after 1800 s")
        with mock.patch.object(backup, "run", side_effect=child):
            with self.assertRaises(backup.BackupError) as error:
                backup.dump(self.config, self.output)
        message = str(error.exception)
        self.assertIn("pg_dump timed out after 1800 s", message)
        self.assertIn("123 bytes written", message)
        self.assertNotIn("CANARY-PASSWORD", message)
        self.assertFalse(self.output.exists())

    def test_cli_prints_safe_cause_instead_of_masking_it(self):
        fakebin = self.root / "fakebin"
        fakebin.mkdir()
        fake_dump = fakebin / "pg_dump"
        fake_dump.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "pathlib.Path(args[args.index('--file') + 1]).write_bytes(b'p' * 42)\n"
            "raise SystemExit(3)\n")
        fake_dump.chmod(0o755)
        # #5189 : a child process escapes the patches of setUp; the launcher
        # pins the unreachable source server, the clients answer for themselves.
        (fakebin / "pg_restore").write_text("#!/bin/sh\necho 'pg_restore (PostgreSQL) 17.2'\n")
        (fakebin / "pg_restore").chmod(0o755)
        fake_dump.write_text(fake_dump.read_text().replace(
            "args = sys.argv[1:]\n",
            "args = sys.argv[1:]\n"
            "if args == ['--version']:\n"
            "    print('pg_dump (PostgreSQL) 17.2'); raise SystemExit(0)\n"))
        launcher = self.root / "launcher.py"
        launcher.write_text(
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('helper', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "module.source_server_major = lambda parameters: 17\n"
            "raise SystemExit(module.main())\n" % str(PATH))
        with mock.patch.dict(os.environ, {"PATH": str(fakebin) + os.pathsep + os.environ["PATH"]}):
            done = subprocess.run([sys.executable, str(launcher), "dump",
                                   "--config", str(self.config), "--output", str(self.output)],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("pg_dump exited with status 3", done.stderr)
        self.assertIn("42 bytes written", done.stderr)
        self.assertNotIn("CANARY-PASSWORD", done.stdout + done.stderr)
        self.assertFalse(self.output.exists())

    # --- #2261 : rapatriement compressé du dump serveur -------------------
    def test_pull_compressed_dump_is_private_verified_and_atomic(self):
        payload = b"server-side dump\n" * 1000
        digest = hashlib.sha256(payload).hexdigest()
        result = backup.pull(self.output, digest, expected_bytes=len(payload),
                             source=io.BytesIO(gzip.compress(payload)))
        self.assertEqual(self.output.read_bytes(), payload)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(result, {"artifact_sha256": digest, "bytes": len(payload)})

    def test_pull_wrong_digest_or_size_never_publishes(self):
        payload = b"server-side dump\n" * 1000
        digest = hashlib.sha256(payload).hexdigest()
        for expected_bytes, expected_digest, cause in (
                (len(payload), "0" * 64, "digest mismatch"),
                (len(payload) + 1, digest, "incomplete"),
                (len(payload) - 1, digest, "exceeds the attested size")):
            with self.subTest(cause=cause):
                with self.assertRaises(backup.BackupError) as error:
                    backup.pull(self.output, expected_digest, expected_bytes=expected_bytes,
                                source=io.BytesIO(gzip.compress(payload)))
                self.assertIn(cause, str(error.exception))
                self.assertFalse(self.output.exists())
                self.assertEqual(list(self.output.parent.iterdir()), [])

    def test_pull_existing_destination_is_never_replaced_or_read(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"old")
        class NeverRead:
            def read(self, size=-1):
                self.fail("the source was read despite the existing destination")
        with self.assertRaises(backup.BackupError):
            backup.pull(self.output, "0" * 64, source=NeverRead())
        self.assertEqual(self.output.read_bytes(), b"old")

    def test_pull_timeout_reports_bytes_written(self):
        class Slow(io.BytesIO):
            def read(self, size=-1):
                time.sleep(2)
                return super().read(size)
        payload = b"x" * 100
        with self.assertRaises(backup.BackupError) as error:
            backup.pull(self.output, hashlib.sha256(payload).hexdigest(),
                        expected_bytes=len(payload), timeout=1,
                        source=Slow(gzip.compress(payload)))
        message = str(error.exception)
        self.assertIn("timed out after 1", message)
        self.assertIn("0 bytes written", message)
        self.assertFalse(self.output.exists())

    def test_pull_os_error_does_not_echo_the_diagnostic(self):
        class Broken:
            def read(self, size=-1):
                raise OSError("CANARY-PASSWORD")
        with self.assertRaises(backup.BackupError) as error:
            backup.pull(self.output, "0" * 64, source=Broken())
        self.assertIn("dump retrieval failed", str(error.exception))
        self.assertNotIn("CANARY", str(error.exception))
        self.assertFalse(self.output.exists())
