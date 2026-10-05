# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : lots (`work_items`) et leur journal (`work_item_events`).

L29 (0026) : rattachement au plan (`package_id`, `package_parent`), fermeture
sur fusion constatée (`close_merged`) et fermeture explicite (`close`), chacune
en UNE transaction : transition conditionnelle, journal et jalon.

SQL déplacé tel quel depuis `ameesh.work` (lot L1). Chaque transition
laisse une ligne dans `work_item_events` (qui, quand, pourquoi).
"""
from __future__ import annotations

from .. import interface

ITEM_COLUMNS = """
    id, type, source, app, title, body, issue_ref, workstream, state, assignee,
    loops, budget_usd, spent_usd, package_id, package_parent, pr_ref, close_reason,
    superseded_by,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from updated_at)::float8 as updated_ts,
    extract(epoch from closed_at)::float8  as closed_ts
"""


#: Les délais d'un lot, dérivés de ses jalons (L10, R19). Forme **stable** :
#: L24 (`ameesh progress`) et Nexlink la consomment telle quelle ; une étape qui
#: n'a pas eu lieu est NULL, jamais zéro. Le premier gel et le premier verdict
#: font foi ; `verdicts` et `blocked_verdicts` comptent les tours de revue.
_DELAYS_SELECT = """
    SELECT w.id AS work_item_id, w.state, w.assignee, w.title,
           extract(epoch FROM w.created_at)::float8 AS created_ts,
           extract(epoch FROM w.updated_at)::float8 AS updated_ts,
           extract(epoch FROM req.at)::float8 AS requested_ts,
           extract(epoch FROM gel.at)::float8 AS frozen_ts,
           extract(epoch FROM rev.at)::float8 AS reviewed_ts,
           rev.verdict AS reviewed_verdict,
           extract(epoch FROM fus.at)::float8 AS merged_ts,
           extract(epoch FROM (gel.at - req.at))::float8 AS request_to_freeze_s,
           extract(epoch FROM (rev.at - gel.at))::float8 AS freeze_to_review_s,
           extract(epoch FROM (fus.at - gel.at))::float8 AS freeze_to_merge_s,
           extract(epoch FROM (fus.at - rev.at))::float8 AS review_to_merge_s,
           extract(epoch FROM (fus.at - req.at))::float8 AS total_s,
           coalesce(allv.n, 0)::int AS verdicts,
           coalesce(blk.n, 0)::int AS blocked_verdicts
      FROM work_items w
      LEFT JOIN LATERAL (
            SELECT min(m.at) AS at FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'requested') req ON true
      LEFT JOIN LATERAL (
            SELECT min(m.at) AS at FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'frozen') gel ON true
      LEFT JOIN LATERAL (
            SELECT m.at, m.verdict FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'verdict'
             ORDER BY m.at, m.id LIMIT 1) rev ON true
      LEFT JOIN LATERAL (
            SELECT min(m.at) AS at FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'merged') fus ON true
      LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'verdict') allv ON true
      LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'verdict'
               AND m.verdict = 'blocked') blk ON true
"""


class _Refrozen(Exception):
    """Annule la fermeture : un nouveau gel a été validé pendant l'attente."""


