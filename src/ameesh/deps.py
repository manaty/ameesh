# SPDX-License-Identifier: AGPL-3.0-only
"""Shared dependencies per lockfile (lot #248, L231).

The 2026-10-11 19:20 outage was btrfs METADATA exhaustion: 144 `node_modules`
and 6 Rust `target/` directories in nexlink worktrees, millions of inodes.
Reflinks save data, not metadata. So a host keeps ONE copy of a package's
`node_modules` per lockfile, and worktrees point to it:

* **Cache entry**: `<cache_root>/nm/<key>/<package>/node_modules`, where
  `<key>` hashes the lockfile and everything an install depends on
  (`cache_key`). It is filled once by `npm ci`, in place, under a lock
  (`<cache_root>/nm/<key>.lock`), then made read-only; a marker file written
  last (`.complete.json`, atomic rename) publishes it. Filling in place keeps
  the absolute paths an install script may write valid.
* **Link**: the worktree's `<package>/node_modules` is a SYMLINK to the entry.
  A local `node_modules` directory is never replaced unless asked
  (`--replace`). If the entry cannot be filled, the package falls back to a
  local `npm ci`.
* **Writable corners**: `node_modules/.cache` and `node_modules/.vite` stay
  writable in the entry (babel, eslint, vite and vitest write there); they
  are shared by the worktrees of the same lockfile.
* **`npm` in a turn** (`npm_shim`): the turn's PATH starts with an `npm`
  wrapper. `npm ci` (and `npm install` without a package) in a package with
  a lockfile links the shared tree instead of installing; `npm install <pkg>`
  first swaps the link for a local tree; in a REVIEW worktree
  (`is_review_worktree`) every install is refused: reviews read the diff,
  tests run in the agent's main worktree.
* **Garbage collection** (`gc_node_modules`, from the periodic housekeeping
  pass): an entry no worktree links to any more is deleted once idle.
* **Rust** is not here: `CARGO_TARGET_DIR` is one directory per agent,
  `~/.cache/cargo-target/<agent>`, set by the systemd drop-in
  `deploy/systemd/ameesh-runner-agent@.service.d/cargo-target.conf`.

Links are recorded in the entry (`.links/`, one small file per link) so the
GC never has to walk worktrees. Nothing here ever follows a symlink when
deleting: the housekeeping of idle worktrees (#247) skips symlinked
`node_modules`, and never enters `<cache_root>/nm`.
"""
from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

from . import platform

#: sub-directories of the cache root
NM_DIR = "nm"
NODE_MODULES = "node_modules"
LOCKFILE = "package-lock.json"
#: marker written last: an entry without it is partial
COMPLETE = ".complete.json"
#: one file per link pointing to the entry (its content: the link's path)
LINKS = ".links"
#: tool caches inside node_modules that stay writable
WRITABLE = (".cache", ".vite")
#: npm lifecycle scripts run by `npm ci` (their files are part of the key)
LIFECYCLE = ("preinstall", "install", "postinstall", "prepare")
#: bump when the key or the entry layout changes
KEY_VERSION = "ameesh-nm/1"
SCHEMA = "ameesh-deps/1"
#: longest wait for another agent filling the same entry (seconds)
FILL_WAIT_S = 1800.0
#: a partial entry (no marker, lock free) older than this is garbage
PARTIAL_MIN_AGE_S = 3600.0
#: an unreferenced entry is deleted after this long without a new link
DEFAULT_DEPS_IDLE_S = 3 * 24 * 3600.0
#: directories never searched for packages
SKIP_DIRS = (NODE_MODULES, "target", "dist", "build")
#: search depth for packages under a worktree
SEARCH_DEPTH = 3
#: header of the lines added to `info/exclude`
EXCLUDE_HEADER = "# ameesh deps (#248): shared-dependency links, ignored even where " \
                 "the pattern is `node_modules/`"
#: a word of a worktree's directory name that makes it a review worktree
#: (`nexlink-codex2-review-ime`, `review-98-r2`, `dh2b-review`)
REVIEW_WORDS = ("review", "relecture", "revue")
#: `AMEESH_DEPS=off`: the npm wrapper passes everything through;
#: `AMEESH_DEPS=allow-review`: installs allowed in a review worktree
DEPS_ENV = "AMEESH_DEPS"
#: set by the wrapper for the real npm it runs (no recursion)
PASSTHROUGH_ENV = "AMEESH_DEPS_PASSTHROUGH"
#: npm commands that install into node_modules
INSTALL_COMMANDS = ("ci", "clean-install", "install-clean", "isntall-clean", "ic",
                    "install", "i", "in", "ins", "inst", "insta", "instal", "isnt",
                    "isnta", "isntal", "isntall", "add",
                    "install-test", "it", "install-ci-test", "cit")
