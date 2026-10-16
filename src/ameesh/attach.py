# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded native access to an agent session (L82, decision 0035)."""
from __future__ import annotations

import math
import time

from . import mail, stagnation

DEFAULT_MAX = 2 * 3600.0
NOTICE = 60.0
INPUT_DELAY = 10 * 60.0
INPUT_CHECK = 30.0
HANDOFF_TIMEOUT = 60.0


def duration(value: str) -> float:
    seconds = stagnation.parse_duration(value)
    if not math.isfinite(seconds):
        raise ValueError("attach duration must be finite and positive")
    return seconds


class Deadline:
    """Monotonic limit, independent from lease renewal and wall-clock jumps."""
    def __init__(self, seconds: float):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("attach duration must be finite and positive")
        self.end = time.monotonic() + seconds
        self.notice_at = self.end - min(NOTICE, seconds / 2)
        self.noticed = False

    def notice(self) -> float | None:
        now = time.monotonic()
        if not self.noticed and now >= self.notice_at:
            self.noticed = True
            return max(0.0, self.end - now)
        return None

    def expired(self) -> bool:
        return time.monotonic() >= self.end


def waiting_since(db, name: str, row: dict, prompt_since: float | None) -> float | None:
    """Oldest queued input, including pending instructions. Never consumed.

    L77 chat inputs use the mailbox too, so the same check covers them.
    """
    times = [float(m["created_ts"]) for m in mail.unread_active(db, name, limit=1)]
    if row.get("pending_prompt") and prompt_since is not None:
        times.append(prompt_since)
    return min(times) if times else None
