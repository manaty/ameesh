# SPDX-License-Identifier: AGPL-3.0-only
"""#4361 — a bounded pause of the hourly packet tick (`scripts/paquet-horaire.py`).

Before #4361 the only way to keep the tick from composing during an
off-schedule staging was to stop `ameesh-paquet-horaire.timer`; the staging
return check (`ameesh role timers --check`) then failed on « timer is not
active » and staging #1155 was reported as failed with 20 healthy runners.

The pause lives in the shared event log (`ameesh_events`, like the staging
proof of #3981), never in a local file: the tick runs on whichever candidate
holds the `mesh-design` lease, and a lease move must not lift the pause. One
event type, `packet.pause`, object ``paquet-horaire:<project>:<target>``; the
newest event of that object is the state:

* ``{"state": "paused", "until": <epoch>, "reason": …, "by": …}``
* ``{"state": "resumed", "by": …}``

A pause always has a deadline (at most `MAX_S` ahead): at `until`, the tick
resumes by itself, with no gesture. The timer stays active, so
`role timers --check` stays green and names the pause as a warning.
"""
from __future__ import annotations

import time

from . import storage

EVENT = "packet.pause"
#: a pause is a staging window, not a freeze: 12 h at most, renewable
MAX_S = 12 * 3600
#: the log reads recent events only (`recent_type`, retention index)
LOOKBACK_DAYS = 2
#: `data` stays short (events.py: identifiers and states)
REASON_MAX = 200


class PauseError(ValueError):
    """A pause refused (no deadline, deadline past or too far, no reason)."""


def key(project: str, target: str) -> str:
    return "paquet-horaire:%s:%s" % (project, target)


def _events(db):
    return storage.of(db).events


def pause(db, *, project: str, target: str, until: float, reason: str, by: str,
          now: float | None = None) -> dict:
    """Records a pause until `until` (epoch); the recorded data."""
    now = time.time() if now is None else float(now)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise PauseError("a pause needs a reason")
    if len(reason) > REASON_MAX:
        raise PauseError("reason longer than %d characters" % REASON_MAX)
    if until is None:
        raise PauseError("a pause needs a deadline (--until)")
    until = float(until)
    if until <= now:
        raise PauseError("deadline already past")
    if until - now > MAX_S:
        raise PauseError("deadline more than %d h ahead" % (MAX_S // 3600))
    data = {"state": "paused", "until": int(until), "reason": reason, "by": by,
            "target": target}
    data["event"] = _events(db).record(EVENT, key(project, target), project, data)
    return data


def resume(db, *, project: str, target: str, by: str) -> dict:
    """Records the end of the pause (a no-op state if none was active)."""
    data = {"state": "resumed", "by": by, "target": target}
    data["event"] = _events(db).record(EVENT, key(project, target), project, data)
    return data


def latest(db, *, project: str, target: str) -> dict | None:
    rows = _events(db).recent_type(EVENT, LOOKBACK_DAYS, 1, object=key(project, target))
    for row in rows or ():
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict):
            return data
    return None


def active_of(data: dict | None, now: float | None = None) -> dict | None:
    """The pause `data` describes if it is still running at `now`, else None."""
    now = time.time() if now is None else float(now)
    if not isinstance(data, dict) or data.get("state") != "paused":
        return None
    try:
        until = float(data.get("until"))
    except (TypeError, ValueError):
        return None
    return data if until > now else None


def active(db, *, project: str, target: str, now: float | None = None) -> dict | None:
    return active_of(latest(db, project=project, target=target), now)


def describe(data: dict, zone=None) -> str:
    """« en pause jusqu'à HH:MM (raison) », in `zone` (canon) or UTC."""
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(float(data["until"]), zone or timezone.utc)
    return "en pause jusqu'à %s (%s, par %s)" % (
        when.strftime("%Y-%m-%d %H:%M %Z"), data.get("reason") or "?", data.get("by") or "?")
