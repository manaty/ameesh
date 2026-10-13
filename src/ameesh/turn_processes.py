# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only process-group liveness for closing turn resources."""
from __future__ import annotations

from collections.abc import Iterable

from . import platform


def running_groups(pgids: Iterable[int] = ()) -> set[int] | None:
    """Keep live, changing, or unreadable groups open.

    A zombie-only group must have identical member identities in both strict
    scans. Zombies cannot fork replacements. For requested groups absent in
    both scans, probe kernel existence to cover fork/exit enumeration races.
    Never reap or send a nonzero signal.
    """
    try:
        snapshots = []
        groups = set()
        for _ in range(2):
            members = {}
            for row in platform.process_table(strict=True):
                members.setdefault(row["pgid"], set()).add((row["pid"], row["start"]))
                if not row["zombie"]:
                    groups.add(row["pgid"])
            snapshots.append(members)
        first, second = snapshots
        # Snapshot limitation (#472): a live member can fork, then setsid()
        # before its stat is read, in both scans. Identical zombie sets can
        # then hide a descendant still in the group. Excluding this fork +
        # setsid race requires cgroup membership tracking; these read-only
        # snapshots can only decide whether to settle a resource record.
        for pgid in first.keys() | second.keys():
            if first.get(pgid) != second.get(pgid):
                groups.add(pgid)
        for pgid in pgids:
            if pgid not in first and pgid not in second and platform.process_group_exists(pgid):
                groups.add(pgid)
        return groups
    except (platform.NotAvailable, OSError):
        return None


def group_running(pgid: int | None) -> bool:
    """Unknown membership keeps the resource open; stable zombies do not."""
    if not pgid:
        return False
    groups = running_groups([pgid])
    return groups is None or pgid in groups
