# SPDX-License-Identifier: AGPL-3.0-only
"""#2743: ad-hoc clones of ended lots, under the declared work roots.

A clone is a directory directly under a work root whose `.git` is a
directory (a clone, not a linked worktree). Agents leave these copies when
they clone a repository by hand (`git clone ... nexlink-665-reseed`); the
ordinary worktree lifecycle does not see them: they are not linked
worktrees and no turn recorded them.

A clone is removed only when ALL of this is proved:

* a lot is named by its **directory** (`<prefix>-<id>[-...]`) or by a
  `lot/<id>` / `work/<id>` branch; every identified lot exists in ameesh
  and ended (`merged`, `promoted`, `closed`) more than `CLONE_IDLE_S` ago.
  An `agent/` or `review/` branch alone proves nothing: those names are
  also the branches of long-lived agent checkouts;
* no activity in it for `CLONE_IDLE_S` (mtime/ctime at any depth, Git
  metadata included); the walk must be complete;
* its saved work is proved by [`housekeeping._git_copy_work`]: clean,
  ignored files only under the host's build artifacts, HEAD pushed or
  integrated, no unpushed local branch, no stash, no submodule, no other
  linked worktree, no remote stored inside it;
* it contains no nested Git copy (a nested clone with its own work would
  go with it);
* no other clone has an ``.git/objects/info/alternates`` entry pointing at
  its object directory (#2738): deleting it would break that clone;
* no process of the user works in it, and its name is not the name of a
  registered agent's checkout.

Everything else is reported with the reason, never removed. The pass is a
dry run by default: it returns the plan, writes nothing and removes
nothing. A removal goes through the host lock and trash journal of
[`build_artifacts`] (the directory is renamed, then deleted), exactly like a
worktree whose lot ended.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time

from . import build_artifacts
from . import housekeeping as hk
from . import storage
from .housekeeping_discovery import _marker

#: a clone must be inactive (and its lot ended) for this long before removal
CLONE_IDLE_S = 24 * 3600.0
#: bound of the shallow inventory of a pass
CLONE_LIMIT = 2000
#: bound of the whole clone pass (heavy Git checks included)
CLONE_BUDGET_S = 60.0


def _real(path: str) -> str:
    return hk._real(path)


def _agent_names(agents) -> tuple[str, ...]:
    return tuple(sorted({name for name in agents or () if name}, key=len, reverse=True))


def _strip_agents(name: str, agents) -> str:
    """Drop the registered agent names from a directory name.

    `ameesh-deepseek4-1666` names the lot #1666, not the agent number 4;
    `mty-amh-impl-04-1666` likewise. A name that IS an agent name is kept
    empty: that directory is an agent checkout, not a lot copy.
    """
    rest = name
    for agent in _agent_names(agents):
        if rest == agent:
            return ""
        if rest.startswith(agent + "-"):
            rest = rest[len(agent) + 1:]
            continue
        marker = "-" + agent + "-"
        if marker in rest:
            rest = rest.replace(marker, "-", 1)
    return rest


def named_lots(name: str, agents=()) -> list[int]:
    """Lot ids named by a clone's directory name.

    A one-digit group is read as an agent number when the name carries a
    longer number too (`ds4-749-review` is lot 749). When it is the only
    number it may be a lot (`lot-1`), so it is kept.
    """
    rest = _strip_agents(name or "", agents)
    # A version (`0.2.10`) is not a lot: a group surrounded by dots is not a
    # candidate. `-665-`, `_665`, a trailing `665` are.
    groups = re.findall(r"(?<![0-9.])([0-9]+)(?![0-9.])", rest)
    if any(len(part) > 1 for part in groups):
        groups = [part for part in groups if len(part) > 1]
    return [int(part) for part in groups]


def _branch_lots(path: str) -> list[int] | None:
    """Lot ids named by the clone's `lot/` or `work/` local branches.

    None when Git cannot be read (a doubt: nothing is removed). `agent/`
    and `review/` branches are deliberately ignored: a long-lived agent
    checkout would otherwise be attributed to an old lot it once worked on.
    """
    heads = hk._git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], path)
    if heads is None or heads.returncode != 0:
        return None
    branches = [line.strip() for line in heads.stdout.splitlines() if line.strip()]
    current = hk._git(["symbolic-ref", "-q", "--short", "HEAD"], path)
    if current is None or current.returncode not in (0, 1):
        return None
    if current.returncode == 0 and current.stdout.strip():
        branches.append(current.stdout.strip())
    out: list[int] = []
    for branch in dict.fromkeys(branches):
        if branch.split("/", 1)[0] not in ("lot", "work"):
            continue
        out.extend(hk.cited_lots(branch))
    return out


def _attribution(db, path: str, agents=()) -> tuple[list[dict] | None, list[int], str]:
    """Lots named by the clone itself: (rows, ids, reason). Rows None = doubt."""
    ids = named_lots(os.path.basename(os.path.normpath(path)), agents)
    branches = _branch_lots(path)
    if branches is None:
        return None, ids, "local branches unreadable"
    ids.extend(branches)
    unique = list(dict.fromkeys(ids))
    rows = []
    try:
        for ident in unique:
            row = storage.of(db).work.get(ident)
            if row is not None:
                rows.append(row)
    except Exception:
        return None, unique, "lot state unreadable"
    return rows, unique, ""


def _lot_end(row: dict) -> float | None:
    """When the lot ended, or None when its record cannot prove it.

    `merged`/`promoted` have closed_at NULL; updated_at is the merge time or
    later, so the age is never overestimated for those states.
    """
    ended = row.get("closed_ts")
    if ended is None:
        ended = row.get("updated_ts")
    try:
        return float(ended)
    except (TypeError, ValueError):
        return None


def _clones_under(roots) -> list[str]:
    """Direct children of the work roots that are clones (`git` marker dir)."""
    found: list[str] = []
    for root in roots or ():
        root = hk._expand(root)
        if not os.path.isdir(root):
            continue
        try:
            with os.scandir(root) as children:
                for child in sorted(children, key=lambda entry: entry.name):
                    if len(found) >= CLONE_LIMIT:
                        return found
                    try:
                        is_dir = child.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if not is_dir:
                        continue
                    path = os.path.join(root, child.name)
                    if _marker(path) == "clone":
                        found.append(path)
        except OSError:
            continue
    return found


def _repository_copies(state_dir: str, roots, agents, known_clones=None) -> list[str]:
    """Known clones on this host whose alternates can point at a candidate.

    The shallow work-root inventory, the private radar/train clones under
    the state directory (`git clone --shared`, #2738), and the registered
    agents' checkouts. Reading one more file is cheap; a dependent outside
    this perimeter would be a doubt we cannot see.
    """
    found: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        real = _real(path)
        if real not in seen and _marker(real) == "clone":
            seen.add(real)
            found.append(real)

    for path in (known_clones if known_clones is not None else _clones_under(roots)):
        add(path)
    for base in (os.path.join(state_dir, "radar"),
                 os.path.join(state_dir, "trains"),
                 os.path.join(state_dir, "trains", "shadow")):
        try:
            with os.scandir(base) as children:
                for child in sorted(children, key=lambda entry: entry.name):
                    try:
                        if child.is_dir(follow_symlinks=False):
                            add(child.path)
                    except OSError:
                        continue
        except OSError:
            continue
    for cwd in (agents or {}).values():
        if cwd:
            add(cwd)
    return found


def _objects_dir(repo: str) -> str:
    """Absolute object directory of a clone, or '' when it cannot be read."""
    try:
        st = os.lstat(os.path.join(repo, ".git"))
    except OSError:
        return ""
    if not stat.S_ISDIR(st.st_mode):
        return ""
    return _real(os.path.join(repo, ".git", "objects"))


def _alternate_paths(info: str, objects: str) -> list[str] | None:
    try:
        if os.stat(info).st_size > 1024 * 1024:
            return None
        with open(info, "r", encoding="utf-8", errors="surrogateescape") as stream:
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return None
    out = []
    for line in lines:
        value = line.strip()
        if not value:
            continue
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                return None          # a doubt, never a missed dependency
            if not isinstance(value, str):
                return None
        out.append(value if os.path.isabs(value) else os.path.join(objects, value))
    return out


def _dependent_objects(cfg, roots, agents, candidates, known_clones=None
                       ) -> tuple[dict[str, set[str]], str]:
    """candidate object dir -> repositories whose alternates point inside it.

    A doubt (unreadable alternates file) is returned as the second value:
    the caller then removes nothing, rather than break a clone it could not
    inspect.
    """
    objects = {}
    for path in candidates:
        obj = _objects_dir(path)
        if obj:
            objects[path] = obj
    by_objects = {obj: path for path, obj in objects.items()}
    out: dict[str, set[str]] = {path: set() for path in candidates}
    for repo in _repository_copies(cfg.state_dir, roots, agents,
                                   known_clones=known_clones):
        obj = _objects_dir(repo)
        if not obj:
            continue
        info = os.path.join(obj, "info", "alternates")
        try:
            st = os.lstat(info)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return out, "alternates unreadable (%s): %s" % (
                repo, exc.strerror or exc)
        if not stat.S_ISREG(st.st_mode):
            return out, "alternate marker is not a file: %s" % info
        paths = _alternate_paths(info, obj)
        if paths is None:
            return out, "alternates unreadable: %s" % info
        for path in paths:
            target = _real(path)
            for candidate_obj, candidate in by_objects.items():
                if hk._under(target, candidate_obj):
                    out[candidate].add(repo)
    return out, ""


def _removal_check(path: str, cfg, rules, allow, roots, agents) -> str:
    """Why the clone must stay, right before it is renamed ('' to go)."""
    if hk.worktree_in_use(path, allow=allow):
        return "a process is working in it"
    saved = hk._git_copy_work(path, path, rules)
    if saved:
        return saved
    dependents, doubt = _dependent_objects(cfg, roots, agents, [path])
    if doubt:
        return doubt
    if dependents.get(path):
        return "alternate dependent: %s" % ", ".join(sorted(dependents[path])[:3])
    return ""


def _remove(path: str, *, cfg, rules, allow, roots, agents, lock_path, journal_path,
            skip_roots) -> tuple[str, int, str]:
    """Remove one clone under the host lock and trash journal."""
    if not lock_path or not journal_path:
        return "garde", 0, "neither lock nor housekeeping journal"
    lock = build_artifacts._lock(lock_path)
    if lock is None:
        return "attend", 0, "another pass is deleting on the host"
    try:
        journal = build_artifacts.Journal(journal_path)
        budget = build_artifacts.Budget(build_artifacts.PASS_BUDGET_S)
        real = _real(path)
        ident = build_artifacts.identity(real)
        parent_id = build_artifacts.identity(os.path.dirname(real))
        avoid = build_artifacts._identities(
            [_real(p) for p in skip_roots if p] + [_real(p) for p in roots if p])
        if ident is None:
            return "garde", 0, "directory unreadable"

        def check() -> str:
            return _removal_check(path, cfg, rules, allow, roots, agents)

        try:
            done = build_artifacts.remove_worktree_dir(
                path, (real, ident), parent_id, journal=journal, avoid=avoid,
                budget=budget, check=check, kind="clone")
        except (OSError, ValueError) as exc:
            return "garde", 0, "removal refused: %s" % exc
        freed = done["bytes"]
        more = done["left"][:3] + done["errors"][:3]
        if done["stopped"]:
            more.append("time budget reached")
        if more:
            return "partial", freed, "left in %s: %s" % (done["trash"], "; ".join(more))
        return "removed", freed, ""
    finally:
        lock.close()


def reap(db, host: str, cfg, *, roots, agents: dict, protected, dry_run: bool,
         now: float, artifacts=None, allow=(), lock_path: str | None = None,
         journal_path: str | None = None, skip_roots=()) -> tuple[list[dict], list[dict], list[str]]:
    """Inventory the ad-hoc clones of a host's work roots: (entries, plan, skipped).

    `agents` maps a registered agent name to its working directory;
    `protected` holds the working directories of this host (never removed
    or entered). Dry run writes nothing and returns the same plan a real
    pass would act on.
    """
    if not roots or not os.path.isdir(cfg.state_dir):
        return [], [], []
    entries: list[dict] = []
    plan: list[dict] = []
    skipped: list[str] = []
    rules = build_artifacts.rules_of(artifacts)
    agents = dict(agents or {})
    protected_real = {_real(path) for path in protected or () if path}
    checkout_names = set(agents)
    for cwd in agents.values():
        if cwd:
            checkout_names.add(os.path.basename(os.path.normpath(_real(cwd))))
    tracked = set()
    try:
        tracked = {row["path"] for row in
                   storage.of(db).housekeeping.worktrees(host, ("active", "kept"), limit=None)}
    except Exception:
        skipped.append("clones: tracked worktrees unreadable, nothing removed")
        return [], [], skipped
    inventory = _clones_under(roots)
    if not inventory:
        return [], [], []
    dependents, alternates_doubt = _dependent_objects(
        cfg, roots, agents, inventory, known_clones=inventory)
    deadline = time.monotonic() + CLONE_BUDGET_S
    inspected = 0
    for path in inventory:
        if inspected >= CLONE_LIMIT or time.monotonic() >= deadline:
            skipped.append("clones: inventory bounded (%d inspected), the rest waits"
                           % inspected)
            break
        inspected += 1
        real = _real(path)
        name = os.path.basename(os.path.normpath(path))
        base = {"kind": "clone", "path": path, "agent": None, "lot": None}
        if real in protected_real or any(
                hk._under(_real(cwd), real) for cwd in protected_real if cwd):
            continue
        if real in tracked:
            continue
        if name in checkout_names:
            plan.append(dict(base, action="garde",
                             detail="name of a registered agent checkout; never removed "
                                    "automatically"))
            if not dry_run:
                entries.append(dict(base, action="signaled",
                                    detail="name of a registered agent checkout; kept"))
            continue
        rows, _ids, why = _attribution(db, path, agents)
        if rows is None:
            plan.append(dict(base, action="garde", detail="%s; kept" % why))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail="%s; kept" % why))
            continue
        if not rows:
            detail = ("no lot named by the directory or a lot/work branch; never "
                      "removed automatically")
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        base["lot"] = ",".join(str(row.get("id")) for row in rows)
        ended = []
        open_lots = []
        for row in rows:
            state = str(row.get("state") or "")
            if state not in hk.LOT_ENDED:
                open_lots.append("#%s %s" % (row.get("id"), state or "unknown"))
                continue
            moment = _lot_end(row)
            if moment is None:
                open_lots.append("#%s ended but its end time is unreadable" % row.get("id"))
                continue
            ended.append((moment, row))
        if open_lots:
            detail = "lot %s not proved ended; kept" % ", ".join(open_lots)
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        age = max(moment for moment, _row in ended)
        if now - age < CLONE_IDLE_S:
            detail = "lot %s ended %s ago; waits 24 h" % (
                base["lot"], hk.fmt_idle(now - age))
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        if hk.worktree_in_use(path, allow=allow):
            detail = "a process is working in it; kept"
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        activity = hk.scan_activity(path)
        if not activity["complete"]:
            detail = "activity not verifiable (%s); kept" % activity["why"]
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        latest = activity["latest"]
        if latest is None:
            detail = "activity unreadable; kept"
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        if now - latest < CLONE_IDLE_S:
            detail = "active %s ago; kept" % hk.fmt_idle(now - latest)
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        copies, doubt = hk._git_copies_report(path)
        nested = [copy for copy in copies if _real(copy) != real]
        if doubt:
            detail = "nested Git copies not fully inspected (%s); kept" % doubt
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        if nested:
            detail = "nested Git copy (%s); kept" % nested[0]
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        saved = hk._git_copy_work(path, path, rules)
        if saved:
            plan.append(dict(base, action="garde", detail="%s; kept" % saved))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail="%s; kept" % saved))
            continue
        if alternates_doubt:
            detail = "%s; kept" % alternates_doubt
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        if dependents.get(path):
            detail = "alternate dependent (%s); kept" % ", ".join(
                sorted(dependents[path])[:3])
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        detail = ("lot %s ended %s ago, clean and pushed, idle %s, no alternate "
                  "dependent" % (base["lot"], hk.fmt_idle(now - age),
                                 hk.fmt_idle(now - latest)))
        size = activity["bytes"]
        if dry_run:
            plan.append(dict(base, action="retrait", bytes=size, detail=detail))
            continue
        outcome, freed, note = _remove(
            path, cfg=cfg, rules=rules, allow=allow, roots=roots, agents=agents,
            lock_path=lock_path, journal_path=journal_path, skip_roots=skip_roots)
        if outcome in ("removed", "partial"):
            detail = "%s: %s" % (detail, "removed (logged trash)" if outcome == "removed"
                                else "partly removed")
            entries.append(dict(base, action=outcome, bytes=freed, detail=detail))
            plan.append(dict(base, action="retrait", bytes=freed, detail=detail))
        else:
            detail = "%s; %s" % (detail, note)
            entries.append(dict(base, action="signaled", detail=detail))
            plan.append(dict(base, action=outcome, detail=detail))
    return entries, plan, skipped
