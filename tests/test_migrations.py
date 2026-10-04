# SPDX-License-Identifier: AGPL-3.0-only
"""Migrations versionnées : application, idempotence, immuabilité."""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid

from ameesh import authority, db as db_mod, mail, migrations, registry, signing

from .support import PgTestCase


class MigrationsTest(PgTestCase):
    def test_schema_migre_versionne(self):
        rows = self.db.query(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        # toutes les migrations livrées sont appliquées, dans l'ordre (les
        # numéros réservés aux lots non encore fusionnés peuvent manquer)
        self.assertEqual([row["version"] for row in rows],
                         [m.version for m in migrations.discover()])
        names = {int(row["version"]): row["name"] for row in rows}
        expected = {1: "init", 2: "overview", 3: "authority", 4: "work_items", 5: "key_roles",
                    6: "authority_fixes", 7: "nonces_backfill", 8: "canon", 9: "threads",
                    11: "receipts", 12: "events", 14: "cost", 22: "placement",
                    23: "authenticator_syncs", 24: "canon_state_authenticators"}
        self.assertEqual({v: names.get(v) for v in expected}, expected)
        # 0024 : la partie authentificateurs de l'état du canon (revue L9b, codex2)
        colonnes = {row["column_name"] for row in self.db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'canon_state'")}
        self.assertTrue({"auth_status", "auth_diagnostic", "auth_checked_at"} <= colonnes)
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
        self.assertTrue({"public_key", "public_key_fingerprint", "key_revoked_at",
                         "key_role", "current_prompt"} <= colonnes)
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
        self.assertEqual(count, len(migrations.discover()))

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
            self.assertEqual([m.version for m in done],
                             [m.version for m in migrations.discover()])
            tables = db.query(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN ('agent_registry','agent_mailbox',"
                "'schema_migrations','mesh_approvals','work_items','work_item_events',"
                "'thread_index')",
                (schema,),
            )[0]["n"]
            self.assertEqual(tables, 7)
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_mise_a_niveau_remplit_le_ledger_des_consommations(self):
        """Sonde codex3 (B3 upgrade) : un reçu consommé avant 0006 ne se rejoue pas.

        On rejoue le chemin réel : schéma 0001–0004, approbation signée déjà
        consommée (colonne seule), puis application de 0005–0007, suppression et
        réinsertion du même reçu signé — find_approval doit refuser.
        """
        schema = "t_upgrade_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        try:
            # 1) un schéma à l'ancienne : uniquement 0001–0004
            import tempfile
            with tempfile.TemporaryDirectory() as ancien:
                for name in sorted(os.listdir(migrations.MIGRATIONS_DIR)):
                    if name[:4] in ("0001", "0002", "0003", "0004"):
                        with open(os.path.join(migrations.MIGRATIONS_DIR, name), encoding="utf-8") as src:
                            with open(os.path.join(ancien, name), "w", encoding="utf-8") as dst:
                                dst.write(src.read())
                done = migrations.migrate(db, directory=ancien)
                self.assertEqual([m.version for m in done], [1, 2, 3, 4])

            # 2) l'ancien monde écrit avec les colonnes de 0003 seulement
            #    (le code v1 d'aujourd'hui connaît current_prompt/key_role, pas
            #    ce schéma : on simule l'ancien code, pas le nouveau).
            seed = signing.backend("pure").generate_seed()
            public = signing.backend("pure").public_from_seed(seed)
            db.execute(
                "INSERT INTO agent_registry (name, harness, host, public_key, "
                "public_key_fingerprint, key_updated_at) VALUES (%s, %s, %s, %s, %s, now())",
                ("proprietaire", "claude", "laptop", authority.b64(public),
                 signing.fingerprint(public)),
            )
            built = authority.build_approval(
                seed, approver="proprietaire", action="merge", artifact_kind="diff",
                artifact_hash=authority.hash_artifact("diff d'avant migration"), ttl=3600)
            approval_id = db.query(
                """
                INSERT INTO mesh_approvals
                    (approver, action, artifact_kind, artifact_hash, decision, nonce,
                     signed_payload, signature, signature_key, created_at, expires_at,
                     consumed_at, consumed_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        to_timestamp(%s::bigint / 1000000.0),
                        to_timestamp(%s::bigint / 1000000.0), now(), 'avant')
                RETURNING id
                """,
                (built["approver"], built["action"], built["artifact_kind"],
                 built["artifact_hash"], built["decision"], built["nonce"],
                 built["signed_payload"], built["signature"], built["signature_key"],
                 built["created_us"], built["expires_us"]),
            )[0]["id"]
            artifact = built["artifact_hash"]
            built["id"] = int(approval_id)

            # 3) migration réelle : 0005, 0006, 0007 (le backfill). Une clé
            #    enregistrée avant 0005 devient « agent » (moindre privilège) :
            #    l'acte explicite --role owner est rejoué, comme à la bascule.
            done = migrations.migrate(db)
            self.assertEqual([m.version for m in done],
                             [m.version for m in migrations.discover() if m.version >= 5])
            db.execute("UPDATE agent_registry SET key_role = 'owner' "
                       "WHERE name = 'proprietaire'")
            ledger = db.query(
                "SELECT approver, nonce, consumed_by FROM mesh_consumed_nonces")
            self.assertEqual(len(ledger), 1)
            self.assertEqual(ledger[0]["consumed_by"], "avant")

            # 4) suppression puis réinsertion du même reçu signé : refusé
            db.execute("DELETE FROM mesh_approvals WHERE id = %s", (built["id"],))
            db.query(
                """
                INSERT INTO mesh_approvals
                    (approver, action, artifact_kind, artifact_hash, decision, nonce,
                     signed_payload, signature, signature_key, created_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        to_timestamp(%s::bigint / 1000000.0), to_timestamp(%s::bigint / 1000000.0))
                RETURNING id
                """,
                (built["approver"], built["action"], built["artifact_kind"],
                 built["artifact_hash"], built["decision"], built["nonce"],
                 built["signed_payload"], built["signature"], built["signature_key"],
                 built["created_us"], built["expires_us"]),
            )
            row, verdict = authority.find_approval(db, "merge", artifact)
            self.assertIsNone(row, "le reçu réinséré ne doit pas être reconsommable")
            self.assertIn("nonce déjà consommé", verdict.reason)
        finally:
            db.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_statement_timeout_borne_les_requetes(self):
        """Une requête qui pend est annulée : le battement de bail ne bloque pas."""
        import dataclasses
        import time as temps
        cfg = dataclasses.replace(self.cfg, statement_timeout_ms=300)
        db = db_mod.connect(cfg)
        try:
            debut = temps.monotonic()
            with self.assertRaises(db_mod.DbError) as ctx:
                db.query("SELECT pg_sleep(3)")
            self.assertLess(temps.monotonic() - debut, 2.5)
            self.assertIn("timeout", str(ctx.exception).lower())
        finally:
            db.close()

    def test_migration_longue_non_bornee_par_le_statement_timeout(self):
        """Les migrations lèvent le délai (SET LOCAL) : une longue passe aboutit."""
        import dataclasses
        import tempfile
        schema = "t_lente_%s" % uuid.uuid4().hex[:8]
        db = self.connect(schema)
        try:
            db.execute('CREATE SCHEMA "%s"' % schema)
            with tempfile.TemporaryDirectory() as dossier:
                with open(os.path.join(dossier, "0001_lente.sql"), "w", encoding="utf-8") as fh:
                    fh.write("create table lente (id int primary key);\n"
                             "select pg_sleep(0.6);\n")
                cfg = dataclasses.replace(self.cfg, schema=schema, statement_timeout_ms=200)
                lent = db_mod.connect(cfg)
                try:
                    done = migrations.migrate(lent, directory=dossier)
                    self.assertEqual([m.version for m in done], [1])
                finally:
                    lent.close()
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
