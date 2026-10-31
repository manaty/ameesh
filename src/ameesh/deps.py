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

from . import platform, deps_packagers

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
KEY_VERSION = "ameesh-nm/2"
SCHEMA = "ameesh-deps/1"
#: longest wait for another agent filling the same entry (seconds)
FILL_WAIT_S = 1800.0
#: a partial entry (no marker, lock free) older than this is garbage
PARTIAL_MIN_AGE_S = 3600.0
#: never sooner than this after a link to it was last seen alive
GRACE_S = 24 * 3600.0
#: an unreferenced entry is deleted after this long without a new link
#: (#3746: 24 h, was 3 days; a full agents disk needs the room sooner)
DEFAULT_DEPS_IDLE_S = GRACE_S
#: touched by the GC each time it sees a live link to the entry
ALIVE = ".last-alive"
#: the one install mode of a fill (part of the key): a full development tree,
#: whatever NODE_ENV, `omit` or `ignore-scripts` say in the environment or an
#: npmrc; a production turn never gets a tree it did not ask for (see npm_shim)
INSTALL_ARGS = ("ci", "--include=dev", "--include=optional", "--include=peer",
                "--ignore-scripts=false")
#: performance-only options of the fill (not in the key)
FILL_OPTIONS = ("--prefer-offline", "--no-audit", "--no-fund")
#: the only variables a fill sees (plus `LC_*`): no NODE_ENV, no other
#: npm setting than its download cache, nothing a postinstall could read
FILL_ENV = ("PATH", "HOME", "LANG", "LANGUAGE", "TMPDIR", "TMP", "TEMP",
            "npm_config_cache", "NPM_CONFIG_CACHE", "AMEESH_REAL_NPM")
#: set in every fill, whatever the environment says: sharp uses its prebuilt
#: libvips even where the host has one (pc-smichea: libvips 8.18.6), else it
#: builds from source and the fill fails (#249); not in the key: without a
#: global libvips it changes nothing, with one a fill never succeeded
FILL_FIXED_ENV = {"SHARP_IGNORE_GLOBAL_LIBVIPS": "1"}
#: an abandoned temporary fill (`<key>.tmp-<pid>-<random>`) is deleted after
#: this long, if no process uses it and its key's lock is free
TMP_SEP = ".tmp-"
#: longest walk of an abandoned temporary fill for its newest file (seconds);
#: past it the directory is kept
TEMP_WALK_S = 30.0
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
#: npm commands that install into node_modules
INSTALL_COMMANDS = ("ci", "clean-install", "install-clean", "isntall-clean", "ic",
                    "install", "i", "in", "ins", "inst", "insta", "instal", "isnt",
                    "isnta", "isntal", "isntall", "add",
                    "install-test", "it", "install-ci-test", "cit")
#: options of `npm ci` that do not change the installed tree
NEUTRAL_OPTIONS = ("--prefer-offline", "--offline", "--no-audit", "--no-fund",
                   "--silent", "--quiet", "-q", "-s", "--no-progress", "--progress=false",
                   "--audit=false", "--fund=false", "--no-update-notifier",
                   "--include=dev")
#: npm command names and aliases (npm 11): the first of them on the command
#: line is the command
NPM_COMMANDS = frozenset(INSTALL_COMMANDS + (
    "access", "adduser", "audit", "bugs", "cache", "completion", "config", "c", "dedupe",
    "ddp", "deprecate", "diff", "dist-tag", "dist-tags", "docs", "home", "doctor", "edit",
    "exec", "x", "explain", "why", "explore", "find-dupes", "fund", "get", "help",
    "hlep", "help-search", "init", "create", "innit", "issues", "link", "ln", "ll", "la",
    "login", "logout", "ls", "list", "org", "outdated", "owner", "author", "pack", "ping",
    "pkg", "prefix", "profile", "prune", "publish", "query", "rebuild", "rb", "repo",
    "restart", "root", "run", "run-script", "rum", "urn", "sbom", "search", "find", "s",
    "se", "set", "shrinkwrap", "star", "stars", "start", "stop", "team", "test", "t",
    "tst", "token", "undeprecate", "uninstall", "unlink", "remove", "rm", "r", "un",
    "unpublish", "unstar", "update", "up", "upgrade", "udpate", "version", "verison",
    "view", "info", "show", "v", "whoami"))
#: commands that write into node_modules besides the installs: in a review
#: worktree refused, elsewhere run by the real npm on a local tree
TREE_COMMANDS = ("dedupe", "ddp", "link", "ln", "prune", "rebuild", "rb", "uninstall",
                 "unlink", "remove", "rm", "r", "un", "update", "up", "upgrade", "udpate")
#: npm options that take a value when written without `=` (`--prefix DIR`)
VALUE_OPTIONS = ("--prefix", "-C", "--location", "-w", "--workspace", "--omit",
                 "--include", "--cache", "--registry", "--userconfig", "--globalconfig",
                 "--loglevel", "--tag", "--install-strategy", "--script-shell",
                 "--node-options", "--os", "--cpu", "--libc", "--before")
#: name of the wrapper's directory under the housekeeping state
SHIM_DIR = "bin-deps"


class FillError(RuntimeError):
    """The cache entry could not be filled."""


