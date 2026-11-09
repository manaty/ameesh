# SPDX-License-Identifier: AGPL-3.0-only
"""#6475 : journal A11 des intentions et issues de reprise (U14).

Témoins demandés par le lot : origines distinctes, intention distincte de
l'effet, reprise manuelle mesurable, compteur conservé à la relève du
détenteur, entrée reçue deux fois sans doublon.
"""
from __future__ import annotations

import hashlib
import json
import unittest

from ameesh import db as db_mod
from ameesh import repair_retry, storage

from .support import PgTestCase
from .test_6313_repair_report import capability, run


def key(n: int) -> str:
    """Une clé d'idempotence de 64 hexadécimaux, stable pour un témoin."""
    return hashlib.sha256(b'retry-%d' % n).hexdigest()


def retry(n: int, **kw):
    values = dict(project='ameesh', environment='staging', origin='automatic',
                  intent='role.pass', cause='command',
                  episode='role:mesh-design/alerts', idempotency_key=key(n),
                  requested_at='2026-11-09T12:00:00Z', attempt=1)
    values.update(kw)
    return repair_retry.Retry(**values)


class RepairRetryContractTest(unittest.TestCase):
    """Le contrat pur, testable sans base."""

    def test_key_is_stable_and_depends_on_order(self):
        self.assertEqual(repair_retry.key_of('role.pass', 'mesh-design', 'a'),
                         repair_retry.key_of('role.pass', 'mesh-design', 'a'))
        self.assertNotEqual(repair_retry.key_of('role.pass', 'a'),
                            repair_retry.key_of('a', 'role.pass'))
        self.assertRegex(repair_retry.key_of('x'), r'^[0-9a-f]{64}$')

    def test_intention_and_effect_are_separate_states(self):
        requested = retry(1)
        self.assertEqual(requested.state, 'requested')
        self.assertEqual(requested.record()['engaged_at'], '')
        engaged = retry(1, engaged_at='2026-11-09T12:00:01Z')
        self.assertEqual(engaged.state, 'engaged')
        completed = retry(1, engaged_at='2026-11-09T12:00:01Z',
                          completed_at='2026-11-09T12:00:02Z')
        self.assertEqual(completed.state, 'completed')
        # La reprise demandée reste comptée : l'effet ne l'efface pas.
        summary = repair_retry.summary([completed])
        self.assertEqual(summary['totals'],
                         {'requested': 1, 'engaged': 1, 'completed': 1,
                          'failed': 0, 'unknown': 0, 'avoided': 0,
                          'retries': 0})

    def test_origins_are_distinct_and_manual_is_measurable(self):
        records = [retry(1, origin='operator', cause='operator'),
                   retry(2, origin='agent', cause='agent'),
                   retry(3, origin='automatic')]
        summary = repair_retry.summary(records)
        self.assertEqual(sorted(summary['by_origin']),
                         ['agent', 'automatic', 'operator'])
        self.assertEqual(summary['by_origin']['operator']['requested'], 1)
        self.assertEqual(summary['by_origin']['agent']['requested'], 1)
        self.assertEqual(summary['by_origin']['automatic']['requested'], 1)

    def test_manual_origin_is_read_from_the_calling_identity(self):
        from ameesh.reprise import _retry_origin
        self.assertEqual(_retry_origin(''), 'operator')
        self.assertEqual(_retry_origin('human:smichea'), 'operator')
        self.assertEqual(_retry_origin('mty-amh-impl-06'), 'agent')

    def test_retry_is_counted_beyond_the_first_attempt(self):
        records = [retry(1, attempt=1), retry(2, attempt=2, origin='agent'),
                   retry(3, attempt=3, origin='operator')]
        summary = repair_retry.summary(records)
        self.assertEqual(summary['totals']['retries'], 2)
        self.assertEqual(summary['max_attempt'], 3)

    def test_strict_record_refuses_unknown_field_and_free_text(self):
        record = retry(1).record()
        self.assertEqual(repair_retry.from_record(record).state, 'requested')
        with self.assertRaisesRegex(repair_retry.RetryError, 'unknown field'):
            repair_retry.from_record({**record, 'detail': 'private text'})
        for field, value in (('origin', 'human'), ('intent', 'free text'),
                             ('cause', 'a raw message'),
                             ('episode', 'a path /etc/passwd')):
            with self.subTest(field=field):
                with self.assertRaises(repair_retry.RetryError):
                    retry(1, **{field: value})
        with self.assertRaisesRegex(repair_retry.RetryError,
                                    'idempotency_key'):
            retry(1, idempotency_key='not-a-digest')

    def test_avoided_carries_no_effect_and_no_attempt(self):
        avoided = retry(1, attempt=0, avoided_at='2026-11-09T12:00:00Z')
        self.assertEqual(avoided.state, 'avoided')
        with self.assertRaises(repair_retry.RetryError):
            retry(1, attempt=1, avoided_at='2026-11-09T12:00:00Z')
        with self.assertRaises(repair_retry.RetryError):
            retry(1, avoided_at='2026-11-09T12:00:00Z',
                  engaged_at='2026-11-09T12:00:01Z')

    def test_terminal_requires_engagement(self):
        with self.assertRaisesRegex(repair_retry.RetryError, 'engagement'):
            retry(1, completed_at='2026-11-09T12:00:01Z')
        with self.assertRaisesRegex(repair_retry.RetryError,
                                    'one terminal outcome'):
            retry(1, engaged_at='2026-11-09T12:00:01Z',
                  completed_at='2026-11-09T12:00:02Z',
                  failed_at='2026-11-09T12:00:03Z')


