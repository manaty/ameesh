# SPDX-License-Identifier: AGPL-3.0-only
"""Classes abstraites du stockage : ce qu'un pilote doit fournir.

Un pilote (`storage.postgres` en v1) implémente chaque domaine ; `Storage`
les regroupe pour une connexion (ou une transaction ouverte). Les garanties
de chaque opération (atomicité, verrous, recontrôle des échéances, fencing
par `lease_epoch`) sont documentées ici et dans le pilote : un autre pilote
doit les tenir à l'identique.

Hors de l'interface : le schéma et la connexion, propres au pilote
------------------------------------------------------------------

Ni les migrations ni le contrôle du schéma ne sont des opérations de
l'interface (spec §10 : « les migrations restent des fichiers SQL versionnés
propres au pilote »). Les points d'entrée (CLI, exécuteur, ameesh-approve,
tests) les appellent sur la connexion, avant `storage.of(db)` :

* `ameesh.migrations` — pilote Postgres : fichiers `migrations/NNNN_nom.sql`
  immuables (empreinte SHA-256 dans `schema_migrations`), chacun appliqué en
  UNE transaction sous verrou consultatif (`pg_advisory_xact_lock`), délai
  de requête levé le temps de la transaction, schéma nommé créé au besoin.
  Ces fichiers définissent tout le schéma dans le dialecte du pilote :
  tables et index, vues (`agent_mesh_overview`, `mesh_approvals_status`),
  triggers (réveil `agent_mail` / `agent_lease` / `work_item`, journal et
  gardes de `actions`) et fonctions PL/pgSQL (`ameesh_action_*`,
  `ameesh_receipt_*`, `ameesh_standing_*`, `ameesh_placement_profile`). Un
  autre pilote a SA suite de migrations, sur le même principe (versions
  immuables, application atomique et exclusive).
* `ameesh.db.require_schema` (schéma migré, sinon `SchemaMissing` qui dit
  de lancer `agent-mesh migrate`), avec `ameesh.db.connect` (psycopg | psql,
  `search_path`, `statement_timeout`), `transaction()` et les erreurs
  `DbError` / `Unavailable` / `SchemaMissing` que les modules métier
  attrapent : c'est la connexion du pilote.

Ce qu'un pilote SQLite devra fournir (profil local, décision 0016)
------------------------------------------------------------------

Non implémenté ; liste de contrôle pour qui l'écrira.

1. `SqliteStorage(Storage)`, `driver = "sqlite"`, choisi par `storage.of(db)`
   selon la connexion. La connexion offre `query` / `execute` / `script` /
   `transaction()` (rappelable dans une transaction déjà ouverte, comme les
   deux pilotes Postgres) et lève les erreurs de `ameesh.db` (`DbError`,
   `Unavailable`, `SchemaMissing`).

2. Tous les domaines de `Storage`, chaque opération avec la MÊME signature,
   les mêmes clés dans les lignes rendues (instants en secondes epoch
   flottantes `*_ts`, JSON rendu en dict / list, comptes en int) et les
   mêmes refus (None, faux, liste vide : jamais une exception pour un refus
   métier) :

   agents          upsert get overview claimable mark_event_wake set_session
                   set_session_account cwd_used set_status set_mode wake_check set_pending_prompt
                   clear_model count
   leases          claim attach_claim state renew release turn_in_progress
                   reap clear_session take_pending_prompt begin_turn
                   restore_prompt end_turn adopt_session_origin
   pending_spend   put set_model set_resident_usage get clear
                   (Postgres optional: put_get clear_if)
   turn_costs      last_reading insert spent ledger correct last_before first_after
   mailbox         send unread unread_active unread_urgent get mark_delivered reserve
                   deliver release unread_counts history pending_recipients
                   pending_recipients_sorted unread_total dead_letters forward
   wakeups         subscribe notify
   keys            info register revoke registered
   approvals       create recent candidates consume
   nonces          state consume
   work            add get items move note assign events milestones add_milestone
                   delays timeline link_package by_package close_merged close
                   refresh_package_parents delegate current_delegation
                   mark_delegate_turn due_delegations resolve_delegation
                   overdue_delegations returned_delegations backlog_add
                   backlog_basket_add backlog auto_takes_since unassigned_lots issue_feed set_issue_ref set_estimate
                   estimates mark_started estimate_history
   packages       all get upsert retire lock_sync
   actions         get recent attempts events log_event last_event_note
                   decision_queues covering_grants launched propose bind
                   launch settle replace cancel
   action_source   columns rows
   canon           record_state state record_auth_state host_rows
                   write_declared clear_responsible set_placement
                   lineage_rows revive clear_pending_stop move_host
                   stop_removed
   ephemerals      creator create exists
   authenticators  lock_registry registry_lock_held under_registry_lock
                   canon_refs last_sync journal_head append_sync rebase_default
                   registered for_approver active_holders update_meta revoke insert
   threads         index indexed
   grants          register reserve release live_reservations candidates
                   revoke get
   placements      recorded
   progress        lots lot_events lot_transitions lot_event_last lot_hold_events
                   packet_exits
                   lot_milestones lot_actions actions agents costs packages
                   package_items lot_messages
   projects        board
   decisions       create get pending history pending_for_lot close register_chat
   roadmap         plan_item item_plans plan_package items commitments
                   add_commitment record_proposal set_commitment
   operations      set_settings set_session_work_item listing request_restart
                   apply_restart adopt resume message_lots assigned_open_lots
                   open_lots_activity state_lots_activity turns record_gauges
                   gauge_history latest_gauges gauge_latest assigners
                   record_balance balances
   session_bindings active bind set_pid revoke listing with_pids
   lot_sessions    switch listing actionable note_wake purge
   budgets         limits put events
   api_requests    reserve complete release
   alert_acknowledgements live acknowledge invalidate clock
   watches         put delete list deliver
   events          record after readable waiting bounds count_before prune
   turn_resources  open_turn close_turn mark_orphan settle get open_by_agent orphans
                   stale_running churn
   turn_observation read
   housekeeping    log recent summary last_measure register_worktree worktrees
                   set_worktree_status set_worktree_attribution

3. Atomicité et verrous. Postgres tient les garanties par des écritures
   conditionnelles en une instruction (`UPDATE … WHERE`, `INSERT … ON
   CONFLICT`, `INSERT … SELECT`), des verrous de ligne (`FOR UPDATE`,
   `FOR SHARE`), un verrou consultatif transactionnel (registre des
   authentificateurs) et des fonctions PL/pgSQL qui sont chacune UNE
   transaction (`actions.launch` / `settle` / `replace`, `nonces.consume`,
   `grants.register` / `reserve` / `release`). SQLite n'a qu'un écrivain :
   une opération documentée « UNE transaction », ou qui lit puis écrit sous
   condition, s'exécute en `BEGIN IMMEDIATE` (verrou d'écriture pris AVANT
   la première lecture) et garde le motif verrou → recontrôle des échéances
   à l'heure réelle → écriture ; le fencing par `lease_epoch` et le
   « un seul gagnant » des réclamations, consommations et réservations
   restent dans la condition de l'écriture. `lock_registry` /
   `registry_lock_held` : vrais seulement dans la transaction qui a pris le
   verrou ; le jeton `RegistryLock` ne se forge toujours pas hors d'un
   pilote. Le code PL/pgSQL devient du code du pilote, dans la transaction,
   même ordre de contrôles, mêmes issues ; une échéance dépassée lève une
   `DbError` dont le texte commence par
   `ameesh_echeance [expired|iat_future] : ` (lu par `ameesh.receipts`).
   `busy_timeout` tient le rôle de `statement_timeout`.

4. Heure. Postgres distingue `now()` (début de la transaction) et
   `clock_timestamp()` (heure réelle, celle des recontrôles après verrou) ;
   SQLite lit son horloge au moment de chaque contrôle, après le verrou.

5. Réveil. Sans LISTEN/NOTIFY, `wakeups.subscribe` rend un abonnement LOCAL
   au même contrat (`wait(timeout)` : signal, None au délai, `down` ;
   `close()` depuis un autre fil), signalé par le pilote APRÈS le commit des
   écritures qui, en Postgres, déclenchent `agent_mail` (dépôt d'un
   message), `agent_lease` (bail, epoch ou statut d'un agent) et
   `work_item` (lots) ; `notify` émet sur ce même abonnement. Il peut aussi
   rendre None : l'exécuteur se replie alors sur le sondage (`poll`),
   chemin déjà en place.

6. Schéma. Sa propre suite de migrations (voir ci-dessus) : tables, index,
   vues, journal et gardes de `actions` (triggers SQLite), profil de
   placement (`ameesh_placement_profile` en fonction enregistrée sur la
   connexion ou calculé par le pilote) ; son `require_schema` ;
   `PRAGMA table_info` à la place de `pg_attribute` (`action_source`).

7. Tests. La suite doit passer sur les deux pilotes (0016). Ses montages
   appellent encore `db.query` / `db.execute` en SQL Postgres (schéma
   jetable par classe, TRUNCATE) : à rendre propres au pilote.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

from .conversations import Conversations
from .provenance import Provenance
from .work_dependencies import WorkDependencies
from .work_footprints import WorkFootprints
from .migration_reservations import MigrationReservations

import abc
import re
from datetime import datetime
from typing import Any, Callable, Protocol, Sequence, TypedDict

#: #1654 : observation NON fournie au CAS `set_issue_ref` — le contrôle de la
#: réservation (`migration_repo`) n'est alors pas appliqué, les appelants qui
#: ne lisent pas la réservation gardent le CAS d'origine.
UNCHECKED = object()

#: §4 (owner 14/10/2026) : début de la note de journal d'un lot RENDU à la file
#: d'équipe (`work.return_to_queue`, balayage `ameesh work wip`). Un lot
#: `build` sans assigné n'est prenable QUE s'il a été rendu ainsi : un lot en
#: cours (prérequis, travail confié par courrier) garde son état sans passer à
#: la file (le pilote lit ce marqueur dans le journal, `requeued_sql`).
RETURNED_NOTE = "returned to the queue"
#: Débuts des notes par lesquelles un lot est CONFIÉ : attribution et création
#: (`work.assign`, `work.add`), délégation à échéance (`work.delegate`), prises
#: automatiques (`backlog.LOT_TAKE_NOTE`, `backlog.AUTO_TAKE_NOTE`), reprise
#: sur pause budget (`handoff.HANDOFF_NOTE`). La DERNIÈRE de ces notes porte
#: l'exception pilote à la limite d'en-cours (`work.WIP_EXCEPTION_MARK`) : une
#: note postérieure sans le marqueur l'efface, et un retour §4 doit être
#: POSTÉRIEUR à la dernière attribution pour rendre le lot prenable.
#: Les textes sont définis ici, seuls : `wip_exceptions`, `requeued_sql`,
#: `handoff.last_assigner` et les prises y lisent les mêmes constantes.
DELEGATED_NOTE = "delegated to "
LOT_TAKE_NOTE = "lot de l'équipe pris de lui-même"
AUTO_TAKE_NOTE = "prise automatique"
HANDOFF_NOTE = "pause budget (reprise) : lot repris"
ASSIGN_NOTE_PREFIXES = ("assigned to ", "assigné à ", "created and assigned to ",
                        DELEGATED_NOTE, LOT_TAKE_NOTE, AUTO_TAKE_NOTE, HANDOFF_NOTE)
#: #2584 : début de la note de journal d'une ligne de panier (`backlog_basket_add`)
#: — le compte des lignes d'un panier se lit sur ce préfixe
BASKET_LINE_NOTE = "basket line"
#: #3447 : début de la note qui déclare (ou efface) l'exécutant externe d'un
#: lot. `debit` lit la date de la déclaration sur ce préfixe ; le texte est
#: défini ici, seul.
EXTERNAL_EXECUTOR_NOTE = "external executor:"
EXTERNAL_EXECUTOR_CLEARED_NOTE = "external executor cleared"
#: #3560 : la cause d'éjection de la file quand le projet exige une CI sur le
#: commit gelé (`ci_required`) et qu'aucun run ne s'y lit (`merge_queue._required_ci`).
#: Sur une ligne d'inscription au paquet, ce n'est pas un échec du lot : le §11 bis
#: ne lance aucune CI de lot, la CI complète du paquet en tient lieu. Le texte est
#: défini ici, seul : la file l'écrit, `queue_add` et le train le relisent.
CI_REQUIRED_EJECTION = "requires CI on the frozen commit (ci_required)"


def ci_required_ejection(row) -> bool:
    """#3560 : une ligne de file `ejected` pour la seule absence de CI de lot."""
    return bool(row) and row.get("state") == "ejected" \
        and CI_REQUIRED_EJECTION in str(row.get("reason") or "")


class Domain(abc.ABC):
    """Opérations d'un domaine, liées à une connexion ou une transaction."""

    __slots__ = ("db",)

    def __init__(self, db: Any):
        self.db = db


# --------------------------------------------------------------------------
# registre des agents (`agent_registry`) : lignes et baux
# --------------------------------------------------------------------------

class Agents(Domain):
    """Lignes du registre hors bail : inscription, lecture, statut."""

    @abc.abstractmethod
    def upsert(self, name: str, *, chantier: str | None, harness: str | None,
               host: str | None, cwd: str | None, session_id: str | None,
               status: str | None, status_text: str | None, model: str | None,
               budget_usd: float | None, mode: str | None = None) -> dict:
        """Crée l'agent ou met à jour les champs fournis ; rend la ligne.

        `mode` (L37, 0030) n'est écrit qu'à la création (défaut `execute`)."""

    @abc.abstractmethod
    def human_known(self, human: str) -> bool:
        """L46 : `human:<id>` est-il déjà résolu en base (responsable d'un
        agent ou d'un paquet du canon) ?"""

    @abc.abstractmethod
    def upsert_unleased(self, name: str, *, chantier: str | None, harness: str | None,
                        host: str | None, cwd: str | None,
                        session_id: str | None) -> dict:
        """L46 : inscription par un hook SANS bail, en une instruction.

        Ligne neuve : agent `externe`. Ligne d'un agent `execute` : seul
        `last_seen` avance. Ligne `externe` : hôte/harnais/chantier suivis ;
        session et dossier écrits seulement s'il n'y en avait pas."""

    @abc.abstractmethod
    def get(self, name: str) -> dict | None:
        """Agent by primary or alias; alias resolutions are durably audited.

        #3729 : la ligne porte aussi `last_mail_ts` et `last_mail_host`, le
        dernier message déposé et l'hôte qui l'a déposé — le signe de vie que
        `ameesh show` affiche comme `ameesh list` depuis #3337."""

    @abc.abstractmethod
    def resolve_name(self, name: str, *, operation: str = "lookup") -> str:
        """Resolve a spelling and audit aliases; unknown names stay unchanged."""

    @abc.abstractmethod
    def operator_pause(self, name: str, *, paused: bool, reason: str,
                       actor: str, principal: str,
                       expect_reason: str | None = None,
                       not_after: float | None = None,
                       until: float | None = None,
                       indefinite: bool = False) -> dict | None:
        """Native human hold: row lock, state and audit in one atomic statement.

        No status/prompt/lease changes; repeated state has no new event.
        External agents are returned unchanged. Not exposed to executors.

        `expect_reason` (#2152) : ne lève que la pause dont la raison est
        exactement celle-là. La comparaison et la levée restent dans la même
        instruction, sous le verrou de ligne : une pause étrangère posée entre
        la lecture et la reprise n'est jamais levée. Refusé pour POSER une
        pause (`paused=True`).

        `not_after` (#2184/R3) : échéance absolue (epoch) pour une POSE ; la
        base la compare à son horloge sous le verrou de ligne et refuse la
        pose au-delà (`changed=false`, `refusal='not_after'`). Une pose
        distante retardée ne peut donc pas committer après le nettoyage.

        `until` (#3961) : échéance (epoch) de la pause posée, levée ensuite
        par `operator_pause_expire` ; une échéance déjà passée est refusée
        (`refusal='until_past'`). `indefinite` : pose sans échéance, choix
        explicite (sans lui, une pose exige `until`).
        """

    @abc.abstractmethod
    def operator_pause_expire(self, name: str | None = None) -> list[dict]:
        """#3961 : lève les pauses opérateur échues (toutes, ou celle de
        `name`), une fois chacune, avec un événement d'audit au nom de
        l'humain qui les avait posées ; rend les pauses levées."""

    @abc.abstractmethod
    def retire(self, name: str, *, actor: str, reason: str) -> dict | None:
        """#3460 : retire l'agent sous le verrou de sa ligne ; None s'il est inconnu.

        Rend `{name, changed, blockers}`. Un bail vivant, un lot assigné non
        clos, une veille `ameesh watch` vers l'agent ou un lot ouvert dont il
        est l'exécutant externe déclaré sont des `blockers`
        (`{kind: lease|lot|external_executor|watch, …}`) : rien n'est écrit tant qu'il en reste.
        Seules `retired_at`, `retired_by` et `retired_reason` changent ; ni
        statut, ni mode, ni bail. Déjà retiré : `changed` faux. Not exposed to
        executors."""

    @abc.abstractmethod
    def restore(self, name: str, *, actor: str) -> dict | None:
        """#3460 : l'inverse de `retire` ; None si l'agent est inconnu.

        Efface les trois colonnes du retrait et rend `{name, changed}` ; un
        agent qui n'était pas retiré rend `changed` faux. Not exposed to
        executors."""

    @abc.abstractmethod
    def same_identity(self, first: str, second: str) -> bool:
        """#4299 : vrai si les deux graphies désignent la même fiche du
        registre (nom ou alias). Lecture seule : rien n'est audité."""

    @abc.abstractmethod
    def harnesses(self) -> dict[str, str]:
        """L71 : `{agent: harnais}` pour tout le registre (comptabilité, jauges)."""

    @abc.abstractmethod
    def overview(self) -> list[dict]:
        """Vue d'observabilité (`mesh list --json`), colonnes du canon comprises."""

    @abc.abstractmethod
    def reachability(self) -> list[dict]:
        """#3631 : les seules colonnes qui disent si un relecteur désigné lira
        son courrier — `name`, `aliases`, `retired_ts`, `mode`, `status`,
        `status_text` (#4491, préfixe `auth_blocked`), `host`.

        La passe de redésignation lit ce fait à chaque synchronisation de
        canon sur chaque exécuteur : `overview()` y joignait le canon, les
        baux et des colonnes larges, et expirait sous la pression de la base
        commune (639 passes perdues en 24 h). Aucune décision de sélection
        n'est prise ici, seulement la preuve de joignabilité."""

    @abc.abstractmethod
    def stopped_with(self, reasons: Sequence[str]) -> list[str]:
        """#3907 : les noms des agents `stopped` dont `stop_reason` est dans
        `reasons`, triés ; aucune raison, aucune lecture.

        Le balayage des agents arrêtés (#753) tourne à chaque passe de chaque
        exécuteur, et une passe suit chaque NOTIFY du mesh : `overview()` y
        coûtait 0,4 à 1,2 s et saturait la base commune (27/10, charge 44 sur
        4 vCPU). Le filtre ne lit que deux colonnes du registre."""

    @abc.abstractmethod
    def claimable(self, host: str, names: Sequence[str] | None, *,
                  require_responsible: bool) -> list[dict]:
        """Agents de l'hôte sans bail vivant, réclamables (mêmes règles que `claim`).

        L165 (EP3) : pour un agent `releve: election` rendu sur un hôte
        candidat, `host` est l'hôte qui réclame et `previous_host` l'hôte de la
        ligne (l'ancien détenteur, ou l'hôte de la dernière synchronisation) ;
        `session_id` est encore la session de cet ancien détenteur."""

    @abc.abstractmethod
    def runner_seen(self, host: str, owner: str, names: Sequence[str] | None) -> None:
        """Une écriture groupée, à l'heure de la base, pour les personas execute
        placées sur host, limitées par names si fourni. Aucun test de bail ou
        d'éligibilité. Ne modifie ni placement, ni statut, ni last_seen."""

    @abc.abstractmethod
    def note_unleased(self, name: str, host: str, error: str) -> bool:
        """Native runner's lease_not_taken diagnostic, only on its host and
        without a live lease, after taking the row lock. Keeps agent status.
        Cleared by a successful claim; refused to mediated executors."""

    @abc.abstractmethod
    def mark_event_wake(self, name: str) -> None:
        """Note l'instant du dernier réveil d'événements."""

    @abc.abstractmethod
    def set_session(self, name: str, session_id: str, account: str | None = None, *,
                    harness: str | None = None, cwd: str | None = None) -> None:
        """Enregistre la session du harnais et le compte sous lequel elle tourne
        (`session_account`, L39 ; None : compte inconnu ou aucun compte déclaré).
        #408 : `harness`/`cwd` donnés, ceux de son ouverture (`session_origin`
        = la session) ; sinon son origine est inconnue."""

    @abc.abstractmethod
    def set_session_account(self, name: str, account: str | None) -> bool:
        """Change le compte de la session courante (reprise portable sous un
        autre compte, L39) ; faux si l'agent est inconnu ou sans session."""

    @abc.abstractmethod
    def cwd_used(self, cwd: str, exclude: str) -> bool:
        """Ce dossier est-il celui d'un autre agent que `exclude` ?"""

    @abc.abstractmethod
    def set_status(self, name: str, status: str, status_text: str | None,
                   error: str | None, stop_reason: str | None = None) -> None:
        """Pose le statut (et l'erreur) de l'agent ; `stop_reason` (L37) n'est
        gardé que pour `stopped` / `dead`."""

    def set_status_text(self, name: str, text: str) -> bool:
        """#2789 : le texte libre de `agent-mail status`, en une instruction,
        SANS toucher au statut : `status`, `last_error` et `stop_reason`
        restent ceux de l'exécuteur (un tour en cours reste `running`). Écrit
        seulement sur un agent `idle`, `queued` ou `running` : le texte d'un
        agent `blocked`, `stopped` ou `dead` est un marqueur (retenue, arrêt)
        que sa levée relit à l'identique. Faux si rien n'est écrit.

        Pas une opération du contrat médié : un statut libre n'est pas admis
        depuis une session médiée (`MediatedBackend.set_status`)."""
        raise NotImplementedError

    @abc.abstractmethod
    def set_mode(self, name: str, mode: str) -> bool:
        """Pose le mode (`execute` | `externe`, L37) ; faux si l'agent est inconnu."""

    @abc.abstractmethod
    def wake_check(self, name: str) -> dict | None:
        """Ce qui dit si l'agent est réveillable (L37) : `mode`, `status`,
        `responsible`, `alive` (éphémère non échu), `canon_claim_ok` (condition
        de réclamation du canon), `placement_admitted` (L46 : admission seule,
        sans l'état du canon), `canon_status`, `placement_diagnostic` ; None si
        inconnu."""

    @abc.abstractmethod
    def set_pending_prompt(self, name: str, prompt: str | None) -> None:
        """Pose la consigne en attente (statut `queued` si elle n'est pas nulle)."""

    @abc.abstractmethod
    def clear_model(self, name: str) -> None:
        """Efface le modèle de l'agent (retour au défaut du harnais, `ameesh set`)."""

    @abc.abstractmethod
    def count(self) -> int:
        """Nombre d'agents inscrits (`doctor`)."""


class Leases(Domain):
    """Baux : réclamation atomique, fencing par `lease_epoch`.

    Toute opération qui écrit sous un bail prend le verrou de la ligne
    D'ABORD, puis recontrôle l'échéance avec l'heure réelle de la base
    (`clock_timestamp()`) dans l'instruction qui écrit, hors du WHERE
    verrouillant : un verrou attendu au-delà de l'échéance refuse l'écriture.
    """

    @abc.abstractmethod
    def claim(self, name: str, owner: str, ttl_seconds: float, *,
              require_responsible: bool, host: str | None = None,
              election_brief: str | None = None) -> dict | None:
        """Prend le bail s'il est libre ou expiré ; rend la ligne, sinon None.

        `host` (L165, EP1) : l'hôte qui réclame, pour un agent
        `releve: election`. None : l'hôte de la ligne (comportement d'un agent
        épinglé). Le serveur médié force cet argument à l'hôte de l'exécuteur.

        `election_brief` (L165, EP3) : appliqué seulement sur une relève
        croisée d'un agent élu (la ligne était chez un autre hôte) — la même
        instruction oublie la session de l'ancien hôte, laisse son compte au
        choix de cet hôte et écrit le brief comme résumé de la session neuve.
        Ignoré sur le même hôte et pour un agent `hote`. Une relève croisée
        SANS brief est **refusée** (le CAS ne déplace pas la ligne), cas d'un
        exécuteur médié (contrat 1.9 : valeur forcée à null côté appareil)."""

    @abc.abstractmethod
    def attach_claim(self, name: str, owner: str, ttl_seconds: float, *,
                     require_responsible: bool) -> dict | None:
        """Prend le bail pour une session interactive (`ameesh attach`) ; la
        ligne rendue porte `harness` et `created_ts` (L719), l'identité de
        l'enregistrement revendiqué."""

    @abc.abstractmethod
    def state(self, name: str) -> dict | None:
        """Nom primaire résolu, détenteur, epoch, statut et vie du bail
        (`live`), ou None."""

    @abc.abstractmethod
    def renew(self, name: str, owner: str, epoch: int, ttl_seconds: float) -> float | None:
        """Prolonge le bail sans raccourcir son échéance ; rend la nouvelle
        échéance, None si on ne le détient plus."""

    @abc.abstractmethod
    def release(self, name: str, owner: str, epoch: int) -> bool:
        """Rend le bail ; l'epoch avance."""

    @abc.abstractmethod
    def turn_in_progress(self, name: str) -> bool:
        """`status = 'running'` avec un bail vivant ?"""

    @abc.abstractmethod
    def reap(self, host: str | None) -> list[dict]:
        """Marque morts les agents dont le bail a expiré en plein tour."""

    @abc.abstractmethod
    def clear_session(self, name: str, owner: str, epoch: int) -> bool:
        """Oublie la session, fencé par un bail valide ; l'oubli est durable
        (`session_reset_at` posé dans la même écriture), pour qu'un fichier
        local non retiré ne ressuscite pas la session (#794)."""

    @abc.abstractmethod
    def adopt_session_origin(self, name: str, owner: str, epoch: int, session: str,
                             lot: str, *, harness: str | None, cwd: str | None) -> bool:
        """#1900 B1 : note l'origine d'une session sans origine, sous un bail
        VIVANT (owner, epoch, échéance recontrôlée après l'attente du verrou)
        et seulement si la ligne porte encore `session` et `lot` : une session
        remplacée entre-temps (rotation, remplaçant, autre lot) n'est jamais
        réécrite. N'écrit que l'origine, le dossier et le harnais — la session
        et son compte restent ceux de la ligne. Faux sinon."""

    @abc.abstractmethod
    def hold_note(self, name: str, owner: str, epoch: int, status_text: str) -> bool:
        """Dit pourquoi un tour attend (L31b), sans changer le statut : le
        texte de statut est posé sous un bail VIVANT détenu par `owner`/`epoch`,
        seulement si l'agent est `idle` ou `queued`. Faux sinon."""

    @abc.abstractmethod
    def release_hold(self, name: str, owner: str, epoch: int, status_text: str,
                     restore: str = "") -> bool:
        """Lève une attente ou une pause de pression (L31b) : seulement si le
        texte de statut est toujours `status_text` (personne ne l'a remplacé),
        sous un bail vivant. Une pause (`blocked`) repasse `queued` ou `idle`
        selon la consigne en attente ; le texte redevient `restore`."""

    @abc.abstractmethod
    def pause(self, name: str, owner: str, epoch: int, status_text: str) -> bool:
        """Met l'agent en pause (`blocked`) sous un bail VIVANT détenu par
        `owner`/`epoch` (L31, 0028), et refuse un tour en cours ou un agent
        arrêté. Verrou de ligne d'abord, conditions recontrôlées dans
        l'écriture. Faux si le bail n'est plus le nôtre ou l'état incompatible.
        """

    @abc.abstractmethod
    def set_marked_block(self, name: str, owner: str, epoch: int, status_text: str,
                         error: str, error_prefix: str) -> str:
        """Pose un blocage MARQUÉ (L35 : « dossier absent ») sous un bail vivant.

        Verrou de ligne d'abord, puis, dans l'écriture : bail détenu par
        `owner`/`epoch` et non échu (`clock_timestamp()`), agent ni arrêté ni
        déjà bloqué pour une AUTRE raison (budget, pression… : un blocage
        marqué `status_text` + `error_prefix` est le seul remplaçable). Rend `"done"`
        (posé), `"kept"` (bail vivant, statut concurrent préservé) ou
        `"lease"` (bail perdu, remplacé ou échu : rien n'est écrit)."""

    @abc.abstractmethod
    def clear_marked_block(self, name: str, owner: str, epoch: int, status_text: str,
                           error_prefix: str) -> str:
        """Lève le blocage marqué (`blocked` + `status_text` exact + `last_error`
        commençant par `error_prefix`) sous un bail vivant (L35) : `queued`
        s'il reste une consigne, sinon `idle`. Rend `"done"`, `"kept"` (bail
        vivant mais ce blocage n'est plus là : arrêt, autre blocage, déjà
        levé — rien n'est écrasé) ou `"lease"` (rien n'est écrit)."""

    @abc.abstractmethod
    def take_pending_prompt(self, name: str, owner: str, epoch: int) -> str | None:
        """Consomme la consigne en attente et passe l'agent en `running` ;
        refusé (None) sur un agent `stopped` (L35)."""

    @abc.abstractmethod
    def begin_turn(self, name: str, owner: str, epoch: int, status_text: str) -> bool:
        """Passe l'agent en `running` sous un bail vivant ; refusé sur un agent
        `stopped` (L35)."""

    @abc.abstractmethod
    def restore_prompt(self, name: str, owner: str, epoch: int) -> bool:
        """Remet en attente la consigne d'un tour qui n'a pas abouti."""

    @abc.abstractmethod
    def end_turn(self, name: str, owner: str, epoch: int, *, status: str,
                 status_text: str | None, error: str | None,
                 cost_usd: float | None) -> bool:
        """Clôt un tour seulement si le bail est encore détenu."""


class Catalog(Domain):
    """Le catalogue des modèles (L14) : deux tables, quatre opérations.

    `reconcile` vit dans `ameesh.catalog` — c'est une décision, pas du SQL. Ici,
    seulement les opérations : ce qu'on voit, ce qui est actif, ce qu'on retire.
    """

    @abc.abstractmethod
    def upsert_models(self, models: Sequence[dict]) -> int:
        """Enregistre des modèles vus : `last_seen` avance, un retrait est effacé."""

    @abc.abstractmethod
    def upsert_harnesses(self, harnesses: Sequence[dict]) -> int:
        """Enregistre des couples modèle × harnais, même règle de réapparition."""

    @abc.abstractmethod
    def update_prices(self, models: Sequence[dict]) -> int:
        """Met à jour prix et fenêtre de contexte SEULEMENT.

        Ni la source, ni `retired_at` : un barème rafraîchi n'est pas une
        réapparition, et il ne doit pas voler la propriété d'une ligne à son
        fournisseur (revue B2).
        """

    @abc.abstractmethod
    def known(self, keys: Sequence[tuple[str, str]]) -> list[dict]:
        """L'état antérieur de ces (fournisseur, modèle), TOUTES sources et états.

        C'est ce que la comparaison de prix demande : un barème qui change le prix
        d'une ligne détenue par un fournisseur doit la comparer à ce qu'elle était,
        pas à ce que la source `prices` possédait (revue, cas croisé).
        """

    @abc.abstractmethod
    def active(self, source: str) -> list[dict]:
        """Les lignes actives d'une source : propriété, prix et contexte."""

    @abc.abstractmethod
    def retire(self, models: Sequence[dict]) -> int:
        """Marque des modèles comme retirés (jamais une suppression)."""

    @abc.abstractmethod
    def listing(self, *, harness: str | None = None) -> list[dict]:
        """Ce que la CLI `models list` montre : catalogue et harnais joints."""

    @abc.abstractmethod
    def show(self, needle: str) -> list[dict]:
        """Les lignes qui portent ce modèle, tous fournisseurs confondus."""


class Veille(Domain):
    """Le relevé précédent de la veille des versions (#4291, migration 0512).

    La présence au catalogue partagé n'en est pas un : `models discover` y
    écrit avant la veille. Ce domaine est la référence durable propre à la
    veille, par harnais et fournisseur.
    """

    @abc.abstractmethod
    def readings(self, harness: str, provider: str) -> list[str]:
        """Les modèles déjà vus par la veille pour ce harnais et fournisseur."""

    @abc.abstractmethod
    def record_readings(self, harness: str, provider: str,
                        model_ids: Sequence[str]) -> int:
        """Enregistre le relevé courant : une ligne par modèle, `last_seen` avance."""


