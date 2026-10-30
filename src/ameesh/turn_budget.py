# SPDX-License-Identifier: AGPL-3.0-only
"""S3 runner adapter: the admitted Agent.sandbox.tranche is the ONLY selector.

No environment, Host or CLI override; profiles without a tranche retain the
existing account-budget path. No real budget row is synthesized.
"""
from __future__ import annotations

import logging

from . import account_budget, canon, canon_sync, placement
from . import db as db_mod
from .storage.postgres.turn_admission import Refusal, ameesh_admit_turn


def local_tranche(cfg, db, agent, *, canon_id=None, memory=None):
    if not getattr(cfg, 'canon', None):
        return None
    memory = memory if memory is not None else {}
    key = (canon_id, agent)
    try:
        matches = [c for c in canon.load_configured(cfg) if c.owns(canon_id)]
        if len(matches) != 1:
            raise Refusal('profile-refused')
        loaded = matches[0]
        state = canon_sync.state(db, cfg.host, loaded)
        pinned = str((state or {}).get('last_good_commit') or '')
        if pinned and (not loaded.sources or loaded.sources[0].commit != pinned):
            loaded = canon.load(loaded.root, ref=pinned, default=loaded.is_default)
        if not loaded.readable or loaded.untrusted:
            raise Refusal('profile-refused')
        card = loaded.agent(agent)
        profile = card.sandbox if card else None
        tranche = profile.tranche if profile else None
        memory[key] = tranche or None
    except Exception:
        if key in memory:
            if memory[key] is None:
                return None
        elif not db.query("select id from tranche_limits where state = 'open' limit 1"):
            logging.getLogger(__name__).warning(
                'tranche selector fallback: canon unavailable, no memory and no open tranche '
                '(canon=%r agent=%s); using account admission', canon_id, agent)
            return None
        raise Refusal('profile-refused') from None
    if not tranche:
        return None
    # Remember a declared selector even when stale/revoked: an incident must
    # never turn a known participant into an ordinary account-only agent.
    if (not placement.canon_fresh(state)[0] or profile.status != 'admitted'
            or canon.sandbox_profile_problems(profile)):
        raise Refusal('profile-refused')
    return tranche


def reserve_local(cfg, db, agent, profile, spend_key, turn_id, *, canon_id=None, retry=False, memory=None):
    try:
        tranche = local_tranche(cfg,db,agent,canon_id=canon_id,memory=memory)
        if tranche is None:
            return account_budget.reserve(db,agent,profile,spend_key)
        account = account_budget.billing_of(profile)
        if not account or not spend_key or not turn_id:
            return 'tranche-unavailable: missing billing account or turn identity'
        ameesh_admit_turn(db,agent,account,profile.reserve_usd,spend_key,tranche,
                          'retry' if retry else 'runner',turn_id=turn_id)
        return ''
    except db_mod.Unavailable:
        raise
    except (Refusal,canon.CanonError,db_mod.DbError) as exc:
        return 'tranche admission refused: %s' % exc