#: options of `npm ci` that do not change the installed tree
NEUTRAL_OPTIONS = ("--prefer-offline", "--offline", "--no-audit", "--no-fund",
                   "--silent", "--quiet", "-q", "-s", "--no-progress", "--progress=false",
                   "--audit=false", "--fund=false", "--no-update-notifier")
#: name of the wrapper's directory under the housekeeping state
SHIM_DIR = "bin-deps"


class FillError(RuntimeError):
    """The cache entry could not be filled."""


# --------------------------------------------------------------------------
# roots and keys
# --------------------------------------------------------------------------

def nm_root(cache_root: str) -> str:
    return os.path.join(cache_root, NM_DIR)


@functools.lru_cache(maxsize=1)
def node_version() -> str:
    """Major version of the `node` on PATH (`v26`), or "" without node.
    Native modules are built for it: a new major is a new entry."""
    try:
        out = subprocess.run(["node", "--version"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.split(".")[0]


def _lifecycle(pkg_dir: str) -> dict:
    try:
        with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as fh:
            scripts = (json.load(fh) or {}).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {name: str(scripts[name]) for name in LIFECYCLE if scripts.get(name)}


def _inside(path: str, top: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(top)
    return real == base or real.startswith(base.rstrip("/") + "/")


def key_files(pkg_dir: str) -> list[str]:
    """The files of the package an install depends on (relative paths, sorted):
    the lockfile, `package.json`, `.npmrc`, `patches/**` (patch-package and
    the nexlink patch scripts), and every file of the package named in a
    lifecycle script (`node scripts/patch.cjs`). They are hashed into the key
    and copied next to the entry for `npm ci`."""
    out = {LOCKFILE, "package.json"}
    if os.path.isfile(os.path.join(pkg_dir, ".npmrc")):
        out.add(".npmrc")
    patches = os.path.join(pkg_dir, "patches")
    if os.path.isdir(patches) and not os.path.islink(patches):
        for current, dirs, files in os.walk(patches):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(current, d)))
            for name in files:
                full = os.path.join(current, name)
                if os.path.isfile(full) and not os.path.islink(full):
                    out.add(os.path.relpath(full, pkg_dir))
    for command in _lifecycle(pkg_dir).values():
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        for token in tokens:
            if token.startswith("-") or os.path.isabs(token):
                continue
            full = os.path.join(pkg_dir, token)
            if os.path.isfile(full) and _inside(full, pkg_dir):
                out.add(os.path.normpath(token))
    return sorted(out)


def cache_key(pkg_dir: str, *, node: str | None = None) -> str:
    """sha256 of what decides the content of `node_modules`: the key files
    (`key_files`), the lifecycle scripts, the node major version, the OS and
    the CPU (native modules, platform-specific packages)."""
    digest = hashlib.sha256()

    def feed(label: str, data: bytes) -> None:
        digest.update(label.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)

    feed("version", KEY_VERSION.encode())
    feed("node", (node_version() if node is None else node).encode())
    feed("platform", ("%s/%s" % (platform.SYSTEM, platform.machine())).encode())
    feed("lifecycle", json.dumps(_lifecycle(pkg_dir), sort_keys=True).encode())
    for rel in key_files(pkg_dir):
        with open(os.path.join(pkg_dir, rel), "rb") as fh:
            feed("file:" + rel, fh.read())
    return digest.hexdigest()


def package_name(pkg_dir: str) -> str:
    """The `<package>` level of the entry: the package directory's name."""
    name = os.path.basename(os.path.abspath(pkg_dir)) or "package"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).lstrip(".") \
        or "package"


def entry_of(root: str, key: str) -> str:
    return os.path.join(root, key)


def entry_modules(root: str, key: str, pkg_name: str) -> str:
    return os.path.join(root, key, pkg_name, NODE_MODULES)


