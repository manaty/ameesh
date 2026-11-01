# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : feuille de route (lot L96, migration 0044).

Dates prévues des tâches (`work_items.planned_*`) et des fiches du plan
(`work_packages.planned_*`, à côté des dates du canon `start_on`…), et
engagements datés (`commitments`). Les dates sont rendues en texte ISO
(`to_char`) : les deux pilotes (psql, psycopg) rendent la même chose.
"""
from __future__ import annotations

import json

from .. import interface

_DATE_KEYS = {"start": "planned_start", "end": "planned_end", "delivery": "planned_delivery"}


def _day(column: str, alias: str | None = None) -> str:
    return "to_char(%s, 'YYYY-MM-DD') AS %s" % (column, alias or column.split(".")[-1])


_PLAN_COLUMNS = ", ".join([
    "id", _day("planned_start"), _day("planned_end"), _day("planned_delivery"),
    "planned_source", "planned_by",
    "extract(epoch from planned_at)::float8 AS planned_ts",
])

_ITEM_COLUMNS = ", ".join([
    "w.id", "w.title", "w.state", "w.app", "w.workstream", "w.assignee", "w.package_id",
    "w.body", "w.external_executor", "w.close_reason",
    "extract(epoch from w.created_at)::float8 AS created_ts",
    "extract(epoch from w.updated_at)::float8 AS updated_ts",
    "extract(epoch from w.closed_at)::float8 AS closed_ts",
    _day("w.planned_start"), _day("w.planned_end"), _day("w.planned_delivery"),
    "w.planned_source", "w.planned_by",
    "extract(epoch from w.planned_at)::float8 AS planned_ts",
    # L157 : durée estimée et début mesuré
    "w.estimate_minutes", "w.estimate_source", "w.estimate_by",
    "extract(epoch from w.estimate_at)::float8 AS estimate_ts",
    "extract(epoch from w.started_at)::float8 AS started_ts",
    # #2204/N5 : la frise porte les relecteurs comme progress/list (lot_state
    # ne doit pas diverger entre les deux lectures).
    "coalesce(to_jsonb(w)->>'reviewer', '') AS reviewer",
    "to_jsonb(w)->'reviewers' AS reviewers",
])

_COMMITMENT_COLUMNS = ", ".join([
    "id", "what", _day("due_on"), "kind", "status", "owner", "project", "work_item_id",
    "package_id", "source_kind", "source_ref", "depends_on", "note", "created_by",
    "proposal_key",
    "extract(epoch from created_at)::float8 AS created_ts",
    "extract(epoch from updated_at)::float8 AS updated_ts",
    "extract(epoch from closed_at)::float8 AS closed_ts",
])

#: tâches de la frise : ouvertes, datées, engagées, ou modifiées récemment
_ITEMS_SQL = """
SELECT __COLS__, count(*) OVER ()::bigint AS total
  FROM work_items w
 WHERE (w.state NOT IN (__DONE__)
    OR w.planned_start IS NOT NULL OR w.planned_end IS NOT NULL
    OR w.planned_delivery IS NOT NULL
    OR w.updated_at >= to_timestamp(%s)
    OR EXISTS (SELECT 1 FROM commitments c
                WHERE c.work_item_id = w.id AND c.status IN ('proposed', 'open')))
 __PROJECT__
 ORDER BY (w.state NOT IN (__DONE__)) DESC, w.created_at, w.id
 LIMIT %s
