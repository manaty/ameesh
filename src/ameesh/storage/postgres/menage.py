# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : ménage de ce que les agents créent (lot L73, migration
0045) — journal `housekeeping_log` et worktrees suivis `managed_worktrees`.

Les décisions (quoi supprimer, quand, à quelles conditions) sont dans
`ameesh.menage` ; ici, seulement le SQL (spec §10).
"""
from __future__ import annotations

import json
from typing import Any

from .. import interface

LOG_COLUMNS = (
    "id, host, extract(epoch from at)::float8 AS at_ts, actor, kind, action, path,"
    " bytes, agent, lot, detail, data"
)

WORKTREE_COLUMNS = (
    "id, host, path, repo, agent, lot, turn_id, branch, head, status, detail,"
    " extract(epoch from created_at)::float8 AS created_ts,"
    " extract(epoch from checked_at)::float8 AS checked_ts,"
    " extract(epoch from ended_at)::float8 AS ended_ts"
)


def _data(row: dict) -> dict:
    value = row.get("data")
    if isinstance(value, str):
        try:
            row["data"] = json.loads(value)
        except ValueError:
            row["data"] = None
    return row


class Housekeeping(interface.Housekeeping):

    def log(self, host, entries, *, actor) -> int:
        count = 0
        for entry in entries or ():
            data = entry.get("data")
            self.db.query(
                "INSERT INTO housekeeping_log"
                " (host, actor, kind, action, path, bytes, agent, lot, detail, data)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id",
                (host or "", actor or "", entry.get("kind") or "", entry.get("action") or "",
                 entry.get("path") or "",
                 None if entry.get("bytes") is None else int(entry["bytes"]),
                 entry.get("agent"), None if entry.get("lot") is None else str(entry["lot"]),
                 entry.get("detail") or "",
                 None if data is None else json.dumps(data, ensure_ascii=False)))
            count += 1
        if count:
            self.db.execute(
                "DELETE FROM housekeeping_log WHERE at < now() - interval '30 days'", ())
        return count

    def recent(self, host, since_s, limit=50, kind=None) -> list[dict]:
        where, params = "", [float(since_s)]
        if host:
            where += " AND host = %s"
            params.append(host)
        if kind:
            where += " AND kind = %s"
            params.append(kind)
        params.append(int(limit))
        rows = self.db.query(
            "SELECT " + LOG_COLUMNS + " FROM housekeeping_log"
            " WHERE at >= now() - make_interval(secs => %s)" + where +
            " ORDER BY at DESC, id DESC LIMIT %s", tuple(params))
        return [_data(dict(r)) for r in rows]

    def latest_by_path(self, host, kind, *, since_s=None) -> list[dict]:
        where, params = "", [kind]
        if host:
            where = " AND host = %s"
            params.append(host)
        if since_s is not None:
            # #2851 : une alerte qui ignore de toute façon les lignes plus
            # vieilles ne doit pas trier tout le journal de rétention.
            where += " AND at >= now() - make_interval(secs => %s)"
            params.append(float(since_s))
        rows = self.db.query(
            "SELECT DISTINCT ON (host, path) " + LOG_COLUMNS + " FROM housekeeping_log"
            " WHERE kind = %s" + where +
            " ORDER BY host, path, at DESC, id DESC", tuple(params))
        return [_data(dict(r)) for r in rows]

    def action_lines(self, host, kind, actions) -> list[dict]:
        actions = [str(action) for action in actions or ()]
        if not actions:
            return []
        where, params = "", [kind, *actions]
        if host:
            where = " AND host = %s"
            params.append(host)
        marks = ", ".join(["%s"] * len(actions))
        rows = self.db.query(
            "SELECT " + LOG_COLUMNS + " FROM housekeeping_log"
            " WHERE kind = %s AND action IN (" + marks + ")" + where +
            " ORDER BY at DESC, id DESC", tuple(params))
        return [_data(dict(r)) for r in rows]

    def summary(self, host, since_s) -> list[dict]:
        # #2617: an unattributed /tmp entry is signaled again every 12 h; in
        # the 24 h window each path counts once, with its latest size, or the
        # report shows its volume twice.
        where, params = "", [float(since_s)]
        if host:
            where = " AND host = %s"
            params.append(host)
        return self.db.query(
            "WITH recent AS ("
            " SELECT host, kind, action, path, bytes, at, id FROM housekeeping_log"
            " WHERE at >= now() - make_interval(secs => %s) AND kind <> 'mesure'" + where +
            "), signaled_once AS ("
            " SELECT DISTINCT ON (host, path) host, kind, action, bytes FROM recent"
            " WHERE kind = 'orphan' AND action = 'signaled'"
            " ORDER BY host, path, at DESC, id DESC)"
            " SELECT host, kind, action, count(*)::int AS n,"
            " coalesce(sum(bytes), 0)::bigint AS bytes FROM ("
            " SELECT host, kind, action, bytes FROM recent"
            " WHERE NOT (kind = 'orphan' AND action = 'signaled')"
            " UNION ALL SELECT host, kind, action, bytes FROM signaled_once"
            ") AS lignes GROUP BY host, kind, action ORDER BY host, kind, action",
            tuple(params))

    def last_measure(self, host) -> list[dict]:
        where, params = "", []
        if host:
            where = " AND host = %s"
            params.append(host)
        rows = self.db.query(
            "SELECT DISTINCT ON (host) " + LOG_COLUMNS + " FROM housekeeping_log"
            " WHERE kind = 'mesure'" + where + " ORDER BY host, at DESC, id DESC",
            tuple(params))
        return [_data(dict(r)) for r in rows]

    def register_worktree(self, *, host, path, repo, agent, lot, turn_id, branch,
                          head) -> dict | None:
        rows = self.db.query(
            "INSERT INTO managed_worktrees"
            " (host, path, repo, agent, lot, turn_id, branch, head)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (host, path) WHERE status IN ('active', 'kept') DO NOTHING"
            " RETURNING " + WORKTREE_COLUMNS,
            (host or "", path, repo or "", agent, None if lot is None else str(lot),
             turn_id, branch or "", head or ""))
        return rows[0] if rows else None

    def worktrees(self, host, statuses=None, limit=500) -> list[dict]:
        clauses, params = [], []  # type: list[str], list[Any]
        if host:
            clauses.append("host = %s")
            params.append(host)
        if statuses:
            clauses.append("status IN (SELECT jsonb_array_elements_text(%s::jsonb))")
            params.append(json.dumps(list(statuses)))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        bound = ""
        if limit is not None:
            bound = " LIMIT %s"
            params.append(int(limit))
        return self.db.query(
            "SELECT " + WORKTREE_COLUMNS + " FROM managed_worktrees" + where +
            " ORDER BY created_at, id" + bound, tuple(params))

    def set_worktree_status(self, worktree_id, status, detail) -> dict | None:
        rows = self.db.query(
            "UPDATE managed_worktrees SET status = %s, detail = %s, checked_at = now(),"
            " ended_at = CASE WHEN %s IN ('removed', 'gone') THEN now() ELSE ended_at END"
            " WHERE id = %s RETURNING " + WORKTREE_COLUMNS,
            (status, detail or "", status, int(worktree_id)))
        return rows[0] if rows else None
