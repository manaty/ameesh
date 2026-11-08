# SPDX-License-Identifier: AGPL-3.0-only
"""A1 journal (#6320): transactional stream cursor and idempotent receipts.

No raw source payload, diagnostic text, or authorization is accepted here.
Occurrences are a source high-water count (max), never a delivery counter.
"""
from __future__ import annotations

import json
from itertools import islice
import re

from ... import repair_signals as signals
from .. import interface


def _scope(project, environment, source):
    return (signals._project(project), signals._environment(environment),
            signals._source(source))


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
