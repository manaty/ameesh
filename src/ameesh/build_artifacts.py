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
  its artifacts, and of its git index and HEAD) for `artifact_idle` (24 h by
  default), and NO process of the user has its cwd, its executable, an open
  file or a mapped file inside it (`/proc`, read again right before each
  deletion). A process of the user that cannot be read leaves a doubt: no
  deletion at all for the pass (the user's service manager excepted:
  `systemd --user` is not dumpable and lives in `/`).
* **Never**: the main checkout of a repository, an agent's working directory
  or a directory that contains one, a locked worktree (`git worktree lock`),
  a worktree whose walk is incomplete (unreadable entry, mount point, too many
  entries).
* **How**: one worktree at a time, in a thread at the lowest CPU priority and
  the idle IO class (`ionice -c 3` when available), within a time budget per
  pass (the rest waits for the next pass). Deleting frees space: under
  pressure the pass keeps going, serialised. Each deletion is logged
  (`housekeeping_log`, kind `artifact`) with the bytes freed; the dry run lists
  what would go and its size.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field

#: default rules: a directory name, optionally `:<file>` that must sit beside it
DEFAULT_RULES = ("node_modules", "target:Cargo.toml")
DEFAULT_IDLE_S = 24 * 3600.0
#: the walk of a worktree (outside its artifacts) stops being trusted beyond this
WALK_LIMIT = 300_000
#: sizes of artifacts are counted up to this many files
SIZE_LIMIT = 5_000_000
#: deletions stop after this long; the rest waits for the next pass
PASS_BUDGET_S = 600.0
#: the runner scans the worktrees at most this often (idleness is counted in days)
SCAN_EVERY_S = 6 * 3600.0
#: processes of the user that are never readable and never inside a worktree:
#: the user's service manager (not dumpable, cwd `/`)
UNREADABLE_OK = frozenset({"systemd", "(sd-pam)"})

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
    return bool(text) and "/" not in text and text not in (".", "..", ".git")


def parse_rules(value) -> tuple[tuple[Rule, ...] | None, str]:
    """Rules of `policy.housekeeping.artifacts`: a list of `name` or
    `name:file` (the directory only counts with `file` beside it); an empty
    list turns the deletion off. (rules, problem)."""
    if not isinstance(value, (list, tuple)):
        return None, "list of directory names expected (e.g. [node_modules, target:Cargo.toml])"
    rules = []
    for item in value:
        name, _, beside = str(item).strip().partition(":")
        if not isinstance(item, str) or not _plain_name(name) or (beside and not _plain_name(beside)):
            return None, "%r: a directory name, optionally `:<file beside it>`" % (item,)
        rules.append(Rule(name, beside))
    return tuple(rules), ""


def rules_of(declared) -> tuple[Rule, ...]:
    rules, _problem = parse_rules(list(DEFAULT_RULES) if declared is None else declared)
    return rules or ()


# --------------------------------------------------------------------------
# processes of the user (`/proc`)
# --------------------------------------------------------------------------

@dataclass
class ProcScan:
    """Where the user's processes are: cwd, executable, open and mapped files."""
    paths: dict = field(default_factory=dict)        #: real path -> "pid comm"
    unreadable: list = field(default_factory=list)   #: "pid comm"
    available: bool = True

    def doubt(self) -> str:
        """Why nothing can be judged free of processes, or ''."""
        if not self.available:
            return "/proc unreadable: processes inside the worktrees unknown"
        if self.unreadable:
            return "process(es) of the user unreadable: %s" % ", ".join(self.unreadable[:5])
        return ""

    def holders(self, root: str) -> list[str]:
        root = os.path.realpath(root).rstrip(os.sep)
        prefix = root + os.sep
        return sorted({who for path, who in self.paths.items()
                       if path == root or path.startswith(prefix)})


