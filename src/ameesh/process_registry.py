# SPDX-License-Identifier: AGPL-3.0-only
"""Registre natif des workflows Process : révision/étape et reçus idempotents.

Tranche B de #1915 (lot #1922). Contrat consommateur : manaty/ameesh#1379
(corps et commentaire 6207021753). Ce module est la **seule** écriture native
d'une transition de workflow ; il ne compile pas le document Process (c'est la
tranche A, `process_workflow`, dont l'artefact `CompiledWorkflow` satisfait le
protocole `WorkflowSpec` ci-dessous) et il n'expose aucune route publique.

Ce que le registre garantit, dans UNE transaction :

1. **Le lot est verrouillé** (`FOR UPDATE`) avant tout contrôle : deux
   commandes du même lot ne se doublent pas.
2. **L'autorité est vérifiée dans la transaction** : l'acteur existe au
   registre, n'est pas arrêté ; pour une source `runner`, le bail est vivant,
   détenu par ce runner à cet epoch (ligne du registre verrouillée `FOR SHARE`,
   donc sérialisée avec une reprise de bail). Un acteur révoqué, une source
   inconnue ou une époque périmée sont refusés **avant** toute lecture de
   reçu : ils ne récupèrent jamais un résultat autorisé par le passé.
3. **La révision est pinnée** : `workflow_ref`/`workflow_version` du lot
   doivent être ceux de l'artefact compilé ; aucune adoption automatique d'une
   nouvelle révision. Seul `transfer_revision`, explicite et journalisé,
   change la révision admise.
4. **La version optimiste** est l'id de la dernière ligne du journal du lot
   (`work_item_events`, 0 si vide) : toute écriture concurrente du journal —
   commande du registre ou transition historique `work.move` — fait échouer
   une commande qui attendait l'ancienne version (`version-conflict`).
5. **La clé de commande est unique par lot** (index partiel) : un rejeu de la
   même intention retrouve le reçu accepté ; la même clé avec une charge
   canonique différente est un `intent-conflict`.
6. **Aucun booléen client ne remplace une garde** : une avance exige un
   vérificateur **installé côté serveur** pour la garde de l'arête, et
   l'entrée comme la sortie de retenue exigent celui installé sous le nom
   réservé `hold` (tranche C : `HOLD_VERIFIER`, l'appelable `verify_hold` de
   `process_verifiers`) ; sans installation, `verifier-uninstalled`. Aucune
   voie de contournement : une retenue ne s'efface pas par une avance. Le
   client fournit des faits structurés (`evidence`), jamais une décision.
7. **Rien à moitié** : l'écriture du lot, l'étape/version et le reçu sont dans
   la même transaction ; une erreur après le début ne laisse ni reçu ni
   transition (test au trigger, `rollback sans demi-événement`).
8. **Les retenues survivent** : `blocked`/`waiting_human` gardent l'étape ;
   une retenue ne se traverse pas — toute avance depuis un lot retenu est
   refusée (`held`), seule une reprise explicite `release` en sort, vérifiée
   côté serveur comme l'entrée ; `waiting_human` exige une demande de décision
   native en attente (L233, la même requête que `work.move`) et sa reprise
   attend que la décision soit répondue (`decision-pending`) ; un cœur terminal
   ne se rouvre jamais, y compris par adoption de workflow.
   Les lots **sans workflow** ne passent pas par ici : `workflow-absent`,
   leur comportement (`work.move`) est inchangé.
9. **Les cœurs libérés portent leurs effets de bord** : une avance vers
   `merged`, `promoted` ou `closed` rend la réservation de migration (#956),
   écrit sa ligne au journal, et réveille les dépendants (L385) — le premier
   passage seulement — sous le verrou de graphe pris avant le lot, comme
   `work.close`/`close_merged`. Le cœur `closed` exige la fermeture dans
   l'évidence (`close_reason` parmi `abandoned`, `superseded` — avec
   `superseded_by` —, `delivered` — avec la tête `sha`), pose
   `close_reason`/`superseded_by` et les jalons `closed`/`delivered` : ce que
   `work.close` refuse désormais à un lot pinné (N8) est fait ici, dans la
   même transaction.

Protocole `WorkflowSpec` (structurel, l'artefact de la tranche A le satisfait) :

    workflow_ref: str            référence canonique (commit/chemin) pinnée
    workflow_version: int        version de la révision Process (>= 0)
    initial: str                 statut de départ, déclaré
    statuses() -> Sequence[str]
    core_of(status) -> str       cœur natif (`intake`…`closed`)
    transitions_from(status) -> Sequence[objet avec .target et .guard]

Artifact trust boundary (N5): `spec` is supplied by the caller — the server
component that compiled the pinned revision. The registry checks that the
artifact `workflow_ref`/`workflow_version` match the command and the lot
(`spec-mismatch`), never its content: the ref names the commit and tranche A
publishes no digest, so the compiled artifact is trusted for that ref. The
caller is the installed server component, never a client; if a later revision
of the artifact publishes a digest, the seam must bind it to the command.

Identité et époque : `Authority(actor, source, epoch, runner_id)` reprend
l'identité liée de `identity.Binding` (`runner` avec runner_id + epoch de bail ;
`explicit`/`session` avec epoch 0). L'`Authority` est construite par le
**composant serveur installé** (résolution d'identité), jamais par un client :
le registre revérifie nativement la ligne du registre des agents (existence,
arrêt, bail vivant détenu par ce runner à cet epoch) et refuse avant de rendre
un reçu.

Deux versions distinctes, jamais confondues :

* `workflow_version` (lot et reçu) : la **révision Process** pinnée, changée
  seulement par `transfer_revision` (explicite, journalisé, sans régression) ;
* `expected_version` / `Receipt.version` : la **version optimiste du journal**
  du lot — l'id de sa dernière ligne `work_item_events` (0 si vide), toutes
  écritures confondues (commandes du registre comme transitions historiques
  `work.move`).

Un rejeu (même clé, même intention canonique) rend le reçu accepté même après
un transfert de révision : c'est le résultat acquis de cette clé. La même clé
avec une autre intention, elle, est un `intent-conflict`.

Preuve persistée (`work_item_events.evidence`, schéma `process-receipt/1`) ::

    {"schema": "process-receipt/1",
     "intent": {"sha256": "…"},                  # charge canonique JCS
     "command": {"action", "guard", "from", "to", "hold",
                 "expected_version", "workflow_ref", "workflow_version"},
     "authority": {"actor", "source", "epoch", "runner_id"},
     "verification": {"required", "verifier", "ok", "reason", "facts"},
     "facts": {…}}                               # faits structurés du client

Codes de refus stables (`RegistryRefusal.code`) : `command-invalid`,
`evidence-invalid`, `authority-invalid`, `authority-source`,
`authority-runner`, `authority-epoch`, `authority-unknown-actor`,
`authority-revoked`, `authority-lease`, `authority-expired`, `spec-invalid`,
`spec-mismatch`, `lot-unknown`, `workflow-absent`, `ref-mismatch`,
`revision-mismatch`, `version-conflict`, `intent-conflict`, `receipt-corrupt`,
`stage-mismatch`, `stage-undeclared`, `edge-undeclared`, `terminal-reopened`,
`held`, `hold-invalid`, `hold-redundant`, `hold-no-decision`, `decision-pending`,
`hold-core-unproven`, `not-held`, `close-reason`, `superseded-unknown`,
`delivered-sha`,
`repair-exhausted`, `revision-regression`, `verifier-uninstalled`,
`verifier-invalid`, `verifier-error`, `verifier-refused`, `verifier-name`.

Les vérificateurs C n'ont pas de SQL : `read_sources(context)` rend, dans la
transaction et sous verrou, les collections réellement collectées
(`milestones`, `receipts`, `decisions`, `deliveries`), jamais une collection
absente (fail-closed).

Le SQL du registre est ici, pas dans le stockage : c'est un composant serveur
installé, pas une vue.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from . import jcs
from .db import Db
from .work import MAX_QA_LOOPS

#: Schéma de la preuve structurée d'un reçu (contrainte SQL de 0452).
RECEIPT_SCHEMA = "process-receipt/1"
#: Domaine de l'empreinte d'intention (séparation de domaines).
INTENT_DOMAIN = b"ameesh-process-command/1\x00"
#: Nom réservé du vérificateur de transfert de révision (pas une garde Process).
TRANSFER_VERIFIER = "revision_transfer"
#: Nom réservé du vérificateur d'entrée ET de sortie de retenue (pas une garde
#: Process) : `hold` comme `release` passent par lui, sans voie de contournement.
#: C'est le nom publié par la tranche C (`process_verifiers.HOLD_VERIFIER`) ;
#: son appelable `verify_hold` dispatche sur l'`action` de la commande.
HOLD_VERIFIER = "hold"

ACTIONS = ("advance", "hold", "release", "transfer")
HOLDS = ("blocked", "waiting_human")
#: Cœurs natifs qu'une étape peut projeter (mêmes que la tranche A).
CORE_STATES = ("intake", "build", "qa", "merged", "promoted", "closed")
#: Cœurs terminaux : jamais rouverts par une commande.
TERMINAL_CORES = frozenset(("promoted", "closed"))
#: Cœurs qui libèrent le lot pour ses dépendants et rendent sa réservation de
#: migration (#956) : les mêmes que `work_dependencies._DONE`. L'avance qui y
#: entre porte les effets de bord de la fermeture historique, que le stockage
#: refuse désormais aux lots pinnés (#1922/N8) : `close_reason`/
#: `superseded_by`, jalons `closed`/`delivered`, réservation rendue, réveil des
#: dépendants (L385).
RELEASED_CORES = frozenset(("merged", "promoted", "closed"))
#: Fermetures admises pour un cœur `closed` (contrainte `close_reason` de la
#: migration 0430) : l'évidence de la commande dit laquelle, comme les trois
#: portes de `work.close` (abandon, remplacement, livraison hors cible).
CLOSE_REASONS = ("abandoned", "superseded", "delivered")
#: Sources d'identité admises (`identity.Binding.source`).
SOURCES = ("runner", "explicit", "session")
#: Reprises qa → build comptées par `work_items.loops` (même borne que work).
REPAIR_CORES = ("qa", "build")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
#: Tête relue d'une livraison hors cible (#988) : la même forme que
#: `work._SHA_RE` (7 à 64 hexadécimaux), pas l'empreinte d'intention (64).
_HEAD_RE = re.compile(r"^[0-9a-f]{7,64}$")
_VERIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class RegistryRefusal(RuntimeError):
    """Refus du registre : un code stable, un message, un détail optionnel.

    Un refus n'écrit rien : il est levé dans la transaction, qui est annulée.
    """

    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.detail = detail


# --------------------------------------------------------------------------
# structures publiées
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Authority:
    """Identité liée qui commande : acteur, source, époque de bail."""

    actor: str
    source: str = "runner"
    epoch: int = 0
    runner_id: str = ""


@dataclass(frozen=True)
class Command:
    """Une commande native : une intention, une clé, une version attendue.

    `evidence` porte les faits **structurés** du client (jamais une décision :
    le vérificateur installé décide). `from_stage`/`to_stage`/`guard` sont les
    entrées explicites d'une avance ; `hold` celle d'une retenue.
    """

    work_item_id: int
    command_id: str
    expected_version: int
    authority: Authority
    workflow_ref: str = ""
    workflow_version: int = 0
    action: str = "advance"
    from_stage: str = ""
    to_stage: str = ""
    guard: str = ""
    hold: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Verification:
    """Résultat d'un vérificateur installé : pas un booléen client."""

    ok: bool
    reason: str = ""
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationContext:
    """Ce qu'un vérificateur installé voit, dans la transaction et sous verrou."""

    db: Any
    work_item: Mapping[str, Any]
    command: Command
    spec: Any
    from_stage: str
    to_stage: str


