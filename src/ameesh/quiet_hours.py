# SPDX-License-Identifier: AGPL-3.0-only
"""Quiet hours per human (#8345, manaty/ameesh#6585).

A human declares a local time window (``22:00-07:00``) next to an IANA time
zone: on a Member card (``timezone`` + ``quiet_hours``) or in the host's
``notify.quiet_hours``.  During that window, `ameesh notify` holds the
non-urgent pushes and sends one summary when the window ends; an urgent type
(``URGENT_TYPES``), an urgent application alert or a ``severity: urgent``
passes through.  A window without a readable time zone is never applied: the
human is never deemed quiet by default (fail-open, documented in
``docs/EXPLOITATION.md``).

The window is wall-clock local time, ``start`` inclusive and ``end``
exclusive.  A wrapping window (``22:00-07:00``) is read as "from 22:00 to
midnight, then midnight to 07:00".  The module only computes the state; it
never sends, so both the notifier and the diagnostics share the same rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

#: ``HH:MM-HH:MM``, 24-hour clock.  The spaces around the dash are accepted so
#: a hand-written card can breathe; they are not part of the stored text.
_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)$")

#: The state values used by `State.problem` (absent when the window applies).
NO_WINDOW = "none"
NO_TIMEZONE = "no-timezone"
INVALID_TIMEZONE = "invalid-timezone"


class QuietHoursError(ValueError):
    """A quiet-hours declaration is unreadable (window or time zone)."""


def _zone(name: str):
    """The `ZoneInfo` of an IANA name, or None when it is unusable."""
    if not name or not isinstance(name, str):
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name.strip())
    except Exception:  # noqa: BLE001 — a missing zone is a fail-open reading
        return None


def known_timezone(name) -> bool:
    """True when `name` is an IANA time zone this host can read."""
    return _zone(name) is not None


@dataclass(frozen=True)
class Window:
    """A local quiet window, in the human's declared time zone."""

    start: str
    end: str
    #: An explicit zone overrides the Member's `timezone` (host config only).
    timezone: str | None = None

    @property
    def text(self) -> str:
        return "%s-%s" % (self.start, self.end)

    def describe(self) -> str:
        if self.timezone:
            return "%s (%s)" % (self.text, self.timezone)
        return self.text

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = value.split(":")
        return int(hour) * 60 + int(minute)

    def contains(self, local: datetime) -> bool:
        """Is the aware local instant inside the window?  End is exclusive."""
        start = self._minutes(self.start)
        end = self._minutes(self.end)
        current = local.hour * 60 + local.minute
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def next_boundary(self, local: datetime, which: str) -> datetime:
        """The next local ``start`` or ``end`` after `local` (aware)."""
        hour, minute = (int(part) for part in getattr(self, which).split(":"))
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local:
            candidate += timedelta(days=1)
        return candidate


def parse(text, *, where: str = "quiet hours") -> Window:
    """Read ``HH:MM-HH:MM``; raise `QuietHoursError` with the reason."""
    if not isinstance(text, str):
        raise QuietHoursError("%s: a window `HH:MM-HH:MM` expected" % where)
    match = _WINDOW_RE.match(text.strip())
    if match is None:
        raise QuietHoursError("%s: a window `HH:MM-HH:MM` expected (24-hour clock), "
                              "not %r" % (where, text))
    start = "%s:%s" % (match.group(1), match.group(2))
    end = "%s:%s" % (match.group(3), match.group(4))
    if start == end:
        raise QuietHoursError("%s: start and end must differ (an empty quiet "
                              "window would never apply)" % where)
    return Window(start=start, end=end)


