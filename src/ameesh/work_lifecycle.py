# SPDX-License-Identifier: AGPL-3.0-only
"""Shared lifecycle vocabulary during the generic-core transition (#114).

Values remain the persisted v1 spellings. L99 T1 (#4349) adds the generic
names as aliases: `canonical` returns the persisted spelling and
`lifecycle_name` the generic one. TERMINAL means no further normal
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

#: L99 T1 (#4349) : le vocabulaire générique du cycle de vie, par orthographe
#: persistée v1. La valeur stockée reste la v1 jusqu'à la bascule du stockage
#: (T5) : `canonical` ramène les deux orthographes sur celle-ci.
GENERIC_NAMES = {
    REQUESTED: "requested",
    IN_PROGRESS: "in_progress",
    TO_VALIDATE: "to_validate",
    DELIVERY_RECORDED: "validated",
    PROMOTED: "delivered",
    BLOCKED: "blocked",
    WAITING_HUMAN: "waiting_human",
    CLOSED: "closed",
}

#: Toute orthographe acceptée → l'orthographe persistée v1. Les noms v1 sont
#: leur propre alias : les deux vocabulaires se lisent partout où un état est
#: fourni (work move, work list --state, filtre d'API).
ALIASES = {state: state for state in STATES}
ALIASES.update({generic: v1 for v1, generic in GENERIC_NAMES.items()})

#: Les orthographes qu'acceptent les choix d'argparse et les filtres.
ACCEPTED_STATES = tuple(ALIASES)


def canonical(state: str) -> str:
    """L'orthographe persistée v1 d'un état nommé dans l'un ou l'autre
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


def delivery_connector() -> str:
    """Compatibility default; module selection will replace this in L98/L100."""
    return "git-merge"
