# SPDX-License-Identifier: AGPL-3.0-only
"""work_items — la machine à états des lots, reprise d'un orchestrateur de
tickets existant (spec §5 point 4).

    intake → build → qa → merged → promoted

plus `blocked` et `waiting_human`. La boucle `qa → build` est bornée à deux
allers-retours (comme dans le design d'origine) : au-delà, il faut bloquer le
lot ou attendre une décision humaine, sinon la boucle coûte des tokens sans fin.

Chaque transition laisse une ligne dans `work_item_events` (qui, quand,
pourquoi) : c'est le ledger de reprise après un redémarrage.
"""
from __future__ import annotations

from .db import Db

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

ITEM_COLUMNS = """
    id, type, source, app, title, body, issue_ref, workstream, state, assignee,
    loops, budget_usd, spent_usd,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from updated_at)::float8 as updated_ts,
    extract(epoch from closed_at)::float8  as closed_ts
"""


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
    rows = db.query(
        """
        INSERT INTO work_items
            (type, source, app, title, body, issue_ref, workstream, assignee,
             budget_usd, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'intake')
        RETURNING __COLUMNS__
        """.replace("__COLUMNS__", ITEM_COLUMNS),
        (type, source, app, title.strip(), body, issue_ref, workstream, assignee,
         budget_usd),
    )
    item = rows[0]
    _event(db, int(item["id"]), "intake", "création", actor)
    return item


def get(db: Db, item_id: int) -> dict | None:
    rows = db.query(
        "SELECT %s FROM work_items WHERE id = %%s" % ITEM_COLUMNS, (int(item_id),))
    return rows[0] if rows else None


def list_items(db: Db, *, state: str | None = None, assignee: str | None = None,
               limit: int = 50) -> list[dict]:
    sql = "SELECT %s FROM work_items WHERE true" % ITEM_COLUMNS
    params: list = []
    if state:
        if state not in STATES:
            raise WorkError("état inconnu : %r (%s)" % (state, ", ".join(STATES)))
        sql += " AND state = %s"
        params.append(state)
    if assignee:
        sql += " AND assignee = %s"
        params.append(assignee)
    sql += " ORDER BY updated_at DESC LIMIT %s"
    params.append(int(limit))
    return db.query(sql, tuple(params))


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

    rows = db.query(
        """
        UPDATE work_items
           SET state = %s,
               loops = loops + %s,
               updated_at = now(),
               closed_at = CASE WHEN %s = 'promoted' THEN now() ELSE NULL END
         WHERE id = %s AND state = %s
        RETURNING __COLUMNS__
        """.replace("__COLUMNS__", ITEM_COLUMNS),
        (state, 1 if returning_loop else 0, state, int(item_id), current),
    )
    if not rows:
        raise WorkError("lot %s déplacé entre-temps : réessayez" % item_id)
    _event(db, int(item_id), state, note or ("%s → %s" % (current, state)), actor)
    return rows[0]


def note(db: Db, item_id: int, text: str, *, actor: str = "") -> bool:
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s introuvable" % item_id)
    if not (text or "").strip():
        raise WorkError("note vide")
    _event(db, int(item_id), item["state"], text.strip(), actor)
    db.execute("UPDATE work_items SET updated_at = now() WHERE id = %s", (int(item_id),))
    return True


def events(db: Db, item_id: int, limit: int = 100) -> list[dict]:
    return db.query(
        """
        SELECT id, work_item_id, state, note, actor,
               extract(epoch from created_at)::float8 as created_ts
          FROM work_item_events
         WHERE work_item_id = %s
         ORDER BY id DESC LIMIT %s
        """,
        (int(item_id), int(limit)),
    )


def _event(db: Db, item_id: int, state: str, note: str, actor: str) -> None:
    db.execute(
        "INSERT INTO work_item_events (work_item_id, state, note, actor) VALUES (%s, %s, %s, %s)",
        (item_id, state, note or "", actor or ""),
    )
