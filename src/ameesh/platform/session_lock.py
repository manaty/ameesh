# SPDX-License-Identifier: AGPL-3.0-only
"""Host-local session exclusion and attach requests (L82).

Locks live on stable files, never unlinked. A dead holder releases its lock
in the OS; neither stale files nor reused process identifiers block a session.

#3802 : a lock may also carry the identity of its holder — a reserved JSON
key in the same file, written by the holder after it took the lock. Reading
it needs no lock (it is diagnostic), and when it is missing or unreadable the
holder is named from `/proc/locks` and `/proc/<pid>` (Linux); the argv is
never read, so a secret on a command line never reaches a journal (#3403).
"""
from __future__ import annotations

import json
import os

#: reserved key of the holder record inside a lock file's JSON state. Mesh
#: keys are 16 hexadecimal characters, so no state key can collide.
HOLDER_KEY = "_holder"


def try_lock(state_dir: str, kind: str):
    """An open exclusive lock, or None. The caller closes it to release it.

    #3802 : the descriptor is explicitly non-inheritable, so a child forked
    or spawned while the lock is held cannot keep the flock after the pass
    closes it (flock follows the open file description, not the process)."""
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    fh = open(os.path.join(state_dir, kind + ".lock"), "a+b")
    try:
        try:
            os.set_inheritable(fh.fileno(), False)
        except (AttributeError, OSError):   # Windows, exotic platform
            pass
        if os.name == "nt":
            import msvcrt
            if os.fstat(fh.fileno()).st_size == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        import errno
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    except BaseException:
        fh.close()
        raise
    return fh


def read_content(lock) -> dict:
    """The JSON object of a lock file's content ({} if empty, partial or not
    an object). Only its holder writes it while the lock is held."""
    try:
        lock.seek(0)
        value = json.loads(lock.read() or b"{}")
    except (ValueError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def write_content(lock, content: dict) -> None:
    """Replace a lock file's content with `content` (a diagnostic state, not
    a protocol: a reader that sees it partial retries nothing and only loses
    the holder's name)."""
    lock.seek(0)
    lock.truncate()
    lock.write(json.dumps(content, sort_keys=True, default=str).encode("ascii"))
    lock.flush()


def read_state(state_dir: str, kind: str) -> dict:
    """The content of a lock file that another process may hold; {} when it
    cannot be read. No lock is taken: a lock file lives at a stable path."""
    try:
        with open(os.path.join(state_dir, kind + ".lock"), "rb") as fh:
            value = json.loads(fh.read() or b"{}")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _proc_lock_pid(path: str) -> int | None:
    """The pid `/proc/locks` names for the flock on `path`, if any (Linux)."""
    try:
        st = os.stat(path)
        dev = "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        with open("/proc/locks", encoding="ascii", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                # `1: FLOCK  ADVISORY  WRITE  1234 00:1b:12345 0 EOF`
                if len(parts) >= 6 and parts[1] == "FLOCK" and parts[5] == dev:
                    return int(parts[4])
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _alive(pid: int) -> bool:
    """Whether a process exists (a permission refusal means it does)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _process_name(pid: int) -> str:
    """The `comm` of `pid` (never its argv: it may carry a secret, #3403)."""
    try:
        with open("/proc/%d/comm" % pid, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()[:64]
    except OSError:
        return ""


def _process_start(pid: int) -> float | None:
    """When `pid` started (wall clock), from `/proc/<pid>/stat` and the boot
    time of `/proc/stat`; None if unreadable."""
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii", errors="replace") as fh:
            stat = fh.read()
        # the command name is parenthesised and may contain spaces: count
        # fields after the last ')'
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        with open("/proc/stat", encoding="ascii") as fh:
            boot = next(int(line.split()[1]) for line in fh
                        if line.startswith("btime "))
        ticks = os.sysconf("SC_CLK_TCK")
        return boot + start_ticks / float(ticks)
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def holder(state_dir: str, kind: str) -> dict | None:
    """The holder record of a held lock, or None.

    Prefers the record the holder wrote (`pid`, `runner`, `since`, `phase`),
    and falls back to the kernel and `/proc` when the file carries none (an
    older holder, a partial write, a lock taken by another tool): the pid
    comes from `/proc/locks`, the name from `comm`, and the start time from
    the process itself — never its argv (#3403)."""
    path = os.path.join(state_dir, kind + ".lock")
    kernel_pid = _proc_lock_pid(path)
    record = read_state(state_dir, kind).get(HOLDER_KEY)
    if isinstance(record, dict) and record.get("pid"):
        try:
            pid = int(record["pid"])
        except (TypeError, ValueError):
            pid = None
        # the kernel is authoritative when it names the flock's pid: a record
        # left by a dead holder must not name a reused pid
        if pid is not None and _alive(pid) and (kernel_pid is None or kernel_pid == pid):
            out = dict(record, pid=pid)
            out.setdefault("source", "lock")
            return out
    pid = kernel_pid
    if pid is None:
        return None
    since = _process_start(pid)
    if since is None:
        try:
            since = os.stat(path).st_mtime
        except OSError:
            since = None
    return {"pid": pid, "runner": _process_name(pid), "since": since,
            "source": "proc-locks"}


def requested(state_dir: str) -> bool:
    lock = try_lock(state_dir, "attach-request")
    if lock is None:
        return True
    lock.close()
    return False
