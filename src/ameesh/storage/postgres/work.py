# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : lots (`work_items`) et leur journal (`work_item_events`).

L29 (0026) : rattachement au plan (`package_id`, `package_parent`), fermeture
sur fusion constatée (`close_merged`) et fermeture explicite (`close`), chacune
en UNE transaction : transition conditionnelle, journal et jalon.

SQL déplacé tel quel depuis `ameesh.work` (lot L1). Chaque transition
laisse une ligne dans `work_item_events` (qui, quand, pourquoi).
"""
from __future__ import annotations

import datetime as _dt
import json
import random
import time

from ...db import DbError, is_unique_violation
from .. import interface
from . import catalog as _catalog
from .work_dependencies import _DONE as _DEPENDENCIES_DONE, _LOCK as _DEPENDENCIES_LOCK

#: #705 : `__REVIEWER__` est remplacé par `item_columns` — la colonne
#: `reviewer` n'existe qu'après la migration 0421, et la base de prod peut
#: être en 0373 quand ce code tourne (l'exécuteur migre à la mise à jour).
_COLUMNS_TEMPLATE = """
    id, type, source, app, title, body, issue_ref, workstream, state, assignee,
    __REVIEWER__,
    loops, budget_usd, spent_usd, package_id, package_parent, pr_ref, close_reason,
    superseded_by, delegated_by,
    expected_value, value_score, priority, team, required_capabilities,
    branch, branch_target, branch_head, migration, migration_repo,
    extract(epoch from branch_head_at)::float8 as branch_head_ts,
    partial_merge, extract(epoch from partial_seen_at)::float8 as partial_seen_ts,
    estimate_minutes, estimate_source, estimate_by, change_size,
    extract(epoch from estimate_at)::float8 as estimate_ts,
    extract(epoch from started_at)::float8 as started_ts,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from updated_at)::float8 as updated_ts,
    extract(epoch from closed_at)::float8  as closed_ts,
    extract(epoch from delegated_at)::float8 as delegated_ts,
    extract(epoch from due_at)::float8 as due_ts
