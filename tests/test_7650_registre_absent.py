# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 : le résolveur de schéma ne doit jamais avarier la transaction.

Constat de revue (ameesh-cx2, #43580) : ``schema_name`` lisait
``module_schemas`` dans la transaction du mouvement et attrapait l'exception.
Sur une base d'avant P1a — ou un registre illisible — PostgreSQL a déjà avarié
la transaction : psql termine la session (``ON_ERROR_STOP``), psycopg refuse
toute instruction suivante. Le repli sur le schéma noyau arrivait trop tard.

Ces tests parlent aux **deux pilotes réels**, sur un schéma sans
``module_schemas`` : la première opération d'une connexion neuve
(``record_resolution``) doit aboutir, registre absent comme registre vide.
"""
from __future__ import annotations

import importlib.util
import unittest
import uuid

from ameesh import config, db as db_mod, storage
from ameesh.modules.dev.storage import schema as schema_mod
from .support import REQUIRE_DB, child_env

HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None
DRIVERS = ("psycopg", "psql") if HAVE_PSYCOPG else ("psql",)

#: les tables que `record_resolution` touche, réduites à leurs colonnes.
_TABLES = (
    "CREATE TABLE work_items ("
    "  id bigint PRIMARY KEY, state text NOT NULL, legacy_state text)",
    "CREATE TABLE work_item_events ("
    "  id bigserial PRIMARY KEY, work_item_id bigint, state text, note text,"
    "  actor text, evidence jsonb)",
    "CREATE TABLE work_resolutions ("
    "  work_item_id bigint NOT NULL, other_item_id bigint NOT NULL,"
    "  commit_sha text NOT NULL, lot_tip text NOT NULL, other_tip text NOT NULL,"
    "  ref text NOT NULL, files text[] NOT NULL, created_by text NOT NULL,"
    "  created_at timestamptz NOT NULL DEFAULT now(),"
    "  PRIMARY KEY (work_item_id, other_item_id))",
    "INSERT INTO work_items (id, state) VALUES (1, 'active'), (2, 'active')",
)

#: la table de P1a, quand le test simule une base déjà migrée mais sans ligne.
_MODULE_SCHEMAS = (
    "CREATE TABLE module_schemas ("
    "  module text PRIMARY KEY, schema_name text NOT NULL UNIQUE,"
    "  schema_sha256 text NOT NULL,"
    "  created_at timestamptz NOT NULL DEFAULT now())"
)

_RESOLUTION = dict(commit="a" * 40, lot_tip="b" * 40, other_tip="c" * 40,
                   ref="refs/heads/lot", files=["src/a.py"], note="note",
                   actor="test")


class RegistryAbsentTest(unittest.TestCase):
    """Un schéma de test isolé, sans les migrations du noyau."""

    def setUp(self):
        self.schema = "t_7650_registre_" + uuid.uuid4().hex[:10]
        self.cfg = config.load(env=child_env(AMEESH_SCHEMA=self.schema))
        try:
            self.admin = db_mod.connect(self.cfg, writer=False)
        except db_mod.Unavailable as exc:
            if not REQUIRE_DB:
                self.skipTest(str(exc))
            raise
        self.addCleanup(self._cleanup)
        self.admin.script("CREATE SCHEMA %s" % db_mod.quote_ident(self.schema))
        self.admin.script(";\n".join(_TABLES))

    def _cleanup(self):
        self.admin.execute("DROP SCHEMA IF EXISTS %s CASCADE"
                           % db_mod.quote_ident(self.schema))
        self.admin.close()

    def _drop_role(self, role):
        if self.admin.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)):
            self.admin.execute("DROP OWNED BY %s" % db_mod.quote_ident(role))
            self.admin.execute("DROP ROLE %s" % db_mod.quote_ident(role))

    def _record_resolution(self, driver, **overrides):
        """La première opération d'une connexion neuve, sur ce schéma."""
        conn = db_mod.connect(self.cfg, driver=driver, writer=False)
        try:
            row = storage.of(conn).work.record_resolution(
                1, 2, **dict(_RESOLUTION, **overrides))
            # cache négatif : la connexion reste sur le schéma noyau
            self.assertEqual(schema_mod.schema_name(conn), self.schema)
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["work_item_id"], 1)
        self.assertEqual(row["other_item_id"], 2)
        self.assertEqual(list(row["files"]), ["src/a.py"])

    def test_registry_absent_does_not_poison_the_first_transaction(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                self._record_resolution(driver)

    def test_empty_registry_falls_back_to_the_core_schema(self):
        """P1a appliquée, P2 non : le registre est lisible mais sans ligne."""
        self.admin.script(_MODULE_SCHEMAS)
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                self._record_resolution(driver)

    def test_unreadable_registry_is_not_queried(self):
        """Sans droit de lecture, la sonde rend faux sans lever.

        Le rôle est simulé par ``SET LOCAL ROLE`` dans la transaction : les
        tables du mouvement restent accessibles, pas le registre.
        """
        role = "t_7650_noselect_" + uuid.uuid4().hex[:10]
        q = db_mod.quote_ident
        self.admin.script(_MODULE_SCHEMAS)
        self.admin.execute("CREATE ROLE %s NOLOGIN" % q(role))
        self.addCleanup(self._drop_role, role)
        self.admin.script(
            "GRANT USAGE ON SCHEMA %(schema)s TO %(role)s;"
            "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA %(schema)s"
            "  TO %(role)s;"
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %(schema)s"
            "  TO %(role)s;"
            "REVOKE ALL ON %(schema)s.module_schemas FROM %(role)s"
            % {"role": q(role), "schema": q(self.schema)})
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                conn = db_mod.connect(self.cfg, driver=driver, writer=False)
                try:
                    with conn.transaction() as tx:
                        tx.execute("SET LOCAL ROLE %s" % q(role))
                        self.assertEqual(
                            tx.query("SELECT current_user AS u")[0]["u"], role)
                        self.assertFalse(tx.query(
                            "SELECT has_table_privilege(%s, 'SELECT') AS p",
                            ("%s.module_schemas" % q(self.schema),))[0]["p"])
                        row = storage.of(tx).work.record_resolution(
                            1, 2, **dict(_RESOLUTION, files=[]))
                    # cache négatif : le registre n'a jamais été lu
                    self.assertEqual(schema_mod.schema_name(conn), self.schema)
                finally:
                    conn.close()
                self.assertIsNotNone(row)
                self.assertEqual(row["work_item_id"], 1)


if __name__ == "__main__":
    unittest.main()
