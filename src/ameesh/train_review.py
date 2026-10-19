# SPDX-License-Identifier: AGPL-3.0-only
"""Exact historical receipt shape shared with #1465/#1467.

The review rule itself now lives in `merge_queue.review_gate` (#2681): the
train calls it with the historical families read by `review_authors`. This
module keeps only the exact receipt contract and the verdict ordering used by
the train (`SHA`, `Receipt`, `MARK_FIELDS`, `_order`). A trusted provider must
establish the commit/milestone-to-turn links; a Git author name, a proximity
in time or a free-form note is not such a link.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .train import TrainError

SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
MARK_FIELDS = ("id", "actor", "at_ts", "sha", "verdict")


@dataclass(frozen=True)
class Receipt:
    subject: str                 # commit:<full sha> or milestone:<id>
    commit: str                  # resolved commit, including short mark SHA
    actor: str
    session: str
    turn: str
    harness: str
    record: str                  # exact provenance record, never free-form note
    milestone: dict | None = None

    def known(self, families):
        return (all(isinstance(value, str) and value.strip() for value in (
            self.subject, self.commit, self.actor, self.session, self.turn, self.record))
                and SHA.fullmatch(self.commit) is not None and self.harness in families)


def _actor(value):
    return str(value or "").strip().casefold()


def _order(mark):
    try:
        stamp, number = float(mark["at_ts"]), int(mark["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TrainError("unreadable verdict identity or date") from exc
    if not math.isfinite(stamp) or number <= 0 or not _actor(mark.get("actor")):
        raise TrainError("unreadable verdict identity or date")
    return stamp, number
