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

#: les mêmes colonnes, qualifiées pour la jointure des lectures à deux temps
#: (#3631 : les identifiants par l'index, la ligne complète par la clé).
LOG_COLUMNS_ALIASED = (
    "l.id, l.host, extract(epoch from l.at)::float8 AS at_ts, l.actor, l.kind,"
    " l.action, l.path, l.bytes, l.agent, l.lot, l.detail, l.data"
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

    def latest_by_path(self, host, kind, *, since_s=None,
                       only_paths_with=None) -> list[dict]:
        where, params = " WHERE kind = %s", [kind]
        window = ""
        if host:
            where += " AND host = %s"
            params.append(host)
        if since_s is not None:
            # #2851 : une alerte qui ignore de toute façon les lignes plus
            # vieilles ne doit pas trier tout le journal de rétention.
            window = " AND at >= now() - make_interval(secs => %s)"
            where += window
            params.append(float(since_s))
        if only_paths_with is not None:
            actions = [str(action) for action in only_paths_with]
            if not actions:
                return []
            # #2851 : les chemins qui portent une de ces actions, puis la
            # dernière ligne de chacun — toutes actions et tous âges confondus.
            # Le filtre des candidats passe en SQL : sans lui, le DISTINCT ON
            # triait les 4 000 chemins `tmp` du journal pour n'en garder
            # qu'une poignée, et un `unowned` suivi d'un `deleted` restait
            # alerté faute de relire la dernière action.
            sub_where, sub_params = "kind = %s", [kind]
            if host:
                sub_where += " AND host = %s"
                sub_params.append(host)
            sub_where += window
            sub_params.extend([float(since_s)] if since_s is not None else [])
            marks = ", ".join(["%s"] * len(actions))
            where += (" AND (host, path) IN (SELECT host, path FROM housekeeping_log"
                      " WHERE " + sub_where + " AND action IN (" + marks + "))")
            params.extend(sub_params + actions)
        # #3631 : les identifiants de la dernière ligne de chaque (hôte, chemin)
        # se lisent dans l'index (kind, host, path, at DESC, id DESC) — sans
        # tri ni accès au tas — et seule la ligne retenue est relue. L'ancien
        # DISTINCT ON sur la table entière ramenait 200 Mo par appel.
        rows = self.db.query(
            "SELECT " + LOG_COLUMNS_ALIASED + " FROM housekeeping_log l"
            " JOIN (SELECT DISTINCT ON (host, path) host, path, id"
            "         FROM housekeeping_log" + where +
            "        ORDER BY host, path, at DESC, id DESC) u ON u.id = l.id"
            " ORDER BY l.host, l.path", tuple(params))
        return [_data(dict(r)) for r in rows]

    def summary(self, host, since_s) -> list[dict]:
        # #2617: an unattributed /tmp entry is signaled again every 12 h; in
        # the 24 h window each path counts once, with its latest size, or the
        # report shows its volume twice. #3728: so does an `unremovable`
        # reading, written at every pass while the episode lasts (84 lines of
        # one 1.3 GiB directory read as 106.6 GiB on agents-2).
        where, params = "", [float(since_s)]
        if host:
            where = " AND host = %s"
            params.append(host)
        once = "((kind = 'orphan' AND action = 'signaled') OR action = 'unremovable')"
        return self.db.query(
            "WITH recent AS ("
            " SELECT host, kind, action, path, bytes, at, id FROM housekeeping_log"
            " WHERE at >= now() - make_interval(secs => %s) AND kind <> 'mesure'" + where +
            "), signaled_once AS ("
            " SELECT DISTINCT ON (host, kind, action, path) host, kind, action, bytes"
            " FROM recent WHERE " + once +
            " ORDER BY host, kind, action, path, at DESC, id DESC)"
            " SELECT host, kind, action, count(*)::int AS n,"
            " coalesce(sum(bytes), 0)::bigint AS bytes FROM ("
            " SELECT host, kind, action, bytes FROM recent"
            " WHERE NOT " + once +
            " UNION ALL SELECT host, kind, action, bytes FROM signaled_once"
            ") AS lignes GROUP BY host, kind, action ORDER BY host, kind, action",
            tuple(params))

    def last_measure(self, host) -> list[dict]:
        where, params = "", []
        if host:
            where = " AND host = %s"
            params.append(host)
        # #3631 : ids par l'index (kind, host, at DESC, id DESC), une ligne
        # relue par hôte — l'ancien DISTINCT ON parcourait tout le journal du
        # ménage (~230 Mo) à chaque passe d'alertes.
        rows = self.db.query(
            "SELECT " + LOG_COLUMNS_ALIASED + " FROM housekeeping_log l"
            " JOIN (SELECT DISTINCT ON (host) host, id FROM housekeeping_log"
            "        WHERE kind = 'mesure'" + where +
            "       ORDER BY host, at DESC, id DESC) u ON u.id = l.id"
            " ORDER BY l.host", tuple(params))
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

    def set_worktree_attribution(self, worktree_id, agent, lot, detail) -> dict | None:
        rows = self.db.query(
            "UPDATE managed_worktrees SET agent = %s, lot = %s, detail = %s,"
            " checked_at = now() WHERE id = %s RETURNING " + WORKTREE_COLUMNS,
            (agent or "", None if lot is None else str(lot), detail or "",
             int(worktree_id)))
        return rows[0] if rows else None
