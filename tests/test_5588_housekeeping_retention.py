# SPDX-License-Identifier: AGPL-3.0-only
"""#5588 : rétention indexée, indépendante des écritures du journal."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import db as db_mod, housekeeping as hk, storage
from ameesh.storage.postgres.menage import Housekeeping
from .support import PgTestCase


class _Cfg:
    """The fields of a host config the housekeeping pass reads (no DB)."""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.host = 'retention-outage'
        self.dsn = 'host=db dbname=ameesh'
        self.schema = 'test'


class RetentionCallsTest(unittest.TestCase):
    def test_each_entry_is_written_without_purging(self):
        db = mock.Mock()
        journal = Housekeeping(db)
        for _ in range(3):
            self.assertEqual(journal.log('h', [{'kind': 'tmp'}, {'kind': 'cache'}],
                                         actor='test'), 2)
        self.assertEqual(journal.log('h', [], actor='test'), 0)
        self.assertEqual(db.query.call_count, 6)
        db.execute.assert_not_called()

    def test_one_purge_at_the_start_of_an_effective_pass(self):
        journal = mock.Mock()
        # Stop before filesystem work, just after the retention boundary.
        for dry_run, disabled, expected in ((False, False, 1),
                                            (True, False, 0),
                                            (False, True, 0)):
            with self.subTest(dry_run=dry_run, disabled=disabled):
                journal.reset_mock()
                with mock.patch.object(hk, 'disabled', return_value=disabled), \
                     mock.patch.object(hk.storage, 'of', return_value=mock.Mock(housekeeping=journal)), \
                     mock.patch.object(hk, 'tmp_root', side_effect=RuntimeError('stop')):
                    if disabled:
                        hk._run_pass(None, None, None, host='h', actor='test',
                                     dry_run=dry_run, now=1.0)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'stop'):
                            hk._run_pass(None, None, None, host='h', actor='test',
                                         dry_run=dry_run, now=1.0)
                self.assertEqual(journal.purge_retention.call_count, expected)


class RetentionSqlTest(PgTestCase):
    def test_migration_adds_the_time_index(self):
        rows = self.db.query(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s"
            " AND indexname = 'housekeeping_log_retention_utc_idx'", (self.schema,))
        self.assertEqual(len(rows), 1)
        self.assertIn("at AT TIME ZONE 'UTC'", rows[0]['indexdef'])

    def test_retention_keeps_recent_rows_and_log_does_not_delete_old_rows(self):
        self.db.execute('DELETE FROM housekeeping_log')
        self.db.execute("INSERT INTO housekeeping_log (host, at, kind, action) VALUES"
                        " ('old', now() - interval '31 days', 'tmp', 'deleted'),"
                        " ('recent', now() - interval '29 days', 'tmp', 'deleted')")
        journal = storage.of(self.db).housekeeping
        journal.log('h', [{'kind': 'tmp', 'action': 'deleted'}], actor='test')
        self.assertEqual(len(self.db.query('SELECT id FROM housekeeping_log')), 3)
        journal.purge_retention()
        self.assertEqual({r['host'] for r in self.db.query('SELECT host FROM housekeeping_log')},
                         {'recent', 'h'})

    def test_retention_uses_same_instant_cutoff_in_non_utc_timezone(self):
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest('SET TIME ZONE needs a persistent session')
        original = self.db.query("SELECT current_setting('TimeZone') AS zone")[0]['zone']
        self.db.execute("SET TIME ZONE 'Pacific/Chatham'")
        try:
            self.db.execute('DELETE FROM housekeeping_log')
            self.db.execute(
                "INSERT INTO housekeeping_log (host, at, kind, action) VALUES"
                " ('expired', now() - interval '30 days' - interval '1 minute', 'tmp', 'deleted'),"
                " ('retained', now() - interval '30 days' + interval '1 minute', 'cache', 'deleted')")
            storage.of(self.db).housekeeping.purge_retention()
            self.assertEqual([r['host'] for r in self.db.query('SELECT host FROM housekeeping_log')],
                             ['retained'])
        finally:
            self.db.query("SELECT set_config('TimeZone', %s, false)", (original,))

    def test_empty_purge_uses_time_index_on_500000_rows(self):
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest('EXPLAIN is wrapped by the psql driver')
        self.db.execute('DELETE FROM housekeeping_log')
        # Fixture-only bulk load: the spelling trigger is irrelevant here;
        # inherited statement timeouts must not make host load a test failure.
        timeout = self.db.query("SELECT current_setting('statement_timeout') AS timeout")[0]['timeout']
        self.db.execute('ALTER TABLE housekeeping_log DISABLE TRIGGER ameesh_agent_spelling')
        try:
            db_mod.execute_without_statement_timeout(
                self.db,
                "INSERT INTO housekeeping_log (host, at, kind, action)"
                " SELECT 'h', now() - make_interval(secs => g), 'tmp', 'measured'"
                " FROM generate_series(1, 500000) g")
        finally:
            self.db.execute('ALTER TABLE housekeeping_log ENABLE TRIGGER ameesh_agent_spelling')
            self.db.query("SELECT set_config('statement_timeout', %s, false)", (timeout,))
        self.db.execute('ANALYZE housekeeping_log')
        # Capture the actual production SQL rather than duplicate its predicate.
        capture = mock.Mock()
        Housekeeping(capture).purge_retention()
        sql, params = capture.execute.call_args.args
        rows = self.db.query('EXPLAIN (ANALYZE, FORMAT JSON) ' + sql, params)
        plan = rows[0]['QUERY PLAN']
        if isinstance(plan, str):
            plan = json.loads(plan)
        document = json.dumps(plan)
        self.assertIn('housekeeping_log_retention_utc_idx', document)
        self.assertIn('Index Scan', document)
        self.assertNotIn('housekeeping_log_kind_host_time_idx', document)
        self.assertNotIn('Seq Scan', document)
        # Server execution time excludes network and driver overhead. Report
        # the performance target without making host load a CI failure (§3).
        elapsed = plan[0]['Execution Time']
        print(f'housekeeping retention: {elapsed:.3f} ms / target < 5 ms (500000 rows)')


class RetentionOutageTest(PgTestCase):
    """#5589 : une panne de base à la rétention ne coupe pas le ménage local."""

    def test_a_retention_failure_does_not_stop_the_local_cleanup(self):
        base = tempfile.mkdtemp(prefix='ameesh-5589-')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        cfg = _Cfg(os.path.join(base, 'state'))
        policy = hk.Policy(worktrees=False, deps=False, artifacts=(),
                           docker_volumes=False, docker_images=False,
                           orphan_idle=0.0)
        troot = hk.tmp_root(cfg, policy)
        hk._ensure_root(troot)
        doomed = os.path.join(troot, 'gone-agent')
        os.makedirs(doomed)
        with open(os.path.join(doomed, 'work.txt'), 'w', encoding='utf-8') as fh:
            fh.write('x' * 64)
        hk._write_owner(troot, 'gone-agent', hk.mesh_key(cfg), hk.ORIGIN_CREATED)
        with mock.patch.object(
                Housekeeping, 'purge_retention',
                side_effect=db_mod.Unavailable(
                    'could not connect to server: Connection refused')):
            report = hk._run_pass(cfg, self.db, policy, host='retention-outage',
                                  actor='test', artifact_scan=False,
                                  now=time.time() + 1.0)
        # The local phase ran after the failed purge: the orphan directory is gone.
        self.assertFalse(os.path.exists(doomed))
        self.assertTrue(any('retention: database unreachable' in note
                            and 'local cleanup continues' in note
                            for note in report['skipped']), report['skipped'])
        self.assertTrue(any(line.get('kind') == 'tmp'
                            and line.get('action') == 'deleted'
                            and line.get('path') == doomed
                            for line in report['entries']), report['entries'])