class RepairRetryStorageTest(PgTestCase):
    """Le journal U5/U14 sur une vraie base : atomicité, doublons, compteur."""

    def setUp(self):
        self.db.execute('DELETE FROM repair_retries')
        self.db.execute('DELETE FROM repair_retry_attempts')
        self.api = storage.of(self.db).repair_signals

    def request(self, n, **kw):
        values = dict(project='ameesh', environment='staging',
                      origin='automatic', intent='role.pass', cause='command',
                      episode='role:mesh-design/alerts')
        values.update(kw)
        return self.api.retry_request(key(n), **values)

    def test_received_twice_is_one_row_without_a_second_attempt(self):
        first = self.request(1)
        self.assertTrue(first['created'])
        self.assertEqual(first['attempt'], 1)
        again = self.request(1)
        self.assertFalse(again['created'])
        self.assertEqual(again['attempt'], 1)
        self.assertEqual(self.db.query(
            'SELECT count(*) AS n FROM repair_retries')[0]['n'], 1)
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 1)

    def test_intent_effect_and_completion_are_distinct(self):
        self.request(1)
        summary = self.api.retry_summary()
        self.assertEqual(summary['totals']['requested'], 1)
        self.assertEqual(summary['totals']['engaged'], 0)
        self.assertEqual(summary['totals']['completed'], 0)
        self.assertTrue(self.api.retry_engage(key(1)))
        self.assertFalse(self.api.retry_engage(key(1)))
        summary = self.api.retry_summary()
        self.assertEqual(summary['totals']['engaged'], 1)
        self.assertEqual(summary['totals']['completed'], 0)
        self.assertTrue(self.api.retry_settle(key(1), 'completed'))
        self.assertFalse(self.api.retry_settle(key(1), 'completed'))
        summary = self.api.retry_summary()
        self.assertEqual(summary['totals']['requested'], 1)
        self.assertEqual(summary['totals']['engaged'], 1)
        self.assertEqual(summary['totals']['completed'], 1)
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 0)

    def test_attempt_counter_survives_holder_handover(self):
        self.request(1)
        other = db_mod.connect(self.cfg)
        try:
            other_api = storage.of(other).repair_signals
            second = other_api.retry_request(
                key(2), project='ameesh', environment='staging',
                origin='agent', intent='role.pass', cause='command',
                episode='role:mesh-design/alerts')
            self.assertEqual(second['attempt'], 2)
            # Le nouveau détenteur relit le même compteur durable.
            self.assertEqual(other_api.retry_attempts(
                'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 2)
        finally:
            other.close()
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 2)
        third = self.request(3)
        self.assertEqual(third['attempt'], 3)
        self.assertTrue(self.api.retry_settle(key(3), 'completed'))
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 0)
        self.assertEqual(self.request(4)['attempt'], 1)
        # Un rejeu tardif ne consomme ni ne perd le compteur courant.
        self.assertEqual(self.request(2)['attempt'], 2)
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 1)

    def test_late_completion_does_not_reset_a_newer_series(self):
        self.assertEqual(self.request(1)['attempt'], 1)
        self.assertEqual(self.request(2)['attempt'], 2)
        self.assertTrue(self.api.retry_settle(key(2), 'completed'))
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 0)
        self.assertEqual(self.request(3)['attempt'], 1)
        # A settles after B: its older generation must not erase C's series.
        self.assertTrue(self.api.retry_settle(key(1), 'completed'))
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 1)
        self.assertEqual(self.request(4)['attempt'], 2)

    def test_replay_between_request_and_engage_does_not_count_twice(self):
        self.request(1)
        replayed = self.api.retry_begin(
            key(1), project='ameesh', environment='staging',
            origin='automatic', intent='role.pass', cause='command',
            episode='role:mesh-design/alerts')
        self.assertFalse(replayed['created'])
        self.assertTrue(replayed['engaged'])
        self.assertFalse(self.api.retry_engage(key(1)))
        self.assertEqual(replayed['attempt'], 1)
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 1)

    def test_avoided_retry_consumes_no_attempt_and_is_idempotent(self):
        self.request(1)
        self.assertTrue(self.api.retry_avoid(
            key(2), project='ameesh', environment='staging',
            origin='automatic', intent='role.pass', cause='lease_busy',
            episode='role:mesh-design/alerts'))
        self.assertFalse(self.api.retry_avoid(
            key(2), project='ameesh', environment='staging',
            origin='automatic', intent='role.pass', cause='lease_busy',
            episode='role:mesh-design/alerts'))
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'role:mesh-design/alerts', 'command'), 1)
        summary = self.api.retry_summary()
        self.assertEqual(summary['totals']['requested'], 2)
        self.assertEqual(summary['totals']['avoided'], 1)
        avoided = repair_retry.from_record(self.api.retry_get(key(2)))
        self.assertEqual(avoided.state, 'avoided')
        self.assertEqual(avoided.attempt, 0)

    def test_summary_window_scope_and_privacy(self):
        self.request(1, requested_at=1_000_000_000,
                     episode='role:old/run')
        self.request(2, requested_at=2_000_000_000,
                     episode='role:new/run')
        self.request(3, project='other', requested_at=2_000_000_000,
                     episode='role:new/run')
        summary = self.api.retry_summary(
            project='ameesh', environment='staging',
            since=1_500_000_000, until=2_100_000_000)
        self.assertEqual(summary['totals']['requested'], 1)
        self.assertEqual(self.api.retry_summary(project='other')[
            'totals']['requested'], 1)
        text = json.dumps(summary)
        for private in (key(1), key(2), 'role:old/run', 'role:new/run'):
            self.assertNotIn(private, text)

    def test_manual_resume_records_intent_engagement_and_completion(self):
        first = repair_retry.observe_manual_resume(
            self.db, project='ameesh', environment='staging',
            agent='mty-amh-impl-06', actor='human:smichea', origin='operator',
            invocation='manual-1')
        self.assertTrue(first['created'])
        self.assertTrue(first['settled'])
        again = repair_retry.observe_manual_resume(
            self.db, project='ameesh', environment='staging',
            agent='mty-amh-impl-06', actor='human:smichea', origin='operator',
            invocation='manual-1')
        self.assertFalse(again['created'])
        self.assertFalse(again['settled'])
        summary = self.api.retry_summary()
        self.assertEqual(summary['totals']['requested'], 1)
        self.assertEqual(summary['totals']['engaged'], 1)
        self.assertEqual(summary['totals']['completed'], 1)
        self.assertEqual(summary['totals']['retries'], 0)
        self.assertEqual(summary['by_origin']['operator']['completed'], 1)
        record = repair_retry.from_record(
            self.api.retry_get(repair_retry.key_of(
                'session.resume', 'mty-amh-impl-06', 'human:smichea',
                'manual-1')))
        self.assertEqual(record.intent, 'session.resume')
        self.assertEqual(record.origin, 'operator')
        self.assertEqual(record.attempt, 1)
        self.assertEqual(record.state, 'completed')

    def test_manual_resume_leaves_unknown_when_a_holder_must_apply_it(self):
        repair_retry.observe_manual_resume(
            self.db, project='ameesh', environment='staging',
            agent='mty-amh-impl-06', actor='mty-amh-impl-07', origin='agent',
            invocation='manual-2', settled='unknown')
        record = repair_retry.from_record(
            self.api.retry_get(repair_retry.key_of(
                'session.resume', 'mty-amh-impl-06', 'mty-amh-impl-07',
                'manual-2')))
        self.assertEqual(record.state, 'unknown')
        summary = self.api.retry_summary()
        self.assertEqual(summary['totals']['unknown'], 1)
        self.assertEqual(summary['by_origin']['agent']['unknown'], 1)

    def test_avoided_resume_consumes_no_attempt_and_is_idempotent(self):
        first = repair_retry.observe_manual_resume_avoided(
            self.db, project='ameesh', environment='staging',
            agent='mty-amh-impl-06', actor='human:smichea', origin='operator',
            invocation='avoid-1', cause='lease_busy')
        again = repair_retry.observe_manual_resume_avoided(
            self.db, project='ameesh', environment='staging',
            agent='mty-amh-impl-06', actor='human:smichea', origin='operator',
            invocation='avoid-1', cause='lease_busy')
        self.assertTrue(first)
        self.assertFalse(again)
        record = repair_retry.from_record(self.api.retry_get(
            repair_retry.key_of('session.resume.avoid', 'mty-amh-impl-06',
                                'human:smichea', 'avoid-1')))
        self.assertEqual(record.state, 'avoided')
        self.assertEqual(record.attempt, 0)
        summary = self.api.retry_summary()
        self.assertEqual(summary['totals']['requested'], 1)
        self.assertEqual(summary['totals']['avoided'], 1)
        self.assertEqual(summary['totals']['engaged'], 0)
        self.assertEqual(self.api.retry_attempts(
            'ameesh', 'staging', 'agent:mty-amh-impl-06', 'lease_busy'), 0)

    def test_n_minus_one_reader_ignores_additive_columns(self):
        self.request(1)
        self.db.execute(
            "ALTER TABLE repair_retries ADD COLUMN future_column text"
            " DEFAULT 'private newer metadata'")
        record = self.api.retry_get(key(1))
        self.assertEqual(repair_retry.from_record(record).state, 'requested')
        with self.assertRaisesRegex(repair_retry.RetryError, 'unknown field'):
            repair_retry.from_record({**record, 'future_column': 'x'})


