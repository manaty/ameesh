# SPDX-License-Identifier: AGPL-3.0-only
"""#1580 (point 2) — forfait margin and usage-billed agents.

Owner request (2026-10-15 10:25): "who distributes agents between accounts to
optimize costs?". The per-turn choice (0034, #1580 point 1) first consumes
what will be lost at reset, but nothing says how many usage-billed agents
(DeepSeek) could move to a forfait (Codex, Claude) that has margin.

`proposal()` reads the forfait accounts' gauges below the **non-critical**
threshold (the `RESERVE` stays with critical agents, L242b), estimates the
share of the long window an agent consumes (observed on the harness, floor
`DEFAULT_AGENT_SHARE`), and returns the number of movable agents — never more
than the measured margin carries. A family is never emptied: at least one
active agent stays in it, so that a review of another family remains possible
(canon §2).

**No switch is made here**: this is a displayed proposal
(`ameesh accounts list`), the human or the pilot decides. The per-turn budget
guard remains the sole judge of execution.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import accounts
from . import cost as cost_mod

#: Share of the long window an agent consumes **at minimum**: fallback when
#: no observation is available, floor of the observation otherwise. Five
#: points of a week: a prudent proposal (fewer agents announced).
DEFAULT_AGENT_SHARE = 0.05
#: A window shorter than a day resets too often to carry a durable switch:
#: capacity is read on the long window (L74).
LONG_WINDOW_S = 86400.0


@dataclass(frozen=True)
class Offer:
    """What a forfait harness can carry, on its long window."""

    harness: str
    free: float                      #: share available below the non-critical threshold
    unit: float                      #: share consumed by one agent (observed/floor)
    capacity: int                    #: agents that `free` carries (floor(free / unit))
    accounts: tuple = ()             #: accounts with a measured margin


@dataclass(frozen=True)
class Proposal:
    """The displayed proposal: how many usage-billed agents to move."""

    movable: int = 0
    #: distribution `(harness, count)` — one forfait harness at a time
    moves: tuple = ()
    offers: tuple = ()
    candidates: int = 0              #: usage-billed agents, excluding critical ones
    kept_for_review: int = 0         #: families not emptied (cross-review)
    reason: str = ""                 #: why no proposal is made

    def text(self) -> str:
        """The display line (`ameesh accounts list`)."""
        if self.reason:
            return "forfait margin: %s" % self.reason
        if not self.movable:
            return ("forfait margin: no movable agent (%d candidate(s), reserve of "
                    "critical agents and reviewer families kept)"
                    % self.candidates)
        detail = ", ".join("%s %d" % (harness, count) for harness, count in self.moves)
        return ("forfait margin available: %d movable agent(s) (%s); reserve of "
                "critical agents and reviewer families kept" % (self.movable, detail))

    def as_dict(self) -> dict:
        return {
            "movable": self.movable,
            "moves": [{"harness": h, "agents": n} for h, n in self.moves],
            "candidates": self.candidates,
            "kept_for_review": self.kept_for_review,
            "offers": [
                {"harness": o.harness, "free": o.free, "unit": o.unit,
                 "capacity": o.capacity, "accounts": list(o.accounts)}
                for o in self.offers],
            "reason": self.reason,
        }


def live_fleet(db, now: float, host: str | None = None) -> dict:
    """`{agent: harness}` of the agents **under lease** at `now` (R3, L37).

    A stopped, expired or `externe` agent is not a move: the lease alone
    authorizes it to run. `host` restricts to one host (the declared accounts
    are those of one host). Unreadable database: `{}` — no proposal rather
    than a wrong count.
    """
    from . import registry  # local import: keeps the display module light
    try:
        rows = registry.overview(db)
    except Exception:  # noqa: BLE001 - optional read, never fatal to the display
        return {}
    fleet = {}
    for row in rows:
        if row.get("status") == "stopped" or row.get("mode") == "externe":
            continue
        expires = row.get("lease_expires_ts")
        if not row.get("lease_owner") or expires is None:
            continue
        if host is not None and row.get("host") != host:
            continue
        try:
            alive = float(expires) > now
        except (TypeError, ValueError):
            alive = False
        if alive:
            fleet[str(row.get("name"))] = str(row.get("harness") or "")
    return fleet


def _offer(items: list, agents: list, now: float, book) -> Offer:
    """The margin of a forfait harness: its accounts, their long window."""
    free = 0.0
    used = 0.0
    measured = []
    for profile in items:
        if profile.kind == "api_key_env":
            continue  # an API key is not a forfait capacity
        # non-critical threshold: the reserve stays with critical agents (L242b)
        # #1580 (slice 3): `model=''` — the margin is read on the general
        # windows; a model-specific bucket (Fable) does not cancel it for
        # the agents of another model. Per-model capacity remains to be done.
        result = accounts.evaluate(book, profile, items, now, record=False,
                                   reserve=True, model="")
        # An expired window does not measure the next one, and an expired short
        # one invalidates the long one still open (#1561). The display promises
        # nothing without a current margin, even if the guard allows a try.
        if any(g.reset_passed(now) for g in result.gauges):
            continue
        stamps = [stamp for g in result.gauges
                  if (stamp := getattr(g, "read_at", None)) is not None]
        if stamps and now - min(stamps) > min(g.window_s for g in result.gauges):
            continue
        long = [g for g in result.gauges if g.window_s >= LONG_WINDOW_S]
        if not long:
            continue  # no long window measured: nothing to carry
        # #1789/N5: the observed unit sums the usage of ALL the harness's
        # measurable accounts, including the one the guard sets aside
        # (saturated, fixed cap): its agents count in `agents`, so its
        # consumption must count too — otherwise the unit is underestimated
        # and the announced capacity too large.
        used += sum(g.used_at(now) for g in long)
        if not result.ok or result.hard:
            continue
        free += min(max(0.0, result.limits[g.key][0] - g.used_at(now)) for g in long)
        measured.append(profile.name)
    unit = DEFAULT_AGENT_SHARE
    if agents:
        # the observation only raises the cost of one agent (never lowers it
        # below the floor): a prudent proposal
        unit = max(DEFAULT_AGENT_SHARE, min(1.0, used / len(agents)))
    harness = items[0].harness if items else ""
    return Offer(harness=harness, free=free, unit=unit,
                 capacity=int(free / unit) if unit > 0 else 0,
                 accounts=tuple(measured))


def proposal(cfg, db, book, *, now: float | None = None, fleet: dict | None = None,
             critical=()) -> Proposal:
    """The switch proposal, without any action: read-only.

    `fleet`: `{agent: harness}` to consider (default: the agents under lease,
    `live_fleet`). `critical`: names never to move (default:
    `cfg.critical_agents`) — their reserve is additionally kept in the margin.
    """
    now = time.time() if now is None else now
    try:
        declared = accounts.parse(getattr(cfg, "accounts", None) or {})
    except accounts.AccountError as exc:
        return Proposal(reason="invalid account configuration: %s" % exc)
    try:
        billed = set(cost_mod.paid_harnesses_of(getattr(cfg, "host", None)))
    except cost_mod.CostError as exc:
        # fail-closed (0019 §2): without descriptors, we do not guess who is
        # usage-billed
        return Proposal(reason="usage-billed harnesses unknown: %s" % exc)
    if fleet is None:
        fleet = live_fleet(db, now, getattr(cfg, "host", None))
    agents = {name: harness for name, harness in fleet.items() if harness}
    kept_names = set(critical) | set(getattr(cfg, "critical_agents", ()) or ())
    candidates = sorted(name for name, harness in agents.items()
                        if harness in billed and name not in kept_names)
    if not candidates:
        return Proposal(reason="no usage-billed agent to move")
    # Same definition of criticals as the executor (L242b), including the
    # roles in the owner canon and the declared orchestrators.
    from . import auditor, sous_utilisation
    try:
        canons = auditor.load_all(cfg)
        declared_critical = sous_utilisation.declared_orchestrators()
        candidates = [name for name in candidates if not auditor.is_critical(
            cfg, db, name, canons, declared=declared_critical)]
    except Exception:  # noqa: BLE001 - unreadable roles: no proposal
        return Proposal(reason="critical agent roles unknown")
    # never empty a family: cross-review (canon §2) assumes at least two
    # families represented after the switch
    kept = 0
    for family in set(agents.values()):
        members = [name for name, harness in agents.items() if harness == family]
        if members and all(name in candidates for name in members):
            kept += 1
    offers = [_offer(declared[harness], [name for name, h in agents.items()
                                         if h == harness], now, book)
              for harness in sorted(declared) if harness not in billed]
    ranked = sorted((o for o in offers if o.capacity > 0),
                    key=lambda o: (-o.capacity, o.harness))
    movable = min(len(candidates) - kept, sum(o.capacity for o in ranked))
    moves = []
    remaining = movable
    for offer in ranked:
        take = min(offer.capacity, remaining)
        if take:
            moves.append((offer.harness, take))
        remaining -= take
    return Proposal(movable=max(0, movable), moves=tuple(moves), offers=tuple(offers),
                    candidates=len(candidates), kept_for_review=kept)