class ToolchainChanged(FillError):
    """The node or npm a fill runs with is not the one its key was made with
    (a pin changed within PROBE_TTL_S, a shim answering by directory): the
    entry is discarded, nothing is linked and nothing installed locally."""


# --------------------------------------------------------------------------
# roots and keys
# --------------------------------------------------------------------------

def nm_root(cache_root: str) -> str:
    return os.path.join(cache_root, NM_DIR)


#: how long a package's toolchain is kept (seconds): one command or one
#: turn start asks several times, the runner lives for days and a package's
#: pin can change at the same path
PROBE_TTL_S = 10.0
_probes: dict = {}
#: in the environment given to the installer: the directory npm ci runs FROM
#: (the package's: a version manager picks the same node and npm as for the
#: key), installing into the temporary entry with `--prefix`; never passed on
FILL_FROM = "AMEESH_FILL_FROM"


def _probe(command: list[str], cwd: str | None = None, path: str | None = None,
           env: dict | None = None) -> str:
    """The output of a toolchain probe run in `cwd` (where a version manager
    such as mise or asdf picks the version), "" if it fails."""
    if path is not None:
        env = dict(os.environ if env is None else env, PATH=path)
    try:
        return subprocess.run(command, capture_output=True, text=True, cwd=cwd, env=env,
                              timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def toolchain(pkg_dir: str | None = None, env: dict | None = None) -> dict:
    """The node and npm the package runs with, resolved TOGETHER from its
    directory and kept PROBE_TTL_S as one entry, so that a key and its fill
    never mix two pins: `node` (`v26.8.1`), `bindir` (its directory, "" if
    unknown), `npm` (the npm beside that node, else `$AMEESH_REAL_NPM` or
    the real npm of PATH, possibly a version manager's shim: the fill runs it
    from the package's directory too) and `npm_version`. Native modules are
    built for that node; that npm decides the layout of the tree."""
    env = os.environ if env is None else env
    memo = (pkg_dir, env.get("PATH"), env.get("AMEESH_REAL_NPM"))
    hit = _probes.get(memo)
    if hit is not None and time.monotonic() - hit[0] < PROBE_TTL_S:
        return dict(hit[1])
    lines = _probe(["node", "-p", "process.version + '\\n' + process.execPath"],
                   pkg_dir).splitlines()
    version = lines[0].strip() if lines else ""
    execpath = lines[1].strip() if len(lines) > 1 else ""
    bindir = os.path.dirname(execpath) if os.path.isabs(execpath) else ""
    beside = os.path.join(bindir, "npm") if bindir else ""
    npm = beside if beside and os.access(beside, os.X_OK) else real_npm(env)
    path = bindir + os.pathsep + env.get("PATH", "") if bindir else None
    tool = {"node": version, "bindir": bindir, "npm": npm,
            "npm_version": _probe([npm, "--version"], pkg_dir, path) if npm else ""}
    _probes[memo] = (time.monotonic(), tool)
    return dict(tool)


def node_version(pkg_dir: str | None = None) -> str:
    """Version of the `node` the package runs with (`toolchain`)."""
    return toolchain(pkg_dir)["node"]


def npm_version(pkg_dir: str | None = None) -> str:
    """Version of the npm the package runs with (`toolchain`)."""
    return toolchain(pkg_dir)["npm_version"]


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


def package_label(pkg_dir: str) -> str:
    """The `<package>` level of the entry: the package's path relative to its
    git worktree (`mobile`, `shared/remote-help`, `_root` for the top), so
    that every worktree of a repository names it the same; outside git, the
    directory's name. Part of the key: the link target depends on nothing
    else than the key."""
    pkg_dir = os.path.abspath(pkg_dir)
    top = _git(pkg_dir, "rev-parse", "--show-toplevel")
    if top is not None and top.returncode == 0 and top.stdout.strip():
        rel = os.path.relpath(pkg_dir, os.path.realpath(top.stdout.strip()))
        if rel.startswith(".."):
            rel = os.path.relpath(os.path.realpath(pkg_dir),
                                  os.path.realpath(top.stdout.strip()))
        if rel == ".":
            return "_root"
        if not rel.startswith(".."):
            return "/".join(_safe_name(part) for part in rel.split(os.sep))
    return _safe_name(os.path.basename(pkg_dir))


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).lstrip(".") \
        or "package"


def cache_key(pkg_dir: str, *, node: str | None = None, npm: str | None = None,
              label: str | None = None, tool: dict | None = None) -> str:
    """sha256 of what decides the content of `node_modules`: the key files
    (`key_files`), the lifecycle scripts, the package's path in its
    repository (`package_label`), the install mode (INSTALL_ARGS), the node
    and npm versions, the OS and the CPU (native modules,
    platform-specific packages)."""
    digest = hashlib.sha256()

    def feed(label: str, data: bytes) -> None:
        digest.update(label.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)

    feed("version", KEY_VERSION.encode())
    if node is None or npm is None:
        tool = tool or toolchain(pkg_dir)
    feed("node", (tool["node"] if node is None else node).encode())
    feed("npm", (tool["npm_version"] if npm is None else npm).encode())
    feed("platform", ("%s/%s" % (platform.SYSTEM, platform.machine())).encode())
    feed("label", (package_label(pkg_dir) if label is None else label).encode())
    feed("mode", " ".join(INSTALL_ARGS).encode())
    feed("lifecycle", json.dumps(_lifecycle(pkg_dir), sort_keys=True).encode())
    for rel in key_files(pkg_dir):
        with open(os.path.join(pkg_dir, rel), "rb") as fh:
            feed("file:" + rel, fh.read())
    return digest.hexdigest()


