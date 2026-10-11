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
from . import catalog as _catalog

ITEM_COLUMNS = """
    id, type, source, app, title, body, issue_ref, workstream, state, assignee,
    loops, budget_usd, spent_usd, package_id, package_parent, pr_ref, close_reason,
    superseded_by, delegated_by,
    expected_value, value_score, priority, team, required_capabilities,
    branch, branch_target, branch_head,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from updated_at)::float8 as updated_ts,
    extract(epoch from closed_at)::float8  as closed_ts,
    extract(epoch from delegated_at)::float8 as delegated_ts,
    extract(epoch from due_at)::float8 as due_ts
"""

#: états de lot terminés (une délégation n'y a plus d'objet)
_CLOSED = "('merged', 'promoted', 'closed')"

#: L40 (0030) : preuve que le délégué `d.delegate` a travaillé sur le lot
#: depuis le début de la délégation — un tour noté par l'exécuteur
#: (`first_turn_at`), une transition ou note du lot, un jalon, une action
#: proposée ou un message lié au lot (`--lot`) de sa part. L'acteur est
#: comparé avec et sans le préfixe `agent:` (forme des actions).
_WORKED = """(
    d.first_turn_at IS NOT NULL
    OR EXISTS (SELECT 1 FROM work_item_events e
                WHERE e.work_item_id = d.work_item_id
                  AND e.actor IN (d.delegate, 'agent:' || d.delegate)
                  AND e.created_at >= d.delegated_at)
    OR EXISTS (SELECT 1 FROM work_item_milestones m
                WHERE m.work_item_id = d.work_item_id
                  AND m.actor IN (d.delegate, 'agent:' || d.delegate)
                  AND m.at >= d.delegated_at)
    OR EXISTS (SELECT 1 FROM actions a
                WHERE a.work_item = d.work_item_id
                  AND a.proposed_by = 'agent:' || d.delegate
                  AND a.created_at >= d.delegated_at)
    OR EXISTS (SELECT 1 FROM agent_mailbox b
                WHERE btrim(b.work_item_id) = d.work_item_id::text
                  AND b.sender = d.delegate
                  AND b.created_at >= d.delegated_at)
)"""

