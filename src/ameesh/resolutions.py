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
   under `<git common dir>/ameesh-resolve/<lot>-<other>`), primes `rerere`
   with any earlier resolution of the pair, and merges the other branch into
   it with `rerere` on: hunks already resolved come back resolved;
2. the agent resolves the remaining conflicts in that worktree;
3. `resolve --done` checks that no conflict marker is left, commits the
   merge (trailer `ameesh-resolution: <lot> <other>`), points
   `refs/ameesh/resolutions/<lot>/<other>` at it, pushes it there on
   `origin` (or to the branch `ameesh/resolve/<lot>-<other>` when the host
   refuses refs outside `refs/heads`), records it (table `work_resolutions`,
   migration 0057) and removes the worktree.

The lot branch never changes: it stays the version WITHOUT the other lot;
the resolution commit R(lot, other) is the version WITH it.

Validity, at each radar pass: in its private clone of the project
(`<state_dir>/radar/<project>.clone`, `git clone --shared`: objects read
from the agents' repository, nothing written to it), the radar primes its
own `rr-cache` from the resolution commits with git's `rerere-train.sh`
(contrib; a port of the same loop when the script is not installed), then
replays the merge of the two current tips. If `rerere` resolves every
conflicted path, the pair is `prepared`; otherwise it is `stale`, and the
lot that prepared it is told (passive mail, L160c). A rebase or a new push
that leaves the conflicting hunks as they were keeps the pair `prepared`:
`rerere` recognises content, not commits.

Resolver: the lot expected to merge second. Until the merge queue (L160e)
gives the order, it is guessed: the lot with the higher priority merges
first; then the one further along (`qa` before `build` before the rest);
then the lot with the smaller change in the conflicting files resolves;
then the newer lot. The radar names the resolver in its mail.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from . import plan_git, storage, work
from .canon import FETCH_TIMEOUT, GIT_TIMEOUT, GitError, _git, _git_env

REF_PREFIX = "refs/ameesh/resolutions/"
#: fallback for hosts that refuse refs outside `refs/heads`
BRANCH_PREFIX = "ameesh/resolve/"
TRAILER = "ameesh-resolution"
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


def ref_name(lot: int, other: int) -> str:
    return "%s%d/%d" % (REF_PREFIX, lot, other)


def branch_name(lot: int, other: int) -> str:
    return "%s%d-%d" % (BRANCH_PREFIX, lot, other)


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

def _run(cwd: str, *args: str, timeout: float = GIT_TIMEOUT,
         stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return _git(cwd, *args, stdin=stdin, timeout=timeout)


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


def rerere_remaining(cwd: str) -> list[str]:
    """Conflicted paths that `rerere` did not resolve."""
    proc = _run(cwd, "rerere", "remaining")
    return sorted({p for p in _text(proc).splitlines() if p}) if proc.returncode == 0 \
        else unmerged(cwd)


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


def train(cwd: str, commits, *, script: str | None = "auto") -> list[str]:
    """Primes the `rr-cache` of the repository of worktree `cwd` from the
    merge commits `commits` (`--overwrite`: a replaced resolution wins),
    with `rerere-train.sh`, or the same loop in Python when the script is
    not installed (`script=None` forces the port). Leaves `cwd` clean, on
    the commit it was on. Returns the commits it learnt from."""
    commits = [c for c in commits if c]
    if not commits:
        return []
    os.makedirs(_check(_run(cwd, "rev-parse", "--path-format=absolute", "--git-path",
                            "rr-cache"), "rev-parse"), exist_ok=True)
    if script == "auto":
        script = rerere_train_script()
    if script:
        head = _check(_run(cwd, "rev-parse", "HEAD"), "rev-parse")
        proc = subprocess.run(["sh", script, "--overwrite", "--no-walk", *commits], cwd=cwd,
                              capture_output=True, env=_git_env(),
                              timeout=FETCH_TIMEOUT, check=False)
        out = proc.stdout.decode("utf-8", "replace")
        # the script returns to the branch it started on; make sure of it
        _run(cwd, "checkout", "-q", "-f", "--detach", head)
        learnt = re.findall(r"^Learning from ([0-9a-f]+)", out, re.M)
        return [c for c in commits if any(c.startswith(short) for short in learnt)]
    head = _check(_run(cwd, "rev-parse", "HEAD"), "rev-parse")
    learnt = []
    try:
        for commit in commits:
            parents = _text(_run(cwd, "rev-parse", commit + "^@")).split()
            if len(parents) < 2:
                continue
            _check(_run(cwd, "checkout", "-q", "-f", "--detach", parents[0]), "checkout")
            if _run(cwd, "-c", "rerere.enabled=true", "merge", "-q", "--no-ff",
                    "--no-commit", *parents[1:]).returncode != 0:
                _run(cwd, "rerere", "forget", ".")
                if unmerged(cwd):
                    _run(cwd, "-c", "rerere.enabled=true", "rerere")
                    _run(cwd, "checkout", commit, "--", ".")
                    _run(cwd, "-c", "rerere.enabled=true", "rerere")
                    learnt.append(commit)
            _run(cwd, "reset", "-q", "--hard")
    finally:
        _run(cwd, "reset", "-q", "--hard")
        _run(cwd, "checkout", "-q", "-f", "--detach", head)
    return learnt


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
    root = plan_git._out(repo or os.getcwd(), "rev-parse", "--show-toplevel")
    if not root:
        raise ResolveError("%s is not inside a git repository: pass --repo"
                           % (repo or os.getcwd()))
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
    if targets[lot] != targets[other]:
        raise ResolveError("lots #%d and #%d do not share a target (%s, %s): their branches "
                           "never merge together" % (lot, other, targets[lot], targets[other]))
    tips = {}
    for n, row in rows.items():
        tips[n] = _branch_tip(repo, row["branch"], fetch=fetch and n == other)
        if not tips[n]:
            raise ResolveError("branch %s of lot #%d is neither local nor on origin"
                               % (row["branch"], n))
    return {"lot": rows[lot], "other": rows[other], "target": targets[lot],
            "lot_tip": tips[lot], "other_tip": tips[other]}


def _recorded(db, lot: int, other: int) -> list[dict]:
    """Recorded resolutions of the pair, either way round, newest first."""
    return [r for r in storage.of(db).work.resolutions([lot])
            if {int(r["work_item_id"]), int(r["other_item_id"])} == {lot, other}]


def _ensure_commit(repo: str, row: dict) -> bool:
    """Makes the recorded resolution commit available locally (fetched from
    `origin` when it was prepared on another host)."""
    sha = row["commit_sha"]
    if _run(repo, "cat-file", "-e", sha + "^{commit}").returncode == 0:
        return True
    if row.get("ref"):
        source = row["ref"] if row["ref"].startswith("refs/") else "refs/heads/" + row["ref"]
        _run(repo, "fetch", "--quiet", "origin", source, timeout=FETCH_TIMEOUT)
    return _run(repo, "cat-file", "-e", sha + "^{commit}").returncode == 0


def _remove_worktree(repo: str, path: str) -> None:
    _run(repo, "worktree", "unlock", path)
    _run(repo, "worktree", "remove", "--force", path)
    shutil.rmtree(path, ignore_errors=True)
    _run(repo, "worktree", "prune")
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


def open_resolution(db, lot: int, other: int, *, repo: str | None = None) -> dict:
    """Opens (or shows, if already open) the resolution of `lot` with
    `other`. Returns `{state: open|clean, path, files, replayed, remaining}`."""
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    meta = _read_meta(path)
    if meta is not None:
        remaining = unmerged(path)
        return dict(meta, state="open", path=path, remaining=remaining,
                    markers=with_markers(path, meta["files"]), already=True)
    if os.path.exists(path):
        _remove_worktree(repo, path)
    ctx = pair(db, lot, other, repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _check(_run(repo, "worktree", "add", "--quiet", "--detach", path, ctx["lot_tip"]),
           "worktree add")
    try:
        _run(repo, "worktree", "lock", "--reason",
             "ameesh work resolve %d --with %d" % (lot, other), path)
        primed = [r["commit_sha"] for r in _recorded(db, lot, other)
                  if _ensure_commit(repo, r)]
        train(path, primed[:1])
        merge = _run(path, "-c", "rerere.enabled=true", "-c", "merge.conflictStyle=zdiff3",
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
        remaining = rerere_remaining(path)
        meta = {"lot": lot, "other": other, "lot_branch": ctx["lot"]["branch"],
                "other_branch": ctx["other"]["branch"], "target": ctx["target"],
                "lot_tip": ctx["lot_tip"], "other_tip": ctx["other_tip"], "files": files,
                "replayed": sorted(set(files) - set(remaining)),
                "primed": primed[:1]}
        with open(_meta_path(path), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
    except BaseException:
        if _read_meta(path) is None:
            _remove_worktree(repo, path)
        raise
    return dict(meta, state="open", path=path, remaining=remaining,
                markers=with_markers(path, files), already=False)


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


def push(repo: str, sha: str, lot: int, other: int) -> tuple[str | None, str | None]:
    """Pushes the resolution commit to `origin`, under its dedicated ref,
    else as a branch. Returns (ref pushed, error)."""
    if not plan_git._out(repo, "remote", "get-url", "origin"):
        return None, "no origin remote"
    error = None
    for ref in (ref_name(lot, other), "refs/heads/" + branch_name(lot, other)):
        proc = _run(repo, "push", "--quiet", "--force", "--no-verify", "origin",
                    "%s:%s" % (sha, ref), timeout=FETCH_TIMEOUT)
        if proc.returncode == 0:
            return ref, None
        error = _err(proc).splitlines()[-1:] or ["git push failed"]
        error = error[0]
    return None, error


def finish_resolution(db, lot: int, other: int, *, repo: str | None = None,
                      actor: str = "", note: str = "", push_fn=push) -> dict:
    """Commits the resolution open in its worktree, points the dedicated ref
    at it, pushes it, records it, and removes the worktree."""
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    meta = _read_meta(path)
    if meta is None:
        raise ResolveError("no resolution of #%d with #%d is open here: "
                           "ameesh work resolve %d --with %d" % (lot, other, lot, other))
    merging = _run(path, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    if merging:
        left = with_markers(path, meta["files"])
        if left:
            raise ResolveError("conflict markers left in %s (%s)" % (", ".join(left), path))
        _check(_run(path, "add", "-u"), "add")
        present = [f for f in meta["files"] if os.path.lexists(os.path.join(path, f))]
        if present:
            _check(_run(path, "add", "--", *present), "add")
        still = unmerged(path)
        if still:
            raise ResolveError("still unmerged: %s (%s)" % (", ".join(still), path))
        _check(_run(path, "-c", "rerere.enabled=true", "commit", "--quiet", "--no-verify",
                    "-F", "-", stdin=_message(meta, note).encode()), "commit")
    sha = _check(_run(path, "rev-parse", "HEAD"), "rev-parse")
    parents = _text(_run(path, "rev-parse", sha + "^@")).split()
    if parents != [meta["lot_tip"], meta["other_tip"]]:
        raise ResolveError("HEAD of %s is not the merge of %s and %s: ameesh work resolve "
                           "%d --with %d --abort, then start again"
                           % (path, meta["lot_tip"][:12], meta["other_tip"][:12], lot, other))
    _check(_run(repo, "update-ref", ref_name(lot, other), sha), "update-ref")
    pushed, error = push_fn(repo, sha, lot, other)
    row = storage.of(db).work.record_resolution(
        lot, other, commit=sha, lot_tip=meta["lot_tip"], other_tip=meta["other_tip"],
        ref=pushed, files=meta["files"],
        note="resolution prepared with #%d: %s (%s)" % (other, sha[:12],
                                                        pushed or "local only"),
        actor=actor)
    if row is None:
        raise ResolveError("lot #%d or #%d no longer exists: nothing recorded (commit %s "
                           "kept in %s)" % (lot, other, sha[:12], path))
    _remove_worktree(repo, path)
    return {"lot": lot, "other": other, "commit": sha, "ref": pushed, "push_error": error,
            "local_ref": ref_name(lot, other), "files": meta["files"]}


def abort_resolution(lot: int, other: int, *, repo: str | None = None) -> bool:
    repo = repo_root(repo)
    path = worktree_path(repo, lot, other)
    existed = os.path.exists(path) or os.path.exists(_meta_path(path))
    _remove_worktree(repo, path)
    return existed


def of_lot(db, lot: int) -> list[dict]:
    """Recorded resolutions where `lot` is either side."""
    return storage.of(db).work.resolutions([lot])


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

class Replayer:
    """The radar's private clone of a project, with its own `rr-cache`,
    primed from the resolution commits. Objects come from the agents'
    repository (`--shared`); merges are replayed in the clone's worktree."""

    def __init__(self, repo: str, path: str, *, script: str | None = "auto") -> None:
        self.repo, self.path, self.script = repo, path, script

    def ensure(self) -> None:
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

    def _trained_path(self) -> str:
        return os.path.join(self.path, ".git", "ameesh-trained.json")

    def trained(self) -> list[str]:
        try:
            with open(self._trained_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

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

    def prime(self, commits) -> list[str]:
        """Trains on the commits not learnt yet; returns those still missing."""
        commits = list(dict.fromkeys(commits))
        missing = [c for c in commits if not self.has(c)]
        if missing:
            self.fetch()
            missing = [c for c in commits if not self.has(c)]
        done = self.trained()
        new = [c for c in commits if c not in done and c not in missing]
        if new:
            train(self.path, new, script=self.script)
            with open(self._trained_path(), "w", encoding="utf-8") as fh:
                json.dump(done + new, fh)
        return missing

    def replay(self, a: str, b: str) -> dict:
        """Merges `b` into `a` with `rerere`: `{state: prepared|stale|clean|error,
        remaining, files}`."""
        try:
            _check(_run(self.path, "checkout", "-q", "-f", "--detach", a), "checkout")
            merge = _run(self.path, "merge", "--no-ff", "--no-commit", "--quiet", b)
            if merge.returncode == 0:
                return {"state": "clean", "files": [], "remaining": []}
            files = unmerged(self.path)
            if not files:
                return {"state": "error", "files": [], "remaining": [],
                        "error": _err(merge)[:300]}
            remaining = rerere_remaining(self.path)
            return {"state": "stale" if remaining else "prepared", "files": files,
                    "remaining": remaining}
        finally:
            _run(self.path, "reset", "-q", "--hard")
            _run(self.path, "clean", "-q", "-f", "-d")


def annotate(report: dict, rows: list[dict], lots: dict, *, replayer: Replayer | None,
             cache=None) -> None:
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
    conflicted = [p for p in report.get("pairs") or () if p["state"] == "conflict"]
    found = [latest[frozenset(p["lots"])] for p in conflicted if frozenset(p["lots"]) in latest]
    missing: list[str] = []
    if found and replayer is not None:
        try:
            replayer.ensure()
            missing = replayer.prime([r["commit_sha"] for r in reversed(found)])
        except (GitError, OSError) as exc:
            report["resolution_error"] = "%s: %s" % (type(exc).__name__, exc)
            replayer = None
    for p in conflicted:
        one, two = (dict(branches.get(n) or {"work_item": n}, **{
            k: (lots.get(n) or {}).get(k) for k in ("priority", "state")}) for n in p["lots"])
        if one.get("base") and two.get("base"):
            p["resolver"] = choose_resolver(one, two, p["files"], repo=report.get("repo"),
                                            sizes=sizes)
        row = latest.get(frozenset(p["lots"]))
        if row is None:
            continue
        resolution = {"by": int(row["work_item_id"]), "commit": row["commit_sha"],
                      "ref": row.get("ref"), "created_by": row.get("created_by"),
                      "created_ts": row.get("created_ts")}
        p["resolution"] = resolution
        if replayer is None or row["commit_sha"] in missing:
            resolution["replay"] = "unavailable"
            continue
        key = (p["tips"][0], p["tips"][1], row["commit_sha"])
        if key not in replays:
            try:
                replays[key] = replayer.replay(*p["tips"])
            except (GitError, OSError) as exc:
                replays[key] = {"state": "error", "error": str(exc)[:300]}
        result = replays[key]
        resolution["replay"] = result["state"]
        resolution["remaining"] = result.get("remaining") or []
        if result["state"] in ("prepared", "stale"):
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
