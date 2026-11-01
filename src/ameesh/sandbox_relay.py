# SPDX-License-Identifier: AGPL-3.0-only
"""Turn-scoped relay credentials, issued only after durable admission (#5140).

Only the digest of a random bearer is stored. Its identity, spend key and
deadline always come from the admission, never from a caller's claims.
"""
from __future__ import annotations

import hashlib
import re
import secrets

from . import relay
from .storage.postgres import sandbox as s1

PREFIX = 'amsb1.'
_TOKEN = re.compile(r'amsb1\.[A-Za-z0-9_-]{43}\Z')


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
    """Preserve legacy relay use, fencing lease tokens for sandbox agents."""

    def __init__(self, db, legacy):
        self.db, self.legacy = db, legacy
        self.sandbox = Tokens(db)

    def verify_token(self, token):
        if isinstance(token, str) and token.startswith(PREFIX):
            return self.sandbox.verify_token(token)
        identity = self.legacy.verify_token(token)
        if self.db.query('select 1 from sandbox_turns where agent = %s'
                         ' and owner = %s and lease_epoch = %s limit 1',
                         (identity.agent, identity.lease_owner, identity.lease_epoch)):
            raise relay.TokenError('sandbox turn token required')
        return identity


def compose(db, legacy):
    """The relay verifier: the configured lease verifier, fenced for sandbox
    agents.

    Both relay entry points (the mediated server's `build_relay` and the
    autonomous `relay serve`) compose here: a sandbox bearer is verified by
    `Tokens`, and a legacy bearer for an agent holding a sandbox turn is
    refused. Without it the autonomous relay would refuse every `amsb1.`
    bearer (it never reaches the legacy verifier) or accept a lease bearer
    for a sandbox agent (#5217)."""
    return RelayTokens(db, legacy)
