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
