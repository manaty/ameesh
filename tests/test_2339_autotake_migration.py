# SPDX-License-Identifier: AGPL-3.0-only
"""Additive migration compatibility for a warmed runner connection."""
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest import mock

from ameesh import db as db_mod, runner, storage
from .support import PgTestCase

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'auto_take_probe', ROOT / 'deploy/mise-a-jour/auto-take-probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class LogTest(unittest.TestCase):
    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_failed_take_keeps_bounded_sql_message_and_continues_sweep(self):
        state = mock.Mock()
        state.workers = {}
        state.lock = threading.Lock()
        state.power_hold.is_set.return_value = False
        state.draining.is_set.return_value = False
        state.restart_for_stale = False
        state.host_gate = None
        state.take_interval = 1
        state._take_next = 0
        # L165 (EP2) : `sweep` publie les scores de rôles puis purge les
        # libertés d'élection observées ; la doublure n'en a aucune.
        state.role_evaluation_once = lambda: None
        state._election_free_since = {}
        state.take_pass.side_effect = db_mod.DbError(
            'column x does not exist\nDETAIL: ' + 'x' * 500, sqlstate='42P01')
        with mock.patch.object(runner, 'log_async') as log, \
                mock.patch.object(runner.registry, 'reap', return_value=[]), \
                mock.patch.object(runner.registry, 'claimable', return_value=[]) as claim:
            runner.Runner.sweep(state)
        message = log.call_args.args[0]
        self.assertIn('DbError', message)
        self.assertIn('column x does not exist', message)
        self.assertIn('42P01', message)
        self.assertNotIn('DETAIL', message)
        self.assertNotIn('x' * 50, message)
        self.assertTrue(message.endswith('; runner continues'))
        self.assertNotIn('\n', message)
        self.assertLess(len(message), 380)
        claim.assert_called_once()


class BriefTest(unittest.TestCase):
    """#2358 : le journal garde le message principal, jamais le DETAIL."""

    def test_psycopg_primary_message_wins_over_detail(self):
        class Diagnostic:
            message_primary = 'cached plan must not change result type'

        class Failure(Exception):
            diag = Diagnostic()
            sqlstate = '0A000'

            def __str__(self):
                return ('cached plan must not change result type\n'
                        'DETAIL: ' + 'x' * 500)

        self.assertEqual(
            db_mod.brief(Failure()),
            'cached plan must not change result type (SQLSTATE 0A000)')
        self.assertEqual(
            db_mod._one_line(Failure()), 'cached plan must not change result type')

    def test_detail_is_cut_without_driver_diagnostics(self):
        exc = db_mod.DbError('column x does not exist\nDETAIL: secret value',
                             sqlstate='42703')
        self.assertEqual(db_mod.brief(exc),
                         'column x does not exist (SQLSTATE 42703)')
        self.assertNotIn('secret', db_mod.explain(exc, 300))


class CompatibilityTest(PgTestCase):
    def migrate_after_warmup(self, *, prepare=False):
        # Every test has its own schema. Removing the additive objects models
        # 1.11.5; another connection then applies the actual 1.11.6 migrations.
        old = self.connect()
        self.addCleanup(old.close)
        if prepare:
            if not hasattr(old, 'conn'):
                self.skipTest('named prepared plans are specific to psycopg')
            old.conn.prepare_threshold = 5  # pre-fix psycopg default
        self.drop_work_item_column(
            "reviewers", lambda: self.db.script(
                (ROOT / 'src/ameesh/migrations/0456_work_item_reviewers.sql').read_text()))
        self.db.execute('DROP TABLE review_catch_up')
        work = storage.of(old).work
        for _ in range(8):
            work.review_proposals(1, proposal_prefix='probe')
            work.unassigned_lots(200)
        for name in ('0438_review_catch_up.sql', '0456_work_item_reviewers.sql'):
            self.db.script((ROOT / 'src/ameesh/migrations' / name).read_text())
        return work

    def test_warmed_connection_survives_additive_migrations(self):
        work = self.migrate_after_warmup()
        self.assertEqual(work.review_proposals(1, proposal_prefix='probe'), [])
        self.assertEqual(work.unassigned_lots(200), [])
        probe.probe(self.cfg)

    def test_pre_fix_preparation_reproduces_0456_error(self):
        work = self.migrate_after_warmup(prepare=True)
        with self.assertRaises(db_mod.DbError) as raised:
            work.review_proposals(1, proposal_prefix='probe')
        self.assertEqual(raised.exception.sqlstate, '0A000')
        self.assertIn('cached plan must not change result type', str(raised.exception))

    def test_probe_connection_cannot_write(self):
        def attempt_write(state):
            self.assertTrue(state.dry_run)
            state.db.execute("INSERT INTO work_items(title) VALUES ('forbidden')")
        with mock.patch.object(runner.Runner, 'take_pass', attempt_write):
            with self.assertRaises(db_mod.DbError) as raised:
                probe.probe(self.cfg)
        self.assertEqual(raised.exception.sqlstate, '25006')
        self.assertEqual(self.db.query('SELECT id FROM work_items'), [])

    def test_probe_runs_the_installed_constructor(self):
        # #2359: take_pass may read any attribute its constructor sets; the
        # probe mirrors none of them (`suite_cap` stands for a new one).
        original = runner.Runner.take_pass
        seen = []

        def reads_constructor_state(state):
            seen.append((state.suite_cap is not None, bool(state.workers)))
            return original(state)
        with mock.patch.object(runner.Runner, 'take_pass', reads_constructor_state):
            probe.probe(self.cfg)
        self.assertEqual(seen, [(True, True)])

    def test_setup_failure_is_not_reported_as_incompatible(self):
        with mock.patch.object(runner, 'Runner', side_effect=TypeError('take')):
            with self.assertRaises(probe.ProbeUnusable) as raised:
                probe.probe(self.cfg)
        self.assertIn('TypeError: take', str(raised.exception))
