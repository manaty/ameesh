# SPDX-License-Identifier: AGPL-3.0-only
"""L122 named migration slots and allocation scoped to a target."""
from __future__ import annotations

import abc


class MigrationReservations(abc.ABC):
    @abc.abstractmethod
    def for_lot(self, item_id: int, slot: str | None = None) -> list[dict]:
        """Read records including released and landed slots, without allocating."""

    @abc.abstractmethod
    def update(self, item_id: int, slots: list[str], project: str, repo: str, branch: str,
               sha: str, floor: int, *, action: str, actor: str, expected: dict | None = None) -> list[dict] | dict:
        """Allocate/replay or transition a slot atomically; return record or error.

        Release stale reserved records of this target before allocating.
        A landed slot never reallocates. A released slot can reserve again.
        Allocation and journaling commit together. Lot state and slot are
        checked under locks; different lots share the target's allocator.
        """
