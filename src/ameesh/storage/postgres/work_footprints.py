# SPDX-License-Identifier: AGPL-3.0-only
"""#4173 : empreintes de lot, écrites et journalisées dans une transaction."""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

from ..work_footprints import WorkFootprints as Contract
from .catalog import _text_array
from .work_dependencies import _DONE_SQL, _LOCK as _GRAPH_LOCK

#: Le préfixe de `source` d'un panier (`backlog.BASKET_PREFIX`).
BASKET_PREFIX = "backlog-basket:"

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


#: #4818 (relectures qa-01, dev3) : les lots qui ne sont pas du code — un
#: véhicule de paquet, une question au pilote (`decision`), un lot de
#: découpage (`file-decoupage:`) — n'entrent ni dans le plafond (comptés ou
#: retenus), ni dans le marqueur d'empreinte inconnue, ni dans le refresh au
#: push. Un lot de la file d'amélioration (`improvement`) est du code, pris
#: par `auto_take` comme un autre (B de cx1 sur 176dc95) : il suit la règle
#: commune. Même règle que `ameesh.work_footprints.is_exempt`.
_EXEMPT = ("(" + _VEHICLE + " OR coalesce({o}.source, '') = 'decision'"
           " OR starts_with(coalesce({o}.source, ''), 'file-decoupage:'))")


def exempt_sql(alias: str) -> str:
    return _EXEMPT.format(o=alias)


def in_flight_sql(alias: str) -> str:
    """Le nombre de lots qui OCCUPENT leur empreinte dans le dépôt du lot
    `alias`, lui et les véhicules de paquet exceptés (#4818, plafond de lots
    en vol)."""
    return ("(SELECT count(*) FROM work_items o"
            "  WHERE coalesce(btrim(" + alias + ".app), '') <> ''"
            "    AND btrim(o.app) = btrim(" + alias + ".app)"
            "    AND o.id <> " + alias + ".id AND " + occupying_sql("o") +
            "    AND NOT " + exempt_sql("o") + ")")


def cap_rows_sql(alias: str) -> str:
    """#4818 : `(cap, in_flight)` quand le dépôt du lot `alias` a atteint son
    plafond de lots en vol (`max_in_flight` de sa politique) ; aucune ligne
    sans plafond ou en dessous."""
    return ("SELECT cp.max_in_flight AS cap, " + in_flight_sql(alias) + " AS in_flight"
            "  FROM repository_footprint_policies cp"
            " WHERE cp.app = btrim(" + alias + ".app)"
            "   AND cp.max_in_flight IS NOT NULL"
            "   AND NOT " + exempt_sql(alias) +
            "   AND " + in_flight_sql(alias) + " >= cp.max_in_flight")


def cap_reached_sql(alias: str) -> str:
    return "EXISTS (" + cap_rows_sql(alias) + ")"


def held_sql(alias: str) -> str:
    """Vrai si la garde retient le lot `alias` : son empreinte chevauche celle
    d'un lot occupant de son dépôt, ou le dépôt a atteint son plafond de lots
    en vol (#4818). Le prédicat commun de la prise, de l'attribution et du
    plan de dispatch."""
    return "(" + overlap_exists_sql(alias) + " OR " + cap_reached_sql(alias) + ")"


def unknown_policy_sql(tx, item_id) -> str:
    """La politique d'empreinte inconnue APPLICABLE au lot, lue dans la
    transaction de l'appelant, qui tient le verrou de la garde : celle de son
    dépôt (`open` sans ligne), mais `open` pour un lot exempté (`_EXEMPT`) et
    pour un lot dont le diff réel est connu — le diff prime sur toute
    estimation (B1 de dev3) — ou qui porte une empreinte manuelle : le
    `--set` prime aussi, une ligne vague de panier n'y ajoute pas le marqueur
    (B de docs-01)."""
    rows = tx.query("SELECT p.unknown_policy FROM work_items w"
                    " JOIN repository_footprint_policies p ON p.app = btrim(w.app)"
                    " WHERE w.id = %s AND NOT " + exempt_sql("w") +
                    "   AND NOT EXISTS (SELECT 1 FROM work_item_footprints d"
                    "                    WHERE d.work_item_id = w.id"
                    "                      AND d.source IN ('manual', 'diff'))",
                    (int(item_id),))
    return str(rows[0]["unknown_policy"]) if rows else "open"


