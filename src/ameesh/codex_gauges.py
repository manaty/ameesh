# SPDX-License-Identifier: AGPL-3.0-only
"""Reconcile Codex session readings without trusting the newest file alone."""
from __future__ import annotations

import json
import os
import time
import threading
import heapq
from collections import OrderedDict
from datetime import datetime


class AmbiguousCodexGauges(ValueError):
    """Live readings in one account directory contradict each other."""


def _timestamp(event, fallback):
    try:
        return datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return fallback


_READINGS = OrderedDict()
_CACHE_LOCK = threading.Lock()
MAX_FILES = 32
TAIL_BYTES = 256 * 1024
META_BYTES = 64 * 1024


class SessionReadings(list):
    """A bounded parse, including whether its prefix was omitted."""

    def __init__(self, items, *, truncated):
        super().__init__(items)
        self.truncated = truncated


def recent_files(root):
    """Stat once per journal, retain only the newest bounded sample."""
    def candidates():
        for directory, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(directory, name)
                try:
                    yield os.stat(path).st_mtime, path
                except OSError:
                    continue

    selected = heapq.nlargest(MAX_FILES + 1, candidates())
    return selected[:MAX_FILES], len(selected) > MAX_FILES


def session_reading(path, read_at):
    from .cost import _journal_signature, _racy

    stat = os.stat(path)
    seen = time.time_ns()
    signature = _journal_signature(stat)
    with _CACHE_LOCK:
        cached = _READINGS.get(path)
        if cached is not None and cached[0] == signature and not _racy(stat, seen):
            _READINGS.move_to_end(path)
            return cached[1]
        # Evict even on an unchanged signature: after a clock step back, a
        # formerly settled cache entry can become racy again.
        _READINGS.pop(path, None)
    readings = _session_reading(path, read_at)
    after = os.stat(path)
    # Age is measured BEFORE parsing; a slow read cannot settle its snapshot.
    if signature == _journal_signature(after) and not _racy(stat, seen):
        with _CACHE_LOCK:
            _READINGS[path] = (signature, readings)
            if len(_READINGS) > 4096:
                _READINGS.popitem(last=False)
    return readings


def _session_reading(path, read_at):
    # Local import avoids a cost -> codex_gauges -> cost import cycle.
    from .cost import Gauge, _epoch, _find_rate_limits, _fraction, _read_tail

    identity = None
    try:
        with open(path, "rb") as stream:
            first = json.loads(stream.readline(META_BYTES))
        if first.get("type") == "session_meta":
            identity = first.get("payload", {}).get("creator_account_id")
    except (OSError, ValueError, AttributeError):
        pass
    windows = {}
    for line in _read_tail(path, TAIL_BYTES).splitlines():
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
    return SessionReadings(windows.values(), truncated=os.path.getsize(path) > TAIL_BYTES)


def reconcile(readings, now, *, truncated=False):
    """Keep the newest observation per window; reject mixed identities/plans.

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
        if previous is None or (gauge.read_at or 0) > (previous[0].read_at or 0):
            windows[gauge.key] = (gauge, limits)
    # A long sequence of partial team readings must not evict a saturated
    # short window beyond our I/O budget. Pro legitimately has a week only.
    if truncated:
        missing = []
        if plans != {"pro"} and not any(
                g.window_s <= 5 * 3600 for g, _limits in windows.values()):
            missing.append("short window")
        if not any(g.window_s > 5 * 3600 for g, _limits in windows.values()):
            missing.append("weekly window")
        if missing:
            raise AmbiguousCodexGauges(
                "incomplete Codex account reading: %s absent from bounded session sample"
                % ", ".join(missing))
    if not windows:
        return []
    gauges = [r[0] for r in windows.values()]
    # A partial reading must not clear a retained saturated window's overage.
    overages = [_codex_overage(limits, [gauge]) for gauge, limits in windows.values()]
    from .cost import Overage
    capable = True if any(o.capable is True for o in overages) else (
        False if all(o.capable is False for o in overages) else None)
    return Gauges(gauges, Overage(capable, any(o.in_use for o in overages)))
