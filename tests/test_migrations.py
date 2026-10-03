"""Migrations versionnées : application, idempotence, immuabilité."""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid

from agent_mesh import db as db_mod
from agent_mesh import migrations

from .support import PgTestCase


class MigrationsTest(PgTestCase):
    def test_schema_migre_versionne(self):
        rows = self.db.query(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        self.assertEqual([row["version"] for row in rows], [1, 2])
        self.assertEqual([row["name"] for row in rows], ["init", "overview"])
        for row in rows:
            self.assertEqual(len(row["checksum"]), 64)
        # la vue d'observabilité et les deux triggers de notification existent
        view = self.db.query("SELECT to_regclass('agent_mesh_overview') IS NOT NULL AS ok")[0]
        self.assertTrue(view["ok"])
        triggers = self.db.query(
            "SELECT t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE NOT t.tgisinternal AND n.nspname = current_schema() "
            "AND t.tgname IN ('agent_mailbox_notify','agent_registry_notify')"
        )
        self.assertEqual(len(triggers), 2)

    def test_remigration_idempotente(self):
        self.assertEqual(migrations.migrate(self.db), [])
        count = self.db.query("SELECT count(*)::int AS n FROM schema_migrations")[0]["n"]
        self.assertEqual(count, 2)

    def test_migration_modifiee_refusee(self):
        connue = migrations.discover()[0].checksum
        self.db.execute("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 1")
        try:
            with self.assertRaises(db_mod.DbError) as ctx:
                migrations.pending(self.db)
            self.assertIn("immuable", str(ctx.exception))
        finally:
            self.db.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE version = 1", (connue,)
            )

    def test_version_en_double_refusee(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in ("0007_a.sql", "0007_b.sql"):
                with open(os.path.join(directory, filename), "w", encoding="utf-8") as fh:
                    fh.write("select 1;\n")
            with self.assertRaises(db_mod.DbError) as ctx:
                migrations.discover(directory)
            self.assertIn("double", str(ctx.exception))

    def test_schema_neuf_cree_par_migrate(self):
        """Un schéma de test n'existe pas encore : migrate le crée et l'isole."""
        schema = "t_neuf_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        try:
            # `SET search_path` accepte un schéma absent : on vérifie que
            # migrate le crée au lieu d'échouer sur « no schema has been selected ».
            self.assertFalse(self.db.query(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %s) AS ok", (schema,)
            )[0]["ok"])
            done = migrations.migrate(db)
            self.assertEqual([m.version for m in done], [1, 2])
            tables = db.query(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN ('agent_registry','agent_mailbox',"
                "'schema_migrations')",
                (schema,),
            )[0]["n"]
            self.assertEqual(tables, 3)
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_schema_absent_message_actionnable(self):
        schema = "t_vide_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        try:
            db.execute('CREATE SCHEMA "%s"' % schema)
            with self.assertRaises(db_mod.SchemaMissing) as ctx:
                db_mod.require_schema(db)
            self.assertIn("migrate", str(ctx.exception))
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)


if __name__ == "__main__":
    unittest.main()