#: colonnes d'une délégation (registre `work_item_delegations`)
_DELEGATION_COLUMNS = """
    d.id AS delegation_id, d.work_item_id, d.delegate, d.delegated_by, d.outcome,
    d.resolved_by,
    extract(epoch from d.delegated_at)::float8  AS delegated_ts,
    extract(epoch from d.due_at)::float8        AS due_ts,
    extract(epoch from d.first_turn_at)::float8 AS first_turn_ts,
    extract(epoch from d.resolved_at)::float8   AS resolved_ts
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
            assignee, budget_usd, note, actor, package_id=None, package_parent=None,
            branch=None, branch_target=None, priority=None) -> dict:
        """Crée le lot en `intake`, puis sa première ligne de journal."""
        rows = self.db.query(
            """
            INSERT INTO work_items
                (type, source, app, title, body, issue_ref, workstream, assignee,
                 budget_usd, state, package_id, package_parent, branch, branch_target,
                 priority)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'intake', %s, %s, %s, %s, %s)
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", ITEM_COLUMNS),
            (type, source, app, title, body, issue_ref, workstream, assignee,
             budget_usd, package_id, package_parent, branch, branch_target,
             None if priority is None else int(priority)),
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

    def assign(self, item_id, assignee, *, current, note, actor) -> dict | None:
        """Réassignation (L37, 0030) : conditionnelle à l'assigné lu par
        l'appelant (`current`, NULL compris) et à un lot encore ouvert ; la
        ligne de journal part dans la même transaction. L40 : la délégation
        en cours est effacée (issue `annulee` au registre)."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_items SET assignee = %%s, updated_at = now(),"
                "       delegated_by = NULL, delegated_at = NULL, due_at = NULL"
                " WHERE id = %%s AND assignee IS NOT DISTINCT FROM %%s"
                "   AND state NOT IN ('merged', 'promoted', 'closed')"
                " RETURNING %s" % ITEM_COLUMNS,
                (assignee, int(item_id), current))
            if not rows:
                return None
            tx.query(
                "UPDATE work_item_delegations SET outcome = 'annulee', resolved_at = now(),"
                "       resolved_by = %s"
                " WHERE work_item_id = %s AND outcome IS NULL RETURNING id",
                (actor or "", int(item_id)))
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    # -- délégation à échéance (L40, 0030 point 5) ---------------------------
    def delegate(self, item_id, delegate, *, current, delegated_by, within_s, note,
                 actor) -> dict | None:
        """Délègue le lot en UNE transaction, s'il a encore l'assigné `current`
        et n'est pas fermé : la délégation en cours est remplacée (issue
        `remplacee`), le lot passe au délégué avec délégant, début et
        échéance, le registre reçoit la nouvelle ligne, le journal la note.
        Rend `{item, delegation, replaced}` ou None (lot modifié entre-temps)."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT id FROM work_items WHERE id = %%s"
                "   AND assignee IS NOT DISTINCT FROM %%s AND state NOT IN %s"
                " FOR NO KEY UPDATE" % _CLOSED, (int(item_id), current))
            if not rows:
                return None
            replaced = tx.query(
                "UPDATE work_item_delegations SET outcome = 'remplacee', resolved_at = now(),"
                "       resolved_by = %s"
                " WHERE work_item_id = %s AND outcome IS NULL"
                " RETURNING id, delegate, delegated_by,"
                "           extract(epoch from due_at)::float8 AS due_ts",
                (actor or "", int(item_id)))
            rows = tx.query(
                "UPDATE work_items SET assignee = %%s, delegated_by = %%s, delegated_at = now(),"
                "       due_at = now() + make_interval(secs => %%s), updated_at = now()"
                " WHERE id = %%s RETURNING %s" % ITEM_COLUMNS,
                (delegate, delegated_by, float(within_s), int(item_id)))
            item = rows[0]
            delegation = tx.query(
                "INSERT INTO work_item_delegations AS d"
                "       (work_item_id, delegate, delegated_by, delegated_at, due_at)"
                " SELECT id, %%s, %%s, delegated_at, due_at FROM work_items WHERE id = %%s"
                " RETURNING %s" % _DELEGATION_COLUMNS,
                (delegate, delegated_by, int(item_id)))[0]
            WorkItems(tx)._event(int(item_id), item["state"], note, actor)
            return {"item": item, "delegation": delegation,
                    "replaced": replaced[0] if replaced else None}

    def current_delegation(self, item_id) -> dict | None:
        rows = self.db.query(
            "SELECT %s, %s AS worked FROM work_item_delegations d"
            " WHERE d.work_item_id = %%s AND d.outcome IS NULL" % (_DELEGATION_COLUMNS, _WORKED),
            (int(item_id),))
        return rows[0] if rows else None

    def mark_delegate_turn(self, agent, item_ids, note) -> list[int]:
        """Un tour de `agent` commence sur ces lots : la délégation en cours
        qui lui est confiée reçoit son premier tour (une seule fois), et le
        journal du lot le note (acteur : le délégué), en UNE transaction (le
        pilote psql n'accepte pas un WITH qui écrit, imbriqué)."""
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_item_delegations SET first_turn_at = now()"
                " WHERE outcome IS NULL AND first_turn_at IS NULL"
                "   AND delegate = %%s AND work_item_id IN (%s)"
                " RETURNING work_item_id" % ", ".join(["%s"] * len(ids)),
                tuple([agent] + ids))
            marques = sorted(int(r["work_item_id"]) for r in rows)
            if marques:
                tx.query(
                    "INSERT INTO work_item_events (work_item_id, state, note, actor)"
                    " SELECT id, state, %%s, %%s FROM work_items WHERE id IN (%s)"
                    " RETURNING id" % ", ".join(["%s"] * len(marques)),
                    tuple([note, agent] + marques))
            return marques

    def due_delegations(self, now_ts) -> list[dict]:
        """Délégations en cours dont l'échéance est passée à `now_ts`, avec
        l'état du lot et la preuve de travail du délégué (`worked`)."""
        return self.db.query(
            "SELECT %s, %s AS worked, w.title, w.state, w.assignee"
            "  FROM work_item_delegations d JOIN work_items w ON w.id = d.work_item_id"
            " WHERE d.outcome IS NULL AND d.due_at < to_timestamp(%%s)"
            " ORDER BY d.due_at, d.id" % (_DELEGATION_COLUMNS, _WORKED),
            (float(now_ts),))

    def resolve_delegation(self, delegation_id, *, now_ts, actor, describe) -> dict | None:
        """Traite UNE délégation échue, en UNE transaction : verrou des lignes
        du lot et de la délégation (`NO KEY UPDATE` : n'attend pas une
        insertion au journal, qui ne prend que `KEY SHARE` sur le lot ; un
        premier tour en cours d'écriture est attendu, puis vu), relecture de
        la délégation (encore en cours, échue) et de la preuve de travail sous
        ce verrou. Issue :

        * `annulee` : lot fermé, ou assigné qui n'est plus le délégué —
          l'échéance est effacée, rien d'autre ;
        * `soldee` : le délégué a travaillé — l'échéance est effacée ;
        * `rendue` : aucun travail — le lot revient au délégant, délégation
          effacée.

        `describe(issue, délégation)` rend la ligne de journal de l'issue (ou
        rien), écrite dans la même transaction. None si un autre exécuteur
        l'a déjà traitée : jamais deux retours."""
        with self.db.transaction() as tx:
            lot = tx.query(
                "SELECT w.id FROM work_items w JOIN work_item_delegations d"
                "    ON d.work_item_id = w.id WHERE d.id = %s FOR NO KEY UPDATE OF w, d",
                (int(delegation_id),))
            if not lot:
                return None
            # relu APRÈS le verrou, dans une instruction suivante : en READ
            # COMMITTED elle voit l'issue posée par un exécuteur concurrent
            rows = tx.query(
                "SELECT %s, %s AS worked, w.title, w.state, w.assignee"
                "  FROM work_item_delegations d JOIN work_items w ON w.id = d.work_item_id"
                " WHERE d.id = %%s AND d.outcome IS NULL AND d.due_at < to_timestamp(%%s)"
                % (_DELEGATION_COLUMNS, _WORKED),
                (int(delegation_id), float(now_ts)))
            if not rows:
                return None
            row = rows[0]
            item_id = int(row["work_item_id"])
            closed = row["state"] in ("merged", "promoted", "closed")
            if closed or row.get("assignee") != row["delegate"]:
                outcome = "annulee"
            elif row.get("worked"):
                outcome = "soldee"
            else:
                outcome = "rendue"
            done = tx.query(
                "UPDATE work_item_delegations SET outcome = %s, resolved_at = now(),"
                "       resolved_by = %s"
                " WHERE id = %s AND outcome IS NULL RETURNING id",
                (outcome, actor or "", int(delegation_id)))
            if not done:
                return None
            if outcome == "rendue":
                item = tx.query(
                    "UPDATE work_items SET assignee = %%s, delegated_by = NULL,"
                    "       delegated_at = NULL, due_at = NULL, updated_at = now()"
                    " WHERE id = %%s RETURNING %s" % ITEM_COLUMNS,
                    (row["delegated_by"], item_id))
            elif outcome == "annulee":
                # L46 : lot fermé OU réassigné hors `work assign` — la
                # délégation annulée ne laisse ni délégant ni date
                item = tx.query(
                    "UPDATE work_items SET delegated_by = NULL, delegated_at = NULL,"
                    "       due_at = NULL WHERE id = %%s RETURNING %s" % ITEM_COLUMNS,
                    (item_id,))
            else:
                item = tx.query(
                    "UPDATE work_items SET due_at = NULL WHERE id = %%s RETURNING %s"
                    % ITEM_COLUMNS, (item_id,))
            text = describe(outcome, row) if describe else None
            if text:
                WorkItems(tx)._event(item_id, item[0]["state"], text, actor)
            return dict(row, outcome=outcome, item=item[0])

    def overdue_delegations(self, now_ts) -> list[dict]:
        """Lots ouverts en retard : délégation en cours dont l'échéance est
        passée et que personne n'a encore traitée (alerte `delegation_expired`)."""
        return self.db.query(
            "SELECT %s, w.title, w.state, w.assignee, w.package_id"
            "  FROM work_item_delegations d JOIN work_items w ON w.id = d.work_item_id"
            " WHERE d.outcome IS NULL AND d.due_at < to_timestamp(%%s)"
            "   AND w.state NOT IN %s"
            " ORDER BY d.due_at, d.id" % (_DELEGATION_COLUMNS, _CLOSED),
            (float(now_ts),))

    def returned_delegations(self, since_ts) -> list[dict]:
        """Délégations rendues au délégant depuis `since_ts` (alerte
        `delegation_expired`), avec le lot."""
        return self.db.query(
            "SELECT %s, w.title, w.state, w.assignee, w.package_id"
            "  FROM work_item_delegations d JOIN work_items w ON w.id = d.work_item_id"
            " WHERE d.outcome = 'rendue' AND d.resolved_at >= to_timestamp(%%s)"
            " ORDER BY d.resolved_at, d.id" % _DELEGATION_COLUMNS,
            (float(since_ts),))

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

    # -- branche d'un lot (L118) ---------------------------------------------
    def set_branch(self, item_id, branch, target, *, note, actor) -> dict | None:
        """Pose la branche (et la cible) d'un lot ouvert, avec une ligne de
        journal ; le dernier commit vu est oublié si la branche change. None
        si le lot n'est plus ouvert."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_items SET"
                "       branch_head = CASE WHEN branch IS DISTINCT FROM %%s"
                "                          THEN NULL ELSE branch_head END,"
                "       branch = %%s, branch_target = %%s, updated_at = now()"
                " WHERE id = %%s AND state NOT IN ('merged', 'promoted', 'closed')"
                " RETURNING %s" % ITEM_COLUMNS,
                (branch, branch, target, int(item_id)))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    def set_branch_head(self, item_id, branch, head) -> bool:
        """Retient le dernier commit de la branche vu en avance sur sa cible
        (sans journal ni `updated_at` : un relevé n'est pas une activité du
        lot). Conditionné à la branche lue : rien si elle a changé."""
        rows = self.db.query(
            "UPDATE work_items SET branch_head = %s"
            " WHERE id = %s AND branch = %s AND branch_head IS DISTINCT FROM %s"
            " RETURNING id", (head, int(item_id), branch, head))
        return bool(rows)

    def open_for_sweep(self, limit) -> list[dict]:
        """Les lots ouverts que le relevé des fusions examine — avec une
        branche ou un assigné —, du plus ancien au plus récent."""
        return self.db.query(
            "SELECT %s FROM work_items"
            " WHERE (branch IS NOT NULL OR assignee IS NOT NULL)"
            "   AND state NOT IN ('merged', 'promoted', 'closed')"
            " ORDER BY id LIMIT %%s" % ITEM_COLUMNS, (int(limit),))

    def open_for(self, assignee) -> list[dict]:
        """Les lots ouverts (ni fusionnés, ni promus, ni fermés) d'un assigné."""
        return self.db.query(
            "SELECT %s FROM work_items"
            " WHERE assignee = %%s AND state NOT IN ('merged', 'promoted', 'closed')"
            " ORDER BY id" % ITEM_COLUMNS, (assignee,))

    def open_by_ref(self, ref) -> list[dict]:
        """Les lots ouverts désignés par une référence : `issue_ref`, fiche du
        plan (`package_id`), branche, ou premier mot du titre (« REF : … »),
        sans distinction de casse."""
        return self.db.query(
            "SELECT %s FROM work_items"
            " WHERE state NOT IN ('merged', 'promoted', 'closed')"
            "   AND (lower(issue_ref) = lower(%%s) OR lower(package_id) = lower(%%s)"
            "        OR lower(branch) = lower(%%s)"
            "        OR lower(rtrim(split_part(btrim(title), ' ', 1), ':')) = lower(%%s))"
            " ORDER BY id" % ITEM_COLUMNS, (ref, ref, ref, ref))

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

    # -- file d'amélioration (L119, décision 0037) ------------------------------
    def backlog_add(self, *, title, body, source, expected_value, value_score, priority,
                    team, required_capabilities, package_id, package_parent, note,
                    actor) -> dict:
        with self.db.transaction() as tx:
            rows = tx.query(
                """
                INSERT INTO work_items
                    (type, source, title, body, state, expected_value, value_score,
                     priority, team, required_capabilities, package_id, package_parent)
                VALUES ('improvement', %s, %s, %s, 'intake', %s, %s, %s, %s, %s::text[],
                        %s, %s)
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", ITEM_COLUMNS),
                (source, title, body, expected_value, int(value_score), int(priority), team,
                 _catalog._text_array(required_capabilities) if required_capabilities else None,
                 package_id, package_parent))
            item = rows[0]
            WorkItems(tx)._event(int(item["id"]), "intake", note, actor)
            return item

    def backlog(self, *, open_only, limit) -> list[dict]:
        where = ("state = 'intake' AND assignee IS NULL" if open_only
                 else "state NOT IN %s" % _CLOSED)
        return self.db.query(
            "SELECT %s FROM work_items WHERE type = 'improvement' AND %s"
            " ORDER BY coalesce(priority, 2), value_score DESC NULLS LAST, id"
            " LIMIT %%s" % (ITEM_COLUMNS, where), (int(limit),))

    def auto_takes_since(self, seconds, note_prefix) -> int:
        rows = self.db.query(
            "SELECT count(*)::bigint AS n FROM work_item_events"
            " WHERE created_at > now() - make_interval(secs => %s)"
            "   AND starts_with(note, %s)", (float(seconds), note_prefix))
        return int(rows[0]["n"]) if rows else 0

    # -- issues GitHub des lots (L126) ------------------------------------------
    def issue_feed(self, limit) -> list[dict]:
        """Lots ouverts ou porteurs d'une `issue_ref` (les plus récents d'abord
        pour la borne), rendus par id croissant, avec l'équipe de leur fiche et
        l'équipe, le chantier et l'hôte de leur assigné."""
        return self.db.query(
            "SELECT w.*, k.team AS package_team, r.team AS assignee_team,"
            "       r.chantier AS assignee_chantier, r.host AS assignee_host"
            "  FROM (SELECT %s FROM work_items"
            "         WHERE state NOT IN %s OR coalesce(btrim(issue_ref), '') <> ''"
            "         ORDER BY id DESC LIMIT %%s) w"
            "  LEFT JOIN work_packages k ON k.id = w.package_id"
            "  LEFT JOIN agent_registry r ON r.name = w.assignee"
            " ORDER BY w.id" % (ITEM_COLUMNS, _CLOSED), (int(limit),))

    def set_issue_ref(self, item_id, issue_ref, *, current) -> bool:
        rows = self.db.query(
            "UPDATE work_items SET issue_ref = %s"
            " WHERE id = %s AND coalesce(issue_ref, '') = coalesce(%s, '')"
            " RETURNING id", (issue_ref, int(item_id), current))
        return bool(rows)

    # -- demandes d'humains sans lot (L130) --------------------------------------
    def recent_or_cited(self, *, since_ts, ids) -> list[dict]:
        cited = sorted({int(i) for i in ids or ()})
        extra = (" OR id IN (%s)" % ", ".join(["%s"] * len(cited))) if cited else ""
        return self.db.query(
            "SELECT id, title, source, body,"
            "       extract(epoch from created_at)::float8 AS created_ts"
            "  FROM work_items"
            " WHERE created_at >= to_timestamp(%s)" + extra +
            " ORDER BY id", (float(since_ts), *cited))

    def _event(self, item_id, state, note, actor) -> None:
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor) "
            "VALUES (%s, %s, %s, %s)",
            (item_id, state, note or "", actor or ""),
        )