@dataclass(frozen=True)
class Receipt:
    """Le reçu d'une commande acceptée (ou rejouée)."""

    accepted: bool
    replayed: bool
    work_item_id: int
    command_id: str
    action: str
    event_id: int
    version: int
    state: str
    workflow_ref: str
    workflow_stage: str | None
    workflow_version: int
    guard: str
    intent_sha256: str

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "replayed": self.replayed,
            "work_item_id": self.work_item_id,
            "command_id": self.command_id,
            "action": self.action,
            "event_id": self.event_id,
            "version": self.version,
            "state": self.state,
            "workflow_ref": self.workflow_ref,
            "workflow_stage": self.workflow_stage,
            "workflow_version": self.workflow_version,
            "guard": self.guard,
            "intent_sha256": self.intent_sha256,
        }


@dataclass(frozen=True)
class RegistryState:
    """Lecture du registre d'un lot : état, révision, version du journal."""

    work_item_id: int
    state: str
    workflow_ref: str | None
    workflow_stage: str | None
    workflow_version: int
    version: int
    receipts: int


Verifier = Callable[[VerificationContext], Verification]

#: Vérificateurs installés côté serveur (tranche C) : garde Process → appelable.
#: Vide par défaut : aucune avance n'est possible sans installation explicite.
INSTALLED_VERIFIERS: dict[str, Verifier] = {}


def install_verifier(name: str, verifier: Verifier, *,
                     replace: bool = False) -> None:
    """Installe un vérificateur serveur pour une garde (tranche C).

    Refuse un nom hors forme, un appelable non appelable, et l'écrasement
    silencieux d'une installation par un autre objet (`replace=True` pour un
    remplacement explicite). Idempotent pour le même objet.
    """
    if not isinstance(name, str) or not _VERIFIER_RE.match(name):
        raise RegistryRefusal("verifier-name", "nom de vérificateur invalide : %r" % (name,))
    if not callable(verifier):
        raise RegistryRefusal("verifier-invalid", "%s n'est pas appelable" % name)
    current = INSTALLED_VERIFIERS.get(name)
    if current is not None and current is not verifier and not replace:
        raise RegistryRefusal("verifier-name",
                              "un vérificateur est déjà installé pour %s" % name)
    INSTALLED_VERIFIERS[name] = verifier


def installed_verifiers() -> dict[str, Verifier]:
    """Copie du registre installé (lecture seule pour l'appelant)."""
    return dict(INSTALLED_VERIFIERS)


# --------------------------------------------------------------------------
# validation hors transaction : forme, intention canonique
# --------------------------------------------------------------------------

