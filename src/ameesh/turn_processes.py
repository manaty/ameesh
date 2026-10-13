# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only process-group liveness for closing turn resources."""
from __future__ import annotations

from collections.abc import Iterable

from . import platform


#: a strict scan still fails closed on an unreadable member; retry the pair
#: before giving up, so a transient read error does not leave every group
#: unknown for a whole review period.
SNAPSHOT_ATTEMPTS = 3


def _two_snapshots() -> tuple[dict, dict, set]:
    """Two back-to-back strict snapshots: (first, second, non-zombie groups)."""
    snapshots = []
    groups: set = set()
    for _ in range(2):
        members: dict = {}
        for row in platform.process_table(strict=True):
            members.setdefault(row["pgid"], set()).add((row["pid"], row["start"]))
            if not row["zombie"]:
                groups.add(row["pgid"])
        snapshots.append(members)
    return snapshots[0], snapshots[1], groups


def running_groups(pgids: Iterable[int] = ()) -> set[int] | None:
    """Keep live, changing, or unreadable groups open.

    A zombie-only group must have identical member identities in both strict
    scans. Zombies cannot fork replacements. For requested groups absent in
    both scans, probe kernel existence to cover fork/exit enumeration races.
    A member that vanished mid-scan is omitted by the scan itself (it is gone
    and cannot fork again); an unreadable member still fails the pair closed.
    The pair is retried `SNAPSHOT_ATTEMPTS` times before giving up.
    Never reap or send a nonzero signal.
    """
    try:
        for _ in range(SNAPSHOT_ATTEMPTS):
            try:
                first, second, groups = _two_snapshots()
                break
            except (platform.NotAvailable, OSError):
                continue
        else:
            return None
        # Snapshot limitation (#472, #717): a live member can fork and leave
        # the group — or vanish mid-scan after a fork landing past the pid
        # enumeration — without appearing in either scan. Identical zombie
        # sets can then hide a descendant still in the group. Excluding those
        # fork races requires cgroup membership tracking; these read-only
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
