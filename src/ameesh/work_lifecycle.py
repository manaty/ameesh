# SPDX-License-Identifier: AGPL-3.0-only
"""Lifecycle storage vocabulary after L99 T5 (#4353).

Both development delivery stages persist the core ``delivered``. Their
workflow stage and ``legacy_state`` distinguish integration from promotion.
Read groups include the old spellings for mixed-version input and history;
``STATES`` lists only current stored cores. Terminal development transitions
are owned by the dev module, not inferred from the core alone.
"""
from __future__ import annotations

REQUESTED = "intake"
IN_PROGRESS = "build"
QA = "qa"
VALIDATED = "validated"
TO_VALIDATE = (QA, VALIDATED)
DELIVERY_RECORDED = "delivered"
PROMOTED = "delivered"
BLOCKED = "blocked"
WAITING_HUMAN = "waiting_human"
CLOSED = "closed"

STATES = (REQUESTED, IN_PROGRESS, *TO_VALIDATE, DELIVERY_RECORDED,
          BLOCKED, WAITING_HUMAN, CLOSED)
ACTIVE = (REQUESTED, IN_PROGRESS)
DELIVERED = (DELIVERY_RECORDED, "merged", "promoted")
COMPLETED = (*DELIVERED, CLOSED)
TERMINAL = (PROMOTED, "promoted", CLOSED)
WAITING = {BLOCKED, WAITING_HUMAN}
WAITING_ON_OTHERS = (*TO_VALIDATE, WAITING_HUMAN, BLOCKED)

#: L99 T1 (#4349) : le vocabulaire générique du cycle de vie, par orthographe
#: persistée. T5 distingue validated du core delivered.
GENERIC_NAMES = {
    REQUESTED: "requested",
    IN_PROGRESS: "in_progress",
    QA: "to_validate",
    VALIDATED: "validated",
    DELIVERY_RECORDED: "delivered",
    BLOCKED: "blocked",
    WAITING_HUMAN: "waiting_human",
    CLOSED: "closed",
}

#: Toute orthographe acceptée → l'orthographe persistée. Les noms v1 sont
#: des alias : les deux vocabulaires se lisent partout où un état est
#: fourni (work move, work list --state, filtre d'API).
ALIASES = {state: state for state in STATES}
ALIASES.update({generic: v1 for v1, generic in GENERIC_NAMES.items()})
ALIASES.update({"merged": DELIVERY_RECORDED, "promoted": PROMOTED})

#: Les orthographes qu'acceptent les choix d'argparse et les filtres.
ACCEPTED_STATES = tuple(ALIASES)


def canonical(state: str) -> str:
    """L'orthographe persistée d'un état nommé dans l'un ou l'autre
    vocabulaire (L99 T1), sinon ValueError — la couche commande en fait son
    propre refus (`work.WorkError`) avec les orthographes acceptées."""
    try:
        return ALIASES[state]
    except KeyError:
        raise ValueError("unknown state: %r" % (state,)) from None


def lifecycle_name(state: str) -> str:
    """Le nom générique du cycle de vie d'un état, quelle que soit son
    orthographe (L99 T1)."""
    return GENERIC_NAMES[canonical(state)]


def phase(item) -> str:
    """Compatibility workflow phase; persisted ``state`` remains generic.

    Historical events and pre-switch rows still carry their original state.
    ``legacy_state`` preserves custom pinned Process stages without inventing
    a workflow reference or changing signed artifacts.
    """
    state = str(item.get("state") or "")
    if state == DELIVERY_RECORDED:
        return str(item.get("legacy_state") or item.get("workflow_stage") or "merged")
    return state


def phase_name(state: str) -> str:
    """Accept a development stage or a generic core command name."""
    if state in ("merged", "promoted"):
        return state
    # The pre-switch generic delivery command meant the promotion stage.
    return "promoted" if state == "delivered" else canonical(state)
