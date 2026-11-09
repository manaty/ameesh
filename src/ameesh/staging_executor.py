# SPDX-License-Identifier: AGPL-3.0-only
"""#4880: a durable staging request, carried by an elected service.

The service runs on every configured candidate, outside agent turns. Only
the current role holder may start an attempt. A shared execution lease keeps
an old holder and its replacement from overlapping. After an interrupted
attempt, a site-supplied quiescence probe MUST prove the old execution and
its remote operations stopped before another attempt can start.

#5500: the shared database is writable by every agent under the same role,
so a request read there is untrusted input. Two activation conditions close
on doubt: nothing read or run may be writable by another account, and the
requested commit's CI is read at the forge itself, never from the database.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

from ameesh import role_run, staging_watch, storage

EVENT = "staging.request"
#: One attempt's worktree is named after its run, so the tree kept after a
#: lost lease can be found and removed once the stop is proved (#4898 N3).
WORKTREE_PREFIX = "ameesh-release-staging-"
#: #5500: the forge is read anonymously (public repository, no new secret).
FORGE_API = "https://api.github.com"
#: Every refusal of the forge check starts with this prefix.
CI_REFUSAL = "forge CI: "
#: Anonymous quota is 60 requests/h per address: a refused request is read
#: again at most this often, while the timer passes every minute.
CI_RECHECK_S = 300
#: The required aggregate of ameesh (decision #128512).
TESTS_WORKFLOW = ".github/workflows/tests.yml"
#: Runs of these events judge the commit; schedule and workflow_run runs
#: only carry the branch head's SHA.
_GATING_EVENTS = ("push", "pull_request", "pull_request_target")
_NEGATIVE = ("failure", "timed_out", "startup_failure")
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Every fingerprint refusal starts with this prefix (#5665).
FINGERPRINT_REFUSAL = "fingerprint: "
#: The probe's default python, as in quiescence-staging.sh.
PROBE_PYTHON = ".local/share/ameesh/venv/bin/python"
#: #5743: a stable venv that only carries launchers (bascule.py) holds this
#: marker; its live version is the target of the sibling link `venv-courant`.
BASCULE_LAUNCHERS = ".bascule-lanceurs"
BASCULE_CURRENT = "venv-courant"


class ExecutionLease(role_run._AlertsLease):
    """Reuse the deadline, renewal and process-group guards of role passes."""
    def __init__(self, db, role, host, key):
        super().__init__(db, role, host)
        self.key = "staging-execution:" + key
        self.owner = "staging-execution:%s:%s" % (host, uuid.uuid4().hex)


def settings(path: str) -> dict:
    data = json.loads(Path(path).read_text())
    candidates = data.get("candidates", [])
    if (not isinstance(candidates, list) or not all(
            isinstance(host, str) and host.strip() for host in candidates)
            or len(set(candidates)) < 2):
        raise ValueError("staging requires at least two declared candidates")
    for field in ("key", "project", "role", "repo", "bascule_env", "github"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError("staging configuration missing " + field)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", data["key"]):
        raise ValueError("invalid staging execution key")
    if not _SLUG.fullmatch(data["github"]):
        raise ValueError("github must be owner/repo")
    if not isinstance(data.get("arguments", []), list) or not all(
            isinstance(arg, str) for arg in data.get("arguments", [])):
        raise ValueError("staging arguments must be an argv list")
    probe = data.get("quiescence_probe")
    if probe is not None and (not isinstance(probe, list) or not probe
                              or not all(isinstance(arg, str) for arg in probe)):
        raise ValueError("quiescence_probe must be a nonempty argv list")
    # #5665: optional expected SHA256 per fingerprinted path; absent, the
    # fingerprints are only recorded.
    expected = data.get("expected_sha256", {})
    if not isinstance(expected, dict) or not all(
            isinstance(path, str) and os.path.isabs(path)
            and isinstance(digest, str) and _SHA256.fullmatch(digest)
            for path, digest in expected.items()):
        raise ValueError("expected_sha256 must map absolute paths to SHA256 digests")
    for key, default in (("timeout_s", staging_watch.STAGING_FALLBACK_S),
                         ("start_timeout_s", 300),
                         # #5450: the return step is bounded by its own
                         # duration and by the service unit's TimeoutStartSec.
                         ("return_timeout_s", 1800),
                         ("service_timeout_s", 41400)):
        value = float(data.get(key, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(key + " must be finite and positive")
        data[key] = value
    # #5500: the file itself is guarded; never a path named inside it.
    data["config_path"] = os.path.abspath(path)
    return data


# -- #5500: activation conditions -------------------------------------------

def _owners() -> set:
    """The executing account; root is trusted anyway (system directories)."""
    return {os.geteuid(), 0}


def _entry_problem(path: str, st, owners) -> str | None:
    if st.st_uid not in owners:
        return "%s owned by uid %d" % (path, st.st_uid)
    # A link's own mode means nothing; its target is checked separately.
    if not stat.S_ISLNK(st.st_mode) and st.st_mode & 0o022:
        return "%s writable by group or others (mode %o)" % (
            path, stat.S_IMODE(st.st_mode))
    return None


def path_problem(path: str, *, missing_ok: bool = False) -> str | None:
    """The path, its resolved target and every parent of both: owned by the
    executing account (or root), writable by neither group nor others."""
    owners = _owners()
    seen = set()
    for candidate in (os.path.abspath(path), os.path.realpath(path)):
        current = candidate
        while True:
            if current not in seen:
                seen.add(current)
                try:
                    st = os.lstat(current)
                except FileNotFoundError:
                    if not missing_ok:
                        return "%s missing" % current
                    st = None
                except OSError as exc:
                    return "%s unreadable (%s)" % (current, exc.strerror or exc)
                problem = st is not None and _entry_problem(current, st, owners)
                if problem:
                    return problem
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def tree_problem(root: str, _visited: set | None = None) -> str | None:
    """`path_problem` for root, then every entry below it, links included:
    a link to a folder has its whole target tree checked too."""
    problem = path_problem(root)
    if problem:
        return problem
    visited = set() if _visited is None else _visited
    visited.add(os.path.realpath(root))
    owners = _owners()
    targets = set()

    def fail(exc):
        raise exc
    try:
        for top, dirs, files in os.walk(root, onerror=fail):
            for name in dirs + files:
                entry = os.path.join(top, name)
                st = os.lstat(entry)
                problem = _entry_problem(entry, st, owners)
                if problem:
                    return problem
                if stat.S_ISLNK(st.st_mode):
                    targets.add(os.path.realpath(entry))
    except OSError as exc:
        return "%s unreadable (%s)" % (getattr(exc, "filename", None) or root,
                                       exc.strerror or exc)
    for target in sorted(targets):
        if os.path.isdir(target):
            if target in visited:
                continue
            problem = tree_problem(target, visited)
        else:
            # A dangling link in a source tree runs nothing; its parent is kept.
            problem = path_problem(target, missing_ok=True)
        if problem:
            return problem
    return None


def _program(argv0: str, env: dict) -> str | None:
    if os.path.isabs(argv0):
        return argv0
    return shutil.which(argv0, path=env.get("PATH", ""))


def _config_dirs() -> list:
    """The folders of the ameesh configuration (with its base and profiles)."""
    from ameesh import config
    explicit = next((os.environ[name] for name in ("AMEESH_CONFIG", "AGENT_MESH_CONFIG")
                     if os.environ.get(name)), "")
    paths = ([explicit] if explicit else
             [config.DEFAULT_CONFIG, config.LEGACY_CONFIG])
    return [os.path.dirname(config._expand(path)) for path in paths]


def startup_problem(config_path: str) -> str | None:
    """Before any configuration is read: the code that runs and its settings."""
    package = os.path.dirname(os.path.abspath(__file__))
    for root in (sys.prefix, package):
        problem = tree_problem(root)
        if problem:
            return "code: " + problem
    problem = path_problem(sys.executable)
    if problem:
        return "interpreter: " + problem
    for folder in [os.path.dirname(os.path.abspath(config_path))] + _config_dirs():
        if folder == os.path.dirname(os.path.abspath(config_path)) or os.path.isdir(folder):
            problem = tree_problem(folder)
            if problem:
                return "configuration: " + problem
    return None


def activation_problem(options: dict, env: dict) -> str | None:
    """#5500: the guard before every sequence, closed on doubt.

    The clone, the code, the configuration, BASCULE_ENV, the quiescence
    probe and every program launched (git, bash, the PATH they resolve
    through) belong to the executing account (or root) and are writable by
    no other account, parent folders included."""
    problem = startup_problem(options["config_path"])
    if problem:
        return problem
    problem = tree_problem(str(Path(options["repo"]).resolve()))
    if problem:
        return "clone: " + problem
    problem = path_problem(options["bascule_env"])
    if problem:
        return "BASCULE_ENV: " + problem
    for folder in env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []:
        if not folder:
            return "PATH: empty entry (the current folder)"
        if not os.path.isabs(folder):
            return "PATH: relative entry %s" % folder
        problem = path_problem(folder, missing_ok=True)
        if problem:
            return "PATH: " + problem
    programs = ["git", "bash"]
    probe = options.get("quiescence_probe") or []
    if probe:
        programs.append(probe[0])
        # An argument naming an existing file is a script the probe runs.
        programs += [arg for arg in probe[1:] if os.path.isabs(arg)
                     and os.path.exists(arg)]
    for name in programs:
        program = _program(name, env)
        if not program:
            return "program not found: %s" % name
        problem = path_problem(program)
        if problem:
            return "program: " + problem
    return None


# -- #5665: fingerprints of the probe and its notifier ------------------------

def _sha256(path: str) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: str) -> str:
    """One digest of a package: every file's relative path and SHA256, sorted
    (bytecode caches excluded: they are rebuilt from the sources)."""
    import hashlib
    lines = []

    def fail(exc):
        raise exc
    for top, dirs, files in os.walk(root, onerror=fail):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            path = os.path.join(top, name)
            lines.append("%s\0%s\n" % (os.path.relpath(path, root), _sha256(path)))
    return hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()


def _sourced_literal(path: str, name: str) -> tuple:
    """(value or None, problem) of `name` in a file the probe sources.

    The literal shell forms that set it (`NAME=…`, `export`, `readonly`,
    `declare -x`, `typeset -x`) and `unset NAME` (or `unset -v NAME`) are
    applied in file order; the value is "" when the last operation clears
    the name, None when the file never names it. Any other active line
    naming it is a refusal: an unrecognized shell form could set or clear
    the variable without us seeing it, and only a plain literal is accepted
    (an expansion could name anything). A comment line is inert."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, "%s unreadable (%s)" % (path, getattr(exc, "strerror", None) or exc)
    escaped = re.escape(name)
    assignment = re.compile(
        r"\s*(?:(?:export|readonly)\s+|(?:declare|typeset)(?:\s+-[A-Za-z]+)*\s+)?"
        r"%s=(.*)" % escaped)
    unset = re.compile(r"\s*unset(?:\s+-v)?\s+%s\s*" % escaped)
    mention = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % escaped)
    value = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = assignment.fullmatch(line)
        if match:
            raw = match.group(1).strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                raw = raw[1:-1]
            if not re.fullmatch(r"[A-Za-z0-9_./+:@%-]*", raw):
                return None, "%s in %s is not a plain path" % (name, path)
            value = raw
        elif unset.fullmatch(line):
            # `unset` in the sourced file removes even the unit's value: the
            # probe then falls back to its python and package.
            value = ""
        elif mention.search(line):
            return None, "%s in %s is not a recognized assignment" % (name, path)
    return value, None


