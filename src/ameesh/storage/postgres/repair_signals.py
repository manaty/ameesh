# SPDX-License-Identifier: AGPL-3.0-only
"""U5/U14 repair journal storage (#6320, #6475).

A1: transactional stream cursor and idempotent receipts.  A11: durable retry
intent/outcome journal and its per-cause attempt counter.  No raw source
payload, diagnostic text, or authorization is accepted here.  Occurrences are
a source high-water count (max), never a delivery counter.
"""
from __future__ import annotations

import datetime
import json
from itertools import islice
import re

from ... import repair_retry as retries
from ... import repair_signals as signals
from .. import interface


def _scope(project, environment, source):
    return (signals._project(project), signals._environment(environment),
            signals._source(source))


def _iso(value):
    """Un horodatage de base rendu au format du contrat, sans fuseau local.

    psycopg rend un ``datetime`` ; le pilote psql rend le texte de
    ``timestamptz`` (``2026-11-09 12:00:00+00``).  Les deux passent par le
    même formatage, sinon le lecteur strict refuserait le reçu.
    """
    if value is None:
        return ''
    if isinstance(value, str):
        text = value.strip()
        try:
            value = datetime.datetime.fromisoformat(
                text.replace(' ', 'T').replace('Z', '+00:00'))
        except ValueError:
            return text
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def _retry_record(row):
    record = {name: row[name] for name in retries.FIELDS if name != 'schema'}
    record['schema'] = retries.SCHEMA
    for name in ('requested_at', 'engaged_at', 'completed_at', 'failed_at',
                 'unknown_at', 'avoided_at'):
        record[name] = _iso(row[name])
    record['attempt'] = int(row['attempt'])
    return record


def _retry_scope(project, environment, origin, intent, cause, episode, key):
    return (retries._key(key), retries._project(project),
            retries._environment(environment), retries._origin(origin),
            retries._intent(intent), retries._cause(cause),
            retries._episode(episode))


def _when(value, field_name):
    """Un instant fourni (ISO/epoch) ou None, prêt pour ``%s::timestamptz``."""
    return retries._at(value, field_name) or None


