# SPDX-License-Identifier: AGPL-3.0-only
"""#3728 — archive, then removal, of a worktree of an ended lot kept only for
modified or untracked files (#3734: or for ignored files only).

The housekeeping keeps such a worktree for good (`kept — N file(s) modified
or untracked`): 137 on ameesh-agents-2 and 147 on ameesh-agents-1 on
26/10/2026, most of them of lots merged days before, and nothing ever
removed them. Past `policy.housekeeping.dirty_worktree_after` (a duration of
the Host card, compiled from the canon; undeclared: nothing is archived),
the pass archives what removing it would lose, then removes it like a kept
merged worktree (`housekeeping._remove_kept_worktree`):

- `staged.patch` (`git diff --cached --binary HEAD`: HEAD → index) and
  `unstaged.patch` (`git diff --binary`: index → working copy), two layers so
  that a file staged then modified again (`MM`) keeps both contents
  (cx2, B/P1 on b5195435: `git diff HEAD` lost the staged one); restored by
  `git apply --index` of the first, then `git apply` of the second, on the
  recorded HEAD; a conflicted index (unmerged entries) is refused;
- `untracked.tar.gz`: the untracked files and the ignored ones, outside the
  regenerable build output (`artifacts` rules: node_modules, target) and the
  cache directories (`EXCLUDED_NAMES`; #3734: a file or a link so named is
  archived); symbolic links stored as links, never followed;
- `manifest.json`: path, branch, HEAD, lot, `git status`, the archived files,
  sizes, and `retain_until_ts` (`policy.housekeeping.dirty_archive_retention`).

Archives live under `<state>/.menage/worktree-archives/` (mode 0700: an
ignored `.env` may hold a secret), are written under a `.partial-` name then
renamed, and go once their `retain_until_ts` is past (`prune`). The guards of
the removal (lot ended, no commit missing from the forge, no process, no git
lock, no turn in progress, idle) are the caller's; this module refuses
(`ArchiveRefused`) whatever it cannot archive whole: in doubt, the worktree
stays."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import time

#: directory of the archives, under the managed directory of the state
ARCHIVE_DIR = "worktree-archives"
#: prefix of an archive being written (deleted by `prune` once old)
PARTIAL_PREFIX = ".partial-"
#: an archive directory: `<UTC time>-lot<id>-<worktree name>-<random>`
ARCHIVE_RE = re.compile(r"\A\d{8}T\d{6}Z-lot[0-9A-Za-z#]*-.+-[0-9a-f]{8}\Z")
#: `git status` codes of an unmerged (conflicted) index entry
UNMERGED = frozenset(("DD", "AU", "UD", "UA", "DU", "AA", "UU"))
#: caches never archived (regenerable), besides the build output rules
EXCLUDED_NAMES = frozenset((
    ".cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".gradle", ".turbo", ".next", ".parcel-cache", ".venv", ".tox", ".nox"))
#: what one archive may hold (files outside the exclusions); beyond it the
#: worktree is kept and said — an archive is a safety net, not a copy of a
#: build directory nobody declared
MAX_BYTES = 512 * 1024 ** 2
MAX_FILES = 20_000
#: a `.partial-` directory older than this is a crashed pass: deleted
PARTIAL_GRACE_S = 24 * 3600.0
GIT_TIMEOUT = 60.0


class ArchiveRefused(Exception):
    """The worktree cannot be archived whole: it is kept (the message says why)."""


def root_of(state_dir: str, managed_dir: str) -> str:
    return os.path.join(state_dir, managed_dir, ARCHIVE_DIR)


def _git(path: str, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git", "-C", path, "--no-optional-locks", *args],
                              capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArchiveRefused("git %s failed (%s)" % (args[0], exc)) from exc
    if proc.returncode != 0:
        raise ArchiveRefused("git %s failed (%s)" % (
            args[0], proc.stderr.decode("utf-8", "replace").strip()[:200]))
    return proc


def _names(raw: bytes) -> list[str]:
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def status_entries(path: str, *, timeout: float = GIT_TIMEOUT) -> list[str]:
    """`git status --porcelain -z` as `XY path` lines; a rename or copy
    carries its origin (`XY path <- origin`), the next field of `-z`."""
    fields = _names(_git(path, "status", "--porcelain=v1", "-z",
                         "--untracked-files=normal", timeout=timeout).stdout)
    out: list[str] = []
    it = iter(fields)
    for field in it:
        if field[:1] in ("R", "C") or field[1:2] in ("R", "C"):
            field = "%s <- %s" % (field, next(it, "?"))
        out.append(field)
    return out


def _excluded(top: str, rel: str, rules) -> bool:
    """`rel` (relative to `top`) is inside a cache or a build output dir."""
    parts = rel.rstrip("/").split("/")
    parent = top
    for name in parts:
        here = os.path.join(parent, name)
        # #3734: a cache name excludes a directory only; a file or a link so
        # named (a `.cache` note, a link to a cache) is archived
        if (name in EXCLUDED_NAMES or name.startswith(".ameesh-trash-")) \
                and os.path.isdir(here) and not os.path.islink(here):
            return True
        if any(rule.matches(parent, name) for rule in rules) and os.path.isdir(here):
            return True
        parent = here
    return False


def _walk(top: str, rel: str, rules, out: list) -> None:
    """Files under the ignored directory `rel`, outside the exclusions; a
    symbolic link is one entry, never followed."""
    full = os.path.join(top, rel)
    for current, dirs, files in os.walk(full):
        crel = os.path.relpath(current, top)
        keep = []
        for name in dirs:
            sub = os.path.join(crel, name)
            if os.path.islink(os.path.join(current, name)):
                out.append(sub)
            elif not _excluded(top, sub, rules):
                keep.append(name)
        dirs[:] = keep
        out.extend(os.path.join(crel, name) for name in files
                   if not _excluded(top, os.path.join(crel, name), rules))


def untracked_files(path: str, rules, *, timeout: float = GIT_TIMEOUT) -> list[str]:
    """Untracked and ignored files of the worktree `path`, relative, outside
    the build output `rules` and `EXCLUDED_NAMES`."""
    found = _names(_git(path, "ls-files", "-z", "--others", "--exclude-standard",
                        timeout=timeout).stdout)
    ignored = _names(_git(path, "ls-files", "-z", "--others", "--ignored",
                          "--exclude-standard", "--directory", timeout=timeout).stdout)
    out = [rel for rel in found if not _excluded(path, rel, rules)]
    for rel in ignored:
        if _excluded(path, rel, rules):
            continue
        if rel.endswith("/") and not os.path.islink(os.path.join(path, rel.rstrip("/"))):
            _walk(path, rel.rstrip("/"), rules, out)
        else:
            out.append(rel.rstrip("/"))
    return sorted(set(out))


def _size(path: str, files: list[str]) -> int:
    total = 0
    for rel in files:
        try:
            st = os.lstat(os.path.join(path, rel))
        except OSError as exc:
            raise ArchiveRefused("%s unreadable (%s)" % (rel, exc.strerror or exc)) from exc
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
        elif not stat.S_ISLNK(st.st_mode):
            raise ArchiveRefused("%s is neither a file nor a link" % rel)
    return total


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def archive(path: str, root: str, *, rules=(), lot=None, state: str = "",
            branch: str = "", head: str = "", now: float | None = None,
            retention_s: float, max_bytes: int | None = None,
            max_files: int | None = None, deadline: float | None = None) -> dict:
    """Archives the worktree `path` under `root` (created 0700). Returns
    `archive` (its directory), `bytes` (on disk), `patch_bytes`, `files`
    (untracked archived), `tracked` (tracked entries changed),
    `retain_until_ts`. `ArchiveRefused` when it cannot be whole: nothing is
    left behind (the partial directory is deleted)."""
    now = time.time() if now is None else now
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    max_files = MAX_FILES if max_files is None else max_files

    # #3802 B1 (cx2) : `deadline` est l'échéance monotone du passage, réserve
    # déduite. Ce qui reste à archiver attend le passage suivant : jamais un
    # archivage à moitié (le dossier partiel est retiré par le `except`).
    def _left() -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    def _check() -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise ArchiveRefused("pass budget reached: archive left for the next pass")

    def _timeout() -> float:
        left = _left()
        if left is None:
            return GIT_TIMEOUT
        _check()
        return min(GIT_TIMEOUT, max(0.1, left))

    _check()
    entries = status_entries(path, timeout=_timeout())
    tracked = [e for e in entries if not e.startswith("?? ")]
    unmerged = [e for e in tracked if e[:2] in UNMERGED]
    if unmerged:
        raise ArchiveRefused("unmerged index entries (%s)" % unmerged[0])
    diff = ("diff", "--binary", "--no-ext-diff", "--no-textconv")
    staged = _git(path, *diff, "--cached", "HEAD", timeout=_timeout()).stdout
    unstaged = _git(path, *diff, timeout=_timeout()).stdout
    # every change of a layer must show in that layer's patch: else git says
    # something we do not understand, and the worktree stays
    for layer, column, patch in (("staged", 0, staged), ("unstaged", 1, unstaged)):
        changed = [e for e in tracked if e[column] not in " ?"]
        if changed and not patch:
            raise ArchiveRefused("%s changes absent from their patch (%s)" % (
                layer, changed[0]))
    files = untracked_files(path, rules, timeout=_timeout())
    if len(files) > max_files:
        raise ArchiveRefused("%d untracked file(s) to archive (limit %d)" % (
            len(files), max_files))
    size = _size(path, files)
    # dev3 N1: the patches count too
    if size + len(staged) + len(unstaged) > max_bytes:
        raise ArchiveRefused("%d bytes to archive (limit %d)" % (
            size + len(staged) + len(unstaged), max_bytes))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    label = re.sub(r"[^0-9A-Za-z._-]", "_", os.path.basename(path.rstrip("/")))[:80] or "wt"
    name = "%s-lot%s-%s-%s" % (stamp, re.sub(r"[^0-9A-Za-z]", "", str(lot or "")),
                               label, secrets.token_hex(4))
    partial = os.path.join(root, PARTIAL_PREFIX + name)
    final = os.path.join(root, name)
    # dev3 B1: creating the root or the partial directory may fail (ENOSPC,
    # EACCES, a file in the way): a refusal, never an aborted pass
    _check()
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        os.mkdir(partial, 0o700)
    except OSError as exc:
        raise ArchiveRefused("archive directory not created (%s)" % exc) from exc
    try:
        _write(os.path.join(partial, "staged.patch"), staged)
        _write(os.path.join(partial, "unstaged.patch"), unstaged)
        if files:
            tar_path = os.path.join(partial, "untracked.tar.gz")
            with tarfile.open(tar_path, "w:gz") as tar:
                for rel in files:
                    _check()
                    tar.add(os.path.join(path, rel), arcname=rel, recursive=False)
            with open(tar_path, "rb") as fh:
                os.fsync(fh.fileno())
            with tarfile.open(tar_path, "r:gz") as tar:
                if sorted(m.name for m in tar.getmembers()) != sorted(
                        os.path.normpath(rel) for rel in files):
                    raise ArchiveRefused("untracked.tar.gz does not hold every file")
        manifest = {
            "schema": "ameesh-worktree-archive/1", "path": path, "branch": branch,
            "head": head, "lot": lot, "lot_state": state, "archived_ts": now,
            "retain_until_ts": now + float(retention_s), "status": entries,
            "untracked": files, "untracked_bytes": size,
            "patch_bytes": len(staged) + len(unstaged),
            # dev3 B2: `git apply` refuses an empty patch (exit 128): only
            # the layers that hold something are in the command
            "restore": " && ".join(
                ["git worktree add --detach <path> %s" % (head or "<HEAD>")]
                + (["git -C <path> apply --index --binary %s/staged.patch" % final]
                   if staged else [])
                + (["git -C <path> apply --binary %s/unstaged.patch" % final]
                   if unstaged else [])
                + (["tar -xzf %s/untracked.tar.gz -C <path>" % final] if files else []))}
        _write(os.path.join(partial, "manifest.json"),
               (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode())
        _fsync_dir(partial)
        os.rename(partial, final)
        _fsync_dir(root)
    except BaseException as exc:
        shutil.rmtree(partial, ignore_errors=True)
        if isinstance(exc, ArchiveRefused):
            raise
        if isinstance(exc, (OSError, tarfile.TarError, ValueError)):
            raise ArchiveRefused("archive not written (%s)" % exc) from exc
        raise
    return {"archive": final, "bytes": disk_bytes(final),
            "patch_bytes": len(staged) + len(unstaged),
            "files": len(files), "tracked": len(tracked),
            "retain_until_ts": manifest["retain_until_ts"]}


def disk_bytes(top: str) -> int:
    total = 0
    for current, _dirs, files in os.walk(top):
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_blocks * 512
            except OSError:
                pass
    return total


def _manifest(directory: str) -> dict | None:
    try:
        with open(os.path.join(directory, "manifest.json"), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def survey(root: str) -> dict:
    """`count` and `bytes` of the archives kept under `root`."""
    out = {"root": root, "count": 0, "bytes": 0}
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        full = os.path.join(root, name)
        if ARCHIVE_RE.match(name) and os.path.isdir(full) and not os.path.islink(full):
            out["count"] += 1
            out["bytes"] += disk_bytes(full)
    return out


def prune(root: str, *, now: float | None = None, dry_run: bool = False) -> list[dict]:
    """The archives past their `retain_until_ts`, and partial ones older than
    `PARTIAL_GRACE_S`, deleted (journal lines: kind `archive`, action
    `expired`). An archive without a readable manifest is never deleted."""
    now = time.time() if now is None else now
    out: list[dict] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        full = os.path.join(root, name)
        if os.path.islink(full) or not os.path.isdir(full):
            continue
        if name.startswith(PARTIAL_PREFIX) and ARCHIVE_RE.match(name[len(PARTIAL_PREFIX):]):
            try:
                old = now - os.lstat(full).st_mtime >= PARTIAL_GRACE_S
            except OSError:
                continue
            why = "partial archive of a crashed pass"
        elif ARCHIVE_RE.match(name):
            data = _manifest(full)
            until = data.get("retain_until_ts") if data else None
            if not isinstance(until, (int, float)) or isinstance(until, bool):
                continue
            old = now >= until
            why = "retention over (archived %s)" % time.strftime(
                "%Y-%m-%d %H:%MZ", time.gmtime(float(data.get("archived_ts") or 0)))
        else:
            continue
        if not old:
            continue
        size = disk_bytes(full)
        if not dry_run:
            shutil.rmtree(full, ignore_errors=True)
            if os.path.lexists(full):
                continue
        out.append({"kind": "archive", "action": "expired", "path": full, "bytes": size,
                    "detail": why})
    return out
