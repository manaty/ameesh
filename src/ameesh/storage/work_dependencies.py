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
    def add_link(self, item_id: int, other_id: int, kind: str, *, actor: str) -> dict:
        """#4167: atomically validate, add and journal a typed link.

        `kind` is `duplicates`, `supersedes`, `part_of` (directed: `item_id`
        duplicates/supersedes/is part of `other_id`) or `conflicts_with`
        (symmetric, stored with the smaller id first). Same graph lock as the
        dependencies. Self links (`self_link`), unknown endpoints
        (`unknown_lot`), a conflict with a finished lot (`finished_lot`) and a
        cycle in the directed graph of the same kind (`link_cycle`) are
        refused without mutation. An existing link replays as `added: False`
        without changing activity. The journal goes to `item_id`.
        """

    @abc.abstractmethod
    def remove_link(self, item_id: int, other_id: int, kind: str, *, actor: str) -> bool:
        """Remove and journal once; False when the link is already absent."""

    @abc.abstractmethod
    def decide_link(self, item_id: int, other_id: int, *, decision: str, actor: str) -> dict:
        """Record the decision taken on a `conflicts_with` link, journaled on
        both lots. Returns `decided` (False on the same decision replayed), or
        `error` (`no_link`)."""

    @abc.abstractmethod
    def links_for(self, item_ids: Sequence[int]) -> list[dict]:
        """Every typed link touching these lots, in either direction.

        One row per link: `work_item_id`, `other_id`, `kind`, `created_by`,
        `created_ts`, `decision`, `decided_by`, `decided_ts`, the current
        state of both ends (`item_state`, `other_state`) and `open` (neither
        end is merged/promoted/closed). No rendering limit.
        """

    @abc.abstractmethod
    def undecided_conflicts(self) -> list[dict]:
        """`conflicts_with` links between two open lots with no decision,
        with both ends' assignee, project and title (rows of `links_for`
        plus `item_assignee`, `other_assignee`, `item_app`, `item_title`,
        `other_title`)."""

    @abc.abstractmethod
    def set_landing(self, item_id: int, vehicle_id: int, *, until_on: str, actor: str) -> dict:
        """Declare (or renew) a planned landing, pinned to the vehicle's freeze.

        Both lots must exist and be open, and the vehicle must differ from the
        lot. The landing lot must be in review (`qa`). The vehicle must carry a
        frozen head (the declaration is pinned to
        exactly that SHA) and must not be `blocked`. The pin is read in the
        same transaction as the write. Repeating the gesture replaces the
        previous declaration and journals once. Returns `landed`, or `error`
        (`unknown_lot`, `finished_lot`, `not_in_review`, `finished_vehicle`,
        `no_freeze`, `blocked_vehicle`).
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
    def landings_on_vehicle(self, vehicle_id: int) -> list[dict]:
        """#3641: declarations pinned to this vehicle, one row per unit.

        Read by the freeze path to warn the declarants when a re-freeze makes
        their declaration stale: `work_item_id`, `vehicle_id`, `pinned_sha`,
        `until_on`, `created_by`.
        """

    @abc.abstractmethod
    def landing_matches(self, item_id: int, vehicle_id: int, pinned_sha: str, *,
                        frozen_sha: str) -> bool:
        """#3641: is the declaration still the one pinned at `frozen_sha`?

        Read under the same lock, before warning the declarant: a declaration
        renewed since, or a newer freeze, gets no warning for a delayed one.
        """

    @abc.abstractmethod
    def release_unassigned(self, item_ids: Sequence[int], *, actor: str) -> list[int]:
        """Return still-unassigned blocked candidates to intake and journal them.

        The caller supplies candidates with no remaining planned wait, in
        the closure transaction while holding the dependency-graph lock.
        Recheck state and assignee when writing; return only changed ids.
        A lot pinned to a Process workflow is never released here: its hold
        is lifted only by a `release` command of the registry (#1922/B4).
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
