# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres driver: the mesh's event log (`ameesh_events`, L85, migration 0396)."""
from __future__ import annotations

import json

from .. import interface

_COLUMNS = ("id, type, object, project, data,"
            " extract(epoch from created_at)::float8 as ts")


class Events(interface.Events):

    def record(self, type, object, project, data) -> int:
        return int(self.db.query(
            "insert into ameesh_events (type, object, project, data)"
            " values (%s, %s, coalesce(nullif(%s, ''), ameesh_agent_project(%s)), %s::jsonb)"
            " returning id",
            (type, object, project or "", (data or {}).get("agent"),
             json.dumps(data or {}, sort_keys=True, default=str)))[0]["id"])

    def after(self, after_id, limit) -> list:
        return self.db.query(
            "select " + _COLUMNS + " from ameesh_events where id > %s order by id limit %s",
            (int(after_id), int(limit)))

    def readable(self, position, limit) -> list:
        tx, last_id = position
        return self.db.query(
            "select " + _COLUMNS + ", tx::text as tx from ameesh_events"
            " where (tx, id) > (%s::text::xid8, %s)"
            " and tx < pg_snapshot_xmin(pg_current_snapshot())"
            " order by tx, id limit %s", (str(int(tx)), int(last_id), int(limit)))

    def waiting(self, position) -> bool:
        tx, last_id = position
        return bool(self.db.query(
            "select exists (select 1 from ameesh_events where (tx, id) > (%s::text::xid8, %s)"
            " and tx >= pg_snapshot_xmin(pg_current_snapshot())) as w",
            (str(int(tx)), int(last_id)))[0]["w"])

    def bounds(self) -> dict:
        row = self.db.query(
            "select (select min(id) from ameesh_events) as first,"
            " (select coalesce(max(id), 0) from ameesh_events) as last,"
            " (select tx::text || ':' || id from ameesh_events"
            "   where tx < pg_snapshot_xmin(pg_current_snapshot())"
            "   order by tx desc, id desc limit 1) as head,"
            " (select tx::text || ':' || id from ameesh_events"
            "   order by tx desc, id desc limit 1) as top,"
            " (select tx::text || ':' || id from ameesh_events"
            "   order by tx, id limit 1) as oldest")[0]

        def position(text):
            return (0, 0) if not text else tuple(int(part) for part in text.split(":"))
        return {"first": None if row["first"] is None else int(row["first"]),
                "last": int(row["last"]), "head": position(row["head"]),
                "top": position(row["top"]), "oldest": position(row["oldest"])}

    def count_before(self, before_ts) -> int:
        return int(self.db.query("select count(*) as n from ameesh_events"
                                 " where created_at < to_timestamp(%s)",
                                 (float(before_ts),))[0]["n"])

    def prune(self, before_ts, limit) -> int:
        return len(self.db.query(
            "delete from ameesh_events where id in ("
            " select id from ameesh_events where created_at < to_timestamp(%s)"
            " order by id limit %s) returning id", (float(before_ts), int(limit))))