"""

#: colonnes écrites par `add_commitment` / `record_proposal`
_WRITABLE = ("what", "due_on", "kind", "status", "owner", "project", "work_item_id",
             "package_id", "source_kind", "source_ref", "depends_on", "note", "created_by",
             "proposal_key")
#: colonnes que `set_commitment` peut changer
_SETTABLE = ("what", "kind", "status", "due_on", "note", "owner", "project",
             "work_item_id", "package_id", "source_kind", "source_ref", "depends_on")


def _row(row: dict) -> dict:
    deps = row.get("depends_on")
    if isinstance(deps, str):
        try:
            deps = json.loads(deps)
        except ValueError:
            deps = []
    row["depends_on"] = list(deps or [])
    if row.get("work_item_id") is not None:
        row["work_item_id"] = int(row["work_item_id"])
    row["id"] = int(row["id"])
    return row


def _iso_day(value) -> str | None:
    return str(value)[:10] if value else None


def _set_clause(dates: dict) -> tuple[list[str], list]:
    sets, params = [], []
    for key, column in _DATE_KEYS.items():
        if key in dates:
            sets.append("%s = %%s::date" % column)
            params.append(dates[key])
    return sets, params


class Roadmap(interface.Roadmap):

    def plan_item(self, item_id, dates, *, source, actor, expect=None) -> dict | None:
        sets, params = _set_clause(dates)
        sets += ["planned_source = coalesce(%s, planned_source)", "planned_by = %s",
                 "planned_at = now()"]
        params += [source, actor or ""]
        where, keys = "id = %s", [int(item_id)]
        if expect is not None:          # #740: unchanged since it was read
            where += (" AND planned_start IS NOT DISTINCT FROM %s::date"
                      " AND planned_end IS NOT DISTINCT FROM %s::date"
                      " AND planned_source IS NOT DISTINCT FROM %s")
            keys += [_iso_day(expect.get("planned_start")),
                     _iso_day(expect.get("planned_end")), expect.get("planned_source")]
        rows = self.db.query(
            "UPDATE work_items SET %s WHERE %s RETURNING %s" % (
                ", ".join(sets), where, _PLAN_COLUMNS),
            tuple(params) + tuple(keys))
        if not rows:
            return None
        rows[0]["id"] = int(rows[0]["id"])
        return rows[0]

    def item_plans(self, ids) -> list[dict]:
        ids = [int(i) for i in ids]
        if not ids:
            return []
        rows = self.db.query(
            "SELECT %s FROM work_items WHERE id IN (%s) ORDER BY id" % (
                _PLAN_COLUMNS, ", ".join(["%s"] * len(ids))), tuple(ids))
        for row in rows:
            row["id"] = int(row["id"])
        return rows

    def plan_package(self, ident, dates, *, source, actor) -> dict | None:
        sets, params = _set_clause(dates)
        sets += ["planned_source = coalesce(%s, planned_source)", "planned_by = %s",
                 "planned_at = now()"]
        params += [source, actor or ""]
        rows = self.db.query(
            "UPDATE work_packages SET %s WHERE id = %%s RETURNING %s" % (
                ", ".join(sets), _PLAN_COLUMNS),
            tuple(params) + (ident,))
        return rows[0] if rows else None

    def items(self, *, since_ts, limit, done_states, project=None) -> list[dict]:
        done = list(done_states) or [""]
        sql = _ITEMS_SQL.replace("__COLS__", _ITEM_COLUMNS).replace(
            "__DONE__", ", ".join(["%s"] * len(done))).replace(
                "__PROJECT__", "AND (w.app = %s OR w.workstream = %s)" if project else "")
        params = tuple(done) + (float(since_ts),)
        if project:
            params += (project, project)
        return self.db.query(sql, params + tuple(done) + (int(limit),))

    def commitments(self, *, statuses=None, ids=None) -> list[dict]:
        where, params = [], []
        if statuses is not None:
            statuses = list(statuses)
            if not statuses:
                return []
            where.append("status IN (%s)" % ", ".join(["%s"] * len(statuses)))
            params += statuses
        if ids is not None:
            ids = [int(i) for i in ids]
            if not ids:
                return []
            where.append("id IN (%s)" % ", ".join(["%s"] * len(ids)))
            params += ids
        sql = "SELECT %s FROM commitments" % _COMMITMENT_COLUMNS
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY due_on NULLS LAST, id"
        return [_row(r) for r in self.db.query(sql, tuple(params))]

    def _insert(self, row: dict, *, on_conflict: str = "") -> list[dict]:
        cols = [c for c in _WRITABLE if c in row]
        values = []
        params = []
        for c in cols:
            if c == "due_on":
                values.append("%s::date")
                params.append(row[c])
            elif c == "depends_on":
                values.append("%s::jsonb")
                params.append(json.dumps(list(row[c] or [])))
            else:
                values.append("%s")
                params.append(row[c])
        return self.db.query(
            "INSERT INTO commitments (%s) VALUES (%s) %s RETURNING %s" % (
                ", ".join(cols), ", ".join(values), on_conflict, _COMMITMENT_COLUMNS),
            tuple(params))

    def add_commitment(self, row) -> dict:
        return _row(self._insert(row)[0])

    def record_proposal(self, row) -> dict | None:
        row = dict(row, status="proposed")
        rows = self._insert(row, on_conflict="ON CONFLICT (proposal_key) DO NOTHING")
        return _row(rows[0]) if rows else None

    def set_commitment(self, ident, *, current, values, expect=None) -> dict | None:
        sets, params = [], []
        for key in _SETTABLE:
            if key not in values:
                continue
            if key == "due_on":
                sets.append("due_on = %s::date")
                params.append(values[key])
            elif key == "depends_on":
                sets.append("depends_on = %s::jsonb")
                params.append(json.dumps(list(values[key] or [])))
            else:
                sets.append("%s = %%s" % key)
                params.append(values[key])
        sets.append("updated_at = now()")
        if values.get("status") in ("done", "cancelled"):
            sets.append("closed_at = now()")
        current = list(current)
        checks, expected = [], []
        if expect is not None:
            for key in _SETTABLE:
                if key not in expect:
                    continue
                cast = "::date" if key == "due_on" else "::jsonb" if key == "depends_on" else ""
                checks.append(" AND %s IS NOT DISTINCT FROM %%s%s" % (key, cast))
                expected.append(json.dumps(expect[key]) if key == "depends_on" else expect[key])
        rows = self.db.query(
            "UPDATE commitments SET %s WHERE id = %%s AND status IN (%s)%s RETURNING %s" % (
                ", ".join(sets), ", ".join(["%s"] * len(current)), "".join(checks), _COMMITMENT_COLUMNS),
            tuple(params) + (int(ident),) + tuple(current) + tuple(expected))
        return _row(rows[0]) if rows else None
