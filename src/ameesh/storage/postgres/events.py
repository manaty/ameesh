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

    def readable(self, position, limit) -> tuple:
        # ONE statement, so ONE snapshot: the rows and `waiting` agree (with
        # two snapshots, a transaction ending in between hid both)
        tx, last_id = position
        pos = (str(int(tx)), int(last_id))
        rows = self.db.query(
            "with s as materialized (select pg_snapshot_xmin(pg_current_snapshot()) as x),"
            " r as (select " + _COLUMNS + ", tx::text as tx from ameesh_events, s"
            "   where (tx, id) > (%s::text::xid8, %s) and tx < s.x order by tx, id limit %s)"
            " select w.waiting, r.* from (select exists (select 1 from ameesh_events, s"
            "   where (tx, id) > (%s::text::xid8, %s) and tx >= s.x) as waiting) w"
            " left join r on true order by r.tx::xid8, r.id", pos + (int(limit),) + pos)
        waiting = bool(rows[0]["waiting"]) if rows else False
        return ([{k: v for k, v in row.items() if k != "waiting"}
                 for row in rows if row["id"] is not None], waiting)

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

    def prune(self, before_ts, limit, timeout_s=None) -> int:
        sql = ("delete from ameesh_events where id in ("
               " select id from ameesh_events where created_at < to_timestamp(%s)"
               " order by id limit %s for update skip locked) returning id")
        params = (float(before_ts), int(limit))
        if timeout_s is None:
            return len(self.db.query(sql, params))
        ms = "%dms" % max(1, int(timeout_s * 1000))
        with self.db.transaction() as tx:
            # local to this transaction: a lock or a slow delete never
            # outlasts the caller's budget (the statement fails instead)
            tx.query("select set_config('lock_timeout', %s, true),"
                     " set_config('statement_timeout', %s, true)", (ms, ms))
            return len(tx.query(sql, params))