def read_marker(entry: str) -> dict | None:
    try:
        with open(os.path.join(entry, COMPLETE), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# finding packages
# --------------------------------------------------------------------------

def _walk(top: str, depth: int):
    """Directories under `top` (itself included) down to `depth`, never
    through a symlink, a hidden directory or a SKIP_DIRS one."""
    stack = [(os.path.abspath(top), 0)]
    while stack:
        current, level = stack.pop()
        yield current
        if level >= depth:
            continue
        try:
            with os.scandir(current) as it:
                names = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for name in reversed(names):
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            stack.append((os.path.join(current, name), level + 1))


def find_packages(top: str, depth: int = SEARCH_DEPTH) -> list[str]:
    """Package directories (with a `package-lock.json`) under `top`."""
    if os.path.isfile(os.path.join(top, LOCKFILE)):
        return [os.path.abspath(top)]
    return [d for d in _walk(top, depth) if os.path.isfile(os.path.join(d, LOCKFILE))]


def is_shared_link(path: str, cache_root: str | None = None) -> bool:
    """`path` is a symlink (never followed); with `cache_root`, one that
    points into the shared node_modules cache."""
    if not os.path.islink(path):
        return False
    if cache_root is None:
        return True
    try:
        target = os.path.join(os.path.dirname(path), os.readlink(path))
    except OSError:
        return False
    target = os.path.normpath(target)
    root = os.path.normpath(nm_root(cache_root))
    return target == root or target.startswith(root + "/")


# --------------------------------------------------------------------------
# read-only trees and their deletion
# --------------------------------------------------------------------------

def _seal(modules: str) -> tuple[int, int]:
    """Make `modules` read-only (directories 0555, write bits removed from
    files), except the WRITABLE tool caches at its top; never through a
    symlink. Returns (files, bytes)."""
    for name in WRITABLE:
        os.makedirs(os.path.join(modules, name), exist_ok=True)
    files = size = 0
    skip = {os.path.join(modules, name) for name in WRITABLE}
    for current, dirs, names in os.walk(modules, topdown=False):
        if current in skip or any(current.startswith(s + "/") for s in skip):
            continue
        for name in names:
            full = os.path.join(current, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            files += 1
            size += st.st_blocks * 512 if st.st_blocks else st.st_size
            if stat.S_ISREG(st.st_mode) and st.st_mode & 0o222:
                os.chmod(full, stat.S_IMODE(st.st_mode) & ~0o222)
        for name in dirs:
            full = os.path.join(current, name)
            if full in skip or os.path.islink(full):
                continue
            os.chmod(full, 0o555)
    os.chmod(modules, 0o555)
    return files, size


def _unseal(top: str) -> None:
    """Give the owner write access to every directory under `top` (never
    through a symlink), so that it can be deleted."""
    if os.path.islink(top) or not os.path.isdir(top):
        return
    try:
        os.chmod(top, 0o755)
    except OSError:
        pass
    for current, dirs, _names in os.walk(top):
        for name in dirs:
            full = os.path.join(current, name)
            if not os.path.islink(full):
                try:
                    os.chmod(full, 0o755)
                except OSError:
                    pass


def remove_tree(path: str, root: str) -> None:
    """Delete `path`, which must lie strictly under `root`, after unsealing
    it; a symlink is unlinked, never followed."""
    real_root = os.path.realpath(root).rstrip("/")
    parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    if not (parent == real_root or parent.startswith(real_root + "/")):
        raise RuntimeError("%s is outside %s" % (path, root))
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
        return
    _unseal(path)
    shutil.rmtree(path)


# --------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------

def _lock(path: str, *, wait: float, exclusive: bool = True):
    """An open flock'ed file, or None after `wait` seconds."""
    fh = open(path, "a+")
    deadline = time.monotonic() + max(0.0, wait)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        try:
            fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(0.5)


# --------------------------------------------------------------------------
# filling and linking
# --------------------------------------------------------------------------

def real_npm(env: dict | None = None) -> str | None:
    """The real npm: `$AMEESH_REAL_NPM`, else the first `npm` of PATH that is
    not in a wrapper directory."""
    env = os.environ if env is None else env
    if env.get("AMEESH_REAL_NPM") and os.access(env["AMEESH_REAL_NPM"], os.X_OK):
        return env["AMEESH_REAL_NPM"]
    for directory in (env.get("PATH") or os.defpath).split(os.pathsep):
        if not directory or os.path.basename(os.path.normpath(directory)) == SHIM_DIR:
            continue
        candidate = os.path.join(directory, "npm")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def npm_ci(directory: str, env: dict | None = None) -> None:
    """The default installer: the real `npm ci`, gently (nice), cache first."""
    env = dict(os.environ if env is None else env)
    env[PASSTHROUGH_ENV] = "1"
    command = [real_npm(env) or "npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"]
    if shutil.which("nice"):
        command = ["nice", "-n", "10"] + command
    try:
        done = subprocess.run(command, cwd=directory, env=env, capture_output=True,
                              text=True)
    except OSError as exc:
        raise FillError("npm ci could not start: %s" % exc)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-5:]
        raise FillError("npm ci failed (%d): %s" % (done.returncode, " | ".join(tail)))


def fill(pkg_dir: str, root: str, key: str, *, installer=npm_ci, env: dict | None = None,
         wait: float = FILL_WAIT_S) -> dict:
    """Fill the entry `key` from `pkg_dir` if it is not complete yet; returns
    its marker. Raises FillError (lock not obtained, install failed)."""
    os.makedirs(root, mode=0o755, exist_ok=True)
    entry = entry_of(root, key)
    marker = read_marker(entry)
    if marker:
        return marker
    lock = _lock(os.path.join(root, key + ".lock"), wait=wait)
    if lock is None:
        raise FillError("another fill of %s still running after %ds" % (key[:12], wait))
    try:
        marker = read_marker(entry)
        if marker:
            return marker
        if os.path.lexists(entry):          # a partial fill, under our lock
            remove_tree(entry, root)
        name = package_name(pkg_dir)
        stage = os.path.join(entry, name)
        os.makedirs(stage, mode=0o755)
        os.makedirs(os.path.join(entry, LINKS), mode=0o755)
        copied = key_files(pkg_dir)
        for rel in copied:
            dest = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(pkg_dir, rel), dest)
        started = time.time()
        try:
            installer(stage, env)
            modules = os.path.join(stage, NODE_MODULES)
            if os.path.islink(modules) or not os.path.isdir(modules):
                raise FillError("the installer left no node_modules directory")
            # only package.json and the lockfile stay beside node_modules: tools
            # that walk up from a real path find the package they expect
            for rel in copied:
                if rel not in ("package.json", LOCKFILE):
                    os.unlink(os.path.join(stage, rel))
            for current, dirs, files in os.walk(stage, topdown=False):
                if current != stage and not dirs and not files and \
                        not current.startswith(modules):
                    os.rmdir(current)
            files, size = _seal(modules)
        except BaseException:
            try:
                remove_tree(entry, root)
            except OSError:
                pass
            raise
        marker = {"schema": SCHEMA, "key": key, "package": name,
                  "source": os.path.abspath(pkg_dir), "node": node_version(),
                  "created": time.time(), "install_s": round(time.time() - started, 1),
                  "files": files, "bytes": size}
        tmp = os.path.join(entry, COMPLETE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(marker, fh, sort_keys=True)
        os.replace(tmp, os.path.join(entry, COMPLETE))
        return marker
    finally:
        lock.close()


def _record_link(entry: str, link: str) -> None:
    links = os.path.join(entry, LINKS)
    os.makedirs(links, exist_ok=True)
    name = hashlib.sha1(os.path.abspath(link).encode()).hexdigest()
    with open(os.path.join(links, name), "w", encoding="utf-8") as fh:
        fh.write(os.path.abspath(link))


def _symlink(target: str, link: str) -> None:
    """Point `link` (absent or a symlink) to `target`, atomically."""
    tmp = os.path.join(os.path.dirname(link), ".%s.ameesh-%d" % (os.path.basename(link),
                                                                 os.getpid()))
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link)


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def git_exclude(link: str) -> str | None:
    """Keep `link` out of `git status`: a `node_modules/` (or `target/`)
    pattern only matches directories, not a symlink. Adds the anchored path
    to the repository's `info/exclude` (shared by its worktrees, which have
    the same layout); returns the pattern added, None if nothing to do."""
    where = os.path.dirname(os.path.abspath(link))
    top = _git(where, "rev-parse", "--show-toplevel")
    if top is None or top.returncode != 0:
        return None
    top_dir = top.stdout.strip()
    ignored = _git(top_dir, "check-ignore", "-q", "--no-index", "--",
                   os.path.relpath(link, top_dir))
    if ignored is not None and ignored.returncode == 0:
        return None
    common = _git(where, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common is None or common.returncode != 0:
        return None
    exclude = os.path.join(common.stdout.strip(), "info", "exclude")
    pattern = "/" + os.path.relpath(os.path.abspath(link), top_dir)
    try:
        with open(exclude, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    if pattern in lines:
        return None
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    with open(exclude, "a", encoding="utf-8") as fh:
        if lines and lines[-1] != "" and EXCLUDE_HEADER not in lines:
            fh.write("\n")
        if EXCLUDE_HEADER not in lines:
            fh.write(EXCLUDE_HEADER + "\n")
        fh.write(pattern + "\n")
    return pattern


def link_package(pkg_dir: str, cache_root: str, *, fill_missing: bool = True,
                 replace: bool = False, installer=npm_ci, env: dict | None = None,
                 wait: float = FILL_WAIT_S) -> dict:
    """Give `pkg_dir` its shared `node_modules`; returns what was done:

    * `linked`: the symlink now points to the complete entry (`filled`
      also says whether this call filled it);
    * `local-kept`: a real `node_modules` directory is there and `replace`
      is false: nothing touched;
    * `not-cached`: no complete entry and `fill_missing` is false;
    * `local`: the entry could not be filled, `npm ci` ran locally
      (`reason`), or `local-failed` if that failed too;
    * `no-lockfile`."""
    pkg_dir = os.path.abspath(pkg_dir)
    result = {"package": pkg_dir}
    if not os.path.isfile(os.path.join(pkg_dir, LOCKFILE)):
        return dict(result, status="no-lockfile")
    root = nm_root(cache_root)
    key = cache_key(pkg_dir)
    target = entry_modules(root, key, package_name(pkg_dir))
    link = os.path.join(pkg_dir, NODE_MODULES)
    result.update(key=key, target=target)
    if os.path.lexists(link) and not os.path.islink(link):
        if not replace:
            return dict(result, status="local-kept")
    elif os.path.islink(link) and os.readlink(link) == target and \
            read_marker(entry_of(root, key)):
        _record_link(entry_of(root, key), link)
        return dict(result, status="linked", filled=False, already=True)
    filled = False
    if not read_marker(entry_of(root, key)):
        if not fill_missing:
            return dict(result, status="not-cached")
        try:
            fill(pkg_dir, root, key, installer=installer, env=env, wait=wait)
            filled = True
        except FillError as exc:
            if os.path.islink(link):
                os.unlink(link)
            try:
                installer(pkg_dir, env)
            except FillError as local_exc:
                return dict(result, status="local-failed", reason=str(exc),
                            error=str(local_exc))
            return dict(result, status="local", reason=str(exc))
    # shared lock: the GC (exclusive, never waits) cannot delete the entry
    # between the check of its marker and the record of this link
    lock = _lock(os.path.join(root, key + ".lock"), wait=wait, exclusive=False)
    try:
        if not read_marker(entry_of(root, key)):
            return dict(result, status="not-cached")
        old = None
        if os.path.lexists(link) and not os.path.islink(link):
            old = os.path.join(pkg_dir, ".node_modules.ameesh-old-%d" % os.getpid())
            os.rename(link, old)
        _symlink(target, link)
        _record_link(entry_of(root, key), link)
    finally:
        if lock is not None:
            lock.close()
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)
    result.update(status="linked", filled=filled, already=False,
                  excluded=git_exclude(link), replaced=old is not None)
    return result


def link_tree(top: str, cache_root: str, *, fill_missing: bool = True,
              replace: bool = False, installer=npm_ci, env: dict | None = None,
              wait: float = FILL_WAIT_S) -> list[dict]:
    """`link_package` for every package under `top`."""
    return [link_package(p, cache_root, fill_missing=fill_missing, replace=replace,
                         installer=installer, env=env, wait=wait)
            for p in find_packages(top)]


def link_ready(top: str, cache_root: str) -> list[dict]:
    """The runner's hook at the start of a turn (cheap, never installs): link
    the packages under `top` that have NO `node_modules` and whose entry is
    already complete. Never in a review worktree."""
    if is_review_worktree(top):
        return []
    out = []
    for pkg in find_packages(top):
        if os.path.lexists(os.path.join(pkg, NODE_MODULES)):
            continue
        done = link_package(pkg, cache_root, fill_missing=False)
        if done["status"] == "linked":
            out.append(done)
    return out


def make_local(pkg_dir: str, *, installer=npm_ci, env: dict | None = None) -> dict:
    """Replace the symlink by a local `node_modules` (`npm ci` in place): for
    `npm install <pkg>`, which writes into node_modules and the lockfile, or
    a tool that needs a real directory."""
    link = os.path.join(os.path.abspath(pkg_dir), NODE_MODULES)
    result = {"package": os.path.abspath(pkg_dir)}
    if os.path.lexists(link) and not os.path.islink(link):
        return dict(result, status="local-kept")
    if os.path.islink(link):
        os.unlink(link)
    try:
        installer(os.path.abspath(pkg_dir), env)
    except FillError as exc:
        return dict(result, status="local-failed", error=str(exc))
    return dict(result, status="local")


# --------------------------------------------------------------------------
# review worktrees and the npm wrapper of the turns
# --------------------------------------------------------------------------

def worktree_root(path: str) -> str | None:
    where = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    done = _git(where, "rev-parse", "--show-toplevel")
    if done is None or done.returncode != 0:
        return None
    return done.stdout.strip() or None


def is_review_worktree(path: str) -> bool:
    """A review worktree: a directory from `path` up to its git worktree's top
    (every parent outside git) has a word of REVIEW_WORDS in its name
    (`nexlink-codex2-review-ime`, `review-98-r2`, `dh2b-review`). Reviews read
    the diff and get no dependencies; tests run in the agent's main worktree."""
    top = worktree_root(path)
    current = os.path.abspath(path)
    while True:
        words = re.split(r"[^a-z0-9]+", os.path.basename(current).lower())
        if any(word in REVIEW_WORDS for word in words):
            return True
        parent = os.path.dirname(current)
        if parent == current or (top and current == top):
            return False
        current = parent


def package_dir(cwd: str) -> str | None:
    """The package npm would act on from `cwd`: the nearest directory with a
    `package.json`, upwards, without leaving the git worktree."""
    current = os.path.abspath(cwd)
    stop = worktree_root(current)
    while True:
        if os.path.isfile(os.path.join(current, "package.json")):
            return current
        parent = os.path.dirname(current)
        if parent == current or (stop and current == stop):
            return None
        current = parent


def lock_in_sync(pkg_dir: str) -> bool:
    """The lockfile's root entry declares the same dependencies as
    `package.json` (what `npm ci` checks): if not, npm itself must run."""
    try:
        with open(os.path.join(pkg_dir, "package.json"), encoding="utf-8") as fh:
            manifest = json.load(fh) or {}
        with open(os.path.join(pkg_dir, LOCKFILE), encoding="utf-8") as fh:
            root = ((json.load(fh) or {}).get("packages") or {}).get("") or {}
    except (OSError, ValueError, AttributeError):
        return False
    if manifest.get("workspaces"):
        return False
    return all((manifest.get(field) or {}) == (root.get(field) or {})
               for field in ("dependencies", "devDependencies", "optionalDependencies",
                             "peerDependencies"))


def _npm_words(args: list[str]) -> tuple[str | None, list[str], list[str]]:
    """(command, positional arguments, options) of an npm command line."""
    command, positional, options = None, [], []
    for arg in args:
        if arg.startswith("-"):
            options.append(arg)
        elif command is None:
            command = arg
        else:
            positional.append(arg)
    return command, positional, options


SHIM_TEXT = """#!/bin/sh
# ameesh deps (#248): npm of the turns. `npm ci` links the node_modules shared
# per lockfile; installs are refused in review worktrees. AMEESH_DEPS=off: plain npm.
exec %s -m ameesh.deps npm "$@"
"""


def install_shim(directory: str, path_env: str, python: str | None = None) -> dict:
    """Write the `npm` wrapper into `directory`; returns the variables to set
    (PATH first, the real npm), empty if npm is not on `path_env`."""
    real = real_npm({"PATH": path_env})
    if real is None:
        return {}
    text = SHIM_TEXT % shlex.quote(python or sys.executable)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    target = os.path.join(directory, "npm")
    try:
        with open(target, encoding="utf-8") as fh:
            current = fh.read()
    except OSError:
        current = None
    if current != text:
        tmp = target + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    return {"PATH": directory + os.pathsep + path_env, "AMEESH_REAL_NPM": real}


def _say(text: str) -> None:
    print("ameesh deps: %s" % text, file=sys.stderr)


def npm_shim(args: list[str], *, env: dict | None = None, cwd: str | None = None,
             execute=None, installer=npm_ci) -> int:
    """The `npm` of a turn. Returns an exit code, or replaces the process by
    the real npm (`execute`, `os.execve` by default) when it lets it run."""
    env = dict(os.environ if env is None else env)
    cwd = os.getcwd() if cwd is None else cwd
    real = real_npm(env)

    def passthrough() -> int:
        if real is None:
            _say("no npm found on PATH")
            return 127
        run = execute or os.execve
        return run(real, [real] + list(args), dict(env, **{PASSTHROUGH_ENV: "1"})) or 0

    mode = (env.get(DEPS_ENV) or "").strip().lower()
    if env.get(PASSTHROUGH_ENV) or mode in ("off", "0", "no", "none"):
        return passthrough()
    command, positional, options = _npm_words(args)
    if command not in INSTALL_COMMANDS:
        return passthrough()
    if any(o.split("=")[0] in ("-g", "--global", "--location") for o in options):
        return passthrough()                      # outside any worktree
    if mode != "allow-review" and is_review_worktree(cwd):
        _say("`npm %s` refused in a review worktree (%s): reviews get no dependencies "
             "(#248); run the tests in your main worktree (check out the branch "
             "there). AMEESH_DEPS=allow-review to override."
             % (command, worktree_root(cwd) or cwd))
        return 1
    if any(o.split("=")[0] in ("--prefix", "-C", "-w", "--workspace", "--workspaces")
           for o in options):
        return passthrough()
    pkg = package_dir(cwd)
    if pkg is None or not os.path.isfile(os.path.join(pkg, LOCKFILE)):
        return passthrough()
    link = os.path.join(pkg, NODE_MODULES)
    ci = command in ("ci", "clean-install", "install-clean", "isntall-clean", "ic")
    plain = ci or (command not in ("install-test", "it", "install-ci-test", "cit")
                   and not positional)
    neutral = all(o in NEUTRAL_OPTIONS or o.startswith("--loglevel") for o in options)
    if plain and neutral and lock_in_sync(pkg):
        try:
            cache_root = resolve_cache_root(env)
        except Exception as exc:
            _say("cache root unknown (%s): plain npm" % exc)
            return passthrough()
        if not read_marker(entry_of(nm_root(cache_root), cache_key(pkg))):
            _say("filling the shared node_modules of %s once for this lockfile "
                 "(npm ci, then read-only)…" % pkg)
        result = link_package(pkg, cache_root, replace=True, installer=installer, env=env)
        _say(_describe(result).strip())
        if result["status"] == "linked":
            _say("node_modules is a read-only link shared per lockfile; "
                 "`ameesh deps local` before `npm install <pkg>`")
        return 0 if result["status"] in ("linked", "local") else 1
    if os.path.islink(link):
        # npm would write into the read-only shared tree: it gets a local one
        os.unlink(link)
        _say("shared node_modules unlinked in %s: npm installs a local tree" % pkg)
    return passthrough()


# --------------------------------------------------------------------------
# status and garbage collection
# --------------------------------------------------------------------------

def live_links(entry: str) -> list[str]:
    """Recorded links that still point into `entry` (stale records pruned)."""
    links = os.path.join(entry, LINKS)
    out = []
    try:
        names = sorted(os.listdir(links))
    except OSError:
        return out
    for name in names:
        record = os.path.join(links, name)
        try:
            with open(record, encoding="utf-8") as fh:
                link = fh.read().strip()
        except OSError:
            continue
        alive = False
        if link and os.path.islink(link):
            target = os.path.normpath(os.path.join(os.path.dirname(link), os.readlink(link)))
            alive = target.startswith(os.path.normpath(entry) + "/")
        if alive:
            out.append(link)
        else:
            try:
                os.unlink(record)
            except OSError:
                pass
    return out


def entries(cache_root: str) -> list[dict]:
    """Every entry of the node_modules cache, with its state."""
    root = nm_root(cache_root)
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        full = os.path.join(root, name)
        if name.endswith(".lock") or os.path.islink(full) or not os.path.isdir(full):
            continue
        marker = read_marker(full)
        try:
            mtime = os.lstat(full).st_mtime
        except OSError:
            continue
        links_dir = os.path.join(full, LINKS)
        try:
            last_link = max([os.lstat(os.path.join(links_dir, n)).st_mtime
                             for n in os.listdir(links_dir)] or [0.0])
        except OSError:
            last_link = 0.0
        out.append({"key": name, "path": full, "complete": marker is not None,
                    "marker": marker or {}, "mtime": mtime, "last_link": last_link})
    return out


def gc_node_modules(cache_root: str, *, now: float | None = None,
                    idle: float = DEFAULT_DEPS_IDLE_S, dry_run: bool = False) -> list[dict]:
    """Delete the entries no worktree links to any more, idle for `idle`
    seconds (since their creation and their last link), and partial ones left
    by a dead fill; each under the entry's lock, never while it is filled.
    Returns housekeeping log lines (`kind` "deps")."""
    now = time.time() if now is None else float(now)
    root = nm_root(cache_root)
    out = []
    for item in entries(cache_root):
        entry = item["path"]
        if item["complete"]:
            if live_links(entry):
                continue
            since = max(float(item["marker"].get("created") or 0), item["last_link"])
            if now - since < idle:
                continue
            why = "no worktree links to it since %s" % time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(since))
        else:
            if now - item["mtime"] < PARTIAL_MIN_AGE_S:
                continue
            why = "partial fill (no %s)" % COMPLETE
        size = int(item["marker"].get("bytes") or 0)
        line = {"kind": "deps", "action": "deleted", "path": entry, "bytes": size,
                "detail": why}
        if dry_run:
            out.append(dict(line, action="would-delete"))
            continue
        lock = _lock(os.path.join(root, item["key"] + ".lock"), wait=0)
        if lock is None:
            continue                       # being filled or linked right now
        try:
            if item["complete"] and live_links(entry):
                continue
            remove_tree(entry, root)
            try:
                os.unlink(os.path.join(root, item["key"] + ".lock"))
            except OSError:
                pass
        except OSError as exc:
            out.append(dict(line, action="kept", detail="%s ; deletion failed: %s"
                            % (why, exc)))
            continue
        finally:
            lock.close()
        out.append(line)
    return out


