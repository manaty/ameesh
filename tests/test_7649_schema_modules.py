# SPDX-License-Identifier: AGPL-3.0-only
"""P1a #7649 : schémas et migrations de module (étude #7616 §4.1–4.5).

Le noyau garde son registre ; un module déclare ``SCHEMA``, ``MIGRATIONS`` et
``REQUIRES``. Le runner crée un schéma ``<schéma_noyau>__<module>``, applique
les migrations du module dans la même transaction et le même verrou que le
noyau, et le doctor rapporte les deux registres. Aucune table n'est déplacée.
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from unittest import mock

from . import support  # isole l'environnement de l'hôte avant le produit
from ameesh import db as db_mod, migrations
from ameesh.modules import registry


def write_migrations(directory: str, files: dict[str, str]) -> None:
    for name, sql in files.items():
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            handle.write(sql)


def fake_schema_module(name: str, directory: str, *, requires=(), schema=None):
    """Un descripteur `registry.Module` tel que la découverte le rendrait."""
    return registry.Module(
        name=name, version="test", source="external", capabilities=(), cli=None,
        storage=(), schema=schema or name,
        migrations=tuple(migrations.discover(directory, module=name)),
        requires=tuple(requires))


def dummy_migration(module: str, version: int = 1) -> migrations.Migration:
    return migrations.Migration(version=version, name="essai", path="",
                                sql="select 1", checksum="0" * 64, module=module)


class FakeDistribution:
    name = "ameesh-sample-module"
    version = "0.1.0"


class FakeEntryPoint:
    group = "ameesh.modules"

    def __init__(self, module, name="hello"):
        self.name = name
        self.module = module
        self.value = "hello_module"
        self.dist = FakeDistribution()

    def load(self):
        return self.module


class FakeEntryPoints(list):
    def select(self, group=None):
        return [point for point in self if group in (None, point.group)]


class ExternalDeclarationTest(unittest.TestCase):
    """Q5 : un module externe peut déclarer ses migrations (code installé)."""

    def module(self, directory: str, *, folder="migrations"):
        package = os.path.join(directory, "package")
        os.makedirs(package, exist_ok=True)
        migrations_folder = os.path.join(package, "migrations")
        os.makedirs(migrations_folder, exist_ok=True)
        write_migrations(migrations_folder, {
            "0001_create.sql": "create table hello (id int);"})
        module = types.ModuleType("hello_module")
        path = os.path.join(package, "hello.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("")
        module.__file__ = path
        module.NAME = "hello"
        module.descriptor = lambda options=None: {
            "types": ["docs"], "schemas": {}, "rules": []}
        module.SCHEMA = "hello"
        module.MIGRATIONS = folder
        return module

    def test_an_external_module_exposes_its_schema_and_migrations(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module(directory)
            with mock.patch("ameesh.modules.metadata.entry_points",
                            return_value=FakeEntryPoints([FakeEntryPoint(module)])), \
                    mock.patch.object(registry, "_builtin", return_value={}):
                found = registry.discover()
        row = found.get("hello")
        self.assertIsNotNone(row)
        self.assertEqual(row.schema, "hello")
        self.assertEqual([migration.label for migration in row.migrations],
                         ["hello/0001_create"])
        self.assertEqual(row.as_dict()["schema"], "hello")

    def test_an_installed_module_cannot_escape_its_package(self):
        with tempfile.TemporaryDirectory() as directory:
            module = self.module(directory, folder="../outside")
            with mock.patch("ameesh.modules.metadata.entry_points",
                            return_value=FakeEntryPoints([FakeEntryPoint(module)])), \
                    mock.patch.object(registry, "_builtin", return_value={}):
                found = registry.discover()
        self.assertIsNone(found.get("hello"))
        self.assertIn("escapes its package", found.refused[0]["error"])


class DeclarationTest(unittest.TestCase):
    """Les faits de schéma se lisent sans exécuter le module."""

    def package(self, directory: str, *, file: bool = True) -> types.ModuleType:
        module = types.ModuleType("sample")
        path = os.path.join(directory, "sample.py")
        if file:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("")
            module.__file__ = path
        return module

    def test_declaration_reads_schema_migrations_and_requires(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = os.path.join(directory, "migrations")
            os.makedirs(folder)
            write_migrations(folder, {
                "0002_second.sql": "create table b (id int);",
                "0001_first.sql": "create table a (id int);"})
            module = self.package(directory)
            module.SCHEMA = "dev"
            module.MIGRATIONS = "migrations"
            module.REQUIRES = ("it",)
            found = migrations.module_metadata("sample", module)
        self.assertEqual(found["schema"], "dev")
        self.assertEqual([migration.label for migration in found["migrations"]],
                         ["sample/0001_first", "sample/0002_second"])
        self.assertEqual(found["requires"], ("it",))

    def test_no_migrations_declares_no_schema_to_create(self):
        module = types.ModuleType("it")
        self.assertEqual(migrations.module_metadata("it", module),
                         {"schema": None, "migrations": (), "requires": (),
                          "directory": None})
        module.SCHEMA = "it"
        self.assertEqual(migrations.module_metadata("it", module)["schema"], "it")

    def test_migrations_folder_is_confined_to_the_package(self):
        with tempfile.TemporaryDirectory() as directory:
            package = os.path.join(directory, "pkg")
            os.makedirs(package)
            os.makedirs(os.path.join(directory, "outside"))
            module = self.package(package)
            for folder in ("../outside", "/tmp", "absent"):
                with self.subTest(folder=folder):
                    module.MIGRATIONS = folder
                    with self.assertRaises(ValueError):
                        migrations.module_metadata("sample", module)

    def test_a_broken_requires_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = os.path.join(directory, "migrations")
            os.makedirs(folder)
            write_migrations(folder, {"0001_a.sql": "select 1;"})
            module = self.package(directory)
            module.MIGRATIONS = "migrations"
            for requires in ("it", ("sample",), ("it", "it"), ("bad name",)):
                with self.subTest(requires=requires):
                    module.REQUIRES = requires
                    with self.assertRaises(ValueError):
                        migrations.module_metadata("sample", module)


class SchemaNameTest(unittest.TestCase):
    def test_derived_from_the_core_schema_and_the_module_slug(self):
        self.assertEqual(migrations.schema_name("public", "dev"), "public__dev")
        self.assertEqual(migrations.schema_name("t_4616", "work-types"),
                         "t_4616__work_types")

    def test_long_name_keeps_a_stable_sha_suffix_and_postgres_limit(self):
        first = migrations.schema_name("t_4616", "a" * 60)
        second = migrations.schema_name("t_4616", "a" * 60)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first.encode("utf-8")), 63)
        self.assertTrue(first.startswith("t_4616__"))
        self.assertNotEqual(first, migrations.schema_name("t_4616", "a" * 59 + "b"))

    def test_identity_is_the_logical_pair_not_the_migrations(self):
        self.assertEqual(migrations.schema_identity("dev", "dev"),
                         migrations.schema_identity("dev", "dev"))
        self.assertNotEqual(migrations.schema_identity("dev", "dev"),
                            migrations.schema_identity("it", "dev"))


class TopologyTest(unittest.TestCase):
    def module(self, name, *, requires=(), migrated=True, schema=None):
        return registry.Module(
            name=name, version="test", source="external", capabilities=(), cli=None,
            storage=(), schema=schema or name,
            migrations=(dummy_migration(name),) if migrated else (),
            requires=tuple(requires))

    def test_requires_are_applied_first_whatever_the_discovery_order(self):
        found = migrations.module_schemas(
            [self.module("a", requires=("b",)), self.module("b"), self.module("c")],
            "public")
        self.assertEqual([schema.module for schema in found], ["b", "a", "c"])

    def test_cycle_is_refused_before_anything_is_applied(self):
        with self.assertRaisesRegex(db_mod.DbError, "cycle"):
            migrations.module_schemas(
                [self.module("a", requires=("b",)), self.module("b", requires=("a",))],
                "public")

    def test_unknown_or_schema_less_requirement_is_refused(self):
        with self.assertRaisesRegex(db_mod.DbError, "unknown module"):
            migrations.module_schemas([self.module("a", requires=("absent",))], "public")
        with self.assertRaisesRegex(db_mod.DbError, "declares no schema"):
            migrations.module_schemas(
                [self.module("a", requires=("it",)), self.module("it", migrated=False)],
                "public")

    def test_duplicate_module_name_is_refused(self):
        with self.assertRaisesRegex(db_mod.DbError, "duplicate module"):
            migrations.module_schemas([self.module("a"), self.module("a")], "public")

    def test_bare_profile_has_no_module_schema(self):
        with mock.patch.object(registry, "_builtin", return_value={}), \
                mock.patch.object(registry, "_entry_points", return_value=()):
            self.assertEqual(migrations.installed_modules(), ())
        self.assertEqual(migrations.module_schemas((), "public"), [])


class NonAdditiveTest(unittest.TestCase):
    def test_set_schema_is_not_additive(self):
        for sql in (
                "alter table t set schema autre;",
                "alter table if exists only t set schema autre;",
                "alter sequence s set schema autre;",
                "alter materialized view v set schema autre;",
                "alter table a add column c text, alter table b set schema autre;",
                "do $$ begin execute 'alter table t set schema autre'; end $$;"):
            with self.subTest(sql=sql):
                self.assertIn("object moved to another schema (set schema)",
                              migrations.non_additive_sql(sql))

    def test_set_schema_in_a_comment_does_not_count(self):
        self.assertEqual(
            migrations.non_additive_sql("-- alter table t set schema autre\n"
                                        "alter table t add column c text;"), [])

    def test_a_module_migration_uses_every_current_rule(self):
        migration = migrations.Migration(
            version=1, name="cutover", path="", sql="alter table t set schema autre;",
            checksum="0" * 64, module="dev")
        self.assertTrue(migrations.non_additive(migration))
        self.assertEqual(migrations.non_additive(migrations.Migration(
            version=1, name="ajout", path="", sql="alter table t add column c text;",
            checksum="0" * 64, module="dev")), [])

    def test_security_definer_requires_an_explicit_search_path(self):
        unsafe = migrations.Migration(
            version=1, name="f", path="",
            sql="create function f() returns trigger language plpgsql "
                "security definer as $$ begin return new; end $$;",
            checksum="0" * 64, module="dev")
        self.assertEqual(migrations.module_sql_problems(unsafe),
                         ["SECURITY DEFINER without SET search_path"])
        safe = migrations.Migration(
            version=1, name="f", path="",
            sql="create function f() returns trigger language plpgsql "
                "security definer set search_path = pg_catalog, pg_temp "
                "as $$ begin return new; end $$;",
            checksum="0" * 64, module="dev")
        self.assertEqual(migrations.module_sql_problems(safe), [])
        commented = migrations.Migration(
            version=1, name="f", path="",
            sql="-- security definer\ncreate table t (id int);",
            checksum="0" * 64, module="dev")
        self.assertEqual(migrations.module_sql_problems(commented), [])


class SchemaMigrationDbTest(support.PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM module_schema_migrations")
        self.db.execute("DELETE FROM module_schema_non_additive_migrations")
        self.db.execute("DELETE FROM module_schemas")
        prefix = self.schema + migrations.MODULE_SCHEMA_SEP
        for row in self.db.query(
                "SELECT nspname FROM pg_namespace WHERE left(nspname, %s) = %s",
                (len(prefix), prefix)):
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE'
                            % row["nspname"].replace('"', '""'))

    def physical(self, module):
        return migrations.schema_name(self.schema, module)

    def test_the_three_registries_are_additive_and_present(self):
        for table in ("module_schemas", "module_schema_migrations",
                      "module_schema_non_additive_migrations"):
            with self.subTest(table=table):
                self.assertTrue(self.db.query(
                    "SELECT to_regclass(%s) IS NOT NULL AS ok", (table,))[0]["ok"])
        columns = {row["column_name"] for row in self.db.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'module_schemas'")}
        self.assertTrue({"module", "schema_name", "schema_sha256", "created_at"} <= columns)

    def test_a_module_migration_is_applied_in_its_derived_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            write_migrations(directory, {
                "0001_create.sql": "create table widgets (id bigserial primary key);"})
            module = fake_schema_module("dev", directory)
            done = migrations.migrate(self.db, modules=[module])
        self.assertEqual([migration.label for migration in done],
                         ["dev/0001_create"])
        physical = self.physical("dev")
        self.assertTrue(self.db.query(
            "SELECT to_regclass(%s) IS NOT NULL AS ok", (physical + ".widgets",))[0]["ok"])
        row = self.db.query(
            "SELECT schema_name, schema_sha256 FROM module_schemas WHERE module = 'dev'")[0]
        self.assertEqual(row["schema_name"], physical)
        self.assertEqual(row["schema_sha256"], migrations.schema_identity("dev", "dev"))
        self.assertEqual(self.db.query(
            "SELECT checksum FROM module_schema_migrations WHERE module = 'dev' "
            "AND version = 1")[0]["checksum"], module.migrations[0].checksum)
        # Rejouer la passe ne réapplique rien.
        self.assertEqual(migrations.migrate(self.db, modules=[module]), [])

    def test_requires_orders_the_schemas_and_a_foreign_key_proves_it(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            write_migrations(first, {
                "0001_base.sql": "create table b (id bigint primary key);"})
            write_migrations(second, {
                "0001_child.sql": "create table a (id bigint primary key, "
                                  "b_id bigint references %s.b(id));"
                                  % self.physical("b")})
            base = fake_schema_module("b", first)
            child = fake_schema_module("a", second, requires=("b",))
            # L'ordre de découverte est l'inverse de l'ordre d'application.
            done = migrations.migrate(self.db, modules=[child, base])
        self.assertEqual([migration.label for migration in done],
                         ["b/0001_base", "a/0001_child"])
        for table in ("b", "a"):
            with self.subTest(table=table):
                self.assertTrue(self.db.query(
                    "SELECT to_regclass(%s) IS NOT NULL AS ok",
                    ("%s.%s" % (self.physical(table), table),))[0]["ok"])

    def test_additive_only_refuses_a_module_set_schema_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            write_migrations(directory, {
                "0001_cutover.sql": "alter table work_items set schema ghosts;"})
            module = fake_schema_module("dev", directory)
            with self.assertRaises(migrations.NonAdditiveMigration):
                migrations.migrate(self.db, modules=[module], additive_only=True)
        self.assertFalse(self.db.query(
            "SELECT to_regclass(%s) IS NOT NULL AS ok",
            (self.physical("dev"),))[0]["ok"])
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM module_schemas")[0]["n"], 0)

    def test_the_runner_refuses_security_definer_without_search_path(self):
        with tempfile.TemporaryDirectory() as directory:
            write_migrations(directory, {
                "0001_function.sql":
                    "create function f() returns trigger language plpgsql "
                    "security definer as $$ begin return new; end $$;"})
            module = fake_schema_module("dev", directory)
            with self.assertRaisesRegex(db_mod.DbError, "SECURITY DEFINER"):
                migrations.migrate(self.db, modules=[module])
        self.assertFalse(self.db.query(
            "SELECT to_regclass(%s) IS NOT NULL AS ok",
            (self.physical("dev"),))[0]["ok"])
        self.assertEqual(self.db.query(
            "SELECT count(*)::int AS n FROM module_schemas")[0]["n"], 0)

    def test_a_marked_non_additive_module_migration_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            write_migrations(directory, {
                "0001_marked.sql": "-- ameesh: non-additive\n"
                                   "create table marked (id int);"})
            module = fake_schema_module("dev", directory)
            migrations.migrate(self.db, modules=[module])
        row = self.db.query(
            "SELECT name FROM module_schema_non_additive_migrations "
            "WHERE module = 'dev' AND version = 1")[0]
        self.assertEqual(row["name"], "marked")

    def test_modified_checksum_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            write_migrations(directory, {"0001_create.sql": "create table w (id int);"})
            module = fake_schema_module("dev", directory)
            migrations.migrate(self.db, modules=[module])
        self.db.execute("UPDATE module_schema_migrations SET checksum = 'deadbeef' "
                        "WHERE module = 'dev' AND version = 1")
        with self.assertRaisesRegex(db_mod.DbError, "modified after being applied"):
            migrations.pending_modules(
                self.db, migrations.module_schemas([module], self.schema))

    def test_registry_identity_is_never_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            write_migrations(directory, {"0001_create.sql": "create table w (id int);"})
            module = fake_schema_module("dev", directory)
            migrations.migrate(self.db, modules=[module])
        self.db.execute("UPDATE module_schemas SET schema_name = %s WHERE module = 'dev'",
                        (self.physical("dev") + "_old",))
        with self.assertRaisesRegex(db_mod.DbError, "registry names"):
            migrations.pending_modules(
                self.db, migrations.module_schemas([module], self.schema))

    def test_check_all_reads_both_registries_without_writing(self):
        schema = "lecture_%s" % os.urandom(4).hex()
        self.db.execute('CREATE SCHEMA "%s"' % schema)
        other = self.connect(schema)
        try:
            with tempfile.TemporaryDirectory() as directory:
                write_migrations(directory, {"0001_create.sql": "create table w (id int);"})
                module = fake_schema_module("dev", directory)
                check = migrations.check_all(
                    other, modules=[module], core_schema=schema)
            self.assertEqual(check.modules.checks[0].module, "dev")
            self.assertFalse(check.modules.checks[0].registered)
            self.assertEqual([migration.label for migration in
                              check.modules.checks[0].missing], ["dev/0001_create"])
            self.assertFalse(other.query(
                "SELECT to_regclass('module_schemas') IS NOT NULL AS ok")[0]["ok"])
            self.assertFalse(other.query(
                "SELECT to_regclass('schema_migrations') IS NOT NULL AS ok")[0]["ok"])
        finally:
            other.close()
            self.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)

    def test_doctor_reports_schema_and_migration_state(self):
        with tempfile.TemporaryDirectory() as directory:
            write_migrations(directory, {"0001_create.sql": "create table w (id int);"})
            module = fake_schema_module("dev", directory)
            pending_ok, pending = migrations.module_doctor_lines(
                self.db, self.schema, modules=[module])
            self.assertTrue(pending_ok)
            self.assertIn("dev/0001_create", "\n".join(pending))
            migrations.migrate(self.db, modules=[module])
            applied_ok, applied = migrations.module_doctor_lines(
                self.db, self.schema, modules=[module])
            self.assertTrue(applied_ok)
            self.assertIn("1/1 migration(s) applied", "\n".join(applied))
            self.db.execute("UPDATE module_schema_migrations SET checksum = 'deadbeef' "
                            "WHERE module = 'dev'")
            broken_ok, broken = migrations.module_doctor_lines(
                self.db, self.schema, modules=[module])
            self.assertFalse(broken_ok)
            self.assertIn("modified after being applied", "\n".join(broken))

    def test_doctor_reports_an_orphan_module(self):
        orphan = self.schema + "__ghost"
        self.db.execute('CREATE SCHEMA "%s"' % orphan)
        self.addCleanup(self.db.execute, 'DROP SCHEMA IF EXISTS "%s" CASCADE' % orphan)
        self.db.execute(
            "INSERT INTO module_schemas (module, schema_name, schema_sha256) "
            "VALUES ('ghost', %s, %s)",
            (orphan, migrations.schema_identity("ghost", "ghost")))
        ok, lines = migrations.module_doctor_lines(self.db, self.schema, modules=())
        self.assertTrue(ok)
        self.assertIn("registered in schema", "\n".join(lines))
        self.assertIn("orphan module schema %s" % orphan, "\n".join(lines))

    def test_module_json_fields_are_additive_and_read_only(self):
        dev = registry.discover().get("dev")
        self.assertEqual(dev.schema, "dev")
        self.assertEqual(dev.as_dict()["schema"], "dev")
        self.assertEqual(dev.as_dict()["migrations"], [])
        self.assertEqual(dev.as_dict()["requires"], [])


if __name__ == "__main__":
    unittest.main()
