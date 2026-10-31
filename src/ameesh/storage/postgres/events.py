# SPDX-License-Identifier: AGPL-3.0-only
"""Postgres driver: the mesh's event log (`ameesh_events`, L85, migration 0396)."""
from __future__ import annotations

import json

from .. import interface

_COLUMNS = ("id, type, object, project, data,"
            " extract(epoch from created_at)::float8 as ts")

# #5045: retain the latest pause state, including a resume tombstone.
_DISPOSABLE = ("not (e.type = 'packet.pause' and not exists ("
               "select 1 from ameesh_events newer where newer.type = e.type"
               " and newer.object = e.object and newer.id > e.id))")



def _position(text) -> tuple:
    """A "<tx>:<id>" watermark as a position; (0, 0) when there is none."""
    return (0, 0) if not text else tuple(int(part) for part in text.split(":"))


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

    def recent_type(self, type, days, limit, object=None) -> list:
        # #3981 : la preuve de staging vit dans le journal partagé. La borne
        # `created_at` garde la lecture sur l'index de rétention ;
        # la preuve de staging n'a de sens que récente. La lecture d'un état
        # durable exact utilise l'index type/objet/id (#5045, migration 0528).
        if days is None and object is None:
            raise ValueError("an unbounded event read requires an exact object")
        sql = "select " + _COLUMNS + " from ameesh_events where type = %s"
        params = [type]
        if days is not None:
            sql += " and created_at > now() - make_interval(days => %s::int)"
            params.append(int(days))
        if object is not None:
            sql += " and object = %s"
            params.append(object)
        sql += " order by id desc limit %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def latest_type(self, type, days, limit) -> list:
        # Explicit predicate also works with generic prepared plans: a bound
        # `type` alone cannot imply the partial staging-progress index.
        staging = (" and type in ('staging.sequence', 'staging.request')"
                   if type in ("staging.sequence", "staging.request") else "")
        return self.db.query(
            "select * from (select distinct on (object) " + _COLUMNS
            + " from ameesh_events where type = %s"
            + staging
            + " and created_at > now() - make_interval(days => %s::int)"
              " order by object, id desc) latest order by id desc limit %s",
            (type, int(days), int(limit)))

    def readable(self, position, limit) -> tuple:
        # ONE statement, so ONE snapshot: the rows and the bounds agree (with
        # two snapshots, a transaction ending in between hid both).
        # R2 auditeur P1: qualify the sort, `tx` alone resolves to the text
        # output column (`tx::text as tx`), so a limited read sorted the
        # positions as text and crossed a power of ten backwards.
        # R2 auditeur P2-1: read `limit + 1` rows in (tx, id) order and
        # decide `waiting` from that prefix, instead of probing the log with
        # correlated `exists`: a bound taken from the snapshot is not an
        # index scan key, so the probe rescanned the whole table at every
        # poll and every replay batch. Sorted by `tx`, the deliverable rows
        # (tx < xmin) come first: the first row at or above xmin is the
        # block; at or above xmax it is another cluster (logical restore).
        # #928/F6: the retention's watermark travels in the SAME statement, so
        # a reader that a concurrent prune passed cannot silently skip.
        tx, last_id = position
        pos = (str(int(tx)), int(last_id))
        rows = self.db.query(
            "with s as materialized (select pg_snapshot_xmin(pg_current_snapshot()) as x,"
            "   pg_snapshot_xmax(pg_current_snapshot()) as m)"
            " select (select x from s)::text as xmin, (select m from s)::text as xmax,"
            " (select tx::text || ':' || id from ameesh_events_pruned) as pruned, r.*"
            " from (values (true)) w(one) left join (select " + _COLUMNS
            + ", tx::text as tx from ameesh_events"
            "   where (tx, id) > (%s::text::xid8, %s)"
            "   order by ameesh_events.tx, ameesh_events.id limit %s) r on true"
            " order by r.tx::xid8, r.id",
            pos + (int(limit) + 1,))
        if not rows:
            return [], None, (0, 0)
        xmin, xmax, waiting = int(rows[0]["xmin"]), int(rows[0]["xmax"]), None
        pruned = _position(rows[0]["pruned"])
        kept = []
        for row in rows:
            if row["id"] is None:            # the left join's only row: empty log
                continue
            value = int(row["tx"])
            if value >= xmin:
                waiting = "restore" if value >= xmax else "xmin"
                break
            kept.append(row)
        return ([{k: v for k, v in row.items() if k not in ("xmin", "xmax", "pruned")}
                 for row in kept[:int(limit)]], waiting, pruned)

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
        return {"first": None if row["first"] is None else int(row["first"]),
                "last": int(row["last"]), "head": _position(row["head"]),
                "top": _position(row["top"]), "oldest": _position(row["oldest"]),
                "pruned": _position(row["pruned"]), "cluster": row["cluster"]}

    def count_before(self, days) -> int:
        # `now()` (the database's clock) is the clock of the table's own
        # guard: a host ahead of the base cannot have its deletion refused
        return int(self.db.query(
            "select count(*) as n from ameesh_events e"
            " where created_at < now() - make_interval(days => %s::int) and " + _DISPOSABLE,
            (int(days),))[0]["n"])

    def prune(self, days, limit, timeout_s=None) -> int:
        # by `created_at`, on the database's clock (same clock as the guard);
        # the greatest position deleted raises the watermark in the SAME
        # transaction; no SKIP LOCKED: the lock waits, bounded by `timeout_s`.
        # #1711: the chosen batch is locked (FOR UPDATE) — a second pruner that
        # waited on the first one's rows skips those it deleted and takes the
        # next ones, instead of deleting nothing and reporting a stuck
        # retention while the other one progresses
        with self.db.transaction() as tx:
            if timeout_s is not None:
                # local to this transaction: a lock or a slow delete never
                # outlasts the caller's budget (the statement fails instead)
                ms = "%dms" % max(1, int(timeout_s * 1000))
                tx.query("select set_config('lock_timeout', %s, true),"
                         " set_config('statement_timeout', %s, true)", (ms, ms))
            gone = tx.query(
                "delete from ameesh_events where id in ("
                " select id from ameesh_events e"
                " where created_at < now() - make_interval(days => %s::int)"
                " and " + _DISPOSABLE +
                " order by created_at, id limit %s for update) returning tx::text as tx, id",
                (int(days), int(limit)))
            if gone:
                top = max((int(row["tx"]), int(row["id"])) for row in gone)
                tx.execute(
                    "insert into ameesh_events_pruned (one, tx, id)"
                    " values (true, %s::text::xid8, %s)"
                    " on conflict (one) do update set tx = excluded.tx, id = excluded.id,"
                    " at = now() where (ameesh_events_pruned.tx, ameesh_events_pruned.id)"
                    " < (excluded.tx, excluded.id)", (str(top[0]), top[1]))
            return len(gone)
