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


def _latest_by_path_sql(kind: str, *, host: str | None = None,
                        since_s: float | None = None,
                        actions: list[str] | None = None) -> tuple[str, tuple]:
    """Le SQL de `latest_by_path` et ses paramètres (une seule requête).

    `actions` : les chemins candidats sont ceux qui portent une de ces
    actions ; la dernière ligne de chacun (toutes actions confondues) fait
    foi. La lecture part alors de l'index (kind, action, host, path, at desc)
    — chercher ces candidats dans tout le journal coûtait 15-18 s par lecture
    (#4888) — puis relit la ligne retenue par l'index
    (kind, host, path, at desc, id desc). Sans `actions`, la lecture reste le
    `DISTINCT ON` borné par `since_s` de #2851.
    """
    window = ""
    if since_s is not None:
        window = " AND at >= now() - make_interval(secs => %s)"
    if actions is not None:
        marks = ", ".join(["%s"] * len(actions))
        candidates = "SELECT DISTINCT host, path FROM housekeeping_log WHERE kind = %s"
        params: list = [kind]
        if host:
            candidates += " AND host = %s"
            params.append(host)
        candidates += window
        if since_s is not None:
            params.append(float(since_s))
        candidates += " AND action IN (" + marks + ")"
        params.extend(str(action) for action in actions)
        latest = "l.kind = %s AND l.host = p.host AND l.path = p.path"
        params.append(kind)
        if since_s is not None:
            latest += " AND l.at >= now() - make_interval(secs => %s)"
            params.append(float(since_s))
        sql = ("SELECT " + LOG_COLUMNS_ALIASED + " FROM (" + candidates + ") p"
               " CROSS JOIN LATERAL ("
               "   SELECT l.* FROM housekeeping_log l WHERE " + latest +
               "   ORDER BY l.at DESC, l.id DESC LIMIT 1) l"
               " ORDER BY l.host, l.path")
        return sql, tuple(params)
    where, params = " WHERE kind = %s", [kind]
    if host:
        where += " AND host = %s"
        params.append(host)
    if since_s is not None:
        # #2851 : une alerte qui ignore de toute façon les lignes plus
        # vieilles ne doit pas trier tout le journal de rétention.
        where += window
        params.append(float(since_s))
    # #3631 : les identifiants de la dernière ligne de chaque (hôte, chemin)
    # se lisent dans l'index (kind, host, path, at DESC, id DESC) — sans
    # tri ni accès au tas — et seule la ligne retenue est relue. L'ancien
    # DISTINCT ON sur la table entière ramenait 200 Mo par appel.
    sql = ("SELECT " + LOG_COLUMNS_ALIASED + " FROM housekeeping_log l"
           " JOIN (SELECT DISTINCT ON (host, path) host, path, id"
           "         FROM housekeeping_log" + where +
           "        ORDER BY host, path, at DESC, id DESC) u ON u.id = l.id"
           " ORDER BY l.host, l.path")
    return sql, tuple(params)


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
        actions = None
        if only_paths_with is not None:
            actions = [str(action) for action in only_paths_with]
            if not actions:
                return []
        sql, params = _latest_by_path_sql(kind, host=host, since_s=since_s,
                                          actions=actions)
        return [_data(dict(r)) for r in self.db.query(sql, params)]

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
            where = " WHERE l.host = %s"
            params.append(host)
        # #3631 : ids par l'index (kind, host, at DESC, id DESC), une ligne
        # relue par hôte — l'ancien DISTINCT ON parcourait tout le journal du
        # ménage (~230 Mo) à chaque passe d'alertes.
        # #3741 : le DISTINCT ON lisait encore toutes les entrées d'index des
        # mesures (27 000, 0,25 s à vide, 6 s sous charge) ; le parcours
        # récursif saute d'un hôte au suivant (`host` est NOT NULL) et lit une
        # entrée d'index par hôte.
        rows = self.db.query(
            "WITH RECURSIVE h(host) AS ("
            "  (SELECT host FROM housekeeping_log WHERE kind = 'mesure'"
            "    ORDER BY host LIMIT 1)"
            "  UNION ALL"
            "  SELECT (SELECT n.host FROM housekeeping_log n"
            "           WHERE n.kind = 'mesure' AND n.host > h.host"
            "           ORDER BY n.host LIMIT 1)"
            "    FROM h WHERE h.host IS NOT NULL)"
            "SELECT " + LOG_COLUMNS_ALIASED + " FROM h"
            " CROSS JOIN LATERAL (SELECT u.id FROM housekeeping_log u"
            "   WHERE u.kind = 'mesure' AND u.host = h.host"
            "   ORDER BY u.at DESC, u.id DESC LIMIT 1) u"
            " JOIN housekeeping_log l ON l.id = u.id" + where +
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
