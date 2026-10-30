# SPDX-License-Identifier: AGPL-3.0-only
"""#4822 : bases de test invalides laissées par un DROP DATABASE annulé.

Un DROP DATABASE interrompu pendant son checkpoint laisse la base à
``datconnlimit = -2`` (DATCONNLIMIT_INVALID_DB) : elle n'accepte plus de
connexion et l'autovacuum du banc s'arrête (#4680). La passe de démarrage de
la suite la retire ; un nom de base étranger reste gardé. Le témoin produit
réellement l'état invalide (annulation du backend après l'invalidation en
place) puis vérifie que la passe suivante ne laisse plus rien.
"""
from __future__ import annotations

import dataclasses
import shutil
import subprocess
import time
import unittest
import urllib.parse
import uuid
from unittest import mock

from ameesh import bench as bench_mod
from ameesh import config as config_mod
from ameesh import db as db_mod

from .support import TEST_DSN, PgTestCase


class InvalidDatabasePolicyTest(unittest.TestCase):
    """Sans serveur : la purge ne vise que les bases jetables connues."""

    def setUp(self):
        self.db = mock.MagicMock()
        self.db.cfg = config_mod.Config(
            dsn="postgresql://bench/agent_mesh", schema="public",
            backend="pg", statement_timeout_ms=30000)
        self.db.query.return_value = [
            {"datname": "agent_mesh_test_1"},
            {"datname": "t_executor_db_abc"},
            {"datname": "ameesh_backup_test_abc"},
            {"datname": "ameesh_verify_abc"},
            {"datname": "prod_live"},
        ]
        self.admin = mock.MagicMock()

    def _purge(self, **kwargs):
        with mock.patch.object(db_mod, "connect",
                               return_value=self.admin) as connect:
            result = bench_mod.purge_invalid_databases(self.db, **kwargs)
        return result, connect

    def test_only_throwaway_database_names_are_dropped(self):
        result, connect = self._purge()
        # Le DROP réparateur ne doit pas hériter du délai de la suite : c'est
        # ce délai qui a annulé le premier DROP et laissé l'état invalide.
        self.assertEqual(connect.call_args.args[0].statement_timeout_ms, 0)
        self.assertEqual(result.dropped, (
            "agent_mesh_test_1", "t_executor_db_abc",
            "ameesh_backup_test_abc", "ameesh_verify_abc"))
        self.assertEqual(result.kept, (("prod_live", "not a test database"),))
        self.assertEqual([call.args[0] for call in self.admin.execute.call_args_list], [
            'DROP DATABASE IF EXISTS "agent_mesh_test_1" WITH (FORCE)',
            'DROP DATABASE IF EXISTS "t_executor_db_abc" WITH (FORCE)',
            'DROP DATABASE IF EXISTS "ameesh_backup_test_abc" WITH (FORCE)',
            'DROP DATABASE IF EXISTS "ameesh_verify_abc" WITH (FORCE)',
        ])
        self.admin.close.assert_called_once()

    def test_only_scopes_the_pass(self):
        result, _ = self._purge(only={"t_executor_db_abc"})
        self.assertEqual(result.dropped, ("t_executor_db_abc",))
        self.assertIn(("agent_mesh_test_1", "outside only"), result.kept)


class CancelledDropPurgeTest(PgTestCase):
    """Le vrai état invalide, produit par un DROP annulé, part à la passe."""

    def _admin(self):
        return db_mod.connect(dataclasses.replace(
            self.cfg, statement_timeout_ms=0))

    def _force_drop(self, database: str) -> None:
        try:
            admin = self._admin()
            try:
                admin.execute("DROP DATABASE IF EXISTS %s WITH (FORCE)"
                              % db_mod.quote_ident(database))
            finally:
                admin.close()
        except Exception:  # noqa: BLE001 — cleanup : ne masque pas l'échec d'origine
            pass

    def _create(self, database: str) -> None:
        admin = self._admin()
        try:
            admin.execute("CREATE DATABASE %s TEMPLATE template0"
                          % db_mod.quote_ident(database))
        finally:
            admin.close()

    def _datconnlimit(self, database: str):
        rows = self.db.query(
            "SELECT datconnlimit FROM pg_database WHERE datname = %s",
            (database,))
        return rows[0]["datconnlimit"] if rows else None

    @staticmethod
    def _database_dsn(database: str) -> str:
        parts = urllib.parse.urlsplit(TEST_DSN)
        if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
            raise unittest.SkipTest(
                "AMEESH_TEST_DSN n'est pas une URL : témoin sauté")
        return urllib.parse.urlunsplit(parts._replace(path="/" + database))

    def test_cancelled_drop_is_purged_at_the_next_pass(self):
        if not shutil.which("psql"):
            self.skipTest("psql absent")
        database = "t_executor_db_" + uuid.uuid4().hex[:8]
        self.addCleanup(self._force_drop, database)
        self._create(database)
        source = db_mod.connect(dataclasses.replace(
            self.cfg, dsn=self._database_dsn(database), schema="public"))
        try:
            # Un checkpoint plus long que la boucle de lecture : l'invalidation
            # en place (avant le checkpoint) est visible avant la fin du DROP.
            source.execute("CREATE TABLE big AS SELECT generate_series(1, 1000000) AS n")
        finally:
            source.close()
        proc = subprocess.Popen(
            ["psql", "-w", "-X", "-q", "-v", "ON_ERROR_STOP=1", TEST_DSN,
             "-c", "DROP DATABASE %s" % db_mod.quote_ident(database)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self._datconnlimit(database) == -2:
                    rows = self.db.query(
                        "SELECT pid FROM pg_stat_activity"
                        " WHERE datname = current_database()"
                        " AND pid <> pg_backend_pid()"
                        " AND position(%s in query) > 0",
                        (database,))
                    if rows:
                        self.db.execute("SELECT pg_cancel_backend(%s)",
                                        (rows[0]["pid"],))
                        break
                if proc.poll() is not None:
                    break
                time.sleep(0.005)
            out, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if self._datconnlimit(database) != -2:
            self.skipTest("DROP terminé ou annulé avant l'invalidation : "
                          "l'état invalide n'a pas été observé")
        self.assertNotEqual(proc.returncode, 0, out + err)
        self.assertIn("canceling statement", err)
        # La passe que `_bench_upkeep` exécute au démarrage de la suite.
        bench_mod.purge_invalid_databases(self.db)
        self.assertIsNone(self._datconnlimit(database))