def _notifier(options: dict, env: dict) -> tuple:
    """([files], [packages], problem): what the probe's alert runs.

    QUIESCENCE_NOTIFIER (the unit's environment, overridden by BASCULE_ENV
    which the probe sources), otherwise the probe's python and the `ameesh`
    package of its venv; a stable venv of launchers stands for its live
    version (#5743), as in quiescence-staging.sh."""
    value = env.get("QUIESCENCE_NOTIFIER") or None
    sourced, problem = _sourced_literal(options["bascule_env"], "QUIESCENCE_NOTIFIER")
    if problem:
        return [], [], problem
    if sourced is not None:
        value = sourced or None
    if value:
        program = _program(value, env)
        if not program or not os.path.isfile(program):
            return [], [], "notifier not found: %s" % value
        return [program], [], None
    home = env.get("HOME") or os.path.expanduser("~")
    python = env.get("QUIESCENCE_PYTHON") or os.path.join(home, PROBE_PYTHON)
    venv = os.path.dirname(os.path.dirname(os.path.abspath(python)))
    if os.path.isfile(os.path.join(venv, BASCULE_LAUNCHERS)):
        # #5743: the stable venv's python is a launcher and its lib/ holds no
        # package; the probe runs the live version's python, as resolved here.
        current = os.path.join(os.path.dirname(venv), BASCULE_CURRENT)
        if not os.path.islink(current):
            return [], [], "notifier: no live venv (%s)" % current
        venv = os.path.realpath(current)
        python = os.path.join(venv, "bin", "python")
        if not os.path.isfile(python):
            return [], [], "notifier: live venv incomplete (%s)" % python
    import glob
    packages = sorted(glob.glob(os.path.join(
        venv, "lib", "python3*", "site-packages", "ameesh")))
    if len(packages) != 1 or not os.path.isdir(packages[0]):
        return [], [], "notifier package ameesh not found once in %s" % venv
    return [python], packages, None


