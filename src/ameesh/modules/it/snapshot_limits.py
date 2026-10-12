# SPDX-License-Identifier: AGPL-3.0-only
"""Project-private copy limits and serialized snapshot-store admission."""
from contextlib import contextmanager
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import threading
import time

DEFAULTS = {"max_bytes": 128 * 1024 * 1024, "total_bytes": 512 * 1024 * 1024,
            "min_free_bytes": 512 * 1024 * 1024, "copy_seconds": 15.0}


def settings(raw=None):
    if raw is not None and (not isinstance(raw, dict) or set(raw) - set(DEFAULTS)):
        raise ValueError("snapshot_limits expects max_bytes, total_bytes, min_free_bytes, copy_seconds")
    result = dict(DEFAULTS, **(raw or {}))
    for key, value in result.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            raise ValueError("snapshot limit %s must be positive and finite" % key)
        if key != "copy_seconds" and not isinstance(value, int):
            raise ValueError("snapshot byte limits must be integers")
    return result


def check_time(deadline):
    if time.monotonic() >= deadline:
        raise ValueError("artifact snapshot copy timed out; no proof recorded")


@contextmanager
def copy_timer(deadline):
    """Interrupt blocking copy I/O in the one-shot helper's main thread."""
    check_time(deadline)
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return
    old_handler = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()

    def expired(signum, frame):
        raise ValueError("artifact snapshot copy timed out; no proof recorded")

    signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, max(0.000001, deadline - started))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_timer[0]:
            signal.setitimer(signal.ITIMER_REAL,
                             max(0.000001, old_timer[0] - (time.monotonic() - started)), old_timer[1])


@contextmanager
def store_lock(root, deadline):
    """Serialize admission/copy; execution can proceed concurrently afterwards."""
    if os.name != "posix":
        yield
        return
    import fcntl
    fd = os.open(Path(root, ".snapshot.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("snapshot store lock must be private and producer-owned")
        while True:
            check_time(deadline)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(fd)


def used_bytes(root):
    """Count live and stale helper copies without following symlinks."""
    total = 0
    for folder in Path(root).iterdir():
        if not folder.name.startswith("execution-"):
            continue
        try:
            if not stat.S_ISDIR(folder.lstat().st_mode):
                raise ValueError("unexpected snapshot store entry")
            for file in folder.iterdir():
                info = file.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("unexpected snapshot store content")
                total += info.st_size
        except FileNotFoundError:  # another completed helper only frees space
            continue
    return total


def check_storage(root, used, copied, limits, *, incoming=0):
    if used + copied + incoming > limits["total_bytes"]:
        raise ValueError("snapshot store total byte limit exceeded; no proof recorded")
    if shutil.disk_usage(root).free - incoming < limits["min_free_bytes"]:
        raise ValueError("snapshot volume below free-space reserve; no proof recorded")
