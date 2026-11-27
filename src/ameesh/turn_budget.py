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
        if not pinned:
            raise Refusal('profile-refused')
        if not loaded.sources or loaded.sources[0].commit != pinned:
            loaded = canon.load(loaded.root, ref=pinned, default=loaded.is_default)
        if (not loaded.readable or loaded.untrusted or not loaded.sources
                or loaded.sources[0].commit != pinned):
            raise Refusal('profile-refused')
        card = loaded.agent(agent)
        profile = card.sandbox if card else None
        tranche = profile.tranche if profile else None
        memory[key] = tranche or None
    except Exception:
        if key in memory:
            if memory[key] is None:
                return None
        elif not db.query("select id from tranche_limits limit 1"):
            logging.getLogger(__name__).warning(
                'tranche selector fallback: canon unavailable, no memory and empty tranche_limits '
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
    from . import accounts as accounts_mod
    from . import budget as budget_mod
    # #8347 : la part du plafond de la lignée se réserve AVANT l'enveloppe du
    # compte (une seule admission par lignée, verrou commun) ; un refus plus
    # loin rend la réservation, un tour refusé ne laisse rien d'ouvert.
    reserve_usd = float(getattr(profile, "reserve_usd", 0.0) or 0.0)
    if reserve_usd <= 0:
        reserve_usd = float(accounts_mod.DEFAULT_RESERVE_USD)
    try:
        tranche = local_tranche(cfg,db,agent,canon_id=canon_id,memory=memory)
        refus = budget_mod.reserve_lineage(cfg, db, agent, spend_key,
                                           reserve_usd=reserve_usd)
        if refus:
            return 'lineage admission refused: %s' % refus
        if tranche is None:
            refus = account_budget.reserve(db,agent,profile,spend_key)
        else:
            account = account_budget.billing_of(profile)
            if not account or not spend_key or not turn_id:
                refus = 'tranche-unavailable: missing billing account or turn identity'
            else:
                ameesh_admit_turn(db,agent,account,profile.reserve_usd,spend_key,
                                  tranche,'retry' if retry else 'runner',
                                  turn_id=turn_id)
                refus = ''
        if refus:
            budget_mod.release_lineage(db, spend_key)
        return refus
    except db_mod.Unavailable:
        budget_mod.release_lineage(db, spend_key)
        raise
    except (Refusal,canon.CanonError,db_mod.DbError) as exc:
        budget_mod.release_lineage(db, spend_key)
        return 'tranche admission refused: %s' % exc
