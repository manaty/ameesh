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
import secrets
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
        self.admin.name = "psycopg"

    def _purge(self, **kwargs):
        with mock.patch.object(db_mod, "connect",
                               return_value=self.admin) as connect:
            result = bench_mod.purge_invalid_databases(self.db, **kwargs)
        return result, connect

    def test_only_throwaway_database_names_are_dropped(self):
        result, connect = self._purge()
        # Le DROP réparateur ne doit pas hériter du délai de la suite : c'est
        # ce délai qui a annulé le premier DROP et laissé l'état invalide.
        # Le zéro est posé dans la session, pas seulement à la connexion.
        self.assertEqual(connect.call_args.args[0].statement_timeout_ms, 0)
        self.assertEqual(result.dropped, (
            "agent_mesh_test_1", "t_executor_db_abc",
            "ameesh_backup_test_abc", "ameesh_verify_abc"))
        self.assertEqual(result.kept, (("prod_live", "not a test database"),))
        expected = []
        for name in ("agent_mesh_test_1", "t_executor_db_abc",
                     "ameesh_backup_test_abc", "ameesh_verify_abc"):
            expected += ["SET statement_timeout = 0",
                         'DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name]
        self.assertEqual([call.args[0] for call in self.admin.execute.call_args_list],
                         expected)
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
                db_mod.execute_without_statement_timeout(
                    admin, "DROP DATABASE IF EXISTS %s WITH (FORCE)"
                    % db_mod.quote_ident(database))
            finally:
                admin.close()
        except Exception:  # noqa: BLE001 — cleanup : ne masque pas l'échec d'origine
            pass

    def _create(self, database: str) -> None:
        admin = self._admin()
        try:
            db_mod.execute_without_statement_timeout(
                admin, "CREATE DATABASE %s TEMPLATE template0"
                % db_mod.quote_ident(database))
        finally:
            admin.close()

    def _fill(self, database: str) -> None:
        """Un peu de données : un checkpoint de DROP dépasse 1 ms."""
        source = db_mod.connect(dataclasses.replace(
            self.cfg, dsn=self._database_dsn(database), schema="public"))
        try:
            source.execute("CREATE TABLE big AS SELECT generate_series(1, 200000) AS n")
        finally:
            source.close()

    def _drop_role(self, role: str) -> None:
        try:
            self.db.execute("DROP OWNED BY %s" % db_mod.quote_ident(role))
        except Exception:  # noqa: BLE001 — cleanup
            pass
        try:
            self.db.execute("DROP ROLE IF EXISTS %s" % db_mod.quote_ident(role))
        except Exception:  # noqa: BLE001 — cleanup
            pass

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

    @staticmethod
    def _role_dsn(role: str, password: str, database: str) -> str:
        parts = urllib.parse.urlsplit(TEST_DSN)
        if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
            raise unittest.SkipTest(
                "AMEESH_TEST_DSN n'est pas une URL : témoin sauté")
        host = parts.hostname + (":%d" % parts.port if parts.port else "")
        netloc = "%s:%s@%s" % (urllib.parse.quote(role),
                               urllib.parse.quote(password), host)
        return urllib.parse.urlunsplit(
            (parts.scheme, netloc, "/" + database, parts.query, ""))

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

    def test_explicit_zero_overrides_a_role_and_database_timeout(self):
        """Un délai hérité du rôle et de la base ne borne pas le DROP.

        `statement_timeout_ms=0` à la connexion ne neutralise pas ces délais ;
        le zéro doit être posé dans la session qui exécute le DROP.
        """
        suffix = uuid.uuid4().hex[:8]
        role = "t_timeout_" + suffix
        password = secrets.token_hex(16)
        witness = "t_executor_db_" + suffix
        admin_db = "t_admin_" + suffix
        self.addCleanup(self._force_drop, witness)
        self.addCleanup(self._force_drop, admin_db)
        self.addCleanup(self._drop_role, role)
        self._create(witness)
        self._create(admin_db)
        self._fill(witness)
        self.db.execute("CREATE ROLE %s LOGIN PASSWORD '%s'"
                        % (db_mod.quote_ident(role), password))
        self.db.execute("ALTER ROLE %s SET statement_timeout = '20ms'"
                        % db_mod.quote_ident(role))
        self.db.execute("ALTER DATABASE %s OWNER TO %s" % (
            db_mod.quote_ident(witness), db_mod.quote_ident(role)))
        self.db.execute("ALTER DATABASE %s SET statement_timeout = '20ms'"
                        % db_mod.quote_ident(admin_db))
        cfg = dataclasses.replace(
            self.cfg,
            dsn=self._role_dsn(role, password, admin_db),
            schema="public", statement_timeout_ms=0)
        admin = db_mod.connect(cfg)
        try:
            setting = admin.query(
                "SELECT current_setting('statement_timeout') AS statement_timeout"
            )[0]["statement_timeout"]
            self.assertEqual(setting, "20ms", "le délai hérité doit être réel")
            db_mod.execute_without_statement_timeout(
                admin, "DROP DATABASE %s" % db_mod.quote_ident(witness))
        finally:
            admin.close()
        self.assertIsNone(self._datconnlimit(witness))

    def test_explicit_zero_overrides_the_dsn_options_timeout(self):
        """Le paramètre `options=` du DSN ne borne pas le DROP réparateur."""
        database = "t_executor_db_" + uuid.uuid4().hex[:8]
        self.addCleanup(self._force_drop, database)
        self._create(database)
        self._fill(database)
        parts = urllib.parse.urlsplit(TEST_DSN)
        if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
            self.skipTest("AMEESH_TEST_DSN n'est pas une URL : témoin sauté")
        option = urllib.parse.quote("-c statement_timeout=20")
        query = ("%s&options=%s" % (parts.query, option)
                 if parts.query else "options=" + option)
        dsn = urllib.parse.urlunsplit(parts._replace(query=query))
        cfg = dataclasses.replace(self.cfg, dsn=dsn,
                                  statement_timeout_ms=0)
        admin = db_mod.connect(cfg)
        try:
            setting = admin.query(
                "SELECT current_setting('statement_timeout') AS statement_timeout"
            )[0]["statement_timeout"]
            self.assertEqual(setting, "20ms", "le délai du DSN doit être réel")
            db_mod.execute_without_statement_timeout(
                admin, "DROP DATABASE %s" % db_mod.quote_ident(database))
        finally:
            admin.close()
        self.assertIsNone(self._datconnlimit(database))
