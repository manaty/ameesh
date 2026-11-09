# SPDX-License-Identifier: AGPL-3.0-only
"""U6: bounded observation of committed inputs, without creating work.

The shared U5 cursor is a scan position, never a lease or authorization.
Scans wrap at EOF: late commits and changed existing inputs are eventually
revisited, including a crash after receipt but before settlement. Replaying
uses stable source identities and occurrence high-water counts, not deliveries.
For alerts, a key's occurrence is the number of ``alert.raised`` events already
committed for that project and key: raising one key N times yields N, while
replaying the same page recomputes the same count and never adds a delivery.
Alerts that carry no project are the mesh's own findings (hosts, accounts).
They are observed under the scope declared by the caller (the mesh project read
from the configuration, never a built-in name) with a scan position of their
own: receiving them never advances, nor is skipped by, the project's stream.
Only metadata is selected; source bodies and exception messages stay private.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

from . import fingerprints, repair_signals, repair_triage, storage
from .repair_correlation import ENVIRONNEMENT, git_is_ancestor

# U5 currently settles entries individually; cap database round trips per turn.
BATCH = 20

#: Alert subsets of :func:`scan`: the historical per-project page, and the
#: page of alerts that carry no project, observed under a declared scope.
PROJECT = "project"
WITHOUT_PROJECT = "without_project"


def project_checkout(cfg, project):
    """Un clone local du dépôt de `project` pour A4, ou ``''``.

    Le clone de release (`release.projects.<p>.repo`) est celui depuis lequel
    le staging est attesté ; la déclaration radar de l'hôte vient ensuite.
    Lecture seule : rien n'est récupéré, et un clone en retard laisse la
    relation non prouvée (`git_is_ancestor` rend ``None``) au lieu d'inventer
    une direction. Sans configuration ou sans clone, l'appelant garde le
    verdict `version-differente` d'avant #6486.
    """
    if cfg is None or not project:
        return ''
    candidates = []
    release = getattr(cfg, 'release', None)
    if isinstance(release, dict):
        entry = (release.get('projects') or {}).get(project)
        if isinstance(entry, dict):
            candidates.append(entry.get('repo'))
    try:
        from . import radar
        entry = radar.settings_of(cfg).projects.get(project)
        if entry is not None:
            candidates.append(getattr(entry, 'repo', ''))
    except Exception:  # noqa: BLE001 — une déclaration illisible ne prouve rien
        pass
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            path = os.path.abspath(os.path.expanduser(candidate.strip()))
        except (OSError, ValueError):
            continue
        # Un worktree lié a un fichier `.git`, un clone un dossier.
        if os.path.isdir(path) and os.path.exists(os.path.join(path, '.git')):
            return path
    return ''


def _alert_occurrences(db, project, keys, *, without_project=False):
    """Number of committed raises of each alert key, a source high-water count.

    One aggregate per page, then the identity of a raise is its alert key, so N
    raises of one key give N occurrences while one signal identity stays unique.
    The partial ``ameesh_events_alert_project_id_idx`` bounds the aggregate to
    the project's alerts (never the whole event log); the count is read from the
    source rows, never incremented by a delivery, so a page replay is neutral.
    For alerts carrying no project the same index bounds the aggregate to the
    ``project IS NULL`` rows: the whole event log is never swept.
    A raise whose lower event id commits late is still counted on a later pass:
    the count is an upper bound of the raises already visible, not a position.
    The caller keeps at least the duplicates of the page it already holds, so a
    concurrent retention prune cannot make an observed raise disappear.
    """
    keys = sorted({key for key in keys if isinstance(key, str) and key})
    if not keys:
        return {}
    if without_project:
        rows = db.query(
            "SELECT data->>'key' AS key, count(*) AS occurrences"
            " FROM ameesh_events WHERE project IS NULL AND type='alert.raised'"
            " AND data->>'key' = ANY(%s::text[]) GROUP BY 1", (keys,))
    else:
        rows = db.query(
            "SELECT data->>'key' AS key, count(*) AS occurrences"
            " FROM ameesh_events WHERE project=%s AND type='alert.raised'"
            " AND data->>'key' = ANY(%s::text[]) GROUP BY 1", (project, keys))
    return {row['key']: int(row['occurrences']) for row in rows}


def _alert_page(db, project, after, *, without_project=False):
    """One bounded page of alerts, ordered by event id, with metadata only.

    ``after`` is the last event id of the previous page. The partial index of
    migration 0557 serves both forms: ``project = <scope>`` for the historical
    scan, and ``project IS NULL`` for the mesh's own alerts.
    """
    columns = (
        "SELECT id, data->>'key' AS key, data->>'since' AS since,"
        " coalesce(nullif(data->>'revision',''),data->>'observed_version','') AS version,"
        # #6485: a JSON null or absent key is no alias; any other non-string
        # value becomes '' so that ingest refuses the row, never coerces it.
        " CASE WHEN coalesce(jsonb_typeof(data->'fingerprint_key'),'null')='null'"
        " THEN NULL WHEN jsonb_typeof(data->'fingerprint_key')='string'"
        " THEN data->>'fingerprint_key' ELSE '' END"
        " AS fingerprint_key,"
        " extract(epoch FROM created_at) AS observed"
        " FROM ameesh_events WHERE ")
    if without_project:
        return db.query(
            columns + "project IS NULL AND type='alert.raised'"
            " AND id>%s ORDER BY id LIMIT %s", (after, BATCH))
    return db.query(
        columns + "project=%s AND type='alert.raised'"
        " AND id>%s ORDER BY id LIMIT %s", (project, after, BATCH))


def _alert_inputs(rows, project, occurrences):
    """Turn one alert page into sources for :func:`ingest`.

    ``occurrences`` is the committed high-water count of each key; the page's
    own duplicates are a lower bound used when a concurrent prune removed a
    raise between the two reads.
    """
    page_occurrences = {}
    for row in rows:
        key = row['key']
        if isinstance(key, str) and key:
            page_occurrences[key] = page_occurrences.get(key, 0) + 1
    inputs = []
    for row in rows:
        try:
            since = float(row['since']) if row.get('since') else row['observed']
        except (ValueError, TypeError):
            since = row['observed']  # malformed source timestamp: observation time
        key = row['key']
        item = {'key': row['id'],
                'occurrence': max(occurrences.get(key, 0),
                                  page_occurrences.get(key, 1)),
                'observed_version': row['version'], 'entry': {
                    'key': key, 'project': project, 'since': since}}
        # #6485: a producer that knows the historical fingerprint of the
        # alert writes it as `fingerprint_key`; absent, no alias is invented.
        if row.get('fingerprint_key') is not None:
            item['fingerprint_key'] = row['fingerprint_key']
        inputs.append(item)
    return inputs


def ingest(db, *, project, environment, source, rows, expected_cursor,
           next_cursor, now, repo=''):
    """Filter poison entries, record refusals, then atomically receive valid ones.

    Rows contain a stable metadata ``key``, a source ``entry`` and optionally
    structured ``facts`` and an existing typed ``outcome``/``reference``.
    Qualification is advisory: even a confirmed route never opens anything.
    An outcome is recorded only with proof supplied by the existing producer.

    ``repo`` is a local clone of the project's repository; when it is given,
    A4's bounded git predicate is injected into :func:`fingerprints.observe_cause`
    so a recurrence on an earlier commit is classified as ``old_version``
    (#6486). Without it, unequal SHAs stay ``version-differente`` as before.
    """
    api = storage.of(db).repair_signals
    is_ancestor = git_is_ancestor(repo) if repo else None
    accepted = []
    for row in rows:
        try:
            signal = repair_signals.from_source(
                source, row['entry'], project=project, environment=environment,
                received_at=now, occurrence=row.get('occurrence', 1),
                cause_key=row.get('cause_key', ''),
                observed_version=row.get('observed_version', ''))
            if row.get('fingerprint_key') is not None:
                # #6485: an unusable historical key refuses the row here,
                # where it is counted, instead of failing its A3 receipt
                # after receipt (a warning and a lost observation).
                fingerprints.historical_alias(project, row['fingerprint_key'])
        except (repair_signals.SignalError, fingerprints.FingerprintError):
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
        accepted.append((signal, outcome, row.get('reference', ''), triage,
                         row.get('fingerprint_key')))
    if not api.receive(project, environment, source,
                       [signal for signal, _, _, _, _ in accepted],
                       expected_cursor=expected_cursor, next_cursor=next_cursor):
        return False
    for signal, outcome, reference, triage, legacy_key in accepted:
        settled = api.settle(signal.id, outcome, reference=reference)
        if not settled and outcome in ('lot', 'basket', 'decision'):
            api.settle(signal.id, outcome, reference=reference, expected='unknown')
    # U7 is advisory: finish settlement of the entire U5 page first.
    for signal, outcome, reference, triage, legacy_key in accepted:
        lot = (int(reference[4:]) if outcome == 'lot'
               and reference.startswith('lot:') and reference[4:].isdigit() else None)
        try:
            fingerprints.observe_cause(db, signal, triage, lot=lot,
                                       legacy_key=legacy_key, now=now,
                                       is_ancestor=is_ancestor)
        except Exception as exc:  # one advisory receipt cannot interrupt U5
            logging.getLogger(__name__).warning(
                'repair-cause-observation: source_error (%s)',
                type(exc).__name__[:80])
    return True


def _scan_without_project(db, scope, *, environment, now, repo=''):
    """One bounded page of alerts carrying no project, under a declared scope.

    The scope is the mesh project the caller read from the configuration: those
    host and account findings belong to it, but they do not share the project
    stream's scan position. Their position lives in
    ``repair_observation_scopes`` and is committed in the same transaction as
    the journal receipt: a crash either replays the page (deduplicated by
    identity and occurrence) or advances both. The journal stream's cursor is
    read and written back unchanged, so a project scan and this scan never make
    each other skip a raise; a concurrent project page only makes this one
    retry, it never loses the receipt.
    """
    with db.transaction() as tx:
        api = storage.of(tx).repair_signals
        tx.execute(
            "INSERT INTO repair_observation_scopes(scope,environment,source)"
            " VALUES (%s,%s,'alerts') ON CONFLICT DO NOTHING",
            (scope, environment))
        cursor = tx.query(
            "SELECT cursor FROM repair_observation_scopes"
            " WHERE scope=%s AND environment=%s AND source='alerts'"
            " FOR UPDATE", (scope, environment))[0]['cursor']
        try:
            after = int(cursor) if cursor is not None else 0
        except (TypeError, ValueError):
            after = 0  # malformed position: start over, replay is neutral
        rows = _alert_page(tx, scope, after, without_project=True)
        next_cursor = str(rows[-1]['id']) if rows else None
        if rows:
            inputs = _alert_inputs(
                rows, scope, _alert_occurrences(
                    tx, scope, [row['key'] for row in rows],
                    without_project=True))
            stream = api.state(scope, environment, 'alerts')['cursor']
            if not ingest(tx, project=scope, environment=environment,
                          source='alerts', rows=inputs,
                          expected_cursor=stream, next_cursor=stream, now=now,
                          repo=repo):
                return False
        tx.execute(
            "UPDATE repair_observation_scopes SET cursor=%s, updated_at=now()"
            " WHERE scope=%s AND environment=%s AND source='alerts'",
            (next_cursor, scope, environment))
    return True


def scan(db, project, source, *, environment=ENVIRONNEMENT, now=None,
         alerts=PROJECT, repo=''):
    """One bounded circular page of committed alerts or GitHub deliveries.

    Wraparound deliberately trades extra deduplicated reads for no irreversible
    high-water skip when a source transaction commits out of ID order. A new
    holder resumes the shared scan, not the previous host's local state.
    Alert occurrences come from the source raises of the key, never from the
    number of pages that delivered it (see :func:`_alert_occurrences`).
    ``alerts`` selects the alert subset: ``PROJECT`` (events carrying
    ``project``, the historical scan) or ``WITHOUT_PROJECT`` (events carrying
    none, observed under ``project`` as the declared mesh scope, with a
    position of their own). ``repo`` is the project's local clone for A4
    ancestry, or ``''`` (no predicate).
    """
    now = time.time() if now is None else now
    api = storage.of(db).repair_signals
    cursor = api.state(project, environment, source)['cursor']
    if source == 'alerts':
        if alerts == WITHOUT_PROJECT:
            return _scan_without_project(db, project, environment=environment,
                                         now=now, repo=repo)
        if alerts != PROJECT:
            raise ValueError('unsupported alert subset')
        after = int(cursor) if cursor is not None else 0
        rows = _alert_page(db, project, after)
        inputs = _alert_inputs(rows, project, _alert_occurrences(
            db, project, [row['key'] for row in rows]))
        next_cursor = str(rows[-1]['id']) if rows else None
    elif source == 'github_watch':
        if alerts != PROJECT:
            raise ValueError('unsupported alert subset')
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
                  rows=inputs, expected_cursor=cursor, next_cursor=next_cursor,
                  now=now, repo=repo)


def observe(db, *, project=None, source='alerts', now=None, log=None,
            mesh_project=None, cfg=None):
    """Best effort at the existing entry points; one source cannot stop another.

    No source failure is recursively converted into a signal or a new lot.
    A failed receipt retains its cursor; an interrupted settlement is replayed
    on the next scan cycle. Diagnostics carry only a bounded exception class.

    ``mesh_project`` is the declared scope of alerts that carry no project,
    read by the caller from the configuration (the mesh project); it is never
    defaulted to a built-in name here. Without it, those alerts are not
    observed at all rather than attributed to an invented project.
    ``cfg`` lets the observation resolve each project's local clone
    (:func:`project_checkout`) for A4 ancestry; without it, no predicate is
    injected and unequal SHAs keep the historical ``version-differente``.
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
            scan(db, name, source, now=now,
                 repo=project_checkout(cfg, name))
        except Exception as exc:
            log('repair-observation: source_error (%s)' % type(exc).__name__)
    if project is not None or source != 'alerts':
        return
    scope = (mesh_project or '').strip()
    if not scope:
        log('repair-observation: scope_unset')
        return
    try:
        scan(db, scope, source, now=now, alerts=WITHOUT_PROJECT,
             repo=project_checkout(cfg, scope))
    except Exception as exc:
        log('repair-observation: source_error (%s)' % type(exc).__name__)
