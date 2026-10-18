# SPDX-License-Identifier: AGPL-3.0-only
"""L385: graph edits and audit records on the same Postgres transaction."""
from __future__ import annotations

from ..work_dependencies import WorkDependencies as Contract

_DONE = ("merged", "promoted", "closed")
_DONE_SQL = "('merged', 'promoted', 'closed')"
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':work_dependencies', 0))"

#: #1922 : un lot pinné sur une révision Process ne bouge que par le registre
#: (`process_registry`), qui vérifie la garde serveur avant d'écrire. Les
#: écritures historiques d'état filtrent donc la colonne DANS le SQL : une
#: adoption concurrente (qui garde l'état) ne laisse plus passer un move
#: legacy entre la lecture et l'UPDATE (N8 de la relecture Claude). La lecture
#: par `to_jsonb` rend `''` sur une base d'avant 0452, sans casser.
_UNPINNED = "coalesce(to_jsonb(work_items)->>'workflow_ref', '') = ''"


def _last_milestone(alias: str, kind: str, column: str) -> str:
    """La `column` du dernier jalon `kind` de ce lot (NULL s'il n'y en a pas)."""
    return ("(SELECT m.%s FROM work_item_milestones m"
            "  WHERE m.work_item_id = %s.id AND m.kind = '%s'"
            "  ORDER BY m.at DESC, m.id DESC LIMIT 1)" % (column, alias, kind))


def last_verdict(alias: str) -> str:
    """#1677 : le DERNIER verdict d'un lot (`ok`/`blocked`/NULL), quel que soit
    le gel qu'il nomme. Pour la règle de relecture, voir `current_verdict`."""
    return _last_milestone(alias, "verdict", "verdict")


def last_verdict_sha(alias: str) -> str:
    """Le sha nommé par le dernier verdict (`''` quand il n'en nomme aucun)."""
    return _last_milestone(alias, "verdict", "sha")


def last_freeze_sha(alias: str) -> str:
    """Le sha nommé par le dernier gel (`''` quand il n'en nomme aucun)."""
    return _last_milestone(alias, "frozen", "sha")


#: #1677 B1 : le verdict n'est retenu que s'il NOMME le dernier gel — la règle
#: de `progress._verdict_applies` et de `merge_queue` (un ok d'une tête
#: jamais regelée ne vaut plus). Le préfixe est admis (`progress._same_commit`,
#: casse ignorée) ; quand un sha manque d'un côté, on retombe sur les instants.
def current_verdict(alias: str) -> str:
    """SQL : le verdict RETENU d'un lot (`ok`/`blocked`/NULL), c'est-à-dire le
    dernier verdict s'il porte sur le dernier gel — un lot regelé après un `ok`
    repasse en relecture, un lot regelé après un `blocked` n'attend plus de
    correction. NULL quand le verdict ne porte pas sur le dernier gel (ou
    qu'il n'y en a pas).

    Seul point de lecture de cette règle : `not_blocking`, `reviewed_head`, la
    prise (`assign`, `unassigned_lots`), les lots actionnables du relevé et les
    propositions de relecture s'y appuient, pour qu'un lot ne soit jamais pris
    sur un verdict périmé (#1677 B1)."""
    return (
        "(SELECT CASE"
        "   WHEN v.verdict IS NULL THEN NULL"
        "   WHEN f.sha IS NULL THEN v.verdict"
        "   WHEN btrim(v.sha) <> '' AND btrim(f.sha) <> '' THEN"
        "        CASE WHEN lower(btrim(v.sha)) = lower(btrim(f.sha))"
        "               OR starts_with(lower(btrim(v.sha)), lower(btrim(f.sha)))"
        "               OR starts_with(lower(btrim(f.sha)), lower(btrim(v.sha)))"
        "             THEN v.verdict END"
        "   WHEN v.at >= f.at THEN v.verdict END"
        "   FROM (SELECT m.verdict, m.sha, m.at FROM work_item_milestones m"
        "          WHERE m.work_item_id = %s.id AND m.kind = 'verdict'"
        "          ORDER BY m.at DESC, m.id DESC LIMIT 1) v"
        "   LEFT JOIN LATERAL (SELECT m.sha, m.at FROM work_item_milestones m"
        "          WHERE m.work_item_id = %s.id AND m.kind = 'frozen'"
        "          ORDER BY m.at DESC, m.id DESC LIMIT 1) f ON true)"
        % (alias, alias))


