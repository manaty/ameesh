# SPDX-License-Identifier: AGPL-3.0-only
"""Regenerable build artifacts of idle agent worktrees (lot L230, #247).

After the 2026-10-11 disk outage (335 worktrees, 144 with their own
`node_modules` of several GB, Rust `target/` dirs of ~14 GB each), the
housekeeping pass frees what a build regenerates:

* **What**: directories named by the policy rules (`artifacts`, default
  `node_modules` at any depth, and `target` only next to a `Cargo.toml`),
  inside the non-main worktrees of the repositories of the registered agents.
  Never inside `.git`, never in a nested checkout (judged on its own), never
  through a symbolic link (a `node_modules` shared by #248 is a link into the
  cache: skipped, not even unlinked), never under the managed cache root.
* **When**: the worktree had no activity (mtime or ctime of anything outside
  its artifacts, of its git index and HEAD, and of the last entry of its
  reflog: its file mtime is rewritten by `git gc`, #4685) for
  `artifact_idle` (24 h by
  default), or for `artifact_ended_idle` (6 h by default, #3956) when every
  lot it names is finished: nothing rebuilds in it any more, so its
  regenerable output is only weight. And NO process of the user has its cwd,
  its executable, an open file or a mapped file inside it
  (`platform.user_paths`, read again right before each deletion; the
  descriptors of the pass itself do not count, #5225). A process
  of the user that cannot be read leaves a doubt: no deletion at all for the
  pass (the dry run still lists its plan, #3195), unless its name is allowed
  by the policy (`unreadable_ok`, empty by default; logged) or it is the
  user's service manager (`systemd --user`: not dumpable, lives in `/`). A
  key agent (`gpg-agent`) is not dumpable on purpose and its open descriptors
  cannot be proven from outside: it stays a doubt (#3195 R5); an agent host
  runs none (docs/EXPLOITATION.md).
* **Never**: the main checkout of a repository, a directory that contains an
  agent's working directory (the directory itself is only guarded by `keep()`
  and the process check: #3956 removes its artifacts once its lots are all
  finished and it is idle), a worktree charged to an agent in turn or
  named (branch or directory) after a lot still open (#3195: `keep`), a
  locked worktree (`git worktree lock`),
  a worktree whose walk is incomplete (unreadable entry, mount point, too many
  entries), an artifact with a file tracked by git inside (untracked files
  inside `node_modules` or `target` are regenerable by definition: deleted),
  a `target` without a `Cargo.toml` beside it (whatever the policy says,
  checked again right before deleting). Inside an artifact, a subtree on
  another filesystem, one that holds a `.git`, and the managed roots are left
  in place (the deletion is then `partial`).
* **How**: the artifact is reached through directory file descriptors from
  the worktree root, never following a link; the root itself must still be
  the surveyed directory (same device and inode, and the kernel's path of the
  descriptor is the surveyed real path: an ancestor swapped for a link is
  refused). It is then renamed atomically to a sibling
  `.ameesh-trash-<pid>-<ts>-<random>` (a process starting later sees no
  `node_modules`, not half of one), recorded in the host's journal BEFORE
  the rename, and deleted through descriptors too. A trash left by an
  interrupted pass is finished by the next one only if the journal records
  it (same name, same parent, same inode): any other `.ameesh-trash-*` is
  an ordinary directory, never deleted. One worktree at a time, in a thread
  at the lowest CPU priority and the idle IO class (`ionice -c 3` when
  available), within a time budget per pass checked at every entry of every
  walk, dry-run sizing included (the rest waits for the next pass), one
  pass at a time on the host (`flock`, runner and CLI, shared with the
  removal of merged worktrees). Deleting frees space: under pressure the
  pass keeps going, serialised. Each deletion is logged (`housekeeping_log`,
  kind `artifact`: `deleted`, or `partial` with what was left and why) with
  the bytes freed; the dry run lists what would go and its size, and writes
  nothing.
* **Out of scope, by design**: processes of other users (an agent host runs
  its agents as one user); `unreadable_ok` matches names only, an explicit
  choice of the host's owner.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass

from . import platform

#: default rules: a directory name, optionally `:<file>` that must sit beside it
DEFAULT_RULES = ("node_modules", "target:Cargo.toml")
#: never bypassable by the policy: a `target` only counts beside a `Cargo.toml`
FORCED_BESIDE = {"target": "Cargo.toml"}
DEFAULT_IDLE_S = 24 * 3600.0
#: #3956 : inactivité exigée d'un worktree dont TOUS les lots cités sont finis.
#: Plus rien ne s'y reconstruit : ses sorties régénérables ne sont que du poids,
#: et les garder 24 h comme un worktree sans lot a rempli le disque d'agents-1.
DEFAULT_ENDED_IDLE_S = 6 * 3600.0
#: #3956 : un worktree qui CONTIENT le dossier de travail d'un agent n'est
#: jamais touché ; ce dossier lui-même ne l'est plus par statut — ses artefacts
#: ne partent que si `keep()` le permet (aucun lot ouvert, aucun tour) et
#: qu'aucun processus ne s'y tient, sinon ses node_modules restaient à vie.
#: the walk of a worktree (outside its artifacts) stops being trusted beyond this
WALK_LIMIT = 300_000
#: sizes of artifacts are counted up to this many files (dry run)
SIZE_LIMIT = 5_000_000
#: deletions stop after this long; the rest waits for the next pass
PASS_BUDGET_S = 600.0
#: the runner scans the worktrees at most this often (idleness is counted in days)
SCAN_EVERY_S = 6 * 3600.0
#: an artifact renamed before its deletion
TRASH_PREFIX = ".ameesh-trash-"
#: the names this module gives (`trash_name`); a trash is ours only if the journal says so
TRASH_RE = re.compile(r"\A\.ameesh-trash-\d+-\d+-[0-9a-f]{16}\Z")
GIT_TIMEOUT = 30.0

_last_scan: dict = {}


@dataclass(frozen=True)
class Rule:
    name: str
    beside: str = ""

    def matches(self, parent: str, name: str) -> bool:
        if name != self.name:
            return False
        return not self.beside or os.path.isfile(os.path.join(parent, self.beside))

    def __str__(self) -> str:
        return "%s:%s" % (self.name, self.beside) if self.beside else self.name


def _plain_name(text: str) -> bool:
    return bool(text) and "/" not in text and text not in (".", "..", ".git") \
        and not text.startswith(TRASH_PREFIX)


def parse_names(value) -> tuple[tuple[str, ...] | None, str]:
    """Process names of `policy.housekeeping.unreadable_ok`. (names, problem)."""
    if not isinstance(value, (list, tuple)) or \
            not all(isinstance(v, str) and _plain_name(v.strip()) for v in value):
        return None, "list of process names expected (e.g. [btop])"
    return tuple(v.strip() for v in value), ""


def parse_rules(value) -> tuple[tuple[Rule, ...] | None, str]:
    """Rules of `policy.housekeeping.artifacts`: a list of `name` or
    `name:file` (the directory only counts with `file` beside it); an empty
    list turns the deletion off. `target` always requires `Cargo.toml`
    (`FORCED_BESIDE`). (rules, problem)."""
    if not isinstance(value, (list, tuple)):
        return None, "list of directory names expected (e.g. [node_modules, target:Cargo.toml])"
    rules = []
    for item in value:
        if not isinstance(item, str):
            return None, "%r: a directory name, optionally `:<file beside it>`" % (item,)
        name, _, beside = item.strip().partition(":")
        if not _plain_name(name) or (beside and not _plain_name(beside)):
            return None, "%r: a directory name, optionally `:<file beside it>`" % (item,)
        forced = FORCED_BESIDE.get(name)
        if forced and beside != forced:
            return None, "%r: `%s` only counts beside a `%s` (write %s:%s)" % (
                item, name, forced, name, forced)
        rules.append(Rule(name, beside))
    return tuple(rules), ""


def rules_of(declared) -> tuple[Rule, ...]:
    rules, _problem = parse_rules(list(DEFAULT_RULES) if declared is None else declared)
    return rules or ()


class Budget:
    """The time budget of a pass, checked inside every walk."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + seconds

    def remaining(self) -> float:
        """Seconds left before the deadline (0 once it is reached)."""
        return max(0.0, self.deadline - self.clock())

    def expired(self) -> bool:
        return self.clock() > self.deadline


