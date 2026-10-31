# SPDX-License-Identifier: AGPL-3.0-only
"""S3's shared money boundary. No nested transaction in admission/settlement.

Trusted runner inputs only; this is not an external lifecycle write API.
Sandbox lifecycle takes sandbox/registry locks before calling settlement.
Admission lock order: placement, retry family, tranche, account. All admission
paths use the tranche row as their serialization point. Replays precede gates.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from ... import jcs
from ...sandbox_provider import job_name
from .budgets import Budgets
from .sandbox import Refusal, TERMINAL


def _money(value, *, positive=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (positive and number <= 0):
            raise ValueError
        # The SQL storage unit is a micro-dollar. Never round a bound down.
        if number != number.quantize(Decimal('0.000001')):
            raise ValueError
        return number
    except (ValueError, InvalidOperation):
        raise Refusal('invalid-cost') from None


def _lock(tx, key):
    tx.query("select pg_advisory_xact_lock(hashtextextended(current_schema() || ':' || %s,0))",
             (key,))


def _digest(value):
    return 'sha256:' + hashlib.sha256(jcs.canonicalize(value)).hexdigest()


def _event(tx, tranche, action, actor, reason, *, reservation=None, data=None):
    tx.execute('insert into tranche_events (tranche_id,reservation_id,action,actor,reason,data)'
               ' values (%s,%s,%s,%s,%s,%s::jsonb)',
               (tranche,reservation,action,actor,reason,json.dumps(data or {})))


def _available(tx,tranche_id):
    if not tx.query("select id from tranche_limits where id = %s and state = 'open'"
                    ' and not overdraft and starts_at <= clock_timestamp()'
                    ' and (ends_at is null or ends_at > clock_timestamp())',(tranche_id,)):
        raise Refusal('tranche-unavailable')


def _replay(tx, *, agent, account, usd, key, tranche, turn_id, path, snapshot):
    rows = tx.query('select r.*, a.id as account_reservation_id from tranche_reservations r'
                    ' join account_reservations a on a.spend_key = r.spend_key'
                    ' where r.spend_key = %s', (key,))
    if not rows:
        # A spend key already admitted outside this boundary cannot be adopted.
        if tx.query('select id from account_reservations where spend_key = %s',(key,)):
            raise Refusal('replay-mismatch')
        return None
    row = rows[0]
    if (row['agent'],row['billing_account'],_money(row['reserved_usd']),row['tranche_id'],
            row['turn_id'],row['path']) != (agent,account,usd,tranche,turn_id,path):
        raise Refusal('replay-mismatch')
    sand = tx.query('select * from sandbox_turns where spend_key = %s',(key,))
    if bool(sand) != bool(snapshot):
        raise Refusal('replay-mismatch')
    if snapshot:
        old = sand[0]
        if any(old[k] != v for k,v in snapshot.items()):
            raise Refusal('replay-mismatch')
    result = dict(tranche_reservation_id=row['id'],
                  account_reservation_id=row['account_reservation_id'], replayed=True)
    if sand:
        result.update({k:sand[0][k] for k in
                       ('turn_id','attempt','scope','scope_digest','job_name','deadline_ts')})
    return result


def ameesh_admit_turn(db, agent, billing_account, reserve_usd, spend_key,
                      tranche_id, path, *, turn_id, sandbox=None):
    """Reserve both envelopes, optionally creating S1's intent atomically.

    `sandbox` is a trusted S2 snapshot plus owner/epoch, source, request_ids,
    scope WITHOUT attempt, max_attempts and max_concurrent_jobs. The caller
    validates request authorization/canon freshness before NEW admission;
    replay is returned unchanged even when new admissions are now refused.
    """
    usd = _money(reserve_usd, positive=True)
    if not all(isinstance(v,str) and v for v in
               (agent,billing_account,spend_key,tranche_id,path,turn_id)):
        raise Refusal('missing')
    snapshot = None
    if sandbox is not None:
        snapshot = dict(sandbox)
        expected = {'owner','lease_epoch','request_ids','scope','max_attempts','profile_ref',
                    'profile_digest','placement_ref','placement_digest','canon_ref','limits',
                    'image_digest','provider','namespace','source','max_concurrent_jobs'}
        if set(snapshot) != expected:
            raise Refusal('missing')
        max_jobs = snapshot.pop('max_concurrent_jobs')
        if type(max_jobs) is not int or max_jobs < 1:
            raise Refusal('profile-refused')
        ids = snapshot['request_ids']
        if not isinstance(ids,(list,tuple)) or any(type(v) is not int or v < 1 for v in ids):
            raise Refusal('missing')
        snapshot['request_ids'] = list(ids)
        scope = snapshot['scope']
        if 'attempt' in scope:
            raise Refusal('scope-mismatch')
        snapshot['retry_family'] = _digest(scope)
        snapshot['scope_generation'] = scope['generation']
        snapshot['turn_id'] = turn_id
        snapshot['agent'] = agent
        snapshot['tranche_id'] = tranche_id
        snapshot['spend_key'] = spend_key
        if type(snapshot['max_attempts']) is not int or snapshot['max_attempts'] < 1:
            raise Refusal('profile-refused')
        # A deadline is computed only once at admission; replay compares the
        # immutable limit snapshot, not a freshly computed wall-clock value.
        if _money(snapshot['limits']['usd_per_turn'],positive=True) != usd:
            raise Refusal('profile-refused')
    with db.transaction() as tx:
        # Compare scope without server-assigned attempt on response-loss replay.
        replay_snapshot = dict(snapshot) if snapshot else None
        if replay_snapshot:
            replay_snapshot.pop('scope')
        replay = _replay(tx,agent=agent,account=billing_account,usd=usd,key=spend_key,
                         tranche=tranche_id,turn_id=turn_id,path=path,snapshot=replay_snapshot)
        if replay:
            if snapshot and {k:v for k,v in replay['scope'].items() if k != 'attempt'} != scope:
                raise Refusal('replay-mismatch')
            return replay
        if snapshot:
            placement_key = snapshot['placement_ref'].rsplit('@',1)[0]
            _lock(tx,'sandbox-placement:' + placement_key)
            _lock(tx,'sandbox-family:' + snapshot['retry_family'])
        # A common key lock also serializes non-sandbox duplicate submissions
        # across different tranches; it is before every tranche/account lock.
        _lock(tx,'turn-admission:' + spend_key)
        replay = _replay(tx,agent=agent,account=billing_account,usd=usd,key=spend_key,
                         tranche=tranche_id,turn_id=turn_id,path=path,snapshot=replay_snapshot)
        if replay:
            if snapshot and {k:v for k,v in replay['scope'].items() if k != 'attempt'} != scope:
                raise Refusal('replay-mismatch')
            return replay
        if snapshot:
            count = tx.query('select count(*) as n from sandbox_turns where retry_family = %s',
                             (snapshot['retry_family'],))[0]['n']
            if count >= snapshot['max_attempts']:
                raise Refusal('retry-exhausted')
            live = tx.query("select count(*) as n from sandbox_turns where regexp_replace(placement_ref, '@[^@]*$', '') = %s"
                            " and state not in ('succeeded','failed','cancelled','expired','lost')",
                            (placement_key,))[0]['n']
            if live >= max_jobs:
                raise Refusal('placement-full')
            if tx.query('select turn_id from sandbox_turns where agent = %s'
                        " and state not in ('succeeded','failed','cancelled','expired','lost')",(agent,)):
                raise Refusal('turn-still-live')
            # Refuse stale owner, even when a turn_resources row was never attached.
            if not tx.query('select name from agent_registry where name = %s'
                            ' and lease_owner = %s and lease_epoch = %s'
                            ' and lease_expires_at > clock_timestamp()',
                            (agent,snapshot['owner'],snapshot['lease_epoch'])):
                raise Refusal('stale')
            snapshot['attempt'] = count + 1
            snapshot['scope'] = dict(scope,attempt=count + 1)
            snapshot['scope_digest'] = _digest(snapshot['scope'])
            snapshot['job_name'] = job_name(turn_id)
        limits = tx.query('select * from tranche_limits where id = %s for update',(tranche_id,))
        _available(tx,tranche_id)
        used = tx.query('select coalesce(sum(usd),0)::text as usd from tranche_reservations'
                        " where tranche_id = %s and state in ('open','kept','settled')",
                        (tranche_id,))[0]['usd']
        if _money(used) + usd > _money(limits[0]['cap_usd']):
            raise Refusal('tranche-exhausted')
        account = Budgets(db).account_reserve_tx(tx,billing_account,agent,usd,spend_key)
        if not account['ok']:
            raise Refusal('account-exhausted')
        # Both locks may have waited across the end of the tranche/lease.
        _available(tx,tranche_id)
        if snapshot and not tx.query('select name from agent_registry where name = %s'
                                     ' and lease_owner = %s and lease_epoch = %s'
                                     ' and lease_expires_at > clock_timestamp()',
                                     (agent,snapshot['owner'],snapshot['lease_epoch'])):
            raise Refusal('stale')
        row = tx.query('insert into tranche_reservations (tranche_id,spend_key,turn_id,'
                       'agent,billing_account,scope_digest,path,reserved_usd,usd)'
                       ' values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id',
                       (tranche_id,spend_key,turn_id,agent,billing_account,
                        snapshot['scope_digest'] if snapshot else None,path,usd,usd))[0]
        if snapshot:
            seconds = snapshot['limits']['turn_seconds']
            if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not 0 < seconds < float('inf'):
                raise Refusal('profile-refused')
            columns = tuple(snapshot)
            json_cols = {'scope','limits','source'}
            tx.execute('insert into sandbox_turns (' + ','.join(columns) + ',deadline_ts) values ('
                       + ','.join('%s::jsonb' if c in json_cols else '%s::bigint[]' if c == 'request_ids' else '%s' for c in columns)
                       + ",clock_timestamp() + %s * interval '1 second')",
                       tuple(json.dumps(snapshot[c]) if c in json_cols else
                             '{' + ','.join(map(str,snapshot[c])) + '}' if c == 'request_ids' else snapshot[c]
                             for c in columns) + (seconds,))
        _event(tx,tranche_id,'admit',agent,path,reservation=row['id'],data={'spend_key':spend_key})
        result = dict(tranche_reservation_id=row['id'],account_reservation_id=account['id'],replayed=False)
        if snapshot:
            result.update({k:snapshot[k] for k in ('turn_id','attempt','scope','scope_digest','job_name')})
            result['deadline_ts'] = tx.query('select deadline_ts from sandbox_turns where turn_id = %s',
                                             (turn_id,))[0]['deadline_ts']
        return result


def settle_tx(tx, spend_key, actual_usd=None, *, sandbox_turn=None, actor='runner'):
    """Internal callback boundary: caller owns transaction, no nested commit.

    Sandbox cost comes only from tagged relay rows, never the Job envelope.
    Unknown outcome remains kept at the full reserved bound. Terminal replay
    never changes a settlement; human reconciliation is separate and audited.
    """
    rows = tx.query('select * from tranche_reservations where spend_key = %s',(spend_key,))
    if not rows:
        raise Refusal('missing-reservation')
    row = rows[0]
    tx.query('select id from tranche_limits where id = %s for update',(row['tranche_id'],))
    _lock_account(tx,row['billing_account'])
    row = tx.query('select * from tranche_reservations where spend_key = %s for update',(spend_key,))[0]
    if row['state'] != 'open':
        return {'usd':float(row['usd']) if row['state'] == 'settled' else None,'state':row['state']}
    account = tx.query('select * from account_reservations where spend_key = %s for update',(spend_key,))
    if not account or account[0]['billing_account'] != row['billing_account']:
        raise Refusal('missing-reservation')
    if sandbox_turn is not None:
        if sandbox_turn['turn_id'] != row['turn_id'] or sandbox_turn['agent'] != row['agent']:
            raise Refusal('replay-mismatch')
        # A void/unknown row poisons the measurement; no relay row is never zero.
        measured = tx.query('select count(*) as n, count(usd) as priced,'
                            ' count(*) filter (where void_reason is not null) as uncertain,'
                            ' sum(usd)::text as usd from turn_costs where turn_id = %s'
                            " and billing_account = %s and source = 'relay'",
                            (row['turn_id'],row['billing_account']))[0]
        pending = tx.query("select id from tranche_relay_requests where turn_id = %s and state = 'open' limit 1",
                           (row['turn_id'],))
        actual_usd = (measured['usd'] if not pending and measured['n'] and measured['priced'] == measured['n']
                      and not measured['uncertain'] else None)
    actual = _money(actual_usd) if actual_usd is not None else None
    # An earlier unsettled account reservation has already been kept. Preserve
    # the same uncertainty on both envelopes rather than settle only half.
    if account[0]['state'] == 'kept':
        actual = None
    if account[0]['state'] not in ('open','kept'):
        raise Refusal('reservation-state-mismatch')
    state = 'kept' if actual is None else 'settled'
    amount = row['reserved_usd'] if actual is None else actual
    tx.execute('update turn_costs set billing_account = %s where spend_key = %s'
               ' and billing_account is null',(row['billing_account'],spend_key))
    tx.execute('update account_reservations set state = %s, closed_at = clock_timestamp(),'
               ' closed_by = %s where spend_key = %s',(state,actor,spend_key))
    tx.execute('update tranche_reservations set usd = %s,state = %s,closed_at = clock_timestamp(),'
               ' closed_by = %s where id = %s',(amount,state,actor,row['id']))
    if actual is not None and actual > _money(row['reserved_usd']):
        tx.execute('update tranche_limits set overdraft = true where id = %s',(row['tranche_id'],))
        _event(tx,row['tranche_id'],'overdraft',actor,'actual exceeds reserved bound',reservation=row['id'])
    _event(tx,row['tranche_id'],state,actor,'turn settlement',reservation=row['id'])
    return {'usd':None if actual is None else float(actual),'state':state}


def _lock_account(tx,account):
    # Same account key as the pre-existing daily-envelope boundary.
    tx.query('select pg_advisory_xact_lock(hashtext(%s))',('ameesh-account-budget:' + account,))


def ameesh_settle_turn(db, spend_key, actual_usd=None, *, actor='runner'):
    with db.transaction() as tx:
        # Local settlement may not pretend to be a sandbox measurement.
        if tx.query('select turn_id from sandbox_turns where spend_key = %s',(spend_key,)):
            raise Refusal('wrong-completion')
        return settle_tx(tx,spend_key,actual_usd,actor=actor)


def sandbox_settle(tx, admission, outcome):
    """S1 callback (tx, immutable admission row, observed outcome)."""
    return settle_tx(tx,admission['spend_key'],sandbox_turn=admission,actor='sandbox:' + outcome)


def tranche_show(db, tranche_id=None):
    return db.query('select l.*, coalesce((select sum(r.usd) from tranche_reservations r'
                    " where r.tranche_id = l.id and r.state in ('open','kept','settled')),0) as used_usd"
                    ' from tranche_limits l' + (' where l.id = %s' if tranche_id else '')
                    + ' order by l.id', (tranche_id,) if tranche_id else ())


def tranche_create(db, tranche_id, project, cap_usd, *, starts_at, ends_at=None,
                    actor, admitted_ref, tz='Europe/Paris'):
    from zoneinfo import ZoneInfo
    _human(actor)
    ZoneInfo(tz)
    amount = _money(cap_usd,positive=True)
    if amount != amount.quantize(Decimal('0.01')) or not all(
            isinstance(v,str) and v.strip() for v in (tranche_id,project,admitted_ref)):
        raise Refusal('invalid-tranche')
    with db.transaction() as tx:
        tx.execute('insert into tranche_limits (id,project,cap_usd,starts_at,ends_at,'
                   'admitted_by,admitted_ref,tz) values (%s,%s,%s,%s,%s,%s,%s,%s)',
                   (tranche_id,project,amount,starts_at,ends_at,actor,admitted_ref,tz))
        _event(tx,tranche_id,'create',actor,admitted_ref,data={'cap_usd':str(amount)})


def _human(actor):
    if not isinstance(actor,str) or not actor.startswith('human:') or not actor[6:]:
        raise Refusal('human-required')


def tranche_release(db, reservation_id, *, actor, why):
    _human(actor)
    if not isinstance(why,str) or not why.strip():
        raise Refusal('reason-required')
    with db.transaction() as tx:
        rows = tx.query('select * from tranche_reservations where id = %s',(reservation_id,))
        if not rows:
            raise Refusal('missing-reservation')
        row = rows[0]
        tx.query('select id from tranche_limits where id = %s for update',(row['tranche_id'],))
        _lock_account(tx,row['billing_account'])
        row = tx.query('select * from tranche_reservations where id = %s for update',(reservation_id,))[0]
        if row['state'] not in ('open','kept'):
            raise Refusal('reservation-state-mismatch')
        # A live Job/turn cannot be made free by releasing its budget.
        if tx.query('select turn_id from sandbox_turns where spend_key = %s'
                    " and state not in ('succeeded','failed','cancelled','expired','lost')",(row['spend_key'],)):
            raise Refusal('turn-still-live')
        tx.execute("update tranche_reservations set state = 'released',closed_at = clock_timestamp(),"
                   'closed_by = %s where id = %s',(actor,reservation_id))
        tx.execute("update account_reservations set state = 'released',closed_at = clock_timestamp(),"
                   "closed_by = %s where spend_key = %s and state in ('open','kept')",
                   (actor,row['spend_key']))
        _event(tx,row['tranche_id'],'release',actor,why,reservation=reservation_id)
        return row


def tranche_close(db, tranche_id, *, actor, why, reconcile=False):
    _human(actor)
    if not why or not why.strip():
        raise Refusal('reason-required')
    with db.transaction() as tx:
        rows = tx.query('select * from tranche_limits where id = %s for update',(tranche_id,))
        if not rows:
            raise Refusal('tranche-unavailable')
        if reconcile:
            used = tx.query('select coalesce(sum(usd),0)::text as usd from tranche_reservations'
                            " where tranche_id = %s and state in ('open','kept','settled')",
                            (tranche_id,))[0]['usd']
            if _money(used) > _money(rows[0]['cap_usd']):
                raise Refusal('tranche-exhausted')
            tx.execute('update tranche_limits set overdraft = false where id = %s',(tranche_id,))
        else:
            tx.execute("update tranche_limits set state = 'closed' where id = %s",(tranche_id,))
        _event(tx,tranche_id,'reconcile' if reconcile else 'close',actor,why)


def settle_local_reservation(db, spend_key, *, state, actor):
    """Compatibility for the runner's existing account settlement entry point.

    An admitted tranche turn never settles just its account half. A refusal
    before launch returns both bounds; an unknown/missing ledger keeps both.
    """
    with db.transaction() as tx:
        row = tx.query('select * from tranche_reservations where spend_key = %s',(spend_key,))[0]
        if tx.query('select turn_id from sandbox_turns where spend_key = %s',(spend_key,)):
            raise Refusal('wrong-completion')
        if state == 'released':
            tx.query('select id from tranche_limits where id = %s for update',(row['tranche_id'],))
            _lock_account(tx,row['billing_account'])
            # Trusted runner: before Popen only. This does not release a kept
            # uncertain turn; that remains a human gesture with a reason.
            changed = tx.query("update tranche_reservations set state = 'released',"
                               'closed_at = clock_timestamp(),closed_by = %s'
                               " where spend_key = %s and state = 'open' returning id",(actor,spend_key))
            if changed:
                tx.execute("update account_reservations set state = 'released',"
                           'closed_at = clock_timestamp(),closed_by = %s'
                           " where spend_key = %s and state = 'open'",(actor,spend_key))
                _event(tx,row['tranche_id'],'release',actor,'runner refused before launch',reservation=row['id'])
            return row
        actual = None
        if state == 'settled':
            costs = tx.query('select count(*) as n,count(usd) as priced,'
                             'count(*) filter (where void_reason is not null) as uncertain,'
                             'sum(usd)::text as usd from turn_costs where spend_key = %s'
                             " and source <> 'device'",(spend_key,))[0]
            if costs['n'] and costs['n'] == costs['priced'] and not costs['uncertain']:
                actual = costs['usd']
        elif state != 'kept':
            raise Refusal('invalid-settlement')
        settle_tx(tx,spend_key,actual,actor=actor)
        return row


def relay_begin(db, identity):
    """Durable uncertainty marker BEFORE a provider side effect, token identity only."""
    import uuid
    if not identity.turn:
        return None
    with db.transaction() as tx:
        rows = tx.query('select s.*,r.billing_account from sandbox_turns s'
                        ' join tranche_reservations r on r.spend_key = s.spend_key'
                        ' where s.turn_id = %s and s.agent = %s'
                        ' and s.owner = %s and s.lease_epoch = %s for update of s',
                        (identity.turn,identity.agent,identity.lease_owner,identity.lease_epoch))
        if not rows:
            if identity.executor.startswith('sandbox:'):
                raise Refusal('revoked')
            return None  # non-tranche relay use retains its existing behavior
        row=rows[0]
        # Verification happened before this transaction. Fence a lease lost
        # meanwhile before recording intent to contact the provider (#5140).
        from .sandbox import _live
        _live(tx, row, identity.lease_owner, identity.lease_epoch)
        if row['state'] in TERMINAL or row['cancel_requested_at'] or tx.query(
                'select turn_id from sandbox_turns where turn_id = %s'
                ' and deadline_ts <= clock_timestamp()', (identity.turn,)):
            raise Refusal('revoked')
        request_id=uuid.uuid4().hex
        tx.execute('insert into tranche_relay_requests (id,turn_id) values (%s,%s)',
                   (request_id,identity.turn))
        return request_id
