# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh-offload-shell: the VM side of heavy-suite offloading (lot L159b,
mode b1 of the L159 study, `docs/design/etudes/l159-capacite-des-hotes.md`).
Linux only, and standalone: standard library only, no import of ameesh
(installed alone, run with `python3 -I`); it sits in `ameesh.platform` because
it reads /proc itself (L63).

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

Protocol lines go to stderr, prefixed with `ameesh-offload: `; for `run`,
also with the session nonce (32 hex digits) that this shell draws itself and
announces on the first line, `ameesh-offload: N session`, before any job
starts. The job never holds the session's streams: every phase writes into
pipes that this shell relays (stdout to stdout, stderr to stderr, a protocol
line always starting a line), so no process of the job, even one surviving
it, can read the stream back through `/proc/self/fd/2` or write into it once
the job is over. The client takes only lines carrying the nonce for protocol;
the nonce never passes through argv or an environment, and the shell makes
itself non-dumpable, so a job of the same UID cannot read it in
`/proc/<pid>/{environ,cmdline,mem,fd}`. A `run` always ends with
`ameesh-offload: N result {json}` (outcome `done`, `refused`, `stopped`,
`timeout`, `cancelled` or `error`), whose `mac` is an HMAC-SHA256 of the
result keyed by the nonce; the client strips them.

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
* without XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS for the command and the
  setup (the client's code), and under a seccomp filter that refuses
  `socket(AF_UNIX)` (job-user executor, B3 of the #354 R1 review): the
  profile's own hooks, root-owned install-side code, keep both (rootless
  podman);
* killed whole at the end: its slice, and the process group of each phase
  (without `scope`); what survives anyway only writes into closed pipes;
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

if __name__ == "__main__":
    # first thing (r5): until then /proc/<pid>/fd of this shell, its session's
    # streams, can be opened by any process of the same UID. The interpreter's
    # own start stays a window: only another UID for the jobs closes it
    import ctypes as _ctypes
    if _ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise SystemExit("ameesh-offload: prctl(PR_SET_DUMPABLE, 0) failed")

import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import stat
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
#: the user manager's address: never given to the client's code (command,
#: setup), which could start a unit outside the job's slice through it
BUS_ENV = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
POLL_S = 1.0
#: after a phase, or the job, ends: how long its pipes are still relayed
#: (their last output) before being closed
DRAIN_S = 2.0
#: snapshots offered to the client as thin-pack bases
HAVE_DEPTH = 32
#: the `after` hook (teardown) runs even after a stop, within this delay
AFTER_HOOK_S = 120


#: every git of the shell: no global or system config, and the keys of a
#: repository config that run a command off: a job (same UID) can write the
#: mirrors' config or ~/.gitconfig, its hooks would run outside the slice
GIT_SAFE = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "core.sshCommand=false", "-c", "core.pager=cat",
            "-c", "core.alternateRefsCommand=", "-c", "gc.auto=0",
            # no transport at all: a promisor remote planted in the mirror's
            # config (extensions.partialClone, remote.*.uploadpack) would run
            # its command on a lazy fetch of a missing object
            "-c", "protocol.allow=never"]
GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1",
           "GIT_NO_LAZY_FETCH": "1"}

#: a helper that has not finished in this delay is killed (with its process
#: group): a child keeping the captured stderr open would block the shell
HELPER_TIMEOUT_S = 600
#: index-pack reads the upload: the client's own bound on it
INDEX_PACK_TIMEOUT_S = 1800


def _helper(argv: list[str], *, stdin=None, input: bytes | None = None,
            check: bool = False, timeout: float = HELPER_TIMEOUT_S
            ) -> subprocess.CompletedProcess:
    """A helper of the shell (git, systemctl): never a fd of the session —
    stdin closed (or the given pipe / bytes), stdout and stderr captured —
    and, for git, the isolated config above. `check` raises with its stderr;
    past `timeout`, OSError."""
    if argv[0] == "git":
        argv = GIT_SAFE + argv[1:]
    env = dict(os.environ, **GIT_ENV)
    if input is None and stdin is None:
        stdin = subprocess.DEVNULL
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                            start_new_session=True)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
        raise OSError("%s: no end after %d s, killed" % (" ".join(argv[:4]), timeout))
    res = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if check and res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, argv, res.stdout, res.stderr)
    return res


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
    # L159b2: the job's UID is not the shell's by default on an installed VM
    cfg.setdefault("executor", EXECUTOR_USER_SCOPE)
    if cfg["executor"] not in EXECUTORS:
        raise ValueError("unknown executor %r (expected one of %s)"
                         % (cfg["executor"], ", ".join(EXECUTORS)))
    if cfg["executor"] == EXECUTOR_JOB_USER:
        cfg.setdefault("job_user", "ameesh-job")
        #: the group shared with the job: the shell is a member of it, so it
        #: can give the job the folders it must write (install.sh)
        cfg.setdefault("job_group", "ameesh-jobs")
        cfg.setdefault("job_home", os.path.join(root, "home"))
        cfg.setdefault("launcher", "/usr/local/lib/ameesh-guest/ameesh-job-launcher")
    return cfg


