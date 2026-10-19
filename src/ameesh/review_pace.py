# SPDX-License-Identifier: AGPL-3.0-only
"""#2668 — the plan pace of a model family, for the designation at freeze.

Canon §7 (process in code): when several model families are eligible for a
review, the designation must prefer the one **furthest behind its plan**, so
that a family is not exhausted before the end of the week. The load of the
agents only breaks a tie.

The reading is the one of `accounts` (canon §7, #1606 point 6):

- the window published by the harness and its reset date (`cost.Gauge`);
- the plan pace, `min(ceiling, elapsed × multiplier + margin)`, with the
  account's `pace_multiplier` and `pace_ceiling` when the host declares them
  (defaults of `accounts`/`cost` otherwise);
- the reserve kept for the critical agents (`accounts.RESERVE`), deducted like
  a non-critical turn does (#2648): a reviewer stops below the pace, never at
  it.

The value passed to the selection is the **advance** in points,
`used_at − held_cap`: positive means the family has consumed more than its
pace allows (ahead), negative means it is behind. Within an account the
binding window is the one with the largest advance; across the accounts of a
family the reading is the **most constrained current account** (the largest
advance): a fresh sibling does not hide the plan that is ahead. An account is
read as a whole, so a partial reading (a dated window reset while another is
open) is stale.

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
    known. A row without a positive window cannot be placed on the pace: it is
    dropped, never defaulted to a guessed window.
    """
    row = dict(row or {})
    harness = str(row.get("harness") or "").strip().lower()
    key = str(row.get("key") or row.get("gauge_key") or "").strip()
    if not harness or not key:
        return None
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
                      source=str(row.get("source") or ""))


def declared_profiles(cfg) -> dict:
    """`{harness: [Profile]}` declared by the host, empty on a broken config.

    The gauge history is shared by the mesh, the account settings are local:
    a host that declares neither Claude nor Codex reads their gauges with the
    defaults, which is the reading of `accounts` without a declared profile.
    """
    try:
        return accounts.parse(getattr(cfg, "accounts", None) or {})
    except accounts.AccountError:
        return {}


def account_advance(gauges: Sequence[cost.Gauge], *, now: float,
                    profile: Optional[accounts.Profile] = None) -> Optional[float]:
    """The advance of one account, in points, or None when the reading is stale.

    It is `used_at − held_cap` on the **binding window** (the largest advance).
    `held_cap` carries the account's `pace_multiplier`/`pace_ceiling` and the
    reserve of the critical agents (`accounts.RESERVE`), exactly like the
    pause of a non-critical turn.
    """
    gauges = list(gauges or ())
    if not gauges or cost.stale_reading(gauges, now):
        return None
    multiplier = profile.pace_multiplier if profile is not None else 1.0
    ceiling = profile.pace_ceiling if profile is not None else cost.PACE_CEILING
    advances = [gauge.used_at(now) - gauge.held_cap(
        now, accounts.RESERVE, multiplier, ceiling) for gauge in gauges]
    return max(advances) if advances else None


def family_advances(rows, *, now: float, profiles: Optional[Mapping] = None) -> dict:
    """`{family: advance}` for the families with a current reading.

    `rows` are the gauge-history rows (`operations.latest_gauges`), `profiles`
    the declared `{harness: [Profile]}` (absent: defaults). A harness that has
    named account readings drops its anonymous ones, like
    `sous_utilisation.latest_account_gauges`: they are the default account,
    already counted. A family's value is the **largest** current account
    advance (the most constrained one); an account with a stale reading is
    ignored, and a family with no current account is absent (neutral).
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
    profiles = profiles or {}
    out: dict = {}
    for (harness, account), gauges in grouped.items():
        items = profiles.get(harness) or []
        profile = None
        if account:
            profile = accounts.by_name(items, account)
        elif len(items) == 1:
            profile = items[0]
        elif items:
            # the anonymous readings belong to the harness's default account
            profile = accounts.primary_of(harness, items)
        advance = account_advance(gauges, now=now, profile=profile)
        if advance is None:
            continue
        current = out.get(harness)
        if current is None or advance > current:
            out[harness] = advance
    return out
