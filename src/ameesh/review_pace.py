# SPDX-License-Identifier: AGPL-3.0-only
"""#2668 — the plan pace of a model family, for the designation at freeze.

Canon §7 (process in code): when several model families are eligible for a
review, the designation must prefer the one **furthest behind its plan**, so
that a family is not exhausted before the end of the week. The load of the
agents only breaks a tie.

The reading is the **raw** pace (canon §7, #1606 point 6):

- the window published by the harness and its reset date (`cost.Gauge`);
- the nominal pace, `min(cost.PACE_CEILING, elapsed + cost.PACE_MARGIN)`: the
  **same scale for every family**. The account's declared
  `pace_multiplier`/`pace_ceiling` are deliberately not read — a declared ×10
  made Claude look further behind than Codex and inverted the intended
  witness (B of mesh-design on 4123b99, 19/10/2026);
- the reserve kept for the critical agents (`accounts.RESERVE`), deducted like
  a non-critical turn does (#2648): a reviewer stops below the pace, never at
  it.

The value passed to the selection is the **advance** in points,
`used_at − held_cap`: positive means the family has consumed more than its
pace allows (ahead), negative means it is behind. The plan is read on the
**plan horizon**, the longest applicable window (the weekly `seven_day` in
practice): a short `five_hour` burst is a capacity signal, not the weekly
plan, and must not outweigh the weekly gap (#2675). Within that horizon the
binding window is the one with the largest advance; across the accounts of a
family the reading is the **most constrained current account** (the largest
advance): a fresh sibling does not hide the plan that is ahead. An account is
read as a whole, so a partial reading (a dated window reset while another is
open) is stale.

A **model-scoped** bucket (`seven_day@fable`) keeps its scope: it is part of
the applicable set only for a reviewer whose model contains that scope
(`fable` in `fable` or `claude-fable`, `accounts.model_scoped`); another
reviewer, or an unknown model, sees the general windows only. The selection
matches the scopes to the candidate's model, so a bucket that applies to one
model never contaminates the family (cx3 B/P1).

Freshness is #1561: a **stale** reading is *neutral* — the family is absent
from the mapping, and the selection falls back on its other criteria (load);
an absent reading is neutral too, never a measured 0 % (L117 only says an
unused account is tried first). The family list and the required number are
not read here: they come from the compiled canon (`mechanics`), the caller
passes them.

This module is pure: the history rows are read by the adapter
(`review_designation_activation`), the selection consumes the advances.
"""
from __future__ import annotations

import math
from typing import Mapping, Optional, Sequence

from . import accounts
from . import cost


