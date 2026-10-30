# SPDX-License-Identifier: AGPL-3.0-only
"""#4173 : empreintes de lot, écrites et journalisées dans une transaction."""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

from ..work_footprints import WorkFootprints as Contract
from .work_dependencies import _DONE_SQL, _LOCK as _GRAPH_LOCK

#: Un lot OCCUPE son empreinte dès qu'il est confié ou démarré, jusqu'à sa
#: fusion (ou sa fermeture) : sa branche peut encore entrer en conflit. Un lot
#: `intake` sans assigné n'occupe rien.
_OCCUPYING = ("({o}.state NOT IN " + _DONE_SQL +
              f" AND ({{o}}.assignee IS NOT NULL OR {{o}}.state <> '{lifecycle.REQUESTED}'))")

#: Deux chemins se chevauchent : égaux, ou l'un est un dossier parent de
#: l'autre. Même règle que `ameesh.work_footprints.overlaps`.
_PATHS_OVERLAP = ("({a}.path = {b}.path OR starts_with({a}.path, {b}.path || '/')"
                  " OR starts_with({b}.path, {a}.path || '/'))")


def occupying_sql(alias: str) -> str:
    return _OCCUPYING.format(o=alias)


def overlap_rows_sql(alias: str) -> str:
    """Les paires (chemin du lot `alias`, chemin d'un lot occupant du même
    dépôt) qui se chevauchent — corps de `overlapping` et de la garde."""
    return ("SELECT o.id, o.state, o.assignee, mine.path, theirs.path AS other_path"
            "  FROM work_item_footprints mine"
            "  JOIN work_item_footprints theirs"
            "    ON theirs.work_item_id <> mine.work_item_id"
            "   AND " + _PATHS_OVERLAP.format(a="theirs", b="mine") +
            "  JOIN work_items o ON o.id = theirs.work_item_id"
            " WHERE mine.work_item_id = " + alias + ".id"
            "   AND coalesce(btrim(" + alias + ".app), '') <> ''"
            "   AND btrim(o.app) = btrim(" + alias + ".app)"
            "   AND " + occupying_sql("o"))


def overlap_exists_sql(alias: str) -> str:
    """Vrai si le lot `alias` chevauche un lot occupant de son dépôt.

    `unassigned_lots` (sélection) et `_assign_locked` (sous le verrou du
    graphe) appliquent ce même prédicat : une seule règle."""
    return "EXISTS (" + overlap_rows_sql(alias) + ")"


class WorkFootprints(Contract):
    def __init__(self, db):
        self.db = db

    def replace(self, item_id, source, paths, *, actor, head=None, replaces=()):
        sources = sorted({str(source)} | {str(s) for s in replaces})
        wanted = sorted({str(p) for p in paths})
        with self.db.transaction() as tx:
            # Le verrou de la garde (prise, attribution, création attribuée),
            # pris avant la ligne du lot comme elles : une empreinte ne change
            # pas entre leur contrôle et leur écriture (B2 de la relecture).
            tx.execute(_GRAPH_LOCK)
            rows = tx.query("SELECT id, CASE WHEN state = 'delivered' THEN coalesce(to_jsonb(work_items)->>'legacy_state', state) ELSE state END AS state FROM work_items WHERE id = %s"
                            " FOR NO KEY UPDATE", (int(item_id),))
            if not rows:
                return None
            before = self._paths(tx, item_id)
            tx.execute("DELETE FROM work_item_footprints WHERE work_item_id = %s"
                       " AND source IN (" + ", ".join(["%s"] * len(sources)) + ")",
                       (int(item_id),) + tuple(sources))
            for path in wanted:
                tx.execute("INSERT INTO work_item_footprints"
                           "(work_item_id, source, path, head, created_by)"
                           " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                           (int(item_id), str(source), path, head, actor))
            after = self._paths(tx, item_id)
            changed = after != before
            if changed:
                tx.execute("INSERT INTO work_item_events(work_item_id, state, note, actor,"
                           " created_at) VALUES (%s, %s, %s, %s, clock_timestamp())",
                           (int(item_id), rows[0]["state"],
                            "Footprint (%s%s): %s" % (
                                source, " @%s" % head[:12] if head else "",
                                ", ".join(after) or "none"), actor))
                tx.execute("UPDATE work_items SET updated_at = clock_timestamp()"
                           " WHERE id = %s", (int(item_id),))
            return {"paths": after, "changed": changed}

    @staticmethod
    def _paths(tx, item_id):
        return [r["path"] for r in tx.query(
            "SELECT DISTINCT path FROM work_item_footprints WHERE work_item_id = %s"
            " ORDER BY path", (int(item_id),))]

    def for_items(self, item_ids):
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        return self.db.query(
            "SELECT work_item_id, source, path, head, created_by,"
            "       extract(epoch FROM created_at)::float8 AS created_ts"
            "  FROM work_item_footprints"
            " WHERE work_item_id IN (" + ", ".join(["%s"] * len(ids)) + ")"
            " ORDER BY work_item_id, path, source", tuple(ids))

    def overlapping(self, item_id):
        return self.db.query(
            "SELECT DISTINCT x.* FROM work_items c, LATERAL ("
            + overlap_rows_sql("c") + ") x WHERE c.id = %s ORDER BY x.id, x.path",
            (int(item_id),))
