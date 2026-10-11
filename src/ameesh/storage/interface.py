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
                   restore_prompt end_turn
   pending_spend   put set_model get clear
   turn_costs      last_reading insert spent ledger correct
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
                   overdue_delegations returned_delegations backlog_add backlog
                   auto_takes_since issue_feed set_issue_ref
   packages       all get upsert retire
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
   progress        lots lot_events lot_milestones lot_actions actions agents costs
                   packages package_items lot_messages
   projects        board
   roadmap         plan_item item_plans plan_package items commitments
                   add_commitment record_proposal set_commitment
   operations      set_settings set_session_work_item listing request_restart
                   apply_restart adopt resume message_lots assigned_open_lots
                   open_lots_activity turns record_gauges gauge_history
                   latest_gauges assigners record_balance balances
   session_bindings active bind set_pid revoke listing with_pids
   budgets         limits put events
   turn_resources  open_turn close_turn mark_orphan get open_by_agent orphans
                   stale_running churn
   housekeeping    log recent summary last_measure register_worktree worktrees
                   set_worktree_status

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

import abc
from typing import Any, Callable, Protocol, Sequence


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
        """La ligne de l'agent, ou None."""

    @abc.abstractmethod
    def harnesses(self) -> dict[str, str]:
        """L71 : `{agent: harnais}` pour tout le registre (comptabilité, jauges)."""

    @abc.abstractmethod
    def overview(self) -> list[dict]:
        """Vue d'observabilité (`mesh list --json`), colonnes du canon comprises."""

    @abc.abstractmethod
    def claimable(self, host: str, names: Sequence[str] | None, *,
                  require_responsible: bool) -> list[dict]:
        """Agents de l'hôte sans bail vivant, réclamables (mêmes règles que `claim`)."""

    @abc.abstractmethod
    def mark_event_wake(self, name: str) -> None:
        """Note l'instant du dernier réveil d'événements."""

    @abc.abstractmethod
    def set_session(self, name: str, session_id: str, account: str | None = None) -> None:
        """Enregistre la session du harnais et le compte sous lequel elle tourne
        (`session_account`, L39 ; None : compte inconnu ou aucun compte déclaré)."""

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
              require_responsible: bool) -> dict | None:
        """Prend le bail s'il est libre ou expiré ; rend la ligne, sinon None."""

    @abc.abstractmethod
    def attach_claim(self, name: str, owner: str, ttl_seconds: float, *,
                     require_responsible: bool) -> dict | None:
        """Prend le bail pour une session interactive (`ameesh attach`)."""

    @abc.abstractmethod
    def state(self, name: str) -> dict | None:
        """Détenteur, epoch, statut et vie du bail (`live`), ou None."""

    @abc.abstractmethod
    def renew(self, name: str, owner: str, epoch: int, ttl_seconds: float) -> float | None:
        """Prolonge le bail ; rend la nouvelle échéance, None si on ne le détient plus."""

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
        """Oublie la session, fencé par un bail valide."""

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


