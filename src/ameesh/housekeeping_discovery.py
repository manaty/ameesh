# SPDX-License-Identifier: AGPL-3.0-only
"""#2180: find work copies outside the before/after window of a turn.

Only linked worktrees of a local agent's repository enter the existing
cleanup lifecycle. Main checkouts, unknown repositories and plain folders
are reported. Ad-hoc clones are left to `housekeeping_clones` (#2743),
which attributes them to a lot and reports or removes them under its own
guards; this module does not report them twice. Discovery itself never
removes anything or revives old lot metadata: a reused path gets a fresh
registration and age guard.
"""
from __future__ import annotations

import os
import stat
import time

from . import housekeeping as hk
from . import storage

DISCOVERY_LIMIT = 2000
DISCOVERY_BUDGET_S = 30.0


def _marker(path: str) -> str:
    """Inspect only the directory and its marker, without following links."""
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode) or os.path.realpath(path) != path:
            return "unsafe"
        mode = os.lstat(os.path.join(path, ".git")).st_mode
    except FileNotFoundError:
        return "plain"
    except OSError:
        return "unsafe"
    if stat.S_ISREG(mode):
        return "linked"
    return "clone" if stat.S_ISDIR(mode) else "unsafe"


def _attribution(db, path: str, branch: str, agents: dict, witness: str):
    nested = [(name, cwd) for name, cwd in agents.items()
              if hk._under(path, hk._real(cwd))]
    if nested:
        nearest = max(len(hk._real(cwd)) for _name, cwd in nested)
        owners = [name for name, cwd in nested if len(hk._real(cwd)) == nearest]
        if len(owners) == 1:
            name = owners[0]
            return name, hk.lot_of_worktree(db, name, path, branch)
        return witness, None
    candidates = hk.cited_lots(branch)
    if not candidates:
        for name in agents:
            ident = hk.named_lot(os.path.basename(path), name)
            if ident is not None:
                candidates.append(ident)
                witness = name
    for ident in candidates:
        row = storage.of(db).work.get(ident)
        if row and row.get("assignee") in agents:
            return row["assignee"], str(ident)
    return witness, None


def discover(db, host: str, agents: dict, roots, *, dry_run: bool) -> dict:
    """Bounded, shallow inventory; dry run performs no registration or log.

    `agents` contains only this host's registered working directories.
    `roots` comes from its Host card, never from a guess about HOME. Git
    lists each known repository once, including siblings outside the roots.
    """
    entries, plan, skipped = [], [], []
    deadline = time.monotonic() + DISCOVERY_BUDGET_S
    seen, candidates = set(), {}
    inspected = 0

    def exhausted():
        return inspected >= DISCOVERY_LIMIT or time.monotonic() >= deadline

    def signal(path, detail):
        line = dict(kind="workcopy", path=path, agent=None, lot=None, detail=detail)
        plan.append(dict(line, action="garde"))
        if not dry_run:
            entries.append(dict(line, action="signaled"))

    for name, cwd in sorted(agents.items()):
        if exhausted():
            break
        cwd = hk._expand(cwd)
        if cwd in seen or _marker(cwd) not in ("clone", "linked"):
            continue
        listed = hk.list_worktrees(cwd)
        inspected += 1
        if not listed:
            continue
        repo = hk.common_dir(cwd)
        if not repo:
            continue
        for index, row in enumerate(listed):
            path = row["path"]
            seen.add(path)
            if index and not row.get("bare"):
                candidates[path] = (row, repo, name)

    protected = {hk._real(cwd) for cwd in agents.values()}
    store = storage.of(db).housekeeping
    tracked = {row["path"] for row in store.worktrees(host, ("active", "kept"))}
    remaining = [item for item in sorted(candidates.items())
                 if item[0] not in protected and item[0] not in tracked]
    for path, (row, repo, witness) in remaining[:DISCOVERY_LIMIT]:
        if time.monotonic() >= deadline:
            break
        if _marker(path) != "linked" or not hk.same_repository(path, repo):
            signal(path, "linked worktree no longer matches its repository; kept")
            continue
        # A checkout or recreation can happen after the repository listing.
        # Attribute the current branch, never the old snapshot's lot.
        head = hk._git(["rev-parse", "--verify", "HEAD"], path)
        branch = hk._git(["symbolic-ref", "-q", "--short", "HEAD"], path)
        if head is None or head.returncode != 0 or branch is None \
                or branch.returncode not in (0, 1):
            signal(path, "current worktree references unreadable; kept")
            continue
        branch_name = branch.stdout.strip() if branch.returncode == 0 else ""
        agent, lot = _attribution(db, path, branch_name, agents, witness)
        detail = ("discovered linked worktree; fresh age guard and normal cleanup checks"
                  if lot else "%s: discovered linked worktree; normal orphan checks"
                  % hk.UNATTRIBUTED)
        line = dict(kind="worktree", path=path, agent=agent, lot=lot, detail=detail)
        plan.append(dict(line, action="register"))
        if dry_run:
            continue
        fresh = store.register_worktree(host=host, path=path, repo=repo, agent=agent,
            lot=lot, turn_id=None, branch=branch_name, head=head.stdout.strip())
        if fresh is not None:
            store.set_worktree_status(fresh["id"], "active", detail)
            entries.append(dict(line, action="registered"))

    # Reporting comes after adoption so a large container directory cannot
    # starve registration. Never recurse through source trees or links.
    for root in sorted(set(roots)):
        root = hk._expand(root)
        if exhausted():
            break
        if _marker(root) != "plain" or not os.path.isdir(root):
            continue
        try:
            with os.scandir(root) as children:
                for child in children:
                    if exhausted():
                        break
                    inspected += 1
                    if not child.is_dir(follow_symlinks=False):
                        continue
                    path = child.path
                    if path in protected or path in candidates:
                        continue
                    kind = _marker(path)
                    if kind == "clone":
                        # #2743: `housekeeping_clones` owns ad-hoc clones
                        # (attribution, dry-run report, removal guards).
                        continue
                    elif kind == "plain":
                        signal(path, "directory without a Git marker; never removed automatically")
                    elif kind == "linked":
                        signal(path, "linked worktree outside the known local repositories; kept")
                    else:
                        signal(path, "unreadable directory or symbolic Git marker; kept")
        except OSError:
            skipped.append("work copy discovery: root unreadable (%s)" % root)
    if exhausted() or len(remaining) > DISCOVERY_LIMIT:
        skipped.append("work copy discovery: bounded inventory incomplete")
    return {"entries": entries, "plan": plan, "skipped": skipped}
