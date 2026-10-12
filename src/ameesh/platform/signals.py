# SPDX-License-Identifier: AGPL-3.0-only
"""Stable process handles for background termination; no bare-PID fallback."""
from __future__ import annotations

import os
import select
import signal

from .process import start_time


class ProcessHandle:
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

    def send(self, signum: int) -> None:
        if self.fd is None:
            return
        try:
            signal.pidfd_send_signal(self.fd, signum)
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