class PendingSpend(Domain):
    """Marqueur comptable d'un tour (`spend_pending`) : une ligne par agent."""

    @abc.abstractmethod
    def put(self, name: str, start_index: int, turn: str | None, model: str | None) -> bool:
        """Pose (atomiquement) le marqueur du tour, en remplaçant le précédent."""

    @abc.abstractmethod
    def set_model(self, name: str, model: str | None) -> bool:
        """Remplace le modèle du marqueur ; faux s'il n'y a pas de marqueur."""

    @abc.abstractmethod
    def get(self, name: str) -> dict | None:
        """Le marqueur (agent, start_index, turn, model, created_ts), ou None."""

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
    def insert(self, *, agent: str, harness: str, turn: str | None, model: str | None,
               session: str | None, usd: float, input_tokens: int,
               cached_input_tokens: int, output_tokens: int, cum_usd: float | None,
               cum_input_tokens: int | None, cum_cached_input_tokens: int | None,
               cum_output_tokens: int | None, account: str | None = None,
               spend_key: str | None = None) -> bool:
        """Écrit la ligne du tour (une instruction) ; une erreur de base remonte
        telle quelle et n'a rien écrit. `account` (L30, migration 0028) : le
        compte qui a porté le tour ; None = colonne non écrite. `spend_key`
        (L60, migration 0041) : clé du marqueur comptable ; une ligne portant
        déjà cette clé n'est pas réécrite (faux). Vrai si la ligne est écrite."""

    @abc.abstractmethod
    def spent(self, seconds: float, *, agent: str,
              harnesses: Sequence[str] | None, account: str | None = None) -> float:
        """Somme des coûts des `seconds` dernières secondes (horloge de la
        base), de l'agent (`"all"` : tout le compte, comme `cost spent`), de
        ces harnais seulement si donnés, de ce compte seulement si donné (L30)."""

    @abc.abstractmethod
    def spent_between(self, from_ts: float, to_ts: float, *,
                      harnesses: Sequence[str]) -> float:
        """L95 : somme des coûts de ces harnais dont la ligne est écrite dans
        ]from_ts, to_ts] (epoch) ; les lignes écartées (0043) ne comptent pas.
        Sert à comparer l'estimation à la baisse réelle d'un solde sur le même
        intervalle."""

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
            actor: str) -> tuple[bool, float | None]:
        """Pose (`usd` > 0) ou retire (`usd` None) un plafond, et journalise,
        en UNE transaction (verrou de la ligne). Rend (changé, ancienne
        valeur) ; rien n'est écrit ni journalisé si la valeur est la même."""

    @abc.abstractmethod
    def events(self, limit: int, scope: str | None = None) -> list[dict]:
        """Les derniers changements, récents d'abord : scope, window_s,
        old_usd, new_usd, actor, at_ts."""


# --------------------------------------------------------------------------
# comptes multiples par fournisseur (L30, décision 0027, migration 0028)
# --------------------------------------------------------------------------

class Accounts(Domain):
    """Compte actif par (hôte, harnais), retenues et journal des bascules.

    Les profils (dossiers de configuration, clés) sont des secrets d'hôte : ils
    ne passent jamais par ce domaine, qui ne voit que des NOMS de comptes."""

    @abc.abstractmethod
    def active(self, host: str, harness: str) -> dict | None:
        """La ligne `account_active` (account, forced, updated_at), ou None."""

    @abc.abstractmethod
    def init_active(self, host: str, harness: str, account: str) -> dict:
        """Pose le compte actif s'il n'y en a pas encore ; rend la ligne en place."""

    @abc.abstractmethod
    def switch(self, host: str, harness: str, *, expected: str | None, to: str,
               kind: str, reason: str, agent: str | None, forced: bool = False) -> bool:
        """Change le compte actif ET journalise la bascule, en UNE instruction.

        Comparer-et-changer : `expected` est le compte que l'appelant a vu
        actif (None = n'importe lequel, forçage manuel). Une bascule
        automatique (`forced` faux) ne touche jamais un compte forcé. Faux si
        un autre worker a déjà basculé : rien n'est écrit."""

    @abc.abstractmethod
    def set_auto(self, host: str, harness: str, *, reason: str) -> bool:
        """Lève le forçage manuel (journalisé `auto`) ; faux si rien n'était forcé."""

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
    def switches(self, host: str, harness: str | None, limit: int) -> list[dict]:
        """Les dernières bascules de l'hôte (toutes ou d'un harnais), récentes d'abord."""


# --------------------------------------------------------------------------
# boîte aux lettres (`agent_mailbox`)
# --------------------------------------------------------------------------

