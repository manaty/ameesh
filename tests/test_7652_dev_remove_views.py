# SPDX-License-Identifier: AGPL-3.0-only
"""P3 on an isolated populated schema, including P2 rollback and re-upgrade."""
from dataclasses import replace
from pathlib import Path
import uuid

from ameesh import db as db_mod, migrations, work
from ameesh.modules import dev
from ameesh.modules.dev.storage import schema as dev_schema
from ameesh.modules.dev.tables import TABLES
from . import support
from . import test_7651_dev_schema_cutover as p2
from .role_acl import serialize_role_acl

P3_ROLLBACK = (Path(support.REPO) /
               "src/ameesh/modules/dev/pending/rollback/0002_remove_compatibility_views.sql").read_text()


def removal_modules():
    # Explicit test-only P3 activation; production discovery sees P2 only.
    found = p2.cutover_modules()
    pending = migrations.discover(str(Path(dev.__file__).parent / 'pending'), module='dev')
    return replace(found, modules=tuple(
        replace(module, migrations=module.migrations + tuple(pending))
        if module.name == 'dev' else module for module in found.modules))


@support.needs_builtin_modules
class RemoveViewsTest(support.PgTestCase):
    populate = p2.DevCutoverTest.populate
    snapshot = p2.DevCutoverTest.snapshot
    owned_sequence = p2.DevCutoverTest.owned_sequence

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        serialize_role_acl(cls)

    def setUp(self):
        super().setUp()
        self.addCleanup(self.restore)

    def restore(self):
        versions = self.db.query("SELECT version FROM module_schema_migrations WHERE module='dev'")
        if any(row['version'] == 2 for row in versions):
            self.db.script(P3_ROLLBACK)
        if versions:
            self.db.script(p2.ROLLBACK)
        connection = dev_schema._connection(self.db)
        if hasattr(connection, '_ameesh_dev_schema'):
            delattr(connection, '_ameesh_dev_schema')

    def test_roundtrip_and_guards(self):
        self.db.execute('TRUNCATE ' + ','.join(TABLES) + ' RESTART IDENTITY CASCADE')
        self.populate()
        before = self.snapshot(self.schema)
        migrations.migrate(self.db, modules=p2.cutover_modules())
        # P2 requires stopped writers; simulate a fresh executor connection.
        connection = dev_schema._connection(self.db)
        if hasattr(connection, '_ameesh_dev_schema'):
            delattr(connection, '_ameesh_dev_schema')
        modules = removal_modules()
        functions = ('work_items_migration_slot_sync', 'work_migration_reservation_guard',
                     'ameesh_review_seats_guard', 'ameesh_review_verdict_guard')
        def definitions():
            return self.db.query('SELECT p.oid,p.proname,p.proowner,p.proacl::text,p.prosecdef,p.proconfig,pg_get_functiondef(p.oid) AS definition '
                                 'FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace '
                                 'WHERE n.nspname=%s AND p.proname = ANY(%s) ORDER BY p.proname',
                                 (self.schema, list(functions)))
        original = definitions()
        owned = self.owned_sequence(migrations.schema_name(self.schema, "dev"))
        self.assertEqual(len(original), 4)
        migration = migrations.migrate(self.db, modules=modules)
        self.assertEqual([m.version for m in migration], [2])
        physical = migrations.schema_name(self.schema, 'dev')
        self.assertEqual(self.snapshot(physical), before)
        self.assertEqual(self.owned_sequence(physical), owned)
        self.assertEqual([{k: v for k, v in row.items() if k != 'definition'} for row in definitions()],
                         [{k: v for k, v in row.items() if k != 'definition'} for row in original])
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
        rollback = P3_ROLLBACK
        self.db.script(rollback)
        self.assertEqual(definitions(), original)
        self.assertEqual(self.db.query("SELECT version FROM module_schema_migrations WHERE module='dev' ORDER BY version"), [{'version': 1}])
        self.assertEqual([m.version for m in migrations.migrate(self.db, modules=modules)], [2])
        # Test resets must target module tables after P2/P3, never a P2 view.
        dev_schema.ModuleDb(self.db).execute('TRUNCATE ci_runs RESTART IDENTITY CASCADE')
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM %s.ci_runs' % physical)[0]['n'], 0)
        self.db.script(rollback)
        self.db.script(p2.ROLLBACK)

    def test_column_acl_and_rollback_views_without_default_grant_leak(self):
        role = 't_remove_' + uuid.uuid4().hex[:10]
        self.db.execute('CREATE ROLE %s NOLOGIN' % role)
        def drop_role():
            self.restore()
            self.db.execute('DROP OWNED BY %s' % role)
            self.db.execute('DROP ROLE %s' % role)
        self.addCleanup(drop_role)
        self.db.execute('GRANT USAGE ON SCHEMA %s TO %s' % (self.schema, role))
        self.db.execute('GRANT SELECT (head_sha) ON ci_runs TO %s WITH GRANT OPTION' % role)
        self.db.execute('GRANT INSERT (repo,run_id,head_sha,branch) ON ci_runs TO %s' % role)
        migrations.migrate(self.db, modules=p2.cutover_modules())
        physical = migrations.schema_name(self.schema, 'dev')
        def views():
            return self.db.query(
                'SELECT c.relname,c.relowner,pg_get_viewdef(c.oid) AS definition, '
                '(SELECT jsonb_agg(a ORDER BY a.grantee,a.privilege_type) FROM aclexplode(c.relacl) a '
                "WHERE a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')) AS dml_acl, "
                '(SELECT jsonb_agg(jsonb_build_array(att.attname,a.grantee,a.privilege_type,a.is_grantable) '
                'ORDER BY att.attname,a.grantee,a.privilege_type) FROM pg_attribute att, '
                'LATERAL aclexplode(att.attacl) a WHERE att.attrelid=c.oid) AS column_acl '
                'FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace '
                "WHERE n.nspname=%s AND c.relkind='v' ORDER BY c.relname", (self.schema,))
        original = views()
        def exercise_access():
            with self.db.transaction() as tx:
                tx.execute('SET LOCAL SESSION AUTHORIZATION %s' % role)
                self.assertEqual(len(tx.query('SELECT head_sha FROM %s.ci_runs' % physical)), 1)
            for statement in ['SELECT repo FROM %s.ci_runs' % physical,
                              'DELETE FROM %s.ci_runs' % physical]:
                with self.assertRaises(db_mod.DbError):
                    with self.db.transaction() as tx:
                        tx.execute('SET LOCAL SESSION AUTHORIZATION %s' % role)
                        tx.execute(statement)
        self.db.execute("INSERT INTO ci_runs (repo,run_id,head_sha,branch) VALUES ('acl/demo',1,%s,'main')", ('a'*40,))
        before = self.snapshot(physical)
        exercise_access()
        migrations.migrate(self.db, modules=removal_modules())
        self.assertEqual(self.snapshot(physical), before)
        exercise_access()
        # New rollback views must discard grants inherited from default ACLs.
        self.db.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT ON TABLES TO %s'
                        % (self.schema, role))
        self.db.script(P3_ROLLBACK)
        self.assertEqual(views(), original)
        exercise_access()
        with self.db.transaction() as tx:
            tx.execute('SET LOCAL SESSION AUTHORIZATION %s' % role)
            self.assertEqual(len(tx.query('SELECT head_sha FROM ci_runs')), 1)
        with self.assertRaises(db_mod.DbError):
            with self.db.transaction() as tx:
                tx.execute('SET LOCAL SESSION AUTHORIZATION %s' % role)
                tx.execute('SELECT repo FROM ci_runs')

    def test_dependent_view_refuses_removal_atomically(self):
        migrations.migrate(self.db, modules=p2.cutover_modules())
        self.db.execute('CREATE VIEW p3_dependency AS SELECT run_id FROM ci_runs')
        try:
            original = self.db.query(
                'SELECT p.oid,p.proconfig,pg_get_functiondef(p.oid) AS definition FROM pg_proc p '
                'JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=%s ORDER BY p.oid',
                (self.schema,))
            with self.assertRaises(db_mod.DbError):
                migrations.migrate(self.db, modules=removal_modules())
            self.assertEqual(self.db.query(
                "SELECT version FROM module_schema_migrations WHERE module='dev'"), [{'version': 1}])
            for name in TABLES:
                self.assertEqual(self.db.query(
                    'SELECT relkind FROM pg_class WHERE oid=to_regclass(%s)',
                    (self.schema + '.' + name,))[0]['relkind'], 'v')
            self.assertEqual(self.db.query(
                'SELECT p.oid,p.proconfig,pg_get_functiondef(p.oid) AS definition FROM pg_proc p '
                'JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=%s ORDER BY p.oid',
                (self.schema,)), original)
        finally:
            self.db.execute('DROP VIEW p3_dependency')

    def test_p2_activation_keeps_views_until_separate_p3_activation(self):
        activated_p2 = p2.cutover_modules()
        self.assertEqual([m.version for m in activated_p2.get('dev').migrations], [1])
        self.assertEqual([m.version for m in migrations.migrate(self.db, modules=activated_p2)], [1])
        for name in TABLES:
            self.assertEqual(self.db.query(
                'SELECT relkind FROM pg_class WHERE oid=to_regclass(%s)',
                (self.schema + '.' + name,))[0]['relkind'], 'v')
        self.assertEqual(migrations.migrate(self.db, modules=activated_p2), [])
        self.assertEqual([m.version for m in migrations.migrate(self.db, modules=removal_modules())], [2])
        for name in TABLES:
            self.assertIsNone(self.db.query('SELECT to_regclass(%s) AS rel',
                                           (self.schema + '.' + name,))[0]['rel'])