def _dir(cfg: dict, *parts: str, mode: int = 0o700) -> str:
    path = os.path.join(cfg["root"], *parts)
    fresh = not os.path.isdir(path)
    os.makedirs(path, mode=mode, exist_ok=True)
    if fresh:
        # makedirs applies the umask; the job's traversal of a directory the
        # shell has just made (L159b2: cache entries, job folders) must not
        # depend on it. Keep the parent's setgid bit: without it the next
        # level down takes the shell's primary group instead of the shared
        # group, and `grant` (chmod only, the shell cannot chgrp what it does
        # not own as a member) cannot repair it (B1 of the #354 R1 review).
        try:
            if os.stat(os.path.dirname(path)).st_mode & stat.S_ISGID:
                mode |= stat.S_ISGID
            os.chmod(path, mode)
        except OSError:
            pass
    return path


#: the session nonce of a `run`, drawn here and announced first (`session`):
#: every protocol line carries it, so a command printing a line shaped like
#: the protocol (a fake `result`, `refused`…) on the same stderr is never taken
#: for one. It lives only in this process's memory, which `_no_dump` closes
#: to the job (same UID)
NONCE = ""
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")

#: the process this shell was started under: the forced command's sshd
#: session. Captured at the entry of `main`, before any admission work, so a
#: session that dies during admission (the client's wait budget expires
#: before `job` is announced) is never mistaken for a live one: recording
#: `os.getppid()` later would record the reparenting target instead (#5184).
SESSION_PARENT: int | None = None


