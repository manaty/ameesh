# SPDX-License-Identifier: AGPL-3.0-only
"""#7066: a signed, bounded exception for one named account turn.

The signed answer itself is the token document. Database columns only index it;
they never grant authority. Preview is read-only; consumption and begin_turn
share a transaction. Shared account limits and pauses are never changed.
"""
from __future__ import annotations

import json
import math
import time

from . import accounts, auth_failure, db as db_mod, decisions, provider_credits, storage

KIND = "ameesh-account-turn-exception/1"
PREFIX = "I authorize one named account turn within these bounds:\n"


class ExceptionError(ValueError):
    pass


def document(db, decision_id: int, *, now: float) -> dict:
    view = decisions.get(db, decision_id, now=now)
    if not view or view["state"] != "answered":
        raise ExceptionError("exception requires an answered owner decision")
    decisions.require_current(db, view)
    valid, reason = decisions.verify_proof(db, view, live=True)
    if not valid:
        raise ExceptionError("exception signature rejected: " + reason)
    text = (view.get("answer") or {}).get("text") or ""
    if not text.startswith(PREFIX):
        raise ExceptionError("signed answer must start with: " + PREFIX.strip())
    try:
        token = json.loads(text[len(PREFIX):])
    except (ValueError, TypeError) as exc:
        raise ExceptionError("signed answer must be an exception JSON document") from exc
    fields = {"kind", "agent", "harness", "account", "turns", "extra_points", "expires_at"}
    if not isinstance(token, dict) or set(token) != fields or token["kind"] != KIND:
        raise ExceptionError("invalid exception document fields or kind")
    if type(token["turns"]) is not int or token["turns"] != 1:
        raise ExceptionError("an exception authorizes exactly one turn")
    for key in ("agent", "harness", "account"):
        if not isinstance(token[key], str) or not token[key].strip() or token[key] != token[key].strip():
            raise ExceptionError("invalid exception " + key)
    for key in ("extra_points", "expires_at"):
        try:
            finite = type(token[key]) in (int, float) and math.isfinite(token[key])
        except OverflowError:
            finite = False
        if not finite:
            raise ExceptionError("invalid exception " + key)
    if not 0 < token["extra_points"] <= 100:
        raise ExceptionError("extra_points must be greater than zero and at most 100")
    if token["expires_at"] <= now:
        raise ExceptionError("exception expired")
    return token


def register(db, decision_id: int, *, now: float | None = None) -> dict:
    if storage.is_remote(db):
        raise ExceptionError("account exceptions require transactional local storage")
    now = time.time() if now is None else now
    token = document(db, decision_id, now=now)
    storage.of(db).accounts.register_exception(decision_id, token["agent"],
                                              token["harness"], token["account"])
    return token


def eligible(db, cfg, agent, profile, items, book, token, *, now, critical=False,
             margin=False, model="", environ=None) -> bool:
    """Only quota caps and pacing can yield; every other account guard stays."""
    if (token["agent"], token["harness"], token["account"]) != (
            agent, profile.harness, profile.name):
        return False
    if profile.kind != "config_dir" or profile.scope != "personal":
        return False  # percentages cannot bound a metered monetary allowance
    from . import account_claims
    profile = account_claims.mark_refused(cfg, [profile], db=db, now=now)[0]
    if profile.name in auth_failure.failing(db, cfg.host, profile.harness):
        return False
    if profile.name in provider_credits.exhausted_names(db, profile.harness):
        return False
    store = storage.of(db).accounts
    held, conflicts = accounts._etat_placement(store, cfg.host, profile.harness, items, now)
    if profile.name in held | conflicts:
        return False
    state = accounts.evaluate(book, profile, items, now, environ, record=False,
                              critical=critical, reserve=margin or not critical,
                              model=model)
    if not within_quota(profile, state, token, now=now, critical=critical, margin=margin):
        return False
    observed = min(g.read_at for g in state.gauges)
    pause = store.account_pauses(profile.harness).get(profile.name)
    if pause and accounts._pause_valid(pause, now) and float(pause["observed_at"]) > observed:
        return False  # a newer shared observation cannot be ignored
    return True


