# SPDX-License-Identifier: AGPL-3.0-only
"""A3 receipts: exact cause, aliases, real writers and terminal-state races."""
from dataclasses import replace
import threading
from unittest import mock

from ameesh import fingerprints as fp, repair_signals as signals
from ameesh import repair_triage as triage, repair_observation as obs, work
from .support import PgTestCase
from .test_6317_repair_correlation import NOW, OBSERVE, ACTIVE, HOST, HOST2, attestation, ascend


class CauseObservationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ('fingerprint_cause_observations', 'repair_signals',
                      'repair_signal_streams', 'repair_signal_refusals'):
            self.db.execute('DELETE FROM ' + table)

    def signal(self, ident=1, version=OBSERVE, cause='command-failed'):
        return signals.from_inbox({'id': ident}, project='ameesh',
                                  environment='staging', received_at=NOW,
                                  cause_key=cause, observed_version=version)

    def qualification(self, signal, reason='timeout', component='composer',
                      test='test_contract', covered=False, nature='product_failure'):
        return triage.qualify(signal, triage.QualificationFacts(
            nature=nature, confidence='confirmed', evidence_ref=signal.evidence_ref,
            component=component, reason=reason, test=test, covered=covered))

    def lot(self):
        return work.add(self.db, title='Existing cause', app='ameesh',
                        type='bug', actor='test')['id']

    def observe(self, signal=None, qualification=None, db=None, **kw):
        signal = signal or self.signal()
        return fp.observe_cause(db or self.db, signal,
                                qualification or self.qualification(signal), **kw)

    def test_four_exact_duplicates_only_propose_existing_lots(self):
        for i in range(4):
            lot = self.lot()
            first = self.signal(i * 2 + 1, cause='cause-%s' % i)
            second = self.signal(i * 2 + 2, cause='cause-%s' % i)
            self.observe(first, lot=lot)
            result = self.observe(second)
            self.assertEqual(result['candidates'], [lot])
            self.assertEqual(result['relation'], 'existing_cause')
            self.assertEqual(result['effects'], 'none')
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM work_items')[0]['n'], 4)

    def test_historical_duplicate_links_do_not_invent_structured_causes(self):
        # Study #6308: the explicit links are known, their technical causes
        # are not provided by that cohort. Preserve the association only.
        pairs = ((5600, 5599), (5626, 5606), (5746, 5742), (5872, 5826))
        for duplicate, original in pairs:
            lot = work.add(self.db, title='Historical witness %s' % original,
                            app='ameesh', actor='test')['id']
            signal = self.signal(duplicate)
            result = self.observe(signal, triage.qualify(signal), lot=lot)
            self.assertEqual(result['relation'], 'unknown')
            self.assertEqual(result['cause_id'], '')
            self.assertEqual(result['lot'], lot)
            self.assertEqual(result['candidates'], [])

    def test_eight_generic_recurrences_are_not_eight_known_causes(self):
        for i in range(8):
            signal = self.signal(i + 1)
            result = self.observe(signal, triage.qualify(signal))
            self.assertEqual(result['relation'], 'unknown')
            self.assertEqual(result['cause_id'], '')
            self.assertEqual(result['candidates'], [])
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM work_items')[0]['n'], 0)

    def test_alias_is_only_context_same_symptom_different_causes(self):
        s = self.signal()
        lot = self.lot()
        first = self.observe(s, lot=lot)
        second = self.observe(s, self.qualification(s, reason='permission-denied'))
        self.assertTrue(second['alias_seen'])
        self.assertNotEqual(first['cause_id'], second['cause_id'])
        self.assertEqual(second['candidates'], [])
        self.assertEqual(second['relation'], 'new_cause')
        # A changed source alias can retain an explicitly established cause.
        third = self.observe(self.signal(2))
        self.assertEqual(third['candidates'], [lot])

    def test_old_fingerprint_source_is_an_alias_not_a_cause(self):
        opened = fp.open_lot(self.db, app='ameesh', key='old-command-rc1',
                             title='Old symptom', environment='staging', actor='test')
        result = self.observe(self.signal(2), legacy_key='old-command-rc1')
        self.assertEqual(result['alias'], opened['source'])
        self.assertEqual(result['alias_candidates'], [opened['lot']])
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['relation'], 'same_symptom_unknown')
        self.assertEqual(work.get(self.db, opened['lot'])['source'], opened['source'])

    def test_closed_cause_active_old_and_mixed_versions(self):
        lot = self.lot()
        self.observe(lot=lot)
        work.close(self.db, lot, abandoned=True, actor='test')
        for ident, version, active, expected in (
                (2, ACTIVE, attestation(ACTIVE), 'recurrence_candidate_active'),
                (3, OBSERVE, attestation(ACTIVE), 'old_version'),
                (4, OBSERVE, {**attestation(OBSERVE),
                              **attestation(ACTIVE, host=HOST2)}, 'same_cause_version_unknown')):
            result = self.observe(self.signal(ident, version=version), active=active,
                                  hosts=sorted(active), now=NOW, is_ancestor=ascend)
            self.assertEqual(result['relation'], expected)
            self.assertEqual(result['candidates'], [lot])
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM work_items')[0]['n'], 1)

    def test_unbound_facts_and_covered_negative_contract_stay_advisory(self):
        s = self.signal()
        covered = self.qualification(s, nature='negative_contract', covered=True)
        result = self.observe(s, covered)
        self.assertEqual(result['effects'], 'none')
        unknown = triage.qualify(s, triage.QualificationFacts(
            nature='product_failure', confidence='confirmed', evidence_ref='other',
            component='composer', reason='timeout'))
        self.assertEqual(self.observe(s, unknown)['cause_id'], '')
        with self.assertRaises(ValueError):
            self.observe(s, replace(covered, signal_id='other'))

    def test_two_real_writers_and_replay_share_one_receipt(self):
        barrier = threading.Barrier(2)
        results, errors = [], []
        def run():
            db = self.connect()
            try:
                barrier.wait(timeout=10)
                results.append(self.observe(db=db))
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.observe(), results[0])
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM fingerprint_cause_observations')[0]['n'], 1)

    def test_close_winning_the_row_lock_is_seen_as_terminal(self):
        lot = self.lot()
        self.observe(lot=lot)
        locked, release = threading.Event(), threading.Event()
        errors = []
        def close():
            db = self.connect()
            try:
                with db.transaction() as tx:
                    tx.query('SELECT id FROM work_items WHERE id=%s FOR UPDATE', (lot,))
                    locked.set()
                    if not release.wait(timeout=20):
                        raise RuntimeError('close not released')
                    work.close(tx, lot, abandoned=True, actor='test')
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        closer = threading.Thread(target=close)
        closer.start()
        self.assertTrue(locked.wait(timeout=20))
        # Let the real close commit before the observer acquires its row lock.
        release.set()
        result = self.observe(self.signal(2, version=ACTIVE), active=attestation(ACTIVE),
                              hosts=[HOST], now=NOW)
        closer.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(result['relation'], 'recurrence_candidate_active')

    def test_foreign_or_missing_lot_is_not_an_association(self):
        foreign = work.add(self.db, title='Other project', app='other', actor='test')['id']
        for ident, lot in ((1, foreign), (2, 99999999)):
            result = self.observe(self.signal(ident), lot=lot)
            self.assertEqual(result['relation'], 'unknown_lot')
            self.assertIsNone(result['lot'])
            self.assertEqual(result['candidates'], [])

    def test_scope_and_test_discriminators_never_cross_match(self):
        s = self.signal()
        self.observe(s, lot=self.lot())
        for variant in (replace(s, environment='production'),
                        replace(s, project='other'),
                        replace(s, source='thread', native_id='ameesh/topic#1')):
            result = self.observe(variant)
            self.assertEqual(result['candidates'], [])
        q = self.qualification(s, test='other_contract')
        self.assertEqual(self.observe(s, q)['candidates'], [])

    def test_a3_failure_cannot_interrupt_page_settlement_or_other_receipts(self):
        original = fp.observe_cause
        def observe(db, signal, qualification, **kw):
            page = obs.storage.of(db).repair_signals.page('ameesh', 'staging', 'inbox')
            self.assertEqual([row['signal']['outcome'] for row in page],
                             ['unknown', 'unknown'])
            if signal.native_id == '1':
                raise RuntimeError('PRIVATE SOURCE CONTENT')
            return original(db, signal, qualification, **kw)
        rows = [{'key': ident, 'entry': {'id': ident}} for ident in (1, 2)]
        with mock.patch.object(fp, 'observe_cause', side_effect=observe) as observer:
            with self.assertLogs('ameesh.repair_observation', level='WARNING') as logs:
                self.assertTrue(obs.ingest(
                    self.db, project='ameesh', environment='staging', source='inbox',
                    rows=rows, expected_cursor=None, next_cursor='two', now=NOW))
        self.assertEqual(observer.call_count, 2)
        self.assertEqual(logs.output, [
            'WARNING:ameesh.repair_observation:repair-cause-observation: source_error (RuntimeError)'])
        self.assertEqual(self.db.query(
            'SELECT count(*) AS n FROM fingerprint_cause_observations')[0]['n'], 1)

    def test_u6_writes_unknown_receipt_without_creating_work(self):
        row = {'key': 1, 'entry': {'id': 1}}
        for expected, cursor in ((None, 'one'), ('one', 'two')):
            self.assertTrue(obs.ingest(self.db, project='ameesh', environment='staging',
                                      source='inbox', rows=[row], expected_cursor=expected,
                                      next_cursor=cursor, now=NOW))
        records = self.db.query('SELECT record FROM fingerprint_cause_observations')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['record']['relation'], 'unknown')
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM work_items')[0]['n'], 0)
