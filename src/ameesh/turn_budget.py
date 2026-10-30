# SPDX-License-Identifier: AGPL-3.0-only
"""S3 runner adapter: the admitted Agent.sandbox.tranche is the ONLY selector.

No environment, Host or CLI override; profiles without a tranche retain the
existing account-budget path. No real budget row is synthesized.
"""
from __future__ import annotations

from . import account_budget, canon, canon_sync, placement
from . import db as db_mod
from .storage.postgres.turn_admission import Refusal, ameesh_admit_turn


def local_tranche(cfg, db, agent, *, canon_id=None):
    if not getattr(cfg,'canon',None):
        return None
    matches = [c for c in canon.load_configured(cfg) if c.owns(canon_id)]
    if len(matches) != 1:
        raise Refusal('profile-refused')
    loaded = matches[0]
    state = canon_sync.state(db,cfg.host,loaded)
    pinned = str((state or {}).get('last_good_commit') or '')
    if pinned and (not loaded.sources or loaded.sources[0].commit != pinned):
        loaded = canon.load(loaded.root,ref=pinned,default=loaded.is_default)
    if not loaded.readable or loaded.untrusted:
        raise Refusal('profile-refused')
    card = loaded.agent(agent)
    if card is None or card.sandbox is None or not card.sandbox.tranche:
        return None
    # Freshness gates only tranche participants; ordinary agents keep the
    # existing runner's canon and account gates, with no new eligibility rule.
    if not placement.canon_fresh(state)[0]:
        raise Refusal('profile-refused')
    profile = card.sandbox
    if profile.status != 'admitted' or canon.sandbox_profile_problems(profile):
        raise Refusal('profile-refused')
    return profile.tranche


def reserve_local(cfg, db, agent, profile, spend_key, turn_id, *, canon_id=None, retry=False):
    try:
        tranche = local_tranche(cfg,db,agent,canon_id=canon_id)
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
