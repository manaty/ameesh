# SPDX-License-Identifier: AGPL-3.0-only
"""Turn-scoped relay credentials, issued only after durable admission (#5140).

Only the digest of a random bearer is stored. The verified identity (agent,
owner, lease epoch, host) and the deadline always come from the admission,
never from a caller's claims. The spend key is reached through the turn id
(`sandbox_turns.spend_key`), never carried by the token (#5217/N1).
"""
from __future__ import annotations

import hashlib
import re
import secrets

from . import relay
from .storage.postgres import sandbox as s1

PREFIX = 'amsb1.'
_TOKEN = re.compile(r'amsb1\.[A-Za-z0-9_-]{43}\Z')

#: states in which a sandbox turn admits no bearer and no longer fences the
#: agent's ordinary lease bearer; the sandbox storage boundary owns this set.
#: `expired`/`lost` may have no `completed_at`: retention is then bounded on
#: `deadline_ts` (the bearer was already useless after it).
_TERMINAL_STATES = tuple(sorted(s1.TERMINAL))
_TERMINAL_SQL = "('" + "','".join(_TERMINAL_STATES) + "')"


def _identity(tx, turn_id, agent, owner, epoch, host):
    # Same lock order as cancellation/completion: sandbox, then registry.
    row = s1._locked(tx, turn_id)
    if row['agent'] != agent:
        raise s1.Refusal('stale')
    s1._revoked(row)
    s1._live(tx, row, owner, epoch)
    s1._late(tx, row)
    registry = tx.query('select host from agent_registry where name = %s', (agent,))
    if not registry or registry[0]['host'] != host:
        raise s1.Refusal('stale')
    expiry = tx.query('select extract(epoch from deadline_ts)::float8 as expiry'
                      ' from sandbox_turns where turn_id = %s', (turn_id,))[0]['expiry']
    return relay.Identity('sandbox:' + turn_id, host, agent, owner, int(epoch),
                          turn=turn_id, expires_ts=float(expiry))


def issue(db, *, agent, owner, epoch, turn_id, host):
    """Return a bearer and verified identity; the DB clock bounds its life."""
    token = PREFIX + secrets.token_urlsafe(32)
    with db.transaction() as tx:
        identity = _identity(tx, turn_id, agent, owner, epoch, host)
        tx.execute('insert into sandbox_relay_tokens (token_sha256,turn_id,host)'
                   ' values (%s,%s,%s)', (hashlib.sha256(token.encode()).hexdigest(), turn_id, host))
    return token, identity


class Tokens:
    """Separate verifier: an ordinary lease bearer is never a sandbox bearer."""

    def __init__(self, db):
        self.db = db

    def verify_token(self, token):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise relay.TokenError('sandbox turn token required')
        try:
            with self.db.transaction() as tx:
                rows = tx.query('select s.turn_id,s.agent,s.owner,s.lease_epoch,t.host'
                                ' from sandbox_relay_tokens t'
                                ' join sandbox_turns s on s.turn_id = t.turn_id'
                                ' join agent_registry r on r.name = s.agent'
                                ' where t.token_sha256 = %s',
                                (hashlib.sha256(token.encode()).hexdigest(),))
                if not rows:
                    raise relay.TokenError('unknown sandbox turn token')
                row = rows[0]
                return _identity(tx, row['turn_id'], row['agent'], row['owner'],
                                 row['lease_epoch'], row['host'])
        except s1.Refusal as exc:
            raise relay.TokenError(exc.code) from None


class RelayTokens:
    """Preserve legacy relay use, fencing the lease bearer of a sandbox agent.

    Fencing is per (agent, owner, lease epoch) and only while the sandbox turn
    is live: a terminal turn no longer accepts its own bearer, so the ordinary
    lease bearer becomes usable again. A lease shared by several agents is
    supported (only the agent holding the live sandbox turn is fenced); two
    concurrent sandbox turns on one agent are not (unique live index,
    #5217/N5)."""

    def __init__(self, db, legacy):
        self.db, self.legacy = db, legacy
        self.sandbox = Tokens(db)

    def verify_token(self, token):
        if isinstance(token, str) and token.startswith(PREFIX):
            return self.sandbox.verify_token(token)
        identity = self.legacy.verify_token(token)
        if self.db.query('select 1 from sandbox_turns where agent = %s'
                         ' and owner = %s and lease_epoch = %s'
                         ' and state not in ' + _TERMINAL_SQL + ' limit 1',
                         (identity.agent, identity.lease_owner, identity.lease_epoch)):
            raise relay.TokenError('sandbox turn token required')
        return identity


def compose(db, legacy):
    """The relay verifier: the configured lease verifier, fenced for sandbox
    agents.

    Both relay entry points (the mediated server's `build_relay` and the
    autonomous `relay serve`) compose here: a sandbox bearer is verified by
    `Tokens`, and a legacy bearer for an agent holding a live sandbox turn is
    refused. Without it the autonomous relay would refuse every `amsb1.`
    bearer (it never reaches the legacy verifier) or accept a lease bearer
    for a sandbox agent (#5217)."""
    return RelayTokens(db, legacy)


def purge(db, *, days, limit: int = 1000) -> int:
    """Delete the token digests of turns closed for longer than `days` (#5217).

    A digest is useless once its turn is closed (`Tokens` re-checks the turn
    and its deadline), so only the digest goes: the turn, its result and its
    recorded costs stay. Bounded batch per pass; `days` is the retention in
    days and must be positive (0 = keep forever is handled by the caller).
    The age is measured on the DATABASE clock."""
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError('retention days must be a positive integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError('purge limit must be a positive integer')
    with db.transaction() as tx:
        rows = tx.query(
            'delete from sandbox_relay_tokens t'
            ' where t.token_sha256 in ('
            '   select t2.token_sha256 from sandbox_relay_tokens t2'
            '   join sandbox_turns s on s.turn_id = t2.turn_id'
            '   where s.state in ' + _TERMINAL_SQL +
            '   and coalesce(s.completed_at, s.deadline_ts)'
            '       < clock_timestamp() - make_interval(days => %s)'
            '   order by t2.token_sha256 limit %s)'
            ' returning t.token_sha256', (int(days), int(limit)))
        return len(rows)