class WorkItems(interface.WorkItems):

    def add(self, *, type, source, app, title, body, issue_ref, workstream,  # noqa: A002
            assignee, budget_usd, note, actor, package_id=None, package_parent=None) -> dict:
        """Crée le lot en `intake`, puis sa première ligne de journal."""
        rows = self.db.query(
            """
            INSERT INTO work_items
                (type, source, app, title, body, issue_ref, workstream, assignee,
                 budget_usd, state, package_id, package_parent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'intake', %s, %s)
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", ITEM_COLUMNS),
            (type, source, app, title, body, issue_ref, workstream, assignee,
             budget_usd, package_id, package_parent),
        )
        item = rows[0]
        self._event(int(item["id"]), "intake", note, actor)
        return item

    def get(self, item_id) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM work_items WHERE id = %%s" % ITEM_COLUMNS, (int(item_id),))
        return rows[0] if rows else None

    def items(self, *, state, assignee, limit) -> list[dict]:
        sql = "SELECT %s FROM work_items WHERE true" % ITEM_COLUMNS
        params: list = []
        if state:
            sql += " AND state = %s"
            params.append(state)
        if assignee:
            sql += " AND assignee = %s"
            params.append(assignee)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    def move(self, item_id, state, *, current, loops, note, actor) -> dict | None:
        """Déplace le lot s'il est encore en `current` (sinon None, rien
        d'écrit), puis journalise la transition."""
        rows = self.db.query(
            """
            UPDATE work_items
               SET state = %s,
                   loops = loops + %s,
                   updated_at = now(),
                   closed_at = CASE WHEN %s = 'promoted' THEN now() ELSE NULL END
             WHERE id = %s AND state = %s
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", ITEM_COLUMNS),
            (state, loops, state, int(item_id), current),
        )
        if not rows:
            return None
        self._event(int(item_id), state, note, actor)
        return rows[0]

    def note(self, item_id, state, text, actor) -> None:
        self._event(int(item_id), state, text, actor)
        self.db.execute("UPDATE work_items SET updated_at = now() WHERE id = %s",
                        (int(item_id),))

    def events(self, item_id, limit) -> list[dict]:
        return self.db.query(
            """
            SELECT id, work_item_id, state, note, actor,
                   extract(epoch from created_at)::float8 as created_ts
              FROM work_item_events
             WHERE work_item_id = %s
             ORDER BY id DESC LIMIT %s
            """,
            (int(item_id), int(limit)),
        )

    def milestones(self, item_id, limit) -> list[dict]:
        return self.db.query(
            """
            SELECT id, work_item_id, kind, sha, actor, verdict, note,
                   extract(epoch from at)::float8 as at_ts, at
              FROM work_item_milestones
             WHERE work_item_id = %s
             ORDER BY at DESC, id DESC LIMIT %s
            """,
            (int(item_id), int(limit)),
        )

    def add_milestone(self, item_id, kind, *, sha, actor, verdict, note) -> dict:
        """Écrit le jalon sous le verrou de la ligne du lot (L29) : un gel ne
        s'insère jamais pendant qu'une fermeture sur fusion tient ce lot."""
        with self.db.transaction() as tx:
            tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                     (int(item_id),))
            rows = tx.query(
                """
                INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, verdict, note)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, work_item_id, kind, sha, actor, verdict, note,
                          extract(epoch from at)::float8 as at_ts, at
                """,
                (int(item_id), kind, sha or "", actor or "", verdict, note or ""),
            )
            return rows[0]

    def delays(self, limit, *, ids=None) -> list[dict]:
        if ids is not None:
            wanted = sorted({int(value) for value in ids})
            if not wanted:
                return []
            marks = ", ".join(["%s"] * len(wanted))
            return self.db.query(_DELAYS_SELECT + " WHERE w.id IN (%s)" % marks,
                                 tuple(wanted))
        return self.db.query(
            _DELAYS_SELECT + " ORDER BY w.updated_at DESC, w.id DESC LIMIT %s",
            (int(limit),),
        )

    def timeline(self, item_id) -> dict | None:
        rows = self.db.query(_DELAYS_SELECT + " WHERE w.id = %s", (int(item_id),))
        return rows[0] if rows else None

    def link_package(self, item_id, package_id, parent, *, note, actor) -> dict | None:
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_items SET package_id = %%s, package_parent = %%s, updated_at = now()"
                " WHERE id = %%s RETURNING %s" % ITEM_COLUMNS,
                (package_id, parent, int(item_id)))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    def by_package(self, package_id) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM work_items WHERE package_id = %%s ORDER BY id" % ITEM_COLUMNS,
            (package_id,))

    def close_merged(self, item_id, *, current, sha, actor, note, pr_ref,
                     frozen_id=None) -> dict | None:
        """`current` → `merged` si le lot y est encore (sinon None, rien d'écrit),
        en UNE transaction : transition, journal, puis le jalon `merged` (posé
        par le trigger de 0013) reçoit le commit et l'auteur de la fusion.

        `frozen_id` (revue codex1, P2-1) : le gel examiné doit être le dernier
        gel du lot. L'UPDATE prend d'abord le verrou de la ligne du lot (en
        l'attendant au besoin) ; le dernier gel est relu APRÈS, dans une
        instruction suivante de la même transaction — en READ COMMITTED elle
        voit tout gel validé pendant l'attente (le sous-select d'une seule
        instruction verrait l'instantané d'avant l'attente). S'il a changé,
        la transaction est annulée (None). Un gel ne peut pas s'insérer
        ensuite : `add_milestone` prend le même verrou de ligne."""
        try:
            with self.db.transaction() as tx:
                rows = tx.query(
                    """
                    UPDATE work_items
                       SET state = 'merged', updated_at = now(), closed_at = NULL,
                           pr_ref = coalesce(%s, pr_ref)
                     WHERE id = %s AND state = %s
                    RETURNING __COLUMNS__
                    """.replace("__COLUMNS__", ITEM_COLUMNS),
                    (pr_ref, int(item_id), current))
                if not rows:
                    return None
                if frozen_id is not None:
                    latest = tx.query(
                        "SELECT id FROM work_item_milestones"
                        " WHERE work_item_id = %s AND kind = 'frozen'"
                        " ORDER BY at DESC, id DESC LIMIT 1", (int(item_id),))
                    if not latest or int(latest[0]["id"]) != int(frozen_id):
                        raise _Refrozen()
                WorkItems(tx)._event(int(item_id), "merged", note, actor)
                tx.query(
                    "UPDATE work_item_milestones SET sha = %s, actor = %s, note = %s"
                    " WHERE work_item_id = %s AND kind = 'merged' AND sha = '' RETURNING id",
                    (sha or "", actor or "", note or "", int(item_id)))
                return rows[0]
        except _Refrozen:
            return None

    def close(self, item_id, *, current, reason, superseded_by, actor, note) -> dict | None:
        """`current` → `closed` (abandon ou remplacement) si le lot y est encore ;
        journal et jalon `closed` dans la même transaction."""
        with self.db.transaction() as tx:
            rows = tx.query(
                """
                UPDATE work_items
                   SET state = 'closed', close_reason = %s, superseded_by = %s,
                       updated_at = now(), closed_at = now()
                 WHERE id = %s AND state = %s
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", ITEM_COLUMNS),
                (reason, superseded_by, int(item_id), current))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), "closed", note, actor)
            tx.query(
                "INSERT INTO work_item_milestones (work_item_id, kind, actor, note)"
                " VALUES (%s, 'closed', %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (int(item_id), actor or "", note or ""))
            return rows[0]

    def refresh_package_parents(self) -> int:
        rows = self.db.query(
            """
            UPDATE work_items w SET package_parent = p.parent
              FROM work_packages p
             WHERE w.package_id = p.id AND w.package_parent IS DISTINCT FROM p.parent
            RETURNING w.id
            """)
        return len(rows)

    def _event(self, item_id, state, note, actor) -> None:
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor) "
            "VALUES (%s, %s, %s, %s)",
            (item_id, state, note or "", actor or ""),
        )
