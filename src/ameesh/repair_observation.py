# SPDX-License-Identifier: AGPL-3.0-only
"""U6: bounded observation of committed inputs, without creating work.

The shared U5 cursor is a scan position, never a lease or authorization.
Scans wrap at EOF: late commits and changed existing inputs are eventually
revisited, including a crash after receipt but before settlement. Replaying
uses stable source identities and occurrence high-water counts, not deliveries.
For alerts, a key's occurrence is the number of ``alert.raised`` events already
committed for that project and key: raising one key N times yields N, while
replaying the same page recomputes the same count and never adds a delivery.
Only metadata is selected; source bodies and exception messages stay private.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import repair_signals, repair_triage, storage
from .repair_correlation import ENVIRONNEMENT

# U5 currently settles entries individually; cap database round trips per turn.
BATCH = 20


def _alert_occurrences(db, project, keys):
    """Number of committed raises of each alert key, a source high-water count.

    One aggregate per page, then the identity of a raise is its alert key, so N
    raises of one key give N occurrences while one signal identity stays unique.
    The partial ``ameesh_events_alert_project_id_idx`` bounds the aggregate to
    the project's alerts (never the whole event log); the count is read from the
    source rows, never incremented by a delivery, so a page replay is neutral.
    A raise whose lower event id commits late is still counted on a later pass:
    the count is an upper bound of the raises already visible, not a position.
    The caller keeps at least the duplicates of the page it already holds, so a
    concurrent retention prune cannot make an observed raise disappear.
    """
    keys = sorted({key for key in keys if isinstance(key, str) and key})
    if not keys:
        return {}
    rows = db.query(
        "SELECT data->>'key' AS key, count(*) AS occurrences"
        " FROM ameesh_events WHERE project=%s AND type='alert.raised'"
        " AND data->>'key' = ANY(%s::text[]) GROUP BY 1", (project, keys))
    return {row['key']: int(row['occurrences']) for row in rows}


def ingest(db, *, project, environment, source, rows, expected_cursor,
           next_cursor, now):
    """Filter poison entries, record refusals, then atomically receive valid ones.

    Rows contain a stable metadata ``key``, a source ``entry`` and optionally
    structured ``facts`` and an existing typed ``outcome``/``reference``.
    Qualification is advisory: even a confirmed route never opens anything.
    An outcome is recorded only with proof supplied by the existing producer.
    """
    api = storage.of(db).repair_signals
    accepted = []
    for row in rows:
        try:
            signal = repair_signals.from_source(
                source, row['entry'], project=project, environment=environment,
                received_at=now, occurrence=row.get('occurrence', 1),
                observed_version=row.get('observed_version', ''))
        except repair_signals.SignalError:
            key = hashlib.sha256(json.dumps(
                [project, environment, source, row['key']],
                ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
            api.refuse(project, environment, source, key, 'invalid_envelope')
            continue
        triage = repair_triage.qualify(signal, row.get('facts'))
        # Routes without an existing work reference remain unknown in
        # observation mode; the route alone is never an outcome proof.
        outcome = row.get('outcome') or ('ignored' if triage.route == 'ignored'
                                        else 'unknown')
        accepted.append((signal, outcome, row.get('reference', '')))
    if not api.receive(project, environment, source,
                       [signal for signal, _, _ in accepted],
                       expected_cursor=expected_cursor, next_cursor=next_cursor):
        return False
    for signal, outcome, reference in accepted:
        settled = api.settle(signal.id, outcome, reference=reference)
        if not settled and outcome in ('lot', 'basket', 'decision'):
            api.settle(signal.id, outcome, reference=reference, expected='unknown')
    return True


def scan(db, project, source, *, environment=ENVIRONNEMENT, now=None):
    """One bounded circular page of committed alerts or GitHub deliveries.

    Wraparound deliberately trades extra deduplicated reads for no irreversible
    high-water skip when a source transaction commits out of ID order. A new
    holder resumes the shared scan, not the previous host's local state.
    Alert occurrences come from the source raises of the key, never from the
    number of pages that delivered it (see :func:`_alert_occurrences`).
    """
    now = time.time() if now is None else now
    api = storage.of(db).repair_signals
    cursor = api.state(project, environment, source)['cursor']
    if source == 'alerts':
        after = int(cursor) if cursor is not None else 0
        rows = db.query(
            "SELECT id, data->>'key' AS key, data->>'since' AS since,"
            " coalesce(nullif(data->>'revision',''),data->>'observed_version','') AS version,"
            " extract(epoch FROM created_at) AS observed"
            " FROM ameesh_events WHERE project=%s AND type='alert.raised'"
            " AND id>%s ORDER BY id LIMIT %s", (project, after, BATCH))
        page_occurrences = {}
        for row in rows:
            key = row['key']
            if isinstance(key, str) and key:
                page_occurrences[key] = page_occurrences.get(key, 0) + 1
        occurrences = _alert_occurrences(
            db, project, [row['key'] for row in rows])
        inputs = []
        for row in rows:
            try:
                since = float(row['since']) if row.get('since') else row['observed']
            except (ValueError, TypeError):
                since = row['observed']  # malformed source timestamp: observation time
            key = row['key']
            inputs.append({'key': row['id'],
                           'occurrence': max(occurrences.get(key, 0),
                                             page_occurrences.get(key, 1)),
                           'observed_version': row['version'], 'entry': {
                'key': key, 'project': project, 'since': since}})
        next_cursor = str(rows[-1]['id']) if rows else None
    elif source == 'github_watch':
        after = json.loads(cursor) if cursor is not None else ['', '', 0]
        rows = db.query(
            "SELECT p.repo,p.kind,p.object_id,p.issue_ref,p.delivered_at,"
            " (SELECT w.id FROM work_items w"
            " WHERE w.app=p.project AND lower(w.issue_ref)=lower(p.issue_ref)"
            " AND (p.kind='issue' OR w.created_at>=to_timestamp(p.delivered_at))"
            " ORDER BY w.id LIMIT 1) AS lot"
            " FROM github_watch_pending p WHERE p.project=%s"
            " AND (p.repo,p.kind,p.object_id)>(%s,%s,%s)"
            " ORDER BY p.repo,p.kind,p.object_id LIMIT %s",
            (project, *after, BATCH))
        inputs = []
        for row in rows:
            item = {'key': [row['repo'], row['kind'], row['object_id']],
                    'entry': {'issue_ref': row['issue_ref'], 'kind': row['kind'],
                              'object_id': int(row['object_id'])}}
            if row['lot']:
                item.update(outcome='lot', reference='lot:%s' % row['lot'])
            inputs.append(item)
        next_cursor = (json.dumps([rows[-1]['repo'], rows[-1]['kind'],
                                  rows[-1]['object_id']]) if rows else None)
    else:
        raise ValueError('unsupported observation source')
    return ingest(db, project=project, environment=environment, source=source,
                  rows=inputs, expected_cursor=cursor, next_cursor=next_cursor, now=now)


def observe(db, *, project=None, source='alerts', now=None, log=None):
    """Best effort at the existing entry points; one source cannot stop another.

    No source failure is recursively converted into a signal or a new lot.
    A failed receipt retains its cursor; an interrupted settlement is replayed
    on the next scan cycle. Diagnostics carry only a bounded exception class.
    """
    log = log or (lambda text: None)
    try:
        projects = ([project] if project is not None else
                    [row['project'] for row in db.query(
                        "SELECT DISTINCT project FROM ameesh_events"
                        " WHERE type='alert.raised' AND project IS NOT NULL"
                        " ORDER BY project")])
    except Exception as exc:
        log('repair-observation: source_error (%s)' % type(exc).__name__)
        return
    for name in projects:
        try:
            scan(db, name, source, now=now)
        except Exception as exc:
            log('repair-observation: source_error (%s)' % type(exc).__name__)
