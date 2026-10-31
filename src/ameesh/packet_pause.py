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
resumes by itself, unless `await_announcement` is true (#5045): then the
deadline alerts the pilot and only a sourced `resume` lifts the pause. The
latest state (including a resume) is retained even beyond log retention.
The timer stays active, so
`role timers --check` stays green and names the pause as a warning.
"""
from __future__ import annotations

import math
import time

from . import storage

EVENT = "packet.pause"
#: a pause is a staging window, not a freeze: 12 h at most, renewable
MAX_S = 12 * 3600
#: recent history is bounded; its preceding state has no age cutoff (#5045)
LOOKBACK_DAYS = 2
#: `data` stays short (events.py: identifiers and states)
REASON_MAX = 200
#: #5057: an expired announcement pause reminds the pilot at this cadence
REMIND_S = 3600


class PauseError(ValueError):
    """A pause refused (no deadline, deadline past or too far, no reason)."""


def key(project: str, target: str) -> str:
    return "paquet-horaire:%s:%s" % (project, target)


def _events(db):
    return storage.of(db).events


def pause(db, *, project: str, target: str, until: float, reason: str, by: str,
          now: float | None = None, await_announcement: bool = False) -> dict:
    """Records a pause with deadline `until` (epoch); optionally await announcement."""
    now = time.time() if now is None else float(now)
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise PauseError("a pause needs a reason")
    if len(reason) > REASON_MAX:
        raise PauseError("reason longer than %d characters" % REASON_MAX)
    if until is None:
        raise PauseError("a pause needs a deadline (--until)")
    until = float(until)
    if not math.isfinite(until) or until <= now:
        raise PauseError("deadline already past")
    if until - now > MAX_S:
        raise PauseError("deadline more than %d h ahead" % (MAX_S // 3600))
    before = latest(db, project=project, target=target)
    if (before and before.get("state") == "paused" and before.get("await_announcement")
            and not await_announcement):
        raise PauseError("announcement still required: resume --source before an ordinary pause")
    data = {"state": "paused", "until": int(until), "reason": reason, "by": by,
            "target": target}
    if await_announcement:
        data["await_announcement"] = True
    data["event"] = _events(db).record(EVENT, key(project, target), project, data)
    return data


def resume(db, *, project: str, target: str, by: str, source: str = "") -> dict:
    """Records the end of the pause (a no-op state if none was active)."""
    before = latest(db, project=project, target=target)
    source = " ".join(str(source or "").split())
    if len(source) > REASON_MAX:
        raise PauseError("announcement source longer than 200 characters")
    if before and before.get("state") == "paused" and before.get("await_announcement"):
        if not source:
            raise PauseError("announcement source required (author, date, message; --source)")
    data = {"state": "resumed", "by": by, "target": target}
    if source:
        data["source"] = source
    data["event"] = _events(db).record(EVENT, key(project, target), project, data)
    return data


def latest(db, *, project: str, target: str) -> dict | None:
    rows = _events(db).recent_type(EVENT, None, 1, object=key(project, target))
    for row in rows or ():
        data = row.get("data") if isinstance(row, dict) else None
        if isinstance(data, dict):
            return dict(data, event=row["id"])
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
    return data if until > now or data.get("await_announcement") else None


def active(db, *, project: str, target: str, now: float | None = None) -> dict | None:
    return active_of(latest(db, project=project, target=target), now)


def alert_due(db, data: dict | None, *, project: str, target: str,
              recipients, now: float | None = None) -> None:
    """#5045: deadline of an announcement pause alerts, never resumes.

    #5057: the alert is repeated every `REMIND_S` past the deadline while the
    announcement is missing, so a missed mail does not freeze merges in
    silence (8 h 37 in 0518). One mail per pause, period and recipient,
    including across a composer lease move; a failed delivery is retried at
    the next tick of the same period with the same key.
    """
    from . import mail, work
    now = time.time() if now is None else float(now)
    if not active_of(data, now) or not data.get("await_announcement"):
        return
    until = float(data["until"])
    if until > now:
        return
    period = int((now - until) // REMIND_S)
    body = ("Paquet horaire %s/%s : échéance de pause atteinte. "
            "Annonce explicite toujours attendue ; aucun geste de composition. "
            "Motif : %s. Publier la fin de fenêtre puis lever avec "
            "paquet-horaire.py resume --source (auteur, date, message)."
            % (project, target, data.get("reason") or "?"))
    suffix = ""
    if period:
        late = int(now - until) // 60
        body = ("Relance %d : composition gelée depuis %d h %02d après l’échéance. %s"
                % (period, late // 60, late % 60, body))
        suffix = "-r%d" % period
    for recipient in dict.fromkeys(recipients):
        mail.send(db, work.SYSTEM_SENDER, recipient, body, kind="request",
                  idempotency_key="packet-pause-%s-%s%s" % (data["event"], recipient, suffix))


def describe(data: dict, zone=None) -> str:
    """« en pause jusqu'à HH:MM (raison) », in `zone` (canon) or UTC."""
    from datetime import datetime, timezone
    when = datetime.fromtimestamp(float(data["until"]), zone or timezone.utc)
    if data.get("await_announcement"):
        return "en pause jusqu’à annonce explicite (alerte à %s ; %s, par %s)" % (
            when.strftime("%Y-%m-%d %H:%M %Z"), data.get("reason") or "?", data.get("by") or "?")
    return "en pause jusqu'à %s (%s, par %s)" % (
        when.strftime("%Y-%m-%d %H:%M %Z"), data.get("reason") or "?", data.get("by") or "?")


#: #4444: a read of the recent pauses keeps a bounded number of events
WINDOWS_LIMIT = 200


def windows(db, *, project: str, now: float | None = None) -> list[dict]:
    """#4444: the recent pause windows of `project`, every target, oldest first.

    Each window is ``{"start", "end", "until", "reason", "by", "target"}``:
    it opens at the `paused` event and ends at its deadline (ordinary pause)
    or the next event of the same object (`resumed`, or a renewed pause).
    Announcement pauses remain open beyond their alert deadline. The state
    preceding the lookback is included, so an old open pause is not forgotten,
    with the pauses it renewed (#5057), so a long chain keeps its true start.
    The SLO reads them to stop the GO → merge clock while no composition
    runs (`suspended_start`).
    An event without a date opens no window.
    """
    now = time.time() if now is None else float(now)
    prefix = key(project, "")
    rows = [row for row in _events(db).context_type(
                EVENT, LOOKBACK_DAYS, WINDOWS_LIMIT, object_prefix=prefix) or ()
            if isinstance(row, dict) and str(row.get("object") or "").startswith(prefix)]
    rows.sort(key=lambda row: (float(row.get("ts") or 0.0), int(row.get("id") or 0)))
    out: list[dict] = []
    open_: dict = {}
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        obj = row["object"]
        try:
            ts = float(row["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        current = open_.pop(obj, None)
        if current is not None:
            current["end"] = min(ts, current["end"])
            current["open"] = False
        if data.get("state") != "paused":
            continue
        try:
            until = float(data.get("until"))
        except (TypeError, ValueError):
            continue
        if until <= ts:
            continue
        window = {"start": ts, "end": max(until, now) if data.get("await_announcement") else until,
                  "until": int(until), "open": bool(data.get("await_announcement")),
                  "reason": data.get("reason") or "", "by": data.get("by") or "",
                  "target": data.get("target") or obj[len(prefix):]}
        open_[obj] = window
        out.append(window)
    return out


def suspended_start(start: float, pauses, next_slot, now: float
                    ) -> tuple[float, dict | None]:
    """#4444: the equivalent start of a merge wait running from `start`.

    A packet pause stops the clock, whenever it began (before the slot, or
    while the lot was already waiting for one): no composition runs during
    it, and the first one after it is `next_slot(end)` (`end` itself when
    `next_slot` gives nothing). Time accrued before the pause stays accrued.
    While a pause runs at `now`, the wait is planned: the equivalent start is
    `now` (nothing elapsed). Returns that start, such that `now - start` is
    the time counted, and the last pause crossed (None when none was).
    """
    now = float(now)
    cursor = float(start)
    accrued = 0.0
    crossed = None
    for window in sorted(pauses, key=lambda w: w["start"]):
        if window["start"] >= now:
            continue
        # #5057: a pause running at `now` wins even when an earlier one pushed
        # the cursor past `now` (its next slot is still ahead).
        if window["end"] > now or (window.get("open") and window["end"] >= now):
            return now, window
        if window["end"] <= cursor or cursor >= now:
            continue
        crossed = window
        accrued += max(0.0, window["start"] - cursor)
        after = next_slot(window["end"])
        cursor = max(cursor, float(after) if after is not None else float(window["end"]))
    if crossed is None:
        return float(start), None
    accrued += max(0.0, now - cursor)
    return now - accrued, crossed
