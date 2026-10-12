# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh-offload-shell: the VM side of heavy-suite offloading (lot L159b,
mode b1 of the L159 study, `docs/design/etudes/l159-capacite-des-hotes.md`).

It is the forced command of the `ameesh-guest` SSH key: the key has no shell,
no port forwarding, only these four verbs (read from SSH_ORIGINAL_COMMAND):

  put <repo> --have        stdout: the mirror's recent snapshots (bases
                           for the client's thin pack)
  put <repo> <commit>      stdin: a thin git pack; stores it in the mirror and
                           keeps <commit> under refs/offload/
  run <repo> <commit> [--profile P] [--cwd DIR] [--timeout S] [--env K=V]...
      -- <argv...>         runs argv in a throwaway checkout of <commit>;
                           stdout and stderr pass through, the exit code too
  status [<job>]           one JSON object per job (running or waiting)
  cancel <job>             stops a job

Protocol lines go to stderr, prefixed with `ameesh-offload: ` and, when the
client gives one (`<verb> --nonce N …`, 32 hex digits, per session), with
that nonce: the command's own stderr shares the stream, and the client
takes only lines carrying its nonce for protocol. A `run` always ends with
`ameesh-offload: N result {json}` (outcome `done`, `refused`, `stopped`,
`timeout`, `cancelled` or `error`); the client strips them.

Each job runs:
* under a concurrency token (`flock` on `locks/slot-N`; the client sees the
  wait);
