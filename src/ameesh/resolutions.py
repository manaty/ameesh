# SPDX-License-Identifier: AGPL-3.0-only
"""Prepared conflict resolutions between two lots (lot L160d, study L160 § 3.2).

    ameesh work resolve <lot> --with <other>            open the resolution
    ameesh work resolve <lot> --with <other> --done     commit, push, record
    ameesh work resolve <lot> --with <other> --abort    drop it
    ameesh work resolve <lot> [--json]                  resolutions of a lot

When the radar (L160b) finds two lots whose branches merge with a conflict,
one of them, the RESOLVER, prepares the resolution while it still knows its
code, long before either lot merges:

1. `resolve` adds a throwaway worktree of the lot branch (detached, locked,
   under `<git common dir>/ameesh-resolve/<lot>-<other>`) and merges the
   other branch into it, `rerere` off. When the pair was resolved before,
   that merge is replayed in a scratch clone whose empty `rr-cache` is
   primed with that one resolution, and the files `rerere` resolved there are
   copied into the worktree: hunks already resolved come back resolved. The
   agents' repository never gets an `rr-cache` (one would turn `rerere` on
   for their own merges);
2. the agent resolves the remaining conflicts in that worktree (a new file
   is `git add`-ed: `--done` refuses untracked files);
3. `resolve --done` checks that no conflict marker is left, commits the
   merge (trailer `ameesh-resolution: <lot> <other>`), points
   `refs/ameesh/resolutions/<lot>/<other>` at it, pushes it there on
   `origin` (or to the branch `ameesh/resolve/<lot>-<other>` when the host
   refuses refs outside `refs/heads`; never forced over a branch it did
   not push), records it (table `work_resolutions`, migration 0057) and
   removes the worktree, only when clean.

The lot branch never changes: it stays the version WITHOUT the other lot;
the resolution commit R(lot, other) is the version WITH it.

Validity, at each radar pass: in its private clone of the project
(`<state_dir>/radar/<project>.clone`, `git clone --shared`: objects read
from the agents' repository, nothing written to it), the radar replays the
merge of the two current tips with an `rr-cache` holding the pair's own
resolution only (trained once per resolution commit with git's
`rerere-train.sh`, contrib, or a port of the same loop when the script is
not installed, then kept aside: another pair's resolution, or a replaced
one, never resolves this pair). The replay reads `.gitattributes` from the
target's tree (`attr.tree`), like the radar's trial merge and the landing
merge, so a `merge=union` carried by a single branch does not decide it
(#2189). If `rerere` resolves every conflicted path, the pair is
`prepared`; otherwise it is `stale`, and the lot that prepared it is told
(passive mail, L160c). A rebase or a new push
that leaves the conflicting hunks as they were keeps the pair `prepared`:
`rerere` recognises content, not commits.

Resolver: the lot expected to merge second. Until the merge queue (L160e)
gives the order, it is guessed: the lot with the higher priority merges
first; then the one further along (`qa` before `build` before the rest);
then the lot with the smaller change in the conflicting files resolves;
then the newer lot. The radar names the resolver in its mail.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager

from . import migrations, plan_git, storage, work
from .canon import (FETCH_TIMEOUT, GIT_TIMEOUT, NO_LAZY_FETCH, GitError, _git,
                    _git_env)

REF_PREFIX = "refs/ameesh/resolutions/"
#: un `owner/name` GitHub : une option `--repo` peut le porter, mais ce n'est
#: jamais un dossier de travail (#2349).
_GITHUB_KEY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
#: fallback for hosts that refuse refs outside `refs/heads`
BRANCH_PREFIX = "ameesh/resolve/"
TRAILER = "ameesh-resolution"
# v3 keeps both merge directions and retrains the checkout -m preimages that
# v2 (#2136) could cache before #2165 fixed the Python trainer.
TRAINING_CACHE = "ameesh-rr-v3"
# v4 is trained with the target's attributes (`attr.tree`), like
# `radar.trial_merge` and a landing merge: a cache trained from a branch that
# carries `merge=union` alone does not cover the conflicts the target sees, and
# the other way round (#2189). Land and the merge queue keep TRAINING_CACHE.
TRAINING_CACHE_TARGET = "ameesh-rr-v4"
#: worktrees of open resolutions, under the repository's common directory
WORKTREE_DIR = "ameesh-resolve"
#: where git ships `rerere-train.sh`, relative to `git --exec-path` or absolute
RERERE_TRAIN = (
    "../../share/doc/git/contrib/rerere-train.sh",
    "../../share/git-core/contrib/rerere-train.sh",
    "../../share/git/contrib/rerere-train.sh",
    "/usr/share/doc/git/contrib/rerere-train.sh",
    "/usr/share/git-core/contrib/rerere-train.sh",
    "/usr/local/share/git-core/contrib/rerere-train.sh",
    "/opt/homebrew/share/git-core/contrib/rerere-train.sh",
)
_MARKER_RE = re.compile(rb"^(<{7} |>{7} |={7}$|\|{7} )", re.M)
#: how far along a lot is, to guess which of two lots merges first
_STATE_RANK = {"qa": 2, "build": 1}


class ResolveError(RuntimeError):
    """A resolution that cannot be opened or recorded: actionable message."""


class CloneBusy(GitError):
    """The radar clone's worktree is held by another operation (#2428).

    Nothing was read nor written: the caller defers to its next pass. A
    stacked trial that finds the clone busy is not an ejection, and an
    operation it could not run is never cached as a result."""


#: how long an operation waits for the clone's worktree before deferring.
#: A landing gate can hold it for a while; a radar pass or a turn that does
#: not get it retries at its next pass (`CloneBusy`), never ejects a lot.
CLONE_LOCK_WAIT_S = float(os.environ.get("AMEESH_CLONE_LOCK_WAIT", "60"))


def ref_name(lot: int, other: int) -> str:
    return "%s%d/%d" % (REF_PREFIX, lot, other)


def branch_name(lot: int, other: int) -> str:
    return "%s%d-%d" % (BRANCH_PREFIX, lot, other)


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def _run(cwd: str, *args: str, timeout: float = GIT_TIMEOUT,
         stdin: bytes | None = None,
         env_extra: dict | None = None) -> subprocess.CompletedProcess:
    return _git(cwd, *args, stdin=stdin, timeout=timeout, env_extra=env_extra)


def _text(proc) -> str:
    return proc.stdout.decode("utf-8", "replace").strip()


def _err(proc) -> str:
    return (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()


def _check(proc, what: str) -> str:
    if proc.returncode != 0:
        raise GitError("git %s: %s" % (what, _err(proc)))
    return _text(proc)


def _paths(proc) -> list[str]:
    return sorted({p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p})


def unmerged(cwd: str) -> list[str]:
    return _paths(_run(cwd, "diff", "--name-only", "--diff-filter=U", "-z"))


def _merge_rr(cwd: str) -> bool:
    """`test -s "$GIT_DIR/MERGE_RR"`: a conflicted merge left something to learn."""
    path = _text(_run(cwd, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_RR"))
    return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0


def _restore_rr(cache: str, stash: str) -> None:
    """Puts the hidden `rr-cache` back under its name: the entries the trial
    merge recorded win over the stashed ones (same conflict, fresher record),
    the others are kept, and the stash goes away."""
    os.makedirs(cache, exist_ok=True)
    for name in os.listdir(stash):
        src, dst = os.path.join(stash, name), os.path.join(cache, name)
        if os.path.exists(dst):
            shutil.rmtree(src, ignore_errors=True)
        else:
            os.rename(src, dst)
    shutil.rmtree(stash, ignore_errors=True)


def rerere_remaining(cwd: str) -> list[str]:
    """Conflicted paths that `rerere` did not resolve."""
    proc = _run(cwd, "-c", "rerere.enabled=true", "rerere", "remaining")
    return sorted({p for p in _text(proc).splitlines() if p}) if proc.returncode == 0 \
        else unmerged(cwd)


def exclude_generated(cwd: str, files, generated) -> None:
    """Remove generated conflicts from the index before invoking rerere.

    The target version is only a placeholder; land regenerates it before
    committing. For a deletion on the target side there is no placeholder.
    """
    for path in sorted(set(files) & set(generated)):
        ours = _run(cwd, "checkout", "--ours", "--", path)
        if ours.returncode == 0:
            _check(_run(cwd, "add", "--", path), "add")
        else:
            _check(_run(cwd, "rm", "-f", "--", path), "rm")


def with_markers(cwd: str, paths) -> list[str]:
    """Those of `paths` that still hold a conflict marker."""
    out = []
    for path in paths:
        try:
            with open(os.path.join(cwd, path), "rb") as fh:
                if _MARKER_RE.search(fh.read()):
                    out.append(path)
        except (IsADirectoryError, FileNotFoundError):
            continue
    return out


def rerere_train_script() -> str | None:
    """git's `contrib/rerere-train.sh`, if installed."""
    exec_path = ""
    try:
        exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True,
                                   timeout=GIT_TIMEOUT, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    for candidate in RERERE_TRAIN:
        path = candidate if os.path.isabs(candidate) or not exec_path \
            else os.path.normpath(os.path.join(exec_path, candidate))
        if os.path.isfile(path):
            return path
    return None