def canonical_command_id(value: Any) -> str:
    """UUID canonique minuscule, ou `command-invalid`."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise RegistryRefusal("command-invalid", "command_id n'est pas un UUID : %r" % (value,))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_authority(authority: Any) -> None:
    if not isinstance(authority, Authority):
        raise RegistryRefusal("authority-invalid", "Authority attendue")
    if not isinstance(authority.actor, str) or not _NAME_RE.match(authority.actor):
        raise RegistryRefusal("authority-invalid", "acteur invalide : %r" % (authority.actor,))
    if authority.source not in SOURCES:
        raise RegistryRefusal("authority-source",
                              "source %r hors de %s" % (authority.source, ", ".join(SOURCES)))
    if not _is_int(authority.epoch) or authority.epoch < 0:
        raise RegistryRefusal("authority-epoch", "époque invalide : %r" % (authority.epoch,))
    if authority.source == "runner":
        if not isinstance(authority.runner_id, str) or not authority.runner_id:
            raise RegistryRefusal("authority-runner", "source runner sans runner_id")
    else:
        if authority.epoch != 0:
            raise RegistryRefusal("authority-epoch",
                                  "source %s sans bail : époque attendue 0, reçue %s"
                                  % (authority.source, authority.epoch))
        if authority.runner_id:
            raise RegistryRefusal("authority-runner",
                                  "source %s avec un runner_id" % authority.source)


def _validate_command(command: Any) -> None:
    if not isinstance(command, Command):
        raise RegistryRefusal("command-invalid", "Command attendue")
    if not _is_int(command.work_item_id) or command.work_item_id <= 0:
        raise RegistryRefusal("command-invalid", "work_item_id invalide : %r" % (command.work_item_id,))
    canonical_command_id(command.command_id)
    if not _is_int(command.expected_version) or command.expected_version < 0:
        raise RegistryRefusal("command-invalid",
                              "expected_version invalide : %r" % (command.expected_version,))
    _validate_authority(command.authority)
    if command.action not in ACTIONS:
        raise RegistryRefusal("command-invalid",
                              "action %r hors de %s" % (command.action, ", ".join(ACTIONS)))
    if not isinstance(command.workflow_ref, str) or not command.workflow_ref.strip():
        raise RegistryRefusal("command-invalid", "workflow_ref requise")
    if not _is_int(command.workflow_version) or command.workflow_version < 0:
        raise RegistryRefusal("command-invalid",
                              "workflow_version invalide : %r" % (command.workflow_version,))
    if command.evidence is None:
        raise RegistryRefusal("evidence-invalid", "evidence absente")
    if not isinstance(command.evidence, Mapping):
        raise RegistryRefusal("evidence-invalid", "evidence doit être un objet JSON")
    if command.action == "advance":
        for field_name in ("from_stage", "to_stage", "guard"):
            value = getattr(command, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RegistryRefusal("command-invalid", "%s requis pour une avance" % field_name)
    elif command.action == "hold":
        if command.hold not in HOLDS:
            raise RegistryRefusal("hold-invalid",
                                  "retenue %r hors de %s" % (command.hold, ", ".join(HOLDS)))
    elif command.action == "release":
        if command.hold:
            raise RegistryRefusal("hold-invalid", "une reprise ne porte pas de retenue")


def command_intent(command: Command) -> dict:
    """Charge canonique de l'intention : les entrées explicites, rien d'autre.

    Les valeurs dérivées (étape courante, état résultant) n'y sont pas : un
    rejeu de la même intention les recalcule et doit retrouver le même reçu.
    """
    intent: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "action": command.action,
        "work_item_id": int(command.work_item_id),
        "command_id": canonical_command_id(command.command_id),
        "expected_version": int(command.expected_version),
        "workflow_ref": command.workflow_ref,
        "workflow_version": int(command.workflow_version),
        "authority": {
            "actor": command.authority.actor,
            "source": command.authority.source,
            "epoch": int(command.authority.epoch),
            "runner_id": command.authority.runner_id,
        },
        "facts": dict(command.evidence),
    }
    if command.action == "advance":
        intent["from"] = command.from_stage
        intent["to"] = command.to_stage
        intent["guard"] = command.guard
    elif command.action == "hold":
        intent["hold"] = command.hold
    return intent


def intent_sha256(command: Command) -> str:
    """SHA-256 JCS de l'intention canonique d'une commande.

    Refuse (`evidence-invalid`) une charge que JCS ne sérialise pas : la même
    intention doit produire exactement les mêmes octets pour tous.
    """
    try:
        canon = jcs.dumps(command_intent(command)).encode("utf-8")
    except jcs.JcsError as exc:
        raise RegistryRefusal("evidence-invalid", "charge non canonique : %s" % exc) from exc
    return hashlib.sha256(INTENT_DOMAIN + canon).hexdigest()


# --------------------------------------------------------------------------
# protocole d'artefact compilé (tranche A) — structurel, sans import
# --------------------------------------------------------------------------

def _spec_ref(spec: Any) -> str:
    return str(getattr(spec, "workflow_ref", "") or "")


def _spec_version(spec: Any) -> int:
    value = getattr(spec, "workflow_version", None)
    if not _is_int(value) or value < 0:
        raise RegistryRefusal("spec-invalid", "workflow_version de l'artefact invalide "
                              "(reçu %r)" % (value,))
    return value


def _validate_spec(spec: Any) -> None:
    if spec is None:
        raise RegistryRefusal("spec-invalid", "artefact compilé requis")
    if not _spec_ref(spec):
        raise RegistryRefusal("spec-invalid", "workflow_ref de l'artefact absente")
    _spec_version(spec)
    if not _spec_statuses(spec):
        raise RegistryRefusal("spec-invalid", "artefact sans étape")
    if not isinstance(getattr(spec, "initial", ""), str) or not getattr(spec, "initial", ""):
        raise RegistryRefusal("spec-invalid", "artefact sans étape initiale")


def _spec_statuses(spec: Any) -> tuple[str, ...]:
    statuses = getattr(spec, "statuses", None)
    if not callable(statuses):
        raise RegistryRefusal("spec-invalid", "artefact sans statuses()")
    try:
        return tuple(str(s) for s in statuses())
    except RegistryRefusal:
        raise
    except Exception as exc:
        raise RegistryRefusal("spec-invalid", "statuses() illisible : %s" % exc) from exc


def _spec_core(spec: Any, status: str) -> str | None:
    core = getattr(spec, "core_of", None)
    if not callable(core):
        raise RegistryRefusal("spec-invalid", "artefact sans core_of()")
    try:
        value = str(core(status))
    except Exception:
        return None
    return value if value in CORE_STATES else None


def _spec_edges(spec: Any, source: str) -> tuple[tuple[str, str], ...]:
    transitions = getattr(spec, "transitions_from", None)
    if not callable(transitions):
        raise RegistryRefusal("spec-invalid", "artefact sans transitions_from()")
    try:
        raw = transitions(source)
    except Exception as exc:
        raise RegistryRefusal("spec-invalid", "transitions_from() illisible : %s" % exc) from exc
    out: list[tuple[str, str]] = []
    for edge in raw:
        target = getattr(edge, "target", None)
        guard = getattr(edge, "guard", None)
        if target is None and isinstance(edge, Sequence) and len(edge) == 2:
            target, guard = edge
        out.append((str(target), str(guard)))
    return tuple(out)


# --------------------------------------------------------------------------
# la transaction
# --------------------------------------------------------------------------

def _registry(verifiers: Mapping[str, Verifier] | None) -> dict[str, Verifier]:
    if verifiers is None:
        return dict(INSTALLED_VERIFIERS)
    if not isinstance(verifiers, Mapping):
        raise RegistryRefusal("verifier-invalid", "verifiers doit être une table garde → appelable")
    return dict(verifiers)


def _lock_lot(tx: Any, work_item_id: int) -> Mapping[str, Any]:
    """Verrouille le lot et rend sa ligne, ou refuse `lot-unknown`.

    La ligne est le snapshot du lot pour le vérificateur (tranche C) : elle
    porte ce que C lit (`title`, `closed_at`, `reviewer`, `type`, `loops`…).
    `reviewer` vient de `to_jsonb`, comme les autres lectures #705 : une base
    d'avant 0421 rend `''` au lieu de casser.
    """
    rows = tx.query(
        "SELECT w.id, w.state, w.assignee, w.type, w.app, w.workstream, w.loops,"
        "       w.title, w.closed_at, w.migration, w.migration_repo,"
        "       coalesce(to_jsonb(w)->>'reviewer', '') AS reviewer,"
        "       w.workflow_ref, w.workflow_stage, w.workflow_version"
        "  FROM work_items w WHERE w.id = %s FOR UPDATE",
        (int(work_item_id),))
    if not rows:
        raise RegistryRefusal("lot-unknown", "lot %s introuvable" % work_item_id)
    return rows[0]


def _check_authority(tx: Any, authority: Authority) -> dict:
    """Vérifie l'identité liée DANS la transaction, ligne du registre verrouillée.

    `FOR SHARE` sérialise avec une reprise ou une libération de bail : ou bien
    la commande passe avant la révocation, ou bien elle voit l'époque périmée
    et refuse — jamais un résultat autorisé par le passé.
    """
    rows = tx.query(
        "SELECT name, status, lease_owner, lease_epoch,"
        "       (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL"
        "        AND lease_expires_at > clock_timestamp()) AS live"
        "  FROM agent_registry WHERE name = %s FOR SHARE",
        (authority.actor,))
    if not rows:
        raise RegistryRefusal("authority-unknown-actor",
                              "acteur %s absent du registre" % authority.actor)
    row = rows[0]
    if row.get("status") in ("stopped", "dead"):
        raise RegistryRefusal("authority-revoked",
                              "acteur %s hors service (%s)"
                              % (authority.actor, row.get("status")))
    if authority.source == "runner":
        owner = row.get("lease_owner") or ""
        if owner != authority.runner_id:
            raise RegistryRefusal(
                "authority-lease",
                "bail de %s tenu par %s, pas par %s"
                % (authority.actor, owner or "personne", authority.runner_id))
        if int(row.get("lease_epoch") or 0) != authority.epoch:
            raise RegistryRefusal(
                "authority-epoch",
                "époque %s au registre, %s dans la commande"
                % (row.get("lease_epoch"), authority.epoch))
        if not row.get("live"):
            raise RegistryRefusal("authority-expired", "bail de %s échu" % authority.actor)
    return {
        "actor": authority.actor,
        "source": authority.source,
        "epoch": int(authority.epoch),
        "runner_id": authority.runner_id,
    }


def _journal_version(tx: Any, work_item_id: int) -> int:
    rows = tx.query(
        "SELECT coalesce(max(id), 0) AS version FROM work_item_events"
        " WHERE work_item_id = %s", (int(work_item_id),))
    return int(rows[0]["version"])


def _as_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _stored_intent(evidence: Any) -> str:
    data = _as_json(evidence)
    if not isinstance(data, Mapping):
        return ""
    intent = data.get("intent")
    if not isinstance(intent, Mapping):
        return ""
    value = str(intent.get("sha256") or "")
    return value if _SHA_RE.match(value) else ""


def _replay(tx: Any, command: Command, intent: str) -> Receipt | None:
    """Le reçu de cette clé, s'il existe : rejeu si l'intention est identique.

    Un reçu illisible ou d'une autre intention est un refus, jamais un
    résultat deviné. L'autorité a déjà été vérifiée : un rejeu par un acteur
    révoqué n'arrive pas ici.
    """
    rows = tx.query(
        "SELECT id, state, workflow_stage, workflow_version, evidence"
        "  FROM work_item_events WHERE work_item_id = %s AND command_id = %s::uuid",
        (int(command.work_item_id), canonical_command_id(command.command_id)))
    if not rows:
        return None
    row = rows[0]
    stored = _stored_intent(row["evidence"])
    if not stored:
        raise RegistryRefusal("receipt-corrupt",
                              "reçu %s du lot %s sans intention canonique"
                              % (command.command_id, command.work_item_id))
    if stored != intent:
        raise RegistryRefusal("intent-conflict",
                              "la clé %s du lot %s porte une autre intention"
                              % (command.command_id, command.work_item_id))
    return Receipt(
        accepted=True, replayed=True,
        work_item_id=int(command.work_item_id),
        command_id=canonical_command_id(command.command_id),
        action=command.action,
        event_id=int(row["id"]), version=int(row["id"]),
        state=str(row["state"]),
        workflow_ref=command.workflow_ref,
        workflow_stage=row["workflow_stage"],
        workflow_version=int(row["workflow_version"] or 0),
        guard=command.guard,
        intent_sha256=stored,
    )


def _check_revision(row: Mapping[str, Any], command: Command) -> None:
    """La commande vise exactement la révision admise du lot."""
    if row.get("workflow_ref") is None:
        raise RegistryRefusal("workflow-absent",
                              "lot %s sans workflow : comportement inchangé"
                              % command.work_item_id)
    if str(row["workflow_ref"]) != command.workflow_ref:
        raise RegistryRefusal("ref-mismatch",
                              "lot %s pinné sur %r, commande sur %r"
                              % (command.work_item_id, row["workflow_ref"],
                                 command.workflow_ref))
    if int(row.get("workflow_version") or 0) != command.workflow_version:
        raise RegistryRefusal(
            "revision-mismatch",
            "lot %s en révision %s, commande en révision %s (transfert explicite requis)"
            % (command.work_item_id, row.get("workflow_version"),
               command.workflow_version))


def _check_spec(spec: Any, command: Command) -> None:
    if _spec_ref(spec) != command.workflow_ref or _spec_version(spec) != command.workflow_version:
        raise RegistryRefusal(
            "spec-mismatch",
            "artefact %s@%s, commande %s@%s"
            % (_spec_ref(spec), _spec_version(spec), command.workflow_ref,
               command.workflow_version))


def _check_version(tx: Any, command: Command) -> int:
    version = _journal_version(tx, command.work_item_id)
    if version != command.expected_version:
        raise RegistryRefusal(
            "version-conflict",
            "lot %s en version %s, commande sur %s"
            % (command.work_item_id, version, command.expected_version))
    return version


@dataclass(frozen=True)
class _Plan:
    state: str
    stage: str
    loops: int
    note: str
    close_reason: str = ""
    superseded_by: int | None = None
    delivered_sha: str = ""


def _close_facts(tx: Any, command: Command) -> tuple[str, int | None, str]:
    """The closure carried by the command evidence, checked against legacy.

    Entering the terminal core `closed` means the lot was abandoned,
    superseded or delivered outside the target (the three doors of
    `work.close`, constraint `close_reason` of 0430). The registry never
    guesses: a missing or unknown reason is refused, `superseded_by` must name
    an existing lot (not itself), and a delivery carries the reviewed head
    (`sha`, 7 to 64 hex) that the `delivered` milestone persists.
    """
    evidence = command.evidence if isinstance(command.evidence, Mapping) else {}
    reason = str(evidence.get("close_reason") or "").strip()
    if reason not in CLOSE_REASONS:
        raise RegistryRefusal(
            "close-reason",
            "an advance to closed requires close_reason in evidence (%s), got %r"
            % (", ".join(CLOSE_REASONS), reason))
    superseded_by: int | None = None
    sha = ""
    if reason == "superseded":
        value = evidence.get("superseded_by")
        if not _is_int(value) or int(value) <= 0:
            raise RegistryRefusal(
                "superseded-unknown",
                "a superseded closure carries a positive superseded_by, got %r" % (value,))
        if int(value) == int(command.work_item_id):
            raise RegistryRefusal("superseded-unknown", "a lot cannot supersede itself")
        if not tx.query("SELECT 1 FROM work_items WHERE id = %s", (int(value),)):
            raise RegistryRefusal("superseded-unknown",
                                  "superseding lot %s not found" % value)
        superseded_by = int(value)
    elif reason == "delivered":
        sha = str(evidence.get("sha") or "").strip().lower()
        if not _HEAD_RE.match(sha):
            raise RegistryRefusal(
                "delivered-sha",
                "a delivered closure carries the reviewed head (sha, 7 to 64 hex), got %r"
                % (evidence.get("sha"),))
    return reason, superseded_by, sha


def _close_label(reason: str, superseded_by: int | None, sha: str) -> str:
    """The closure label of the journal and milestone note (legacy wording)."""
    if reason == "superseded":
        return "superseded by #%s" % superseded_by
    if reason == "delivered":
        return "delivered (head %s)" % sha[:12]
    return "abandoned"


def _plan_advance(tx: Any, row: Mapping[str, Any], command: Command, spec: Any) -> _Plan:
    statuses = _spec_statuses(spec)
    if command.from_stage not in statuses or command.to_stage not in statuses:
        raise RegistryRefusal("edge-undeclared",
                              "étape non déclarée : %s → %s"
                              % (command.from_stage, command.to_stage))
    current = row.get("workflow_stage")
    if not current:
        raise RegistryRefusal("stage-mismatch", "lot %s sans étape courante" % command.work_item_id)
    if str(current) != command.from_stage:
        raise RegistryRefusal("stage-mismatch",
                              "lot %s en étape %s, commande depuis %s"
                              % (command.work_item_id, current, command.from_stage))
    if row["state"] in TERMINAL_CORES:
        raise RegistryRefusal("terminal-reopened",
                              "lot %s en %s : un terminal ne se rouvre pas"
                              % (command.work_item_id, row["state"]))
    if row["state"] in HOLDS:
        raise RegistryRefusal(
            "held",
            "lot %s en %s : une reprise explicite (release) est requise"
            % (command.work_item_id, row["state"]))
    if (command.to_stage, command.guard) not in _spec_edges(spec, command.from_stage):
        raise RegistryRefusal("edge-undeclared",
                              "arête %s → %s (%s) absente de l'artefact"
                              % (command.from_stage, command.to_stage, command.guard))
    core_from = _spec_core(spec, command.from_stage)
    core_to = _spec_core(spec, command.to_stage)
    if core_from is None or core_to is None:
        raise RegistryRefusal("spec-invalid", "cœur inconnu pour une étape déclarée")
    loops = 1 if (core_from, core_to) == REPAIR_CORES else 0
    if loops and int(row.get("loops") or 0) >= MAX_QA_LOOPS:
        raise RegistryRefusal(
            "repair-exhausted",
            "boucle qa → build épuisée (%s) : bloquer le lot ou demander une décision"
            % MAX_QA_LOOPS)
    note = "%s → %s (%s)" % (command.from_stage, command.to_stage, command.guard)
    close_reason, superseded_by, delivered_sha = "", None, ""
    if core_to == "closed":
        close_reason, superseded_by, delivered_sha = _close_facts(tx, command)
        note = "%s — %s" % (note, _close_label(close_reason, superseded_by, delivered_sha))
    return _Plan(state=core_to, stage=command.to_stage, loops=loops, note=note,
                 close_reason=close_reason, superseded_by=superseded_by,
                 delivered_sha=delivered_sha)


def _lock_decisions(tx: Any, work_item_id: int) -> None:
    """Lock the open decision requests of the lot, AFTER the lot row (N12).

    Lock order: the lot first, then the requests — the order of the single
    L233 statement (`work.move(held=True)` locks the lot row, then the open
    requests in its `EXISTS … FOR UPDATE`). The reverse order (requests then
    lot) could deadlock with that statement on the same request; here the
    command waits on the lot holding nothing. The lock keeps an open request
    closed-proof until commit: an answer cannot slip between the check and the
    hold write, and `_pending_decision`, read after both locks, sees every
    request committed while the lot lock was awaited.

    An INSERT committed after that read is not blocked by a row lock
    (PostgreSQL has no gap lock): the ask path must take the lot lock before
    inserting its request for a full serialization — noted for the C/D
    consumer, see the design document.
    """
    tx.query(
        "SELECT 1 FROM agent_mailbox"
        " WHERE delivered_at IS NULL AND kind = 'request'"
        "   AND (payload -> 'decision') IS NOT NULL"
        "   AND btrim(work_item_id) = %s FOR UPDATE",
        (str(int(work_item_id)),))


def _pending_decision(tx: Any, work_item_id: int) -> bool:
    """L233 : une demande de décision native ouverte tient le lot.

    Read after both locks (lot then requests, `_lock_decisions`): every
    request committed while the lot lock was awaited is visible here.
    """
    rows = tx.query(
        "SELECT 1 FROM agent_mailbox r"
        " WHERE r.delivered_at IS NULL AND r.kind = 'request'"
        "   AND (r.payload -> 'decision') IS NOT NULL"
        "   AND btrim(r.work_item_id) = %s",
        (str(int(work_item_id)),))
    return bool(rows)


def _plan_hold(tx: Any, row: Mapping[str, Any], command: Command) -> _Plan:
    if row["state"] in TERMINAL_CORES:
        raise RegistryRefusal("terminal-reopened",
                              "lot %s en %s : un terminal ne se rouvre pas"
                              % (command.work_item_id, row["state"]))
    stage = row.get("workflow_stage")
    if not stage:
        raise RegistryRefusal("stage-mismatch", "lot %s sans étape courante" % command.work_item_id)
    if row["state"] == command.hold:
        raise RegistryRefusal("hold-redundant",
                              "lot %s déjà en %s" % (command.work_item_id, command.hold))
    if command.hold == "waiting_human" and not _pending_decision(tx, command.work_item_id):
        raise RegistryRefusal(
            "hold-no-decision",
            "waiting_human exige une demande de décision native en attente (L233)")
    return _Plan(state=command.hold, stage=str(stage), loops=0,
                 note="retenue %s (étape %s conservée)" % (command.hold, stage))


def _plan_release(tx: Any, row: Mapping[str, Any], command: Command, spec: Any) -> _Plan:
    if row["state"] not in HOLDS:
        raise RegistryRefusal("not-held",
                              "lot %s en %s : aucune retenue à lever"
                              % (command.work_item_id, row["state"]))
    if row["state"] == "waiting_human" and _pending_decision(tx, command.work_item_id):
        raise RegistryRefusal(
            "decision-pending",
            "lot %s en waiting_human : la décision native est encore ouverte, "
            "la reprise attend la réponse (L233)" % command.work_item_id)
    stage = row.get("workflow_stage")
    if not stage:
        raise RegistryRefusal("stage-mismatch", "lot %s sans étape courante" % command.work_item_id)
    core = _spec_core(spec, str(stage))
    if core is None:
        raise RegistryRefusal("stage-undeclared",
                              "étape %s absente de l'artefact" % stage)
    if core in TERMINAL_CORES:
        raise RegistryRefusal("terminal-reopened", "l'étape %s est terminale" % stage)
    return _Plan(state=core, stage=str(stage), loops=0,
                 note="reprise de %s vers %s" % (row["state"], stage))


def _pre_hold_core(tx: Any, work_item_id: int) -> str | None:
    """The last core recorded before the hold, read from the lot journal.

    A hold keeps the step but not the core: the row only says `blocked` or
    `waiting_human`, and the core before the hold is not a column anywhere.
    The journal is the verifiable record — every transition writes the state
    it produces, holds included — so the last line that is not a hold is the
    core the lot will resume into. `None` when the journal proves nothing (a
    lot created held, an out-of-band rewrite): fail-closed, the caller
    refuses.
    """
    rows = tx.query(
        "SELECT state FROM work_item_events"
        " WHERE work_item_id = %s AND state NOT IN ('blocked', 'waiting_human')"
        " ORDER BY id DESC LIMIT 1", (int(work_item_id),))
    if not rows:
        return None
    state = str(rows[0]["state"])
    return state if state in CORE_STATES else None


def _check_held_core(tx: Any, command: Command, core: str) -> None:
    """N10: the core before the hold must be proven, never assumed.

    Adopting or transferring a held lot keeps the hold; the core it resumes
    into is the one the new revision projects for the kept (or initial) step.
    Since the row does not carry the pre-hold core, the journal is the only
    witness: without one the registry refuses (`hold-core-unproven`) instead
    of silently resuming elsewhere; with one, a divergence is a
    `stage-mismatch`.
    """
    proof = _pre_hold_core(tx, int(command.work_item_id))
    if proof is None:
        raise RegistryRefusal(
            "hold-core-unproven",
            "lot %s is held and its journal proves no core before the hold: "
            "refusing a step that could resume elsewhere" % command.work_item_id)
    if proof != core:
        raise RegistryRefusal(
            "stage-mismatch",
            "lot %s was %s before the hold, the adopted step projects %s"
            % (command.work_item_id, proof, core))


def _verify(verifiers: Mapping[str, Verifier], guard: str, tx: Any,
            row: Mapping[str, Any], command: Command, spec: Any,
            from_stage: str, to_stage: str) -> Verification | None:
    """Appelle le vérificateur installé de la garde. Aucun défaut permissif."""
    verifier = verifiers.get(guard)
    if verifier is None:
        raise RegistryRefusal("verifier-uninstalled",
                              "aucun vérificateur installé pour la garde %r" % guard)
    context = VerificationContext(db=tx, work_item=dict(row), command=command,
                                  spec=spec, from_stage=from_stage, to_stage=to_stage)
    try:
        result = verifier(context)
    except RegistryRefusal:
        raise
    except Exception as exc:
        raise RegistryRefusal("verifier-error",
                              "%s : %s" % (type(exc).__name__, exc)) from exc
    if not isinstance(result, Verification):
        raise RegistryRefusal(
            "verifier-invalid",
            "le vérificateur %s rend %s : un Verification est attendu, jamais un booléen"
            % (guard, type(result).__name__))
    if not result.ok:
        raise RegistryRefusal("verifier-refused", result.reason or "refusé par %s" % guard)
    return result


def _evidence(command: Command, authority: dict, verification: Verification | None,
              intent: str, applied: dict, verifier_name: str) -> str:
    return json.dumps({
        "schema": RECEIPT_SCHEMA,
        "intent": {"sha256": intent},
        "command": applied,
        "authority": authority,
        "verification": ({"required": True, "verifier": verifier_name or None,
                          "ok": verification.ok, "reason": verification.reason,
                          "facts": dict(verification.facts or {})}
                         if verification is not None
                         else {"required": False, "verifier": None, "ok": None,
                               "reason": "", "facts": {}}),
        "facts": dict(command.evidence or {}),
    }, ensure_ascii=False, sort_keys=True)


def _applied(command: Command, plan: _Plan) -> dict:
    return {
        "action": command.action,
        "guard": command.guard or None,
        "from": command.from_stage or None,
        "to": command.to_stage or None,
        "hold": command.hold or None,
        "expected_version": int(command.expected_version),
        "workflow_ref": command.workflow_ref,
        "workflow_version": int(command.workflow_version),
    }


def _insert_receipt(tx: Any, *, work_item_id: int, state: str, note: str, actor: str,
                    command_id: str, stage: str, version: int, evidence: str) -> int:
    rows = tx.query(
        "INSERT INTO work_item_events"
        " (work_item_id, state, note, actor, command_id, workflow_stage,"
        "  workflow_version, evidence)"
        " VALUES (%s, %s, %s, %s, %s::uuid, %s, %s, %s::jsonb)"
        " RETURNING id",
        (int(work_item_id), state, note, actor, canonical_command_id(command_id),
         stage, int(version), evidence))
    return int(rows[0]["id"])


def _release_migration(tx: Any, command: Command, row: Mapping[str, Any],
                       state: str) -> None:
    """#956: a lot entering a released core gives its migration number back.

    The legacy `work.move`/`work.close` released the reservation in their own
    transaction; a pinned lot never reaches those, so the registry carries the
    same gesture — the storage `_release_migration`, including its journal
    line, written before the receipt so that `Receipt.version` stays the last
    id of the lot journal.
    """
    number = row.get("migration")
    if number is None:
        return
    tx.execute(
        "UPDATE work_items SET migration = NULL, migration_repo = NULL,"
        "       updated_at = now() WHERE id = %s", (int(command.work_item_id),))
    tx.execute(
        "INSERT INTO work_item_events (work_item_id, state, note, actor)"
        " VALUES (%s, %s, %s, %s)",
        (int(command.work_item_id), state,
         "migration %s released (state %s, #956)" % (number, state),
         command.authority.actor))


def _close_milestones(tx: Any, command: Command, plan: _Plan) -> None:
    """The closure milestones the legacy `work.close` wrote: `closed` always,
    `delivered` (with the reviewed head) for a delivery outside the target."""
    tx.execute(
        "INSERT INTO work_item_milestones (work_item_id, kind, actor, note)"
        " VALUES (%s, 'closed', %s, %s) ON CONFLICT DO NOTHING",
        (int(command.work_item_id), command.authority.actor, plan.note))
    if plan.close_reason == "delivered":
        tx.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, note)"
            " VALUES (%s, 'delivered', %s, %s, %s) ON CONFLICT DO NOTHING",
            (int(command.work_item_id), plan.delivered_sha,
             command.authority.actor, plan.note))


def _notify_released(tx: Any, command: Command, plan: _Plan, previous: str,
                     threads: list) -> None:
    """L385: the first entry into a released core wakes the dependants.

    Under the dependency graph lock (taken before the lot, as the legacy
    closures do), the mail is committed with the transition: no window where
    the release is committed without its wake. A lot already released does not
    wake twice (a `merged` then `closed`/`promoted` path); a closure carries
    its reason to the message.
    """
    if previous in RELEASED_CORES:
        return
    from . import work_dependencies
    work_dependencies.notify_released(
        tx, int(command.work_item_id), blocker_state=plan.state,
        blocker_reason=plan.close_reason if plan.state == "closed" else "",
        after_commit=threads)


def _receipt(command: Command, plan: _Plan, event_id: int, version: int,
             intent: str, replayed: bool = False) -> Receipt:
    return Receipt(
        accepted=True, replayed=replayed,
        work_item_id=int(command.work_item_id),
        command_id=canonical_command_id(command.command_id),
        action=command.action,
        event_id=int(event_id), version=int(version),
        state=plan.state, workflow_ref=command.workflow_ref,
        workflow_stage=plan.stage, workflow_version=int(command.workflow_version),
        guard=command.guard, intent_sha256=intent,
    )


# --------------------------------------------------------------------------
# API : appliquer une commande, transférer une révision, lire l'état
# --------------------------------------------------------------------------

def apply_command(db: Db, command: Command, *, spec: Any) -> Receipt:
    """Applique une commande native au lot, ou refuse (rien d'écrit).

    Tout — verrou du lot, autorité, révision, version, garde, écriture du lot
    et reçu — tient dans une transaction. Un rejeu de la même intention rend le
    reçu accepté (`replayed=True`) ; la même clé avec une autre intention est
    un `intent-conflict`.

    Les vérificateurs viennent du registre **installé** (`install_verifier`) :
    l'appelant ne fournit jamais le résultat d'une garde. `_apply` réserve
    l'injection au banc de tests.
    """
    return _apply(db, command, spec=spec, verifiers=INSTALLED_VERIFIERS)


def _apply(db: Db, command: Command, *, spec: Any,
           verifiers: Mapping[str, Verifier] | None = None) -> Receipt:
    _validate_command(command)
    if command.action == "transfer":
        raise RegistryRefusal("command-invalid",
                              "action transfer : passer par transfer_revision")
    _validate_spec(spec)
    _check_spec(spec, command)
    registry = _registry(verifiers)
    intent = intent_sha256(command)
    # N9 : l'avance qui entre dans un cœur libéré décide le réveil des
    # dépendants sous le verrou de graphe, pris AVANT le lot — l'ordre des
    # fermetures historiques (`work.close`/`close_merged`), qui le tiennent
    # puis lisent `released_by`. Sans lui, deux libérations concurrentes
    # perdraient ou doubleraient le réveil ; pris après le lot, il
    # s'interbloquerait avec une fermeture legacy.
    released = (command.action == "advance"
                and _spec_core(spec, command.to_stage) in RELEASED_CORES)
    threads: list = []
    with db.transaction() as tx:
        if released:
            from . import work_dependencies
            work_dependencies.lock(tx)
        row = _lock_lot(tx, command.work_item_id)
        if command.action in ("hold", "release"):
            # N12 : le lot d'abord, puis les demandes de décision ouvertes —
            # l'ordre de l'instruction unique de L233 (`work.move(held=True)`
            # verrouille le lot, puis les demandes dans son EXISTS ... FOR
            # UPDATE). La lecture de `_pending_decision` vient après les deux
            # verrous : une demande committée en attendant le lot est vue.
            _lock_decisions(tx, command.work_item_id)
        authority = _check_authority(tx, command.authority)
        replayed = _replay(tx, command, intent)
        if replayed is not None:
            return replayed
        _check_revision(row, command)
        _check_version(tx, command)

        verification: Verification | None = None
        if command.action == "advance":
            plan = _plan_advance(tx, row, command, spec)
            verification = _verify(registry, command.guard, tx, row, command, spec,
                                   command.from_stage, command.to_stage)
            verifier_name = command.guard
        elif command.action == "hold":
            plan = _plan_hold(tx, row, command)
            verification = _verify(registry, HOLD_VERIFIER, tx, row, command, spec,
                                   plan.stage, plan.stage)
            verifier_name = HOLD_VERIFIER
        else:  # release (transfer est refusé plus haut)
            plan = _plan_release(tx, row, command, spec)
            verification = _verify(registry, HOLD_VERIFIER, tx, row, command, spec,
                                   plan.stage, plan.stage)
            verifier_name = HOLD_VERIFIER

        tx.execute(
            "UPDATE work_items"
            "   SET state = %s,"
            "       workflow_stage = %s,"
            "       loops = loops + %s,"
            "       close_reason = CASE WHEN %s = 'closed' THEN %s ELSE close_reason END,"
            "       superseded_by = CASE WHEN %s = 'closed' THEN %s ELSE superseded_by END,"
            "       updated_at = now(),"
            "       closed_at = CASE WHEN %s IN ('promoted', 'closed')"
            "                         THEN now() ELSE closed_at END,"
            "       started_at = CASE WHEN %s IN ('build', 'qa')"
            "                         THEN coalesce(started_at, now()) ELSE started_at END"
            " WHERE id = %s",
            (plan.state, plan.stage, plan.loops, plan.state, plan.close_reason or None,
             plan.state, plan.superseded_by, plan.state, plan.state,
             int(command.work_item_id)))
        if plan.state in RELEASED_CORES:
            _release_migration(tx, command, row, plan.state)
        if plan.state == "closed":
            _close_milestones(tx, command, plan)
        evidence = _evidence(command, authority, verification, intent,
                             _applied(command, plan), verifier_name)
        event_id = _insert_receipt(
            tx, work_item_id=command.work_item_id, state=plan.state, note=plan.note,
            actor=command.authority.actor, command_id=command.command_id,
            stage=plan.stage, version=command.workflow_version, evidence=evidence)
        if plan.state in RELEASED_CORES:
            _notify_released(tx, command, plan, str(row["state"]), threads)
        receipt = _receipt(command, plan, event_id, event_id, intent)
    # le fil lisible après le commit seulement : un envoi annulé n'y laisse rien
    for record in threads:
        record(db.cfg, db)
    return receipt


def transfer_revision(db: Db, *, work_item_id: int, command_id: str,
                      expected_version: int, workflow_ref: str, workflow_version: int,
                      authority: Authority, spec: Any,
                      evidence: Mapping[str, Any] | None = None) -> Receipt:
    """Pinn (ou change) la révision Process d'un lot — geste explicite seulement.

    Première adoption : l'étape initiale de l'artefact doit projeter le cœur
    natif courant du lot (une retenue, elle, est conservée) ; un lot terminal
    n'est jamais rouvert, même par adoption. Transfert : l'étape courante est
    conservée, elle doit exister dans le nouvel artefact et projeter son cœur
    courant hors retenue ; une régression de version est refusée. Un
    vérificateur serveur `revision_transfer` doit être installé : aucun
    transfert n'est possible par défaut, et l'appelant ne fournit jamais son
    résultat (`_transfer` réserve l'injection au banc de tests).
    """
    return _transfer(db, work_item_id=work_item_id, command_id=command_id,
                     expected_version=expected_version, workflow_ref=workflow_ref,
                     workflow_version=workflow_version, authority=authority,
                     spec=spec, evidence=evidence, verifiers=INSTALLED_VERIFIERS)


def _transfer(db: Db, *, work_item_id: int, command_id: str,
              expected_version: int, workflow_ref: str, workflow_version: int,
              authority: Authority, spec: Any,
              evidence: Mapping[str, Any] | None = None,
              verifiers: Mapping[str, Verifier] | None = None) -> Receipt:
    command = Command(
        work_item_id=int(work_item_id), command_id=command_id,
        expected_version=int(expected_version), authority=authority,
        workflow_ref=workflow_ref, workflow_version=int(workflow_version),
        action="transfer", evidence=dict(evidence or {}))
    _validate_command(command)
    _validate_spec(spec)
    _check_spec(spec, command)
    registry = _registry(verifiers)
    intent = intent_sha256(command)
    with db.transaction() as tx:
        row = _lock_lot(tx, command.work_item_id)
        authority = _check_authority(tx, command.authority)
        replayed = _replay(tx, command, intent)
        if replayed is not None:
            return replayed
        _check_version(tx, command)

        if row.get("workflow_ref") is None:
            if row["state"] in TERMINAL_CORES:
                raise RegistryRefusal(
                    "terminal-reopened",
                    "lot %s en %s : un terminal ne se rouvre pas, même par adoption"
                    % (command.work_item_id, row["state"]))
            stage = str(getattr(spec, "initial"))
            core = _spec_core(spec, stage)
            if core is None:
                raise RegistryRefusal("spec-invalid",
                                      "étape initiale %r sans cœur" % stage)
            if core in TERMINAL_CORES:
                raise RegistryRefusal("terminal-reopened",
                                      "étape initiale %s déjà terminale" % stage)
            if row["state"] in HOLDS:
                # N10 : le cœur d'avant la retenue n'est pas sur la ligne ; le
                # journal doit le prouver avant d'adopter une étape.
                _check_held_core(tx, command, str(core))
                state = row["state"]
            elif core == str(row["state"]):
                state = core
            else:
                raise RegistryRefusal(
                    "stage-mismatch",
                    "adoption : l'étape initiale %s projette %s, lot en %s"
                    % (stage, core, row["state"]))
        else:
            if int(row.get("workflow_version") or 0) > command.workflow_version:
                raise RegistryRefusal(
                    "revision-regression",
                    "révision %s → %s : un transfert ne revient pas en arrière"
                    % (row.get("workflow_version"), command.workflow_version))
            stage = str(row.get("workflow_stage") or getattr(spec, "initial"))
            core = _spec_core(spec, stage)
            if core is None:
                raise RegistryRefusal("stage-undeclared",
                                      "étape %s absente de l'artefact" % stage)
            if row["state"] in HOLDS:
                _check_held_core(tx, command, str(core))
            elif core != str(row["state"]):
                raise RegistryRefusal(
                    "stage-mismatch",
                    "transfert : l'étape %s projette %s, lot en %s"
                    % (stage, core, row["state"]))
            state = row["state"]

        verification = _verify(registry, TRANSFER_VERIFIER, tx, row, command, spec,
                               row.get("workflow_stage") or "", stage)
        plan = _Plan(state=str(state), stage=stage, loops=0,
                     note="transfert de révision %s@%s" % (command.workflow_ref,
                                                           command.workflow_version))
        tx.execute(
            "UPDATE work_items"
            "   SET state = %s, workflow_ref = %s, workflow_stage = %s,"
            "       workflow_version = %s, updated_at = now()"
            " WHERE id = %s",
            (plan.state, command.workflow_ref, plan.stage,
             int(command.workflow_version), int(command.work_item_id)))
        applied = _applied(command, plan)
        applied.update({"previous_ref": row.get("workflow_ref"),
                        "previous_version": int(row.get("workflow_version") or 0)})
        payload = _evidence(command, authority, verification, intent, applied,
                            TRANSFER_VERIFIER)
        event_id = _insert_receipt(
            tx, work_item_id=command.work_item_id, state=plan.state, note=plan.note,
            actor=command.authority.actor, command_id=command.command_id,
            stage=plan.stage, version=command.workflow_version, evidence=payload)
        return _receipt(command, plan, event_id, event_id, intent)


def registry_state(db: Db, work_item_id: int) -> RegistryState:
    """État du registre d'un lot : révision, étape, version du journal, reçus."""
    rows = db.query(
        "SELECT id, state, workflow_ref, workflow_stage, workflow_version"
        "  FROM work_items WHERE id = %s", (int(work_item_id),))
    if not rows:
        raise RegistryRefusal("lot-unknown", "lot %s introuvable" % work_item_id)
    row = rows[0]
    version = _journal_version(db, work_item_id)
    receipts = db.query(
        "SELECT count(*)::int AS n FROM work_item_events"
        " WHERE work_item_id = %s AND command_id IS NOT NULL", (int(work_item_id),))
    return RegistryState(
        work_item_id=int(work_item_id), state=str(row["state"]),
        workflow_ref=row["workflow_ref"], workflow_stage=row["workflow_stage"],
        workflow_version=int(row["workflow_version"] or 0),
        version=int(version), receipts=int(receipts[0]["n"]))


def receipt_of(db: Db, work_item_id: int, command_id: str) -> dict | None:
    """Le reçu d'une clé, ou None : lecture seule, sans autorité ni verrou."""
    rows = db.query(
        "SELECT id, state, note, actor, workflow_stage, workflow_version, evidence,"
        "       extract(epoch from created_at)::float8 AS created_ts"
        "  FROM work_item_events WHERE work_item_id = %s AND command_id = %s::uuid",
        (int(work_item_id), canonical_command_id(command_id)))
    if not rows:
        return None
    row = dict(rows[0])
    row["evidence"] = _as_json(row["evidence"])
    return row


#: Collections réellement collectées pour la tranche C (pas d'`observations` :
#: la source n'est pas installée, un vérificateur qui l'exige doit refuser).
SOURCE_KEYS = ("milestones", "receipts", "decisions", "deliveries")


def read_sources(context: VerificationContext) -> dict[str, Any]:
    """Les collections réelles du lot, lues dans la transaction du vérificateur.

    Tranche C : ses vérificateurs sont purs et sans SQL ; ce lecteur est le
    leur, appelé depuis l'appelable installé. Le mapping ne déclare que ce qui
    est réellement collecté — `SOURCE_KEYS` — jamais une collection absente
    (`observations` n'est pas installée) : un vérificateur qui l'exige refuse
    `source-unavailable` (fail-closed), jamais un accord par défaut.

    * `milestones` : `{id, kind, sha, verdict, note}` ;
    * `receipts` : reçus acceptés (`command_id` non nul) — `{accepted, guard,
      event_id, command_id, workflow_stage, workflow_version, sha256,
      evidence, facts}` ; `guard` est la garde Process, ou le nom réservé du
      vérificateur (`hold`, `revision_transfer`) ;
    * `decisions` : demandes de décision natives ouvertes, `{id, kind, payload,
      work_item_id}` (L233) ;
    * `deliveries` : jalons natifs `delivered`, `{id, sha, accepted}`.
    """
    if not isinstance(context, VerificationContext):
        raise RegistryRefusal("verifier-invalid", "VerificationContext attendu")
    db = context.db
    item_id = int(context.command.work_item_id)
    milestones = [
        {"id": int(row["id"]), "kind": str(row["kind"]),
         "sha": str(row["sha"] or ""), "verdict": row["verdict"],
         "note": str(row["note"] or "")}
        for row in db.query(
            "SELECT id, kind, sha, verdict, note FROM work_item_milestones"
            " WHERE work_item_id = %s ORDER BY id", (item_id,))]
    receipts: list[dict[str, Any]] = []
    for row in db.query(
            "SELECT id, command_id::text AS command_id, workflow_stage,"
            "       workflow_version, evidence"
            "  FROM work_item_events"
            " WHERE work_item_id = %s AND command_id IS NOT NULL ORDER BY id",
            (item_id,)):
        evidence = _as_json(row["evidence"])
        if not isinstance(evidence, Mapping):
            evidence = {}
        command = evidence.get("command")
        verification = evidence.get("verification")
        guard = ""
        if isinstance(command, Mapping):
            guard = str(command.get("guard") or "")
        if not guard and isinstance(verification, Mapping):
            guard = str(verification.get("verifier") or "")
        facts = evidence.get("facts")
        receipts.append({
            "accepted": True,
            "guard": guard,
            "event_id": int(row["id"]),
            "command_id": str(row["command_id"]),
            "workflow_stage": row["workflow_stage"],
            "workflow_version": int(row["workflow_version"] or 0),
            "sha256": _stored_intent(evidence),
            "evidence": dict(evidence),
            "facts": dict(facts) if isinstance(facts, Mapping) else {},
        })
    decisions = [
        {"id": int(row["id"]), "kind": str(row["kind"]),
         "payload": _as_json(row["payload"]),
         "work_item_id": str(row["work_item_id"] or "")}
        for row in db.query(
            "SELECT id, kind, payload, work_item_id FROM agent_mailbox"
            " WHERE delivered_at IS NULL AND kind = 'request'"
            "   AND (payload -> 'decision') IS NOT NULL"
            "   AND btrim(work_item_id) = %s ORDER BY id", (str(item_id),))]
    deliveries = [
        {"id": int(row["id"]), "sha": str(row["sha"] or ""), "accepted": True}
        for row in db.query(
            "SELECT id, sha FROM work_item_milestones"
            " WHERE work_item_id = %s AND kind = 'delivered' ORDER BY id",
            (item_id,))]
    return {"milestones": milestones, "receipts": receipts,
            "decisions": decisions, "deliveries": deliveries}