class Journal:
    """The trash dirs renamed by a deleting pass and not deleted yet: a file
    of the host next to the lock, one JSON record per line (`trash`, its
    `parent`, `parent_id` and `id` as (device, inode), `kind`, `origin`).
    A record is written (fsync) BEFORE the rename and dropped once the trash
    is gone: only a recorded trash is ever resumed. Written under the lock."""

    def __init__(self, path: str):
        self.path = path

    def records(self) -> list[dict]:
        """Recorded trash dirs; OSError if the journal cannot be read."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("trash"), str) \
                    and isinstance(rec.get("parent"), str) \
                    and TRASH_RE.match(os.path.basename(rec["trash"])) \
                    and os.path.dirname(rec["trash"]) == rec["parent"]:
                out.append(rec)
        return out

    def _write(self, records: list[dict]) -> None:
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def add(self, record: dict) -> None:
        self._write([r for r in self.records() if r["trash"] != record["trash"]] + [record])

    def drop(self, trash: str) -> None:
        records = self.records()
        kept = [r for r in records if r["trash"] != trash]
        if len(kept) != len(records):
            self._write(kept)


def trash_name() -> str:
    return "%s%d-%d-%s" % (TRASH_PREFIX, os.getpid(), int(time.time()), secrets.token_hex(8))


def identity(path: str) -> tuple[int, int] | None:
    """(device, inode) of the directory `path` itself (not followed), or None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if stat.S_ISDIR(st.st_mode) else None


# --------------------------------------------------------------------------
# one worktree
# --------------------------------------------------------------------------

