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
