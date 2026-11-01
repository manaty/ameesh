# SPDX-License-Identifier: AGPL-3.0-only
"""#5189 : garde déterministe des majors PostgreSQL avant dump et restauration.

Incident CI #5162 : pg_restore 17 contre un serveur 16. Le helper L232b lit les
majors des serveurs source et cible, ceux du pg_dump et du pg_restore que le
même PATH exécute, et ceux écrits dans l'en-tête de l'archive ; il refuse avant
de commencer, avec la raison et les versions, jamais un DSN.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "deploy" / "mise-a-jour" / "sauvegarder-prive.py"
SPEC = importlib.util.spec_from_file_location("sauvegarder_prive_5189", PATH)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None


def header(source="17.11", dumped="17.2", version=(1, 16, 0), int_size=4):
    """En-tête custom (ReadHead, pg_backup_archiver.c) suivi d'un corps factice."""
    def write_int(value):
        return bytes([1 if value < 0 else 0]) + abs(value).to_bytes(int_size, "little")

    def write_str(text):
        if text is None:
            return write_int(-1)
        data = text.encode()
        return write_int(len(data)) + data

    data = b"PGDMP" + bytes(version) + bytes([int_size, 8, 1])
    data += b"\x00" if version >= (1, 15, 0) else write_int(-1)
    data += b"".join(write_int(value) for value in (5, 4, 3, 2, 1, 126, 0))
    return data + write_str("ameesh") + write_str(source) + write_str(dumped) + b"\x00" * 64


class RuleTest(unittest.TestCase):
    """Les cinq témoins du lot, sur la règle pure."""

    def refused(self, reason, **majors):
        with self.assertRaises(backup.BackupError) as error:
            backup.require_compatible(**majors)
        self.assertIn(reason, str(error.exception))
        for value in majors.values():
            self.assertIn(str(value), str(error.exception))
        return str(error.exception)

    def test_archive_17_into_server_16_is_refused(self):
        self.refused("archive newer than the target server", source=17, dump=17, restore=17, target=16)

    def test_client_16_against_source_17_is_refused(self):
        self.refused("pg_dump older than the source server", source=17, dump=16, restore=16)

    def test_archive_17_with_restore_16_is_refused(self):
        self.refused("pg_restore older than the archive", source=17, dump=17, restore=16, target=17)

    def test_restore_17_into_target_16_is_refused(self):
        self.refused("pg_restore newer than the target server", source=16, dump=16, restore=17, target=16)

    def test_chain_17_is_compliant(self):
        backup.require_compatible(source=17, dump=17, restore=17, target=17)
        # un dump plus récent que sa source, restauré par un client égal à la cible
        backup.require_compatible(source=16, dump=17, restore=17, target=17)

    def test_unknown_majors_are_not_compared(self):
        backup.require_compatible(dump=17, restore=17)


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-5189-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, data):
        path = self.root / "archive.dump"
        path.write_bytes(data)
        return path

    def test_header_majors_of_formats_1_14_and_1_16(self):
        self.assertEqual(backup.archive_majors(self.archive(header("17.11", "17.2"))), (17, 17))
        self.assertEqual(backup.archive_majors(self.archive(
            header("15.8", "15.8", version=(1, 14, 0), int_size=8))), (15, 15))
        self.assertEqual(backup.archive_majors(self.archive(header("16.4", "17.0"))), (16, 17))

    def test_unreadable_header_refuses(self):
        for data in (b"", b"not a dump", header()[:40], header(dumped=None),
                     header(version=(1, 9, 0)), b"PGDMP" + bytes((1, 16, 0, 0, 8, 1))):
            with self.subTest(data=data[:12]), self.assertRaises(backup.BackupError):
                backup.archive_majors(self.archive(data))

    def fake(self, name, output, status=0):
        fakebin = self.root / "bin"
        fakebin.mkdir(exist_ok=True)
        path = fakebin / name
        path.write_text("#!/bin/sh\nprintf '%%s\\n' %r\nexit %d\n" % (output, status))
        path.chmod(0o755)
        return {"PATH": str(fakebin)}

    def test_client_major_runs_the_binary_of_the_given_path(self):
        env = self.fake("pg_restore", "pg_restore (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)")
        self.assertEqual(backup.client_major("pg_restore", env), 16)
        env = self.fake("pg_dump", "pg_dump (PostgreSQL) 17.11")
        self.assertEqual(backup.client_major("pg_dump", env), 17)

    def test_unreadable_or_missing_client_refuses(self):
        for env in (self.fake("pg_dump", "pg_dump 17"), self.fake("pg_dump", "pg_dump (PostgreSQL) 17", 1),
                    {"PATH": str(self.root / "nowhere")}):
            with self.subTest(env=env), self.assertRaises(backup.BackupError):
                backup.client_major("pg_dump", env)

    def test_server_major(self):
        connection = mock.MagicMock()
        connection.info.server_version = 170011
        self.assertEqual(backup.server_major(connection), 17)
        for value in (90624, None, True, "170011"):
            connection.info.server_version = value
            with self.subTest(value=value), self.assertRaises(backup.BackupError):
                backup.server_major(connection)