# --------------------------------------------------------------------------
# command line: `ameesh deps`
# --------------------------------------------------------------------------

def resolve_cache_root(env: dict | None = None) -> str:
    """The housekeeping cache root: `$AMEESH_CACHE_DIR` in a turn, else the
    one of this host's policy."""
    env = os.environ if env is None else env
    if env.get("AMEESH_CACHE_DIR"):
        return env["AMEESH_CACHE_DIR"]
    from . import canon as canon_mod
    from . import config as config_mod
    from . import menage as menage_mod
    cfg = config_mod.load()
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception:
        canons = []
    policy = menage_mod.effective(canons, cfg.host or "")
    root = menage_mod.cache_root(cfg, policy)
    menage_mod._ensure_root(root)
    return root


STATUS_TEXT = {
    "linked": "linked",
    "local-kept": "local node_modules kept (--replace to swap it for the shared one)",
    "not-cached": "not in the cache yet (run without --no-fill)",
    "local": "local npm ci (the cache could not be filled)",
    "local-failed": "FAILED: neither the cache nor a local npm ci",
    "no-lockfile": "no package-lock.json: left alone",
}


def _describe(item: dict) -> str:
    where = item.get("package") or "?"
    text = STATUS_TEXT.get(item["status"], item["status"])
    if item["status"] == "linked":
        text = "already linked" if item.get("already") else (
            "filled and linked" if item.get("filled") else "linked")
        text += " -> %s" % item.get("target")
    for extra in ("reason", "error"):
        if item.get(extra):
            text += " (%s)" % item[extra]
    return "  %-48s %s" % (where, text)


