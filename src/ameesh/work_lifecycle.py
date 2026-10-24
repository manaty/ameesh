# SPDX-License-Identifier: AGPL-3.0-only
"""Shared lifecycle vocabulary during the generic-core transition (#114).

Values remain the persisted v1 spellings. TERMINAL means no further normal
transition is allowed; COMPLETED means the task no longer needs production
work. A recorded delivery can still be promoted, so those groups differ.
"""
from __future__ import annotations

REQUESTED = "intake"
IN_PROGRESS = "build"
TO_VALIDATE = "qa"
DELIVERY_RECORDED = "merged"
PROMOTED = "promoted"
BLOCKED = "blocked"
WAITING_HUMAN = "waiting_human"
CLOSED = "closed"

STATES = (REQUESTED, IN_PROGRESS, TO_VALIDATE, DELIVERY_RECORDED, PROMOTED,
          BLOCKED, WAITING_HUMAN, CLOSED)
ACTIVE = (REQUESTED, IN_PROGRESS)
DELIVERED = (DELIVERY_RECORDED, PROMOTED)
COMPLETED = (*DELIVERED, CLOSED)
TERMINAL = (PROMOTED, CLOSED)
WAITING = {BLOCKED, WAITING_HUMAN}
WAITING_ON_OTHERS = (TO_VALIDATE, WAITING_HUMAN, BLOCKED)


def delivery_connector() -> str:
    """Compatibility default; module selection will replace this in L98/L100."""
    return "git-merge"