def fingerprints(options: dict, env: dict) -> tuple:
    """#5665: ({path: sha256}, problem) before a sequence, closed on doubt.

    The probe program, every absolute script it receives, the notifier (or
    the python and the `ameesh` package it imports), each read in full; a
    package's whole tree is guarded, not only its root. An unreadable or
    writable file, a `QUIESCENCE_NOTIFIER` form that is not a recognized
    literal, or a digest other than one declared in `expected_sha256`, is a
    refusal before any host operation."""
    probe = options.get("quiescence_probe") or []
    files, packages = [], []
    if probe:
        program = _program(probe[0], env)
        if not program:
            return {}, "probe not found: %s" % probe[0]
        files.append(program)
        files += [arg for arg in probe[1:] if os.path.isabs(arg)
                  and os.path.isfile(arg)]
        notifier, packages, problem = _notifier(options, env)
        if problem:
            return {}, problem
        files += notifier
    found = {}
    for path in files + packages:
        # #5669: the package's files are imported too; its root and parents
        # alone would let another account write the code that runs.
        problem = tree_problem(path) if path in packages else path_problem(path)
        if problem:
            return {}, problem
        try:
            found[os.path.realpath(path)] = (
                _tree_sha256(path) if path in packages else _sha256(path))
        except OSError as exc:
            return {}, "%s unreadable (%s)" % (
                getattr(exc, "filename", None) or path, exc.strerror or exc)
    for path, digest in sorted(options.get("expected_sha256", {}).items()):
        actual = found.get(os.path.realpath(path))
        if actual is None:
            return found, "%s expected but not run" % path
        if actual != digest:
            return found, "%s is %s, expected %s" % (path, actual, digest)
    return found, None


