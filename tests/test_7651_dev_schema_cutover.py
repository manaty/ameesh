# SPDX-License-Identifier: AGPL-3.0-only
"""P2: populated isolated P1 copy -> dev/0001 -> operator rollback -> P2.

No shared mesh database is migrated. Every table's rows, OID, foreign keys,
ACL and owned sequence state are compared, beyond mere row counts.
"""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from contextlib import redirect_stdout, redirect_stderr
import io
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

from ameesh import db as db_mod, migrations, work
from ameesh.modules import registry, dev
from ameesh.modules.dev.tables import TABLES
from . import support, gabarit
from .role_acl import serialize_role_acl

FOLDER = Path(support.REPO) / 'src/ameesh/modules/dev/migrations'
ROLLBACK = (FOLDER / 'rollback/0001_module_schema_cutover.sql').read_text()


def cutover_modules():
    """Activation ONLY for isolated-copy proofs; runtime remains code-only."""
    with mock.patch.object(dev, 'MIGRATIONS', 'migrations'):
        found = registry.discover()
    return replace(found, modules=tuple(
        replace(module, migrations=tuple(m for m in module.migrations if m.version == 1))
        if module.name == 'dev' else module for module in found.modules))


class CutoverDeclarationTest(unittest.TestCase):
    def test_non_additive_and_packaged_separately_from_rollback(self):
        found = cutover_modules().get('dev')
        self.assertEqual([m.label for m in found.migrations],
                         ['dev/0001_module_schema_cutover'])
        self.assertTrue(migrations.non_additive(found.migrations[0]))
        self.assertEqual(migrations.module_sql_problems(found.migrations[0]), [])
        self.assertEqual(len(TABLES), 39)
        for table in TABLES:
            self.assertIn("'%s'" % table, found.migrations[0].sql)
            self.assertIn("'%s'" % table, ROLLBACK)
        self.assertNotRegex(ROLLBACK, r'(?i)DROP\s+(?:SCHEMA|VIEW)\s+[^;]+\s+CASCADE')