class PendingSpend(Domain):
    """Marqueur comptable d'un tour (`spend_pending`) : une ligne par agent.

    Le marqueur porte son identité **exacte** (#2166) : `created_at`, texte
    UTC à la microseconde, et `spend_key`, la clé du grand livre
    (`turn_costs.spend_key`, L60) et de la réservation #1469. Une écriture
    conditionnelle s'y compare, jamais à un `extract(epoch)::float8` relu —
    une connexion à précision réduite (`extra_float_digits=0`) en perd les
    derniers chiffres. `spend_key` est NULL sur les marqueurs d'avant 0471 :
    leur réparation retrouve la clé déjà écrite par leur tour (ligne ou
    réservation) par l'identité du tour, jamais par un float8 recalculé ni
    par l'ordre des écritures ; plusieurs candidats incohérents suspendent
    (`ledger_ambiguous`).

    #2905 : la colonne `spend_key` n'est crue que si elle est l'identité
    exacte du marqueur. Après un retour arrière de version (§14), l'ancien
    `put` réécrit cette identité sans la clé : la colonne garde alors celle du
    tour précédent, que `get` traite comme NULL (repli par l'identité du tour)
    — l'écrire ferait perdre le coût du tour au conflit de
    `turn_costs.spend_key`.

    Extensions PostgreSQL optionnelles (#1469), détectées par `hasattr` :
    `put_get(name, start_index, turn, model)` écrit et rend le marqueur comme
    `get`, dans la même instruction ; `clear_if(name, created_at, spend_key)`
    ne retire que ce marqueur. Absentes de l'interface distante : elle garde
    les opérations de base, avec le contrôle du bail côté serveur.
    """

    @abc.abstractmethod
    def put(self, name: str, start_index: int, turn: str | None, model: str | None,
            work_item_id: int | None = None, turn_id: str | None = None) -> bool:
        """Pose (atomiquement) le marqueur du tour, en remplaçant le précédent.
        `work_item_id` (#796, migration 0423) : le lot du tour, pour que la
        ligne écrite à la réparation — par le même exécuteur ou un autre —
        garde l'attribution ; None = lot inconnu.
        `turn_id` (#796) : l'identifiant du tour (`turn_resources.turn_id`),
        pour la même raison ; None = tour inconnu."""

    @abc.abstractmethod
    def set_model(self, name: str, model: str | None) -> bool:
        """Remplace le modèle du marqueur ; faux s'il n'y a pas de marqueur."""

    @abc.abstractmethod
    def set_resident_usage(self, name: str, usage: dict | None,
                           created_at: str | None = None,
                           spend_key: str | None = None) -> bool:
        """Pose (ou efface avec None) l'événement `resident.turn` du marqueur
        (L80a) ; faux s'il n'y a pas de marqueur, ou si `created_at`/`spend_key`
        désignent un autre marqueur. `created_at` est le texte exact rendu par
        `get`/`put_get` (#2166), jamais un float8 relu. Cet état durable permet
        à un worker de remplacement de réparer un tour résident dont
        l'événement n'a pas été écrit."""

    @abc.abstractmethod
    def get(self, name: str) -> dict | None:
        """Le marqueur (agent, start_index, turn, model, created_at,
        created_ts, spend_key, ledger_key, ledger_ambiguous, work_item_id,
        turn_id, resident_usage), ou None. `created_at` est l'identité exacte
        (#2166) ; `spend_key` est l'identité brute portée par la colonne (NULL
        avant 0471 ; elle borne `clear_if`/`set_resident_usage` et peut être
        périmée après un retour arrière, #2905) ; `ledger_key` est la clé fiable
        déjà écrite par ce tour — celle du marqueur seulement si la colonne EST
        son identité (`agent:index:created_at`, le format de `put`), ou celle
        l'identité du tour (`turn_id`, instant de pose, époque de la clé égale
        à un arrondi historique exact du marqueur, jamais une fenêtre), et la
        ligne/réservation doit suivre le marqueur — jamais par le seul index
        de début ni par ordre temporel ;
        `ledger_ambiguous` vrai interdit de deviner et suspend la comptabilité
        (B1, notamment une réservation étrangère au même index) ;
        `created_ts` reste l'epoch float8 pour le début du tour
        (`turn_costs.started_at`)."""

    @abc.abstractmethod
    def clear(self, name: str) -> bool:
        """Retire le marqueur ; faux s'il n'y en avait pas."""


# --------------------------------------------------------------------------
# grand livre des coûts (`turn_costs`, L12/L13)
# --------------------------------------------------------------------------

class TurnCosts(Domain):
    """Une ligne par tour compté : son coût ET le cumul brut du harnais, qui
    sert de repère au tour suivant. Insertion et repère sont la même écriture
    (une seule instruction) : un tour rejoué après une insertion en échec
    retrouve le même repère. L'état de pause comptable vit ailleurs
    (`PendingSpend`) ; ce domaine n'ouvre aucune transaction."""

    @abc.abstractmethod
    def last_reading(self, agent: str | None, harness: str,
                     session: str | None = None) -> dict | None:
        """Dernier relevé CONNU (cumul en dollars ou en jetons non nul) de
        l'agent pour ce harnais — de cette session seulement si `session` est
        donnée : session, cum_usd, cum_input_tokens, cum_cached_input_tokens,
        cum_output_tokens ; ou None. `agent=None` (L71) : tout agent."""

    @abc.abstractmethod
    def last_before(self, agent: str, ts: float) -> dict | None:
        """#1777 : la dernière ligne écrite de `agent` à `ts` (epoch) ou avant :
        `harness`, `session`, `recorded_ts` (epoch) ; ou None. Lecture seule,
        pour relire le harnais qui tournait avant un instant donné (le gel d'un
        lot)."""

    @abc.abstractmethod
    def session_total(self, agent: str, session: str) -> dict | None:
        """#4448 : la mesure de rotation d'une session — somme des jetons
        (`input_tokens + output_tokens`, cache compris quand le harnais le
        compte dans l'entrée) des tours de `agent` pour `session`, leur
        nombre, le harnais du dernier tour et son instant : `tokens`, `turns`,
        `harness`, `last_ts` (epoch) ; ou None si aucun tour. `turns` compte
        **tous** les tours inscrits au grand livre, réussis ou non — la
        définition du compteur vivant de l'exécuteur (#4448 B), et non les
        seuls succès. C'est la même mesure que ce compteur, relue pour
        l'alerte `session_too_big`."""

    @abc.abstractmethod
    def first_after(self, agent: str, ts: float) -> dict | None:
        """#1777 : la première ligne écrite de `agent` à `ts` (epoch) ou après :
        `harness`, `session`, `recorded_ts` (epoch) ; ou None. `cost.record`
        écrit la ligne d'un tour à sa fin : le tour qui a posé un gel est le
        premier écrit APRÈS l'instant du gel. Lecture seule."""

    @abc.abstractmethod
    def work_item_of_turn(self, turn_id: str) -> int | None:
        """#4679 : le lot que le grand livre attribue au tour `turn_id`, ou None.

        Un reçu de commit du hook post-commit ne connaît pas son lot
        (`work_item_id` NULL), mais la ligne `turn_costs` du tour le porte. La
        lecture de provenance s'en sert pour ne pas exclure un relecteur sur un
        reçu d'un autre lot. Aucune ligne, identifiant vide, ou plusieurs lots
        distincts pour le même tour : None — un doute, jamais une attribution
        devinée."""
        raise NotImplementedError

    @abc.abstractmethod
    def insert(self, *, agent: str, harness: str, turn: str | None, model: str | None,
               session: str | None, usd: float, input_tokens: int,
               cached_input_tokens: int, output_tokens: int, cum_usd: float | None,
               cum_input_tokens: int | None, cum_cached_input_tokens: int | None,
               cum_output_tokens: int | None, account: str | None = None,
               spend_key: str | None = None, source: str | None = None,
               executor: str | None = None, lease_owner: str | None = None,
               lease_epoch: int | None = None, void_reason: str | None = None,
               price_fallback: str | None = None,
               cache_write_tokens: int | None = None,
               work_item_id: int | None = None,
               started_at: float | None = None,
               turn_id: str | None = None,
               inputs: Sequence[str] | None = None) -> bool:
        """Écrit la ligne du tour (une instruction) ; une erreur de base remonte
        telle quelle et n'a rien écrit. `account` (L30, migration 0028) : le
        compte qui a porté le tour ; None = colonne non écrite. `spend_key`
        (L60, migration 0041) : clé du marqueur comptable ; une ligne portant
        déjà cette clé n'est pas réécrite (faux). `void_reason` (L225,
        migration 0043) : la ligne naît écartée des sommes ; `price_fallback`
        (L225, migration 0059) : tour facturé au repli prudent ;
        `cache_write_tokens` (L225, 0059) : écritures de cache Claude du tour ;
        None = colonne non écrite. Vrai si la ligne est écrite.
        `source`, `executor`, `lease_owner`, `lease_epoch` (L111, migration
        0064) : `relay` pour la mesure du relais de modèle, `device` pour la
        déclaration d'un appareil médié (hors plafond) ; None = colonnes non
        écrites (`source` vaut alors `harness`).
        `work_item_id` et `started_at` (#796, migration 0423) : le lot servi par
        le tour et son instant de début (epoch, horloge de la base) ; None =
        inconnu, colonne non écrite. `turn_id` (#796) : l'identifiant du tour
        (`turn_resources.turn_id`), pour relier la ligne à ses ressources ;
        None = inconnu. `inputs` (L80b, migration 0472) : les identifiants des
        messages servis par le tour (JSON array) ; None = colonne non écrite."""

    @abc.abstractmethod
    def spent(self, seconds: float, *, agent: str,
              harnesses: Sequence[str] | None, account: str | None = None) -> float:
        """Somme des coûts des `seconds` dernières secondes (horloge de la
        base), de l'agent (`"all"` : tout le compte, comme `cost spent`), de
        ces harnais seulement si donnés, de ce compte seulement si donné (L30).
        Les lignes `source = 'device'` (L111) ne comptent jamais : la dépense
        d'un appareil médié est celle que le relais a mesurée."""

    @abc.abstractmethod
    def spent_between(self, from_ts: float, to_ts: float, *,
                      harnesses: Sequence[str]) -> float:
        """L95 : somme des coûts de ces harnais dont la ligne est écrite dans
        ]from_ts, to_ts] (epoch) ; les lignes écartées (0043) ne comptent pas.
        Sert à comparer l'estimation à la baisse réelle d'un solde sur le même
        intervalle."""

    @abc.abstractmethod
    def paid_lines(self, from_ts: float, *, harnesses: Sequence[str]) -> list[dict]:
        """L242b : les lignes de ces harnais écrites après `from_ts` (epoch),
        hors lignes écartées : `{agent, harness, account, ts, usd}`. Sert à
        rapprocher l'estimation des baisses de solde compte par compte et
        intervalle par intervalle (`CostBook.paid_spent`)."""

    @abc.abstractmethod
    def models_used(self, harness: str, from_ts: float, *, limit: int = 50) -> list[dict]:
        """#4291 : les modèles distincts réellement facturés pour ce harnais
        depuis `from_ts` (epoch), hors lignes écartées et hors appareils
        médiés : `{model, turns, first_ts, last_ts}`, du plus récent au plus
        ancien. Lecture seule, bornée par `limit`."""

    @abc.abstractmethod
    def unpriced(self, since_ts: float, prefix: str) -> list[dict]:
        """L225 : tours écrits depuis `since_ts` (epoch) facturés au repli
        prudent (`price_fallback`) ou écartés faute de barème (`void_reason`
        commençant par `prefix`), regroupés par agent, harnais et modèle :
        agent, harness, model, turns, voided (tours écartés), usd (des tours
        gardés), input_tokens, cached_input_tokens, output_tokens, first_ts,
        last_ts. Lecture seule."""

    @abc.abstractmethod
    def ledger(self, *, since_ts: float | None = None) -> list[dict]:
        """L95 : les lignes du grand livre (depuis `since_ts` si donné), dans
        l'ordre d'écriture (`recorded_ts`, `id`) : id, agent, harness, turn,
        model, session, account, usd, input_tokens, cached_input_tokens,
        output_tokens, cum_*, recorded_ts, et `void_reason` (None si la
        migration 0043 n'est pas passée). Lecture seule."""

    @abc.abstractmethod
    def correct(self, *, run_id: str, actor: str, corrections: Sequence[dict]) -> int:
        """L95 (migration 0043) : applique des corrections en UNE transaction.
        Chacune `{id, kind, reason, set}` ; `set` ne nomme que `usd`, `model`,
        `input_tokens`, `cached_input_tokens`, `output_tokens` ou
        `void_reason`. La copie complète de la ligne d'avant va d'abord au
        journal `turn_cost_corrections`, puis la ligne est mise à jour ; une
        ligne déjà écartée n'est pas touchée. Rien n'est supprimé. Rend le
        nombre de lignes corrigées."""


# --------------------------------------------------------------------------
# plafonds de budget du mesh (L70, décision 0019 §2, migration 0042)
# --------------------------------------------------------------------------

class Budgets(Domain):
    """Plafonds de budget réglés en base, pour tout le mesh, et leur journal.

    Une ligne par (portée, fenêtre) : portée `''` = tout le mesh, sinon un
    agent ; fenêtre 3600 ou 86400 s. Les changements sont journalisés dans
    la même transaction (acteur, ancienne et nouvelle valeur) ; un
    déclencheur réveille les exécuteurs (canal `ameesh_budget`)."""

    @abc.abstractmethod
    def limits(self) -> list[dict]:
        """Toutes les lignes : scope, window_s, usd, set_by, updated_ts."""

    @abc.abstractmethod
    def put(self, scope: str, window_s: int, usd: float | None, *,
            actor: str, lower_only: bool = False,
            ceiling: float | None = None) -> tuple[bool, float | None]:
        """Pose (`usd` > 0) ou retire (`usd` None) un plafond, et journalise,
        en UNE transaction (verrou de la ligne). Rend (changé, ancienne
        valeur) ; rien n'est écrit ni journalisé si la valeur est la même.

        `lower_only` (L87, `POST /api/v1/budget/caps`) : ValueError, rien
        d'écrit, sauf si `usd` est strictement sous le plafond en vigueur,
        lu sous le verrou — la ligne en place, sinon `ceiling` (celui de la
        configuration ; None : aucun plafond)."""

    @abc.abstractmethod
    def events(self, limit: int, scope: str | None = None) -> list[dict]:
        """Les derniers changements, récents d'abord : scope, window_s,
        old_usd, new_usd, actor, at_ts."""


# --------------------------------------------------------------------------
# API idempotency (L87, migration 0440)
# --------------------------------------------------------------------------

class AlertAcknowledgements(Domain):
    """L87c: native operator acknowledgements; expired rows retain the audit."""

    @abc.abstractmethod
    def live(self) -> list[dict]:
        """Active rows: id, alert_key, severity, observation_host, perimeter,
        actor, principal, acknowledged_ts."""

    @abc.abstractmethod
    def acknowledge(self, key: str, severity: str, actor: str, principal: str, *,
                    observation_host: str | None = None,
                    perimeter: str | None = None) -> dict:
        """Atomically acknowledge once per key/principal/severity; row + changed.

        `perimeter` (#1473): canonical thresholds of the observation that
        raised the alert; NULL when unknown (row predating the column)."""

    @abc.abstractmethod
    def invalidate(self, id: int, *, reason: str, observed_ts: float) -> bool:
        """Expire a row predating the observation, retaining actor/principal."""

    @abc.abstractmethod
    def clock(self) -> float:
        """Database clock before collecting the snapshot, epoch seconds."""


class ApiRequests(Domain):
    """The `Idempotency-Key` of the API's actions (`ameesh serve`): one row
    per (human, key), reserved before the action runs, completed with its
    response once it has answered."""

    @abc.abstractmethod
    def reserve(self, human: str, key: str, fingerprint: str, *,
                retention_s: int) -> dict | None:
        """Reserves (human, key) for the request `fingerprint`, in ONE
        transaction, after purging the rows older than `retention_s` (a purge
        that never waits on a locked row, #1497 N3). None when
        the key is new (the caller acts); otherwise the row in place:
        fingerprint, status, response (status and response None while the
        first request is still running)."""

    @abc.abstractmethod
    def complete(self, human: str, key: str, status: int, response: dict) -> bool:
        """Stores the response of a reserved key; False if the reservation is
        gone or already completed (nothing written)."""

    @abc.abstractmethod
    def release(self, human: str, key: str) -> bool:
        """Drops a reservation that was never completed (transient failure:
        the client may retry with the same key); False if there was none."""

    @abc.abstractmethod
    def retake(self, human: str, key: str, older_than_s: float) -> bool:
        """Takes over a reservation left unanswered for `older_than_s`: its
        reservation instant becomes now, in one statement, so one caller only
        wins (§16, #3889: an outcome left unknown is reconciled); False if
        the row is answered, more recent, absent or taken by another."""


# --------------------------------------------------------------------------
# watches managed by ameesh (L121, migration 0362)
# --------------------------------------------------------------------------

class Watches(Domain):
    """The periodic watches of agents (`ameesh watch`): one row per watched
    agent, with the host that holds its timer."""

    @abc.abstractmethod
    def put(self, agent: str, every_s: int, host: str, *, actor: str) -> dict:
        """Records (or moves) the watch of `agent`; the row written: agent,
        every_s, host, enabled_by, enabled_ts, last_sent_ts."""

    @abc.abstractmethod
    def delete(self, agent: str, host: str | None = None) -> dict | None:
        """Removes the watch of `agent` (only if it is on `host`, when
        given: a watch moved meanwhile stays); the row removed, None if none."""

    @abc.abstractmethod
    def list(self, agent: str | None = None) -> list[dict]:
        """The watches (of `agent` only if given), by agent name."""

    @abc.abstractmethod
    def deliver(self, agent: str, host: str, send: Callable[[Any], None]) -> dict | None:
        """One firing, in one transaction: the watch row of `agent` is locked;
        if it is on `host`, `send(tx)` drops the message in that transaction
        and `last_sent_ts` is set. A move or
        a disable from another host waits for the end of the firing. The row
        read (with `sent` true or false), None if there is no watch; `send`
        raising rolls the transaction back."""


# --------------------------------------------------------------------------
# event log of the mesh (L85, migration 0396)
# --------------------------------------------------------------------------

class Events(Domain):
    """The append-only `ameesh_events` log, source of `GET /api/v1/events`.

    The rows are written by triggers on the tables they describe (same
    transaction as the change); `record` is for what has no table (an alert
    raised or resolved). A row: id, type, object, project, data, ts."""

    @abc.abstractmethod
    def record(self, type: str, object: str, project: str | None, data: dict) -> int:
        """Appends one event; its id. Without `project`, the project of
        `data["agent"]` if any."""

    @abc.abstractmethod
    def after(self, after_id: int, limit: int) -> list[dict]:
        """The events of id greater than `after_id`, by increasing id, at
        most `limit` (a listing for humans: ids are not in commit order)."""

    @abc.abstractmethod
    def recent_type(self, type: str, days: int, limit: int,
                    object: str | None = None) -> list[dict]:
        """The most recent events of `type` (optionally about `object`) written
        in the last `days`, newest first, at most `limit` (#3981: the shared
        staging attestation). The `created_at` bound keeps the read on the
        retention index; `days` is the durability the caller needs, never a
        substitute for an absent event."""

    def latest_type(self, type: str, days: int, limit: int) -> list[dict]:
        """Newest snapshot per object, newest first, bounded by objects.

        Unlike recent_type, a chatty object cannot hide an interrupted one.
        A driver without this capability must refuse instead of truncating.
        """
        raise NotImplementedError("latest event per object is unavailable")

    @abc.abstractmethod
    def readable(self, position: tuple, limit: int) -> tuple[list[dict], str | None, tuple]:
        """The reader's order: (the events after `position` = (tx, id) whose
        transaction is over for good (`tx` below the snapshot's xmin), by
        (tx, id), at most `limit`, each row also carrying `tx`; why events
        after `position` wait, None if none does: "xmin" for an older open
        transaction, "restore" for rows of another cluster above the counter;
        the retention's watermark `(tx, id)`, `(0, 0)` when it never deleted).
        All from ONE snapshot. Such a transaction can no longer add an event
        before the last one read; a `position` below the watermark returned
        with it may have lost events (reset)."""

    @abc.abstractmethod
    def bounds(self) -> dict:
        """`first`, `last`: the smallest and largest ids kept (None and 0 when
        empty); `head`: the position (tx, id) of the last readable event,
        `top`: of the last event written, `oldest`: of the first one kept
        ((0, 0) when none); `pruned`: the greatest position the retention
        deleted ((0, 0) when none); `cluster`: the identity of the journal
        (cluster, timeline and the log's own random identifier), outside which
        a position means nothing (None when its functions are refused)."""

    @abc.abstractmethod
    def count_before(self, days: int) -> int:
        """The number of events older than `days`, on the database's clock."""

    @abc.abstractmethod
    def prune(self, days: int, limit: int, timeout_s: float | None = None) -> int:
        """Deletes at most `limit` events older than `days` (the oldest first,
        on the database's clock) and raises the watermark
        `bounds()["pruned"]` in the same statement; the number deleted.
        `timeout_s`: lock and statement timeout of the deletion (DbError past
        it). The table refuses an event younger than 30 days."""


# --------------------------------------------------------------------------
# comptes multiples par fournisseur (L30, décision 0027, migration 0028)
# --------------------------------------------------------------------------

class Accounts(Domain):
    """Compte actif par (hôte, harnais), retenues et journal des bascules.

    Les profils (dossiers de configuration, clés) sont des secrets d'hôte : ils
    ne passent jamais par ce domaine, qui ne voit que des NOMS de comptes."""

    @abc.abstractmethod
    def active(self, host: str, harness: str) -> dict | None:
        """La ligne `account_active` (account, forced, forced_until,
        updated_at), ou None. `forced_until` (epoch, #3755) est l'échéance du
        forçage manuel ; None = pas de forçage, ou ligne forcée posée hors
        `ameesh accounts use` sans échéance. `updated_ts` (epoch) date la
        dernière écriture."""

    @abc.abstractmethod
    def init_active(self, host: str, harness: str, account: str) -> dict:
        """Pose le compte actif s'il n'y en a pas encore ; rend la ligne en place."""

    @abc.abstractmethod
    def switch(self, host: str, harness: str, *, expected: str | None, to: str,
               kind: str, reason: str, agent: str | None, forced: bool = False,
               forced_until: float | None = None) -> bool:
        """Change le compte actif ET journalise la bascule, en UNE instruction.

        Comparer-et-changer : `expected` est le compte que l'appelant a vu
        actif (None = n'importe lequel, forçage manuel). Une bascule
        automatique (`forced` faux) ne touche jamais un compte forcé. Faux si
        un autre worker a déjà basculé : rien n'est écrit.

        #3755 : `forced_until` (epoch) pose l'échéance d'un forçage manuel ;
        toute bascule non forcée efface l'échéance."""

    @abc.abstractmethod
    def force_switch(self, host: str, harness: str, *, initial: str, to: str,
                     reason: str, agent: str | None, forced_until: float,
                     now: float, allow: bool = False) -> dict:
        """#3755 : bascule forcée sous le **verrou de placement**.

        Une seule transaction qui, sous `_verrou_placement` (le verrou de
        `hold`, `switch_guarded` et `placement_state`) :

        * relit la retenue de `to` (`account_holds`) ; une retenue **valide**
          (`until_ts > now`) refuse la bascule sans `allow` — rend
          `{"changed": False, "held": raison}`, rien n'est écrit (pas même la
          levée de la retenue). C'est la course fermée : une retenue publiée
          entre la lecture des jauges et la bascule est vue ici, jamais
          ignorée puis effacée ;
        * avec `allow` (`--force "raison"`), la bascule passe et la retenue
          de `to` est levée dans la même transaction (geste explicite) ;
        * sans retenue valide, la bascule passe et une retenue échue de `to`
          est nettoyée.

        `initial` est le compte à journaliser comme quitté quand
        `account_active` n'a pas encore de ligne (premier choix de l'hôte).
        Rend `{"changed": bool, "held": str | None}` ; `held` n'est renseigné
        que si la bascule a été refusée pour une retenue valide."""

    @abc.abstractmethod
    def switch_guarded(self, host: str, harness: str, *, expected: str | None, to: str,
                       kind: str, reason: str, agent: str | None, now: float,
                       hold_reason: str | None = None,
                       hold_until: float | None = None) -> bool:
        """#1883 : bascule validée contre les retenues, en UNE transaction.

        Même contrat que `switch` (compare-and-changer `expected`, un forçage
        manuel n'est pas défait par l'automate), PLUS, sous le **même verrou**
        de la ligne `account_active` :

        * une retenue **valide** de `to` (`until_ts > now`) refuse la bascule :
          rien n'est écrit, l'appelant relit l'actif — c'est ce qui ferme la
          course « classement sur un instantané, retenue publiée entre-temps »
          (R1 auditeur, B2) ;
        * quand `hold_reason` est donné, pose (ou rafraîchit) la retenue du
          compte **quitté** dans la même transaction — le compte qu'on quitte
          en dépassement devient indisponible pour les autres workers au même
          instant où la bascule est visible ;
        * les retenues **échues** de `to` sont nettoyées dans la même
          transaction (R2 auditeur, B2) : un nettoyage APRÈS le commit
          effacerait une retenue publiée entre-temps par la bascule d'un autre
          runner, qui attend le même verrou.

        Faux sans rien écrire : compte vu changé, forçage manuel en place,
        aucun changement, ou retenue valide sur `to`."""

    @abc.abstractmethod
    def set_auto(self, host: str, harness: str, *, reason: str,
                 expected: str | None = None,
                 expected_until: float | None = None) -> bool:
        """Lève le forçage manuel (journalisé `auto`) ; faux si rien n'était forcé.

        #295 : `expected` (None = n'importe lequel) est le compte forcé que
        l'appelant a lu ; faux sans rien écrire si la ligne a changé de compte
        entre-temps (un forçage humain concurrent n'est pas défait).

        #3755 : `expected_until` (epoch) est l'échéance du forçage que
        l'appelant a vue échue. Comparée sous le verrou de la ligne, elle
        refuse la levée si un geste a renouvelé le forçage du MÊME compte avec
        une autre échéance entre-temps ; None = ne pas comparer (levée
        manuelle par `ameesh accounts auto`)."""

    @abc.abstractmethod
    def holds(self, host: str, harness: str) -> dict[str, dict]:
        """Retenues en cours, par compte : {account: {until_ts, reason}}."""

    @abc.abstractmethod
    def hold(self, host: str, harness: str, account: str, until_ts: float | None,
             reason: str) -> None:
        """Pose (ou remplace) la retenue d'un compte quitté au seuil."""

    @abc.abstractmethod
    def release(self, host: str, harness: str, account: str) -> None:
        """Retire la retenue d'un compte (il redevient éligible)."""

    @abc.abstractmethod
    def identities(self, host: str, harness: str, since: float) -> dict[str, list[dict]]:
        """#1883 : les empreintes de dossier publiées pour ce couple (hôte,
        harnais), encore vivantes depuis `since` (epoch) :
        {account: [{identity, last_ts}]}. Deux empreintes sous le même nom
        sont deux dossiers résolus : le compte est refusé au placement. Jamais
        un chemin, seulement une empreinte (secret d'hôte)."""

    @abc.abstractmethod
    def publish_identity(self, host: str, harness: str, account: str,
                         identity: str) -> None:
        """#1883 : publie (ou rafraîchit) l'empreinte du dossier résolu
        `identity` du compte, pour que les autres processus de l'hôte
        détectent un même nom déclaré vers deux dossiers différents."""

    @abc.abstractmethod
    def placement_state(self, host: str, harness: str, since: float) -> dict:
        """#1883 (R3 auditeur, B1) : retenues ET identités d'un couple (hôte,
        harnais), lues en UNE transaction sous le verrou de placement.

        Rend `{"holds": {account: {until_ts, reason}}, "identities":
        {account: [{identity, last_ts}]}}`, les identités encore vivantes
        depuis `since` (epoch). Toute publication de retenue
        (`switch_guarded`, `hold`) prend le même verrou : aucune retenue ne
        peut être publiée entre la lecture des retenues et celle des
        identités — c'est l'état cohérent sur lequel `accounts.choose` valide
        son choix (R3 auditeur, B1). `holds` et `identities` seuls restent des
        lectures d'un jeu, sans cohérence entre eux."""

    @abc.abstractmethod
    def switches(self, host: str, harness: str | None, limit: int) -> list[dict]:
        """Les dernières bascules de l'hôte (toutes ou d'un harnais), récentes d'abord."""


# --------------------------------------------------------------------------
# crédits du compte fournisseur, par compte logique (#4164)
# --------------------------------------------------------------------------

class ProviderCredits(Domain):
    """État réel des crédits d'un compte fournisseur (`ok|low|exhausted`).

    Clé : le compte **logique** (`harness`, `account`), le même sur tous les
    hôtes — le solde prépayé est celui du fournisseur. Les lignes ne portent
    que des noms, une raison courte et le texte d'erreur borné du
    fournisseur : jamais de secret ni d'invite. Instants de la base (`now()`).

    #4166 : `low` s'évalue sur un **montant de référence** — le prépayé
    déclaré par un humain, sinon le dernier sommet de la série de soldes — et
    un solde projeté (dépense réglée) ou réel (API du fournisseur). L'état et
    son instant (`since`) sont la clé d'alerte ; `balance_usd`, `ratio` et
    `runway_ts` sont le dernier relevé, pour l'affichage et les alertes.
    """

    @abc.abstractmethod
    def get(self, harness: str, account: str) -> dict | None:
        """La ligne du compte (`state`, `since_ts`, `checked_ts`,
        `next_probe_ts`, `reason`, `detail`, `source`, `agent`, `host`, et
        #4166 `prepaid_usd`, `prepaid_ts`, `prepaid_by`, `balance_usd`,
        `ratio`, `runway_ts`), None si le compte n'a jamais été vu."""

    @abc.abstractmethod
    def listing(self) -> list[dict]:
        """Toutes les lignes, par harnais puis compte."""

    @abc.abstractmethod
    def mark_exhausted(self, harness: str, account: str, *, reason: str,
                       detail: str | None, source: str, agent: str | None,
                       host: str | None, probe_after_s: float,
                       balance_usd: float | None = None,
                       ratio: float | None = None,
                       runway_ts: float | None = None) -> bool:
        """Pose `exhausted` (prochaine sonde dans `probe_after_s`). Vrai si
        c'est une **transition** (le compte n'était pas déjà épuisé) ; sinon
        seuls l'heure du contrôle et le détail sont actualisés — l'échéance de
        sonde n'avance que par `claim_probe`. Le relevé (#4166) est facultatif
        et n'écrase pas l'ancien quand il est absent."""

    @abc.abstractmethod
    def mark_low(self, harness: str, account: str, *, reason: str,
                 detail: str | None, source: str, agent: str | None,
                 host: str | None, balance_usd: float | None,
                 ratio: float | None, runway_ts: float | None,
                 allow_from_exhausted: bool = True) -> bool:
        """Pose `low` avec le relevé qui l'a décidé. Vrai si c'est une
        **transition** (depuis `ok` ou, si `allow_from_exhausted`, depuis
        `exhausted` : des crédits sont revenus, mais bas) ; un compte déjà
        `low` garde son `since` (une seule alerte par épisode) et ne voit que
        son relevé actualisé. Avec `allow_from_exhausted=False`, un compte
        `exhausted` n'est jamais modifié : les écritures de l'évaluation
        périodique ne rouvrent pas un compte que seule la sonde rouvre
        (#4164, B1 cx2/auditeur)."""

    @abc.abstractmethod
    def mark_ok(self, harness: str, account: str, *, source: str,
                agent: str | None = None, host: str | None = None,
                balance_usd: float | None = None, ratio: float | None = None,
                runway_ts: float | None = None,
                allow_from_exhausted: bool = True) -> bool:
        """Pose `ok` et le relevé éventuel. Vrai si c'est une transition
        depuis `low`/`exhausted` (une seule des écritures concurrentes
        l'obtient). Avec `allow_from_exhausted=False`, un compte `exhausted`
        n'est jamais rouvert par cette écriture (seule la sonde ou un relevé
        de solde réel le rouvre)."""

    @abc.abstractmethod
    def declare_prepaid(self, harness: str, account: str, *, usd: float,
                        actor: str) -> tuple[bool, float | None]:
        """Déclare le montant prépayé disponible **maintenant** (un humain) :
        `prepaid_usd`, `prepaid_at = now()`, `prepaid_by = actor`, et le
        relevé repart de ce montant. Rend `(changé, ancien montant)`. N'écrit
        jamais `state` : l'évaluation qui suit décide (et un compte `exhausted`
        ne repasse `ok` qu'à la sonde, #4164)."""

    @abc.abstractmethod
    def clear_prepaid(self, harness: str, account: str, *, actor: str
                      ) -> tuple[bool, float | None]:
        """Retire la déclaration prépayée (l'état repasse par l'évaluation).
        Rend `(changé, ancien montant)`."""

    @abc.abstractmethod
    def claim_probe(self, harness: str, account: str, interval_s: float) -> bool:
        """Réserve atomiquement la sonde d'un compte épuisé dont l'échéance
        est passée, et repousse la suivante de `interval_s`. Vrai pour un
        seul appelant par échéance."""


# --------------------------------------------------------------------------
# fenêtre courte du fournisseur, par compte logique (#3779)
# --------------------------------------------------------------------------

class AccountWindows(Domain):
    """Refus 429 du fournisseur, partagés par les exécuteurs d'un même compte.

    Les lignes de `account_rate_events` ne portent que des noms de comptes,
    des noms d'agents, l'hôte et le texte d'erreur du fournisseur (borné) :
    jamais de secret, d'invite ni de jeton. La clé est le compte **logique**
    (`harness`, `account`), le même nom sur tous les hôtes du réservoir (Codex :
    `primaire`) — le bucket de rafale est celui du fournisseur, pas celui d'un
    hôte.
    """

    @abc.abstractmethod
    def record(self, harness: str, account: str, host: str, agent: str,
               kind: str, detail: str | None) -> None:
        """Ajoute un refus fournisseur (`kind` = `rate_limited`)."""

    @abc.abstractmethod
    def recent(self, harness: str, account: str, since_s: float) -> dict:
        """Les refus des `since_s` dernières secondes : `count`, `last_at_ts`
        (epoch), `last_agent`, `last_host`, `last_detail`. Zéros et None sans
        refus ; l'instant est celui de la base (`now()`), commun aux hôtes."""

    @abc.abstractmethod
    def active(self, harness: str, account: str) -> list[dict]:
        """Les tours actifs du compte, **bail vivant seulement** (`lease_owner`
        posé et `lease_expires_at` futur) : un hôte tombé ne bloque pas le
        plafond de son compte pour toujours. La place porte le harnais et le
        compte **effectifs** du tour (`account_turn_slots`, écrit avec
        `begin_turn`) : un agent d'un autre harnais qui emprunte ce compte
        compte dans son plafond. Rend `name`, `host`, `status_since_ts`
        (epoch). Un agent sans place (tour commencé avant le déploiement) n'est
        pas compté : la garde s'ouvre un tour, jamais ne bloque à tort."""

    @abc.abstractmethod
    def start(self, harness: str, account: str, host: str, agent: str,
              *, slot_only: bool = False) -> None:
        """Journalise le début d'un tour du compte (écart entre deux débuts) et
        pose/actualise sa place active avec le harnais et le compte effectifs
        (`harness`, `account`) — appelé dans la transaction d'admission, avec
        `begin_turn`.

        `slot_only` : ne journalise pas le début (harnais non suivi, qui n'a
        pas d'écart à tenir) et ne fait que remplacer la place de l'agent.
        Un agent qui revient d'un emprunt Codex sous son harnais propre efface
        ainsi son ancienne place Codex (#3779, avis cx2)."""

    @abc.abstractmethod
    def last_start(self, harness: str, account: str) -> dict:
        """Le dernier début de tour du compte : `last_at_ts` (epoch, None sans
        début) et `last_agent`, `last_host`. Instant de la base, commun aux
        hôtes."""