class Mailbox(Domain):
    """Messages durables ; le dépôt réveille le destinataire (NOTIFY en Postgres)."""

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
        """Messages non remis, du plus ancien au plus récent."""

    @abc.abstractmethod
    def unread_active(self, recipient: str, limit: int) -> list[dict]:
        """L125 : messages non remis qui ouvriront un tour — ni accusé, ni
        copie, ni diffusion non urgents (`mail.passive_reason`) —, du plus
        ancien au plus récent."""

    @abc.abstractmethod
    def unread_urgent(self, recipient: str, limit: int) -> list[dict]:
        """Messages non remis marqués `payload.urgent`."""

    @abc.abstractmethod
    def get(self, message_id: int) -> dict | None:
        """Un message par son id."""

    @abc.abstractmethod
    def mark_delivered(self, ids: Sequence[int]) -> int:
        """Marque remis ; rend le nombre de messages passés remis."""

    @abc.abstractmethod
    def reserve(self, recipient: str, owner: str | None, epoch: int | None,
                token: str, *, ids: Sequence[int] | None = None, porteur: str,
                ttl_seconds: float, limit: int = 200) -> list[dict]:
        """Réserve atomiquement des non-lus sans réservation active (le
        destinataire est verrouillé dans le registre, le bail (owner, epoch)
        contrôlé s'il est donné) ; rend les messages réservés sous `token`."""

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
        `sender`, `kind`, `work_item_id`, `created_ts`."""


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
    """Clés publiques Ed25519 des agents (colonnes de `agent_registry`)."""

    @abc.abstractmethod
    def info(self, agent: str) -> dict | None:
        """Clé, empreinte, rôle et dates de l'agent, ou None."""

    @abc.abstractmethod
    def register(self, agent: str, public_key: str, fingerprint: str, role: str,
                 note: str | None) -> bool:
        """Enregistre la clé publique (base64) ; faux si l'agent est inconnu."""

    @abc.abstractmethod
    def revoke(self, agent: str) -> bool:
        """Révoque la clé active ; faux s'il n'y en avait pas."""

    @abc.abstractmethod
    def registered(self) -> list[dict]:
        """Agents qui ont une clé publique (révoquée comprise), par nom :
        name, public_key_fingerprint, key_role, key_updated_ts, key_revoked_ts."""


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