def _parents(cwd: str, commit: str) -> list[str]:
    return _text(_run(cwd, "rev-parse", commit + "^@")).split()


def _attr_args(attributes_tree: str | None) -> list[str]:
    """The git arguments that read `.gitattributes` from `attributes_tree`
    instead of the checked-out tree (`git merge-tree` and `git merge` honour
    the same `attr.tree`, git >= 2.40)."""
    return ["-c", "attr.tree=%s" % attributes_tree] if attributes_tree else []


def _attr_env(env: dict, attributes_tree: str | None) -> dict:
    """`_attr_args` for a child process (rerere-train.sh runs its own git)."""
    if attributes_tree:
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="attr.tree",
                   GIT_CONFIG_VALUE_0=attributes_tree)
    return env


def _learn(cwd: str, commit: str, first: str, others, *,
           attributes_tree: str | None = None) -> bool:
    """Plays the merge of `others` into `first` and records, in `rr-cache`, the
    resolution the tree of `commit` carries when that merge conflicts. Returns
    whether it learnt from a conflict (a clean merge has none to record).
    Leaves `cwd` clean on a detached `first`; a failure is cleaned up by the
    caller, which also restores the commit `cwd` was on.

    The trial merge must render the conflicts itself: with the cache in place
    it would replay a recorded resolution into the files, and `rerere` would
    record that stale content as the new preimage (#2165). The cache is put
    back before `forget`, which drops the pair's entries (`--overwrite`) while
    the other pairs' entries survive."""
    cache = _check(_run(cwd, "rev-parse", "--path-format=absolute", "--git-path",
                        "rr-cache"), "rev-parse")
    _check(_run(cwd, "checkout", "-q", "-f", "--detach", first), "checkout")
    stash = "%s.learn-%d" % (cache, os.getpid())
    hidden = os.path.isdir(cache) and not os.path.isdir(stash)
    if hidden:
        os.rename(cache, stash)
    try:
        merge = _run(cwd, "-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=false",
                     *_attr_args(attributes_tree),
                     "merge", "-q", "--no-ff", "--no-commit", *others)
    finally:
        if hidden and os.path.isdir(stash):
            _restore_rr(cache, stash)
    if merge.returncode == 0:
        _run(cwd, "reset", "-q", "--hard")
        return False
    # `forget` exits 0 even when it did nothing (rerere off): the MERGE_RR
    # gate below tells whether a conflict is left to learn from, exactly as
    # `test -s "$GIT_DIR/MERGE_RR"` does in rerere-train.sh (#2165).
    _run(cwd, "-c", "rerere.enabled=true", "rerere", "forget", ".")
    if not _merge_rr(cwd):
        _run(cwd, "reset", "-q", "--hard")
        return False
    # the preimage is the conflict the merge just rendered: never re-render it
    # with `checkout -m`, whose markers ("ours"/"theirs") differ from the
    # merge's ("HEAD"/branch) and miss at replay (#2165).
    _check(_run(cwd, "-c", "rerere.enabled=true", "rerere"), "rerere")
    _check(_run(cwd, "checkout", commit, "--", "."), "checkout")
    _check(_run(cwd, "-c", "rerere.enabled=true", "rerere"), "rerere")
    _run(cwd, "reset", "-q", "--hard")
    return True


def train(cwd: str, commits, *, script: str | None = "auto",
          attributes_tree: str | None = None) -> list[str]:
    """Primes the `rr-cache` of the repository of worktree `cwd` from the
    merge commits `commits` (`--overwrite`: a replaced resolution wins),
    with `rerere-train.sh`, or the same loop in Python when the script is
    not installed (`script=None` forces the port). Leaves `cwd` clean, on
    the commit it was on. Returns the commits it learnt from.

    #2136 : a merge is not symmetric — a `.gitattributes` a merge brings in
    only drives that merge once it is checked out (so `merge=union` applies
    on one side only), so the conflicted paths can differ in each direction.
    The script and the loop only learn the first-parent direction; the other
    is learnt here from the same commit, so a resolution prepared in one
    direction replays in the landing direction of the queue.

    #2165 : the Python port hides the cache during the trial merge, so
    `--overwrite` really replaces a resolution the merge would have replayed
    from the cache; it records the preimage the merge rendered, never one
    re-rendered by `checkout -m`.

    #2189 : `attributes_tree` makes the trial merges read `.gitattributes`
    from the target, exactly like the replay and `radar.trial_merge`, so a
    conflict that only the target's attributes create is learnt too. The
    first-parent direction of rerere-train.sh gets it through the git
    environment (`GIT_CONFIG_*`), its reverse through `_learn`."""
    commits = [c for c in commits if c]
    if not commits:
        return []
    cache = _check(_run(cwd, "rev-parse", "--path-format=absolute", "--git-path",
                        "rr-cache"), "rev-parse")
    os.makedirs(cache, exist_ok=True)
    if script == "auto":
        script = rerere_train_script()
    head = _check(_run(cwd, "rev-parse", "HEAD"), "rev-parse")
    learnt = []
    try:
        if script:
            env = _attr_env(_git_env(), attributes_tree)
            proc = subprocess.run(["sh", script, "--overwrite", "--no-walk", *commits],
                                  cwd=cwd, capture_output=True, env=env,
                                  timeout=FETCH_TIMEOUT, check=False)
            out = proc.stdout.decode("utf-8", "replace")
            found = re.findall(r"^Learning from ([0-9a-f]+)", out, re.M)
            learnt = [c for c in commits if any(c.startswith(short) for short in found)]
        else:
            for commit in commits:
                parents = _parents(cwd, commit)
                if len(parents) < 2:
                    continue
                if _learn(cwd, commit, parents[0], parents[1:],
                          attributes_tree=attributes_tree):
                    learnt.append(commit)
        for commit in commits:
            parents = _parents(cwd, commit)
            if len(parents) == 2 and _learn(cwd, commit, parents[1], [parents[0]],
                                            attributes_tree=attributes_tree):
                learnt.append(commit)
    finally:
        _run(cwd, "reset", "-q", "--hard")
        _run(cwd, "checkout", "-q", "-f", "--detach", head)
    seen = set(learnt)
    return [c for c in commits if c in seen]


