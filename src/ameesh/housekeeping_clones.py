# SPDX-License-Identifier: AGPL-3.0-only
"""#2743: ad-hoc clones of ended lots, under the declared work roots.

A clone is a directory directly under a work root whose `.git` is a
directory (a clone, not a linked worktree). Agents leave these copies when
they clone a repository by hand (`git clone ... nexlink-665-reseed`); the
ordinary worktree lifecycle does not see them: they are not linked
worktrees and no turn recorded them.

A clone is removed only when ALL of this is proved:

* a lot is named by its **directory** (`<prefix>-<id>[-...]`) or by a
  `lot/<id>` / `work/<id>` branch; **every** identified lot exists in
  ameesh (an unknown id is a doubt: `copy-2181-999999` is not proved for
  #999999, so #2181 alone cannot justify a removal) and ended (`merged`,
  `promoted`, `closed`) more than `CLONE_IDLE_S` ago. An `agent/` or
  `review/` branch alone proves nothing: those names are also the branches
  of long-lived agent checkouts;
* no activity in it for `CLONE_IDLE_S` (mtime/ctime at any depth, Git
  metadata included); the walk must be complete;
* its saved work is proved by [`housekeeping._git_copy_work`]: clean,
  ignored files only under the host's build artifacts, HEAD pushed or
  integrated, no local ref commit outside remote branches, no stash, no
  submodule, no other linked worktree, no remote stored inside it;
* it contains no nested Git copy (a nested clone with its own work would
  go with it);
* no other clone has an ``.git/objects/info/alternates`` entry pointing at
  its object directory (#2738): deleting it would break that clone. The
  dependency inventory walks the work roots, the radar/train trees and the
  registered agent checkouts recursively — a dependent nested under a plain
  container, a **bare** repository or **another checkout's working tree** is
  seen — skipping only a Git copy's ``.git`` metadata and the build output
  and cache directories (a bare repository has no working tree to enter). A
  bounded, mount-crossing or unreadable inventory is a **doubt**: nothing is
  removed, and the reason is reported;
* no process of the user works in it, and its name is not the name of a
  registered agent's checkout;
* the candidate inventory and the dependency inventory are both complete:
  a hidden entry, an unreadable root/marker, a limit or a time budget makes
  the pass report and remove nothing (`skipped` says why).

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
#: bound of the recursive Git-copy inventory that proves dependencies (the
#: whole tree of the work roots and agent checkouts is entered, not only
#: their containers: a dependent can be nested in another checkout)
DEPENDENCY_LIMIT = 300_000
DEPENDENCY_BUDGET_S = 60.0
#: regenerable directories never entered by the dependency inventory
DEPENDENCY_SKIP = (".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
                   ".ruff_cache", ".tox", ".nox", "htmlcov", "_site")


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
        if rest.endswith("-" + agent):
            rest = rest[:-len(agent) - 1]
            continue
        marker = "-" + agent + "-"
        if marker in rest:
            rest = rest.replace(marker, "-", 1)
    return rest


#: a number glued to one of these tokens still names a lot (`wt3220`,
#: `.work-1392`, `diag462` #3277) ; glued to any other letters it is a SHA
#: fragment (`review-3220-ab1234cd` names #3220, not #1234 ; `wt3220-ca47`
#: not #47). `diag` holds `i` and `g`, out of hex: never a SHA fragment.
NAME_TOKEN = re.compile(
    r"(?:^|[._-])(wt|work|lot|review|prep|tmp|scratch|agent|paquet|diag)$",
    re.IGNORECASE)

#: a date (`2026-10-24`, `20261024`) names no lot (#3277): blanked before the
#: numbers are read, so `diag2024-10-24` is not #2024, #10 and #24.
NAME_DATE = re.compile(r"(?<!\d)(?:(?:19|20)\d\d-\d\d-\d\d|20\d{6})(?!\d)")


def _named_numbers(rest: str) -> list[str]:
    """The numbers a name carries, with the conventions that make one a lot.

    A version (`0.2.10`) is not a lot: a number touching a dot is out. A
    number glued to letters is a SHA fragment, not a lot, unless a
    worktree/clone token names it. A zero-padded group (`-02`) is never a lot
    (#2182). A one-digit group is an agent number when the name also carries
    a longer number (`ds4-749-review` is #749)."""
    found = []
    rest = NAME_DATE.sub(lambda match: "-" * len(match.group()), rest)
    for match in re.finditer(r"\d+", rest):
        start, end = match.span()
        before = rest[start - 1] if start else ""
        after = rest[end] if end < len(rest) else ""
        if before == "." or after == ".":
            continue
        if (before.isalpha() or after.isalpha()) \
                and not NAME_TOKEN.search(rest[:start]):
            continue
        part = match.group()
        if len(part) > 1 and part.startswith("0"):
            continue
        found.append(part)
    if any(len(part) > 1 for part in found):
        found = [part for part in found if len(part) > 1]
    return found


def named_lots(name: str, agents=()) -> list[int]:
    """Lot ids named by a clone's directory name.

    A one-digit group is read as an agent number when the name carries a
    longer number too (`ds4-749-review` is lot 749). When it is the only
    number it may be a lot (`lot-1`), so it is kept. `_named_numbers` states
    the rest of the convention (versions, SHA fragments, zero padding).
    """
    rest = _strip_agents(name or "", agents)
    return [int(part) for part in _named_numbers(rest)]


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
        prefix, _, rest = branch.partition("/")
        if prefix not in ("lot", "work") or not rest:
            continue
        # `lot/<id>-<slug>`: the ids are the leading numeric groups; the
        # first non-numeric token starts the slug (`lot/1111-roadmap-14-10`
        # names lot #1111, not #14 or #10) and ends the lot candidates.
        for part in rest.split("-"):
            if not part.isdigit():
                break
            out.append(int(part))
    return out


def _attribution(db, path: str, agents=()) -> tuple[list[dict] | None, list[int], str]:
    """Lots named by the clone itself: (rows, missing ids, reason).

    Every id the directory or a `lot/`/`work/` branch names must exist in
    ameesh: an unknown id is a doubt, never silently dropped (`copy-2181-
    999999` is not proved for #999999, so #2181 alone cannot justify a
    removal). `rows` None = unreadable registry.
    """
    ids = named_lots(os.path.basename(os.path.normpath(path)), agents)
    branches = _branch_lots(path)
    if branches is None:
        return None, ids, "local branches unreadable"
    ids.extend(branches)
    unique = list(dict.fromkeys(ids))
    rows = []
    missing = []
    try:
        for ident in unique:
            row = storage.of(db).work.get(ident)
            if row is None:
                missing.append(ident)
            else:
                rows.append(row)
    except Exception:
        return None, unique, "lot state unreadable"
    return rows, missing, ""


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


def _clones_under(roots) -> tuple[list[str], bool, str]:
    """Candidates directly under the work roots: (paths, complete, why).

    The candidate list itself may be bounded: a host with more clones than
    `CLONE_LIMIT` reports the first ones, and the pass says the inventory is
    incomplete and removes nothing. An unreadable entry/marker is a doubt,
    never silently skipped: it could hide a clone whose dependency we would
    then fail to prove.
    """
    found: list[str] = []
    more = False
    for root in roots or ():
        root = hk._expand(root)
        try:
            st = os.stat(root)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return found, False, "unreadable root %s (%s)" % (
                root, exc.strerror or exc)
        if not stat.S_ISDIR(st.st_mode):
            continue
        try:
            with os.scandir(root) as children:
                try:
                    entries = sorted(children, key=lambda entry: entry.name)
                except OSError as exc:
                    return found, False, "unreadable root %s (%s)" % (
                        root, exc.strerror or exc)
                for child in entries:
                    try:
                        is_dir = child.is_dir(follow_symlinks=False)
                    except OSError as exc:
                        return found, False, "unreadable entry %s (%s)" % (
                            child.path, exc.strerror or exc)
                    if not is_dir:
                        continue
                    path = os.path.join(root, child.name)
                    kind = _marker(path)
                    if kind == "clone":
                        if len(found) < CLONE_LIMIT:
                            found.append(path)
                        else:
                            more = True
                    elif kind == "unsafe":
                        return found, False, "unsafe Git marker: %s" % path
        except OSError as exc:
            return found, False, "unreadable root %s (%s)" % (
                root, exc.strerror or exc)
    if more:
        return found, False, "more than %d candidate clones" % CLONE_LIMIT
    return found, True, ""


def _linked_objects(path: str) -> tuple[str, str]:
    """Object directory of a linked worktree at `path`: (objects, why)."""
    marker = os.path.join(path, ".git")
    try:
        with open(marker, "r", encoding="utf-8", errors="surrogateescape") as stream:
            text = stream.read(4096)
    except OSError as exc:
        return "", "gitdir file unreadable (%s)" % (exc.strerror or exc)
    if not text.startswith("gitdir:"):
        return "", "gitdir file without a gitdir line"
    gitdir = text[len("gitdir:"):].strip()
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(path, gitdir)
    gitdir = _real(gitdir)
    try:
        with open(os.path.join(gitdir, "commondir"), "r",
                  encoding="utf-8", errors="surrogateescape") as stream:
            common = stream.read(4096).strip()
    except FileNotFoundError:
        # A submodule or --separate-git-dir copy has no commondir:
        # its gitdir itself owns the objects. Linked worktrees name their
        # common directory explicitly in commondir.
        common = gitdir
    except OSError as exc:
        return "", "commondir unreadable (%s)" % (exc.strerror or exc)
    if not os.path.isabs(common):
        common = os.path.join(gitdir, common)
    return _real(os.path.join(common, "objects")), ""


def _git_objects(path: str) -> tuple[str, str]:
    """Object directory of the Git copy at `path`, or ('', doubt).

    The `.git` marker is read first: most directories of an inventory are
    not Git copies, and a missing marker is answered with one `lstat`; the
    bare-repository signature is only tried after it.
    """
    marker = os.path.join(path, ".git")
    try:
        mode = os.lstat(marker).st_mode
    except FileNotFoundError:
        if hk._bare_git_copy(path):
            return _real(os.path.join(path, "objects")), ""
        return "", ""
    except OSError as exc:
        return "", "Git marker unreadable (%s)" % (exc.strerror or exc)
    if stat.S_ISDIR(mode):
        return _real(os.path.join(marker, "objects")), ""
    if stat.S_ISREG(mode):
        return _linked_objects(path)
    return "", "Git marker is neither a file nor a directory"


def _inventory_top(path: str) -> tuple[bool, str]:
    """Is `path` a directory the dependency inventory must walk? (yes, why).

    A missing top is absent (radar/trains need not exist), a top that is not
    a directory is skipped, but an unreadable top is a **doubt**: it could
    hide a dependent, so the inventory is incomplete and nothing is removed.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False, ""
    except OSError as exc:
        return False, "unreadable top %s (%s)" % (path, exc.strerror or exc)
    return stat.S_ISDIR(st.st_mode), ""


def _dependency_inventory(cfg, roots, agents, rules, *,
                          budget_s: float | None = None) -> tuple[list[tuple[str, str]], bool, str]:
    """Every Git copy whose alternates could depend on a candidate: (copies, complete, why).

    Perimeter: the work roots, the radar/train trees and the registered
    agents' checkouts **recursively**, including the working tree of every
    Git copy (a dependent can be nested in another checkout, not only in a
    plain container or a bare repository). Only `.git` metadata is skipped
    inside a copy, and a bare repository has no working tree to enter. Build
    output directories are not entered (they are regenerable and never hold a
    clone whose objects a candidate owns); symbolic links are not followed,
    like the rest of the housekeeping. A bound, an unreadable top, directory
    or marker makes the inventory incomplete: the caller then removes
    nothing.
    """
    seen: set[str] = set()
    walked: set[str] = set()
    copies: list[tuple[str, str]] = []
    seconds = DEPENDENCY_BUDGET_S if budget_s is None \
        else max(0.0, min(DEPENDENCY_BUDGET_S, float(budget_s)))
    budget = build_artifacts.Budget(seconds)
    opened = 0

    def add(path: str) -> tuple[str, str]:
        """Add a copy to the inventory: (objects, why); objects '' = no copy.

        `path` is already canonical (the walk never follows a symbolic link),
        so the inventory need not resolve it again for every directory.
        """
        if path in seen:
            return "", ""
        seen.add(path)
        objects, why = _git_objects(path)
        if why:
            return "", why
        if objects:
            copies.append((path, objects))
        return objects, ""

    def walk(top: str) -> str:
        nonlocal opened
        top = _real(top)
        stack = [top]
        try:
            device = os.lstat(top).st_dev
        except OSError as exc:
            return "unreadable root %s (%s)" % (top, exc.strerror or exc)
        while stack:
            current = stack.pop()
            if current in walked:
                continue
            walked.add(current)
            if budget.expired():
                return "time budget of the dependency inventory reached"
            opened += 1
            if opened > DEPENDENCY_LIMIT:
                return "more than %d directories in the dependency inventory" % DEPENDENCY_LIMIT
            objects, why = add(current)
            if why:
                return why
            if objects and hk._bare_git_copy(current):
                # A bare repository has no working tree; its objects are the
                # proof point, there is no checkout under it to search.
                continue
            try:
                with os.scandir(current) as children:
                    entries = sorted(children, key=lambda entry: entry.name)
            except OSError as exc:
                return "unreadable directory %s (%s)" % (current, exc.strerror or exc)
            for entry in entries:
                if entry.name == ".git":
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    return "unreadable entry %s (%s)" % (entry.path, exc.strerror or exc)
                if st.st_dev != device:
                    return "mount point inside %s: %s" % (top, entry.path)
                if entry.name in DEPENDENCY_SKIP:
                    continue
                if any(rule.matches(current, entry.name) for rule in rules):
                    continue
                stack.append(entry.path)
        return ""

    tops: list[str] = []
    for root in roots or ():
        path = hk._expand(root)
        present, why = _inventory_top(path)
        if why:
            return copies, False, why
        if present:
            tops.append(path)
    for cwd in (agents or {}).values():
        if not cwd:
            continue
        path = hk._expand(cwd)
        present, why = _inventory_top(path)
        if why:
            return copies, False, why
        if present:
            tops.append(path)
    for base in (os.path.join(cfg.state_dir, "radar"),
                 os.path.join(cfg.state_dir, "trains")):
        present, why = _inventory_top(base)
        if why:
            return copies, False, why
        if present:
            tops.append(base)
    for top in dict.fromkeys(tops):
        why = walk(top)
        if why:
            return copies, False, why
    return copies, True, ""


def _alternate_paths(info: str, objects: str) -> list[str] | None:
    """Paths of an alternates file, resolved like Git does (relative to objects)."""
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


def _dependents(copies, candidates) -> tuple[dict[str, set[str]], str]:
    """candidate path -> repositories whose alternates point inside its objects.

    A doubt (unreadable alternates file) is returned as the second value: the
    caller then removes nothing, rather than break a clone it could not
    inspect.
    """
    by_objects = {}
    for path in candidates:
        obj = _objects_dir(path)
        if obj:
            by_objects[obj] = path
    out: dict[str, set[str]] = {path: set() for path in candidates}
    for repo, objects in copies:
        info = os.path.join(objects, "info", "alternates")
        try:
            st = os.lstat(info)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return out, "alternates unreadable (%s): %s" % (repo, exc.strerror or exc)
        if not stat.S_ISREG(st.st_mode):
            return out, "alternate marker is not a file: %s" % info
        paths = _alternate_paths(info, objects)
        if paths is None:
            return out, "alternates unreadable: %s" % info
        for value in paths:
            target = _real(value)
            for candidate_obj, candidate in by_objects.items():
                if hk._under(target, candidate_obj):
                    out[candidate].add(repo)
    return out, ""


def _objects_dir(repo: str) -> str:
    """Absolute object directory of a clone, or '' when it cannot be read."""
    objects, _why = _git_objects(repo)
    return objects


def _removal_check(path: str, cfg, rules, allow, roots, agents,
                   budget_s: float | None = None) -> str:
    """Why the clone must stay, right before it is renamed ('' to go).

    The dependency inventory is read again, whole: a bounded or unreadable
    inventory is a doubt, never a proof that no clone depends on the
    candidate.
    """
    in_use, holder_detail = hk.worktree_holders(path, allow=allow)
    if in_use:
        return holder_detail
    saved = hk._git_copy_work(path, path, rules)
    if saved:
        return saved
    if not _objects_dir(path):
        return "candidate object directory unreadable"
    copies, complete, why = _dependency_inventory(cfg, roots, agents, rules,
                                                  budget_s=budget_s)
    if not complete:
        return "dependency inventory incomplete (%s)" % why
    dependents, doubt = _dependents(copies, [path])
    if doubt:
        return doubt
    if dependents.get(path):
        return "alternate dependent: %s" % ", ".join(sorted(dependents[path])[:3])
    return ""


def _remove(path: str, *, cfg, rules, allow, roots, agents, lock_path, journal_path,
            skip_roots, budget_s: float | None = None) -> tuple[str, int, str]:
    """Remove one clone under the host lock and trash journal."""
    if not lock_path or not journal_path:
        return "garde", 0, "neither lock nor housekeeping journal"
    lock = build_artifacts._lock(lock_path)
    if lock is None:
        return "attend", 0, "another pass is deleting on the host"
    try:
        journal = build_artifacts.Journal(journal_path)
        seconds = build_artifacts.PASS_BUDGET_S if budget_s is None \
            else max(0.0, min(build_artifacts.PASS_BUDGET_S, float(budget_s)))
        budget = build_artifacts.Budget(seconds)
        real = _real(path)
        ident = build_artifacts.identity(real)
        parent_id = build_artifacts.identity(os.path.dirname(real))
        avoid = build_artifacts._identities(
            [_real(p) for p in skip_roots if p] + [_real(p) for p in roots if p])
        if ident is None:
            return "garde", 0, "directory unreadable"

        def check() -> str:
            return _removal_check(path, cfg, rules, allow, roots, agents,
                                  budget_s=seconds)

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
         journal_path: str | None = None, skip_roots=(),
         budget_s: float | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """Inventory the ad-hoc clones of a host's work roots: (entries, plan, skipped).

    `agents` maps a registered agent name to its working directory;
    `protected` holds the working directories of this host (never removed
    or entered). Dry run writes nothing and returns the same plan a real
    pass would act on. `budget_s` (#3802) borne l'inventaire profond (walks
    des racines de travail) et les retraits par le temps qui reste au
    passage, réserve déduite par l'appelant ; un inventaire coupé est
    incomplet, donc rien n'est retiré.
    """
    if not roots or not os.path.isdir(cfg.state_dir):
        return [], [], []
    if budget_s is not None and budget_s <= 0:
        return [], [], ["clones: pass budget reached, nothing removed"]
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
    # #3802 B1/cx2 : l'inventaire profond (walks de /home/ameesh/work) est
    # borné par le budget du passage, pas par son seul plafond de 60 s ; il
    # tourne sous le verrou host-pass, donc il ne doit pas manger la réserve.
    seconds = CLONE_BUDGET_S if budget_s is None \
        else max(0.0, min(CLONE_BUDGET_S, float(budget_s)))
    deadline = time.monotonic() + seconds
    inventory, inventory_complete, inventory_why = _clones_under(roots)
    if not inventory:
        if not inventory_complete:
            skipped.append("clones: candidate inventory incomplete (%s)" % inventory_why)
        return [], [], skipped
    if not inventory_complete:
        skipped.append("clones: candidate inventory incomplete (%s); nothing removed"
                       % inventory_why)
    copies: list[tuple[str, str]] = []
    dependencies_complete = True
    dependencies_why = ""
    if inventory_complete:
        copies, dependencies_complete, dependencies_why = _dependency_inventory(
            cfg, roots, agents, rules, budget_s=seconds)
        if not dependencies_complete:
            skipped.append("clones: dependency inventory incomplete (%s); nothing "
                           "removed" % dependencies_why)
    dependents: dict[str, set[str]] = {path: set() for path in inventory}
    alternates_doubt = ""
    if dependencies_complete:
        candidates = []
        for path in inventory:
            if not _objects_dir(path):
                dependencies_complete = False
                dependencies_why = "candidate object directory unreadable: %s" % path
                skipped.append("clones: dependency inventory incomplete (%s); nothing "
                               "removed" % dependencies_why)
                break
            candidates.append(path)
        if dependencies_complete:
            dependents, alternates_doubt = _dependents(copies, candidates)
            if alternates_doubt:
                skipped.append("clones: %s; nothing removed" % alternates_doubt)
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
        rows, missing, why = _attribution(db, path, agents)
        if rows is None:
            plan.append(dict(base, action="garde", detail="%s; kept" % why))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail="%s; kept" % why))
            continue
        if missing:
            detail = "named lot(s) %s unknown; kept" % ", ".join(
                "#%s" % ident for ident in missing[:5])
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
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
        if not inventory_complete:
            detail = "candidate inventory incomplete (%s); kept" % inventory_why
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        if not dependencies_complete:
            detail = "dependency inventory incomplete (%s); kept" % dependencies_why
            plan.append(dict(base, action="garde", detail=detail))
            if not dry_run:
                entries.append(dict(base, action="signaled", detail=detail))
            continue
        in_use, holder_detail = hk.worktree_holders(path, allow=allow)
        if in_use:
            detail = "%s; kept" % holder_detail
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
            lock_path=lock_path, journal_path=journal_path, skip_roots=skip_roots,
            budget_s=max(0.0, deadline - time.monotonic()))
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
