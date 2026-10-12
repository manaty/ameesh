# SPDX-License-Identifier: AGPL-3.0-only
"""Stable process handles for background termination; no bare-PID fallback."""
from __future__ import annotations

import os
import errno
import select
import signal
import time
import threading

from .process import start_time, _stat_fields


# Serialize background freezes with the independent suite-cap signal thread.
CLEANUP_SIGNAL_LOCK = threading.RLock()


class PidfdWaitUnavailable(RuntimeError):
    """Kernel/API cannot wait on pidfds; caller must guard any PID fallback."""


class ProcessHandle:
    # Linux 5.3 can open pidfds but cannot yet use waitid(P_PIDFD).
    _wait_supported: bool | None = None

    def __init__(self, pid: int, fd: int):
        self.pid, self.fd = pid, fd

    @classmethod
    def open(cls, pid: int, expected_start) -> "ProcessHandle | None":
        if expected_start is None or not hasattr(os, "pidfd_open") \
                or not hasattr(signal, "pidfd_send_signal"):
            return None
        fd = None
        try:
            fd = os.pidfd_open(pid)
            # Validate the snapshot after acquiring the handle. Subsequent
            # signals use that handle even if the numeric PID is reused.
            if start_time(pid) != expected_start:
                os.close(fd)
                return None
            return cls(pid, fd)
        except Exception:
            if fd is not None:
                os.close(fd)
            return None

    def duplicate(self) -> "ProcessHandle":
        """Retain this same kernel identity for the tracker's later waits."""
        return ProcessHandle(self.pid, os.dup(self.fd))

    def live(self) -> bool:
        if self.fd is None:
            return False
        try:
            # pidfds become readable at exit, including unreaped zombies.
            poll = select.poll()
            poll.register(self.fd, select.POLLIN)
            return not poll.poll(0)
        except (OSError, ValueError):
            return False

    def location(self) -> tuple[int, int] | None:
        """Current group/session, only while this retained identity is live."""
        if not self.live():
            return None
        try:
            group, session = os.getpgid(self.pid), os.getsid(self.pid)
        except OSError:
            return None
        return (group, session) if self.live() else None

    def state(self) -> str | None:
        """State of this live identity; None if no longer readable/live."""
        if not self.live():
            return None
        try:
            state = _stat_fields(self.pid)[0]
            return state if self.live() else None
        except (OSError, ValueError, IndexError):
            return None

    def reap(self) -> int | None:
        """Consume only this retained child's exit, never a reused numeric PID."""
        if self.fd is None:
            return None
        if ProcessHandle._wait_supported is False or not hasattr(os, "P_PIDFD"):
            ProcessHandle._wait_supported = False
            raise PidfdWaitUnavailable("waitid(P_PIDFD) unavailable")
        try:
            info = os.waitid(os.P_PIDFD, self.fd, os.WEXITED | os.WNOHANG)
        except ChildProcessError:
            return None
        except OSError as exc:
            if exc.errno in (errno.ENOSYS, errno.EINVAL):
                ProcessHandle._wait_supported = False
                raise PidfdWaitUnavailable("waitid(P_PIDFD) unsupported") from exc
            return None
        ProcessHandle._wait_supported = True
        if info is None:
            return None
        return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status

    def freeze(self, timeout: float = .5) -> bool:
        """Stop this identity and confirm delivery before checking ownership."""
        if not self.send(signal.SIGSTOP):
            return False
        end = time.monotonic() + timeout
        while self.live() and time.monotonic() < end:
            try:
                if _stat_fields(self.pid)[0] == "T" and self.live():
                    return True
            except (OSError, ValueError, IndexError):
                return False
            time.sleep(.005)
        return False

    def send(self, signum: int) -> bool:
        if self.fd is None:
            return False
        while True:
            try:
                signal.pidfd_send_signal(self.fd, signum)
                return True
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                return False
            except ValueError:
                return False

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