def _cursor(value):
    if value is not None and (not isinstance(value, str) or len(value) > 512
                              or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid cursor')
    return value


def _signal(row):
    # Deliberate N-1 projection: SQL/storage additions never enter the strict
    # repair-signal/1 reader. The original reader still rejects unknown fields.
    record = {k: row['envelope'][k] for k in signals.FIELDS}
    record['outcome'] = row['outcome']
    return signals.from_record(record)


class RepairSignals(interface.RepairSignals):
    def state(self, project, environment, source):
        rows = self.db.query(
            'SELECT cursor, position FROM repair_signal_streams'
            ' WHERE project=%s AND environment=%s AND source=%s',
            _scope(project, environment, source))
        return rows[0] if rows else {'cursor': None, 'position': 0}

    def receive(self, project, environment, source, entries, *,
                expected_cursor, next_cursor):
        scope = _scope(project, environment, source)
        expected_cursor, next_cursor = _cursor(expected_cursor), _cursor(next_cursor)
        entries = list(islice(iter(entries), 1001))
        if len(entries) > 1000:
            raise ValueError('batch exceeds 1000 signals')
        # Validate everything before opening the transaction. Rejected input
        # never advances the source cursor; U6 may call refuse separately.
        records = []
        for entry in entries:
            signal = signals.from_record(entry.record() if isinstance(entry, signals.Signal) else entry)
            if (signal.project, signal.environment, signal.source) != scope:
                raise ValueError('signal belongs to another stream')
            if signal.outcome != 'pending':
                raise ValueError('reception requires a pending signal')
            records.append(signal)
        with self.db.transaction() as tx:
            tx.execute(
                'INSERT INTO repair_signal_streams(project,environment,source)'
                ' VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', scope)
            state = tx.query(
                'SELECT cursor, position FROM repair_signal_streams'
                ' WHERE project=%s AND environment=%s AND source=%s FOR UPDATE', scope)[0]
            if state['cursor'] != expected_cursor:
                return False
            position = state['position']
            for signal in records:
                found = tx.query('SELECT id FROM repair_signals WHERE id=%s', (signal.id,))
                if found:
                    tx.execute(
                        'UPDATE repair_signals SET envelope=jsonb_set(envelope,'
                        " '{occurrence}', to_jsonb(greatest((envelope->>'occurrence')::bigint,%s::bigint))),"
                        ' updated_at=now() WHERE id=%s', (signal.occurrence, signal.id))
                else:
                    position += 1
                    tx.execute(
                        'INSERT INTO repair_signals'
                        '(id,project,environment,source,native_id,position,envelope)'
                        ' VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)',
                        (signal.id, *scope, signal.native_id, position,
                         json.dumps(signal.record())))
            tx.execute('UPDATE repair_signal_streams SET cursor=%s, position=%s'
                       ' WHERE project=%s AND environment=%s AND source=%s',
                       (next_cursor, position, *scope))
        return True

    def page(self, project, environment, source, *, after=0, limit=100):
        scope = _scope(project, environment, source)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError('invalid position')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('limit must be 1..1000')
        rows = self.db.query(
            'SELECT envelope, outcome, outcome_ref, position FROM repair_signals'
            ' WHERE project=%s AND environment=%s AND source=%s AND position>%s'
            ' ORDER BY position LIMIT %s', (*scope, after, limit))
        return [{'signal': _signal(row).record(), 'position': row['position'],
                 'outcome_ref': row['outcome_ref']} for row in rows]

    def settle(self, signal_id, outcome, *, reference='', expected='pending'):
        if outcome not in signals.OUTCOMES or expected not in signals.OUTCOMES:
            raise ValueError('unknown outcome')
        if outcome == 'pending':
            raise ValueError('settlement cannot reset reception')
        if outcome in ('lot', 'basket', 'decision'):
            if not isinstance(reference, str) or not re.fullmatch(outcome + r':[1-9][0-9]{0,18}', reference):
                raise ValueError('typed outcome reference required')
        elif reference != '':
            raise ValueError('this outcome carries no reference')
        return bool(self.db.query(
            'UPDATE repair_signals SET outcome=%s,outcome_ref=%s,updated_at=now()'
            ' WHERE id=%s AND outcome=%s RETURNING id',
            (outcome, reference, signal_id, expected)))

    def refuse(self, project, environment, source, rejection_key, reason):
        scope = _scope(project, environment, source)
        if not isinstance(rejection_key, str) or not re.fullmatch('[0-9a-f]{64}', rejection_key):
            raise ValueError('metadata identity digest required')
        if reason not in ('invalid_envelope', 'unsupported_schema', 'source_error'):
            raise ValueError('unknown refusal reason')
        self.db.execute(
            'INSERT INTO repair_signal_refusals(project,environment,source,rejection_key,reason)'
            ' VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING', (*scope, rejection_key, reason))

    def summary(self, *, project=None, environment=None, since=None, until=None):
        # Aggregates only: no IDs, evidence refs, cursors or envelope fields.
        if project is not None:
            signals._project(project)
        if environment is not None:
            signals._environment(environment)
        rows = self.db.query(
            'SELECT source, environment, outcome, count(*) AS signals,'
            " sum((envelope->>'occurrence')::bigint) AS occurrences"
            ' FROM repair_signals WHERE (%s::text IS NULL OR project=%s)'
            ' AND (%s::text IS NULL OR environment=%s)'
            ' AND (%s::double precision IS NULL OR created_at>=to_timestamp(%s))'
            ' AND (%s::double precision IS NULL OR created_at<to_timestamp(%s))'
            ' GROUP BY source,environment,outcome ORDER BY source,environment,outcome',
            (project, project, environment, environment, since, since, until, until))
        return {'schema': 'repair-signal-journal-summary/1', 'groups': rows,
                'signals': sum(int(r['signals']) for r in rows),
                'occurrences': sum(int(r['occurrences']) for r in rows)}

    # -- A11 retry journal (#6475) --------------------------------------------
    def retry_request(self, idempotency_key, *, project, environment, origin,
                      intent, cause, episode, requested_at=None):
        """Record one retry intent and consume a durable attempt.

        The idempotency key is the whole identity: replaying the same
        intention returns the existing attempt without incrementing the
        shared counter.  The intent is visible before any effect; ``engage``
        and ``settle`` then record that effect separately.
        """
        key, project, environment, origin, intent, cause, episode = \
            _retry_scope(project, environment, origin, intent, cause, episode,
                         idempotency_key)
        requested = _when(requested_at, 'requested_at')
        with self.db.transaction() as tx:
            inserted = tx.query(
                'INSERT INTO repair_retries'
                '(idempotency_key,project,environment,origin,intent,cause,episode,'
                ' attempt,requested_at)'
                ' VALUES (%s,%s,%s,%s,%s,%s,%s,0,coalesce(%s::timestamptz,now()))'
                ' ON CONFLICT (idempotency_key) DO NOTHING'
                ' RETURNING idempotency_key,requested_at',
                (key, project, environment, origin, intent, cause, episode,
                 requested))
            if not inserted:
                row = tx.query(
                    'SELECT attempt,requested_at FROM repair_retries'
                    ' WHERE idempotency_key=%s', (key,))[0]
                return {'idempotency_key': key, 'attempt': int(row['attempt']),
                        'created': False,
                        'requested_at': _iso(row['requested_at'])}
            # Atomic upsert: two holders share the same durable counter, so a
            # handover neither restarts nor loses the attempt series.
            attempt = tx.query(
                'INSERT INTO repair_retry_attempts'
                '(project,environment,episode,cause,attempts)'
                ' VALUES (%s,%s,%s,%s,1)'
                ' ON CONFLICT (project,environment,episode,cause)'
                ' DO UPDATE SET attempts=repair_retry_attempts.attempts+1,'
                ' updated_at=now()'
                ' RETURNING attempts',
                (project, environment, episode, cause))[0]['attempts']
            tx.execute(
                'UPDATE repair_retries SET attempt=%s'
                ' WHERE idempotency_key=%s', (attempt, key))
        return {'idempotency_key': key, 'attempt': int(attempt),
                'created': True, 'requested_at': _iso(inserted[0]['requested_at'])}

    def retry_engage(self, idempotency_key, *, engaged_at=None):
        """Mark the effect as started; False when it already was (idempotent)."""
        key = retries._key(idempotency_key)
        when = _when(engaged_at, 'engaged_at')
        rows = self.db.query(
            'UPDATE repair_retries'
            ' SET engaged_at=coalesce(%s::timestamptz,now()), updated_at=now()'
            ' WHERE idempotency_key=%s AND avoided_at IS NULL'
            ' AND engaged_at IS NULL AND completed_at IS NULL'
            ' AND failed_at IS NULL AND unknown_at IS NULL'
            ' RETURNING idempotency_key', (when, key))
        return bool(rows)

    def retry_begin(self, idempotency_key, *, project, environment, origin,
                    intent, cause, episode, engaged_at=None):
        """Request then engage in one logical step, replay-safe.

        A crash between the two calls leaves a visible ``requested`` row; a
        replay completes the engagement without consuming a second attempt.
        """
        result = self.retry_request(
            idempotency_key, project=project, environment=environment,
            origin=origin, intent=intent, cause=cause, episode=episode)
        result['engaged'] = self.retry_engage(idempotency_key,
                                              engaged_at=engaged_at)
        return result

    def retry_settle(self, idempotency_key, outcome, *, settled_at=None):
        """Record the terminal outcome, once; completed resets the counter.

        ``unknown`` is terminal for U14: reconciling an engaged effect is a
        later, explicit step, never a silent overwrite.
        """
        key = retries._key(idempotency_key)
        if outcome not in retries.OUTCOMES:
            raise ValueError('unknown outcome')
        column = {'completed': 'completed_at', 'failed': 'failed_at',
                  'unknown': 'unknown_at'}[outcome]
        when = _when(settled_at, 'settled_at')
        with self.db.transaction() as tx:
            sql = ('UPDATE repair_retries'
                   ' SET engaged_at=coalesce(engaged_at,%s::timestamptz,now()),'
                   ' ' + column + '=coalesce(%s::timestamptz,now()),'
                   ' updated_at=now()'
                   ' WHERE idempotency_key=%s AND avoided_at IS NULL'
                   ' AND completed_at IS NULL AND failed_at IS NULL'
                   ' AND unknown_at IS NULL AND ' + column + ' IS NULL'
                   ' RETURNING project,environment,episode,cause')
            rows = tx.query(sql, (when, when, key))
            if not rows:
                return False
            row = rows[0]
            if outcome == 'completed':
                tx.execute(
                    'UPDATE repair_retry_attempts SET attempts=0, updated_at=now()'
                    ' WHERE project=%s AND environment=%s AND episode=%s'
                    ' AND cause=%s',
                    (row['project'], row['environment'], row['episode'],
                     row['cause']))
        return True

    def retry_avoid(self, idempotency_key, *, project, environment, origin,
                    intent, cause, episode, avoided_at=None):
        """Record a retry avoided by idempotence, consuming no attempt."""
        key, project, environment, origin, intent, cause, episode = \
            _retry_scope(project, environment, origin, intent, cause, episode,
                         idempotency_key)
        rows = self.db.query(
            'INSERT INTO repair_retries'
            '(idempotency_key,project,environment,origin,intent,cause,episode,'
            ' attempt,avoided_at)'
            ' VALUES (%s,%s,%s,%s,%s,%s,%s,0,'
            ' coalesce(%s::timestamptz,now()))'
            ' ON CONFLICT (idempotency_key) DO NOTHING'
            ' RETURNING idempotency_key',
            (key, project, environment, origin, intent, cause, episode,
             _when(avoided_at, 'avoided_at')))
        return bool(rows)

    def retry_get(self, idempotency_key):
        """The strict ``repair-retry/1`` record, or None."""
        key = retries._key(idempotency_key)
        rows = self.db.query(
            'SELECT * FROM repair_retries WHERE idempotency_key=%s', (key,))
        return _retry_record(rows[0]) if rows else None

    def retry_attempts(self, project, environment, episode, cause):
        """The durable consecutive-attempt counter, 0 when none is stored."""
        rows = self.db.query(
            'SELECT attempts FROM repair_retry_attempts'
            ' WHERE project=%s AND environment=%s AND episode=%s AND cause=%s',
            (retries._project(project), retries._environment(environment),
             retries._episode(episode), retries._cause(cause)))
        return int(rows[0]['attempts']) if rows else 0

    def retry_summary(self, *, project=None, environment=None, since=None,
                      until=None):
        """Aggregated A11 counters: no key, episode, origin-free text or ref."""
        if project is not None:
            retries._project(project)
        if environment is not None:
            retries._environment(environment)
        rows = self.db.query(
            'SELECT origin, intent, cause,'
            ' count(*)::bigint AS requested,'
            ' count(*) FILTER (WHERE engaged_at IS NOT NULL)::bigint AS engaged,'
            ' count(*) FILTER (WHERE completed_at IS NOT NULL)::bigint AS completed,'
            ' count(*) FILTER (WHERE failed_at IS NOT NULL)::bigint AS failed,'
            ' count(*) FILTER (WHERE unknown_at IS NOT NULL)::bigint AS "unknown",'
            ' count(*) FILTER (WHERE avoided_at IS NOT NULL)::bigint AS avoided,'
            ' count(*) FILTER (WHERE attempt>1)::bigint AS retries,'
            ' coalesce(max(attempt),0)::bigint AS max_attempt'
            ' FROM repair_retries WHERE (%s::text IS NULL OR project=%s)'
            ' AND (%s::text IS NULL OR environment=%s)'
            ' AND (%s::double precision IS NULL OR requested_at>=to_timestamp(%s))'
            ' AND (%s::double precision IS NULL OR requested_at<to_timestamp(%s))'
            ' GROUP BY origin,intent,cause ORDER BY origin,intent,cause',
            (project, project, environment, environment, since, since, until,
             until))
        return retries.summary_from_groups(rows)
