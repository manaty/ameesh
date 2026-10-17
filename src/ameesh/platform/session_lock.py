# SPDX-License-Identifier: AGPL-3.0-only
"""Host-local session exclusion and attach requests (L82).

Locks live on stable files, never unlinked. A dead holder releases its lock
in the OS; neither stale files nor reused process identifiers block a session.
"""
from __future__ import annotations

import os


def try_lock(state_dir: str, kind: str):
    """An open exclusive lock, or None. The caller closes it to release it."""
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    fh = open(os.path.join(state_dir, kind + ".lock"), "a+b")
    try:
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


def requested(state_dir: str) -> bool:
    lock = try_lock(state_dir, "attach-request")
    if lock is None:
        return True
    lock.close()
    return False