def entry_of(root: str, key: str) -> str:
    return os.path.join(root, key)


def entry_modules(root: str, key: str, label: str) -> str:
    return os.path.join(root, key, label, NODE_MODULES)


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


def fill_env(env: dict | None = None) -> dict:
    """The environment of a fill: a WHITELIST only (FILL_ENV, `LC_*`), so
    that nothing outside the key (NODE_ENV, `npm_config_*`, a variable a
    postinstall reads) can change the tree; INSTALL_ARGS say the mode,
    FILL_FIXED_ENV the variables every fill gets."""
    out = {}
    for name, value in (os.environ if env is None else env).items():
        if name in FILL_ENV or name.startswith("LC_"):
            out[name] = value
    out.update(FILL_FIXED_ENV)
    return out


def npm_ci(directory: str, env: dict | None = None, *, lock_fd: int | None = None) -> None:
    """The default installer: the real `npm ci` in the one install mode
    (INSTALL_ARGS), gently (nice), cache first. `lock_fd`, the fill lock, is
    inherited by npm and its install scripts: the lock outlives this process
    until the last of them exits."""
    origin = (env or {}).get(FILL_FROM)
    env = fill_env(env)
    command = [real_npm(env) or "npm", *INSTALL_ARGS, *FILL_OPTIONS]
    cwd = directory
    if origin and os.path.isdir(origin):
        # from the package's directory, into `directory`
        cwd, command = origin, command + ["--prefix", directory]
    nice = shutil.which("nice")
    if nice:
        command = [nice, "-n", "10"] + command
    try:
        done = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                              text=True, pass_fds=(lock_fd,) if lock_fd is not None else ())
    except OSError as exc:
        raise FillError("npm ci could not start: %s" % exc)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-5:]
        raise FillError("npm ci failed (%d): %s" % (done.returncode, " | ".join(tail)))


def _check_toolchain(pkg_dir: str, tool: dict, env: dict, when: str) -> None:
    """Raise ToolchainChanged if the fill's environment (`env`, run from
    FILL_FROM, as npm ci is) resolves another node or npm than the key's
    `tool`, before or after the install; only what `tool` resolved is
    checked."""
    run_env, cwd = fill_env(env), env.get(FILL_FROM)
    seen = {}
    if tool.get("bindir"):
        seen["node"] = _probe(["node", "-p", "process.version"], cwd, env=run_env)
    if tool.get("npm"):
        seen["npm_version"] = _probe([tool["npm"], "--version"], cwd, env=run_env)
    changed = ["%s %s, key %s" % (name, got or "?", tool[name])
               for name, got in seen.items() if got != tool[name]]
    if changed:
        for memo in [m for m in _probes if m[0] == pkg_dir]:
            del _probes[memo]               # the next attempt resolves again
        raise ToolchainChanged("toolchain changed %s the fill (%s): entry discarded"
                               % (when, "; ".join(changed)))


def fill(pkg_dir: str, root: str, key: str, *, installer=npm_ci, env: dict | None = None,
         wait: float = FILL_WAIT_S, tool: dict | None = None) -> dict:
    """Fill the entry `key` from `pkg_dir` if it is not complete yet; returns
    its marker. `tool`: the toolchain the key was made with (`toolchain`).
    Raises FillError (lock not obtained, install failed)."""
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
        if os.path.lexists(entry):          # an unpublished entry, under our lock
            remove_tree(entry, root)
        # a unique temporary directory, renamed into place once complete: a
        # stray writer left by a dead fill (an install script still running)
        # only ever writes into ITS temporary directory, never into a
        # published entry; the GC deletes abandoned ones (`gc_node_modules`)
        work = "%s%s%d-%s" % (key, TMP_SEP, os.getpid(), os.urandom(4).hex())
        temp = os.path.join(root, work)
        name = package_label(pkg_dir)
        stage = os.path.join(temp, name)
        os.makedirs(stage, mode=0o755)
        os.makedirs(os.path.join(temp, LINKS), mode=0o755)
        copied = key_files(pkg_dir)
        for rel in copied:
            dest = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(pkg_dir, rel), dest)
        # the toolchain of the key: its node first on PATH, its npm, run from
        # the package's directory (a version manager's shim picks the same)
        env = dict(os.environ if env is None else env)
        tool = tool or toolchain(pkg_dir, env)
        if tool["bindir"]:
            env["PATH"] = tool["bindir"] + os.pathsep + env.get("PATH", "")
        if tool["npm"]:
            env["AMEESH_REAL_NPM"] = tool["npm"]
        env[FILL_FROM] = os.path.abspath(pkg_dir)
        started = time.time()
        try:
            _check_toolchain(pkg_dir, tool, env, "before")
            try:
                installer(stage, env, lock_fd=lock.fileno())
            except FillError:
                # a failed install whose toolchain changed meanwhile: no
                # fallback either (ToolchainChanged), and the memo is dropped
                _check_toolchain(pkg_dir, tool, env, "during")
                raise
            _check_toolchain(pkg_dir, tool, env, "after")
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
            marker = {"schema": SCHEMA, "key": key, "package": name, "label": name,
                      "source": os.path.abspath(pkg_dir), "node": tool["node"],
                      "npm": tool["npm_version"], "mode": " ".join(INSTALL_ARGS),
                      "created": time.time(),
                      "install_s": round(time.time() - started, 1),
                      "files": files, "bytes": size}
            with open(os.path.join(temp, COMPLETE), "w", encoding="utf-8") as fh:
                json.dump(marker, fh, sort_keys=True)
            published = False
            if not os.path.lexists(entry):
                try:
                    os.rename(temp, entry)    # atomic; refused if entry not empty
                    published = True
                except OSError:
                    pass
            if not published:
                marker = read_marker(entry)
                if not marker:
                    raise FillError("entry %s appeared unpublished during the fill"
                                    % key[:12])
                remove_tree(temp, root)       # someone else published first
        except BaseException:
            try:
                if os.path.lexists(temp):
                    remove_tree(temp, root)
            except OSError:
                pass
            raise
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
    try:
        os.replace(tmp, link)       # refused over a directory: never deletes one
    except OSError:
        os.unlink(tmp)
        raise


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