class WorkItems(Domain):
    """Lots et journal de leurs transitions (ledger de reprise)."""

    @abc.abstractmethod
    def add(self, *, type: str, source: str, app: str, title: str, body: str,  # noqa: A002
            issue_ref: str | None, workstream: str | None, assignee: str | None,
            budget_usd: float | None, note: str, actor: str,
            package_id: str | None = None, package_parent: str | None = None,
            branch: str | None = None, branch_target: str | None = None) -> dict:
        """Crée le lot en `intake` (rattaché à une fiche WorkPackage si
        `package_id`, L29 ; avec sa branche si `branch`, L118) et sa première
        ligne de journal ; rend le lot."""

    @abc.abstractmethod
    def set_branch(self, item_id: int, branch: str | None, target: str | None, *,
                   note: str, actor: str) -> dict | None:
        """L118 : pose la branche et la cible d'un lot ouvert (journalisé) ;
        None si le lot n'est plus ouvert."""

    @abc.abstractmethod
    def set_branch_head(self, item_id: int, branch: str, head: str | None) -> bool:
        """L118 : retient le dernier commit de la branche vu en avance sur sa
        cible, si la branche du lot est toujours `branch`."""

    @abc.abstractmethod
    def open_for_sweep(self, limit: int) -> list[dict]:
        """L118 : les lots ouverts que le relevé des fusions examine (avec une
        branche ou un assigné), du plus ancien au plus récent."""

    @abc.abstractmethod
    def open_for(self, assignee: str) -> list[dict]:
        """L118 : les lots ouverts d'un assigné."""

    @abc.abstractmethod
    def open_by_ref(self, ref: str) -> list[dict]:
        """L118 : les lots ouverts désignés par une référence (issue, fiche,
        branche, premier mot du titre)."""

    @abc.abstractmethod
    def get(self, item_id: int) -> dict | None:
        """Un lot, ou None."""

    @abc.abstractmethod
    def items(self, *, state: str | None, assignee: str | None, limit: int) -> list[dict]:
        """Lots, du plus récemment modifié au plus ancien."""

    @abc.abstractmethod
    def move(self, item_id: int, state: str, *, current: str, loops: int, note: str,
             actor: str) -> dict | None:
        """Passe le lot de `current` à `state` (boucle QA : `loops` ajouté) et
        journalise ; None (rien d'écrit) s'il n'est plus en `current`."""

    @abc.abstractmethod
    def note(self, item_id: int, state: str, text: str, actor: str) -> None:
        """Journalise une note et rafraîchit `updated_at`."""

    @abc.abstractmethod
    def assign(self, item_id: int, assignee: str, *, current: str | None, note: str,
               actor: str) -> dict | None:
        """Réassigne le lot (L37) s'il a encore l'assigné `current` et n'est ni
        fusionné ni fermé, et journalise, en UNE transaction ; None sinon.
        L40 : efface la délégation en cours (issue `annulee`)."""

    # -- délégation à échéance (L40, décision 0030 point 5) -------------------
    @abc.abstractmethod
    def delegate(self, item_id: int, delegate: str, *, current: str | None,
                 delegated_by: str, within_s: float, note: str, actor: str) -> dict | None:
        """Délègue le lot (assigné encore `current`, lot ouvert) en UNE
        transaction : délégation en cours remplacée, lot au délégué avec
        délégant, début et échéance (`now + within_s`), ligne au registre des
        délégations, journal. Rend `{item, delegation, replaced}` ; None si
        le lot a bougé."""

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
    def events(self, item_id: int, limit: int) -> list[dict]:
        """Journal du lot, du plus récent au plus ancien."""

    @abc.abstractmethod
    def milestones(self, item_id: int, limit: int) -> list[dict]:
        """Les jalons d'un lot (0013), du plus récent au plus ancien."""

    @abc.abstractmethod
    def add_milestone(self, item_id: int, kind: str, *, sha: str, actor: str,
                      verdict: str | None, note: str) -> dict:
        """Écrit un jalon déclaré (`frozen`, `verdict`) et le rend."""

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
    def close_merged(self, item_id: int, *, current: str, sha: str, actor: str, note: str,
                     pr_ref: str | None, frozen_id: int | None = None) -> dict | None:
        """Fusion constatée (L29) : `current` → `merged` si le lot y est encore
        et, si `frozen_id` est donné, si ce jalon `frozen` est toujours le
        dernier gel du lot (sinon None, rien d'écrit) ; journal et jalon
        `merged` (commit, auteur de la fusion) dans la même transaction."""

    @abc.abstractmethod
    def close(self, item_id: int, *, current: str, reason: str, superseded_by: int | None,
              actor: str, note: str) -> dict | None:
        """Fermeture explicite (L29) : `current` → `closed` (`abandoned` |
        `superseded`) si le lot y est encore, journal et jalon `closed` dans la
        même transaction ; None sinon."""

    @abc.abstractmethod
    def refresh_package_parents(self) -> int:
        """Recopie dans les lots le parent courant de leur fiche ; rend le nombre
        de lots changés."""

    # -- file d'amélioration (L119, décision 0037) ------------------------------
    @abc.abstractmethod
    def backlog_add(self, *, title: str, body: str, source: str, expected_value: str,
                    value_score: int, priority: int, team: str | None,
                    required_capabilities: list | None, package_id: str | None,
                    package_parent: str | None, note: str, actor: str) -> dict:
        """Crée un élément de la file : un lot `improvement` en `intake`, sans
        assigné, et sa première ligne de journal ; rend le lot."""

    @abc.abstractmethod
    def backlog(self, *, open_only: bool, limit: int) -> list[dict]:
        """Les éléments de la file dans l'ordre de prise (priorité, puis valeur
        décroissante, puis ancienneté). `open_only` : seulement ceux qu'on peut
        encore prendre (`intake`, sans assigné) ; sinon tous les éléments non
        terminés."""

    @abc.abstractmethod
    def auto_takes_since(self, seconds: float, note_prefix: str) -> int:
        """Nombre de prises automatiques (lignes de journal dont la note
        commence par `note_prefix`) sur les `seconds` dernières secondes, tous
        hôtes confondus."""

    # -- issues GitHub des lots (L126) ------------------------------------------
    @abc.abstractmethod
    def issue_feed(self, limit: int) -> list[dict]:
        """Les lots que la projection en issues examine : ouverts, ou qui
        portent une `issue_ref` (les `limit` plus récents), par id croissant.
        Chaque ligne ajoute l'équipe de la fiche du plan (`package_team`) et
        l'équipe, le chantier et l'hôte de l'assigné au registre
        (`assignee_team`, `assignee_chantier`, `assignee_host`). Une
        instruction."""

    @abc.abstractmethod
    def set_issue_ref(self, item_id: int, issue_ref: str | None, *,
                      current: str | None) -> bool:
        """Pose `issue_ref` si le lot porte encore `current` (NULL et chaîne
        vide confondus) : faux si la valeur a changé entre-temps (un autre
        projecteur l'a posée). Ni journal ni `updated_at` : une projection
        n'est pas une activité du lot."""


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
                     canon: str = "", canon_id: str | None = None) -> None:
        """Écrit l'état du canon de l'hôte ; le dernier commit valide ne suit que
        `good`. L42 (0031) : clé (host, canon), '' = canon par défaut ;
        `canon_id` : identifiant réel du canon (gardé s'il n'est pas fourni)."""

    @abc.abstractmethod
    def state(self, host: str, canon: str = "") -> dict | None:
        """Dernier état enregistré du canon `canon` ('' : par défaut) de l'hôte, ou None."""

    @abc.abstractmethod
    def states(self, host: str) -> list[dict]:
        """Tous les états enregistrés pour l'hôte, un par canon (L42)."""

    @abc.abstractmethod
    def key_for_root(self, host: str, root: str) -> str | None:
        """Clé (non vide) d'un canon non par défaut déjà enregistré pour l'hôte
        avec cette racine, ou None (L42 : identité d'un canon illisible)."""

    @abc.abstractmethod
    def record_auth_state(self, host: str, status: str, diagnostic: str,
                          canon: str = "") -> dict | None:
        """Inscrit l'issue de la synchronisation des authentificateurs ; rend
        l'issue précédente (`auth_status`, `auth_diagnostic`), ou None."""

    @abc.abstractmethod
    def close_others(self, host: str, keep: Sequence[str], diagnostic: str) -> list[str]:
        """Passe `invalid` les états `ok` de l'hôte dont la clé n'est pas dans
        `keep` (canons retirés de la configuration, L42) ; rend leurs clés."""

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
        canon par défaut `new_default` repassent à NULL."""

    @abc.abstractmethod
    def host_rows(self, host: str, names: Sequence[str]) -> list[dict]:
        """Agents de l'hôte (et ceux nommés) avec colonnes déclaratives, statut,
        `profile_fresh` et `in_turn`."""

    @abc.abstractmethod
    def write_declared(self, name: str, values: dict) -> None:
        """Écrit les colonnes déclaratives (crée l'agent au besoin) et le profil
        évalué des valeurs écrites, dans la même instruction."""

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
    def stop_removed(self, name: str, host: str, *, pending_text: str,
                     stop_text: str, owner: tuple[str, bool] | None = None) -> dict | None:
        """Arrête un agent retiré du canon (sauf en plein tour : arrêt demandé) ;
        rend name et status, ou None s'il n'y avait rien à faire. `owner` (L42) :
        (id, défaut) du canon auquel la ligne doit appartenir — jamais celle
        d'un autre canon."""


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
            raise RegistryLockError("RegistryLock : rendu par receipts.lock_registry seulement")
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

class Threads(Domain):
    """Index des fils (le fil lui-même vit chez son transport, hors base)."""

    @abc.abstractmethod
    def index(self, *, project: str, lot: str, transport: str, host: str, location: str,
              entry_id: str, mailbox_ids: Sequence[int], author: str, excerpt: str,
              ts: float, trace: dict) -> None:
        """Compte l'entrée dans l'index du fil et, si elle porte des messages
        de la boîte, y note `trace` (id externe) — en une seule écriture."""

    @abc.abstractmethod
    def indexed(self) -> list[dict]:
        """Fils indexés, du plus récemment écrit au plus ancien."""


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
        close_reason, updated_ts) : la progression d'un epic ne dépend pas de
        la fenêtre."""

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
        """État d'exécution de chaque agent (de `chantier` ou `team` =
        `project` si donné) : statut, bail vivant (`lease_live`), consigne en
        cours, marqueur du tour (`turn_started_ts`, `turn_label`), non-lus."""

    @abc.abstractmethod
    def costs(self, *, since_ts: float, agents: Sequence[str] | None) -> list[dict]:
        """Grand livre agrégé par (agent, harnais, modèle) sur la fenêtre la
        plus large de `since_ts` et des 24 dernières heures (horloge de la
        base) : `usd_window`, `usd_1h`, `usd_24h`, tours et jetons de la
        fenêtre. `agents` restreint aux agents nommés (None = tous)."""


# --------------------------------------------------------------------------
# vue par projet (lot L62) : qui travaille sur quoi
# --------------------------------------------------------------------------

class Projects(Domain):
    """Lecture de la vue par projet (`ameesh projects`, L62). Aucune écriture.

    UNE requête, UN aller-retour : la vue se rafraîchit souvent et sert aussi
    d'en-tête à `ameesh progress`."""

    @abc.abstractmethod
    def board(self, *, max_lots: int) -> dict:
        """`{"agents": [...], "lots": [...], "dead_letters": [...]}`.

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
        `mailbox.dead_letters` (une ligne par destinataire et expéditeur)."""


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
                  actor: str) -> dict | None:
        """Pose les dates prévues d'une tâche (`dates` : clés parmi `start`,
        `end`, `delivery` ; valeur None = date effacée ; clé absente =
        inchangée), avec qui et quand (`planned_by`, `planned_at`). Ce n'est
        PAS une activité de la tâche : ni `updated_at` ni le journal ne
        bougent (une replanification ne masque pas une stagnation). Rend
        `{id, planned_start, planned_end, planned_delivery, planned_source,
        planned_by, planned_ts}`, ou None si la tâche est inconnue."""

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
              done_states: Sequence[str]) -> list[dict]:
        """Les tâches de la feuille de route : ouvertes (état hors de
        `done_states`, fourni par l'appelant), ou datées, ou portant un
        engagement en cours, ou modifiées depuis `since_ts` — colonnes de `work_items` utiles à la
        frise (id, title, state, app, workstream, assignee, package_id,
        created_ts, updated_ts, closed_ts) et dates prévues ; `total` avant
        la borne."""

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
                       values: dict) -> dict | None:
        """Change un engagement (`status`, `due_on`, `note`…) s'il est dans
        l'un des statuts `current` ; None sinon (inconnu ou déjà changé).
        Un statut `done` ou `cancelled` pose `closed_at`."""


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
                "turn_max_seconds", "turn_mail_max", "turn_grace_seconds")
    #: réglages entiers (colonne `bigint`) : la valeur texte est convertie
    INTEGER_SETTINGS = ("context_max_tokens", "turn_max_seconds", "turn_mail_max",
                        "turn_grace_seconds")

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
    def listing(self) -> list[dict]:
        """Une ligne par agent : réglages, dossier de travail (`cwd`), statut et
        `status_since_ts`, bail
        (`lease_live`, `lease_expires_ts`), tour en cours (`turn_started_ts`,
        `turn_label` du marqueur comptable), non-lus qui ouvriront un tour
        (`unread`, `oldest_unread_ts` ; L125 : hors courrier passif, compté à
        part dans `passive_unread`), lot de session (`session_work_item`,
        `session_lot_title`, `session_lot_state`), lot assigné ouvert le plus
        récent (`assigned_lot_id`, `assigned_lot_title`, `assigned_lot_state`),
        dernier tour du grand livre (`last_turn_reread_tokens` = entrée +
        entrée en cache, `last_turn_session`, `last_turn_recorded_ts`) et
        demande de redémarrage (`restart_requested_ts`)."""

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
    def open_lots_activity(self, limit: int) -> list[dict]:
        """Lots ouverts avec leur DERNIÈRE activité (`last_activity_ts`) :
        maximum de la ligne du lot, de ses transitions, jalons, actions et
        messages liés (`agent_mailbox.work_item_id`) ; plus id, title, state,
        assignee. Les plus anciennement actifs d'abord."""

    @abc.abstractmethod
    def turns(self, *, agent: str | None, since_s: float, limit: int) -> list[dict]:
        """Lignes du grand livre `turn_costs` des `since_s` dernières
        secondes (de l'agent si donné), les plus récentes d'abord : id,
        agent, harness, turn, model, session, usd, input_tokens,
        cached_input_tokens, output_tokens, recorded_ts."""

    @abc.abstractmethod
    def record_gauges(self, readings: Sequence[dict]) -> int:
        """Ajoute les relevés de jauge (harness, key, used, resets_at,
        window_s, et `account` facultatif — L30, 0028) qui CHANGENT le dernier
        relevé de leur jauge (du même compte), ou dont le dernier relevé a plus
        de dix minutes. Rend le nombre de lignes."""

    @abc.abstractmethod
    def gauge_history(self, *, since_s: float, harness: str | None,
                      account: str | None = None) -> list[dict]:
        """Relevés des `since_s` dernières secondes, dans l'ordre
        chronologique : harness, key, used, resets_at_ts, window_s,
        observed_ts, account (L30 ; None sans comptes). `account` filtre."""

    @abc.abstractmethod
    def latest_gauges(self, *, since_s: float) -> list[dict]:
        """L94 : le DERNIER relevé de chaque jauge (harness, account, key)
        observé dans les `since_s` dernières secondes : harness, key, used,
        resets_at_ts, window_s, observed_ts, account (None sans comptes)."""

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

