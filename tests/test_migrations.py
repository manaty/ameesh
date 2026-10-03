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
        self.assertEqual([row["version"] for row in rows], [1, 2, 3, 4])
        self.assertEqual([row["name"] for row in rows],
                         ["init", "overview", "authority", "work_items"])
        for row in rows:
            self.assertEqual(len(row["checksum"]), 64)
        # la vue d'observabilité et les deux triggers de notification existent
        for relation in ("agent_mesh_overview", "mesh_approvals_status"):
            with self.subTest(relation=relation):
                found = self.db.query(
                    "SELECT to_regclass(%s) IS NOT NULL AS ok", (relation,))[0]
                self.assertTrue(found["ok"])
        # les colonnes d'autorité et de signature sont bien là
        colonnes = {row["column_name"] for row in self.db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'agent_registry'")}
        self.assertTrue({"public_key", "public_key_fingerprint", "key_revoked_at"} <= colonnes)
        colonnes = {row["column_name"] for row in self.db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'agent_mailbox'")}
        self.assertTrue({"nonce", "signed_payload", "signature_expires_at"} <= colonnes)
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
        self.assertEqual(count, 4)

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
            self.assertEqual([m.version for m in done], [1, 2, 3, 4])
            tables = db.query(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN ('agent_registry','agent_mailbox',"
                "'schema_migrations','mesh_approvals','work_items','work_item_events')",
                (schema,),
            )[0]["n"]
            self.assertEqual(tables, 6)
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
