# SPDX-License-Identifier: AGPL-3.0-only
"""Conversation storage contract (L78); cursors are scoped to one agent."""
from __future__ import annotations

import abc
from typing import Sequence


class Conversations(abc.ABC):
    @abc.abstractmethod
    def append(self, *, agent: str, session: str | None, turn: str | None,
               kind: str, author: str, text: str, truncated: bool, meta: dict,
               event_id: str | None, project: str, lot: str | None,
               thread_text: str | None) -> dict:
        """Lock the agent cursor through commit; return an existing event on replay.

        A notification contains only agent and seq, and is published at commit.
        R12 text is kept in an outbox until thread delivery succeeds.
        """

    @abc.abstractmethod
    def read(self, agent: str, *, after: int = 0, limit: int = 100) -> dict:
        """One snapshot: ascending entries, last_seq, oldest_seq and more.

        No thread outbox content is exported. Cursors survive retention.
        """

    @abc.abstractmethod
    def recent(self, agents: Sequence[str], *, limit: int = 20) -> list[dict]:
        """Per-agent last_seq and last `limit` entries in ascending order.

        Empty history keeps its cursor, including after retention.
        """

    @abc.abstractmethod
    def pending_threads(self, agent: str, *, limit: int = 32) -> list[dict]:
        """Undelivered R12 entries, oldest first, locked until commit.

        Includes thread_text; call within a transaction to serialize delivery.
        """

    @abc.abstractmethod
    def thread_delivered(self, agent: str, seq: int, entry_id: str) -> None:
        """Acknowledge a thread post; retry can produce an auditable duplicate."""

    @abc.abstractmethod
    def prune(self, agent: str, *, before_ts: float,
              thread_before_ts: float | None = None) -> int:
        """Delete old detail for this agent, preserving the cursor and pending
        R12 entries, unless older than `thread_before_ts` (thread retention)."""
