# SPDX-License-Identifier: AGPL-3.0-only
"""work_items — la machine à états des lots, reprise d'un orchestrateur de
tickets existant (spec §5 point 4).

    intake → build → qa → merged → promoted

plus `blocked` et `waiting_human`. La boucle `qa → build` est bornée à deux
allers-retours (comme dans le design d'origine) : au-delà, il faut bloquer le
lot ou attendre une décision humaine, sinon la boucle coûte des tokens sans fin.

Chaque transition laisse une ligne dans `work_item_events` (qui, quand,
pourquoi) : c'est le ledger de reprise après un redémarrage.

Le SQL est dans le stockage (`storage.of(db).work`, spec §10).
"""
from __future__ import annotations

from . import storage
from .db import Db
from .storage.postgres import work as _pg

STATES = ("intake", "build", "qa", "merged", "promoted", "blocked", "waiting_human")
TERMINAL = ("promoted",)
MAX_QA_LOOPS = 2
FORWARD = {
    "intake": ("build",),
    "build": ("qa",),
    "qa": ("merged", "build"),
    "merged": ("promoted",),
    "promoted": (),
}

#: transitions autorisées, attentes comprises
TRANSITIONS = {
    "intake": {"build", "blocked", "waiting_human"},
    "build": {"qa", "blocked", "waiting_human"},
    "qa": {"merged", "build", "blocked", "waiting_human"},
    "merged": {"promoted", "blocked", "waiting_human"},
    "promoted": set(),
    "blocked": {"intake", "build", "qa", "merged", "waiting_human"},
    "waiting_human": {"intake", "build", "qa", "merged", "blocked"},
}
WAITING = {"blocked", "waiting_human"}

#: colonnes rendues pour un lot (pilote Postgres ; alias de compatibilité)
ITEM_COLUMNS = _pg.ITEM_COLUMNS


class WorkError(RuntimeError):
    """Transition ou donnée refusée — message toujours actionnable."""


def add(
    db: Db,
    *,
    title: str,
    type: str = "evolution",  # noqa: A002 - nom de colonne de l'orchestrateur d'origine
    source: str = "",
    app: str = "",
    body: str = "",
    issue_ref: str | None = None,
    workstream: str | None = None,
    assignee: str | None = None,
    budget_usd: float | None = None,
    actor: str = "",
) -> dict:
    if type not in ("bug", "evolution"):
        raise WorkError("type inconnu : %r (bug ou evolution)" % type)
    if not (title or "").strip():
        raise WorkError("titre obligatoire")
    return storage.of(db).work.add(
        type=type, source=source, app=app, title=title.strip(), body=body,
        issue_ref=issue_ref, workstream=workstream, assignee=assignee,
        budget_usd=budget_usd, note="création", actor=actor)


def get(db: Db, item_id: int) -> dict | None:
    return storage.of(db).work.get(item_id)


def list_items(db: Db, *, state: str | None = None, assignee: str | None = None,
               limit: int = 50) -> list[dict]:
    if state and state not in STATES:
        raise WorkError("état inconnu : %r (%s)" % (state, ", ".join(STATES)))
    return storage.of(db).work.items(state=state, assignee=assignee, limit=limit)


def move(db: Db, item_id: int, state: str, *, note: str = "", actor: str = "") -> dict:
    """Déplace un lot en vérifiant la transition, la boucle QA et le terminal."""
    if state not in STATES:
        raise WorkError("état inconnu : %r (%s)" % (state, ", ".join(STATES)))
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s introuvable" % item_id)
    current = item["state"]
    if current == state:
        raise WorkError("lot %s déjà en %s" % (item_id, state))
    if state not in TRANSITIONS[current]:
        raise WorkError(
            "transition refusée : %s → %s (autorisées depuis %s : %s)"
            % (current, state, current, ", ".join(sorted(TRANSITIONS[current])) or "aucune"))
    returning_loop = current == "qa" and state == "build"
    if returning_loop and int(item["loops"]) >= MAX_QA_LOOPS:
        raise WorkError(
            "boucle qa → build épuisée (%d) : mettre le lot en blocked ou waiting_human"
            % MAX_QA_LOOPS)

    row = storage.of(db).work.move(
        item_id, state, current=current, loops=1 if returning_loop else 0,
        note=note or ("%s → %s" % (current, state)), actor=actor)
    if row is None:
        raise WorkError("lot %s déplacé entre-temps : réessayez" % item_id)
    return row


def note(db: Db, item_id: int, text: str, *, actor: str = "") -> bool:
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s introuvable" % item_id)
    if not (text or "").strip():
        raise WorkError("note vide")
    storage.of(db).work.note(item_id, item["state"], text.strip(), actor)
    return True


def events(db: Db, item_id: int, limit: int = 100) -> list[dict]:
    return storage.of(db).work.events(item_id, limit)
