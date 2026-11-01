# SPDX-License-Identifier: AGPL-3.0-only
"""Pilote Postgres : lots (`work_items`) et leur journal (`work_item_events`).

L29 (0026) : rattachement au plan (`package_id`, `package_parent`), fermeture
sur fusion constatée (`close_merged`) et fermeture explicite (`close`), chacune
en UNE transaction : transition conditionnelle, journal et jalon.

SQL déplacé tel quel depuis `ameesh.work` (lot L1). Chaque transition
laisse une ligne dans `work_item_events` (qui, quand, pourquoi).
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import contextlib
import datetime as _dt
import functools
import json
import random
import time

from ... import packet_head
from ...db import DbError, in_transaction, is_unique_violation
from ...slo import VEHICLE_EXIT_MARK
from .. import interface
from . import catalog as _catalog
from .work_footprints import (cap_reached_sql as footprint_cap_sql,
                              cap_rows_sql as footprint_cap_rows_sql,
                              held_sql as footprint_held_sql,
                              in_flight_sql as footprint_in_flight_sql,
                              occupying_sql as footprint_occupying_sql,
                              overlap_exists_sql as footprint_overlap_sql,
                              overlap_rows_sql as footprint_overlap_rows_sql)
from .work_dependencies import (_DONE as _DEPENDENCIES_DONE, _LOCK as _DEPENDENCIES_LOCK,
                                _UNPINNED)
from .work_dependencies import current_verdict as _current_verdict
from .work_dependencies import last_freeze_sha as _last_freeze_sha
from .work_dependencies import not_blocking as _not_blocking
from .work_dependencies import start_defers as _start_defers

#: #705 : `__REVIEWER__` est remplacé par `item_columns` — la colonne
#: `reviewer` n'existe qu'après la migration 0421, et la base de prod peut
#: être en 0373 quand ce code tourne (l'exécuteur migre à la mise à jour).
_COLUMNS_TEMPLATE = """
    id, type, source, app, title, body, issue_ref, workstream, state, assignee,
    __REVIEWER__,
    __EXTERNAL__,
    __LIFECYCLE__,
    loops, budget_usd, spent_usd, package_id, package_parent, pr_ref, close_reason,
    superseded_by, delegated_by,
    expected_value, value_score, priority, team, required_capabilities,
    branch, branch_target, branch_head, migration, migration_repo,
    __MIGRATIONS__,
    migration_landed, migration_landed_repo,
    extract(epoch from branch_head_at)::float8 as branch_head_ts,
    partial_merge, extract(epoch from partial_seen_at)::float8 as partial_seen_ts,
    estimate_minutes, estimate_source, estimate_by, change_size,
    extract(epoch from estimate_at)::float8 as estimate_ts,
    extract(epoch from started_at)::float8 as started_ts,
    extract(epoch from created_at)::float8 as created_ts,
    extract(epoch from updated_at)::float8 as updated_ts,
    extract(epoch from closed_at)::float8  as closed_ts,
    extract(epoch from delegated_at)::float8 as delegated_ts,
    extract(epoch from due_at)::float8 as due_ts,
    __WAKE__
"""


#: #1979 : la marque « verdict non requis » de la colonne `reviewer`
#: (`work.waive_review`) ; un nom d'agent ne commence jamais par `-`.
REVIEWER_NONE = "-none"

#: #5016 : les seuls champs de la ligne du lot dont la porte des avis dépend
#: (dépôt, cible, auteurs, véhicule de paquet) et la dispense de re-verdict
#: (`reviewer`/`reviewers`, lue par `_waiver`). Une note concurrente ne déplace
#: que `updated_at` et le journal : comparer la ligne entière annulait un
#: quorum déjà acquis, sans réessai, et laissait le lot en qa.
_GATE_ITEM_FIELDS = ("id", "app", "type", "assignee", "package_id",
                     "branch", "branch_target", "pr_ref", "reviewer",
                     "reviewers")


def _gate_item(item) -> dict:
    """Projection stable de la ligne pour les gardes de validation (#5016)."""
    item = item or {}
    return {key: item.get(key) for key in _GATE_ITEM_FIELDS}


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


def has_wake_at(db) -> bool:
    """#2466 : les colonnes `wake_at`/`wake_claim_at` existent-elles (0466) ?

    Même règle que `has_reviewer` : seule la réponse positive est mise en
    cache ; un exécuteur démarré avant la migration voit les colonnes dès
    qu'elles sont appliquées, sans redémarrage. Les deux colonnes viennent de
    la même migration : les exiger ensemble évite d'utiliser un schéma
    intermédiaire (wake_at seul) avec le code du claim."""
    if getattr(db, "_has_wake_at", False):
        return True
    rows = db.query(
        "SELECT count(*)::int AS n FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = 'work_items'"
        "   AND column_name IN ('wake_at', 'wake_claim_at')")
    found = bool(rows) and int(rows[0]["n"]) == 2
    if found:
        try:
            db._has_wake_at = True
        except Exception:  # objet sans attributs (proxy) : on relira
            pass
    return found


def has_migration_slots(db) -> bool:
    """#2481 : la table des créneaux de migration (#711, 0419) existe-t-elle ?

    Seule la réponse POSITIVE est mise en cache, comme `has_reviewer` : une
    base migrée pendant la vie du démon voit ses créneaux sans redémarrage.
    Sur une base d'avant 0419, la liste se replie sur le champ scalaire."""
    if getattr(db, "_has_migration_slots", False):
        return True
    found = bool(db.query(
        "SELECT 1 AS ok FROM information_schema.tables"
        " WHERE table_schema = current_schema()"
        "   AND table_name = 'work_migration_reservations'"))
    if found:
        try:
            db._has_migration_slots = True
        except Exception:
            pass
    return found


def item_columns(db, ref: str | None = None, *, row_json: str | None = None) -> str:
    """Les colonnes d'un lot ; `reviewer` quand 0421 est appliquée, `''` sinon
    (les lectures d'avant la migration ne cassent pas, #705).

    `ref` (le nom de la table dans la requête, « work_items ») lit `reviewer`
    par `to_jsonb`, sans sonde de colonne : les commandes à budget
    d'allers-retours (L61 : `work list`, `progress`) n'en paient pas un de
    plus. `to_jsonb` rend NULL sur une base d'avant 0421, d'où le coalesce.
    #2466 : `wake_ts` suit le même chemin, NULL sur une base d'avant 0466.

    #2481 : `migrations` est la liste des créneaux réservés du lot (JSONB),
    lue dans `work_migration_reservations` par une sous-requête corrélée —
    une seule requête, pas d'aller-retour par lot. La liste n'est lue que sur
    la ligne complète (sans `ref`) : le chemin `ref` est celui des lectures à
    budget L61, et la sonde de table y coûterait justement l'aller-retour
    qu'il économise. Ces vues rendent `[]` et se replient sur le scalaire
    (`work.migration_line`), jamais sur une erreur : sur une base d'avant
    0419, la liste est de toute façon vide.

    #3741 : `row_json` est une expression qui porte déjà `to_jsonb` de la
    ligne, calculé une fois par la requête de l'appelant ; les colonnes lues
    par `to_jsonb` la lisent au lieu de recalculer l'appel (0,6 ms par ligne
    sous charge, trois fois par ligne sur un flux de 3 500 lots)."""
    doc = row_json or "to_jsonb(%s)" % (ref or "work_items")
    if ref:
        reviewer = "coalesce(%s->>'reviewer', '') AS reviewer" % doc
    elif has_reviewer(db):
        reviewer = "reviewer"
    else:
        reviewer = "''::text AS reviewer"
    # to_jsonb keeps reads compatible with schemas predating 0456, without
    # another catalogue round trip (including bounded progress/list reads).
    reviewers = "%s->'reviewers' AS reviewers" % doc
    # #3447 : même compatibilité — la colonne n'existe qu'après 0482.
    external = "%s->>'external_executor' AS external_executor" % doc
    wake = ("extract(epoch from (%s->>'wake_at')::timestamptz)::float8"
            " as wake_ts" % doc)
    if ref:
        migrations = "'[]'::jsonb AS migrations"
    elif has_migration_slots(db):
        migrations = (
            "coalesce((SELECT jsonb_agg(jsonb_build_object("
            "'number', r.number, 'repo', r.target_repo, 'slot', r.slot)"
            " ORDER BY r.number, r.slot)"
            " FROM work_migration_reservations r"
            " WHERE r.work_item_id = work_items.id AND r.status = 'reserved'), '[]'::jsonb)"
            " AS migrations")
    else:
        migrations = "'[]'::jsonb AS migrations"
    return (_COLUMNS_TEMPLATE
            .replace("__REVIEWER__", reviewer + ", " + reviewers)
            .replace("__EXTERNAL__", external)
            .replace("__LIFECYCLE__", "%s->>'legacy_state' AS legacy_state, "
                     "%s->>'workflow_stage' AS workflow_stage" % (doc, doc))
            .replace("__WAKE__", wake)
            .replace("__MIGRATIONS__", migrations))


def has_reviewers(db) -> bool:
    if getattr(db, "_has_reviewers", False):
        return True
    found = bool(db.query(
        "SELECT 1 AS ok FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = 'work_items'"
        " AND column_name = 'reviewers'"))
    if found:
        try:
            db._has_reviewers = True
        except Exception:
            pass
    return found

#: #2346 : le réglage lu par le trigger d'audit des relecteurs (migration 0462)
_AUDIT_SETTING = "ameesh.reviewer_audit"


@contextlib.contextmanager
def python_reviewer_audit(db):
    """#2346 : le drapeau qui tait le trigger d'audit ne couvre QUE l'UPDATE
    dont le code Python journalise lui-même le retrait.

    `SET LOCAL` vaut pour toute la transaction : un retrait SQL direct, plus
    loin dans la même transaction (même après un `work.assign` imbriqué dans
    la transaction d'un appelant), perdait sa ligne `db:reviewers_sync`. Le
    drapeau est posé juste avant l'UPDATE et la valeur précédente rendue juste
    après — sauvegarde/restauration, pas un effacement : un appel imbriqué
    garde l'état de son appelant. Sans la migration 0456, rien à faire."""
    if not has_reviewers(db):
        yield
        return
    previous = db.query(
        "SELECT current_setting(%s, true) AS value", (_AUDIT_SETTING,))
    previous = (previous[0].get("value") if previous else None) or ""
    db.execute("SELECT set_config(%s, 'python', true)", (_AUDIT_SETTING,))
    try:
        yield
    finally:
        try:
            db.execute("SELECT set_config(%s, %s, true)",
                       (_AUDIT_SETTING, previous))
        except Exception:  # noqa: BLE001 - transaction déjà en échec, rien à rendre
            pass

#: L160d (0057): a prepared conflict resolution
QUEUE_COLUMNS = """
    q.work_item_id, q.target, q.frozen_sha, q.state, q.rank, q.landing_sha, q.reason,
    q.queued_by, q.source, q.generation, q.frozen_id,
    EXISTS (SELECT 1 FROM integration_train_queue_receipts tr
            WHERE tr.work_item_id = q.work_item_id AND tr.generation = q.generation
              AND tr.kind = 'packet') AS packet_allocated,
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
_CLOSED = f"('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
#: #3370 : états d'un lot qui n'est plus ouvert — la correction de priorité
#: les refuse, relus sous le verrou de ligne (une transition intercalée entre
#: le contrôle de `work.set_priority` et l'écriture ne passe plus).
_CLOSED_PRIORITY = lifecycle.COMPLETED
#: L165b : un lot qui attend un autre que son agent (`qa`, `waiting_human`,
#: `blocked`) ne l'occupe pas — même liste que `operations.listing` et
#: `sous_utilisation.LOTS_WAITING_ON_OTHERS`
_WAITING_ON_OTHERS = f"('{lifecycle.QA}', '{lifecycle.VALIDATED}', '{lifecycle.WAITING_HUMAN}', '{lifecycle.BLOCKED}')"
#: #443, #478 : réservation automatique du prochain numéro libre — un budget
#: de temps (et non plus un nombre d'essais) et une attente plein jitter
#: (`uniform(0, min(plafond, base·2^n))`) bornée au budget restant, pour que
#: 20 réservations simultanées se départagent sans épuiser les essais ;
#: épuisé, `ReservationContention` (WorkError lisible).
_RESERVE_BUDGET_S = 30.0
_RESERVE_WAIT_BASE_S = 0.005
_RESERVE_WAIT_MAX_S = 0.5
#: #879 : pendant la fenêtre de déploiement mixte, un client pré-0419 verrouille
#: la ligne du lot puis le dépôt (trigger), l'ordre inverse du code courant :
#: PostgreSQL tue l'une des deux transactions (40P01). La victime rejouée dans
#: le bon ordre retrouve les verrous libérés par l'autre. Reprise bornée : au
#: delà, l'erreur remonte telle quelle.
_DEADLOCK_ATTEMPTS = 5
_DEADLOCK_WAIT_BASE_S = 0.05
_DEADLOCK_WAIT_MAX_S = 0.5
#: l'index unique (dépôt, numéro) de L122 : lui seul fait une contention
MIGRATION_INDEX = "work_items_migration_repo_idx"
#: #711/#2481 : l'index partiel des créneaux réservés (dépôt, cible, numéro) ;
#: un second numéro du même lot y est inséré comme créneau supplémentaire
MIGRATION_SLOTS_INDEX = "work_migration_reservations_active_idx"
#: l'index unique (dépôt, numéro atterri) de #1567 : deux lots d'un même dépôt
#: ne nomment jamais le même numéro atterri
MIGRATION_LANDED_INDEX = "work_items_migration_landed_idx"
#: #3836 : un gel prend le verrou consultatif du dépôt de migration et, par la
#: transition d'état, celui du projet. Il ne doit jamais attendre un verrou
#: plusieurs minutes en les tenant : une attente de 174 s sur le lookup de
#: provenance de #3820 a expiré sur `statement_timeout` et laissé une
#: transaction `idle in transaction` qui a bloqué les écritures du mesh. Le
#: `lock_timeout` fait échouer l'attente, la transaction du jalon est annulée
#: (donc ses verrous relâchés), et l'appelant relance après le détenteur.
FREEZE_LOCK_TIMEOUT_MS = 15000
#: L233b : une reprise sur pause budget à la fois pour tout le mesh
_HANDOFF_LOCK = ("SELECT pg_advisory_xact_lock("
                 "hashtextextended(current_schema() || ':budget_handoff', 0))")
#: §4 canon : le verrou consultatif d'un agent sérialise la prise sous la
#: limite d'en-cours (`assign_capped`) — le schéma entre dans la clé, deux
#: schémas du même serveur (tests, registres) ne se bloquent pas.
_WIP_LOCK = ("SELECT pg_advisory_xact_lock("
             "hashtextextended(current_schema() || %s, 0))")
_WIP_LOCK_PREFIX = "ameesh-wip:"


def requeued_sql(alias: str) -> str:
    """§4 : le lot `alias` a-t-il été RENDU à la file — un `build` dont le
    journal porte `interface.RETURNED_NOTE` APRÈS sa dernière attribution
    (`interface.ASSIGN_NOTE_PREFIXES`). Un lot `build` sans assigné qui n'a pas
    été rendu (prérequis, travail confié par courrier) n'est donc pas prenable.

    Fragment SQL booléen, jamais dans une chaîne passée à `%` (ses `%s` sont
    des paramètres) ; il attend les paramètres `REQUEUED_PARAMS`, dans l'ordre
    du texte : la note de retour, puis un paramètre par préfixe d'attribution
    (un `unnest(%s::text[])` recevrait un littéral de tableau dépendant du
    pilote : chaque préfixe est donc son propre `%s`)."""
    starts = " OR ".join(["starts_with(d.note, %s)"] * len(interface.ASSIGN_NOTE_PREFIXES))
    return ("EXISTS (SELECT 1 FROM work_item_events r"
            " WHERE r.work_item_id = {a}.id AND starts_with(r.note, %s)"
            "   AND r.id > coalesce((SELECT max(d.id) FROM work_item_events d"
            "                        WHERE d.work_item_id = {a}.id"
            "                          AND (" + starts + ")), 0))"
            ).format(a=alias)


#: les paramètres du fragment `requeued_sql`, dans l'ordre du texte
REQUEUED_PARAMS = (interface.RETURNED_NOTE,) + tuple(interface.ASSIGN_NOTE_PREFIXES)

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


#: #1693 (N4)/#2317 (N2) : un jalon `merged` n'est effectif que si aucune
#: correction `merged → build` ne l'a annulé — une correction au moins aussi
#: récente que le jalon. Les jalons annulés RESTENT dans la table (aucune
#: reprise destructive) ; la lecture calcule le jalon effectif depuis le
#: journal. La correction se reconnaît à la TRANSITION du journal (fonction
#: 0463), jamais au texte libre de sa note.
def _effective_merged(alias: str) -> str:
    return ("NOT agent_mesh_merged_annulled(%s.work_item_id, %s.at)"
            % (alias, alias))


#: Les délais d'un lot, dérivés de ses jalons (L10, R19). Forme **stable** :
#: L24 (`ameesh progress`) et Nexlink la consomment telle quelle ; une étape qui
#: n'a pas eu lieu est NULL, jamais zéro. Le premier gel et le premier verdict
#: font foi ; `verdicts` et `blocked_verdicts` comptent les tours de revue.
_DELAYS_SELECT = """
    SELECT w.id AS work_item_id, w.state, w.assignee, w.title,
           to_jsonb(w)->>'legacy_state' AS legacy_state,
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
            SELECT max(m.at) AS at FROM work_item_milestones m
             WHERE m.work_item_id = w.id AND m.kind = 'merged'
               AND __MERGED_EFFECTIVE__) fus ON true
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
""".replace("__MERGED_EFFECTIVE__", _effective_merged("m"))


def _land_default_slot(tx, item_id, number, repo) -> None:
    """#4097 : le créneau `default` atterri suit le numéro réellement posé.

    Le déclencheur 0467 lande le créneau au numéro RÉSERVÉ quand le lot passe
    `merged` ; un paquet qui renumérote (602 → 599) laissait donc `landed` à
    602, numéro jamais posé sur la cible. Le numéro déclaré (`migration_landed`,
    #1567) le remplace, dans la transaction qui l'écrit. Seul l'index des
    créneaux `reserved` est unique : aucune course d'allocation ici.
    #4100 : un créneau relâché par le relevé (aucun numéro posé sûr) est
    rétabli `landed` au numéro déclaré ensuite."""
    if not has_migration_slots(tx):
        return
    tx.execute(
        "UPDATE work_migration_reservations SET number = %s, status = 'landed',"
        "       updated_at = now()"
        " WHERE work_item_id = %s AND slot = 'default'"
        "   AND status IN ('landed', 'released') AND target_repo = %s"
        "   AND (number <> %s OR status = 'released')",
        (int(number), int(item_id), "github.com/" + repo, int(number)))


def _release_landed_slot(tx, item_id, number, repo) -> None:
    """#4100 : le créneau `default` atterri à un numéro que la cible ne porte
    pas, et que le relevé n'a pu suivre sous aucun autre (aucun candidat, ou
    plusieurs) : il est relâché, jamais deviné. La note de l'appelant le dit."""
    if not has_migration_slots(tx):
        return
    tx.execute(
        "UPDATE work_migration_reservations SET status = 'released', updated_at = now()"
        " WHERE work_item_id = %s AND slot = 'default' AND status = 'landed'"
        "   AND target_repo = %s AND number = %s",
        (int(item_id), "github.com/" + repo, int(number)))


class _Refrozen(Exception):
    """Annule la fermeture : un nouveau gel a été validé pendant l'attente."""


def _deadlock_victim(exc) -> bool:
    """40P01, reconnu par le seul code du serveur (jamais le texte)."""
    return isinstance(exc, DbError) and exc.sqlstate == '40P01'


def _retry_deadlock(writer):
    """Rejoue `writer` quand sa transaction est victime d'un interblocage.

    Chaque essai ouvre sa propre transaction : la reprise reprend les verrous
    dans l'ordre du code courant (dépôt avant ligne de lot). Le nombre
    d'essais est borné, et seule la victime 40P01 est rejouée.

    Jamais en transaction appelante (#879 B2) : sous psql l'erreur a fermé la
    session, et la reprise en rouvrirait une neuve, hors du bloc atomique de
    l'appelant (`import_reservations`) ; sous psycopg la transaction est
    avortée. C'est à l'appelant de décider de l'échec ou de la reprise."""

    @functools.wraps(writer)
    def wrapped(self, *args, **kwargs):
        for attempt in range(_DEADLOCK_ATTEMPTS):
            try:
                return writer(self, *args, **kwargs)
            except DbError as exc:
                if (not _deadlock_victim(exc) or attempt == _DEADLOCK_ATTEMPTS - 1
                        or in_transaction(self.db)):
                    raise
                time.sleep(random.uniform(
                    0.0, min(_DEADLOCK_WAIT_MAX_S, _DEADLOCK_WAIT_BASE_S * 2 ** attempt)))
        raise AssertionError('unreachable')

    return wrapped


def _lock_migration_writer(tx, item_id, proposed=None):
    """Take repository locks before the lot and recheck its observed scope."""
    before = tx.query('SELECT migration_repo FROM work_items WHERE id = %s', (int(item_id),))
    if not before:
        return False
    previous = before[0]['migration_repo']
    repositories = {repo for repo in (previous, proposed) if repo}
    # A legacy reservation may move between repositories. Lock both old and
    # new in canonical order, including release's OLD repository.
    for repo in sorted(repositories):
        tx.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                   "current_schema() || ':migration-repo:' || %s, 0))", ('github.com/' + repo,))
    after = tx.query('SELECT migration_repo FROM work_items WHERE id = %s FOR NO KEY UPDATE',
                     (int(item_id),))
    if not after:
        return False
    # A concurrent legacy reservation can change the repository while we
    # wait. Abort rather than acquire a new repo lock after locking the lot.
    if after[0]['migration_repo'] != previous:
        raise DbError('lot migration repository changed; retry')
    return True


