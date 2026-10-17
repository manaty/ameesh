# SPDX-License-Identifier: AGPL-3.0-only
"""L385: graph edits and audit records on the same Postgres transaction."""
from __future__ import annotations

from ..work_dependencies import WorkDependencies as Contract

_DONE = ("merged", "promoted", "closed")
_DONE_SQL = "('merged', 'promoted', 'closed')"
_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':work_dependencies', 0))"


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