def _comm(base: str) -> str:
    try:
        with open(os.path.join(base, "comm"), encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return "?"


def _target(link: str) -> str:
    target = os.readlink(link)
    if target.endswith(" (deleted)"):
        target = target[:-len(" (deleted)")]
    return target if target.startswith("/") else ""


def scan_processes(proc: str = "/proc", uid: int | None = None) -> ProcScan:
    """Snapshot of the user's processes. A process gone meanwhile is ignored;
    one that cannot be read is listed in `unreadable` (a doubt), unless it is
    the user's service manager (`UNREADABLE_OK`)."""
    uid = os.getuid() if uid is None else uid
    out = ProcScan()
    try:
        pids = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        out.available = False
        return out
    for pid in pids:
        base = os.path.join(proc, pid)
        try:
            if os.lstat(base).st_uid != uid:
                continue
        except OSError:
            continue
        who = "%s %s" % (pid, _comm(base))
        found: list[str] = []
        try:
            for name in ("cwd", "exe", "root"):
                try:
                    found.append(_target(os.path.join(base, name)))
                except FileNotFoundError:
                    if name == "cwd":
                        raise
            for fd in os.listdir(os.path.join(base, "fd")):
                try:
                    found.append(_target(os.path.join(base, "fd", fd)))
                except FileNotFoundError:
                    continue
            with open(os.path.join(base, "maps"), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    parts = line.split(None, 5)
                    if len(parts) == 6 and parts[5].startswith("/"):
                        found.append(parts[5].rstrip("\n").removesuffix(" (deleted)"))
        except (FileNotFoundError, ProcessLookupError):
            continue   # gone meanwhile
        except OSError:
            if _comm(base) not in UNREADABLE_OK:
                out.unreadable.append(who)
            continue
        for path in found:
            # `root` is `/` for every process: it only tells a chroot apart
            if path and path != "/":
                out.paths.setdefault(os.path.realpath(path), who)
    return out


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


def survey(worktree: str, rules, *, skip_roots=(), limit: int = WALK_LIMIT) -> dict:
    """Walk of a worktree outside its artifacts: `latest` activity (mtime or
    ctime, git index and HEAD included), `artifacts` (directories matched by
    the rules, not entered), `complete`, `why`.

    Not entered: `.git`, a nested checkout (a directory with a `.git`), a
    symbolic link, another filesystem (incomplete), `skip_roots`."""
    out: dict = {"latest": None, "artifacts": [], "complete": True, "why": ""}
    try:
        top = os.lstat(worktree)
    except OSError as exc:
        out.update(complete=False, why="unreadable (%s)" % (exc.strerror or exc))
        return out
    latest = max(top.st_mtime, top.st_ctime)
    gitdir = _gitdir(worktree)
    for name in ("index", "HEAD", os.path.join("logs", "HEAD")):
        try:
            st = os.stat(os.path.join(gitdir, name)) if gitdir else None
        except OSError:
            st = None
        if st is not None:
            latest = max(latest, st.st_mtime, st.st_ctime)
    skip = {os.path.realpath(p) for p in skip_roots if p}
    count = 0
    stack = [worktree]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            out.update(complete=False, why="unreadable directory: %s" % current)
            continue
        for entry in entries:
            count += 1
            if count > limit:
                out.update(complete=False, latest=latest,
                           why="more than %d entries: activity not verifiable" % limit)
                return out
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                out.update(complete=False, why="unreadable entry: %s" % entry.path)
                continue
            is_dir = stat.S_ISDIR(st.st_mode)
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
    out["latest"] = latest
    return out


def size_of(path: str, limit: int = SIZE_LIMIT) -> int:
    """Bytes used under `path`, without following links nor leaving the filesystem."""
    try:
        device = os.lstat(path).st_dev
    except OSError:
        return 0
    total, count, stack = 0, 0, [path]
    while stack and count < limit:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    count += 1
                    total += st.st_blocks * 512 if st.st_blocks else st.st_size
                    if stat.S_ISDIR(st.st_mode) and st.st_dev == device:
                        stack.append(entry.path)
        except OSError:
            continue
    return total


def remove_artifact(path: str, worktree: str) -> int:
    """Deletes the artifact directory `path` of `worktree`; bytes freed.

    Refused (`ValueError`) unless `path` is a real directory (not a link)
    strictly inside `worktree`. A read-only directory of ours inside is made
    writable to finish the job; links inside are removed, never followed."""
    real_wt = os.path.realpath(worktree).rstrip(os.sep)
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    if not (parent == real_wt or parent.startswith(real_wt + os.sep)):
        raise ValueError("%s is outside %s" % (path, worktree))
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ValueError("%s is not a directory" % path)
    before = size_of(path)
    uid = os.getuid()

    def retry(func, failed, _exc_info):
        directory = os.path.dirname(failed)
        try:
            dst = os.lstat(directory)
            if stat.S_ISDIR(dst.st_mode) and dst.st_uid == uid:
                os.chmod(directory, stat.S_IMODE(dst.st_mode) | 0o700)
            func(failed)
        except OSError:
            pass

    shutil.rmtree(path, onerror=retry)
    return before - (size_of(path) if os.path.lexists(path) else 0)


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


def run(candidates: list[dict], rules, *, idle_s: float, protected, skip_roots=(),
        owner_of=lambda path: None, dry_run: bool, now: float, proc: str = "/proc",
        budget_s: float = PASS_BUDGET_S, clock=time.monotonic) -> dict:
    """Plans, and unless `dry_run` deletes, the artifacts of idle worktrees.

    `candidates`: from `worktrees()`; `protected`: agents' working
    directories (a worktree that is or contains one is never touched);
    `owner_of(path)`: the agent a worktree is charged to. Returns `entries`
    (deletions to log), `plan`, `skipped` (reasons, one per worktree held)."""
    entries: list[dict] = []
    plan: list[dict] = []
    skipped: list[str] = []
    _last_scan["at"] = now
    if not rules or not candidates:
        return {"entries": entries, "plan": plan, "skipped": skipped}
    scan = scan_processes(proc)
    doubt = scan.doubt()
    if doubt:
        skipped.append("artifacts: nothing deleted, %s" % doubt)
        return {"entries": entries, "plan": plan, "skipped": skipped}
    protected = [os.path.realpath(p) for p in protected if p]
    deadline = clock() + budget_s
    recent = 0
    for wt in candidates:
        path = wt["path"]
        real = os.path.realpath(path)
        if wt.get("locked") or any(p == real or p.startswith(real.rstrip(os.sep) + os.sep)
                                   for p in protected):
            continue
        found = survey(path, rules, skip_roots=skip_roots)
        if not found["artifacts"]:
            continue
        agent = owner_of(path)
        base = {"kind": "artifact", "agent": agent}
        if not found["complete"]:
            skipped.append("artifacts of %s kept: %s" % (path, found["why"]))
            continue
        idle = now - float(found["latest"])
        if idle < idle_s:
            recent += 1
            continue
        held = scan.holders(path)
        if held:
            skipped.append("artifacts of %s kept: process inside (%s)" % (path, ", ".join(held[:3])))
            continue
        why = "worktree %s idle for %s" % (path, _fmt_age(idle))
        for artifact in found["artifacts"]:
            if clock() > deadline:
                plan.append(dict(base, path=artifact, action="remis",
                                 detail="%s ; time budget of the pass reached" % why))
                continue
            if dry_run:
                plan.append(dict(base, path=artifact, action="suppression",
                                 bytes=size_of(artifact), detail=why))
                continue
            # read again right before deleting: a process may have come in
            again = scan_processes(proc)
            held = again.holders(path)
            if again.doubt() or held:
                skipped.append("artifacts of %s kept: %s" % (
                    path, again.doubt() or "process inside (%s)" % ", ".join(held[:3])))
                break
            try:
                freed = remove_artifact(artifact, path)
            except (OSError, ValueError) as exc:
                skipped.append("artifact %s kept: %s" % (artifact, exc))
                continue
            plan.append(dict(base, path=artifact, action="suppression", bytes=freed, detail=why))
            entries.append(dict(base, path=artifact, action="deleted", bytes=freed,
                                detail="%s ; regenerable build output" % why,
                                data={"worktree": path, "rule": next(
                                    (str(r) for r in rules
                                     if r.matches(os.path.dirname(artifact),
                                                  os.path.basename(artifact))), "")}))
    if recent:
        skipped.append("artifacts: %d worktree(s) active within %s, kept"
                       % (recent, _fmt_age(idle_s)))
    left = sum(1 for p in plan if p["action"] == "remis")
    if left:
        skipped.append("artifacts: time budget reached, %d left for the next pass" % left)
    return {"entries": entries, "plan": plan, "skipped": skipped}
