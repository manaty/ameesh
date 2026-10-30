# SPDX-License-Identifier: AGPL-3.0-only
"""#4173 : empreintes de lot, écrites et journalisées dans une transaction."""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

from ..work_footprints import WorkFootprints as Contract
from .catalog import _text_array
from .work_dependencies import _DONE_SQL, _LOCK as _GRAPH_LOCK

#: Un lot OCCUPE son empreinte dès qu'il est confié ou démarré, jusqu'à sa
#: fusion (ou sa fermeture) : sa branche peut encore entrer en conflit. Un lot
#: `intake` sans assigné n'occupe rien.
_OCCUPYING = ("({o}.state NOT IN " + _DONE_SQL +
              f" AND ({{o}}.assignee IS NOT NULL OR {{o}}.state <> '{lifecycle.REQUESTED}'))")

#: Deux chemins se chevauchent : égaux, ou l'un est un dossier parent de
#: l'autre ; le marqueur `.` (empreinte inconnue, dépôt entier, #4818)
#: chevauche tout. Même règle que `ameesh.work_footprints.overlaps`.
_PATHS_OVERLAP = ("({a}.path = {b}.path OR starts_with({a}.path, {b}.path || '/')"
                  " OR starts_with({b}.path, {a}.path || '/')"
                  " OR {a}.path = '.' OR {b}.path = '.')")

#: #4818 : un chemin partagé du dépôt (fusion triviale, `shared_paths` de sa
#: politique) ou un chemin sous un dossier partagé ne compte pas pour le
#: chevauchement. Même règle que `ameesh.work_footprints.is_shared`.
_SHARED = ("EXISTS (SELECT 1 FROM repository_footprint_policies sp,"
           " unnest(sp.shared_paths) s(path)"
           " WHERE sp.app = btrim({app}) AND ({p}.path = s.path"
           " OR starts_with({p}.path, s.path || '/')))")


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
            "   AND " + occupying_sql("o") +
            "   AND NOT " + _SHARED.format(app=alias + ".app", p="mine") +
            "   AND NOT " + _SHARED.format(app=alias + ".app", p="theirs"))


def overlap_exists_sql(alias: str) -> str:
    """Vrai si le lot `alias` chevauche un lot occupant de son dépôt.

    `unassigned_lots` (sélection) et `_assign_locked` (sous le verrou du
    graphe) appliquent ce même prédicat : une seule règle."""
    return "EXISTS (" + overlap_rows_sql(alias) + ")"


#: #4667 : un véhicule de paquet (`integration/**`) n'a pas de delta propre ;
#: il n'entre pas dans le plafond de lots en vol, ni comme compté ni comme
#: retenu (même lecture que `packet_guard.is_packet_branch`).
_VEHICLE = ("(coalesce(btrim({o}.branch), '')"
            " ~ '^(refs/heads/|refs/remotes/origin/|origin/)?integration/')")


def in_flight_sql(alias: str) -> str:
    """Le nombre de lots qui OCCUPENT leur empreinte dans le dépôt du lot
    `alias`, lui et les véhicules de paquet exceptés (#4818, plafond de lots
    en vol)."""
    return ("(SELECT count(*) FROM work_items o"
            "  WHERE coalesce(btrim(" + alias + ".app), '') <> ''"
            "    AND btrim(o.app) = btrim(" + alias + ".app)"
            "    AND o.id <> " + alias + ".id AND " + occupying_sql("o") +
            "    AND NOT " + _VEHICLE.format(o="o") + ")")


def cap_rows_sql(alias: str) -> str:
    """#4818 : `(cap, in_flight)` quand le dépôt du lot `alias` a atteint son
    plafond de lots en vol (`max_in_flight` de sa politique) ; aucune ligne
    sans plafond ou en dessous."""
    return ("SELECT cp.max_in_flight AS cap, " + in_flight_sql(alias) + " AS in_flight"
            "  FROM repository_footprint_policies cp"
            " WHERE cp.app = btrim(" + alias + ".app)"
            "   AND cp.max_in_flight IS NOT NULL"
            "   AND NOT " + _VEHICLE.format(o=alias) +
            "   AND " + in_flight_sql(alias) + " >= cp.max_in_flight")


def cap_reached_sql(alias: str) -> str:
    return "EXISTS (" + cap_rows_sql(alias) + ")"


