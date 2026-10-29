# SPDX-License-Identifier: AGPL-3.0-only
"""#3728 — archive, then removal, of a worktree of an ended lot kept only for
modified or untracked files (#3734: or for ignored files only; #4442: or for
commits absent from the forge).

The housekeeping keeps such a worktree for good (`kept — modified or
untracked files`, `kept — commits not pushed`): 137 on ameesh-agents-2 and 147
on ameesh-agents-1 on 26/10/2026, most of them of lots merged days before,
and nothing ever removed them; #4442 counted 491 kept worktrees. Past
`policy.housekeeping.dirty_worktree_after` (a duration of the Host card,
compiled from the canon; undeclared: nothing is archived), the pass archives
what removing it would lose, then removes it like a kept merged worktree
(`housekeeping._remove_kept_worktree`):

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
- `HEAD.bundle` (#4442): `git bundle create … HEAD --not --remotes`, so the
  commits no remote holds survive the removal; absent when every commit of
  HEAD is on a remote (an empty bundle is refused by git). Fetched back with
  `git fetch <archive>/HEAD.bundle HEAD:refs/heads/<name>`;
- `manifest.json`: path, branch, HEAD, lot, `git status`, the archived files,
  sizes, the bundle and its commit count, and `retain_until_ts`
  (`policy.housekeeping.dirty_archive_retention`).

Archives live under `<state>/.menage/worktree-archives/` (mode 0700: an
ignored `.env` may hold a secret), are written under a `.partial-` name then
renamed, and go once their `retain_until_ts` is past (`prune`). The guards of
the removal (lot ended, no process, no git lock, no turn in progress, idle)
are the caller's; this module refuses (`ArchiveRefused`) whatever it cannot
archive whole: in doubt, the worktree stays."""
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


def _walk(top: str, rel: str, rules, out: set[str], *, check=None,
          limit: int | None = None) -> None:
    """Files under the ignored directory `rel`, outside the exclusions; a
    symbolic link is one entry, never followed.

    `check` is called while walking, at every directory and entry: a large
    ignored tree cannot outlive the pass budget (#3802 B1, cx2). `limit`
    stops the walk as soon as more than that many distinct entries are
    found, so the caller's `max_files` refusal does not wait for the whole
    tree either."""
    pending = [rel]
    while pending:
        crel = pending.pop()
        try:
            with os.scandir(os.path.join(top, crel)) as entries:
                for entry in entries:
                    if check is not None:
                        check()
                    name = entry.name
                    sub = os.path.join(crel, name)
                    try:
                        link = entry.is_symlink()
                        is_dir = entry.is_dir(follow_symlinks=False)
                        target_dir = link and entry.is_dir(follow_symlinks=True)
                    except OSError:
                        link, is_dir, target_dir = False, False, False
                    if is_dir and not link:
                        if not _excluded(top, sub, rules):
                            pending.append(sub)
                    elif link and target_dir:
                        # a symbolic link is one entry, never followed
                        out.add(sub)
                    elif not _excluded(top, sub, rules):
                        out.add(sub)
                    if limit is not None and len(out) > limit:
                        raise ArchiveRefused(
                            "%d untracked file(s) to archive (limit %d)"
                            % (len(out), limit))
        except OSError:
            continue
        if check is not None:
            check()


def untracked_files(path: str, rules, *, timeout=GIT_TIMEOUT, check=None,
                    max_files: int | None = None) -> list[str]:
    """Untracked and ignored files of the worktree `path`, relative, outside
    the build output `rules` and `EXCLUDED_NAMES`.

    `timeout` is a delay or a callable returning the delay, evaluated before
    each `git` call: a slow `git ls-files` never gets the delay already spent
    by the previous one (#3802 B1, cx2). `check` and `max_files` bound the
    walk of the ignored directories the same way."""
    def _delay() -> float:
        return float(timeout() if callable(timeout) else timeout)

    found = _names(_git(path, "ls-files", "-z", "--others", "--exclude-standard",
                        timeout=_delay()).stdout)
    ignored = _names(_git(path, "ls-files", "-z", "--others", "--ignored",
                          "--exclude-standard", "--directory",
                          timeout=_delay()).stdout)
    out: set[str] = set()
    for rel in found:
        if check is not None:
            check()
        if not _excluded(path, rel, rules):
            out.add(rel)
    if max_files is not None and len(out) > max_files:
        raise ArchiveRefused("%d untracked file(s) to archive (limit %d)" % (
            len(out), max_files))
    for rel in ignored:
        if check is not None:
            check()
        if _excluded(path, rel, rules):
            continue
        if rel.endswith("/") and not os.path.islink(os.path.join(path, rel.rstrip("/"))):
            _walk(path, rel.rstrip("/"), rules, out, check=check, limit=max_files)
        else:
            out.add(rel.rstrip("/"))
    return sorted(out)