@support.needs_builtin_modules
class DevCutoverTest(support.PgTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        serialize_role_acl(cls)

    def setUp(self):
        super().setUp()
        self.db.execute('TRUNCATE ' + ','.join(TABLES) + ' RESTART IDENTITY CASCADE')
        self.physical = migrations.schema_name(self.schema, 'dev')
        self.addCleanup(self.restore)

    def restore(self):
        if self.db.query("SELECT EXISTS (SELECT FROM module_schemas WHERE module='dev') AS ok")[0]['ok']:
            self.db.script(ROLLBACK)

    def upgrade(self):
        result = migrations.migrate(self.db, modules=cutover_modules())
        self.assertEqual([m.label for m in result], ['dev/0001_module_schema_cutover'])
        return result

    def populate(self):
        self.lot = work.add(self.db, title='fixture cutover', actor='human:alice', cfg=self.cfg)['id']
        self.db.execute("INSERT INTO ci_runs (repo, run_id, head_sha, branch, work_item_id)"
                        " VALUES ('acme/demo', 11, %s, 'main', %s)", ('a' * 40, self.lot))
        self.db.execute("INSERT INTO provenance_receipts (subject, sha, actor, session, turn, harness, work_item_id)"
                        " VALUES (%s, %s, 'alice', 'session', 'turn', 'codex', %s)",
                        ('commit:' + 'a' * 40, 'a' * 40, self.lot))
        self.db.execute("INSERT INTO ci_disk_state (repo,host,volume) VALUES ('acme/demo','fixture-host','data')")
        self.db.execute("UPDATE work_items SET migration=9122, migration_repo='acme/demo',"
                        " branch_target='main' WHERE id=%s", (self.lot,))
        self.db.execute("SELECT nextval('merge_queue_generation_seq')")

    def snapshot(self, schema):
        tables = {}
        for name in TABLES:
            qualified = '%s.%s' % (db_mod.quote_ident(schema), db_mod.quote_ident(name))
            rows = self.db.query('SELECT to_jsonb(t)::text AS value FROM %s t ORDER BY 1' % qualified)
            catalog = self.db.query(
                'SELECT c.oid::bigint AS oid, c.relowner::bigint AS owner, c.relacl::text AS acl,'
                ' (SELECT jsonb_agg(jsonb_build_array(oid,contype,conrelid,confrelid,conkey,confkey) ORDER BY oid)::text FROM pg_constraint'
                ' WHERE conrelid=c.oid) AS constraints'
                ' FROM pg_class c WHERE c.oid=to_regclass(%s)', (qualified,))[0]
            tables[name] = (rows, catalog)
        return tables

    def owned_sequence(self, schema):
        name = self.db.query('SELECT pg_get_serial_sequence(%s, %s) AS name',
                             (schema + '.work_migration_reservations', 'id'))[0]['name']
        oid = self.db.query('SELECT to_regclass(%s)::oid::bigint AS oid', (name,))[0]['oid']
        return oid, self.db.query('SELECT last_value, is_called FROM ' + name)

    def test_round_trip_preserves_data_oids_acls_and_sequences(self):
        self.populate()
        before = self.snapshot(self.schema)
        owned = self.owned_sequence(self.schema)
        guard = self.db.query("SELECT pg_get_functiondef('ameesh_limited_login_guard()'::regprocedure) AS s")[0]['s']
        seq = self.db.query("SELECT last_value, is_called FROM merge_queue_generation_seq")
        self.upgrade()
        self.assertEqual(self.snapshot(self.physical), before)
        self.assertEqual(self.owned_sequence(self.physical), owned)
        for name, (rows, _) in before.items():
            self.assertEqual(self.db.query('SELECT to_jsonb(t)::text AS value FROM %s t ORDER BY 1' % name), rows)
        for name in TABLES:
            self.assertEqual(self.db.query('SELECT relkind FROM pg_class WHERE oid=to_regclass(%s)',
                                           (self.schema + '.' + name,))[0]['relkind'], 'v')
        self.assertEqual(self.db.query("SELECT last_value, is_called FROM merge_queue_generation_seq"), seq)
        self.assertEqual(migrations.migrate(self.db, modules=cutover_modules()), [])
        # The same old unqualified writer works after cutover, including upsert.
        self.db.execute("INSERT INTO ci_runs (repo, run_id, head_sha, branch)"
                        " VALUES ('acme/demo',11,%s,'main') ON CONFLICT (repo,run_id)"
                        " DO UPDATE SET head_sha=excluded.head_sha", ('b' * 40,))
        self.db.execute("UPDATE ci_runs SET head_sha=%s WHERE run_id=11", ('a' * 40,))
        self.db.script(ROLLBACK)
        self.assertEqual(self.snapshot(self.schema), before)
        self.assertEqual(self.owned_sequence(self.schema), owned)
        self.assertEqual(self.db.query("SELECT last_value, is_called FROM merge_queue_generation_seq"), seq)
        self.assertEqual(self.db.query("SELECT pg_get_functiondef('ameesh_limited_login_guard()'::regprocedure) AS s")[0]['s'], guard)
        self.assertEqual(self.db.query("SELECT * FROM module_schema_migrations WHERE module='dev'"), [])
        self.assertEqual(self.db.query("SELECT * FROM module_schema_non_additive_migrations WHERE module='dev'"), [])
        self.upgrade()
        self.assertEqual(self.snapshot(self.physical), before)
        self.assertEqual(self.owned_sequence(self.physical), owned)

    def test_backup_restored_copy_upgrades_and_rolls_back_with_data(self):
        self.populate()
        dump = gabarit._dump(self.db, self.schema)
        if dump is None:
            self.skipTest('pg_dump matching the test server is unavailable')
        backup = Path(self.tmp) / 'p1-backup.sql'
        backup.touch(mode=0o600)
        backup.write_text(dump)
        clone_schema = self.schema + '_copy'
        clone = db_mod.connect(replace(self.cfg, schema=clone_schema))
        try:
            gabarit.cloner(clone, clone_schema, self.schema, backup.read_text())
            self.assertEqual(clone.query('SELECT count(*) AS n FROM ci_runs')[0]['n'], 1)
            self.assertEqual(clone.query('SELECT count(*) AS n FROM provenance_receipts')[0]['n'], 1)
            physical = migrations.schema_name(clone_schema, 'dev')
            def content(schema):
                return {name: clone.query('SELECT to_jsonb(t)::text AS value FROM %s.%s t ORDER BY 1'
                                          % (schema, name)) for name in TABLES}
            before = content(clone_schema)
            migrations.migrate(clone, modules=cutover_modules())
            self.assertEqual(content(physical), before)
            clone.script(ROLLBACK)
            self.assertEqual(content(clone_schema), before)
            self.assertEqual(clone.query("SELECT * FROM module_schemas WHERE module='dev'"), [])
        finally:
            clone.execute('DROP SCHEMA IF EXISTS %s CASCADE' % migrations.schema_name(clone_schema, 'dev'))
            clone.execute('DROP SCHEMA IF EXISTS %s CASCADE' % clone_schema)
            clone.close()

    def test_core_migration_slot_trigger_and_module_storage_after_cutover(self):
        self.populate()
        self.upgrade()
        self.db.execute("UPDATE work_items SET migration=9123, migration_repo='acme/demo',"
                        " branch_target='main' WHERE id=%s", (self.lot,))
        rows = self.db.query('SELECT number FROM work_migration_reservations WHERE work_item_id=%s', (self.lot,))
        self.assertEqual(rows[0]['number'], 9123)
        # New connection, as required when executors restart after the move.
        fresh = db_mod.connect(self.cfg)
        try:
            from ameesh import storage
            port = storage.of(fresh).work
            request = port.train_requalify_request('demo', 'a'*40, 'b'*40, ['review:fixture'], self.lot, 'alice')
            self.assertIsNotNone(request)
            self.assertEqual(len(port.train_requalify_requests('demo')), 1)
            self.assertEqual(port.train_requalify_resolve(request['id'], 'applied', actor='host')['state'], 'applied')
        finally:
            fresh.close()

    def test_rollback_refuses_later_migration_without_moving_any_table(self):
        self.upgrade()
        self.db.execute("INSERT INTO module_schema_migrations (module,version,name,checksum)"
                        " VALUES ('dev',2,'fixture','fixture')")
        try:
            with self.assertRaisesRegex(db_mod.DbError, 'later migrations'):
                self.db.script(ROLLBACK)
            self.assertEqual(self.db.query('SELECT relkind FROM pg_class WHERE oid=to_regclass(%s)',
                                           (self.physical + '.ci_runs',))[0]['relkind'], 'r')
        finally:
            self.db.execute("DELETE FROM module_schema_migrations WHERE module='dev' AND version=2")

    def test_code_only_release_keeps_staging_preflight_open(self):
        self.assertEqual(registry.discover().get('dev').migrations, ())
        self.assertEqual(migrations.migrate(self.db, additive_only=True, modules=registry.discover()), [])
        self.assertEqual(self.db.query("SELECT * FROM module_schemas WHERE module='dev'"), [])

    def test_code_only_cli_and_restarted_runner_keep_p1_schema(self):
        """Exercise the staging migration commands and a fresh executor on a copy."""
        self.assert_code_only_cli_and_restarted_runner()

    def assert_code_only_cli_and_restarted_runner(self):
        for args in (("migrate", "--dry-run", "--additive-only"),
                     ("migrate", "--additive-only"), ("migrate",),
                     ("doctor", "--probe")):
            result = self.mesh(*args, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runner = self.runner("--once", "--migrate", "--agents", "nobody-7651", timeout=180)
        self.assertIn(runner.returncode, (0, 3), runner.stdout + runner.stderr)
        self.assertIn("no work available", runner.stdout + runner.stderr)
        self.assertEqual(self.db.query("SELECT * FROM module_schemas WHERE module='dev'"), [])
        self.assertEqual(self.db.query(
            "SELECT relkind FROM pg_class WHERE oid='work_items'::regclass")[0]['relkind'], 'r')

    def test_cutoff_and_staging_needed_keep_p2_dormant_on_copy(self):
        """Real release routing and migration gates; remote hosts are replaced by a copy.

        The release fixture supplies a local Git origin, canon and green CI.
        Only the host deployment boundary is replaced: it runs the real
        preflight/CLI and starts a fresh executor against this isolated P1 DB.
        """
        from ameesh import mechanics, release_cutoff as rc, staging_pending
        from tests.test_2448_release_cutoff import ReleaseFixture, _paris
        from tests.test_3845_paquet_horaire import ph

        fixture = ReleaseFixture()
        # The Git fixture changes HOME; keep the test DB's ordinary libpq
        # credential path, without copying or reading its contents.
        pgpass = mock.patch.dict(os.environ, {
            'PGPASSFILE': os.environ.get('PGPASSFILE', str(Path.home() / '.pgpass'))})
        pgpass.start()
        self.addCleanup(pgpass.stop)
        repo = str(support.REPO)
        # #8017 : dans l'invité de prévalidation, l'arbre appartient à un autre
        # UID que le job et le HOME de la fixture n'a pas de `safe.directory` :
        # Git refuse le dépôt. Ce dépôt seul (jamais `*`) est déclaré sûr par
        # `GIT_CONFIG_*`, hérité par les sous-processus.
        count = int(os.environ.get('GIT_CONFIG_COUNT') or 0)
        safe = mock.patch.dict(os.environ, {
            'GIT_CONFIG_COUNT': str(count + 1),
            'GIT_CONFIG_KEY_%d' % count: 'safe.directory',
            'GIT_CONFIG_VALUE_%d' % count: repo})
        safe.start()
        self.addCleanup(safe.stop)
        # Restore the fixture's environment before removing its safe.directory.
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        sha = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD'],
                                      text=True).strip()
        before = self.snapshot(self.schema)
        calls = []

        def stage_copy(project, release_sha, included, state_dir, db=None):
            gate = subprocess.run(
                [sys.executable, '-m', 'ameesh.staging_migrations_preflight',
                 '--repo', repo, '--sha', sha, '--project', 'ameesh'],
                env=self.env(), cwd=self.tmp, capture_output=True, text=True,
                timeout=support.scaled(180))
            self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
            self.assert_code_only_cli_and_restarted_runner()
            self.assertEqual(self.snapshot(self.schema), before)
            calls.append(release_sha)
            return {'exit': 0}

        # The hourly packet tick has its own CLI dry-run before the cutoff.
        class Shell:
            def run(shell, argv, **kwargs):
                result = subprocess.run(argv, env=self.env(), cwd=self.tmp,
                                        capture_output=True, text=True, timeout=180)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return ph.Result(result.returncode, result.stdout, result.stderr)

        ph.align_schema(SimpleNamespace(repo=Path(repo), python=sys.executable,
                                        shell=Shell(), dry_run=False))
        with mock.patch.object(rc, 'run_staging', side_effect=stage_copy) as stage:
            report = fixture.execute()
            self.assertEqual(report['result'], 'released', report)
            stage.assert_called_once()

        # Same real _staging_step/maybe_staging path, outside a cutoff.
        needed = {'result': 'needed', 'candidate': fixture.head,
                  'candidate_ready': True, 'regressions': {},
                  'candidate_unblocks': [1, 2, 3],
                  'waiting': [{'lot': n, 'blocker': 7651} for n in (1, 2, 3)]}
        rules = SimpleNamespace(staging=mechanics.Staging(3, 1200, 600),
                                next_cutoff=lambda now: _paris(23),
                                next_packet=lambda now: _paris(23))
        port = SimpleNamespace(work=SimpleNamespace(
            land_lock=mock.Mock(return_value=7), land_unlock=mock.Mock()))
        with mock.patch.object(staging_pending.storage, 'of', return_value=port), \
             mock.patch('ameesh.work.corrector', return_value='pilot'), \
             mock.patch.object(staging_pending, 'inspect', return_value=needed), \
             mock.patch.object(staging_pending, 'regressions', return_value={}), \
             mock.patch.object(rc, 'compiled_rules', return_value=(rules, None)), \
             mock.patch.object(rc, 'run_staging', side_effect=stage_copy) as stage:
            report = staging_pending.apply(fixture.cfg, None, fixture.project,
                                           now=_paris(22, 30))
            self.assertEqual(report['result'], 'staged', report)
            stage.assert_called_once()
        self.assertEqual(len(calls), 2)

    def test_additive_only_does_not_even_create_the_module_schema(self):
        from ameesh import cli
        output = io.StringIO()
        with mock.patch.object(migrations, 'installed_modules',
                               return_value=cutover_modules().modules), \
             redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(cli.cmd_migrate(self.cfg, dry_run=True, additive_only=True), 3)
            self.assertEqual(cli.cmd_migrate(self.cfg, additive_only=True), 3)
        self.assertIn('dev/0001_module_schema_cutover: NOT additive', output.getvalue())
        self.assertIn('nothing applied', output.getvalue())
        with self.assertRaises(migrations.NonAdditiveMigration):
            migrations.migrate(self.db, additive_only=True, modules=cutover_modules())
        self.assertEqual(self.db.query("SELECT * FROM module_schemas WHERE module='dev'"), [])
        self.assertFalse(self.db.query('SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s) AS ok',
                                      (self.physical,))[0]['ok'])

    def test_rls_preflight_is_atomic(self):
        self.db.execute('ALTER TABLE ci_runs ENABLE ROW LEVEL SECURITY')
        self.addCleanup(self.db.execute, 'ALTER TABLE ci_runs DISABLE ROW LEVEL SECURITY')
        with self.assertRaisesRegex(db_mod.DbError, 'unexpected RLS'):
            self.upgrade()
        self.assertEqual(self.db.query("SELECT * FROM module_schemas WHERE module='dev'"), [])
        self.assertEqual(self.db.query("SELECT relkind FROM pg_class WHERE oid='ci_runs'::regclass")[0]['relkind'], 'r')

    def test_compatibility_view_dml_and_core_foreign_key(self):
        self.populate()
        self.upgrade()
        with self.db.transaction() as tx:
            self.assertEqual(len(tx.query('SELECT * FROM ci_runs WHERE run_id=11 FOR UPDATE')), 1)
            rows = tx.query("INSERT INTO ci_runs (repo,run_id,head_sha,branch)"
                            " VALUES ('acme/demo',12,%s,'main') RETURNING run_id", ('c' * 40,))
            self.assertEqual(rows[0]['run_id'], 12)
            tx.execute("UPDATE ci_runs c SET branch='feature' FROM work_items w WHERE c.work_item_id=w.id")
            tx.execute('DELETE FROM ci_runs c USING work_items w WHERE c.work_item_id=w.id')
        with self.assertRaises(db_mod.DbError):
            self.db.execute("INSERT INTO provenance_receipts (subject,sha,actor,session,turn,harness,work_item_id)"
                            " VALUES (%s,%s,'alice','s','t','codex',-1)", ('commit:' + 'b'*40, 'b'*40))

    def test_relation_grant_without_core_usage_does_not_open_module_schema(self):
        role = 't_cutover_' + uuid.uuid4().hex[:10]
        self.db.execute('CREATE ROLE %s NOLOGIN' % role)
        try:
            self.db.execute('GRANT SELECT ON ci_runs TO %s' % role)
            self.assertFalse(self.db.query("SELECT has_schema_privilege(%s,%s,'USAGE') AS ok",
                                           (role, self.schema))[0]['ok'])
            self.upgrade()
            self.assertFalse(self.db.query("SELECT has_schema_privilege(%s,%s,'USAGE') AS ok",
                                           (role, self.physical))[0]['ok'])
        finally:
            self.restore()
            self.db.execute('DROP OWNED BY %s' % role)
            self.db.execute('DROP ROLE %s' % role)

    def test_column_acl_and_limited_login_receipt_through_view(self):
        role = 't_cutover_' + uuid.uuid4().hex[:10]
        self.db.execute('CREATE ROLE %s NOLOGIN' % role)
        def drop_role():
            self.restore()
            self.db.execute('DROP OWNED BY %s' % role)
            self.db.execute('DROP ROLE %s' % role)
        self.addCleanup(drop_role)
        marker_exists = self.db.query("SELECT to_regrole('ameesh_login_limite') IS NOT NULL AS ok")[0]['ok']
        if not marker_exists:
            self.db.execute('CREATE ROLE ameesh_login_limite NOLOGIN')
            self.addCleanup(self.db.execute, 'DROP ROLE ameesh_login_limite')
        self.db.execute('GRANT ameesh_login_limite TO %s' % role)
        self.db.execute('GRANT USAGE ON SCHEMA %s TO %s' % (self.schema, role))
        self.db.execute('GRANT SELECT ON limited_logins TO %s' % role)
        self.db.execute('GRANT INSERT (subject,sha,actor,session,turn,harness), SELECT (subject)'
                        ' ON provenance_receipts TO %s' % role)
        self.db.execute('GRANT SELECT (subject) ON provenance_receipts TO %s WITH GRANT OPTION' % role)
        # These defaults apply to the NEW view, not to the existing table.
        # Copying ACLs must remove this accidental whole-table SELECT grant.
        self.db.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA %s GRANT SELECT ON TABLES TO %s'
                        % (self.schema, role))
        self.db.execute("INSERT INTO limited_logins (rolname,agent,host) VALUES (%s,'alice','host')", (role,))
        self.upgrade()
        with self.db.transaction() as tx:
            tx.execute('SET LOCAL SESSION AUTHORIZATION %s' % role)
            tx.execute("INSERT INTO provenance_receipts (subject,sha,actor,session,turn,harness)"
                       " VALUES (%s,%s,'alice','s','t','codex')", ('commit:' + 'd'*40, 'd'*40))
            self.assertEqual(len(tx.query('SELECT subject FROM provenance_receipts')), 1)
        for statement in [
                "INSERT INTO provenance_receipts (subject,sha,actor,session,turn,harness)"
                " VALUES ('commit:%s','%s','bob','s','t','codex')" % ('e'*40, 'e'*40),
                'SELECT sha FROM provenance_receipts']:
            with self.assertRaises(db_mod.DbError):
                with self.db.transaction() as tx:
                    tx.execute('SET LOCAL SESSION AUTHORIZATION %s' % role)
                    tx.execute(statement)
        self.db.script(ROLLBACK)
        with self.db.transaction() as tx:
            tx.execute('SET LOCAL SESSION AUTHORIZATION %s' % role)
            self.assertEqual(len(tx.query('SELECT subject FROM provenance_receipts')), 1)


if __name__ == '__main__':
    unittest.main()