class HostResources(Domain):
    """Relevés des ressources d'un hôte (état d'exécution, jamais canon).

    Un relevé porte : hôte, instant, mémoire disponible, swap utilisé, charge
    1 minute, nombre de CPU, disque libre (et le dossier mesuré), nombre de
    tours en cours. Les mesures illisibles sont NULL : on ne devine pas.
    Instants rendus en secondes epoch (`sampled_ts`).
    """

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
    def turns_in_progress(self, host: str) -> int:
        """Nombre d'agents de l'hôte en tour (statut `running`, bail vivant)."""

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
    def get(self, turn_id: str) -> dict | None:
        """La ligne d'un tour (L73 : un conteneur étiqueté par ce tour), ou None."""

    @abc.abstractmethod
    def open_by_agent(self, agent: str) -> list[dict]:
        """Les lignes encore `running` d'un agent, la plus récente d'abord."""

    @abc.abstractmethod
    def orphans(self, host: str | None = None, limit: int = 50) -> list[dict]:
        """Les lignes `orphan` (de l'hôte si donné), les plus récentes d'abord."""

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
    def recent(self, host: str | None, since_s: float, limit: int = 50) -> list[dict]:
        """Les lignes des `since_s` dernières secondes (de l'hôte si donné),
        les plus récentes d'abord ; instant en `at_ts`."""

    @abc.abstractmethod
    def summary(self, host: str | None, since_s: float) -> list[dict]:
        """Par (hôte, kind, action) : nombre de lignes et octets, sur les
        `since_s` dernières secondes (les bilans `mesure` exclus)."""

    @abc.abstractmethod
    def last_measure(self, host: str | None) -> list[dict]:
        """Le dernier bilan (`kind = mesure`) de chaque hôte (ou de `host`)."""

    @abc.abstractmethod
    def register_worktree(self, *, host: str, path: str, repo: str, agent: str,
                          lot: str | None, turn_id: str | None, branch: str,
                          head: str) -> dict | None:
        """Enregistre un worktree apparu pendant un tour ; None s'il est déjà
        suivi (vivant ou gardé) sur cet hôte."""

    @abc.abstractmethod
    def worktrees(self, host: str | None, statuses: Sequence[str] | None = None,
                  limit: int = 500) -> list[dict]:
        """Worktrees suivis (de l'hôte, des statuts donnés), les plus anciens
        d'abord ; instants en `created_ts`, `checked_ts`, `ended_ts`."""

    @abc.abstractmethod
    def set_worktree_status(self, worktree_id: int, status: str, detail: str) -> dict | None:
        """Change le statut d'un worktree suivi (`ended_at` posé pour
        `removed` et `gone`) ; rend la ligne, ou None."""


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
# le stockage d'une connexion
# --------------------------------------------------------------------------

class Storage(abc.ABC):
    """Les domaines du stockage, liés à une connexion ou une transaction."""

    catalog: Catalog

    #: nom du pilote de stockage (`postgres`)
    driver: str
    #: la connexion (ou la transaction) sous-jacente
    db: Any

    agents: Agents
    leases: Leases
    pending_spend: PendingSpend
    turn_costs: TurnCosts
    accounts: Accounts
    budgets: Budgets
    mailbox: Mailbox
    wakeups: Wakeups
    keys: Keys
    approvals: Approvals
    nonces: Nonces
    work: WorkItems
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
    roadmap: Roadmap
    operations: Operations
    hosts: HostResources
    turn_resources: TurnResources
    visibility: Visibility
    housekeeping: Housekeeping
    session_bindings: SessionBindings
