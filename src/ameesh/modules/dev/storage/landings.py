# SPDX-License-Identifier: AGPL-3.0-only
"""P1b #7650 : les déclarations d'atterrissage (`work_landings`).

Une déclaration de paquet épinglée au gel d'un véhicule est une donnée du
workflow de développement (étude #7616 §3.2, famille « paquets »). Le noyau
garde la transaction, le verrou de graphe et le journal ; ce domaine porte le
SQL de la table.
"""
from __future__ import annotations

from .schema import DevDomain, ModuleDb


class Landings(DevDomain):
    """`work_landings` : quel lot atterrit sur quel véhicule, à quelle tête."""

    def set(self, tx, item_id, vehicle_id, *, pinned_sha, until_on, actor) -> None:
        # La transaction vient du noyau (brute) : la qualifier ici, une fois
        # (revue B de #7650 — `work_landings` est une table du module).
        tx = ModuleDb(tx)
        tx.execute(
            "INSERT INTO work_landings(work_item_id, vehicle_id, pinned_sha, until_on, created_by)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (work_item_id) DO UPDATE"
            "     SET vehicle_id = EXCLUDED.vehicle_id,"
            "         pinned_sha = EXCLUDED.pinned_sha,"
            "         until_on = EXCLUDED.until_on,"
            "         created_by = EXCLUDED.created_by,"
            "         created_at = clock_timestamp()",
            (int(item_id), int(vehicle_id), pinned_sha, until_on, actor))

    def clear(self, tx, item_id):
        """Retire la déclaration ; rend le véhicule retiré, ou None."""
        tx = ModuleDb(tx)
        rows = tx.query(
            "DELETE FROM work_landings WHERE work_item_id = %s"
            " RETURNING vehicle_id", (int(item_id),))
        return rows[0]["vehicle_id"] if rows else None

    def on_vehicle(self, vehicle_id) -> list[dict]:
        """#3641 : les déclarations épinglées à ce véhicule (lecture groupée)."""
        return self.db.query(
            "SELECT work_item_id, vehicle_id, pinned_sha,"
            "       until_on::text AS until_on, created_by"
            "  FROM work_landings"
            " WHERE vehicle_id = %s"
            " ORDER BY work_item_id", (int(vehicle_id),))

    def pinned(self, item_id, vehicle_id):
        """La tête épinglée de la déclaration, ou None."""
        rows = self.db.query(
            "SELECT pinned_sha FROM work_landings"
            " WHERE work_item_id = %s AND vehicle_id = %s",
            (int(item_id), int(vehicle_id)))
        return (rows[0]["pinned_sha"] or "").strip() if rows else None

    def for_items(self, item_ids) -> list[dict]:
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        return self.db.query(
            "SELECT l.work_item_id, l.vehicle_id, l.pinned_sha,"
            "       l.until_on::text AS until_on,"
            "       l.created_by,"
            "       extract(epoch FROM l.created_at)::float8 AS created_ts,"
            "       w.state AS vehicle_state,"
            "       w.state NOT IN ('delivered', 'promoted', 'closed') AS open"
            "  FROM work_landings l JOIN work_items w ON w.id = l.vehicle_id"
            " WHERE l.work_item_id IN (" + ", ".join(["%s"] * len(ids)) + ")"
            " ORDER BY l.work_item_id", tuple(ids))
