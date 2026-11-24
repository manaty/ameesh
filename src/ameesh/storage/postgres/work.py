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
from ...db import DbError, in_transaction, is_unique_violation, prerequisite_query
from ...slo import VEHICLE_EXIT_MARK
from .. import interface
from . import catalog as _catalog
from .work_dev import DevWork
from .provenance import Provenance as _DevProvenance
from .external_executors import require_active as require_external_executor_active
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
    __UNIQUE__,
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

#: #7940 : l'instant lisible d'un hold qui ne retient plus le démarrage.
def _hold_iso(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return "?"

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

    P1b #7650 : la table appartient au module `dev` ; la sonde vit avec son
    stockage (`DevWork.slots_available`), le noyau garde l'API et le repli sur
    le champ scalaire quand la table manque."""
    return DevWork(db).slots_available()


#: #5404 : les colonnes optionnelles lues par `item_columns` et `work.get`.
_OPTIONAL_ITEM_COLUMNS = ("reviewer", "reviewers", "external_executor", "unique_ref",
                          "wake_at", "legacy_state",
                          "external_session", "external_claim_session",
                          "external_claimed_at", "external_claim_actor",
                          "external_claim_host", "external_claim_proof",
                          "external_claim_harness", "external_claim_binding",
                          "workflow_ref", "workflow_stage", "workflow_version")


def has_current_item_columns(db) -> bool:
    """#5404 : toutes les colonnes optionnelles existent-elles ? Même règle que
    `has_reviewer` : seule la réponse positive est mise en cache, une migration
    appliquée pendant qu'une connexion vit est donc vue à la lecture suivante."""
    if getattr(db, "_has_current_item_columns", False):
        return True
    found = prerequisite_query(db,
        "SELECT count(*) AS n FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = 'work_items'"
        " AND column_name IN (%s)" % ", ".join("'%s'" % c for c in _OPTIONAL_ITEM_COLUMNS))
    current = bool(found) and found[0].get("n") == len(_OPTIONAL_ITEM_COLUMNS)
    if current:
        try:
            db._has_current_item_columns = True
        except Exception:
            pass
    return current


def legacy_state_column(db, alias: str) -> str:
    """Read the delivery phase without serialising a current work_items row."""
    return (f"{alias}.legacy_state" if has_current_item_columns(db)
            else f"to_jsonb({alias})->>'legacy_state'")


def item_columns(db, ref: str | None = None, migrations: str | None = None) -> str:
    """Les colonnes d'un lot ; `reviewer` quand 0421 est appliquée, `''` sinon
    (les lectures d'avant la migration ne cassent pas, #705).

    `ref` (le nom de la table dans la requête, « work_items ») qualifie les
    colonnes, sans sonde de colonne : les commandes à budget d'allers-retours
    (L61 : `work list`, `progress`) n'en paient pas un de plus, et exigent le
    schéma courant.

    #2481 : `migrations` est la liste des créneaux réservés du lot (JSONB),
    lue dans `work_migration_reservations` par une sous-requête corrélée —
    une seule requête, pas d'aller-retour par lot. La liste n'est lue que sur
    la ligne complète (sans `ref`) : le chemin `ref` est celui des lectures à
    budget L61, et la sonde de table y coûterait justement l'aller-retour
    qu'il économise. Ces vues rendent `[]` et se replient sur le scalaire
    (`work.migration_line`), jamais sur une erreur : sur une base d'avant
    0419, la liste est de toute façon vide.

    #5583 : `migrations` fournit l'expression de la colonne à la place de la
    sous-requête corrélée — un agrégat borné que l'appelant joint. Le flux
    GitHub (5 000 lots) ne sonde plus la table des créneaux une fois par
    ligne ; les lectures d'une ligne gardent le comportement par défaut.

    #5404 : les colonnes se lisent directement. `to_jsonb(ligne)` coûtait plus
    d'une milliseconde par ligne (corps compris, quatre fois par ligne) : la
    moitié d'`ameesh alerts`. Il ne reste que le repli sans `ref` d'une base en
    retard sur le schéma (fenêtre de migration, #2339), derrière une sonde mise
    en cache. `to_jsonb(colonne)` garde la forme JSON de `reviewers`.

    #6090 : `unique_ref` est la clé durable de `work add --unique-ref` ; une
    base d'avant 0555 la rend NULL par le repli `to_jsonb`, sans erreur."""
    col = "%s." % ref if ref else ""
    if ref:
        reviewer = "coalesce(%sreviewer, '') AS reviewer" % col
    elif has_reviewer(db):
        reviewer = "reviewer"
    else:
        reviewer = "''::text AS reviewer"
    if ref or has_current_item_columns(db):
        reviewers = "to_jsonb(%sreviewers) AS reviewers" % col
        external = ("%sexternal_executor AS external_executor, "
                    "%sexternal_session AS external_session, "
                    "%sexternal_claim_session AS external_claim_session, "
                    "%sexternal_claim_actor AS external_claim_actor, "
                    "%sexternal_claim_host AS external_claim_host, "
                    "%sexternal_claim_proof AS external_claim_proof, "
                    "%sexternal_claim_harness AS external_claim_harness, "
                    "%sexternal_claim_binding AS external_claim_binding, "
                    "extract(epoch from %sexternal_claimed_at)::float8"
                    " AS external_claimed_ts" % ((col,) * 9))
        unique_ref = "%sunique_ref AS unique_ref" % col
        wake = "extract(epoch from %swake_at)::float8 as wake_ts" % col
        lifecycle_columns = ("%slegacy_state AS legacy_state, "
                             "%sworkflow_stage AS workflow_stage" % (col, col))
    else:
        reviewers = "to_jsonb(work_items)->'reviewers' AS reviewers"
        external = ("to_jsonb(work_items)->>'external_executor' AS external_executor, "
                    "to_jsonb(work_items)->>'external_session' AS external_session, "
                    "to_jsonb(work_items)->>'external_claim_session'"
                    " AS external_claim_session, "
                    "to_jsonb(work_items)->>'external_claim_actor'"
                    " AS external_claim_actor, "
                    "to_jsonb(work_items)->>'external_claim_host'"
                    " AS external_claim_host, "
                    "to_jsonb(work_items)->>'external_claim_proof'"
                    " AS external_claim_proof, "
                    "to_jsonb(work_items)->>'external_claim_harness'"
                    " AS external_claim_harness, "
                    "nullif(to_jsonb(work_items)->>'external_claim_binding', '')"
                    "::bigint AS external_claim_binding, "
                    "extract(epoch from (to_jsonb(work_items)->>'external_claimed_at')"
                    "::timestamptz)::float8 AS external_claimed_ts")
        unique_ref = "to_jsonb(work_items)->>'unique_ref' AS unique_ref"
        wake = ("extract(epoch from (to_jsonb(work_items)->>'wake_at')::timestamptz)::float8"
                " as wake_ts")
        lifecycle_columns = ("to_jsonb(work_items)->>'legacy_state' AS legacy_state, "
                             "to_jsonb(work_items)->>'workflow_stage' AS workflow_stage")
    if ref:
        migrations_column = "'[]'::jsonb AS migrations"
    elif migrations is not None:
        migrations_column = migrations
    else:
        # P1b #7650 : l'expression vit avec le stockage du module `dev`.
        migrations_column = DevWork(db).migrations_column()
    return (_COLUMNS_TEMPLATE
            .replace("__REVIEWER__", reviewer + ", " + reviewers)
            .replace("__EXTERNAL__", external)
            .replace("__UNIQUE__", unique_ref)
            .replace("__LIFECYCLE__", lifecycle_columns)
            .replace("__WAKE__", wake)
            .replace("__MIGRATIONS__", migrations_column))


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
        # Le SQL des politiques nomme des tables du module : port `DevWork`
        # sur la transaction de création (revue B de #7650).
        item["footprint_policy"] = DevWork(tx).unknown_policy(item["id"])
        if not paths and item["footprint_policy"] == "repository":
            paths = ["."]
    return paths




class WorkItems(interface.WorkItems):

    def __init__(self, db):
        super().__init__(db)
        # P1b #7650 : le stockage des tables dev est porté par le module ;
        # ces délégués gardent l'API historique (`storage.of(db).work`).
        self._dev = DevWork(db)

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
            parent_lot=None, unique_execution=False, unique_ref=None,
            external_executor=None, external_session=None,
            footprint=None, footprint_unknown=False) -> dict:
        """Crée le lot en `intake`, puis sa première ligne de journal ; L157 :
        avec sa durée estimée (et sa ligne d'historique), en UNE transaction.
        #540 : `queued_note` (voir l'interface).
        L539 : `mark_note` marque `mark_item_id` dans la **même** transaction,
        si bien qu'un échec de la marque annule la création (et l'inverse).
        La ligne marquée est verrouillée **avant** l'insertion et la
        `issue_ref` est relue sous ce verrou : deux appels concurrents ne
        peuvent pas créer deux lots pour la même référence (revue #544 r2).

        #2954 : `unique_execution` prend d'abord un verrou consultatif de
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

        #6090 : `unique_ref` est la clé d'idempotence durable fournie par
        l'appelant (`work add --unique-ref`). Sous un verrou consultatif
        propre à la clé, le lot qui la porte déjà est relu **quel que soit son
        état** et rendu tel quel (marqué `existing`) : le second appel ne crée
        rien et ne lève pas. Le premier créateur gagne ; la clé est globale au
        mesh et comparée exactement (opaque, à la casse près). L'index unique
        partiel de 0555 est la barrière de dernier recours, le verrou la
        sérialisation.

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
            if unique_ref:
                # #6090 : la clé durable est relue sous son verrou avant toute
                # écriture. Le second appelant attend le commit du premier,
                # puis rend son lot au lieu d'en créer un ; un lot fermé garde
                # sa clé, comme l'identité d'exécution de #2954.
                tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                         ("work-add-unique-ref:" + unique_ref,))
                existing = tx.query(
                    "SELECT %s FROM work_items WHERE unique_ref = %%s"
                    % item_columns(self.db), (unique_ref,))
                if existing:
                    return dict(existing[0], existing=True)
            if external_executor:
                # #6091 : un `system:…` doit être encore déclaré actif SOUS le
                # verrou de sa ligne, tenu jusqu'à l'écriture du lot. Un
                # `retire` concurrent (UPDATE de la même ligne) attend ce
                # commit s'il arrive après ; s'il a déjà commité, la
                # déclaration n'est plus active et la création est refusée.
                require_external_executor_active(tx, external_executor)
            if unique_execution and str(issue_ref or "").strip():
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
            if unique_execution and str(issue_ref or "").strip():
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
                      if queued_note is not None and assignee and type != "follow-up" else None)
            if behind is not None:
                assignee, note = ((assignee, forced_note.format(lot=behind)) if forced_note
                                  else (None, queued_note.format(lot=behind)))
            rows = tx.query(
                """
                INSERT INTO work_items
                    (type, source, app, title, body, issue_ref, workstream, assignee,
                     budget_usd, state, package_id, package_parent, branch, branch_target,
                     priority, estimate_minutes, estimate_source, estimate_by, estimate_at,
                     change_size, external_executor, external_session, unique_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'intake', %s, %s, %s, %s, %s,
                        %s, %s, %s, CASE WHEN %s THEN now() END, %s, %s, %s, %s)
                RETURNING __COLUMNS__
                """.replace("__COLUMNS__", item_columns(self.db)),
                (type, source, app, title, body, issue_ref, workstream, assignee,
                 budget_usd, package_id, package_parent, branch, branch_target,
                 None if priority is None else int(priority),
                 estimate_minutes, estimate_source if estimate_minutes else None,
                 (actor or "") if estimate_minutes else None, bool(estimate_minutes),
                 change_size or None, external_executor or None,
                 external_session or None, unique_ref or None),
            )
            item = rows[0]
            if estimate_minutes:
                WorkItems(tx)._estimate_row(int(item["id"]), estimate_minutes,
                                            estimate_source, actor)
            WorkItems(tx)._event(int(item["id"]), "intake", note, actor)
            if footprint_unknown:
                # Port du module sur la transaction de création (revue B de
                # #7650) : le SQL des politiques n'est pas qualifié par le noyau.
                item["footprint_policy"] = self._dev.unknown_policy(
                    item["id"], tx)
                if not footprint and item["footprint_policy"] == "repository":
                    footprint = ["."]
            if footprint:
                WorkItems(tx)._intake_footprint(item["id"], footprint, actor)
                if assignee:
                    exempted = WorkItems(tx)._footprint_guard(item["id"])
                    if exempted:
                        WorkItems(tx)._warn_human_wait_overlap(
                            item["id"], "intake", exempted, actor)
            elif assignee and type != "follow-up":
                # #4818 : sans empreinte, le plafond de lots en vol du dépôt
                # garde encore la création attribuée
                exempted = WorkItems(tx)._footprint_guard(item["id"])
                if exempted:
                    WorkItems(tx)._warn_human_wait_overlap(
                        item["id"], "intake", exempted, actor)
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

    def _workflow_columns(self) -> str:
        # #1922/B5 : `workflow_ref` (0452) est porté par la lecture, comme
        # `reviewer` (#705) l'est par `item_columns` — `to_jsonb` rend '' sur
        # une base d'avant la migration. Les commandes historiques le lisent
        # par cette interface : un SELECT brut serait refusé sur une connexion
        # d'exécuteur médié (`work move`/`note`/`close` d'une VM).
        # #2130/D : `workflow_stage` et `workflow_version` sont portés par la
        # même lecture — `project_lots` y passe pour les lots anciens/orphelins
        # retrouvés par leur marqueur, et la projection a besoin des trois
        # champs (sinon `projection-stage-missing` sur un lot sain). Une base
        # pré-0452 rend '', NULL, 0, derrière la sonde #5404.
        if has_current_item_columns(self.db):
            return ("coalesce(workflow_ref, '') AS workflow_ref,"
                    " workflow_stage,"
                    " coalesce(workflow_version, 0) AS workflow_version")
        return ("coalesce(to_jsonb(work_items)->>'workflow_ref', '') AS workflow_ref,"
                " to_jsonb(work_items)->>'workflow_stage' AS workflow_stage,"
                " coalesce((to_jsonb(work_items)->>'workflow_version')::bigint, 0)"
                " AS workflow_version")

    def review_floor(self, item_id: int) -> dict | None:
        from ... import review_floor

        if self.get(item_id) is None:
            return None
        manual = review_floor.of_lot(self.db, item_id)
        production = review_floor.production_decision_floor(self.db, item_id)
        codes = ["manual"] if manual["floor"] else []
        if production:
            codes.append("production")
        return {"floor": max(int(manual["floor"]), 2 if production else 0),
                "codes": codes}

    def get(self, item_id) -> dict | None:
        # #5993 : la tête du dernier gel et son instant, plus le dernier
        # passage en build, sont portés par cette lecture — comme
        # `workflow_ref` l'est pour le pin. La commande `work move ... qa` les
        # lit sans second aller-retour, y compris sur une connexion
        # d'exécuteur médiée (le contrat n'a pas d'op `work.milestones`).
        # Un gel antérieur au dernier passage en build ne couvre pas la tête
        # retravaillée : c'est le cas que la garde refuse (#5993 N1).
        # Cette lecture exige SELECT sur les jalons et le journal : les
        # écrivains isolés des modules lisent par `get_brief` (#420, #5993).
        rows = self.db.query(
            "SELECT %s, %s, f.frozen_sha,"
            " extract(epoch from f.frozen_at)::float8 AS frozen_at,"
            " extract(epoch from b.build_at)::float8 AS build_at"
            " FROM work_items"
            " LEFT JOIN LATERAL ("
            "   SELECT m.sha AS frozen_sha, m.at AS frozen_at"
            "     FROM work_item_milestones m"
            "    WHERE m.work_item_id = work_items.id AND m.kind = 'frozen'"
            "    ORDER BY m.at DESC, m.id DESC LIMIT 1) f ON true"
            " LEFT JOIN LATERAL ("
            "   SELECT e.created_at AS build_at"
            "     FROM work_item_events e"
            "    WHERE e.work_item_id = work_items.id AND e.state = 'build'"
            "    ORDER BY e.id DESC LIMIT 1) b ON true"
            " WHERE work_items.id = %%s"
            % (item_columns(self.db), self._workflow_columns()),
            (int(item_id),))
        return rows[0] if rows else None

    def get_brief(self, item_id) -> dict | None:
        # #5993 : projection d'avant la garde, sans jalons ni journal. Les
        # écrivains isolés des modules (#420) n'ont que SELECT sur
        # `work_items` ; `it.item` lit le lot par ici sous `ameesh_it_qa` /
        # `ameesh_it_proof`.
        rows = self.db.query(
            "SELECT %s, %s FROM work_items WHERE id = %%s"
            % (item_columns(self.db), self._workflow_columns()),
            (int(item_id),))
        return rows[0] if rows else None

    def stacked_on(self, app, targets) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM work_items WHERE app = %%s"
            " AND branch_target = ANY(%%s::text[]) AND state NOT IN %s"
            " ORDER BY id" % (item_columns(self.db), _CLOSED),
            (app, list(targets)))

    def items(self, *, state, assignee, limit) -> list[dict]:
        # L61 : lecture de `reviewer` sans sonde, cette commande est bornée
        where = " WHERE true"
        params: list = []
        if state:
            where += (" AND coalesce(legacy_state, state) = %s"
                      if state in ("merged", "promoted") else " AND state = %s")
            params.append(state)
        if assignee:
            where += " AND assignee = %s"
            params.append(assignee)
        # #5347 : `sync-merges` lit 1 000 lots sur les 5 230 lignes du tas, et la
        # lecture dépassait le `statement_timeout` (30 s) avant même
        # `_PACKET_SELECT`. La sous-requête interne trie et coupe sans
        # expressions coûteuses ; les colonnes sont calculées sur les lignes
        # déjà bornées, sans `to_jsonb` de la ligne entière (#5404).
        sql = ("SELECT %s FROM (SELECT * FROM work_items%s"
               " ORDER BY updated_at DESC LIMIT %%s) work_items"
               " ORDER BY work_items.updated_at DESC"
               % (item_columns(self.db, "work_items"), where))
        params.append(None if limit is None else int(limit))
        return self.db.query(sql, tuple(params))

    def activity(self, assignees, *, limit=1000) -> dict[str, dict]:
        """#7327 : l'activité des lots de tous ces agents en une requête.

        Même résultat que la lecture par agent (`items`), mais un seul
        aller-retour et sans charger l'historique : par assigné, le plus récent
        `updated_at` et la projection minimale des lots non terminaux (voir
        `interface.WorkItems.activity`). `open` vaut None quand `limit` lots
        ont été lus : la liste serait tronquée.

        Comme `items`, cette lecture exige le schéma courant (colonnes
        `wake_at`/`legacy_state`) et n'utilise que des colonnes directes, sans
        `to_jsonb(work_items)` : le corps du lot n'est jamais lu.
        """
        names = sorted({str(name) for name in assignees if name})
        if not names:
            return {}
        limit = int(limit)
        done = tuple(sorted(lifecycle.COMPLETED))
        rows = self.db.query(
            "SELECT assignee,"
            " extract(epoch from max(updated_at))::float8 AS latest_updated_ts,"
            " CASE WHEN count(*) < %d THEN coalesce(jsonb_agg("
            "   jsonb_build_object("
            "     'id', id, 'state', state, 'assignee', assignee,"
            # `items` ne projette pas planned_start : la réduction doit porter
            # la même valeur (None), sinon held_actions changerait de résultat.
            "     'planned_start', NULL,"
            "     'wake_ts', extract(epoch from wake_at)::float8,"
            "     'updated_ts', extract(epoch from updated_at)::float8)"
            "   ORDER BY updated_at DESC, id DESC) FILTER (WHERE state NOT IN (%s)), '[]'::jsonb)"
            " END AS open"
            " FROM work_items WHERE assignee = ANY(%%s::text[])"
            " GROUP BY assignee"
            % (limit, ", ".join(["%s"] * len(done))),
            (*done, names))
        found = {row["assignee"]: row for row in rows}
        out = {}
        for name in names:
            row = found.get(name)
            if row is None:
                out[name] = {"latest_updated_ts": None, "open": []}
                continue
            latest = row["latest_updated_ts"]
            out[name] = {
                "latest_updated_ts": None if latest is None else float(latest),
                "open": list(row["open"]) if row["open"] is not None else None,
            }
        return out

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

        #5582 : les barrières bon marché (état, anti-jointure du lot de
        # relecture, marque terminale) sont **matérialisées** avant le jalon de
        # fusion et les compteurs. Sans cela le planificateur partait du
        # balayage des jalons `merged` depuis la borne (2 700 au central) et
        # évaluait `agent_mesh_merged_annulled` deux fois par jalon, plus un
        # parcours d'événements par lot pour la marque terminale et les
        # tentatives : 3,7 s par appel. La CTE `MATERIALIZED` est une clôture
        # d'optimisation : `since`/`annulled` et le compteur ne s'exécutent que
        # sur les candidats qui passent déjà les barrières bon marché.

        Le filtre lui-même (`_missing_ref_gates`) est partagé avec
        `missing_ref_eligible`, pour qu'un lot donné reçoive la même vérité que
        le balayage (B1 de la R1 #1104)."""
        wanted = [str(s) for s in (states or ())]
        if not wanted:
            return []
        params: list = list(wanted)
        sql = (
            "WITH candidats AS MATERIALIZED ("
            "SELECT __COLUMNS__ FROM work_items w"
            " WHERE w.state IN (__STATES__)"
            + self._missing_ref_pre_gates(
                params, ref_prefix=ref_prefix, terminal_prefix=terminal_prefix,
                since=since)
            + ") SELECT * FROM candidats w WHERE true"
            + self._missing_ref_late_gates(
                params, attempt_prefix=attempt_prefix, retry_s=retry_s,
                max_attempts=max_attempts, since=since)
            # la CTE ne rend que les colonnes de `item_columns` : `updated_ts`
            # est l'époque de `updated_at`, de même ordre exact (microseconde).
            + " ORDER BY w.updated_ts DESC, w.id DESC LIMIT %s"
        )
        params.append(int(limit))
        return self.db.query(
            sql.replace("__COLUMNS__", item_columns(self.db, "w")).replace(
                "__STATES__", ", ".join(["%s"] * len(wanted))),
            tuple(params))

    @staticmethod
    def _missing_ref_pre_gates(params: list, *, ref_prefix,
                               terminal_prefix="", since=None) -> str:
        """#5582 : les barrières bon marché de la sélection, matérialisées.

        L'anti-jointure du lot de relecture (`ref_prefix`), la marque terminale
        et l'**existence** d'un jalon `merged` depuis la borne se lisent par
        index, sans relire le journal pour l'annulation. Elles réduisent les
        ~2 700 jalons `merged` du central à la poignée de candidats encore dus.
        La marque terminale est un compteur (`= 0`) et non un `NOT EXISTS` :
        le planificateur ne peut pas la replier en anti-jointure de hachage qui
        balaierait tout `work_item_events` (garde de plan #5582) ; elle passe
        par l'index `(work_item_id, split_part(note, ' ', 1))`."""
        sql = (
            "   AND NOT EXISTS (SELECT 1 FROM work_items r"
            "                    WHERE r.issue_ref = %s || w.id::text)"
        )
        params.append(str(ref_prefix))
        if terminal_prefix:
            sql += (
                "   AND (SELECT count(*) FROM work_item_events e"
                "         WHERE e.work_item_id = w.id"
                "           AND split_part(e.note, ' ', 1) = %s || w.id::text) = 0"
            )
            params.append(str(terminal_prefix))
        if since is not None:
            sql += (
                "   AND EXISTS (SELECT 1 FROM work_item_milestones m"
                "                WHERE m.work_item_id = w.id AND m.kind = 'merged'"
                "                  AND m.at >= %s)"
            )
            params.append(since)
        return sql

    @staticmethod
    def _missing_ref_late_gates(params: list, *, attempt_prefix="", retry_s=0,
                                max_attempts=0, since=None) -> str:
        """#5582 : les barrières coûteuses, après la matérialisation.

        Le jalon de fusion effectif (`since` + `agent_mesh_merged_annulled`)
        et le compteur de tentatives ne s'évaluent que sur les candidats
        retenus par `_missing_ref_pre_gates` — jamais sur tous les jalons
        depuis la borne (constat #5579)."""
        sql = ""
        if since is not None:
            sql += (
                "   AND EXISTS (SELECT 1 FROM work_item_milestones m"
                "                WHERE m.work_item_id = w.id AND m.kind = 'merged'"
                "                  AND m.at >= %s"
                "                  AND " + _effective_merged("m") + ")"
            )
            params.append(since)
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

    @classmethod
    def _missing_ref_gates(cls, params: list, *, ref_prefix,
                           attempt_prefix="", retry_s=0, terminal_prefix="",
                           max_attempts=0, since=None) -> str:
        """L539 (#1104) : les conditions « relecture encore due » d'un lot.

        Rendues en SQL pour être ajoutées après `WHERE …, ` et complète
        `params` dans l'ordre du texte produit. Même vérité pour un lot donné
        (`missing_ref_eligible`, une ligne) que pour le balayage
        (`items_missing_ref`, qui matérialise les barrières bon marché avant
        les coûteuses — #5582)."""
        return (
            cls._missing_ref_pre_gates(
                params, ref_prefix=ref_prefix,
                terminal_prefix=terminal_prefix, since=since)
            + cls._missing_ref_late_gates(
                params, attempt_prefix=attempt_prefix, retry_s=retry_s,
                max_attempts=max_attempts, since=since))

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
        return self._dev.review_catch_up_bound()

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
        from ... import review_floor
        production_floor = (review_floor.production_decision_floor(self.db, item_id)
                            if state == "validated" else None)
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
                        or _gate_item(WorkItems(tx).get(item_id)) != _gate_item(validation_item)
                        or review_floor.production_decision_floor(tx, item_id) != production_floor):
                    gate = None
                if not gate or not gate.get("ok"):
                    from ...work import WorkError
                    raise WorkError("validated requires the review quorum on the frozen head")
            rows = tx.query(sql, params)
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), state, note, actor)
            if lifecycle.canonical(state) == lifecycle.IN_PROGRESS:
                # #8096 : la file de fusion est un état de « prêt » ; un lot
                # qui repasse en build en sort dans la même transaction. La
                # ligne et le reçu `removed` sont écrits par le module dev.
                DevWork(tx).queue_take_out_on_build(
                    int(item_id),
                    note="merge queue: taken out (lot returned to build)",
                    actor=actor)
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
            # `unassigned_lots` (une seule règle). Le prédicat nomme les
            # empreintes (table du module `dev`) : il est exécuté par le
            # module, sur sa connexion qualifiée (revue B1 de #7650).
            if self._dev.start_blocked(item_id):
                return None
            ready_state = item["state"]
            exempted = []
            if ready_state == lifecycle.REQUESTED:
                # #7940 : les occupants dont un hold d'attente humaine tient
                # depuis plus de 24 h ne retiennent plus le démarrage ; leur
                # chevauchement est journalisé sur les deux lots. Le plafond
                # de lots en vol reste appliqué tel quel (#4818).
                overlaps = self._footprint_overlaps(item_id, start_on=True,
                                                    blocking_only=False)
                if any(not row.get("human_wait") for row in overlaps):
                    return None
                exempted = [row for row in overlaps if row.get("human_wait")]
                if self._footprint_cap(item_id):
                    return None
        else:
            ready_state = None
            exempted = []
            if assignee is not None and current is None:
                # #4173 : l'attribution d'un lot libre (`work assign`) prend
                # le même verrou que la prise, AVANT toute ligne : deux
                # attributions concurrentes de lots chevauchants décident
                # l'une après l'autre.
                self.db.execute(_DEPENDENCIES_LOCK)
                exempted = self._footprint_guard(item_id, free_only=True,
                                                 start_on=True)
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
        if row is not None and exempted and row.get("assignee"):
            self._warn_human_wait_overlap(item_id, row.get("state") or ready_state,
                                          exempted, actor)
        if row is not None and behind is not None:
            row["forced_behind" if forced_note else "queued_behind"] = behind
        return row

    def _footprint_overlaps(self, item_id, *, free_only=False,
                            start_on=False, blocking_only=True) -> list[dict]:
        """#4173, sous le verrou du graphe : les chevauchements de l'empreinte
        du lot avec celle d'un lot occupant de son dépôt (requête POSTÉRIEURE
        au verrou, comme le contrôle des dépendances). `free_only` : vide si
        le lot occupe déjà son empreinte (réattribution d'un lot démarré).
        `blocking_only=False` (: #7940) rend aussi les occupants qu'une
        attente humaine de plus de 24 h ne fait plus retenir."""
        return self._dev._footprint_overlaps(item_id, free_only=free_only,
                                             start_on=start_on,
                                             blocking_only=blocking_only)

    def _footprint_cap(self, item_id, *, free_only=False):
        """#4818, sous le verrou du graphe : `{cap, in_flight}` si le dépôt du
                lot a atteint son plafond de lots en vol, sinon None ; `free_only` :
                None si le lot occupe déjà son empreinte (il est déjà compté)."""
        return self._dev._footprint_cap(item_id, free_only=free_only)

    def _footprint_guard(self, item_id, *, free_only=False,
                         start_on=False) -> list[dict]:
        """La garde d'attribution d'un lot libre : `FootprintOverlap` s'il
        chevauche un lot occupant, `RepositoryCapReached` si son dépôt est au
        plafond (#4818). Rien n'est écrit ; l'appelant tient le verrou.

        #7940 : rend les chevauchements écartés parce que l'occupant porte une
        attente humaine de plus de 24 h — l'appelant journalise l'avertissement
        sur les deux lots. Le chevauchement n'est plus une attente de fusion,
        mais le lot en attente garde son empreinte pour l'information."""
        overlaps = self._footprint_overlaps(item_id, free_only=free_only,
                                            start_on=start_on,
                                            blocking_only=False)
        blocked = [row for row in overlaps if not row.get("human_wait")]
        if blocked:
            raise interface.FootprintOverlap(blocked)
        cap = self._footprint_cap(item_id, free_only=free_only)
        if cap:
            raise interface.RepositoryCapReached(cap["cap"], cap["in_flight"])
        return [row for row in overlaps if row.get("human_wait")]

    def _warn_human_wait_overlap(self, item_id, state, rows, actor) -> None:
        """#7940 : journalise le chevauchement qu'une attente humaine de plus
        de 24 h ne fait plus retenir.

        Une note sur le lot démarré nomme l'occupant et le chemin partagé ;
        une note sur l'occupant nomme le lot démarré, le chemin partagé et le
        geste de son go — se rebaser sur la cible et relire le delta
        (canon §2). Le lot en attente garde son empreinte : aucune ligne
        `work_item_footprints` n'est touchée.
        """
        by_lot: dict[int, list[dict]] = {}
        for row in rows:
            by_lot.setdefault(int(row["id"]), []).append(row)
        pairs = []
        pending = []
        for lot, lot_rows in sorted(by_lot.items()):
            first = lot_rows[0]
            shared = "; ".join(sorted({
                "%s ~ %s" % (row.get("path"), row.get("other_path"))
                for row in lot_rows}))
            hold = " ".join(str(first.get("human_hold_note") or "").split())[:160]
            since = _hold_iso(first.get("human_hold_ts"))
            pending.append((lot, first.get("state"), (
                "#%d started on the footprint of this lot (%s) although the human "
                "wait \"%s\" has been open since %s (> 24 h): it no longer blocks "
                "the start (#7940). At your go, rebase on the target and review "
                "the delta (canon §2)." % (int(item_id), shared,
                                           hold or "hold: attente owner", since))))
            pairs.append("#%d on %s (hold since %s)" % (lot, shared, since))
        candidate = (
            "footprint overlap with %s: not blocking the start — the overlapping "
            "lot holds a human wait older than 24 h and keeps its footprint for "
            "information (#7940). It rebases on the target and its review covers "
            "the delta at its go (canon §2)." % "; ".join(pairs))
        for lot, lot_state, note in pending:
            self._event(lot, lot_state or state, note, actor)
        self._event(int(item_id), state, candidate, actor)

    def _pending(self, assignee, item_id) -> int | None:
        """#540, dans la transaction ouverte et sous le verrou de l'agent : son
        plus ancien lot ouvert qui l'occupe, autre que `item_id`, ou None.
        L165b : n'occupent pas l'agent le lot qui attend un autre (`qa`,
        `waiting_human`, `blocked`) ni celui dont le début est prévu après
        aujourd'hui — le jour local du processus qui attribue, comme
        `operations.listing`. #5161 : un lot en qa à correctif dû n'y entre
        pas — il n'est plus « à démarrer » (#540), il occupe son auteur par
        `active_lots`."""
        today = _dt.date.today().isoformat()
        self.db.query(PENDING_LOCK, (assignee,))
        rows = self.db.query(
            "SELECT id FROM work_items WHERE assignee = %%s AND started_at IS NULL"
            "   AND type <> 'follow-up'"
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

    def set_external_executor(self, item_id, executor, *, session=None, note,
                              actor, expect_executor=None,
                              expect_session=None) -> dict | None:
        """#3447 : pose (ou efface, `executor=None`) l'exécutant externe d'un lot
        ouvert, avec sa ligne de journal, en une transaction.

        Un lot assigné ne reçoit pas d'exécutant externe (deux porteurs) ; une
        levée reste possible pour nettoyer un lot déjà assigné. None si le lot
        est fermé ou si la pose perd la course contre une attribution : sous le
        verrou de la ligne, la condition est relue, pas devinée.

        #6839 : `session` nomme la session exécutante du feu (facultative). Une
        pose concurrente d'une prise exclusive est refusée (`external_claim_session`
        relu sous le même verrou) ; une levée efface la prise, sa session et son
        instant dans la même écriture.

        #7870 B2 : `expect_executor`/`expect_session` (levée par l'exécutant
        réservé) sont comparés à la ligne RELUE sous `FOR UPDATE` ; une
        réservation renommée entre la preuve et le verrou rend None sans rien
        écrire (ni journal, ni effacement)."""
        closed = f"('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
        with self.db.transaction() as tx:
            if executor:
                require_external_executor_active(tx, executor)
                rows = tx.query(
                    ("UPDATE work_items SET external_executor = %s,"
                     " external_session = %s, updated_at = now()"
                     f" WHERE id = %s AND state NOT IN {closed}"
                     "   AND assignee IS NULL AND external_claim_session IS NULL"
                     " RETURNING __COLUMNS__").replace(
                         "__COLUMNS__", item_columns(self.db)),
                    (executor, session or None, int(item_id)))
                if not rows:
                    return None
                WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
                return rows[0]
            # #7870 : la ligne de journal de la levée est écrite AVANT
            # l'effacement de la réservation. La garde 0567 des connexions
            # limitées lit `work_items.external_executor` au moment de
            # l'INSERT : après la levée, le lot n'est plus réservé et la
            # garde refuserait la trace à l'exécutant qui vient de se lever.
            # Sous le verrou de la ligne, l'état lu ici ne bouge plus jusqu'à
            # l'écriture ; un échec de l'UPDATE annule tout.
            current = tx.query(
                "SELECT state, external_executor, external_session"
                f" FROM work_items WHERE id = %s AND state NOT IN {closed}"
                " FOR UPDATE", (int(item_id),))
            if not current:
                return None
            # #7870 B2 : la preuve de self-clear a lu la réservation AVANT ce
            # verrou ; si un pilote l'a renommée entre-temps (session ou
            # exécutant), elle ne vaut plus pour cette ligne. Rien n'est écrit,
            # pas même la trace : la levée sera relue et retentée.
            if expect_executor is not None:
                held = (current[0]["external_executor"] or "").strip()
                held_session = (current[0]["external_session"] or "").strip()
                if held != expect_executor \
                        or held_session != (expect_session or ""):
                    return None
            WorkItems(tx)._event(int(item_id), current[0]["state"], note, actor)
            rows = tx.query(
                ("UPDATE work_items SET external_executor = NULL,"
                 " external_session = NULL, external_claim_session = NULL,"
                 " external_claimed_at = NULL, external_claim_actor = NULL,"
                 " external_claim_host = NULL, external_claim_proof = NULL,"
                 " external_claim_harness = NULL, external_claim_binding = NULL,"
                 " updated_at = now()"
                 " WHERE id = %s RETURNING __COLUMNS__").replace(
                     "__COLUMNS__", item_columns(self.db)),
                (int(item_id),))
            if not rows:
                # sous le verrou, une disparition est anormale : la trace
                # sans levée ne doit pas être commitée
                raise DbError("lot %s changed while clearing its external "
                              "executor: nothing changed" % item_id)
            return rows[0]

    #: #6839 : les états d'un lot qui ne prennent plus de geste externe
    _EXTERNAL_CLOSED = lifecycle.COMPLETED

    @_retry_deadlock
    def claim_external(self, item_id, session, *, note, actor, host=None,
                       proof=None, harness=None, binding=None) -> dict:
        """#6839 : la prise exclusive d'un geste d'exécutant externe.

        La ligne du lot est verrouillée avant la lecture de la prise : deux
        sessions concurrentes se sérialisent, la seconde voit la première et
        n'écrit rien. Le résultat est un objet, pas une exception — le métier
        nomme le refus :

        * `{"error": "unknown_lot"}` ;
        * `{"error": "closed", "state": …}` ;
        * `{"error": "not_external"}` : lot sans exécutant externe déclaré ;
        * `{"error": "taken", "session": …, "at": …}` : déjà pris, avec la
          porteuse et l'instant de sa prise ;
        * `{"error": "reserved_session", "session": …}` : le feu nomme une
          autre session ;
        * `{"error": "no_proof", "session": …}` : aucune liaison de session
          vérifiée ;
        * `{"error": "not_proved", "session": …}` : le libellé ne correspond
          pas à la session vérifiée de l'appelant ;
        * `{"item": …}` : la prise est écrite, la session, l'instant, l'acteur,
          l'hôte et la preuve avec ; #6877 : le harnais et la ligne de liaison
          aussi quand l'appelant les a donnés (`harness`, `binding`).
        """
        harness = (harness or "").strip() or None
        binding = int(binding) if binding is not None else None
        if (harness is None) != (binding is None):
            return {"error": "not_proved", "session": session}
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT id, state, external_executor, external_session,"
                " external_claim_session,"
                " extract(epoch from external_claimed_at)::float8 AS claimed_ts"
                " FROM work_items WHERE id = %s FOR UPDATE", (int(item_id),))
            if not rows:
                return {"error": "unknown_lot"}
            item = rows[0]
            if item["state"] in self._EXTERNAL_CLOSED:
                return {"error": "closed", "state": item["state"]}
            if not (item.get("external_executor") or "").strip():
                return {"error": "not_external"}
            if (item.get("external_claim_session") or "").strip():
                return {"error": "taken",
                        "session": item["external_claim_session"],
                        "at": item.get("claimed_ts")}
            if not proof:
                return {"error": "no_proof", "session": session}
            if proof != session:
                return {"error": "not_proved", "session": session}
            named = (item.get("external_session") or "").strip()
            if named and named != session:
                return {"error": "reserved_session", "session": named}
            rows = tx.query(
                ("UPDATE work_items SET external_claim_session = %s,"
                 " external_claimed_at = clock_timestamp(), external_claim_actor = %s,"
                 " external_claim_host = %s, external_claim_proof = %s,"
                 " external_claim_harness = %s, external_claim_binding = %s,"
                 " updated_at = now()"
                 " WHERE id = %s RETURNING __COLUMNS__").replace(
                     "__COLUMNS__", item_columns(self.db)),
                (session, actor or "", host, proof, harness, binding, int(item_id)))
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return {"item": rows[0]}

    @_retry_deadlock
    def release_external(self, item_id, session, *, note, actor, host=None,
                         proof=None, harness=None, binding=None,
                         force=False) -> dict:
        """#6839 : lever la prise exclusive d'un geste externe.

        Seule la session porteuse la lève, depuis l'acteur, l'hôte et la
        liaison L41 vérifiée de la prise ; une autre session qui passerait le
        libellé public est refusée, sauf `force` (geste de pilote, vérifié par
        le métier). #6877 : le harnais et la LIGNE de liaison de la prise sont
        aussi exigés quand elle les porte (une révocation puis nouvelle liaison
        est une autre ligne : le pilote libère en `--force` si la porteuse a
        disparu). Le feu nommé (`external_session`) reste : une autre session
        ne peut reprendre que si le feu n'en nommait aucune. Résultats :
        `unknown_lot`, `no_claim`, `not_holder` (avec la porteuse),
        `not_holder_identity`, `not_holder_host`, `not_holder_harness`,
        `not_holder_binding`, `not_proved`, ou `{"item": …}`."""
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT id, external_claim_session, external_claim_actor,"
                " external_claim_host, external_claim_proof,"
                " external_claim_harness, external_claim_binding,"
                " extract(epoch from external_claimed_at)::float8 AS claimed_ts"
                " FROM work_items WHERE id = %s FOR UPDATE", (int(item_id),))
            if not rows:
                return {"error": "unknown_lot"}
            item = rows[0]
            holder = (item.get("external_claim_session") or "").strip()
            if not holder:
                return {"error": "no_claim"}
            if holder != session and not force:
                return {"error": "not_holder", "session": holder}
            if not force:
                claimed_by = (item.get("external_claim_actor") or "").strip()
                claimed_on = (item.get("external_claim_host") or "").strip()
                proved = (item.get("external_claim_proof") or "").strip()
                claimed_harness = (item.get("external_claim_harness") or "").strip()
                claimed_binding = item.get("external_claim_binding")
                if claimed_by and claimed_by != (actor or ""):
                    return {"error": "not_holder_identity", "session": holder,
                            "actor": claimed_by, "host": claimed_on,
                            "at": item.get("claimed_ts")}
                if claimed_on and claimed_on != (host or ""):
                    return {"error": "not_holder_host", "session": holder,
                            "host": claimed_on, "at": item.get("claimed_ts")}
                if proved and proved != (proof or ""):
                    return {"error": "not_proved", "session": holder,
                            "proof": proved}
                if claimed_harness and claimed_harness != (harness or "").strip():
                    return {"error": "not_holder_harness", "session": holder,
                            "harness": claimed_harness,
                            "at": item.get("claimed_ts")}
                if claimed_binding is not None \
                        and int(claimed_binding) != int(binding or 0):
                    return {"error": "not_holder_binding", "session": holder,
                            "binding": int(claimed_binding),
                            "at": item.get("claimed_ts")}
            rows = tx.query(
                ("UPDATE work_items SET external_claim_session = NULL,"
                 " external_claimed_at = NULL, external_claim_actor = NULL,"
                 " external_claim_host = NULL, external_claim_proof = NULL,"
                 " external_claim_harness = NULL, external_claim_binding = NULL,"
                 " updated_at = now()"
                 " WHERE id = %s RETURNING __COLUMNS__").replace(
                     "__COLUMNS__", item_columns(self.db)),
                (int(item_id),))
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return {"item": rows[0]}

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

    def follow_up_watch(self) -> list[dict]:
        """#8196 : les suivis ouverts et leur réveil (alerte
        `follow_up_unfollowed`). Une ligne `wake: scheduled wake delivered`
        est écrite par `consume_wake`, une ligne `wake: cancelled` par
        `schedule_wake` : le journal dit quand le réveil a cessé d'être armé."""
        if not has_wake_at(self.db):
            return []
        delivered = "wake: scheduled wake delivered%"
        return self.db.query(
            "SELECT w.id, w.title, w.state, w.assignee,"
            "       extract(epoch from w.created_at)::float8 AS created_ts,"
            "       extract(epoch from w.wake_at)::float8 AS wake_ts,"
            "       extract(epoch from d.created_at)::float8 AS delivered_ts,"
            "       extract(epoch from x.created_at)::float8 AS disarmed_ts,"
            "       extract(epoch from n.created_at)::float8 AS noted_ts"
            "  FROM work_items w"
            "  LEFT JOIN LATERAL (SELECT e.id, e.created_at FROM work_item_events e"
            "                      WHERE e.work_item_id = w.id"
            "                        AND e.note LIKE %%s"
            "                      ORDER BY e.id DESC LIMIT 1) d ON true"
            "  LEFT JOIN LATERAL (SELECT e.created_at FROM work_item_events e"
            "                      WHERE e.work_item_id = w.id"
            "                        AND (e.note LIKE %%s OR e.note LIKE %%s)"
            "                      ORDER BY e.id DESC LIMIT 1) x ON true"
            "  LEFT JOIN LATERAL (SELECT min(e.created_at) AS created_at"
            "                       FROM work_item_events e"
            "                      WHERE e.work_item_id = w.id AND e.id > d.id"
            "                        AND e.actor = w.assignee) n ON true"
            " WHERE w.type = %%s AND w.state NOT IN %s"
            " ORDER BY w.id" % _CLOSED,
            (delivered, delivered, "wake: cancelled%", lifecycle.FOLLOW_UP))

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

    def record_designation_mark(self, item_id, state, text, actor, evidence) -> None:
        """#6365 : la marque de désignation — ou, #8118, de continuation — et
        sa preuve structurée ; écrivain système hors du contrat L107, appelé
        par le port de désignation seul."""
        if not isinstance(evidence, dict) or not evidence:
            raise DbError("designation mark without its structured evidence")
        self._event(int(item_id), state, text, actor, evidence=evidence)
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

    def schedule_wake(self, item_id, wake_ts, *, text, actor,
                      expect=None, arm_id=None, claim_ttl=None) -> dict | None:
        """#2466 : pose (ou efface) le réveil programmé et journalise, atomique.

        #6134 : avec `expect`, l'effacement n'a lieu que si le réveil est
        encore celui inspecté : même échéance, même événement d'armement (si
        `arm_id`) et aucun claim **vivant**. Un claim expiré (livraison
        abandonnée, `claim_ttl`) ne bloque pas : sans cela, après un hold levé,
        chaque passage verrait le réveil dû, refuserait de l'annuler et
        boucherait sans fin. Un réarmement concurrent par `ameesh work wake`
        ou une note `hold:` n'est jamais écrasé ; la méthode rend alors None
        sans rien écrire (ni journal, ni colonne)."""
        if not has_wake_at(self.db):
            raise DbError("work_items.wake_at is missing: apply migration 0466 "
                          "(`ameesh migrate`) before scheduling a wake")
        item_id = int(item_id)
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT state, extract(epoch from wake_at)::float8 AS wake_ts,"
                "       (wake_claim_at IS NOT NULL) AS claimed,"
                "       extract(epoch from (clock_timestamp() - wake_claim_at))::float8"
                "         AS claim_age_s"
                "  FROM work_items WHERE id = %s AND state NOT IN"
                f" ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}') FOR NO KEY UPDATE",
                (item_id,))
            if not rows:
                return None
            if wake_ts is None and expect is not None:
                row = rows[0]
                if row["wake_ts"] is None \
                        or abs(float(row["wake_ts"]) - float(expect)) >= 0.001:
                    return None
                if row["claimed"]:
                    if claim_ttl is None or row["claim_age_s"] is None \
                            or float(row["claim_age_s"]) < float(claim_ttl):
                        return None
                if arm_id is not None:
                    current = tx.query(
                        "SELECT id FROM work_item_events"
                        " WHERE work_item_id = %s"
                        "   AND (note LIKE %s OR (note LIKE %s AND note LIKE %s))"
                        "   AND note NOT LIKE %s AND note NOT LIKE %s"
                        " ORDER BY id DESC LIMIT 1",
                        (item_id, "%[réveil programmé:%", "wake: %", "%(due %",
                         "wake: scheduled wake delivered%", "wake: cancelled%"))
                    if not current or int(current[0]["id"]) != int(arm_id):
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
        # #5758 : les transitions que l'outil écrit pour un jalon (gel, quorum
        # d'avis) sont signées par lui, pas par l'agent dont le jalon les
        # déclenche : un hold de pilote ne tombe que sur un geste, jamais sur
        # une écriture automatique (`planned_waits.is_automatic_transition`).
        from ...work import SYSTEM_SENDER
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
                                    "validated → qa — review quorum changed (#4351)",
                                    SYSTEM_SENDER)
            same_head = False
            if (kind == "frozen" and items
                    and not items[0].get("workflow_ref")
                    and items[0]["state"] == lifecycle.VALIDATED):
                # #5481 : un regel du même SHA ne rouvre pas la revue. Le
                # quorum rendu sur ce contenu immuable reste acquis ; le
                # journal dit seulement que la validation est gardée.
                previous = tx.query(
                    "SELECT sha FROM work_item_milestones"
                    " WHERE work_item_id = %s AND kind = 'frozen' AND id < %s"
                    " ORDER BY id DESC LIMIT 1",
                    (int(item_id), int(milestone["id"])))
                # #5481 : le comparateur de commits du cœur, déjà importé ici
                # (#4501) ; le cœur n'importe pas l'implémentation du module.
                same_head = bool(previous and packet_head.same_commit(
                    previous[0].get("sha"), sha))
            if (kind == "frozen" and items
                    and not items[0].get("workflow_ref")
                    and same_head):
                WorkItems(tx)._event(
                    int(item_id), "validated",
                    "same head re-freeze %s — validation kept (#5481)"
                    % (sha or "")[:12], actor)
            elif (kind == "frozen" and items
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
                    work.move(tx, item_id, lifecycle.IN_PROGRESS, actor=SYSTEM_SENDER,
                              note="intake → build — frozen (#3467)")
                work.move(tx, item_id, lifecycle.QA, actor=SYSTEM_SENDER,
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
                # P1b #7650 : la preuve appartient au module `dev` ; le jalon
                # et sa transaction restent au noyau.
                _DevProvenance(tx).milestone_receipt(
                    tx, milestone["id"], sha=proof["sha"], actor=proof["actor"],
                    session=proof["session"], turn=proof["turn"],
                    harness=proof["harness"], work_item_id=item_id)
            if kind == "frozen" and has_reviewer(self.db):
                WorkItems(tx)._lapse_waiver(int(item_id), milestone, actor)
            if on_frozen is not None:
                on_frozen(tx, milestone, threads)
        if kind in ("verdict", "frozen"):
            # Git/classification reads run after the writer commits. Nested
            # designation hooks are reconciled by the outer freeze writer.
            from ...freeze_gate import _in_transaction
            if not _in_transaction(self.db):
                self._sync_validation(item_id, SYSTEM_SENDER)
        return milestone

    def _validation_gate(self, item_id, marks=None, *, item=None):
        from ... import merge_queue
        item = self.get(item_id) if item is None else item
        marks = self.milestones(item_id, None) if marks is None else marks
        # #7677 : la cible fraîche, comme la désignation au gel ; un fetch
        # n'entre jamais dans une transaction.
        gate = merge_queue.review_gate_for_item(
            self.db, getattr(self.db, "cfg", None), item, marks,
            refresh_target=not in_transaction(self.db))
        return gate

    def sync_validations(self) -> list[dict]:
        """#6257: retry committed review gates without requiring a new verdict."""
        if in_transaction(self.db):
            return []
        from ...work import SYSTEM_SENDER
        candidates = self.db.query(
            "SELECT work_items.id FROM work_items WHERE work_items.state = 'qa' AND " + _UNPINNED +
            " AND EXISTS (SELECT 1 FROM work_item_milestones m"
            " WHERE m.work_item_id = work_items.id AND m.kind = 'verdict') ORDER BY work_items.id")
        results = []
        for item in candidates:
            try:
                self._sync_validation(item["id"], SYSTEM_SENDER)
                results.append({"id": item["id"], "state": self.get(item["id"])["state"]})
            except Exception as exc:
                # One unreadable lot must not starve the rest of the pass.
                results.append({"id": item["id"], "error": str(exc)})
        return results

    def _sync_validation(self, item_id, actor):
        """Compute the gate outside locks, then compare the immutable ledger.

        Any concurrent freeze/verdict invalidates this observation. #5016: a
        concurrent note only moves `updated_at`, so the guard compares the
        milestones and the fields the gate actually reads, never the whole
        row — a stale note no longer cancels an acquired quorum for good.
        The next writer or notification pass reconciles its own snapshot;
        no stale quorum can promote a lot.

        #6466: the refusal note obeys the same guard. The gate can read a qa
        snapshot for minutes; when a validation or a milestone lands before
        the note, writing it would leave a « validation pending » trace on a
        lot that is no longer waiting for anything. State and observation are
        therefore re-read under the lot lock, and nothing is written if the
        snapshot moved.
        """
        if in_transaction(self.db):
            return
        item = self.get(item_id)
        if item["state"] != "qa" or item.get("workflow_ref"):
            return
        marks = self.milestones(item_id, None)
        from ... import plan_git
        freeze = plan_git.latest_freeze(marks)
        # #5481 : un verdict rendu sur la tête gelée compte même si son
        # identifiant précède un regel du **même SHA** : un contenu identique
        # ne périme pas un quorum déjà rendu. Un gel d'une autre tête garde la
        # règle historique (un verdict postérieur au gel est nécessaire pour
        # payer la lecture de la porte). La porte relit ensuite les avis de la
        # tête courante et refuse tout blocage.
        if not freeze or not any(
                mark.get("kind") == "verdict"
                and (mark["id"] > freeze["id"]
                     or plan_git.same_commit(mark.get("sha"), freeze.get("sha")))
                for mark in marks):
            return
        from ... import review_floor
        try:
            production_floor = review_floor.production_decision_floor(self.db, item_id)
            gate = self._validation_gate(item_id, marks, item=item)
        except Exception as exc:
            gate = {"ok": False, "error": str(exc)}
        if not gate or not gate.get("ok"):
            detail = {"freeze": freeze.get("sha"), "gate": None if gate is None else "refused",
                      "why": (gate or {}).get("why") or "review gate inconclusive",
                      "error": (gate or {}).get("error") or ""}
            text = ("validation pending (#6257): " +
                    json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str))
            # #6466 : la porte a été calculée hors verrou ; un lot validé, ou
            # des jalons changés, entre-temps ne reçoit pas de trace de refus
            # périmée (même garde que le chemin de succès, #5016).
            with self.db.transaction() as tx:
                rows = tx.query(
                    "SELECT state FROM work_items WHERE id = %s AND " + _UNPINNED +
                    " FOR NO KEY UPDATE", (int(item_id),))
                if not rows or rows[0]["state"] != "qa":
                    return
                if not self._validation_snapshot_matches(tx, item_id, item, marks):
                    return
                if tx.query("SELECT id FROM work_item_events"
                            " WHERE work_item_id = %s AND note = %s LIMIT 1",
                            (int(item_id), text)):
                    return
                WorkItems(tx).note(item_id, rows[0]["state"], text, actor)
            return
        with self.db.transaction() as tx:
            tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                     (int(item_id),))
            if not self._validation_snapshot_matches(tx, item_id, item, marks):
                return
            if review_floor.production_decision_floor(tx, item_id) != production_floor:
                return
            rows = tx.query("UPDATE work_items SET state = 'validated', updated_at = now()"
                            " WHERE id = %s AND state = 'qa' AND " + _UNPINNED + " RETURNING id",
                            (int(item_id),))
            if rows:
                WorkItems(tx)._event(int(item_id), "validated",
                                    "qa → validated — review quorum (#4423)", actor)
        if rows:
            # #6089 : après le commit, le détenteur d'un hold de pilote (qui
            # survit au quorum, #5758) apprend que les avis sont acquis.
            from ... import validated_hold_notice
            try:
                validated_hold_notice.notify(self.db, int(item_id))
            except Exception:  # noqa: BLE001 - l'avis ne défait pas le quorum
                pass

    def _validation_snapshot_matches(self, tx, item_id, item, marks) -> bool:
        """#5016/#6466 : l'observation qui a produit la porte tient-elle
        toujours, sous le verrou du lot ? Les jalons et les champs lus par la
        porte se comparent ; une note concurrente ne change que `updated_at`.
        L'état (`qa`) est vérifié par l'appelant, avec le même verrou."""
        return (WorkItems(tx).milestones(item_id, None) == marks
                and _gate_item(WorkItems(tx).get(item_id)) == _gate_item(item))

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
    def _release_migration(self, tx, item_id, row, *, why, actor):
        """#956, #2481 : libère les réservations du lot qui entre dans un état
                terminal (`merged`, `promoted`, `closed`), dans la transaction même de
                la transition : une réservation tenue par un lot fusionné bloquait le
                numéro pour toujours. Le numéro reste protégé tant qu'un fichier le
                porte sur la cible (réservation à numéro exact, `target_numbers`) ou
                sur une branche (allocation du prochain libre, `repo_numbers`). Tous
                les créneaux réservés du lot sont rendus, pas seulement le scalaire."""
        return self._dev._release_migration(tx, item_id, row, why=why, actor=actor)


    def _release_migration_rows(self, tx, item_id, *, slots, scalar, number, note, actor, state, repo=None):
        """Libère le numéro `number` (None : tous) du lot, et rend sa ligne.
        
                Le scalaire est la réservation principale ; s'il est libéré alors que
                d'autres créneaux legacy restent, le plus petit est promu et son
                créneau libéré AVANT l'UPDATE du scalaire : le déclencheur
                `work_items_migration_slot_sync` réécrit alors le créneau `default`
                sans rencontrer le doublon du créneau promu. Les slots nommés restent
                intacts : leur identité ne devient jamais `default` (#4430)."""
        return self._dev._release_migration_rows(tx, item_id, slots=slots, scalar=scalar, number=number, note=note, actor=actor, state=state, repo=repo)

    @_retry_deadlock
    def reserve_migration(self, item_id, repo, number, floor, *, note, actor, issue_ref, release=False, release_number=None):
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
        return self._dev.reserve_migration(item_id, repo, number, floor, note=note, actor=actor, issue_ref=issue_ref, release=release, release_number=release_number)

    def migration_holder(self, number, repo):
        """Le lot OUVERT qui tient ce numéro (#956 : un lot terminal ne le
                tient plus). #2481 : les créneaux supplémentaires comptent aussi (un
                lot peut réserver plusieurs numéros)."""
        return self._dev.migration_holder(number, repo)

    def slots_available(self, db=None) -> bool:
        """P1b #7650 : la table des créneaux existe-t-elle sur ce schéma ?"""
        return self._dev.slots_available(db)

    def reserved_numbers(self, item_id) -> list[int]:
        """P1b #7650 : les numéros de migration encore réservés d'un lot."""
        return self._dev.reserved_numbers(item_id)

    def release_reserved_slots(self, item_id) -> None:
        """P1b #7650 : rend tous les créneaux de migration réservés d'un lot."""
        self._dev.release_slots(self._dev.db, item_id)

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
            DevWork(tx).land_default_slot(item_id, number, repo)
            return rows[0]

    def landed_slot(self, item_id):
        """#4100 : le créneau `default` atterri du lot (`number`, `repo` sous la
                forme `owner/repo`), ou None."""
        return self._dev.landed_slot(item_id)

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
            DevWork(tx).release_landed_slot(item_id, number, repo)
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    def migration_landed_holder(self, number, repo) -> dict | None:
        """Le lot qui nomme déjà ce numéro atterri dans ce dépôt (#1567)."""
        rows = self.db.query(
            "SELECT id, title, state FROM work_items WHERE migration_landed = %s"
            " AND migration_landed_repo = %s", (int(number), repo))
        return rows[0] if rows else None

    # -- prepared conflict resolutions (L160d, 0057) ---------------------------
    def record_resolution(self, item_id, other_id, *, commit, lot_tip, other_tip, ref, files, note, actor):
        """Upsert of the pair's resolution and its journal line, in one
                transaction. `files` goes through JSON: the psql driver binds no
                arrays."""
        return self._dev.record_resolution(item_id, other_id, commit=commit, lot_tip=lot_tip, other_tip=other_tip, ref=ref, files=files, note=note, actor=actor)

    def resolutions(self, item_ids):
        return self._dev.resolutions(item_ids)

    # -- merge queue (L160e, 0058) ---------------------------------------------
    def _queue_row(self, db, item_id):
        return self._dev._queue_row(db, item_id)

    def queue_add(self, item_id, *, target, frozen_sha, note, actor, source='', frozen_id=None, packet=None, packet_initial=False, lease=None, withdrawn_after=None):
        return self._dev.queue_add(item_id, target=target, frozen_sha=frozen_sha, note=note, actor=actor, source=source, frozen_id=frozen_id, packet=packet, packet_initial=packet_initial, lease=lease, withdrawn_after=withdrawn_after)

    def queue_update(self, item_id, *, state=None, rank=None, reason=None, landing_sha=None, note='', actor='', clear_rank=False, lease=None, freeze=None, entry=None):
        return self._dev.queue_update(item_id, state=state, rank=rank, reason=reason, landing_sha=landing_sha, note=note, actor=actor, clear_rank=clear_rank, lease=lease, freeze=freeze, entry=entry)

    def queue_remove(self, item_id, *, note, actor):
        return self._dev.queue_remove(item_id, note=note, actor=actor)

    def queue_withdrawn_since(self, item_id, since_ts, *, target=None, frozen_id=None):
        return self._dev.queue_withdrawn_since(item_id, since_ts, target=target, frozen_id=frozen_id)

    def land_lock(self, key, owner, ttl_s, *, epoch=None):
        return self._dev.land_lock(key, owner, ttl_s, epoch=epoch)

    def land_unlock(self, key, owner, epoch):
        return self._dev.land_unlock(key, owner, epoch)

    def land_holder(self, key):
        """#3598 : qui tient le bail, son époque, et jusqu'à quand, pour le
                refus de `give`/`withdraw`. #6563 : l'époque sert aussi de preuve
                fraîche au détenteur (`ameesh work packet check`). Horloge de la base,
                lecture seule, jamais une garde. L'échéance est rendue en UTC ISO
                (`…Z`), identique sous les deux pilotes plutôt que datetime (psycopg)
                ou texte (psql)."""
        return self._dev.land_holder(key)

    def train_read(self, key):
        return self._dev.train_read(key)

    def train_save(self, key, document, *, revision, owner, epoch):
        return self._dev.train_save(key, document, revision=revision, owner=owner, epoch=epoch)

    def train_abandon_request(self, key, plan_id, request):
        return self._dev.train_abandon_request(key, plan_id, request)

    def train_requalify_request(self, project, pin, gate, reviews, lot, requester):
        return self._dev.train_requalify_request(project, pin, gate, reviews, lot, requester)

    def train_requalify_requests(self, project):
        return self._dev.train_requalify_requests(project)

    def train_requalify_resolve(self, request_id, state, *, actor, error=""):
        return self._dev.train_requalify_resolve(request_id, state, actor=actor, error=error)

    def train_abandon(self, key, plan_id, document, *, revision, owner, epoch):
        return self._dev.train_abandon(key, plan_id, document, revision=revision, owner=owner, epoch=epoch)

    def train_adopt(self, key, owner, *, revision, previous, service, ttl_s):
        return self._dev.train_adopt(key, owner, revision=revision, previous=previous, service=service, ttl_s=ttl_s)

    def train_fire_read(self, key, plan_id):
        return self._dev.train_fire_read(key, plan_id)

    def train_fire_give(self, key, document):
        return self._dev.train_fire_give(key, document)

    def train_fire_change(self, key, fire_id, state, *, actor, owner=None, epoch=None):
        return self._dev.train_fire_change(key, fire_id, state, actor=actor, owner=owner, epoch=epoch)

    def queue(self, target=None):
        return self._dev.queue(target)

    def set_branch_head(self, item_id, branch, head):
        """Retient le dernier commit de la branche vu en avance sur sa cible
                (sans journal ni `updated_at` : un relevé n'est pas une activité du
                lot), et quand (L118d, 0372, horloge de la base). Une pointe inchangée
                est relevée de nouveau si son instant précède la dernière fusion
                partielle vue (#346 r2) : elle est encore là après cette fusion.
                Conditionné à la branche lue : rien si elle a changé. #6626 : la
                tête entre aussi, une fois, à l'historique des têtes du lot (un
                enfant parti d'une tête intermédiaire reste vu empilé)."""
        return self._dev.set_branch_head(item_id, branch, head)

    def branch_heads(self, item_ids):
        return self._dev.branch_heads(item_ids)

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
            # #8191: a follow-up stays with its stopped agent (_release_agent); counting it would relock forever.
            "   AND type <> 'follow-up'"
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
        # 1. Development lots return to the team; personal follow-ups stay assigned.
        rows = self.db.query(
            "SELECT %s FROM work_items WHERE assignee = %%s"
            f"   AND state NOT IN ('{lifecycle.DELIVERY_RECORDED}', '{lifecycle.PROMOTED}', '{lifecycle.CLOSED}')"
            "   AND type <> 'follow-up'"
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
                "   AND type <> 'follow-up'"
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

    def by_unique_ref(self, key) -> dict | None:
        """#6090 : le lot qui porte exactement cette clé durable, quel que soit
        son état, ou None. La comparaison est à la casse près : la clé est
        opaque, fournie par l'appelant (l'espacement par source lui revient).
        Une clé vide ne désigne rien, comme une source vide."""
        text = str(key or "").strip()
        if not text:
            return None
        rows = self.db.query(
            "SELECT %s FROM work_items WHERE unique_ref = %%s"
            % item_columns(self.db), (text,))
        return rows[0] if rows else None

    def by_package(self, package_id) -> list[dict]:
        return self.db.query(
            "SELECT %s FROM work_items WHERE package_id = %%s ORDER BY id" % item_columns(self.db),
            (package_id,))

    # -- sujets (#8193, 0608) -------------------------------------------------
    _SUBJECT_COLUMNS = (
        "id, project, slug, title, author, state, github_ref, closed_by,"
        " extract(epoch from created_at)::float8 AS created_ts,"
        " extract(epoch from closed_at)::float8 AS closed_ts")

    def subject_add(self, project, slug, title, *, author) -> tuple[dict, bool]:
        # ON CONFLICT DO NOTHING : deux créations concurrentes du même slug
        # rendent la même ligne, la seconde sans la réécrire.
        rows = self.db.query(
            "INSERT INTO work_subjects (project, slug, title, author)"
            " VALUES (%%s, %%s, %%s, %%s) ON CONFLICT (project, slug) DO NOTHING"
            " RETURNING %s" % self._SUBJECT_COLUMNS,
            (project, slug, title, author or ""))
        if rows:
            return rows[0], True
        return self.subjects(project=project, slug=slug)[0], False

    def subjects(self, *, project=None, slug=None, open_only=False) -> list[dict]:
        where, params = " WHERE true", []
        if project is not None:
            where += " AND project = %s"
            params.append(project)
        if slug is not None:
            where += " AND slug = %s"
            params.append(slug)
        if open_only:
            where += " AND state = 'open'"
        return self.db.query(
            "SELECT %s FROM work_subjects%s ORDER BY project, slug"
            % (self._SUBJECT_COLUMNS, where), tuple(params))

    def subject_close(self, subject_id, *, actor) -> dict | None:
        rows = self.db.query(
            "UPDATE work_subjects SET state = 'closed',"
            " closed_at = coalesce(closed_at, now()),"
            " closed_by = coalesce(closed_by, %%s)"
            " WHERE id = %%s RETURNING %s" % self._SUBJECT_COLUMNS,
            (actor or "", int(subject_id)))
        return rows[0] if rows else None

    def subject_attach(self, item_id, subject_id, *, note, actor) -> dict | None:
        with self.db.transaction() as tx:
            rows = tx.query(
                "UPDATE work_items SET subject_id = %%s, updated_at = now()"
                " WHERE id = %%s RETURNING %s, subject_id" % item_columns(self.db),
                (None if subject_id is None else int(subject_id), int(item_id)))
            if not rows:
                return None
            WorkItems(tx)._event(int(item_id), rows[0]["state"], note, actor)
            return rows[0]

    def subject_items(self, *, subject_id=None, package_id=None) -> list[dict]:
        clauses, params = [], []
        if subject_id is not None:
            clauses.append("subject_id = %s")
            params.append(int(subject_id))
        if package_id:
            clauses.append("package_id = %s OR package_parent = %s")
            params.extend([package_id, package_id])
        if not clauses:
            return []
        return self.db.query(
            "SELECT %s, subject_id FROM work_items WHERE %s ORDER BY id"
            % (item_columns(self.db), " OR ".join("(%s)" % c for c in clauses)),
            tuple(params))

    def record_packet(self, package_id, *, branch, base_sha, head_sha, units, note, actor, expect_pushed=None):
        return self._dev.record_packet(package_id, branch=branch, base_sha=base_sha, head_sha=head_sha, units=units, note=note, actor=actor, expect_pushed=expect_pushed)

    def record_packet_snapshot(self, *, repository, target, package_id, branch, head_sha, base_sha, payload, actor, lease_key, lease_owner, lease_epoch):
        return self._dev.record_packet_snapshot(repository=repository, target=target, package_id=package_id, branch=branch, head_sha=head_sha, base_sha=base_sha, payload=payload, actor=actor, lease_key=lease_key, lease_owner=lease_owner, lease_epoch=lease_epoch)

    def packet_snapshots(self, *, repository, target=None, package_id=None, branch=None, head_sha=None):
        return self._dev.packet_snapshots(repository=repository, target=target, package_id=package_id, branch=branch, head_sha=head_sha)

    def packet_members(self, *, item_ids=None, package_ids=None):
        return self._dev.packet_members(item_ids=item_ids, package_ids=package_ids)

    def packet_members_since(self, since_ts):
        """#3256 C (P1b #7650) : membres des paquets composés depuis cet instant."""
        return self._dev.packet_members_since(since_ts)

    def train_states(self):
        """P1b #7650 : les trains d'intégration centraux (alerte #6014)."""
        return self._dev.train_states()

    def train_projects(self):
        """P1b #7650 : les projets visés par un train."""
        return self._dev.train_projects()

    def train_live_projects(self, fresh_s):
        """#8159 : les projets dont un train tourne, vus de tout hôte."""
        return self._dev.train_live_projects(fresh_s)

    def train_receipt_kinds(self, work_item_id, frozen_id, *, exact=False):
        """P1b #7650 : reçus d'allocation/retrait d'un lot (attestation)."""
        return self._dev.train_receipt_kinds(work_item_id, frozen_id, exact=exact)

    def train_receipts(self, item_ids):
        """P1b #7650 : reçus de file des lots donnés (#2965)."""
        return self._dev.train_receipts(item_ids)

    def alerts_entrust(self, role, host, *, lock_key, owner, epoch):
        """#4468 (P1b #7650) : transmission du passage d'alertes sous bail."""
        return self._dev.alerts_entrust(
            role, host, lock_key=lock_key, owner=owner, epoch=epoch)

    def packet_member_heads(self, package_id):
        """#3416 (P1b #7650) : têtes déclarées par les membres d'un véhicule."""
        return self._dev.packet_member_heads(package_id)

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
                    DevWork(tx).land_default_slot(item_id, number, repo)
                elif release_landed is not None:
                    DevWork(tx).release_landed_slot(item_id, *release_landed)
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

    def project_plan_rows(self, project, *, assignment_id=None):
        """#4174: complete open project snapshot, including every waiting lot."""
        return self._dev.project_plan_rows(project, assignment_id=assignment_id)

    def unassigned_lots(self, limit):
        return self._dev.unassigned_lots(limit)

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
    def issue_feed(self, limit):
        """Lots ouverts, porteurs d'une `issue_ref`, ou pinnés sur une révision
                Process (#2130/D : un cœur `merged` reste projeté ouvert, il doit donc
                être dans le flux), les plus récents d'abord pour la borne, rendus par
                id croissant avec l'équipe de leur fiche et l'équipe, le chantier et
                l'hôte de leur assigné.
        
                #2130/D : `workflow_ref`, `workflow_stage` et `workflow_version` sont
                portés par la lecture comme B5 les porte dans `work.get` (`to_jsonb`,
                sans sonde de colonne) ; base pré-0452 : `''`, NULL, 0.
        
                #5583 : `merged_ts` se calcule en ensemble. La fenêtre (`ids`) est
                matérialisée une fois ; `merged` agrège les jalons de fusion par lot et
                n'évalue `agent_mesh_merged_annulled` que là où une correction `build`
                postérieure au dernier jalon existe (2 lots sur 5 000 au central) ; la
                liste des créneaux de migration est un agrégat borné aux lots de la
                fenêtre. L'ordre par id croissant est rendu en mémoire : trier 5 000
                identifiants coûte moins que trier 5 000 lignes larges (5 Mo, tri
                externe, diagnostic #5579).
                """
        return self._dev.issue_feed(limit)

    def issue_refs(self, limit) -> list[dict]:
        """#5584 : `(id, issue_ref)` des lots porteurs d'une `issue_ref`, dans la
        même fenêtre que `issue_feed` (mêmes lots, même borne), sans ses
        colonnes larges ni ses sous-requêtes. La table de correspondance
        `issue_ref → lots` ne lit que ces deux champs ; le flux complet coûtait
        ~10 s sous charge pour la construire (diagnostic #5579). Extension
        PostgreSQL locale : l'appelant replie sur `issue_feed` ailleurs."""
        return self.db.query(
            "SELECT w.id, btrim(w.issue_ref) AS issue_ref"
            "  FROM (SELECT id, issue_ref FROM work_items"
            "         WHERE state NOT IN %s OR coalesce(btrim(issue_ref), '') <> ''"
            "            OR coalesce(workflow_ref, '') <> ''"
            "         ORDER BY id DESC LIMIT %%s) w"
            " WHERE coalesce(btrim(w.issue_ref), '') <> ''"
            " ORDER BY w.id" % (_CLOSED,),
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

    def _intake_footprint(self, item_id, paths, actor):
        """#4186 : écrit les chemins d'empreinte `intake` absents (union), avec
                leur ligne de journal — le geste commun de `add` et de la file
                d'amélioration. Rend les chemins ajoutés (vide : rien à changer).
                Le verrou du graphe est pris par les appelants qui gardent une création
                attribuée ; ces lots naissent libres."""
        return self._dev._intake_footprint(item_id, paths, actor)

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
            f" WHERE (w.state <> '{lifecycle.CLOSED}' OR w.close_reason = 'delivered')"
            # #8194 : un suivi n'entre ni dans `work estimates` ni dans la table
            # de référence (ni, par elle, dans l'alerte estimate_bias)
            "   AND " + lifecycle.not_follow_up_sql("w"))
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
    def measure_candidates(self, *, ids, retry_diff_s, since_s, limit):
        """Les lots livrés à mesurer (ou à remesurer), le plus récent d'abord.
        
                #5411 : sans `ids`, la lecture part des seuls lots qui MANQUENT de
                mesure (ou dont le diff/CI est incomplet), matérialisés une fois
                (145 lots sur la base du 01/11) ; le LATERAL du jalon `merged` ne
                tourne plus pour les 4 289 lots ouverts démarrés (111 931 tampons,
                2,2 s mesurés). Les deux conditions restent celles de #3056/#2317 :
                un lot mesuré sans diff, ou sans CI alors qu'il a une branche, est
                remesuré tant que sa fusion est récente."""
        return self._dev.measure_candidates(ids=ids, retry_diff_s=retry_diff_s, since_s=since_s, limit=limit)

    def save_measure(self, item_id, measure):
        return self._dev.save_measure(item_id, measure)

    def measure(self, item_id):
        return self._dev.measure(item_id)

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

    def _event(self, item_id, state, note, actor, *, evidence=None) -> None:
        # Historical journals and signed receipts retain their stage vocabulary.
        # A note on a delivered core must not erase the merge/promotion phase
        # from the previous-event checks used for merge annulment and SLOs.
        # #6365 : `evidence` n'est passé que par un écrivain système (la marque
        # de désignation) ; aucune écriture à texte libre ne le remplit.
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor, evidence) "
            "SELECT id, CASE WHEN %s = 'delivered' THEN "
            "coalesce(to_jsonb(work_items)->>'legacy_state', state) ELSE %s END, %s, %s, "
            "%s::jsonb FROM work_items WHERE id = %s",
            (state, state, note or "", actor or "",
             None if evidence is None else json.dumps(evidence), item_id),
        )
