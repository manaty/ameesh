# SPDX-License-Identifier: AGPL-3.0-only
"""Development workflow stages and compatibility transitions (#4353).

Stage names stay distinct when their persisted core becomes ``delivered``.
The core obtains this declaration through the module registry.
"""
from __future__ import annotations

from ... import work_lifecycle as lifecycle

MERGED = "merged"
PROMOTED = "promoted"
STAGES = {MERGED: "delivered", PROMOTED: "delivered"}

FORWARD = {
    lifecycle.REQUESTED: (lifecycle.IN_PROGRESS,),
    lifecycle.IN_PROGRESS: (lifecycle.QA,),
    lifecycle.QA: (lifecycle.VALIDATED, MERGED, lifecycle.IN_PROGRESS),
    lifecycle.VALIDATED: (MERGED, lifecycle.IN_PROGRESS),
    MERGED: (PROMOTED,),
    PROMOTED: (),
    lifecycle.CLOSED: (),
}

#: transitions autorisées, attentes comprises
TRANSITIONS = {
    lifecycle.REQUESTED: {lifecycle.IN_PROGRESS, lifecycle.BLOCKED, lifecycle.WAITING_HUMAN},
    lifecycle.IN_PROGRESS: {lifecycle.QA, lifecycle.BLOCKED, lifecycle.WAITING_HUMAN},
    lifecycle.QA: {
        lifecycle.VALIDATED,
        MERGED,
        lifecycle.IN_PROGRESS,
        lifecycle.BLOCKED,
        lifecycle.WAITING_HUMAN,
    },
    lifecycle.VALIDATED: {
        lifecycle.QA,
        MERGED,
        lifecycle.IN_PROGRESS,
        lifecycle.BLOCKED,
        lifecycle.WAITING_HUMAN,
    },
    MERGED: {PROMOTED, lifecycle.BLOCKED, lifecycle.WAITING_HUMAN},
    PROMOTED: set(),
    lifecycle.CLOSED: set(),
    lifecycle.BLOCKED: {
        lifecycle.VALIDATED,
        lifecycle.REQUESTED,
        lifecycle.IN_PROGRESS,
        lifecycle.QA,
        MERGED,
        lifecycle.WAITING_HUMAN,
    },
    lifecycle.WAITING_HUMAN: {
        lifecycle.VALIDATED,
        lifecycle.REQUESTED,
        lifecycle.IN_PROGRESS,
        lifecycle.QA,
        MERGED,
        lifecycle.BLOCKED,
    },
}
#: corrections d'un état posé par erreur (`move(..., correct=…)`) : (depuis,
#: vers). Un `promoted` à tort revient en `merged` ; un `merged` à tort (une
#: PR d'étape fermée trop tôt, #759) revient en `build` ; ensuite, les
#: transitions habituelles (merged → blocked → …) s'appliquent.
CORRECTIONS = {
    (PROMOTED, MERGED),
    (MERGED, lifecycle.IN_PROGRESS),
}


def promote(db, item_id, *, note="", actor=""):
    """Record the development promotion through the normal guarded writer."""
    from ... import work
    return work.move(db, item_id, PROMOTED, note=note, actor=actor)