def held_sql(alias: str) -> str:
    """Vrai si la garde retient le lot `alias` : son empreinte chevauche celle
    d'un lot occupant de son dépôt, ou le dépôt a atteint son plafond de lots
    en vol (#4818). Le prédicat commun de la prise, de l'attribution et du
    plan de dispatch."""
    return "(" + overlap_exists_sql(alias) + " OR " + cap_reached_sql(alias) + ")"


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
            rows = tx.query("SELECT id, state FROM work_items WHERE id = %s"
                            " FOR NO KEY UPDATE", (int(item_id),))
            if not rows:
                return None
            before = self._paths(tx, item_id)
            tx.execute("DELETE FROM work_item_footprints WHERE work_item_id = %s"
                       " AND source IN (" + ", ".join(["%s"] * len(sources)) + ")",
                       (int(item_id),) + tuple(sources))
            if str(source) == "diff" and wanted:
                # #4818 : le diff réel connaît l'empreinte ; le marqueur
                # d'empreinte inconnue de l'analyse (`.`, dépôt entier)
                # tombe. Un `.` posé à la main (`--set .`) reste.
                tx.execute("DELETE FROM work_item_footprints WHERE work_item_id = %s"
                           " AND source = 'intake' AND path = '.'", (int(item_id),))
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

    def cap_reached(self, item_id):
        rows = self.db.query(
            "SELECT x.* FROM work_items c, LATERAL (" + cap_rows_sql("c") + ") x"
            " WHERE c.id = %s", (int(item_id),))
        return dict(rows[0]) if rows else None

    def policies(self, apps=None):
        if apps is None:
            rows = self.db.query(
                "SELECT app, unknown_policy, max_in_flight, shared_paths, updated_by,"
                "       extract(epoch FROM updated_at)::float8 AS updated_ts"
                "  FROM repository_footprint_policies ORDER BY app")
        else:
            wanted = sorted({str(a).strip() for a in apps if str(a or "").strip()})
            if not wanted:
                return {}
            rows = self.db.query(
                "SELECT app, unknown_policy, max_in_flight, shared_paths, updated_by,"
                "       extract(epoch FROM updated_at)::float8 AS updated_ts"
                "  FROM repository_footprint_policies WHERE app IN ("
                + ", ".join(["%s"] * len(wanted)) + ") ORDER BY app", tuple(wanted))
        return {r["app"]: dict(r, shared_paths=list(r.get("shared_paths") or ()))
                for r in rows}

    def set_policy(self, app, *, actor, unknown_policy=None, max_in_flight=None,
                   clear_cap=False, shared_paths=None):
        with self.db.transaction() as tx:
            # le verrou de la garde : une politique ne change pas entre le
            # contrôle d'une attribution et son écriture
            tx.execute(_GRAPH_LOCK)
            rows = tx.query("SELECT unknown_policy, max_in_flight, shared_paths"
                            "  FROM repository_footprint_policies WHERE app = %s"
                            " FOR UPDATE", (str(app),))
            before = dict(rows[0]) if rows else {"unknown_policy": "open",
                                                 "max_in_flight": None,
                                                 "shared_paths": []}
            after = {"unknown_policy": unknown_policy or before["unknown_policy"],
                     "max_in_flight": (None if clear_cap else
                                       max_in_flight if max_in_flight is not None
                                       else before["max_in_flight"]),
                     "shared_paths": sorted(set(shared_paths)) if shared_paths is not None
                     else sorted(before.get("shared_paths") or ())}
            tx.execute("INSERT INTO repository_footprint_policies"
                       "(app, unknown_policy, max_in_flight, shared_paths, updated_by)"
                       " VALUES (%s, %s, %s, %s::text[], %s)"
                       " ON CONFLICT (app) DO UPDATE SET unknown_policy = EXCLUDED.unknown_policy,"
                       " max_in_flight = EXCLUDED.max_in_flight,"
                       " shared_paths = EXCLUDED.shared_paths,"
                       " updated_by = EXCLUDED.updated_by, updated_at = clock_timestamp()",
                       (str(app), after["unknown_policy"], after["max_in_flight"],
                        _text_array(after["shared_paths"]), actor))
            before["shared_paths"] = sorted(before.get("shared_paths") or ())
            return {"app": str(app), "before": before, "after": after,
                    "changed": before != after}

    def record_analysis(self, item_id, analysis, *, actor):
        import json as _json
        with self.db.transaction() as tx:
            rows = tx.query("SELECT state FROM work_items WHERE id = %s", (int(item_id),))
            if not rows:
                return None
            tx.execute(
                "INSERT INTO work_item_impact_analyses(work_item_id, status, reason,"
                " paths, weak, policy, elapsed_ms, git_calls, analyzed_by)"
                " VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)"
                " ON CONFLICT (work_item_id) DO UPDATE SET status = EXCLUDED.status,"
                " reason = EXCLUDED.reason, paths = EXCLUDED.paths, weak = EXCLUDED.weak,"
                " policy = EXCLUDED.policy, elapsed_ms = EXCLUDED.elapsed_ms,"
                " git_calls = EXCLUDED.git_calls, analyzed_by = EXCLUDED.analyzed_by,"
                " attempts = work_item_impact_analyses.attempts + 1,"
                " analyzed_at = clock_timestamp()",
                (int(item_id), analysis["status"], analysis.get("reason"),
                 _json.dumps(analysis.get("paths") or []),
                 _json.dumps(analysis.get("weak") or []), analysis.get("policy"),
                 analysis.get("elapsed_ms"), analysis.get("git_calls"), actor))
            tx.execute("INSERT INTO work_item_events(work_item_id, state, note, actor,"
                       " created_at) VALUES (%s, %s, %s, %s, clock_timestamp())",
                       (int(item_id), rows[0]["state"], analysis["note"], actor))
            return {"id": int(item_id), "status": analysis["status"]}

    def analyses(self, item_ids):
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return {}
        rows = self.db.query(
            "SELECT work_item_id, status, reason, paths, weak, policy, elapsed_ms,"
            "       git_calls, attempts, analyzed_by,"
            "       extract(epoch FROM analyzed_at)::float8 AS analyzed_ts"
            "  FROM work_item_impact_analyses WHERE work_item_id IN ("
            + ", ".join(["%s"] * len(ids)) + ")", tuple(ids))
        return {int(r["work_item_id"]): dict(r) for r in rows}

    def claim_unknown(self, *, retry_after_s, limit, apps=None):
        app_filter = ""
        params: tuple = (float(retry_after_s),)
        if apps is not None:
            wanted = sorted({str(a).strip() for a in apps if str(a or "").strip()})
            if not wanted:
                return []
            app_filter = (" AND btrim(w.app) IN (" + ", ".join(["%s"] * len(wanted)) + ")")
            params += tuple(wanted)
        # la prise est l'écriture de `analyzed_at` : deux exécuteurs ne
        # réanalysent pas le même lot dans la même fenêtre
        return self.db.query(
            "UPDATE work_item_impact_analyses a SET analyzed_at = clock_timestamp()"
            "  FROM (SELECT a2.work_item_id FROM work_item_impact_analyses a2"
            "          JOIN work_items w ON w.id = a2.work_item_id"
            f"        WHERE a2.status = 'unknown' AND w.state = '{lifecycle.REQUESTED}'"
            "           AND a2.analyzed_at < clock_timestamp() - make_interval(secs => %s)"
            # une empreinte manuelle remplace l'estimation : rien à retenter
            "           AND NOT EXISTS (SELECT 1 FROM work_item_footprints m"
            "                            WHERE m.work_item_id = w.id AND m.source = 'manual')"
            + app_filter +
            "         ORDER BY a2.analyzed_at LIMIT %s"
            "           FOR UPDATE OF a2 SKIP LOCKED) pick"
            " WHERE a.work_item_id = pick.work_item_id"
            " RETURNING a.work_item_id", params + (int(limit),))

    def unknown_apps(self):
        return sorted({str(r["app"]) for r in self.db.query(
            "SELECT DISTINCT btrim(w.app) AS app FROM work_item_impact_analyses a"
            "  JOIN work_items w ON w.id = a.work_item_id"
            f" WHERE a.status = 'unknown' AND w.state = '{lifecycle.REQUESTED}'"
            "   AND coalesce(btrim(w.app), '') <> ''")})

    def refresh_candidates(self, *, limit, assignees=None):
        app_filter = ""
        params: tuple = ()
        if assignees is not None:
            wanted = sorted({str(a).strip() for a in assignees if str(a or "").strip()})
            if not wanted:
                return []
            app_filter = (" AND w.assignee IN (" + ", ".join(["%s"] * len(wanted)) + ")")
            params += tuple(wanted)
        return self.db.query(
            "SELECT w.id, w.app, w.branch, w.branch_target, w.assignee, w.state,"
            "       (SELECT f.head FROM work_item_footprints f"
            "         WHERE f.work_item_id = w.id AND f.source = 'diff'"
            "         ORDER BY f.created_at DESC LIMIT 1) AS diff_head,"
            "       EXISTS (SELECT 1 FROM work_item_footprints f"
            "                WHERE f.work_item_id = w.id AND f.source = 'diff') AS has_diff"
            "  FROM work_items w"
            " WHERE coalesce(btrim(w.branch), '') <> ''"
            "   AND coalesce(btrim(w.app), '') <> ''"
            "   AND " + occupying_sql("w") + app_filter +
            " ORDER BY w.id LIMIT %s", params + (int(limit),))