# --------------------------------------------------------------------------
# resolver
# --------------------------------------------------------------------------

def _priority(row: dict) -> int:
    return int(row.get("priority") or 2)


def change_size(repo: str, base: str, tip: str, files) -> int:
    """Lines added and removed between `base` and `tip` in `files`."""
    proc = _run(repo, "diff", "--numstat", "--no-renames", base, tip, "--", *files)
    total = 0
    for line in _text(proc).splitlines() if proc.returncode == 0 else ():
        added, removed = (line.split("\t") + ["", ""])[:2]
        total += (int(added) if added.isdigit() else 0) + \
            (int(removed) if removed.isdigit() else 0)
    return total


def choose_resolver(a: dict, b: dict, files, *, repo: str | None = None,
                    sizes: dict | None = None) -> dict:
    """The lot of the pair (`a`, `b`: `{work_item, priority?, state?, base,
    tip}`) that prepares the resolution: the one expected to merge second.
    Returns `{lot, reason}`."""
    pa, pb = _priority(a), _priority(b)
    if pa != pb:
        return {"lot": (b if pa < pb else a)["work_item"], "reason": "priority"}
    ra, rb = _STATE_RANK.get(a.get("state") or "", 0), _STATE_RANK.get(b.get("state") or "", 0)
    if ra != rb:
        return {"lot": (b if ra > rb else a)["work_item"], "reason": "state"}
    if repo and files:
        sizes = sizes if sizes is not None else {}
        for one in (a, b):
            key = (one["base"], one["tip"], tuple(files))
            if key not in sizes:
                sizes[key] = change_size(repo, one["base"], one["tip"], files)
        sa = sizes[(a["base"], a["tip"], tuple(files))]
        sb = sizes[(b["base"], b["tip"], tuple(files))]
        if sa != sb:
            return {"lot": (a if sa < sb else b)["work_item"], "reason": "smaller change"}
    return {"lot": max(a["work_item"], b["work_item"]), "reason": "newer lot"}


# --------------------------------------------------------------------------
# ameesh work resolve: the agent's side
# --------------------------------------------------------------------------

def repo_root(repo: str | None) -> str:
    """La racine du dépôt désigné par `--repo` (ou le dossier courant).

    `--repo` désigne un **dossier** de travail ; un `owner/name` GitHub n'est
    pas un chemin. Il est accepté quand il nomme l'`origin` du dossier courant
    (on travaille alors dans ce dépôt), sinon refusé avec un message
    explicite : un dossier absent faisait répondre « git not found » (#2349).
    """
    wanted = str(repo or "").strip()
    if wanted:
        path = os.path.expanduser(wanted)
        if os.path.isdir(path):
            wanted = path
        else:
            origin = migrations.origin_repo(os.getcwd())
            if _GITHUB_KEY.fullmatch(wanted) and migrations.repo_key(wanted) == origin:
                wanted = os.getcwd()
            else:
                raise ResolveError(
                    "--repo %s is not a local directory: pass the checkout's path, or run "
                    "from its repository (a GitHub owner/name is accepted only for the "
                    "current checkout, whose origin is %s)" % (wanted, origin or "none"))
    else:
        wanted = os.getcwd()
    try:
        root = plan_git._out(wanted, "rev-parse", "--show-toplevel")
    except GitError as exc:
        raise ResolveError("cannot read the repository at %s: %s" % (wanted, exc)) from exc
    if not root:
        raise ResolveError("%s is not inside a git repository: pass --repo" % wanted)
    return root


