# SPDX-License-Identifier: AGPL-3.0-only
"""L385: graph edits and audit records on the same Postgres transaction."""
from __future__ import annotations

from ..work_dependencies import WorkDependencies as Contract
from ... import work_lifecycle as lifecycle

_DONE = lifecycle.COMPLETED
_DONE_SQL = f"('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
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

    Seul point de lecture de cette règle : les lots actionnables du relevé et
    les propositions de relecture s'y appuient, pour qu'un lot ne soit jamais
    relu sur un verdict périmé (#1677 B1). La prise (L165) ne s'en sert plus :
    depuis #2252, seule une fusion satisfait une dépendance."""
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


def freeze_due(alias: str) -> str:
    """#3986: author owes a declared freeze unless review is explicitly waived.

    A QA transition alone is not a declared freeze. Without a freeze the
    class resolver cannot certify a light lot, so this keeps its cautious
    default. The pilot's explicit review waiver still takes precedence.
    """
    return ("(NOT EXISTS (SELECT 1 FROM work_item_milestones m"
            " WHERE m.work_item_id = {a}.id AND m.kind = 'frozen')"
            " AND coalesce(to_jsonb({a})->>'reviewer', '') <> '-none')").format(a=alias)


def not_blocking(alias: str) -> str:
    """#2252 : prédicat SQL « ce lot ne bloque plus » — il est FUSIONNÉ
    (`merged`/`promoted`/`closed`). Un lot en `qa`, même à verdict courant
    favorable et à tête relue, ne libère plus ses dépendants.

    #1677 avait admis le `qa` à verdict courant favorable comme satisfait, le
    dépendant s'empilant sur la tête gelée. Le 17/10, #661 a reçu un `ok` à
    13:45:52, #667 a été pris à 13:45:57, puis #661 a reçu un `blocked` à
    13:46:07 : l'agent s'est retrouvé sur une base qui venait de changer. La
    règle voulue par #2252 est celle de `work show` et de « planned: waiting
    on lots » — une dépendance n'est satisfaite que fusionnée —, pour qu'aucun
    lot ne soit pris sur une base instable. L'inverse exact du `blocking` rendu
    par `for_items` et lu par la prise (`unassigned_lots`, `assign ready_only`).
    """
    return "(%s.state IN %s)" % (alias, _DONE_SQL)


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
                   " SELECT id, CASE WHEN state = 'delivered' THEN coalesce(to_jsonb(work_items)->>'legacy_state', state) ELSE state END, %s, %s, clock_timestamp() FROM work_items WHERE id = %s",
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
        # #4621 : la cause dependency-blocking nomme la phase de livraison
        # (`merged`/`promoted`), comme les états de policy, pas le core
        # `delivered`. Le prédicat `blocking` garde sa définition (lifecycle
        # DELIVERED) : seul l'affichage change.
        return self.db.query("""
            SELECT d.work_item_id, d.blocker_id, d.created_by,
                   extract(epoch FROM d.created_at)::float8 AS created_ts,
                   coalesce(to_jsonb(w)->>'legacy_state', w.state) AS blocker_state,
                   w.branch AS blocker_branch,
                   (SELECT m.sha FROM work_item_milestones m
                     WHERE m.work_item_id = w.id AND m.kind = 'frozen'
                     ORDER BY m.at DESC, m.id DESC LIMIT 1) AS blocker_frozen_sha,
                   NOT """ + not_blocking("w") + """ AS blocking
            FROM work_item_dependencies d JOIN work_items w ON w.id = d.blocker_id
            WHERE d.work_item_id IN (""" + ", ".join(["%s"] * len(ids)) + ")"
            " ORDER BY d.work_item_id, d.blocker_id", tuple(ids))

    # ------------------------------------------------------------------
    # #4167 : liens typés (duplicates, supersedes, part_of, conflicts_with)
    # ------------------------------------------------------------------

    @staticmethod
    def _link_ends(item_id, other_id, kind):
        """`conflicts_with` est symétrique : rangé plus petit id d'abord."""
        if kind == "conflicts_with" and item_id > other_id:
            return other_id, item_id
        return item_id, other_id

    def add_link(self, item_id, other_id, kind, *, actor):
        if item_id == other_id:
            return {"error": "self_link"}
        with self.db.transaction() as tx:
            # même verrou que le graphe des prérequis : une passe qui lit les
            # deux graphes ne voit jamais une édition à moitié faite
            tx.execute(_LOCK)
            rows = tx.query("SELECT id, state FROM work_items WHERE id IN (%s, %s)"
                            " ORDER BY id FOR UPDATE", (item_id, other_id))
            states = {int(row["id"]): row["state"] for row in rows}
            if item_id not in states or other_id not in states:
                return {"error": "unknown_lot"}
            a, b = self._link_ends(item_id, other_id, kind)
            if tx.query("SELECT 1 FROM work_item_links WHERE work_item_id = %s"
                        " AND other_id = %s AND kind = %s", (a, b, kind)):
                return {"added": False}
            if kind == "conflicts_with":
                # un conflit avec un lot fini n'a plus rien à trancher
                if states[item_id] in _DONE or states[other_id] in _DONE:
                    return {"error": "finished_lot"}
            elif tx.query("""
                    WITH RECURSIVE reach(id) AS (
                        SELECT other_id FROM work_item_links
                         WHERE work_item_id = %s AND kind = %s
                        UNION
                        SELECT l.other_id FROM work_item_links l JOIN reach r
                            ON l.work_item_id = r.id AND l.kind = %s
                    ) SELECT 1 FROM reach WHERE id = %s LIMIT 1
                    """, (other_id, kind, kind, item_id)):
                return {"error": "link_cycle"}
            tx.execute("INSERT INTO work_item_links(work_item_id, other_id, kind, created_by)"
                       " VALUES (%s, %s, %s, %s)", (a, b, kind, actor))
            self._landing_journal(tx, item_id, "Link added: %s lot #%d" % (kind, other_id),
                                  actor)
            return {"added": True}

    def remove_link(self, item_id, other_id, kind, *, actor):
        a, b = self._link_ends(item_id, other_id, kind)
        with self.db.transaction() as tx:
            tx.execute(_LOCK)
            tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE", (item_id,))
            removed = tx.query("DELETE FROM work_item_links WHERE work_item_id = %s"
                               " AND other_id = %s AND kind = %s RETURNING kind",
                               (a, b, kind))
            if not removed:
                return False
            self._landing_journal(tx, item_id, "Link removed: %s lot #%d" % (kind, other_id),
                                  actor)
            return True

    def decide_link(self, item_id, other_id, *, decision, actor):
        a, b = self._link_ends(item_id, other_id, "conflicts_with")
        with self.db.transaction() as tx:
            tx.execute(_LOCK)
            tx.query("SELECT id FROM work_items WHERE id IN (%s, %s) ORDER BY id FOR UPDATE",
                     (a, b))
            rows = tx.query("SELECT decision FROM work_item_links WHERE work_item_id = %s"
                            " AND other_id = %s AND kind = 'conflicts_with' FOR UPDATE",
                            (a, b))
            if not rows:
                return {"error": "no_link"}
            if rows[0].get("decision") == decision:
                return {"decided": False}
            tx.execute("UPDATE work_item_links SET decision = %s, decided_by = %s,"
                       " decided_at = clock_timestamp() WHERE work_item_id = %s"
                       " AND other_id = %s AND kind = 'conflicts_with'",
                       (decision, actor, a, b))
            for lot, peer in ((a, b), (b, a)):
                self._landing_journal(tx, lot, "Conflict with lot #%d decided: %s"
                                      % (peer, decision), actor)
            return {"decided": True}

    _LINK_COLUMNS = """
            l.work_item_id, l.other_id, l.kind, l.created_by,
            extract(epoch FROM l.created_at)::float8 AS created_ts,
            l.decision, l.decided_by,
            extract(epoch FROM l.decided_at)::float8 AS decided_ts,
            a.state AS item_state, b.state AS other_state,
            (a.state NOT IN """ + _DONE_SQL + " AND b.state NOT IN " + _DONE_SQL + """) AS open
            """

    def links_for(self, item_ids):
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        marks = ", ".join(["%s"] * len(ids))
        return self.db.query(
            "SELECT" + self._LINK_COLUMNS +
            " FROM work_item_links l JOIN work_items a ON a.id = l.work_item_id"
            " JOIN work_items b ON b.id = l.other_id"
            " WHERE l.work_item_id IN (" + marks + ") OR l.other_id IN (" + marks + ")"
            " ORDER BY l.work_item_id, l.other_id, l.kind", tuple(ids) * 2)

    def undecided_conflicts(self):
        return self.db.query(
            "SELECT" + self._LINK_COLUMNS + ","
            " a.assignee AS item_assignee, b.assignee AS other_assignee,"
            " a.app AS item_app, a.title AS item_title, b.title AS other_title"
            " FROM work_item_links l JOIN work_items a ON a.id = l.work_item_id"
            " JOIN work_items b ON b.id = l.other_id"
            " WHERE l.kind = 'conflicts_with' AND l.decision IS NULL"
            " AND a.state NOT IN " + _DONE_SQL + " AND b.state NOT IN " + _DONE_SQL +
            " ORDER BY l.work_item_id, l.other_id")

    @staticmethod
    def _landing_journal(tx, item_id, note, actor):
        tx.execute("INSERT INTO work_item_events(work_item_id, state, note, actor, created_at)"
                   " SELECT id, CASE WHEN state = 'delivered' THEN coalesce(to_jsonb(work_items)->>'legacy_state', state) ELSE state END, %s, %s, clock_timestamp() FROM work_items WHERE id = %s",
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
            if states[item_id] not in lifecycle.TO_VALIDATE:
                return {"error": "not_in_review"}
            if states[vehicle_id] in _DONE:
                return {"error": "finished_vehicle"}
            if states[vehicle_id] == lifecycle.BLOCKED:
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

    def landings_on_vehicle(self, vehicle_id):
        """#3641 : les déclarations épinglées à ce véhicule (lecture groupée)."""
        return self.db.query("""
            SELECT work_item_id, vehicle_id, pinned_sha,
                   until_on::text AS until_on, created_by
              FROM work_landings
             WHERE vehicle_id = %s
             ORDER BY work_item_id""", (int(vehicle_id),))

    @staticmethod
    def _current_freeze(tx, vehicle_id):
        """#3641 : le gel courant du véhicule, sous le verrou des landings.

        Même règle que `set_landing` et `planned_waits.latest_freeze` : le
        dernier jalon `frozen` non vide, départagé par `(at, id)`. Rend `''`
        sans gel."""
        rows = tx.query("""
            SELECT sha FROM work_item_milestones
             WHERE work_item_id = %s AND kind = 'frozen'
               AND btrim(coalesce(sha, '')) <> ''
             ORDER BY at DESC, id DESC LIMIT 1""", (int(vehicle_id),))
        return (rows[0]["sha"] or "").strip() if rows else ""

    def landing_matches(self, item_id, vehicle_id, pinned_sha, *, frozen_sha):
        """#3641 : la déclaration est-elle encore celle du gel `frozen_sha` ?

        Revalidation sous le verrou des landings, avant de prévenir le
        déclarant : une déclaration renouvelée depuis, ou un gel plus récent,
        ne reçoit pas d'avis pour un gel retardé."""
        with self.db.transaction() as tx:
            tx.execute(_LOCK)
            tx.execute("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                       (int(vehicle_id),))
            if self._current_freeze(tx, vehicle_id) != frozen_sha:
                return False
            rows = tx.query(
                "SELECT pinned_sha FROM work_landings"
                " WHERE work_item_id = %s AND vehicle_id = %s",
                (int(item_id), int(vehicle_id)))
            return bool(rows) and (rows[0]["pinned_sha"] or "").strip() == pinned_sha

    def landings_for(self, item_ids):
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        return self.db.query(f"""
            SELECT l.work_item_id, l.vehicle_id, l.pinned_sha,
                   l.until_on::text AS until_on,
                   l.created_by,
                   extract(epoch FROM l.created_at)::float8 AS created_ts,
                   w.state AS vehicle_state,
                   w.state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}') AS open
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
            f"UPDATE work_items SET state = '{lifecycle.REQUESTED}', updated_at = clock_timestamp()"
            " WHERE id IN (" + ", ".join(["%s"] * len(ids)) + ")"
            f" AND state = '{lifecycle.BLOCKED}' AND assignee IS NULL"
            " AND " + _UNPINNED + " RETURNING id", tuple(ids))
        for row in rows:
            self.db.execute(
                "INSERT INTO work_item_events(work_item_id, state, note, actor, created_at)"
                " VALUES (%s, 'intake', %s, %s, clock_timestamp())",
                (int(row["id"]),
                 "Dependency released: back to intake (no assignee, #1668)", actor or ""))
        return [int(row["id"]) for row in rows]
