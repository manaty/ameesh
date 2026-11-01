# SPDX-License-Identifier: AGPL-3.0-only
"""Real archive/restore proof on newly created databases of the test bench."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock

try:  # la restauration réelle parle psycopg ; la part psql de la CI n'en a pas
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
except ImportError:  # pragma: no cover - dépend de la part de CI
    psycopg = None

from tests.support import TEST_DSN, REQUIRE_DB
from tests.test_l232b_private_backup import backup


@unittest.skipUnless(psycopg is not None,
                     "psycopg absent (part psql) : restauration réelle non couverte")
class BackupRestoreTest(unittest.TestCase):
    def setUp(self):
        if not all(shutil.which(tool) for tool in ("pg_dump", "pg_restore")):
            self.skipTest("PostgreSQL backup clients absent")
        self.parameters = conninfo_to_dict(TEST_DSN)
        try:
            self.admin = psycopg.connect(TEST_DSN, autocommit=True)
        except psycopg.Error:
            if REQUIRE_DB:
                raise
            self.skipTest("test database absent")
        self.addCleanup(self.admin.close)
        # #4822 : un CREATE/DROP DATABASE déclenche un checkpoint ; sans ce
        # réglage, le statement_timeout du rôle pourrait l'annuler et laisser
        # une base invalide (datconnlimit = -2), que la suite purge ensuite.
        self.admin.execute("SET statement_timeout = 0")
        server_major = int(self.admin.execute("SHOW server_version_num").fetchone()[0]) // 10000
        client_major = int(subprocess.check_output(["pg_dump", "--version"], text=True).split()[2].split(".")[0])
        if client_major < server_major:
            self.skipTest("pg_dump must support the test server's PostgreSQL version")
        self.database = "ameesh_backup_test_" + uuid.uuid4().hex
        # Le cleanup est posé avant la création : une création annulée peut
        # laisser une base invalide que ce DROP FORCE retire.
        self.addCleanup(lambda: self.admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(self.database))))
        self.admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(self.database)))
        self.source_dsn = make_conninfo(**dict(self.parameters, dbname=self.database))
        with psycopg.connect(self.source_dsn) as source:
            source.execute("CREATE SCHEMA mesh_fixture")
            source.execute("CREATE TABLE mesh_fixture.schema_migrations (version integer, checksum text)")
            source.execute("INSERT INTO mesh_fixture.schema_migrations VALUES (1, 'fixture-checksum')")
            source.execute("CREATE TABLE mesh_fixture.agent_registry (name text)")
            source.execute("INSERT INTO mesh_fixture.agent_registry VALUES ('before-snapshot')")
            source.execute("CREATE TABLE mesh_fixture.agent_mailbox (body text)")
            source.execute("INSERT INTO mesh_fixture.agent_mailbox VALUES ('fixture-mail')")
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-backup-restore-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_config = self.root / "source.json"
        self.restore_config = self.root / "restore.json"
        for path, dsn in ((self.source_config, self.source_dsn), (self.restore_config, TEST_DSN)):
            path.write_text(json.dumps({"dsn": dsn}))
            path.chmod(0o600)
        self.archive, self.inventory = self.root / "archive.dump", self.root / "inventory.json"

    def snapshot(self):
        backup.snapshot_dump(self.source_config, self.archive, self.inventory, schema="mesh_fixture")
        return json.loads(self.inventory.read_text())

    def restore(self, expected):
        connect = psycopg.connect
        databases = []

        def record(dsn, **kwargs):
            name = conninfo_to_dict(dsn).get("dbname", "")
            if name.startswith("ameesh_verify_"):
                databases.append(name)
            return connect(dsn, **kwargs)
        try:
            with mock.patch("psycopg.connect", side_effect=record):
                return backup.verify_restore(self.restore_config, self.archive, expected)
        finally:
            for name in databases:
                self.assertIsNone(self.admin.execute(
                    "SELECT datname FROM pg_database WHERE datname=%s", (name,)).fetchone())
            with connect(self.source_dsn) as source:
                self.assertEqual(source.execute("SELECT body FROM mesh_fixture.agent_mailbox").fetchall(),
                                 [("fixture-mail",)])

    def test_exported_snapshot_restores_despite_concurrent_source_writes(self):
        dump = backup.dump_parameters

        def concurrent_write(parameters, output, **options):
            with psycopg.connect(self.source_dsn) as source:
                source.execute("INSERT INTO mesh_fixture.agent_registry VALUES ('after-snapshot')")
            dump(parameters, output, **options)
        with mock.patch.object(backup, "dump_parameters", side_effect=concurrent_write):
            expected = self.snapshot()
        self.assertEqual(expected["row_counts"], {"agent_registry": 1, "agent_mailbox": 1})
        with psycopg.connect(self.source_dsn) as source:
            self.assertEqual(source.execute("SELECT count(*) FROM mesh_fixture.agent_registry").fetchone(), (2,))
        self.assertTrue(self.restore(expected)["restore_verified"])

    def test_real_restore_rejects_wrong_inventory_and_removes_database(self):
        expected = self.snapshot()
        expected["row_counts"]["agent_registry"] = 999
        with self.assertRaisesRegex(backup.BackupError, "row inventory mismatch"):
            self.restore(expected)

    def test_failed_real_restore_removes_disposable_database(self):
        expected = self.snapshot()
        # #5189 : the header (and its majors) stays readable, so the check
        # passes and pg_restore itself fails on the truncated table of contents
        self.archive.write_bytes(self.archive.read_bytes()[:256] + b"not a PostgreSQL archive")
        expected["artifact_sha256"] = backup.digest(self.archive)
        recovery_id = uuid.uuid4()
        with mock.patch.object(backup.uuid, "uuid4", return_value=recovery_id):
            with self.assertRaisesRegex(backup.BackupError, "pg_restore exited"):
                backup.verify_restore(self.restore_config, self.archive, expected)
        self.assertIsNone(self.admin.execute("SELECT datname FROM pg_database WHERE datname=%s",
                                             ("ameesh_verify_" + recovery_id.hex,)).fetchone())
        # an unreadable header is refused before any database is created
        self.archive.write_bytes(b"not a PostgreSQL archive")
        expected["artifact_sha256"] = backup.digest(self.archive)
        with mock.patch.object(backup.uuid, "uuid4", return_value=recovery_id):
            with self.assertRaisesRegex(backup.BackupError, "archive header unreadable"):
                backup.verify_restore(self.restore_config, self.archive, expected)
        self.assertIsNone(self.admin.execute("SELECT datname FROM pg_database WHERE datname=%s",
                                             ("ameesh_verify_" + recovery_id.hex,)).fetchone())
        with psycopg.connect(self.source_dsn) as source:
            self.assertEqual(source.execute("SELECT name FROM mesh_fixture.agent_registry").fetchall(),
                             [("before-snapshot",)])
