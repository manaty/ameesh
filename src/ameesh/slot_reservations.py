# SPDX-License-Identifier: AGPL-3.0-only
"""Slot reservations and class admission (#5601, tranche 2 of #4201).

A Host record may reserve some of its `policy.max_agents` places for a
project or a priority class (`docs/usage/capacity-plan.md`):

    policy:
      max_agents: 6
      slot_reservations: {"project:nexlink": 2, "class:1": 1}

* `project:<app>` keeps places for agents whose next lot belongs to `<app>`;
* `class:<n>` keeps places for agents whose next lot has priority `<n>`.

The "next lot" of an agent is the first row of `lot_sessions.actionable`,
the one the agent would wake for (priority, then creation date). With
reservations, the claim pass admits candidates by class, then by the
creation date of that lot; elected roles stay first (their relief must not
wait behind backlog). A place is free for anyone only while the unfilled
reservations still fit in what remains. Without reservations nothing
changes: the candidates keep the registry order and no lot is read.

The capacity planner (`capacity_plan`) reads the same keys for its slots.
This module is pure: no database, no clock.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

KINDS = ("project", "class")


def parse(raw: Any) -> tuple[dict[str, int], list[str]]:
    """`policy.slot_reservations` -> ({key: count}, problems). An unreadable
    entry is dropped (and reported): a reservation that cannot be read must
    not hold places nobody can take."""
    if raw is None:
        return {}, []
    if not isinstance(raw, Mapping):
        return {}, ["`policy.slot_reservations`: mapping expected"]
    out: dict[str, int] = {}
    problems = []
    for key, value in raw.items():
        name = normalize_key(key)
        if name is None:
            problems.append("`policy.slot_reservations.%s`: key `project:<app>` or "
                            "`class:<priority>` expected" % key)
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append("`policy.slot_reservations.%s`: non-negative integer expected" % key)
            continue
        if value:
            out[name] = out.get(name, 0) + value
    return out, problems


def normalize_key(key: Any) -> Optional[str]:
    if not isinstance(key, str) or ":" not in key:
        return None
    kind, _, value = key.partition(":")
    kind, value = kind.strip(), value.strip()
    if kind not in KINDS or not value:
        return None
    if kind == "class":
        try:
            value = str(int(value))
        except ValueError:
            return None
    return "%s:%s" % (kind, value)


def keys_for(project: Any = None, priority: Any = None) -> list[str]:
    """The reservation keys a lot can fill, class first (admission by class)."""
    out = []
    if priority is not None and not isinstance(priority, bool):
        try:
            out.append("class:%d" % int(priority))
        except (TypeError, ValueError):
            pass
    if project not in (None, ""):
        out.append("project:%s" % str(project).strip())
    return out


def merge(policies: Iterable[Mapping[str, int]]) -> dict[str, int]:
    """Several Host records of one host: the largest count of each key (a
    reservation declared in one canon is kept by the others)."""
    out: dict[str, int] = {}
    for one in policies:
        for key, count in (one or {}).items():
            out[key] = max(out.get(key, 0), int(count))
    return out


def admission_key(agent: Mapping, demand: Optional[Mapping]) -> tuple:
    """Sort key of a candidate: elected roles first, then the class and the
    creation date of its next lot; no lot last."""
    if agent.get("releve") == "election":
        return (0, 0, 0.0)
    if not demand:
        return (2, 0, 0.0)
    try:
        priority = int(demand.get("priority"))
    except (TypeError, ValueError):
        priority = 99
    created = demand.get("created_ts")
    return (1, priority, float(created) if created is not None else float("inf"))


def order(candidates: Sequence[Mapping], demands: Mapping[str, Optional[Mapping]],
          reservations: Mapping[str, int]) -> list:
    """Candidates in admission order. No reservation: unchanged (the
    registry order). Ties keep the registry order (stable sort)."""
    if not reservations:
        return list(candidates)
    return sorted(candidates, key=lambda a: admission_key(a, demands.get(a.get("name"))))


def decide(reservations: Mapping[str, int], held: Mapping[Optional[str], int],
           used: int, cap: Optional[int], wants: Sequence[str]) -> tuple[bool, Optional[str]]:
    """(admitted, key filled) for one candidate.

    `held`: places in use per reservation key filled (None: unreserved);
    `used`: places in use in all; `wants`: keys the candidate's next lot can
    fill (`keys_for`). An unfilled reservation the candidate matches gives it
    a place while one is free; otherwise it takes a place only if the
    unfilled reservations of the others still fit in the rest."""
    if cap is None:
        return True, None
    free = cap - used
    if free <= 0:
        return False, None
    for key in wants:
        if held.get(key, 0) < reservations.get(key, 0):
            return True, key
    owed = sum(max(0, n - held.get(key, 0)) for key, n in reservations.items())
    return free > owed, None