# --------------------------------------------------------------------------
# credential health (L226a/L226b, migrations 0060 and 0068)
# --------------------------------------------------------------------------

class Credentials(Domain):
    """Last known health of each harness credential, per (host, harness,
    account); `account` '' is the harness's default credential. Names only,
    never a secret."""

    @abc.abstractmethod
    def report(self, host: str, harness: str, account: str, state: str, *,
               reason: str = "", source: str, expires_at: float | None = None,
               fingerprint: str | None = None,
               expect_failures: int | None = None) -> dict:
        """Records `state` (healthy | failing | unknown) from `source`
        (sentinel | turn | manual | relay); `failing_since` is kept while failing and
        cleared otherwise; `expires_at` (epoch, None = unknown) is the
        credential's forecast expiry, overwritten on each report. A `failing`
        report stamps `last_failure_at`, stores `fingerprint` (of the
        credential that failed, never the credential) and counts one more
        failure. Any other report with `expect_failures` lands only if the
        failure count is still that one (0 for a missing row). Returns
        {state, previous} (previous: None if new), or {skipped: True}."""

    @abc.abstractmethod
    def annotate(self, host: str, harness: str, account: str, reason: str, *,
                 expect_failures: int) -> bool:
        """#210 r4: replaces the `reason` of a `failing` row (adds the fix
        command to a failure a turn detected), nothing else, and only if the
        failure count is still `expect_failures`. True if written."""

    @abc.abstractmethod
    def health(self, host: str, harness: str) -> dict[str, dict]:
        """{account: row} for a harness on a host: state, reason, source,
        checked_ts, failing_since_ts, expires_ts, last_failure_ts,
        failing_fingerprint, failures."""

    @abc.abstractmethod
    def listing(self, host: str | None = None) -> list[dict]:
        """Every row (of a host, or of all hosts), without
        `failing_fingerprint`: only columns the supervisor role reads."""


# --------------------------------------------------------------------------
# boîte aux lettres (`agent_mailbox`)
# --------------------------------------------------------------------------

class MailboxRefused(RuntimeError):
    """L976 : le renvoi d'une boîte morte est refusé par l'état du registre,
    relu SOUS VERROU dans la transaction qui copie — un destinataire relancé
    entre la lecture et l'écriture, un successeur arrêté ou un nom absent.
    L'appelant (`ameesh.undeliverable`) la rend en `Refused` lisible."""


class RecipientRetired(MailboxRefused):
    """#3460 : dépôt refusé, le destinataire est retiré (`ameesh agent retire`).

    Le retrait est relu dans l'instruction même qui dépose, sous un verrou
    partagé de la ligne du registre que `retire` prend en exclusif : un
    retrait committé entre un précontrôle et le dépôt gagne, aucun message
    n'est écrit. Une diffusion (« all », copies) saute le retiré ; un envoi à
    un seul destinataire remonte le refus."""


class Mailbox(Domain):
    """Messages durables ; le dépôt réveille le destinataire (NOTIFY en Postgres)."""

    @abc.abstractmethod
    def by_idempotency_key(self, sender: str, recipient: str, key: str) -> int | None:
        """L'id du message de `sender` à `recipient` déposé sous cette
        Idempotency-Key (`payload.idempotency_key`, §16 #3889), ou None."""

    @abc.abstractmethod
    def lock_recipient(self, recipient: str) -> dict | None:
        """Lock the registry recipient against reassignment/deletion until commit.

        Call within a transaction, recheck authorization, then deposit there.
        Returns name and chantier; missing recipients are not authorized.
        """

    @abc.abstractmethod
    def send(self, sender: str, recipient: str, body: str, *, host: str | None,
             kind: str, payload: dict | None, work_item_id: str | None,
             signature: str | None, signature_key: str | None,
             signed_payload: str | None, nonce: str | None, created_us: int | None,
             expires_us: int | None) -> dict:
        """Dépose un message ; rend id, created_ts et les projets du fil
        (`sender_project`, `recipient_project`) des deux agents."""

    @abc.abstractmethod
    def unread(self, recipient: str, limit: int) -> list[dict]:
        """Messages non remis, du plus ancien au plus récent, avec leur lot
        (`work_item_id`, #794 : le brief d'un lot ne montre que son fil)."""

    @abc.abstractmethod
    def unread_active(self, recipient: str, limit: int) -> list[dict]:
        """L125 : messages non remis qui ouvriront un tour — ni accusé, ni
        copie, ni diffusion non urgents (`mail.passive_reason`) —, du plus
        ancien au plus récent."""

    @abc.abstractmethod
    def unread_urgent(self, recipient: str, limit: int) -> list[dict]:
        """Messages non remis marqués `payload.urgent`."""

    @abc.abstractmethod
    def unread_human(self, recipient: str, limit: int, *,
                     names: Sequence[str] | None = None) -> list[dict]:
        """L77/B13 : messages non remis d'un humain ACTIF, filtrés DANS la requête.

        Un humain (`payload.human`, ancien expéditeur `human:*`, ou nom
        déclaré de l'hôte dans `names`, None = aucun) ne doit pas rester
        invisible derrière plus de `UNREAD_LIMIT` non-lus ordinaires : le rang 2
        le sert avant la suite et la consigne en attente. Du plus ancien au
        plus récent.

        L77/B14 : les passifs (copie `cc`, accusé…) sont écartés par la
        requête, comme par `mail.passive_reason` — une copie humaine ne
        remplit pas la fenêtre devant un humain actif.
        """

    @abc.abstractmethod
    def unread_human_page(self, recipient: str, limit: int, *,
                          after_id: int | None = None,
                          names: Sequence[str] | None = None) -> list[dict]:
        """L77/B14 : les humains actifs d'APRÈS `after_id`, du plus ancien au
        plus récent.

        Le serveur médié borne chaque page à 200 et `unread_human` n'a pas de
        décalage : un préfixe de plus de 200 humains retenus (lots retenus)
        ne se franchit qu'avec un curseur. `after_id` est l'id du dernier
        message de la page précédente ; None lit depuis le début. Les passifs
        sont écartés comme dans `unread_human`.
        """

    @abc.abstractmethod
    def unread_mid_turn(self, recipient: str, limit: int, *,
                        include_human: bool = True) -> list[dict]:
        """L514/#885 : les messages non remis qu'une remise mi-tour peut
        livrer — verdict, arbitrage, message d'un humain, urgent
        (`mail.mid_turn`) —, du plus ancien au plus récent, sans s'arrêter
        aux `UNREAD_LIMIT` premiers non-lus : un verdict derrière plus de 200
        non-lus est vu au prochain point sûr, pas à la fin du tour.
        `include_human=False` (L77/B11) : les messages humains sont laissés à
        la file du tour résident."""

    @abc.abstractmethod
    def get(self, message_id: int) -> dict | None:
        """Un message par son id."""

    @abc.abstractmethod
    def mark_delivered(self, ids: Sequence[int]) -> int:
        """Marque remis ; rend le nombre de messages passés remis."""

    @abc.abstractmethod
    def withdraw(self, ids: Sequence[int], note: dict) -> list[int]:
        """L161c : retire des messages NON REMIS et sans réservation active
        (marqués remis sans être montrés, `note` dans `meta.retire`) ; rend
        les ids retirés. Un message déjà remis ou en cours de remise reste ;
        seuls les messages de CI d'`ameesh` (`payload.ci`) peuvent l'être."""

    @abc.abstractmethod
    def reserve(self, recipient: str, owner: str | None, epoch: int | None,
                token: str, *, ids: Sequence[int] | None = None, porteur: str,
                ttl_seconds: float, limit: int = 200,
                turn_id: str | None = None) -> list[dict]:
        """Réserve atomiquement des non-lus sans réservation active (le
        destinataire est verrouillé dans le registre, le bail (owner, epoch)
        contrôlé s'il est donné) ; rend les messages réservés sous `token`.

        `turn_id` (#2894) : le tour de l'exécuteur qui réserve sa consigne,
        écrit dans `meta.consigne.turn` (additif) pour relier la remise au
        document `ameesh-turn-context/1` ; absent pour une réservation de hook.
        """

    @abc.abstractmethod
    def deliver(self, recipient: str, owner: str | None, epoch: int | None,
                token: str, ids: Sequence[int]) -> list[int]:
        """Solde la réservation `token` (et elle seule), bail vivant exigé s'il
        est donné ; rend les ids passés remis."""

    @abc.abstractmethod
    def release(self, recipient: str, token: str, ids: Sequence[int]) -> int:
        """Annule la réservation `token` (rien n'a été montré)."""

    @abc.abstractmethod
    def unread_counts(self) -> dict[str, int]:
        """Nombre de messages non remis par destinataire."""

    @abc.abstractmethod
    def history(self, recipient: str, limit: int) -> list[dict]:
        """Derniers messages du destinataire, du plus récent au plus ancien."""

    @abc.abstractmethod
    def staging_emission(self, recipient: str, sender: str, key: str) -> int | None:
        """#4180 : id du signal staging de cette clé, sans fenêtre historique.

        Le destinataire, l'expéditeur et la clé sont filtrés avant la borne.
        None prouve l'absence ; une erreur de lecture doit se propager.
        """

    @abc.abstractmethod
    def review_requests(self, recipient: str, lot: str) -> list[dict]:
        """#2504 : les demandes de relecture (`kind = 'request'`) du
        destinataire liées à ce lot, du plus récent au plus ancien.

        Le filtre (destinataire, nature, lot) est fait DANS la requête : un
        `history` borne avant de filtrer, donc une demande ancienne du même lot
        sortait de la fenêtre et une re-désignation renvoyait un doublon."""

    @abc.abstractmethod
    def delivered_for_lot(self, recipient: str, lot: str, limit: int) -> list[dict]:
        """Messages déjà remis d'un lot, du plus récent au plus ancien, bornés
        par `limit`. Filtre destinataire ET lot dans la requête, sans fenêtre
        de temps : un verdict ancien reste lisible dans le brief de son lot.
        Non admis en mode médié (contrat fermé) : l'appelant garde les non-lus."""

    @abc.abstractmethod
    def pending_recipients(self) -> list[str]:
        """Destinataires qui ont du courrier non remis."""

    @abc.abstractmethod
    def pending_recipients_sorted(self) -> list[str]:
        """Destinataires qui ont du courrier non remis, par ordre de nom
        (`export-v0`)."""

    @abc.abstractmethod
    def unread_total(self) -> int:
        """Nombre de messages non remis, tous destinataires confondus (`doctor`)."""

    @abc.abstractmethod
    def dead_letters(self) -> list[dict]:
        """Courrier en souffrance : non remis, dont le destinataire est absent du
        registre ou arrêté. Une ligne par (destinataire, expéditeur) :
        `recipient`, `sender`, `n`, `oldest_ts`, `newest_ts`, `unknown`."""

    @abc.abstractmethod
    def forward(self, old: str, new: str, by: str) -> list[dict]:
        """Re-livre à `new`, en UNE transaction, les messages non remis de `old`
        (hors réservation active) : une copie garde expéditeur, corps, nature,
        charge, lot, hôte et date d'origine (`meta.forwarded_from` : agent,
        message, auteur du renvoi ; la signature, qui couvre le destinataire,
        ne suit pas) ; l'original passe remis, renvoi noté (`meta.forwarded`).
        Rend une ligne par message renvoyé : `original_id`, `new_id`,
        `sender`, `kind`, `work_item_id`, `created_ts`.

        L976 : la décision est prise DANS cette transaction, sous le verrou
        advisory par nom (`registry.NAME_LOCK_*`, partagé avec tous les chemins
        de création d'une ligne du registre) puis sous les lignes du registre
        de `old` et `new` (`FOR UPDATE`, par ordre de nom), leur statut relu
        après l'attente éventuelle : `old` relancé, `new` absent ou arrêté
        lèvent `MailboxRefused`. Une relance, un arrêt ou une INSCRIPTION
        concurrente de `old` (nom absent : aucune ligne à verrouiller) ne peut
        plus se glisser entre la lecture et la copie (relectures codex3, B1
        puis B1 résiduel)."""

    @abc.abstractmethod
    def expire_dead_letters(self, recipient: str, note: dict, before_ts: float,
                            dry_run: bool = False) -> list[dict]:
        """L976 : expire le courrier en souffrance de `recipient` — non remis,
        reçu avant `before_ts`, sans réservation active, hors demande de
        décision (L124) — à condition qu'il soit TOUJOURS une boîte morte
        (absent du registre ou arrêté) : marqué remis, `note` dans
        `meta.expired`. Un destinataire relancé entre la lecture et l'écriture
        garde son courrier. Rend une ligne par message expiré : `id`, `sender`,
        `kind`, `created_ts` ; `dry_run` : ceux qui le seraient, rien n'est
        écrit.

        L976 : le statut est relu SOUS LE VERROU — verrou advisory par nom
        (`registry.NAME_LOCK_*`, partagé avec tous les chemins de création)
        pris dans une instruction à part AVANT la décision, puis verrou de la
        ligne du registre (`FOR SHARE`) quand elle existe — donc après
        l'attente éventuelle sur une relance en cours : le courrier d'un vivant
        n'est jamais expiré. L'inscription concurrente d'un nom ABSENT (aucune
        ligne à verrouiller) prend le même verrou advisory : elle attend, ou sa
        ligne est vue par la lecture suivante (relectures codex3, B1 puis B1
        résiduel). `dry_run` : lecture seule, sans verrou."""

    @abc.abstractmethod
    def human_messages(self, *, since_ts: float, humans: Sequence[str],
                       limit: int) -> list[dict]:
        """L130 : les messages d'humains déposés depuis `since_ts`, du plus
        ancien au plus récent (les `limit` plus récents) : expéditeur
        `human:<id>`, marqué humain à l'envoi (`payload.human`), nommé dans
        `humans` (humains déclarés), ou chat du propriétaire (`chat-…` inscrit
        `externe`). Sans les accusés (`payload.ack`), les copies
        (`payload.cc`) ni les demandes de décision. Colonnes : `id`, `sender`,
        `recipient`, `body`, `kind`, `payload`, `work_item_id`, `lot_id` (le
        lot existant que désigne `work_item_id`, sinon NULL), `created_ts`.
        Lecture seule."""


# --------------------------------------------------------------------------
# réveil des exécuteurs (LISTEN/NOTIFY en Postgres)
# --------------------------------------------------------------------------

class Subscription(Protocol):
    """Abonnement ouvert par `Wakeups.subscribe` (une connexion dédiée en
    Postgres). `wait` et `close` peuvent être appelés depuis deux fils
    différents : `close` interrompt l'abonnement d'un exécuteur qui s'arrête."""

    def wait(self, timeout: float) -> dict | None:
        """Attend un signal au plus `timeout` secondes. Rend
        `{"channel", "payload"}` pour un signal, None si le délai s'écoule
        (l'appelant sonde alors l'état), ou `{"event": "down", "error"}` si
        l'abonnement est rompu (l'appelant le ferme et se réabonne)."""

    def close(self) -> None:
        """Ferme l'abonnement (idempotent, ne lève pas)."""


class Wakeups(Domain):
    """Signaux de réveil, par canal : le dépôt d'un message (`agent_mail`) et
    les changements de bail ou de statut (`agent_lease`) en émettent (en
    Postgres : des triggers, migration 0001). Un signal n'est qu'un réveil :
    l'état fait foi dans les tables, relu après chaque réveil ou délai. Sans
    réveil (`subscribe` rend None), l'exécuteur se replie sur le sondage."""

    @abc.abstractmethod
    def subscribe(self, channels: Sequence[str]) -> Subscription | None:
        """Ouvre un abonnement à ces canaux ; None si le pilote ne sait pas
        réveiller (ou si l'abonnement n'a pas pu s'ouvrir)."""

    @abc.abstractmethod
    def notify(self, channel: str, payload: str) -> None:
        """Émet un signal sur `channel` (diagnostic de bout en bout, `doctor`)."""


# --------------------------------------------------------------------------
# autorité : clés des agents, approbations signées, nonces consommés
# --------------------------------------------------------------------------

class Keys(Domain):
    """Clés publiques Ed25519 des agents (colonnes de `agent_registry`) et
    des humains (`key_holders`, #330), lues ensemble dans la vue `mesh_keys`."""

    @abc.abstractmethod
    def info(self, agent: str) -> dict | None:
        """Clé, empreinte, rôle et dates de l'agent, ou None."""

    @abc.abstractmethod
    def register(self, agent: str, public_key: str, fingerprint: str, role: str,
                 note: str | None) -> bool:
        """Enregistre la clé publique (base64) ; faux si l'agent est inconnu."""

    @abc.abstractmethod
    def register_human(self, name: str, human: str, public_key: str, fingerprint: str,
                       role: str, *, replace: bool = False) -> str:
        """Enregistre la clé d'un humain sous `name`, hors du registre des
        agents (`key_holders`, #330) : `created` ; `exists` si le détenteur
        existe déjà (sans `replace`) ; `agent` si un agent porte ce nom.
        `replace` (geste du propriétaire, #326 r1) remplace la clé, même
        révoquée : `replaced` ; la base le refuse hors du rôle propriétaire.
        Les règles sont celles de la base (0364) : une erreur de la base
        remonte telle quelle. La ligne de contournement du 12/10 (`smichea`)
        quitte le registre."""

    @abc.abstractmethod
    def revoke(self, agent: str) -> bool:
        """Révoque la clé active ; faux s'il n'y en avait pas."""

    @abc.abstractmethod
    def owner_registrations(self, since: float) -> list[dict]:
        """Clés owner enregistrées ou remplacées (#326 r2-r3), lues dans le
        journal `key_events`, qu'aucune révocation n'efface : par le geste du
        propriétaire, depuis `since` ; sans ce geste, tant que le propriétaire
        ne les a pas confirmées (`confirm`). id, name, holder, event,
        fingerprint, db_user, owner_gesture, at_ts."""

    @abc.abstractmethod
    def confirm(self, name: str) -> int:
        """Le propriétaire confirme les enregistrements de la clé de `name`
        (#326 r3) : un événement `confirmed`, que la base refuse hors de son
        geste (rôle propriétaire) ; rend son id, 0 sans clé de ce nom."""

    @abc.abstractmethod
    def registered(self) -> list[dict]:
        """Détenteurs d'une clé publique (révoquée comprise), par nom : name,
        holder (`human` ou `agent`), public_key_fingerprint, key_role,
        key_updated_ts, key_revoked_ts."""


class Approvals(Domain):
    """Approbations Ed25519 (`mesh_approvals`) et leur consommation unique."""

    @abc.abstractmethod
    def create(self, *, approver: str, action: str, artifact_kind: str,
               artifact_hash: str, decision: str, nonce: str, signed_payload: str,
               signature: str, signature_key: str, created_us: int, expires_us: int,
               meta: dict | None) -> int:
        """Enregistre une approbation signée ; rend son id."""

    @abc.abstractmethod
    def recent(self, *, action: str | None, artifact_hash: str | None,
               limit: int) -> list[dict]:
        """Approbations avec leur état, de la plus récente à la plus ancienne."""

    @abc.abstractmethod
    def candidates(self, action: str, artifact_hash: str, decision: str) -> list[dict]:
        """Les 20 dernières approbations pour (action, empreinte, décision)."""

    @abc.abstractmethod
    def consume(self, approval_id: int, by: str) -> bool:
        """Consomme l'approbation une seule fois, par (approver, nonce) :
        faux si ce nonce était déjà consommé."""


class Nonces(Domain):
    """Registre des nonces consommés (`mesh_consumed_nonces`)."""

    @abc.abstractmethod
    def state(self, approver: str, nonce: str) -> dict | None:
        """La consommation de (approver, nonce) — consumed_by, consumed_ts — ou None."""

    @abc.abstractmethod
    def consume(self, approver: str, nonce: str, *, by: str, challenge: str,
                authenticator_id: int | None, exp: int, iat: int, clock_skew: int) -> bool:
        """Consomme (approver, nonce) une seule fois, en UNE transaction :
        authentificateur (s'il est donné) lu sous verrou partagé, puis
        échéances signées recontrôlées à l'heure réelle de la base APRÈS ce
        dernier verrou, décision par insertion sur la clé (approver, nonce),
        échéances recontrôlées après elle. Faux si déjà consommé ou
        authentificateur révoqué ; échéance dépassée : erreur de base
        (`ameesh_echeance [expired|iat_future]`), rien n'est consommé."""


# --------------------------------------------------------------------------
# lots (`work_items`, `work_item_events`)
# --------------------------------------------------------------------------

class PendingLot(RuntimeError):
    """#540 : `assign(…, take=True)` refusé : l'agent a déjà le lot non
    démarré `lot` (lu sous son verrou) ; rien n'est écrit."""

    def __init__(self, lot: int):
        super().__init__("lot #%d not started" % lot)
        self.lot = lot


class FootprintOverlap(RuntimeError):
    """#4173 : l'attribution d'un lot qui n'occupait pas encore son empreinte
    est refusée — elle chevauche celle d'un lot occupant du même dépôt
    (`overlaps` : lignes de `work_footprints.overlapping`) ; rien n'est écrit."""

    def __init__(self, overlaps: list):
        self.overlaps = list(overlaps)
        super().__init__("footprint overlaps lot(s) %s" % ", ".join(
            "#%d" % int(i) for i in sorted({int(r["id"]) for r in self.overlaps})))


class BasketEstimateRequired(RuntimeError):
    """#4117 : `backlog_basket_add` allait OUVRIR un panier pour un agent sans
    estimation ni référence publiée ; `backlog.add` le traduit en
    `estimates.EstimateError` (L157). Rien n'est écrit (la transaction du
    panier est annulée). Levée seulement à l'ouverture : une ligne ajoutée à
    un panier déjà ouvert ne crée aucun lot et n'exige pas d'estimation."""


class ReservationContention(RuntimeError):
    """L122, #478 : le budget de réessais d'une réservation automatique est
    épuisé — le prochain numéro libre n'a pas pu être réservé à temps sous la
    contention. L'appelant (`ameesh.work`) la rend en `WorkError` lisible
    (« reservation contention, retry »), jamais en DbError brute. Levée
    seulement quand `number` est absent : un numéro explicite déjà pris garde
    la DbError de l'index."""


