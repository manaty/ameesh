# SPDX-License-Identifier: AGPL-3.0-only
"""Shared daily USD envelope per provider billing account (#1469, issue #1101).

A host profile names the billing account it spends from
(`accounts.<harness>[].billing_account`, e.g. `openai-manaty`). Every key,
profile name, agent and host that names it shares one envelope, set by a human
(`ameesh budget set --account <id> --per-day <usd> [--tz Europe/Paris]`), per
**calendar day** in its zone (reset at 00:00 local; a DST day lasts 23 h or
25 h and keeps the same cap). It is distinct from the sliding 24 h cap of the
mesh.

**Admission.** Before a turn, the runner reserves the profile's `reserve_usd`
(the bound of one turn, 2 $ by default) under an advisory lock on the account,
in one transaction: settled spend of the day + open reservations + kept
reservations of the day + this one must fit the cap, whatever the number of
runners and hosts. The billing mode comes from the account, not the harness:
an API-key Codex profile with a billing account is reserved and counted.

**Settlement.** At the end of the turn the ledger row (`turn_costs`) is tagged
with the account and the reservation closed, in one transaction. An uncertain
outcome (error, lost lease, preemption, crash then repair) keeps the
reservation in full for its day (`kept`); only a human releases it
(`ameesh budget release <id>`). An unknown paid request is never replayed:
the next turn needs a new admission.

**Exhaustion.** The agent is paused (`budget : account … until 00:00`); no
critical exception and no failover to another provider bypass it — the
critical fallback only takes a profile whose own account still has room.
Spend made with the same keys outside ameesh is not seen (coverage boundary,
`site/docs/concepts/budgets.md`).
"""
from __future__ import annotations

import time

from . import db as db_mod
from . import storage

SCOPE_PREFIX = "account:"
DEFAULT_TZ = "Europe/Paris"


def _api(db):
    """The storage domain; a backend without it (mediated device) fails closed."""
    api = getattr(storage.of(db), "budgets", None)
    if api is None or not hasattr(api, "account_reserve"):
        raise db_mod.DbError("account budgets not available on this storage backend")
    return api


def billing_of(profile) -> str:
    return (getattr(profile, "billing_account", "") or "") if profile is not None else ""


def _until(reset_ts) -> str:
    return time.strftime("%H:%M", time.localtime(float(reset_ts))) if reset_ts else "?"


def used(row: dict) -> float:
    return float(row["settled"]) + float(row["reserved"]) + float(row["kept"])


def exhausted_reason(account: str, row: dict) -> str:
    return "account %s %.2f/%.2f $ (calendar day %s) until %s" % (
        account, used(row), float(row["usd"]), row["tz"], _until(row.get("reset_ts")))


def reason(db, profile) -> str:
    """Pause reason when the profile's account cannot admit one more turn ('' = ok).

    A read error pauses too: without the envelope the spend would be blind."""
    account = billing_of(profile)
    if not account:
        return ""
    try:
        rows = _api(db).account_usage(account)
    except db_mod.Unavailable:
        raise
    except db_mod.DbError as exc:
        return "account %s envelope unreadable (%s)" % (account, db_mod.explain(exc))
    if not rows:
        return ""
    row = rows[0]
    if used(row) + float(profile.reserve_usd) > float(row["usd"]) + 1e-9:
        return exhausted_reason(account, row)
    return ""


def reserve(db, agent: str, profile) -> str:
    """Reserves the turn bound on the profile's account; '' = admitted."""
    account = billing_of(profile)
    if not account:
        return ""
    try:
        got = _api(db).account_reserve(account, agent, float(profile.reserve_usd))
    except db_mod.Unavailable:
        raise
    except db_mod.DbError as exc:
        return "account %s reservation failed (%s)" % (account, db_mod.explain(exc))
    if got["ok"]:
        return ""
    return exhausted_reason(account, got["usage"])


def settle(db, agent: str, *, state: str = "settled", actor: str = "runner"):
    """Closes the agent's open reservation; None when it has none."""
    return _api(db).account_settle(agent, state=state, actor=actor)


def usage(db) -> list:
    """The envelopes, for `ameesh budget`; [] when the table is missing."""
    try:
        rows = _api(db).account_usage()
    except db_mod.DbError:
        return []
    for row in rows:
        row["remaining"] = round(max(0.0, float(row["usd"]) - used(row)), 6)
    return rows
