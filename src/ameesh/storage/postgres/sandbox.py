# SPDX-License-Identifier: AGPL-3.0-only
"""Dormant sandbox persistence boundary (#2202 S1).

Only the runner calls these functions. Each lifecycle operation locks the
correlation then the registry, and owns one transaction, on either DB driver.
No admission, supervisor, external write route or runtime activation lives here.
S3 supplies the settlement callback (tx, admission, outcome); it must settle
account AND tranche in that transaction. Missing settlement fails closed.
"""
from __future__ import annotations

import hashlib
import json
import logging

from ...db import bind, sql_literal

from ... import jcs

TERMINAL = frozenset(('succeeded', 'failed', 'cancelled', 'expired', 'lost'))
MAX_RESULT_BYTES = 65536


class Refusal(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def emit_observation(tx, event_type, object_ref, data):
    """Best-effort event in the lifecycle transaction, on both DB drivers.

    A server-side subtransaction preserves S1/S3 on insert errors (including
    query cancellation). Success commits with the transition, so rollback
    cannot leave an event and replay cannot duplicate its result.
    """
    statement = bind("insert into ameesh_events (type, object, data) values (%s, %s, %s::jsonb)",
                     (event_type, object_ref, json.dumps(data)))
    body = ("BEGIN PERFORM set_config('ameesh.sandbox_event_error', '', true); "
            "BEGIN EXECUTE " + sql_literal(statement) + "; "
            "EXCEPTION WHEN query_canceled OR OTHERS THEN "
            "PERFORM set_config('ameesh.sandbox_event_error', SQLSTATE, true); "
            "END; END")
    tx.execute("DO " + sql_literal(body))
    error = tx.query("select current_setting('ameesh.sandbox_event_error') as error")[0]['error']
    if error:
        logging.getLogger(__name__).warning("sandbox event %s could not be recorded (SQLSTATE %s)",
                                            event_type, error)


def _refuse(code):
    raise Refusal(code)


def _locked(tx, turn_id):
    rows = tx.query('select * from sandbox_turns where turn_id = %s for update',
                    (turn_id,))
    if not rows:
        _refuse('unknown-turn')
    return rows[0]


def _live(tx, row, owner, epoch):
    rows = tx.query('select name from agent_registry where name = %s'
                    ' and lease_owner = %s and lease_epoch = %s'
                    ' and lease_expires_at > clock_timestamp() for update',
                    (row['agent'], owner, epoch))
    # Check the clock again AFTER acquiring a contended registry lock.
    if (row['owner'] != owner or row['lease_epoch'] != epoch or not rows
            or not tx.query('select name from agent_registry where name = %s'
                            ' and lease_expires_at > clock_timestamp()',
                            (row['agent'],))):
        _refuse('stale')


def _revoked(row):
    if row['cancel_requested_at'] or row['state'] in ('cancelling', 'cancelled'):
        _refuse('revoked')


def _late(tx, row):
    if row['state'] in TERMINAL or tx.query(
            'select 1 as late from sandbox_turns where turn_id = %s'
            ' and deadline_ts <= clock_timestamp()', (row['turn_id'],)):
        _refuse('late')


def ameesh_turn_close(tx, name, owner, epoch, *, status, status_text, error,
                      cost_usd, sandbox_turn_id=None, turn_id=None):
    """Internal shared close; caller holds the sandbox/registry locks.

    Sandbox completion is called only after its unique result insertion. A
    local close cannot bypass the result boundary, even on a terminal row.

    The generic close is fenced by the identity of the turn it closes
    (``turn_id``), never by the latest ``turn_resources`` row (#4881): a local
    turn whose resource row could not be attached must still close, and a
    terminal sandbox turn stays bound to its own ``turn_id``. Without an
    identity (legacy caller) only a live sandbox turn on the same lease
    refuses, which fails closed without guessing.
    """
    if sandbox_turn_id is None:
        tx.query('select name from agent_registry where name = %s for update', (name,))
    if sandbox_turn_id is None:
        blocked = False
        if turn_id is not None:
            blocked = bool(tx.query(
                'select turn_id from sandbox_turns where turn_id = %s',
                (turn_id,)))
        if not blocked:
            blocked = bool(tx.query(
                'select turn_id from sandbox_turns where agent = %s'
                " and owner = %s and lease_epoch = %s"
                " and state not in ('succeeded','failed','cancelled','expired','lost')"
                ' limit 1', (name, owner, epoch)))
        if blocked:
            _refuse('wrong-completion')
    return bool(tx.query(
        'update agent_registry set status = %s,'
        ' status_text = coalesce(%s, status_text), last_error = %s,'
        ' current_prompt = null, turns = turns + 1,'
        ' spent_usd = spent_usd + coalesce(%s, 0), last_turn_at = now(),'
        ' last_seen = now(), updated_at = now()'
        ' where name = %s and lease_owner = %s and lease_epoch = %s returning name',
        (status, status_text, error, cost_usd, name, owner, epoch)))


def ameesh_sandbox_bind(db, turn_id, owner, epoch, job_uid):
    with db.transaction() as tx:
        row = _locked(tx, turn_id)
        _revoked(row)
        _live(tx, row, owner, epoch)
        _late(tx, row)
        if not isinstance(job_uid, str) or not job_uid:
            _refuse('missing')
        if row['job_uid'] and row['job_uid'] != job_uid:
            _refuse('job-conflict')
        if not row['job_uid']:
            tx.query("select pg_advisory_xact_lock(hashtextextended(current_schema() || ':' || %s, 0))",
                     ('sandbox-job:' + job_uid,))
            if tx.query('select turn_id from sandbox_turns where job_uid = %s',
                        (job_uid,)):
                _refuse('job-conflict')
            tx.execute("update sandbox_turns set job_uid = %s, state = 'bound'"
                       ' where turn_id = %s', (job_uid, turn_id))
            emit_observation(tx, 'sandbox.bound', 'turn:' + turn_id,
                             {'turn_id': turn_id, 'job_uid': job_uid})
        return {'turn_id': turn_id, 'job_uid': job_uid}


def _canonical(result):
    try:
        raw = jcs.canonicalize(result, doubles=True)
    except (jcs.JcsError, TypeError, ValueError, OverflowError, RecursionError):
        _refuse('missing')
    if len(raw) > MAX_RESULT_BYTES or not isinstance(result, dict):
        _refuse('missing')
    return raw, 'sha256:' + hashlib.sha256(raw).hexdigest()


def _validate(row, result):
    # §9 fixed precedence: a wrong echoed turn/epoch is `stale`, before the
    # scope comparison (#4881). Keep `missing` for an absent field.
    if ('turn_id' in result and 'epoch' in result
            and (result['turn_id'] != row['turn_id']
                 or result['epoch'] != row['lease_epoch'])):
        _refuse('stale')
    if (result.get('scope') != row['scope']
            or result.get('scope_digest') != row['scope_digest']):
        _refuse('scope-mismatch')
    required = ('schema', 'turn_id', 'epoch', 'result_id', 'outcome', 'verdict',
                'findings', 'evidence', 'usage', 'cost', 'sandbox_session',
                'source', 'finished_at')
    if any(key not in result for key in required):
        _refuse('missing')
    if result['schema'] != 'ameesh-sandbox-result/1':
        _refuse('contract-mismatch')
    if (not row['job_uid'] or not isinstance(result['result_id'], str)
            or not result['result_id'] or not isinstance(result['usage'], dict)):
        _refuse('missing')
    # The result payload is bounded and sanitised: exactly the four admitted
    # counters, never an arbitrary envelope (#4881).
    if set(result['usage']) != {'turns', 'tool_calls', 'seconds', 'model_calls'}:
        _refuse('missing')
    allowed = set(required) | {'scope', 'scope_digest', 'tools', 'capabilities', 'image_digest'}
    if set(result) - allowed:
        _refuse('missing')
    if result.get('image_digest', row['image_digest']) != row['image_digest']:
        _refuse('source-mismatch')
    if not isinstance(result['cost'], dict) or set(result['cost']) != {'usd'}:
        _refuse('missing')
    if type(result['cost']['usd']) not in (int, float) or result['cost']['usd'] < 0:
        _refuse('missing')
    if not all(isinstance(result[key], str) and result[key]
               for key in ('outcome', 'verdict', 'finished_at')):
        _refuse('missing')
    limits = row['limits']
    for counter in ('turns', 'tool_calls', 'seconds', 'model_calls'):
        value = result['usage'].get(counter)
        if type(value) is not int or value < 0:
            _refuse('missing')
        bound = limits.get(counter, limits.get('turn_seconds') if counter == 'seconds' else None)
        if bound is not None and value > bound:
            _refuse('limit-exceeded')
    for key in ('tools', 'capabilities'):
        declared = result.get(key, [])
        admitted = limits.get(key, [])
        if isinstance(admitted, dict):
            admitted = [name for name, enabled in admitted.items() if enabled is True]
        if key == 'capabilities' and isinstance(declared, dict):
            if any(type(value) is not bool for value in declared.values()):
                _refuse('missing')
            declared = [name for name, enabled in declared.items() if enabled]
        if (not isinstance(declared, list) or any(not isinstance(v, str) for v in declared)):
            _refuse('missing')
        if not set(declared) <= set(admitted):
            _refuse('profile-refused')
    if limits.get('session', 'none') == 'none' and result['sandbox_session'] is not None:
        _refuse('profile-refused')
    # When the profile admits a session, the identifier is a string or null;
    # a non-string must refuse as `missing`, never reach the text column and
    # fail in the driver (#4881, auditor N).
    session = result['sandbox_session']
    if session is not None and (not isinstance(session, str) or not session):
        _refuse('missing')
    if (not isinstance(result['source'], dict)
            or result['source'].get('contract') != row['source'].get('contract')):
        _refuse('contract-mismatch')
    if result['source'] != row['source']:
        _refuse('source-mismatch')
    # Keep bounded references and summaries, never arbitrary transcript blobs.
    for field, required_fields, allowed_fields in (
            ('findings', {'severity', 'code', 'summary'},
             {'severity', 'code', 'summary', 'path', 'line', 'url'}),
            ('evidence', {'kind'}, {'kind', 'head', 'name', 'conclusion', 'url', 'path', 'line'})):
        entries = result[field]
        if not isinstance(entries, list) or len(entries) > 128:
            _refuse('missing')
        for entry in entries:
            if (not isinstance(entry, dict) or not required_fields <= set(entry)
                    or set(entry) - allowed_fields):
                _refuse('missing')
            for key, value in entry.items():
                if key == 'line':
                    if type(value) is not int or value < 1:
                        _refuse('missing')
                elif not isinstance(value, str) or len(value) > 4096:
                    _refuse('missing')


def _finish(tx, row, *, settle, outcome, cost_usd=None, close_registry=True):
    # No callback means S3 is not installed; roll back rather than returning a
    # receipt whose two reservations were not settled atomically.
    if settle is None:
        raise RuntimeError('sandbox settlement boundary unavailable')
    settled = settle(tx, row, outcome)
    if settled is not None:
        cost_usd = settled.get('usd')
    if close_registry:
        agent = tx.query('select operator_paused, pending_prompt from agent_registry'
                         ' where name = %s', (row['agent'],))[0]
        queued = agent['operator_paused'] or bool(agent['pending_prompt'])
        if not ameesh_turn_close(
                tx, row['agent'], row['owner'], row['lease_epoch'],
                status='queued' if queued else 'idle', status_text=None, error=None,
                cost_usd=cost_usd, sandbox_turn_id=row['turn_id']):
            _refuse('stale')
    tx.execute('delete from spend_pending where agent = %s and turn_id = %s',
               (row['agent'], row['turn_id']))
    tx.execute("update turn_resources set status = 'done', ended_at = clock_timestamp()"
               ' where agent = %s and turn_id = %s', (row['agent'], row['turn_id']))


def ameesh_sandbox_complete(db, turn_id, owner, epoch, result, *, settle=None):
    with db.transaction() as tx:
        row = _locked(tx, turn_id)
        _revoked(row)
        # §4 steps 2-3 and §9: revocation and a stale lease win over the
        # stored-result path. A replay is `replayed` only while this owner and
        # epoch still hold the lease; changing that order would reverse the
        # design's fixed precedence (#4881, pinned by the tests).
        _live(tx, row, owner, epoch)
        raw, digest = _canonical(result)
        result = json.loads(raw)
        stored = tx.query('select result_sha256, receipt from sandbox_results'
                          ' where turn_id = %s', (turn_id,))
        if stored:
            if stored[0]['result_sha256'] != digest:
                _refuse('duplicate')
            return dict(stored[0]['receipt'], replayed=True)
        _late(tx, row)
        _validate(row, result)
        # Serialize the global id check across distinct turn row locks.
        tx.query("select pg_advisory_xact_lock(hashtextextended(current_schema() || ':' || %s, 0))",
                 ('sandbox-result:' + result['result_id'],))
        if tx.query('select turn_id from sandbox_results where result_id = %s',
                    (result['result_id'],)):
            _refuse('duplicate')
        receipt = dict(schema='ameesh-sandbox-receipt/1', turn_id=turn_id,
                       epoch=epoch, scope=row['scope'], scope_digest=row['scope_digest'],
                       result_id=result['result_id'], result_sha256=digest,
                       job=dict(namespace=row['namespace'], name=row['job_name'],
                                uid=row['job_uid']), source=row['source'], replayed=False)
        tx.execute('insert into sandbox_results (turn_id, epoch, result_id,'
                   ' result_sha256, payload, receipt) values (%s, %s, %s, %s, %s::jsonb, %s::jsonb)',
                   (turn_id, epoch, result['result_id'], digest, raw.decode(), json.dumps(receipt)))
        _finish(tx, row, settle=settle, outcome='succeeded')
        tx.execute("update sandbox_turns set state = 'succeeded', result_id = %s,"
                   ' result_sha256 = %s, sandbox_session = %s, completed_at = clock_timestamp()'
                   ' where turn_id = %s',
                   (result['result_id'], digest, result['sandbox_session'], turn_id))
        emit_observation(tx, 'sandbox.result', 'turn:' + turn_id,
                         {'turn_id': turn_id, 'result_id': result['result_id']})
        return receipt


def ameesh_sandbox_cancel(db, turn_id, owner, epoch, *, observed=False,
                          orphaned=False, settle=None, recovery=False,
                          terminal_state='cancelled'):
    """Fence, then record trusted supervisor observation (never user input).

    Even intent may hide a created Job after a lost create response. Absence
    must be observed explicitly; a null UID alone never proves no Job exists.
    recovery is reserved to the trusted orphan sweep: it fences the OLD row,
    observes/settles it, and never changes the current registry turn or lease.
    """
    with db.transaction() as tx:
        row = _locked(tx, turn_id)
        if recovery:
            if row['owner'] != owner or row['lease_epoch'] != epoch:
                _refuse('stale')
            if not (observed or orphaned):
                raise ValueError('recovery requires an observation or orphan report')
        else:
            _live(tx, row, owner, epoch)
        if row['result_id']:
            _refuse('already-completed')
        if row['state'] in TERMINAL:
            _refuse('not-cancellable')
        if observed and orphaned:
            raise ValueError('observation and orphaned are mutually exclusive')
        if terminal_state not in ('cancelled', 'expired', 'lost'):
            raise ValueError('invalid observation state')
        if terminal_state != 'cancelled' and not recovery:
            raise ValueError('expiry/loss observation is reserved to recovery')
        previous_state = row["state"]
        state = terminal_state if observed else ('orphaned' if orphaned else 'cancelling')
        if observed:
            _finish(tx, row, settle=settle, outcome=state, close_registry=not recovery)
        tx.execute('update sandbox_turns set cancel_requested_at ='
                   ' coalesce(cancel_requested_at, clock_timestamp()), state = %s,'
                   ' cancel_observed_at = case when %s then clock_timestamp()'
                   ' else cancel_observed_at end, completed_at = case when %s'
                   ' then clock_timestamp() else completed_at end where turn_id = %s',
                   (state, observed, observed, turn_id))
        if state != previous_state:
            emit_observation(tx, 'sandbox.orphan' if orphaned else 'sandbox.cancelled',
                             'turn:' + turn_id, {'turn_id': turn_id, 'state': state})
        return {'turn_id': turn_id, 'state': state}