def _no_dump() -> None:
    """PR_SET_DUMPABLE 0: /proc/<pid>/{environ,cmdline,mem,fd} of this shell
    become root's, so a job of the same UID reads neither the nonce nor the
    session's streams through them. Reset by execve: the job's own processes
    stay dumpable."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0)")


def _access_mode(pid: str, fd: str) -> int | None:
    try:
        with open("/proc/%s/fdinfo/%s" % (pid, fd)) as fh:
            for line in fh:
                if line.startswith("flags:"):
                    return int(line.split()[1], 8) & os.O_ACCMODE
    except (OSError, ValueError, IndexError):
        pass
    return None


def foreign_holders() -> list[str]:
    """Processes, other than this shell, that hold the same end as one of its
    fds 0-2 (the session's streams), among those whose /proc/<pid>/fd this
    UID can read: they could read the session's input or write into its
    output. The other ends belong to sshd (not dumpable, and the reverse
    access mode anyway). Called after `_no_dump`, before this shell starts
    anything."""
    own = set()
    for fd in (0, 1, 2):
        try:
            target = os.readlink("/proc/self/fd/%d" % fd)
            # only the session's own objects: a shared file (/dev/null…) is
            # open in many processes and carries no stream of the session
            if target.startswith(("pipe:", "socket:")):
                own.add((target, fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE))
        except OSError:
            pass
    targets = {target for target, _ in own}
    me = os.getpid()
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        try:
            fds = os.listdir("/proc/%s/fd" % name)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink("/proc/%s/fd/%s" % (name, fd))
            except OSError:
                continue
            if target not in targets:
                continue
            mode = _access_mode(name, fd)
            if any(t == target and (m == mode or os.O_RDWR in (m, mode))
                   for t, m in own):
                found.append("pid %s fd %s (%s)" % (name, fd, target))
    return found


#: this shell's stderr carries both the protocol and the job's relayed
#: stderr: one writer at a time, and whether the last byte ended a line
_ERR_LOCK = threading.Lock()
_err_at_bol = [True]


def emit(kind: str, payload: object = None) -> None:
    line = PREFIX + (NONCE + " " if NONCE else "") + kind
    if payload is not None:
        line += " " + (payload if isinstance(payload, str)
                       else json.dumps(payload, sort_keys=True))
    with _ERR_LOCK:
        try:
            # the job's last output may lack its newline: a protocol line
            # always starts a line of its own
            sys.stderr.write(("" if _err_at_bol[0] else "\n") + line + "\n")
            sys.stderr.flush()
            _err_at_bol[0] = True
        except OSError:
            # the SSH session is gone (EPIPE): the job loop sees it by its parent
            pass


def result_mac(nonce: str, result: dict) -> str:
    """HMAC-SHA256 of a result (job id included), keyed by the session nonce;
    the client checks it on the result line."""
    body = json.dumps({k: v for k, v in result.items() if k != "mac"}, sort_keys=True)
    return hmac.new(nonce.encode(), body.encode(), hashlib.sha256).hexdigest()


def emit_result(result: dict) -> None:
    if NONCE:
        result = dict(result, mac=result_mac(NONCE, result))
    emit("result", result)


def _relay_out(chunk: bytes) -> None:
    try:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
    except OSError:
        pass


def _relay_err(chunk: bytes) -> None:
    with _ERR_LOCK:
        try:
            sys.stderr.flush()
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
            _err_at_bol[0] = chunk.endswith(b"\n")
        except OSError:
            pass


class Relay(threading.Thread):
    """Copies one pipe of a phase (read end `fd`) to `sink` until the pipe is
    closed by every writer, or until `close` and a short delay: a process
    of the job that survives it keeps the write end, never the session's."""

    def __init__(self, fd: int, sink):
        super().__init__(daemon=True)
        self.fd = fd
        self.sink = sink
        self.close_at: float | None = None

    def run(self) -> None:
        try:
            while self.close_at is None or time.time() < self.close_at:
                ready, _, _ = select.select([self.fd], [], [], 0.2)
                if not ready:
                    continue
                chunk = os.read(self.fd, 65536)
                if not chunk:
                    break
                self.sink(chunk)
        finally:
            os.close(self.fd)

    def close(self, grace: float = DRAIN_S) -> None:
        if self.close_at is None:
            self.close_at = time.time() + grace


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
        _helper(["git", "init", "-q", "--bare", path], check=True)
        _helper(["git", "-C", path, "config", "gc.auto", "0"], check=True)
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
            if _helper(["git", "-C", mirror, "cat-file", "-e", name + "^{commit}"]
                       ).returncode == 0:
                sys.stdout.write(name + "\n")
        return 0
    commit = args[1]
    if not SHA_RE.match(commit):
        raise Refused("invalid commit id %r" % commit)
    index_pack(mirror)
    if _helper(["git", "-C", mirror, "cat-file", "-e", commit + "^{commit}"]
               ).returncode != 0:
        raise Refused("commit %s missing after the pack" % commit)
    _helper(["git", "-C", mirror, "update-ref", "refs/offload/" + commit, commit],
            check=True)
    touch_snapshot(mirror, commit)
    prune_mirror(cfg, mirror)
    return 0


def index_pack(mirror: str) -> None:
    """Stores the pack read on the session's stdin. The shell copies it into a
    pipe: git never holds a fd of the session."""
    read_fd, write_fd = os.pipe()
    box: dict = {}

    def run() -> None:
        try:
            with os.fdopen(read_fd, "rb") as pipe:
                box["res"] = _helper(["git", "-C", mirror, "index-pack", "--stdin",
                                      "--fix-thin"], stdin=pipe,
                                     timeout=INDEX_PACK_TIMEOUT_S)
        except BaseException as exc:  # given back to the caller
            box["exc"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        with os.fdopen(write_fd, "wb") as pipe:
            shutil.copyfileobj(sys.stdin.buffer, pipe, 1 << 16)
    except BrokenPipeError:
        pass  # git stopped reading: its exit code says why
    thread.join()
    if "exc" in box:
        raise box["exc"]
    res = box["res"]
    if res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, ["git", "index-pack"],
                                            res.stdout, res.stderr)


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
    out = _helper(["git", "-C", mirror, "for-each-ref", "--format=%(refname)",
                   "refs/offload/"], check=True)
    old = []
    for ref in out.stdout.decode().split():
        stamp = os.path.join(stamps, ref.rsplit("/", 1)[-1])
        if not os.path.exists(stamp) or os.path.getmtime(stamp) < limit:
            old.append(ref)
            if os.path.exists(stamp):
                os.unlink(stamp)
    if old:
        _helper(["git", "-C", mirror, "update-ref", "--stdin"],
                input="".join("delete %s\n" % ref for ref in old).encode(), check=True)
    _helper(["git", "-C", mirror, "-c", "gc.auto=50", "gc", "--auto", "--quiet"])
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

def _open_private(path: str, flags: int = os.O_WRONLY | os.O_CREAT | os.O_TRUNC) -> int:
    """A file of the shell's own state: the job must never be able to make the
    shell write *through* a link it planted there (B2 of the #354 R1 review).
    O_NOFOLLOW refuses the link; O_EXCL refuses a re-planted entry."""
    flags |= os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno not in (errno.ELOOP, errno.EEXIST):
            raise
    # a link (or a stale entry) is in the way: remove the entry itself, never
    # its target, then create ours exclusively
    try:
        os.unlink(path)
    except OSError:
        pass
    return os.open(path, flags | os.O_EXCL, 0o600)


def _write_private(path: str, data: str = "") -> None:
    with os.fdopen(_open_private(path), "w", encoding="utf-8") as fh:
        fh.write(data)


def _write_meta(path: str, meta: dict) -> None:
    tmp = path + ".tmp"
    with os.fdopen(_open_private(tmp), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, sort_keys=True)
    os.replace(tmp, path)


def _read_meta(job_dir: str) -> dict | None:
    try:
        fd = os.open(os.path.join(job_dir, "meta.json"),
                     os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, encoding="utf-8") as fh:
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


def _signal_shell(meta: dict, sig: int) -> bool:
    """Signals the job's shell only while it is still that process. The pidfd
    (Linux 5.3+) is opened first, the identity checked after: if it matches,
    the pidfd holds that very shell, so a PID reused between the check and
    the signal is never hit. Only without pidfd support (no API, ENOSYS) do
    the check and the signal stay two steps (a narrow window); any other
    failure to open it (EPERM, EMFILE...) raises rather than signal a PID
    that may have been reused. False when the shell is gone."""
    pid = meta.get("shell_pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    pidfd = None
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        try:
            pidfd = os.pidfd_open(pid)
        except ProcessLookupError:
            return False
        except OSError as exc:
            if exc.errno != errno.ENOSYS:
                raise
    try:
        if not _shell_alive(meta):
            return False
        if pidfd is None:
            os.kill(pid, sig)
        else:
            signal.pidfd_send_signal(pidfd, sig)
    except ProcessLookupError:
        return False
    finally:
        if pidfd is not None:
            os.close(pidfd)
    return True


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
    _helper(["systemctl", "--user", *args])


def open_slice(cfg: dict, job_id: str) -> None:
    """Caps the job's slice before anything runs in it: the per-job
    `MemoryMax` then covers setup, hooks, command and containers together."""
    slice_name, _ = unit_names(job_id)
    props = ["MemoryMax=%s" % cfg["agent_memory_max"], "MemorySwapMax=0"] \
        if cfg.get("agent_memory_max") else []
    if props:
        res = _helper(["systemctl", "--user", "set-property", "--runtime", slice_name, *props])
        if res.returncode != 0:
            raise Refused("cannot cap the job slice: %s"
                          % res.stderr.decode(errors="replace").strip())


def close_slice(job_id: str) -> None:
    """Kills whatever is left in the job's slice, then drops its runtime caps."""
    slice_name, _ = unit_names(job_id)
    _systemctl("kill", "--signal=SIGKILL", slice_name)
    _systemctl("stop", slice_name)
    _systemctl("revert", slice_name)


#: how a job's phases reach their slice (L159b2)
EXECUTOR_USER_SCOPE = "user-scope"
EXECUTOR_JOB_USER = "job-user"
EXECUTORS = (EXECUTOR_USER_SCOPE, EXECUTOR_JOB_USER)


class Executor:
    """Where the phases of one job run.

    `user-scope` (historical): the shell's own systemd user manager, so the
    job keeps the shell's UID -- `_no_dump` and `foreign_holders` close what
    that lets a job reach. `job-user`: a dedicated user, through the
    root-owned launcher, so the kernel already denies the job the shell's
    `/proc`, HOME, mirrors and caches, and cannot write the repositories the
    shell's own git reads (reviews r5/r7 of #213)."""

    name = EXECUTOR_USER_SCOPE
    #: True while the job shares the shell's UID (the /proc of one is readable)
    same_uid = True
    #: True when the phase's environment travels on stdin (the launcher's JSON)
    env_on_stdin = False
    #: True when the phases run in a systemd slice with a per-job cap
    scope = False

    def __init__(self, cfg: dict, job_id: str):
        self.cfg = cfg
        self.job_id = job_id

    # -- before anything runs ----------------------------------------------
    def check(self, env: dict) -> None:
        """Refuse the job (the client falls back) rather than report the
        failure of the launch as the command's own exit code."""

    def tune_env(self, env: dict) -> None:
        """The host's identity for the job (HOME, USER, LOGNAME)."""

    def client_env(self, phase: str, env: dict) -> dict:
        """What the client's own code (command, setup) may see of the host."""
        return env

    # -- how a phase is launched -------------------------------------------
    def argv(self, phase: str, argv: list[str]) -> list[str]:
        return argv

    # -- the job's containment ---------------------------------------------
    def open(self) -> None:
        """Caps the job's slice before anything runs in it."""

    def kill(self) -> None:
        """SIGKILLs whatever is left of the job."""

    def close(self) -> None:
        """`kill`, then drops the job's runtime containment."""

    # -- what the job may reach --------------------------------------------
    def grant(self, path: str) -> None:
        """A path the shell just made: let the job write it and its tree."""

    def granted(self, path: str) -> bool:
        """True when a tree the shell built already carries what the job
        needs from it: then it is never granted again."""
        return True

    def grant_traverse(self, path: str) -> None:
        """A path of the shell's own state that the job must only enter (its
        job folder): group read and traverse, never group write, so nothing
        the job plants there can be followed by the shell (B2 of the #354 R1
        review)."""

    def clean(self, path: str) -> None:
        """Remove a path the job may have filled in."""
        shutil.rmtree(path, ignore_errors=True)


class UserScopeExecutor(Executor):
    """The shell's own systemd user manager: the job keeps the shell's UID."""

    name = EXECUTOR_USER_SCOPE
    same_uid = True
    env_on_stdin = False

    def __init__(self, cfg: dict, job_id: str):
        super().__init__(cfg, job_id)
        self.scope = bool(cfg.get("scope"))

    def check(self, env: dict) -> None:
        if self.scope:
            check_user_manager(env)

    def client_env(self, phase: str, env: dict) -> dict:
        if phase in ("command", "setup") and not self.scope:
            # with a scope, the `env -u` wrapper in `argv` does this: the
            # address stays in systemd-run's own environment
            return {k: v for k, v in env.items() if k not in BUS_ENV}
        return env

    def argv(self, phase: str, argv: list[str]) -> list[str]:
        if not self.scope:
            return argv
        if phase in ("command", "setup"):
            # systemd-run needs the user manager's address, the scope's
            # process (the client's code) does not get it
            unset = [w for key in BUS_ENV for w in ("-u", key)]
            argv = ["/usr/bin/env", *unset, "--"] + argv
        slice_name, unit = unit_names(self.job_id)
        if phase != "command":
            unit += "-" + phase
        return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                "--slice", slice_name, "--unit", unit, "--"] + argv

    def open(self) -> None:
        open_slice(self.cfg, self.job_id)

    def kill(self) -> None:
        if self.scope:
            _systemctl("kill", "--signal=SIGKILL", unit_names(self.job_id)[0])

    def close(self) -> None:
        if self.scope:
            close_slice(self.job_id)


class JobUserExecutor(Executor):
    """A dedicated user (L159b2), through the root-owned launcher.

    The job has another UID than the shell: it reads neither the shell's
    `/proc` (so not the nonce nor the session's streams), nor its HOME,
    mirrors and caches, and it cannot write the repositories the shell's own
    git will read. Its systemd manager (linger) still owns the job's slice,
    so rootless Podman and its cgroup delegation keep working."""

    name = EXECUTOR_JOB_USER
    same_uid = False
    env_on_stdin = True
    scope = True

    @property
    def user(self) -> str:
        return str(self.cfg["job_user"])

    @property
    def group(self) -> str:
        return str(self.cfg.get("job_group") or "ameesh-jobs")

    @property
    def home(self) -> str:
        return str(self.cfg.get("job_home") or self.cfg["root"])

    def _gid(self) -> int | None:
        import grp
        try:
            return grp.getgrnam(self.group).gr_gid
        except KeyError:
            # an install.sh older than the shared group: the setgid
            # inheritance still gives the job what it was granted
            return None

    @property
    def launcher(self) -> str:
        return str(self.cfg["launcher"])

    def _launcher(self, *args: str) -> list[str]:
        # sudo resets the environment: everything the job needs travels in the
        # launcher's JSON on stdin, never as arguments (except the phase's own
        # argv, which the launcher hands to systemd-run after `--`)
        return ["/usr/bin/sudo", "-n", "-u", self.user, "--", self.launcher, *args]

    def _call(self, *args: str) -> subprocess.CompletedProcess:
        return _helper(self._launcher(*args))

    def check(self, env: dict) -> None:
        if not os.path.isfile(self.launcher):
            raise Refused("job launcher %s is missing (run deploy/guest/install.sh)"
                          % self.launcher)
        res = self._call("check")
        if res.returncode != 0:
            raise Refused("job launcher unavailable for %s: %s"
                          % (self.user, res.stderr.decode(errors="replace").strip()))

    def tune_env(self, env: dict) -> None:
        env["HOME"] = self.home
        env["USER"] = self.user
        env["LOGNAME"] = self.user
        # hooks (rootless Podman) need the job's own manager; the launcher
        # sets the same values for the phases it starts
        run_dir = "/run/user/%d" % _uid_of(self.user)
        env["XDG_RUNTIME_DIR"] = run_dir
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % run_dir

    def client_env(self, phase: str, env: dict) -> dict:
        if phase in ("command", "setup"):
            # the client's code does not get the job's user manager: it could
            # start a unit outside the job's slice through it
            return {k: v for k, v in env.items() if k not in BUS_ENV}
        return env

    def argv(self, phase: str, argv: list[str]) -> list[str]:
        return self._launcher("run", "--job", self.job_id, "--phase", phase, "--", *argv)

    def open(self) -> None:
        res = self._call("open", "--job", self.job_id)
        if res.returncode != 0:
            raise Refused("cannot cap the job slice: %s"
                          % res.stderr.decode(errors="replace").strip())

    def kill(self) -> None:
        self._call("kill", "--job", self.job_id)

    def close(self) -> None:
        self._call("close", "--job", self.job_id)

    def grant(self, path: str) -> None:
        """The shell owns what it has just made (checkout, job folder, cache
        entry): add the group's write bit and the setgid bit, so the job can
        build there and its own files keep the shared group. By descriptor
        (`_grant_tree`), so a same-UID job cannot race the walk with a planted
        link (B3 of the #354 R2 review). The group is set explicitly where the
        shell is a member of it (install.sh): setgid inheritance alone breaks
        on a folder the shell did not create under a setgid parent (B1 of the
        #354 R1 review)."""
        try:
            _grant_tree(path, self._gid())
        except OSError:
            return

    def granted(self, path: str) -> bool:
        """A ready cache is usable when the shared group already owns it and
        can write it: the shell granted it when it built it. A tree the job
        may have written (group write) is never granted again -- a same-UID
        job could have planted a link inside it (B3 of the #354 R2 review) --
        so a cache that does not qualify is rebuilt instead."""
        try:
            st = os.lstat(path)
        except OSError:
            return False
        if not stat.S_ISDIR(st.st_mode) or not stat.S_IMODE(st.st_mode) & 0o020:
            return False
        gid = self._gid()
        return gid is None or st.st_gid == gid

    def grant_traverse(self, path: str) -> None:
        """The job's own folder: it enters it (`work`, `tmp` are granted
        below) but cannot create, rename or remove anything *in* it, so
        `meta.json` and `cancel` stay out of its reach. Opened `O_NOFOLLOW`
        and changed on the descriptor (B3 of the #354 R2 review)."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            st = os.fstat(fd)
            gid = self._gid()
            if gid is not None and st.st_gid != gid:
                try:
                    os.fchown(fd, -1, gid)
                except OSError:
                    pass
            os.fchmod(fd, stat.S_IMODE(st.st_mode) | 0o050 | stat.S_ISGID)
        except OSError:
            return
        finally:
            os.close(fd)

    def clean(self, path: str) -> None:
        """The job may have left files the shell cannot enter (0700): the
        launcher empties them as the job's UID, the shell removes what is
        left."""
        self._call("clean", "--path", path)
        shutil.rmtree(path, ignore_errors=True)


def _uid_of(user: str) -> int:
    import pwd
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        # a VM whose install.sh never created the job user: the client falls
        # back locally instead of reading a traceback
        raise Refused("no such job user %r (run deploy/guest/install.sh)" % user) from None


def _grant_fd(fd: int, gid: int | None = None) -> None:
    """Grant one already-open inode: `fchmod`/`fchown` on the descriptor, so
    no path is ever re-resolved (B3 of the #354 R2 review)."""
    st = os.fstat(fd)
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return
    mode = stat.S_IMODE(st.st_mode) | 0o060      # the shared group reads/writes
    if stat.S_ISDIR(st.st_mode):
        mode |= 0o2010                            # traverse, and setgid below
    elif mode & 0o100:
        mode |= 0o010                             # runnable, so group-runnable
    if gid is not None and st.st_gid != gid:
        try:
            # the shell is a member of the shared group (install.sh); a
            # non-member falls back to the setgid inheritance. Before the
            # chmod: a chown can drop the setgid bit of a directory.
            os.fchown(fd, -1, gid)
        except OSError:
            pass
    os.fchmod(fd, mode)


def _grant_tree(path: str, gid: int | None = None) -> None:
    """`_grant_fd` over a tree, by descriptor: every directory is opened
    `O_NOFOLLOW|O_DIRECTORY` before it is read, every entry `O_NOFOLLOW`
    before it is granted, and a link is never followed nor granted. A same-UID
    job racing the walk with a planted link therefore cannot make the shell
    chmod its target (B3 of the #354 R2 review). A grant that raises closes
    the directories still stacked, so the walk leaks no descriptor (N3 of the
    #354 R2 review)."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        root_fd = os.open(path, flags | os.O_DIRECTORY)
    except OSError:
        return
    stack = [root_fd]
    try:
        while stack:
            fd = stack.pop()
            try:
                _grant_fd(fd, gid)
                try:
                    names = os.listdir(fd)
                except OSError:
                    names = ()
                for name in names:
                    if name in (".", ".."):
                        continue
                    try:
                        child = os.open(name, flags | os.O_DIRECTORY, dir_fd=fd)
                    except OSError:
                        child = -1
                    if child >= 0:
                        stack.append(child)
                        continue
                    try:
                        child = os.open(name, flags, dir_fd=fd)
                    except OSError:
                        continue      # a link, a socket, an entry we may not open
                    try:
                        _grant_fd(child, gid)
                    finally:
                        os.close(child)
            finally:
                os.close(fd)
    finally:
        # an error while granting must not leave the directories still on the
        # stack open: close what is left before the exception leaves
        for fd in stack:
            try:
                os.close(fd)
            except OSError:
                pass


def make_executor(cfg: dict, job_id: str) -> Executor:
    kind = str(cfg.get("executor") or EXECUTOR_USER_SCOPE)
    if kind == EXECUTOR_USER_SCOPE:
        return UserScopeExecutor(cfg, job_id)
    if kind == EXECUTOR_JOB_USER:
        return JobUserExecutor(cfg, job_id)
    raise Refused("unknown executor %r" % kind)


class Job:
    """Runs the phases of one job under the same supervision: cancel, SSH
    session loss, host gate and one deadline for the whole job."""

    def __init__(self, cfg: dict, job_id: str, job_dir: str,
                 executor: Executor | None = None, ssh_parent: int | None = None):
        self.cfg = cfg
        self.job_id = job_id
        self.cancel_flag = os.path.join(job_dir, "cancel")
        # `main` passes the parent observed before admission; direct callers
        # (tests) and jobs created outside `main` keep the historical anchor.
        self.ssh_parent = os.getppid() if ssh_parent is None else ssh_parent
        # set from the slot on (`set_limit`): waiting in the queue is no timeout
        self.deadline = float("inf")
        self.limit = int(cfg["max_job_seconds"])
        self.stop: dict = {}
        self.proc: subprocess.Popen | None = None
        self.executor = executor or make_executor(cfg, job_id)
        self.scope = self.executor.scope
        #: every phase's pipes and process group, closed and killed by `close`
        self.relays: list[Relay] = []
        self.groups: list[int] = []

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
        return self.executor.argv(phase, argv)

    def close(self) -> None:
        """End of the job: kills what is left of every phase, then relays
        their pipes' last output and closes them. Without `scope`, a process
        that left its group (setsid) lives on, writing into closed pipes."""
        self.executor.kill()
        for pgid in self.groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for relay in self.relays:
            relay.close()
        for relay in self.relays:
            relay.join(DRAIN_S + 1)

    def kill(self) -> None:
        self.executor.kill()
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
        """Runs one phase in the job's slice. Its stdout and stderr are pipes
        of this shell, never the session's: stdout is relayed to stdout,
        to stderr (hooks: the client's stdout is the command's alone), or
        captured; stderr to stderr. Returns (exit code, captured stdout); a
        supervised phase ends early, killed, when `check` says so."""
        env = self.executor.client_env(phase, env)
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        stdin = subprocess.PIPE if self.executor.env_on_stdin else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(self.argv(phase, argv), cwd=cwd, env=env, stdout=out_w,
                                    stderr=err_w, stdin=stdin,
                                    start_new_session=True)
        except BaseException:
            for fd in (out_r, err_r):
                os.close(fd)
            raise
        finally:
            os.close(out_w)
            os.close(err_w)
        if self.executor.env_on_stdin:
            # the launcher reads the job's environment as JSON on stdin; its
            # own stdin is then at EOF, which the phase inherits
            try:
                proc.stdin.write(json.dumps(env).encode())
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        self.proc = proc
        self.groups.append(proc.pid)
        chunks: list[bytes] = []
        sink = chunks.append if capture else (_relay_out if phase == "command" else _relay_err)
        reader = Relay(out_r, sink)
        self.relays += [reader, Relay(err_r, _relay_err)]
        for relay in self.relays[-2:]:
            relay.start()
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
        # its output up to the end of the pipe; a child left running (a
        # hook's server) holds the pipe open: then no more than DRAIN_S
        reader.join(DRAIN_S)
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
    # the job enters it: the leaf must be traversable by the shared group
    base = _dir(job.cfg, "cache", profile_name, mode=0o750)
    key = _lock_hash(work, profile)
    final = os.path.join(base, key)
    fd = _open_private(final + ".lock", os.O_RDWR | os.O_CREAT)
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
            if job.executor.granted(final):
                return final
            # a cache built before the shared group (or by an interrupted
            # install) does not carry what the job needs: it is NOT re-granted
            # by path, a same-UID job could have planted a link or a hard link
            # inside it (B3 of the #354 R2 review). Rebuilt below, under the
            # shared group; the next runs reuse it.
        emit("setup", {"profile": profile_name, "cache": key})
        # built in place (a venv records its own path); `.ready` marks a
        # complete cache, so a setup killed halfway is redone
        job.executor.clean(final)
        os.makedirs(final, mode=0o700)
        job.executor.grant(final)
        rc, _ = job.run("setup", ["/bin/sh", "-ec", profile["setup"]], work,
                        dict(env, OFFLOAD_CACHE=final))
        if job.stop:
            job.executor.clean(final)
            raise Stopped()
        if rc != 0:
            job.executor.clean(final)
            raise Refused("profile %s: setup failed (exit %d)" % (profile_name, rc))
        _write_private(ready)
        return final
    finally:
        os.close(fd)


def checkout(mirror: str, work: str, commit: str) -> None:
    """A throwaway work tree of `commit`, borrowing the mirror's objects
    (a clone of a mirror without branches would warn, and copy refs)."""
    _helper(["git", "init", "-q", work], check=True)
    with open(os.path.join(work, ".git", "objects", "info", "alternates"), "w",
              encoding="utf-8") as fh:
        fh.write(os.path.join(mirror, "objects") + "\n")
    _helper(["git", "-C", work, "checkout", "-q", "--detach", commit], check=True)


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
        emit_result(result)
        return 2
    if SESSION_PARENT is not None and os.getppid() != SESSION_PARENT:
        # the session died while the request was on its way (idle client,
        # slow startup): no client is left to receive a result or to cancel,
        # and a job started now would never be seen. Create none (#5184).
        result.update(outcome="cancelled",
                      reason="SSH session closed before the job started")
        emit_result(result)
        return 130
    state, _, reason = gate_state(cfg)
    if state != "available":
        result.update(outcome="refused", reason="host %s%s" % (state, ": " + reason if reason else ""))
        emit_result(result)
        return 75
    mirror = mirror_path(cfg, req["repo"])
    if _helper(["git", "-C", mirror, "cat-file", "-e", req["commit"] + "^{commit}"]
               ).returncode != 0:
        result.update(outcome="refused", reason="unknown commit %s (put it first)" % req["commit"])
        emit_result(result)
        return 2
    touch_snapshot(mirror, req["commit"])
    env = base_env(cfg)
    executor = make_executor(cfg, job_id)
    try:
        executor.tune_env(env)
        if executor.scope:
            executor.check(env)
    except Refused as exc:
        result.update(outcome="refused", reason=str(exc))
        emit_result(result)
        return 75
    prune_orphans(cfg)
    job_dir = os.path.join(_dir(cfg, "jobs", mode=0o750), job_id)
    os.makedirs(job_dir, mode=0o700)
    # the job enters its folder, but only `work` and `tmp` below are writable
    # by it: meta.json and the cancel flag stay out of its reach, so it cannot
    # plant the links the shell would follow (B2 of the #354 R1 review)
    executor.grant_traverse(job_dir)
    meta_path = os.path.join(job_dir, "meta.json")
    meta = {"job": job_id, "state": "waiting", "repo": req["repo"], "commit": req["commit"],
            "profile": req["profile"], "argv": req["argv"], "created_ts": int(started),
            "shell_pid": os.getpid(), "shell_start": _proc_identity(os.getpid())}
    _write_meta(meta_path, meta)
    emit("job", job_id)
    job = Job(cfg, job_id, job_dir, executor=executor, ssh_parent=SESSION_PARENT)

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
        job.executor.grant(tmp)
        checkout(mirror, work, req["commit"])
        job.executor.grant(work)
        env.update(TMPDIR=tmp, TMP=tmp, TEMP=tmp, OFFLOAD_JOB=job_id, OFFLOAD_WORK=work)
        if job.executor.scope:
            env["OFFLOAD_SLICE"] = unit_names(job_id)[0]
            job.executor.open()
            sliced = True
        profile = load_profile(cfg, req["profile"])
        env["OFFLOAD_CACHE"] = prepare_cache(job, req["profile"], profile, work, env)
        for key, value in profile["env"].items():
            env[key] = _expand(str(value), env)
        # B1 of the #354 R2 review: the client's `--env` goes to the command
        # only. The hooks run in the job's scope with the user manager, and an
        # `LD_AUDIT` from the client would load its library there before any
        # filter (the launcher also strips the loader variables from them).
        cmd_env = dict(env)
        cmd_env.update(req["env"])
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
                    cmd_env[key] = value
        # a session lost during admission, or a cancel that arrived before the
        # command: stop here, before `running` and the launch. Starting the
        # command would leave a job the client can neither see nor cancel
        # (#5184: the client's budget expired before `job` was announced).
        if job.check():
            raise Stopped()
        meta.update(state="running", started_ts=int(time.time()), limit_s=job.limit)
        _write_meta(meta_path, meta)
        emit("start", {"job": job_id, "profile": req["profile"], "limit_s": job.limit})
        rc, _ = job.run("command", req["argv"], cwd, cmd_env)
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
            # B4 of the #354 R2 review: what the command left behind shares
            # the job's UID and is still alive; the `after` hook runs without
            # the payload's filter and with the manager's address. Kill the
            # scope before it, so nothing of the payload survives into it.
            job.executor.kill()
            if profile is not None and profile["after"]:
                # teardown (the job's containers), even after a stop
                _hook(job, "after", profile["after"], work, env, supervised=False)
        finally:
            job.close()
            if sliced:
                job.executor.close()
            result["seconds"] = round(time.time() - started, 1)
            # journal and cleanup first: once the result is out, the client
            # may move on (and the session may be gone already)
            _journal(cfg, dict(meta, **result))
            if slot_fd is not None:
                os.close(slot_fd)
            job.executor.clean(job_dir)
            emit_result(result)


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
    jobs = _dir(cfg, "jobs", mode=0o750)
    for name in os.listdir(jobs):
        meta = _read_meta(os.path.join(jobs, name))
        if meta is None or _shell_alive(meta):
            continue
        job_id = str(meta.get("job") or name)
        job_dir = os.path.join(jobs, name)
        executor = make_executor(cfg, job_id)
        executor.kill()
        try:
            profile = load_profile(cfg, str(meta.get("profile") or "default"))
        except Refused:
            profile = None
        if profile is not None and profile["after"]:
            env = base_env(cfg)
            executor.tune_env(env)
            tmp = os.path.join(job_dir, "tmp")
            env.update(OFFLOAD_JOB=job_id, OFFLOAD_WORK=os.path.join(job_dir, "work"),
                       TMPDIR=tmp, TMP=tmp, TEMP=tmp)
            if executor.scope:
                env["OFFLOAD_SLICE"] = unit_names(job_id)[0]
            _quiet_phase(executor, "after", ["/bin/sh", "-ec", profile["after"]],
                         cfg["root"], env, AFTER_HOOK_S)
        executor.close()
        meta["state"] = "orphan"
        _journal(cfg, dict(meta, outcome="error", reason="orphan job killed and removed"))
        executor.clean(job_dir)


def _quiet_phase(executor: Executor, phase: str, argv: list[str], cwd: str,
                 env: dict, timeout: float) -> None:
    """A phase whose output goes nowhere (the teardown of an orphan job)."""
    env = executor.client_env(phase, env)
    stdin = subprocess.PIPE if executor.env_on_stdin else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(executor.argv(phase, argv), cwd=cwd, env=env,
                                stdin=stdin, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return
    if executor.env_on_stdin:
        try:
            proc.stdin.write(json.dumps(env).encode())
            proc.stdin.close()
        except (OSError, ValueError):
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _journal(cfg: dict, entry: dict) -> None:
    """One line per job in `jobs.jsonl`: the usage measure of the study (4.4)."""
    entry = {k: v for k, v in entry.items() if k not in ("shell_pid", "shell_start", "pid")}
    entry["ended_ts"] = int(time.time())
    try:
        # 0600: with a job user (L159b2) the volume is traversable by the
        # shared group, and every job's command lines stay the shell's alone
        fd = os.open(os.path.join(cfg["root"], "jobs.jsonl"),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
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
    _write_private(os.path.join(job_dir, "cancel"))
    _signal_shell(meta, signal.SIGTERM)  # never a process that reused the PID
    sys.stdout.write("cancelled %s\n" % args[0])
    return 0


VERBS = {"put": cmd_put, "run": cmd_run, "status": cmd_status, "cancel": cmd_cancel}


def main(argv: list[str] | None = None) -> int:
    # the session parent, before anything slow: a later `os.getppid()` may be
    # the reparenting target of a session that already died (see Job)
    global SESSION_PARENT
    SESSION_PARENT = os.getppid()
    argv = sys.argv[1:] if argv is None else argv
    config = DEFAULT_CONFIG
    if argv[:1] == ["--config"] and len(argv) >= 2:
        config = argv[1]
    # the request leaves the environment: no child inherits it
    request = os.environ.pop("SSH_ORIGINAL_COMMAND", "")
    try:
        words = shlex.split(request)
    except ValueError:
        words = []
    if not words or words[0] not in VERBS:
        sys.stderr.write(PREFIX + "allowed verbs: put, run, status, cancel\n")
        return 2
    if "--nonce" in words[1:2]:
        # a client's nonce would sit in argv and environments: no longer taken
        sys.stderr.write(PREFIX + "--nonce is no longer accepted: update the client\n")
        return 2
    try:
        _no_dump()
    except OSError as exc:
        sys.stderr.write(PREFIX + "error %s\n" % exc)
        return 70
    if words[0] == "run":
        global NONCE
        NONCE = secrets.token_hex(16)
        emit("session")
    try:
        cfg = load_config(config)
    except (OSError, ValueError) as exc:
        emit("error", str(exc))
        return 70
    if make_executor(cfg, "probe").same_uid:
        # with a dedicated job user (L159b2) the kernel already denies the
        # job's UID the shell's /proc: the scan has nothing left to find
        # (review r7 of #213)
        holders = foreign_holders()
        if holders:
            # an error, never `refused`: the client must not rerun locally what
            # another process may be watching
            reason = "session streams held by another process: " + ", ".join(holders[:5])
            if words[0] == "run":
                emit_result({"job": None, "outcome": "error", "exit": None, "reason": reason})
            else:
                emit("error", reason)
            return 70
    try:
        return VERBS[words[0]](cfg, words[1:])
    except Refused as exc:
        emit("refused", str(exc))
        return 2
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        emit("error", str(exc))
        return 70


if __name__ == "__main__":
    sys.exit(main())