* in a transient slice of the guest's user manager, `ameesh-job-<id>.slice`,
  capped by `MemoryMax` (config `agent_memory_max`) when `scope` is true:
  setup, hooks (scopes `ameesh-job-<id>-<phase>`), the command (scope
  `ameesh-job-<id>`) and the job's containers (`--cgroup-parent
  $OFFLOAD_SLICE`) share that cap, and the slice is killed at the end;
* supervised in every phase: cancel, SSH session loss, gate and one deadline
  for the whole job (from its slot) end setup, hooks and command alike;
* with a private TMPDIR inside the job directory, removed at the end;
* with the dependencies of its profile cached by the hash of the profile's
  lock files (`profiles/<name>.json`, see `deploy/guest/`).

It obeys the host gate (`ameesh-host-state/1`, study section 1.5) when
`gate.mode` is `file`: no new job unless `available`; a running job is
killed at `drain_deadline_ts` when `draining`, immediately when `stopped`.
An unreadable, absent or expired state counts as `stopped`.

Standard library only: `deploy/guest/install.sh` copies this file to the VM,
where ameesh itself is not installed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

PREFIX = "ameesh-offload: "
DEFAULT_CONFIG = "/srv/ameesh-guest/offload.json"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: env variables a job never receives from the client (the VM decides them)
RESERVED_ENV = ("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP",
                "LD_PRELOAD", "LD_LIBRARY_PATH", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "OFFLOAD_JOB", "OFFLOAD_WORK",
                "OFFLOAD_CACHE", "OFFLOAD_SLICE")
POLL_S = 1.0
#: snapshots offered to the client as thin-pack bases
HAVE_DEPTH = 32
#: the `after` hook (teardown) runs even after a stop, within this delay
AFTER_HOOK_S = 120


class Refused(Exception):
    """The request is invalid, or the host does not take jobs now."""


class Stopped(Exception):
    """The job was cancelled, timed out or stopped by the gate (`Job.stop`)."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    root = cfg.get("root") or os.path.dirname(os.path.abspath(path))
    cfg["root"] = root
    cfg.setdefault("slots", 3)
    cfg.setdefault("agent_memory_max", "")
    cfg.setdefault("max_job_seconds", 3600)
    cfg.setdefault("scope", True)
    cfg.setdefault("keep_days", 7)
    cfg.setdefault("gate", {"mode": "none"})
    # root-owned on a real VM (install.sh): the guest cannot write a hook
    cfg.setdefault("profiles", os.path.join(root, "profiles"))
    return cfg


def _dir(cfg: dict, *parts: str) -> str:
    path = os.path.join(cfg["root"], *parts)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


#: the client's per-session nonce (`<verb> --nonce N …`): every protocol line
#: carries it, so a command printing a line shaped like the protocol (a fake
#: `result`, `refused`…) on the same stderr is never taken for one; the job
#: never receives it (its environment comes from `base_env`)
NONCE = ""
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def emit(kind: str, payload: object = None) -> None:
    line = PREFIX + (NONCE + " " if NONCE else "") + kind
    if payload is not None:
        line += " " + (payload if isinstance(payload, str)
                       else json.dumps(payload, sort_keys=True))
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except OSError:
        # the SSH session is gone (EPIPE): the job loop sees it by its parent
        pass


# --------------------------------------------------------------------------
# host gate (ameesh-host-state/1)
# --------------------------------------------------------------------------

def gate_state(cfg: dict, now: float | None = None) -> tuple[str, float | None, str]:
    """(state, drain deadline or None, reason). Without a gate, `available`."""
    gate = cfg.get("gate") or {}
    if gate.get("mode", "none") != "file":
        return "available", None, ""
    now = time.time() if now is None else now
    path = gate.get("path", "/run/ameesh-gate/state/state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return "stopped", None, "gate unreadable"
    if not isinstance(doc, dict) or doc.get("schema") != "ameesh-host-state/1":
        return "stopped", None, "gate unreadable"
    expires = doc.get("expires_ts")
    if isinstance(expires, (int, float)) and expires < now:
        return "stopped", None, "gate expired"
    state = doc.get("state")
    reason = str(doc.get("reason") or "")
    if state == "available":
        until = doc.get("until_ts")
        if isinstance(until, (int, float)) and until < now:
            return "stopped", None, reason or "window ended"
        return "available", None, reason
    if state == "draining":
        deadline = doc.get("drain_deadline_ts")
        return "draining", deadline if isinstance(deadline, (int, float)) else now, reason
    return "stopped", None, reason or str(state)


# --------------------------------------------------------------------------
# put: the repository mirror
# --------------------------------------------------------------------------

def mirror_path(cfg: dict, repo: str) -> str:
    if not NAME_RE.match(repo):
        raise Refused("invalid repository name %r" % repo)
    path = os.path.join(_dir(cfg, "mirrors"), repo + ".git")
    if not os.path.isdir(path):
        subprocess.run(["git", "init", "-q", "--bare", path], check=True)
        subprocess.run(["git", "-C", path, "config", "gc.auto", "0"], check=True)
    return path


def cmd_put(cfg: dict, args: list[str]) -> int:
    if len(args) != 2:
        raise Refused("usage: put <repo> --have | put <repo> <commit>")
    mirror = mirror_path(cfg, args[0])
    if args[1] == "--have":
        # its recent snapshots, last used first: the client's thin-pack bases
        stamps = os.path.join(mirror, "offload-stamps")
        names = [n for n in os.listdir(stamps) if SHA_RE.match(n)] \
            if os.path.isdir(stamps) else []
        names.sort(key=lambda n: os.path.getmtime(os.path.join(stamps, n)), reverse=True)
        for name in names[:HAVE_DEPTH]:
            if subprocess.run(["git", "-C", mirror, "cat-file", "-e", name + "^{commit}"],
                              stderr=subprocess.DEVNULL).returncode == 0:
                sys.stdout.write(name + "\n")
        return 0
    commit = args[1]
    if not SHA_RE.match(commit):
        raise Refused("invalid commit id %r" % commit)
    subprocess.run(["git", "-C", mirror, "index-pack", "--stdin", "--fix-thin"],
                   stdin=sys.stdin.buffer, stdout=subprocess.DEVNULL, check=True)
    if subprocess.run(["git", "-C", mirror, "cat-file", "-e", commit + "^{commit}"]
                      ).returncode != 0:
        raise Refused("commit %s missing after the pack" % commit)
    subprocess.run(["git", "-C", mirror, "update-ref", "refs/offload/" + commit, commit],
                   check=True)
    touch_snapshot(mirror, commit)
    prune_mirror(cfg, mirror)
    return 0


def touch_snapshot(mirror: str, commit: str) -> None:
    """Marks a snapshot as used (put or run): pruning goes by last use, since
    a snapshot carries the date of its HEAD, not of its upload."""
    stamps = os.path.join(mirror, "offload-stamps")
    os.makedirs(stamps, exist_ok=True)
    with open(os.path.join(stamps, commit), "a"):
        pass
    os.utime(os.path.join(stamps, commit))


def prune_mirror(cfg: dict, mirror: str) -> None:
    """Drops snapshots unused for `keep_days`, then lets git repack."""
    limit = time.time() - float(cfg["keep_days"]) * 86400
    stamps = os.path.join(mirror, "offload-stamps")
    out = subprocess.run(
        ["git", "-C", mirror, "for-each-ref", "--format=%(refname)", "refs/offload/"],
        capture_output=True, text=True, check=True)
    old = []
    for ref in out.stdout.split():
        stamp = os.path.join(stamps, ref.rsplit("/", 1)[-1])
        if not os.path.exists(stamp) or os.path.getmtime(stamp) < limit:
            old.append(ref)
            if os.path.exists(stamp):
                os.unlink(stamp)
    if old:
        subprocess.run(["git", "-C", mirror, "update-ref", "--stdin"],
                       input="".join("delete %s\n" % ref for ref in old),
                       text=True, check=True)
    subprocess.run(["git", "-C", mirror, "-c", "gc.auto=50", "gc", "--auto", "--quiet"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # dependency caches unused for as long (their mtime is touched on use)
    caches = _dir(cfg, "cache")
    for profile in os.listdir(caches):
        pdir = os.path.join(caches, profile)
        for key in os.listdir(pdir) if os.path.isdir(pdir) else ():
            path = os.path.join(pdir, key)
            if os.path.isdir(path) and os.path.getmtime(path) < limit:
                shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

def _write_meta(path: str, meta: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, sort_keys=True)
    os.replace(tmp, path)


def _read_meta(job_dir: str) -> dict | None:
    try:
        with open(os.path.join(job_dir, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _proc_identity(pid: int) -> str | None:
    """`boot_id:start time` of a process (Linux /proc), None if unknown: a PID
    alone can be reused by an unrelated process, after a reboot too."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
            boot = fh.read().strip()
        with open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace") as fh:
            # the name may hold spaces and parentheses: the fields follow the last ')'
            start = fh.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None
    return "%s:%s" % (boot, start)


def _shell_alive(meta: dict) -> bool:
    """The job's shell still runs: its PID exists and, when its identity was
    recorded, is still the same process (not a reused PID)."""
    pid = meta.get("shell_pid")
    if not _alive(pid):
        return False
    recorded = meta.get("shell_start")
    return recorded is None or _proc_identity(pid) == recorded


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_slot(cfg: dict, job_id: str, should_stop) -> int:
    """A concurrency token (an open, locked fd). Polls while all are taken,
    so a cancel or a gate change still ends the wait."""
    locks = _dir(cfg, "locks")
    slots = max(1, int(cfg["slots"]))
    announced = False
    while True:
        for n in range(slots):
            fd = os.open(os.path.join(locks, "slot-%d" % n), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            return fd
        if not announced:
            emit("waiting", {"job": job_id, "slots": slots})
            announced = True
        reason = should_stop()
        if reason:
            raise Refused(reason)
        time.sleep(POLL_S)


def _lock_hash(work: str, profile: dict) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(profile.get("setup", ""), sort_keys=True).encode())
    for rel in profile.get("lockfiles", []):
        digest.update(b"\0" + rel.encode() + b"\0")
        try:
            with open(os.path.join(work, rel), "rb") as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(b"<absent>")
    return digest.hexdigest()[:16]


def load_profile(cfg: dict, name: str) -> dict:
    if not NAME_RE.match(name):
        raise Refused("invalid profile name %r" % name)
    path = os.path.join(cfg["profiles"], name + ".json")
    try:
        with open(path, encoding="utf-8") as fh:
            profile = json.load(fh)
    except FileNotFoundError:
        raise Refused("unknown profile %r" % name) from None
    profile.setdefault("lockfiles", [])
    profile.setdefault("setup", "")
    profile.setdefault("before", "")
    profile.setdefault("after", "")
    profile.setdefault("env", {})
    return profile


def _expand(value: str, env: dict) -> str:
    return re.sub(r"\$\{?(OFFLOAD_JOB|OFFLOAD_WORK|OFFLOAD_CACHE|HOME|PATH)\}?",
                  lambda m: env.get(m.group(1), ""), value)


def unit_names(job_id: str) -> tuple[str, str]:
    """(slice, command scope) of a job. The slice holds every phase (setup,
    hooks, command) and the job's containers; `-` nests slices in systemd, so
    the id's dashes become `_`: ameesh.slice/ameesh-job.slice/ameesh-job-<id>.slice."""
    return ("ameesh-job-%s.slice" % job_id.replace("-", "_"), "ameesh-job-%s" % job_id)


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_slice(cfg: dict, job_id: str) -> None:
    """Caps the job's slice before anything runs in it: the per-job
    `MemoryMax` then covers setup, hooks, command and containers together."""
    slice_name, _ = unit_names(job_id)
    props = ["MemoryMax=%s" % cfg["agent_memory_max"], "MemorySwapMax=0"] \
        if cfg.get("agent_memory_max") else []
    if props:
        res = subprocess.run(["systemctl", "--user", "set-property", "--runtime",
                              slice_name, *props], capture_output=True, text=True)
        if res.returncode != 0:
            raise Refused("cannot cap the job slice: %s" % res.stderr.strip())


def close_slice(job_id: str) -> None:
    """Kills whatever is left in the job's slice, then drops its runtime caps."""
    slice_name, _ = unit_names(job_id)
    _systemctl("kill", "--signal=SIGKILL", slice_name)
    _systemctl("stop", slice_name)
    _systemctl("revert", slice_name)


class Job:
    """Runs the phases of one job under the same supervision: cancel, SSH
    session loss, host gate and one deadline for the whole job."""

    def __init__(self, cfg: dict, job_id: str, job_dir: str):
        self.cfg = cfg
        self.job_id = job_id
        self.cancel_flag = os.path.join(job_dir, "cancel")
        self.ssh_parent = os.getppid()
        # set from the slot on (`set_limit`): waiting in the queue is no timeout
        self.deadline = float("inf")
        self.limit = int(cfg["max_job_seconds"])
        self.stop: dict = {}
        self.proc: subprocess.Popen | None = None
        self.scope = bool(cfg.get("scope"))

    def set_limit(self, seconds: int) -> None:
        self.limit = seconds
        self.deadline = time.time() + seconds

    def session_gone(self) -> bool:
        # no tty, so no SIGHUP for sure: a dead sshd session reparents us
        return os.getppid() != self.ssh_parent

    def check(self, now: float | None = None) -> dict:
        """Fills and returns `stop` when the job must end."""
        now = time.time() if now is None else now
        if os.path.exists(self.cancel_flag):
            self.stop.setdefault("outcome", "cancelled")
            self.stop.setdefault("reason", "cancel requested")
        elif self.session_gone():
            self.stop.setdefault("outcome", "cancelled")
            self.stop.setdefault("reason", "SSH session closed")
        if not self.stop:
            st, drain_at, why = gate_state(self.cfg, now)
            if st == "stopped" or (st == "draining" and drain_at is not None
                                   and now >= drain_at):
                self.stop.update(outcome="stopped",
                                 reason="host %s%s" % (st, ": " + why if why else ""))
            elif now >= self.deadline:
                self.stop.update(outcome="timeout", reason="over %d s" % self.limit)
        return self.stop

    def should_stop(self) -> str:
        """For waits before the job starts (slot): any gate state but
        `available` ends them."""
        if self.check():
            return "cancelled" if self.stop["outcome"] == "cancelled" else self.stop["reason"]
        st, _, why = gate_state(self.cfg)
        return "" if st == "available" else "host %s%s" % (st, ": " + why if why else "")

    def argv(self, phase: str, argv: list[str]) -> list[str]:
        if not self.scope:
            return argv
        slice_name, unit = unit_names(self.job_id)
        if phase != "command":
            unit += "-" + phase
        return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                "--slice", slice_name, "--unit", unit, "--"] + argv

    def kill(self) -> None:
        if self.scope:
            _systemctl("kill", "--signal=SIGKILL", unit_names(self.job_id)[0])
        proc = self.proc
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run(self, phase: str, argv: list[str], cwd: str, env: dict,
            capture: bool = False, supervised: bool = True,
            limit: float | None = None) -> tuple[int, str]:
        """Runs one phase in the job's slice. stdout passes through, goes to
        stderr (hooks: the client's stdout is the command's alone), or is
        captured. Returns (exit code, captured stdout); a supervised phase
        ends early, killed, when `check` says so."""
        out = subprocess.PIPE if capture else (None if phase == "command" else sys.stderr.fileno())
        proc = subprocess.Popen(self.argv(phase, argv), cwd=cwd, env=env, stdout=out,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self.proc = proc
        chunks: list[bytes] = []
        reader = None
        if capture:
            reader = threading.Thread(target=lambda: chunks.append(proc.stdout.read()),
                                      daemon=True)
            reader.start()
        end = None if limit is None else time.time() + limit
        try:
            while True:
                try:
                    rc = proc.wait(timeout=POLL_S)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if (supervised and self.check()) or (end is not None and time.time() >= end):
                    self.kill()
                    rc = proc.wait()
                    break
        finally:
            self.proc = None
        if reader is not None:
            reader.join(timeout=5)
        return rc, b"".join(chunks).decode(errors="replace")


def _hook(job: Job, phase: str, script: str, cwd: str, env: dict,
          supervised: bool = True) -> tuple[int, str]:
    """A profile hook: a shell snippet written by the VM's owner, never by the
    client. Its stdout is captured, its stderr passes through. The `after`
    hook (teardown) runs even after a stop, within AFTER_HOOK_S."""
    return job.run(phase, ["/bin/sh", "-ec", script], cwd, env, capture=True,
                   supervised=supervised, limit=None if supervised else AFTER_HOOK_S)


def prepare_cache(job: Job, profile_name: str, profile: dict, work: str,
                  env: dict) -> str:
    """The dependency cache of the profile, built once per lock-file hash."""
    if not profile["setup"]:
        return ""
    base = _dir(job.cfg, "cache", profile_name)
    key = _lock_hash(work, profile)
    final = os.path.join(base, key)
    fd = os.open(final + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # another job may be building it: wait, still supervised
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if job.check():
                    raise Stopped() from None
                time.sleep(POLL_S)
        ready = os.path.join(final, ".ready")
        if os.path.exists(ready):
            os.utime(final)
            return final
        emit("setup", {"profile": profile_name, "cache": key})
        # built in place (a venv records its own path); `.ready` marks a
        # complete cache, so a setup killed halfway is redone
        shutil.rmtree(final, ignore_errors=True)
        os.makedirs(final, mode=0o700)
        rc, _ = job.run("setup", ["/bin/sh", "-ec", profile["setup"]], work,
                        dict(env, OFFLOAD_CACHE=final))
        if job.stop:
            shutil.rmtree(final, ignore_errors=True)
            raise Stopped()
        if rc != 0:
            shutil.rmtree(final, ignore_errors=True)
            raise Refused("profile %s: setup failed (exit %d)" % (profile_name, rc))
        open(ready, "w").close()
        return final
    finally:
        os.close(fd)


def checkout(mirror: str, work: str, commit: str) -> None:
    """A throwaway work tree of `commit`, borrowing the mirror's objects
    (a clone of a mirror without branches would warn, and copy refs)."""
    subprocess.run(["git", "init", "-q", work], check=True)
    with open(os.path.join(work, ".git", "objects", "info", "alternates"), "w",
              encoding="utf-8") as fh:
        fh.write(os.path.join(mirror, "objects") + "\n")
    subprocess.run(["git", "-C", work, "checkout", "-q", "--detach", commit], check=True)


def check_user_manager(env: dict) -> None:
    """The job's scope needs the guest's systemd user manager (linger). Without
    it, refuse the job (the client falls back) rather than report the
    failure of `systemd-run` as the command's own exit code."""
    if not env.get("XDG_RUNTIME_DIR"):
        # sshd without pam_systemd does not set it
        run_dir = "/run/user/%d" % os.getuid()
        if os.path.isdir(run_dir):
            env["XDG_RUNTIME_DIR"] = run_dir
            os.environ["XDG_RUNTIME_DIR"] = run_dir  # for `systemctl --user kill`
    bus = os.path.join(env.get("XDG_RUNTIME_DIR", ""), "bus")
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and not os.path.exists(bus):
        raise Refused("no systemd user manager for the job scope (loginctl enable-linger?)")


def parse_run(args: list[str]) -> dict:
    if "--" not in args:
        raise Refused("run: missing `--` before the command")
    cut = args.index("--")
    head, argv = args[:cut], args[cut + 1:]
    if len(head) < 2 or not argv:
        raise Refused("usage: run <repo> <commit> [options] -- <command...>")
    req = {"repo": head[0], "commit": head[1], "profile": "default", "cwd": "",
           "timeout": None, "env": {}, "argv": argv}
    i = 2
    while i < len(head):
        opt = head[i]
        val = head[i + 1] if i + 1 < len(head) else None
        if val is None:
            raise Refused("run: %s needs a value" % opt)
        if opt == "--profile":
            req["profile"] = val
        elif opt == "--cwd":
            norm = os.path.normpath(val)
            if os.path.isabs(norm) or norm.split(os.sep)[0] == "..":
                raise Refused("run: --cwd must stay inside the repository")
            req["cwd"] = "" if norm == "." else norm
        elif opt == "--timeout":
            req["timeout"] = int(val)
        elif opt == "--env":
            if not ENV_RE.match(val):
                raise Refused("run: invalid --env %r" % val)
            key, value = val.split("=", 1)
            if key in RESERVED_ENV:
                raise Refused("run: %s is set by the host" % key)
            req["env"][key] = value
        else:
            raise Refused("run: unknown option %s" % opt)
        i += 2
    if not SHA_RE.match(req["commit"]):
        raise Refused("invalid commit id %r" % req["commit"])
    return req


def cmd_run(cfg: dict, args: list[str]) -> int:
    started = time.time()
    job_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime(started)) + "-" + secrets.token_hex(3)
    result = {"job": job_id, "outcome": "error", "exit": None}
    try:
        req = parse_run(args)
    except (Refused, ValueError) as exc:
        result.update(outcome="refused", reason=str(exc))
        emit("result", result)
        return 2
    state, _, reason = gate_state(cfg)
    if state != "available":
        result.update(outcome="refused", reason="host %s%s" % (state, ": " + reason if reason else ""))
        emit("result", result)
        return 75
    mirror = mirror_path(cfg, req["repo"])
    if subprocess.run(["git", "-C", mirror, "cat-file", "-e", req["commit"] + "^{commit}"],
                      stderr=subprocess.DEVNULL).returncode != 0:
        result.update(outcome="refused", reason="unknown commit %s (put it first)" % req["commit"])
        emit("result", result)
        return 2
    touch_snapshot(mirror, req["commit"])
    env = base_env(cfg)
    if cfg.get("scope"):
        try:
            check_user_manager(env)
        except Refused as exc:
            result.update(outcome="refused", reason=str(exc))
            emit("result", result)
            return 75
    prune_orphans(cfg)
    job_dir = os.path.join(_dir(cfg, "jobs"), job_id)
    os.makedirs(job_dir, mode=0o700)
    meta_path = os.path.join(job_dir, "meta.json")
    meta = {"job": job_id, "state": "waiting", "repo": req["repo"], "commit": req["commit"],
            "profile": req["profile"], "argv": req["argv"], "created_ts": int(started),
            "shell_pid": os.getpid(), "shell_start": _proc_identity(os.getpid())}
    _write_meta(meta_path, meta)
    emit("job", job_id)
    job = Job(cfg, job_id, job_dir)

    def on_signal(signum, _frame):
        # the SSH session went away (SIGHUP), or `cancel` (SIGTERM)
        job.stop.setdefault("outcome", "cancelled")
        job.stop.setdefault("reason", "signal %d" % signum)
        job.kill()

    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)

    slot_fd = None
    profile: dict | None = None
    sliced = False
    work = os.path.join(job_dir, "work")
    try:
        try:
            slot_fd = acquire_slot(cfg, job_id, job.should_stop)
        except Refused as exc:
            result.update(outcome="cancelled" if str(exc) == "cancelled" else "refused",
                          reason=str(exc))
            return 75 if result["outcome"] == "refused" else 130
        # one deadline for the whole job, from its slot: setup, hooks, command
        job.set_limit(min(int(cfg["max_job_seconds"]), req["timeout"] or 10 ** 9))
        tmp = os.path.join(job_dir, "tmp")
        os.makedirs(tmp, mode=0o700)
        checkout(mirror, work, req["commit"])
        env.update(TMPDIR=tmp, TMP=tmp, TEMP=tmp, OFFLOAD_JOB=job_id, OFFLOAD_WORK=work)
        if job.scope:
            env["OFFLOAD_SLICE"] = unit_names(job_id)[0]
            open_slice(cfg, job_id)
            sliced = True
        profile = load_profile(cfg, req["profile"])
        env["OFFLOAD_CACHE"] = prepare_cache(job, req["profile"], profile, work, env)
        for key, value in profile["env"].items():
            env[key] = _expand(str(value), env)
        env.update(req["env"])
        cwd = os.path.join(work, req["cwd"]) if req["cwd"] else work
        if not os.path.isdir(cwd):
            raise Refused("run: --cwd %s does not exist in the snapshot" % req["cwd"])
        if profile["before"]:
            rc, out = _hook(job, "before", profile["before"], work, env)
            if job.stop:
                raise Stopped()
            if rc != 0:
                raise Refused("profile %s: before hook failed (exit %d)" % (req["profile"], rc))
            for line in out.splitlines():
                if ENV_RE.match(line):
                    key, value = line.split("=", 1)
                    env[key] = value
        meta.update(state="running", started_ts=int(time.time()), limit_s=job.limit)
        _write_meta(meta_path, meta)
        emit("start", {"job": job_id, "profile": req["profile"], "limit_s": job.limit})
        rc, _ = job.run("command", req["argv"], cwd, env)
        result["exit"] = rc
        if job.stop:
            raise Stopped()
        result["outcome"] = "done"
        return rc if 0 <= rc < 256 else 128 + (-rc)
    except Stopped:
        result.update(job.stop)
        return {"stopped": 75, "timeout": 124}.get(job.stop["outcome"], 130)
    except Refused as exc:
        result.update(outcome="refused", reason=str(exc))
        return 75
    except (OSError, subprocess.CalledProcessError) as exc:
        result.update(outcome="error", reason=str(exc))
        return 70
    finally:
        try:
            if profile is not None and profile["after"]:
                # teardown (the job's containers), even after a stop
                _hook(job, "after", profile["after"], work, env, supervised=False)
        finally:
            if sliced:
                close_slice(job_id)
            result["seconds"] = round(time.time() - started, 1)
            # journal and cleanup first: once the result is out, the client
            # may move on (and the session may be gone already)
            _journal(cfg, dict(meta, **result))
            if slot_fd is not None:
                os.close(slot_fd)
            shutil.rmtree(job_dir, ignore_errors=True)
            emit("result", result)


def base_env(cfg: dict) -> dict:
    """What every job and hook gets from the host (never from the client)."""
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", cfg["root"]), "USER": os.environ.get("USER", ""),
           "LOGNAME": os.environ.get("LOGNAME", ""),
           "LANG": os.environ.get("LANG", "C.UTF-8")}
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def prune_orphans(cfg: dict) -> None:
    """Jobs whose shell died without cleaning up (killed, host rebooted): the
    slot lock went with the process, but the suite and its containers may
    still run. Kill the job's slice, run the profile's teardown, then drop
    the folder."""
    jobs = _dir(cfg, "jobs")
    for name in os.listdir(jobs):
        meta = _read_meta(os.path.join(jobs, name))
        if meta is None or _shell_alive(meta):
            continue
        job_id = str(meta.get("job") or name)
        if cfg.get("scope"):
            _systemctl("kill", "--signal=SIGKILL", unit_names(job_id)[0])
        try:
            profile = load_profile(cfg, str(meta.get("profile") or "default"))
        except Refused:
            profile = None
        if profile is not None and profile["after"]:
            env = dict(base_env(cfg), OFFLOAD_JOB=job_id,
                       OFFLOAD_WORK=os.path.join(jobs, name, "work"))
            if cfg.get("scope"):
                env["OFFLOAD_SLICE"] = unit_names(job_id)[0]
            try:
                subprocess.run(["/bin/sh", "-ec", profile["after"]], env=env,
                               cwd=cfg["root"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=AFTER_HOOK_S)
            except subprocess.TimeoutExpired:
                pass
        if cfg.get("scope"):
            close_slice(job_id)
        meta["state"] = "orphan"
        _journal(cfg, dict(meta, outcome="error", reason="orphan job killed and removed"))
        shutil.rmtree(os.path.join(jobs, name), ignore_errors=True)


def _journal(cfg: dict, entry: dict) -> None:
    """One line per job in `jobs.jsonl`: the usage measure of the study (4.4)."""
    entry = {k: v for k, v in entry.items() if k not in ("shell_pid", "shell_start", "pid")}
    entry["ended_ts"] = int(time.time())
    try:
        with open(os.path.join(cfg["root"], "jobs.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def cmd_status(cfg: dict, args: list[str]) -> int:
    jobs = _dir(cfg, "jobs")
    names = sorted(os.listdir(jobs))
    if args:
        if not NAME_RE.match(args[0]):
            raise Refused("invalid job id %r" % args[0])
        names = [n for n in names if n == args[0]]
    gate, _, reason = gate_state(cfg)
    sys.stdout.write(json.dumps({"host": {"gate": gate, "reason": reason,
                                          "slots": int(cfg["slots"])}}) + "\n")
    for name in names:
        meta = _read_meta(os.path.join(jobs, name))
        if meta is None:
            continue
        if not _shell_alive(meta):
            meta["state"] = "orphan"
        meta.pop("shell_pid", None)
        meta.pop("shell_start", None)
        sys.stdout.write(json.dumps(meta, sort_keys=True) + "\n")
    return 0


def cmd_cancel(cfg: dict, args: list[str]) -> int:
    if len(args) != 1 or not NAME_RE.match(args[0]):
        raise Refused("usage: cancel <job>")
    job_dir = os.path.join(_dir(cfg, "jobs"), args[0])
    meta = _read_meta(job_dir)
    if meta is None:
        raise Refused("no job %s" % args[0])
    open(os.path.join(job_dir, "cancel"), "w").close()
    if _shell_alive(meta):  # never a process that reused the PID
        os.kill(meta["shell_pid"], signal.SIGTERM)
    sys.stdout.write("cancelled %s\n" % args[0])
    return 0


VERBS = {"put": cmd_put, "run": cmd_run, "status": cmd_status, "cancel": cmd_cancel}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config = DEFAULT_CONFIG
    if argv[:1] == ["--config"] and len(argv) >= 2:
        config = argv[1]
    request = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        words = shlex.split(request)
    except ValueError:
        words = []
    if not words or words[0] not in VERBS:
        sys.stderr.write(PREFIX + "allowed verbs: put, run, status, cancel\n")
        return 2
    if words[1:2] == ["--nonce"]:
        if len(words) < 3 or not NONCE_RE.match(words[2]):
            sys.stderr.write(PREFIX + "invalid --nonce\n")
            return 2
        global NONCE
        NONCE = words[2]
        words = words[:1] + words[3:]
    try:
        cfg = load_config(config)
        return VERBS[words[0]](cfg, words[1:])
    except Refused as exc:
        emit("refused", str(exc))
        return 2
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        emit("error", str(exc))
        return 70


if __name__ == "__main__":
    sys.exit(main())