class ForgeUnreadable(RuntimeError):
    pass


def canon_commit(cfg) -> str:
    """#5727: the commit of this host's default canon, '' when unresolved.

    Read by object name only (`rev-parse`, no hook nor fsmonitor), as the
    canon loader does; never a refusal: the field is evidence, not a gate."""
    from ameesh import canon
    path = getattr(cfg, "canon", "")
    try:
        repo = canon.git_toplevel(path) if path else None
        if not repo:
            return ""
        for rev in ([cfg.canon_ref] if getattr(cfg, "canon_ref", "")
                    else ["origin/main", "origin/HEAD"]):
            commit = canon.git_commit(repo, rev)
            if commit:
                return commit
    except canon.GitError:
        pass
    return ""


def forge_runs(slug: str, sha: str) -> list:
    """The workflow runs of one exact commit, read anonymously at the forge."""
    url = "%s/repos/%s/actions/runs?head_sha=%s&per_page=100" % (FORGE_API, slug, sha)
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ameesh-staging-executor"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ForgeUnreadable("HTTP %d" % exc.code) from exc
    except (OSError, ValueError) as exc:
        raise ForgeUnreadable(type(exc).__name__) from exc
    runs = body.get("workflow_runs") if isinstance(body, dict) else None
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise ForgeUnreadable("unexpected response")
    if int(body.get("total_count") or 0) > len(runs):
        raise ForgeUnreadable("more than %d runs" % len(runs))
    return runs


def run_verdict(runs: list, sha: str, *, packet: bool = False) -> str | None:
    """None when the exact commit is green, else the refusal.

    Green: the latest run of the required aggregate (`tests`) succeeded and
    no gating workflow's latest run concluded negatively. A packet head
    must have been judged by its PR's full suite (`ci-full-<sha>`)."""
    latest = {}
    for run in runs:
        if run.get("head_sha") != sha or run.get("event") not in _GATING_EVENTS:
            continue
        path = str(run.get("path") or "").partition("@")[0]
        if path not in latest or int(run.get("id") or 0) > int(latest[path].get("id") or 0):
            latest[path] = run
    for path in sorted(latest):
        run = latest[path]
        if run.get("conclusion") in _NEGATIVE:
            return "red %s (%s)" % (run.get("name") or path, run.get("html_url") or run.get("id"))
    tests = latest.get(TESTS_WORKFLOW)
    if tests is None:
        return "no tests run for %s" % sha[:12]
    if packet and (tests.get("event") != "pull_request"
                   or "ci-full-" + sha not in str(tests.get("display_title") or "")):
        return "packet head %s without its full PR suite" % sha[:12]
    if tests.get("status") != "completed":
        return "tests of %s still %s" % (sha[:12], tests.get("status"))
    if tests.get("conclusion") != "success":
        return "tests of %s %s" % (sha[:12], tests.get("conclusion"))
    return None


def forge_ci(repo: str, slug: str, sha: str, *, runs_of=None) -> tuple:
    """#5500: (refusal or None, evidence) for the requested commit.

    The canon's §11 bis: the commit's own green CI, or the tree of a green
    packet PR head (`release_cutoff.packet_head`: same tree, no conflict
    resolution). A `[skip ci]` release commit inherits its first parent,
    as in `release_cutoff.commit_is_green`; a landing merge never does.
    Commit structure is read in the guarded clone, CI only at the forge."""
    from ameesh import release_cutoff
    runs_of = runs_of or forge_runs
    current = sha
    refusal = None
    try:
        for _ in range(32):
            own = run_verdict(runs_of(slug, current), current)
            if own is None:
                return None, "CI green on %s" % current
            refusal = refusal or own
            head = release_cutoff.packet_head(repo, current)
            if head and run_verdict(runs_of(slug, head), head, packet=True) is None:
                return None, "tree of %s is green packet head %s" % (current, head)
            if (release_cutoff._landing_release(repo, current)
                    or not release_cutoff._release_commit(repo, current)):
                return CI_REFUSAL + refusal, ""
            parent = release_cutoff._out(repo, "rev-parse", "--verify", current + "^1")
            if not re.fullmatch(r"[0-9a-f]{40}", parent):
                return CI_REFUSAL + "release parent unreadable", ""
            current = parent
    except ForgeUnreadable as exc:
        return CI_REFUSAL + "unreadable (%s)" % exc, ""
    return CI_REFUSAL + "release chain too long", ""


def _record(db, key, data, *, lease):
    # A stale executor must never overwrite its successor's shared request.
    # Recheck and journal in the SAME transaction under the execution lock.
    if lease.remaining() <= 0:
        raise RuntimeError("staging execution lease expired before journal write")
    # The CAS below also renews the server expiry. Measure from before the
    # call, and extend the local monotonic deadline from the same instant:
    # otherwise a slow preparation renews in the database without surviving
    # its own local deadline (#4898 N1).
    started = time.monotonic()
    with db.transaction() as tx:
        st = storage.of(tx)
        epoch = st.work.land_lock(lease.key, lease.owner, lease.ttl, epoch=lease.epoch)
        if epoch != lease.epoch:
            raise RuntimeError("staging execution lease lost before journal write")
        if not lease.note_renewed(epoch, started):
            raise RuntimeError("staging execution lease expired before journal write")
        return st.events.record(EVENT, "staging-request:" + key, None, data)


