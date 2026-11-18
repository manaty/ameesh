# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 : le résolveur de schéma ne doit jamais avarier la transaction.

Constat de revue (ameesh-cx2, #43580) : ``schema_name`` lisait
``module_schemas`` dans la transaction du mouvement et attrapait l'exception.
Sur une base d'avant P1a — ou un registre illisible — PostgreSQL a déjà avarié
la transaction : psql termine la session (``ON_ERROR_STOP``), psycopg refuse
toute instruction suivante. Le repli sur le schéma noyau arrivait trop tard.

Constat #43584 : un registre **présent mais inaccessible** ne doit pas être
traité comme absent. Après P2, choisir le noyau écrirait au mauvais endroit ;
le résolveur refuse alors explicitement, avant tout accès aux tables ``dev``.

Ces tests parlent aux **deux pilotes réels** : registre absent, vide, sans
``SELECT``, sans ``USAGE``, et simulation P2 (registre lisible puis illisible).
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
Q = db_mod.quote_ident

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

#: la table de P1a, quand le test simule une base déjà migrée.
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
        self.admin.script("CREATE SCHEMA %s" % Q(self.schema))
        self._create_tables()

    def _cleanup(self):
        for schema in (self.schema + "__dev", self.schema):
            self.admin.execute("DROP SCHEMA IF EXISTS %s CASCADE" % Q(schema))
        self.admin.close()

    def _create_tables(self, schema=None):
        prefix = "SET LOCAL search_path TO %s;\n" % Q(schema) if schema else ""
        self.admin.script(prefix + ";\n".join(_TABLES))

    def _create_dev_schema(self):
        """La cible d'une P2 appliquée : le schéma physique du module."""
        self.dev = self.schema + "__dev"
        self.admin.script("CREATE SCHEMA %s" % Q(self.dev))
        self._create_tables(self.dev)

    def _create_registry(self, schema_name=None):
        self.admin.execute("SET search_path TO %s" % Q(self.schema))
        self.admin.script(_MODULE_SCHEMAS)
        if schema_name is not None:
            self.admin.execute(
                "INSERT INTO module_schemas (module, schema_name, schema_sha256)"
                " VALUES ('dev', %s, %s)", (schema_name, "a" * 64))

    def _count(self, table, schema=None):
        return self.admin.query(
            "SELECT count(*) AS n FROM %s.%s"
            % (Q(schema or self.schema), Q(table)))[0]["n"]

    def _new_role(self, name):
        role = "t_7650_%s_%s" % (name, uuid.uuid4().hex[:10])
        self.admin.execute("CREATE ROLE %s NOLOGIN" % Q(role))
        self.addCleanup(self._drop_role, role)
        return role

    def _drop_role(self, role):
        if self.admin.query("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)):
            self.admin.execute("DROP OWNED BY %s" % Q(role))
            self.admin.execute("DROP ROLE %s" % Q(role))

    def _grant_movement(self, role, schema):
        """Les droits nécessaires au mouvement, sans le registre."""
        self.admin.script(
            "GRANT USAGE ON SCHEMA %(schema)s TO %(role)s;"
            "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA %(schema)s"
            "  TO %(role)s;"
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %(schema)s"
            "  TO %(role)s"
            % {"role": Q(role), "schema": Q(schema)})

    def _resolution_as(self, role, driver, **overrides):
        """`record_resolution` sous un rôle, dans une transaction neuve."""
        conn = db_mod.connect(self.cfg, driver=driver, writer=False)
        try:
            with conn.transaction() as tx:
                tx.execute("SET LOCAL ROLE %s" % Q(role))
                self.assertEqual(
                    tx.query("SELECT current_user AS u")[0]["u"], role)
                return storage.of(tx).work.record_resolution(
                    1, 2, **dict(_RESOLUTION, **overrides))
        finally:
            conn.close()

    def _record_resolution(self, driver, expected_schema=None, **overrides):
        """La première opération d'une connexion neuve, sur ce schéma."""
        conn = db_mod.connect(self.cfg, driver=driver, writer=False)
        try:
            row = storage.of(conn).work.record_resolution(
                1, 2, **dict(_RESOLUTION, **overrides))
            self.assertEqual(schema_mod.schema_name(conn),
                             expected_schema or self.schema)
        finally:
            conn.close()
        return row

    def _assert_row(self, row):
        self.assertIsNotNone(row)
        self.assertEqual(row["work_item_id"], 1)
        self.assertEqual(row["other_item_id"], 2)
        self.assertEqual(list(row["files"]), ["src/a.py"])

    def test_registry_absent_does_not_poison_the_first_transaction(self):
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                self._assert_row(self._record_resolution(driver))

    def test_empty_registry_falls_back_to_the_core_schema(self):
        """P1a appliquée, P2 non : le registre est lisible mais sans ligne."""
        self._create_registry()
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                self._assert_row(self._record_resolution(driver))

    def test_registry_without_select_is_refused(self):
        """Présent, USAGE accordé, SELECT refusé : erreur, jamais le noyau."""
        role = self._new_role("noselect")
        self._create_registry()
        self._grant_movement(role, self.schema)
        self.admin.execute("REVOKE ALL ON %s.module_schemas FROM %s"
                           % (Q(self.schema), Q(role)))
        for driver in DRIVERS:
            with self.subTest(driver=driver), self.assertRaisesRegex(
                    db_mod.DbError, "present but not readable"):
                self._resolution_as(role, driver)
        self.assertEqual(self._count("work_resolutions"), 0)

    def test_registry_select_without_schema_usage_is_refused(self):
        """SELECT accordé mais USAGE manquant : la sonde le voit et refuse.

        Ce cas ne peut pas passer par `record_resolution` : sans USAGE sur le
        schéma noyau, sa première lecture de `work_items` échoue avant le
        registre. On résout donc directement, sous le rôle.
        """
        role = self._new_role("nousage")
        self._create_registry()
        self.admin.execute("GRANT SELECT ON %s.module_schemas TO %s"
                           % (Q(self.schema), Q(role)))
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                conn = db_mod.connect(self.cfg, driver=driver, writer=False)
                try:
                    with self.assertRaisesRegex(db_mod.DbError,
                                                "present but not readable"):
                        with conn.transaction() as tx:
                            tx.execute("SET LOCAL ROLE %s" % Q(role))
                            schema_mod.schema_name(tx)
                finally:
                    conn.close()

    def test_registry_readable_after_p2_uses_the_registered_schema(self):
        """P2 : la ligne du registre désigne le schéma physique du module."""
        self._create_dev_schema()
        self._create_registry(self.dev)
        for driver in DRIVERS:
            with self.subTest(driver=driver):
                self._assert_row(self._record_resolution(
                    driver, expected_schema=self.dev))
                # même paire (1, 2) : l'upsert met à jour, le compte reste 1
                self.assertEqual(self._count("work_resolutions", self.dev), 1)
        self.assertEqual(self._count("work_resolutions"), 0)

    def test_unreadable_registry_after_p2_does_not_fall_back_to_core(self):
        """P2 : le rôle lit les tables `dev`, pas le registre : refus explicite."""
        role = self._new_role("p2noselect")
        self._create_dev_schema()
        self._create_registry(self.dev)
        # les lectures noyau du mouvement et les tables dev restent permises
        self._grant_movement(role, self.schema)
        self._grant_movement(role, self.dev)
        self.admin.execute("REVOKE ALL ON %s.module_schemas FROM %s"
                           % (Q(self.schema), Q(role)))
        for driver in DRIVERS:
            with self.subTest(driver=driver), self.assertRaisesRegex(
                    db_mod.DbError, "present but not readable"):
                self._resolution_as(role, driver)
        self.assertEqual(self._count("work_resolutions", self.dev), 0)
        self.assertEqual(self._count("work_resolutions"), 0)


if __name__ == "__main__":
    unittest.main()
