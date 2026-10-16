# SPDX-License-Identifier: AGPL-3.0-only
"""L385: graph edits and audit records on the same Postgres transaction."""
from __future__ import annotations

from ..work_dependencies import WorkDependencies as Contract

_DONE = ("merged", "promoted", "closed")
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':work_dependencies', 0))"

#: #1922 : un lot pinné sur une révision Process ne bouge que par le registre
#: (`process_registry`), qui vérifie la garde serveur avant d'écrire. Les
#: écritures historiques d'état filtrent donc la colonne DANS le SQL : une
#: adoption concurrente (qui garde l'état) ne laisse plus passer un move
#: legacy entre la lecture et l'UPDATE (N8 de la relecture Claude). La lecture
#: par `to_jsonb` rend `''` sur une base d'avant 0452, sans casser.
_UNPINNED = "coalesce(to_jsonb(work_items)->>'workflow_ref', '') = ''"


class WorkDependencies(Contract):
    def __init__(self, db):
        self.db = db

    def lock(self):
        self.db.execute(_LOCK)

    @staticmethod
    def _journal(tx, item_id, blocker_id, action, actor):
        tx.execute("INSERT INTO work_item_events(work_item_id, state, note, actor, created_at)"
                   " SELECT id, state, %s, %s, clock_timestamp() FROM work_items WHERE id = %s",
                   ("Dependency %s: lot #%d" % (action, blocker_id), actor, item_id))
        tx.execute("UPDATE work_items SET updated_at = clock_timestamp() WHERE id = %s", (item_id,))

    def add(self, item_id, blocker_id, *, actor):
        if item_id == blocker_id:
            return {"error": "self_dependency"}
        with self.db.transaction() as tx:
            # One graph lock per schema, including removals: two concurrent
            # edits cannot independently pass their cycle checks then cycle.
            tx.execute(_LOCK)
            rows = tx.query("SELECT id, state FROM work_items WHERE id IN (%s, %s)"
                            " ORDER BY id FOR UPDATE", (item_id, blocker_id))
            states = {int(row["id"]): row["state"] for row in rows}
            if item_id not in states or blocker_id not in states:
                return {"error": "unknown_lot"}
            if tx.query("SELECT 1 FROM work_item_dependencies WHERE work_item_id = %s"
                        " AND blocker_id = %s", (item_id, blocker_id)):
                return {"added": False}
            if states[item_id] in _DONE or states[blocker_id] in _DONE:
                return {"error": "finished_lot"}
            cycle = tx.query("""
                WITH RECURSIVE reach(id) AS (
                    SELECT blocker_id FROM work_item_dependencies WHERE work_item_id = %s
                    UNION
                    SELECT d.blocker_id FROM work_item_dependencies d JOIN reach r
                        ON d.work_item_id = r.id
                ) SELECT 1 FROM reach WHERE id = %s LIMIT 1
                """, (blocker_id, item_id))
            if cycle:
                return {"error": "dependency_cycle"}
            tx.execute("INSERT INTO work_item_dependencies(work_item_id, blocker_id, created_by)"
                       " VALUES (%s, %s, %s)", (item_id, blocker_id, actor))
            self._journal(tx, item_id, blocker_id, "added", actor)
            return {"added": True}

    def remove(self, item_id, blocker_id, *, actor):
        with self.db.transaction() as tx:
            tx.execute(_LOCK)
            tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE", (item_id,))
            removed = tx.query("DELETE FROM work_item_dependencies WHERE work_item_id = %s"
                               " AND blocker_id = %s RETURNING work_item_id", (item_id, blocker_id))
            if not removed:
                return False
            self._journal(tx, item_id, blocker_id, "removed", actor)
            return True

    def for_items(self, item_ids):
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        return self.db.query("""
            SELECT d.work_item_id, d.blocker_id, d.created_by,
                   extract(epoch FROM d.created_at)::float8 AS created_ts,
                   w.state AS blocker_state,
                   w.state NOT IN ('merged', 'promoted', 'closed') AS blocking
            FROM work_item_dependencies d JOIN work_items w ON w.id = d.blocker_id
            WHERE d.work_item_id IN (""" + ", ".join(["%s"] * len(ids)) + ")"
            " ORDER BY d.work_item_id, d.blocker_id", tuple(ids))

    def release_unassigned(self, item_ids, *, actor):
        """#1668 : un dépendant NON ASSIGNÉ dont la dernière attente vient de
        tomber repasse `blocked` → `intake` dans la transaction de fermeture.

        Sans ce geste, `lot_sessions.actionable` (qui ne lit que `intake` et
        `build`) ne le voit pas : il reste invisible au self-service jusqu'à ce
        qu'une alerte stale le fasse rouvrir à la main, jusqu'à 14 h plus tard.
        Les lots assignés ne bougent pas : le courrier de réveil (L385) porte
        le geste à leur agent, qui peut juger la suite. Appelé dans la
        transaction qui ferme le bloqueur, sous son verrou de graphe : la
        réouverture est committée avec la fermeture, ou pas du tout.
        Un lot retenu par le registre Process (`workflow_ref` non nul) n'est
        jamais rouvert ici : sa retenue (`blocked`) ne tombe que par une
        commande `release` du registre, vérifiée par C (#1922/B4).
        Rend les identifiants rouverts."""
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        rows = self.db.query(
            "UPDATE work_items SET state = 'intake', updated_at = clock_timestamp()"
            " WHERE id IN (" + ", ".join(["%s"] * len(ids)) + ")"
            " AND state = 'blocked' AND assignee IS NULL"
            " AND " + _UNPINNED + " RETURNING id", tuple(ids))
        for row in rows:
            self.db.execute(
                "INSERT INTO work_item_events(work_item_id, state, note, actor, created_at)"
                " VALUES (%s, 'intake', %s, %s, clock_timestamp())",
                (int(row["id"]),
                 "Dependency released: back to intake (no assignee, #1668)", actor or ""))
        return [int(row["id"]) for row in rows]