def within_quota(profile, state, token, *, now, critical=False, margin=False):
    if (state.problems or not state.gauges or getattr(state.gauges, "errors", None)
            or accounts.in_overage(state.gauges, now)):
        return False
    stamps = [getattr(g, "read_at", None) for g in state.gauges]
    if any(stamp is None or not 0 <= now - stamp <= 3600 for stamp in stamps):
        return False
    if accounts.reading_outdated(state.gauges, now):
        return False
    limits = accounts.limits(profile, state.gauges, now, critical=critical,
                             reserve=margin or not critical)
    extra = token["extra_points"] / 100
    for gauge in state.gauges:
        used = gauge.used_at(now)
        cap = min(1.0, limits[gauge.key][0] + extra)
        if not math.isfinite(used) or used >= cap or math.isclose(used, cap, rel_tol=0, abs_tol=1e-12):
            return False
        # A separately configured short-window cap is not a pacing exception.
        if (profile.short_window_cap is not None and gauge.window_s <= accounts.SHORT_WINDOW_S
                and used >= profile.short_window_cap):
            return False
    return True


def ceiling_reason(worker, profile, state, *, now):
    """In-flight quota checks keep the exception bounded until the turn ends."""
    from . import account_turn
    identifier, admitted_profile = worker._account_exception_live
    try:
        token = document(worker.db, identifier, now=now)
    except (ExceptionError, decisions.DecisionError, db_mod.DbError):
        return "signed account exception is no longer valid"
    if profile != admitted_profile or (token["agent"], token["harness"], token["account"]) != (
            worker.name, profile.harness, profile.name):
        return "signed account exception profile changed"
    if not within_quota(profile, state, token, now=now,
                        critical=account_turn.is_critical(worker),
                        margin=getattr(worker, "_account_exception_margin", False)):
        return "signed account exception bound reached or quota observation unavailable"
    try:
        pause = storage.of(worker.db).accounts.account_pauses(profile.harness).get(profile.name)
    except db_mod.DbError:
        return "signed account exception quota observation unavailable"
    if (pause and accounts._pause_valid(pause, now)
            and float(pause["observed_at"]) > min(g.read_at for g in state.gauges)):
        return "signed account exception quota observation is older than the shared pause"
    return None


def preview(worker, items, book, *, now=None, margin=False):
    """Return (profile, decision id), without spending or changing a shared choice."""
    if storage.is_remote(worker.db):
        return None
    now = time.time() if now is None else now
    from . import account_turn
    harness = (worker.agent or {}).get("harness") or ""
    for row in storage.of(worker.db).accounts.pending_exceptions(worker.name, harness):
        try:
            token = document(worker.db, row["decision_id"], now=now)
        except (ExceptionError, decisions.DecisionError):
            continue
        profile = accounts.by_name(items, token["account"])
        if profile is not None and eligible(
                worker.db, worker.cfg, worker.name, profile, items, book, token,
                now=now, critical=account_turn.is_critical(worker), margin=margin,
                model=(worker.agent or {}).get("model") or book.model_of(worker.name)):
            return profile, int(row["decision_id"])
    return None


def begin(worker, profile, callback, *, db=None) -> bool:
    """Revalidate and consume under the token row lock, together with turn start."""
    decision_id = getattr(worker, "_account_exception", None)
    db = worker.db if db is None else db
    if decision_id is None:
        return callback(db)
    if storage.is_remote(worker.db) or profile is None:
        return False
    from . import account_turn, cost
    with db.transaction() as tx:
        store = storage.of(tx).accounts
        if not store.lock_exception(decision_id):
            return False
        now = time.time()
        try:
            token = document(tx, decision_id, now=now)
        except (ExceptionError, decisions.DecisionError):
            return False
        items = accounts.profiles(worker.cfg, profile.harness)
        current = accounts.by_name(items, profile.name)
        if current is None or current != profile:
            return False
        book = cost.CostBook(state_dir=worker.cfg.state_dir, db=tx,
                             tools={worker.name: profile.harness})
        if not eligible(tx, worker.cfg, worker.name, current, items, book, token,
                        now=now, critical=account_turn.is_critical(worker),
                        margin=getattr(worker, "_account_exception_margin", False),
                        model=(worker.agent or {}).get("model") or book.model_of(worker.name)):
            return False
        if not callback(tx):
            return False
        store.consume_exception(decision_id, worker.cfg.host)
    worker._account_exception_live = (decision_id, profile)
    return True
