# SPDX-License-Identifier: AGPL-3.0-only
"""L122 named migration slots and allocation scoped to a target."""
from __future__ import annotations

import abc


class MigrationReservations(abc.ABC):
    @abc.abstractmethod
    def lock_target(self, repo: str, branch: str) -> None:
        """Serialize target observations and edits in an open transaction.

        The repository lock is acquired first to cover unknown legacy targets.
        """

    @abc.abstractmethod
    def for_target(self, repo: str, branch: str) -> list[dict]:
        """Active target records, including conservative unknown legacy targets."""

    @abc.abstractmethod
    def for_lot(self, item_id: int, slot: str | None = None) -> list[dict]:
        """Read records including released and landed slots, without allocating."""

    @abc.abstractmethod
    def update(self, item_id: int, slots: list[str], project: str, repo: str, branch: str,
               sha: str, floor: int, *, action: str, actor: str, expected: dict | None = None,
               observed: list[dict] | None = None, requalified: dict | None = None) -> list[dict] | dict:
        """Allocate/replay or transition a slot atomically; return record or error.

        Apply ancestry/file observations taken under the target lock before allocating.
        Arrivals become landed; only ancestors lacking their numbers are released.
        A landed slot never reallocates. A released slot can reserve again.
        Allocation and journaling commit together. Lot state and slot are
        checked under locks; different lots share the target's allocator.
        """
