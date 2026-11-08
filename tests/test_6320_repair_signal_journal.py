# SPDX-License-Identifier: AGPL-3.0-only
"""Real SQL witnesses for U5: cursor atomicity, N-1 projection and privacy."""
from __future__ import annotations

import json

from ameesh import storage
from ameesh.repair_signals import Signal, from_record
from .support import PgTestCase


class RepairJournalTest(PgTestCase):
    def setUp(self):
        self.db.execute('DELETE FROM repair_signals')
        self.db.execute('DELETE FROM repair_signal_streams')
        self.db.execute('DELETE FROM repair_signal_refusals')
        self.api = storage.of(self.db).repair_signals

    def signal(self, n=1, **kw):
        return Signal(project='acme', environment='staging', source='inbox',
                      native_id=str(n), received_at='2026-11-08T12:00:00Z', **kw)

    def receive(self, entries, expected=None, next='one'):
        return self.api.receive('acme', 'staging', 'inbox', entries,
                                expected_cursor=expected, next_cursor=next)

    def page(self, **kw):
        return self.api.page('acme', 'staging', 'inbox', **kw)

    def test_duplicate_delivery_and_stale_cursor_preserve_settlement(self):
        signal = self.signal()
        self.assertTrue(self.receive([signal, signal]))
        self.assertTrue(self.api.settle(signal.id, 'lot', reference='lot:6320'))
        self.assertFalse(self.receive([self.signal(2)]))
        self.assertTrue(self.receive([self.signal(occurrence=4)], 'one', 'two'))
        self.assertTrue(self.receive([signal], 'two', 'three'))
        rows = self.page()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['signal']['occurrence'], 4)
        self.assertEqual(rows[0]['signal']['outcome'], 'lot')
        self.assertEqual(rows[0]['outcome_ref'], 'lot:6320')
        self.assertFalse(self.api.settle(signal.id, 'ignored'))

    def test_crash_before_commit_rolls_back_receipts_and_cursor(self):
        with self.assertRaisesRegex(RuntimeError, 'crash'):
            with self.db.transaction() as tx:
                self.assertTrue(storage.of(tx).repair_signals.receive(
                    'acme', 'staging', 'inbox', [self.signal()],
                    expected_cursor=None, next_cursor='one'))
                raise RuntimeError('crash')
        self.assertEqual(self.page(), [])
        self.assertEqual(self.api.state('acme', 'staging', 'inbox')['cursor'], None)
        self.assertTrue(self.receive([self.signal()]))
        # Lost response after commit: replay with the old cursor has no effect.
        self.assertFalse(self.receive([self.signal()]))
        self.assertEqual(len(self.page()), 1)

    def test_error_between_receipt_and_cursor_is_atomic(self):
        with self.assertRaises(Exception):
            with self.db.transaction() as tx:
                storage.of(tx).repair_signals.receive(
                    'acme', 'staging', 'inbox', [self.signal()],
                    expected_cursor=None, next_cursor='one')
                tx.execute('SELECT 1/0')
        self.assertEqual(self.page(), [])
        self.assertEqual(self.api.state('acme', 'staging', 'inbox')['position'], 0)

    def test_pagination_is_bounded_stable_and_isolated(self):
        self.receive([self.signal(n) for n in range(1, 6)])
        self.receive([self.signal(1)], 'one', 'two')
        first = self.page(limit=2)
        second = self.page(after=first[-1]['position'], limit=2)
        third = self.page(after=second[-1]['position'], limit=2)
        self.assertEqual([r['position'] for r in first+second+third], list(range(1, 6)))
        self.assertEqual(self.api.page('acme', 'production', 'inbox'), [])
        self.assertEqual(self.page(after=5), [])
        for kw in ({'limit': 0}, {'limit': 1001}, {'after': -1}):
            with self.assertRaises(ValueError):
                self.page(**kw)

    def test_n_minus_one_reader_ignores_additive_storage_fields(self):
        self.receive([self.signal()])
        self.db.execute("ALTER TABLE repair_signals ADD COLUMN future_column text DEFAULT 'private newer metadata'")
        # Simulates a newer writer's additive envelope metadata. The old U2
        # reader remains strict; only the storage projection knows to ignore it.
        self.db.execute("UPDATE repair_signals SET envelope=envelope || %s::jsonb",
                        (json.dumps({'future_field': 'not-for-the-old-reader'}),))
        from_record(self.page()[0]['signal'])
        self.assertNotIn('future_field', self.page()[0]['signal'])
        with self.assertRaises(ValueError):
            from_record({**self.signal().record(), 'future_field': 1})

    def test_invalid_batch_does_not_advance_and_refusal_is_bounded(self):
        bad = {**self.signal().record(), 'body': 'untrusted private instructions'}
        with self.assertRaises(ValueError):
            self.receive([self.signal(2), bad])
        self.assertEqual(self.page(), [])
        self.assertIsNone(self.api.state('acme', 'staging', 'inbox')['cursor'])
        for _ in range(2):
            self.api.refuse('acme', 'staging', 'inbox', 'a'*64, 'invalid_envelope')
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM repair_signal_refusals')[0]['n'], 1)
        with self.assertRaises(ValueError):
            self.api.refuse('acme', 'staging', 'inbox', 'b'*64, 'raw secret text')

    def test_summary_exposes_no_signal_evidence_cursor_or_native_id(self):
        self.receive([self.signal(occurrence=3)], next='private-source-cursor')
        summary = self.api.summary(project='acme', environment='staging')
        self.assertEqual(summary['signals'], 1)
        self.assertEqual(summary['occurrences'], 3)
        text = json.dumps(summary)
        for private in ('native_id', 'evidence_ref', 'envelope', 'private-source-cursor', self.signal().id):
            self.assertNotIn(private, text)
        self.assertEqual(self.api.summary(project='other')['signals'], 0)

    def test_outcome_references_are_typed_and_not_free_text(self):
        self.receive([self.signal()])
        for outcome, ref in [('lot', 'secret'), ('basket', 'lot:1'), ('ignored', 'private text')]:
            with self.assertRaises(ValueError):
                self.api.settle(self.signal().id, outcome, reference=ref)

    def test_two_receivers_with_same_cursor_only_one_commits(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from ameesh import db as db_mod
        ready = Barrier(2)

        def writer(n):
            db = db_mod.connect(self.cfg)
            try:
                ready.wait(timeout=10)
                return storage.of(db).repair_signals.receive(
                    'acme', 'staging', 'inbox', [self.signal(n)],
                    expected_cursor=None, next_cursor=str(n))
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(writer, [1, 2]))
        self.assertEqual(sorted(outcomes), [False, True])
        rows = self.page()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['position'], 1)
        self.assertEqual(self.api.state('acme', 'staging', 'inbox')['cursor'],
                         rows[0]['signal']['native_id'])

    def test_uncommitted_receive_is_invisible_and_replay_has_no_gap(self):
        from ameesh import db as db_mod
        observer = db_mod.connect(self.cfg)
        try:
            with self.db.transaction() as tx:
                api = storage.of(tx).repair_signals
                api.receive('acme', 'staging', 'inbox', [self.signal()],
                            expected_cursor=None, next_cursor='one')
                other = storage.of(observer).repair_signals
                self.assertEqual(other.page('acme', 'staging', 'inbox'), [])
                self.assertIsNone(other.state('acme', 'staging', 'inbox')['cursor'])
            self.assertEqual(len(other.page('acme', 'staging', 'inbox')), 1)
            self.receive([self.signal(2)], 'one', 'two')
            self.assertEqual([r['position'] for r in self.page()], [1, 2])
        finally:
            observer.close()
