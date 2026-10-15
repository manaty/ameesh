# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only Linux tool placement snapshot (#490); no per-turn cleanup claim."""
from __future__ import annotations

import re
from pathlib import Path

from . import NotAvailable, is_linux


def unified_cgroup(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("0::/"):
            return line[3:].rstrip("/") or "/"
    raise ValueError("no unified cgroup-v2 entry")


def check(runner_pid: int, proc: Path | None = None) -> dict:
    """Read-only snapshot of descendants, including dsh scopes named by PID.

    A reparented process in a scope is attributable through the harness PID in
    its unit name. An escaped/reparented PGID process is not attributable here:
    this is placement evidence, not an exhaustive per-turn cleanup proof.
    """
    if proc is None:
        if not is_linux():
            raise NotAvailable("tool cgroup placement", "requires Linux cgroup v2")
        proc = Path("/proc")
    root = unified_cgroup((proc / str(runner_pid) / "cgroup").read_text())
    rows = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # comm in /proc/PID/stat can contain spaces and parentheses.
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            rows[int(entry.name)] = (
                int(fields[1]), unified_cgroup((entry / "cgroup").read_text()))
        except FileNotFoundError:
            continue  # exited during the snapshot
    owned = {runner_pid}
    while True:
        more = {pid for pid, (ppid, _) in rows.items() if ppid in owned} - owned
        if not more:
            break
        owned.update(more)
    outside = []
    for pid, (_, group) in sorted(rows.items()):
        scope = re.search(r"/dsh-subprocess-(\d+)-[^/]+\.scope(?:/|$)", group)
        if pid not in owned and not (scope and int(scope[1]) in owned):
            continue
        if root != "/" and group != root and not group.startswith(root + "/"):
            outside.append({"pid": pid, "cgroup": group})
    return {"runner_pid": runner_pid, "runner_cgroup": root,
            "ok": not outside, "outside": outside}