def worktree_path(repo: str, lot: int, other: int) -> str:
    common = _check(_run(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                    "rev-parse")
    return os.path.join(common, WORKTREE_DIR, "%d-%d" % (lot, other))


def _meta_path(path: str) -> str:
    return path + ".json"


def _branch_tip(repo: str, branch: str, *, fetch: bool) -> str | None:
    """The tip of a lot branch: local, else `origin/<branch>`, refreshed
    from `origin` first when the branch is not local (another host)."""
    if fetch and not plan_git._commit(repo, "refs/heads/" + branch):
        _run(repo, "fetch", "--quiet", "origin",
             "+refs/heads/%s:refs/remotes/origin/%s" % (branch, branch), timeout=FETCH_TIMEOUT)
    return plan_git._tip(repo, branch)


def pair(db, lot: int, other: int, repo: str, *, fetch: bool = True) -> dict:
    """Both lots, their branches and tips; ResolveError when they cannot be
    merged together."""
    if lot == other:
        raise ResolveError("a lot does not conflict with itself")
    rows = {}
    for n in (lot, other):
        row = work.get(db, n)
        if row is None:
            raise ResolveError("lot #%d not found" % n)
        if not row.get("branch"):
            raise ResolveError("lot #%d has no branch (ameesh work assign %d <agent> "
                               "--branch …)" % (n, n))
        rows[n] = row
    default = None
    targets = {}
    for n, row in rows.items():
        if not row.get("branch_target") and default is None:
            default = plan_git.default_target(repo)
        targets[n] = row.get("branch_target") or default
    # #1802 : `main` et `origin/main` (le défaut du dépôt) désignent la même
    # branche : comparer les formes courtes, pas les libellés enregistrés.
    if plan_git.short_branch(targets[lot]) != plan_git.short_branch(targets[other]):
        raise ResolveError("lots #%d and #%d do not share a target (%s, %s): their branches "
                           "never merge together" % (lot, other, targets[lot], targets[other]))
    target = default or targets[lot]
    # #2189 : the landing merge and the radar's trial merge read the target's
    # `.gitattributes`; preparation and replay must use the same tree, else a
    # resolution is prepared against attributes no merge will use.
    target_head = None
    if target:
        try:
            from .radar import target_commit
            target_head, _ref, _branch, _remote = target_commit(repo, target)
        except (GitError, OSError):
            target_head = None
    tips = {}
    for n, row in rows.items():
        tips[n] = _branch_tip(repo, row["branch"], fetch=fetch and n == other)
        if not tips[n]:
            raise ResolveError("branch %s of lot #%d is neither local nor on origin"
                               % (row["branch"], n))
    return {"lot": rows[lot], "other": rows[other], "target": target,
            "target_head": target_head,
            "lot_tip": tips[lot], "other_tip": tips[other]}


def _recorded(db, lot: int, other: int) -> list[dict]:
    """Recorded resolutions of the pair, either way round, newest first."""
    return [r for r in storage.of(db).work.resolutions([lot])
            if {int(r["work_item_id"]), int(r["other_item_id"])} == {lot, other}]


def _ensure_commit(repo: str, row: dict) -> bool:
    """Makes the recorded resolution commit available locally (fetched from
    `origin` when it was prepared on another host)."""
    sha = row["commit_sha"]
    # #1749 : les deux sondes de présence restent locales ; le fetch explicite
    # ci-dessous est le seul pas réseau.
    if _run(repo, "cat-file", "-e", sha + "^{commit}",
            env_extra=NO_LAZY_FETCH).returncode == 0:
        return True
    if row.get("ref"):
        source = row["ref"] if row["ref"].startswith("refs/") else "refs/heads/" + row["ref"]
        _run(repo, "fetch", "--quiet", "origin", source, timeout=FETCH_TIMEOUT)
    return _run(repo, "cat-file", "-e", sha + "^{commit}",
                env_extra=NO_LAZY_FETCH).returncode == 0


def _status(path: str) -> list[str]:
    """`git status --porcelain` of the worktree, untracked and ignored files
    included (`!!`): removing the worktree would lose them too."""
    proc = _run(path, "status", "--porcelain", "-z", "--untracked-files=all", "--ignored")
    _check(proc, "status")
    return [e for e in proc.stdout.decode("utf-8", "replace").split("\0") if e[2:3] == " "]


def _admin_cwd(repo: str, path: str) -> str:
    """A cwd that survives the removal of `path` (#2142). Quand `--done` est
    lancé depuis le worktree de résolution, `repo` *est* `path` : le
    `git worktree prune` qui suit la suppression tournait alors dans un dossier
    disparu et git répondait `git not found`, après l'enregistrement de la
    résolution. On administre depuis le worktree principal, qui survit."""
    if os.path.realpath(repo) != os.path.realpath(path):
        return repo
    proc = _run(repo, "worktree", "list", "--porcelain")
    if proc.returncode == 0:
        for line in _text(proc).splitlines():
            if line.startswith("worktree ") and line[len("worktree "):].strip():
                main = line[len("worktree "):].strip()
                if os.path.isdir(main) and os.path.realpath(main) != os.path.realpath(path):
                    return main
                break
    parent = os.path.dirname(path)
    return parent if os.path.isdir(parent) else repo


def _remove_worktree(repo: str, path: str, *, force: bool = True) -> None:
    """Removes the worktree and its metadata. `force=False` (after `--done`):
    only a clean worktree, else ResolveError and nothing removed."""
    if not force and os.path.isdir(path):
        dirty = _status(path)
        if dirty:
            raise ResolveError("worktree %s not removed, it holds changes: %s; keep them, "
                               "or drop the worktree with: git worktree remove -f -f %s"
                               % (path, ", ".join(e[3:] for e in dirty[:10]), path))
    admin = _admin_cwd(repo, path)
    _run(admin, "worktree", "unlock", path)
    if not force:
        # no --force, no rmtree: git refuses a worktree that changed since the check
        proc = _run(admin, "worktree", "remove", path)
        if proc.returncode != 0 and os.path.isdir(path):
            _run(admin, "worktree", "lock", "--reason", "ameesh work resolve", path)
            raise ResolveError("worktree %s not removed: %s; drop it with: "
                               "git worktree remove -f -f %s" % (path, _err(proc), path))
    else:
        _run(admin, "worktree", "remove", "--force", path)
        shutil.rmtree(path, ignore_errors=True)
    _run(admin, "worktree", "prune")
    try:
        os.unlink(_meta_path(path))
    except FileNotFoundError:
        pass


def _read_meta(path: str) -> dict | None:
    try:
        with open(_meta_path(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and os.path.isdir(path) else None


def _current_resolution(db, lot: int, other: int, lot_tip: str,
                        other_tip: str) -> dict | None:
    """La résolution enregistrée de la paire qui couvre les deux têtes
    courantes, dans un sens ou dans l'autre (`prepared` du radar), la plus
    récente d'abord."""
    for row in _recorded(db, lot, other):
        mine, theirs = ((row["lot_tip"], row["other_tip"])
                        if int(row["work_item_id"]) == lot
                        else (row["other_tip"], row["lot_tip"]))
        if mine == lot_tip and theirs == other_tip:
            return row
    return None


def open_resolution(db, lot: int, other: int, *, repo: str | None = None) -> dict:
    """Opens (or shows, if already open) the resolution of `lot` with
    `other`. Returns `{state: open|prepared|clean, path, files, replayed,
    remaining}`."""
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    meta = _read_meta(path)
    if meta is not None:
        remaining = unmerged(path)
        return dict(meta, state="open", path=path, remaining=remaining,
                    markers=with_markers(path, meta["files"]), already=True)
    if os.path.exists(path):  # never removed without being asked: it may hold work
        raise ResolveError("%s exists without readable metadata (%s): look at it, then "
                           "ameesh work resolve %d --with %d --abort to drop it"
                           % (path, _meta_path(path), lot, other))
    ctx = pair(db, lot, other, repo)
    row = _current_resolution(db, lot, other, ctx["lot_tip"], ctx["other_tip"])
    if row is not None:
        # #2142 : la paire porte déjà une résolution à jour : ne pas rouvrir un
        # worktree pour la même chose. L'état est `prepared` (le radar la
        # rejoue), pas `open` avec un index non fusionné.
        return {"state": "prepared", "lot": lot, "other": other, "path": None,
                "commit": row["commit_sha"], "ref": row.get("ref"),
                "files": list(row.get("files") or []),
                "lot_tip": ctx["lot_tip"], "other_tip": ctx["other_tip"],
                "target": ctx["target"], "replayed": [], "remaining": [], "markers": []}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _check(_run(repo, "worktree", "add", "--quiet", "--detach", path, ctx["lot_tip"]),
           "worktree add")
    try:
        _run(repo, "worktree", "lock", "--reason",
             "ameesh work resolve %d --with %d" % (lot, other), path)
        # rerere off: neither the agents' rr-cache nor rerere.autoUpdate decide
        merge = _run(path, "-c", "rerere.enabled=false", "-c", "merge.conflictStyle=zdiff3",
                     *_attr_args(ctx.get("target_head")),
                     "merge", "--no-ff", "--no-commit", "--quiet", ctx["other_tip"])
        files = unmerged(path)
        if merge.returncode == 0 or not files:
            _remove_worktree(repo, path)
            if merge.returncode != 0:
                raise ResolveError("git merge %s into %s failed: %s"
                                   % (ctx["other"]["branch"], ctx["lot"]["branch"],
                                      _err(merge)))
            return {"state": "clean", "lot": lot, "other": other, "path": None, "files": [],
                    "lot_tip": ctx["lot_tip"], "other_tip": ctx["other_tip"]}
        primed = [r["commit_sha"] for r in _recorded(db, lot, other)
                  if _ensure_commit(repo, r)][:1]
        replayed = _replay_into(repo, path, primed[0], ctx["lot_tip"], ctx["other_tip"],
                                files, attributes_tree=ctx.get("target_head")) \
            if primed else []
        meta = {"lot": lot, "other": other, "lot_branch": ctx["lot"]["branch"],
                "other_branch": ctx["other"]["branch"], "target": ctx["target"],
                "target_head": ctx.get("target_head"),
                "lot_tip": ctx["lot_tip"], "other_tip": ctx["other_tip"], "files": files,
                "replayed": replayed, "primed": primed}
        with open(_meta_path(path), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
    except BaseException:
        if _read_meta(path) is None:
            _remove_worktree(repo, path)
        raise
    return dict(meta, state="open", path=path, remaining=unmerged(path),
                markers=with_markers(path, files), already=False)


def _replay_into(repo: str, path: str, commit: str, lot_tip: str, other_tip: str,
                 files, *, attributes_tree: str | None = None) -> list[str]:
    """Replays the merge in a scratch clone primed with `commit` only, and
    copies into the worktree `path` the files `rerere` resolved there (left
    unmerged in its index, for the agent to check). Returns them.
    `attributes_tree`: the target's, as `open_resolution` merges (#2189)."""
    scratch = tempfile.mkdtemp(prefix="ameesh-resolve-")
    try:
        replayer = Replayer(repo, os.path.join(scratch, "clone"))
        replayer.ensure()
        if replayer.prime([commit], attributes_tree=attributes_tree):
            return []
        resolved = []

        def export(clone: str, paths) -> None:
            for f in paths:
                if f in files and os.path.isfile(os.path.join(clone, f)):
                    shutil.copyfile(os.path.join(clone, f), os.path.join(path, f))
                    resolved.append(f)
        replayer.replay(lot_tip, other_tip, commit, attributes_tree=attributes_tree,
                        export=export)
        return sorted(resolved)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _merged_paths(path: str, meta: dict) -> set[str]:
    """Staged paths that differ from both the lot tip and the other tip: the
    content the merge or the agent made, where a marker may hide."""
    def changed(rev: str) -> set[str]:
        return set(_paths(_run(path, "diff", "--cached", "--name-only", "--no-renames",
                               "-z", rev)))
    return changed(meta["lot_tip"]) & changed(meta["other_tip"])


def _message(meta: dict, note: str) -> str:
    files = meta["files"]
    return ("Resolution: #%d with #%d\n\n"
            "Merge of %s (%s) into %s (%s), prepared ahead of the merge (L160d).\n"
            "Conflicts: %s.\n%s\n%s: %d %d\n"
            % (meta["lot"], meta["other"], meta["other_branch"], meta["other_tip"][:12],
               meta["lot_branch"], meta["lot_tip"][:12],
               ", ".join(files[:20]) + (" …" if len(files) > 20 else ""),
               "\n%s\n" % note.strip() if note and note.strip() else "",
               TRAILER, meta["lot"], meta["other"]))


def push(repo: str, sha: str, lot: int, other: int, *,
         previous: str | None = None) -> tuple[str | None, str | None]:
    """Pushes the resolution commit to `origin`, under its dedicated ref
    (forced: one ref per pair, written by L160d only), else as a branch,
    never forced over a branch L160d did not push: `--force-with-lease` on
    `previous`, the commit recorded there for the pair (none: the branch
    must not exist). Returns (ref pushed, error)."""
    if not plan_git._out(repo, "remote", "get-url", "origin"):
        return None, "no origin remote"
    error = None
    branch = "refs/heads/" + branch_name(lot, other)
    for ref, force in ((ref_name(lot, other), "--force"),
                       (branch, "--force-with-lease=%s:%s" % (branch, previous or ""))):
        proc = _run(repo, "push", "--quiet", force, "--no-verify", "origin",
                    "%s:%s" % (sha, ref), timeout=FETCH_TIMEOUT)
        if proc.returncode == 0:
            return ref, None
        error = _err(proc).splitlines()[-1:] or ["git push failed"]
        error = error[0]
    return None, error


def _prepared_out(row: dict, lot: int, other: int, *, already: bool) -> dict:
    """Sortie d'un `--done` dont le commit est déjà enregistré : seul le
    nettoyage restait (#2142)."""
    return {"lot": lot, "other": other, "commit": row["commit_sha"], "ref": row.get("ref"),
            "push_error": None, "local_ref": ref_name(lot, other),
            "files": list(row.get("files") or []), "state": "prepared", "already": already}


def finish_resolution(db, lot: int, other: int, *, repo: str | None = None,
                      actor: str = "", note: str = "", push_fn=push) -> dict:
    """Commits the resolution open in its worktree, points the dedicated ref
    at it, pushes it, records it, and removes the worktree. Idempotent : si le
    commit est déjà enregistré (nettoyage manqué d'un `--done` précédent), elle
    ne repousse ni ne réenregistre, elle finit le nettoyage et rend l'état
    `prepared` (#2142)."""
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    meta = _read_meta(path)
    if meta is None:
        row = next(iter(_recorded(db, lot, other)), None)
        if row is not None and not os.path.isdir(path) and _ensure_commit(repo, row):
            # le worktree est déjà retiré et la résolution enregistrée : le
            # `.json` de méta (et l'entrée git) peuvent rester d'un nettoyage
            # interrompu ; on les enlève au lieu d'exiger une nouvelle passe.
            _remove_worktree(repo, path)
            return _prepared_out(row, lot, other, already=True)
        if os.path.exists(path):
            raise ResolveError("%s exists without readable metadata: look at it, then "
                               "ameesh work resolve %d --with %d --abort"
                               % (path, lot, other))
        raise ResolveError("no resolution of #%d with #%d is open here: "
                           "ameesh work resolve %d --with %d" % (lot, other, lot, other))
    merging = _run(path, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    if merging:
        left = with_markers(path, meta["files"])
        if left:
            raise ResolveError("conflict markers left in %s (%s)" % (", ".join(left), path))
        untracked = [e[3:] for e in _status(path) if e.startswith("??")]
        if untracked:
            raise ResolveError("untracked files in %s: %s; git add them if the resolution "
                               "needs them, else delete them, or drop the worktree with: "
                               "git worktree remove -f -f %s"
                               % (path, ", ".join(untracked[:10]), path))
        ignored = [e[3:] for e in _status(path) if e.startswith("!!")]
        if ignored:
            raise ResolveError("ignored files in %s: %s; removing the worktree would lose "
                               "them: git add -f them if the resolution needs them, else "
                               "move or delete them, or drop the worktree with: "
                               "git worktree remove -f -f %s"
                               % (path, ", ".join(ignored[:10]), path))
        _check(_run(path, "add", "-u"), "add")
        present = [f for f in meta["files"] if os.path.lexists(os.path.join(path, f))]
        if present:
            _check(_run(path, "add", "--", *present), "add")
        still = unmerged(path)
        if still:
            raise ResolveError("still unmerged: %s (%s)" % (", ".join(still), path))
        # a conflicted file renamed then `git add -A`-ed is not in meta["files"]:
        # check every staged path that is neither side's version
        left = with_markers(path, sorted(set(meta["files"]) | _merged_paths(path, meta)))
        if left:
            raise ResolveError("conflict markers left in %s (%s)" % (", ".join(left), path))
        _check(_run(path, "-c", "rerere.enabled=false", "commit", "--quiet", "--no-verify",
                    "-F", "-", stdin=_message(meta, note).encode()), "commit")
    else:
        dirty = _status(path)
        if dirty:  # a resumed --done: the commit is made, later changes are not in it
            raise ResolveError("changes in %s since the resolution commit: %s; they would "
                               "be lost: undo them, or --abort and start again"
                               % (path, ", ".join(e[3:] for e in dirty[:10])))
    sha = _check(_run(path, "rev-parse", "HEAD"), "rev-parse")
    parents = _text(_run(path, "rev-parse", sha + "^@")).split()
    if parents != [meta["lot_tip"], meta["other_tip"]]:
        raise ResolveError("HEAD of %s is not the merge of %s and %s: ameesh work resolve "
                           "%d --with %d --abort, then start again"
                           % (path, meta["lot_tip"][:12], meta["other_tip"][:12], lot, other))
    if _status(path):
        raise ResolveError("%s is not clean after the resolution commit: %s"
                           % (path, ", ".join(e[3:] for e in _status(path)[:10])))
    row = next(iter(_recorded(db, lot, other)), None)
    if row is not None and row["commit_sha"] == sha:
        # déjà poussé et enregistré par une passe précédente : ne pas repousser
        # ni réenregistrer, finir le nettoyage et rendre l'état `prepared`
        # (#2142, `--done` idempotent).
        _remove_worktree(repo, path, force=False)
        return _prepared_out(row, lot, other, already=True)
    _check(_run(repo, "update-ref", ref_name(lot, other), sha), "update-ref")
    branch = "refs/heads/" + branch_name(lot, other)
    previous = next((r["commit_sha"] for r in _recorded(db, lot, other)
                     if int(r["work_item_id"]) == lot and r.get("ref") == branch), None)
    pushed, error = push_fn(repo, sha, lot, other, previous=previous)
    row = storage.of(db).work.record_resolution(
        lot, other, commit=sha, lot_tip=meta["lot_tip"], other_tip=meta["other_tip"],
        ref=pushed, files=meta["files"],
        note="resolution prepared with #%d: %s (%s)" % (other, sha[:12],
                                                        pushed or "local only"),
        actor=actor)
    if row is None:
        raise ResolveError("lot #%d or #%d no longer exists: nothing recorded (commit %s "
                           "kept in %s)" % (lot, other, sha[:12], path))
    _remove_worktree(repo, path, force=False)
    return {"lot": lot, "other": other, "commit": sha, "ref": pushed, "push_error": error,
            "local_ref": ref_name(lot, other), "files": meta["files"],
            "state": "prepared", "already": False}


def abort_resolution(lot: int, other: int, *, repo: str | None = None) -> bool:
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    existed = os.path.exists(path) or os.path.exists(_meta_path(path))
    _remove_worktree(repo, path)
    return existed


def of_lot(db, lot: int) -> list[dict]:
    """Recorded resolutions where `lot` is either side."""
    return storage.of(db).work.resolutions([lot])


def of_lot_or_error(db, lot: int) -> tuple[list[dict], str | None]:
    """`of_lot`, and the error instead of raising (`work show` on a database
    without migration 0057)."""
    try:
        return of_lot(db, lot), None
    except Exception as exc:  # noqa: BLE001 - e.g. table work_resolutions missing
        return [], "%s: %s" % (type(exc).__name__, str(exc).splitlines()[0] if str(exc)
                               else "")


def show_lines(rows: list[dict], lot: int) -> list[str]:
    import time
    out = []
    for r in rows:
        mine = int(r["work_item_id"]) == lot
        out.append("%s #%d: %s (%s), by %s on %s UTC%s" % (
            "resolution with" if mine else "resolved by",
            int(r["other_item_id"] if mine else r["work_item_id"]), r["commit_sha"][:12],
            r.get("ref") or "local only", r.get("created_by") or "?",
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(r.get("created_ts") or 0)),
            "; conflicts: %s" % ", ".join(r["files"][:6]) if r.get("files") else ""))
    return out


# --------------------------------------------------------------------------
# radar side: replay in the private clone
# --------------------------------------------------------------------------

class _CloneLock:
    """An exclusive flock on a clone's worktree, reentrant in one process.

    The radar clone is shared by every operation of the host: the executor's
    radar pass, a `work land` or `work queue add` started by a turn, the fire
    passage. `flock` carries the exclusion across those processes; the
    `RLock` serialises the threads of one process, which flock alone does not
    do, and makes a nested operation (a trial that primes resolutions, a
    landing that merges) cheap. The lock file sits beside the clone, never
    inside it: `Replayer.ensure()` may remove the clone."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.mutex = threading.RLock()
        self.depth = 0
        self.handle = None

    def acquire(self, wait: float) -> None:
        deadline = time.monotonic() + max(0.0, wait)
        if not self.mutex.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise CloneBusy("the radar clone's worktree is busy (%s)" % self.path)
        if self.depth:
            self.depth += 1
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            handle = open(self.path, "a+")
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        handle.close()
                        raise CloneBusy("the radar clone's worktree is busy (%s)"
                                        % self.path) from None
                    time.sleep(min(0.1, remaining))
            self.handle, self.depth = handle, 1
        except BaseException:
            self.mutex.release()
            raise

    def release(self) -> None:
        self.depth -= 1
        if not self.depth:
            handle, self.handle = self.handle, None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        self.mutex.release()


#: one lock per clone path, shared by every Replayer of one process
_CLONE_LOCKS: dict = {}
_CLONE_LOCKS_MUTEX = threading.Lock()


def _clone_lock(path: str) -> _CloneLock:
    key = os.path.realpath(path)
    with _CLONE_LOCKS_MUTEX:
        lock = _CLONE_LOCKS.get(key)
        if lock is None:
            lock = _CLONE_LOCKS[key] = _CloneLock(key + ".worktree.lock")
        return lock


class Replayer:
    """The radar's private clone of a project. Objects come from the agents'
    repository (`--shared`); merges are replayed in the clone's worktree.
    Each resolution commit gets its own `rr-cache`, trained once and kept
    under `.git/ameesh-rr-v3/<commit>` (branches' attributes) or
    `.git/ameesh-rr-v4/<commit>` (the target's, `attributes_tree`): a replay
    sees the pair's resolution only, never another pair's nor a replaced one."""

    def __init__(self, repo: str, path: str, *, script: str | None = "auto",
                 target: str = "") -> None:
        self.repo, self.path, self.script = repo, path, script
        #: #2673: la cible déclarée du projet (`radar.projects.target`) que le
        #: clone doit porter ; vide : sa branche par défaut.
        self.target = (target or "").strip()
        #: attribute keys already read, per target tree (`attributes_key`)
        self._attr_keys: dict = {}

    @contextmanager
    def locked(self, *, wait: float | None = None):
        """Holds the clone's worktree lock while the block reads or writes it.

        Every checkout, merge or reset in the clone's worktree — a stacked
        trial, a landing and its gates, a prepared resolution's replay —
        runs under this lock: a concurrent operation can no longer change the
        tree under a gate (#2428). The wait is bounded
        (`CLONE_LOCK_WAIT_S`); the caller then raises `CloneBusy` and retries
        at its next pass, instead of acting on a tree someone else is using.
        Reentrant in one thread."""
        lock = _clone_lock(self.path)
        lock.acquire(CLONE_LOCK_WAIT_S if wait is None else wait)
        try:
            yield
        finally:
            lock.release()

    def ensure(self) -> None:
        with self.locked():
            if not os.path.isdir(os.path.join(self.path, ".git")):
                shutil.rmtree(self.path, ignore_errors=True)
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                _check(_run(os.path.dirname(self.path), "clone", "--quiet", "--shared",
                            "--no-checkout", self.repo, self.path, timeout=FETCH_TIMEOUT),
                       "clone")
                for key, value in (("user.name", "ameesh radar"),
                                   ("user.email", "radar@ameesh.invalid"),
                                   ("rerere.enabled", "true"), ("rerere.autoUpdate", "false"),
                                   ("gc.auto", "0"), ("core.hooksPath", os.devnull),
                                   ("advice.detachedHead", "false")):
                    _check(_run(self.path, "config", key, value), "config")
                head = plan_git._out(self.repo, "rev-parse", "HEAD")
                _check(_run(self.path, "checkout", "-q", "-f", "--detach", head), "checkout")
            else:
                _run(self.path, "reset", "-q", "--hard")
            self.declare_target()

    def _declared_ref(self) -> str:
        """La branche à déclarer dans le clone : la cible du projet quand elle
        nomme une branche récupérée, sinon la branche par défaut du clone."""
        if self.target:
            name = plan_git.short_branch(self.target)
            if name and plan_git._commit(self.path, "refs/remotes/origin/" + name):
                return "origin/" + name
            if plan_git._commit(self.path, self.target):
                return self.target
            return self.target
        for candidate in plan_git.DEFAULT_TARGETS:
            if plan_git._commit(self.path, candidate):
                return candidate
        return ""

    def declare_target(self) -> str:
        """#2673 : écrit `ameesh.target` dans le clone (création/rafraîchissement).

        Un clone radar hérite de `origin/HEAD` du checkout source : quand
        celui-ci est détaché sur une release, `plan_git.default_target` y lit
        `release/*` et `lot_class` classe « release » les lots sans cible. Si
        la clé manque, la cible du projet est écrite, sinon la branche par
        défaut du clone (`origin/main`, puis main/master). Une cible déjà posée
        n'est jamais écrasée (geste d'exploitation, #2673). Rend la cible
        écrite, vide si le clone n'existe pas ou n'offre aucune branche."""
        if not os.path.isdir(os.path.join(self.path, ".git")):
            return ""
        if os.path.realpath(self.path) == os.path.realpath(self.repo):
            # le clone est le dépôt source : jamais de configuration chez lui
            return ""
        configured = plan_git.declared_target(self.path) or ""
        if configured:
            return configured
        value = self._declared_ref()
        if not value:
            return ""
        _check(_run(self.path, "config", plan_git.TARGET_GIT_KEY, value), "config")
        return value

    def _rr_cache(self) -> str:
        return os.path.join(self.path, ".git", "rr-cache")

    def _store(self, sha: str = "", attributes_tree: str | None = None) -> str:
        # Retrain legacy first-parent caches and v2 checkout -m preimages.
        root = TRAINING_CACHE_TARGET if attributes_tree else TRAINING_CACHE
        return os.path.join(self.path, ".git", root, sha)

    def attributes_key(self, attributes_tree: str) -> str:
        """The `.gitattributes` paths and entries of `attributes_tree`: two target trees
        with the same key drive the same merges, so a cache trained for one
        serves the other; a different key retrains it (#2189)."""
        if attributes_tree not in self._attr_keys:
            proc = _run(self.repo, "ls-tree", "-r", "-z", "--full-tree",
                        attributes_tree)
            blobs = []
            if proc.returncode == 0:
                for entry in proc.stdout.decode("utf-8", "replace").split("\0"):
                    _info, _, more = entry.partition("\t")
                    if more and more.rsplit("/", 1)[-1] == ".gitattributes":
                        # The directory determines a rule's scope; the mode
                        # also matters (Git does not follow attribute symlinks).
                        blobs.append(entry)
            # unreadable tree: fall back on its name, never reuse a wrong cache
            self._attr_keys[attributes_tree] = json.dumps(blobs) if proc.returncode == 0 \
                else attributes_tree
        return self._attr_keys[attributes_tree]

    def trained(self, attributes_tree: str | None = None) -> list[str]:
        try:
            root = self._store(attributes_tree=attributes_tree)
            key = self.attributes_key(attributes_tree) if attributes_tree else None
            names = []
            for name in os.listdir(root):
                store = self._store(name, attributes_tree)
                if name.startswith(".") or not os.path.isdir(store):
                    continue
                if key is not None:
                    try:
                        with open(os.path.join(store, ".attr-tree"), encoding="utf-8") as fh:
                            trained_key = fh.read().strip()
                    except OSError:
                        continue
                    if trained_key != key:
                        continue
                names.append(name)
            return sorted(names)
        except FileNotFoundError:
            return []

    def has(self, sha: str) -> bool:
        return _run(self.path, "cat-file", "-e", sha + "^{commit}").returncode == 0

    def fetch(self) -> None:
        """Resolution refs of this host (agents' repository) and of the
        others (its `origin`)."""
        specs = ("+%s*:%s*" % (REF_PREFIX, REF_PREFIX),
                 "+refs/heads/%s*:refs/ameesh/resolve/*" % BRANCH_PREFIX)
        _run(self.path, "fetch", "--quiet", self.repo, *specs, timeout=FETCH_TIMEOUT)
        url = plan_git._out(self.repo, "remote", "get-url", "origin")
        if url:
            _run(self.path, "fetch", "--quiet", url, *specs, timeout=FETCH_TIMEOUT)
        self.declare_target()

    def _empty_cache(self) -> None:
        shutil.rmtree(self._rr_cache(), ignore_errors=True)
        os.makedirs(self._rr_cache())

    def prime(self, commits, *, attributes_tree: str | None = None) -> list[str]:
        """Trains a cache of its own for each commit not learnt yet; returns
        the commits still missing, or whose training failed. With
        `attributes_tree`, the trial merges read the target's attributes
        (#2189) and the cache lives apart from the branches' one."""
        with self.locked():
            return self._prime(commits, attributes_tree=attributes_tree)

    def _prime(self, commits, *, attributes_tree: str | None = None) -> list[str]:
        commits = list(dict.fromkeys(commits))
        missing = [c for c in commits if not self.has(c)]
        if missing:
            self.fetch()
            missing = [c for c in commits if not self.has(c)]
        done = set(self.trained(attributes_tree))
        for commit in commits:
            if commit in done or commit in missing:
                continue
            self._empty_cache()
            try:
                train(self.path, [commit], script=self.script,
                      attributes_tree=attributes_tree)
            except (GitError, OSError, subprocess.SubprocessError):
                # this pair only: the others are still primed and replayed
                missing.append(commit)
                continue
            tmp = self._store("." + commit, attributes_tree)
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.copytree(self._rr_cache(), tmp)
            if attributes_tree:
                with open(os.path.join(tmp, ".attr-tree"), "w", encoding="utf-8") as fh:
                    fh.write(self.attributes_key(attributes_tree) + "\n")
            store = self._store(commit, attributes_tree)
            shutil.rmtree(store, ignore_errors=True)  # another target key, or a retrain
            os.replace(tmp, store)
        return missing

    def replay(self, a: str, b: str, commit: str, *, attributes_tree: str | None = None,
               export=None, generated=()) -> dict:
        """Merges `b` into `a` with `rerere` and the cache of `commit` only:
        `{state: prepared|stale|clean|error, remaining, files}`. `export(clone,
        resolved paths)` runs before the clone is reset. `generated` (L160g):
        paths that `land` regenerates — never left to rerere, never remaining.
        `attributes_tree`: read `.gitattributes` from that tree, as the radar's
        trial merge does (#2189), whichever tip is checked out."""
        with self.locked():
            return self._replay(a, b, commit, attributes_tree=attributes_tree,
                                export=export, generated=generated)

    def _replay(self, a: str, b: str, commit: str, *, attributes_tree: str | None = None,
                export=None, generated=()) -> dict:
        self._empty_cache()
        if commit in self.trained(attributes_tree):
            shutil.rmtree(self._rr_cache())
            shutil.copytree(self._store(commit, attributes_tree), self._rr_cache())
        try:
            _check(_run(self.path, "checkout", "-q", "-f", "--detach", a), "checkout")
            merge = _run(self.path, "-c", "rerere.enabled=false", "-c", "rerere.autoUpdate=false",
                         *_attr_args(attributes_tree),
                         "merge", "--no-ff", "--no-commit", "--quiet", b)
            if merge.returncode == 0:
                return {"state": "clean", "files": [], "remaining": []}
            files = unmerged(self.path)
            if not files:
                return {"state": "error", "files": [], "remaining": [],
                        "error": _err(merge)[:300]}
            exclude_generated(self.path, files, generated)
            _check(_run(self.path, "-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=false",
                        "rerere"), "rerere")
            remaining = [f for f in rerere_remaining(self.path) if f not in generated]
            if export is not None:
                export(self.path, sorted(set(files) - set(remaining) - set(generated)))
            return {"state": "stale" if remaining else "prepared", "files": files,
                    "remaining": remaining}
        finally:
            _run(self.path, "reset", "-q", "--hard")
            _run(self.path, "clean", "-q", "-f", "-d")
            self._empty_cache()  # rerere never learns from what is left here


def annotate(report: dict, rows: list[dict], lots: dict, *, replayer: Replayer | None,
             cache=None, generated=()) -> None:
    """Completes a radar report: each pair in conflict gets its resolver,
    and, if a resolution of the pair is recorded, its replay (`prepared` or
    `stale`). `rows`: recorded resolutions (newest first); `lots`: open lots
    by id (priority, state)."""
    branches = {b["work_item"]: b for b in report.get("branches") or ()}
    latest: dict = {}
    for row in rows:
        key = frozenset((int(row["work_item_id"]), int(row["other_item_id"])))
        latest.setdefault(key, row)
    sizes = getattr(cache, "sizes", None) if cache is not None else None
    replays = getattr(cache, "replays", None) if cache is not None else None
    sizes = {} if sizes is None else sizes
    replays = {} if replays is None else replays
    conflicted = [p for p in report.get("pairs") or ()
                  if p["state"] in ("conflict", "target-conflict", "unclassified", "superseded")]

    def target_tree(p: dict) -> str | None:
        """The target's tree, whose `.gitattributes` decide the pair's merge
        (#2189), like `radar.trial_merge`'s `attributes_tree`."""
        if p.get("target_head"):
            return p["target_head"]
        for n in p["lots"]:
            head = (branches.get(n) or {}).get("target_head")
            if head:
                return head
        return None

    wanted: dict = {}
    for p in conflicted:
        row = latest.get(frozenset(p["lots"]))
        if row is not None:
            wanted.setdefault(target_tree(p), []).append(row["commit_sha"])
    missing: set = set()
    if wanted and replayer is not None:
        try:
            replayer.ensure()
            for tree, commits in wanted.items():
                missing.update((c, tree) for c in
                               replayer.prime(commits, attributes_tree=tree))
        except (GitError, OSError) as exc:
            report["resolution_error"] = "%s: %s" % (type(exc).__name__, exc)
            replayer = None
    for p in conflicted:
        one, two = (dict(branches.get(n) or {"work_item": n}, **{
            k: (lots.get(n) or {}).get(k) for k in ("priority", "state")}) for n in p["lots"])
        if one.get("base") and two.get("base") and not p.get("attribution_pending"):
            p["resolver"] = choose_resolver(one, two, p["files"], repo=report.get("repo"),
                                            sizes=sizes)
        row = latest.get(frozenset(p["lots"]))
        if row is None:
            continue
        resolution = {"by": int(row["work_item_id"]), "commit": row["commit_sha"],
                      "ref": row.get("ref"), "created_by": row.get("created_by"),
                      "created_ts": row.get("created_ts")}
        p["resolution"] = resolution
        tree = target_tree(p)
        if replayer is None or (row["commit_sha"], tree) in missing:
            resolution["replay"] = "unavailable"
            continue
        # only the target's `.gitattributes` can change the replay: a target
        # move that leaves them alone reuses the cached result (#2189)
        attrs = replayer.attributes_key(tree) if tree else None
        key = (p["tips"][0], p["tips"][1], row["commit_sha"], attrs,
               tuple(sorted(generated)))
        # #2254: an `error` is not a result of these inputs — a checkout
        # disturbed by another pass, an unreadable object, a saturated disk —
        # it is retried at the next pass. Keeping it would freeze a transient
        # failure on this executor for as long as the tips stay put, and make
        # it alternate with the `prepared` another executor reports: the very
        # input-keyed cache that saves a replay must not keep a non-answer.
        result = replays.get(key)
        if result is None or result.get("state") == "error":
            try:
                result = replayer.replay(*p["tips"], row["commit_sha"],
                                         attributes_tree=tree, generated=generated)
            except (GitError, OSError) as exc:
                result = {"state": "error", "error": str(exc)[:300]}
            if result.get("state") != "error":
                replays[key] = result
        resolution["replay"] = result["state"]
        resolution["remaining"] = result.get("remaining") or []
        if (p["state"] == "conflict" and result["state"] in ("prepared", "stale")
                and (result["state"] == "prepared" or not p.get("attribution_pending"))):
            p["state"] = result["state"]


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def add_parser(work_sub, func) -> None:
    parser = work_sub.add_parser(
        "resolve", help="prepare the resolution of a conflict between two lots, ahead of "
                        "the merge (L160d): ameesh work resolve <lot> --with <other>")
    parser.add_argument("id", type=int, help="your lot (the resolver)")
    parser.add_argument("--with", dest="other", type=int, default=None,
                        help="the lot whose branch conflicts with yours")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--done", action="store_true",
                        help="commit the resolution, push it under refs/ameesh/resolutions/, "
                             "record it and remove the worktree")
    action.add_argument("--abort", action="store_true", help="drop the open resolution")
    parser.add_argument("-m", "--message", default="",
                        help="with --done: a note added to the resolution commit")
    parser.add_argument("--repo", default=None,
                        help="the project repository (default: the current git repository)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--actor", default="",
                        help="default: the identity bound to the session (AGENT_MAIL_NAME)")
    parser.set_defaults(func=func)


def _print_open(out: dict) -> None:
    if out["state"] == "clean":
        print("#%d and #%d merge without conflict (%s + %s): nothing to prepare"
              % (out["lot"], out["other"], out["lot_tip"][:12], out["other_tip"][:12]))
        return
    if out["state"] == "prepared":
        print("resolution of #%d with #%d already prepared: %s (%s)" % (
            out["lot"], out["other"], out["commit"][:12], out.get("ref") or "local only"))
        print("nothing to open; the radar replays it at each pass and tells the "
              "resolver when a tip moves and it goes stale")
        return
    print("resolution of #%d with #%d %s: %s" % (
        out["lot"], out["other"], "already open" if out.get("already") else "open",
        out["path"]))
    print("merge of %s (%s) into %s (%s), target %s" % (
        out["other_branch"], out["other_tip"][:12], out["lot_branch"], out["lot_tip"][:12],
        out["target"]))
    print("conflicts: %s" % ", ".join(out["files"]))
    if out.get("replayed"):
        print("replayed by rerere from an earlier resolution: %s"
              % ", ".join(out["replayed"]))
    left = sorted(set(out.get("markers") or ()) | set(out.get("remaining") or ()))
    print("to resolve: %s" % (", ".join(left) if left else "nothing left"))
    print("then: ameesh work resolve %d --with %d --done   (or --abort)"
          % (out["lot"], out["other"]))


def run(cfg, db, args) -> int:
    try:
        if args.other is None:
            if args.done or args.abort:
                raise ResolveError("--done and --abort need --with <other lot>")
            rows = of_lot(db, args.id)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print("\n".join(show_lines(rows, args.id)) if rows
                      else "lot #%d: no prepared resolution" % args.id)
            return 0
        if args.abort:
            existed = abort_resolution(args.id, args.other, repo=args.repo)
            out = {"lot": args.id, "other": args.other, "aborted": existed}
            print(json.dumps(out) if args.json else
                  "resolution of #%d with #%d %s" % (args.id, args.other,
                                                     "dropped" if existed else "was not open"))
            return 0
        if args.done:
            out = finish_resolution(db, args.id, args.other, repo=args.repo,
                                    actor=args.actor, note=args.message)
            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
                return 0
            if out.get("already"):
                print("resolution of #%d with #%d already prepared: %s, %s" % (
                    out["lot"], out["other"], out["commit"][:12],
                    "pushed to origin %s" % out["ref"] if out["ref"] else
                    "kept in %s only" % out["local_ref"]))
                print("the worktree was cleaned up; nothing re-committed, nothing re-recorded")
                return 0
            print("resolution of #%d with #%d recorded: %s, %s" % (
                out["lot"], out["other"], out["commit"][:12],
                "pushed to origin %s" % out["ref"] if out["ref"] else
                "kept in %s only (push failed: %s)" % (out["local_ref"], out["push_error"])))
            print("the radar replays it at each pass: prepared while the conflict stays the "
                  "same, stale (and you are told) when it changes")
            return 0
        out = open_resolution(db, args.id, args.other, repo=args.repo)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            _print_open(out)
        return 0
    except (ResolveError, GitError, ValueError, plan_git.MergeProbeError) as exc:
        import sys
        print("error: %s" % exc, file=sys.stderr)
        return 1
