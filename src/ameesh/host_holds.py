# SPDX-License-Identifier: AGPL-3.0-only
"""Host hold (lot #254): the owner stops a host's runners on purpose.

A disk migration or a night of load: the owner stops a host's runners, and
until now every alert read it as an outage — stale lots, mail waiting, idle
capacity, each advising to wake, resume or reassign. A hold is recorded
(`host_holds`, 0392) by `ameesh hosts HOST --hold --until 2h --reason …`,
lifted by `--release`, and always ends: `until` is required, 24 h at most on
database time (whatever the caller's clock); past it the hold is ignored and
the alerts come back by themselves. Every hold and release is kept in
`host_hold_events`, written in the same transaction.

While it lasts, `exploitation.alerts` — the one source of `ameesh alerts`,
the notifier, the briefs and their reminders — leaves out the nudges about
the agents held with the host (`MUTED_TYPES` keyed on one agent;
`idle_capacity` and `plan_underused` leave them out of their lists), and
says it in one `host_hold` line per host instead. An agent is held when
every host it is admitted on is held (`admitted_hosts`), or, admitted
nowhere, when its registry host is. A lost lease (`dead_runner`) stays loud:
a hold is no reason for a working runner to vanish.

Nor does a held host take new work: its runners claim no lease
(`claimable`), no agent is relocated onto it (`relocation`, and the
`relocate` write itself), and automatic intake skips its agents
(`backlog.auto_take`).
"""
from __future__ import annotations

import datetime as _dt
import re

from . import storage

#: a hold always ends, and soon: the longest one
MAX_HOLD_S = 24 * 3600.0

#: alerts about one agent that a hold mutes: each one advises waking,
#: resuming or reassigning what the owner stopped on purpose (not
#: `dead_runner`: a lease lost by a working runner is an outage, hold or not)
MUTED_TYPES = ("idle_with_mail", "stopped_with_mail", "stale_lot", "engagement_overdue")

_SPAN_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.I)
_UNIT_S = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class HoldError(ValueError):
    pass


def parse_until(text: str) -> tuple[float | None, float | None]:
    """`--until`: a span (`90m`, `2h`) or an ISO date (a naive one is local
    time), as `(seconds, None)` or `(None, epoch)`. The span is checked here
    (after now, `MAX_HOLD_S` at most); an instant is judged by the database
    on its own clock (`HostResources.hold`)."""
    text = (text or "").strip()
    match = _SPAN_RE.match(text)
    if match:
        seconds = float(match.group(1)) * _UNIT_S[match.group(2).lower()]
        if seconds <= 0:
            raise HoldError("--until: %s is not a span after now" % text)
        if seconds > MAX_HOLD_S:
            raise HoldError(TOO_LONG)
        return seconds, None
    try:
        moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        raise HoldError("--until: a span (90m, 2h) or an ISO date, not %r" % text) from None
    return None, (moment.timestamp() if moment.tzinfo else moment.astimezone().timestamp())


TOO_LONG = ("--until: a hold lasts %d h at most (it always ends; set it again if needed)"
            % (MAX_HOLD_S // 3600))


def known(cfg, db, host: str) -> bool:
    """A host of a configured canon (Host card) or of the registry: the only
    ones a hold may name."""
    from . import canon as canon_mod
    if any(h.title == host for canon in canon_mod.load_configured(cfg) for h in canon.hosts):
        return True
    return any(row.get("host") == host for row in storage.of(db).operations.listing())


def live(db) -> dict[str, dict]:
    """The holds in progress, by host."""
    return {row["host"]: row for row in storage.of(db).hosts.holds()}


def held_agents(listing: list, holds: dict) -> dict[str, str]:
    """{agent: held host} for the agents a hold stops."""
    out = {}
    if not holds:
        return out
    for row in listing:
        admitted = [h for h in (row.get("admitted_hosts") or ()) if h]
        if admitted:
            if all(h in holds for h in admitted):
                out[row["name"]] = row.get("host") if row.get("host") in holds else admitted[0]
        elif row.get("host") in holds:
            out[row["name"]] = row["host"]
    return out


def mute(alerts: list, listing: list, holds: dict, held: dict, now: float) -> list:
    """The alerts with the held agents' nudges left out, and one `host_hold`
    line per hold."""
    if not holds:
        return alerts
    from .exploitation import _alert

    kept, muted = [], {host: [] for host in holds}
    for alert in alerts:
        host = held.get(alert.get("agent") or "")
        if alert.get("type") in MUTED_TYPES and host:
            muted[host].append(alert)
        else:
            kept.append(alert)
    for host, hold in sorted(holds.items()):
        agents = sorted(name for name, h in held.items() if h == host)
        counts: dict[str, int] = {}
        for alert in muted[host]:
            counts[alert["type"]] = counts.get(alert["type"], 0) + 1
        until = _dt.datetime.fromtimestamp(float(hold["until_ts"])).astimezone()
        kept.append(_alert(
            "host_hold", None, hold.get("created_ts"), len(muted[host]), None,
            "host %s on hold until %s (by %s: %s); %d alert(s) muted%s for %s; idle capacity "
            "and plan alerts leave them out. Lift it: ameesh hosts %s --release" % (
                host, until.isoformat(timespec="minutes"), hold["author"], hold["reason"],
                len(muted[host]),
                " (%s)" % ", ".join("%s %d" % kv for kv in sorted(counts.items()))
                if counts else "",
                "its agents (%s)" % ", ".join(agents) if agents else "no agent of its own",
                host),
            host=host, until=round(float(hold["until_ts"]), 3), author=hold["author"],
            reason=hold["reason"], agents=agents, muted=counts))
    return kept
