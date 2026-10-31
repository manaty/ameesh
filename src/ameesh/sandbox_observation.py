# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only sandbox projection shared by API, CLI and turn observation (#4972).

Explicit columns and closed projections: never return a result payload, prompt,
provider credentials or a supervisor handoff. No Kubernetes I/O on a read.
"""
from __future__ import annotations

STATES = ('intent', 'created', 'bound', 'running', 'cancelling', 'succeeded',
          'failed', 'cancelled', 'expired', 'orphaned', 'lost')
MAX_LIMIT = 1000

# The only selected result fields are the bounded counters/verdict and receipt.
_SELECT = """
SELECT s.turn_id, s.agent, s.owner, s.lease_epoch, s.state, s.scope,
       s.scope_digest, s.profile_ref, s.placement_ref, s.canon_ref,
       s.namespace, s.job_name, s.job_uid, s.job_phase, s.result_sha256,
       s.refusal, s.source,
       extract(epoch from s.deadline_ts)::float8 AS deadline_ts,
       extract(epoch from s.started_at)::float8 AS started_ts,
       extract(epoch from s.completed_at)::float8 AS completed_ts,
       r.payload->'usage' AS counters, r.payload->>'verdict' AS verdict,
       r.receipt, t.tranche_id, t.state AS tranche_state,
       t.reserved_usd::float8 AS reserved_usd, t.usd::float8 AS tranche_usd,
       l.project
  FROM sandbox_turns s
  LEFT JOIN sandbox_results r ON r.turn_id = s.turn_id
  LEFT JOIN tranche_reservations t ON t.spend_key = s.spend_key
  LEFT JOIN tranche_limits l ON l.id = t.tranche_id
"""


class Unknown(LookupError):
    code = 'unknown-turn'


def _pick(value, keys):
    return {k: value[k] for k in keys if k in value}


_SCOPE = ('repository', 'pr', 'head', 'base', 'generation', 'attempt')
_SOURCE = ('release', 'commit', 'contract')


def project(row):
    """Closed public shape, including a separate tranche accounting block."""
    tranche = None
    if row.get('tranche_id') is not None:
        state = row['tranche_state']
        tranche = dict(tranche_id=row['tranche_id'], state=state,
                       reserved_usd=row['reserved_usd'],
                       settled_usd=row['tranche_usd'] if state == 'settled' else None,
                       kept_usd=row['tranche_usd'] if state == 'kept' else None)
    counters = row.get('counters')
    if counters is not None:
        counters = {k: counters.get(k) for k in ('turns', 'tool_calls', 'seconds', 'model_calls')}
    receipt = row.get('receipt')
    if receipt is not None:
        receipt = {k: receipt.get(k) for k in (
            'schema', 'turn_id', 'epoch', 'scope', 'scope_digest', 'result_id',
            'result_sha256', 'job', 'source', 'replayed')}
        receipt['scope'] = _pick(receipt.get('scope') or {}, _SCOPE)
        receipt['source'] = _pick(receipt.get('source') or {}, _SOURCE)
        receipt['job'] = _pick(receipt.get('job') or {}, ('namespace', 'name', 'uid'))
    return dict(schema='ameesh-sandbox-turn/1', turn_id=row['turn_id'],
                agent=row['agent'], owner=row['owner'], epoch=row['lease_epoch'],
                state=row['state'], scope=_pick(row['scope'], _SCOPE), scope_digest=row['scope_digest'],
                profile_ref=row['profile_ref'], placement_ref=row['placement_ref'],
                canon_ref=row['canon_ref'], source=_pick(row['source'], _SOURCE),
                job=dict(namespace=row['namespace'], name=row['job_name'],
                         uid=row['job_uid'], phase=row['job_phase']),
                deadline_ts=row['deadline_ts'], started_ts=row['started_ts'],
                completed_ts=row['completed_ts'], counters=counters,
                result_sha256=row['result_sha256'], verdict=row.get('verdict'),
                receipt=receipt, refusal=row['refusal'], tranche=tranche)


def _visible(row, sees):
    return sees is None or sees(dict(project=row.get('project'),
                                     object='turn:' + row['turn_id']))


def get(db, turn_id, *, sees=None):
    rows = db.query(_SELECT + ' WHERE s.turn_id = %s', (turn_id,))
    if not rows or not _visible(rows[0], sees):
        return None
    return project(rows[0])


def show(db, turn_id, *, sees=None):
    result = get(db, turn_id, sees=sees)
    if result is None:
        raise Unknown('unknown-turn')
    return result


def listing(db, *, agent=None, state=None, scope=None, limit=100, sees=None):
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError('limit must be between 1 and %d' % MAX_LIMIT)
    if state is not None and state not in STATES:
        raise ValueError('unknown sandbox state')
    clauses, values = [], []
    for column, value in (('agent', agent), ('state', state), ('scope_digest', scope)):
        if value is not None:
            clauses.append('s.' + column + ' = %s')
            values.append(value)
    sql = _SELECT + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
    rows = db.query(sql + ' ORDER BY s.started_at DESC, s.turn_id LIMIT %s',
                    tuple(values) + (limit,))
    return dict(schema='ameesh-sandbox-turns/1',
                turns=[project(row) for row in rows if _visible(row, sees)])


def current_agents(db, names, *, sees=None):
    """Current resource identity only; never label a previous turn as current."""
    if not names:
        return {}
    rows = db.query(_SELECT + " JOIN turn_resources u ON u.turn_id = s.turn_id"
                    " AND u.agent = s.agent AND u.status = 'running'"
                    " JOIN agent_registry a ON a.name = s.agent"
                    " AND a.lease_owner = s.owner AND a.lease_epoch = s.lease_epoch"
                    " WHERE s.agent IN (" + ",".join("%s" for _ in names) + ")"
                    " ORDER BY s.started_at, s.turn_id", tuple(names))
    return {row['agent']: project(row) for row in rows if _visible(row, sees)}


def alert_facts(db):
    """Live correlations only; facts, never a new observation of a Job."""
    return db.query("""
        SELECT s.turn_id, s.agent, s.state, s.refusal, s.job_phase,
               s.result_id, s.cancel_observed_at,
               extract(epoch from s.started_at)::float8 AS started_ts,
               extract(epoch from s.deadline_ts)::float8 AS deadline_ts,
               (a.lease_owner = s.owner AND a.lease_epoch = s.lease_epoch
                AND a.lease_expires_at > clock_timestamp()) AS lease_live
          FROM sandbox_turns s LEFT JOIN agent_registry a ON a.name = s.agent
         WHERE s.state NOT IN ('succeeded','failed','cancelled','expired','lost')
    """)