class WorkItems(Domain):
    """Lots et journal de leurs transitions (ledger de reprise)."""

    @abc.abstractmethod
    def set_app(self, item_id: int, app: str, *, actor: str,
                if_empty: bool = False, assignee: str | None = None) -> dict | None:
        """L118c: journaled project correction, atomically with its old value."""

    @abc.abstractmethod
    def set_type(self, item_id: int, type: str, *, current: str, actor: str) -> dict | None:
        """#2954: journaled lot type correction, if the lot still carries
        `current` (compare-and-set, no lost concurrent correction)."""

    @abc.abstractmethod
    def set_priority(self, item_id: int, priority: int, *, current: int | None,
                     reason: str, actor: str) -> dict | None:
        """#3370: journaled priority correction, if the lot still carries
        `current` (compare-and-set, NULL included, no lost concurrent
        correction)."""

    @abc.abstractmethod
    def without_project(self) -> list[dict]:
        """L118c: open lots with empty app, once per id, including waiting_human."""

    @abc.abstractmethod
    def creator(self, item_id: int) -> str:
        """Actor of the first journal entry, without truncating long journals."""

    @abc.abstractmethod
    def add(self, *, type: str, source: str, app: str, title: str, body: str,  # noqa: A002
            issue_ref: str | None, workstream: str | None, assignee: str | None,
            budget_usd: float | None, note: str, actor: str,
            package_id: str | None = None, package_parent: str | None = None,
            branch: str | None = None, branch_target: str | None = None,
            priority: int | None = None, estimate_minutes: int | None = None,
            estimate_source: str | None = None, change_size: str | None = None,
            queued_note: str | None = None, forced_note: str | None = None,
            mark_item_id: int | None = None, mark_note: str = "",
            parent_lot: int | None = None, unique_ref: bool = False,
            external_executor: str | None = None,
            footprint: list | None = None) -> dict:
        """#4173 : `footprint`, l'empreinte d'intake écrite avec le lot ; un lot
        créé attribué dont elle chevauche celle d'un lot en cours du même dépôt
        lève `FootprintOverlap` (rien n'est écrit).

        Crée le lot en `intake` (rattaché à une fiche WorkPackage si
        `package_id`, L29 ; avec sa branche si `branch`, L118 ; sa priorité
        si `priority`, L130 ; avec sa durée estimée et sa ligne d'historique
        si `estimate_minutes`, L157 ; avec la taille déclarée du changement
        si `change_size`, L157b) et sa première ligne de journal ; rend le
        lot. #540 : avec `queued_note`, un `assignee` qui a déjà un lot
        ouvert qui l'occupe (L165b : non démarré, ni en attente d'un autre,
        ni prévu après aujourd'hui) ne le reçoit pas : le lot va
        dans la file d'équipe (sans assigné), journalisé par `queued_note`
        (`{lot}` : le lot déjà en attente), rendu avec `queued_behind`.
        Avec `forced_note` aussi (`--force` motivé) : l'agent le reçoit quand
        même, journalisé par `forced_note`, rendu avec `forced_behind`.
        Contrôle et écriture sous le verrou de transaction de l'agent.

        `mark_note`, si non vide, est écrit au journal de `mark_item_id` dans
        la **même** transaction (L539 : la marque d'idempotence du lot de
        relecture ne peut pas survivre seule à l'échec de l'autre écriture) ;
        `{lot}` y est remplacé par l'identifiant du lot créé. La ligne
        `mark_item_id` est verrouillée avant l'insertion et `issue_ref` y est
        relue sous ce verrou : deux créations concurrentes de la même
        référence ne peuvent pas aboutir toutes les deux (revue #544 r2).

        `parent_lot` (#1193): the explicit parent of a suite lot, linked in the
        **same** transaction — a blocking edge if the parent is still open, a
        journal trace if it is already finished, everything rolled back if it
        is unknown.

        `unique_ref` (#2954): with an `issue_ref`, take a transaction advisory
        lock on the reference and refuse the creation (`DbError`) when any lot,
        whatever its state, already carries it. Two hosts creating the incident
        of the same run serialize: the second sees the first and the caller
        re-reads the winner. #3607 : the re-read covers the exact `source` too,
        not only `issue_ref` — the identity of an execution is also carried
        there (`ci-incident:<repo>:<run>`), and the ticket sync may have moved
        the winner's `issue_ref` to its mirror ticket between the lock wait and
        the re-read. Off by default: several lots may legitimately share a
        reference except where the identity is one per execution.

        `external_executor` (#3447) : le lot est créé réservé à un exécutant
        externe déclaré (agent `externe` avec responsable humain, ou humain),
        **sans** `assignee` ; la prise automatique L165 ne le prend pas. Le
        champ ne se propage pas au sous-lot (`parent_lot`) : la part d'un
        agent reste assignable.

        L'ordre des verrous est commun avec `assign` (#540, revue R1 delta) :
        les lignes de lots d'abord (`mark_item_id` et `parent_lot`, par
        identifiant croissant), le verrou d'agent ensuite."""

    @abc.abstractmethod
    def set_estimate(self, item_id: int, minutes: int, *, source: str | None,
                     actor: str) -> dict | None:
        """L157 : pose l'estimation en vigueur d'un lot non terminé, avec sa
        ligne d'historique (sans journal ni `updated_at`, comme une date
        prévue) ; None si introuvable ou terminé."""

    @abc.abstractmethod
    def estimates(self, item_id: int) -> list[dict]:
        """L157 : l'historique des estimations d'un lot (`minutes`, `source`,
        `estimated_by`, `at_ts`), de la plus ancienne à la plus récente."""

    @abc.abstractmethod
    def mark_started(self, agent: str, item_ids, note: str) -> list[int]:
        """L157 : premier tour de `agent` sur ces lots — ceux qui lui sont
        assignés, ouverts et sans début mesuré, reçoivent `started_at`
        (journalisé) ; rend leurs ids."""

    @abc.abstractmethod
    def estimate_history(self, *, app: str | None, limit: int) -> list[dict]:
        """L157 : les lots livrés, les plus récents d'abord, avec début mesuré,
        fusion, estimation en vigueur au début (`late` si posée après),
        auteur, source et nombre d'estimations."""

    @abc.abstractmethod
    def publish_reference(self, body: dict, *, actor: str) -> dict:
        """L157b : publie une table de référence des durées (schéma
        `ameesh-estimate-reference/1`) ; rend `{id, published_by,
        published_ts}`."""

    @abc.abstractmethod
    def reference(self) -> dict | None:
        """L157b : la table de référence en vigueur, la dernière publiée
        (`{id, published_by, published_ts, body}`), ou None."""

    @abc.abstractmethod
    def measure_candidates(self, *, ids: Sequence[int] | None, retry_diff_s: float,
                           since_s: float | None, limit: int) -> list[dict]:
        """L157c: delivered lots with a measured start and no measure yet, or
        measured without a diff (or without CI though the lot has a branch)
        and merged less than `retry_diff_s` ago (a repository holding the
        merge commit may show up, `gh` may answer); `since_s`: merged
        less than this ago only (None: any age). `ids`: only these lots,
        measured or not, of any age. Rows: `{id, state, assignee, branch,
        branch_target, change_size, started_ts, merged_ts, merge_sha, measured,
        has_diff, has_ci}`."""

    @abc.abstractmethod
    def save_measure(self, item_id: int, measure: dict) -> dict:
        """L157c: writes (or replaces) the measure of a delivered lot; a
        measure without diff keeps the diff already stored, one without CI
        keeps the phases already stored with a known CI. Returns it.
        `agent_source` (#796) says where `agent_minutes` comes from: 'turns'
        (the attributed turns) or 'states' (the state timeline)."""

    @abc.abstractmethod
    def measure(self, item_id: int) -> dict | None:
        """L157c: the measure of a lot (phases, diff), or None."""

    @abc.abstractmethod
    def turn_spans(self, item_id: int) -> list[dict]:
        """#796 : les tours du grand livre attribués à ce lot, dans l'ordre du
        début : `{start_ts, end_ts, turn_id}` (epoch ; `end_ts` = écriture de la
        ligne). `start_ts` = `turn_resources.started_at` quand le tour y est
        relié (le tour a pris la main), sinon `turn_costs.started_at` (le
        marqueur, posé avant le lancement) ; None si aucun des deux n'est connu
        (ligne d'avant 0423), et le tour est alors omis. Liste vide = aucun tour
        attribué, donc temps d'agent inconnu par les tours."""

    @abc.abstractmethod
    def set_branch(self, item_id: int, branch: str | None, target: str | None, *,
                   note: str, actor: str) -> dict | None:
        """L118 : pose la branche et la cible d'un lot ouvert (journalisé) ;
        None si le lot n'est plus ouvert."""

    @abc.abstractmethod
    def attach_implicit_branch(self, item_id: int, branch: str, *, detached_prefix: str,
                               note: str, actor: str) -> dict | None:
        """#2102 : pose la branche implicite (L118b) d'un lot ouvert, sans
        cible, avec une ligne de journal, seulement s'il n'a pas de branche et
        qu'aucune ligne de son journal ne commence par `detached_prefix`
        (détachement), vérifiés sous le verrou de la ligne du lot ; None sinon."""

    @abc.abstractmethod
    def detach_branch(self, item_id: int, expected: str | None, *, note: str,
                      actor: str) -> dict | None:
        """#2102 : retire la branche (et la cible, le dernier commit vu) d'un
        lot ouvert, avec une ligne de journal, seulement si sa branche est
        encore `expected` (None : aucune), vérifié sous le verrou de la ligne
        du lot ; None sinon (lot fermé, ou branche changée entre-temps)."""

    @abc.abstractmethod
    def reserve_migration(self, item_id: int, repo: str | None, number: int | None,
                          floor: int, *, note: str, actor: str, issue_ref: str | None,
                          release: bool = False,
                          release_number: int | None = None) -> dict | None:
        """L122, #443, #2481 : réserve un numéro de migration du dépôt `repo`
        au lot ouvert (journalisé) : `number` s'il est donné, sinon le prochain
        libre de ce dépôt, au moins `floor`. Un lot garde ses réservations
        précédentes : `number` s'ajoute à la liste (`migrations`), il ne
        remplace plus le numéro courant. L'unicité (dépôt, numéro) est celle
        des index uniques : deux réservations simultanées ne prennent jamais
        le même numéro ; la perdante réessaie avec une attente aléatoire
        croissante, dans un budget de temps (#478). `note` peut citer
        `%(number)04d` pour une réservation ou une libération d'UN numéro ; une
        libération totale nomme elle-même l'ensemble des numéros (un gabarit
        y rend la liste, jamais le sentinelle 0). None si le lot n'est plus
        ouvert, ou si `issue_ref` —
        la référence lue par l'appelant — a changé entre-temps (#1830 : NULL
        et chaîne vide confondus) ; DbError si `number` est pris par un autre
        lot ; `ReservationContention` si le budget du prochain libre est
        épuisé. `release` libère toutes les réservations du lot, ou seulement
        `release_number` s'il est donné, sans condition sur la référence.
        Les colonnes scalaires (`migration`, `migration_repo`) restent la
        réservation principale et `migrations` porte la liste complète."""

    @abc.abstractmethod
    def migration_holder(self, number: int, repo: str) -> dict | None:
        """L122 : le lot (id, titre, état) qui a réservé ce numéro du dépôt."""

    @abc.abstractmethod
    def record_landed_migration(self, item_id: int, number: int, repo: str, *,
                                note: str, actor: str) -> dict | None:
        """#1567 : enregistre (ou corrige) le numéro atterri d'un lot déjà
        fusionné ou promu, journalisé dans la même transaction ; None si le lot
        n'est pas dans un état terminal. La réservation n'est pas touchée."""

    @abc.abstractmethod
    def landed_slot(self, item_id: int) -> dict | None:
        """#4100 : le créneau `default` atterri du lot (`number`, `repo`), ou None."""

    @abc.abstractmethod
    def release_landed_slot(self, item_id: int, number: int, repo: str, *,
                            note: str, actor: str) -> dict | None:
        """#4100 : relâche le créneau atterri `number` d'un lot fusionné ou
        promu (numéro absent de la cible, aucun numéro posé sûr), journalisé
        dans la même transaction ; None si le lot n'est pas terminal."""

    @abc.abstractmethod
    def migration_landed_holder(self, number: int, repo: str) -> dict | None:
        """#1567 : le lot (id, titre, état) qui nomme déjà ce numéro atterri."""

    @abc.abstractmethod
    def record_resolution(self, item_id: int, other_id: int, *, commit: str, lot_tip: str,
                          other_tip: str, ref: str | None, files: list[str], note: str,
                          actor: str) -> dict | None:
        """L160d (0057): records (or replaces) the resolution prepared by lot
        `item_id` for its conflict with lot `other_id`, with a journal line on
        `item_id`. None if either lot is unknown."""

    @abc.abstractmethod
    def resolutions(self, item_ids: list[int]) -> list[dict]:
        """L160d (0057): the recorded resolutions where either side is one of
        `item_ids`, newest first."""

    @abc.abstractmethod
    def queue_add(self, item_id: int, *, target: str, frozen_sha: str, note: str,
                  actor: str, source: str = "", frozen_id: int | None = None,
                  packet: dict | None = None, packet_initial: bool = False,
                  lease: tuple | None = None,
                  withdrawn_after: float | None = None) -> dict | None:
        """L160e (0058): puts (or puts back) the lot in the merge queue of
        `target`, state `queued`, with a journal line; `source`: the
        repository it was queued from (`merge_queue.source_of`). None if the
        lot is no longer open. `packet` (#1467) is the expected QA item
        snapshot (app, branch, branch_target, type, resolved_target): create only, with a real
        expected freeze. A replay returns its same queued generation; an
        existing different entry, withdrawal, or changed snapshot refuses.
        Packet allocation/withdrawal receipts survive in typed train storage,
        separate from free-text work notes (including legacy receipt notes);
        they never certify historical reviews or CI. These adapter write
        paths do not isolate receipts from a trusted role's arbitrary SQL.
        A first insertion also needs `packet_initial=True` from the caller's qualified history
        provider: absence of a queue row cannot prove never-withdrawn for
        historical lots. It cannot override a recorded withdrawal.
        `lease` (#1467): optional `(target key, owner, epoch)`, locked after
        the lot and checked live before allocation/replay, in this transaction.
        `withdrawn_after` (#3532): an epoch instant; refuses (None) when a
        typed `removed` receipt of the lot was written at or after it, read
        under the lot row lock that `queue_remove` takes first. #3550: the
        receipt only binds the entry it removed — `target` and `frozen_id`
        (the expected freeze) scope it."""

    @abc.abstractmethod
    def queue_update(self, item_id: int, *, state: str | None = None,
                     rank: int | None = None, reason: str | None = None,
                     landing_sha: str | None = None, note: str = "",
                     actor: str = "", clear_rank: bool = False,
                     lease: tuple | None = None,
                     freeze: tuple | None = None, entry: tuple | None = None) -> dict | None:
        """L160e (0058): changes the state, rank, reason or landing commit of
        a queued lot (`queued` and `ejected` clear the landing commit;
        `ejected` or `clear_rank` clear the rank); a journal line when
        `note` is given. None if the lot is not queued. `lease` (#194 r5):
        `(target key, owner, epoch)` — the write happens only while that
        lease holds (`merge_locks`, not expired), in the same statement.
        `freeze` (#194 r8): `(frozen_sha, landing_sha)` — the write happens
        only while the lot is still `landing` on that freeze and that
        landing commit (a merge that happened, recorded without its lease).
        `entry` (#517): `(generation, target, frozen_id, frozen_sha)` compares
        the admission identity and current latest freeze, with or without a
        lease. Freeze insertion and this comparison share the lot row lock."""

    @abc.abstractmethod
    def queue_remove(self, item_id: int, *, note: str, actor: str) -> bool:
        """L160e (0058): takes the lot out of the queue, with a journal line.
        Locks the lot before its queue row and records a typed `removed`
        receipt for the deleted row's freeze and target in the same
        transaction. A free-text journal note cannot write this receipt."""

    @abc.abstractmethod
    def queue_withdrawn_since(self, item_id: int, since_ts: float, *,
                              target: str | None = None,
                              frozen_id: int | None = None) -> float | None:
        """#3532: the epoch instant of the lot's latest typed `removed`
        receipt written at or after `since_ts`, None without one. #3550:
        `target` and `frozen_id` restrict the read to the queue entry the
        caller is about to replace; a removal of another target or of an
        older freeze is not a take-out of that entry. Legacy receipts without
        a target still match (they cannot be told apart); a receipt without a
        freeze never matches a named freeze."""

    @abc.abstractmethod
    def land_lock(self, key: str, owner: str, ttl_s: float, *,
                  epoch: int | None = None) -> int | None:
        """L160e2 (0058): takes (or, with its `epoch`, renews) the merge lease
        of `key` (project and target) for `owner`; returns its epoch, None if
        another owner holds a lease that has not expired. Its own owner takes it
        again at a new epoch (an executor restarted mid-landing). Renewal with
        an epoch never revives an expired lease (#1680)."""

    @abc.abstractmethod
    def land_unlock(self, key: str, owner: str, epoch: int) -> bool:
        """L160e2 (0058): frees the lease, if still held at that epoch."""

    @abc.abstractmethod
    def land_holder(self, key: str) -> dict | None:
        """#3598: the live holder of the merge lease of `key`, for a refusal
        message: `{owner, expires_at, left_s}`, or None. A diagnostic read,
        never a guard; server-only, like `land_lock`/`land_unlock`."""

    @abc.abstractmethod
    def train_read(self, key: str) -> dict | None:
        """#1467: durable train document and its revision, or None."""

    @abc.abstractmethod
    def train_save(self, key: str, document: dict, *, revision: int,
                   owner: str, epoch: int) -> dict | None:
        """#1467: compare-and-swap train state under the existing land lease.

        revision=0 creates it. Every write locks and then rechecks the lease
        against wall time, and compares the previous document revision.
        A stale holder cannot overwrite a newer train. No remote API grant.
        """

    @abc.abstractmethod
    def train_abandon(self, key: str, plan_id: str, document: dict, *, revision: int,
                      owner: str, epoch: int) -> dict | None:
        """#3138: terminal write of one exact never-fired train plan.

        Same lease/epoch and revision checks as `train_save`, plus in the same
        transaction: the persisted plan id is still `plan_id`, its phase is not
        terminal or `promoting`, and no active/promoting/landed fire receipt
        exists for it. A concurrent fire grant is serialized on the target
        lease, so a plan cannot be abandoned between the grant's check and its
        insertion. Returns the new document, None on any mismatch.
        """

    @abc.abstractmethod
    def train_adopt(self, key: str, owner: str, *, revision: int,
                    previous: tuple | None, service: tuple, ttl_s: float) -> dict | None:
        """#3142: the train service's current holder takes over the target
        reservation another `train:*` owner left behind.

        In one transaction: ``service`` (key, owner, epoch) must be the live
        service lease; a key bound to a role
        (``train-service:<role>:<epoch>``) also requires that role's live
        lease at that exact epoch, so an old election's still-live service
        key cannot adopt the reservation back from the new holder; the target
        row must be held by another `train:*` owner (at ``previous``
        (owner, epoch) when given, expired or not); the train document must
        still be at ``revision``. The row passes to ``owner`` at the next
        epoch, so every write of the old epoch is refused afterwards. A
        reservation ticket of the document naming the held (owner, epoch) is
        rewritten to the new ones; with ``previous``, it must exist.
        Never takes an ordinary (non-`train:*`) reservation. Returns
        {epoch, previous_owner, previous_epoch}, or None.
        """

    @abc.abstractmethod
    def train_fire_read(self, key: str, plan_id: str) -> dict | None:
        """#1467: latest persisted fire receipt, including withdrawn history."""

    @abc.abstractmethod
    def train_fire_give(self, key: str, document: dict) -> dict | None:
        """#1467: append a fire for the current exact persisted plan.

        Locks the target; refuses replacement of an active/claimed fire.
        Caller authenticates the pilot on a declared role, never history.
        """

    @abc.abstractmethod
    def train_fire_change(self, key: str, fire_id: int, state: str, *, actor: str,
                          owner: str | None = None, epoch: int | None = None) -> dict | None:
        """#1467: serialized revoke, promotion claim, or completion.

        Claim requires the live train lease and persisted promotion intent.
        It commits BEFORE any external effect; an uncertain claim never
        becomes revocable or active again automatically. Completion requires
        a live holder; a restarted holder first observes the exact remote
        head in business code. A failed transition returns None.
        No mediated operation or authority is granted by this interface.
        """

    @abc.abstractmethod
    def queue(self, target: str | None = None) -> list[dict]:
        """L160e (0058): the queue rows (of `target`, else every target),
        ranked first, then by arrival.

        #3560: `packet_allocated` is true when the row's current generation
        carries the train's `packet` receipt: the train enrolled it and lands
        it itself; the radar queue never lands it."""

    @abc.abstractmethod
    def set_branch_head(self, item_id: int, branch: str, head: str | None) -> bool:
        """L118 : retient le dernier commit de la branche vu en avance sur sa
        cible, si la branche du lot est toujours `branch` (et quand, L118d)."""

    @abc.abstractmethod
    def set_partial_seen(self, item_id: int, sha: str) -> bool:
        """#346 r2 : retient la dernière fusion partielle `sha` du lot et
        l'instant où le relevé l'a vue la première fois."""

    @abc.abstractmethod
    def open_for_sweep(self, limit: int) -> list[dict]:
        """L118 : les lots ouverts que le relevé des fusions examine (avec une
        branche ou un assigné), du plus ancien au plus récent."""

    @abc.abstractmethod
    def open_for(self, assignee: str) -> list[dict]:
        """L118 : les lots ouverts d'un assigné."""

    @abc.abstractmethod
    def held_by(self, agents: Sequence[str]) -> dict[str, dict]:
        """#2877 : ce que ces agents tiennent encore, en une lecture sans
        verrou : par assigné, ``open`` (un lot ni fusionné, ni promu, ni fermé)
        et ``promoted`` (un lot promu) ; un agent qui ne tient rien est absent.
        Sert à ne verrouiller (``release_agent``) que s'il y a un lot à rendre."""

    @abc.abstractmethod
    def release_agent(self, agent: str, *, reason: str, actor: str,
                      measure_actor: str = "", measure_reason: str = "",
                      dry_run: bool = False,
                      stopped_with: tuple[str, ...] = ()) -> list[dict]:
        """#753 : à l'arrêt d'un agent, ses lots ouverts retournent à la file
        d'équipe (assigné effacé, journal au lot avec la raison), et ses lots
        promus dont la mesure reste à refaire allaient à ``measure_actor``
        (l'auditeur, ``measure_reason`` au journal), en une transaction. Rend
        les lots touchés. Idempotent : un lot dont l'assigné a changé
        entre-temps n'est pas touché. ``dry_run`` : ce qui serait fait, rien
        n'est écrit. ``stopped_with`` : si non vide, la transaction revérifie
        que l'agent est encore ``stopped`` pour l'une de ces raisons avant de
        rendre un lot (course fermée entre le relevé du registre et le geste)."""

    @abc.abstractmethod
    def open_by_ref(self, ref: str) -> list[dict]:
        """L118 : les lots ouverts désignés par une référence (issue, fiche,
        branche, premier mot du titre)."""

    @abc.abstractmethod
    def by_issue_ref(self, ref: str) -> list[dict]:
        """L539 : les lots portant exactement cette `issue_ref`, **quel que
        soit leur état** — l'identité d'un lot de relecture après fusion
        survit à sa fermeture, sinon un second lot serait ouvert."""

    @abc.abstractmethod
    def by_source(self, source: str) -> list[dict]:
        """#3607 : les lots dont la `source` vaut exactement cette référence,
        **quel que soit leur état**, la comparaison étant à la casse près
        (comme `by_issue_ref` : la casse d'un dépôt GitHub ne fait pas une
        autre exécution). L'identité d'un incident de CI vit aussi dans
        `source` (`ci-incident:<repo>:<run>`) : un lot ouvert à la main la
        porte quand la synchro des tickets a remplacé son `issue_ref` par le
        ticket miroir. Sans cette lecture, l'ouverture automatique doublerait
        l'incident. Aucun motif : préfixe et suffixe ne matchent pas, et une
        `source` vide ne désigne rien (sans la garde, elle rendrait tous les
        lots sans source)."""

    @abc.abstractmethod
    def lock(self, item_id: int) -> dict | None:
        """Le lot, verrouillé `FOR UPDATE` jusqu'à la fin de la transaction
        courante ; None s'il n'existe pas. #2744 : c'est le verrou commun des
        écritures de retenue (les notes `hold:`/`unhold:` le prennent avant
        `note`, les transitions par leur UPDATE) et de la fermeture par
        contenu — une retenue ne se glisse pas entre la lecture du journal et
        la fermeture."""

    @abc.abstractmethod
    def lock_workflow_activation(self, project: str) -> None:
        """Share the project policy lock through the adoption transaction."""

    @abc.abstractmethod
    def adopt_workflow(self, item_id: int, *, ref: str, version: int,
                       stage: str, state: str, actor: str) -> None:
        """Set the first workflow pin and journal it under the caller's lot lock."""

    @abc.abstractmethod
    def get(self, item_id: int) -> dict | None:
        """Un lot, ou None. #1922/B5 : le lot porte `workflow_ref` (la
        révision Process pinnée, '' quand il n'y en a pas — y compris sur une
        base d'avant la migration 0452). Les commandes historiques
        (`work.move`/`close`/`close_merged`) lisent la garde par cette
        interface, jamais par un SELECT brut : celui-ci est refusé sur une
        connexion d'exécuteur médié."""

    @abc.abstractmethod
    def items(self, *, state: str | None, assignee: str | None, limit: int | None) -> list[dict]:
        """Lots, du plus récemment modifié au plus ancien ; None sans limite."""

    @abc.abstractmethod
    def items_missing_ref(self, *, states: Sequence[str], ref_prefix: str,
                          limit: int, attempt_prefix: str = "",
                          retry_s: float = 0, terminal_prefix: str = "",
                          max_attempts: int = 0, since: datetime | None = None) -> list[dict]:
        """L539 (#1104) : les lots de `states` dont **aucun** lot ne porte la
        référence `ref_prefix + <id>` (celui qui leur correspond), du plus
        récemment modifié au plus ancien, `limit` au plus.

        La borne porte sur les lots **manquants rendus**, jamais sur un
        balayage préalable : un lot ancien encore manquant n'est pas caché par
        `limit` lots plus récents déjà pourvus (R2 #1104 P2-2). `states` est un
        seul ensemble ordonné, donc un `promoted` n'est pas affamé par des
        `merged` plus récents. Lecture pure : ni verrou ni écriture.

        `attempt_prefix`/`retry_s` (binôme mesh-design, 14/10) : excluent aussi
        **avant** la borne les lots dont le journal porte une tentative
        `attempt_prefix<id>` plus récente que `retry_s` secondes (voir
        `log_review_attempt`) — sinon les mêmes candidats sans ouverture
        affameraient les plus anciens. Le délai écoulé, ils redeviennent
        sélectionnables (délai, pas suppression).

        `since` (arbitrage pilote du 15/10 sur le P2 de la R2 #1104) : ne rend
        que les lots dont le jalon `merged` est à `since` ou après — la borne
        d'activation (`review_catch_up_bound`), jamais un balayage historique.
        Un lot sans jalon `merged` lisible est exclu (jamais deviné).

        `terminal_prefix`/`max_attempts` (même arbitrage) : un lot dont le
        journal porte une marque terminale `terminal_prefix<id>` est exclu
        **toujours** (non-light confirmé, doute épuisé) ; un lot dont le
        journal porte `max_attempts` tentatives `attempt_prefix<id>` ou plus
        est exclu de même (le traitement d'un doute est borné, jamais horaire
        à perpétuité)."""

    @abc.abstractmethod
    def review_catch_up_bound(self) -> str | None:
        """L539 (#1104) : la borne d'activation du rattrapage des relectures
        après fusion — la ligne unique écrite par la migration 0438 au moment
        de l'activation du lot. Commune aux exécuteurs (base partagée),
        conservée après redémarrage ; `None` si elle manque, et le rattrapage
        est alors fail-closed (aucun balayage historique). Rendue en ISO-8601
        (identique quel que soit le pilote), utilisable en paramètre SQL.
        Lecture pure : ni verrou ni écriture."""

    @abc.abstractmethod
    def move(self, item_id: int, state: str, *, current: str, loops: int, note: str,
             actor: str, held: bool = False) -> dict | None:
        """Passe le lot de `current` à `state` (boucle QA : `loops` ajouté) et
        journalise ; None (rien d'écrit) s'il n'est plus en `current`.

        `held` (L233): also require an open decision request for the lot, in
        the same statement, the request row locked: an answer closing it
        concurrently waits for this move (then releases the lot) or makes it
        fail (None) — the lot never ends waiting with no request."""

    @abc.abstractmethod
    def designate_reviewer(self, item_id: int, reviewer: str, *, actor: str,
                           event: str | None = None, remove: bool = False) -> dict | None:
        """#2076 : ajoute un relecteur désigné (ou le retire explicitement),
        et journalise ; None (rien d'écrit) si le lot n'est plus en `qa` ou si
        son assigné est devenu le relecteur à ajouter (la règle est relue dans
        l'écriture, pas seulement avant : #705 r2, B1).

        #1979 : `reviewer` vide efface toutes les désignations et `event` remplace la
        ligne de journal par défaut (« reviewer X designated ») — il porte
        alors le retrait, ou la marque « verdict non requis » de
        `work.waive_review` (`work.REVIEWER_NONE`).

        Les colonnes viennent des migrations 0421 et 0456 : avant elles, la lecture s'en
        passe (`item_columns`) et l'écriture est refusée avec la marche à
        suivre. Un exécuteur médié ne désigne jamais de relecteur (opération
        refusée au contrat L107/L109) : la désignation est un geste
        d'orchestrateur ou d'humain (`work.designate_reviewer`)."""

    @abc.abstractmethod
    def waiting_episodes(self, item_ids: Sequence[int]) -> dict[int, dict]:
        """L233: for each lot, its current waiting episode, stable across
        notes: `{id: {episode, start_ts, first_ts}}` — `episode`, the id of
        the lot's last journal line in another state than `waiting_human`
        (0 if none), `start_ts`, its time (the lot's creation if none),
        `first_ts`, the first `waiting_human` line after it (None if the
        state was set without a journal line)."""

    @abc.abstractmethod
    def note(self, item_id: int, state: str, text: str, actor: str, *, once: bool = False) -> None:
        """Journalise une note et rafraîchit `updated_at`.

        `once` sérialise sur le lot et n'insère pas un texte déjà présent.
        """

    @abc.abstractmethod
    def update_generated(self, item_id: int, *, title: str, body: str,
                         priority: int | None, note: str, actor: str) -> dict | None:
        """#4291 : rafraîchit le titre et le corps **générés** d'un lot ouvert
        (veille des versions), et ne fait que **monter** la priorité (1 est la
        plus haute) : une priorité choisie par un humain n'est jamais
        abaissée par une détection automatique. Écriture et ligne de journal
        `note` dans la même transaction, sous le verrou de ligne du lot ; une
        valeur inchangée n'écrit rien. Rend le lot relu, ou None s'il n'existe
        pas ou n'est plus ouvert.
        """

    @abc.abstractmethod
    def note_with_wake(self, item_id: int, state: str, text: str, actor: str,
                       wake_ts: float, *, once: bool = False) -> None:
        """#2466 : journalise une note et pose, dans la même transaction, le
        réveil programmé du lot (epoch) : une note `hold: …` ne laisse jamais
        la retenue sans son échéance si l'écriture s'interrompt."""

    @abc.abstractmethod
    def schedule_wake(self, item_id: int, wake_ts: float | None, *, text: str,
                      actor: str) -> dict | None:
        """#2466 : pose (ou efface avec `wake_ts=None`) le réveil programmé du
        lot et journalise le geste, en une transaction ; rend le lot relu, ou
        None s'il n'existe pas ou n'est plus ouvert. Un seul réveil par lot :
        le dernier posé remplace le précédent."""

    @abc.abstractmethod
    def log_review_attempt(self, item_id: int, state: str, text: str, actor: str) -> None:
        """L539 (#1104) : journalise une tentative de rattrapage sans création.

        Le texte commence par le marqueur `<attempt_prefix><id>` (premier mot),
        que `items_missing_ref` reconnaît ; le journal seul est écrit,
        `updated_at` reste intact (une tentative de fond ne réordonne pas la
        sélection et ne rend pas le lot actif)."""

    @abc.abstractmethod
    def lock_deep_analysis(self, item_id: int) -> None:
        """L317c : verrou consultatif de transaction propre au lot, pris avant
        de relire puis d'écrire une demande d'analyse profonde (deux demandes
        simultanées ne voient pas toutes deux « rien de demandé »). À appeler
        dans `db.transaction()`."""

    @abc.abstractmethod
    def assign(self, item_id: int, assignee: str, *, current: str | None, note: str,
               actor: str, queued_note: str | None = None,
               forced_note: str | None = None, take: bool = False,
               ready_only: bool = False) -> dict | None:
        """Réassigne le lot (L37) s'il a encore l'assigné `current` et n'est ni
        fusionné ni fermé, et journalise, en UNE transaction ; None sinon.
        L40 : efface la délégation en cours (issue `annulee`). #540 : avec
        `queued_note`, un lot non démarré ne va pas à un `assignee` qui a déjà
        un autre lot ouvert qui l'occupe (L165b, comme `add`) : il passe dans
        la file d'équipe (sans assigné) (`forced_note` de même). `take=True`
        (la prise automatique, `backlog.auto_take`) : même contrôle sous le
        même verrou, mais le lot reste où il est et `PendingLot` est levée.
        `ready_only` (L165) exige un lot intake sans assigné dont toutes les
        dépendances sont FUSIONNÉES (`merged`/`promoted`/`closed`, #2252) ;
        vérifié sous verrou dans la même transaction que l'attribution.
        `current` doit alors être NULL : toute autre valeur est une erreur
        d'appel (`ValueError` immédiate, avant le verrou) et non un None
        silencieux (#1696). La réservation manuelle garde son comportement par
        défaut. #705 : si le nouvel assigné est le relecteur désigné, la
        désignation tombe dans la même écriture (le journal la nomme) — un
        relecteur ne relit pas son propre travail. #3447 : un lot réservé à un
        exécutant externe (`external_executor`) n'est pas assignable : refusé
        sous le même verrou, tant que la réservation n'est pas levée."""

    @abc.abstractmethod
    def set_external_executor(self, item_id: int, executor: str | None, *,
                              note: str, actor: str) -> dict | None:
        """#3447 : pose (ou efface, `executor=None`) l'exécutant externe d'un
        lot ouvert, avec sa ligne de journal, en UNE transaction ; None si le
        lot est fermé, ou si la pose perd la course contre une attribution.
        Un lot assigné ne reçoit pas d'exécutant externe (deux porteurs)."""

    @abc.abstractmethod
    def count_builds(self, assignee: str) -> int:
        """§4 (owner 14/10/2026) : les lots en `build` de l'agent, tous projets
        confondus."""

    @abc.abstractmethod
    def assign_capped(self, item_id: int, assignee: str, *, current: str | None,
                      note: str, actor: str, max_build: int,
                      queued_note: str | None = None,
                      forced_note: str | None = None, take: bool = False,
                      ready_only: bool = False) -> dict | None:
        """`assign` sous la limite d'en-cours (§4), en UNE transaction : le
        verrou consultatif de l'agent est pris AVANT le compte de ses lots en
        `build`, si bien que deux prises simultanées du même agent ne peuvent
        pas franchir `max_build`. None si le plafond est atteint ou si le lot a
        bougé.

        Reprend les options de `assign` (#540 `queued_note`/`forced_note`/
        `take`, L165 `ready_only`) : la prise automatique vérifie donc le lot
        non démarré, les dépendances et le plafond §4 sous le même verrou.
        `ready_only` accepte un lot `intake` ou un lot `build` RENDU à la file
        (§4) dont toutes les dépendances sont satisfaites."""

    @abc.abstractmethod
    def unassign(self, item_id: int, *, current: str | None, note: str,
                 actor: str, states: Sequence[str] | None = None) -> dict | None:
        """§4 : rend le lot à la file — assignation retirée si le lot a encore
        l'assigné `current` et n'est ni fusionné ni fermé, journal dans la même
        transaction ; None sinon. `states` borne en plus les états rendus (le
        retour manuel §4 : `intake` ou `build`), relu dans la transaction qui
        écrit. Branche, tête de branche et jalons ne sont pas touchés ; L40 :
        la délégation en cours est annulée."""

    @abc.abstractmethod
    def unassign_surplus(self, item_id: int, *, current: str, marker: str,
                         prefixes: Sequence[str], max_build: int, note: str,
                         actor: str) -> dict | None:
        """§4 : restitution du SURPLUS d'un agent, sous les gardes
        transactionnelles — le verrou consultatif de l'agent (`assign_capped`)
        est pris AVANT toute relecture, puis TOUTES les lignes `build` de
        l'agent sont verrouillées (`FOR NO KEY UPDATE`, id croissant : ordre
        stable) AVANT de lire la moindre exception. Le candidat doit faire
        partie de ces lignes : encore en `build`, encore assigné à `current`,
        sans exception pilote (`marker` dans la note de sa dernière
        attribution, `prefixes`) ; l'agent doit encore dépasser `max_build`
        lots en `build` SANS exception, recensés sous ces verrous. None sinon,
        rien n'est écrit : deux balayages concurrents ne peuvent donc pas
        rendre plus que le surplus, aucun geste sur un AUTRE lot compté
        (réattribution ou exception, qui passent par un UPDATE de sa ligne) ne
        peut se glisser dans le compte, et un lot passé en `qa`, repris ou doté
        d'une exception avant la lecture n'est pas rendu. Journal et annulation
        de la délégation en cours dans la même transaction (comme
        `unassign`)."""

    @abc.abstractmethod
    def wip_exceptions(self, item_ids: Sequence[int], marker: str,
                       prefixes: Sequence[str]) -> list[int]:
        """§4 : parmi `item_ids`, ceux dont la DERNIÈRE attribution (note
        commençant par l'un des `prefixes`) porte `marker` — l'exception pilote
        explicite à la limite d'en-cours. Une réassignation postérieure sans le
        marqueur l'a retirée."""

    @abc.abstractmethod
    def handoff(self, item_id: int, assignee: str | None, *, current: str, state: str,
                paused_prefix: str, note_prefix: str, window_s: float,
                max_per_window: int, note: str, actor: str,
                still_paused=None) -> dict | None:
        """L233b : `current`, en pause budget, cède le lot à `assignee`, en UNE
        transaction sous verrou consultatif (une reprise à la fois pour tout
        le mesh), journalisée comme `assign` ; None si l'une des conditions
        ne tient plus au moment d'écrire : le lot a encore l'assigné `current`
        et l'état `state` (celui contrôlé) ; `current` est encore en pause
        (`blocked`, `status_text` commençant par `paused_prefix`, ligne
        verrouillée jusqu'à la fin) ; `assignee` n'a aucun lot ouvert ; moins
        de `max_per_window` notes commençant par `note_prefix` dans les
        `window_s` dernières secondes.

        `assignee` None rend le lot à la file d'équipe (assigné NULL, L165) :
        aucun contrôle du preneur n'a lieu, les autres tiennent.

        #4066 : `still_paused(status_text)`, s'il est donné, juge aussi la
        pause relue sous le verrou (une pause devenue la seule fenêtre courte
        du compte ne cède plus rien)."""

    @abc.abstractmethod
    def delegate(self, item_id: int, delegate: str, *, current: str | None,
                 delegated_by: str, within_s: float, note: str, actor: str) -> dict | None:
        """Délègue le lot (assigné encore `current`, lot ouvert) en UNE
        transaction : délégation en cours remplacée, lot au délégué avec
        délégant, début et échéance (`now + within_s`), ligne au registre des
        délégations, journal. Rend `{item, delegation, replaced}` ; None si
        le lot a bougé. #705 : déléguer à son relecteur désigné retire la
        désignation dans la même écriture (le journal la nomme)."""

    @abc.abstractmethod
    def current_delegation(self, item_id: int) -> dict | None:
        """La délégation en cours du lot (registre), avec `worked` (preuve de
        travail du délégué depuis son début), ou None."""

    @abc.abstractmethod
    def mark_delegate_turn(self, agent: str, item_ids, note: str) -> list[int]:
        """Premier tour de `agent` sur ces lots qui lui sont délégués : noté
        une fois au registre (`first_turn_at`) et au journal (acteur : le
        délégué). Rend les lots marqués."""

    @abc.abstractmethod
    def due_delegations(self, now_ts: float) -> list[dict]:
        """Délégations en cours échues à `now_ts`, avec le lot et `worked`."""

    @abc.abstractmethod
    def resolve_delegation(self, delegation_id: int, *, now_ts: float, actor: str,
                           describe) -> dict | None:
        """Traite une délégation échue en UNE transaction, sous le verrou de la
        ligne du lot (recontrôle après verrou) : `annulee` (lot fermé ou
        assigné changé), `soldee` (travail du délégué) ou `rendue` (retour au
        délégant). None si déjà traitée : un seul gagnant entre exécuteurs."""

    @abc.abstractmethod
    def overdue_delegations(self, now_ts: float) -> list[dict]:
        """Délégations en cours échues, non traitées, sur des lots ouverts."""

    @abc.abstractmethod
    def returned_delegations(self, since_ts: float) -> list[dict]:
        """Délégations rendues au délégant depuis `since_ts`."""

    @abc.abstractmethod
    def events(self, item_id: int, limit: int | None) -> list[dict]:
        """Journal du lot, du plus récent au plus ancien."""

    @abc.abstractmethod
    def events_noted(self, prefix: str, since_ts: float, *,
                     ids: Sequence[int] | None = None) -> list[dict]:
        """L317c : les lignes de journal dont la note commence par `prefix`,
        écrites depuis `since_ts` (les plus anciennes d'abord).

        `ids` borne la lecture à ces éléments (#2162, `backlog._DirNotes`) :
        sans lui, tous les lots ; avec lui, l'historique des lots clos n'est
        pas lu. Le filtre porte sur les ids, jamais sur une fenêtre
        temporelle : une note ancienne d'un élément candidat reste lue, donc
        dédupliquée exactement."""

    @abc.abstractmethod
    def corrections(self, ids: Sequence[int] | None = None) -> dict[int, float]:
        """#2317 (N2/N4) : instant de la dernière correction `merged → build`
        par lot, `{}` sans correction.

        Une correction se reconnaît à la **transition** du journal (un
        événement `build` dont l'état précédent est `merged`), jamais au texte
        de sa note — une note libre du même préfixe n'annule rien. `ids` borne
        la lecture aux lots candidats au lieu de relire tout le journal à
        chaque relevé."""

    @abc.abstractmethod
    def milestones(self, item_id: int, limit: int | None) -> list[dict]:
        """Les jalons d'un lot (0013), du plus récent au plus ancien.
        `None` lit tout l'historique, notamment les anciens blocages du train.
        Chaque ligne porte `annulled` : vrai pour un jalon `merged` qu'une
        correction `merged → build` postérieure a annulé (#2317)."""

    @abc.abstractmethod
    def verdicts_for_commit(self, commit: str) -> list[dict]:
        """All verdicts naming this exact commit, across lots, including hex
        abbreviations. Provenance and current opposition remain caller checks.
        No history limit: a block must not disappear behind newer rows.
        """

    @abc.abstractmethod
    def earlier_freezes(self, app: str, lot: int, before_ts: float) -> list[dict]:
        """Up to 256 earlier freezes of other lots in this project.

        A bounded provenance lookup: missing candidates retain unknown authors.
        """

    @abc.abstractmethod
    def add_milestone(self, item_id: int, kind: str, *, sha: str, actor: str,
                      verdict: str | None, note: str, on_frozen=None,
                      after_commit: list | None = None,
                      proof: dict | None = None) -> dict:
        """Écrit un jalon déclaré (`frozen`, `verdict`) et le rend.

        Un gel depuis `intake`, `build` ou `validated` entre en `qa` avant
        son callback (#3467/#4351), dans la même transaction. Un verdict
        retire la validation précédente. Cette tranche de compatibilité ne
        crée pas de lot `validated` : l’écrivain suivra le staging des lecteurs.
        Un lot épinglé à un Process conserve son état (#3489).
        Un état bloqué ou terminal n'est pas levé par ce geste.

        `on_frozen` (facultatif, #1617) : callback du gel, appelé dans la
        transaction du jalon `frozen` avec `(tx, milestone, after_commit)` ;
        `after_commit` reçoit les écritures de fil différées, que **l'appelant**
        vide après le commit qu'il tient (un `with` imbriqué ne commite pas).
        Absents, rien ne change.

        `proof` (facultatif, #1465) : l'identité exacte du tour qui déclare
        (`actor`, `session`, `turn`, `harness`, `sha` complet), écrite DANS LA
        MÊME TRANSACTION que le jalon sous le sujet `milestone:<id>`. Absent,
        le jalon s'écrit sans preuve : l'origine reste inconnue, jamais
        devinée. Les deux options sont indépendantes et se composent."""

    @abc.abstractmethod
    def delays(self, limit: int, *, ids: Sequence[int] | None = None) -> list[dict]:
        """Les délais par lot (R19), du plus récemment modifié au plus ancien.

        `ids` restreint le balayage à ces lots : `work list` s'en sert pour que
        les délais d'une ligne filtrée ne dépendent pas de la fenêtre globale
        des `limit` lots les plus récents (revue codex2, B3).

        Forme **stable** (L24 et Nexlink la consomment) : `work_item_id`,
        `state`, `assignee`, `title`, `created_ts`, `updated_ts`,
        `requested_ts`, `frozen_ts`, `reviewed_ts`, `reviewed_verdict`,
        `merged_ts`, `request_to_freeze_s`, `freeze_to_review_s`,
        `freeze_to_merge_s`, `review_to_merge_s`, `total_s`, `verdicts`,
        `blocked_verdicts`. Une étape absente est NULL, jamais zéro.
        `delivered_ts` (#988) s'y ajoute : la fin d'un lot livré hors cible,
        quand `merged_ts` reste NULL (`total_s` compte alors jusqu'à elle).
        """

    @abc.abstractmethod
    def timeline(self, item_id: int) -> dict | None:
        """Les délais d'un lot (même forme que [`delays`]), ou None."""

    @abc.abstractmethod
    def link_package(self, item_id: int, package_id: str | None, parent: str | None, *,
                     note: str, actor: str) -> dict | None:
        """Rattache le lot à une fiche WorkPackage (et à son parent), journalise ;
        None si le lot n'existe pas. UNE instruction."""

    @abc.abstractmethod
    def by_package(self, package_id: str) -> list[dict]:
        """Les lots rattachés à cette fiche, tous états confondus, par id."""

    @abc.abstractmethod
    def record_packet(self, package_id: int, *, branch: str, base_sha: str,
                      head_sha: str, units: Sequence[dict], note: str,
                      actor: str,
                      expect_pushed: str | None = None) -> list[dict]:
        """#2443 : enregistre la composition d'un paquet (lot porteur
        `package_id`, branche `branch`), en remplaçant l'ensemble de ses
        unités, en UNE transaction.

        `units` : des dicts `work_item_id`, `unit_sha`, `source_sha`,
        `frozen_sha`. Un lot porteur ou une unité inconnue annule tout
        (l'intégrité référentielle tranche) ; l'événement `note` est écrit au
        journal du porteur dans la même transaction. Rend les lignes relues
        par `packet_members` (ordre de `recorded_at` décroissant).

        #4501 : le porteur est verrouillé (`FOR NO KEY UPDATE`) avant toute
        lecture de sa tête poussée ; le dernier gel et `branch_head` sont relus
        **dans cette transaction**, et l'écriture est refusée si la tête
        poussée n'est ni `head_sha` ni `expect_pushed`. Un gel, un relevé ou un
        autre enregistrement concurrent est ainsi sérialisé avec l'écriture.
        """

    @abc.abstractmethod
    def packet_members(self, *, item_ids: Sequence[int] | None = None,
                       package_ids: Sequence[int] | None = None) -> list[dict]:
        """#2443 : les appartenances de paquet, filtrées par unités
        (`item_ids`) ou par porteur (`package_ids`), les plus récentes
        d'abord. Chaque ligne porte `package_id`, `work_item_id`,
        `package_branch`, `base_sha`, `head_sha`, `unit_sha`, `source_sha`,
        `frozen_sha`, `recorded_by`, `recorded_ts`, l'état du porteur
        (`package_state`), son jalon de fusion (`package_merged_sha`) et sa
        tête gelée (`package_frozen_sha`, #3634)."""

    @abc.abstractmethod
    def close_merged(self, item_id: int, *, current: str, sha: str, actor: str, note: str,
                     pr_ref: str | None, frozen_id: int | None = None,
                     landed: tuple[int, str] | None = None,
                     unfrozen: bool = False,
                     release_landed: tuple[int, str] | None = None) -> dict | None:
        """Fusion constatée (L29) : `current` → `merged` si le lot y est encore
        et, si `frozen_id` est donné, si ce jalon `frozen` est toujours le
        dernier gel du lot — `unfrozen` (#2444) : si le lot n'a toujours aucun
        gel (sinon None, rien d'écrit) ; journal et jalon
        `merged` (commit, auteur de la fusion) dans la même transaction.
        `landed` (#1567) : `(numéro, dépôt)` du numéro réellement atterri,
        écrit dans la même transaction."""

    @abc.abstractmethod
    def close(self, item_id: int, *, current: str, reason: str, superseded_by: int | None,
              sha: str = "", actor: str, note: str) -> dict | None:
        """Fermeture explicite (L29, #988) : `current` → `closed` (`abandoned` |
        `superseded` | `delivered` | `done`) si le lot y est encore, journal et
        jalon `closed` dans la même transaction ; `delivered` écrit en plus le
        jalon `delivered` (le `sha` de la tête relue, l'auteur) ; None sinon."""

    @abc.abstractmethod
    def rectify_close(self, item_id: int, *, current_reason: str, reason: str,
                      actor: str, note: str) -> dict | None:
        """Rectifie la raison d'une fermeture déjà écrite (#1280) : le lot reste
        `closed` — `closed_at` et le jalon `closed` sont conservés, la fermeture
        a bien eu lieu — seule la raison change (`superseded_by` est effacé) et
        l'événement est journalisé. None si le lot n'est plus `closed` avec
        `current_reason` (course perdue)."""

    @abc.abstractmethod
    def refresh_package_parents(self) -> int:
        """Recopie dans les lots le parent courant de leur fiche ; rend le nombre
        de lots changés."""

    # -- file d'amélioration (L119, décision 0037) ------------------------------
    @abc.abstractmethod
    def backlog_add(self, *, title: str, body: str, source: str, expected_value: str,
                    value_score: int, priority: int, team: str | None,
                    required_capabilities: list | None, package_id: str | None,
                    package_parent: str | None, note: str, actor: str,
                    app: str = "", estimate_minutes: int | None = None,
                    estimate_source: str | None = None, estimate_by: str | None = None,
                    change_size: str | None = None, footprint: list | None = None) -> dict:
        """Crée un élément de la file : un lot `improvement` en `intake`, sans
        assigné, et sa première ligne de journal ; rend le lot.

        `app`: its project (L118c), empty when unknown. #4117 : `estimate_minutes`
        (avec sa source et son auteur) et `change_size` sont posés en même temps
        que le lot et sa ligne d'historique (`work_item_estimates`), comme
        `work.add` (L157) ; `estimate_by` vaut `actor` quand il est omis.
        #4186 : `footprint` (chemins normalisés) est l'empreinte d'intake,
        écrite avec la création."""

    @abc.abstractmethod
    def backlog_basket_add(self, *, key: str, title: str, expected_value: str, line: str,
                           value_score: int, priority: int, team: str | None,
                           required_capabilities: list | None, package_id: str | None,
                           package_parent: str | None, note: str, actor: str,
                           app: str = "", estimate_minutes: int | None = None,
                           estimate_source: str | None = None,
                           estimate_by: str | None = None,
                           change_size: str | None = None,
                           estimate_required: bool = False,
                           footprint: list | None = None) -> dict:
        """#2584 (canon §2 l.129) : ajoute une ligne au panier ouvert `key`
        d'un chantier, ou ouvre ce panier ; rend le panier.

        Le panier est un lot `improvement` en `intake`, sans assigné, dont la
        `source` vaut `key` : la clé seule le désigne, l'équipe d'une ligne
        n'ouvre pas un second panier. La ligne s'ajoute au corps ; la valeur
        du panier est la plus haute de ses lignes, sa priorité la plus haute
        (le plus petit nombre), ses capacités exigées l'union des leurs, son
        équipe la première donnée (`team` de la ligne qui l'ouvre, sinon
        d'une suivante) ;
        `note` (qui commence par `BASKET_LINE_NOTE`) va au journal. Un panier
        déjà pris (assigné, ou sorti d'`intake`) est fermé aux lignes : la
        suivante ouvre un nouveau panier. Deux ajouts simultanés sur la même
        clé n'ouvrent jamais deux paniers (un écrivain par clé). Le lot rendu
        porte `basket_lines` (lignes au journal du panier) et `basket_opened`
        (vrai si cet ajout l'a ouvert).

        #4117 : l'estimation et la taille ne s'appliquent qu'au panier OUVERT
        par cet ajout ; une ligne ajoutée à un panier existant ne le réestime
        pas (aucun rétro-remplissage). `estimate_required` : un agent ne peut
        pas OUVRIR un panier sans estimation ni référence (L157) ; la garde
        est lue sous le verrou du panier, au point `opened`, pour qu'une ligne
        ajoutée à un panier déjà ouvert ne soit jamais refusée. Le refus lève
        `BasketEstimateRequired`, que `backlog.add` traduit.

        #4186 : `footprint` est l'union des chemins cités par cette ligne avec
        ceux déjà connus du panier (jamais un remplacement) ; l'empreinte est
        écrite avec la création de la ligne."""

    @abc.abstractmethod
    def backlog(self, *, open_only: bool, limit: int,
                ready_only: bool = False) -> list[dict]:
        """Les éléments de la file dans l'ordre de prise (priorité, puis valeur
        décroissante, puis ancienneté). `open_only` : seulement ceux qu'on peut
        encore prendre (`intake`, ou `build` RENDU à la file par la limite
        d'en-cours §4 — `RETURNED_NOTE` postérieur à la dernière attribution —
        sans assigné) ; sinon tous les éléments non terminés. `ready_only`
        exclut les dépendances ouvertes avant la borne pour la prise
        automatique ; la liste ordinaire les conserve."""

    @abc.abstractmethod
    def auto_takes_since(self, seconds: float, note_prefix: str) -> int:
        """Nombre de prises automatiques (lignes de journal dont la note
        commence par `note_prefix`) sur les `seconds` dernières secondes, tous
        hôtes confondus."""

    @abc.abstractmethod
    def project_plan_rows(self, project: str, *, assignment_id: int | None = None) -> list[dict]:
        """#4174: open dispatch readiness (bugs and evolutions, not the L119
        improvement queue), with `footprint_overlap` (#4173); assignment_id
        limits to target and unassigned bugs."""
        raise NotImplementedError

    @abc.abstractmethod
    def unassigned_lots(self, limit: int | None) -> list[dict]:
        """L165 : les lots du projet encore à confier (`intake`, ou `build`
        RENDU à la file par la limite d'en-cours §4 : `RETURNED_NOTE` postérieur
        à la dernière attribution, il garde son état et sa branche —, sans
        assigné, hors file d'amélioration, sans dépendance non fusionnée), dans
        l'ordre de prise : priorité, puis les `build` avant les `intake` à
        priorité égale, puis dates prévues (début, fin, livraison ; sans date en
        dernier), puis ancienneté. Chaque ligne ajoute `lot_team` (équipe du lot,
        sinon de sa fiche ou de la fiche parente, sinon son application),
        `pending_decision` (une demande de décision attend sur le lot),
        `decision_milestone` (un jalon de décision ouvert y est rattaché) et
        `returned_to_queue` (§4, le lot `build` a été rendu ; `false` pour un
        `intake`). #3447 : un lot réservé à un exécutant externe
        (`external_executor`) n'est pas offert — le filtre précède le LIMIT,
        comme les dépendances.
        `limit=None` reads the full queue for project phase selection (#4174).
        #1193: a lot whose explicit prerequisite is not finished is excluded —
        the ready intake never offers a lot that cannot be started.
        #2252 : « finished » veut dire fusionné (`merged`/`promoted`/`closed`) :
        un bloqueur en `qa`, même à verdict favorable, garde une tête qui peut
        encore changer ; la règle est celle de `work show` et de
        « planned: waiting on lots »."""

    @abc.abstractmethod
    def review_proposals(self, limit: int, *, proposal_prefix: str,
                         window_s: float = 21600.0) -> list[dict]:
        """#1677 (c) : les lots en `qa` qui attendent un geste de relecture —
        sans verdict COURANT (une relecture est due) ou à verdict courant
        bloquant (une correction est due) — avec leur auteur, leur branche,
        leur tête gelée, leur équipe résolue (`lot_team`) et les colonnes du
        lot dont la prise a besoin.

        Un lot déjà muni d'un relecteur désigné, ou sans tête gelée, est
        écarté ; un lot dont une proposition (`proposal_prefix` au journal) a
        moins de `window_s` n'est pas reproposé. Le verdict retenu est celui
        qui nomme le dernier gel (#1677 B1) : un `ok` périmé par un regel ne
        dispense pas de la relecture."""

    @abc.abstractmethod
    def claim_review_proposal(self, item_id: int, *, agent: str, kind: str,
                              proposal_prefix: str, window_s: float,
                              actor: str) -> bool:
        """#1738 (N5/N7) : enregistre la proposition d'un lot en `qa` sous un
        verrou consultatif par lot — le contrôle (fenêtre `window_s`,
        `proposal_prefix` au journal, lot encore en `qa`, agent différent de
        l'assigné), la désignation du relecteur (#705, pour `kind='review'`)
        et la note tiennent ensemble. Rend False sans rien écrire si un autre
        passage a déjà proposé le lot, si le lot n'est plus en `qa`, ou si
        l'agent en est l'assigné — deux passages (hôtes distincts) ne
        proposent jamais le même lot deux fois. #1738 R1 (B1) : sous le
        verrou, le lot est revalidé avec le prédicat de `review_proposals`
        (relecteur vide, verdict courant, gel) et `kind` doit y correspondre ;
        le relecteur n'est posé que s'il est encore vide (compare-and-set)."""

    @abc.abstractmethod
    def assignment_events(self, item_id: int, prefixes: Sequence[str]) -> list[dict]:
        """L233b : la dernière ligne de journal du lot dont la note commence
        par l'un des `prefixes` (une attribution), puis sa ligne la plus
        ancienne (la création), du plus récent au plus ancien — quelle que
        soit la longueur du journal."""

    @abc.abstractmethod
    def handoff_lots(self, assignee: str, limit: int) -> list[dict]:
        """L233b : les lots `intake` et `build` de `assignee`, hors file
        d'amélioration, dans l'ordre de `unassigned_lots`. Chaque ligne ajoute
        `lot_team`, `pending_decision`, `decision_milestone` (comme
        `unassigned_lots`), `package_team` (équipe de la fiche, pour
        `projects.lot_project`) et `frozen_ts` (dernier jalon de gel, None
        sans gel)."""

    # -- issues GitHub des lots (L126) ------------------------------------------
    @abc.abstractmethod
    def issue_feed(self, limit: int) -> list[dict]:
        """Les lots que la projection en issues examine : ouverts, porteurs
        d'une `issue_ref`, ou pinnés sur une révision Process (#2130/D : un
        cœur `merged` reste projeté ouvert), les `limit` plus récents, par id
        croissant. Chaque ligne ajoute l'équipe de la fiche du plan
        (`package_team`), l'équipe, le chantier et l'hôte de l'assigné au
        registre (`assignee_team`, `assignee_chantier`, `assignee_host`), et
        la révision Process du lot (`workflow_ref`, `workflow_stage`,
        `workflow_version` ; `''`, NULL, 0 sur une base pré-0452). Une
        instruction."""

    @abc.abstractmethod
    def set_issue_ref(self, item_id: int, issue_ref: str | None, *,
                      current: str | None,
                      migration_repo: object = UNCHECKED) -> bool:
        """Pose `issue_ref` si le lot porte encore `current` (NULL et chaîne
        vide confondus) : faux si la valeur a changé entre-temps (un autre
        projecteur l'a posée). Ni journal ni `updated_at` : une projection
        n'est pas une activité du lot.

        #1654 : `migration_repo` est l'observation de l'appelant ; fournie,
        elle resserre le CAS sur la réservation (`IS NOT DISTINCT FROM`, NULL
        compris), pour qu'une réservation prise entre la lecture et l'écriture
        ne passe pas en silence. `UNCHECKED` (défaut) garde le CAS d'origine."""

    @abc.abstractmethod
    def recent_or_cited(self, *, since_ts: float, ids: Sequence[int]) -> list[dict]:
        """L130 : les lots créés depuis `since_ts`, et ceux dont le numéro est
        dans `ids` (cités par un message), quel que soit leur âge : `id`,
        `title`, `source`, `body`, `created_ts`. Lecture seule."""


