# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 : les worktrees suivis (`managed_worktrees`) vivent avec `dev`.

Le journal du ménage (`housekeeping_log`) reste au noyau ; la table des
worktrees attribués aux agents et aux lots est une table de développement
(étude #7616 §3.2, famille « espaces de travail »). Le noyau garde l'API
`storage.of(db).housekeeping` ; ce domaine porte le SQL.
"""
from __future__ import annotations

import json
from typing import Any

from .schema import DevDomain

WORKTREE_COLUMNS = (
    "id, host, path, repo, agent, lot, turn_id, branch, head, status, detail,"
    " extract(epoch from created_at)::float8 AS created_ts,"
    " extract(epoch from checked_at)::float8 AS checked_ts,"
    " extract(epoch from ended_at)::float8 AS ended_ts"
)


class Worktrees(DevDomain):
    """`managed_worktrees` : le suivi des worktrees d'un hôte (L73)."""

    def register(self, *, host, path, repo, agent, lot, turn_id, branch,
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

    def for_lot(self, host, lot, *, status="active") -> list[str]:
        """Les chemins des worktrees actifs attribués à ce lot sur cet hôte."""
        rows = self.db.query(
            "SELECT path FROM managed_worktrees"
            " WHERE host = %s AND lot = %s AND status = %s ORDER BY id DESC",
            (host, str(lot), status))
        return [str(row["path"]) for row in rows]

    def set_status(self, worktree_id, status, detail) -> dict | None:
        rows = self.db.query(
            "UPDATE managed_worktrees SET status = %s, detail = %s, checked_at = now(),"
            " ended_at = CASE WHEN %s IN ('removed', 'gone') THEN now() ELSE ended_at END"
            " WHERE id = %s RETURNING " + WORKTREE_COLUMNS,
            (status, detail or "", status, int(worktree_id)))
        return rows[0] if rows else None

    def set_attribution(self, worktree_id, agent, lot, detail) -> dict | None:
        rows = self.db.query(
            "UPDATE managed_worktrees SET agent = %s, lot = %s, detail = %s,"
            " checked_at = now() WHERE id = %s RETURNING " + WORKTREE_COLUMNS,
            (agent or "", None if lot is None else str(lot), detail or "",
             int(worktree_id)))
        return rows[0] if rows else None
