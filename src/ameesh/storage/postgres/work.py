# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : lots (`work_items`) et leur journal (`work_item_events`).

SQL déplacé tel quel depuis `ameesh.work` (lot L1). Chaque transition
laisse une ligne dans `work_item_events` (qui, quand, pourquoi).
"""
from __future__ import annotations

from .. import interface

ITEM_COLUMNS = """
    id, type, source, app, title, body, issue_ref, workstream, state, assignee,
    loops, budget_usd, spent_usd,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from updated_at)::float8 as updated_ts,
    extract(epoch from closed_at)::float8  as closed_ts
"""


class WorkItems(interface.WorkItems):

    def add(self, *, type, source, app, title, body, issue_ref, workstream,  # noqa: A002
            assignee, budget_usd, note, actor) -> dict:
        """Crée le lot en `intake`, puis sa première ligne de journal."""
        rows = self.db.query(
            """
            INSERT INTO work_items
                (type, source, app, title, body, issue_ref, workstream, assignee,
                 budget_usd, state)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'intake')
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", ITEM_COLUMNS),
            (type, source, app, title, body, issue_ref, workstream, assignee,
             budget_usd),
        )
        item = rows[0]
        self._event(int(item["id"]), "intake", note, actor)
        return item

    def get(self, item_id) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM work_items WHERE id = %%s" % ITEM_COLUMNS, (int(item_id),))
        return rows[0] if rows else None

    def items(self, *, state, assignee, limit) -> list[dict]:
        sql = "SELECT %s FROM work_items WHERE true" % ITEM_COLUMNS
        params: list = []
        if state:
            sql += " AND state = %s"
            params.append(state)
        if assignee:
            sql += " AND assignee = %s"
            params.append(assignee)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def move(self, item_id, state, *, current, loops, note, actor) -> dict | None:
        """Déplace le lot s'il est encore en `current` (sinon None, rien
        d'écrit), puis journalise la transition."""
        rows = self.db.query(
            """
            UPDATE work_items
               SET state = %s,
                   loops = loops + %s,
                   updated_at = now(),
                   closed_at = CASE WHEN %s = 'promoted' THEN now() ELSE NULL END
             WHERE id = %s AND state = %s
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", ITEM_COLUMNS),
            (state, loops, state, int(item_id), current),
        )
        if not rows:
            return None
        self._event(int(item_id), state, note, actor)
        return rows[0]

    def note(self, item_id, state, text, actor) -> None:
        self._event(int(item_id), state, text, actor)
        self.db.execute("UPDATE work_items SET updated_at = now() WHERE id = %s",
                        (int(item_id),))

    def events(self, item_id, limit) -> list[dict]:
        return self.db.query(
            """
            SELECT id, work_item_id, state, note, actor,
                   extract(epoch from created_at)::float8 as created_ts
              FROM work_item_events
             WHERE work_item_id = %s
             ORDER BY id DESC LIMIT %s
            """,
            (int(item_id), int(limit)),
        )

    def _event(self, item_id, state, note, actor) -> None:
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor) "
            "VALUES (%s, %s, %s, %s)",
            (item_id, state, note or "", actor or ""),
        )
