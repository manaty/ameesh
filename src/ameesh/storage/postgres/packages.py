# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : fiches WorkPackage recopiées du canon (`work_packages`, 0026, L29).

Déclaratif seulement : `canon sync` y écrit les fiches (avec `canon_ref`),
jamais l'état d'exécution, qui reste dans `work_items`.
"""
from __future__ import annotations

import json

from .. import interface

_COLUMNS = """
    id, kind, title, parent, responsible, team, scope, status, canon_ref, canon, present,
    extract(epoch from synced_at)::float8 AS synced_ts,
    to_char(start_on, 'YYYY-MM-DD') AS start_on,
    to_char(end_on, 'YYYY-MM-DD') AS end_on,
    to_char(delivery_on, 'YYYY-MM-DD') AS delivery_on,
    to_char(planned_start, 'YYYY-MM-DD') AS planned_start,
    to_char(planned_end, 'YYYY-MM-DD') AS planned_end,
    to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery,
    planned_source, planned_by
"""


class WorkPackages(interface.WorkPackages):

    def all(self, *, include_absent: bool = False) -> list[dict]:
        sql = "SELECT %s FROM work_packages" % _COLUMNS
        if not include_absent:
            sql += " WHERE present"
        return self.db.query(sql + " ORDER BY id")

    def get(self, ident) -> dict | None:
        rows = self.db.query("SELECT %s FROM work_packages WHERE id = %%s" % _COLUMNS,
                             (ident,))
        return rows[0] if rows else None

    def upsert(self, row) -> None:
        self.db.query(
            """
            INSERT INTO work_packages
                (id, kind, title, parent, responsible, team, scope, status, canon_ref,
                 canon, start_on, end_on, delivery_on, present, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::date, %s::date,
                    %s::date, true, now())
            ON CONFLICT (id) DO UPDATE SET
                kind = excluded.kind, title = excluded.title, parent = excluded.parent,
                responsible = excluded.responsible, team = excluded.team,
                scope = excluded.scope, status = excluded.status,
                canon_ref = excluded.canon_ref, canon = excluded.canon,
                start_on = excluded.start_on, end_on = excluded.end_on,
                delivery_on = excluded.delivery_on,
                present = true, synced_at = now()
            RETURNING id
            """,
            (row["id"], row["kind"], row["title"], row.get("parent"), row.get("responsible"),
             row.get("team"),
             None if row.get("scope") is None else json.dumps(list(row["scope"])),
             row.get("status"), row["canon_ref"], row.get("canon"),
             # L96 : dates déclarées au canon (jours ISO)
             row.get("start_on"), row.get("end_on"), row.get("delivery_on")),
        )

    def retire(self, idents) -> int:
        idents = list(idents)
        if not idents:
            return 0
        rows = self.db.query(
            "UPDATE work_packages SET present = false, synced_at = now()"
            " WHERE present AND id IN (%s) RETURNING id" % ", ".join(["%s"] * len(idents)),
            tuple(idents))
        return len(rows)
