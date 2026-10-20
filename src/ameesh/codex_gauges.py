# SPDX-License-Identifier: AGPL-3.0-only
"""Reconcile Codex session readings without trusting the newest file alone."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from datetime import datetime


class AmbiguousCodexGauges(ValueError):
    """Live readings in one account directory contradict each other."""


def _timestamp(event, fallback):
    try:
        return datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return fallback


def session_reading(path, read_at):
    stat = os.stat(path)
    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return _session_reading(path, read_at, signature)


@lru_cache(maxsize=4096)
def _session_reading(path, read_at, signature):
    # Local import avoids a cost -> codex_gauges -> cost import cycle.
    from .cost import Gauge, _epoch, _find_rate_limits, _fraction, _read_tail

    identity = None
    try:
        with open(path, "rb") as stream:
            first = json.loads(stream.readline(1_000_000))
        if first.get("type") == "session_meta":
            identity = first.get("payload", {}).get("creator_account_id")
    except (OSError, ValueError, AttributeError):
        pass
    windows = {}
    for line in _read_tail(path, 2_000_000).splitlines():
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        limits = _find_rate_limits(event)
        if not limits:
            continue
        stamp = _timestamp(event, read_at)
        for slot in ("primary", "secondary"):
            value = limits.get(slot)
            if not isinstance(value, dict) or "used_percent" not in value:
                continue
            minutes = float(value.get("window_minutes") or 0)
            gauge = Gauge("codex", "codex-%dmin" % int(minutes),
                          _fraction(value["used_percent"], percent=True),
                          _epoch(value.get("resets_at")),
                          minutes * 60 or 7 * 86400.0, read_at=stamp)
            previous = windows.get(gauge.key)
            if previous is None or (stamp or 0) >= (previous[0].read_at or 0):
                windows[gauge.key] = (gauge, limits, identity)
    return list(windows.values())


def reconcile(readings, now):
    """Keep every window, reject conflicting live identities/plans/resets.

    Session metadata names the creator, not necessarily the account used by
    a resumed session. It cannot by itself certify a later rate-limit event.
    Never blend incompatible plans into a seemingly healthy account.
    """
    from .cost import Gauges, _codex_overage

    live = [r for r in readings if not r[0].reset_passed(now)]
    plans = {r[1].get("plan_type") for r in live if r[1].get("plan_type")}
    identities = {r[2] for r in live if r[2]}
    if len(plans) > 1 or len(identities) > 1:
        raise AmbiguousCodexGauges("ambiguous Codex account: conflicting live session identities or plans")
    windows = {}
    for gauge, limits, _identity in readings:
        previous = windows.get(gauge.key)
        if previous is not None:
            old = previous[0]
            if (not old.reset_passed(now) and not gauge.reset_passed(now)
                    and old.resets_at and gauge.resets_at
                    and old.resets_at != gauge.resets_at
                    and abs(old.resets_at - gauge.resets_at) < gauge.window_s):
                raise AmbiguousCodexGauges("ambiguous Codex account: conflicting live window resets")
        if previous is None or (gauge.read_at or 0) > (previous[0].read_at or 0):
            windows[gauge.key] = (gauge, limits)
    if not windows:
        return []
    gauges = [r[0] for r in windows.values()]
    # A partial reading must not clear a retained saturated window's overage.
    overages = [_codex_overage(limits, [gauge]) for gauge, limits in windows.values()]
    from .cost import Overage
    capable = True if any(o.capable is True for o in overages) else (
        False if all(o.capable is False for o in overages) else None)
    return Gauges(gauges, Overage(capable, any(o.in_use for o in overages)))
