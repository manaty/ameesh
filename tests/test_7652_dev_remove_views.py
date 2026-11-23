# SPDX-License-Identifier: AGPL-3.0-only
"""P3 on an isolated populated schema, including P2 rollback and re-upgrade."""
from pathlib import Path
from unittest import mock

from ameesh import db as db_mod, migrations, work
from ameesh.modules import dev, registry
from ameesh.modules.dev.storage import schema as dev_schema
from ameesh.modules.dev.tables import TABLES
from . import support
from .test_7651_dev_schema_cutover import DevCutoverTest, cutover_modules, ROLLBACK


@support.needs_builtin_modules
class RemoveViewsTest(support.PgTestCase):
    populate = DevCutoverTest.populate
    snapshot = DevCutoverTest.snapshot
    owned_sequence = DevCutoverTest.owned_sequence

    def test_roundtrip_and_guards(self):
        self.db.execute('TRUNCATE ' + ','.join(TABLES) + ' RESTART IDENTITY CASCADE')
        self.populate()
        before = self.snapshot(self.schema)
        migrations.migrate(self.db, modules=cutover_modules())
        # P2 requires stopped writers; simulate a fresh executor connection.
        connection = dev_schema._connection(self.db)
        if hasattr(connection, '_ameesh_dev_schema'):
            delattr(connection, '_ameesh_dev_schema')
        with mock.patch.object(dev, 'MIGRATIONS', 'migrations'):
            modules = registry.discover()
        functions = ('work_items_migration_slot_sync', 'work_migration_reservation_guard',
                     'ameesh_review_seats_guard', 'ameesh_review_verdict_guard')
        def definitions():
            return self.db.query('SELECT p.proname, p.proconfig, pg_get_functiondef(p.oid) AS definition '
                                 'FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace '
                                 'WHERE n.nspname=%s AND p.proname = ANY(%s) ORDER BY p.proname',
                                 (self.schema, list(functions)))
        original = definitions()
        self.assertEqual(len(original), 4)
        migration = migrations.migrate(self.db, modules=modules)
        self.assertEqual([m.version for m in migration], [2])
        physical = migrations.schema_name(self.schema, 'dev')
        self.assertEqual(self.snapshot(physical), before)
        for name in TABLES:
            self.assertIsNone(self.db.query('SELECT to_regclass(%s) AS rel',
                                          (self.schema + '.' + name,))[0]['rel'])
        self.db.execute("UPDATE work_items SET migration=9123 WHERE id=%s", (self.lot,))
        self.assertEqual(self.db.query(
            'SELECT number FROM %s.work_migration_reservations WHERE work_item_id=%%s'
            % db_mod.quote_ident(physical), (self.lot,))[0]['number'], 9123)
        other = work.add(self.db, title='conflicting slot', actor='human:alice', cfg=self.cfg)['id']
        with self.assertRaisesRegex(db_mod.DbError, 'already reserved'):
            self.db.execute('INSERT INTO %s.work_migration_reservations '
                            "(work_item_id,slot,number,project,target_repo,target_branch,status) "
                            "VALUES (%%s,'default',9123,'','github.com/acme/demo','main','reserved')"
                            % db_mod.quote_ident(physical), (other,))
        self.db.execute("UPDATE work_items SET reviewers=ARRAY['reviewer'] WHERE id=%s", (self.lot,))
        self.db.execute("UPDATE work_items SET reviewers=ARRAY[]::text[] WHERE id=%s", (self.lot,))
        with self.assertRaisesRegex(db_mod.DbError, 'was withdrawn'):
            self.db.execute("INSERT INTO work_item_milestones (work_item_id,kind,actor) VALUES (%s,'verdict','reviewer')", (self.lot,))
        rollback = (Path(support.REPO) / 'src/ameesh/modules/dev/migrations/rollback/0002_remove_compatibility_views.sql').read_text()
        self.db.script(rollback)
        self.assertEqual(definitions(), original)
        self.assertEqual(self.db.query("SELECT version FROM module_schema_migrations WHERE module='dev' ORDER BY version"), [{'version': 1}])
        self.assertEqual([m.version for m in migrations.migrate(self.db, modules=modules)], [2])
        self.db.script(rollback)
        self.db.script(ROLLBACK)