def _worktree_prefix(run) -> str:
    return WORKTREE_PREFIX + str(run) + "-"


def _retained_trees(repo: str, run) -> list[tuple[str, str]]:
    """The kept worktrees of an attempt run, as (folder, worktree)."""
    if not run:
        return []
    prefix = _worktree_prefix(run)
    base = os.path.dirname(os.path.abspath(repo)) or tempfile.gettempdir()
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []
    found = []
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        found.append((entry.path, os.path.join(entry.path, "worktree")))
    return found


def _drop_retained(repo: str, run) -> None:
    """Remove the worktrees an attempt kept when its execution lease was lost.

    The caller has already proved the old execution stopped (the quiescence
    probe), so no process can still be using them. An absent tree is not an
    error: the attempt may have died before creating it.
    """
    from ameesh import release_cutoff
    for folder, tree in _retained_trees(repo, run):
        release_cutoff._drop_worktree(repo, folder, tree)


def latest(db, key):
    rows = storage.of(db).events.recent_type(
        EVENT, 60, 1, object="staging-request:" + key)
    return rows[0]["data"] if rows else None


def submit(db, cfg, options, sha, *, included=()):
    """Queue a commit without executing any host operation in this process."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("submit requires an exact commit")
    lease = ExecutionLease(db, options["role"], cfg.host, options["key"])
    if lease.acquire() is None:
        raise RuntimeError("staging execution busy; request not replaced")
    try:
        previous = latest(db, options["key"])
        if previous and previous["state"] in ("pending", "running"):
            if previous["sha"] == sha:
                return previous
            if not previous.get("blocked_reason"):
                raise RuntimeError("an unfinished staging request already exists")
        data = dict(request=uuid.uuid4().hex, sha=sha, state="pending",
                    included=[int(lot) for lot in included], notify=options["role"],
                    deadline=time.time() + options.get("start_timeout_s", 300))
        if previous and previous["state"] not in ("completed", "partial"):
            if previous.get("run"):
                data.update(previous_host=previous["host"], previous_run=previous["run"])
            elif previous.get("previous_run"):
                data.update(previous_host=previous["previous_host"],
                            previous_run=previous["previous_run"])
            # #5450: an unproved stop's units are settled by the next attempt,
            # before its quiescence probe; their pid must survive a new submit.
            for field in ("unit_pid", "return_pid", "return_recovery"):
                if previous.get(field):
                    data[field] = previous[field]
            if previous.get("return_recovery") and previous.get("blocked_reason"):
                data["blocked_reason"] = previous["blocked_reason"]
        _record(db, options["key"], data, lease=lease)
        return data
    finally:
        lease.release()


def tick(db, cfg, options) -> dict:
    """Attempt one shared request on the elected, credentialed candidate."""
    host = cfg.host
    if host not in options["candidates"]:
        return {"state": "standby", "reason": "not a candidate"}
    elected = role_run.live(db, options["role"])
    if not elected["held"] or elected.get("holder") != host:
        return {"state": "standby", "reason": "another role holder"}
    lease = ExecutionLease(db, options["role"], host, options["key"])
    if lease.acquire() is None:
        return {"state": "standby", "reason": "execution still leased"}
    execution = role_run._AlertsExecution(lease)
    execution.start()
    # ONE renewal loop covers the whole attempt: the pre-launch phase (probe,
    # fetch, merge-base, regression read, worktree) as well as the child.
    # Without it, a fetch or worktree slower than the 45 s local deadline was
    # reported as a lost lease although the database lease stayed fresh
    # (#4898 N1). The child runs with `watch=False`: a second loop renewing
    # the same epoch would race this one for nothing.
    renewal_stop = threading.Event()
    renewal = None
    attempt_id = 0
    try:
        renewal = threading.Thread(target=role_run._watch_alerts_lease,
                                   args=(lease, execution, renewal_stop),
                                   name="staging-lease", daemon=True)
        renewal.start()
        request = latest(db, options["key"])
        if not request or request["state"] not in ("pending", "running"):
            return {"state": "idle"}
        def blocked(reason):
            if not execution.lost.is_set():
                _record(db, options["key"], dict(request, blocked_reason=reason,
                                                notify=options["role"]), lease=lease)
            return {"state": "blocked", "reason": reason}
        # Read again under the shared lease: a role change at acquisition
        # cannot authorize a new attempt under a stale election result.
        elected = role_run.live(db, options["role"])
        if not elected["held"] or elected.get("holder") != host:
            return {"state": "standby", "reason": "role changed"}
        # #6520 second review: tie the request to this execution before the
        # long preparation below, so a reader can tell a pending handoff this
        # host is working on from a foreign lease, and a blocked request
        # (which keeps state pending) never proves live staging.
        if request.get("execution") != lease.owner:
            request = dict(request, execution=lease.owner)
            _record(db, options["key"], request, lease=lease)
        from .platform.child_environment import execution_transport
        timeout = options.get("timeout_s", staging_watch.STAGING_FALLBACK_S)
        env = execution_transport(dict(os.environ, BASCULE_ENV=options["bascule_env"],
                   STAGING_TIMEOUT_S=str(timeout)))
        # #5500: nothing below may read or run what another account can
        # write. A refusal alerts the pilot (staging_interrupted) and makes
        # no host operation, not even the recovery of a previous attempt.
        problem = activation_problem(options, env)
        if problem:
            reason = "unsafe activation: " + problem
            if request.get("blocked_reason") == reason:
                return {"state": "blocked", "reason": reason}
            return blocked(reason)
        # #5665: the probe and its notifier, read and fingerprinted before
        # any host operation; recorded in the attempt.
        prints, problem = fingerprints(options, env)
        if problem:
            reason = FINGERPRINT_REFUSAL + problem
            if request.get("blocked_reason") == reason:
                return {"state": "blocked", "reason": reason}
            return blocked(reason)
        # Every record of this pass carries them, the probe's refusal too.
        # #5727: so does the commit of the canon this host reads, compared
        # with the agents' canon by the alerts pass.
        request = dict(request, fingerprints=prints, canon_ref=canon_commit(cfg))
        # #5450: a previous stop that could not prove its workstation units
        # stopped left their pid in the request. Settle them before any other
        # host operation; the kept worktree stays until they are proved gone.
        guard_pid = request.get("unit_pid") or request.get("return_pid")
        if isinstance(guard_pid, int) and guard_pid > 0:
            from ameesh import release_cutoff as _release_cutoff
            note, settled = _release_cutoff._settle_poste_units(
                guard_pid, lambda seconds: time.sleep(seconds))
            if not settled:
                return blocked("previous stop not proved: "
                               + (note or "workstation units unreadable"))
            request = dict(request)
            request.pop("unit_pid", None)
            request.pop("return_pid", None)
        env.pop("AMEESH_TURN_ID", None)
        # Only this attempt names its run and a proved-stopped predecessor;
        # never a value inherited from the unit's environment (review qa-01).
        env.pop("STAGING_RUN_ID", None)
        env.pop("STAGING_PREVIOUS_RUN", None)
        env.pop("STAGING_PREVIOUS_HOST", None)
        repo = str(Path(options["repo"]).resolve())
        if request["state"] == "running" or request.get("previous_run"):
            probe = options.get("quiescence_probe")
            if not probe:
                return blocked("quiescence probe missing")
            env.update(STAGING_PREVIOUS_HOST=request.get("previous_host") or request["host"],
                       STAGING_PREVIOUS_RUN=request.get("previous_run") or request["run"])
            # The probe itself is bounded by the same execution watchdog.
            # It must inspect remote operations too, never just a local PID.
            code, lost = role_run._run_under_lease(probe, env, lease, execution,
                                                   watch=False)
            if code or lost:
                return blocked("old execution not proved stopped")
            # #5450: a supervisor killed before its bounded return leaves the
            # return state in the kept tree. The next tick must not drop it
            # and restart silently: it mails the manual fallback and keeps the
            # guard, as the pilot's arbitration requires.
            from ameesh import release_cutoff as _release_cutoff
            for run in (request.get("run"), request.get("previous_run")):
                for _folder, tree in _retained_trees(repo, run):
                    if not os.path.isfile(os.path.join(
                            tree, _release_cutoff.STAGING_RETURN_STATE)):
                        continue
                    # One mail per interrupted return, not one per timer pass.
                    if request.get("return_recovery"):
                        return {"state": "blocked",
                                "reason": request.get("blocked_reason")
                                or "retour apres arret interrompu"}
                    _release_cutoff._manual_return_mail(
                        db, tree, request.get("notify") or "mesh-design",
                        "superviseur interrompu avant la fin du retour")
                    reason = ("retour apres arret interrompu: marche manuelle "
                              "remise au pilote; sources kept")
                    _record(db, options["key"],
                            dict(request, blocked_reason=reason,
                                 return_recovery=True), lease=lease)
                    return {"state": "blocked", "reason": reason}
            # The stop is proved: the trees kept by the interrupted attempt
            # can be removed now (#4898 N3). Only after this proof, never
            # before, since the old process may still be writing in them.
            for run in (request.get("run"), request.get("previous_run")):
                _drop_retained(repo, run)
        if not Path(options["bascule_env"]).is_file():
            return blocked("candidate deployment access missing")
        # #4885: a sequence launched by another path (by hand, synchronous
        # release, another host) holds no execution lease. Its shared
        # progress does: wait for it, without host operation. staging.sh
        # repeats the check atomically when it starts (exit BUSY).
        # A late run still blocks: its deadline proves no stop.
        previous = request.get("previous_run") or request.get("run")
        rows = storage.of(db).events.latest_type(
            staging_watch.EVENT, 60, staging_watch.LIMIT)
        from ameesh import release_cutoff
        attested = storage.of(db).events.latest_type(
            release_cutoff.STAGING_EVENT, 60, staging_watch.LIMIT)
        others = staging_watch.in_flight(
            rows, exempt={previous} if previous else (), attested=attested)
        reason = ("staging history truncated; exclusivity unobservable"
                  if len(rows) >= staging_watch.LIMIT else
                  "another staging sequence running: " + ", ".join(others)
                  if others else None)
        if reason:
            if request.get("blocked_reason") == reason:
                return {"state": "blocked", "reason": reason}
            return blocked(reason)
        sha = request["sha"]
        subprocess.run(["git", "-C", repo, "fetch", "--no-tags", "origin"],
                       check=True, timeout=60, stdout=subprocess.DEVNULL)
        # Resolve the requested commit in the candidate's dedicated clone.
        # A missing commit or one outside the declared target is a refusal.
        subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", sha,
                        options.get("target", "origin/main")], check=True,
                       timeout=30, stdout=subprocess.DEVNULL)
        # #5500: the requested commit's CI, read at the forge before any
        # root operation. A refusal is read again at most every
        # CI_RECHECK_S (anonymous quota); the request stays queued.
        checked = request.get("ci_checked")
        if (str(request.get("blocked_reason") or "").startswith(CI_REFUSAL)
                and isinstance(checked, (int, float))
                and 0 <= time.time() - checked < CI_RECHECK_S):
            return {"state": "blocked", "reason": request["blocked_reason"]}
        refusal, ci_evidence = forge_ci(repo, options["github"], sha)
        if refusal:
            if not execution.lost.is_set():
                _record(db, options["key"], dict(
                    request, blocked_reason=refusal, ci_checked=time.time(),
                    notify=options["role"]), lease=lease)
            return {"state": "blocked", "reason": refusal}
        from ameesh import release_cutoff, staging_pending
        project = release_cutoff.ProjectRelease(name=options["project"], repo=repo)
        if staging_pending.regressions(project, sha, db=db):
            return blocked("candidate would roll back active content")
        queued = request
        attempt = dict(request, state="running", host=host, run=uuid.uuid4().hex,
                       started=time.time(), notify=options["role"],
                       deadline=time.time() + timeout, ci=ci_evidence,
                       # #6520: the live lease this attempt runs under, so a
                       # reader can tell its staging from an orphan lease
                       # without borrowing a terminal request's commit.
                       execution=lease.owner)
        attempt.pop("blocked_reason", None)
        attempt.pop("ci_checked", None)
        attempt.pop("previous_host", None)
        attempt.pop("previous_run", None)
        attempt.pop("unit_pid", None)
        attempt.pop("return_pid", None)
        attempt.pop("return_recovery", None)
        attempt_id = _record(db, options["key"], attempt, lease=lease)
        request = attempt
        env["STAGING_RUN_ID"] = attempt["run"]
        # An immutable worktree of the requested commit is prepared by the
        # existing release primitive, and removed only after the child ends.
        # Its name carries the run, so a tree kept after a lost lease is
        # found by the recovery that proves the old stop.
        folder, tree = release_cutoff._worktree(
            repo, sha, prefix=_worktree_prefix(attempt["run"]))
        drop = True
        # #5450: the same lateness verdict as the independent alerts pass; the
        # attempt's own started/deadline pair is the no-heartbeat fallback.
        monitor = staging_watch.Monitor(db, attempt["run"], fallback=attempt)
        try:
            # #5500: the tree comes from the guarded clone; check it again
            # before running its script. No host operation happened yet:
            # the request is queued again, as for a refused start.
            problem = tree_problem(tree)
            if problem:
                request = queued
                return blocked("unsafe activation: worktree: " + problem)
            argv = ["bash", str(Path(tree) / options.get(
                "script", "deploy/mise-a-jour/staging.sh")),
                    *options.get("arguments", [])]
            if attempt.get("included"):
                argv += ["--lots", " ".join(str(lot) for lot in attempt["included"])]
            argv.append(sha)
            code, lost = role_run._run_under_lease(
                argv, env, lease, execution, watch=False,
                monitor=monitor.check, interval=staging_watch.WATCH_INTERVAL_S,
                stop_grace=staging_watch.STOP_GRACE_S)
            reason = getattr(execution, "stop_reason", None)
            if isinstance(reason, str) and reason:
                # A monitored stop is recorded as failed. The launcher does
                # not attempt the current host's rollback inside the grace
                # (its rolling restart cannot fit); the TERM trap traces the
                # open step and mails the manual rollback. Never claim a
                # rollback without a `retour ok` trace.
                # #5450: `halt` stops the local group, never the workstation's
                # transient units (`poste-maj-staging-<pid>-*`), which run
                # from this worktree. A terminal state releases the shared
                # start and the finally block deletes the tree: both must wait
                # for a proved quiescence, as in the synchronous path. If the
                # stop is not proved, keep the sources and the request running
                # with the guard that keeps every later start behind the
                # quiescence probe.
                drop = False
                pid = getattr(execution, "pgid", None)
                note = ""
                if isinstance(pid, int) and pid > 0:
                    note, drop = release_cutoff._settle_poste_units(
                        pid, lambda seconds: time.sleep(seconds))
                if not drop:
                    guard = ("staging stop not proved: "
                             + (note.strip("; ") or "workstation units unreadable")
                             + "; sources kept, next attempt requires quiescence")
                    _record(db, options["key"],
                            dict(attempt, blocked_reason=guard,
                                 unit_pid=pid), lease=lease)
                    return {"state": "blocked", "reason": guard,
                            "run": attempt["run"]}
                # #5450: with the units quiescent, the return of the current
                # host is a new bounded step run here, outside the killed
                # group. The guard and the worktree stay until it ends; a
                # failed or timed-out return is the manual fallback's job.
                drop = False
                retour = release_cutoff._return_after_stop(
                    tree, sha, env=env,
                    budget=release_cutoff._return_budget(
                        float(attempt.get("started") or time.time()),
                        timeout_s=float(options.get(
                            "return_timeout_s",
                            release_cutoff.STAGING_RETURN_TIMEOUT_S)),
                        service_timeout_s=float(options.get(
                            "service_timeout_s",
                            release_cutoff.STAGING_SERVICE_TIMEOUT_S))))
                drop = retour["drop"]
                if not drop:
                    guard = ("retour apres arret non conclu: "
                             + (retour["note"] or "workstation units unreadable")
                             + "; sources kept, next attempt requires quiescence")
                    _record(db, options["key"],
                            dict(attempt, blocked_reason=guard,
                                 return_pid=retour.get("pid")), lease=lease)
                    return {"state": "blocked", "reason": guard,
                            "run": attempt["run"]}
                if not retour["ok"] and not retour["mailed"]:
                    release_cutoff._manual_return_mail(
                        db, tree, attempt.get("notify") or "mesh-design",
                        retour["note"])
                monitor.stop(reason)
                result = dict(attempt, state="failed", exit=code, reason=reason)
                if note:
                    result["unit_note"] = note
                if retour["note"]:
                    result["return_note"] = retour["note"]
                _record(db, options["key"], result, lease=lease)
                return result
            if lost:
                # A workstation's transient unit may outlive the bash group.
                # Keep its sources until quiescence is proved on recovery.
                drop = False
                request = attempt
                return blocked("execution lease lost")
            proofs = storage.of(db).events.recent_type(
                staging_watch.EVENT, 60, 1, object="staging-run:" + attempt["run"])
            terminal = proofs[0]["data"]["state"] if proofs else None
            if code == staging_watch.BUSY and terminal is None:
                # Refused before any host operation (shared start or local
                # flock): the request is queued again, not failed.
                request = queued
                return blocked("another staging sequence running")
            if code == 0 and terminal not in ("completed", "partial"):
                # The last snapshot publication can fail after a successful
                # sequence. The shared attestation written by actif.py is
                # then the terminal proof; without it the alerts pass would
                # raise a false staging_interrupted (#4898 N2).
                attested = storage.of(db).events.recent_type(
                    release_cutoff.STAGING_EVENT, 60, 20,
                    object="staging:" + sha)
                if any(int(row.get("id") or 0) > int(attempt_id or 0)
                       and (row.get("data") or {}).get("sha") == sha
                       for row in attested):
                    terminal = "completed"
            result = dict(attempt, state=terminal if code == 0 and terminal in ("completed", "partial") else "failed",
                          exit=code)
            _record(db, options["key"], result, lease=lease)
            return result
        finally:
            if drop:
                release_cutoff._drop_worktree(repo, folder, tree)
    except Exception as exc:
        # Preserve an interrupted attempt for recovery, and make pre-launch
        # errors visible now rather than waiting for its deadline.
        if 'request' in locals() and request and not execution.lost.is_set():
            _record(db, options["key"], dict(request,
                    blocked_reason="executor error: " + type(exc).__name__,
                    notify=options["role"]), lease=lease)
        raise
    finally:
        renewal_stop.set()
        if renewal is not None:
            renewal.join()
        execution.close()
        lease.release()


def main(argv=None):
    from .platform.child_environment import install
    install()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tick")
    submit_p = commands.add_parser("submit")
    submit_p.add_argument("sha")
    args = parser.parse_args(argv)
    # #5500: whatever this process creates (the attempt's worktree) must
    # pass the guard: never group or other writable.
    os.umask(0o022)
    if args.command == "tick":
        from .platform.child_environment import harden
        if not harden():
            return 1
        # #5500: refuse before reading a configuration another account
        # could have written; the pilot sees the request time out.
        problem = startup_problem(args.config)
        if problem:
            print("ameesh: staging executor refused: unsafe activation: " + problem,
                  file=sys.stderr)
            return 1
    from ameesh import config, db as db_mod
    cfg = config.load()
    options = settings(args.config)
    conn = db_mod.connect(cfg)
    try:
        result = (submit(conn, cfg, options, args.sha) if args.command == "submit"
                  else tick(conn, cfg, options))
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("state") in ("failed", "blocked") else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
