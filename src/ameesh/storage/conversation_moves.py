# SPDX-License-Identifier: AGPL-3.0-only
"""Conversation move storage contract (#7050, design 5322 §2.3).

One row per attempt (`conversation_moves`, migration 0572). Every transition
is a conditional write under the agent's registry row lock: the lease (CAS +
epoch) stays the only arbiter, and `agent_registry.session_id` changes only at
`verify`. Rows carry ids, hosts, bounds and a digest, never segment text.
"""
from __future__ import annotations

import abc

#: states of an attempt; `OPEN` ones block a second attempt for the agent
STATES = ("planned", "prepared", "switched", "recovered", "rehydrating",
          "verified", "failed")
OPEN = ("planned", "prepared", "switched", "recovered", "rehydrating")
#: before the switch: the source keeps its lease but opens no new turn
BEFORE_SWITCH = ("planned", "prepared")
#: after the switch: the row belongs to the target host
AFTER_SWITCH = ("switched", "recovered", "rehydrating")
#: a planned or prepared attempt older than this no longer holds the source
#: back (compiled, seconds): a target that never prepares cannot stop the agent
PLAN_TTL_S = 900
#: #7052: a switched/recovered attempt no target claimed within this no longer
#: blocks a new plan (compiled, seconds). The source closes its attempt after
#: its holdoff; this is the last resort when both hosts are gone, so the next
#: present candidate can plan a recovery instead of waiting forever.
HANDOVER_TTL_S = 300


class ConversationMoves(abc.ABC):
    @abc.abstractmethod
    def plan(self, *, move_id: str, agent: str, target_host: str, reason: str,
             requested_by: str) -> dict | None:
        """Open an attempt for the agent's current lease epoch, or reread it.

        The source (host, cwd, lease owner, session) is copied from the locked
        registry row. Returns the row with `created` (false on replay or when
        another attempt is already open); None if the agent is unknown, not
        `execute`, or already on `target_host`."""

    @abc.abstractmethod
    def get(self, move_id: str) -> dict | None:
        """One attempt by id."""

    @abc.abstractmethod
    def open_for(self, agent: str) -> dict | None:
        """The agent's open attempt, if any."""

    @abc.abstractmethod
    def release_refused(self, agent: str, *, owner: str, epoch: int, host: str) -> bool:
        """After a `conversation_move` admission refusal: `running` back to
        `queued` (prompt waiting) or `idle`, in one compare-and-set on the
        source lease (owner, epoch, host, status still `running`). Any other
        holder, an operator stop or a target turn is left untouched."""

    @abc.abstractmethod
    def holds(self, agent: str) -> bool:
        """An attempt still holds the agent's session back: switched,
        recovered or rehydrating, or planned/prepared younger than
        `PLAN_TTL_S` (the same rule as `begin_turn`)."""

    @abc.abstractmethod
    def for_target(self, host: str) -> list[dict]:
        """Open attempts whose target is `host`, oldest first."""

    @abc.abstractmethod
    def open_handover(self, agent: str) -> dict | None:
        """#7052: the newest post-switch attempt of a `preference` role whose
        target has not claimed yet (no live lease).

        States `switched`, `recovered` and `rehydrating`. Carries `source_host`
        and `target_host`, plus `switched_ts` and `now_ts` on the database
        clock, so the caller computes the holdoff. This is how a candidate
        tells "the conversation was handed to that host, let it claim" from
        "the row is simply free"."""

    @abc.abstractmethod
    def prepare(self, move_id: str, *, target_host: str, target_cwd: str,
                checks: dict) -> dict | None:
        """`planned` → `prepared` once the target has checked harness,
        account, folder and binary, without a model call. `checks` holds
        booleans only. Replay returns the row; None if not applicable."""

    @abc.abstractmethod
    def switch(self, move_id: str, *, target_host: str) -> dict | None:
        """`prepared` → `switched` (source lease live and idle, or released)
        or `recovered` (source lease expired), in one transaction: the lease
        is released (epoch + 1) and the registry row moves to the target host
        and folder; its session is kept (provisional until `verify`). Refused
        while a source turn runs, while an interactive session holds the
        lease, after `PLAN_TTL_S`, or if the row left the source host."""

    @abc.abstractmethod
    def bind(self, move_id: str, *, owner: str, epoch: int, host: str,
             from_seq: int, to_seq: int, digest: str) -> dict | None:
        """The claim of the move: `switched`/`recovered` → `rehydrating`,
        fenced by the target's live lease `(owner, epoch)` on `host`; records
        the segment bounds and digest. Replay with the same lease returns the
        row unchanged (its digest is the reference)."""

    @abc.abstractmethod
    def note_turn(self, move_id: str, *, owner: str, epoch: int,
                  session: str | None) -> bool:
        """The target's first turn was launched; its native session, once
        known, is noted on the attempt only (never in the registry)."""

    @abc.abstractmethod
    def verify(self, move_id: str, *, owner: str, epoch: int, session: str,
               account: str | None, harness: str | None,
               cwd: str | None) -> dict | None:
        """`rehydrating` → `verified`: the registry session becomes the
        target's, in the same transaction, under the live lease; the source
        session becomes `stale-cache`. Replay returns the row."""

    @abc.abstractmethod
    def fail(self, move_id: str, *, error: str, owner: str | None = None,
             epoch: int | None = None, only: tuple[str, ...] | None = None) -> dict | None:
        """→ `failed`. Before the switch the source still holds everything:
        only the attempt changes. After it, with the live lease `(owner,
        epoch)` or with no live lease, the lease is released (epoch + 1) and
        the registry row goes back to the source host and folder, its session
        untouched; a launched target turn leaves a notice in the pending
        prompt. None if the attempt is closed or the caller is fenced out.
        `only`, when given, restricts the transition to those states: the
        target's pass fails a preparation only while the attempt is still
        `planned`, never one a concurrent pass prepared or switched."""