class RepairReportA11Test(unittest.TestCase):
    """Le rapport U1 distingue demandées, engagées, achevées et évitées."""

    def test_a11_retry_counters_by_origin(self):
        records = [
            retry(1, origin='operator', engaged_at='2026-11-09T12:00:01Z',
                  completed_at='2026-11-09T12:00:02Z'),
            retry(2, origin='automatic', attempt=2,
                  engaged_at='2026-11-09T12:00:01Z',
                  failed_at='2026-11-09T12:00:02Z'),
            retry(3, origin='automatic', attempt=0,
                  avoided_at='2026-11-09T12:00:00Z'),
        ]
        summary = repair_retry.summary(records)
        a11 = capability(run(retry_summary=summary), 'A11')['counters']
        self.assertEqual(a11['reprises_demandees'], 3)
        self.assertEqual(a11['reprises_engagees'], 2)
        self.assertEqual(a11['reprises_achevees'], 1)
        self.assertEqual(a11['reprises_evitees'], 1)
        self.assertEqual(a11['reprises_rejouees'], 1)
        self.assertEqual(a11['reprises_par_origine']['operator']['completed'], 1)
        self.assertEqual(a11['reprises_par_origine']['automatic']['failed'], 1)
        unseen = capability(run(), 'A11')
        self.assertIsNone(unseen['counters']['reprises_par_origine'])
        for name in ('reprises_par_origine', 'reprises_demandees',
                     'reprises_engagees', 'reprises_achevees',
                     'reprises_evitees', 'reprises_rejouees'):
            self.assertIn(name, unseen['unknown'])