def _packagers(pkg_dir: str) -> list[str]:
    """Conservative guard: root/sibling tooling can package any app in git.

    Inspect manifests even without a lockfile. Never cross into nested git
    repositories, generated trees, hidden directories or directory symlinks.
    """
    root = worktree_root(pkg_dir)
    found = set(deps_packagers.packagers(pkg_dir))
    if not root:
        return sorted(found)
    stack = [root]
    while stack:
        current = stack.pop()
        if current != pkg_dir and os.path.isfile(os.path.join(current, "package.json")):
            found.update(deps_packagers.packagers(current))
        with os.scandir(current) as entries:
            for entry in entries:
                if (entry.name.startswith(".") or entry.name in SKIP_DIRS
                        or not entry.is_dir(follow_symlinks=False)
                        or os.path.lexists(os.path.join(entry.path, ".git"))):
                    continue
                stack.append(entry.path)
    return sorted(found)


def link_package(pkg_dir: str, cache_root: str, *, fill_missing: bool = True,
                 replace: bool = False, installer=npm_ci, env: dict | None = None,
                 wait: float = FILL_WAIT_S, force: bool = False) -> dict:
    """Give `pkg_dir` its shared `node_modules`; returns what was done:

    * `linked`: the symlink now points to the complete entry (`filled`
      also says whether this call filled it);
    * `local-kept`: a real `node_modules` directory is there and `replace`
      is false: nothing touched;
    * `not-cached`: no complete entry and `fill_missing` is false;
    * `local`: the entry could not be filled, `npm ci` ran locally
      (`reason`), or `local-failed` if that failed too;
    * `packager-refused`: this package needs a local tree for packaging
      (`force` permits a development link); `package-invalid`: its manifest
      could not be inspected;
    * `no-lockfile`."""
    pkg_dir = os.path.abspath(pkg_dir)
    result = {"package": pkg_dir}
    if not os.path.isfile(os.path.join(pkg_dir, LOCKFILE)):
        return dict(result, status="no-lockfile")
    try:
        packagers = _packagers(pkg_dir)
    except (OSError, ValueError) as exc:
        return dict(result, status="package-invalid", error=str(exc))
    if packagers:
        result["packagers"] = packagers
        if not force:
            return dict(result, status="packager-refused",
                        reason=deps_packagers.local_reason(packagers) +
                        "; --force overrides this refusal for development only")
    root = nm_root(cache_root)
    label = package_label(pkg_dir)
    tool = toolchain(pkg_dir, env)        # once: the key and the fill agree
    key = cache_key(pkg_dir, label=label, tool=tool)
    target = entry_modules(root, key, label)
    link = os.path.join(pkg_dir, NODE_MODULES)
    result.update(key=key, target=target)
    had_real = os.path.lexists(link) and not os.path.islink(link)
    if had_real:
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
            fill(pkg_dir, root, key, installer=installer, env=env, wait=wait, tool=tool)
            filled = True
        except ToolchainChanged as exc:
            return dict(result, status="local-kept", reason=str(exc))   # nothing linked
        except FillError as exc:
            if os.path.isdir(link) and not os.path.islink(link) and \
                    not (replace and had_real):
                # created during the failed fill (a local npm ci): never redone
                return dict(result, status="local-kept", reason=str(exc))
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
            if not replace:
                # created during the fill (a local npm ci, the user): never touched
                return dict(result, status="local-kept", filled=filled)
            old = os.path.join(pkg_dir, ".node_modules.ameesh-old-%d" % os.getpid())
            os.rename(link, old)
        # write-ahead: recorded before it exists, so a crash between the two
        # leaves a stale record (pruned), never a live link the GC cannot see
        _record_link(entry_of(root, key), link)
        try:
            _symlink(target, link)
        except OSError:
            if os.path.isdir(link) and not os.path.islink(link) and old is None:
                return dict(result, status="local-kept", filled=filled)   # just appeared
            raise
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
              wait: float = FILL_WAIT_S, force: bool = False) -> list[dict]:
    """`link_package` for every package under `top`."""
    return [link_package(p, cache_root, fill_missing=fill_missing, replace=replace,
                         installer=installer, env=env, wait=wait, force=force)
            for p in find_packages(top)]