# --------------------------------------------------------------------------
# plan de travail (`work_packages`, L29) : copie des fiches WorkPackage
# --------------------------------------------------------------------------

class WorkPackages(Domain):
    """Fiches WorkPackage du canon, recopiées par `canon sync` (déclaratif)."""

    @abc.abstractmethod
    def all(self, *, include_absent: bool = False) -> list[dict]:
        """Les fiches (présentes au canon seulement, sauf `include_absent`), par id :
        id, kind, title, parent, responsible, team, scope (liste ou None),
        status, canon_ref, canon (L42 ; NULL = canon par défaut), present, synced_ts ;
        L96 : dates du canon `start_on`, `end_on`, `delivery_on` et dates posées
        dans ameesh `planned_start`, `planned_end`, `planned_delivery`,
        `planned_source`, `planned_by` (jours ISO ou None)."""

    @abc.abstractmethod
    def get(self, ident: str) -> dict | None:
        """Une fiche (présente ou retirée), ou None."""

    @abc.abstractmethod
    def upsert(self, row: dict) -> None:
        """Écrit une fiche (présente)."""

    @abc.abstractmethod
    def retire(self, idents: Sequence[str]) -> int:
        """Marque ces fiches retirées du canon (`present` faux, jamais effacées)."""

    @abc.abstractmethod
    def lock_sync(self) -> None:
        """Verrou transactionnel des fiches, pris AVANT leur lecture (L42/0031) :
        un par SCHÉMA, tous canons confondus — les identifiants de paquets sont
        globaux, le premier canon qui en déclare un le garde — et le même pour
        toute écriture de `canon` (`canon.rebase_default`). À appeler dans une
        transaction ouverte : il tient jusqu'au COMMIT, comme
        `authenticators.lock_registry`. L'attente du verrou ne compte pas dans
        le `statement_timeout` de la session (un premier sync long ne fait pas
        échouer les suivants), mais elle reste BORNÉE (#1747) : par le
        `lock_timeout` que le pilote Postgres pose le temps de la prise, et,
        sous psql, par le délai client de chaque instruction
        (`statement_timeout` + 5 s) — la plus courte des deux s'applique."""


# --------------------------------------------------------------------------
# actions et porte (`actions`, `action_attempts`, `action_events`)
# --------------------------------------------------------------------------

class Actions(Domain):
    """Actions, tentatives et journal.

    `launch`, `settle` et `replace` sont chacune UNE transaction qui prend ses
    verrous puis recontrôle les échéances (reçu : exp, iat ; grant : until) à
    l'heure réelle de la base, après le dernier verrou et avant d'écrire ;
    elles rendent une ligne `result` / `detail` (et les champs propres à la
    transition), jamais une exception pour un refus.
    """

    @abc.abstractmethod
    def get(self, action_id: str, *, with_receipts: bool) -> dict | None:
        """Une action (avec ses reçus si demandé), ou None."""

    @abc.abstractmethod
    def recent(self, *, state: str | None, project: str | None, limit: int) -> list[dict]:
        """Actions, de la plus récente à la plus ancienne."""

    @abc.abstractmethod
    def attempts(self, action_id: str) -> list[dict]:
        """Tentatives de l'action, dans l'ordre."""

    @abc.abstractmethod
    def events(self, action_id: str) -> list[dict]:
        """Journal de l'action, dans l'ordre."""

    @abc.abstractmethod
    def log_event(self, action_id: str, attempt_no: int, event: str, from_state: str | None,
                  to_state: str | None, actor: str, note: str) -> None:
        """Événement informatif du journal (sans transition)."""

    @abc.abstractmethod
    def last_event_note(self, action_id: str, event: str) -> str | None:
        """Note du dernier événement `event` de l'action, ou None."""

    @abc.abstractmethod
    def decision_queues(self) -> tuple[list[dict], list[dict], list[dict]]:
        """(à approuver avec reçu, issue inconnue non remplacée, lancées échues)."""

    @abc.abstractmethod
    def covering_grants(self, action_id: str, amount: int, connector: str, operation: str,
                        action_class: str, currency: str | None,
                        canon: str = "") -> list[dict]:
        """Grants vivants qui couvrent l'action (pré-filtre sans verrou), avec
        `live` : une réservation vivante de l'action existe déjà. L44 : seuls
        ceux dont l'authentificateur est déclaré par `canon`."""

    @abc.abstractmethod
    def launched(self, *, action_id: str | None, grace: float, force: bool) -> list[dict]:
        """Actions `launched` dont l'échéance + `grace` est passée (toutes si `force`)."""

    @abc.abstractmethod
    def propose(self, *, action_id: str, project: str, work_item: int | None,
                proposed_by: str, connector: str, operation: str, target: str,
                args_json: str, action_class: str, amount: int | None,
                currency: str | None, policy_version: str, digest: str, dedupe: str,
                requires_receipt: bool, approvers_json: str, note: str) -> None:
        """Enregistre une action `proposed`."""

    @abc.abstractmethod
    def bind(self, action_id: str, *, from_states: tuple, digest: str, auth: dict,
             by: str) -> bool:
        """Lie une autorisation (`→ approved`) si l'action est encore dans
        `from_states`, non remplacée, d'empreinte `digest`."""

    @abc.abstractmethod
    def launch(self, action_id: str, *, digest: str, auth_kind: str, auth_nonce: str | None,
               auth_grant_id: int | None, by: str, timeout: float,
               clock_skew: int) -> dict | None:
        """approved → launched + consommation du nonce ou réservation + tentative."""

    @abc.abstractmethod
    def settle(self, action_id: str, attempt: int, *, from_state: str, state: str,
               external_ref: str | None, error: str | None, by: str, note: str,
               settled_by: str, stale_after: float | None) -> dict | None:
        """Issue d'une tentative, par transition conditionnelle depuis `from_state`."""

    @abc.abstractmethod
    def replace(self, action_id: str, *, digest: str, new_id: str, new_digest: str,
                receipt_json: str, approver: str, nonce: str, challenge: str,
                authenticator_id: int, by: str, clock_skew: int) -> dict | None:
        """Consomme la décision « assumer le doublon » et crée l'action qui remplace."""

    @abc.abstractmethod
    def cancel(self, action_id: str, *, by: str, note: str) -> bool:
        """`proposed | approved | failed → cancelled` ; faux sinon."""


class ActionSource(Domain):
    """Lecture SEULE, par ameesh-approve, de la table d'actions configurée
    (`action_table`, `actions` par défaut ; nom d'identifiant SQL simple,
    refusé sinon) : le service relit l'action lui-même, jamais un texte
    d'agent, et vérifie d'abord que la table a les colonnes de l'empreinte."""

    @abc.abstractmethod
    def columns(self, table: str) -> set:
        """Noms des colonnes vivantes de `table` ; vide si elle n'existe pas."""

    @abc.abstractmethod
    def rows(self, table: str, action_id: str, columns) -> list[dict]:
        """Lignes de `table` pour cet `action_id`, réduites aux `columns`
        nommées (jamais `*` : le rôle d'approve n'a de droits que sur elles)."""


# --------------------------------------------------------------------------
# canon : état par hôte, colonnes déclaratives, retrait, éphémères
# --------------------------------------------------------------------------

class Canon(Domain):
    """Ce que `canon sync` écrit au registre (§4.4) : état du canon de l'hôte
    (`canon_state`), colonnes DÉCLARATIVES des agents, arrêt d'un agent
    retiré du canon. Jamais les colonnes d'état (bail, session, dépense,
    consigne), sauf `status` / `status_text` d'un agent retiré ou réintégré."""

    @abc.abstractmethod
    def record_state(self, host: str, status: str, *, root: str, source: str,
                     commit: str | None, good: bool, diagnostic: str,
                     canon: str = "", canon_id: str | None = None,
                     expected: str | None = None) -> str | None:
        """Écrit l'état du canon de l'hôte ; le dernier commit valide ne suit que
        `good`. L42 (0031) : clé (host, canon), '' = canon par défaut ;
        `canon_id` : identifiant réel du canon (gardé s'il n'est pas fourni).

        #3955 : `expected` (None : sans condition) rend l'avance du dernier
        commit valide CONDITIONNELLE — la valeur enregistrée doit encore valoir
        `expected` ('' : aucune) pour que `last_good_commit` / `last_good_at`
        suivent `commit` ; une synchronisation concurrente qui a avancé la
        ligne entre la lecture et l'écriture est donc détectée. #3964 : la
        provenance (statut, racine, source, diagnostic, horodatage) suit le
        même CAS — une écriture au CAS perdu laisse celle du gagnant. Rend la
        valeur EFFECTIVE de `last_good_commit` après l'écriture (None :
        aucune) : l'appelant qui attend `commit` sait que son écriture a été
        retenue, sinon il relit et rejoue (cas d'usage
        `canon_sync.record_state`)."""

    @abc.abstractmethod
    def state(self, host: str, canon: str = "") -> dict | None:
        """Dernier état enregistré du canon `canon` ('' : par défaut) de l'hôte, ou None."""

    @abc.abstractmethod
    def states(self, host: str) -> list[dict]:
        """Tous les états enregistrés pour l'hôte, un par canon (L42)."""

    @abc.abstractmethod
    def claim_commit(self, host: str, old: str, new: str, canon: str = "") -> bool:
        """#793 : avance le repère d'envoi `process_commit` de `old` à `new`,
        si aucune autre synchronisation ne l'a fait ; False sinon. NULL :
        initialiser depuis `last_good_commit`. `record_state` ne touche jamais
        ce repère. Dans la transaction de l'appelant, la ligne reste verrouillée
        jusqu'à son commit."""

    @abc.abstractmethod
    def key_for_root(self, host: str, root: str) -> str | None:
        """Clé (non vide) d'un canon non par défaut déjà enregistré pour l'hôte
        avec cette racine, ou None (L42 : identité d'un canon illisible)."""

    @abc.abstractmethod
    def record_auth_state(self, host: str, status: str, diagnostic: str,
                          canon: str = "", *, quiet_prefix: str = "") -> dict | None:
        """Atomic transition; returns the previous state under its row lock.

        quiet_prefix marks only a first transient error (previous status not
        error); a repeated error stores the unmarked diagnostic atomically.
        """

    @abc.abstractmethod
    def close_others(self, host: str, keep: Sequence[str], diagnostic: str) -> list[str]:
        """Passe `invalid` les états `ok` de l'hôte dont la clé n'est pas dans
        `keep` (canons retirés de la configuration, L42) ; rend leurs clés."""

    @abc.abstractmethod
    def claim_sync(self, host: str, canon: str, token: str, interval_s: float) -> bool:
        """Claim one periodic sync per host/canon/interval in a short statement."""

    @abc.abstractmethod
    def complete_sync(self, host: str, canon: str, token: str, signature: str) -> None:
        """Publish completion only if this claim is still current."""

    @abc.abstractmethod
    def shared_sync(self, host: str, canon: str) -> dict | None:
        """Completed current claim's state; None while pending or absent."""

    @abc.abstractmethod
    def lock_default_state(self, host: str) -> dict | None:
        """L46 : la ligne '' (canon par défaut) de l'hôte, lue `FOR UPDATE`
        (dans la transaction de l'appelant), ou None."""

    @abc.abstractmethod
    def set_default_id(self, host: str, canon_id: str) -> None:
        """L46 : inscrit l'identifiant du canon par défaut sur la ligne '' de
        l'hôte (dans la transaction du rebase)."""

    @abc.abstractmethod
    def flag_default(self, host: str, status: str, diagnostic: str) -> None:
        """L46 : statut et diagnostic de la ligne '' seulement (ni racine, ni
        source, ni identifiant)."""

    @abc.abstractmethod
    def rebase_default(self, host: str, previous: str, new_default: str = "") -> dict:
        """Changement de canon par défaut (L42) : les lignes sans canon de
        l'hôte (et les paquets sans canon) reçoivent l'identifiant `previous`.
        L44 : les actions aussi ('' → `previous`, puis `new_default` → '').
        L46 : les lignes (agents, éphémères compris, et paquets) du nouveau
        canon par défaut `new_default` repassent à NULL.
        #1464 (N2 #793) : l'état recopié sous la clé `previous` emporte le
        repère d'envoi `process_commit` — sans lui, les règles déjà poussées
        repartent une fois (repli sur `last_good_commit`)."""

    @abc.abstractmethod
    def host_rows(self, host: str, names: Sequence[str]) -> list[dict]:
        """Agents de l'hôte (et ceux nommés) avec colonnes déclaratives, statut,
        `lease_epoch`, `profile_fresh` et `in_turn`."""

    @abc.abstractmethod
    def lot_projects(self) -> list[str]:
        """L243 : les projets des lots (`app`), distincts."""

    @abc.abstractmethod
    def write_declared(self, name: str, values: dict) -> bool:
        """Écrit les colonnes déclaratives (crée l'agent au besoin) et le profil
        évalué des valeurs écrites, dans la même instruction. Retourne False
        si le CAS refuse une lecture périmée : la politique n'est pas appliquée."""

    @abc.abstractmethod
    def set_election(self, name: str, releve: str, candidats: Sequence[str] | None,
                     *, canon: str) -> None:
        """L165 (EP1) : écrit `releve` / `candidats` de la ligne du canon et
        retire les verdicts de candidats qui ne sont plus des candidats (ou
        tous si le rôle redevient `hote`). Ne déplace jamais l'agent."""

    @abc.abstractmethod
    def write_candidate(self, name: str, host: str, *, canon: str, cwd: str | None,
                        ok: bool | None, diagnostic: str, ref: str | None,
                        harness: str | None, provider: str | None,
                        model: str | None, credential_mode: str | None) -> None:
        """L165 (EP1) : inscrit le verdict de placement de `name` sur l'hôte
        candidat `host` (profil évalué calculé dans la même instruction), pour
        que la réclamation depuis cet hôte soit jugée sur SA politique."""

    @abc.abstractmethod
    def candidate_names(self, host: str, canon: str = "") -> list[str]:
        """L165 (EP1) : noms des agents qui ont un verdict de candidat sur
        `host`, pour que le sync de CET hôte voie aussi les fiches retirées
        dont la ligne est encore chez un autre détenteur."""

    @abc.abstractmethod
    def close_candidates(self, name: str, *, canon: str, diagnostic: str) -> None:
        """L165 (EP1) : ferme TOUS les verdicts candidats d'un agent dont la
        fiche est en erreur (responsable non résolu, candidat non admis…),
        quel que soit l'hôte qui l'a constaté. Laisse `releve` intact ; une
        synchronisation ultérieure rouvrira les hôtes valides."""

    @abc.abstractmethod
    def clear_candidate(self, name: str, host: str, *, canon: str) -> None:
        """L165 (EP1) : retire le verdict candidat de `name` sur `host` (hôte
        qui n'est plus admis), sans toucher aux autres candidats."""

    @abc.abstractmethod
    def clear_responsible(self, name: str, host: str,
                          owner: tuple[str, bool] | None = None) -> None:
        """Vide le responsable d'un agent non éphémère de l'hôte ; `owner`
        (L42) : (id, défaut) du canon auquel la ligne doit appartenir."""

    @abc.abstractmethod
    def set_placement(self, name: str, host: str, *, ok: bool | None, diagnostic: str,
                      ref: str | None, profile: str | None) -> None:
        """Écrit un verdict de placement s'il change ; profil None : le profil
        courant de la ligne, calculé dans la même instruction."""

    @abc.abstractmethod
    def lineage_rows(self) -> list[dict]:
        """Éphémères et leurs créateurs, avec verdicts et profil courant."""

    @abc.abstractmethod
    def revive(self, name: str, text: str, stop_mark: str) -> bool:
        """Un agent arrêté par sync (`status_text` commençant par `stop_mark`)
        repart `idle` ; faux s'il n'est pas dans ce cas."""

    @abc.abstractmethod
    def clear_pending_stop(self, name: str, pending_mark: str) -> None:
        """Efface la marque d'arrêt demandé en fin de tour."""

    @abc.abstractmethod
    def move_host(self, name: str, host: str, *, target: str, canon_ref: str,
                  diagnostic: str, ref: str | None) -> None:
        """Suit un déplacement décidé au canon : placement à réévaluer sur `target`."""

    @abc.abstractmethod
    def relocate(self, name: str, host: str, *, target: str, canon_ref: str,
                 diagnostic: str, ref: str | None, keep_session: bool,
                 owner: str, epoch: int, summary: str = "") -> dict | None:
        """Déplace l'exécution vers un autre hôte ADMIS (L31, 0028), entre deux
        tours : `host` devient `target`, le verdict de placement est effacé (le
        sync de l'hôte d'arrivée le réévalue). `keep_session` conserve la
        session native (stockage partagé) ; sinon elle est oubliée et `summary`
        ouvre la consigne en attente (rotation avec résumé).

        Fencé par le bail : verrou de ligne d'abord, puis recontrôle DANS
        l'écriture que `owner`/`epoch` détiennent toujours un bail VIVANT, que
        l'agent n'est pas en tour, et que `target` figure encore dans les
        hôtes admis. Rend la ligne (`name`, `host`), ou None (worker périmé,
        tour en cours, destination plus admise)."""

    @abc.abstractmethod
    def relocate_operator(self, name: str, host: str, *, target: str, canon_ref: str,
                          diagnostic: str, ref: str | None, keep_session: bool,
                          runner_stale_s: float, summary: str = "",
                          source_stopped: bool = False) -> dict | None:
        """Déplacement MANUEL d'un agent `releve: hote` entre deux hôtes admis
        (lot #3299), sans bail vivant de l'appelant.

        Mêmes gardes que `relocate` (hôte d'arrivée dans `admitted_hosts`,
        aucun tour en cours, aucun handoff ouvert, aucun `host_hold` sur
        l'arrivée), sauf le bail : il n'est pas exigé quand l'exécuteur de
        départ est arrêté — `runner_seen_at` plus vieux que `runner_stale_s`
        (horloge de la base), `runner_seen_host` d'un autre hôte, ou
        `source_stopped` (preuve locale : l'unité de l'exécuteur est
        inactive, et le dernier relevé vient bien de l'unité DE CET agent,
        `runner_seen_owner` = `runner-<nom>@…` ; le gabarit systemd répond
        `inactive` pour tout nom jamais instancié). Un bail VIVANT n'est
        accepté que dans ce cas : un exécuteur qui sert encore l'agent n'est
        jamais dépossédé en silence. Un agent `releve: election` est refusé
        (son élection déplace son bail).

        `host` est l'hôte attendu (recontrôlé sous le verrou). `keep_session`
        garde la session native et la consigne ; sinon la session est oubliée,
        `session_reset_at` posé, le statut remis à `queued` (sauf `stopped`,
        conservé) et `summary` ouvre la consigne en attente.

        Le bail de la source est **révoqué dans la même écriture**, même avec
        `keep_session` : `lease_owner` et `lease_expires_at` vidés,
        `lease_epoch` incrémenté. `renew` et `begin_turn` ne contrôlent que
        owner/epoch, pas l'hôte ; sans cette révocation, un exécuteur de départ
        encore vivant garderait l'agent et l'arrivée ne pourrait pas le
        réclamer (B1 des deux relectures, #3299). Rend la ligne (`name`,
        `host`, et le bail révoqué `previous_lease_owner` /
        `previous_lease_epoch`), ou None si une garde a changé depuis la
        lecture."""

    @abc.abstractmethod
    def stop_removed(self, name: str, host: str, *, pending_text: str,
                     stop_text: str, owner: tuple[str, bool] | None = None,
                     expected_lease_epoch: int | None = None) -> dict | None:
        """Arrête un agent retiré du canon (sauf en plein tour : arrêt demandé) ;
        rend name et status, ou None s'il n'y avait rien à faire. `owner` (L42) :
        (id, défaut) du canon auquel la ligne doit appartenir — jamais celle
        d'un autre canon. `expected_lease_epoch` (L165) : le retrait constaté
        sur un AUTRE hôte que celui de la ligne ne s'applique que si le bail
        n'a pas changé depuis la lecture (aucune réclamation concurrente
        n'est tuée)."""