class WorkFootprints(Contract):
    def __init__(self, db):
        self.db = db

    def replace(self, item_id, source, paths, *, actor, head=None, replaces=(),
                unknown=False, unless_manual=False):
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
            if unless_manual and tx.query(
                    "SELECT 1 FROM work_item_footprints WHERE work_item_id = %s"
                    " AND source = 'manual' LIMIT 1", (int(item_id),)):
                # B4 de cx1 : un `--set` posé pendant le calcul prime ; la
                # réestimation devenue caduque n'écrit rien
                return {"paths": self._paths(tx, item_id), "changed": False,
                        "policy": None, "skipped": "manual"}
            policy = None
            if unknown:
                # B3 de cx1 : la politique du dépôt est relue SOUS le verrou de
                # `set_policy` — une bascule entre le calcul et l'écriture
                # n'est jamais perdue, dans un sens comme dans l'autre.
                policy = unknown_policy_sql(tx, item_id)
                if not wanted and policy == "repository":
                    wanted = ["."]
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
                # l'analyse inconnue est résolue par le diff : plus de reprise
                tx.execute("UPDATE work_item_impact_analyses SET status = 'known',"
                           " reason = 'superseded by the branch diff'"
                           " WHERE work_item_id = %s AND status = 'unknown'",
                           (int(item_id),))
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
            return {"paths": after, "changed": changed, "policy": policy}

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
            marked = []
            if after["unknown_policy"] != before["unknown_policy"]:
                marked = self._apply_unknown(tx, str(app), after["unknown_policy"], actor)
            return {"app": str(app), "before": before, "after": after,
                    "changed": before != after, "lots": marked}

    @staticmethod
    def _apply_unknown(tx, app, policy, actor):
        """B3 de cx1 : la politique s'applique à l'état inconnu EFFECTIF, dans
        la transaction et sous le verrou de la garde. `repository` pose le
        marqueur `.` (source `intake`) sur chaque lot ouvert du dépôt sans
        aucune ligne d'empreinte (analyse inconnue sous `open`, ou lot d'avant
        #4818) ou dont l'analyse est inconnue sans empreinte manuelle ni diff
        (panier dont une ligne seule est inconnue, B de dev3), véhicules
        exceptés, et leur ouvre une analyse inconnue que l'exécuteur retente ;
        `open` retire les marqueurs `intake` (un `.`
        posé à la main reste). Une ligne de journal par lot. Rend les lots."""
        if policy == "repository":
            rows = tx.query(
                "SELECT w.id, w.state FROM work_items w"
                " WHERE btrim(w.app) = %s AND w.state NOT IN " + _DONE_SQL +
                "   AND NOT " + exempt_sql("w") +
                "   AND (NOT EXISTS (SELECT 1 FROM work_item_footprints f"
                "                     WHERE f.work_item_id = w.id)"
                "        OR (EXISTS (SELECT 1 FROM work_item_impact_analyses a"
                "                     WHERE a.work_item_id = w.id AND a.status = 'unknown')"
                "            AND NOT EXISTS (SELECT 1 FROM work_item_footprints f"
                "                             WHERE f.work_item_id = w.id"
                "                               AND f.source IN ('manual', 'diff'))))"
                " ORDER BY w.id FOR NO KEY UPDATE OF w", (app,))
            note = ("Footprint (intake): . — unknown footprint held as the whole "
                    "repository (policy repository)")
            for row in rows:
                tx.execute("INSERT INTO work_item_footprints(work_item_id, source, path,"
                           " created_by) VALUES (%s, 'intake', '.', %s)"
                           " ON CONFLICT DO NOTHING", (int(row["id"]), actor))
                # une analyse inconnue datée d'hier : la reprise de l'exécuteur
                # la prend à sa prochaine passe
                tx.execute("INSERT INTO work_item_impact_analyses(work_item_id, status,"
                           " reason, policy, analyzed_by, analyzed_at, attempts)"
                           " VALUES (%s, 'unknown', 'no footprint when the repository"
                           " policy became repository', 'repository', %s,"
                           " clock_timestamp() - interval '1 day', 0)"
                           " ON CONFLICT (work_item_id) DO UPDATE SET status = 'unknown',"
                           " policy = 'repository',"
                           " analyzed_at = clock_timestamp() - interval '1 day'",
                           (int(row["id"]), actor))
        else:
            rows = tx.query(
                "SELECT w.id, w.state FROM work_items w"
                " WHERE btrim(w.app) = %s AND EXISTS (SELECT 1 FROM work_item_footprints f"
                "   WHERE f.work_item_id = w.id AND f.source = 'intake' AND f.path = '.')"
                " ORDER BY w.id FOR NO KEY UPDATE OF w", (app,))
            note = "Footprint (intake): unknown footprint released (policy open)"
            for row in rows:
                tx.execute("DELETE FROM work_item_footprints WHERE work_item_id = %s"
                           " AND source = 'intake' AND path = '.'", (int(row["id"]),))
        for row in rows:
            tx.execute("INSERT INTO work_item_events(work_item_id, state, note, actor,"
                       " created_at) VALUES (%s, %s, %s, %s, clock_timestamp())",
                       (int(row["id"]), row["state"], note, actor))
        return [int(row["id"]) for row in rows]

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
        params: tuple = (float(retry_after_s), BASKET_PREFIX)
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
            # une empreinte manuelle remplace l'estimation, un diff la prime,
            # un lot exempté n'en porte pas : rien à retenter
            "           AND NOT EXISTS (SELECT 1 FROM work_item_footprints m"
            "                            WHERE m.work_item_id = w.id"
            "                              AND m.source IN ('manual', 'diff'))"
            "           AND NOT " + exempt_sql("w") +
            # un panier inconnu : son corps unit ses lignes, le réanalyser
            # perdrait l'inconnue d'une ligne (B de dev3 sur 055b48b)
            "           AND NOT (w.type = 'improvement'"
            "                    AND starts_with(coalesce(w.source, ''), %s))"
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

    def refresh_candidates(self, *, limit, assignees=None, after_id=0):
        app_filter = " AND w.id > %s"
        params: tuple = (int(after_id or 0),)
        if assignees is not None:
            wanted = sorted({str(a).strip() for a in assignees if str(a or "").strip()})
            if not wanted:
                return []
            app_filter += (" AND w.assignee IN (" + ", ".join(["%s"] * len(wanted)) + ")")
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
            "   AND NOT " + exempt_sql("w") +
            "   AND " + occupying_sql("w") + app_filter +
            " ORDER BY w.id LIMIT %s", params + (int(limit),))
