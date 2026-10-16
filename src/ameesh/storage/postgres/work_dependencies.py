# SPDX-License-Identifier: AGPL-3.0-only
"""L385: graph edits and audit records on the same Postgres transaction."""
from __future__ import annotations

from ..work_dependencies import WorkDependencies as Contract

_DONE = ("merged", "promoted", "closed")
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':work_dependencies', 0))"


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

    @staticmethod
    def _landing_journal(tx, item_id, note, actor):
        tx.execute("INSERT INTO work_item_events(work_item_id, state, note, actor, created_at)"
                   " SELECT id, state, %s, %s, clock_timestamp() FROM work_items WHERE id = %s",
                   (note, actor, item_id))
        tx.execute("UPDATE work_items SET updated_at = clock_timestamp() WHERE id = %s", (item_id,))

    def set_landing(self, item_id, vehicle_id, *, until_on, actor):
        if item_id == vehicle_id:
            return {"error": "self_landing"}
        with self.db.transaction() as tx:
            self.lock()                       # même verrou que le graphe de dépendances
            rows = tx.query("SELECT id, state FROM work_items WHERE id IN (%s, %s)"
                            " ORDER BY id FOR UPDATE", (item_id, vehicle_id))
            states = {int(row["id"]): row["state"] for row in rows}
            if item_id not in states or vehicle_id not in states:
                return {"error": "unknown_lot"}
            if states[item_id] in _DONE:
                return {"error": "finished_lot"}
            if states[vehicle_id] in _DONE:
                return {"error": "finished_vehicle"}
            if states[vehicle_id] == "blocked":
                return {"error": "blocked_vehicle"}
            # #1945, revue B : l'admission est ÉPINGLÉE au gel du véhicule. La
            # tête est lue dans la même transaction que l'écriture ; sans gel,
            # rien ne peut être épinglé et la déclaration est refusée.
            freezes = tx.query("""
                SELECT sha FROM work_item_milestones
                 WHERE work_item_id = %s AND kind = 'frozen'
                   AND btrim(coalesce(sha, '')) <> ''
                 ORDER BY at DESC, id DESC LIMIT 1""", (vehicle_id,))
            if not freezes:
                return {"error": "no_freeze"}
            pinned = freezes[0]["sha"].strip()
            tx.execute("""
                INSERT INTO work_landings(work_item_id, vehicle_id, pinned_sha, until_on, created_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (work_item_id) DO UPDATE
                    SET vehicle_id = EXCLUDED.vehicle_id,
                        pinned_sha = EXCLUDED.pinned_sha,
                        until_on = EXCLUDED.until_on,
                        created_by = EXCLUDED.created_by,
                        created_at = clock_timestamp()
                """, (item_id, vehicle_id, pinned, until_on, actor))
            self._landing_journal(
                tx, item_id,
                "Landing declared: vehicle #%d until %s (frozen %s)"
                % (vehicle_id, until_on, pinned[:12]), actor)
            return {"landed": True}

    def clear_landing(self, item_id, *, actor):
        with self.db.transaction() as tx:
            self.lock()
            removed = tx.query("DELETE FROM work_landings WHERE work_item_id = %s"
                               " RETURNING vehicle_id", (item_id,))
            if not removed:
                return False
            self._landing_journal(
                tx, item_id, "Landing cleared: vehicle #%d" % int(removed[0]["vehicle_id"]), actor)
            return True

    def landings_for(self, item_ids):
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        return self.db.query("""
            SELECT l.work_item_id, l.vehicle_id, l.pinned_sha,
                   l.until_on::text AS until_on,
                   l.created_by,
                   extract(epoch FROM l.created_at)::float8 AS created_ts,
                   w.state AS vehicle_state,
                   w.state NOT IN ('merged', 'promoted', 'closed') AS open
            FROM work_landings l JOIN work_items w ON w.id = l.vehicle_id
            WHERE l.work_item_id IN (""" + ", ".join(["%s"] * len(ids)) + ")"
            " ORDER BY l.work_item_id", tuple(ids))

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
        Rend les identifiants rouverts."""
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        rows = self.db.query(
            "UPDATE work_items SET state = 'intake', updated_at = clock_timestamp()"
            " WHERE id IN (" + ", ".join(["%s"] * len(ids)) + ")"
            " AND state = 'blocked' AND assignee IS NULL RETURNING id", tuple(ids))
        for row in rows:
            self.db.execute(
                "INSERT INTO work_item_events(work_item_id, state, note, actor, created_at)"
                " VALUES (%s, 'intake', %s, %s, clock_timestamp())",
                (int(row["id"]),
                 "Dependency released: back to intake (no assignee, #1668)", actor or ""))
        return [int(row["id"]) for row in rows]