class Ephemerals(Domain):
    """Agents éphémères (R14) : créés depuis la ligne de leur créateur."""

    @abc.abstractmethod
    def creator(self, name: str) -> dict | None:
        """Le créateur : responsable, éphémère, statut, capacités, `alive`."""

    @abc.abstractmethod
    def create(self, name: str, creator: str, *, cwd: str | None,
               ttl_seconds: float) -> dict | None:
        """Crée l'éphémère atomiquement (INSERT … SELECT sur la ligne du
        créateur) ; None si le nom est pris ou si le créateur a changé."""

    @abc.abstractmethod
    def exists(self, name: str) -> bool:
        """Un agent de ce nom existe-t-il ?"""


# --------------------------------------------------------------------------
# registre des authentificateurs (§8.2) : verrou, journal des synchronisations
# --------------------------------------------------------------------------

class RegistryLockError(RuntimeError):
    """Écriture du registre des authentificateurs hors du chemin autorisé."""


#: preuve réservée aux pilotes : seul `Authenticators.lock_registry` d'un
#: pilote rend un jeton (jamais un module métier)
_LOCK_PROOF = object()


class RegistryLock:
    """Jeton : le verrou du registre est pris dans la transaction de `db`.

    Rendu par `lock_registry` seulement (le constructeur refuse tout autre
    appelant) ; l'écriture du registre revérifie de toute façon, auprès de la
    base, que la session de `db` détient le verrou avant d'écrire."""

    __slots__ = ("db",)

    def __init__(self, db: Any, proof: object = None):
        if proof is not _LOCK_PROOF:
            raise RegistryLockError("RegistryLock: only issued by receipts.lock_registry")
        self.db = db


class Authenticators(Domain):
    """Registre de confiance des authentificateurs (`authenticators`), son
    verrou et le journal des synchronisations (`authenticator_syncs`).

    L44 (0031) : registre et journal PAR CANON (`canon` : '' = canon par
    défaut, l'identifiant du canon sinon) ; le verrou reste unique."""

    @abc.abstractmethod
    def lock_registry(self) -> RegistryLock:
        """Prend le verrou TRANSACTIONNEL du registre (clé du seul registre du
        schéma) dans la transaction courante ; rend son jeton. Réentrant."""

    @abc.abstractmethod
    def registry_lock_held(self) -> bool:
        """La session courante détient-elle le verrou du registre ?"""

    @abc.abstractmethod
    def under_registry_lock(self, work: Callable[[RegistryLock], Any]) -> Any:
        """UNE transaction : verrou du registre pris AVANT tout contrôle, puis
        `work(jeton)` (contrôles, lectures et écritures par `jeton.db`), puis
        COMMIT ; toute exception annule la transaction entière."""

    @abc.abstractmethod
    def canon_refs(self, revocations: Sequence[str], canon: str = "") -> list[dict]:
        """`canon_ref` distincts des lignes du canon, actives ou révoquées par sync."""

    @abc.abstractmethod
    def last_sync(self, canon: str = "") -> dict | None:
        """Dernière synchronisation appliquée du canon (son journal), ou None."""

    @abc.abstractmethod
    def journal_head(self, canon: str = "") -> int | None:
        """Id de la dernière ligne du journal du canon, ou None s'il est vide."""

    @abc.abstractmethod
    def append_sync(self, *, root_member: str, root_commit: str, commits_json: str,
                    branch: str, trust: str, host: str, summary_json: str,
                    expected: int | None, canon: str = "") -> bool:
        """Journalise une synchronisation du canon SI la dernière ligne de son
        journal est encore `expected` (sinon rien, faux)."""

    @abc.abstractmethod
    def rebase_default(self, previous: str, new_default: str) -> dict:
        """Changement de canon par défaut : '' → `previous`, puis
        `new_default` → '' (registre et journal), sous le verrou du registre."""

    @abc.abstractmethod
    def registered(self, *, approver: str | None, include_revoked: bool,
                   canon: str | None = None) -> list[dict]:
        """Authentificateurs (actifs seulement, sauf `include_revoked`) ; d'un
        seul canon si `canon` n'est pas None."""

    @abc.abstractmethod
    def for_approver(self, approver: str) -> list[dict]:
        """Tous les authentificateurs de l'approbateur, révoqués compris."""

    @abc.abstractmethod
    def active_holders(self, facade: str, credential_id: str, canon: str = "") -> list[dict]:
        """Approbateurs qui détiennent ce credential actif dans ce canon."""

    @abc.abstractmethod
    def update_meta(self, authenticator_id: int, *, level: str, aaguid: str | None,
                    canon_ref: str) -> bool:
        """Niveau, aaguid et provenance d'une ligne active (même clé)."""

    @abc.abstractmethod
    def revoke(self, authenticator_id: int, *, reason: str, canon_ref: str | None) -> bool:
        """Révoque une ligne active, en notant le commit qui le constate."""

    @abc.abstractmethod
    def insert(self, record: dict) -> bool:
        """Ajoute une ligne active ; faux si une ligne active a déjà ce credential."""


# --------------------------------------------------------------------------
# approbations permanentes bornées (`standing_approvals`, §8.3)
# --------------------------------------------------------------------------

class Grants(Domain):
    """Grants et réservations. `register`, `reserve` et `release` sont chacune
    UNE transaction (verrous, puis échéances recontrôlées à l'heure réelle de
    la base après le dernier verrou) ; une échéance dépassée lève une erreur
    de base `ameesh_echeance [expired|iat_future]`."""

    @abc.abstractmethod
    def register(self, *, approver: str, nonce: str, challenge: str, authenticator_id: int,
                 receipt_json: str, connector: str, operations_json: str, action_class: str,
                 max_amount: int, currency: str | None, exp: int, iat: int, until: int,
                 clock_skew: int, registered_by: str) -> int | None:
        """Consomme le nonce du reçu `standing` et crée le grant ; rend son id,
        ou None (nonce déjà consommé, authentificateur révoqué)."""

    @abc.abstractmethod
    def reserve(self, grant_id: int, action_id: str, amount: int, *, reserved_by: str | None,
                connector: str, operation: str, action_class: str,
                currency: str | None) -> dict | None:
        """Réserve `amount` pour la tentative ; None = pas de couverture."""

    @abc.abstractmethod
    def release(self, reservation_id: int, reason: str | None) -> dict | None:
        """Libère une réservation (une seule fois) ; None si déjà libérée ou inconnue."""

    @abc.abstractmethod
    def live_reservations(self, action_id: str) -> list[dict]:
        """`grant_id` des réservations vivantes de l'action, dans l'ordre."""

    @abc.abstractmethod
    def candidates(self, connector: str, operation: str, action_class: str, amount: int,
                   currency: str | None, canon: str = "") -> list[dict]:
        """Grants qui pourraient couvrir l'action (pré-filtre sans verrou) ;
        L44 : seuls ceux dont l'authentificateur est déclaré par `canon`."""

    @abc.abstractmethod
    def revoke(self, grant_id: int, by: str) -> bool:
        """Révoque un grant actif."""

    @abc.abstractmethod
    def get(self, grant_id: int) -> dict | None:
        """Un grant, ou None."""


# --------------------------------------------------------------------------
# index des fils lisibles (`thread_index`)
# --------------------------------------------------------------------------

class ThreadCensus(TypedDict):
    """Census d'un fil d'hôte médié, porté par `threads.recount` (L361b) :
    la plus récente entrée gardée, telle que l'index la cite.

    `mailbox_id` est la dernière boîte citée par l'entrée, None si elle n'en
    cite aucune. L'ordre des champs est celui du schéma du contrat."""

    entry_id: str
    author: str
    excerpt: str
    ts: float
    mailbox_id: int | None


class Threads(Domain):
    """Index des fils (le fil lui-même vit chez son transport, hors base)."""

    @abc.abstractmethod
    def index(self, *, project: str, lot: str, transport: str, host: str, location: str,
              entry_id: str, mailbox_ids: Sequence[int], author: str, excerpt: str,
              ts: float, trace: dict, count: bool = True) -> None:
        """Compte l'entrée (sauf `count` faux) dans l'index du fil et, si elle porte des messages
        de la boîte, y note `trace` (id externe) — en une seule écriture.

        `count` faux (L361c) : la trace seule — la boîte est marquée et
        `location` rafraîchie, jamais `entries` ni `last_*`. Le census du
        fichier (`recount`) est alors le seul à écrire l'index : la trace d'une
        entrée expirée entre-temps ne peut pas la ressusciter."""

    @abc.abstractmethod
    def indexed(self) -> list[dict]:
        """Fils indexés, du plus récemment écrit au plus ancien."""

    @abc.abstractmethod
    def lock(self, *, project: str, lot: str, transport: str, host: str,
             location: str) -> int | None:
        """Verrouille la ligne du fil jusqu'à la fin de la transaction (créée
        vide au besoin) : expiration et indexation d'un même fil passent l'une
        après l'autre (L361). Rend `counted_until` : la fin logique du fil au
        dernier recomptage (`expired`), None s'il n'a jamais été recompté."""

    @abc.abstractmethod
    def recount(self, *, project: str, lot: str, transport: str, host: str, location: str,
                kept: int, last: ThreadCensus | None, end: int) -> int:
        """L361b : verrou, recomptage et expiration de l'index en UNE
        transaction serveur, pour un fil dont le fichier vit sur l'hôte médié.

        Le census vient de l'hôte (`kept` entrées gardées, `last` la plus
        récente, `end` la fin logique du fil) : sous le verrou de la ligne
        (`lock`), l'index reprend ce census (`expired`) si son `counted_until`
        n'est pas déjà AU-DELÀ de `end` — un recomptage plus récent fait foi.
        Un `counted_until` égal à `end` n'est repris que si le census ne
        rallonge pas la ligne (`kept <= entries`) : à fin logique constante,
        seule une expiration change le fil (moins d'entrées gardées), donc un
        census plus long a été lu avant elle et est périmé (L361c). Rend le
        `counted_until` retenu."""

    @abc.abstractmethod
    def expired(self, *, project: str, lot: str, transport: str, host: str,
                kept: int, last: dict | None, end: int) -> None:
        """Après une expiration (L361), sous `lock` : `kept` entrées restent ;
        `last` (entry_id, mailbox_id, author, excerpt, ts) est la plus récente
        gardée, None si le fil est vide (aucune entrée expirée n'est citée) ;
        `end`, la fin logique du fil recompté, devient `counted_until`."""


# --------------------------------------------------------------------------
# placement gouverné (colonnes de `agent_registry`, 0022)
# --------------------------------------------------------------------------

class Placements(Domain):
    """Verdicts de placement écrits au registre (lecture seule)."""

    @abc.abstractmethod
    def recorded(self, host: str | None) -> list[dict]:
        """Verdict, profil évalué et profil COURANT de chaque agent (de `host`
        seulement si donné)."""


# --------------------------------------------------------------------------
# vue d'avancement (`ameesh progress`, L24) : lectures seules
# --------------------------------------------------------------------------

# Blancs de `str.strip`/`re` Unicode, explicites pour le lecteur ARE : sa
# classe `\s` dépend de la locale et omet notamment l'espace insécable.
HOLD_NOTE_WHITESPACE = ("\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
                        "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
                        "\u2028\u2029\u202f\u205f\u3000")


def _transitions(events) -> list[dict]:
    """Lignes où l'état d'un lot change (la première de chaque lot en fait
    partie), texte des notes écarté : la règle de `lot_transitions`, partagée
    avec le repli de `lot_hold_events` (#1647, #1664)."""
    out: list[dict] = []
    previous: dict[int, str] = {}
    for ev in events or ():
        lot = int(ev["work_item_id"])
        state = ev.get("state")
        if lot not in previous or previous[lot] != state:
            out.append({key: value for key, value in ev.items() if key != "note"})
        previous[lot] = state
    return out


class Progress(Domain):
    """Lectures de la vue d'avancement (décision 0024) : lots et leur
    journal, actions, état des agents et grand livre des coûts. Aucune
    écriture, aucune transaction ; instants en secondes epoch (`*_ts`).

    `since_ts` borne la fenêtre ; `project` (None = tout) filtre comme le
    dit chaque opération ; une liste vide de noms ou d'ids rend une liste
    vide (jamais une erreur)."""

    @abc.abstractmethod
    def lots(self, *, since_ts: float, project: str | None, limit: int) -> list[dict]:
        """Lots (colonnes de `work.get`) encore ouverts (ni `merged` ni
        `promoted`) ou modifiés depuis `since_ts` ; projet = `app` ou
        `workstream`. Borne de RENDU : les ouverts d'abord, puis les plus
        récemment modifiés ; chaque ligne porte `total`, le nombre de lots
        qui répondaient au filtre avant la borne."""

    @abc.abstractmethod
    def lot_events(self, item_ids: Sequence[int]) -> list[dict]:
        """Journal COMPLET des transitions de ces lots (id, work_item_id,
        state, note, actor, created_ts), dans l'ordre d'écriture."""

    def lot_transitions(self, item_ids: Sequence[int]) -> list[dict]:
        """Transitions SEULES de ces lots (id, work_item_id, state, actor,
        created_ts), dans l'ordre d'écriture : les lignes qui répètent l'état
        précédent (notes) sont écartées, et leur texte n'est jamais lu. C'est
        ce que demandent les vues groupées (`work list`, `progress`,
        `roadmap`) : le journal complet ne sert qu'au détail d'un lot.

        Lecture DÉRIVÉE (pas une opération du contrat médié) : le stockage
        peut l'optimiser — agrégation SQL — sans changer ce que reçoit
        l'appelant ; le repli ci-dessous lit `lot_events`."""
        return _transitions(self.lot_events(item_ids))

    def lot_event_last(self, item_ids: Sequence[int]) -> list[dict]:
        """Dernier instant d'événement de chacun de ces lots (work_item_id,
        last_ts) : l'activité du journal sans en lire les lignes — la borne
        que `stagnation.last_activity` ajoute aux transitions. Lecture
        DÉRIVÉE, repli sur `lot_events`."""
        last: dict[int, float] = {}
        for ev in self.lot_events(item_ids):
            lot = int(ev["work_item_id"])
            ts = ev.get("created_ts")
            if ts is not None:
                last[lot] = max(last.get(lot, float("-inf")), float(ts))
        return [{"work_item_id": lot, "last_ts": last[lot]} for lot in sorted(last)]

    def packet_exits(self, *, since_ts: float) -> list[dict]:
        """#4369 : notes de composition de porteur (tout état) qui nomment
        des unités retirées (`slo.VEHICLE_EXIT_MARK`), écrites depuis
        `since_ts` (id, work_item_id, note, created_ts). La sortie d'une unité
        doit survivre à la fusion ou à la clôture de son porteur. Repli : aucune
        sortie connue — l'horloge part alors du GO, le sens qui n'éteint
        aucune alerte."""
        return []

    def lot_hold_events(self, item_ids: Sequence[int],
                        patterns: Sequence[str]) -> list[dict]:
        """Événements utiles à la lecture des retenues (#1664) : les
        transitions d'état (une retenue tombe au changement d'état) et les
        lignes dont la note correspond à l'un des motifs `patterns` (notes
        hold/levée ; mêmes motifs que `planned_waits`), fusionnées par
        identifiant — la ligne à note l'emporte, elle seule porte le texte —
        et rendues dans l'ordre d'écriture (`id`), comme `lot_transitions`.
        Une note portée par une transition est donc rendue avec elle, et le
        texte des notes ordinaires n'est jamais lu ni transmis. Un motif vide
        est refusé (`ValueError`) : il ferait sortir toutes les notes.

        Les motifs suivent la syntaxe des deux lecteurs (repli Python `re`,
        pilote PostgreSQL ARE : `^`, `\\s`, `(?:…)`) et s'appliquent à la note
        débarrassée de ses blancs de bord. Lecture DÉRIVÉE (pas une opération
        du contrat médié) : le pilote PostgreSQL ne transfère que ces lignes ;
        le repli ci-dessous lit `lot_events` une fois. Un appelant qui a déjà
        lu `lot_transitions` paie une seconde lecture bornée des transitions
        (`max_lots`) — le contrat reste inchangé."""
        ids = [int(i) for i in item_ids]
        if not ids:
            return []
        wanted = tuple(str(pattern) for pattern in patterns)
        if any(not pattern for pattern in wanted):
            raise ValueError("empty hold pattern")
        compiled = tuple(re.compile(pattern, re.I) for pattern in wanted)
        events = list(self.lot_events(ids))
        merged: dict[tuple[int, int], dict] = {}
        for ev in _transitions(events):
            merged[(int(ev["work_item_id"]), int(ev["id"]))] = ev
        if compiled:
            for ev in events:
                note = str(ev.get("note") or "").strip()
                if note and any(rx.match(note) for rx in compiled):
                    merged[(int(ev["work_item_id"]), int(ev["id"]))] = dict(ev)
        return sorted(merged.values(), key=lambda e: int(e["id"]))

    @abc.abstractmethod
    def lot_milestones(self, item_ids: Sequence[int]) -> list[dict]:
        """TOUS les jalons de ces lots (`work_item_milestones`, L10) :
        work_item_id, kind, at_ts, actor, verdict, sha, dans l'ordre
        chronologique (lecture de vérité, sans borne)."""

    @abc.abstractmethod
    def lot_messages(self, item_ids: Sequence[int]) -> list[dict]:
        """Dernier message lié à chacun de ces lots (`agent_mailbox.work_item_id`) :
        work_item_id (int), last_ts — activité du lot (module `stagnation`)."""

    @abc.abstractmethod
    def packages(self) -> list[dict]:
        """Les fiches WorkPackage présentes (L29) : id, kind, title, parent,
        responsible, team, status, canon_ref ; L96 : `start_on`, `end_on`,
        `delivery_on` (canon), `planned_start`, `planned_end`,
        `planned_delivery`, `planned_source`, `planned_by` (ameesh)."""

    @abc.abstractmethod
    def package_items(self) -> list[dict]:
        """TOUS les lots rattachés à une fiche (id, package_id, state,
        close_reason, app, updated_ts) : la progression d'un epic ne dépend
        pas de la fenêtre ; `app` (L304) rattache un jalon à ses projets."""

    @abc.abstractmethod
    def lot_actions(self, item_ids: Sequence[int]) -> list[dict]:
        """TOUTES les actions liées à ces lots (lecture de vérité, sans
        borne : les jalons et l'état d'un lot affiché en dépendent)."""

    @abc.abstractmethod
    def actions(self, *, since_ts: float, project: str | None, limit: int) -> list[dict]:
        """Actions modifiées depuis `since_ts` ou non terminales (du projet
        `project` si donné) : identité, état, approbateur et instants.
        Borne de RENDU : les non terminales d'abord, puis les plus récentes ;
        chaque ligne porte `total` (avant la borne)."""

    @abc.abstractmethod
    def agents(self, project: str | None) -> list[dict]:
        """État d'exécution de chaque agent (L243 : `project` est membre de ses
        `projects`, sinon son `team`, sinon son `workstream`) : statut, bail
        vivant (`lease_live`), consigne en cours, marqueur du tour
        (`turn_started_ts`, `turn_label`), non-lus."""

    @abc.abstractmethod
    def costs(self, *, since_ts: float, agents: Sequence[str] | None) -> list[dict]:
        """Grand livre agrégé par (agent, harnais, modèle) sur la fenêtre la
        plus large de `since_ts` et des 24 dernières heures (horloge de la
        base) : `usd_window`, `usd_1h`, `usd_24h`, tours et jetons de la
        fenêtre. `agents` restreint aux agents nommés (None = tous)."""

    @abc.abstractmethod
    def ci_suite(self, *, since_ts: float, limit: int) -> list[dict]:
        """Exécutions CONCLUES de la suite complète des tests depuis
        `since_ts` (#1476), de la plus récente à la plus ancienne, au plus
        `limit` **par dépôt**, dans les contextes qui la garantissent :
        poussée ou pull request `release/**`|`integration/**` (source ou
        cible), ou exécution manuelle (`workflow_dispatch`) ; une poussée sur
        `main` est écartée (la voie documentation seule L161a y saute les
        parts, et `ci_runs` ne garde pas le mode du plan). Chaque ligne :
        repo, run_id, branch, base, event, `duration_s` (premier relevé du
        veilleur → conclusion, l'unité de la vue) et `concluded_ts`. Les
        exécutions annulées ou remplacées ne comptent pas (pas de durée de
        suite)."""


# --------------------------------------------------------------------------
# vue par projet (lot L62) : qui travaille sur quoi
# --------------------------------------------------------------------------

class Projects(Domain):
    """Lecture de la vue par projet (`ameesh projects`, L62). Aucune écriture.

    UNE requête, UN aller-retour : la vue se rafraîchit souvent et sert aussi
    d'en-tête à `ameesh progress`."""

    @abc.abstractmethod
    def board(self, *, max_lots: int) -> dict:
        """`{"agents": [...], "lots": [...], "dead_letters": [...], "decisions": [...],
        "watches": [...]}` (`watches`, L121: agent, every_s, host, last_sent_ts).

        `agents` : un élément par agent du registre — name, chantier, team,
        harness, host, provider, credential_mode, status, status_text, mode,
        stop_reason, responsible, lease_live, turn_started_ts,
        status_since_ts, last_turn_ts, updated_ts, last_seen_ts, unread,
        lot de session (`session_lot_id` / `_title` / `_state`), dernier lot
        cité par l'agent dans son courrier (`mail_lot_id` / `_title` /
        `_state`), lot assigné ouvert le plus récent (`assigned_lot_id` /
        `_title` / `_state`) — trois lots OUVERTS, ou nuls,
        `open_lots` (lots ouverts assignés), `usd_24h` et `turns_24h`
        (grand livre, horloge de la base).

        `lots` : les lots OUVERTS (ni `merged`, ni `promoted`, ni `closed`),
        au plus `max_lots`, les plus récemment modifiés d'abord — id, title,
        state, app, workstream, package_team, assignee, updated_ts ; chaque
        élément porte `total` (avant la borne).

        `dead_letters` : le courrier en souffrance, comme
        `mailbox.dead_letters` (une ligne par destinataire et expéditeur).

        `decisions` (L124) : les demandes de décision EN ATTENTE, les plus
        anciennes d'abord (au plus 500) — id, owner (destinataire
        `human:<id>`, ou `human:*`), requester, created_ts, urgent, due_ts ;
        dans la même instruction (la vue reste à un aller-retour)."""


# --------------------------------------------------------------------------
# demandes de décision au propriétaire (lot L124) : sans migration, dans
# `agent_mailbox` (kind `request`, `payload.decision`)
# --------------------------------------------------------------------------

class Decisions(Domain):
    """Demandes de décision déposées par les agents pour un humain (L124).

    Une demande est une ligne de `agent_mailbox` : `kind = 'request'`,
    destinataire `human:<id>` (ou `human:*` sans responsable connu), objet
    `payload.decision` (question, options, recommandation, échéance, état,
    réponse). En attente tant que `delivered_at` est NULL ; répondue ou
    retirée, elle est close (`delivered_at` posé) en UNE écriture
    conditionnelle : deux réponses simultanées, un seul gagnant. Les lignes
    rendues portent id, sender, recipient, body, payload (dict), work_item_id,
    status, host, created_ts et closed_ts (`delivered_at`).

    `register_chat` inscrit l'agent de conversation du propriétaire (L123) :
    mode `externe` (jamais réclamé par un exécuteur), responsable humain."""

    @abc.abstractmethod
    def create(self, *, sender: str, recipient: str, body: str, payload: dict,
               work_item_id: str | None, host: str | None) -> dict:
        """Dépose la demande (le dépôt réveille, comme tout courrier) ; rend
        id, created_ts et `sender_project` (projet du fil de l'expéditeur)."""

    @abc.abstractmethod
    def get(self, decision_id: int) -> dict | None:
        """La demande `decision_id` (en attente ou close), ou None."""

    def repair_forward(self, decision_id: int, *, actor: str) -> dict:
        """Restore a human decision accidentally forwarded by mail. Atomically
        lock the original and its copy and restore the unchanged original to
        pending. A pending copy is withdrawn; a copy its requester already
        withdrew keeps its withdrawal trace (#4576). Refuses an original
        answered or withdrawn at its origin, an answered copy, a changed copy
        and a forwarding chain. Raise ValueError when there is no unambiguous
        repair."""
        raise NotImplementedError

    @abc.abstractmethod
    def pending(self, *, limit: int) -> list[dict]:
        """Les demandes en attente, les plus anciennes d'abord."""

    @abc.abstractmethod
    def history(self, *, limit: int) -> list[dict]:
        """Les demandes closes (répondues, retirées), les plus récentes d'abord."""

    @abc.abstractmethod
    def pending_for_lot(self, lot: str) -> list[dict]:
        """Les demandes en attente rattachées au lot `lot` (`work_item_id`)."""

    @abc.abstractmethod
    def close(self, decision_id: int, *, status: str, record: dict) -> dict | None:
        """Clôt la demande si elle est encore en attente : `status`
        (`answered` | `delivered`), `record` fusionné dans `payload.decision`
        (état, réponse ou retrait). Rend la ligne close, ou None (déjà close,
        inconnue) — rien n'est écrit."""

    def validation_register(self, decision_id: int, *, routing: dict):
        """#4256: immutable authority snapshot, outside the mediated contract."""
        raise NotImplementedError

    def validation_route(self, decision_id: int):
        """Private routing snapshot, or None for a legacy request."""
        raise NotImplementedError

    def validation_due(self, *, now: float):
        """All due, pending, not-yet-escalated validations; no queue truncation."""
        raise NotImplementedError

    def validation_lock(self, decision_id: int):
        """Lock the decision mailbox row before escalation or a late reply."""
        raise NotImplementedError

    def validation_escalate(self, decision_id: int, *, recipient: str,
                            decision: dict, body: str, now: float):
        """Update a locked, pending decision and record its single escalation."""
        raise NotImplementedError

    def validation_event(self, decision_id: int, *, person: str, kind: str,
                         now: float, facts: dict, event_key: str):
        """Append idempotent private facts; never a work note or thread entry."""
        raise NotImplementedError

    def validation_history(self, *, person: str):
        """Private audit rows; caller must verify owner key and human session."""
        raise NotImplementedError

    def validation_followup(self, decision_id: int, *, followup_id: int, response_key: str):
        """Record the typed contradiction link under the decision row lock."""
        raise NotImplementedError

    def validation_followup_id(self, decision_id: int, *, response_key: str):
        """Existing contradiction follow-up, or None."""
        raise NotImplementedError

    def reply_for(self, decision_id: int, *, sender: str | None = None) -> dict | None:
        """#3892 : le courrier de réponse déjà déposé pour `decision_id`
        (`kind = 'reply'`, `payload.decision_answer.decision`), ou None.

        Il rend le même rejeu possible après une panne entre la réponse
        gardée et son courrier : le courrier manquant est renvoyé, le courrier
        déjà déposé n'est jamais doublé. `sender` borne la lecture au
        répondeur. Pas une opération du contrat médié : un pilote qui ne
        l'implémente pas lève NotImplementedError et l'appelant envoie le
        courrier sous une clé d'idempotence."""
        raise NotImplementedError

    @abc.abstractmethod
    def source_mail(self, message_id: int) -> dict | None:
        """L233: mail `message_id` (id, sender, recipient, body, kind,
        work_item_id, created_ts), or None — the source of a request filed
        with `ameesh decide ask --from-mail`."""

    @abc.abstractmethod
    def prose_candidates(self, *, recipients: Sequence[str], since_ts: float,
                         limit: int) -> list[dict]:
        """L233: mail sent since `since_ts` by an agent (not `ameesh`, not a
        human, not a chat) to one of `recipients` or to any `human:*`, that is
        neither a request, a reply, an alert (`event`) nor a receipt, and that
        no request names as its source (`payload.decision.source_mail`): the
        `limit` most recent, oldest first, same columns as `source_mail`."""

    @abc.abstractmethod
    def register_chat(self, name: str, *, human: str, host: str, harness: str,
                      cwd: str | None, status_text: str) -> dict | None:
        """Inscrit (ou rafraîchit) l'agent de conversation `name` : mode
        `externe`, responsable `human`, session oubliée (le hook inscrit la
        nouvelle). None si `name` est un agent `execute` ou détient un bail
        vivant : rien n'est écrit."""


# --------------------------------------------------------------------------
# feuille de route (lot L96, migration 0044) : dates prévues, engagements
# --------------------------------------------------------------------------

class Roadmap(Domain):
    """Dates prévues des tâches et des fiches du plan, engagements datés.

    Les dates sont des jours calendaires, échangées en texte ISO
    (`AAAA-MM-JJ`) ; None = pas de date. Aucun état ni jalon de métier ici :
    les dates réelles se lisent dans le journal (`progress`)."""

    @abc.abstractmethod
    def plan_item(self, item_id: int, dates: dict, *, source: str | None,
                  actor: str, expect: dict | None = None) -> dict | None:
        """Pose les dates prévues d'une tâche (`dates` : clés parmi `start`,
        `end`, `delivery` ; valeur None = date effacée ; clé absente =
        inchangée), avec qui et quand (`planned_by`, `planned_at`). Ce n'est
        PAS une activité de la tâche : ni `updated_at` ni le journal ne
        bougent (une replanification ne masque pas une stagnation). Rend
        `{id, planned_start, planned_end, planned_delivery, planned_source,
        planned_by, planned_ts}`, ou None si la tâche est inconnue.

        #740 : avec `expect` (`planned_start`, `planned_end`,
        `planned_source` lus avant), rien n'est écrit si l'un d'eux a changé
        depuis la lecture ; None alors aussi."""

    @abc.abstractmethod
    def item_plans(self, ids: Sequence[int]) -> list[dict]:
        """Les dates prévues de ces tâches (même forme que `plan_item`)."""

    @abc.abstractmethod
    def plan_package(self, ident: str, dates: dict, *, source: str | None,
                     actor: str) -> dict | None:
        """Pose les dates prévues (côté ameesh) d'une fiche WorkPackage ; None
        si elle est inconnue. Les dates du canon (`start_on`…) ne sont pas
        touchées."""

    @abc.abstractmethod
    def items(self, *, since_ts: float, limit: int,
              done_states: Sequence[str], project: str | None = None) -> list[dict]:
        """Les tâches de la feuille de route : ouvertes (état hors de
        `done_states`, fourni par l'appelant), ou datées, ou portant un
        engagement en cours, ou modifiées depuis `since_ts` — colonnes de `work_items` utiles à la
        frise (id, title, state, app, workstream, assignee, package_id,
        created_ts, updated_ts, closed_ts) et dates prévues ; `total` avant
        la borne. Le filtre projet est appliqué avant la borne et le total."""

    @abc.abstractmethod
    def commitments(self, *, statuses: Sequence[str] | None = None,
                    ids: Sequence[int] | None = None) -> list[dict]:
        """Les engagements (tous, ou de ces statuts, ou ces ids), par
        échéance puis id. Instants en `*_ts`, `due_on` en texte ISO,
        `depends_on` en liste."""

    @abc.abstractmethod
    def add_commitment(self, row: dict) -> dict:
        """Écrit un engagement (clés de la table) et le rend."""

    @abc.abstractmethod
    def record_proposal(self, row: dict) -> dict | None:
        """Écrit une proposition (`status = 'proposed'`) sauf si sa
        `proposal_key` existe déjà : None dans ce cas."""

    @abc.abstractmethod
    def set_commitment(self, ident: int, *, current: Sequence[str],
                       values: dict, expect: dict | None = None) -> dict | None:
        """Change un engagement (`status`, `due_on`, `note`…) s'il est dans
        l'un des statuts `current` ; None sinon (inconnu ou déjà changé).
        Un statut `done` ou `cancelled` pose `closed_at`. Avec `expect`, les
        champs éditables doivent être inchangés depuis leur lecture."""


# --------------------------------------------------------------------------
# exploitation (lot L26, migration 0027) : réglages, redémarrage, historiques
# --------------------------------------------------------------------------

