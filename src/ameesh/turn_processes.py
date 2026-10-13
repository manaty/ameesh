# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only process-group liveness for closing turn resources."""
from __future__ import annotations

from . import platform


def running_groups() -> set[int] | None:
    """Groups with a non-zombie member; None means the table is unavailable.

    Keep groups seen on either of two scans: a parent exiting while a child
    replaces it can leave one scan empty. Never reap or signal a process.
    """
    try:
        groups = set()
        for _ in range(2):
            groups.update(row["pgid"] for row in platform.process_table()
                          if not row["zombie"])
        return groups
    except (platform.NotAvailable, OSError):
        return None


def group_running(pgid: int | None) -> bool:
    """Unknown membership keeps the resource open; zombies alone do not."""
    if not pgid:
        return False
    groups = running_groups()
    return groups is None or pgid in groups