def _number(value, default=None):
    """A finite float, or `default`; never guesses on a malformed source."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def gauge_of(row: Mapping) -> Optional[cost.Gauge]:
    """The `Gauge` of one gauge-history row, or None if the row is unreadable.

    The rows come from `operations.latest_gauges`: `key`, `used`,
    `resets_at_ts`, `window_s`, `observed_ts`, `produced_ts` and `source`.
    `read_at` is the instant the harness produced the reading; when the source
    does not date it (#1132, Codex), the insertion instant is the only age
    known. A model-scoped bucket (`seven_day@fable`) keeps its key and carries
    its scope, like `sous_utilisation._gauge`: it is not a family-wide reading.
    A row without a positive window cannot be placed on the pace: it is
    dropped, never defaulted to a guessed window.
    """
    row = dict(row or {})
    harness = str(row.get("harness") or "").strip().lower()
    key = str(row.get("key") or row.get("gauge_key") or "").strip()
    if not harness or not key:
        return None
    _, _, scope = key.partition("@")
    window = _number(row.get("window_s"))
    if window is None or window <= 0:
        return None
    used = _number(row.get("used"), 0.0)
    resets = _number(row.get("resets_at") if row.get("resets_at") is not None
                     else row.get("resets_at_ts"))
    produced = _number(row.get("produced_at") if row.get("produced_at") is not None
                       else row.get("produced_ts"))
    observed = _number(row.get("read_at") if row.get("read_at") is not None
                       else row.get("observed_ts"))
    return cost.Gauge(harness, key, max(0.0, used), resets, window,
                      read_at=produced if produced is not None else observed,
                      produced_at=produced,
                      source=str(row.get("source") or ""),
                      scope=scope.strip().lower())


def _applicable(gauges: Sequence[cost.Gauge], model: str = "") -> list:
    """The gauges that count for a session of `model`.

    General windows always count; a model-scoped bucket (`seven_day@fable`)
    counts only for a model whose name contains its scope, the same rule as
    `accounts.model_scoped` (#1580). An empty/unknown model gets the general
    windows only: a bucket that applies to one model never contaminates
    another reviewer's reading.
    """
    model = str(model or "").strip().lower()
    return [gauge for gauge in gauges
            if not gauge.scope or (model and gauge.scope in model)]


def account_advance(gauges: Sequence[cost.Gauge], *, now: float,
                    model: str = "") -> Optional[float]:
    """The raw advance of one account for `model`, or None when it is stale.

    It is `used_at − held_cap` on the **plan horizon** — the longest
    applicable window, so a `five_hour` burst never becomes the weekly plan
    (#2675) — and within that horizon on the **binding window** (the largest
    advance). Staleness still reads all the applicable windows: a reset short
    window keeps the reading neutral (#1561). The nominal multiplier and the
    default ceiling apply; the reserve of the critical agents
    (`accounts.RESERVE`) is the only deduction. The same scale applies to
    every family, whatever its declared profile says.
    """
    gauges = _applicable(gauges, model)
    if not gauges or cost.stale_reading(gauges, now):
        return None
    horizon = max(gauge.window_s for gauge in gauges)
    plan = [gauge for gauge in gauges if gauge.window_s == horizon]
    advances = [gauge.used_at(now) - gauge.held_cap(now, accounts.RESERVE)
                for gauge in plan]
    return max(advances) if advances else None


def family_advances(rows, *, now: float) -> dict:
    """`{family: {scope: advance}}` for the families with a current raw reading.

    `rows` are the gauge-history rows (`operations.latest_gauges`). A harness
    that has named account readings drops its anonymous ones, like
    `sous_utilisation.latest_account_gauges`: they are the default account,
    already counted. Each account's advance is read on its plan horizon (the
    longest window, #2675), so a short burst does not lift a family. For each
    scope a family carries, its value is the
    **largest** current advance over its accounts (the most constrained one):
    `""` for the general windows, `<scope>` for a model-scoped bucket added to
    the general ones. An account with a stale reading is ignored; a family
    with no current account is absent (neutral), never guessed.

    The selection matches a candidate's model against the scopes
    (`scope in model`, `accounts.model_scoped`): a `seven_day@fable` bucket
    raises the advance of a Fable reviewer, never that of an Opus one or of an
    unknown model, which sees the general windows only.

    A scope that the family carries but for which **no account has a current
    applicable reading** is recorded as `None`, not omitted: the selection
    then reads that model as neutral (0) instead of falling back to the
    account's general reading, which the stale scoped window invalidated
    (cx3 B/P1, second case). Validity stays per model and per account: a
    general-only sibling remains a valid reading for every model.
    """
    rows = list(rows or ())
    named = {str((row or {}).get("harness") or "").strip().lower()
             for row in rows if (row or {}).get("account")}
    grouped: dict = {}
    for row in rows:
        row = dict(row or {})
        harness = str(row.get("harness") or "").strip().lower()
        account = str(row.get("account") or "").strip()
        if not harness or (not account and harness in named):
            continue
        gauge = gauge_of(row)
        if gauge is None:
            continue
        grouped.setdefault((harness, account), []).append(gauge)
    accounts_of: dict = {}
    for (harness, _account), gauges in grouped.items():
        accounts_of.setdefault(harness, []).append(gauges)
    out: dict = {}
    for harness, accounts in accounts_of.items():
        entry: dict = {}
        general = [value for value in
                   (account_advance(gauges, now=now) for gauges in accounts)
                   if value is not None]
        if general:
            entry[""] = max(general)
        scopes = {gauge.scope for gauges in accounts for gauge in gauges if gauge.scope}
        for scope in sorted(scopes):
            values = [value for value in
                      (account_advance(gauges, now=now, model=scope)
                       for gauges in accounts)
                      if value is not None]
            entry[scope] = max(values) if values else None
        if entry:
            out[harness] = entry
    return out