class Operations(Domain):
    """Ce que l'orchestrateur lit et règle pour exploiter les agents (L26).

    Réglages d'agent (`session_policy`, `effort`, `tier`,
    `context_max_tokens`, `turn_max_seconds`, `turn_mail_max`,
    `turn_grace_seconds`), lot de la session
    courante, demande de redémarrage, lectures enrichies pour `ameesh list
    --json` et `ameesh alerts`, usage par tour, historique des jauges de
    forfait et soldes d'un fournisseur payé au token. Instants en secondes
    epoch (`*_ts`) ; une liste vide d'ids rend une liste vide."""

    #: colonnes réglables par `set_settings` (liste fermée)
    SETTINGS = ("session_policy", "effort", "tier", "context_max_tokens",
                "turn_max_seconds", "turn_mail_max", "turn_grace_seconds", "lot_wake",
                "harness_bin")
    #: réglages entiers (colonne `bigint`) : la valeur texte est convertie
    INTEGER_SETTINGS = ("context_max_tokens", "turn_max_seconds", "turn_mail_max",
                        "turn_grace_seconds")

    @abc.abstractmethod
    def publish_peek(self, name: str, owner: str, epoch: int, payload: dict) -> bool:
        """Replace a sanitised observation under the live agent lease; false if fenced."""

    @abc.abstractmethod
    def read_peek(self, name: str, human: str) -> dict | None:
        """Responsible human only; no snapshot data selected on a refused read."""

    @abc.abstractmethod
    def set_settings(self, name: str, values: dict) -> bool:
        """Pose les réglages nommés (clés de `SETTINGS` ; `''` ou None =
        défaut, colonne effacée). Faux si l'agent est inconnu."""

    @abc.abstractmethod
    def set_session_work_item(self, name: str, owner: str, epoch: int,
                              work_item: str | None) -> bool:
        """Note le lot de la session courante, **fencé par le bail** (owner,
        epoch, échéance recontrôlée). Faux si le bail n'est plus le nôtre."""

    @abc.abstractmethod
    def set_dormant(self, name: str, owner: str, epoch: int, status_text: str) -> bool:
        """L76 T2 : le texte de statut d'un résident endormi, en une écriture
        conditionnelle — seulement si l'agent est `idle` et que le bail
        (owner, epoch, échéance) est encore le nôtre. Faux sinon."""

    @abc.abstractmethod
    def listing(self, *, today: str | None = None) -> list[dict]:
        """Une ligne par agent : réglages, dossier de travail (`cwd`), statut et
        `status_since_ts`, début du motif affiché (`status_reason_since_ts`), bail
        (`lease_live`, `lease_expires_ts`), tour en cours (`turn_started_ts`,
        `turn_label` du marqueur comptable), non-lus qui ouvriront un tour
        (`unread`, `oldest_unread_ts` ; L125 : hors courrier passif, compté à
        part dans `passive_unread`), lot de session (`session_work_item`,
        `session_lot_title`, `session_lot_state`, `session_lot_later` : son
        début prévu est après aujourd'hui), lot assigné ouvert le plus
        récent (`assigned_lot_id`, `assigned_lot_title`, `assigned_lot_state`),
        lots qui l'occupent (`active_lots`, L165b : assignés, ouverts, ni en
        `qa`/`waiting_human`/`blocked`, ni prévus après aujourd'hui ;
        « aujourd'hui » : `today`, jour ISO, par défaut le jour local de
        l'hôte, jamais celui du fuseau de la base),
        dernier tour du grand livre (`last_turn_reread_tokens` = entrée +
        entrée en cache, `last_turn_session`, `last_turn_recorded_ts`),
        demande de redémarrage (`restart_requested_ts`) et `ephemeral`
        (#3411 : un agent éphémère ne prend jamais de travail
        automatiquement)."""

    @abc.abstractmethod
    def request_restart(self, name: str, brief: str) -> dict | None:
        """Enregistre une demande de redémarrage (brief compris). None si
        l'agent est inconnu ou arrêté (`stopped`)."""

    @abc.abstractmethod
    def apply_restart(self, name: str, owner: str | None = None,
                      epoch: int | None = None) -> dict | None:
        """Applique la demande en attente, en UNE instruction : session et lot
        de session oubliés, `session_reset_at` posé, consigne en attente =
        brief EN TÊTE, puis la consigne courante (tour inachevé, bail expiré
        ou rendu), puis l'ancienne attente ; `current_prompt` effacé (un claim
        ultérieur ne la remet donc pas devant le brief), statut `queued`,
        demande effacée.

        Sans `owner` : seulement si aucun bail n'est vivant (verrou d'abord,
        échéance recontrôlée à l'heure réelle). Avec `owner`/`epoch` : fencé
        par ce bail. Rend la ligne (`brief`, `session_id` oubliée), ou None."""

    @abc.abstractmethod
    def adopt(self, name: str, *, harness: str, host: str, cwd: str | None,
              session_id: str, session_account: str | None, prompt: str,
              status_text: str) -> dict | None:
        """`ameesh adopt` (L39, 0030) en UNE instruction, seulement si aucun
        bail n'est vivant (verrou d'abord, échéance recontrôlée à l'heure
        réelle) : mode `execute`, harnais, hôte, dossier (s'il est donné),
        session et son compte d'origine, lot de session oublié,
        `session_reset_at` posé, consigne en attente = `prompt` EN TÊTE puis
        la consigne courante d'un tour inachevé puis l'ancienne attente,
        `current_prompt` effacé, demande de redémarrage effacée, statut
        `queued`. Rend `{name, previous_session, previous_mode,
        previous_harness}`, ou None (bail vivant, agent inconnu)."""

    @abc.abstractmethod
    def resume(self, name: str, *, prompt: str, forget: bool,
               status_text: str) -> dict | None:
        """`ameesh resume` (L39, 0030) en UNE instruction (verrou d'abord).

        Sans bail vivant : `forget` oublie la session (et son compte, son lot,
        `session_reset_at` posé) ; consigne en attente = `prompt` EN TÊTE puis
        la consigne courante d'un tour inachevé puis l'ancienne attente ;
        demande de redémarrage effacée ; statut `queued`. Avec un bail vivant
        (agent au repos sous un exécuteur) : seulement sans `forget`, ni tour
        en cours ni arrêt, et la consigne est ajoutée APRÈS l'attente (le tour
        de l'exécuteur n'est pas touché). Rend `{name, previous_session,
        previous_status, live}`, ou None (refus : réessayer)."""

    @abc.abstractmethod
    def message_lots(self, ids: Sequence[int]) -> list[str]:
        """Lots (`work_item_id`, non vides, distincts) de ces messages."""

    @abc.abstractmethod
    def assigned_open_lots(self, name: str) -> list[dict]:
        """Lots ouverts (ni `merged` ni `promoted`) assignés à l'agent : id,
        title, state, updated_ts, du plus récemment modifié au plus ancien."""

    @abc.abstractmethod
    def open_lots_activity(self, limit: int | None) -> list[dict]:
        """Lots ouverts avec leur DERNIÈRE activité (`last_activity_ts`) :
        maximum de la ligne du lot, de ses transitions, jalons, actions et
        messages liés (`agent_mailbox.work_item_id`) ; plus id, title, state,
        assignee, `package_id`, type, `planned_start` (jour ISO ou None,
        origine du délai intake #2562) et `priority` (le délai court du canon §9
        pour un P1, #789), app, workstream, reviewer (#705 : le projet du lot et
        son relecteur désigné), `lot_team` (équipe du lot : la sienne, sinon
        celle de son paquet ou du parent, sinon son `app`), `workflow_ref`
        (#1922/N13 : la révision Process qui pinne le lot, '' sur une base
        d'avant la 0452). Les plus
        anciennement actifs d'abord. `limit=None` lit toute la vérité avant le
        filtrage des attentes planifiées (L368)."""

    @abc.abstractmethod
    def state_lots_activity(self, state: str, limit: int | None) -> list[dict]:
        """Lots d'un état donné (`promoted`, …), même forme que
        `open_lots_activity` — qui exclut `merged`, `promoted` et `closed` :
        un lot promu qu'un agent arrêté garde doit rester visible (#753, R1
        auditeur). `limit=None` : toute la vérité."""

    @abc.abstractmethod
    def build_lots_activity(self, limit: int | None) -> list[dict]:
        """§4 : les lots en `build` assignés à un agent, avec leur dernière
        activité (`last_activity_ts`, même calcul que `open_lots_activity`),
        les plus anciennement actifs d'abord. Lecture DÉDIÉE du balayage de la
        limite d'en-cours : le plafond global de `open_lots_activity` peut
        tronquer les lots d'un agent sur un mesh chargé, jamais ici (les lots
        en `build` sont bornés par le plafond par agent)."""

    @abc.abstractmethod
    def turns(self, *, agent: str | None, since_s: float, limit: int,
              message: str | None = None) -> list[dict]:
        """Lignes du grand livre `turn_costs` des `since_s` dernières
        secondes (de l'agent si donné), les plus récentes d'abord : id,
        agent, harness, turn, model, session, usd, input_tokens,
        cached_input_tokens, output_tokens, work_item_id, started_ts,
        recorded_ts, inputs. `work_item_id` et `started_ts` (#796) valent None
        pour une ligne d'avant la migration 0423 ou un tour sans lot.
        `message` (L80b) restreint aux tours qui ont servi ce message
        (`inputs` contient son identifiant)."""

    @abc.abstractmethod
    def record_gauges(self, readings: Sequence[dict]) -> int:
        """Ajoute les relevés de jauge (harness, key, used, resets_at,
        window_s, `account` facultatif — L30, 0028, `produced_at` et `source`
        facultatifs — #1132, 0434, `overage_capable` et `overage_in_use`
        facultatifs — #3755) qui CHANGENT le dernier relevé de leur jauge
        (du même compte), ou dont le dernier relevé a plus de dix minutes. Rend
        le nombre de lignes. `produced_at` (epoch) est l'instant de PRODUCTION
        du relevé, NULL quand le flux ne le date pas ; `source` nomme le
        journal d'origine. Les drapeaux d'overage font partie de la comparaison
        (#3755) : un relevé identique dont les drapeaux changent est écrit."""

    @abc.abstractmethod
    def gauge_history(self, *, since_s: float, harness: str | None,
                      account: str | None = None) -> list[dict]:
        """Relevés des `since_s` dernières secondes, dans l'ordre
        chronologique d'insertion : harness, key, used, resets_at_ts, window_s,
        observed_ts, produced_ts (#1132 ; None si non daté), source (#1132),
        account (L30 ; None sans comptes) et les drapeaux d'overage
        (`overage_capable`, `overage_in_use`, #3755). `account` filtre."""

    @abc.abstractmethod
    def latest_gauges(self, *, since_s: float, by_source: bool = False) -> list[dict]:
        """L94 : le DERNIER relevé de chaque jauge (harness, account, key)
        observé dans les `since_s` dernières secondes, choisi par instant de
        PRODUCTION (#1132 : `produced_at` d'abord, NULL en dernier, puis
        `observed_at`) : harness, key, used, resets_at_ts, window_s,
        observed_ts, produced_ts, source, account (None sans comptes) et les
        drapeaux d'overage (#3755).

        `by_source` (défaut faux) : une ligne par SOURCE de publication
        (l'agent qui a produit le relevé) au lieu d'une par compte/jauge. Deux
        hôtes peuvent déclarer un compte homonyme (`codex/secondaire`) ; la
        vue unifiée garde le dernier relevé, la vue par source permet à
        l'alerte d'overage de ne pas laisser le relevé d'un hôte effacer
        celui d'un autre. Les autres lecteurs gardent la vue unifiée."""

    @abc.abstractmethod
    def gauge_latest(self, *, harness: str, account: str) -> list[dict]:
        """#1132 : le dernier relevé CONNU de chaque jauge d'un compte nommé,
        sans limite d'âge : le plus récent par instant de production
        (`produced_at` d'abord, NULL en dernier), puis par insertion — harness,
        key, used, resets_at_ts, window_s, observed_ts, produced_ts, source,
        account, drapeaux d'overage (#3755). Aucune ligne pour un compte
        inconnu."""

    @abc.abstractmethod
    def assigners(self, *, since_s: float) -> list[str]:
        """L94 : les acteurs qui ont confié des lots dans les `since_s`
        dernières secondes — délégants (`work_item_delegations.delegated_by`)
        et auteurs d'une réassignation (`work assign`, note « assigné à … »)
        autres que le nouvel assigné. Noms tels qu'écrits (préfixe `agent:`
        compris), triés, sans doublon."""

    @abc.abstractmethod
    def record_balance(self, *, provider: str, currency: str, total: float,
                       granted: float | None, topped_up: float | None,
                       available: bool | None, account: str | None = None,
                       unless_within_s: float | None = None) -> dict | None:
        """Ajoute un solde horodaté (heure de la base) ; rend la ligne.
        `account` (L30) : le compte (clé d'API) dont c'est le solde.
        `unless_within_s` (L71) : rien n'est écrit (None) si un relevé de ce
        fournisseur, compte et devise a moins de `unless_within_s` secondes."""

    @abc.abstractmethod
    def recent_balance(self, *, provider: str, account: str | None,
                       within_s: float) -> bool:
        """L71 : un relevé de ce fournisseur (et compte) a-t-il moins de
        `within_s` secondes ? (plusieurs exécuteurs sur la même clé)"""

    @abc.abstractmethod
    def balances(self, *, provider: str | None, since_s: float,
                 account: str | None = None) -> list[dict]:
        """Soldes des `since_s` dernières secondes, plus le dernier relevé
        ANTÉRIEUR de chaque (fournisseur, devise, compte) — la base de la
        première différence —, dans l'ordre chronologique : provider, currency,
        total, granted, topped_up, available, account, observed_ts."""


# --------------------------------------------------------------------------
# ressources des hôtes (lot L31, décision 0028, migration 0029)
# --------------------------------------------------------------------------

class HostLifecycle(Domain):
    """Durable withdrawal and return intent for admitted hosts (L232e).

    #4148 (B): `release_state` is what ameesh decided, `hook_state` the last
    state the external adapter acknowledged, `claims_open` the durable claim
    gate of a return. `begin_transition`/`finish_transition` journal a
    successful hook call per (host, generation, state), so a replay never
    calls it twice. `claim_release_scan` serialises the release scanner of
    one host across its runners; `next_scan_ts` spaces the expensive idle
    projection without slowing the cheap transitions.
    """

    @abc.abstractmethod
    def get(self, host: str) -> dict | None:
        """Current host mode/generation, scheduled return and release state."""

    @abc.abstractmethod
    def listing(self) -> list[dict]:
        """Persistent host controls, release/hook states included."""

    @abc.abstractmethod
    def drain(self, host: str, *, actor: str, duration: int | None = None,
              critical: Sequence[str] = (), automatic: bool = False,
              close_claims: bool = False) -> dict:
        """Close claims/enable atomically and retain agents' original host.

        #3029: an elected role holding its lease on this host is returned in
        the same transaction, with its epoch bumped, so another candidate can
        claim at once instead of waiting for the TTL. It stays out of the
        L232e handoff journal (there is no local work to publish).

        #4148: `automatic=True` records that ameesh drained a burst host by
        itself; `release_state` becomes `draining`. `close_claims` is true
        only when an external adapter must answer `busy` before a return; with
        no adapter the durable gate stays open, and the `draining` mode alone
        closes claims (an old binary needs that to bring agents back).
        """

    @abc.abstractmethod
    def undrain(self, host: str, *, actor: str, claims_open: bool = True,
                generation: int | None = None,
                release_epoch: int | None = None,
                attempt_stale_after: float | None = None) -> dict | None:
        """Request agents' return; retained origin rows survive this request.

        #4148: `claims_open=False` leaves the claim gate closed for the
        `busy` hook; only the hook's success (or the explicit default) opens
        it. `release_state` returns to `busy` only when claims are opened.
        `generation`/`release_epoch` are the CAS of a return decided from a
        snapshot: a new withdrawal or another return makes it stale.
        `attempt_stale_after` refuses to reopen while an external adapter call
        is still in flight (younger than the bound), even from an older wave.
        """

    @abc.abstractmethod
    def begin_transition(self, host: str, generation: int, state: str, *,
                         release_epoch: int, actor: str, stale_after: float,
                         retry_after: float) -> dict | None:
        """Claim the right to call the adapter for one generation and state.

        None when the transition already succeeded (journal), when it is not
        due yet, when another live attempt holds it (younger than
        `stale_after`), when a failure is younger than `retry_after`, or when
        `release_epoch` moved (a return or a new withdrawal happened): no
        stale release decision is ever accepted. The state must also be legal
        for the current row (`busy` only while draining/returning,
        `draining` only while claims are closed, `idle-drained` only after
        the `draining` acknowledgement).
        """

    @abc.abstractmethod
    def finish_transition(self, host: str, generation: int, state: str, *,
                          release_epoch: int, actor: str, detail: dict,
                          error: str | None = None) -> dict | None:
        """Record one adapter transition, success or failure.

        Success journals `(host, generation, state)` (idempotent), clears the
        attempt and the error, and advances `hook_state`; `busy` also sets the
        release state to busy, `idle-drained` stamps the release. Claims stay
        closed until `open_claims` (a `busy` acknowledgement only means the
        adapter is ready, not that ameesh may already take a lease). Failure
        keeps the acknowledged state and the closed claims, and stores the
        bounded error for the alert. Returns the updated row.
        """

    @abc.abstractmethod
    def open_claims(self, host: str, generation: int, *,
                    require_busy: bool = True,
                    release_epoch: int | None = None,
                    attempt_stale_after: float | None = None) -> bool:
        """Reopen claims after a `busy` acknowledgement and a fresh reading.

        Only a host whose acknowledged hook state is `busy` can reopen; a
        replay, a stale generation or a moved `release_epoch` opens nothing.
        `require_busy=False` is the explicit operator path of a host whose
        adapter was removed. `attempt_stale_after` also refuses while an
        adapter call is in flight.
        """

    @abc.abstractmethod
    def close_claims(self, host: str, generation: int,
                     release_epoch: int) -> bool:
        """Close the durable claim gate of a current withdrawal (CAS).

        #4148: a hook configured after a drain (or a canon read late) must
        close claims before `draining`; the mode already closes them, so this
        only lets the adapter transition be accepted.
        """

    @abc.abstractmethod
    def note_return_request(self, host: str, *, actor: str) -> dict | None:
        """Persist an explicit return intent without opening any claim.

        #4148: an `undrain` issued while an `idle-drained` hook is in flight
        must survive the hook's completion; the scanner then calls `busy` even
        if no work arrived.
        """

    @abc.abstractmethod
    def reopen_return(self, host: str, *, generation: int, release_epoch: int,
                      actor: str,
                      attempt_stale_after: float | None = None) -> dict | None:
        """Atomically reopen a return after a valid `busy` proof.

        The CAS is the generation and the release epoch of the snapshot, and
        the row must still carry `hook_state='busy'`; a released host also
        needs a `busy` journal entry newer than its last `idle-drained`
        (across generations and epochs). A new withdrawal, another return, or
        any in-flight adapter call younger than `attempt_stale_after` leaves
        the claims closed. Returns the updated row, or None.
        """

    @abc.abstractmethod
    def transition_journal(self, host: str) -> list[dict]:
        """Successful adapter transitions of a host, oldest first."""

    @abc.abstractmethod
    def claim_release_scan(self, host: str, *, owner: str, lock_seconds: float,
                           scan_interval: float) -> dict | None:
        """Take or renew the per-host release scanner lock.

        None while another owner holds a live lock. The first claim schedules
        a scan now; `scan_due` says whether `next_scan_at` is due. A later
        claim by the same owner renews it.
        """

    @abc.abstractmethod
    def finish_release_scan(self, host: str, *, owner: str,
                            next_scan_at: float | None = None) -> bool:
        """Release the scanner lock, optionally scheduling the next scan."""

    @abc.abstractmethod
    def note_release_blocked(self, host: str, *, owner: str,
                             reason: str | None = None,
                             source: str | None = None) -> bool:
        """Record why the last automatic release scan did not drain a host.

        #4148: mainly the unknown run source of A's measurement, so the alert
        pass does not have to recompute the idle projection. `reason=None`
        clears the block; only the owner of the scanner lock writes.
        """

    @abc.abstractmethod
    def release_locks(self) -> list[dict]:
        """Scanner locks with their last block (`blocked_reason`, source, since)."""

    @abc.abstractmethod
    def agents(self, host: str, generation: int) -> list[dict]:
        """Agent origins ordered critical first, with progress/refusals."""

    @abc.abstractmethod
    def demand(self, agent: str, current_host: str) -> dict | None:
        """Current withdrawal or return request applicable to this agent."""

    @abc.abstractmethod
    def progress(self, host: str, generation: int, agent: str, *, operation: str | None,
                 missing: Sequence[str]) -> bool:
        """CAS progress against the current host generation."""

    @abc.abstractmethod
    def expire_returns(self, *, open_claims: bool = True,
                       hosts: Sequence[str] | None = None) -> list[dict]:
        """Convert elapsed planned withdrawals to durable return requests.

        #4148: `open_claims=False` keeps the claim gate closed so the `busy`
        hook answers first (the runner's path); the default preserves the
        historical direct call, where no adapter is involved.

        #4435: `hosts` restricts the conversion to those hosts (the mediated
        server expires only the hosts it scans); None keeps every one.
        """

    @abc.abstractmethod
    def complete_return(self, host: str, generation: int) -> bool:
        """Finish a return only when all remaining origins are home and unfenced."""

    @abc.abstractmethod
    def detect_losses(self, *, actor: str, older_than: int,
                      critical: Sequence[str] = (),
                      released_grace: float = 900.0) -> list[dict]:
        """Close stale known hosts atomically; loss is not a quiescence proof.

        #4148: a host that was released and whose `busy` hook answered is
        booting with stale readings on purpose; it is not lost for
        `released_grace` seconds (or until a post-ack reading reopens claims).
        """

    @abc.abstractmethod
    def reconcile_loss(self, host: str, *, actor: str, fresh_within: int) -> dict:
        """Reopen a lost host only on a verified proof.

        Readings in database time must cover at least `fresh_within` without a
        hole that large, with no reading dated in the future; every origin
        agent must still be home and unfenced, with no active handoff and no
        `recover_source` attempt since the loss. A live lease of an agent that
        is home is the returning host's own runner and does not block the
        proof (#3180). The transition `lost -> returning -> available` is
        atomic and leaves its actor and evidence in `ameesh_events`. Returns
        `{"host", "mode", "reconciled", "missing", ...}`; a refusal names the
        missing proof.
        """


class HostResources(Domain):
    """Relevés des ressources d'un hôte (état d'exécution, jamais canon).

    #254 : la pause d'un hôte voulue par le propriétaire (`host_holds`, 0392).

    Un relevé porte : hôte, instant, mémoire disponible, swap utilisé, charge
    1 minute, nombre de CPU, disque libre (et le dossier mesuré), nombre de
    tours en cours ; L159d: PSI, application and guest shares, lendable
    capacity. Les mesures illisibles sont NULL : on ne devine pas.
    Instants rendus en secondes epoch (`sampled_ts`).
    """

    @abc.abstractmethod
    def holds(self) -> list[dict]:
        """#254 : les pauses en cours (`until` pas encore passé, heure de la
        base) : host, reason, author, created_ts, until_ts."""

    @abc.abstractmethod
    def hold(self, host: str, *, reason: str, author: str, seconds: float | None = None,
             until_ts: float | None = None) -> dict | None:
        """#254 : pose (ou remplace) la pause de `host`, pour `seconds` ou
        jusqu'à `until_ts`, bornée à l'heure de la base (après maintenant,
        24 h au plus) ; None hors de ces bornes. L'historique
        (`host_hold_events`) est écrit dans la même transaction."""

    @abc.abstractmethod
    def release(self, host: str, *, author: str) -> bool:
        """#254 : lève la pause de `host` (historique compris) ; faux s'il
        n'y en avait pas."""

    @abc.abstractmethod
    def hold_events(self, host: str | None = None) -> list[dict]:
        """#254 : l'historique des pauses et levées, dans l'ordre."""

    @abc.abstractmethod
    def gate_guard(self, host: str):
        """Context manager yielding host storage under a gate publication lock.
        All record() calls for this host share the lock. Read latest() inside
        the context and complete synchronous enable before releasing it."""

    @abc.abstractmethod
    def record(self, reading: dict) -> dict:
        """Ajoute un relevé (heure de la base) et élague les plus anciens que
        l'horizon de conservation ; rend la ligne écrite."""

    @abc.abstractmethod
    def latest(self, host: str) -> dict | None:
        """Le dernier relevé de l'hôte, ou None s'il n'y en a jamais eu."""

    @abc.abstractmethod
    def history(self, host: str, limit: int) -> list[dict]:
        """Les `limit` derniers relevés de l'hôte, du plus ancien au plus récent."""

    @abc.abstractmethod
    def current(self, host: str | None) -> list[dict]:
        """Le dernier relevé de CHAQUE hôte connu (celui de `host` seulement
        s'il est donné), trié par nom d'hôte."""

    @abc.abstractmethod
    def runners(self, host: str, since_s: float) -> list[dict]:
        """#2022: the last reading of each runner of `host` (`runner_id`)
        within `since_s` seconds, the most zombies first."""

    @abc.abstractmethod
    def turns_in_progress(self, host: str) -> int:
        """Nombre d'agents de l'hôte en tour (statut `running`, bail vivant)."""

    @abc.abstractmethod
    def since(self, host: str, since_s: float) -> list[dict]:
        """L159g: the readings of the host over the last `since_s` seconds,
        oldest first (the pilot report)."""

    @abc.abstractmethod
    def disk_history(self, host: str, since_s: float, step_s: float) -> list[dict]:
        """#2879: the disk fields of the host's readings over the last
        `since_s` seconds, one reading per `step_s` bucket at most, oldest
        first (`id`, `sampled_ts`, `volumes`, `disk_path`, `disk_free_bytes`
        only) — the disk trend does not pay for the ~50 columns of `since`,
        nor for every reading of a dense history. Each `volumes` entry is
        reduced to its trend fields (`path`, `paths`, `disk_free_bytes`)."""

    @abc.abstractmethod
    def record_harness_version(self, host: str, harness: str, *, path: str | None,
                               version: str | None, raw: str | None) -> None:
        """L120: the last version read of `harness` on `host` (one row per
        pair, replaced)."""

    @abc.abstractmethod
    def harness_versions(self, host: str | None = None) -> list[dict]:
        """L120: the versions read, by harness then host: `host`, `harness`,
        `path`, `version`, `raw`, `observed_ts` (epoch seconds)."""

    @abc.abstractmethod
    def lend_basis(self, host: str, since_s: float, slot_s: float,
                   percentile: float) -> dict:
        """L159d: history behind the lendable capacity, over the last
        `since_s` seconds: `samples`, `span_s` (time covered by readings
        with `app_cpu`), `min_free_mem_bytes` (lowest `mem_available +
        guest_mem`, over readings where both are known) and
        `app_cpu_p95` (the `percentile` of the application's CPU averaged
        per `slot_s` slot); None when not measured."""

    @abc.abstractmethod
    def usage(self, host: str, since_s: float) -> dict:
        """L94 : l'utilisation de l'hôte sur les `since_s` dernières secondes :
        `samples`, `first_ts`, `last_ts`, `max_load_per_cpu`,
        `avg_load_per_cpu` (charge 1 min / CPU ; None sans mesure),
        `max_turns`, `avg_turns` (tours en cours)."""


class TurnResources(Domain):
    """Ressources rattachées à un tour (lot L31, décision 0028).

    L'exécuteur ouvre une ligne au lancement du harnais (groupe de processus,
    étiquette de conteneur) et la ferme au retour du tour. Une ressource qui
    survit à son tour est marquée `orphan` — **jamais supprimée** — et
    `ameesh alerts` la signale (`orphan_resource`).

    Délai de grâce (travail de fond d'un tour fini normalement) : la ligne
    reste `running`, son `ended_at` marque la fin du tour ; elle est fermée
    à la fin du travail de fond ou à l'échéance de la grâce.
    """

    @abc.abstractmethod
    def open_turn(self, turn_id: str, agent: str, host: str, *,
                  pgid: int | None, label: str | None,
                  containers: Sequence[str] | None = None) -> None:
        """Ouvre la ligne d'un tour (idempotent sur `turn_id`)."""

    @abc.abstractmethod
    def attach_cgroup(self, turn_id: str, cgroup: str) -> None:
        """L498 : rattache le cgroup délégué du tour (chemin relatif à la
        monture cgroup v2). Appelée juste après `open_turn` par l'exécuteur
        qui a un cgroup ; un exécuteur médié ne l'a pas (opération refusée au
        contrat, la ligne reste sans cgroup)."""

    @abc.abstractmethod
    def close_turn(self, turn_id: str, *, orphan: bool,
                   containers: Sequence[str] | None = None) -> None:
        """Ferme la ligne : `done` si la ressource est rendue, `orphan` sinon."""

    @abc.abstractmethod
    def begin_grace(self, turn_id: str,
                    containers: Sequence[str] | None = None) -> None:
        """Le tour est fini mais son travail de fond tourne encore (délai de
        grâce) : `ended_at` posé, la ligne reste `running` (ses conteneurs ne
        sont pas supprimés par le ménage tant qu'elle l'est)."""

    @abc.abstractmethod
    def mark_orphan(self, turn_id: str,
                    containers: Sequence[str] | None = None) -> None:
        """Marque `orphan` une ressource qui survit à son tour."""

    @abc.abstractmethod
    def settle(self, turn_id: str) -> None:
        """L147 : solde (`done`) la ligne d'un tour dont plus rien ne survit
        (groupe disparu au redémarrage de l'hôte, aucun conteneur en cours)."""

    @abc.abstractmethod
    def settle_many(self, turn_ids: Sequence[str]) -> int:
        """#220 r3 : `settle` de plusieurs lignes en une écriture ; le nombre
        de lignes soldées."""

    @abc.abstractmethod
    def settle_unchanged(self, rows: Sequence[dict]) -> list[str]:
        """#339 r2 : `settle` des lignes lues (`to_review`) qui n'ont pas
        changé depuis — même statut, fin, groupe et conteneurs ; une ligne
        qu'un tour a fermée, marquée ou prolongée entre-temps reste. Les
        `turn_id` soldés."""

    @abc.abstractmethod
    def mark_orphan_many(self, turn_ids: Sequence[str]) -> int:
        """#220 r3 : `mark_orphan` (sans conteneurs) de plusieurs lignes en une
        écriture ; seules celles qui n'étaient pas encore `orphan` changent."""

    @abc.abstractmethod
    def get(self, turn_id: str) -> dict | None:
        """La ligne d'un tour (L73 : un conteneur étiqueté par ce tour), ou None."""

    @abc.abstractmethod
    def open_by_agent(self, agent: str) -> list[dict]:
        """Les lignes encore `running` d'un agent, la plus récente d'abord."""

    @abc.abstractmethod
    def orphans(self, host: str | None = None, limit: int = 50) -> list[dict]:
        """Les lignes `orphan` (de l'hôte si donné), les plus récentes d'abord."""

    @abc.abstractmethod
    def to_review(self, host: str, idle_s: float, after_id: int = 0,
                  limit: int = 500) -> list[dict]:
        """#339: the rows of `host` that may have outlived their resources:
        `orphan` ones, and `running` ones whose turn (or grace period) ended
        or started more than `idle_s` ago, of `id` above `after_id`, by
        increasing `id`: a page of `limit` rows at most."""

    @abc.abstractmethod
    def orphans_started_before(self, host: str, before_ts: float, after_id: int = 0,
                               limit: int = 500) -> list[dict]:
        """L147 : les lignes `orphan` de l'hôte ouvertes avant `before_ts`
        (epoch), d'`id` supérieur à `after_id`, par `id` croissant : une page
        de `limit` lignes au plus (pagination par `id`)."""

    @abc.abstractmethod
    def running_started_before(self, host: str, before_ts: float, after_id: int = 0,
                               limit: int = 500) -> list[dict]:
        """L147b : les lignes `running` de `host` dont le tour a commencé (ou
        fini, délai de grâce) avant `before_ts`, par id croissant au-delà de
        `after_id` — la revue d'après démarrage les lit page par page."""

    @abc.abstractmethod
    def stale_running(self, older_than_s: float, host: str | None = None) -> list[dict]:
        """Les lignes encore `running` ouvertes il y a plus de `older_than_s`
        secondes (fin du tour, pour une ligne en délai de grâce) : un
        exécuteur mort les a laissées derrière lui."""

    @abc.abstractmethod
    def churn(self, since_ts: float, short_s: float) -> list[dict]:
        """L125 : par agent (ordre du nom), les tours FINIS lancés depuis
        `since_ts` : `agent`, `turns`, `short_turns` (durée sous `short_s`
        secondes), `first_short_ts` (début du premier tour court, ou None)."""


class Housekeeping(Domain):
    """Ménage de ce que les agents créent (lot L73, migration 0045).

    Deux tables d'état d'exécution : le journal du ménage
    (`housekeeping_log` : supprimé, évincé, retiré, gardé, signalé, avec la
    taille) et les worktrees apparus pendant un tour (`managed_worktrees`),
    rattachés à l'agent, au tour et au lot. Les décisions (quoi supprimer,
    quand, à quelles conditions) sont dans `ameesh.menage`.
    """

    @abc.abstractmethod
    def log(self, host: str, entries: Sequence[dict], *, actor: str) -> int:
        """Ajoute des lignes au journal (clés : kind, action, path, bytes,
        agent, lot, detail, data) ; rend le nombre de lignes écrites. Élague
        les lignes de plus de 30 jours."""

    @abc.abstractmethod
    def recent(self, host: str | None, since_s: float, limit: int = 50,
               kind: str | None = None) -> list[dict]:
        """Les lignes des `since_s` dernières secondes (de l'hôte si donné, de
        ce `kind` si donné — L162 : les signalements de /tmp ne se noient pas
        dans les bilans), les plus récentes d'abord ; instant en `at_ts`."""

    @abc.abstractmethod
    def latest_by_path(self, host: str | None, kind: str,
                       *, since_s: float | None = None,
                       only_paths_with: Sequence[str] | None = None) -> list[dict]:
        """#222: the last line of each (host, path) of this `kind` (of the host
        if given). Without `since_s`, no time window nor limit: only the
        retention of the log bounds it; with it (#2851), the lines older than
        `since_s` seconds are left out — a caller that ignores them anyway
        does not sort the whole journal.

        `only_paths_with` (#2851) restricts the result to the paths that have
        at least one line whose `action` is in this set. The line returned is
        still the **latest of that path, whatever its action and whatever its
        age**: a path last seen `unowned` then `deleted` comes back with
        `deleted`, so the caller never keeps a closed alert alive. The
        candidate filter runs in SQL, which keeps the sort on a handful of
        paths instead of the whole journal."""

    @abc.abstractmethod
    def summary(self, host: str | None, since_s: float) -> list[dict]:
        """Par (hôte, kind, action) : nombre de lignes et octets, sur les
        `since_s` dernières secondes (les bilans `mesure` exclus).

        #2617 : un signalement `orphan` (ré-écrit toutes les 12 h pour une
        entrée non attribuée) compte une fois par chemin, avec sa dernière
        taille ; #3728 : un relevé `unremovable` aussi (réécrit à chaque
        passage tant que l'épisode dure) ; les autres actions comptent leurs
        lignes."""

    @abc.abstractmethod
    def last_measure(self, host: str | None) -> list[dict]:
        """Le dernier bilan (`kind = mesure`) de chaque hôte (ou de `host`)."""

    @abc.abstractmethod
    def register_worktree(self, *, host: str, path: str, repo: str, agent: str,
                          lot: str | None, turn_id: str | None, branch: str,
                          head: str) -> dict | None:
        """Enregistre un worktree apparu pendant un tour ou découvert dans un
        dépôt local ; None s'il est déjà suivi (vivant ou gardé). Un chemin
        auparavant removed/gone reçoit un nouveau cycle, sans ancien lot."""

    @abc.abstractmethod
    def worktrees(self, host: str | None, statuses: Sequence[str] | None = None,
                  limit: int | None = 500) -> list[dict]:
        """Worktrees suivis (de l'hôte, des statuts donnés), les plus anciens
        d'abord ; instants en `created_ts`, `checked_ts`, `ended_ts`.
        `limit=None` : tous (#2174, le ménage de l'hôte)."""

    @abc.abstractmethod
    def set_worktree_status(self, worktree_id: int, status: str, detail: str) -> dict | None:
        """Change le statut d'un worktree suivi (`ended_at` posé pour
        `removed` et `gone`) ; rend la ligne, ou None."""

    @abc.abstractmethod
    def set_worktree_attribution(self, worktree_id: int, agent: str, lot: str,
                                 detail: str) -> dict | None:
        """Rattache un worktree suivi à l'agent et au lot que son nom prouve
        (#3266) : agent, lot et détail changent, le statut reste ; rend la
        ligne, ou None."""