def _gitdir(worktree: str) -> str:
    dot = os.path.join(worktree, ".git")
    if os.path.isdir(dot) and not os.path.islink(dot):
        return dot
    try:
        with open(dot, encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return ""
    if not line.startswith("gitdir:"):
        return ""
    return os.path.normpath(os.path.join(worktree, line[len("gitdir:"):].strip()))


def _reflog_latest(path: str) -> float | None:
    """Epoch of the last entry of the reflog `path` (None: unreadable).

    The file's own mtime is not activity: `git gc` rewrites every reflog of
    the repository (its `reflog expire` task) without changing the entries,
    so an activity read from it would see all the worktrees active again
    (#4685). A real operation (checkout, reset, commit) appends an entry
    dated now."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            data = fh.read()
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        fields = line.split(b"\t", 1)[0].split()
        if len(fields) < 2 or not re.fullmatch(rb"[+-]\d{4}", fields[-1]):
            continue
        try:
            return float(fields[-2])
        except ValueError:
            continue
    return None


def survey(worktree: str, rules, *, skip_roots=(), skip_ids=(), limit: int = WALK_LIMIT,
           budget: Budget | None = None) -> dict:
    """Walk of a worktree outside its artifacts: `latest` activity (mtime or
    ctime, git index, HEAD and the last reflog entry included), `artifacts`
    (directories matched by the rules, not entered), `real` and `identity`
    (device, inode) of its root, `complete`, `why`.

    Not entered: `.git`, a nested checkout (a directory with a `.git`), a
    symbolic link, another filesystem (incomplete), `skip_roots`, the trash
    dirs of the journal (`skip_ids`: (device, inode)). The deadline and the
    entry limit are checked at every entry."""
    out: dict = {"latest": None, "artifacts": [], "complete": True, "why": "",
                 "real": os.path.realpath(worktree), "identity": None}
    try:
        top = os.lstat(out["real"])
    except OSError as exc:
        out.update(complete=False, why="unreadable (%s)" % (exc.strerror or exc))
        return out
    out["identity"] = (top.st_dev, top.st_ino)
    latest = max(top.st_mtime, top.st_ctime)
    gitdir = _gitdir(worktree)
    for name in ("index", "HEAD"):
        try:
            st = os.stat(os.path.join(gitdir, name)) if gitdir else None
        except OSError:
            st = None
        if st is not None:
            latest = max(latest, st.st_mtime, st.st_ctime)
    # the reflog's file mtime is rewritten by `git gc`: only its last entry
    # dates an operation in the worktree (#4685)
    if gitdir:
        reflog = _reflog_latest(os.path.join(gitdir, "logs", "HEAD"))
        if reflog is not None:
            latest = max(latest, reflog)
    skip = {os.path.realpath(p) for p in skip_roots if p}
    skip_ids = {tuple(i) for i in skip_ids}
    count = 0
    stack = [worktree]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    count += 1
                    if count > limit:
                        out.update(complete=False, latest=latest,
                                   why="more than %d entries: activity not verifiable" % limit)
                        return out
                    if budget is not None and budget.expired():
                        out.update(complete=False, why="time budget of the pass reached")
                        return out
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        out.update(complete=False, why="unreadable entry: %s" % entry.path)
                        continue
                    is_dir = stat.S_ISDIR(st.st_mode)
                    if is_dir and (st.st_dev, st.st_ino) in skip_ids:
                        continue   # our trash, finished by the journal
                    if is_dir and entry.name != ".git" and st.st_dev == top.st_dev \
                            and any(rule.matches(current, entry.name) for rule in rules):
                        # an install or a build is not work: its times are not activity
                        if os.path.realpath(entry.path) not in skip:
                            out["artifacts"].append(entry.path)
                        continue
                    latest = max(latest, st.st_mtime, st.st_ctime)
                    if not is_dir or entry.name == ".git":
                        continue
                    if st.st_dev != top.st_dev:
                        out.update(complete=False, latest=latest,
                                   why="mount point inside: %s" % entry.path)
                        return out
                    if os.path.realpath(entry.path) in skip:
                        continue
                    if os.path.lexists(os.path.join(entry.path, ".git")):
                        continue   # a nested checkout: judged on its own
                    stack.append(entry.path)
        except OSError:
            out.update(complete=False, why="unreadable directory: %s" % current)
            continue
    if budget is not None and budget.expired():
        out.update(complete=False, why="time budget of the pass reached")
        return out
    out["latest"] = latest
    return out


def tracked(worktree: str, artifact: str, budget=None) -> str:
    """Why `artifact` is not regenerable as far as git knows ('' if it is):
    a file tracked by git inside, or git unable to say.

    `budget` (#3802): a `PassBudget`/window; the git call takes only what is
    left, and is not started when there is nothing left."""
    rel = os.path.relpath(artifact, worktree)
    timeout = GIT_TIMEOUT
    if budget is not None:
        timeout = budget.timeout(timeout)
        if timeout <= 0.0:
            return "pass budget reached: git not asked"
    try:
        proc = subprocess.run(["git", "-C", worktree, "--no-optional-locks", "ls-files", "-z",
                               "--", rel], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return "git ls-files impossible (%s)" % type(exc).__name__
    if proc.returncode != 0:
        return "git ls-files failed: %s" % proc.stderr.decode("utf-8", "replace").strip()[:200]
    files = [f for f in proc.stdout.split(b"\0") if f]
    if files:
        return "%d file(s) tracked by git inside (%s)" % (
            len(files), files[0].decode("utf-8", "replace"))
    return ""


def size_of(path: str, limit: int = SIZE_LIMIT,
            budget: Budget | None = None) -> tuple[int, bool]:
    """Bytes used under `path`, without following links nor leaving the
    filesystem; and whether all of it was counted (the entry limit and the
    deadline are checked at every entry)."""
    try:
        device = os.lstat(path).st_dev
    except OSError:
        return 0, False
    total, count, stack = 0, 0, [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    count += 1
                    if count > limit or (budget is not None and budget.expired()):
                        return total, False
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += _blocks(st)
                    if stat.S_ISDIR(st.st_mode) and st.st_dev == device:
                        stack.append(entry.path)
        except OSError:
            continue
    return total, True


def _blocks(st) -> int:
    return st.st_blocks * 512 if getattr(st, "st_blocks", 0) else st.st_size


# --------------------------------------------------------------------------
# deletion through file descriptors
# --------------------------------------------------------------------------

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) \
    | getattr(os, "O_CLOEXEC", 0)


def _open_dir(name: str, dir_fd: int | None = None) -> int:
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def open_checked(path: str, ident) -> int:
    """A descriptor of the directory `path` (a real path), refused
    (`ValueError`) unless it is still the planned one: same (device, inode)
    as `ident`, and the kernel's path of the descriptor is `path` (an
    ancestor swapped for a link after the plan redirects the open)."""
    fd = _open_dir(path)
    try:
        st = os.fstat(fd)
        if ident is None or (st.st_dev, st.st_ino) != tuple(ident):
            raise ValueError("%s is no longer the surveyed directory" % path)
        try:
            where = platform.fd_path(fd)
        except (platform.NotAvailable, OSError) as exc:
            raise ValueError("%s: path of its descriptor unknown (%s)" % (path, exc)) from None
        if where != path:
            raise ValueError("%s now resolves to %s" % (path, where))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_parent(worktree: str, artifact: str, root: tuple) -> tuple[int, str]:
    """A descriptor of the directory that holds `artifact`, reached from the
    worktree root one component at a time without following any link, on the
    worktree's filesystem; and the artifact's name in it. `root`: (real
    path, identity) of the worktree as surveyed (`open_checked`)."""
    rel = os.path.relpath(artifact, worktree)
    parts = rel.split(os.sep)
    if os.path.isabs(rel) or any(p in ("", ".", "..") for p in parts):
        raise ValueError("%s is outside %s" % (artifact, worktree))
    fd = open_checked(*root)
    try:
        device = os.fstat(fd).st_dev
        for part in parts[:-1]:
            nxt = _open_dir(part, dir_fd=fd)
            os.close(fd)
            fd = nxt
            if os.fstat(fd).st_dev != device:
                raise ValueError("%s crosses a filesystem" % artifact)
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def _delete_tree(parent_fd: int, name: str, *, avoid: set, budget: Budget | None,
                 gitlink: bool = False, clone: bool = False) -> dict:
    """Deletes the directory `name` of `parent_fd` through descriptors: links
    are unlinked, never followed; a subtree on another filesystem, holding a
    `.git`, or one of `avoid` ((dev, ino) of the managed roots) is left in
    place; a directory of ours without write permission is opened up.
    `gitlink`: `name` is a removed worktree, whose own `.git` FILE (a link to
    its repository) goes with it; a `.git` below it still keeps its subtree.
    `clone`: `name` is an ad-hoc clone whose own `.git` DIRECTORY goes with
    it (#2743); a `.git` below it still keeps its subtree.
    `bytes` freed, `errors`, `left` (subtrees left on purpose), `stopped`
    (time budget)."""
    out: dict = {"bytes": 0, "errors": [], "left": [], "stopped": False}
    uid = os.getuid()
    device = os.fstat(parent_fd).st_dev

    def opened(pfd: int, entry: str, rel: str) -> int | None:
        try:
            return _open_dir(entry, dir_fd=pfd)
        except PermissionError:
            try:
                st = os.stat(entry, dir_fd=pfd, follow_symlinks=False)
                if stat.S_ISDIR(st.st_mode) and st.st_uid == uid:
                    os.chmod(entry, stat.S_IMODE(st.st_mode) | 0o700, dir_fd=pfd)
                    return _open_dir(entry, dir_fd=pfd)
            except OSError:
                pass
            out["errors"].append("%s: permission denied" % rel)
        except OSError as exc:
            out["errors"].append("%s: %s" % (rel, exc.strerror or exc))
        return None

    def remove(pfd: int, entry: str, rel: str, top: bool = False) -> bool:
        fd = opened(pfd, entry, rel)
        if fd is None:
            return False
        empty = True
        try:
            st = os.fstat(fd)
            if st.st_dev != device:
                out["left"].append("%s (another filesystem)" % rel)
                return False
            if (st.st_dev, st.st_ino) in avoid:
                out["left"].append("%s (managed root)" % rel)
                return False
            names = os.listdir(fd)
            allow_git = False
            if top and ".git" in names:
                try:
                    mode = os.stat(".git", dir_fd=fd, follow_symlinks=False).st_mode
                except OSError:
                    mode = 0
                # a removed worktree has a `.git` FILE; an ad-hoc clone
                # (#2743) has a `.git` DIRECTORY. A `.git` below either one
                # still keeps its subtree.
                allow_git = (gitlink and stat.S_ISREG(mode)) or (
                    clone and stat.S_ISDIR(mode))
            if ".git" in names and not allow_git:
                out["left"].append("%s (holds a .git)" % rel)
                return False
            if st.st_uid == uid and (st.st_mode & 0o700) != 0o700:
                os.fchmod(fd, stat.S_IMODE(st.st_mode) | 0o700)
            for child in names:
                if budget is not None and budget.expired():
                    out["stopped"] = True
                    return False
                try:
                    cst = os.stat(child, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    out["errors"].append("%s/%s: %s" % (rel, child, exc.strerror or exc))
                    empty = False
                    continue
                if stat.S_ISDIR(cst.st_mode):
                    if not remove(fd, child, "%s/%s" % (rel, child)):
                        empty = False
                    continue
                try:
                    os.unlink(child, dir_fd=fd)
                    out["bytes"] += _blocks(cst)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    out["errors"].append("%s/%s: %s" % (rel, child, exc.strerror or exc))
                    empty = False
        except RecursionError:
            out["errors"].append("%s: too deep" % rel)
            return False
        except OSError as exc:
            out["errors"].append("%s: %s" % (rel, exc.strerror or exc))
            return False
        finally:
            os.close(fd)
        if not empty:
            return False
        try:
            st = os.stat(entry, dir_fd=pfd, follow_symlinks=False)
            os.rmdir(entry, dir_fd=pfd)
            out["bytes"] += _blocks(st)
            return True
        except OSError as exc:
            out["errors"].append("%s: %s" % (rel, exc.strerror or exc))
            return False

    remove(parent_fd, name, name, top=True)
    return out


def _to_trash(pfd: int, name: str, journal: Journal, **about) -> dict:
    """Renames the directory `name` of `pfd` to a fresh trash name, recorded
    in the journal first; the record."""
    st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
    pst = os.fstat(pfd)
    try:
        parent = platform.fd_path(pfd)
    except (platform.NotAvailable, OSError) as exc:
        raise ValueError("path of the parent unknown (%s)" % exc) from None
    trash = trash_name()
    record = dict(about, trash=os.path.join(parent, trash), parent=parent,
                  parent_id=[pst.st_dev, pst.st_ino], id=[st.st_dev, st.st_ino],
                  pid=os.getpid(), ts=time.time())
    journal.add(record)
    try:
        os.rename(name, trash, src_dir_fd=pfd, dst_dir_fd=pfd)
    except BaseException:
        journal.drop(record["trash"])
        raise
    return record


def _finish_at(pfd: int, record: dict, journal: Journal, *, avoid, budget) -> dict:
    name = os.path.basename(record["trash"])
    empty = {"bytes": 0, "errors": [], "left": [], "stopped": False}
    try:
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
    except FileNotFoundError:
        journal.drop(record["trash"])
        return dict(empty, trash=record["trash"])
    if not stat.S_ISDIR(st.st_mode) or [st.st_dev, st.st_ino] != list(record.get("id") or ()):
        journal.drop(record["trash"])
        raise ValueError("%s is not the directory renamed by the pass: left alone"
                         % record["trash"])
    kind = record.get("kind")
    out = _delete_tree(pfd, name, avoid=avoid, budget=budget,
                       gitlink=kind == "worktree", clone=kind == "clone")
    if not (out["stopped"] or out["errors"]):
        journal.drop(record["trash"])   # what is left on purpose stays, never resumed
    out["trash"] = record["trash"]
    return out


def finish_trash(record: dict, journal: Journal, *, avoid: set = frozenset(),
                 budget: Budget | None = None) -> dict:
    """Deletes a trash recorded in the journal (an interrupted pass): its
    parent reached by `open_checked`, the trash still the renamed directory
    (same inode). Report of `_delete_tree`, plus `trash`. `ValueError` if
    it is not ours any more (the record is dropped: left alone)."""
    try:
        pfd = open_checked(record["parent"], record.get("parent_id"))
    except FileNotFoundError:
        journal.drop(record["trash"])
        return {"bytes": 0, "errors": [], "left": [], "stopped": False,
                "trash": record["trash"]}
    except ValueError:
        journal.drop(record["trash"])
        raise
    try:
        return _finish_at(pfd, record, journal, avoid=avoid, budget=budget)
    finally:
        os.close(pfd)


def remove_artifact(artifact: str, worktree: str, *, root: tuple, journal: Journal,
                    rules=(), avoid: set = frozenset(), budget: Budget | None = None,
                    check=lambda: "") -> dict:
    """Deletes the artifact directory `artifact` of `worktree`.

    Through descriptors from the worktree root (`_open_parent`, `root`: its
    real path and identity as surveyed): refused (`ValueError`) for a link,
    a non-directory, a path outside the worktree or crossing a filesystem,
    a root that is no longer the surveyed one, an artifact whose rule no
    longer holds (its `Cargo.toml` gone). `check()` is called last, right
    before the rename, and refuses with its reason (a process came in). The
    artifact is renamed to a fresh trash name recorded in the journal
    (`_to_trash`), then deleted. Returns the report of `_delete_tree`, plus
    `trash`."""
    pfd, name = _open_parent(worktree, artifact, root)
    try:
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or st.st_dev != os.fstat(pfd).st_dev:
            raise ValueError("%s is not a directory of the worktree" % artifact)
        rule = next((r for r in rules if r.name == name), None)
        if rule is None:
            raise ValueError("%s matches no rule" % artifact)
        if rule.beside:
            try:
                beside = os.stat(rule.beside, dir_fd=pfd, follow_symlinks=False)
            except OSError:
                beside = None
            if beside is None or not stat.S_ISREG(beside.st_mode):
                raise ValueError("%s: no %s beside it any more" % (artifact, rule.beside))
        refusal = check()
        if refusal:
            raise ValueError(refusal)
        record = _to_trash(pfd, name, journal, kind="artifact", origin=artifact,
                           worktree=worktree)
        return _finish_at(pfd, record, journal, avoid=avoid, budget=budget)
    finally:
        os.close(pfd)


def remove_worktree_dir(worktree: str, root: tuple, parent_id, *, journal: Journal,
                        avoid: set = frozenset(), budget: Budget | None = None,
                        check=lambda: "", kind: str = "worktree") -> dict:
    """A merged worktree that goes (L230): its directory, `root` (real path,
    identity) under a parent of identity `parent_id`, both as planned
    (`open_checked`), is renamed to a fresh trash name recorded in the
    journal right after `check()` (a process came in), then deleted like an
    artifact (its own `.git` file included, a nested `.git` left). The
    caller then lets git forget it (`git worktree prune`). `kind="clone"`
    (#2743) deletes an ad-hoc clone whose own `.git` DIRECTORY goes too.
    `ValueError` if refused; the report of `_delete_tree`, plus `trash`."""
    real, ident = root
    parent, name = os.path.split(real)
    pfd = open_checked(parent, parent_id)
    try:
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != tuple(ident):
            raise ValueError("%s is no longer the planned worktree" % worktree)
        refusal = check()
        if refusal:
            raise ValueError(refusal)
        record = _to_trash(pfd, name, journal, kind=kind, origin=worktree,
                           worktree=worktree)
        return _finish_at(pfd, record, journal, avoid=avoid, budget=budget)
    finally:
        os.close(pfd)


def gently(fn, *args, **kwargs):
    """Runs `fn` in a thread at the lowest CPU priority and the idle IO class;
    its result (or its exception)."""
    box: dict = {}

    def work() -> None:
        tid = threading.get_native_id()
        try:
            os.setpriority(os.PRIO_PROCESS, tid, 19)   # Linux: this thread only
        except (AttributeError, OSError):
            pass
        if shutil.which("ionice"):
            try:
                subprocess.run(["ionice", "-c", "3", "-p", str(tid)], capture_output=True,
                               timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # handed back to the caller
            box["error"] = exc

    thread = threading.Thread(target=work, name="menage-artifacts", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

def worktrees(seeds, list_worktrees) -> list[dict]:
    """Non-main, non-bare worktrees of the repositories of `seeds` (agents'
    working directories, tracked worktrees), each repository listed once."""
    seen: set[str] = set()
    out: list[dict] = []
    for seed in seeds:
        if not seed or not os.path.isdir(seed) or os.path.realpath(seed) in seen:
            continue
        listed = list_worktrees(seed)
        for i, wt in enumerate(listed):
            real = os.path.realpath(wt["path"])
            if real in seen:
                continue
            seen.add(real)
            if i > 0 and not wt.get("bare") and os.path.isdir(wt["path"]):
                out.append(dict(wt, main=listed[0]["path"]))
    return out


def due(now: float) -> bool:
    """For the runner: has the last scan been long enough ago?"""
    return now - _last_scan.get("at", 0.0) >= SCAN_EVERY_S


def _fmt_age(seconds: float) -> str:
    return "%d h" % (seconds // 3600) if seconds >= 3600 else "%d min" % (seconds // 60)


def _lock(path: str):
    """One deleting pass at a time on the host (runner and CLI): an
    exclusive `flock`, or None if another pass holds it."""
    import fcntl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _identities(paths) -> set:
    out = set()
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.add((st.st_dev, st.st_ino))
    return out


def run(candidates: list[dict], rules, *, idle_s: float, protected, skip_roots=(),
        ended_idle_s: float | None = None, ended_of=lambda worktree: False,
        owner_of=lambda path: None, keep=lambda worktree: "", dry_run: bool, now: float,
        allow=(), budget_s: float = PASS_BUDGET_S, clock=time.monotonic,
        lock_path: str | None = None, journal_path: str | None = None) -> dict:
    """Plans, and unless `dry_run` deletes, the artifacts of idle worktrees,
    and first finishes the trash dirs of the journal.

    `candidates`: from `worktrees()`; `protected`: agents' working
    directories (a worktree that CONTAINS one is never touched; such a
    directory itself is guarded by `keep()` and the process check, so its
    artifacts can go — #3956); `skip_roots`: managed roots, never entered nor
    deleted; `ended_idle_s`: shorter inactivity for a worktree whose cited
    lots are all finished (`ended_of`, #3956), None to use `idle_s` only;
    those worktrees are scanned first, so a pass cut by its budget frees them
    before the others;
    `owner_of(path)`: the agent a worktree is charged to; `keep(candidate)`:
    why an idle worktree keeps its artifacts all the same ('' if nothing:
    #3195, an open lot — recorded, or named by its branch or directory —,
    a turn in progress); `allow`: names of unreadable processes that do
    not block (policy `unreadable_ok`); `lock_path`: the host-wide lock of
    deleting passes; `journal_path`: the journal of the trash dirs
    (`Journal`), without which nothing is deleted. A doubt on the processes
    deletes nothing; the dry run still lists the plan (#3195). Returns
    `entries` (to log), `plan`, `skipped` (reasons)."""
    entries: list[dict] = []
    plan: list[dict] = []
    skipped: list[str] = []
    result = {"entries": entries, "plan": plan, "skipped": skipped}
    _last_scan["at"] = now
    journal = Journal(journal_path) if journal_path else None
    try:
        pending = journal.records() if journal is not None else []
    except OSError as exc:
        skipped.append("artifacts: journal unreadable (%s), nothing deleted"
                       % (exc.strerror or exc))
        return result
    if not pending and (not rules or not candidates):
        return result
    if not dry_run and journal is None:
        skipped.append("artifacts: no journal of the trash dirs, nothing deleted")
        return result
    lock = None
    if not dry_run and lock_path:
        lock = _lock(lock_path)
        if lock is None:
            skipped.append("artifacts: another pass is deleting on this host, nothing done")
            return result
    try:
        budget = Budget(budget_s, clock)
        scan = platform.user_paths(allow=allow)
        doubt = scan.doubt()
        if doubt and not dry_run:
            skipped.append("artifacts: nothing deleted, %s" % doubt)
            return result
        if doubt:
            # #3195: the dry run still lists what would go, and why --apply will not
            skipped.append("artifacts: --apply would delete nothing, %s" % doubt)
        if scan.allowed:
            skipped.append("artifacts: unreadable process(es) let through by unreadable_ok: %s"
                           % ", ".join(scan.allowed))
        avoid = _identities(os.path.realpath(p) for p in skip_roots if p)
        _resume(pending, journal, result, scan=scan, avoid=avoid, owner_of=owner_of,
                dry_run=dry_run, budget=budget)
        if rules and candidates:
            if ended_idle_s is not None:
                # #3956: a pass cut by its budget must free the finished lots first
                candidates = _ended_first(candidates, ended_of)
            _run(candidates, rules, result, idle_s=idle_s, protected=protected,
                 ended_idle_s=ended_idle_s, ended_of=ended_of,
                 skip_roots=skip_roots, owner_of=owner_of, keep=keep, dry_run=dry_run, now=now,
                 allow=allow, scan=scan, avoid=avoid, journal=journal,
                 skip_ids=[tuple(r["id"]) for r in pending if r.get("id")], budget=budget)
    finally:
        if lock is not None:
            lock.close()
    return result


def _resume(pending, journal, result, *, scan, avoid, owner_of, dry_run, budget) -> None:
    """The trash dirs of the journal (an interrupted pass): ours, they go
    whatever the activity, unless a process is inside."""
    entries, plan, skipped = result["entries"], result["plan"], result["skipped"]
    for record in pending:
        trash, origin = record["trash"], record.get("origin") or record["trash"]
        base = {"kind": "artifact", "agent": owner_of(record.get("worktree") or origin)}
        why = "trash of an interrupted pass (%s)" % trash
        held = scan.holders(trash)
        if held:
            skipped.append("%s kept: process inside (%s)" % (trash, ", ".join(held[:3])))
            continue
        if budget.expired():
            plan.append(dict(base, path=origin, action="remis",
                             detail="%s ; time budget of the pass reached" % why))
            continue
        if dry_run:
            size, whole = size_of(trash, budget=budget)
            plan.append(dict(base, path=origin, action="suppression", bytes=size,
                             detail=why if whole else "%s ; size counted in part" % why))
            continue
        try:
            done = finish_trash(record, journal, avoid=avoid, budget=budget)
        except (OSError, ValueError) as exc:
            skipped.append("%s kept: %s" % (trash, exc))
            continue
        _log(result, base, origin, why, done, record.get("worktree") or "", "trash",
             scan.allowed)


def _log(result, base, path, why, done, worktree, rule, allowed) -> None:
    complete = not (done["errors"] or done["left"] or done["stopped"])
    notes = done["left"][:3] + done["errors"][:3]
    if done["stopped"]:
        notes.append("time budget reached, %s finished by the next pass" % done["trash"])
    detail = "%s ; regenerable build output" % why if complete else \
        "%s ; partial: %s" % (why, " ; ".join(notes))
    result["plan"].append(dict(base, path=path, action="suppression" if complete else "partiel",
                               bytes=done["bytes"], detail=detail))
    result["entries"].append(dict(base, path=path, action="deleted" if complete else "partial",
                                  bytes=done["bytes"], detail=detail,
                                  data={"worktree": worktree, "trash": done["trash"],
                                        "errors": done["errors"], "left": done["left"],
                                        "unreadable_allowed": list(allowed), "rule": rule}))


def _ended_first(candidates: list[dict], ended_of) -> list[dict]:
    """#3956: the worktrees of finished lots first, then the others by path.

    A pass is cut by its time budget (`PASS_BUDGET_S`, and the host pass
    shares its own): keeping the candidates in their discovery order let the
    tail wait for ever. Nothing here raises: an unreadable lot state only
    leaves the worktree in the second group."""
    def key(wt: dict):
        try:
            finished = bool(ended_of(wt))
        except Exception:  # noqa: BLE001 - an unreadable state never reorders nor fails
            finished = False
        return (0 if finished else 1, str(wt.get("path") or ""))
    return sorted(candidates, key=key)


def _run(candidates, rules, result, *, idle_s, protected, skip_roots, ended_idle_s,
         ended_of, owner_of, keep, dry_run,
         now, allow, scan, avoid, journal, skip_ids, budget: Budget) -> None:
    plan, skipped = result["plan"], result["skipped"]
    protected = [os.path.realpath(p) for p in protected if p]
    roots = [os.path.realpath(p) for p in skip_roots if p]
    recent = 0
    kept: list[str] = []
    for wt in candidates:
        if budget.expired():
            skipped.append("artifacts: time budget reached, the remaining worktrees wait for "
                           "the next pass")
            break
        path = wt["path"]
        real = os.path.realpath(path)
        # #3956 : `p != real` — le dossier de travail d'un agent n'est plus
        # exempté par son seul statut ; un worktree qui en CONTIENT un reste
        # intouchable (le ménage ne juge pas les artefacts d'un autre dossier).
        if wt.get("locked") or any(p != real and p.startswith(real.rstrip(os.sep) + os.sep)
                                   for p in protected):
            continue
        found = survey(path, rules, skip_roots=skip_roots, skip_ids=skip_ids, budget=budget)
        if budget.expired():
            skipped.append("artifacts: time budget reached, the remaining worktrees wait for "
                           "the next pass")
            break
        if not found["artifacts"]:
            continue
        agent = owner_of(path)
        base = {"kind": "artifact", "agent": agent}
        if not found["complete"]:
            skipped.append("artifacts of %s kept: %s" % (path, found["why"]))
            continue
        idle = now - float(found["latest"])
        try:
            finished = bool(ended_idle_s is not None and ended_of(wt))
        except Exception:  # noqa: BLE001 - un état illisible ne raccourcit rien
            finished = False
        limit = ended_idle_s if finished and ended_idle_s is not None else idle_s
        if idle < limit:
            recent += 1
            continue
        try:
            reason = keep(wt)
        except Exception as exc:  # noqa: BLE001 - nothing proved: kept
            reason = "open lot not ruled out (%s)" % type(exc).__name__
        if reason:
            kept.append("%s: %s" % (path, reason))
            continue
        held = scan.holders(path)
        if held:
            skipped.append("artifacts of %s kept: process inside (%s)" % (path, ", ".join(held[:3])))
            continue
        why = "worktree %s idle for %s (%s)" % (
            path, _fmt_age(idle),
            "lots finished" if finished and limit == ended_idle_s else "no open lot")
        for artifact in found["artifacts"]:
            if budget.expired():
                plan.append(dict(base, path=artifact, action="remis",
                                 detail="%s ; time budget of the pass reached" % why))
                continue
            inside = [r for r in roots if r == os.path.realpath(artifact)
                      or r.startswith(os.path.realpath(artifact).rstrip(os.sep) + os.sep)]
            refusal = ("holds a managed root (%s)" % inside[0]) if inside else \
                tracked(path, artifact)
            if refusal:
                skipped.append("artifact %s kept: %s" % (artifact, refusal))
                continue
            if dry_run:
                size, whole = size_of(artifact, budget=budget)
                plan.append(dict(base, path=artifact, action="suppression", bytes=size,
                                 detail=why if whole else "%s ; size counted in part (entry "
                                                          "limit or time budget)" % why))
                continue
            seen: dict = {}

            def check() -> str:
                # read again right before the rename: a process may have come in
                again = platform.user_paths(allow=allow)
                seen["allowed"] = again.allowed
                held = again.holders(path)
                return again.doubt() or ("process inside (%s)" % ", ".join(held[:3])
                                         if held else "")

            try:
                done = remove_artifact(artifact, path, root=(found["real"], found["identity"]),
                                       journal=journal, rules=rules, avoid=avoid, budget=budget,
                                       check=check)
            except (OSError, ValueError) as exc:
                skipped.append("artifact %s kept: %s" % (artifact, exc))
                if "process inside" in str(exc) or "unreadable" in str(exc):
                    break
                continue
            _log(result, base, artifact, why, done, path,
                 next((str(r) for r in rules if r.name == os.path.basename(artifact)), ""),
                 seen.get("allowed", []))
    if recent:
        skipped.append("artifacts: %d worktree(s) active within %s, kept"
                       % (recent, _fmt_age(idle_s)))
    if kept:
        skipped.append("artifacts: %d idle worktree(s) kept (open lot or turn in progress): %s%s"
                       % (len(kept), " ; ".join(kept[:5]), " ; …" if len(kept) > 5 else ""))
    left = sum(1 for p in plan if p["action"] == "remis")
    if left:
        skipped.append("artifacts: time budget reached, %d left for the next pass" % left)