def _size(path: str, files: list[str], *, check=None) -> int:
    total = 0
    for rel in files:
        if check is not None:
            check()
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


def unpushed_commits(path: str, *, timeout=GIT_TIMEOUT) -> int:
    """#4442 : commits de HEAD qu'aucune branche distante ne porte (0 : tout
    est sur la forge). Un Git illisible est un refus : sans ce compte, on ne
    peut pas prouver que les commits seront archivés."""
    raw = _git(path, "rev-list", "--count", "HEAD", "--not", "--remotes",
               timeout=timeout).stdout.strip()
    try:
        return int(raw or b"0")
    except ValueError as exc:
        raise ArchiveRefused("git rev-list count unreadable (%r)" % raw[:40]) from exc


def _restore_ref(name: str) -> str:
    return "ameesh-archive-" + re.sub(r"[^0-9A-Za-z._-]", "-", name)


def archive(path: str, root: str, *, rules=(), lot=None, state: str = "",
            branch: str = "", head: str = "", repo: str = "",
            now: float | None = None, retention_s: float,
            max_bytes: int | None = None, max_files: int | None = None,
            deadline: float | None = None) -> dict:
    """Archives the worktree `path` under `root` (created 0700). Returns
    `archive` (its directory), `bytes` (on disk), `patch_bytes`, `files`
    (untracked archived), `tracked` (tracked entries changed), `commits`
    (#4442: commits of HEAD absent from every remote, bundled) and
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
    files = untracked_files(path, rules, timeout=_timeout, check=_check,
                            max_files=max_files)
    if len(files) > max_files:
        raise ArchiveRefused("%d untracked file(s) to archive (limit %d)" % (
            len(files), max_files))
    size = _size(path, files, check=_check)
    # dev3 N1: the patches count too
    if size + len(staged) + len(unstaged) > max_bytes:
        raise ArchiveRefused("%d bytes to archive (limit %d)" % (
            size + len(staged) + len(unstaged), max_bytes))
    # #4442 : les commits qu'aucune branche distante ne porte partent dans
    # `HEAD.bundle`, sinon retirer le worktree perdrait ce travail. Le compte
    # est lu avant de créer le dossier partiel (un bundle vide est refusé par
    # git, et un worktree sans commit non poussé n'en a pas besoin).
    commits = unpushed_commits(path, timeout=_timeout()) if head else 0
    bundle_bytes = 0
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
        if commits:
            bundle_path = os.path.join(partial, "HEAD.bundle")
            _check()
            _git(path, "bundle", "create", bundle_path, "HEAD", "--not", "--remotes",
                 timeout=_timeout())
            try:
                bundle_bytes = os.stat(bundle_path).st_size
            except OSError as exc:
                raise ArchiveRefused("HEAD.bundle not written (%s)" % exc) from exc
            _check()
            # #4442 : le bundle compte dans la borne, comme les patchs (N1 de
            # #3728) ; un bundle trop gros refuse l'archive entière.
            if size + len(staged) + len(unstaged) + bundle_bytes > max_bytes:
                raise ArchiveRefused("%d bytes to archive (limit %d)" % (
                    size + len(staged) + len(unstaged) + bundle_bytes, max_bytes))
        ref = _restore_ref(name)
        restore = (["git -C %s fetch %s/HEAD.bundle HEAD:refs/heads/%s"
                    % (repo or "<repo>", final, ref)]
                   if commits else []) + [
            "git worktree add --detach <path> %s" % (ref if commits else head or "<HEAD>")]
        manifest = {
            "schema": "ameesh-worktree-archive/1", "path": path, "branch": branch,
            "head": head, "repo": repo, "lot": lot, "lot_state": state,
            "archived_ts": now, "retain_until_ts": now + float(retention_s),
            "status": entries, "untracked": files, "untracked_bytes": size,
            "patch_bytes": len(staged) + len(unstaged),
            "commits": commits, "bundle": "HEAD.bundle" if commits else None,
            "bundle_bytes": bundle_bytes, "restore_ref": ref if commits else None,
            # dev3 B2: `git apply` refuses an empty patch (exit 128): only
            # the layers that hold something are in the command
            "restore": " && ".join(
                restore
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
            "commits": commits, "bundle_bytes": bundle_bytes,
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