class TurnObservation(Domain):
    """Les faits d'un tour ou d'une requête remise (#2167, lot #4831).

    Une seule lecture, sans écriture : le domaine métier
    (`ameesh.turn_observation`) en déduit l'état du tour, la comptabilité et
    la quiescence. Aucun contenu de message n'est rendu : la lecture ne
    sélectionne ni corps ni `payload`."""

    @abc.abstractmethod
    def read(self, *, turn: str | None = None, request: int | None = None) -> dict | None:
        """Les faits d'un tour (`turn`) ou d'un message de la boîte (`request`).

        Rend `{"request": ligne|None, "turn": {"id", "resource", "marker",
        "ledger"}, "open_reservation": ligne|None, "agent": ligne}` ;
        `request` est la ligne de la boîte (destinataire, état, `created_ts`,
        `delivered_ts`, `delivered_turn_id`, `withdrawn`), `resource`/`marker`/
        `ledger` les lignes `turn_resources`, `spend_pending` et `turn_costs`
        du tour (ou None), `open_reservation` la réservation ouverte de
        l'agent quand le tour n'a ni marqueur ni grand livre (#2166 : la clé
        du tour a disparu avec le marqueur), `agent` la ligne du registre.
        None si la cible n'existe pas (message inconnu, tour sans aucun fait,
        ou agent absent du registre).

        Une requête n'est pas un tour : un message non remis rend
        `"turn": {"id": None, ...}` et reste observable (l'agent est son
        destinataire)."""
        raise NotImplementedError


class Visibility(Domain):
    """Cache court de la règle de visibilité (0029) : le responsable et les
    administrateurs d'un hôte doivent avoir accès au dépôt de mémoire de la
    persona. Fail closed : l'absence de verdict frais n'admet pas."""

    @abc.abstractmethod
    def cached(self, persona: str, host: str, now_ts: float,
               context: str) -> dict | None:
        """Le verdict NON EXPIRÉ pour (persona, hôte) **et ce contexte**
        (empreinte du dépôt+forge, des humains requis et de leurs comptes) ;
        None si l'empreinte diffère, même si un ancien verdict vit encore."""

    @abc.abstractmethod
    def put(self, persona: str, host: str, *, ok: bool, diagnostic: str,
            repository: str | None, context: str, ttl_s: float) -> dict:
        """Écrit (ou remplace) le verdict, son contexte et son échéance ; rend
        la ligne."""

    @abc.abstractmethod
    def purge(self, now_ts: float) -> int:
        """Efface les verdicts expirés ; rend le nombre de lignes."""


# --------------------------------------------------------------------------
# liaisons de session (L41, décision 0030, migration 0034)
# --------------------------------------------------------------------------

class SessionBindings(Domain):
    """Liaison explicite (hôte, harnais, identifiant de session) → agent.

    Au plus UNE liaison active par (hôte, harnais, session) ; une liaison
    n'est jamais effacée : la révoquer pose `revoked_at`."""

    @abc.abstractmethod
    def active(self, host: str, harness: str, session_id: str) -> dict | None:
        """La liaison active de cette session sur cet hôte, ou None."""

    @abc.abstractmethod
    def bind(self, *, host: str, harness: str, session_id: str, agent: str,
             pid: int | None, created_by: str,
             pid_started_at: float | None = None) -> dict | None:
        """Crée la liaison si la session n'en a pas d'active ; rend la ligne
        créée, ou None si une liaison active existe déjà (rien n'est écrit).
        L46, L63 : `pid_started_at`, heure de démarrage du PID en secondes
        epoch (NULL : non contrôlée). L'ancienne `pid_start` (tops d'horloge
        Linux, 0036) n'est plus écrite ; elle reste lue."""

    @abc.abstractmethod
    def set_pid(self, binding_id: int, pid: int | None,
                pid_started_at: float | None = None) -> dict | None:
        """Change le PID ancêtre exigé (et son heure de démarrage, L46) d'une
        liaison ACTIVE ; None sinon."""

    @abc.abstractmethod
    def revoke(self, host: str, harness: str, session_id: str) -> dict | None:
        """Révoque la liaison active de cette session ; rend la ligne, ou None."""

    @abc.abstractmethod
    def listing(self, *, host: str | None = None, agent: str | None = None,
                include_revoked: bool = False) -> list[dict]:
        """Les liaisons (actives seulement par défaut), récentes d'abord."""

    @abc.abstractmethod
    def with_pids(self, host: str, pids: Sequence[int]) -> list[dict]:
        """Les liaisons actives de cet hôte dont le PID est dans `pids`."""


# --------------------------------------------------------------------------
# sessions mises de côté par lot et réveils sur lot actionnable (L165b, #408)
# --------------------------------------------------------------------------

class LotSessions(Domain):
    """Une ligne par (agent, lot) : la session mise de côté pour ce lot
    (`session_id` et le harnais, le compte et le dossier sous lesquels seuls
    elle se reprend), et les réveils de l'exécuteur sur ce lot (règle 1).
    La session ACTIVE reste au registre (`session_id`, `session_work_item`)."""

    #: lots que la règle 1 reprend
    ACTIONABLE_STATES = lifecycle.ACTIVE
    #: note de lot qui retient le réveil (`ameesh work note N "hold: …"`)
    HOLD_PREFIX = "hold:"
    #: notes du réveil programmé (#2466 : pose, annulation, déclenchement) :
    #: elles ne changent pas la retenue d'un lot tenu par une note `hold:`
    WAKE_PREFIX = "wake:"

    @abc.abstractmethod
    def switch(self, name: str, owner: str, epoch: int, *, to_lot: str | None,
               harness: str | None, account: str | None, cwd: str | None,
               current: str | None = None) -> dict | None:
        """Change le lot de la session active, **fencé par le bail**, en une
        transaction : la session active `current` est mise de côté pour le lot
        de session noté au registre (s'il est un lot encore ouvert et assigné
        à l'agent), avec l'origine notée à son OUVERTURE (`session_harness`,
        `session_cwd`, `session_account` si `session_origin` est `current` ;
        inconnue sinon, elle ne sera pas reprise) ; puis celle mise de côté
        pour `to_lot` devient active si ce lot est encore ouvert et assigné à
        l'agent et qu'elle a été ouverte sous les harnais `harness`, compte
        `account` (celui du prochain tour) et dossier `cwd` (sinon elle est
        écartée et la session active est vide : une session neuve s'ouvrira).
        `to_lot=None` : la session active est mise de côté pour son lot et
        AUCUNE ne devient active (`session_work_item` remis à NULL) — le tour
        part dans une session sans lot (#2055).
        Rend `{resumed, parked, discarded, discard_reason}` (identifiants ou
        None ; raison `origin` ou `lot`), ou None si le bail n'est plus le
        nôtre (rien n'est écrit)."""

    @abc.abstractmethod
    def listing(self, name: str | None = None) -> list[dict]:
        """Les lignes (d'un agent), avec `parked_ts`, `last_wake_ts`,
        `wake_activity_ts`."""

    @abc.abstractmethod
    def actionable(self, name: str, *, today: str | None = None) -> list[dict]:
        """Les lots de l'agent que la règle 1 peut reprendre, par priorité
        puis ancienneté : assignés à lui, en `intake`/`build`, ni prévus après
        `today`, ni en attente d'une décision (demande non lue, jalon de
        décision ouvert), d'un lot bloquant ouvert ou d'une note `hold:`
        (la dernière posée par un agent du registre ou un humain). La retenue
        tient jusqu'à une note de l'assigné ou d'un humain (`human:…`), une
        levée explicite (`unhold:`, `hold lifted:`…) ou un changement d'état ;
        ne la lèvent pas une note `wake:` (#2466 : pose ou déclenchement d'un
        réveil programmé), une note de l'outillage (#3992 : composition de
        paquet, runner, git…) ni la note ordinaire d'un autre agent (#4162,
        comme `planned_waits.active_holds`). Chaque
        ligne : `id`, `title`, `state`, `priority`, `activity_ts` (dernier
        signe de vie du lot : lot, journal, pointe de branche, courrier sur
        le lot), `wakes`, `last_wake_ts`, `wake_activity_ts`."""

    @abc.abstractmethod
    def note_wake(self, name: str, lot: int, activity_ts: float | None) -> dict:
        """Note un réveil de l'agent sur le lot, horloge de la base : le
        compte repart à 1 si le lot a vécu depuis le réveil précédent
        (`activity_ts` plus récent que `wake_activity_at`), sinon il croît.
        Rend la ligne."""

    @abc.abstractmethod
    def due_wakes(self, name: str, now: float, claim_ttl_s: float = 120.0) -> list[dict]:
        """#2466 : les réveils programmés échus des lots encore ouverts de
        `name`, par échéance puis priorité : `id`, `title`, `state`,
        `priority`, `wake_ts`. Un réveil programmé est un rendez-vous
        explicite : il ne dépend ni de l'état `intake`/`build`, ni d'une note
        `hold:`, ni d'une décision ouverte — seulement de l'assignation et de
        l'échéance. Un claim plus récent que `claim_ttl_s` écarte le lot : sa
        livraison est en cours. Une base d'avant 0466 rend une liste vide."""

    @abc.abstractmethod
    def claim_wake(self, name: str, lot: int, now: float,
                   claim_ttl_s: float = 120.0) -> dict | None:
        """#2466 : réclame le réveil programmé échu du lot (bail de livraison),
        en une écriture atomique : `wake_claim_at` est posé, rien n'est écrit
        au journal (#2614). Rend `{id, state, title, wake_ts}` ou None si le
        lot n'a plus de réveil échu, est déjà réclamé depuis moins de
        `claim_ttl_s`, n'est plus assigné à `name` ou est fini. L'échéance
        n'est pas effacée : `consume_wake` le fera au lancement du tour, un
        claim non consommé expirant après `claim_ttl_s`. Le détenteur renouvelle
        son bail avec `claim_ttl_s=0` tant que le lancement se prépare (#2614)."""

    @abc.abstractmethod
    def consume_wake(self, name: str, lot: int, wake_ts: float) -> bool:
        """#2466 : consomme le réveil programmé délivré : efface `wake_at` et
        `wake_claim_at` (une seule fois), **seulement** si l'échéance est
        encore celle qui a été réclamée (`wake_ts`) et sous claim — une
        nouvelle échéance posée entre-temps est conservée. Rend False sinon.
        #2614 : la consommation écrit au journal, dans la même transaction, la
        ligne `wake: … delivered …` — un témoin par réveil réellement délivré."""

    @abc.abstractmethod
    def purge(self, name: str | None = None) -> list[dict]:
        """Supprime les lignes des lots fusionnés, livrés ou fermés, ou qui ne
        sont plus assignés à leur agent ; rend les lignes supprimées. Ne touche
        pas au registre : la session ACTIVE d'un lot fini est fermée par
        l'agent qui la détient, à son tour suivant, sous son bail (#794,
        revue Claude B1 : ce balayage n'a pas le bail et peut tomber en plein
        tour)."""


# --------------------------------------------------------------------------
# résultats de CI relevés en direct (`ci_runs`, `ci_test_failures`, L161c)
# --------------------------------------------------------------------------

class CiResults(Domain):
    """Exécutions de CI suivies et leurs tests en échec (jamais les réussites)."""

    @abc.abstractmethod
    def run(self, repo: str, run_id: int) -> dict | None:
        """L'exécution suivie, ou None."""

    @abc.abstractmethod
    def open_run(self, repo: str, run_id: int, *, head_sha: str, branch: str, event: str,
                 base: str, work_item_id: int | None, url: str | None,
                 workflow: str = "", created_at: str | None = None,
                 run_attempt: int = 1) -> dict:
        """Enregistre l'exécution (`running`) si elle est nouvelle ; la rend.
        `workflow` : le fichier de workflow qui l'a produite (#1110).
        `created_at` : la création de l'exécution chez GitHub (0422).
        `run_attempt` : l'essai GitHub de l'exécution (#901)."""

    @abc.abstractmethod
    def mark_started(self, repo: str, run_ids: Sequence[int]) -> None:
        """#747 : ces exécutions sont vues `in_progress` ; la première fois,
        `started_at` prend l'heure du relevé (la fin de l'attente d'un runner)."""

    @abc.abstractmethod
    def reopen_run(self, repo: str, run_id: int, *, run_attempt: int) -> dict | None:
        """Une exécution conclue que GitHub dit de nouveau en cours (relance,
        #901 : même `run_id`, `run_attempt` strictement supérieur) repasse
        `running`, note l'attempt et efface les messages de l'attempt
        précédent. None si rien ne change : attempt non supérieur (une liste
        en retard, #828, ne rouvre pas une exécution conclue au même attempt)
        ou tête plus récente de la branche (la relance d'une exécution périmée
        ne la ressuscite pas)."""

    @abc.abstractmethod
    def running(self, repo: str) -> list[dict]:
        """Les exécutions encore `running` du dépôt."""

    @abc.abstractmethod
    def running_on(self, branches: Sequence[str]) -> list[dict]:
        """Les exécutions encore `running` d'une branche de base, tous dépôts
        confondus (#846) : un main rouge est lu sans attendre la conclusion de
        ses jobs agrégateurs, qui peut rester des heures en file."""

    @abc.abstractmethod
    def conclude(self, repo: str, run_id: int, status: str) -> bool:
        """`running` → `status` ; False si elle était déjà conclue."""

    @abc.abstractmethod
    def supersede(self, repo: str, branch: str, head_sha: str, run_id: int) -> list[dict]:
        """Les exécutions `running` ou `cancelled` de la branche, plus
        anciennes que `run_id` et sur une autre tête, passent `superseded` ;
        rend celles qui ont changé."""

    @abc.abstractmethod
    def set_message(self, repo: str, run_id: int, which: str, message_id: int) -> None:
        """Note le message `urgent` ou `verdict` envoyé pour l'exécution."""

    @abc.abstractmethod
    def failures(self, repo: str, run_id: int) -> list[dict]:
        """Les tests en échec de l'attempt courant de l'exécution, dans
        l'ordre où ils ont échoué."""

    @abc.abstractmethod
    def add_failure(self, repo: str, run_id: int, *, run_attempt: int, part: str, test: str,
                    module: str, status: str, inherited: bool, excerpt: str, replay: str,
                    duration_s: float | None, failed_at: str | None) -> bool:
        """Enregistre un échec de l'attempt `run_attempt` ; False s'il l'était
        déjà (même part, même test, même attempt). Un échec de l'attempt
        précédent re-vu à la relance reprend la ligne et compte comme nouveau ;
        un événement d'un attempt antérieur ne réécrit jamais la ligne d'un
        attempt postérieur (#901)."""

    @abc.abstractmethod
    def set_failure_message(self, repo: str, run_id: int, run_attempt: int,
                            tests: Sequence[tuple], message_id: int) -> None:
        """Note le message qui a signalé ces échecs (`(part, test)`) de l'attempt."""

    @abc.abstractmethod
    def base_failures(self, repo: str, branch: str, *, workflow: str = "") -> dict:
        """Dernière exécution conclue (verte ou rouge) de la **tête la plus récemment
        poussée** de `branch`, pour ce workflow (#1683 : la conclusion la plus
        récente peut être celle d'une tête plus ancienne) : `{run_id, head_sha,
        tests}` (tests en échec de son attempt courant), ou {} sans exécution.
        Le workflow évite de comparer deux suites étrangères ; les lignes
        antérieures à la migration 0433, toutes de l'ancien filtre global
        `tests.yml`, y sont nommées."""

    @abc.abstractmethod
    def message_ids(self, repo: str, run_ids: Sequence[int]) -> list[int]:
        """Les messages envoyés pour ces exécutions (urgent et échecs)."""

    @abc.abstractmethod
    def for_lot(self, work_item_id: int, limit: int) -> list[dict]:
        """Les exécutions d'un lot, de la plus récente à la plus ancienne."""

    @abc.abstractmethod
    def latest_concluded(self, branches: Sequence[str]) -> list[dict]:
        """Par dépôt, branche et workflow, sur la dernière tête conclue de la
        branche, l'exécution conclue verte ou rouge la plus récente. #3686 :
        un `workflow_dispatch` d'un workflow qui ne tourne jamais autrement
        sur la branche n'y compte pas (geste d'opérateur, pas l'état fusionné)."""

    @abc.abstractmethod
    def head_replaced(self, repo: str, branch: str, head_sha: str, run_id: int) -> bool:
        """Vrai si une autre tête de cette branche a été vue après cette
        exécution (une exécution de `run_id` supérieur) : son verdict serait
        périmé."""

    # -- #845 : garde de la CI (runners et file), relevée par l'API ---------
    @abc.abstractmethod
    def observe_runners(self, repo: str, runners: Sequence[dict]) -> None:
        """Enregistre l'état des runners du dépôt (relevé `ci.Watcher`) :
        `[{id, name, busy, job, stuck}]`. `stuck_since` garde le début de
        l'épisode « busy sans job in_progress » et redevient NULL dès qu'il
        finit ; `busy_since` suit l'état busy, pour le diagnostic. Les runners
        absents du relevé (retirés du dépôt) sont supprimés : pas d'alerte
        fantôme. Un relevé partiel n'est jamais écrit (l'appelant le saute)."""

    @abc.abstractmethod
    def stuck_runners(self) -> list[dict]:
        """Les runners dont l'épisode « busy sans job in_progress » est encore
        ouvert : `{repo, runner_id, name, job, since_ts, busy_ts, seen_ts}`
        (`since_ts` : début de l'épisode)."""

    @abc.abstractmethod
    def observe_queue(self, repo: str, *, oldest_id: int | None, oldest_at: str | None,
                      queued: int) -> None:
        """Enregistre la file vue du dépôt : le plus vieux **job** `queued`
        (`oldest_id` : identifiant GitHub du job ; `oldest_at` : son
        `created_at`) et le nombre de jobs `queued` vus, toutes exécutions non
        conclues. Un run peut être ancien (reprise) ou `in_progress` et porter
        un job en file : la date du run ne date pas ses jobs. `queued=0` efface
        le relevé précédent."""

    @abc.abstractmethod
    def queued_oldest(self) -> list[dict]:
        """Le dernier relevé de file par dépôt : `{repo, oldest_id, oldest_ts,
        queued, seen_ts}` (les dépôts sans job `queued` ont `oldest_ts` NULL)."""

    @abc.abstractmethod
    def prune_queue(self, repos: Sequence[str]) -> None:
        """Retire les relevés de file des dépôts hors du périmètre actif
        `repos`. Un dépôt sorti de `github.projects` ne doit pas laisser une
        ligne qui fonderait une alerte de silence perpétuelle sur les autres
        hôtes (#2957). Un périmètre vide ne purge rien : il ne prouve pas que
        des dépôts ont été retirés."""

    # -- #3344 : âge de la carte de mesure ----------------------------------
    @abc.abstractmethod
    def observe_map(self, repo: str, *, branch: str, run_id: int | None,
                    head_sha: str | None, published_at: str | None) -> None:
        """Enregistre la dernière exécution réussie de `carte.yml` vue sur la
        branche de base `branch` du dépôt (`published_at` : son `updated_at`
        GitHub ; tout à None si aucune n'a été trouvée). La date du relevé est
        celle de la base."""

    @abc.abstractmethod
    def maps(self) -> list[dict]:
        """Le dernier relevé de carte par dépôt : `{repo, branch, run_id,
        head_sha, published_ts, seen_ts}` (`published_ts` NULL : aucune
        exécution réussie trouvée)."""

    @abc.abstractmethod
    def forget_map(self, repo: str) -> None:
        """Retire le relevé de carte d'un dépôt qui ne porte plus `carte.yml`."""

    @abc.abstractmethod
    def prune_maps(self, repos: Sequence[str]) -> None:
        """Retire les relevés de carte des dépôts hors du périmètre actif
        `repos` (comme `prune_queue`) ; un périmètre vide ne purge rien."""

    # -- #845 (revue N/P2 2328) : fenêtre et cache du relevé ----------------
    @abc.abstractmethod
    def claim_guard_scan(self, repo: str, *, now: float, window_s: float) -> bool:
        """Revendique la fenêtre du relevé de `repo`, atomiquement, à
        l'horloge `now` : vrai si aucun relevé ne l'a prise depuis `window_s`.
        L'écriture précède tout appel HTTP, donc deux relevés concurrents ne
        peuvent pas doubler le coût et un relevé tué en vol expire."""

    @abc.abstractmethod
    def finish_guard_scan(self, repo: str, *, now: float, calls: int, runs: int,
                          covered: int, complete: bool, reason: str | None = None) -> None:
        """Clôt le relevé : appels réellement consommés, exécutions listées et
        couvertes, et raison d'un relevé incomplet (qui n'a rien publié)."""

    @abc.abstractmethod
    def observe_guard_run(self, repo: str, *, run_id: int, attempt: int, status: str,
                          queued: int, oldest_id: int | None, oldest_at: str | None,
                          runners: dict) -> None:
        """Écrit le relevé d'une exécution : jobs `queued` (compte et plus
        vieux) et runners tenant un job `in_progress` (`{nom: job}`)."""

    @abc.abstractmethod
    def prune_guard_runs(self, repo: str, keep) -> None:
        """Retire du cache les exécutions absentes de `keep`
        (`(run_id, attempt)`), qui ne sont plus non conclues."""


class Handoffs(Domain):
    """L232a: destination reservations and durable lot-boundary handoffs."""

    @abc.abstractmethod
    def controller(self, operation: str, *, reconcile: bool = False):
        """Lock context yielding the journal DB, or None if another controller owns it.

        Serialize bounded, idempotent adapter steps with cancel/retry. Journal
        changes commit independently of this lock so the destination can claim
        after activation. Adapters must impose timeouts; no agent-row lock
        spans an external call.
        """

    @abc.abstractmethod
    def begin_effect(self, operation: str, expected: str, effect: str) -> dict | None:
        """Commit effect intent under the controller epoch before invoking a host."""

    @abc.abstractmethod
    def finish_effect(self, operation: str, epoch: int) -> dict:
        """Clear a completed or explicitly quiesced intent under the current controller."""

    @abc.abstractmethod
    def prepare_reconcile(self, operation: str, epoch: int) -> dict:
        """CAS the unfinished intent under the current epoch before host reconciliation."""

    @abc.abstractmethod
    def get(self, operation: str) -> dict | None: ...

    @abc.abstractmethod
    def active(self, agent: str) -> dict | None: ...

    @abc.abstractmethod
    def cooling(self, agent: str, seconds: int) -> bool: ...

    @abc.abstractmethod
    def occupancy(self, host: str) -> int: ...

    @abc.abstractmethod
    def hold(self, operation: str, expected: str) -> dict | None: ...

    @abc.abstractmethod
    def events(self, operation: str) -> list[dict]: ...

    @abc.abstractmethod
    def reserve(self, operation: str, plan: dict, *, ttl: int) -> dict | None: ...

    @abc.abstractmethod
    def renew(self, operation: str, *, ttl: int) -> dict | None:
        """Renew a held reservation after explicit adapter reconciliation and fresh readings."""

    @abc.abstractmethod
    def transition(self, operation: str, expected: str, state: str,
                   *, evidence: dict | None = None, detail: str = "") -> dict | None: ...

    @abc.abstractmethod
    def drain(self, operation: str) -> dict | None: ...

    @abc.abstractmethod
    def recover_source(self, operation: str, *, proof: dict) -> dict | None:
        """Fence the expired source epoch after scoped quiescence and publication proof."""

    @abc.abstractmethod
    def promote_loss(self, operation: str) -> dict | None:
        """Convert a pre-switch handoff after loss, preserving its capacity reservation."""

    @abc.abstractmethod
    def cancellation_guard(self, operation: str):
        """Yield a journal API whose completion/fence writes share the source gate transaction."""

    @abc.abstractmethod
    def switch(self, operation: str, *, cwd: str, brief: str) -> dict | None: ...

    @abc.abstractmethod
    def unfence(self, operation: str, expected: str, state: str) -> dict | None: ...


class PrevalidationReports(Domain):
    """Rapports de prévalidation au gel (#1770, 0450) : une ligne par (lot, tête).

    Le rapport `ameesh-prevalidation/1` est une preuve, pas un verdict : il ne
    remplace ni la désignation au gel (#1617) ni le gel dérivé (#1756)."""

    @abc.abstractmethod
    def record(self, report: dict) -> None:
        """Écrit le rapport de sa clé (lot, tête), après le run distant ;
        réécrire la même clé remplace le rapport courant."""

    @abc.abstractmethod
    def report(self, work_item_id: int, head_sha: str) -> dict | None:
        """Le rapport enregistré pour cette tête exacte, ou None."""

    @abc.abstractmethod
    def latest(self, work_item_id: int) -> dict | None:
        """Le rapport le plus récent du lot (toute tête), ou None."""


# --------------------------------------------------------------------------
# le stockage d'une connexion
# --------------------------------------------------------------------------

class GithubWatch(Domain):
    """#2845: transactional GitHub intake; notifier host only."""

    @abc.abstractmethod
    def cursor(self, project: str, repo: str) -> float | None:
        """Last completed scan, absent before bootstrap."""

    @abc.abstractmethod
    def lock(self, project: str, repo: str, initial: float) -> float:
        """Create and lock the cursor until commit; return its timestamp."""

    @abc.abstractmethod
    def remember(self, project: str, repo: str, kind: str, object_id: int,
                 digest: str) -> bool:
        """True only for a new object version. Caller holds the cursor lock."""

    @abc.abstractmethod
    def advance(self, project: str, repo: str, until: float) -> None:
        """Advance after every mailbox deposit succeeded, never backwards."""

    @abc.abstractmethod
    def scan(self, project: str, repo: str) -> dict | None:
        """Incomplete scan window and next page, absent when the scan is complete."""

    @abc.abstractmethod
    def checkpoint(self, project: str, repo: str, progress: dict | None) -> None:
        """Persist or clear partial progress atomically with mailbox delivery."""


class FingerprintWatch(Domain):
    """#3885: the resume cursor of the failure-fingerprint pass, per project.

    The source is read with a read-only token; the cursor is opaque and only
    advances after a complete, strictly validated page. A failed read keeps
    the previous cursor so the next pass replays the same window.
    """

    @abc.abstractmethod
    def state(self, project: str) -> dict | None:
        """`{cursor, last_read_at, last_error}`, or None before the first read."""

    @abc.abstractmethod
    def advance(self, project: str, url: str, cursor: str | None, *,
                now: float | None = None) -> None:
        """Record a complete read: move the cursor, clear the last error."""

    @abc.abstractmethod
    def fail(self, project: str, url: str, error: str, *,
             now: float | None = None) -> None:
        """Record an explicit refusal/read failure; never move the cursor."""


class NotifyLiveness(Domain):
    """#3007 : preuve partagée que le notificateur joint un humain.

    Le seul notificateur à canaux peut tomber avec son poste ; les autres
    hôtes lisent ces battements dans la base au lieu de conclure de leur
    propre configuration. `human = '*'` dit que `notify.default` couvre tout
    humain. Un battement est un fait d'hôte, jamais un secret : hôte,
    `human:<id>` et instant.
    """

    @abc.abstractmethod
    def beat(self, host: str, humans: Sequence[str], *,
             now: float | None = None) -> int:
        """Écrit (ou rafraîchit) un battement par humain couvert ; rend le
        nombre de lignes. Une liste vide n'écrit rien."""

    @abc.abstractmethod
    def latest(self) -> list[dict]:
        """Tous les battements connus — les périmés compris : l'appelant juge
        la fraîcheur (`host`, `human`, `seen_ts`)."""

    @abc.abstractmethod
    def claim_brief(self, recipient: str, human: str, *, since: float,
                    now: float | None = None) -> dict:
        """Réclame atomiquement le brief de `(recipient, human)` si son dernier
        envoi est plus ancien que `since` (epoch) ; rend
        `{"claimed": bool, "previous_ts": float | None}`. `previous_ts` est
        l'envoi précédent quand la réclamation est prise (None au premier), ce
        qui permet d'écrire la relance. La transaction est terminée au retour :
        l'appelant envoie ensuite le courrier, et `release_brief` le rend en
        cas d'échec."""

    @abc.abstractmethod
    def release_brief(self, recipient: str, human: str) -> None:
        """Rend une réclamation dont le courrier n'a pas pu être déposé."""

    # -- #3576 : état de livraison partagé de la passe alerts ----------------
    @abc.abstractmethod
    def acquire_state(self, scope: str, owner: str, ttl_seconds: float) -> int | None:
        """Prend le bail d'exécution de l'état `scope` ; rend son epoch.

        La ligne d'état est créée au premier appel (`body` vide, à amorcer).
        Un bail non expiré d'un AUTRE owner refuse la prise (`None`) ; le même
        owner le reprend à une epoch neuve. `expires_at` est lu avec l'heure
        réelle de la base, jamais avec l'horloge de l'hôte.
        """

    @abc.abstractmethod
    def read_state(self, scope: str) -> dict | None:
        """Le document de livraison partagé, ou None tant qu'il n'est pas
        amorcé (ligne absente, `body` vide ou schéma inconnu).

        Lecture sans verrou : un écrivain doit avoir pris le bail, l'appelant
        ne livre que sous ce bail.
        """

    @abc.abstractmethod
    def save_state(self, scope: str, body: dict, *, owner: str, epoch: int) -> bool:
        """Écrit le document sous le bail `(owner, epoch)`, s'il est encore
        vivant ; False si le bail a été perdu ou a expiré (rien n'est écrit :
        un détenteur périmé ne remplace jamais l'état du nouveau)."""

    @abc.abstractmethod
    def renew_state(self, scope: str, owner: str, epoch: int,
                    ttl_seconds: float) -> bool:
        """Renouvelle le bail vivant `(owner, epoch)` sans changer son epoch.

        #3913 : une passe d'alertes dure plus que la TTL courte du bail ; le
        renouvellement la maintient sans rouvrir la course à l'écriture.
        False si le bail n'est plus celui-là ou s'il est déjà expiré (un bail
        expiré ne ressuscite jamais).
        """

    @abc.abstractmethod
    def state_lease(self, scope: str) -> dict | None:
        """Le bail courant de l'état : `owner`, `epoch`, `expires_ts` et
        `now_ts` (heure de la base). None si la ligne ou l'owner manque.

        Sert à reprendre le bail d'un processus mort du MÊME hôte (#3913),
        jamais à décider d'un autre hôte : son PID n'est pas visible ici.
        """

    @abc.abstractmethod
    def release_state(self, scope: str, owner: str, epoch: int) -> bool:
        """Rend le bail d'exécution de l'état, à son epoch."""


class Storage(abc.ABC):
    """Les domaines du stockage, liés à une connexion ou une transaction."""

    catalog: Catalog
    veille: Veille

    #: nom du pilote de stockage (`postgres`)
    driver: str
    #: la connexion (ou la transaction) sous-jacente
    db: Any

    agents: Agents
    leases: Leases
    pending_spend: PendingSpend
    turn_costs: TurnCosts
    accounts: Accounts
    account_windows: AccountWindows
    provider_credits: ProviderCredits
    credentials: Credentials
    budgets: Budgets
    api_requests: ApiRequests
    alert_acknowledgements: AlertAcknowledgements
    watches: Watches
    events: Events
    mailbox: Mailbox
    github_watch: GithubWatch
    fingerprints: FingerprintWatch
    notify_liveness: NotifyLiveness
    wakeups: Wakeups
    keys: Keys
    approvals: Approvals
    nonces: Nonces
    work: WorkItems
    work_dependencies: WorkDependencies
    work_footprints: WorkFootprints
    provenance: Provenance
    migration_reservations: MigrationReservations
    lot_sessions: LotSessions
    packages: WorkPackages
    actions: Actions
    action_source: ActionSource
    canon: Canon
    ephemerals: Ephemerals
    authenticators: Authenticators
    threads: Threads
    grants: Grants
    placements: Placements
    progress: Progress
    projects: Projects
    decisions: Decisions
    roadmap: Roadmap
    operations: Operations
    hosts: HostResources
    host_lifecycle: HostLifecycle
    turn_resources: TurnResources
    turn_observation: TurnObservation
    visibility: Visibility
    housekeeping: Housekeeping
    session_bindings: SessionBindings
    ci: CiResults
    conversations: Conversations
    handoffs: Handoffs
    prevalidation: PrevalidationReports
