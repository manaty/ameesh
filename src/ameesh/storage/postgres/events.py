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
            "with s as materialized (select pg_snapshot_xmin(pg_current_snapshot()) as x,"
            "   pg_snapshot_xmax(pg_current_snapshot()) as m),"
            " r as (select " + _COLUMNS + ", tx::text as tx from ameesh_events, s"
            "   where (tx, id) > (%s::text::xid8, %s) and tx < s.x order by tx, id limit %s)"
            " select w.waiting, r.* from (select case"
            "   when not exists (select 1 from ameesh_events, s"
            "     where (tx, id) > (%s::text::xid8, %s) and tx >= s.x) then null"
            # a `tx` this cluster has not assigned yet: rows of another one
            # (logical restore), not an open transaction
            "   when exists (select 1 from ameesh_events, s where tx >= s.m) then 'restore'"
            "   else 'xmin' end as waiting) w"
            " left join r on true order by r.tx::xid8, r.id", pos + (int(limit),) + pos)
        waiting = rows[0]["waiting"] if rows else None
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
            "   order by tx, id limit 1) as oldest,"
            " (select tx::text || ':' || id from ameesh_events_pruned) as pruned,"
            " ameesh_events_cluster() as cluster")[0]

        def position(text):
            return (0, 0) if not text else tuple(int(part) for part in text.split(":"))
        return {"first": None if row["first"] is None else int(row["first"]),
                "last": int(row["last"]), "head": position(row["head"]),
                "top": position(row["top"]), "oldest": position(row["oldest"]),
                "pruned": position(row["pruned"]), "cluster": row["cluster"]}

    def count_before(self, before_ts) -> int:
        return int(self.db.query("select count(*) as n from ameesh_events"
                                 " where created_at < to_timestamp(%s)",
                                 (float(before_ts),))[0]["n"])

    def prune(self, before_ts, limit, timeout_s=None) -> int:
        # by `created_at` (an event kept never stops it); the greatest
        # position deleted raises the watermark in the SAME transaction; no
        # SKIP LOCKED: the lock waits, bounded by `timeout_s`
        with self.db.transaction() as tx:
            if timeout_s is not None:
                # local to this transaction: a lock or a slow delete never
                # outlasts the caller's budget (the statement fails instead)
                ms = "%dms" % max(1, int(timeout_s * 1000))
                tx.query("select set_config('lock_timeout', %s, true),"
                         " set_config('statement_timeout', %s, true)", (ms, ms))
            gone = tx.query(
                "delete from ameesh_events where id in ("
                " select id from ameesh_events where created_at < to_timestamp(%s)"
                " order by created_at, id limit %s) returning tx::text as tx, id",
                (float(before_ts), int(limit)))
            if gone:
                top = max((int(row["tx"]), int(row["id"])) for row in gone)
                tx.execute(
                    "insert into ameesh_events_pruned (one, tx, id)"
                    " values (true, %s::text::xid8, %s)"
                    " on conflict (one) do update set tx = excluded.tx, id = excluded.id,"
                    " at = now() where (ameesh_events_pruned.tx, ameesh_events_pruned.id)"
                    " < (excluded.tx, excluded.id)", (str(top[0]), top[1]))
            return len(gone)