"""


def has_reviewer(db) -> bool:
    """#705 : `work_items.reviewer` existe-t-elle (migration 0421) ?

    Seule la réponse POSITIVE est mise en cache sur la connexion : une colonne
    n'apparaît pas puis ne disparaît pas. La réponse négative ne l'est pas —
    un démon (runner, serve) démarré avant 0421 voit la colonne dès qu'elle
    est appliquée, sans redémarrage (#798, #813) ; sinon il gardait des
    lectures sans relecteur et refusait les désignations jusqu'au
    redémarrage."""
    if getattr(db, "_has_reviewer", False):
        return True
    flag = bool(db.query(
        "SELECT 1 AS ok FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = 'work_items'"
        "   AND column_name = 'reviewer'"))
    if flag:
        try:
            db._has_reviewer = True
        except Exception:  # objet sans attributs (proxy) : on relira
            pass
    return flag


def item_columns(db, ref: str | None = None) -> str:
    """Les colonnes d'un lot ; `reviewer` quand 0421 est appliquée, `''` sinon
    (les lectures d'avant la migration ne cassent pas, #705).

    `ref` (le nom de la table dans la requête, « work_items ») lit `reviewer`
    par `to_jsonb`, sans sonde de colonne : les commandes à budget
    d'allers-retours (L61 : `work list`, `progress`) n'en paient pas un de
    plus. `to_jsonb` rend NULL sur une base d'avant 0421, d'où le coalesce."""
    if ref:
        reviewer = "coalesce(to_jsonb(%s)->>'reviewer', '') AS reviewer" % ref
    elif has_reviewer(db):
        reviewer = "reviewer"
    else:
        reviewer = "''::text AS reviewer"
    return _COLUMNS_TEMPLATE.replace("__REVIEWER__", reviewer)

#: L160d (0057): a prepared conflict resolution
QUEUE_COLUMNS = """
    q.work_item_id, q.target, q.frozen_sha, q.state, q.rank, q.landing_sha, q.reason,
    q.queued_by, q.source, q.generation, q.frozen_id,
    extract(epoch from q.queued_at)::float8 as queued_ts,
    extract(epoch from q.updated_at)::float8 as updated_ts,
    w.title, w.priority, w.state as item_state, w.branch, w.assignee
"""

RESOLUTION_COLUMNS = """
    work_item_id, other_item_id, commit_sha, lot_tip, other_tip, ref, files, created_by,
    extract(epoch from created_at)::float8 as created_ts
"""

#: L233: an open decision request holds the lot — row locked, so that an
#: answer closing it (`decisions.close`, conditional on `delivered_at IS
#: NULL`) is serialized with the move into waiting_human
_HELD = """
               AND EXISTS (SELECT 1 FROM agent_mailbox r
                            WHERE r.delivered_at IS NULL AND r.kind = 'request'
                              AND (r.payload -> 'decision') IS NOT NULL
                              AND btrim(r.work_item_id) = %s
                              FOR UPDATE)"""

#: états de lot terminés (une délégation n'y a plus d'objet)
_CLOSED = "('merged', 'promoted', 'closed')"
#: L165b : un lot qui attend un autre que son agent (`qa`, `waiting_human`,
#: `blocked`) ne l'occupe pas — même liste que `operations.listing` et
#: `sous_utilisation.LOTS_WAITING_ON_OTHERS`
_WAITING_ON_OTHERS = "('qa', 'waiting_human', 'blocked')"
#: #443, #478 : réservation automatique du prochain numéro libre — un budget
#: de temps (et non plus un nombre d'essais) et une attente plein jitter
#: (`uniform(0, min(plafond, base·2^n))`) bornée au budget restant, pour que
#: 20 réservations simultanées se départagent sans épuiser les essais ;
#: épuisé, `ReservationContention` (WorkError lisible).
_RESERVE_BUDGET_S = 30.0
_RESERVE_WAIT_BASE_S = 0.005
_RESERVE_WAIT_MAX_S = 0.5
#: l'index unique (dépôt, numéro) de L122 : lui seul fait une contention
MIGRATION_INDEX = "work_items_migration_repo_idx"
#: L233b : une reprise sur pause budget à la fois pour tout le mesh
_HANDOFF_LOCK = ("SELECT pg_advisory_xact_lock("
                 "hashtextextended(current_schema() || ':budget_handoff', 0))")

#: #540 : verrou de transaction d'un agent : « a-t-il déjà un lot non démarré ? »
#: et l'attribution qui suit passent l'une après l'autre
PENDING_LOCK = ("SELECT pg_advisory_xact_lock("
                "hashtextextended(current_schema() || ':pending_lot:' || %s, 0))")

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
           extract(epoch FROM liv.at)::float8 AS delivered_ts,
           extract(epoch FROM (gel.at - req.at))::float8 AS request_to_freeze_s,
           extract(epoch FROM (rev.at - gel.at))::float8 AS freeze_to_review_s,
           extract(epoch FROM (fus.at - gel.at))::float8 AS freeze_to_merge_s,
           extract(epoch FROM (fus.at - rev.at))::float8 AS review_to_merge_s,
           extract(epoch FROM (coalesce(fus.at, liv.at) - req.at))::float8 AS total_s,
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
            SELECT min(m.at) AS at FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'delivered') liv ON true
      LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'verdict') allv ON true
      LEFT JOIN LATERAL (
            SELECT count(*) AS n FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'verdict'
               AND m.verdict = 'blocked') blk ON true
"""


class WorkItems(interface.WorkItems):

    def set_app(self, item_id, app, *, actor, if_empty=False, assignee=None) -> dict | None:
        with self.db.transaction() as tx:
            rows = tx.query("SELECT %s FROM work_items WHERE id = %%s FOR NO KEY UPDATE"
                            % item_columns(self.db), (int(item_id),))
            if not rows:
                return None
            item = rows[0]
            if if_empty and ((item.get("app") or "").strip()
                             or item["state"] in ("merged", "promoted", "closed")
                             or item.get("assignee") != assignee):
                return None
            if item.get("app") == app:
                return item
            rows = tx.query("UPDATE work_items SET app = %%s, updated_at = now()"
                            " WHERE id = %%s RETURNING %s" % item_columns(self.db),
                            (app, int(item_id)))
            WorkItems(tx)._event(int(item_id), item["state"],
                                 "project %r → %r" % (item.get("app"), app), actor)
            return rows[0]

    def without_project(self) -> list[dict]:
        return self.db.query("SELECT %s FROM work_items WHERE btrim(coalesce(app, '')) = ''"
                             " AND state NOT IN %s ORDER BY id" % (item_columns(self.db), _CLOSED))

    def creator(self, item_id) -> str:
        rows = self.db.query("SELECT actor FROM work_item_events WHERE work_item_id = %s"
                             " ORDER BY id LIMIT 1", (int(item_id),))
        return (rows[0].get("actor") or "") if rows else ""

    def add(self, *, type, source, app, title, body, issue_ref, workstream,  # noqa: A002
            assignee, budget_usd, note, actor, package_id=None, package_parent=None,
            branch=None, branch_target=None, priority=None, estimate_minutes=None,
            estimate_source=None, change_size=None, queued_note=None,
            forced_note=None, mark_item_id=None, mark_note="",
            parent_lot=None) -> dict:
        """Crée le lot en `intake`, puis sa première ligne de journal ; L157 :
        avec sa durée estimée (et sa ligne d'historique), en UNE transaction.
        #540 : `queued_note` (voir l'interface).
        L539 : `mark_note` marque `mark_item_id` dans la **même** transaction,
        si bien qu'un échec de la marque annule la création (et l'inverse).
        La ligne marquée est verrouillée **avant** l'insertion et la
        `issue_ref` est relue sous ce verrou : deux appels concurrents ne
        peuvent pas créer deux lots pour la même référence (revue #544 r2).

        `parent_lot` (#1193): the explicit parent of a suite lot. The parent is
        read back **under lock in the same transaction**: still open, the
        blocking dependency edge is written with the creation (a failure of the
        edge rolls the creation back, the suite lot is never exposed ready);
        already finished, no edge is written (the suite is available at once)
        and the parent stays traced in the journal; unknown, everything is
        rolled back. No number is ever guessed from the title or the body.

        Les verrous sont pris dans l'ordre commun avec `assign` (revue R1
        delta #540) : les lignes de lots d'abord (`mark_item_id` et
        `parent_lot`, par identifiant croissant), le verrou d'agent ensuite.
        `assign` verrouille la ligne du lot puis l'agent (`_pending`) ;
        l'ordre inverse ici (agent puis parent) interbloquait l'ajout d'une
        suite avec l'attribution de son parent."""
        with self.db.transaction() as tx:
            # #540 : lignes de lots d'abord, par identifiant croissant, puis
            # le verrou d'agent de `_pending` — même ordre que `assign`.
            marked = None
            parent = None
            parent_id = None if parent_lot is None else int(parent_lot)
            for lock_id in sorted({int(v) for v in (mark_item_id, parent_lot)
                                   if v is not None}):
                if lock_id == parent_id:
                    parent = tx.query(
                        "SELECT id, state FROM work_items WHERE id = %s FOR UPDATE",
                        (lock_id,))
                    if not parent:
                        raise DbError("parent lot %s: no such lot" % parent_lot)
                if mark_item_id is not None and lock_id == int(mark_item_id):
                    marked = tx.query(
                        "SELECT state FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                        (lock_id,))
                    if not marked:
                        raise DbError("mark_item_id %s: no such lot" % mark_item_id)
            if mark_item_id is not None and str(issue_ref or "").strip():
                # le verrou sur la ligne marquée sérialise les créateurs :
                # le second relit la référence après le commit du premier
                duplicate = tx.query(
                    "SELECT id FROM work_items WHERE lower(issue_ref) = lower(%s)"
                    " ORDER BY id LIMIT 1", (issue_ref,))
                if duplicate:
                    raise DbError("issue_ref %s is already carried by lot #%d"
                                  % (issue_ref, int(duplicate[0]["id"])))
            behind = (WorkItems(tx)._pending(assignee, None)
                      if queued_note is not None and assignee else None)
            if behind is not None:
                assignee, note = ((assignee, forced_note.format(lot=behind)) if forced_note
                                  else (None, queued_note.format(lot=behind)))
            rows = tx.query(
                """
                INSERT INTO work_items
                    (type, source, app, title, body, issue_ref, workstream, assignee,
                     budget_usd, state, package_id, package_parent, branch, branch_target,
                     priority, estimate_minutes, estimate_source, estimate_by, estimate_at,
                     change_size)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'intake', %s, %s, %s, %s, %s,
                        %s, %s, %s, CASE WHEN %s THEN now() END, %s)
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)),
                (type, source, app, title, body, issue_ref, workstream, assignee,
                 budget_usd, package_id, package_parent, branch, branch_target,
                 None if priority is None else int(priority),
                 estimate_minutes, estimate_source if estimate_minutes else None,
                 (actor or "") if estimate_minutes else None, bool(estimate_minutes),
                 change_size or None),
            )
            item = rows[0]
            if estimate_minutes:
                WorkItems(tx)._estimate_row(int(item["id"]), estimate_minutes,
                                            estimate_source, actor)
            WorkItems(tx)._event(int(item["id"]), "intake", note, actor)
            if behind is not None:
                item["forced_behind" if forced_note else "queued_behind"] = behind
            if parent_lot is not None:
                # #1193: the explicit parent of a suite lot is linked in the
                # same transaction as the creation — never a forgotten edge,
                # and an edge failure never leaves the lot ready. The row was
                # locked above, before the agent's lock (#540, ordre commun).
                if parent[0]["state"] not in ("merged", "promoted", "closed"):
                    tx.execute(
                        "INSERT INTO work_item_dependencies(work_item_id, blocker_id,"
                        " created_by) VALUES (%s, %s, %s)",
                        (int(item["id"]), int(parent_lot), actor or ""))
                    WorkItems(tx)._event(
                        int(item["id"]), "intake",
                        "Dependency added: lot #%d" % int(parent_lot), actor)
                else:
                    WorkItems(tx)._event(
                        int(item["id"]), "intake",
                        "suite of lot #%d (already %s)"
                        % (int(parent_lot), parent[0]["state"]), actor)
            if mark_item_id is not None and str(mark_note or "").strip():
                WorkItems(tx)._event(
                    int(mark_item_id), marked[0]["state"],
                    str(mark_note).replace("{lot}", str(item["id"])), actor)
            return item

    def get(self, item_id) -> dict | None:
        rows = self.db.query(
            "SELECT %s FROM work_items WHERE id = %%s" % item_columns(self.db), (int(item_id),))
        return rows[0] if rows else None

    def items(self, *, state, assignee, limit) -> list[dict]:
        # L61 : lecture de `reviewer` sans sonde, cette commande est bornée
        sql = "SELECT %s FROM work_items WHERE true" % item_columns(self.db, "work_items")
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

    def move(self, item_id, state, *, current, loops, note, actor,
             held=False) -> dict | None:
        """Déplace le lot s'il est encore en `current` (sinon None, rien
        d'écrit), puis journalise la transition. `held` (L233) : et si une
        demande de décision ouverte le tient — vérifiée dans la même
        instruction, la demande verrouillée (voir l'interface). Un passage
        vers un état terminal libère la réservation de migration (#956).

        #1693 (N4) : une correction `merged → build` retire le jalon `merged`
        de la frise, dans la transaction de la transition. Une fusion annulée
        ne doit ni dater la livraison (affichage, délais, mesure L157c) ni
        rester comme seule trace d'une fusion : le journal garde la fusion et
        la correction, et la vraie fusion écrit son propre jalon (le trigger
        de 0013 le recrée alors que plus rien ne le porte)."""
        # L157 : le premier passage en build (ou qa) date le début du travail
        sql = """
            UPDATE work_items
               SET state = %s,
                   loops = loops + %s,
                   updated_at = now(),
                   closed_at = CASE WHEN %s = 'promoted' THEN now() ELSE NULL END,
                   started_at = CASE WHEN %s IN ('build', 'qa')
                                     THEN coalesce(started_at, now()) ELSE started_at END
             WHERE id = %s AND state = %s __HELD__
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", item_columns(self.db)).replace("__HELD__", _HELD if held else "")
        params = ((state, loops, state, state, int(item_id), current)
                  + ((str(int(item_id)),) if held else ()))
        # #1693 (N4) : la correction `merged → build` emporte le jalon `merged`
        # dans la même transaction (voir la docstring).
        drop_merged = current == "merged" and state == "build"
        if state not in ("merged", "promoted", "closed") and not drop_merged:
            rows = self.db.query(sql, params)
            if not rows:
                return None
            self._event(int(item_id), state, note, actor)
            return rows[0]
        with self.db.transaction() as tx:
            rows = tx.query(sql, params)
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), state, note, actor)
            if drop_merged:
                tx.execute("DELETE FROM work_item_milestones"
                           " WHERE work_item_id = %s AND kind = 'merged'", (int(item_id),))
            if state in ("merged", "promoted", "closed"):
                self._release_migration(tx, item_id, rows[0],
                                        why="état %s" % state, actor=actor)
            return rows[0]

    def designate_reviewer(self, item_id, reviewer, *, actor) -> dict | None:
        """#705 : pose (ou change) le relecteur désigné d'un lot en `qa`, et le
        journalise ; None si le lot n'est plus en `qa` (rien d'écrit). La
        colonne vient de la migration 0421 : sans elle, l'écriture est refusée
        avec la marche à suivre (les lectures, elles, s'en passent)."""
        if not has_reviewer(self.db):
            raise DbError("work_items.reviewer is missing (migration 0421): "
                          "run \"ameesh migrate\", then designate the reviewer")
        with self.db.transaction() as tx:
            return WorkItems(tx)._designate_reviewer(item_id, reviewer, actor)

    def _designate_reviewer(self, item_id, reviewer, actor) -> dict | None:
        """`designate_reviewer` dans la transaction ouverte : l'écriture et sa
        ligne de journal ensemble. #705 r2 (B1) : la règle « le relecteur n'est
        pas l'assigné » est relue DANS l'update — l'appelant a lu la ligne
        avant, un `assign` a pu la changer entre-temps."""
        rows = self.db.query(
            "UPDATE work_items SET reviewer = %%s, updated_at = now()"
            " WHERE id = %%s AND state = 'qa'"
            "   AND assignee IS DISTINCT FROM %%s"
            " RETURNING %s" % item_columns(self.db),
            (reviewer, int(item_id), reviewer))
        if not rows:
            return None
        self._event(int(item_id), "qa", "reviewer %s designated" % reviewer, actor)
        return rows[0]

    def assign(self, item_id, assignee, *, current, note, actor,
               queued_note=None, forced_note=None, take=False,
               ready_only=False) -> dict | None:
        """Réassignation (L37, 0030) : conditionnelle à l'assigné lu par
        l'appelant (`current`, NULL compris) et à un lot encore ouvert ; la
        ligne de journal part dans la même transaction. L40 : la délégation
        en cours est effacée (issue `annulee` au registre). #540 :
        `queued_note` (voir l'interface). #1696 : `ready_only` (prise
        automatique, L165) exige `current=None` — une combinaison contraire
        est une erreur d'appel, refusée avant le verrou, jamais un None muet."""
        if ready_only and current is not None:
            raise ValueError(
                "assign(ready_only=True) exige current=None : la prise"
                " automatique (L165/#1242) ne réassigne pas un lot attribué")
        with self.db.transaction() as tx:
            if ready_only:
                # Même verrou que les éditions du graphe : une dépendance ne
                # peut apparaître entre le contrôle et l'attribution. Verrouiller
                # aussi les amonts empêche leur réouverture pendant la prise.
                tx.execute(_DEPENDENCIES_LOCK)
                rows = tx.query(
                    "SELECT id, state, assignee FROM work_items"
                    " WHERE id = %s OR id IN (SELECT blocker_id FROM work_item_dependencies"
                    "                         WHERE work_item_id = %s)"
                    " ORDER BY id FOR UPDATE", (int(item_id), int(item_id)))
                item = next((r for r in rows if int(r["id"]) == int(item_id)), None)
                if item is None or item["state"] != "intake" \
                        or item["assignee"] is not None:
                    return None
                if any(int(r["id"]) != int(item_id) and r["state"] not in _DEPENDENCIES_DONE
                       for r in rows):
                    return None
            behind = None
            if (queued_note is not None or take) and assignee:
                lot = tx.query("SELECT started_at IS NULL AS fresh FROM work_items"
                               " WHERE id = %s FOR UPDATE", (int(item_id),))
                if lot and lot[0]["fresh"]:
                    behind = WorkItems(tx)._pending(assignee, int(item_id))
            if behind is not None and take:
                raise interface.PendingLot(behind)
            if behind is not None:
                assignee, note = ((assignee, forced_note.format(lot=behind)) if forced_note
                                  else (None, queued_note.format(lot=behind)))
            row = WorkItems(tx)._reassign(item_id, assignee, current,
                                          "intake" if ready_only else None, note, actor)
            if row is not None and behind is not None:
                row["forced_behind" if forced_note else "queued_behind"] = behind
            return row

    def _pending(self, assignee, item_id) -> int | None:
        """#540, dans la transaction ouverte et sous le verrou de l'agent : son
        plus ancien lot ouvert qui l'occupe, autre que `item_id`, ou None.
        L165b : n'occupent pas l'agent le lot qui attend un autre (`qa`,
        `waiting_human`, `blocked`) ni celui dont le début est prévu après
        aujourd'hui — le jour local du processus qui attribue, comme
        `operations.listing`."""
        today = _dt.date.today().isoformat()
        self.db.query(PENDING_LOCK, (assignee,))
        rows = self.db.query(
            "SELECT id FROM work_items WHERE assignee = %%s AND started_at IS NULL"
            "   AND state NOT IN %s AND state NOT IN %s"
            "   AND (planned_start IS NULL OR planned_start <= %%s)"
            "   AND id <> %%s ORDER BY id LIMIT 1" % (_CLOSED, _WAITING_ON_OTHERS),
            (assignee, today, int(item_id or 0)))
        return int(rows[0]["id"]) if rows else None

    def _reassign(self, item_id, assignee, current, state, note, actor) -> dict | None:
        """`assign` dans la transaction ouverte ; `state` : celui exigé.

        #705 r2 (B1) : la règle « le relecteur n'est pas l'assigné » vaut
        aussi quand l'assignation arrive après la désignation — la désignation
        tombe alors dans la MÊME écriture (l'assignation et la désignation
        peuvent se croiser), et le journal le dit."""
        previous_reviewer = ""
        reviewer_set = ""
        params: list = [assignee]
        if has_reviewer(self.db):
            # #813 : NO KEY UPDATE suffit à sérialiser deux réassignations, et
            # n'attend pas les contrôles de clé étrangère (FOR KEY SHARE) des
            # insertions au journal et aux jalons du lot, que FOR UPDATE
            # bloquait jusqu'au commit.
            seen = self.db.query(
                "SELECT reviewer FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                (int(item_id),))
            previous_reviewer = (seen[0]["reviewer"] or "") if seen else ""
            reviewer_set = (", reviewer = CASE WHEN reviewer = %s THEN ''"
                            " ELSE reviewer END")
            params.append(assignee)
        params += [int(item_id), current, state, state]
        rows = self.db.query(
            ("UPDATE work_items SET assignee = %s" + reviewer_set + ","
             "       updated_at = now(),"
             "       delegated_by = NULL, delegated_at = NULL, due_at = NULL"
             " WHERE id = %s AND assignee IS NOT DISTINCT FROM %s"
             "   AND state NOT IN ('merged', 'promoted', 'closed')"
             "   AND (%s::text IS NULL OR state = %s)"
             " RETURNING __COLUMNS__").replace("__COLUMNS__", item_columns(self.db)),
            tuple(params))
        if not rows:
            return None
        if previous_reviewer and previous_reviewer == assignee:
            note = "%s; reviewer %s cleared (they now hold the lot)" % (
                note, previous_reviewer)
        self.db.query(
            "UPDATE work_item_delegations SET outcome = 'annulee', resolved_at = now(),"
            "       resolved_by = %s"
            " WHERE work_item_id = %s AND outcome IS NULL RETURNING id",
            (actor or "", int(item_id)))
        self._event(int(item_id), rows[0]["state"], note, actor)
        return rows[0]

    def handoff(self, item_id, assignee, *, current, state, paused_prefix, note_prefix,
                window_s, max_per_window, note, actor) -> dict | None:
        """L233b : toutes les conditions relues sous le verrou, dans la
        transaction qui écrit (codex r1, #409) : deux `notify` sur deux hôtes
        ne dépassent ni le plafond ni ne donnent deux lots au même preneur ;
        un agent sorti de pause entre le contrôle git et l'écriture garde son
        lot (sa ligne reste verrouillée jusqu'au commit)."""
        with self.db.transaction() as tx:
            tx.query(_HANDOFF_LOCK)
            taken = tx.query(
                "SELECT count(*)::bigint AS n FROM work_item_events"
                " WHERE created_at > now() - make_interval(secs => %s)"
                "   AND starts_with(note, %s)", (float(window_s), note_prefix))
            if int(taken[0]["n"]) >= int(max_per_window):
                return None
            if tx.query("SELECT 1 FROM work_items WHERE assignee = %%s"
                        "   AND state NOT IN %s LIMIT 1" % _CLOSED, (assignee,)):
                return None
            if not tx.query("SELECT 1 FROM agent_registry WHERE name = %s"
                            "   AND status = 'blocked' AND starts_with(status_text, %s)"
                            " FOR SHARE", (current, paused_prefix)):
                return None
            return WorkItems(tx)._reassign(item_id, assignee, current, state, note, actor)

    # -- délégation à échéance (L40, 0030 point 5) ---------------------------
    def delegate(self, item_id, delegate, *, current, delegated_by, within_s, note,
                 actor) -> dict | None:
        """Délègue le lot en UNE transaction, s'il a encore l'assigné `current`
        et n'est pas fermé : la délégation en cours est remplacée (issue
        `remplacee`), le lot passe au délégué avec délégant, début et
        échéance, le registre reçoit la nouvelle ligne, le journal la note.
        Rend `{item, delegation, replaced}` ou None (lot modifié entre-temps)."""
        with self.db.transaction() as tx:
            with_reviewer = has_reviewer(self.db)
            seen = tx.query(
                "SELECT id%s FROM work_items WHERE id = %%s"
                "   AND assignee IS NOT DISTINCT FROM %%s AND state NOT IN %s"
                " FOR NO KEY UPDATE"
                % (", reviewer" if with_reviewer else "", _CLOSED),
                (int(item_id), current))
            if not seen:
                return None
            # #705 r2 (B1) : déléguer à son relecteur retire la désignation
            # (le relecteur relirait son propre travail), dans l'écriture même.
            previous_reviewer = (seen[0]["reviewer"] or "") if with_reviewer else ""
            replaced = tx.query(
                "UPDATE work_item_delegations SET outcome = 'remplacee', resolved_at = now(),"
                "       resolved_by = %s"
                " WHERE work_item_id = %s AND outcome IS NULL"
                " RETURNING id, delegate, delegated_by,"
                "           extract(epoch from due_at)::float8 AS due_ts",
                (actor or "", int(item_id)))
            reviewer_set = ""
            params: list = [delegate, delegated_by, float(within_s), int(item_id)]
            if with_reviewer:
                reviewer_set = (", reviewer = CASE WHEN reviewer = %s THEN ''"
                                " ELSE reviewer END")
                params = [delegate, delegate, delegated_by, float(within_s),
                          int(item_id)]
            rows = tx.query(
                ("UPDATE work_items SET assignee = %s" + reviewer_set + ","
                 "       delegated_by = %s, delegated_at = now(),"
                 "       due_at = now() + make_interval(secs => %s), updated_at = now()"
                 " WHERE id = %s RETURNING __COLUMNS__"
                 ).replace("__COLUMNS__", item_columns(self.db)),
                tuple(params))
            item = rows[0]
            delegation = tx.query(
                "INSERT INTO work_item_delegations AS d"
                "       (work_item_id, delegate, delegated_by, delegated_at, due_at)"
                " SELECT id, %%s, %%s, delegated_at, due_at FROM work_items WHERE id = %%s"
                " RETURNING %s" % _DELEGATION_COLUMNS,
                (delegate, delegated_by, int(item_id)))[0]
            if previous_reviewer and previous_reviewer == delegate:
                note = "%s; reviewer %s cleared (they now hold the lot)" % (
                    note, previous_reviewer)
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
            with_reviewer = has_reviewer(self.db)
            rows = tx.query(
                "SELECT %s, %s AS worked, w.title, w.state, w.assignee%s"
                "  FROM work_item_delegations d JOIN work_items w ON w.id = d.work_item_id"
                " WHERE d.id = %%s AND d.outcome IS NULL AND d.due_at < to_timestamp(%%s)"
                % (_DELEGATION_COLUMNS, _WORKED, ", w.reviewer" if with_reviewer else ""),
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
                # #705 : le délégant a pu être désigné relecteur pendant la
                # délégation (il n'était plus l'assigné) ; le lot lui revient,
                # la désignation tombe dans la même écriture.
                reviewer_set = ""
                params: list = [row["delegated_by"]]
                if with_reviewer:
                    reviewer_set = (", reviewer = CASE WHEN reviewer = %s THEN ''"
                                    " ELSE reviewer END")
                    params.append(row["delegated_by"])
                item = tx.query(
                    ("UPDATE work_items SET assignee = %s" + reviewer_set + ","
                     "       delegated_by = NULL, delegated_at = NULL, due_at = NULL,"
                     "       updated_at = now()"
                     " WHERE id = %s RETURNING __COLUMNS__"
                     ).replace("__COLUMNS__", item_columns(self.db)),
                    tuple(params + [item_id]))
            elif outcome == "annulee":
                # L46 : lot fermé OU réassigné hors `work assign` — la
                # délégation annulée ne laisse ni délégant ni date
                item = tx.query(
                    "UPDATE work_items SET delegated_by = NULL, delegated_at = NULL,"
                    "       due_at = NULL WHERE id = %%s RETURNING %s" % item_columns(self.db),
                    (item_id,))
            else:
                item = tx.query(
                    "UPDATE work_items SET due_at = NULL WHERE id = %%s RETURNING %s"
                    % item_columns(self.db), (item_id,))
            text = describe(outcome, row) if describe else None
            if outcome == "rendue" and row.get("reviewer") \
                    and row["reviewer"] == row["delegated_by"]:
                text = "%s; reviewer %s cleared (they now hold the lot)" % (
                    text or "delegation expired", row["reviewer"])
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

    def lock_deep_analysis(self, item_id) -> None:
        self.db.query("SELECT pg_advisory_xact_lock(hashtext(%s || current_schema())::bigint)",
                      ("ameesh_deep_analysis:%d:" % int(item_id),))

    def waiting_episodes(self, item_ids) -> dict:
        ids = [int(i) for i in item_ids]
        if not ids:
            return {}
        rows = self.db.query(
            """
            SELECT w.id, coalesce(b.id, 0) AS episode,
                   extract(epoch from coalesce(b.created_at, w.created_at))::float8
                       AS start_ts,
                   extract(epoch from (
                       SELECT min(e.created_at) FROM work_item_events e
                        WHERE e.work_item_id = w.id AND e.state = 'waiting_human'
                          AND e.id > coalesce(b.id, 0)))::float8 AS first_ts
              FROM work_items w
              LEFT JOIN LATERAL (
                   SELECT e.id, e.created_at FROM work_item_events e
                    WHERE e.work_item_id = w.id AND e.state <> 'waiting_human'
                    ORDER BY e.id DESC LIMIT 1) b ON true
             WHERE w.id IN (SELECT jsonb_array_elements_text(%s::jsonb)::bigint)
            """, (json.dumps(ids),))
        return {int(r["id"]): {"episode": int(r["episode"]), "start_ts": r["start_ts"],
                               "first_ts": r["first_ts"]}
                for r in rows}

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

    def events_noted(self, prefix, since_ts) -> list[dict]:
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return self.db.query(
            """
            SELECT e.id, e.work_item_id, e.state, e.note, e.actor, w.assignee,
                   extract(epoch from e.created_at)::float8 as created_ts
              FROM work_item_events e JOIN work_items w ON w.id = e.work_item_id
             WHERE e.note LIKE %s AND e.created_at >= to_timestamp(%s)
             ORDER BY e.id
            """,
            (pattern, float(since_ts)),
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

    def add_milestone(self, item_id, kind, *, sha, actor, verdict, note,
                      on_frozen=None, after_commit=None) -> dict:
        """Écrit le jalon sous le verrou de la ligne du lot (L29) : un gel ne
        s'insère jamais pendant qu'une fermeture sur fusion tient ce lot.

        `on_frozen` (facultatif, #1617) : callback appelé **dans la
        transaction** du jalon, juste après l'INSERT d'un jalon `frozen`, avec
        `(tx, milestone, after_commit)`. Il écrit dans la même transaction que
        le jalon — la désignation des relecteurs y dépose sa marque et ses
        courriers atomiquement — et une exception annule le jalon avec elle.
        `after_commit` est la liste des écritures de fil différées
        (`mail.send(..., after_commit=…)`, motif #315) : `add_milestone` ne la
        vide **pas**, c'est l'appelant qui tient le commit qui la vide après
        son COMMIT. Sur une connexion racine le `with` commite, l'appelant vide
        la liste juste après le retour ; dans une transaction déjà ouverte le
        `with` imbriqué ne commite pas (psql et psycopg), et la vider ici
        publierait le fil avant le commit externe — un rollback laisserait des
        traces sans gel ni marque (revue codex cx2, B2). Sans callback ni
        liste, rien ne change : le gel branché par défaut ne désigne personne.
        Un `on_frozen` sur un autre kind est refusé."""
        if on_frozen is not None and kind != "frozen":
            raise ValueError("on_frozen only makes sense for the `frozen` milestone")
        threads = [] if after_commit is None else after_commit
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
            milestone = rows[0]
            if on_frozen is not None:
                on_frozen(tx, milestone, threads)
        return milestone

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
                " WHERE id = %%s RETURNING %s" % item_columns(self.db),
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
                "       branch_head_at = CASE WHEN branch IS DISTINCT FROM %%s"
                "                             THEN NULL ELSE branch_head_at END,"
                "       branch = %%s, branch_target = %%s, updated_at = now()"
                " WHERE id = %%s AND state NOT IN ('merged', 'promoted', 'closed')"
                " RETURNING %s" % item_columns(self.db),
                (branch, branch, branch, target, int(item_id)))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    # -- numéro de migration réservé (L122) ----------------------------------
    def _release_migration(self, tx, item_id, row, *, why, actor) -> None:
        """#956 : libère la réservation du lot qui entre dans un état terminal
        (`merged`, `promoted`, `closed`), dans la transaction même de la
        transition : une réservation tenue par un lot fusionné bloquait le
        numéro pour toujours. Le numéro reste protégé tant qu'un fichier le
        porte, puisque `repo_numbers` compte les fichiers de toutes les
        branches du dépôt."""
        if row.get("migration") is None:
            return
        number = row["migration"]
        tx.execute(
            "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
            "       updated_at = now() WHERE id = %s", (int(item_id),))
        # le lot rendu à l'appelant reflète l'état validé, pas la valeur d'avant
        row["migration"] = None
        row["migration_repo"] = None
        WorkItems(tx)._event(
            int(item_id), row["state"],
            "migration %s released (%s, #956)" % (number, why), actor)

    def reserve_migration(self, item_id, repo, number, floor, *, note, actor,
                          issue_ref, release=False) -> dict | None:
        """Réserve (ou libère, `release`) le numéro de migration d'un lot
        ouvert dans le dépôt `repo`, avec une ligne de journal. Sans
        `number` : le prochain libre de ce dépôt, au moins `floor`, calculé
        dans l'UPDATE même ; l'index unique `work_items_migration_repo_idx`
        tranche entre deux réservations simultanées : la perdante attend un
        délai plein jitter croissant, borné au budget restant, et recalcule
        (nouvelle transaction) ; épuisé, `ReservationContention` — jamais la
        DbError de l'index (#478). Seule la violation d'unicité de cet index
        réessaie : une autre erreur qui le nomme remonte telle quelle
        (codex #478 r1). Un `number` explicite déjà pris lève la DbError de
        l'index, sans réessai. #1830 : `issue_ref` (lue par l'appelant) est une
        condition du CAS, NULL et chaîne vide confondus — une correction
        concurrente de la référence pendant le scan du dépôt ne peut plus
        laisser `issue_ref` d'un dépôt avec `migration_repo` d'un autre ;
        `release` l'ignore."""
        if release:
            value, params = "NULL", ()
        elif number is None:
            value = ("greatest(%s, (SELECT coalesce(max(migration), 0) + 1 FROM work_items"
                     "               WHERE migration_repo = %s"
                     "                 AND state NOT IN ('merged', 'promoted', 'closed')))")
            params = (int(floor), repo)
        else:
            value, params = "%s", (int(number),)
        # `release` libère aussi un lot déjà terminal (#956) ; les autres
        # chemins ne visent qu'un lot ouvert.
        guard = "" if release else " AND state NOT IN ('merged', 'promoted', 'closed')"
        issue_guard = "" if release else " AND coalesce(issue_ref, '') = coalesce(%%s, '')"
        issue_params = () if release else (issue_ref,)
        deadline = time.monotonic() + _RESERVE_BUDGET_S
        wait = _RESERVE_WAIT_BASE_S
        while True:
            try:
                with self.db.transaction() as tx:
                    if not release:
                        # #956 : une réservation héritée d'un lot terminal
                        # (prise avant ce correctif) ne tient plus le numéro :
                        # elle est libérée dans la transaction de la
                        # réservation, avec sa ligne de journal. Les lignes
                        # sont lues AVANT l'UPDATE : `RETURNING` rendrait la
                        # valeur déjà effacée. `ORDER BY id` : ordre de
                        # verrouillage canonique (#967) — deux transactions qui
                        # reprennent les réservations héritées du même dépôt
                        # verrouillent ainsi les mêmes lignes dans le même
                        # ordre, jamais en sens inverse (interblocage).
                        for old in tx.query(
                                "SELECT id, migration, state FROM work_items"
                                " WHERE migration_repo = %s"
                                "   AND state IN ('merged', 'promoted', 'closed')"
                                " ORDER BY id FOR UPDATE", (repo,)):
                            tx.execute(
                                "UPDATE work_items SET migration = NULL,"
                                "       migration_repo = NULL, updated_at = now()"
                                " WHERE id = %s", (int(old["id"]),))
                            WorkItems(tx)._event(
                                int(old["id"]), old["state"],
                                "migration %s released (lot %s %s, #956)" % (
                                    old["migration"], old["id"], old["state"]), actor)
                    sql = ("UPDATE work_items SET migration = %s, migration_repo = %%s,"
                           "       updated_at = now()"
                           " WHERE id = %%s" + guard + issue_guard +
                           " RETURNING %s") % (value, item_columns(self.db))
                    rows = tx.query(sql,
                                    params + (None if release else repo, int(item_id))
                                    + issue_params)
                    if not rows:
                        return None
                    WorkItems(tx)._event(int(item_id), rows[0]["state"],
                                         note % {"number": int(rows[0]["migration"] or 0)},
                                         actor)
                    return rows[0]
            except DbError as exc:
                if (number is not None or release
                        or not is_unique_violation(exc, MIGRATION_INDEX)):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise interface.ReservationContention(
                        "reservation contention, retry") from exc
                # plein jitter (codex #478 r1) : uniform(0, min(plafond,
                # base·2^n)) — les perdantes se répartissent au lieu de
                # retenter ensemble — borné au budget restant pour n'entamer
                # aucune transaction après l'échéance
                time.sleep(random.uniform(0.0, min(wait, remaining)))
                wait = min(wait * 2, _RESERVE_WAIT_MAX_S)
                if time.monotonic() >= deadline:
                    raise interface.ReservationContention(
                        "reservation contention, retry") from exc

    def migration_holder(self, number, repo) -> dict | None:
        """Le lot OUVERT qui tient ce numéro (#956 : un lot terminal ne le
        tient plus)."""
        rows = self.db.query(
            "SELECT id, title, state FROM work_items WHERE migration = %s"
            " AND migration_repo = %s"
            " AND state NOT IN ('merged', 'promoted', 'closed')", (int(number), repo))
        return rows[0] if rows else None

    # -- prepared conflict resolutions (L160d, 0057) ---------------------------
    def record_resolution(self, item_id, other_id, *, commit, lot_tip, other_tip, ref,
                          files, note, actor) -> dict | None:
        """Upsert of the pair's resolution and its journal line, in one
        transaction. `files` goes through JSON: the psql driver binds no
        arrays."""
        with self.db.transaction() as tx:
            lots = tx.query("SELECT id, state FROM work_items WHERE id IN (%s, %s)",
                            (int(item_id), int(other_id)))
            states = {int(r["id"]): r["state"] for r in lots}
            if len(states) != 2:
                return None
            rows = tx.query(
                "INSERT INTO work_resolutions (work_item_id, other_item_id, commit_sha,"
                "                              lot_tip, other_tip, ref, files, created_by)"
                " VALUES (%%s, %%s, %%s, %%s, %%s, %%s,"
                "         ARRAY(SELECT jsonb_array_elements_text(%%s::jsonb)), %%s)"
                " ON CONFLICT (work_item_id, other_item_id) DO UPDATE SET"
                "   commit_sha = excluded.commit_sha, lot_tip = excluded.lot_tip,"
                "   other_tip = excluded.other_tip, ref = excluded.ref,"
                "   files = excluded.files, created_by = excluded.created_by,"
                "   created_at = now()"
                " RETURNING %s" % RESOLUTION_COLUMNS,
                (int(item_id), int(other_id), commit, lot_tip, other_tip, ref,
                 json.dumps(list(files or ())), actor or ""))
            WorkItems(tx)._event(int(item_id), states[int(item_id)], note, actor)
            return rows[0]

    def resolutions(self, item_ids) -> list[dict]:
        ids = json.dumps(sorted({int(i) for i in item_ids or ()}))
        return self.db.query(
            "SELECT %s FROM work_resolutions"
            " WHERE work_item_id IN (SELECT jsonb_array_elements_text(%%s::jsonb)::bigint)"
            "    OR other_item_id IN (SELECT jsonb_array_elements_text(%%s::jsonb)::bigint)"
            " ORDER BY created_at DESC, work_item_id, other_item_id"
            % RESOLUTION_COLUMNS, (ids, ids))

    # -- merge queue (L160e, 0058) ---------------------------------------------
    def _queue_row(self, db, item_id) -> dict | None:
        rows = db.query("SELECT %s FROM merge_queue q JOIN work_items w ON w.id = q.work_item_id"
                        " WHERE q.work_item_id = %%s" % QUEUE_COLUMNS, (int(item_id),))
        return rows[0] if rows else None

    def queue_add(self, item_id, *, target, frozen_sha, note, actor,
                  source="", frozen_id=None) -> dict | None:
        with self.db.transaction() as tx:
            lot = tx.query("SELECT state FROM work_items WHERE id = %s"
                           " AND state NOT IN ('merged', 'promoted', 'closed')"
                           " FOR NO KEY UPDATE", (int(item_id),))
            if not lot:
                return None
            latest = tx.query("SELECT id FROM work_item_milestones WHERE work_item_id = %s"
                              " AND kind = 'frozen' ORDER BY at DESC, id DESC LIMIT 1", (int(item_id),))
            latest_id = latest[0]["id"] if latest else None
            if frozen_id is not None and frozen_id != latest_id:
                return None
            tx.execute(
                "INSERT INTO merge_queue (work_item_id, target, frozen_sha, queued_by, source, frozen_id)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (work_item_id) DO UPDATE SET target = excluded.target,"
                "   frozen_sha = excluded.frozen_sha, state = 'queued', rank = NULL,"
                "   source = excluded.source, frozen_id = excluded.frozen_id,"
                "   generation = nextval('merge_queue_generation_seq'), landing_sha = NULL,"
                "   reason = '', queued_by = excluded.queued_by, queued_at = now(),"
                "   updated_at = now()",
                (int(item_id), target, frozen_sha, actor or "", source or "", latest_id))
            WorkItems(tx)._event(int(item_id), lot[0]["state"], note, actor)
            return self._queue_row(tx, item_id)

    def queue_update(self, item_id, *, state=None, rank=None, reason=None, landing_sha=None,
                     note="", actor="", clear_rank=False, lease=None,
                     freeze=None, entry=None) -> dict | None:
        key, owner, epoch = lease if lease is not None else (None, None, None)
        frozen, landing = freeze if freeze is not None else (None, None)
        generation, target, frozen_id, entry_sha = entry if entry is not None else (None,) * 4
        with self.db.transaction() as tx:
            if entry is not None:
                # Same lock as add_milestone/queue_add: read the latest freeze
                # after waiting for any concurrent refreeze to finish.
                tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE", (int(item_id),))
            # #194 r6: the lease row is locked by the same statement (an
            # acquisition waits for this write, or this write sees its new
            # epoch), its expiry read on the wall clock, not the transaction's
            rows = tx.query(
                "WITH lease AS (SELECT 1 FROM merge_locks l"
                "    WHERE l.target = %s AND l.owner = %s AND l.epoch = %s"
                "      AND l.expires_at > clock_timestamp() FOR UPDATE)"
                " UPDATE merge_queue SET state = coalesce(%s, state),"
                "   rank = CASE WHEN %s THEN NULL ELSE coalesce(%s, rank) END,"
                "   landing_sha = CASE WHEN %s IN ('queued', 'ejected') THEN NULL"
                "                      ELSE coalesce(%s, landing_sha) END,"
                "   reason = coalesce(%s, reason), updated_at = now()"
                " WHERE work_item_id = %s"
                "   AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM lease))"
                # #194 r8: or under the freeze — still `landing`, same freeze
                # and same landing commit (a new freeze is never overwritten)
                "   AND (%s::text IS NULL OR (state = 'landing' AND frozen_sha = %s"
                "        AND landing_sha IS NOT DISTINCT FROM %s))"
                "   AND (NOT %s OR (generation = %s AND target = %s"
                "        AND frozen_id IS NOT DISTINCT FROM %s AND frozen_sha = %s"
                "        AND frozen_id IS NOT DISTINCT FROM (SELECT id FROM work_item_milestones"
                "          WHERE work_item_id = %s AND kind = 'frozen'"
                "          ORDER BY at DESC, id DESC LIMIT 1)))"
                " RETURNING work_item_id",
                (key, owner, epoch, state, state == "ejected" or bool(clear_rank), rank,
                 state or "", landing_sha, reason, int(item_id), key, frozen, frozen,
                 landing, entry is not None, generation, target, frozen_id, entry_sha, int(item_id)))
            if not rows:
                return None
            if note:
                lot = tx.query("SELECT state FROM work_items WHERE id = %s", (int(item_id),))
                WorkItems(tx)._event(int(item_id), lot[0]["state"], note, actor)
            return self._queue_row(tx, item_id)

    def queue_remove(self, item_id, *, note, actor) -> bool:
        with self.db.transaction() as tx:
            rows = tx.query("DELETE FROM merge_queue WHERE work_item_id = %s"
                            " RETURNING work_item_id", (int(item_id),))
            if not rows:
                return False
            lot = tx.query("SELECT state FROM work_items WHERE id = %s", (int(item_id),))
            WorkItems(tx)._event(int(item_id), lot[0]["state"], note, actor)
            return True

    def land_lock(self, key, owner, ttl_s, *, epoch=None) -> int | None:
        if epoch is not None:  # renewal: only the holder, at its epoch
            rows = self.db.query(
                "UPDATE merge_locks SET expires_at = clock_timestamp()"
                " + %s * interval '1 second'"
                " WHERE target = %s AND owner = %s AND epoch = %s"
                " AND expires_at > clock_timestamp() RETURNING epoch",
                (float(ttl_s), key, owner, int(epoch)))
            return int(rows[0]["epoch"]) if rows else None
        rows = self.db.query(
            "INSERT INTO merge_locks (target, owner, epoch, expires_at)"
            " VALUES (%s, %s, 1, clock_timestamp() + %s * interval '1 second')"
            " ON CONFLICT (target) DO UPDATE SET owner = excluded.owner,"
            "   epoch = merge_locks.epoch + 1, expires_at = excluded.expires_at"
            " WHERE merge_locks.owner IS NULL OR merge_locks.expires_at < clock_timestamp()"
            "    OR merge_locks.owner = excluded.owner"
            " RETURNING epoch", (key, owner, float(ttl_s)))
        return int(rows[0]["epoch"]) if rows else None

    def land_unlock(self, key, owner, epoch) -> bool:
        rows = self.db.query(
            "UPDATE merge_locks SET owner = NULL, expires_at = NULL"
            " WHERE target = %s AND owner = %s AND epoch = %s RETURNING target",
            (key, owner, int(epoch)))
        return bool(rows)

    def queue(self, target=None) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM merge_queue q JOIN work_items w ON w.id = q.work_item_id"
            " WHERE (%%s::text IS NULL OR q.target = %%s)"
            " ORDER BY q.target, q.rank NULLS LAST, q.queued_at, q.work_item_id"
            % QUEUE_COLUMNS, (target, target))

    def set_branch_head(self, item_id, branch, head) -> bool:
        """Retient le dernier commit de la branche vu en avance sur sa cible
        (sans journal ni `updated_at` : un relevé n'est pas une activité du
        lot), et quand (L118d, 0372, horloge de la base). Une pointe inchangée
        est relevée de nouveau si son instant précède la dernière fusion
        partielle vue (#346 r2) : elle est encore là après cette fusion.
        Conditionné à la branche lue : rien si elle a changé."""
        rows = self.db.query(
            "UPDATE work_items SET branch_head = %s, branch_head_at = clock_timestamp()"
            " WHERE id = %s AND branch = %s AND (branch_head IS DISTINCT FROM %s"
            "       OR branch_head_at IS NULL OR branch_head_at <= partial_seen_at)"
            " RETURNING id", (head, int(item_id), branch, head))
        return bool(rows)

    def set_partial_seen(self, item_id, sha) -> bool:
        """#346 r2 : retient la dernière fusion partielle du lot et l'instant
        (horloge de la base) où le relevé l'a vue la première fois ; rien si
        c'est déjà elle (sans journal ni `updated_at`)."""
        rows = self.db.query(
            "UPDATE work_items SET partial_merge = %s, partial_seen_at = clock_timestamp()"
            " WHERE id = %s AND partial_merge IS DISTINCT FROM %s"
            " RETURNING id", (sha, int(item_id), sha))
        return bool(rows)

    def open_for_sweep(self, limit) -> list[dict]:
        """Les lots ouverts que le relevé des fusions examine — avec une
        branche ou un assigné —, du plus ancien au plus récent."""
        return self.db.query(
            "SELECT %s FROM work_items"
            " WHERE (branch IS NOT NULL OR assignee IS NOT NULL)"
            "   AND state NOT IN ('merged', 'promoted', 'closed')"
            " ORDER BY id LIMIT %%s" % item_columns(self.db), (int(limit),))

    def open_for(self, assignee) -> list[dict]:
        """Les lots ouverts (ni fusionnés, ni promus, ni fermés) d'un assigné."""
        return self.db.query(
            "SELECT %s FROM work_items"
            " WHERE assignee = %%s AND state NOT IN ('merged', 'promoted', 'closed')"
            " ORDER BY id" % item_columns(self.db), (assignee,))

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
            " ORDER BY id" % item_columns(self.db), (ref, ref, ref, ref))

    def by_issue_ref(self, ref) -> list[dict]:
        """L539 : les lots portant exactement cette `issue_ref`, quel que soit
        leur état (un lot de relecture fermé reste l'identité de sa référence)."""
        return self.db.query(
            "SELECT %s FROM work_items WHERE lower(issue_ref) = lower(%%s)"
            " ORDER BY id" % item_columns(self.db), (str(ref or ""),))

    def by_package(self, package_id) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM work_items WHERE package_id = %%s ORDER BY id" % item_columns(self.db),
            (package_id,))

    def close_merged(self, item_id, *, current, sha, actor, note, pr_ref,
                     frozen_id=None) -> dict | None:
        """`current` → `merged` si le lot y est encore (sinon None, rien d'écrit),
        en UNE transaction : transition, journal, puis le jalon `merged` (posé
        par le trigger de 0013) reçoit le commit et l'auteur de la fusion.

        `frozen_id` (revue codex1, P2-1) : le gel examiné doit être le dernier
        gel du lot. Le `SELECT ... FOR UPDATE` prend d'abord le verrou de la
        ligne du lot (en l'attendant au besoin) ; le dernier gel est relu
        APRÈS, dans une instruction suivante de la même transaction — en READ
        COMMITTED elle voit tout gel validé pendant l'attente (le sous-select
        d'une seule instruction verrait l'instantané d'avant l'attente). S'il
        a changé, rien n'est écrit (None). Un gel ne peut pas s'insérer
        ensuite : `add_milestone` prend le même verrou de ligne.

        Rien n'est écrit avant ce recontrôle : la fermeture est appelée dans
        la transaction qui porte aussi le réveil des dépendants (L385 suite),
        et un `UPDATE` suivi d'un `None` n'était annulé que par SA transaction
        — sous une transaction englobante, la ligne serait restée modifiée.
        """
        with self.db.transaction() as tx:
            locked = tx.query(
                "SELECT id FROM work_items WHERE id = %s AND state = %s FOR UPDATE",
                (int(item_id), current))
            if not locked:
                return None
            if frozen_id is not None:
                latest = tx.query(
                    "SELECT id FROM work_item_milestones"
                    " WHERE work_item_id = %s AND kind = 'frozen'"
                    " ORDER BY at DESC, id DESC LIMIT 1", (int(item_id),))
                if not latest or int(latest[0]["id"]) != int(frozen_id):
                    return None
            # #1693 (N1) : une nouvelle fusion (après une correction `merged →
            # build`) devient la dernière PR de référence du lot — c'est elle
            # qu'un relevé suivant doit reconnaître pour ne pas refermer le lot.
            rows = tx.query(
                """
                UPDATE work_items
                   SET state = 'merged', updated_at = now(), closed_at = NULL,
                       pr_ref = coalesce(nullif(%s, ''), pr_ref)
                 WHERE id = %s AND state = %s
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)),
                (pr_ref, int(item_id), current))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), "merged", note, actor)
            self._release_migration(tx, item_id, rows[0], why="fusion", actor=actor)
            # #1693 (N4) : le jalon visé est le plus récent encore sans commit.
            # Après une correction, celui de la fusion annulée a été retiré, le
            # trigger de 0013 vient d'en poser un ; sans correction, c'est le seul.
            tx.query(
                "UPDATE work_item_milestones SET sha = %s, actor = %s, note = %s"
                " WHERE id = (SELECT id FROM work_item_milestones"
                "              WHERE work_item_id = %s AND kind = 'merged' AND sha = ''"
                "              ORDER BY at DESC, id DESC LIMIT 1) RETURNING id",
                (sha or "", actor or "", note or "", int(item_id)))
            return rows[0]

    def close(self, item_id, *, current, reason, superseded_by, sha="", actor, note) -> dict | None:
        """`current` → `closed` (abandon, remplacement, ou livraison hors cible)
        si le lot y est encore ; journal et jalon `closed` dans la même
        transaction. `delivered` (#988) écrit en plus le jalon `delivered`, qui
        porte le `sha` de la tête relue."""
        with self.db.transaction() as tx:
            rows = tx.query(
                """
                UPDATE work_items
                   SET state = 'closed', close_reason = %s, superseded_by = %s,
                       updated_at = now(), closed_at = now()
                 WHERE id = %s AND state = %s
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)),
                (reason, superseded_by, int(item_id), current))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), "closed", note, actor)
            self._release_migration(tx, item_id, rows[0], why="clôture", actor=actor)
            tx.query(
                "INSERT INTO work_item_milestones (work_item_id, kind, actor, note)"
                " VALUES (%s, 'closed', %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (int(item_id), actor or "", note or ""))
            if reason == "delivered":
                tx.query(
                    "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, note)"
                    " VALUES (%s, 'delivered', %s, %s, %s)"
                    " ON CONFLICT DO NOTHING RETURNING id",
                    (int(item_id), sha or "", actor or "", note or ""))
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
                    actor, app="") -> dict:
        with self.db.transaction() as tx:
            rows = tx.query(
                """
                INSERT INTO work_items
                    (type, source, title, body, state, expected_value, value_score,
                     priority, team, required_capabilities, package_id, package_parent,
                     app)
                VALUES ('improvement', %s, %s, %s, 'intake', %s, %s, %s, %s, %s::text[],
                        %s, %s, %s)
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)),
                (source, title, body, expected_value, int(value_score), int(priority), team,
                 _catalog._text_array(required_capabilities) if required_capabilities else None,
                 package_id, package_parent, app or ""))
            item = rows[0]
            WorkItems(tx)._event(int(item["id"]), "intake", note, actor)
            return item

    def backlog(self, *, open_only, limit, ready_only=False) -> list[dict]:
        where = ("state = 'intake' AND assignee IS NULL" if open_only
                 else "state NOT IN %s" % _CLOSED)
        if ready_only:
            where += (" AND NOT EXISTS (SELECT 1 FROM work_item_dependencies d"
                      "                      JOIN work_items b ON b.id = d.blocker_id"
                      "                     WHERE d.work_item_id = work_items.id"
                      "                       AND b.state NOT IN %s)" % _CLOSED)
        return self.db.query(
            "SELECT %s FROM work_items WHERE type = 'improvement' AND %s"
            " ORDER BY coalesce(priority, 2), value_score DESC NULLS LAST, id"
            " LIMIT %%s" % (item_columns(self.db), where), (int(limit),))

    def auto_takes_since(self, seconds, note_prefix) -> int:
        rows = self.db.query(
            "SELECT count(*)::bigint AS n FROM work_item_events"
            " WHERE created_at > now() - make_interval(secs => %s)"
            "   AND starts_with(note, %s)", (float(seconds), note_prefix))
        return int(rows[0]["n"]) if rows else 0

    def unassigned_lots(self, limit) -> list[dict]:
        return self.db.query(
            ("SELECT w.*,"
            "       coalesce(nullif(btrim(w.team), ''), nullif(btrim(k.team), ''),"
            "                nullif(btrim(kp.team), ''), nullif(btrim(w.app), '')) AS lot_team,"
            "       EXISTS (SELECT 1 FROM agent_mailbox b"
            "                WHERE b.delivered_at IS NULL AND b.kind = 'request'"
            "                  AND (b.payload -> 'decision') IS NOT NULL"
            "                  AND btrim(b.work_item_id) = w.id::text) AS pending_decision,"
            "       EXISTS (SELECT 1 FROM commitments c"
            "                WHERE c.work_item_id = w.id AND c.kind = 'decision'"
            "                  AND c.status IN ('proposed', 'open')) AS decision_milestone"
            "  FROM (SELECT %s,"
            "               to_char(planned_start, 'YYYY-MM-DD') AS planned_start,"
            "               to_char(planned_end, 'YYYY-MM-DD') AS planned_end,"
            "               to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery"
            "          FROM work_items"
            "         WHERE state = 'intake' AND assignee IS NULL"
            "           AND type <> 'improvement'"
            "           AND NOT EXISTS (SELECT 1 FROM work_item_dependencies d"
            "                             JOIN work_items b ON b.id = d.blocker_id"
            "                            WHERE d.work_item_id = work_items.id"
            "                              AND b.state NOT IN " + _CLOSED + ")) w"
            "  LEFT JOIN work_packages k ON k.id = w.package_id"
            "  LEFT JOIN work_packages kp ON kp.id = w.package_parent"
            " ORDER BY coalesce(w.priority, 2), w.planned_start NULLS LAST,"
            "          w.planned_end NULLS LAST, w.planned_delivery NULLS LAST,"
            "          w.created_ts, w.id"
            " LIMIT %%s") % item_columns(self.db), (int(limit),))

    def assignment_events(self, item_id, prefixes) -> list[dict]:
        return self.db.query(
            """
            (SELECT id, work_item_id, state, note, actor,
                    extract(epoch from created_at)::float8 as created_ts
               FROM work_item_events e
              WHERE work_item_id = %s
                AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(%s::jsonb) p
                             WHERE starts_with(e.note, p))
              ORDER BY id DESC LIMIT 1)
            UNION ALL
            (SELECT id, work_item_id, state, note, actor,
                    extract(epoch from created_at)::float8 as created_ts
               FROM work_item_events
              WHERE work_item_id = %s
              ORDER BY id LIMIT 1)
            ORDER BY id DESC
            """,
            (int(item_id), json.dumps(list(prefixes)), int(item_id)),
        )

    def handoff_lots(self, assignee, limit) -> list[dict]:
        return self.db.query(
            "SELECT w.*, k.team AS package_team,"
            "       coalesce(nullif(btrim(w.team), ''), nullif(btrim(k.team), ''),"
            "                nullif(btrim(kp.team), ''), nullif(btrim(w.app), '')) AS lot_team,"
            "       EXISTS (SELECT 1 FROM agent_mailbox b"
            "                WHERE b.delivered_at IS NULL AND b.kind = 'request'"
            "                  AND (b.payload -> 'decision') IS NOT NULL"
            "                  AND btrim(b.work_item_id) = w.id::text) AS pending_decision,"
            "       EXISTS (SELECT 1 FROM commitments c"
            "                WHERE c.work_item_id = w.id AND c.kind = 'decision'"
            "                  AND c.status IN ('proposed', 'open')) AS decision_milestone,"
            "       (SELECT extract(epoch from max(m.at))::float8 FROM work_item_milestones m"
            "         WHERE m.work_item_id = w.id AND m.kind = 'frozen') AS frozen_ts"
            "  FROM (SELECT %s,"
            "               to_char(planned_start, 'YYYY-MM-DD') AS planned_start,"
            "               to_char(planned_end, 'YYYY-MM-DD') AS planned_end,"
            "               to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery"
            "          FROM work_items"
            "         WHERE assignee = %%s AND state IN ('intake', 'build')"
            "           AND type <> 'improvement') w"
            "  LEFT JOIN work_packages k ON k.id = w.package_id"
            "  LEFT JOIN work_packages kp ON kp.id = w.package_parent"
            " ORDER BY coalesce(w.priority, 2), w.planned_start NULLS LAST,"
            "          w.planned_end NULLS LAST, w.planned_delivery NULLS LAST,"
            "          w.created_ts, w.id"
            " LIMIT %%s" % item_columns(self.db), (assignee, int(limit)))

    # -- issues GitHub des lots (L126) ------------------------------------------
    def issue_feed(self, limit) -> list[dict]:
        """Lots ouverts ou porteurs d'une `issue_ref` (les plus récents d'abord
        pour la borne), rendus par id croissant, avec l'équipe de leur fiche et
        l'équipe, le chantier et l'hôte de leur assigné."""
        return self.db.query(
            "SELECT w.*, k.team AS package_team, r.team AS assignee_team,"
            "       r.chantier AS assignee_chantier, r.host AS assignee_host,"
            # L157 : dates prévues et fusion, pour l'en-tête de l'issue
            "       (SELECT extract(epoch from min(m.at))::float8 FROM work_item_milestones m"
            "         WHERE m.work_item_id = w.id AND m.kind = 'merged') AS merged_ts"
            "  FROM (SELECT %s,"
            "               to_char(planned_start, 'YYYY-MM-DD') AS planned_start,"
            "               to_char(planned_end, 'YYYY-MM-DD') AS planned_end,"
            "               to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery"
            "          FROM work_items"
            "         WHERE state NOT IN %s OR coalesce(btrim(issue_ref), '') <> ''"
            "         ORDER BY id DESC LIMIT %%s) w"
            "  LEFT JOIN work_packages k ON k.id = w.package_id"
            "  LEFT JOIN agent_registry r ON r.name = w.assignee"
            " ORDER BY w.id" % (item_columns(self.db), _CLOSED), (int(limit),))

    def set_issue_ref(self, item_id, issue_ref, *, current,
                      migration_repo=interface.UNCHECKED) -> bool:
        # #1654 : quand l'appelant a lu la réservation, le CAS la couvre aussi
        # (`IS NOT DISTINCT FROM`, NULL compris) : une réservation prise entre
        # sa lecture et cette écriture ne peut plus passer en silence.
        guard, params = "", ()
        if migration_repo is not interface.UNCHECKED:
            guard = " AND migration_repo IS NOT DISTINCT FROM %s"
            params = (migration_repo,)
        rows = self.db.query(
            "UPDATE work_items SET issue_ref = %s"
            " WHERE id = %s AND coalesce(issue_ref, '') = coalesce(%s, '')" + guard +
            " RETURNING id", (issue_ref, int(item_id), current, *params))
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

    # -- durée estimée et durée réelle (L157) ------------------------------------
    def _estimate_row(self, item_id, minutes, source, actor) -> None:
        self.db.execute(
            "INSERT INTO work_item_estimates (work_item_id, minutes, source, estimated_by)"
            " VALUES (%s, %s, %s, %s)",
            (int(item_id), int(minutes), source or None, actor or ""))

    def set_estimate(self, item_id, minutes, *, source, actor) -> dict | None:
        """Pose l'estimation en vigueur d'un lot NON livré (ni fusionné, ni promu,
        ni fermé), avec sa ligne d'historique, en UNE transaction. Comme une
        date prévue (L96), ce n'est pas une activité du lot : ni journal ni
        `updated_at` (une ré-estimation ne masque pas une stagnation) ;
        l'historique dit qui, quand, combien et d'où. None si le lot est
        introuvable ou déjà terminé."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_items SET estimate_minutes = %%s, estimate_source = %%s,"
                "       estimate_by = %%s, estimate_at = now()"
                " WHERE id = %%s AND state NOT IN %s RETURNING %s" % (_CLOSED, item_columns(self.db)),
                (int(minutes), source or None, actor or "", int(item_id)))
            if not rows:
                return None
            WorkItems(tx)._estimate_row(int(item_id), minutes, source, actor)
            return rows[0]

    def estimates(self, item_id) -> list[dict]:
        """L'historique des estimations d'un lot, de la plus ancienne à la
        plus récente."""
        return self.db.query(
            "SELECT id, work_item_id, minutes, source, estimated_by,"
            "       extract(epoch from at)::float8 AS at_ts"
            "  FROM work_item_estimates WHERE work_item_id = %s ORDER BY at, id",
            (int(item_id),))

    def mark_started(self, agent, item_ids, note) -> list[int]:
        """Un tour de `agent` commence sur ces lots : ceux qui lui sont
        assignés, ouverts et sans début mesuré, reçoivent leur début (une
        seule fois), avec une ligne de journal, en UNE transaction."""
        ids = sorted({int(i) for i in item_ids})
        if not ids:
            return []
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_items SET started_at = now()"
                " WHERE started_at IS NULL AND assignee = %%s AND state NOT IN %s"
                "   AND id IN (%s) RETURNING id, state"
                % (_CLOSED, ", ".join(["%s"] * len(ids))), tuple([agent] + ids))
            for row in rows:
                WorkItems(tx)._event(int(row["id"]), row["state"], note, agent)
            return sorted(int(r["id"]) for r in rows)

    def estimate_history(self, *, app, limit) -> list[dict]:
        """Les lots livrés (jalon `merged` ou `delivered`, #988), les plus
        récents d'abord : début mesuré, fin (fusion ou livraison), estimation en
        vigueur au début (sinon la première posée après, `late`), nombre
        d'estimations posées."""
        sql = (
            "SELECT w.id, w.type, w.app, w.workstream, w.title, w.state, w.assignee,"
            "       w.change_size,"
            "       extract(epoch from w.started_at)::float8 AS started_ts,"
            "       extract(epoch from fus.at)::float8 AS merged_ts,"
            "       coalesce(b.minutes, f.minutes, w.estimate_minutes) AS estimate_minutes,"
            "       coalesce(b.estimated_by, f.estimated_by, w.estimate_by) AS estimate_by,"
            "       coalesce(b.source, f.source, w.estimate_source) AS estimate_source,"
            "       (b.minutes IS NULL AND (f.minutes IS NOT NULL"
            "                               OR w.estimate_minutes IS NOT NULL)) AS late,"
            "       coalesce(n.n, 0)::int AS revisions"
            "  FROM work_items w"
            "  JOIN LATERAL (SELECT min(m.at) AS at FROM work_item_milestones m"
            "                 WHERE m.work_item_id = w.id"
            "                   AND m.kind IN ('merged', 'delivered')) fus"
            "    ON fus.at IS NOT NULL"
            "  LEFT JOIN LATERAL (SELECT e.minutes, e.estimated_by, e.source"
            "                       FROM work_item_estimates e"
            "                      WHERE e.work_item_id = w.id AND w.started_at IS NOT NULL"
            "                        AND e.at <= w.started_at"
            "                      ORDER BY e.at DESC, e.id DESC LIMIT 1) b ON true"
            "  LEFT JOIN LATERAL (SELECT e.minutes, e.estimated_by, e.source"
            "                       FROM work_item_estimates e WHERE e.work_item_id = w.id"
            "                      ORDER BY e.at, e.id LIMIT 1) f ON true"
            "  LEFT JOIN LATERAL (SELECT count(*) AS n FROM work_item_estimates e"
            "                      WHERE e.work_item_id = w.id) n ON true"
            " WHERE (w.state <> 'closed' OR w.close_reason = 'delivered')")
        params: list = []
        if app:
            sql += " AND (w.app = %s OR w.workstream = %s)"
            params += [app, app]
        sql += " ORDER BY fus.at DESC, w.id DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    # -- table de référence des durées (L157b) ----------------------------------
    def publish_reference(self, body, *, actor) -> dict:
        """Publie une table de référence (la plus récente est en vigueur)."""
        rows = self.db.query(
            "INSERT INTO estimate_references (published_by, body) VALUES (%s, %s::jsonb)"
            " RETURNING id, published_by, extract(epoch from published_at)::float8"
            "           AS published_ts",
            (actor or "", json.dumps(body, ensure_ascii=False)))
        return rows[0]

    def reference(self) -> dict | None:
        """La table de référence en vigueur (la dernière publiée), ou None."""
        rows = self.db.query(
            "SELECT id, published_by, body,"
            "       extract(epoch from published_at)::float8 AS published_ts"
            "  FROM estimate_references ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    # -- actual time by phase and merged diff size (L157c) ------------------------
    def measure_candidates(self, *, ids, retry_diff_s, since_s, limit) -> list[dict]:
        sql = (
            "SELECT w.id, w.state, w.assignee, w.branch, w.branch_target, w.change_size,"
            "       extract(epoch from w.started_at)::float8 AS started_ts,"
            "       extract(epoch from fus.at)::float8 AS merged_ts, fus.sha AS merge_sha,"
            "       (m.work_item_id IS NOT NULL) AS measured,"
            "       (m.diff_files IS NOT NULL) AS has_diff,"
            "       (m.ci_minutes IS NOT NULL) AS has_ci"
            "  FROM work_items w"
            "  JOIN LATERAL (SELECT x.at, x.sha FROM work_item_milestones x"
            "                 WHERE x.work_item_id = w.id AND x.kind = 'merged'"
            "                 ORDER BY x.at, x.id LIMIT 1) fus ON true"
            "  LEFT JOIN work_item_measures m ON m.work_item_id = w.id"
            " WHERE w.started_at IS NOT NULL AND w.state <> 'closed'")
        params: list = []
        if ids is not None:
            ids = [int(i) for i in ids]
            if not ids:
                return []
            sql += " AND w.id IN (%s)" % ", ".join(["%s"] * len(ids))
            params += ids
        else:
            # measured without a diff, or without CI though the lot has a
            # branch: retried while recent
            sql += (" AND (m.work_item_id IS NULL OR ((m.diff_files IS NULL"
                    "      OR (m.ci_minutes IS NULL AND w.branch IS NOT NULL))"
                    "      AND fus.at > now() - make_interval(secs => %s)))")
            params.append(float(retry_diff_s))
            if since_s is not None:
                sql += " AND fus.at > now() - make_interval(secs => %s)"
                params.append(float(since_s))
        sql += " ORDER BY fus.at DESC, w.id DESC LIMIT %s"
        params.append(int(limit))
        return self.db.query(sql, tuple(params))

    _MEASURE_COLUMNS = (
        "work_item_id, extract(epoch from measured_at)::float8 AS measured_ts,"
        " extract(epoch from started_at)::float8 AS started_ts,"
        " extract(epoch from merged_at)::float8 AS merged_ts,"
        " total_minutes::float8 AS total_minutes, agent_minutes::float8 AS agent_minutes,"
        " ci_minutes::float8 AS ci_minutes, review_minutes::float8 AS review_minutes,"
        " human_minutes::float8 AS human_minutes, ci_runs, merge_sha, diff_files,"
        " diff_insertions, diff_deletions, measured_size, declared_size, agent_source")

    def save_measure(self, item_id, measure) -> dict:
        m = measure
        rows = self.db.query(
            "INSERT INTO work_item_measures (work_item_id, started_at, merged_at,"
            "  total_minutes, agent_minutes, ci_minutes, review_minutes, human_minutes,"
            "  ci_runs, merge_sha, diff_files, diff_insertions, diff_deletions,"
            "  measured_size, declared_size, agent_source)"
            " VALUES (%s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s,"
            "         %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (work_item_id) DO UPDATE SET measured_at = now(),"
            "  started_at = excluded.started_at, merged_at = excluded.merged_at,"
            + ", ".join(
                # a measure whose CI could not be read never replaces phases
                # measured with a known CI
                "  %s = CASE WHEN excluded.ci_minutes IS NULL"
                " AND work_item_measures.ci_minutes IS NOT NULL"
                " THEN work_item_measures.%s ELSE excluded.%s END" % (c, c, c)
                for c in ("total_minutes", "agent_minutes", "ci_minutes", "review_minutes",
                          "human_minutes", "ci_runs")) + ","
            # même garde pour la provenance des minutes d'agent : garder les
            # phases d'une mesure connue (ci_minutes non nul) garde leur
            # source — 'states' ne devient jamais 'turns' (ni l'inverse) sans
            # que agent_minutes soit recalculé par la même règle
            "  agent_source = CASE WHEN excluded.ci_minutes IS NULL"
            " AND work_item_measures.ci_minutes IS NOT NULL"
            " THEN work_item_measures.agent_source ELSE excluded.agent_source END,"
            "  merge_sha = coalesce(excluded.merge_sha, work_item_measures.merge_sha),"
            "  diff_files = coalesce(excluded.diff_files, work_item_measures.diff_files),"
            "  diff_insertions = coalesce(excluded.diff_insertions,"
            "                             work_item_measures.diff_insertions),"
            "  diff_deletions = coalesce(excluded.diff_deletions,"
            "                            work_item_measures.diff_deletions),"
            "  measured_size = coalesce(excluded.measured_size,"
            "                           work_item_measures.measured_size),"
            "  declared_size = excluded.declared_size"
            " RETURNING " + self._MEASURE_COLUMNS,
            (int(item_id), float(m["started_ts"]), float(m["merged_ts"]),
             m["total_minutes"], m["agent_minutes"], m.get("ci_minutes"),
             m["review_minutes"], m["human_minutes"], m.get("ci_runs"), m.get("merge_sha"),
             m.get("diff_files"), m.get("diff_insertions"), m.get("diff_deletions"),
             m.get("measured_size"), m.get("declared_size"),
             m.get("agent_source")))
        return rows[0]

    def measure(self, item_id) -> dict | None:
        rows = self.db.query(
            "SELECT " + self._MEASURE_COLUMNS + " FROM work_item_measures"
            " WHERE work_item_id = %s", (int(item_id),))
        return rows[0] if rows else None

    def turn_spans(self, item_id) -> list[dict]:
        """#796 : les tours attribués au lot (grand livre, 0423), dans l'ordre
        du début. `start_ts` = `turn_resources.started_at` quand le tour y est
        relié (le tour a pris la main), sinon `turn_costs.started_at` (le
        marqueur, posé avant le lancement) — jamais None : un tour sans aucun
        des deux est omis. `end_ts` = `turn_resources.ended_at` quand le tour a
        rendu ses ressources (la fin réelle), sinon l'écriture de la ligne : une
        ligne réparée après une panne n'allonge donc plus la durée d'un tour
        dont la ressource dit déjà quand il s'est terminé."""
        return self.db.query(
            "SELECT extract(epoch from coalesce(tr.started_at, tc.started_at))::float8"
            "         AS start_ts,"
            "       extract(epoch from coalesce(tr.ended_at, tc.recorded_at))::float8"
            "         AS end_ts,"
            "       tc.turn_id AS turn_id"
            "  FROM turn_costs tc"
            "  LEFT JOIN turn_resources tr ON tr.turn_id = tc.turn_id"
            " WHERE tc.work_item_id = %s"
            "   AND coalesce(tr.started_at, tc.started_at) IS NOT NULL"
            " ORDER BY start_ts, tc.id", (int(item_id),))

    def _event(self, item_id, state, note, actor) -> None:
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor) "
            "VALUES (%s, %s, %s, %s)",
            (item_id, state, note or "", actor or ""),
        )