def link_ready(top: str, cache_root: str) -> list[dict]:
    """The runner's hook at the start of a turn (cheap, never installs): link
    the packages of the git worktree holding `top` that have NO
    `node_modules` and whose entry is already complete. Existing shared links
    in packaging projects are removed without installing; run deps unlink to
    install the required local tree. Nothing outside git:
    a turn started in a parent directory (`~/development/manaty`) would reach
    every worktree under it, review worktrees included (#249: 262 links from
    one turn). Never in a review worktree."""
    root = worktree_root(top) if os.path.isdir(top) else None
    if not root or is_review_worktree(top):
        return []
    out = []
    for pkg in _walk(root, SEARCH_DEPTH):
        if not (os.path.isfile(os.path.join(pkg, LOCKFILE)) or
                os.path.isfile(os.path.join(pkg, "package.json"))):
            continue
        if is_review_worktree(pkg):
            continue
        if worktree_root(pkg) != root:      # another worktree nested in this one
            continue
        link = os.path.join(pkg, NODE_MODULES)
        if is_shared_link(link, cache_root):
            try:
                packagers = _packagers(pkg)
            except (OSError, ValueError) as exc:
                os.unlink(link)  # an unreadable manifest cannot preserve a shared tree
                out.append(dict(package=pkg, status="unlinked", error=str(exc)))
            else:
                if packagers:
                    os.unlink(link)
                    out.append(dict(package=pkg, status="unlinked", packagers=packagers,
                                    reason=deps_packagers.local_reason(packagers)))
            continue
        if os.path.lexists(link):
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
    if not os.path.isfile(os.path.join(pkg_dir, LOCKFILE)):
        return dict(result, status="no-lockfile", error="package-lock.json required for npm ci")
    if os.path.lexists(link) and not os.path.islink(link):
        return dict(result, status="local-kept")
    if os.path.islink(link):
        os.unlink(link)
    try:
        installer(os.path.abspath(pkg_dir), env)
    except FillError as exc:
        return dict(result, status="local-failed", error=str(exc))
    return dict(result, status="local", reason="local node_modules installed with npm ci")


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


def _npm_words(args: list[str]) -> tuple[str | None, list[str], list[str], dict]:
    """(command, positional arguments, options, option values) of an npm
    command line. The command is the first word that is an npm command name
    (NPM_COMMANDS), wherever it sits (`npm --fetch-retries 2 ci`): the
    parser never needs to know which options take a value. The word after
    an option of VALUE_OPTIONS written without `=` is that option's value,
    never the command (`npm --prefix test ci`)."""
    command, positional, options, values = None, [], [], {}
    words = list(args)
    index = 0
    while index < len(words):
        arg = words[index]
        index += 1
        if arg == "--":
            if command is not None:
                positional.extend(words[index:])
            break
        if arg.startswith("-"):
            name, eq, value = arg.partition("=")
            if not eq and name in VALUE_OPTIONS and index < len(words):
                value = words[index]
                index += 1
                arg = "%s=%s" % (name, value)
            options.append(arg)
            values[{"-C": "--prefix", "-w": "--workspace"}.get(name, name)] = value
        elif command is None:
            if arg in NPM_COMMANDS:
                command = arg
        else:
            positional.append(arg)
    return command, positional, options, values


def _global(options: list[str], values: dict) -> bool:
    """`-g`, `--global` (not `--global=false`), `--location=global`."""
    for option in options:
        name, _eq, value = option.partition("=")
        if name in ("-g", "--global") and value.lower() not in ("false", "0"):
            return True
    return values.get("--location", "").lower() == "global"


