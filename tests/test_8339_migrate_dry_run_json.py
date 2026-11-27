# SPDX-License-Identifier: AGPL-3.0-only
"""#8339 : `ameesh migrate --dry-run --json`, un objet stable, pas du texte.

IMA#1803 retire ses appels privés à `cli.cmd_migrate`, `migrations.check` et
`migrations.non_additive` : le dry-run doit rendre les migrations en attente
(version, libellé, empreinte, raisons non additives), les migrations
appliquées puis modifiées (les deux empreintes) et les versions inconnues,
sans texte mêlé sur stdout, en gardant les codes 0/3/1.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from ameesh import cli, db as db_mod, migrations
from ameesh.modules import registry

from .support import PgTestCase

SCHEMA = "ameesh-migrate-dry-run/1"


class MigrateDryRunJsonTest(PgTestCase):
    def report(self, *options: str):
        """Lance la commande et rend l'objet ; stdout ne porte que lui."""
        out = self.mesh("migrate", "--dry-run", "--json", *options)
        document = json.loads(out.stdout)
        self.assertEqual(document["schema"], SCHEMA)
        self.assertTrue(document["dry_run"])
        return out, document

    def _restore(self, migration: migrations.Migration) -> None:
        self.db.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s,%s,%s) "
            "ON CONFLICT (version) DO NOTHING",
            (migration.version, migration.name, migration.checksum))

    def _restore_checksum(self, migration: migrations.Migration) -> None:
        self.db.execute(
            "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
            (migration.checksum, migration.version))

    def test_up_to_date_renders_an_empty_object(self):
        out, document = self.report()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(document["additive_only"])
        self.assertEqual(document["pending"], [])
        self.assertEqual(document["modified"], [])
        self.assertEqual(document["unknown"], [])

    def test_a_missing_additive_migration_is_data(self):
        last = next(m for m in migrations.discover() if m.version == 51)
        self.assertEqual(migrations.non_additive(last), [])
        self.db.execute("DELETE FROM schema_migrations WHERE version = %s",
                        (last.version,))
        self.addCleanup(self._restore, last)
        out, document = self.report()
        self.assertEqual(out.returncode, 0, out.stderr)
        entry = next(row for row in document["pending"] if row["version"] == last.version)
        self.assertIsNone(entry["module"])
        self.assertEqual(entry["label"], last.label)
        self.assertEqual(entry["checksum"], last.checksum)
        self.assertEqual(entry["non_additive"], [])
        # Le dry-run n'applique rien, même avec --additive-only.
        out = self.mesh("migrate", "--dry-run", "--additive-only", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads(out.stdout)["additive_only"])
        self.assertNotIn(last.version, migrations.applied(self.db))

    def test_a_non_additive_migration_names_the_guard_verdict(self):
        switch = next(m for m in migrations.discover() if m.version == 518)
        self.db.execute("DELETE FROM schema_migrations WHERE version = %s",
                        (switch.version,))
        self.addCleanup(self._restore, switch)
        # `--dry-run` seul reste un diagnostic (code 0) et nomme la raison.
        out, document = self.report()
        self.assertEqual(out.returncode, 0, out.stderr)
        entry = next(row for row in document["pending"] if row["version"] == switch.version)
        self.assertIn("marked non-additive", entry["non_additive"])
        # Le verdict du garde garde le code 3, et le JSON nomme la migration.
        out = self.mesh("migrate", "--dry-run", "--additive-only", "--json")
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        document = json.loads(out.stdout)
        self.assertTrue(document["additive_only"])
        entry = next(row for row in document["pending"] if row["version"] == switch.version)
        self.assertIn("marked non-additive", entry["non_additive"])
        self.assertNotIn(switch.version, migrations.applied(self.db))

    def test_a_modified_migration_lists_both_checksums(self):
        known = migrations.discover()[0]
        applied = "deadbeef"
        self.db.execute(
            "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
            (applied, known.version))
        self.addCleanup(self._restore_checksum, known)
        out, document = self.report()
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        entry = next(row for row in document["modified"] if row["version"] == known.version)
        self.assertIsNone(entry["module"])
        self.assertEqual(entry["label"], known.label)
        self.assertEqual(entry["checksum"], known.checksum)
        self.assertEqual(entry["applied_checksum"], applied)
        self.assertEqual(document["pending"], [])

    def test_a_modified_module_migration_is_tagged_and_never_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "0001_creer.sql"), "w",
                      encoding="utf-8") as stream:
                stream.write("create table w (id int);")
            module = registry.Module(
                name="dev", version="test", source="external", capabilities=(),
                cli=None, storage=(), schema="dev",
                migrations=tuple(migrations.discover(directory, module="dev")),
                requires=())
            physical = migrations.module_schemas([module], self.schema)[0].schema_name
            migrations.migrate(self.db, modules=[module])
            self.addCleanup(self.db.execute,
                            "DELETE FROM module_schemas WHERE module = 'dev'")
            self.addCleanup(self.db.execute,
                            "DELETE FROM module_schema_migrations WHERE module = 'dev'")
            self.addCleanup(self.db.execute,
                            "DROP SCHEMA IF EXISTS %s CASCADE"
                            % db_mod.quote_ident(physical))
            migration = module.migrations[0]
            self.db.execute(
                "UPDATE module_schema_migrations SET checksum = 'deadbeef' "
                "WHERE module = 'dev' AND version = %s", (migration.version,))
            with mock.patch.object(migrations, "installed_modules",
                                   return_value=(module,)):
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), \
                        contextlib.redirect_stderr(stderr):
                    code = cli.cmd_migrate(self.cfg, dry_run=True, json_output=True)
        self.assertEqual(code, 1, stderr.getvalue())
        document = json.loads(stdout.getvalue())
        entry = next(row for row in document["modified"] if row["module"] == "dev")
        self.assertEqual(entry["label"], migration.label)
        self.assertEqual(entry["checksum"], migration.checksum)
        self.assertEqual(entry["applied_checksum"], "deadbeef")
        self.assertEqual([row for row in document["pending"]
                          if row["module"] == "dev"], [])

    def test_a_fresh_schema_lists_everything_and_creates_no_registry(self):
        schema = self.schema + "_jsonvide"
        self.create_schema(schema)
        self.addCleanup(self.db.execute, 'DROP SCHEMA IF EXISTS "%s" CASCADE' % schema)
        out = self.mesh("migrate", "--dry-run", "--json",
                        env=self.env(AMEESH_SCHEMA=schema))
        self.assertEqual(out.returncode, 0, out.stderr)
        document = json.loads(out.stdout)
        self.assertEqual([row["version"] for row in document["pending"]],
                         [migration.version for migration in migrations.discover()])
        created = self.db.query(
            "SELECT to_regclass(%s) IS NOT NULL AS ok",
            ('"%s".schema_migrations' % schema,))[0]["ok"]
        self.assertFalse(created, "le dry-run a créé le registre des migrations")

    def test_json_without_dry_run_is_refused_before_any_write(self):
        out = self.mesh("migrate", "--json")
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertEqual(out.stdout, "")
        self.assertIn("--json requires --dry-run", out.stderr)

    def test_the_agent_mail_alias_accepts_the_option(self):
        out = self.cli("migrate", "--dry-run", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["schema"], SCHEMA)


if __name__ == "__main__":
    unittest.main()