def _status(top: str, cache_root: str) -> list[dict]:
    out = []
    root = nm_root(cache_root)
    for pkg in find_packages(top):
        key = cache_key(pkg)
        link = os.path.join(pkg, NODE_MODULES)
        target = entry_modules(root, key, package_name(pkg))
        if os.path.islink(link):
            state = "linked" if os.readlink(link) == target else "stale-link"
        elif os.path.lexists(link):
            state = "local"
        else:
            state = "absent"
        out.append({"package": pkg, "key": key, "node_modules": state,
                    "cached": read_marker(entry_of(root, key)) is not None})
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["npm"]:                 # the wrapper (`SHIM_TEXT`)
        return npm_shim(argv[1:])
    parser = argparse.ArgumentParser(
        prog="ameesh deps",
        description="node_modules shared per lockfile (#248)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_link = sub.add_parser(
        "link", help="link each package's node_modules to the shared cache (fill it once "
                     "with npm ci if needed)")
    p_link.add_argument("paths", nargs="*", help="worktrees or packages (default: .)")
    p_link.add_argument("--no-fill", action="store_true",
                        help="only link packages already in the cache")
    p_link.add_argument("--replace", action="store_true",
                        help="replace a local node_modules directory by the link")
    p_link.add_argument("--json", action="store_true")
    p_local = sub.add_parser(
        "local", help="replace the link by a local node_modules (npm ci), e.g. before "
                      "npm install <pkg>")
    p_local.add_argument("paths", nargs="*", help="packages (default: .)")
    p_local.add_argument("--json", action="store_true")
    p_status = sub.add_parser("status", help="each package: its key, link and cache state")
    p_status.add_argument("paths", nargs="*", help="worktrees or packages (default: .)")
    p_status.add_argument("--json", action="store_true")
    p_entries = sub.add_parser("entries", help="the entries of the host's node_modules cache")
    p_entries.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    cache_root = resolve_cache_root()
    paths = getattr(args, "paths", None) or ["."]
    if args.command == "link":
        items: list[dict] = []
        for top in paths:
            items += link_tree(top, cache_root, fill_missing=not args.no_fill,
                               replace=args.replace)
    elif args.command == "local":
        items = [make_local(p) for top in paths for p in find_packages(top)]
    elif args.command == "status":
        items = [i for top in paths for i in _status(top, cache_root)]
    else:
        items = entries(cache_root)
    if args.json:
        print(json.dumps({"schema": SCHEMA, "cache_root": cache_root, "items": items},
                         ensure_ascii=False, sort_keys=True))
    elif args.command in ("link", "local"):
        for item in items:
            print(_describe(item))
        if not items:
            print("  no package-lock.json found")
    elif args.command == "status":
        for item in items:
            print("  %-48s node_modules %-10s cache %s  key %s" % (
                item["package"], item["node_modules"],
                "complete" if item["cached"] else "missing", item["key"][:12]))
    else:
        for item in items:
            marker = item["marker"]
            print("  %s  %-8s %-10s %7s files %9s  %d link(s)" % (
                item["key"][:12], marker.get("package", "?"),
                "complete" if item["complete"] else "PARTIAL",
                marker.get("files", "?"),
                "%.1f MiB" % (marker.get("bytes", 0) / 1024 ** 2),
                len(live_links(item["path"]))))
    failed = any(i.get("status") == "local-failed" for i in items)
    return 1 if failed else 0


__all__ = ["cache_key", "key_files", "find_packages", "fill", "link_package", "link_tree",
           "link_ready", "make_local", "is_shared_link", "is_review_worktree", "npm_shim",
           "install_shim", "gc_node_modules", "entries", "live_links", "nm_root", "FillError",
           "main"]


if __name__ == "__main__":
    sys.exit(main())