def _wants_other_tree(env: dict) -> bool:
    """The environment asks npm for a tree that is not the full development
    one of the cache (NODE_ENV=production, `omit`, `production`, `only`)."""
    for name, value in env.items():
        lowered = name.lower().replace("-", "_")
        if lowered == "node_env" and value.strip().lower() == "production":
            return True
        if lowered in ("npm_config_omit", "npm_config_only") and value.strip():
            return True
        if lowered == "npm_config_production" and value.strip().lower() not in \
                ("", "false", "0"):
            return True
    return False


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
    the real npm (`execute`, `os.execve` by default) when it lets it run.

    Nothing marks the real npm as "already wrapped": `real_npm` never returns
    the wrapper, so there is no recursion, and an `npm ci` run by a script of
    `npm run …` comes back through the wrapper like any other."""
    env = dict(os.environ if env is None else env)
    cwd = os.getcwd() if cwd is None else cwd
    real = real_npm(env)

    def passthrough() -> int:
        if real is None:
            _say("no npm found on PATH")
            return 127
        run = execute or os.execve
        return run(real, [real] + list(args), env) or 0

    mode = (env.get(DEPS_ENV) or "").strip().lower()
    if mode in ("off", "0", "no", "none"):
        return passthrough()
    command, positional, options, values = _npm_words(args)
    where = cwd
    if values.get("--prefix"):
        where = os.path.join(cwd, os.path.expanduser(values["--prefix"]))
    # in a review worktree, no parsing to get wrong: ANY word that names an
    # install-type command refuses (`npm run link` too; false positives are
    # fine there)
    named = [a for a in args if a in INSTALL_COMMANDS + TREE_COMMANDS]
    if named and mode != "allow-review" and (is_review_worktree(where)
                                             or is_review_worktree(cwd)):
        _say("`npm %s` refused in a review worktree (%s): reviews get no dependencies "
             "(#248); run the tests in your main worktree (check out the branch "
             "there). AMEESH_DEPS=allow-review to override."
             % (named[0], worktree_root(where) or where))
        return 1
    # elsewhere a form the parser misses passes through: the shared tree is
    # read-only, a stray install fails (EACCES) and changes nothing
    if command not in INSTALL_COMMANDS + TREE_COMMANDS or _global(options, values):
        return passthrough()                      # global: outside any worktree
    pkg = package_dir(where)
    if pkg is None or not os.path.isfile(os.path.join(pkg, LOCKFILE)):
        return passthrough()
    link = os.path.join(pkg, NODE_MODULES)
    if command in INSTALL_COMMANDS:
        try:
            packagers = _packagers(pkg)
        except (OSError, ValueError) as exc:
            _say("invalid package.json: %s" % exc)
            return 1
        if packagers:
            if os.path.islink(link):
                os.unlink(link)
            _say("%s: running the real npm locally" % deps_packagers.local_reason(packagers))
            return passthrough()
    if command in TREE_COMMANDS:
        if os.path.islink(link):
            # a local tree from the lockfile first (never an empty one), then
            # the command on it
            done = make_local(pkg, installer=installer, env=env)
            if done["status"] != "local":
                _say("`npm %s`: no local tree for %s (%s)"
                     % (command, pkg, done.get("error") or done["status"]))
                return 1
            _say("shared node_modules replaced by a local tree in %s for `npm %s`"
                 % (pkg, command))
        return passthrough()
    ci = command in ("ci", "clean-install", "install-clean", "isntall-clean", "ic")
    plain = ci or (command not in ("install-test", "it", "install-ci-test", "cit")
                   and not positional)
    other = [o for o in options if o.partition("=")[0] not in ("--prefix", "-C")]
    neutral = all(o in NEUTRAL_OPTIONS or o.startswith("--loglevel") for o in other)
    if plain and neutral and not _wants_other_tree(env) and lock_in_sync(pkg):
        try:
            cache_root = resolve_cache_root(env)
        except Exception as exc:
            _say("cache root unknown (%s): plain npm" % exc)
            if os.path.islink(link):
                os.unlink(link)
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

def _points_into(link: str, entry: str) -> bool:
    try:
        target = os.path.normpath(os.path.join(os.path.dirname(link), os.readlink(link)))
    except OSError:
        return False
    return target.startswith(os.path.normpath(entry) + "/")


def live_links(entry: str, *, prune: bool = True) -> list[str]:
    """Recorded links that still point into `entry`; stale records are
    pruned unless `prune` is false (a dry run writes nothing)."""
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
        if link and os.path.islink(link) and _points_into(link, entry):
            out.append(link)
        elif prune:
            try:
                os.unlink(record)
            except OSError:
                pass
    return out


def _label_of(entry: str, marker: dict | None) -> str | None:
    if marker and marker.get("label"):
        return marker["label"]
    for current, dirs, _files in os.walk(entry):
        if NODE_MODULES in dirs:
            return os.path.relpath(current, entry)
        dirs[:] = [d for d in dirs if d != LINKS and not d.startswith(".")]
    return None


def scan_worktrees(entry: str, label: str | None, worktrees) -> list[str]:
    """Links into `entry` found in the known worktrees (`<wt>/<label>/
    node_modules`): what a crash, a copy or a hand-made link left unrecorded."""
    if not label:
        return []
    rel = "" if label == "_root" else label
    found = []
    for top in worktrees or ():
        link = os.path.join(top, rel, NODE_MODULES)
        if os.path.islink(link) and _points_into(link, entry):
            found.append(link)
    return found


def _mtime(path: str) -> float:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return 0.0


def _gc_reason(entry: str, *, now: float, idle: float, worktrees, holders,
               write: bool, kept: list | None = None) -> str | None:
    """Why `entry` can go, or None to keep it. Read again under the lock
    before any deletion: never trusts an earlier listing."""
    marker = read_marker(entry)
    label = _label_of(entry, marker)
    links = live_links(entry, prune=write) + scan_worktrees(entry, label, worktrees)
    if links:
        if write:
            for link in links:
                _record_link(entry, link)
            with open(os.path.join(entry, ALIVE), "w", encoding="utf-8") as fh:
                fh.write(str(now))
        return None
    if marker:
        links_dir = os.path.join(entry, LINKS)
        try:
            last_link = max([_mtime(os.path.join(links_dir, n))
                             for n in os.listdir(links_dir)] or [0.0])
        except OSError:
            last_link = 0.0
        since = max(float(marker.get("created") or 0), last_link,
                    _mtime(os.path.join(entry, ALIVE)))
        if now - since < max(idle, GRACE_S):
            return None
        why = "no worktree links to it since %s" % time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(since))
    else:
        if now - _mtime(entry) < PARTIAL_MIN_AGE_S:
            return None
        why = "partial fill (no %s)" % COMPLETE
    try:
        users = holders(entry)
    except platform.NotAvailable as exc:
        if kept is not None:
            kept.append({"kind": "deps", "action": "kept", "path": entry,
                         "bytes": int((marker or {}).get("bytes") or 0),
                         "detail": "file holders unreadable: %s" % exc})
        return None                        # cannot tell: kept (fail-closed)
    if users:
        return None                        # a dev server, a test run…
    return why


def _gc_temp(item: dict, root: str, *, now: float, holders, dry_run: bool) -> list[dict]:
    """An abandoned temporary fill: deleted after GRACE_S without change, if
    no process uses it (a stray install script) and its key's lock is free
    (a fill in progress)."""
    temp = item["path"]
    newest = _newest(temp, deadline=time.monotonic() + TEMP_WALK_S)
    if newest is None or now - newest < GRACE_S:
        return []                          # walk not finished: kept
    try:
        if holders(temp):
            return []
    except platform.NotAvailable:
        return []
    line = {"kind": "deps", "action": "deleted", "path": temp,
            "bytes": int(item["marker"].get("bytes") or 0),
            "detail": "abandoned temporary fill"}
    if dry_run:
        return [dict(line, action="would-delete")]
    lock = _lock(os.path.join(root, item["key"] + ".lock"), wait=0)
    if lock is None:
        return []
    try:
        remove_tree(temp, root)
    except OSError as exc:
        return [dict(line, action="kept", detail="abandoned temporary fill ; deletion "
                                                 "failed: %s" % exc)]
    finally:
        lock.close()
    return [line]


def _newest(top: str, *, deadline: float) -> float | None:
    """Newest mtime of `top` and everything under it (never through a
    symlink), or None if the walk did not finish before `deadline`
    (`time.monotonic`)."""
    newest = _mtime(top)
    stack = [top]
    while stack:
        if time.monotonic() > deadline:
            return None
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for item in it:
                    try:
                        st = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, st.st_mtime)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(item.path)
        except OSError:
            continue
    return newest


def _holders(entry: str, *, allow=()) -> list[dict]:
    # #5029/#5046 : l'appelant passe la politique d'illisibilité de l'hôte
    # (`policy.housekeeping.unreadable_ok`) ;
    # l'exécuteur ne tient jamais un cache de dépendances.
    return platform.holders(entry, mapped=True, allow=allow)


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
        temp = TMP_SEP in name
        out.append({"key": name.split(TMP_SEP)[0], "path": full, "temp": temp,
                    "complete": marker is not None and not temp,
                    "marker": marker or {}, "mtime": mtime, "last_link": last_link})
    return out


def gc_node_modules(cache_root: str, *, now: float | None = None,
                    idle: float = DEFAULT_DEPS_IDLE_S, dry_run: bool = False,
                    worktrees=None, holders=None, budget=None,
                    reserve: float = 0.0, allow=()) -> list[dict]:
    """Delete the entries no worktree links to any more and partial ones left
    by a dead fill. A complete entry goes only after `idle` (at least GRACE_S)
    since its creation, its last link and the last time a link to it was seen
    alive; never while a process uses a file in it (cwd, open file,
    executable, mapped file: `platform.holders`), never while its lock is
    held (a fill, through npm and its scripts, or a link being made). Links
    are the recorded ones plus those found in `worktrees` (an iterable of
    worktree tops, or a callable returning one, also used for the bounded
    broken-link scan). Lock files are never deleted: two fillers lock the same
    inode. Returns housekeeping log lines (`kind` "deps").

    `allow` is the host's explicit unreadable-process policy, passed to
    platform.holders for complete entries and abandoned fills alike. Unknown
    unreadable processes remain blocking; observed holders always block.

    `budget` (#3802), a `housekeeping.PassBudget`: the loop stops at its next
    entry when the pass budget is spent, without starting another deletion.
    `reserve` keeps that many seconds for the phases that follow (worktrees,
    tail). If the budget runs out DURING the worktree inventory, the list is
    INCOMPLETE: no entry is deleted (a link nobody recorded may live in an
    omitted worktree), and the kept line says it. The check is made again
    under the lock, right before the deletion (#3802 B/P1, cx1)."""
    now = time.time() if now is None else float(now)
    # #5046: the same explicit host policy as tmp/worktrees. An actual
    # observed holder still blocks; only an allowed unreadable daemon is excused.
    holders = holders or (lambda entry: _holders(entry, allow=allow))
    root = nm_root(cache_root)
    out = []
    known = None
    inventory_failed = False

    def tops():
        nonlocal known, inventory_failed
        if known is None:
            try:
                known = list(worktrees() if callable(worktrees) else (worktrees or ()))
            except Exception:
                inventory_failed = True
                known = []
        return known

    def exhausted() -> bool:
        return inventory_failed or (budget is not None and (
            budget.expired() or budget.remaining() <= max(0.0, reserve)))

    for item in entries(cache_root):
        if exhausted():
            break                      # #3802: a bounded host pass
        entry = item["path"]
        if item["temp"]:
            out += _gc_temp(item, root, now=now, holders=holders, dry_run=dry_run)
            continue
        # cheap filter first (no worktree scan unless needed); it refreshes
        # `.last-alive` of the entries it sees linked
        if not dry_run and _gc_reason(entry, now=now, idle=idle, worktrees=(),
                                      holders=lambda _p: [], write=True) is None:
            continue
        # links found by the scan are recorded (kept entries only; no dry run)
        found = tops()
        if exhausted():
            # #3802 B/P1 : l'inventaire a pu être coupé par le budget ou
            # échouer ; une entrée ancienne peut avoir un lien vivant dans un
            # worktree omis. Inventaire incomplet : aucune suppression.
            out.append({"kind": "deps", "action": "kept", "path": entry,
                        "bytes": int(item["marker"].get("bytes") or 0),
                        "detail": "worktree inventory incomplete (%s): kept" % (
                            "unreadable" if inventory_failed
                            else "pass budget reached")})
            break
        why = _gc_reason(entry, now=now, idle=idle, worktrees=found, holders=holders,
                         write=not dry_run, kept=out)
        if why is None:
            continue
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
            why = _gc_reason(entry, now=now, idle=idle, worktrees=tops(), holders=holders,
                             write=True, kept=out)
            if why is None:
                continue
            if exhausted():
                # #3802 B/P1 : re-vérifier sous le verrou, juste avant l'effet
                out.append(dict(line, action="kept",
                                detail="%s ; pass budget reached before deletion: kept"
                                       % why))
                break
            remove_tree(entry, root)
        except OSError as exc:
            out.append(dict(line, action="kept", detail="%s ; deletion failed: %s"
                            % (why, exc)))
            continue
        finally:
            lock.close()
        out.append(dict(line, detail=why))
    seen_links = set()
    for top in tops():
        if exhausted():
            break
        for directory in _walk(top, SEARCH_DEPTH):
            if exhausted():
                break
            link = os.path.join(directory, NODE_MODULES)
            if link not in seen_links and os.path.islink(link) and not os.path.exists(link):
                try:
                    target = os.readlink(link)
                except OSError:
                    continue               # link removed concurrently
                seen_links.add(link)
                out.append({"kind": "deps", "action": "kept", "path": link,
                            "bytes": 0, "detail": "broken node_modules link: %s" %
                            target})

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
        label = package_label(pkg)
        key = cache_key(pkg, label=label)
        link = os.path.join(pkg, NODE_MODULES)
        target = entry_modules(root, key, label)
        if os.path.islink(link):
            state = "linked" if os.readlink(link) == target else "stale-link"
        elif os.path.lexists(link):
            state = "local"
        else:
            state = "absent"
        item = {"package": pkg, "key": key, "node_modules": state,
                "cached": read_marker(entry_of(root, key)) is not None}
        try:
            packagers = _packagers(pkg)
        except (OSError, ValueError) as exc:
            item["warning"] = "invalid package.json: %s" % exc
        else:
            if packagers:
                item["packagers"] = packagers
                if os.path.islink(link):
                    item["warning"] = deps_packagers.local_reason(packagers)
        out.append(item)
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
    p_link.add_argument("--force", action="store_true",
                        help="allow packager projects to link for development; unlink before release")
    p_link.add_argument("--json", action="store_true")
    p_local = sub.add_parser(
        "local", aliases=["unlink"], help="replace the link by a local node_modules (npm ci), "
                                         "before packaging or npm install <pkg>")
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
                               replace=args.replace, force=args.force)
    elif args.command in ("local", "unlink"):
        items = []
        for top in paths:
            packages = [p for p in _walk(top, SEARCH_DEPTH)
                        if os.path.isfile(os.path.join(p, "package.json")) or
                        os.path.isfile(os.path.join(p, LOCKFILE))]
            items += [make_local(p) for p in packages] if packages else [
                dict(package=os.path.abspath(top), status="no-lockfile",
                     error="no package-lock.json found in this path")]
    elif args.command == "status":
        items = [i for top in paths for i in _status(top, cache_root)]
    else:
        items = entries(cache_root)
    if args.json:
        print(json.dumps({"schema": SCHEMA, "cache_root": cache_root, "items": items},
                         ensure_ascii=False, sort_keys=True))
    elif args.command in ("link", "local", "unlink"):
        for item in items:
            print(_describe(item))
        if not items:
            print("  no package-lock.json found")
    elif args.command == "status":
        for item in items:
            print("  %-48s node_modules %-10s cache %s  key %s" % (
                item["package"], item["node_modules"],
                "complete" if item["cached"] else "missing", item["key"][:12]))
            if item.get("warning"):
                print("    WARNING: %s" % item["warning"])
    else:
        for item in items:
            marker = item["marker"]
            print("  %s  %-8s %-10s %7s files %9s  %d link(s)" % (
                item["key"][:12], marker.get("package", "?"),
                "complete" if item["complete"] else "PARTIAL",
                marker.get("files", "?"),
                "%.1f MiB" % (marker.get("bytes", 0) / 1024 ** 2),
                len(live_links(item["path"]))))
    failed = any(i.get("status") in ("local-failed", "packager-refused", "package-invalid",
                                     "no-lockfile") for i in items)
    if args.command == "unlink" and not items:
        failed = True
    return 1 if failed else 0


__all__ = ["cache_key", "key_files", "find_packages", "fill", "link_package", "link_tree",
           "link_ready", "make_local", "is_shared_link", "is_review_worktree", "npm_shim",
           "install_shim", "gc_node_modules", "entries", "live_links", "nm_root", "FillError",
           "main"]


if __name__ == "__main__":
    sys.exit(main())