def reviewed_head(alias: str) -> str:
    """SQL : la tête relue d'un lot — le sha nommé par son verdict courant, à
    défaut celui de son dernier gel ; NULL quand aucun ne nomme de commit.

    #1677 B2 : c'est la base d'empilement d'un dépendant. Un bloqueur en `qa`
    dont le verdict favorable ne nomme aucune tête (ni gel) n'offre aucune base
    et reste donc bloquant : `not_blocking` exige une tête relue."""
    return ("coalesce(nullif(btrim(%s), ''), nullif(btrim(%s), ''))"
            % (last_verdict_sha(alias), last_freeze_sha(alias)))


def not_blocking(alias: str) -> str:
    """#1677 : prédicat SQL « ce lot ne bloque plus » — terminé, ou en `qa`
    avec un verdict courant favorable ET une tête relue sur laquelle le
    dépendant s'empile (B2). L'inverse exact du `blocking` rendu par
    `for_items` et lu par la prise (`unassigned_lots`, `assign ready_only`)."""
    # coalesce : sans verdict, `current_verdict` vaut NULL et la comparaison
    # vaudrait NULL — un `NOT NULL` en SQL ne rend ni vrai ni faux, et la
    # sous-requête `EXISTS` laisserait alors passer le dépendant (#1677).
    return ("(%s.state IN %s OR (%s.state = 'qa' AND coalesce(%s, '') = 'ok'"
            " AND %s IS NOT NULL))"
            % (alias, _DONE_SQL, alias, current_verdict(alias),
               reviewed_head(alias)))


class WorkDependencies(Contract):
    def __init__(self, db):
        self.db = db

    def lock(self):
        # Les fermetures de lot appellent ce verrou avec la TRANSACTION
        # (`work_dependencies.lock(tx)`, work.py) : il tient jusqu'au commit.
        # #2171 N1 : les gestes d'atterrissage ci-dessous, eux, tenaient `db`
        # (le pilote) et non `tx` — ils utilisent donc `tx.execute(_LOCK)`.
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
                   w.branch AS blocker_branch,
                   (SELECT m.sha FROM work_item_milestones m
                     WHERE m.work_item_id = w.id AND m.kind = 'frozen'
                     ORDER BY m.at DESC, m.id DESC LIMIT 1) AS blocker_frozen_sha,
                   NOT """ + not_blocking("w") + """ AS blocking
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
            # #2171 N1 : le verrou est celui de la TRANSACTION. `self.db.execute`
            # le posait dans une autre session (pilote psql, autocommit) : il
            # était relâché aussitôt, et le « même verrou que le graphe » était
            # faux.
            tx.execute(_LOCK)
            rows = tx.query("SELECT id, state FROM work_items WHERE id IN (%s, %s)"
                            " ORDER BY id FOR UPDATE", (item_id, vehicle_id))
            states = {int(row["id"]): row["state"] for row in rows}
            if item_id not in states or vehicle_id not in states:
                return {"error": "unknown_lot"}
            if states[item_id] in _DONE:
                return {"error": "finished_lot"}
            # #2171 N3 : seul un lot en revue peut être parqué ; un lot en
            # build, bloqué ou en attente d'un humain garde son signal.
            if states[item_id] != "qa":
                return {"error": "not_in_review"}
            if states[vehicle_id] in _DONE:
                return {"error": "finished_vehicle"}
            if states[vehicle_id] == "blocked":
                return {"error": "blocked_vehicle"}
            # #1945, revue B : l'admission est ÉPINGLÉE au gel du véhicule. La
            # tête est lue dans la même transaction que l'écriture ; sans gel,
            # rien ne peut être épinglé et la déclaration est refusée.
            # #2171 N2 : à instants égaux, le dernier gel est le (at, id) le plus
            # grand — même règle que planned_waits.latest_freeze.
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
            tx.execute(_LOCK)                 # #2171 N1 : même verrou, dans la transaction
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
