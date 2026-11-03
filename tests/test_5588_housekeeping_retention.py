# SPDX-License-Identifier: AGPL-3.0-only
"""#5588 : rétention indexée, indépendante des écritures du journal."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import db as db_mod, housekeeping as hk, storage
from ameesh.storage.postgres.menage import Housekeeping
from .support import PgTestCase


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
            " AND indexname = 'housekeeping_log_at_idx'", (self.schema,))
        self.assertEqual(len(rows), 1)
        self.assertIn('(at)', rows[0]['indexdef'])

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

    def test_empty_purge_uses_time_index_on_500000_rows(self):
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest('EXPLAIN is wrapped by the psql driver')
        self.db.execute('DELETE FROM housekeeping_log')
        self.db.execute("INSERT INTO housekeeping_log (host, at, kind, action)"
                        " SELECT 'h', now() - make_interval(secs => g), 'tmp', 'measured'"
                        " FROM generate_series(1, 500000) g")
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
        self.assertIn('housekeeping_log_at_idx', document)
        self.assertIn('Index Scan', document)
        self.assertNotIn('housekeeping_log_kind_host_time_idx', document)
        self.assertNotIn('Seq Scan', document)
        # Server execution time excludes network and driver overhead. Report
        # the performance target without making host load a CI failure (§3).
        elapsed = plan[0]['Execution Time']
        print(f'housekeeping retention: {elapsed:.3f} ms / target < 5 ms (500000 rows)')