def _unknown_marker(tx, item, footprint, unknown):
    """#4818 : l'empreinte d'intake d'un lot de la file dont l'analyse est
    inconnue — la politique de son dépôt, lue dans la transaction de création
    (verrou de la garde tenu par l'appelant), posée sur `item`
    (`footprint_policy`) ; sous `repository`, le marqueur `.` (dépôt entier)."""
    paths = list(footprint or ())
    if unknown:
        from .work_footprints import unknown_policy_sql
        item["footprint_policy"] = unknown_policy_sql(tx, item["id"])
        if not paths and item["footprint_policy"] == "repository":
            paths = ["."]
    return paths


class WorkItems(interface.WorkItems):

    @_retry_deadlock
    def set_app(self, item_id, app, *, actor, if_empty=False, assignee=None) -> dict | None:
        with self.db.transaction() as tx:
            if not _lock_migration_writer(tx, item_id):
                return None
            rows = tx.query("SELECT %s FROM work_items WHERE id = %%s FOR NO KEY UPDATE"
                            % item_columns(self.db), (int(item_id),))
            if not rows:
                return None
            item = rows[0]
            if if_empty and ((item.get("app") or "").strip()
                             or item["state"] in lifecycle.COMPLETED
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

    @_retry_deadlock
    def set_type(self, item_id, type, *, current, actor) -> dict | None:  # noqa: A002
        """#2954 : retype journalé, conditionné au type lu (compare-and-set)."""
        with self.db.transaction() as tx:
            if not _lock_migration_writer(tx, item_id):
                return None
            rows = tx.query("SELECT %s FROM work_items WHERE id = %%s FOR NO KEY UPDATE"
                            % item_columns(self.db), (int(item_id),))
            if not rows or rows[0].get("type") != current:
                return None
            if current == type:
                return rows[0]
            rows = tx.query("UPDATE work_items SET type = %%s, updated_at = now()"
                            " WHERE id = %%s AND type = %%s RETURNING %s"
                            % item_columns(self.db),
                            (type, int(item_id), current))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"],
                                 "type %r → %r" % (current, type), actor)
            return rows[0]

    @_retry_deadlock
    def set_priority(self, item_id, priority, *, current, reason, actor) -> dict | None:
        """#3370 : priorité journalée, conditionnée à la valeur lue (NULL inclus)
        et à un lot encore ouvert, relu sous le verrou de ligne."""
        with self.db.transaction() as tx:
            rows = tx.query("SELECT %s FROM work_items WHERE id = %%s FOR NO KEY UPDATE"
                            % item_columns(self.db), (int(item_id),))
            if not rows or rows[0].get("priority") != current:
                return None
            if rows[0].get("state") in _CLOSED_PRIORITY:
                return None
            if current == priority:
                return rows[0]
            rows = tx.query(
                "UPDATE work_items SET priority = %%s, updated_at = now()"
                " WHERE id = %%s AND priority IS NOT DISTINCT FROM %%s RETURNING %s"
                % item_columns(self.db),
                (int(priority), int(item_id), current))
            if not rows:
                return None
            note = "priority %s → %s" % (
                "none" if current is None else current, priority)
            if reason:
                note += " — %s" % reason
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
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
            parent_lot=None, unique_ref=False, external_executor=None,
            footprint=None, footprint_unknown=False) -> dict:
        """Crée le lot en `intake`, puis sa première ligne de journal ; L157 :
        avec sa durée estimée (et sa ligne d'historique), en UNE transaction.
        #540 : `queued_note` (voir l'interface).
        L539 : `mark_note` marque `mark_item_id` dans la **même** transaction,
        si bien qu'un échec de la marque annule la création (et l'inverse).
        La ligne marquée est verrouillée **avant** l'insertion et la
        `issue_ref` est relue sous ce verrou : deux appels concurrents ne
        peuvent pas créer deux lots pour la même référence (revue #544 r2).

        #2954 : `unique_ref` prend d'abord un verrou consultatif de
        transaction sur `issue_ref` puis relit la référence, quel que soit
        l'état des lots ; deux hôtes qui créent l'incident du même run se
        sérialisent et le second reçoit `DbError` (son appelant relit le
        gagnant). Le verrou est pris avant les lignes de lots : aucun chemin
        ne prend l'ordre inverse.

        #3607 : la relecture couvre aussi la `source` exacte, pas seulement
        `issue_ref` — l'identité d'une exécution y vit aussi
        (`ci-incident:<repo>:<run>`) et la synchro des tickets peut avoir
        déplacé l'`issue_ref` du gagnant vers son ticket miroir pendant
        l'attente du verrou.

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
        suite avec l'attribution de son parent.

        #4173 : `footprint` (chemins normalisés) est l'empreinte d'intake,
        écrite dans la même transaction. Un lot créé attribué occupe aussitôt
        son empreinte : sous le verrou du graphe (pris avant toute ligne de
        lot, ordre de la prise), une empreinte qui chevauche celle d'un lot
        occupant du même dépôt lève `FootprintOverlap` et annule la création."""
        with self.db.transaction() as tx:
            if unique_ref and str(issue_ref or "").strip():
                # #2954 : deux watchers (deux hôtes) peuvent ouvrir l'incident
                # du même run dans la même fenêtre. Le verrou est pris avant
                # toute ligne de lot ; la relecture ci-dessous, après le
                # commit du premier, voit sa ligne.
                tx.query("SELECT pg_advisory_xact_lock(hashtextextended("
                         "lower(%s), 0))", (issue_ref,))
            if (assignee and (footprint or str(app or "").strip())) or footprint_unknown:
                # #4818 : le plafond de lots en vol garde aussi une création
                # attribuée sans empreinte ; une analyse inconnue relit la
                # politique sous ce verrou, celui de `set_policy` (B3 de cx1)
                tx.execute(_DEPENDENCIES_LOCK)
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
            if unique_ref and str(issue_ref or "").strip():
                # #2954 : sous le verrou consultatif, la relecture voit le
                # premier créateur (son commit précède l'acquisition) ; un lot
                # fermé garde l'identité de l'exécution, comme `_open`.
                # #3607 : l'identité vit aussi dans `source`, exactement — la
                # synchro des tickets peut avoir déplacé l'`issue_ref` du
                # gagnant vers son ticket miroir pendant l'attente du verrou.
                duplicate = tx.query(
                    "SELECT id FROM work_items"
                    " WHERE lower(issue_ref) = lower(%s) OR lower(source) = lower(%s)"
                    " ORDER BY id LIMIT 1", (issue_ref, issue_ref))
                if duplicate:
                    raise DbError("reference %s is already carried by lot #%d"
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
                     change_size, external_executor)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'intake', %s, %s, %s, %s, %s,
                        %s, %s, %s, CASE WHEN %s THEN now() END, %s, %s)
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)),
                (type, source, app, title, body, issue_ref, workstream, assignee,
                 budget_usd, package_id, package_parent, branch, branch_target,
                 None if priority is None else int(priority),
                 estimate_minutes, estimate_source if estimate_minutes else None,
                 (actor or "") if estimate_minutes else None, bool(estimate_minutes),
                 change_size or None, external_executor or None),
            )
            item = rows[0]
            if estimate_minutes:
                WorkItems(tx)._estimate_row(int(item["id"]), estimate_minutes,
                                            estimate_source, actor)
            WorkItems(tx)._event(int(item["id"]), "intake", note, actor)
            if footprint_unknown:
                from .work_footprints import unknown_policy_sql
                item["footprint_policy"] = unknown_policy_sql(tx, item["id"])
                if not footprint and item["footprint_policy"] == "repository":
                    footprint = ["."]
            if footprint:
                WorkItems(tx)._intake_footprint(item["id"], footprint, actor)
                if assignee:
                    WorkItems(tx)._footprint_guard(item["id"])
            elif assignee:
                # #4818 : sans empreinte, le plafond de lots en vol du dépôt
                # garde encore la création attribuée
                WorkItems(tx)._footprint_guard(item["id"])
            if behind is not None:
                item["forced_behind" if forced_note else "queued_behind"] = behind
            if parent_lot is not None:
                # #1193: the explicit parent of a suite lot is linked in the
                # same transaction as the creation — never a forgotten edge,
                # and an edge failure never leaves the lot ready. The row was
                # locked above, before the agent's lock (#540, ordre commun).
                if parent[0]["state"] not in lifecycle.COMPLETED:
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

    def lock(self, item_id) -> dict | None:
        """#2744 : verrouille la ligne du lot `FOR UPDATE` pour la transaction
        courante (None s'il n'existe pas). Verrou commun avec les notes de
        retenue (`work.note` le prend avant `note`) : la lecture des hold et la
        fermeture par contenu ne peuvent plus s'entrelacer."""
        # Même ordre que close_merged/move/réservation : dépôt avant lot.
        # Prendre la ligne d'abord inverserait cet ordre lors de la fermeture.
        if not _lock_migration_writer(self.db, item_id):
            return None
        rows = self.db.query(
            "SELECT %s FROM work_items WHERE id = %%s FOR UPDATE" % item_columns(self.db),
            (int(item_id),))
        return rows[0] if rows else None

    def lock_workflow_activation(self, project):
        self.db.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext('ameesh-module-project'),"
            " hashtext(%s))", (project,))

    def adopt_workflow(self, item_id, *, ref, version, stage, state, actor):
        self.db.execute(
            "UPDATE work_items SET workflow_ref = %s, workflow_version = %s,"
            " workflow_stage = %s, updated_at = now() WHERE id = %s",
            (ref, int(version), stage, int(item_id)))
        self._event(int(item_id), state,
                    "pipeline adopted: %s version %s stage %s" % (ref, version, stage), actor)

    def get(self, item_id) -> dict | None:
        # #1922/B5 : `workflow_ref` (0452) est porté par la lecture, comme
        # `reviewer` (#705) l'est par `item_columns` — `to_jsonb` rend '' sur
        # une base d'avant la migration. Les commandes historiques le lisent
        # par cette interface : un SELECT brut serait refusé sur une connexion
        # d'exécuteur médié (`work move`/`note`/`close` d'une VM).
        # #2130/D : `workflow_stage` et `workflow_version` sont portés par la
        # même lecture — `project_lots` y passe pour les lots anciens/orphelins
        # retrouvés par leur marqueur, et la projection a besoin des trois
        # champs (sinon `projection-stage-missing` sur un lot sain). Une base
        # pré-0452 rend '', NULL, 0 : aucune sonde de colonne.
        rows = self.db.query(
            "SELECT %s,"
            "       coalesce(to_jsonb(work_items)->>'workflow_ref', '') AS workflow_ref,"
            "       to_jsonb(work_items)->>'workflow_stage' AS workflow_stage,"
            "       coalesce((to_jsonb(work_items)->>'workflow_version')::bigint, 0)"
            "         AS workflow_version"
            " FROM work_items WHERE id = %%s" % item_columns(self.db), (int(item_id),))
        return rows[0] if rows else None

    def items(self, *, state, assignee, limit) -> list[dict]:
        # L61 : lecture de `reviewer` sans sonde, cette commande est bornée
        sql = "SELECT %s FROM work_items WHERE true" % item_columns(self.db, "work_items")
        params: list = []
        if state:
            sql += (" AND coalesce(to_jsonb(work_items)->>'legacy_state', state) = %s"
                    if state in ("merged", "promoted") else " AND state = %s")
            params.append(state)
        if assignee:
            sql += " AND assignee = %s"
            params.append(assignee)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(None if limit is None else int(limit))
        return self.db.query(sql, tuple(params))

    def items_missing_ref(self, *, states, ref_prefix, limit,
                          attempt_prefix="", retry_s=0, terminal_prefix="",
                          max_attempts=0, since=None) -> list[dict]:
        """L539 (#1104) : les lots dont `ref_prefix + id` n'existe pas encore.

        L'anti-jointure est faite **avant** la borne : `limit` borne les
        manquants rendus, pas un balayage de lots déjà pourvus (R2 #1104
        P2-2 : vingt pourvus plus récents ne cachent plus un manquant ancien).
        Les états forment un seul ensemble ordonné ; `id` départage deux
        `updated_at` égaux, sans quoi la sélection ne serait pas stable.

        `attempt_prefix`/`retry_s` (binôme mesh-design, 14/10) : une tentative
        de rattrapage sans création journalise `attempt_prefix<id>` (voir
        `log_review_attempt`) ; un lot dont la tentative est plus récente que
        `retry_s` est exclu **avant** la borne, sinon les mêmes candidats
        confirmés (non-light, doutes, erreurs) consommeraient chaque passage et
        affameraient les plus anciens. Le délai passé, la tentative redevient
        sélectionnable : le marqueur est un délai, pas une suppression.

        `since` (arbitrage pilote du 15/10) : ne rend que les lots dont le
        jalon `merged` est à `since` ou après — la borne d'activation, jamais
        l'historique. `terminal_prefix`/`max_attempts` : une marque terminale
        exclut toujours, et un candidat qui a déjà `max_attempts` tentatives
        n'est plus sélectionné (traitement borné, jamais perpétuel).

        Le filtre lui-même (`_missing_ref_gates`) est partagé avec
        `missing_ref_eligible`, pour qu'un lot donné reçoive la même vérité que
        le balayage (B1 de la R1 #1104)."""
        wanted = [str(s) for s in (states or ())]
        if not wanted:
            return []
        params: list = list(wanted)
        sql = (
            "SELECT __COLUMNS__ FROM work_items w"
            " WHERE w.state IN (__STATES__)"
            + self._missing_ref_gates(
                params, ref_prefix=ref_prefix, attempt_prefix=attempt_prefix,
                retry_s=retry_s, terminal_prefix=terminal_prefix,
                max_attempts=max_attempts, since=since)
            + " ORDER BY w.updated_at DESC, w.id DESC LIMIT %s"
        )
        params.append(int(limit))
        return self.db.query(
            sql.replace("__COLUMNS__", item_columns(self.db, "w")).replace(
                "__STATES__", ", ".join(["%s"] * len(wanted))),
            tuple(params))

    @staticmethod
    def _missing_ref_gates(params: list, *, ref_prefix, attempt_prefix="",
                           retry_s=0, terminal_prefix="", max_attempts=0,
                           since=None) -> str:
        """L539 (#1104) : les conditions « relecture encore due » d'un lot.

        Rendues en SQL pour être ajoutées après `WHERE …, ` et complète
        `params` dans l'ordre du texte produit — le même filtre sert au
        balayage borné (`items_missing_ref`) et à un lot donné
        (`missing_ref_eligible`)."""
        sql = (
            "   AND NOT EXISTS (SELECT 1 FROM work_items r"
            "                    WHERE r.issue_ref = %s || w.id::text)"
        )
        params.append(str(ref_prefix))
        if since is not None:
            sql += (
                "   AND EXISTS (SELECT 1 FROM work_item_milestones m"
                "                WHERE m.work_item_id = w.id AND m.kind = 'merged'"
                "                  AND m.at >= %s"
                "                  AND " + _effective_merged("m") + ")"
            )
            params.append(since)
        if terminal_prefix:
            sql += (
                "   AND NOT EXISTS (SELECT 1 FROM work_item_events e"
                "                    WHERE e.work_item_id = w.id"
                "                      AND split_part(e.note, ' ', 1) = %s || w.id::text)"
            )
            params.append(str(terminal_prefix))
        if attempt_prefix:
            if int(max_attempts) > 0:
                sql += (
                    "   AND (SELECT count(*) FROM work_item_events e"
                    "         WHERE e.work_item_id = w.id"
                    "           AND split_part(e.note, ' ', 1) = %s || w.id::text) < %s"
                )
                params += [str(attempt_prefix), int(max_attempts)]
            if float(retry_s) > 0:
                sql += (
                    "   AND NOT EXISTS (SELECT 1 FROM work_item_events e"
                    "                    WHERE e.work_item_id = w.id"
                    "                      AND split_part(e.note, ' ', 1) = %s || w.id::text"
                    "                      AND e.created_at > now() - make_interval(secs => %s))"
                )
                params += [str(attempt_prefix), float(retry_s)]
        return sql

    def missing_ref_eligible(self, item_id, *, ref_prefix, attempt_prefix="",
                             retry_s=0, terminal_prefix="", max_attempts=0,
                             since=None) -> bool:
        """L539 (#1104, B1 de la R1) : le lot `item_id` passerait-il la
        sélection de `items_missing_ref` ?

        Même filtre, une seule ligne : le chemin `already` de `close_merged`
        ne relance l'ouverture que pour un lot que le rattrapage borné
        sélectionnerait encore — jamais pour l'historique d'avant l'activation
        (migration 0438) ni pour un lot terminal, que `sync-github` rappelle
        pourtant à chaque passage."""
        params: list = []
        sql = ("SELECT 1 FROM work_items w WHERE w.id = %s"
               + self._missing_ref_gates(
                   params, ref_prefix=ref_prefix, attempt_prefix=attempt_prefix,
                   retry_s=retry_s, terminal_prefix=terminal_prefix,
                   max_attempts=max_attempts, since=since)
               + " LIMIT 1")
        return bool(self.db.query(sql, (int(item_id),) + tuple(params)))

    def review_catch_up_bound(self):
        """L539 (#1104) : la borne d'activation (ligne unique de la migration
        0438), ou None si elle manque — le rattrapage reste alors fermé. Rend
        une chaîne ISO-8601, identique pour les deux pilotes (`psql` rend le
        texte, `psycopg` un `datetime`), utilisable telle quelle en paramètre."""
        rows = self.db.query("SELECT bound_at FROM review_catch_up WHERE id")
        if not rows:
            return None
        value = rows[0]["bound_at"]
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    @_retry_deadlock
    def move(self, item_id, state, *, current, loops, note, actor,
             held=False) -> dict | None:
        """Déplace le lot s'il est encore en `current` (sinon None, rien
        d'écrit), puis journalise la transition. `held` (L233) : et si une
        demande de décision ouverte le tient — vérifiée dans la même
        instruction, la demande verrouillée (voir l'interface). Un passage
        vers un état terminal libère la réservation de migration (#956).
        #1693 (N4) : une correction `merged → build` ne retire PAS le jalon
        `merged` de la table — l'historique reste, et la lecture (progress,
        délais, jalons effectifs) écarte le jalon qu'elle annule. La vraie
        fusion en écrit un second, que le trigger de 0013 pose après elle.
        Un lot pinné sur une révision Process (`workflow_ref`) n'est jamais
        déplacé ici : la garde est aussi dans le SQL, pour la course entre la
        lecture de l'appelant et l'adoption concurrente (#1922/N8)."""
        # L157 : le premier passage en build (ou qa) date le début du travail
        sql = """
            UPDATE work_items
               SET state = %s,
                   loops = loops + %s,
                   updated_at = now(),
                   closed_at = CASE WHEN %s = 'promoted' THEN now() ELSE NULL END,
                   started_at = CASE WHEN %s IN ('build', 'qa')
                                     THEN coalesce(started_at, now()) ELSE started_at END
                   __WAIVER__
             WHERE id = %s AND %s IN (
                 state, CASE WHEN state = 'delivered'
                             THEN coalesce(to_jsonb(work_items)->>'legacy_state', state)
                             ELSE state END)
               AND __UNPINNED__ __HELD__
            RETURNING __COLUMNS__
            """.replace("__COLUMNS__", item_columns(self.db)).replace(
                "__UNPINNED__", _UNPINNED).replace("__HELD__", _HELD if held else "")
        # #1979 : la dispense de re-verdict vaut pour la tête de sa revue ; un
        # lot qui quitte qa (reprise, correction) la perd — sa prochaine revue
        # est due.
        waiver = has_reviewer(self.db)
        sql = sql.replace("__WAIVER__", ", reviewer = CASE WHEN reviewer = %s AND %s NOT IN ('qa', 'validated')"
                                        " THEN '' ELSE reviewer END" if waiver else "")
        params = ((state, loops, state, state)
                  + ((REVIEWER_NONE, state) if waiver else ())
                  + (int(item_id), current)
                  + ((str(int(item_id)),) if held else ()))
        validation_item = self.get(item_id) if state == "validated" else None
        validation_marks = self.milestones(item_id, None) if state == "validated" else None
        validation_gate = (self._validation_gate(item_id, validation_marks, item=validation_item)
                           if state == "validated" else None)
        with self.db.transaction() as tx:
            if not _lock_migration_writer(tx, item_id):
                return None
            if state == "validated":
                # The same lock serializes freezes, verdicts and manual entry.
                tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                         (int(item_id),))
                gate = validation_gate
                # #5016 : les jalons (gel/verdict) et les champs lus par la
                # porte se comparent ; une note concurrente ne change que
                # `updated_at` et n'annule plus le quorum.
                if (WorkItems(tx).milestones(item_id, None) != validation_marks
                        or _gate_item(WorkItems(tx).get(item_id)) != _gate_item(validation_item)):
                    gate = None
                if not gate or not gate.get("ok"):
                    from ...work import WorkError
                    raise WorkError("validated requires the review quorum on the frozen head")
            rows = tx.query(sql, params)
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), state, note, actor)
            if lifecycle.canonical(state) in lifecycle.COMPLETED:
                self._release_migration(tx, item_id, rows[0],
                                        why="état %s" % state, actor=actor)
            return rows[0]

    def designate_reviewer(self, item_id, reviewer, *, actor, remove=False,
                           event=None) -> dict | None:
        """Ajoute ou retire un relecteur en `qa`, avec sa ligne de journal.

        Les écritures exigent 0421 et 0456 ; les lectures restent possibles
        avant ces migrations. La mise à jour conditionnelle et le trigger
        sérialisent les désignations et les changements de responsable."""
        if not has_reviewer(self.db):
            raise DbError("work_items.reviewer is missing (migration 0421): "
                          "run \"ameesh migrate\", then designate the reviewer")
        if not has_reviewers(self.db):
            raise DbError("work_items.reviewers is missing (migration 0456): "
                          "run \"ameesh migrate\", then designate the reviewers")
        with self.db.transaction() as tx:
            return WorkItems(tx)._designate_reviewer(item_id, reviewer, actor,
                                                     remove=remove, event=event)

    def _journal_cleared_reviewers(self, item_id, state, previous, row, actor):
        """#2084/N1 : le trigger 0456 retire de `reviewers` toute personne qui
        devient assigné ou délégué. Ce retrait automatique a sa propre ligne de
        journal, nommant chaque relecteur retiré — pas seulement le dernier de
        la liste, que l'ancien champ `reviewer` portait."""
        remaining = row.get("reviewers")
        if remaining is None:
            remaining = [row.get("reviewer")] if row.get("reviewer") else []
        remaining = set(remaining)
        for name in dict.fromkeys(previous or []):
            if name and name != REVIEWER_NONE and name not in remaining:
                self._event(int(item_id), state,
                            "reviewer %s cleared (they now hold the lot)" % name,
                            actor)

    def _designate_reviewer(self, item_id, reviewer, actor, *, remove=False,
                            event=None, vacant=False) -> dict | None:
        """`designate_reviewer` dans la transaction ouverte : l'écriture et sa
        ligne de journal ensemble. #705 r2 (B1) : la règle « le relecteur n'est
        pas l'assigné » est relue DANS l'update — l'appelant a lu la ligne
        avant, un `assign` a pu la changer entre-temps.

        #2084/N2 : un retrait ne journalise que s'il retire quelque chose.
        La liste est lue sous le verrou de la ligne, dans la même transaction,
        puis comparée à l'écriture : `--remove` d'un nom non désigné, ou
        `--remove` global sur une liste déjà vide, ne laisse aucun faux
        « removed » au journal.

        #1738 R1 (B1) : `vacant` en fait un compare-and-set — un relecteur
        posé entre-temps (par le pilote, hors verrou) n'est pas écrasé par
        une proposition de relecture."""
        if remove or not reviewer:
            locked = self.db.query(
                "SELECT " + item_columns(self.db) + " FROM work_items"
                f" WHERE id = %s AND state = '{lifecycle.QA}' FOR NO KEY UPDATE",
                (int(item_id),))
            if not locked:
                return None
            previous = list(locked[0]["reviewers"] or [])
            # Ne pas toucher `updated_at`, ni le trigger, quand le retrait ne
            # retire rien : c'est un non-effet complet, pas seulement muet.
            if not previous or (remove and reviewer not in previous):
                row = locked[0]
                row["reviewer_changed"] = False
                return row
        if remove:
            expression, params = "array_remove(reviewers, %s)", [reviewer]
        elif not reviewer:
            expression, params = "'{}'::text[]", []
        elif reviewer == REVIEWER_NONE:
            expression, params = "ARRAY[%s]::text[]", [REVIEWER_NONE]
        else:
            expression = "array_append(array_remove(array_remove(reviewers, %s), %s), %s)"
            params = [REVIEWER_NONE, reviewer, reviewer]
        rows = self.db.query(
            "UPDATE work_items SET reviewers = " + expression + ", updated_at = now()"
            f" WHERE id = %s AND state = '{lifecycle.QA}'"
            "   AND (assignee IS DISTINCT FROM %s OR %s)"
            + (" AND coalesce(reviewer, '') = ''" if vacant and not remove else "")
            + " RETURNING " + item_columns(self.db),
            tuple(params + [int(item_id), reviewer, bool(remove)]))
        if not rows:
            return None
        row = rows[0]
        self._event(int(item_id), "qa", event or ("reviewer %s %s" % (
            reviewer, "removed" if remove else "designated")), actor)
        row["reviewer_changed"] = True
        return row

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
            return WorkItems(tx)._assign_locked(
                item_id, assignee, current=current, note=note, actor=actor,
                queued_note=queued_note, forced_note=forced_note, take=take,
                ready_only=ready_only)

    def _assign_locked(self, item_id, assignee, *, current, note, actor,
                       queued_note=None, forced_note=None, take=False,
                       ready_only=False) -> dict | None:
        """Corps de `assign`, dans la transaction ouverte (`self.db`) : partagé
        avec `assign_capped`, qui prend en plus le verrou consultatif de
        l'agent et compte ses lots en `build` avant d'appeler ce corps. La
        prise automatique vérifie donc le lot non démarré (#540), les
        dépendances (L165) et le plafond §4 sous le même verrou.

        `ready_only` accepte un lot `intake` ou un lot `build` RENDU à la file
        (§4, `requeued_sql`) : ce dernier garde son état à l'attribution."""
        if ready_only:
            # Même verrou que les éditions du graphe : une dépendance ne
            # peut apparaître entre le contrôle et l'attribution. Verrouiller
            # aussi les amonts empêche leur réouverture pendant la prise.
            self.db.execute(_DEPENDENCIES_LOCK)
            rows = self.db.query(
                "SELECT id, state, assignee,"
                "       to_jsonb(work_items)->>'external_executor' AS external_executor,"
                f"       (state = '{lifecycle.IN_PROGRESS}' AND " + requeued_sql("work_items")
                + ") AS returned_to_queue"
                "  FROM work_items"
                " WHERE id = %s OR id IN (SELECT blocker_id FROM work_item_dependencies"
                "                         WHERE work_item_id = %s)"
                " ORDER BY id FOR UPDATE",
                REQUEUED_PARAMS + (int(item_id), int(item_id)))
            item = next((r for r in rows if int(r["id"]) == int(item_id)), None)
            # #3447 : un lot réservé à un exécutant externe n'est pas prenable
            # — le filtre de `unassigned_lots` est revérifié sous le verrou.
            if current is not None or item is None or item["assignee"] is not None \
                    or (item.get("external_executor") or "").strip() \
                    or (item["state"] != lifecycle.REQUESTED and not item["returned_to_queue"]):
                return None
            # #2252 : toutes les dépendances doivent être FUSIONNÉES
            # (merged/promoted/closed) — un lot en `qa`, même à verdict
            # favorable, garde une tête qui peut encore changer (cas #667 :
            # un `blocked` est arrivé 10 s après la prise). Le contrôle est
            # refait par une requête POSTÉRIEURE au verrou (N4) : en READ
            # COMMITTED, un SELECT … FOR UPDATE lit l'instantané pris avant
            # l'attente du verrou, donc une fusion ou une arête ajoutée
            # pendant cette attente y serait invisible. Même prédicat que
            # `unassigned_lots` (une seule règle).
            if self.db.query("SELECT 1 FROM work_item_dependencies d"
                             " JOIN work_items b ON b.id = d.blocker_id"
                             " WHERE d.work_item_id = %s AND NOT " + _not_blocking("b") +
                             " LIMIT 1", (int(item_id),)):
                return None
            ready_state = item["state"]
            if ready_state == lifecycle.REQUESTED and (
                    self._footprint_overlaps(item_id) or self._footprint_cap(item_id)):
                return None
        else:
            ready_state = None
            if assignee is not None and current is None:
                # #4173 : l'attribution d'un lot libre (`work assign`) prend
                # le même verrou que la prise, AVANT toute ligne : deux
                # attributions concurrentes de lots chevauchants décident
                # l'une après l'autre.
                self.db.execute(_DEPENDENCIES_LOCK)
                self._footprint_guard(item_id, free_only=True)
        behind = None
        if (queued_note is not None or take) and assignee:
            lot = self.db.query("SELECT started_at IS NULL AS fresh FROM work_items"
                                " WHERE id = %s FOR UPDATE", (int(item_id),))
            if lot and lot[0]["fresh"]:
                behind = self._pending(assignee, int(item_id))
        if behind is not None and take:
            raise interface.PendingLot(behind)
        if behind is not None:
            assignee, note = ((assignee, forced_note.format(lot=behind)) if forced_note
                              else (None, queued_note.format(lot=behind)))
        row = self._reassign(item_id, assignee, current, ready_state, note, actor)
        if row is not None and behind is not None:
            row["forced_behind" if forced_note else "queued_behind"] = behind
        return row

    def _footprint_overlaps(self, item_id, *, free_only=False) -> list[dict]:
        """#4173, sous le verrou du graphe : les chevauchements de l'empreinte
        du lot avec celle d'un lot occupant de son dépôt (requête POSTÉRIEURE
        au verrou, comme le contrôle des dépendances). `free_only` : vide si
        le lot occupe déjà son empreinte (réattribution d'un lot démarré)."""
        guard = (" AND NOT " + footprint_occupying_sql("c")) if free_only else ""
        return self.db.query(
            "SELECT DISTINCT x.* FROM work_items c, LATERAL ("
            + footprint_overlap_rows_sql("c") + ") x WHERE c.id = %s" + guard +
            " ORDER BY x.id, x.path", (int(item_id),))

    def _footprint_cap(self, item_id, *, free_only=False) -> dict | None:
        """#4818, sous le verrou du graphe : `{cap, in_flight}` si le dépôt du
        lot a atteint son plafond de lots en vol, sinon None ; `free_only` :
        None si le lot occupe déjà son empreinte (il est déjà compté)."""
        guard = (" AND NOT " + footprint_occupying_sql("c")) if free_only else ""
        rows = self.db.query(
            "SELECT x.* FROM work_items c, LATERAL (" + footprint_cap_rows_sql("c")
            + ") x WHERE c.id = %s" + guard, (int(item_id),))
        return dict(rows[0]) if rows else None

    def _footprint_guard(self, item_id, *, free_only=False) -> None:
        """La garde d'attribution d'un lot libre : `FootprintOverlap` s'il
        chevauche un lot occupant, `RepositoryCapReached` si son dépôt est au
        plafond (#4818). Rien n'est écrit ; l'appelant tient le verrou."""
        overlaps = self._footprint_overlaps(item_id, free_only=free_only)
        if overlaps:
            raise interface.FootprintOverlap(overlaps)
        cap = self._footprint_cap(item_id, free_only=free_only)
        if cap:
            raise interface.RepositoryCapReached(cap["cap"], cap["in_flight"])

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
            "   AND NOT %s"
            "   AND id <> %%s ORDER BY id LIMIT 1" % (
                _CLOSED, _WAITING_ON_OTHERS, _start_defers("work_items", "%s")),
            (assignee, today, int(item_id or 0)))
        return int(rows[0]["id"]) if rows else None

    def _reassign(self, item_id, assignee, current, state, note, actor) -> dict | None:
        """`assign` dans la transaction ouverte ; `state` : celui exigé.

        #705 r2 (B1) : la règle « le relecteur n'est pas l'assigné » vaut
        aussi quand l'assignation arrive après la désignation — la désignation
        tombe alors dans la MÊME écriture (l'assignation et la désignation
        peuvent se croiser), et le journal le dit."""
        previous_reviewers: list[str] = []
        reviewer_set = ""
        params: list = [assignee]
        if has_reviewer(self.db):
            # #813 : NO KEY UPDATE suffit à sérialiser deux réassignations, et
            # n'attend pas les contrôles de clé étrangère (FOR KEY SHARE) des
            # insertions au journal et aux jalons du lot, que FOR UPDATE
            # bloquait jusqu'au commit.
            with_reviewers = has_reviewers(self.db)
            seen = self.db.query(
                "SELECT reviewer%s FROM work_items WHERE id = %%s FOR NO KEY UPDATE"
                % (", reviewers" if with_reviewers else ""), (int(item_id),))
            if not seen:
                return None
            previous_reviewer = seen[0]["reviewer"] or ""
            previous_reviewers = (list(seen[0].get("reviewers") or [])
                                  if with_reviewers else
                                  ([previous_reviewer] if previous_reviewer else []))
            reviewer_set = (", reviewer = CASE WHEN reviewer = %s THEN ''"
                            " ELSE reviewer END")
            params.append(assignee)
        params += [int(item_id), current, state, state]
        # #2204/N1, #2346 : le journal Python ci-dessous nomme le retrait avec
        # l'acteur ; le trigger d'audit ne doit pas doubler la ligne. Le
        # drapeau ne couvre que cet UPDATE (voir python_reviewer_audit).
        with python_reviewer_audit(self.db):
            rows = self.db.query(
                ("UPDATE work_items SET assignee = %s" + reviewer_set + ","
                 "       updated_at = now(),"
                 "       delegated_by = NULL, delegated_at = NULL, due_at = NULL"
                 " WHERE id = %s AND assignee IS NOT DISTINCT FROM %s"
                 f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                 "   AND (%s::text IS NULL OR state = %s)"
                 "   AND to_jsonb(work_items)->>'external_executor' IS NULL"
                 " RETURNING __COLUMNS__").replace("__COLUMNS__", item_columns(self.db)),
                tuple(params))
        if not rows:
            return None
        self.db.query(
            "UPDATE work_item_delegations SET outcome = 'annulee', resolved_at = now(),"
            "       resolved_by = %s"
            " WHERE work_item_id = %s AND outcome IS NULL RETURNING id",
            (actor or "", int(item_id)))
        self._event(int(item_id), rows[0]["state"], note, actor)
        self._journal_cleared_reviewers(item_id, rows[0]["state"],
                                        previous_reviewers, rows[0], actor)
        return rows[0]

    def count_builds(self, assignee) -> int:
        """§4 canon (owner 14/10/2026) : les lots en `build` de l'agent, tous
        projets confondus."""
        rows = self.db.query(
            "SELECT count(*)::int AS n FROM work_items"
            f" WHERE assignee = %s AND state = '{lifecycle.IN_PROGRESS}'", (str(assignee),))
        return int(rows[0]["n"]) if rows else 0

    def assign_capped(self, item_id, assignee, *, current, note, actor,
                      max_build, queued_note=None, forced_note=None, take=False,
                      ready_only=False) -> dict | None:
        """`assign` sous la limite d'en-cours (§4), en UNE transaction : le
        verrou consultatif de l'agent est pris AVANT le compte, puis ses lots
        en `build` sont comptés et l'assignation n'est écrite que sous le
        plafond. Deux prises simultanées du même agent ne peuvent donc pas
        franchir `max_build` ; None si le plafond est atteint ou si le lot a
        bougé (assigné `current` ou fermé).

        Reprend les options de `assign` (#540 `take`/`queued_note`/
        `forced_note`, L165 `ready_only`) : le lot non démarré, les dépendances
        et le plafond §4 sont vérifiés sous le même verrou."""
        with self.db.transaction() as tx:
            tx.query(_WIP_LOCK, (_WIP_LOCK_PREFIX + str(assignee),))
            rows = tx.query(
                "SELECT count(*)::int AS n FROM work_items"
                f" WHERE assignee = %s AND state = '{lifecycle.IN_PROGRESS}'", (str(assignee),))
            if rows and int(rows[0]["n"]) >= int(max_build):
                return None
            return WorkItems(tx)._assign_locked(
                item_id, assignee, current=current, note=note, actor=actor,
                queued_note=queued_note, forced_note=forced_note, take=take,
                ready_only=ready_only)

    def unassign(self, item_id, *, current, note, actor, states=None) -> dict | None:
        """§4 : rend le lot à la file — l'assignation est retirée s'il a encore
        l'assigné `current` et n'est ni fusionné ni fermé, et le journal part
        dans la même transaction ; None sinon. `states` borne en plus les états
        rendus, relu dans la transaction qui écrit. Branche, tête de branche et
        jalons ne sont pas touchés ; L40 : la délégation en cours est annulée
        (le délégué n'a plus le lot)."""
        with self.db.transaction() as tx:
            return WorkItems(tx)._unassign_locked(item_id, current, note, actor,
                                                  states=states)

    def _unassign_locked(self, item_id, current, note, actor, *, states=None) -> dict | None:
        """`unassign` dans la transaction ouverte (`self.db` EST la
        transaction) : la condition d'assigné et d'état, le journal et
        l'annulation de la délégation partent dans la même transaction."""
        cond, params = "", [int(item_id), current]
        if states:
            cond = " AND state IN (" + ", ".join(["%s"] * len(states)) + ")"
            params.extend(list(states))
        rows = self.db.query(
            "UPDATE work_items SET assignee = NULL, updated_at = now(),"
            "       delegated_by = NULL, delegated_at = NULL, due_at = NULL"
            " WHERE id = %s AND assignee IS NOT DISTINCT FROM %s"
            "   AND state NOT IN " + _CLOSED + cond +
            " RETURNING " + item_columns(self.db), tuple(params))
        if not rows:
            return None
        self.db.query(
            "UPDATE work_item_delegations SET outcome = 'annulee', resolved_at = now(),"
            "       resolved_by = %s"
            " WHERE work_item_id = %s AND outcome IS NULL RETURNING id",
            (actor or "", int(item_id)))
        self._event(int(item_id), rows[0]["state"], note, actor)
        return rows[0]

    def unassign_surplus(self, item_id, *, current, marker, prefixes, max_build,
                         note, actor) -> dict | None:
        """§4 : restitution du SURPLUS d'un agent, sous les gardes
        transactionnelles. Le verrou consultatif de l'agent est pris AVANT
        toute relecture (les prises `assign_capped` et les autres balayages du
        même agent attendent), puis TOUTES les lignes `build` de l'agent sont
        verrouillées (`FOR NO KEY UPDATE`, id croissant : ordre stable) AVANT
        de lire la moindre exception. Le candidat doit faire partie de ces
        lignes : encore en `build`, encore assigné à `current`. Les exceptions
        pilote (`marker` dans la note de la dernière attribution, `prefixes`)
        sont alors lues sur un ensemble immobile : toute attribution — assign,
        délégation, prise, reprise, pose d'exception — passe par un UPDATE de
        la ligne du lot et attend donc la fin du compte. Il faut plus de
        `max_build` lots sans exception pour rendre celui-ci. None sinon (rien
        n'est écrit) : deux balayages concurrents ne rendent jamais plus que le
        surplus, et la réattribution d'un AUTRE lot compté ne peut pas effacer
        son exception sous la lecture (elle rendrait un lot qui n'est plus du
        surplus)."""
        agent = str(current)
        with self.db.transaction() as tx:
            work = WorkItems(tx)
            tx.query(_WIP_LOCK, (_WIP_LOCK_PREFIX + agent,))
            builds = tx.query(
                f"SELECT id FROM work_items WHERE assignee = %s AND state = '{lifecycle.IN_PROGRESS}'"
                " ORDER BY id FOR NO KEY UPDATE", (agent,))
            ids = [int(r["id"]) for r in builds]
            if int(item_id) not in ids:
                return None
            if work._wip_exception_locked(int(item_id), marker, prefixes):
                return None
            free = [i for i in ids
                    if not work._wip_exception_locked(i, marker, prefixes)]
            if len(free) <= int(max_build):
                return None
            return work._unassign_locked(item_id, agent, note, actor)

    def set_external_executor(self, item_id, executor, *, note, actor) -> dict | None:
        """#3447 : pose (ou efface, `executor=None`) l'exécutant externe d'un lot
        ouvert, avec sa ligne de journal, en une transaction.

        Un lot assigné ne reçoit pas d'exécutant externe (deux porteurs) ; une
        levée reste possible pour nettoyer un lot déjà assigné. None si le lot
        est fermé ou si la pose perd la course contre une attribution : sous le
        verrou de la ligne, la condition est relue, pas devinée."""
        with self.db.transaction() as tx:
            rows = tx.query(
                ("UPDATE work_items SET external_executor = %s, updated_at = now()"
                 f" WHERE id = %s AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                 "   AND (%s::text IS NULL OR assignee IS NULL)"
                 " RETURNING __COLUMNS__").replace("__COLUMNS__", item_columns(self.db)),
                (executor or None, int(item_id), executor or None))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    def _wip_exception_locked(self, item_id, marker, prefixes) -> bool:
        """§4 : la dernière note d'attribution du lot porte-t-elle `marker` ?
        À lire sous le verrou de la ligne du lot : toute attribution met cette
        ligne à jour avant d'écrire sa note, donc aucune ne peut se glisser
        entre cette lecture et l'écriture qui suit."""
        prefixes = list(prefixes or ())
        if not prefixes:
            return False
        starts = " OR ".join(["starts_with(note, %s)"] * len(prefixes))
        rows = self.db.query(
            "SELECT e.note FROM work_item_events e"
            " JOIN (SELECT max(id) AS id FROM work_item_events"
            "        WHERE work_item_id = %s"
            "          AND (" + starts + ")) last ON last.id = e.id"
            " WHERE position(%s in e.note) > 0",
            (int(item_id),) + tuple(prefixes) + (str(marker),))
        return bool(rows)

    def wip_exceptions(self, item_ids, marker, prefixes) -> list[int]:
        """§4 : parmi `item_ids`, ceux dont la DERNIÈRE attribution porte
        `marker` dans sa note — l'exception pilote explicite à la limite
        d'en-cours. Une attribution est une note commençant par l'un des
        `prefixes` ; une réassignation postérieure sans le marqueur retire
        l'exception."""
        ids = [int(i) for i in item_ids]
        prefixes = list(prefixes or ())
        if not ids or not prefixes:
            return []
        marks = ", ".join(["%s"] * len(ids))
        starts = " OR ".join(["starts_with(note, %s)"] * len(prefixes))
        rows = self.db.query(
            "SELECT e.work_item_id AS id FROM work_item_events e"
            " JOIN (SELECT work_item_id, max(id) AS id FROM work_item_events"
            "        WHERE work_item_id IN (" + marks + ")"
            "          AND (" + starts + ")"
            "        GROUP BY work_item_id) last ON last.id = e.id"
            " WHERE position(%s in e.note) > 0",
            tuple(ids) + tuple(prefixes) + (str(marker),))
        return [int(r["id"]) for r in rows]

    def handoff(self, item_id, assignee, *, current, state, paused_prefix, note_prefix,
                window_s, max_per_window, note, actor, still_paused=None) -> dict | None:
        """L233b : toutes les conditions relues sous le verrou, dans la
        transaction qui écrit (codex r1, #409) : deux `notify` sur deux hôtes
        ne dépassent ni le plafond ni ne donnent deux lots au même preneur ;
        un agent sorti de pause entre le contrôle git et l'écriture garde son
        lot (sa ligne reste verrouillée jusqu'au commit). `assignee` None rend
        le lot à la file d'équipe (assigné NULL, L165) : le contrôle du
        preneur n'a pas lieu."""
        with self.db.transaction() as tx:
            tx.query(_HANDOFF_LOCK)
            taken = tx.query(
                "SELECT count(*)::bigint AS n FROM work_item_events"
                " WHERE created_at > now() - make_interval(secs => %s)"
                "   AND starts_with(note, %s)", (float(window_s), note_prefix))
            if int(taken[0]["n"]) >= int(max_per_window):
                return None
            if assignee is not None and tx.query(
                    "SELECT 1 FROM work_items WHERE assignee = %%s"
                    "   AND state NOT IN %s LIMIT 1" % _CLOSED, (assignee,)):
                return None
            paused = tx.query("SELECT status_text FROM agent_registry WHERE name = %s"
                              "   AND status = 'blocked' AND starts_with(status_text, %s)"
                              " FOR SHARE", (current, paused_prefix))
            if not paused:
                return None
            if still_paused is not None and not still_paused(paused[0]["status_text"]):
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
            with_reviewers = has_reviewers(self.db)
            seen = tx.query(
                "SELECT id%s FROM work_items WHERE id = %%s"
                "   AND assignee IS NOT DISTINCT FROM %%s AND state NOT IN %s"
                # #3447 : un lot réservé à un exécutant externe n'est pas
                # délégable — la course réservation/délégation se tranche ici,
                # sous le verrou de la ligne.
                "   AND to_jsonb(work_items)->>'external_executor' IS NULL"
                " FOR NO KEY UPDATE"
                % (", reviewer, reviewers" if with_reviewers
                   else (", reviewer" if with_reviewer else ""), _CLOSED),
                (int(item_id), current))
            if not seen:
                return None
            # #705 r2 (B1) : déléguer à son relecteur retire la désignation
            # (le relecteur relirait son propre travail), dans l'écriture même.
            previous_reviewer = (seen[0]["reviewer"] or "") if with_reviewer else ""
            previous_reviewers = (list(seen[0].get("reviewers") or [])
                                  if with_reviewers else
                                  ([previous_reviewer] if previous_reviewer else []))
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
            # #2204/N1, #2346 : le journal Python ci-dessous nomme le retrait
            # avec l'acteur ; le drapeau d'audit ne couvre que cet UPDATE.
            with python_reviewer_audit(tx):
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
            WorkItems(tx)._event(int(item_id), item["state"], note, actor)
            WorkItems(tx)._journal_cleared_reviewers(
                item_id, item["state"], previous_reviewers, item, actor)
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
                    " SELECT id, CASE WHEN state = 'delivered' THEN coalesce(to_jsonb(work_items)->>'legacy_state', state) ELSE state END, %%s, %%s FROM work_items WHERE id IN (%s)"
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
            with_reviewers = has_reviewers(self.db)
            rows = tx.query(
                "SELECT %s, %s AS worked, w.title, w.state, w.assignee%s"
                "  FROM work_item_delegations d JOIN work_items w ON w.id = d.work_item_id"
                " WHERE d.id = %%s AND d.outcome IS NULL AND d.due_at < to_timestamp(%%s)"
                % (_DELEGATION_COLUMNS, _WORKED,
                   ", w.reviewer, w.reviewers" if with_reviewers
                   else (", w.reviewer" if with_reviewer else "")),
                (int(delegation_id), float(now_ts)))
            if not rows:
                return None
            row = rows[0]
            item_id = int(row["work_item_id"])
            closed = row["state"] in lifecycle.COMPLETED
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
                # #2204/N1, #2346 : le journal Python ci-dessous nomme le
                # retrait ; le drapeau ne couvre que cet UPDATE.
                with python_reviewer_audit(tx):
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
            if text:
                WorkItems(tx)._event(item_id, item[0]["state"], text, actor)
            if outcome == "rendue":
                previous_reviewers = (list(row.get("reviewers") or [])
                                      if with_reviewers else
                                      ([row["reviewer"]] if row.get("reviewer") else []))
                WorkItems(tx)._journal_cleared_reviewers(
                    item_id, item[0]["state"], previous_reviewers, item[0], actor)
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

    def note(self, item_id, state, text, actor, *, once=False) -> None:
        if once:
            with self.db.transaction() as tx:
                rows = tx.query("SELECT state FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                                (int(item_id),))
                if not rows:
                    raise DbError("lot %s not found" % item_id)
                if tx.query("SELECT id FROM work_item_events"
                            " WHERE work_item_id = %s AND note = %s LIMIT 1",
                            (int(item_id), text)):
                    return
                WorkItems(tx).note(item_id, rows[0]["state"], text, actor)
            return
        self._event(int(item_id), state, text, actor)
        self.db.execute("UPDATE work_items SET updated_at = now() WHERE id = %s",
                        (int(item_id),))

    def note_with_wake(self, item_id, state, text, actor, wake_ts, *, once=False) -> None:
        """#2466 : la note et l'échéance de réveil, sous le même verrou."""
        if not has_wake_at(self.db):
            raise DbError("work_items.wake_at is missing: apply migration 0466 "
                          "(`ameesh migrate`) before scheduling a wake")
        with self.db.transaction() as tx:
            rows = tx.query("SELECT state FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                            (int(item_id),))
            if not rows:
                raise DbError("lot %s not found" % item_id)
            if once and tx.query("SELECT id FROM work_item_events"
                                 " WHERE work_item_id = %s AND note = %s LIMIT 1",
                                 (int(item_id), text)):
                return
            WorkItems(tx)._event(int(item_id), rows[0]["state"], text, actor)
            # l'échéance est écrite avec la note : jamais une retenue dont
            # l'échéance s'est perdue.
            tx.execute(
                "UPDATE work_items SET wake_at = to_timestamp(%s),"
                "       wake_claim_at = NULL, updated_at = now() WHERE id = %s",
                (float(wake_ts), int(item_id)))

    def update_generated(self, item_id, *, title, body, priority, note,
                         actor) -> dict | None:
        """#4291 : titre et corps générés, priorité seulement montée."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT %s FROM work_items WHERE id = %%s AND state NOT IN %s"
                " FOR NO KEY UPDATE" % (item_columns(self.db, "work_items"), _CLOSED),
                (int(item_id),))
            if not rows:
                return None
            row = rows[0]
            current = row.get("priority")
            wanted = current
            if priority is not None and (current is None or int(priority) < int(current)):
                wanted = int(priority)
            if (row.get("title") == title and row.get("body") == body
                    and wanted == current):
                return row
            updated = tx.query(
                "UPDATE work_items SET title = %%s, body = %%s, priority = %%s,"
                " updated_at = now() WHERE id = %%s RETURNING %s"
                % item_columns(self.db, "work_items"),
                (title, body, wanted, int(item_id)))
            if not updated:
                return None
            if note:
                tx.execute(
                    "INSERT INTO work_item_events (work_item_id, state, note, actor)"
                    " VALUES (%s, %s, %s, %s)",
                    (int(item_id), updated[0]["state"], note, actor or ""))
            return updated[0]

    def schedule_wake(self, item_id, wake_ts, *, text, actor) -> dict | None:
        """#2466 : pose (ou efface) le réveil programmé et journalise, atomique."""
        if not has_wake_at(self.db):
            raise DbError("work_items.wake_at is missing: apply migration 0466 "
                          "(`ameesh migrate`) before scheduling a wake")
        item_id = int(item_id)
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT state, extract(epoch from wake_at)::float8 AS wake_ts"
                "  FROM work_items WHERE id = %s AND state NOT IN"
                f" ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}') FOR NO KEY UPDATE",
                (item_id,))
            if not rows:
                return None
            tx.execute(
                "INSERT INTO work_item_events (work_item_id, state, note, actor) "
                "VALUES (%s, %s, %s, %s)",
                (item_id, rows[0]["state"], text or "", actor or ""))
            if wake_ts is None:
                tx.execute("UPDATE work_items SET wake_at = NULL,"
                           "       wake_claim_at = NULL, updated_at = now()"
                           " WHERE id = %s", (item_id,))
            else:
                tx.execute("UPDATE work_items SET wake_at = to_timestamp(%s),"
                           "       wake_claim_at = NULL, updated_at = now()"
                           " WHERE id = %s", (float(wake_ts), item_id))
        return self.get(item_id)

    def log_review_attempt(self, item_id, state, text, actor) -> None:
        """L539 (#1104) : journalise une tentative de rattrapage sans création.

        N'écrit que le journal (`work_item_events`) : `updated_at` n'est pas
        touché — une tentative de fond ne doit ni réordonner la sélection ni
        faire passer le lot pour actif."""
        self._event(int(item_id), str(state or ""), str(text or ""), actor)

    def lock_deep_analysis(self, item_id) -> None:
        self.db.query("SELECT pg_advisory_xact_lock(hashtext(%s || current_schema())::bigint)",
                      ("ameesh_deep_analysis:%d:" % int(item_id),))

    def waiting_episodes(self, item_ids) -> dict:
        ids = [int(i) for i in item_ids]
        if not ids:
            return {}
        rows = self.db.query(
            f"""
            SELECT w.id, coalesce(b.id, 0) AS episode,
                   extract(epoch from coalesce(b.created_at, w.created_at))::float8
                       AS start_ts,
                   extract(epoch from (
                       SELECT min(e.created_at) FROM work_item_events e
                        WHERE e.work_item_id = w.id AND e.state = '{lifecycle.WAITING_HUMAN}'
                          AND e.id > coalesce(b.id, 0)))::float8 AS first_ts
              FROM work_items w
              LEFT JOIN LATERAL (
                   SELECT e.id, e.created_at FROM work_item_events e
                    WHERE e.work_item_id = w.id AND e.state <> '{lifecycle.WAITING_HUMAN}'
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
            (int(item_id), None if limit is None else int(limit)),
        )

    def events_noted(self, prefix, since_ts, *, ids=None) -> list[dict]:
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params = [pattern, float(since_ts)]
        # #2162 : borne la lecture aux éléments candidats (index
        # work_item_events_item_idx) — sans `ids`, le journal entier reste lu.
        # Aucune fenêtre temporelle : une note ancienne d'un candidat compte.
        borne = ""
        if ids is not None:
            ids = [int(item_id) for item_id in ids]
            if not ids:
                return []
            borne = (" AND e.work_item_id IN"
                     " (SELECT jsonb_array_elements_text(%s::jsonb)::bigint)")
            params.append(json.dumps(ids))
        return self.db.query(
            """
            SELECT e.id, e.work_item_id, e.state, e.note, e.actor, w.assignee,
                   e.evidence,
                   extract(epoch from e.created_at)::float8 as created_ts
              FROM work_item_events e JOIN work_items w ON w.id = e.work_item_id
             WHERE e.note LIKE %s AND e.created_at >= to_timestamp(%s)
            """ + borne + """
             ORDER BY e.id
            """,
            tuple(params),
        )

    def corrections(self, ids=None) -> dict[int, float]:
        """#2317 (N2/N4) : instant de la dernière correction `merged → build`
        par lot, `{}` sans correction.

        Une correction se reconnaît à la TRANSITION du journal — l'événement
        `build` dont l'état précédent du lot est `merged` —, jamais au texte
        de sa note (une note libre du même préfixe n'annule rien). `ids`
        borne la lecture aux lots candidats (index `work_item_events_item_idx`)
        au lieu de relire tout le journal à chaque relevé."""
        params: list = []
        borne = ""
        if ids is not None:
            ids = sorted({int(item_id) for item_id in ids})
            if not ids:
                return {}
            borne = " AND c.work_item_id IN (%s)" % ", ".join(["%s"] * len(ids))
            params.extend(ids)
        rows = self.db.query(
            f"""
            SELECT c.work_item_id,
                   max(extract(epoch from c.created_at))::float8 as created_ts
              FROM work_item_events c
              JOIN LATERAL (SELECT p.state FROM work_item_events p
                             WHERE p.work_item_id = c.work_item_id AND p.id < c.id
                             ORDER BY p.id DESC LIMIT 1) prev ON true
             WHERE c.state = '{lifecycle.IN_PROGRESS}' AND prev.state IN ('merged', 'delivered')
            """ + borne + """
             GROUP BY c.work_item_id
            """,
            tuple(params),
        )
        return {int(row["work_item_id"]): float(row["created_ts"]) for row in rows}

    def milestones(self, item_id, limit) -> list[dict]:
        return self.db.query(
            """
            SELECT id, work_item_id, kind, sha, actor, verdict, note,
                   extract(epoch from at)::float8 as at_ts, at,
                   (kind = 'merged'
                    AND agent_mesh_merged_annulled(work_item_id, at)) AS annulled
              FROM work_item_milestones
             WHERE work_item_id = %s
             ORDER BY at DESC, id DESC LIMIT %s
            """,
            (int(item_id), None if limit is None else int(limit)),
        )

    def verdicts_for_commit(self, commit) -> list[dict]:
        return self.db.query(
            """
            SELECT id, work_item_id, kind, sha, actor, verdict, note,
                   extract(epoch from at)::float8 AS at_ts, at
              FROM work_item_milestones
             WHERE kind = 'verdict'
               AND sha ~ '^[0-9a-fA-F]{12,40}$'
               AND starts_with(lower(%s), lower(sha))
             ORDER BY at DESC, id DESC
            """, (commit,))

    def earlier_freezes(self, app, lot, before_ts) -> list[dict]:
        return self.db.query(
            """
            SELECT m.id, m.work_item_id, m.sha, m.actor, m.note,
                   extract(epoch from m.at)::float8 AS at_ts
              FROM work_item_milestones m
              JOIN work_items w ON w.id = m.work_item_id
             WHERE m.kind = 'frozen' AND w.app = %s
               AND m.work_item_id <> %s AND m.at <= to_timestamp(%s)
             ORDER BY m.at DESC, m.id DESC LIMIT 256
            """, (str(app), int(lot), float(before_ts)))

    def add_milestone(self, item_id, kind, *, sha, actor, verdict, note,
                      on_frozen=None, after_commit=None, proof=None) -> dict:
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
        Un `on_frozen` sur un autre kind est refusé.

        `proof` (facultatif, #1465) : quand il porte un sha complet, sa ligne de
        provenance est écrite dans la MÊME transaction — le fait et sa preuve
        naissent ensemble, ou pas du tout. Indépendant du callback de gel.

        #3836 : la transaction d'un jalon `frozen` borne ses attentes de verrou
        (`SET LOCAL lock_timeout`, `FREEZE_LOCK_TIMEOUT_MS`). Cette borne ne
        vaut que pour les prises **postérieures** au `SET LOCAL` : les verrous
        déjà pris par l'appelant dans sa transaction, et les attentes qu'il y a
        faites, restent hors de la borne. Un appel imbriqué est couvert pour
        ses propres prises, et un `lock_timeout` plus court déjà posé par
        l'appelant est gardé."""
        if on_frozen is not None and kind != "frozen":
            raise ValueError("on_frozen only makes sense for the `frozen` milestone")
        threads = [] if after_commit is None else after_commit
        with self.db.transaction() as tx:
            # #3490: a freeze can call work.move below. Acquire its repository
            # advisory lock before any lot row lock, as all migration writers
            # do; reversing that order deadlocks with a concurrent move.
            # Other milestones never move the lot and may follow a note that
            # already holds its row (e.g. a fenced fire exception).
            if kind == "frozen":
                # #3836 : ne jamais attendre un verrou des minutes en tenant
                # ceux du mesh. `SET LOCAL` vaut pour cette transaction, sur
                # les deux pilotes ; une attente expirée lève, le `with` annule
                # le jalon et relâche les verrous, l'appelant relance. Un
                # `lock_timeout` déjà plus court posé par l'appelant est gardé.
                # Limite : la borne couvre les prises qui suivent ce SET LOCAL,
                # y compris dans un appel imbriqué ; les verrous déjà tenus par
                # la transaction de l'appelant ne sont pas bornés (pas de borne
                # rétroactive) — l'appelant pose la sienne avant ses prises.
                current = float(tx.query(
                    "SELECT extract(epoch FROM current_setting('lock_timeout')"
                    "::interval) AS seconds")[0]["seconds"] or 0)
                if current == 0 or current * 1000 > FREEZE_LOCK_TIMEOUT_MS:
                    tx.execute("SET LOCAL lock_timeout = '%dms'"
                               % int(FREEZE_LOCK_TIMEOUT_MS))
                _lock_migration_writer(tx, item_id)
            items = tx.query("SELECT id, app, branch_target, state,"
                             " coalesce(to_jsonb(work_items)->>'workflow_ref', '') AS workflow_ref"
                             " FROM work_items"
                             " WHERE id = %s FOR NO KEY UPDATE", (int(item_id),))
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
            if (kind == "verdict" and items and items[0]["state"] == "validated"
                    and not items[0].get("workflow_ref")):
                tx.execute(f"UPDATE work_items SET state = '{lifecycle.QA}', updated_at = now()"
                           " WHERE id = %s", (int(item_id),))
                WorkItems(tx)._event(int(item_id), "qa",
                                    "validated → qa — review quorum changed (#4351)", actor)
            if (kind == "frozen" and items
                    and not items[0].get("workflow_ref")
                    and items[0]["state"] in (
                        lifecycle.REQUESTED,
                        lifecycle.IN_PROGRESS,
                        lifecycle.VALIDATED,
                    )):
                # #3467: freezing enters review before the designation hook.
                # #3489: pinned lots keep their Process state; work.move
                # rejects them. The milestone and designation still apply.
                # Use the ordinary transitions for unpinned lots,
                # under this lot's lock and transaction: a failing hook must
                # roll back the state, journal and milestone together.
                from ... import work
                if items[0]["state"] == lifecycle.REQUESTED:
                    work.move(tx, item_id, lifecycle.IN_PROGRESS, actor=actor,
                              note="intake → build — frozen (#3467)")
                work.move(tx, item_id, lifecycle.QA, actor=actor,
                          note=("validated → qa — frozen (#4351)"
                                if items[0]["state"] == "validated"
                                else "build → qa — frozen (#3467)"))
            if kind == "verdict" and items:
                from ... import merge_queue, train_events
                item = items[0]
                try:
                    train_events.emit(tx, kind="verdict", project=item.get("app") or "",
                        target=merge_queue._remote_branch(item.get("branch_target") or ""))
                except ValueError:
                    # An old/unscoped lot has no train routing subject. The
                    # milestone remains valid; SQL/transport errors still roll
                    # back its transaction, so a committed verdict loses no hint.
                    pass
            if proof and proof.get("sha"):
                tx.execute(
                    "INSERT INTO provenance_receipts"
                    "       (subject, sha, actor, session, turn, harness, work_item_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (subject) DO NOTHING",
                    ("milestone:%d" % int(milestone["id"]), proof["sha"], proof["actor"],
                     proof["session"], proof["turn"], proof["harness"], int(item_id)))
            if kind == "frozen" and has_reviewer(self.db):
                WorkItems(tx)._lapse_waiver(int(item_id), milestone, actor)
            if on_frozen is not None:
                on_frozen(tx, milestone, threads)
        if kind in ("verdict", "frozen"):
            # Git/classification reads run after the writer commits. Nested
            # designation hooks are reconciled by the outer freeze writer.
            from ...freeze_gate import _in_transaction
            if not _in_transaction(self.db):
                self._sync_validation(item_id, actor)
        return milestone

    def _validation_gate(self, item_id, marks=None, *, item=None):
        from ... import merge_queue
        item = self.get(item_id) if item is None else item
        marks = self.milestones(item_id, None) if marks is None else marks
        gate = merge_queue.review_gate_for_item(
            self.db, getattr(self.db, "cfg", None), item, marks)
        return gate

    def _sync_validation(self, item_id, actor):
        """Compute the gate outside locks, then compare the immutable ledger.

        Any concurrent freeze/verdict invalidates this observation. #5016: a
        concurrent note only moves `updated_at`, so the guard compares the
        milestones and the fields the gate actually reads, never the whole
        row — a stale note no longer cancels an acquired quorum for good.
        The next writer reconciles its own snapshot; no stale quorum can
        promote a lot.
        """
        item = self.get(item_id)
        if item["state"] != "qa" or item.get("workflow_ref"):
            return
        marks = self.milestones(item_id, None)
        from ... import plan_git
        freeze = plan_git.latest_freeze(marks)
        # Refreezing alone resets validation, including on the same SHA.
        if not freeze or not any(mark.get("kind") == "verdict"
                                 and mark["id"] > freeze["id"] for mark in marks):
            return
        gate = self._validation_gate(item_id, marks, item=item)
        if not gate or not gate.get("ok"):
            return
        with self.db.transaction() as tx:
            tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                     (int(item_id),))
            if (WorkItems(tx).milestones(item_id, None) != marks
                    or _gate_item(WorkItems(tx).get(item_id)) != _gate_item(item)):
                return
            rows = tx.query("UPDATE work_items SET state = 'validated', updated_at = now()"
                            " WHERE id = %s AND state = 'qa' AND " + _UNPINNED + " RETURNING id",
                            (int(item_id),))
            if rows:
                WorkItems(tx)._event(int(item_id), "validated",
                                    "qa → validated — review quorum (#4423)", actor)

    def _lapse_waiver(self, item_id, milestone, actor) -> None:
        """#1979 : un gel d'une AUTRE tête lève la dispense de re-verdict
        (`REVIEWER_NONE`) — elle valait pour la tête gelée de sa revue, comme
        un verdict (même règle que `progress._verdict_applies`) ; un gel
        renouvelé sur le même commit la garde."""
        sha = (milestone.get("sha") or "").strip()
        previous = self.db.query(
            "SELECT sha FROM work_item_milestones WHERE work_item_id = %s"
            "   AND kind = 'frozen' AND id < %s ORDER BY id DESC LIMIT 1",
            (item_id, milestone["id"]))
        before = (previous[0]["sha"] or "").strip() if previous else ""
        if sha and before and (sha.startswith(before) or before.startswith(sha)):
            return
        rows = self.db.query(
            "UPDATE work_items SET reviewer = '', updated_at = now()"
            " WHERE id = %s AND reviewer = %s RETURNING state",
            (item_id, REVIEWER_NONE))
        if rows:
            self._event(item_id, rows[0]["state"],
                        "verdict required again: new freeze %s (#1979)"
                        % (sha[:12] or "without sha"), actor)

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
    @_retry_deadlock
    def set_branch(self, item_id, branch, target, *, note, actor) -> dict | None:
        """Pose la branche (et la cible) d'un lot ouvert, avec une ligne de
        journal ; le dernier commit vu est oublié si la branche change. None
        si le lot n'est plus ouvert."""
        with self.db.transaction() as tx:
            if not _lock_migration_writer(tx, item_id):
                return None
            rows = tx.query(
                "UPDATE work_items SET"
                "       branch_head = CASE WHEN branch IS DISTINCT FROM %%s"
                "                          THEN NULL ELSE branch_head END,"
                "       branch_head_at = CASE WHEN branch IS DISTINCT FROM %%s"
                "                             THEN NULL ELSE branch_head_at END,"
                "       branch = %%s, branch_target = %%s, updated_at = now()"
                f" WHERE id = %%s AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                " RETURNING %s" % item_columns(self.db),
                (branch, branch, branch, target, int(item_id)))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    def attach_implicit_branch(self, item_id, branch, *, detached_prefix, note,
                               actor) -> dict | None:
        """#2102 : la ligne du lot est verrouillée AVANT de lire sa branche et
        son journal : un détachement (même transaction que son journal)
        validé avant est vu par la lecture qui suit le verrou, un détachement
        plus tardif attend ce verrou puis vide la branche ; une branche posée
        explicitement entre-temps n'est jamais écrasée."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT state FROM work_items WHERE id = %%s AND branch IS NULL"
                "   AND state NOT IN %s FOR UPDATE" % _CLOSED, (int(item_id),))
            if not rows:
                return None
            if tx.query("SELECT 1 FROM work_item_events WHERE work_item_id = %s"
                        "   AND starts_with(note, %s) LIMIT 1",
                        (int(item_id), detached_prefix)):
                return None
            rows = tx.query(
                "UPDATE work_items SET branch = %%s, branch_target = NULL,"
                "       branch_head = NULL, branch_head_at = NULL, updated_at = now()"
                " WHERE id = %%s AND branch IS NULL RETURNING %s" % item_columns(self.db),
                (branch, int(item_id)))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    def detach_branch(self, item_id, expected, *, note, actor) -> dict | None:
        """#2102 : comme `attach_implicit_branch`, la ligne est verrouillée
        avant la comparaison : une branche posée entre la lecture de
        l'appelant et ce verrou n'est jamais effacée."""
        with self.db.transaction() as tx:
            if not tx.query(
                    "SELECT 1 FROM work_items WHERE id = %%s"
                    "   AND branch IS NOT DISTINCT FROM %%s AND state NOT IN %s"
                    " FOR UPDATE" % _CLOSED, (int(item_id), expected)):
                return None
            rows = tx.query(
                "UPDATE work_items SET branch = NULL, branch_target = NULL,"
                "       branch_head = NULL, branch_head_at = NULL, updated_at = now()"
                " WHERE id = %%s RETURNING %s" % item_columns(self.db), (int(item_id),))
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    # -- numéro de migration réservé (L122) ----------------------------------
    def _release_migration(self, tx, item_id, row, *, why, actor) -> None:
        """#956, #2481 : libère les réservations du lot qui entre dans un état
        terminal (`merged`, `promoted`, `closed`), dans la transaction même de
        la transition : une réservation tenue par un lot fusionné bloquait le
        numéro pour toujours. Le numéro reste protégé tant qu'un fichier le
        porte sur la cible (réservation à numéro exact, `target_numbers`) ou
        sur une branche (allocation du prochain libre, `repo_numbers`). Tous
        les créneaux réservés du lot sont rendus, pas seulement le scalaire."""
        numbers = sorted({int(item["number"]) for item in (row.get("migrations") or ())
                          if item.get("number") is not None})
        if row.get("migration") is not None and int(row["migration"]) not in numbers:
            numbers.append(int(row["migration"]))
            numbers.sort()
        if not numbers:
            return
        if has_migration_slots(tx):
            tx.execute(
                "UPDATE work_migration_reservations SET status = 'released',"
                "       updated_at = now()"
                " WHERE work_item_id = %s AND status = 'reserved'", (int(item_id),))
        tx.execute(
            "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
            "       updated_at = now() WHERE id = %s", (int(item_id),))
        # le lot rendu à l'appelant reflète l'état validé, pas la valeur d'avant
        row["migration"] = None
        row["migration_repo"] = None
        row["migrations"] = []
        what = ("migration %s" % numbers[0] if len(numbers) == 1
                else "migrations %s" % ", ".join(str(n) for n in numbers))
        WorkItems(tx)._event(
            int(item_id), row["state"],
            "%s released (%s, #956)" % (what, why), actor)

    def _migration_row(self, tx, item_id):
        rows = tx.query("SELECT %s FROM work_items WHERE id = %%s" % item_columns(tx),
                        (int(item_id),))
        return rows[0] if rows else None

    def _release_migration_rows(self, tx, item_id, *, slots, scalar, number, note, actor,
                                state, repo=None) -> dict | None:
        """Libère le numéro `number` (None : tous) du lot, et rend sa ligne.

        Le scalaire est la réservation principale ; s'il est libéré alors que
        d'autres créneaux legacy restent, le plus petit est promu et son
        créneau libéré AVANT l'UPDATE du scalaire : le déclencheur
        `work_items_migration_slot_sync` réécrit alors le créneau `default`
        sans rencontrer le doublon du créneau promu. Les slots nommés restent
        intacts : leur identité ne devient jamais `default` (#4430)."""
        numbers = sorted({int(row["number"]) for row in slots})
        if scalar is not None and int(scalar) not in numbers:
            numbers.append(int(scalar))
            numbers.sort()
        if not numbers:
            return self._migration_row(tx, item_id)
        target = None if number is None else int(number)
        if target is not None and target not in numbers:
            return None
        table = has_migration_slots(tx)
        if target is None:
            if table:
                tx.execute(
                    "UPDATE work_migration_reservations SET status = 'released',"
                    "       updated_at = now()"
                    " WHERE work_item_id = %s AND status = 'reserved'", (int(item_id),))
            tx.execute(
                "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
                "       updated_at = now() WHERE id = %s", (int(item_id),))
        elif scalar is not None and target == int(scalar):
            remaining = [row for row in slots if int(row["number"]) != target
                         and (row["slot"] == "default" or (
                             row["slot"] == "legacy-%d" % int(row["number"])
                             and row.get("target_sha") is None))]
            promoted = remaining[0] if remaining else None
            if table:
                for row in slots:
                    if int(row["number"]) == target or (
                            promoted is not None and row["slot"] == promoted["slot"]):
                        tx.execute(
                            "UPDATE work_migration_reservations SET status = 'released',"
                            "       updated_at = now()"
                            " WHERE work_item_id = %s AND slot = %s"
                            "   AND status = 'reserved'", (int(item_id), row["slot"]))
            if promoted is None:
                tx.execute(
                    "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
                    "       updated_at = now() WHERE id = %s", (int(item_id),))
            else:
                promoted_repo = repo or str(promoted.get("target_repo") or "")
                if promoted_repo.startswith("github.com/"):
                    promoted_repo = promoted_repo[len("github.com/"):]
                tx.query(
                    "UPDATE work_items SET migration = %%s, migration_repo = %%s,"
                    "       updated_at = now() WHERE id = %%s"
                    " RETURNING %s" % item_columns(tx),
                    (int(promoted["number"]), promoted_repo, int(item_id)))
        elif table:
            for row in slots:
                if int(row["number"]) == target:
                    tx.execute(
                        "UPDATE work_migration_reservations SET status = 'released',"
                        "       updated_at = now()"
                        " WHERE work_item_id = %s AND slot = %s"
                        "   AND status = 'reserved'", (int(item_id), row["slot"]))
        if target is not None:
            note = note % {"number": target}
        elif "%(number)" in note:
            # #2481 N2 : une libération totale n'a pas UN numéro ; le
            # gabarit nomme l'ensemble au lieu du sentinelle 0.
            note = note.replace("%(number)04d", "%s").replace("%(number)s", "%s")
            note = note % ", ".join(str(value) for value in numbers)
        WorkItems(tx)._event(int(item_id), state, note, actor)
        return self._migration_row(tx, item_id)

    @_retry_deadlock
    def reserve_migration(self, item_id, repo, number, floor, *, note, actor,
                          issue_ref, release=False, release_number=None) -> dict | None:
        """Réserve (ou libère, `release`) un numéro de migration d'un lot
        ouvert dans le dépôt `repo`, avec une ligne de journal. Sans
        `number` : le prochain libre de ce dépôt, au moins `floor` ; l'index
        unique `work_items_migration_repo_idx` et le garde de
        `work_migration_reservations` tranchent entre deux réservations
        simultanées : la perdante attend un délai plein jitter croissant,
        borné au budget restant, et recalcule (nouvelle transaction) ;
        épuisé, `ReservationContention` — jamais la DbError de l'index
        (#478). Une autre erreur qui nomme l'index remonte telle quelle
        (codex #478 r1). Un `number` explicite déjà pris lève la DbError de
        l'index, sans réessai. #1830 : `issue_ref` (lue par l'appelant) est une
        condition du CAS, NULL et chaîne vide confondus — une correction
        concurrente de la référence pendant le scan du dépôt ne peut plus
        laisser `issue_ref` d'un dépôt avec `migration_repo` d'un autre ;
        `release` l'ignore. En transaction appelante (`import_reservations`,
        #879 B2), aucune reprise locale : la transaction y est perdue, la
        DbError remonte et l'appelant décide.

        #2481 : quand le lot a déjà une réservation, `number` (ou le prochain
        libre) est AJOUTÉ comme créneau `legacy-<numéro>` de
        `work_migration_reservations`, et le scalaire reste la réservation
        principale. `release` libère tout, ou seulement `release_number`."""
        if release:
            repo = repo or None
        deadline = time.monotonic() + _RESERVE_BUDGET_S
        wait = _RESERVE_WAIT_BASE_S
        while True:
            try:
                with self.db.transaction() as tx:
                    if not _lock_migration_writer(tx, item_id, None if release else repo):
                        return None
                    current = tx.query(
                        "SELECT id, migration, migration_repo, app, branch_target, state,"
                        "       coalesce(issue_ref, '') AS issue_ref"
                        "  FROM work_items WHERE id = %s", (int(item_id),))
                    if not current:
                        return None
                    current = current[0]
                    scalar = (None if current["migration"] is None
                              else int(current["migration"]))
                    slots = []
                    if has_migration_slots(tx):
                        slots = tx.query(
                            "SELECT slot, number, target_repo, target_sha"
                            "  FROM work_migration_reservations"
                            " WHERE work_item_id = %s AND status = 'reserved'"
                            " ORDER BY number, slot", (int(item_id),))
                    if release:
                        return self._release_migration_rows(
                            tx, item_id, slots=slots, scalar=scalar,
                            number=release_number, note=note, actor=actor,
                            state=current["state"], repo=repo)
                    if current["state"] in lifecycle.COMPLETED:
                        return None
                    if (current["issue_ref"] or "") != (issue_ref or ""):
                        return None
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
                            f"   AND state IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                            " ORDER BY id FOR UPDATE", (repo,)):
                        if has_migration_slots(tx):
                            tx.execute(
                                "UPDATE work_migration_reservations SET status = 'released',"
                                "       updated_at = now()"
                                " WHERE work_item_id = %s AND status = 'reserved'",
                                (int(old["id"]),))
                        tx.execute(
                            "UPDATE work_items SET migration = NULL,"
                            "       migration_repo = NULL, updated_at = now()"
                            " WHERE id = %s", (int(old["id"]),))
                        WorkItems(tx)._event(
                            int(old["id"]), old["state"],
                            "migration %s released (lot %s %s, #956)" % (
                                old["migration"], old["id"], old["state"]), actor)
                    if scalar is None and slots:
                        if any(row["slot"] != "default" and not (
                                row["slot"] == "legacy-%d" % int(row["number"])
                                and row.get("target_sha") is None) for row in slots):
                            raise ValueError("named migration slots require --slot; legacy allocation refused")
                        # état incohérent (scalaire effacé hors API) : le plus
                        # petit créneau redevient la réservation principale
                        first = slots[0]
                        tx.execute(
                            "UPDATE work_migration_reservations SET status = 'released',"
                            "       updated_at = now()"
                            " WHERE work_item_id = %s AND slot = %s"
                            "   AND status = 'reserved'", (int(item_id), first["slot"]))
                        tx.query(
                            "UPDATE work_items SET migration = %%s, migration_repo = %%s,"
                            "       updated_at = now() WHERE id = %%s"
                            " RETURNING %s" % item_columns(tx),
                            (int(first["number"]), repo, int(item_id)))
                        scalar = int(first["number"])
                    if scalar is not None and (current["migration_repo"] or "") != (repo or ""):
                        # déplacement de la réservation principale vers un
                        # autre dépôt (appel storage direct, #711) : les
                        # créneaux de l'ancien dépôt sont rendus, puis le
                        # scalaire est réécrit ; le déclencheur 0419 recrée
                        # le créneau `default` dans le nouveau dépôt.
                        if has_migration_slots(tx):
                            tx.execute(
                                "UPDATE work_migration_reservations SET status = 'released',"
                                "       updated_at = now()"
                                " WHERE work_item_id = %s AND status = 'reserved'",
                                (int(item_id),))
                        if number is None:
                            value, params = (
                                "greatest(%s, (SELECT coalesce(max(migration), 0) + 1"
                                " FROM work_items WHERE migration_repo = %s"
                                f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')),"
                                " (SELECT coalesce(max(number), 0) + 1"
                                "  FROM work_migration_reservations"
                                "  WHERE target_repo = %s AND status = 'reserved'))",
                                (int(floor), repo, "github.com/" + repo))
                        else:
                            value, params = "%s", (int(number),)
                        rows = tx.query(
                            "UPDATE work_items SET migration = %s, migration_repo = %%s,"
                            "       updated_at = now() WHERE id = %%s"
                            f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                            "   AND coalesce(issue_ref, '') = coalesce(%%s, '')"
                            " RETURNING %s" % (value, item_columns(tx)),
                            params + (repo, int(item_id), issue_ref))
                        if not rows:
                            return None
                        WorkItems(tx)._event(
                            int(item_id), rows[0]["state"],
                            note % {"number": int(rows[0]["migration"] or 0)}, actor)
                        return rows[0]
                    if scalar is not None:
                        # la réservation principale est conservée : le
                        # nouveau numéro devient un créneau supplémentaire
                        if number is not None:
                            value = int(number)
                            if value == scalar or any(
                                    int(row["number"]) == value for row in slots):
                                return self._migration_row(tx, item_id)
                        else:
                            row = tx.query(
                                "SELECT greatest(%s,"
                                " (SELECT coalesce(max(migration), 0) + 1 FROM work_items"
                                "  WHERE migration_repo = %s"
                                f"    AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')),"
                                " (SELECT coalesce(max(number), 0) + 1"
                                "  FROM work_migration_reservations"
                                "  WHERE target_repo = %s AND status = 'reserved')) AS n",
                                (int(floor), repo, "github.com/" + repo))[0]
                            value = int(row["n"])
                        slot = "legacy-%d" % value
                        tx.execute(
                            "INSERT INTO work_migration_reservations"
                            " (work_item_id, slot, number, project, target_repo,"
                            "  target_branch, status)"
                            " VALUES (%s, %s, %s, %s, %s, NULL, 'reserved')"
                            " ON CONFLICT (work_item_id, slot) DO UPDATE SET"
                            "   number = excluded.number, project = excluded.project,"
                            "   target_repo = excluded.target_repo,"
                            "   target_branch = excluded.target_branch,"
                            "   status = 'reserved', updated_at = now()",
                            (int(item_id), slot, value, current["app"] or "",
                             "github.com/" + repo))
                        WorkItems(tx)._event(
                            int(item_id), current["state"],
                            note % {"number": value}, actor)
                        return self._migration_row(tx, item_id)
                    # aucune réservation : le scalaire reste la principale,
                    # le déclencheur 0419 crée son créneau `default`
                    if number is None:
                        value, params = (
                            "greatest(%s, (SELECT coalesce(max(migration), 0) + 1"
                            " FROM work_items WHERE migration_repo = %s"
                            f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')),"
                            " (SELECT coalesce(max(number), 0) + 1"
                            "  FROM work_migration_reservations"
                            "  WHERE target_repo = %s AND status = 'reserved'))",
                            (int(floor), repo, "github.com/" + repo))
                    else:
                        value, params = "%s", (int(number),)
                    rows = tx.query(
                        "UPDATE work_items SET migration = %s, migration_repo = %%s,"
                        "       updated_at = now()"
                        " WHERE id = %%s"
                        f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                        "   AND coalesce(issue_ref, '') = coalesce(%%s, '')"
                        " RETURNING %s" % (value, item_columns(tx)),
                        params + (repo, int(item_id), issue_ref))
                    if not rows:
                        return None
                    WorkItems(tx)._event(
                        int(item_id), rows[0]["state"],
                        note % {"number": int(rows[0]["migration"] or 0)}, actor)
                    return rows[0]
            except DbError as exc:
                if (number is not None or release or in_transaction(self.db)
                        or not (is_unique_violation(exc, MIGRATION_INDEX)
                                or is_unique_violation(
                                    exc, "work_migration_reservations_active_idx"))):
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
        tient plus). #2481 : les créneaux supplémentaires comptent aussi (un
        lot peut réserver plusieurs numéros)."""
        if has_migration_slots(self.db):
            rows = self.db.query(
                "SELECT w.id, w.title, w.state FROM work_items w"
                f" WHERE w.state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                "   AND ((w.migration = %s AND w.migration_repo = %s)"
                "        OR EXISTS (SELECT 1 FROM work_migration_reservations r"
                "                    WHERE r.work_item_id = w.id"
                "                      AND r.status = 'reserved' AND r.number = %s"
                "                      AND (r.target_repo = %s OR r.target_repo = %s)))"
                " ORDER BY w.id LIMIT 1",
                (int(number), repo, int(number), repo, "github.com/" + repo))
        else:
            rows = self.db.query(
                "SELECT id, title, state FROM work_items WHERE migration = %s"
                " AND migration_repo = %s"
                f" AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')", (int(number), repo))
        return rows[0] if rows else None

    def record_landed_migration(self, item_id, number, repo, *, note, actor) -> dict | None:
        """#1567 : enregistre (ou corrige) le numéro atterri d'un lot **déjà**
        fusionné ou promu, dans une transaction avec sa ligne de journal ; None
        si le lot n'est pas terminal. La réservation (`migration`,
        `migration_repo`) n'est pas touchée : elle peut déjà être libérée ;
        le créneau `default` atterri, lui, suit le numéro posé (#4097)."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_items SET migration_landed = %s, migration_landed_repo = %s,"
                "       updated_at = now()"
                f" WHERE id = %s AND state IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}')"
                " RETURNING __COLUMNS__".replace("__COLUMNS__", item_columns(self.db)),
                (int(number), repo, int(item_id)))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            _land_default_slot(tx, item_id, number, repo)
            return rows[0]

    def landed_slot(self, item_id) -> dict | None:
        """#4100 : le créneau `default` atterri du lot (`number`, `repo` sous la
        forme `owner/repo`), ou None."""
        if not has_migration_slots(self.db):
            return None
        rows = self.db.query(
            "SELECT number, target_repo FROM work_migration_reservations"
            " WHERE work_item_id = %s AND slot = 'default' AND status = 'landed'",
            (int(item_id),))
        if not rows:
            return None
        repo = str(rows[0]["target_repo"] or "")
        return {"number": int(rows[0]["number"]),
                "repo": repo[len("github.com/"):] if repo.startswith("github.com/") else repo}

    def release_landed_slot(self, item_id, number, repo, *, note, actor) -> dict | None:
        """#4100 : relâche le créneau atterri `number` d'un lot déjà fusionné
        ou promu, avec sa ligne de journal, en une transaction ; None si le lot
        n'est pas terminal."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT __COLUMNS__ FROM work_items WHERE id = %s"
                f"   AND state IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}') FOR UPDATE".replace(
                    "__COLUMNS__", item_columns(self.db)), (int(item_id),))
            if not rows:
                return None
            _release_landed_slot(tx, item_id, number, repo)
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    def migration_landed_holder(self, number, repo) -> dict | None:
        """Le lot qui nomme déjà ce numéro atterri dans ce dépôt (#1567)."""
        rows = self.db.query(
            "SELECT id, title, state FROM work_items WHERE migration_landed = %s"
            " AND migration_landed_repo = %s", (int(number), repo))
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
                  source="", frozen_id=None, packet=None, packet_initial=False, lease=None,
                  withdrawn_after=None) -> dict | None:
        with self.db.transaction() as tx:
            lot = tx.query("SELECT state, app, branch, branch_target, type FROM work_items WHERE id = %s"
                           f" AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                           " FOR NO KEY UPDATE", (int(item_id),))
            if not lot:
                return None
            # #3532: read under the lot row lock that `queue_remove` takes
            # first, so a take-out cannot slip between this check and insertion.
            # #3550: the take-out only binds the target and freeze it names.
            if withdrawn_after is not None and WorkItems(tx).queue_withdrawn_since(
                    item_id, withdrawn_after, target=target,
                    frozen_id=frozen_id) is not None:
                return None
            if lease is not None:
                key, owner, epoch = lease
                held = tx.query("SELECT owner, epoch FROM merge_locks WHERE target = %s FOR UPDATE",
                                (key,))
                if not held or (held[0]["owner"], held[0]["epoch"]) != (owner, epoch):
                    return None
                # Check after acquiring the row lock: waiting cannot extend
                # the lease through a transaction-start timestamp.
                if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                                " AND expires_at > clock_timestamp()", (key,)):
                    return None
            latest = tx.query("SELECT id FROM work_item_milestones WHERE work_item_id = %s"
                              " AND kind = 'frozen' ORDER BY at DESC, id DESC LIMIT 1", (int(item_id),))
            latest_id = latest[0]["id"] if latest else None
            if frozen_id is not None and frozen_id != latest_id:
                return None
            if packet is not None:
                fields = ("app", "branch", "branch_target", "type")
                if frozen_id is None or lot[0]["state"] not in lifecycle.TO_VALIDATE \
                        or set(packet) != set(fields) | {"resolved_target"} \
                        or packet["resolved_target"] != target \
                        or any(lot[0][key] != packet[key] for key in fields):
                    return None
                current = self._queue_row(tx, item_id)
                markers = tx.query("SELECT kind, frozen_id, generation FROM integration_train_queue_receipts"
                                   " WHERE work_item_id = %s ORDER BY id", (int(item_id),))
                allocated = None
                for receipt in markers:
                    if receipt.get("frozen_id") != frozen_id:
                        continue
                    if receipt.get("kind") == "removed":
                        return None
                    if receipt.get("kind") == "packet":
                        allocated = receipt.get("generation")
                if current is not None:
                    same = (current["generation"], current["target"], current["frozen_id"],
                            current["frozen_sha"], current["source"])
                    if same != (allocated, target, frozen_id, frozen_sha, source):
                        return None
                    if current["state"] == "queued":
                        return current
                    if not interface.ci_required_ejection(current):
                        return None
                    # #3560 : la file radar a éjecté cette allocation faute de CI
                    # de lot (§11 bis : il n'y en a pas, la CI du paquet en tient
                    # lieu). Même gel, même SHA, aucun retrait : l'allocation est
                    # remise en file sous une nouvelle génération et son reçu ;
                    # un écrivain resté sur l'ancienne génération est refusé.
                    # La ligne lue n'est pas verrouillée (une éjection n'écrit
                    # que merge_queue) : l'UPDATE compare la ligne entière lue,
                    # cause comprise, et une éjection concurrente pour une autre
                    # cause le fait échouer (revue cx3, B/P2).
                    requeued = tx.query(
                        "UPDATE merge_queue SET state = 'queued', rank = NULL, landing_sha = NULL,"
                        "   reason = '', generation = nextval('merge_queue_generation_seq'),"
                        "   queued_by = %s, queued_at = clock_timestamp(), updated_at = now()"
                        " WHERE work_item_id = %s AND state = 'ejected' AND generation = %s"
                        "   AND reason = %s AND target = %s AND frozen_id IS NOT DISTINCT FROM %s"
                        "   AND frozen_sha = %s AND source = %s"
                        " RETURNING work_item_id",
                        (actor or "", int(item_id), allocated, current["reason"], target,
                         frozen_id, frozen_sha, source))
                    if not requeued:
                        return None
                    WorkItems(tx)._event(int(item_id), lot[0]["state"],
                                         "%s (#3560: allocation ejected for want of a lot CI, "
                                         "requeued: generation %s)" % (note, allocated), actor)
                    row = self._queue_row(tx, item_id)
                    tx.execute("INSERT INTO integration_train_queue_receipts"
                               " (work_item_id, generation, frozen_id, kind) VALUES (%s, %s, %s, 'packet')",
                               (row["work_item_id"], row["generation"], row["frozen_id"]))
                    return dict(row, packet_allocated=True)
                if allocated is not None:
                    return None  # the allocated entry was withdrawn; do not resurrect it
                if packet_initial is not True:
                    return None  # absence is ambiguous for legacy DELETE-only writers
            conflict = (" DO NOTHING" if packet is not None else
                " DO UPDATE SET target = excluded.target,"
                "   frozen_sha = excluded.frozen_sha, state = 'queued', rank = NULL,"
                "   source = excluded.source, frozen_id = excluded.frozen_id,"
                "   generation = nextval('merge_queue_generation_seq'), landing_sha = NULL,"
                "   reason = '', queued_by = excluded.queued_by, queued_at = now(),"
                "   updated_at = now()")
            # Packet cutoff eligibility starts at insertion, not at an
            # enclosing transaction's BEGIN before it waited for these locks.
            inserted = tx.query(
                "INSERT INTO merge_queue (work_item_id, target, frozen_sha, queued_by, source, frozen_id, queued_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, "
                + ("clock_timestamp()" if packet is not None else "now()") + ")"
                " ON CONFLICT (work_item_id)" + conflict + " RETURNING work_item_id",
                (int(item_id), target, frozen_sha, actor or "", source or "", latest_id))
            if not inserted:
                return None
            WorkItems(tx)._event(int(item_id), lot[0]["state"], note, actor)
            row = self._queue_row(tx, item_id)
            if packet is not None:
                tx.execute("INSERT INTO integration_train_queue_receipts"
                           " (work_item_id, generation, frozen_id, kind) VALUES (%s, %s, %s, 'packet')",
                           (row["work_item_id"], row["generation"], row["frozen_id"]))
                # #3560 : la ligne rendue est lue avant son reçu ; elle le porte
                # déjà, comme toute relecture (`packet_allocated`).
                row = dict(row, packet_allocated=True)
            return row

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
            # Same order as allocation/refreeze: a withdrawal cannot vanish
            # between a packet's check for absence and its insertion.
            lot = tx.query("SELECT state FROM work_items WHERE id = %s FOR NO KEY UPDATE", (int(item_id),))
            rows = tx.query("DELETE FROM merge_queue WHERE work_item_id = %s"
                            " RETURNING work_item_id, generation, frozen_id, target",
                            (int(item_id),))
            if not rows:
                return False
            WorkItems(tx)._event(int(item_id), lot[0]["state"], note, actor)
            tx.execute("INSERT INTO integration_train_queue_receipts"
                       " (work_item_id, generation, frozen_id, target, kind)"
                       " VALUES (%s, %s, %s, %s, 'removed')",
                       (rows[0]["work_item_id"], rows[0]["generation"],
                        rows[0]["frozen_id"], rows[0].get("target")))
            return True

    def queue_withdrawn_since(self, item_id, since_ts, *, target=None,
                              frozen_id=None) -> float | None:
        # #3550: a take-out is final only for the queue entry it removed — its
        # target and freeze. A removal of another target, or of an older
        # freeze, must not stop a new fire's fallback. Legacy receipts without
        # a target keep matching (they cannot be told apart); a receipt
        # without a freeze never matches a named freeze.
        rows = self.db.query(
            "SELECT extract(epoch FROM max(created_at))::float8 AS at"
            " FROM integration_train_queue_receipts WHERE work_item_id = %s"
            " AND kind = 'removed' AND created_at >= to_timestamp(%s)"
            " AND (%s::text IS NULL OR target IS NULL OR target = %s)"
            " AND (%s::bigint IS NULL OR (frozen_id IS NOT NULL AND frozen_id = %s))",
            (int(item_id), float(since_ts), target, target, frozen_id, frozen_id))
        return rows[0]["at"] if rows and rows[0]["at"] is not None else None

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

    def land_holder(self, key) -> dict | None:
        """#3598 : qui tient le bail, et jusqu'à quand, pour le refus de
        `give`/`withdraw`. Horloge de la base, lecture seule, jamais une garde.
        L'échéance est rendue en UTC ISO (`…Z`), identique sous les deux
        pilotes plutôt que datetime (psycopg) ou texte (psql)."""
        rows = self.db.query(
            "SELECT owner,"
            " to_char(expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
            "   AS expires_at,"
            " extract(epoch FROM (expires_at - clock_timestamp()))::float8 AS left_s"
            " FROM merge_locks WHERE target = %s AND owner IS NOT NULL"
            "   AND expires_at > clock_timestamp()", (key,))
        return dict(rows[0]) if rows else None

    def train_read(self, key) -> dict | None:
        rows = self.db.query(
            "SELECT revision, document FROM integration_trains WHERE target_key = %s", (key,))
        if not rows:
            return None
        document = rows[0]["document"]
        if isinstance(document, str):
            document = json.loads(document)
        return dict(document, revision=int(rows[0]["revision"]))

    def train_save(self, key, document, *, revision, owner, epoch) -> dict | None:
        with self.db.transaction() as tx:
            held = tx.query("SELECT owner, epoch"
                            " FROM merge_locks WHERE target = %s FOR UPDATE", (key,))
            if not held or held[0]["owner"] != owner or int(held[0]["epoch"]) != epoch:
                return None
            # Recheck after the row lock, never using a pre-lock clock.
            live = tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                            " AND expires_at > clock_timestamp()", (key,))
            if not live:
                return None
            rows = tx.query(
                "INSERT INTO integration_trains (target_key, revision, document)"
                " SELECT %s, 1, %s::jsonb WHERE %s = 0"
                " ON CONFLICT (target_key) DO NOTHING RETURNING revision",
                (key, json.dumps(document), int(revision))) if revision == 0 else tx.query(
                "UPDATE integration_trains SET revision = revision + 1, document = %s::jsonb,"
                " updated_at = clock_timestamp() WHERE target_key = %s AND revision = %s"
                " RETURNING revision", (json.dumps(document), key, int(revision)))
            return dict(document, revision=int(rows[0]["revision"])) if rows else None

    def train_abandon(self, key, plan_id, document, *, revision, owner, epoch) -> dict | None:
        with self.db.transaction() as tx:
            held = tx.query("SELECT owner, epoch"
                            " FROM merge_locks WHERE target = %s FOR UPDATE", (key,))
            if not held or held[0]["owner"] != owner or int(held[0]["epoch"]) != epoch:
                return None
            live = tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                            " AND expires_at > clock_timestamp()", (key,))
            if not live:
                return None
            # The target row lock is also taken by train_fire_give: a grant is
            # either committed before this read (seen just below and refused),
            # or waits and then finds the terminal phase this write leaves.
            rows = tx.query("SELECT document FROM integration_trains"
                            " WHERE target_key = %s FOR UPDATE", (key,))
            if not rows:
                return None
            current = rows[0]["document"]
            if isinstance(current, str):
                current = json.loads(current)
            phase = current.get("phase")
            if phase in ("landed", "empty", "paused", "invalidated") or phase == "promoting" \
                    or (current.get("plan") or {}).get("id") != plan_id:
                return None
            fire = WorkItems(tx).train_fire_read(key, plan_id)
            if fire and fire.get("state") in ("active", "promoting", "landed"):
                return None
            if (document.get("plan") or {}).get("id") != plan_id:
                return None
            updated = tx.query(
                "UPDATE integration_trains SET revision = revision + 1, document = %s::jsonb,"
                " updated_at = clock_timestamp() WHERE target_key = %s AND revision = %s"
                " RETURNING revision", (json.dumps(document), key, int(revision)))
            return dict(document, revision=int(updated[0]["revision"])) if updated else None

    def train_adopt(self, key, owner, *, revision, previous, service, ttl_s) -> dict | None:
        if not owner.startswith("train:") or key == service[0]:
            return None
        # L165 (EP4) × #3142, B cx2 : une clé de service liée à un rôle porte
        # l'époque du rôle (`train-service:<rôle>:<époque>`). Elle survit à sa
        # tenure par le TTL de service (900 s) : l'adoption doit aussi prouver
        # que cette époque est le bail vivant courant du rôle, sinon l'ancien
        # élu reprendrait la réservation au nouvel élu.
        parts = str(service[0] or "").split(":")
        role = (parts[1], int(parts[2])) if (len(parts) == 3 and parts[1]
                and parts[0] == "train-service" and parts[2].isdigit()) else None
        with self.db.transaction() as tx:
            # Service lease first, then the target: no writer locks them in
            # the other order (train_save only locks the target row).
            if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s AND owner = %s"
                            " AND epoch = %s AND expires_at > clock_timestamp() FOR SHARE",
                            (service[0], service[1], int(service[2]))):
                return None
            if role is not None:
                current = tx.query(
                    "SELECT lease_epoch, status,"
                    " lease_expires_at > clock_timestamp() AS live"
                    " FROM agent_registry WHERE name = %s FOR SHARE", (role[0],))
                if not current or current[0]["lease_epoch"] is None \
                        or int(current[0]["lease_epoch"]) != role[1] \
                        or not current[0]["live"] \
                        or str(current[0]["status"] or "") == "stopped":
                    return None
            held = tx.query("SELECT owner, epoch FROM merge_locks WHERE target = %s FOR UPDATE",
                            (key,))
            if not held or not (held[0]["owner"] or "").startswith("train:") \
                    or held[0]["owner"] == owner:
                return None
            before = (held[0]["owner"], int(held[0]["epoch"]))
            if previous is not None and before != (previous[0], int(previous[1])):
                return None
            rows = tx.query("SELECT revision, document FROM integration_trains"
                            " WHERE target_key = %s FOR UPDATE", (key,))
            if (int(rows[0]["revision"]) if rows else 0) != int(revision):
                return None
            document = rows[0]["document"] if rows else None
            if isinstance(document, str):
                document = json.loads(document)
            ticket = (document or {}).get("reservation")
            # A ticket naming the held epoch moves with it, whoever asked:
            # left behind, it would bind the plan to the fenced holder.
            if not isinstance(ticket, dict) \
                    or (ticket.get("owner"), ticket.get("epoch")) != before:
                if previous is not None:
                    return None
                document = None
            # The target or document lock may have waited: recheck the
            # service lease against wall time after every lock, like train_save.
            if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s AND owner = %s"
                            " AND epoch = %s AND expires_at > clock_timestamp()",
                            (service[0], service[1], int(service[2]))):
                return None
            # The role lease can also expire while those locks wait: FOR SHARE
            # blocks a claim, not time. Recheck the exact epoch, liveness and
            # status after every lock, next to the final service check
            # (B/P1 cx2, #3142×#3014).
            if role is not None:
                current = tx.query(
                    "SELECT 1 FROM agent_registry WHERE name = %s AND lease_epoch = %s"
                    " AND lease_expires_at > clock_timestamp()"
                    " AND coalesce(status, '') <> 'stopped' FOR SHARE",
                    (role[0], role[1]))
                if not current:
                    return None
            epoch = int(tx.query(
                "UPDATE merge_locks SET owner = %s, epoch = epoch + 1,"
                " expires_at = clock_timestamp() + %s * interval '1 second'"
                " WHERE target = %s RETURNING epoch", (owner, float(ttl_s), key))[0]["epoch"])
            if document is not None:
                document["reservation"] = dict(document["reservation"], owner=owner, epoch=epoch)
                tx.execute("UPDATE integration_trains SET revision = revision + 1,"
                           " document = %s::jsonb, updated_at = clock_timestamp()"
                           " WHERE target_key = %s", (json.dumps(document), key))
            return {"epoch": epoch, "previous_owner": before[0], "previous_epoch": before[1]}

    @staticmethod
    def _train_fire(rows):
        if not rows:
            return None
        row = dict(rows[0])
        document = row.pop("document")
        if isinstance(document, str):
            document = json.loads(document)
        return dict(document, **row)

    def train_fire_read(self, key, plan_id) -> dict | None:
        return self._train_fire(self.db.query(
            "SELECT id, document, state, changed_by, owner, epoch,"
            " extract(epoch from created_at)::float8 AS created_ts"
            " FROM integration_train_fires WHERE target_key = %s AND plan_id = %s"
            " ORDER BY id DESC LIMIT 1", (key, plan_id)))

    def train_fire_give(self, key, document) -> dict | None:
        with self.db.transaction() as tx:
            if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s FOR UPDATE", (key,)):
                return None
            # Serialize both a regrant and a plan revision with the claim.
            rows = tx.query("SELECT document FROM integration_trains WHERE target_key = %s",
                            (key,))
            current = rows[0]["document"] if rows else {}
            if isinstance(current, str):
                current = json.loads(current)
            if current.get("plan") != document["plan"] or current.get("phase") not in (
                    "publishing", "ci-intent", "ci", "ready"):
                return None
            ops = WorkItems(tx)
            old = ops.train_fire_read(key, document["plan"]["id"])
            if old and old["state"] != "revoked":
                return None
            tx.execute("INSERT INTO integration_train_fires"
                       " (target_key, plan_id, document, state, changed_by)"
                       " VALUES (%s, %s, %s::jsonb, 'active', %s)",
                       (key, document["plan"]["id"], json.dumps(document), document["actor"]))
            return ops.train_fire_read(key, document["plan"]["id"])

    def train_fire_change(self, key, fire_id, state, *, actor, owner=None, epoch=None) -> dict | None:
        expected = {"revoked": "active", "promoting": "active", "landed": "promoting"}
        if state not in expected:
            raise ValueError("train fire transition: revoked | promoting | landed expected")
        with self.db.transaction() as tx:
            locks = tx.query("SELECT owner, epoch FROM merge_locks WHERE target = %s FOR UPDATE",
                             (key,))
            if not locks:
                return None
            if state != "revoked":
                if owner is None or epoch is None or locks[0]["owner"] != owner \
                        or int(locks[0]["epoch"]) != epoch:
                    return None
                if not tx.query("SELECT 1 FROM merge_locks WHERE target = %s"
                                " AND expires_at > clock_timestamp()", (key,)):
                    return None
            rows = tx.query("SELECT document, owner, epoch FROM integration_train_fires"
                            " WHERE target_key = %s AND id = %s AND state = %s FOR UPDATE",
                            (key, int(fire_id), expected[state]))
            if not rows:
                return None
            document = rows[0]["document"]
            if isinstance(document, str):
                document = json.loads(document)
            if state != "revoked":
                # The engine must have durably entered promoting on this
                # exact plan, under the same target reservation.
                if not tx.query("SELECT 1 FROM integration_trains WHERE target_key = %s"
                                " AND document->>'phase' = 'promoting'"
                                " AND document->'plan' = %s::jsonb",
                                (key, json.dumps(document["plan"]))):
                    return None
                # A new live holder may finish a previous holder's claim
                # after observing the exact remote head. It cannot reactivate
                # the fire or retry the effect; business code supplies that
                # fresh observation before requesting this transition.
            tx.execute("UPDATE integration_train_fires SET state = %s, changed_by = %s,"
                       " owner = %s, epoch = %s, updated_at = clock_timestamp() WHERE id = %s",
                       (state, actor, owner, epoch, int(fire_id)))
            return WorkItems(tx).train_fire_read(key, document["plan"]["id"])

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
            f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
            " ORDER BY id LIMIT %%s" % item_columns(self.db), (int(limit),))

    def open_for(self, assignee) -> list[dict]:
        """Les lots ouverts (ni fusionnés, ni promus, ni fermés) d'un assigné."""
        return self.db.query(
            "SELECT %s FROM work_items"
            f" WHERE assignee = %%s AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
            " ORDER BY id" % item_columns(self.db), (assignee,))

    def held_by(self, agents) -> dict[str, dict]:
        """#2877 : ce que ces agents tiennent encore, en une lecture sans
        verrou — par assigné, `open` (un lot ni fusionné, ni promu, ni fermé)
        et `promoted` (un lot promu) ; un agent qui ne tient rien est absent."""
        names = sorted({str(name) for name in agents if name})
        if not names:
            return {}
        rows = self.db.query(
            f"SELECT assignee, bool_or(coalesce(to_jsonb(work_items)->>'legacy_state', state) <> 'promoted') AS open,"
            f"       bool_or(coalesce(to_jsonb(work_items)->>'legacy_state', state) = 'promoted') AS promoted"
            "  FROM work_items"
            f" WHERE assignee IN (%s) AND coalesce(to_jsonb(work_items)->>'legacy_state', state) NOT IN ('merged', 'closed')"
            " GROUP BY assignee" % ", ".join(["%s"] * len(names)), tuple(names))
        return {row["assignee"]: {"open": bool(row["open"]),
                                  "promoted": bool(row["promoted"])} for row in rows}

    def release_agent(self, agent, *, reason, actor, measure_actor="",
                      measure_reason="", dry_run=False, stopped_with=()) -> list[dict]:
        """#753 : à l'arrêt d'un agent, ses lots ouverts retournent à la file
        d'équipe — assigné effacé, journal au lot (raison), délégation en cours
        annulée —, et ses lots promus (un déploiement porte toujours sa mesure
        post-déploiement à refaire) vont à `measure_actor` (l'auditeur), journal
        au lot (`measure_reason`). Le tout dans une transaction ; le lot est
        verrouillé puis l'assigné recontrôlé : un lot repris entre-temps n'est
        pas touché. `stopped_with` : si non vide, l'agent est relu et verrouillé
        dans la transaction, et rien n'est rendu s'il n'est plus `stopped` pour
        l'une de ces raisons (reprise entre le relevé et le geste). `dry_run` :
        rien n'est écrit, les lots concernés sont rendus tels quels (clé
        `assigned_to` pour ceux qui iraient à l'acteur de la mesure)."""
        with self.db.transaction() as tx:
            return WorkItems(tx)._release_agent(
                agent, reason=reason, actor=actor, measure_actor=measure_actor,
                measure_reason=measure_reason, dry_run=dry_run,
                stopped_with=stopped_with)

    def _cancel_delegation(self, item_id, actor) -> None:
        """Annule la délégation en cours du lot (L40), dans la transaction."""
        self.db.query(
            "UPDATE work_item_delegations SET outcome = 'annulee',"
            "       resolved_at = now(), resolved_by = %s"
            " WHERE work_item_id = %s AND outcome IS NULL RETURNING id",
            (actor or "", int(item_id)))

    def _release_agent(self, agent, *, reason, actor, measure_actor="",
                       measure_reason="", dry_run=False, stopped_with=()) -> list[dict]:
        """`release_agent` dans la transaction ouverte."""
        lock = "" if dry_run else " FOR NO KEY UPDATE"
        # #753 (N3) : recontrôle de l'agent DANS la transaction. Sans lui, une
        # reprise (`ameesh resume`) ou un arrêt d'une autre nature entre le
        # relevé du registre et le geste rendrait les lots d'un agent qui
        # travaille de nouveau. Verrou de la ligne du registre d'abord (ordre
        # stable agent -> lots).
        # #1425 (N6) : cet ordre est un invariant global. Les seules
        # transactions qui verrouillent les DEUX tables le prennent dans ce
        # sens — ici et `handoff` (`agent_registry ... FOR SHARE` puis
        # `work_items`), plus `handoffs.drain` (registre puis lecture des
        # lots) ; aucun chemin ne verrouille `work_items` puis
        # `agent_registry`. Le tenir ainsi évite le cycle de verrous que
        # PostgreSQL devrait sinon détecter et abandonner.
        if stopped_with:
            rows = self.db.query(
                "SELECT status, stop_reason FROM agent_registry"
                "   WHERE name = %s" + lock, (agent,))
            if (not rows or rows[0]["status"] != "stopped"
                    or rows[0]["stop_reason"] not in tuple(stopped_with)):
                return []
        out: list[dict] = []
        # 1. lots ouverts : file d'équipe (assignee = NULL)
        rows = self.db.query(
            "SELECT %s FROM work_items WHERE assignee = %%s"
            f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
            " ORDER BY id%s" % (item_columns(self.db), lock), (agent,))
        for row in rows:
            if dry_run:
                out.append(dict(row, dry_run=True))
                continue
            updated = self.db.query(
                "UPDATE work_items SET assignee = NULL, updated_at = now(),"
                "       delegated_by = NULL, delegated_at = NULL, due_at = NULL"
                " WHERE id = %%s AND assignee = %%s"
                f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
                " RETURNING %s" % item_columns(self.db), (int(row["id"]), agent))
            if not updated:
                continue
            self._cancel_delegation(row["id"], actor)
            self._event(int(row["id"]), updated[0]["state"], reason, actor)
            out.append(updated[0])
        # 2. lots promus : acteur de la mesure post-deploiement (l'auditeur).
        #    `work_item_measures` mesure la duree et le diff de livraison, pas
        #    la mesure d'impact a refaire apres deploiement : elle ne dit donc
        #    pas si l'action est close (revue auditeur, R1 #753).
        if measure_actor and measure_actor != agent:
            limit = "" if dry_run else " FOR NO KEY UPDATE OF w"
            rows = self.db.query(
                "SELECT %s FROM work_items w"
                f" WHERE w.assignee = %%s AND coalesce(to_jsonb(w)->>'legacy_state', w.state) = 'promoted'"
                " ORDER BY w.id%s" % (item_columns(self.db, "w"), limit), (agent,))
            for row in rows:
                if dry_run:
                    out.append(dict(row, dry_run=True, assigned_to=measure_actor))
                    continue
                updated = self.db.query(
                    "UPDATE work_items SET assignee = %%s, updated_at = now(),"
                    "       delegated_by = NULL, delegated_at = NULL, due_at = NULL"
                    f" WHERE id = %%s AND assignee = %%s AND coalesce(to_jsonb(work_items)->>'legacy_state', state) = 'promoted'"
                    " RETURNING %s" % item_columns(self.db),
                    (measure_actor, int(row["id"]), agent))
                if not updated:
                    continue
                self._cancel_delegation(row["id"], actor)
                self._event(int(row["id"]), "promoted", measure_reason, actor)
                # #1425 : la même clé que le chemin `dry_run` — sans elle, les
                # appelants (`cmd_stop`, la passe du runner) journalisaient un
                # lot remis à l'auditeur comme « retourné à la file d'équipe ».
                out.append(dict(updated[0], assigned_to=measure_actor))
        return out

    def open_by_ref(self, ref) -> list[dict]:
        """Les lots ouverts désignés par une référence : `issue_ref`, fiche du
        plan (`package_id`), branche, ou premier mot du titre (« REF : … »),
        sans distinction de casse."""
        return self.db.query(
            "SELECT %s FROM work_items"
            f" WHERE state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
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

    def by_source(self, source) -> list[dict]:
        """#3607 : les lots dont la `source` vaut exactement cette référence,
        quel que soit leur état. La comparaison est à la casse près, comme
        `by_issue_ref` (la casse d'un dépôt GitHub ne fait pas une autre
        exécution). Une source vide ne désigne rien : sans la garde, elle
        rendrait tous les lots sans source."""
        text = str(source or "").strip()
        if not text:
            return []
        return self.db.query(
            "SELECT %s FROM work_items WHERE lower(source) = lower(%%s)"
            " ORDER BY id" % item_columns(self.db), (text,))

    def by_package(self, package_id) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM work_items WHERE package_id = %%s ORDER BY id" % item_columns(self.db),
            (package_id,))

    # -- paquet de composition (#2443) ---------------------------------------
    #: la lecture commune aux deux méthodes : l'appartenance, le porteur et
    #: ses jalons de fusion et de gel, en une requête.
    _PACKET_SELECT = (
        "SELECT m.package_id, m.work_item_id, m.package_branch, m.base_sha,"
        "       m.head_sha, m.unit_sha, m.source_sha, m.frozen_sha,"
        "       m.recorded_by, extract(epoch from m.recorded_at)::float8 AS recorded_ts,"
        "       p.state AS package_state, p.branch AS carrier_branch,"
        "       p.branch_target AS package_target,"
        "       (SELECT ms.sha FROM work_item_milestones ms"
        "         WHERE ms.work_item_id = m.package_id AND ms.kind = 'merged'"
        "           AND ms.sha <> ''"
        "         ORDER BY ms.at DESC, ms.id DESC LIMIT 1) AS package_merged_sha,"
        # #3634 : la tête gelée du porteur, sans filtre sur son sha : le
        # dernier gel fait autorité, même s'il ne nomme aucun commit.
        "       (SELECT ms.sha FROM work_item_milestones ms"
        "         WHERE ms.work_item_id = m.package_id AND ms.kind = 'frozen'"
        "         ORDER BY ms.at DESC, ms.id DESC LIMIT 1) AS package_frozen_sha"
        "  FROM work_packet_members m JOIN work_items p ON p.id = m.package_id"
    )

    def record_packet(self, package_id, *, branch, base_sha, head_sha, units, note,
                      actor, expect_pushed=None) -> list[dict]:
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT id, state, branch_head,"
                "       extract(epoch FROM branch_head_at)::float8 AS branch_head_ts"
                " FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                (int(package_id),))
            if not rows:
                raise DbError("packet #%s: no such work item" % package_id)
            # #4501 : sous le verrou du porteur, relire la tête poussée avant
            # toute écriture. `FOR NO KEY UPDATE` est exclusif avec lui-même
            # (matrice PostgreSQL : deux verrous de ce mode s'attendent) et
            # avec les verrous pris par un gel (`add_milestone`) ou un relevé
            # de `branch_head` (`UPDATE`) : soit ils ont commité avant et
            # cette lecture les voit, soit ils attendent et l'enregistrement
            # porte sur la tête courante. Sans cette relecture, une lecture
            # initiale périmée de `packet_link.record` écrasait le manifeste
            # d'une tête poussée entre-temps; sans le verrou exclusif, deux
            # enregistrements concurrents mélangeaient leurs membres.
            freeze = tx.query(
                "SELECT sha, extract(epoch FROM at)::float8 AS at_ts"
                " FROM work_item_milestones"
                " WHERE work_item_id = %s AND kind = 'frozen'"
                " ORDER BY at DESC, id DESC LIMIT 1", (int(package_id),))
            protected = packet_head.protected_head(
                freeze[0]["sha"] if freeze else "",
                freeze[0]["at_ts"] if freeze else 0,
                rows[0].get("branch_head") or "",
                rows[0].get("branch_head_ts") or 0)
            if protected and not packet_head.same_commit(protected, head_sha) \
                    and not (expect_pushed
                             and packet_head.same_commit(expect_pushed, protected)):
                raise DbError(packet_head.refusal(
                    int(package_id), protected, head_sha, expect_pushed))
            # #4369 : une unité que la recomposition retire sort du véhicule ;
            # la note du porteur la nomme, l'horloge « verdict → fusion »
            # repart de là (`slo.vehicle_exits`). Une seule ligne par
            # recomposition, même quand le composeur rejoue le paquet pas à pas.
            kept = {int(unit["work_item_id"]) for unit in units}
            removed = [int(row["work_item_id"]) for row in tx.query(
                "SELECT work_item_id FROM work_packet_members WHERE package_id = %s"
                " ORDER BY work_item_id", (int(package_id),))
                if int(row["work_item_id"]) not in kept]
            if removed:
                note = "%s%s%s" % (note or "", VEHICLE_EXIT_MARK,
                                   ", ".join("#%d" % lot for lot in removed))
            tx.execute("DELETE FROM work_packet_members WHERE package_id = %s",
                       (int(package_id),))
            for unit in units:
                tx.execute(
                    "INSERT INTO work_packet_members"
                    " (package_id, work_item_id, package_branch, base_sha, head_sha,"
                    "  unit_sha, source_sha, frozen_sha, recorded_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (int(package_id), int(unit["work_item_id"]), branch, base_sha,
                     head_sha, unit["unit_sha"], unit.get("source_sha") or "",
                     unit.get("frozen_sha") or "", actor or ""))
            WorkItems(tx)._event(int(package_id), rows[0]["state"], note, actor)
            return WorkItems(tx).packet_members(package_ids=[int(package_id)])

    def packet_members(self, *, item_ids=None, package_ids=None) -> list[dict]:
        where, params = [], []
        if item_ids is not None:
            ids = sorted({int(value) for value in item_ids})
            if not ids:
                return []
            where.append("m.work_item_id IN (%s)" % ", ".join(["%s"] * len(ids)))
            params.extend(ids)
        if package_ids is not None:
            ids = sorted({int(value) for value in package_ids})
            if not ids:
                return []
            where.append("m.package_id IN (%s)" % ", ".join(["%s"] * len(ids)))
            params.extend(ids)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return self.db.query(
            self._PACKET_SELECT + clause
            + " ORDER BY m.recorded_at DESC, m.package_id DESC, m.work_item_id",
            tuple(params))

    @_retry_deadlock
    def close_merged(self, item_id, *, current, sha, actor, note, pr_ref,
                     frozen_id=None, landed=None, unfrozen=False,
                     release_landed=None) -> dict | None:
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
        ensuite : `add_milestone` prend le même verrou de ligne. `unfrozen`
        (#2444, gel d'origine verdict) : aucun gel examiné ; tout gel relu
        à cet instant refuse de même.

        `landed` (#1567) : `(numéro, dépôt)` du numéro réellement atterri,
        écrit dans la transaction de la fusion (`migration_landed`,
        `migration_landed_repo`).

        #1693 (N1/N4) : le `pr_ref` d'une fusion réelle remplace celui de la
        fusion annulée (une chaîne vide ne l'efface pas), et le jalon visé est
        le plus récent encore sans commit — après une correction, le jalon de
        la fusion annulée reste dans la table, le trigger de 0013 a posé celui
        de la vraie fusion.

        Rien n'est écrit avant ce recontrôle : la fermeture est appelée dans
        la transaction qui porte aussi le réveil des dépendants (L385 suite),
        et un `UPDATE` suivi d'un `None` n'était annulé que par SA transaction
        — sous une transaction englobante, la ligne serait restée modifiée.
        Un lot pinné sur une révision Process n'est jamais fermé ici : le
        filtre est dans le SQL (#1922/N8).
        """
        number, repo = landed if landed else (None, None)
        try:
            with self.db.transaction() as tx:
                if not _lock_migration_writer(tx, item_id):
                    return None
                locked = tx.query(
                    "SELECT id FROM work_items WHERE id = %s AND state = %s AND "
                    + _UNPINNED + " FOR UPDATE",
                    (int(item_id), current))
                if not locked:
                    return None
                if frozen_id is not None or unfrozen:
                    latest = tx.query(
                        "SELECT id FROM work_item_milestones"
                        " WHERE work_item_id = %s AND kind = 'frozen'"
                        " ORDER BY at DESC, id DESC LIMIT 1", (int(item_id),))
                    if (bool(latest) if frozen_id is None
                            else not latest or int(latest[0]["id"]) != int(frozen_id)):
                        raise _Refrozen()
                rows = tx.query(
                    f"""
                    UPDATE work_items
                       SET state = '{lifecycle.DELIVERY_RECORDED}', workflow_stage = 'merged',
                           legacy_state = 'merged', updated_at = now(), closed_at = NULL,
                           pr_ref = coalesce(nullif(%s, ''), pr_ref),
                           migration_landed = coalesce(%s, migration_landed),
                           migration_landed_repo = coalesce(%s, migration_landed_repo)
                     WHERE id = %s AND state = %s AND __UNPINNED__
                    RETURNING __COLUMNS__
                    """.replace("__COLUMNS__", item_columns(self.db)).replace(
                        "__UNPINNED__", _UNPINNED),
                    (pr_ref, number, repo, int(item_id), current))
                if not rows:
                    return None
                WorkItems(tx)._event(int(item_id), "merged", note, actor)
                self._release_migration(tx, item_id, rows[0], why="fusion", actor=actor)
                if number is not None:
                    _land_default_slot(tx, item_id, number, repo)
                elif release_landed is not None:
                    _release_landed_slot(tx, item_id, *release_landed)
                tx.query(
                    "UPDATE work_item_milestones SET sha = %s, actor = %s, note = %s"
                    " WHERE id = (SELECT id FROM work_item_milestones"
                    "              WHERE work_item_id = %s AND kind = 'merged' AND sha = ''"
                    "                AND " + _effective_merged("work_item_milestones") +
                    "              ORDER BY at DESC, id DESC LIMIT 1) RETURNING id",
                    (sha or "", actor or "", note or "", int(item_id)))
                return rows[0]
        except _Refrozen:
            return None

    @_retry_deadlock
    def close(self, item_id, *, current, reason, superseded_by, sha="", actor, note) -> dict | None:
        """`current` → `closed` (abandon, remplacement, ou livraison hors cible)
        si le lot y est encore ; journal et jalon `closed` dans la même
        transaction. `delivered` (#988) écrit en plus le jalon `delivered`, qui
        porte le `sha` de la tête relue. Un lot pinné sur une révision Process
        n'est jamais fermé ici : le filtre est dans le SQL (#1922/N8)."""
        with self.db.transaction() as tx:
            if not _lock_migration_writer(tx, item_id):
                return None
            rows = tx.query(
                f"""
                UPDATE work_items
                   SET state = '{lifecycle.CLOSED}', close_reason = %s, superseded_by = %s,
                       updated_at = now(), closed_at = now()
                 WHERE id = %s AND state = %s AND __UNPINNED__
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)).replace(
                    "__UNPINNED__", _UNPINNED),
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

    def rectify_close(self, item_id, *, current_reason, reason, actor, note) -> dict | None:
        """#1280 : rectifie la raison d'une fermeture déjà écrite. La ligne
        reste `closed` ; `closed_at` et le jalon `closed` sont conservés (la
        fermeture a bien eu lieu), `superseded_by` est effacé, et l'événement
        trace la rectification. Conditionnel à la raison lue par l'appelant :
        une rectification concurrente ne s'écrase pas."""
        with self.db.transaction() as tx:
            rows = tx.query(
                f"""
                UPDATE work_items
                   SET close_reason = %s, superseded_by = NULL, updated_at = now()
                 WHERE id = %s AND state = '{lifecycle.CLOSED}' AND close_reason = %s
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)),
                (reason, int(item_id), current_reason))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), "closed", note, actor)
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
                    actor, app="", estimate_minutes=None, estimate_source=None,
                    estimate_by=None, change_size=None, footprint=None,
                    footprint_unknown=False) -> dict:
        with self.db.transaction() as tx:
            if footprint_unknown:
                # #4818 (B2 de cx3) : la politique se lit et le marqueur
                # s'écrit dans la transaction de création, sous le verrou de
                # la garde — aucune prise ne voit le lot sans son marqueur
                tx.execute(_DEPENDENCIES_LOCK)
            # #4117 : l'estimation (et sa ligne d'historique) est posée avec la
            # création, comme `work.add` (L157) ; jamais après.
            by = (estimate_by or actor or "") if estimate_minutes else None
            rows = tx.query(
                """
                INSERT INTO work_items
                    (type, source, title, body, state, expected_value, value_score,
                     priority, team, required_capabilities, package_id, package_parent,
                     app, estimate_minutes, estimate_source, estimate_by, estimate_at,
                     change_size)
                VALUES ('improvement', %s, %s, %s, 'intake', %s, %s, %s, %s, %s::text[],
                        %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s)
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)),
                (source, title, body, expected_value, int(value_score), int(priority), team,
                 _catalog._text_array(required_capabilities) if required_capabilities else None,
                 package_id, package_parent, app or "", estimate_minutes,
                 estimate_source if estimate_minutes else None, by,
                 bool(estimate_minutes), change_size or None))
            item = rows[0]
            if estimate_minutes:
                WorkItems(tx)._estimate_row(int(item["id"]), estimate_minutes,
                                            estimate_source, estimate_by or actor)
            WorkItems(tx)._event(int(item["id"]), "intake", note, actor)
            # #4186 : la file d'amélioration n'oublie plus l'empreinte — la
            # ligne de panier l'unionne avec celles des lignes précédentes.
            item = dict(item)
            footprint = _unknown_marker(tx, item, footprint, footprint_unknown)
            WorkItems(tx)._intake_footprint(int(item["id"]), footprint, actor)
            return item

    def backlog_basket_add(self, *, key, title, expected_value, line, value_score, priority,
                           team, required_capabilities, package_id, package_parent, note,
                           actor, app="", estimate_minutes=None, estimate_source=None,
                           estimate_by=None, change_size=None,
                           estimate_required=False, footprint=None,
                           footprint_unknown=False) -> dict:
        caps = _catalog._text_array(required_capabilities) if required_capabilities else None
        # #4117 : l'estimation ne s'applique qu'au panier OUVERT par cet ajout ;
        # une ligne ajoutée à un panier existant ne le réestime pas.
        by = (estimate_by or actor or "") if estimate_minutes else None
        with self.db.transaction() as tx:
            if footprint_unknown:
                # #4818 (B1 de cx3) : comme `backlog_add`, le verrou de la
                # garde avant celui du panier et la ligne du lot
                tx.execute(_DEPENDENCIES_LOCK)
            # #2584 : un seul écrivain par clé de panier — deux ajouts
            # simultanés n'ouvrent jamais deux paniers du même chantier.
            tx.query("SELECT pg_advisory_xact_lock(hashtextextended("
                     "current_schema() || ':backlog-basket:' || %s, 0))", (key,))
            # Une prise concurrente (`assign_capped`) attend le verrou de
            # ligne ; gagnée avant nous, la ligne ne vérifie plus le filtre :
            # le panier pris reste fermé aux lignes. La clé seule fait le
            # panier : l'équipe d'une ligne n'en ouvre pas un second.
            rows = tx.query(
                "SELECT id FROM work_items"
                f" WHERE type = 'improvement' AND source = %s AND state = '{lifecycle.REQUESTED}'"
                "   AND assignee IS NULL"
                " ORDER BY id LIMIT 1 FOR NO KEY UPDATE", (key,))
            opened = not rows
            # #4117 : la garde d'estimation est lue ICI, sous le verrou du
            # panier : une ligne ajoutée à un panier déjà ouvert ne crée aucun
            # lot et n'a pas à porter d'estimation (P2 de la relecture cx2).
            if opened and estimate_required and not estimate_minutes:
                raise interface.BasketEstimateRequired(
                    "opening a basket requires an estimate or a published reference")
            if opened:
                rows = tx.query(
                    "INSERT INTO work_items"
                    "    (type, source, title, body, state, expected_value, value_score,"
                    "     priority, team, required_capabilities, package_id, package_parent,"
                    "     app, estimate_minutes, estimate_source, estimate_by, estimate_at,"
                    "     change_size)"
                    " VALUES ('improvement', %s, %s, %s, 'intake', %s, %s, %s, %s,"
                    "         %s::text[], %s, %s, %s, %s, %s, %s,"
                    "         CASE WHEN %s THEN now() END, %s)"
                    " RETURNING id",
                    (key, title, line, expected_value, int(value_score), int(priority),
                     team, caps, package_id, package_parent, app or "", estimate_minutes,
                     estimate_source if estimate_minutes else None, by,
                     bool(estimate_minutes), change_size or None))
            else:
                tx.query(
                    "UPDATE work_items"
                    "   SET body = CASE WHEN btrim(coalesce(body, '')) = '' THEN %s"
                    "                   ELSE body || %s END,"
                    "       value_score = greatest(coalesce(value_score, 0), %s),"
                    "       priority = least(coalesce(priority, %s), %s),"
                    "       required_capabilities = (SELECT array_agg(DISTINCT c ORDER BY c)"
                    "            FROM unnest(coalesce(required_capabilities, '{}'::text[])"
                    "                        || coalesce(%s::text[], '{}'::text[])) AS c),"
                    "       team = coalesce(team, %s),"
                    "       updated_at = now()"
                    " WHERE id = %s RETURNING id",
                    (line, "\n" + line, int(value_score), int(priority), int(priority),
                     caps, team, int(rows[0]["id"])))
            item_id = int(rows[0]["id"])
            if opened and estimate_minutes:
                WorkItems(tx)._estimate_row(item_id, estimate_minutes, estimate_source,
                                            estimate_by or actor)
            WorkItems(tx)._event(item_id, "intake", note, actor)
            # #4186 : l'union des chemins des lignes du panier, jamais un
            # remplacement — une ligne ajoutée à un panier ouvert l'élargit ;
            # une ligne inconnue sous `repository` y ajoute le marqueur.
            policy = {"id": item_id}
            footprint = _unknown_marker(tx, policy, footprint, footprint_unknown)
            WorkItems(tx)._intake_footprint(item_id, footprint, actor)
            rows = tx.query(
                "SELECT " + item_columns(self.db) + ","
                "       (SELECT count(*) FROM work_item_events e"
                "         WHERE e.work_item_id = work_items.id"
                "           AND starts_with(e.note, %s))::bigint AS basket_lines"
                "  FROM work_items WHERE id = %s",
                (interface.BASKET_LINE_NOTE, item_id))
            item = dict(rows[0])
            item["basket_lines"] = int(item.get("basket_lines") or 0)
            item["basket_opened"] = opened
            if "footprint_policy" in policy:
                item["footprint_policy"] = policy["footprint_policy"]
            return item

    def backlog(self, *, open_only, limit, ready_only=False) -> list[dict]:
        # §4 : un élément `build` RENDU à la file (limite d'en-cours) reste
        # prenable, comme un lot de l'équipe ; un `build` en cours, non.
        params: tuple = ()
        if open_only:
            where = (f"(state = '{lifecycle.REQUESTED}' OR (state = '{lifecycle.IN_PROGRESS}' AND "
                     + requeued_sql("work_items") + ")) AND assignee IS NULL")
            params = REQUEUED_PARAMS
        else:
            where = "state NOT IN %s" % _CLOSED
        if ready_only:
            # #3296 : le début prévu (jour local du processus, comme
            # `lot_takeable` et `_pending`) — un élément à venir attend sa date.
            where += " AND (planned_start IS NULL OR planned_start <= %s)"
            where += (" AND NOT EXISTS (SELECT 1 FROM work_item_dependencies d"
                      "                      JOIN work_items b ON b.id = d.blocker_id"
                      "                     WHERE d.work_item_id = work_items.id"
                      "                       AND b.state NOT IN %s)" % _CLOSED)
            params += (_dt.date.today().isoformat(),)
        sql = ("SELECT " + item_columns(self.db) + " FROM work_items"
               " WHERE type = 'improvement' AND " + where
               + " ORDER BY coalesce(priority, 2), value_score DESC NULLS LAST, id"
               " LIMIT %s")
        return self.db.query(sql, params + (int(limit),))

    def auto_takes_since(self, seconds, note_prefix) -> int:
        rows = self.db.query(
            "SELECT count(*)::bigint AS n FROM work_item_events"
            " WHERE created_at > now() - make_interval(secs => %s)"
            "   AND starts_with(note, %s)", (float(seconds), note_prefix))
        return int(rows[0]["n"]) if rows else 0

    def project_plan_rows(self, project, *, assignment_id=None) -> list[dict]:
        """#4174: complete open project snapshot, including every waiting lot."""
        return self.db.query(
            "WITH pending_decisions AS MATERIALIZED ("
            " SELECT DISTINCT btrim(work_item_id) AS lot FROM agent_mailbox"
            " WHERE delivered_at IS NULL AND kind = 'request'"
            " AND payload->'decision' IS NOT NULL) "
            "SELECT w.*, extract(epoch FROM w.created_at)::float8 AS created_ts,"
            "       extract(epoch FROM w.wake_at)::float8 AS wake_ts,"
            "       to_char(w.planned_start, 'YYYY-MM-DD') AS planned_start,"
            "       to_char(w.planned_end, 'YYYY-MM-DD') AS planned_end,"
            "       to_char(w.planned_delivery, 'YYYY-MM-DD') AS planned_delivery,"
            "       CASE WHEN w.state = 'build' THEN " + requeued_sql("w") +
            " ELSE false END AS returned_to_queue,"
            "       coalesce(nullif(btrim(w.team), ''), nullif(btrim(k.team), ''),"
            "                nullif(btrim(kp.team), ''), nullif(btrim(w.app), '')) AS lot_team,"
            "       ARRAY(SELECT d.blocker_id FROM work_item_dependencies d"
            "             JOIN work_items b ON b.id = d.blocker_id"
            "             WHERE d.work_item_id = w.id AND NOT " + _not_blocking("b") +
            "             ORDER BY d.blocker_id) AS blocking_dependencies,"
            "       ARRAY(SELECT b.id FROM work_item_links l"
            "             JOIN work_items b ON b.id = CASE WHEN l.work_item_id = w.id"
            "                  THEN l.other_id ELSE l.work_item_id END"
            "             WHERE (l.work_item_id = w.id OR l.other_id = w.id)"
            "               AND l.kind = 'conflicts_with' AND b.state NOT IN " + _CLOSED +
            "             ORDER BY b.id) AS open_conflicts,"
            "       (pd.lot IS NOT NULL) AS pending_decision,"
            # Match the intake footprint guard used by dispatch (#4173).
            "       (w.state = 'intake' AND " + footprint_overlap_sql("w") + ")"
            "         AS footprint_overlap,"
            # #4818 : plafond de lots en vol du dépôt, et ce qu'il faut au plan
            # pour choisir un ensemble aux empreintes disjointes deux à deux
            "       (w.state = 'intake' AND w.assignee IS NULL AND "
            + footprint_cap_sql("w") + ") AS repository_cap,"
            "       ARRAY(SELECT DISTINCT f.path FROM work_item_footprints f"
            "             WHERE f.work_item_id = w.id ORDER BY f.path) AS footprint_paths,"
            "       CASE WHEN w.state = 'intake' THEN " + footprint_in_flight_sql("w") +
            " END AS repository_in_flight,"
            "       EXISTS (SELECT 1 FROM commitments c WHERE c.work_item_id = w.id"
            "               AND c.kind = 'decision' AND c.status IN ('proposed', 'open'))"
            "         AS decision_milestone"
            " FROM work_items w LEFT JOIN work_packages k ON k.id = w.package_id"
            " LEFT JOIN work_packages kp ON kp.id = w.package_parent"
            " LEFT JOIN pending_decisions pd ON pd.lot = w.id::text"
            " WHERE coalesce(nullif(btrim(w.app), ''), nullif(btrim(w.team), ''),"
            "                nullif(btrim(k.team), ''), nullif(btrim(kp.team), ''), '') = %s"
            " AND w.state NOT IN " + _CLOSED + " AND w.type <> 'improvement'"
            + (" AND (w.id = %s OR (w.type = 'bug' AND w.assignee IS NULL"
               " AND w.state IN ('intake', 'build')))" if assignment_id is not None else "") +
            " ORDER BY w.id", REQUEUED_PARAMS + (str(project),) +
            ((int(assignment_id),) if assignment_id is not None else ()))

    def unassigned_lots(self, limit) -> list[dict]:
        # #2252 : seuls les lots dont TOUTES les dépendances sont fusionnées
        # (`merged`/`promoted`/`closed`) sont prenables. La règle est celle de
        # `work show` (`depends … blocking`) et de « planned: waiting on lots » :
        # une dépendance en `qa`, même jugée, n'offre pas de base stable.
        sql = ("SELECT w.*,"
               f"       (w.state = '{lifecycle.IN_PROGRESS}' AND " + requeued_sql("w") + ") AS returned_to_queue,"
               "       coalesce(nullif(btrim(w.team), ''), nullif(btrim(k.team), ''),"
               "                nullif(btrim(kp.team), ''), nullif(btrim(w.app), '')) AS lot_team,"
               "       EXISTS (SELECT 1 FROM agent_mailbox b"
               "                WHERE b.delivered_at IS NULL AND b.kind = 'request'"
               "                  AND (b.payload -> 'decision') IS NOT NULL"
               "                  AND btrim(b.work_item_id) = w.id::text) AS pending_decision,"
               "       EXISTS (SELECT 1 FROM commitments c"
               "                WHERE c.work_item_id = w.id AND c.kind = 'decision'"
               "                  AND c.status IN ('proposed', 'open')) AS decision_milestone"
               "  FROM (SELECT " + item_columns(self.db) + ","
               "               to_char(planned_start, 'YYYY-MM-DD') AS planned_start,"
               "               to_char(planned_end, 'YYYY-MM-DD') AS planned_end,"
               "               to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery"
               "          FROM work_items"
               f"         WHERE (state = '{lifecycle.REQUESTED}' OR (state = '{lifecycle.IN_PROGRESS}' AND "
               + requeued_sql("work_items") + "))"
               "           AND assignee IS NULL"
               # #3447 : un lot réservé à un exécutant externe n'est pas
               # prenable (le filtre précède le LIMIT, comme les dépendances).
               "           AND to_jsonb(work_items)->>'external_executor' IS NULL"
               "           AND type <> 'improvement'"
               "           AND NOT EXISTS (SELECT 1 FROM work_item_dependencies d"
               "                             JOIN work_items b ON b.id = d.blocker_id"
               "                            WHERE d.work_item_id = work_items.id"
               "                              AND NOT " + _not_blocking("b") + ")"
               # #4173 : un lot `intake` dont l'empreinte chevauche celle d'un
               # lot occupant du même dépôt attend sa fusion (un lot `build`
               # rendu à la file occupe déjà la sienne) ; même prédicat que la
               # garde de `_assign_locked`, avant le LIMIT.
               # #4818 : et le plafond de lots en vol de son dépôt (`held_sql`).
               f"           AND (state <> '{lifecycle.REQUESTED}' OR NOT "
               + footprint_held_sql("work_items") + ")) w"
               "  LEFT JOIN work_packages k ON k.id = w.package_id"
               "  LEFT JOIN work_packages kp ON kp.id = w.package_parent"
               f" ORDER BY coalesce(w.priority, 2), (w.state = '{lifecycle.IN_PROGRESS}') DESC,"
               "          w.planned_start NULLS LAST,"
               "          w.planned_end NULLS LAST, w.planned_delivery NULLS LAST,"
               "          w.created_ts, w.id"
               " LIMIT %s")
        # les deux fragments attendent les mêmes paramètres, dans l'ordre du texte
        return self.db.query(sql, REQUEUED_PARAMS + REQUEUED_PARAMS +
                             (None if limit is None else int(limit),))

    def review_proposals(self, limit, *, proposal_prefix, window_s=21600.0) -> list[dict]:
        """#1677 (c) : les lots en `qa` qui attendent un geste de relecture —
        sans verdict COURANT (une relecture est due) ou à verdict courant
        bloquant (une correction est due) — avec leur auteur, leur branche,
        leur tête gelée, leur équipe résolue (`lot_team`, même règle que
        `unassigned_lots`) et les colonnes du lot dont la prise a besoin
        (`required_capabilities`, `type`, …).

        La prise y apparie un agent au repos d'une autre famille que l'auteur
        (canon §2). Un lot déjà muni d'un relecteur désigné (0421) a un
        destinataire : il n'est pas proposé. Un lot sans tête gelée n'a rien à
        relire. Un lot dont la proposition date de moins de `window_s` (note
        `proposal_prefix…` au journal) est écarté : deux passages ne proposent
        pas le même lot deux fois. B1 : le verdict lu est le verdict courant —
        un `ok` portant sur une tête regelée depuis ne dispense plus de la
        relecture."""
        return self._proposal_rows(limit, proposal_prefix, window_s)

    def _proposal_rows(self, limit, proposal_prefix, window_s, item_id=None):
        """`review_proposals`, restreint à un lot si `item_id` (#1738 R1 B1 :
        la prise revalide le même prédicat sous son verrou)."""
        only = "   AND w.id = %d" % int(item_id) if item_id is not None else ""
        reviewer_guard = "   AND coalesce(w.reviewer, '') = ''" if has_reviewer(self.db) else ""
        frozen_sha = _last_freeze_sha("w")
        current = _current_verdict("w")
        return self.db.query(
            "SELECT w.*,"
            "       coalesce(nullif(btrim(w.team), ''), nullif(btrim(k.team), ''),"
            "                nullif(btrim(kp.team), ''), nullif(btrim(w.app), '')) AS lot_team,"
            "       " + frozen_sha + " AS frozen_sha,"
            "       " + current + " AS last_verdict"
            "  FROM work_items w"
            "  LEFT JOIN work_packages k ON k.id = w.package_id"
            "  LEFT JOIN work_packages kp ON kp.id = w.package_parent"
            f" WHERE w.state = '{lifecycle.QA}'" + only +
            "   AND w.assignee IS NOT NULL AND btrim(w.assignee) <> ''"
            "   AND left(w.assignee, 6) <> 'human:'"
            "   AND " + frozen_sha + " IS NOT NULL"
            + reviewer_guard +
            "   AND coalesce(" + current + ", '') <> 'ok'"
            "   AND NOT EXISTS (SELECT 1 FROM work_item_events e"
            "                    WHERE e.work_item_id = w.id"
            "                      AND starts_with(e.note, %s)"
            "                      AND e.created_at > now() - make_interval(secs => %s))"
            " ORDER BY coalesce(w.priority, 2), w.id"
            " LIMIT %s",
            (str(proposal_prefix), float(window_s), int(limit)))

    def claim_review_proposal(self, item_id, *, agent, kind, proposal_prefix,
                              window_s, actor) -> bool:
        """#1738 (N5/N7) : voir l'interface."""
        item = int(item_id)
        with self.db.transaction() as tx:
            # Un verrou consultatif par lot : deux passages (hôtes distincts)
            # sérialisent ici, et le second relit la fenêtre APRÈS le premier.
            tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                     ("review proposal:%d" % item,))
            # Les jalons et la désignation du pilote prennent ce verrou de
            # ligne, pas le verrou consultatif. Le prédicat est relu APRÈS
            # l'attente : il voit les verdicts/gels validés entre-temps, et
            # aucun ne peut s'insérer entre cette lecture et la proposition.
            if not tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                            (item,)):
                return False
            # #1738 R1 (B1) : sous le verrou, le lot est relu avec le prédicat
            # de `review_proposals` (qa, gel, relecteur vide, verdict courant,
            # fenêtre) et le `kind` doit être celui qu'il donne aujourd'hui.
            me = WorkItems(tx)
            rows = me._proposal_rows(1, proposal_prefix, window_s, item_id=item)
            if not rows or (rows[0].get("assignee") or "").strip() == agent:
                return False
            if kind != ("correction" if rows[0].get("last_verdict") == "blocked"
                        else "review"):
                return False
            if kind == "review" and has_reviewer(tx):
                # N7 : une relecture désigne son relecteur (#705) — le lot sort
                # des propositions suivantes et `stale_lot` s'adresse à lui.
                # B1 : compare-and-set, le pilote ne prend pas ce verrou.
                if me._designate_reviewer(item, agent, actor, vacant=True) is None:
                    return False
            me._event(item, "qa",
                      "%s: lot #%d to %s (%s)" % (proposal_prefix, item, agent, kind),
                      actor)
            tx.execute("UPDATE work_items SET updated_at = now() WHERE id = %s", (item,))
            return True

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
            f"         WHERE assignee = %%s AND state IN ('{lifecycle.REQUESTED}', '{lifecycle.IN_PROGRESS}')"
            "           AND type <> 'improvement') w"
            "  LEFT JOIN work_packages k ON k.id = w.package_id"
            "  LEFT JOIN work_packages kp ON kp.id = w.package_parent"
            " ORDER BY coalesce(w.priority, 2), w.planned_start NULLS LAST,"
            "          w.planned_end NULLS LAST, w.planned_delivery NULLS LAST,"
            "          w.created_ts, w.id"
            " LIMIT %%s" % item_columns(self.db), (assignee, int(limit)))

    # -- issues GitHub des lots (L126) ------------------------------------------
    def issue_feed(self, limit) -> list[dict]:
        """Lots ouverts, porteurs d'une `issue_ref`, ou pinnés sur une révision
        Process (#2130/D : un cœur `merged` reste projeté ouvert, il doit donc
        être dans le flux), les plus récents d'abord pour la borne, rendus par
        id croissant avec l'équipe de leur fiche et l'équipe, le chantier et
        l'hôte de leur assigné.

        #2130/D : `workflow_ref`, `workflow_stage` et `workflow_version` sont
        portés par la lecture comme B5 les porte dans `work.get` (`to_jsonb`,
        sans sonde de colonne) ; base pré-0452 : `''`, NULL, 0.
        """
        # #3741 : `to_jsonb(work_items)` une fois par ligne (`j.doc`), non
        # cinq — sur les ~3 500 lignes du flux, ses cinq appels coûtaient 17 s
        # sous charge et faisaient expirer la synchro GitHub (27 s mesurées,
        # contre 30 s de statement_timeout). `OFFSET 0` empêche le planificateur
        # de remonter la sous-requête latérale, ce qui recopierait l'appel à
        # chaque référence.
        return self.db.query(
            "SELECT w.*, k.team AS package_team, r.team AS assignee_team,"
            "       r.chantier AS assignee_chantier, r.host AS assignee_host,"
            # L157 : dates prévues et fusion, pour l'en-tête de l'issue
            "       (SELECT extract(epoch from max(m.at))::float8 FROM work_item_milestones m"
            "         WHERE m.work_item_id = w.id AND m.kind = 'merged'"
            "           AND " + _effective_merged("m") + ") AS merged_ts"
            "  FROM (SELECT %s,"
            "               coalesce(j.doc->>'workflow_ref', '') AS workflow_ref,"
            "               j.doc->>'workflow_stage' AS workflow_stage,"
            "               coalesce((j.doc->>'workflow_version')::bigint, 0)"
            "                 AS workflow_version,"
            "               to_char(planned_start, 'YYYY-MM-DD') AS planned_start,"
            "               to_char(planned_end, 'YYYY-MM-DD') AS planned_end,"
            "               to_char(planned_delivery, 'YYYY-MM-DD') AS planned_delivery"
            "          FROM work_items"
            "          CROSS JOIN LATERAL (SELECT to_jsonb(work_items) AS doc OFFSET 0) j"
            "         WHERE state NOT IN %s OR coalesce(btrim(issue_ref), '') <> ''"
            "            OR coalesce(j.doc->>'workflow_ref', '') <> ''"
            "         ORDER BY id DESC LIMIT %%s) w"
            "  LEFT JOIN work_packages k ON k.id = w.package_id"
            "  LEFT JOIN agent_registry r ON r.name = w.assignee"
            " ORDER BY w.id" % (item_columns(self.db, row_json="j.doc"), _CLOSED),
            (int(limit),))

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

    def _intake_footprint(self, item_id, paths, actor) -> list[str]:
        """#4186 : écrit les chemins d'empreinte `intake` absents (union), avec
        leur ligne de journal — le geste commun de `add` et de la file
        d'amélioration. Rend les chemins ajoutés (vide : rien à changer).
        Le verrou du graphe est pris par les appelants qui gardent une création
        attribuée ; ces lots naissent libres."""
        wanted = sorted({str(path) for path in (paths or ()) if str(path).strip()})
        if not wanted:
            return []
        have = {row["path"] for row in self.db.query(
            "SELECT path FROM work_item_footprints"
            " WHERE work_item_id = %s AND source = 'intake'", (int(item_id),))}
        missing = [path for path in wanted if path not in have]
        for path in missing:
            self.db.execute(
                "INSERT INTO work_item_footprints"
                "(work_item_id, source, path, created_by)"
                " VALUES (%s, 'intake', %s, %s) ON CONFLICT DO NOTHING",
                (int(item_id), path, actor or ""))
        if missing:
            self._event(int(item_id), "intake", "Footprint (intake): %s"
                        % ", ".join(sorted(have | set(missing))), actor)
        return missing

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
            # #2317 (N6) : la fin reste la PREMIÈRE livraison — la fusion,
            # sinon la livraison hors cible —, mais on ignore les jalons
            # `merged` annulés par une correction. Le max de #1693 faisait
            # glisser la fin d'un lot historique au jalon `delivered` le plus
            # récent et faussait la table de référence.
            "  JOIN LATERAL (SELECT min(m.at) AS at FROM work_item_milestones m"
            "                 WHERE m.work_item_id = w.id"
            "                   AND m.kind IN ('merged', 'delivered')"
            "                   AND (m.kind <> 'merged' OR "
            + _effective_merged("m") + ")) fus"
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
            f" WHERE (w.state <> '{lifecycle.CLOSED}' OR w.close_reason = 'delivered')")
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
            "                   AND " + _effective_merged("x") +
            "                 ORDER BY x.at DESC, x.id DESC LIMIT 1) fus ON true"
            "  LEFT JOIN work_item_measures m ON m.work_item_id = w.id"
            f" WHERE w.started_at IS NOT NULL AND w.state <> '{lifecycle.CLOSED}'")
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
            # a measure that could not read a size keeps the stored one, unless
            # it doubts for good (`size_doubt`, #3056): the old size is then
            # cleared, not shown as measured
            + ", ".join(
                "  %s = %s" % (c, "NULL" if m.get("size_doubt") else
                               "coalesce(excluded.%s, work_item_measures.%s)" % (c, c))
                for c in ("diff_files", "diff_insertions", "diff_deletions",
                          "measured_size")) + ","
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
        # Historical journals and signed receipts retain their stage vocabulary.
        # A note on a delivered core must not erase the merge/promotion phase
        # from the previous-event checks used for merge annulment and SLOs.
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor) "
            "SELECT id, CASE WHEN %s = 'delivered' THEN "
            "coalesce(to_jsonb(work_items)->>'legacy_state', state) ELSE %s END, %s, %s "
            "FROM work_items WHERE id = %s",
            (state, state, note or "", actor or "", item_id),
        )
