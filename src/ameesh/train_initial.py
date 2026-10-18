# SPDX-License-Identifier: AGPL-3.0-only
"""Qualified initial queue history for packet enrollment (#2446).

``integration_train_queue_receipts`` records every packet allocation and every
withdrawal written after #1467. Older withdrawals were free-text journal lines,
and a free note is not proof: absence of a queue row therefore stays
``queue-history-unknown`` for a legacy freeze.

This reader supplies the missing qualified fact for the switch-over, on an
explicit activation boundary:

* the freeze was created at or after the instant the train became the only
  writer of the packet queue (``initial_since``, configured by the pilot);
* no receipt names that freeze (neither an allocation nor a withdrawal).

Both facts are read from storage on the caller's connection, each time the
enrollment asks. A freeze older than the boundary, or one with any receipt,
is not an initial absence: the normal receipt/replay path decides. Nothing is
written, and no registry, note or branch is consulted.
"""
from __future__ import annotations

import math

from .train import TrainError


class Boundary:
    """``initial_since``: the exact instant the train owns the packet queue."""

    def __init__(self, at):
        if isinstance(at, bool) or not isinstance(at, (int, float)) \
                or not math.isfinite(at):
            raise TrainError("qualified packet activation boundary required")
        self.at = float(at)

    def __call__(self, db, *, subject):
        """True only for a freeze born after the boundary with no receipt."""
        if not isinstance(subject, dict):
            raise TrainError("explicit queue subject required for initial history")
        lot = subject.get("work_item_id")
        frozen_id = subject.get("frozen_id")
        if type(lot) is not int or lot <= 0 or type(frozen_id) is not int or frozen_id <= 0:
            raise TrainError("exact lot and freeze required for initial history")
        rows = db.query(
            "SELECT kind FROM integration_train_queue_receipts"
            " WHERE work_item_id = %s AND frozen_id = %s ORDER BY id", (lot, frozen_id))
        if rows:
            # An allocation is replayed from its own receipt; a withdrawal is
            # final. Neither is an initial absence.
            return False
        rows = db.query(
            "SELECT extract(epoch FROM at)::float8 AS at FROM work_item_milestones"
            " WHERE id = %s AND work_item_id = %s AND kind = 'frozen'", (frozen_id, lot))
        if not rows:
            raise TrainError("frozen milestone of lot #%s missing from complete history" % lot)
        return float(rows[0]["at"]) >= self.at