@unittest.skipUnless(HAVE_PSYCOPG, "psycopg absent (part psql) : canal privé non couvert")
class RefusedBeforeStartTest(unittest.TestCase):
    """Le refus précède tout geste : ni pg_dump, ni base jetable, ni listage."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-5189-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"dsn": "postgresql://acme:CANARY-PASSWORD@db.invalid/db"}))

    def clients(self, **majors):
        return mock.patch.object(backup, "client_major", side_effect=lambda name, env=None: majors[name])

    def test_dump_refused_before_pg_dump(self):
        with mock.patch.object(backup, "source_server_major", return_value=17), \
                self.clients(pg_dump=16, pg_restore=16), mock.patch.object(backup, "run") as child, \
                self.assertRaises(backup.BackupError) as error:
            backup.dump(self.config, self.root / "out/archive.dump")
        child.assert_not_called()
        self.assertIn("pg_dump older than the source server", str(error.exception))
        self.assertNotIn("CANARY", str(error.exception))
        self.assertNotIn("db.invalid", str(error.exception))
        self.assertFalse((self.root / "out/archive.dump").exists())

    def test_dump_refused_when_its_own_listing_would_fail(self):
        with mock.patch.object(backup, "source_server_major", return_value=17), \
                self.clients(pg_dump=17, pg_restore=16), mock.patch.object(backup, "run") as child, \
                self.assertRaisesRegex(backup.BackupError, "pg_restore older than the archive"):
            backup.dump(self.config, self.root / "out/archive.dump")
        child.assert_not_called()

    def test_unreachable_source_names_the_step_only(self):
        with self.assertRaises(backup.BackupError) as error:
            backup.source_server_major({"host": "db.invalid", "dbname": "db", "user": "acme",
                                        "password": "CANARY-PASSWORD", "connect_timeout": "1"})
        self.assertEqual(str(error.exception), "source server unreachable for the version check")

    def test_verify_restore_refused_before_any_database(self):
        archive = self.root / "archive.dump"
        admin = mock.MagicMock()
        context = mock.MagicMock()
        context.__enter__.return_value = admin
        for dumped, target, restore, reason in (
                ("17.2", 160015, 17, "archive newer than the target server"),
                ("16.4", 160015, 17, "pg_restore newer than the target server"),
                ("17.2", 170011, 16, "pg_restore older than the archive")):
            archive.write_bytes(header(dumped, dumped))
            expected = {"schema": "public", "migrations": [["1", "x"]], "row_counts": {"t": 0},
                        "artifact_sha256": backup.digest(archive)}
            admin.reset_mock()
            admin.info.server_version = target
            with self.subTest(reason=reason), mock.patch("psycopg.connect", return_value=context), \
                    self.clients(pg_restore=restore), mock.patch.object(backup, "run") as child, \
                    self.assertRaises(backup.BackupError) as error:
                backup.verify_restore(self.config, archive, expected)
            self.assertIn(reason, str(error.exception))
            self.assertIn("target server %d" % (target // 10000), str(error.exception))
            admin.execute.assert_not_called()
            child.assert_not_called()
            self.assertEqual(list(self.root.glob(".verify-*")), [])

    def test_retrieve_refused_before_listing(self):
        archive_bytes = header("17.11", "17.2")
        sha = __import__("hashlib").sha256(archive_bytes).hexdigest()
        credentials = self.root / "restore.json"
        credentials.write_text(json.dumps({"access_key": "A", "secret_key": "S",
                                           "bucket": "bucket", "region": "fr-par"}))
        credentials.chmod(0o600)

        def child(args, env=None, timeout=None):
            if args[0] == "curl":
                Path(args[args.index("--output") + 1]).write_bytes(archive_bytes)
                return
            raise AssertionError("listing must not run: %r" % args[:2])

        output = self.root / "recovered/archive.dump"
        with self.clients(pg_restore=16), mock.patch.object(backup, "run", side_effect=child), \
                self.assertRaisesRegex(backup.BackupError, "pg_restore older than the archive"):
            backup.retrieve(credentials, "host", "archive.dump", sha, output)
        self.assertFalse(output.exists())


def real_pg_dump():
    for candidate in ("/usr/lib/postgresql/17/bin/pg_dump", shutil.which("pg_dump")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


@unittest.skipUnless(os.environ.get("AMEESH_TEST_DSN") and real_pg_dump(),
                     "AMEESH_TEST_DSN ou pg_dump absent : en-tête réel non couvert")
class RealArchiveTest(unittest.TestCase):
    def test_header_of_a_real_archive_names_server_and_client(self):
        binary = real_pg_dump()
        with tempfile.TemporaryDirectory(prefix="ameesh-5189-") as temp:
            archive = Path(temp) / "schema.dump"
            done = subprocess.run([binary, "-Fc", "--schema-only", "--dbname", os.environ["AMEESH_TEST_DSN"],
                                   "--file", str(archive)], capture_output=True, timeout=120)
            if done.returncode != 0:
                self.skipTest("pg_dump %s refuse ce serveur (non concluant)" % binary)
            source, dumped = backup.archive_majors(archive)
        self.assertEqual(dumped, backup.client_major(binary))
        if HAVE_PSYCOPG:
            import psycopg
            with psycopg.connect(os.environ["AMEESH_TEST_DSN"]) as connection:
                self.assertEqual(source, backup.server_major(connection))


if __name__ == "__main__":
    unittest.main()