def parse_value(raw, *, where: str = "quiet hours", allow_timezone: bool = True) -> Window:
    """Read a card/config value: a string, or ``{start, end, timezone?}``."""
    if isinstance(raw, str):
        return parse(raw, where=where)
    if not isinstance(raw, dict):
        raise QuietHoursError("%s: a window `HH:MM-HH:MM` or an object "
                              "{start, end, timezone?} expected" % where)
    unknown = sorted(set(raw) - ({"start", "end", "timezone"} if allow_timezone
                                 else {"start", "end"}))
    if unknown:
        raise QuietHoursError("%s: unknown key(s) %s" % (where, ", ".join(unknown)))
    start, end = raw.get("start"), raw.get("end")
    if start is None or end is None:
        raise QuietHoursError("%s: `start` and `end` are required" % where)
    window = parse("%s-%s" % (start, end), where=where)
    timezone = raw.get("timezone")
    if timezone is None:
        return window
    if not allow_timezone:
        raise QuietHoursError("%s: declare the IANA time zone in the Member's "
                              "`timezone` field, not here" % where)
    if not isinstance(timezone, str) or not timezone.strip():
        raise QuietHoursError("%s.timezone: IANA time zone expected" % where)
    timezone = timezone.strip()
    if not known_timezone(timezone):
        raise QuietHoursError("%s.timezone: IANA time zone expected, not %r"
                              % (where, timezone))
    return Window(start=window.start, end=window.end, timezone=timezone)


@dataclass(frozen=True)
class State:
    """The quiet-hours state of one human at one instant."""

    active: bool = False
    window: Window | None = None
    timezone: str | None = None
    #: ``notify`` (host configuration) or ``canon`` (Member card).
    source: str | None = None
    #: None when the window applies; NO_WINDOW / NO_TIMEZONE / INVALID_TIMEZONE.
    problem: str | None = NO_WINDOW
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    def _stamp(self, value: datetime | None) -> str | None:
        return value.strftime("%Y-%m-%d %H:%M %Z") if value is not None else None

    def to_dict(self) -> dict:
        return {"active": self.active,
                "window": self.window.text if self.window is not None else None,
                "timezone": self.timezone,
                "source": self.source,
                "problem": self.problem,
                "starts_at": self._stamp(self.starts_at),
                "ends_at": self._stamp(self.ends_at)}

    def line(self, human: str) -> str:
        """One readable diagnostic line, naming the window and the zone."""
        origin = (" (%s)" % self.source) if self.source else ""
        if self.window is None:
            return "%s: no quiet hours declared (never held)" % human
        if self.problem == NO_TIMEZONE:
            return ("%s: quiet hours %s%s without a time zone — never held "
                    "(fail-open)" % (human, self.window.text, origin))
        if self.problem == INVALID_TIMEZONE:
            return ("%s: quiet hours %s%s with an unreadable time zone %r — never "
                    "held (fail-open)" % (human, self.window.text, origin,
                                          self.timezone))
        if self.active:
            return ("%s: in quiet hours now (%s %s%s; until %s)"
                    % (human, self.window.text, self.timezone, origin,
                       self._stamp(self.ends_at)))
        return ("%s: outside quiet hours (%s %s%s; next %s)"
                % (human, self.window.text, self.timezone, origin,
                   self._stamp(self.starts_at)))


def state(window: Window | None, timezone: str | None, now: float, *,
          source: str | None = None) -> State:
    """The state of `window` in `timezone` at epoch `now`.

    A missing window is "no quiet hours"; a missing or unreadable time zone
    is a named fail-open: the human is never quiet without a zone.
    """
    if window is None:
        return State(problem=NO_WINDOW, source=source)
    zone_name = window.timezone or timezone
    if not zone_name:
        return State(window=window, problem=NO_TIMEZONE, source=source)
    zone = _zone(zone_name)
    if zone is None:
        return State(window=window, timezone=zone_name,
                     problem=INVALID_TIMEZONE, source=source)
    local = datetime.fromtimestamp(float(now), zone)
    active = window.contains(local)
    if active:
        return State(active=True, window=window, timezone=zone_name, source=source,
                     problem=None, ends_at=window.next_boundary(local, "end"))
    return State(active=False, window=window, timezone=zone_name, source=source,
                 problem=None, starts_at=window.next_boundary(local, "start"))
