# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit execution-lot dependencies (L385)."""
from __future__ import annotations

import abc
from typing import Sequence


class WorkDependencies(abc.ABC):
    @abc.abstractmethod
    def add(self, item_id: int, blocker_id: int, *, actor: str) -> dict:
        """Atomically validate, add and journal an edge, or return `error`.

        Self links and graph cycles are refused. Concurrent graph edits
        serialize; both endpoints are locked while checking their state.
        Existing edges replay without changing activity. New edges require
        both lots to be open. Returns `added` on success.
        """

    @abc.abstractmethod
    def remove(self, item_id: int, blocker_id: int, *, actor: str) -> bool:
        """Remove and journal once; False when the edge is already absent."""

    @abc.abstractmethod
    def for_items(self, item_ids: Sequence[int]) -> list[dict]:
        """All declared edges, with current blocker state and `blocking`.

        Finished blockers remain visible for audit, but are not blocking;
        a reopened blocker becomes blocking again. No rendering limit.
        """

    @abc.abstractmethod
    def set_landing(self, item_id: int, vehicle_id: int, *, until_on: str, actor: str) -> dict:
        """Declare (or renew) a planned landing, pinned to the vehicle's freeze.

        Both lots must exist and be open, and the vehicle must differ from the
        lot. The vehicle must carry a frozen head (the declaration is pinned to
        exactly that SHA) and must not be `blocked`. The pin is read in the
        same transaction as the write. Repeating the gesture replaces the
        previous declaration and journals once. Returns `landed`, or `error`
        (`unknown_lot`, `finished_lot`, `finished_vehicle`, `no_freeze`,
        `blocked_vehicle`).
        """

    @abc.abstractmethod
    def clear_landing(self, item_id: int, *, actor: str) -> bool:
        """Withdraw the declaration and journal once; False when there is none."""

    @abc.abstractmethod
    def landings_for(self, item_ids: Sequence[int]) -> list[dict]:
        """Declared landings of these lots, with the vehicle's current state.

        One row per declared lot: `work_item_id`, `vehicle_id`, `pinned_sha`,
        `until_on` (ISO day), `created_by`, `created_ts`, `vehicle_state`,
        `open` (the vehicle is not merged/promoted/closed). The current head is
        not returned: the caller reads the vehicle's last freeze and compares
        it to `pinned_sha`.
        """

    @abc.abstractmethod
    def release_unassigned(self, item_ids: Sequence[int], *, actor: str) -> list[int]:
        """Return still-unassigned blocked candidates to intake and journal them.

        The caller supplies candidates with no remaining planned wait, in
        the closure transaction while holding the dependency-graph lock.
        Recheck state and assignee when writing; return only changed ids.
        """

    @abc.abstractmethod
    def lock(self) -> None:
        """Take the schema-wide dependency-graph lock in the current transaction.

        A lot that closes can release its dependents: the closure reads the
        graph to decide whether the last wait just fell, then writes the wake
        mail. Two concurrent closures must not both read the graph before the
        other commits — each would see the other blocker still open and no one
        would wake the dependent. Taking this lock before the read and holding
        it until the transaction ends (the write included) serialises them:
        the second closure reads the first one's commit, so exactly one of the
        two sees the last wait fall. Same lock as the graph edits (`add`,
        `remove`), so a closure never decides on a graph being edited.
        """
