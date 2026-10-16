# SPDX-License-Identifier: AGPL-3.0-only
"""Workflows Process (#1915, tranche C) : verificateurs finis sur preuves reelles.

C decide, pour une commande de transition, si les **preuves reelles** exigees
par la garde et par le type du lot sont reunies. Le coeur (`verify`,
`verify_hold`) est **pur** : aucune lecture SQL, aucune ecriture, aucune
expression venue d'un canon. B (`process_registry`, #1922) tient la
transaction : verrou du lot, autorite/epoch, revision, version optimiste du
journal, arete et retenue, recu idempotent ; il appelle l'appelable serveur
installe pour la garde, **dans sa transaction**, avec un `VerificationContext`.
Le lecteur des sources SQL (`read_sources`) est du cote B : C ne lit jamais la
base lui-meme (arbitrage pilote du 16/10).

Deux versions distinctes, jamais confondues :

* `workflow_ref` + `workflow_version` : la **revision Process** pinnee (A,
  #1918) ; elle ne change que par transfert explicite ;
* `expected_version` / `Receipt.version` : la **version optimiste du journal**,
  tenue par B. C ne la verifie pas et ne la rend pas.

Regles de decision :

* la requete ne porte que des **references** (sha, observation, recu, lot) ;
  un booleen client, une valeur d'un autre type ou une cle inconnue sont des
  refus (`evidence-invalid`) ; jamais un accord par defaut ;
* une collection exigee par la garde absente des sources est un refus
  (`source-unavailable`) : un verificateur indisponible refuse sa transition,
  il ne l'accepte pas en silence ;
* une preuve de deploiement staging/preprod est un **recu B accepte qui
  reference une observation reelle distincte** : un recu de demande de
  deploiement ne prouve pas son succes (`observation-unwitnessed`) ; une
  observation referencee mais non installee refuse (`source-unavailable`).

Registre fini (contrat #1379, commentaire 6207021753) : `GUARD_NAMES`. Types
installes opt-in : `INSTALLED_TYPES` ; les types de code (`CODE_TYPES`)
exigent une livraison/acceptation reelle, les autres leur propre resultat et
la revue applicable. `promotion` n'accorde aucun pouvoir de gouvernance et
l'ouverture d'une PR de promotion seule ne complete jamais un lot.

Holds : `blocked` retient l'etape et la revision ; `waiting_human` exige une
decision native reellement en attente (`decisions_pending >= 1`), jamais un
simple drapeau. Les entrees et sorties de retenue (`action` `hold`/`release`)
passent par l'appelable reserve `HOLD_VERIFIER`, jamais par une voie de
contournement ; une avance depuis une retenue est refusee (`hold-advance`).
Les reparations sont bornees par `MAX_REPAIRS` ; aucune reouverture terminale.

Collections d'un snapshot (noms des sources lues par B) : `milestones`,
`receipts`, `observations`, `decisions`, `deliveries`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

#: Registre fini des verificateurs du contrat consommateur (#1379).
GUARD_NAMES = (
    "planning",
    "start_work",
    "review_requested",
    "qa_started",
    "qa_repair",
    "merged_and_staging_deployed",
    "staging_pass",
    "staging_failure",
    "preprod_deployed",
    "preprod_pass",
    "preprod_failure",
    "repair_started",
    "stop_work",
    "work_completed",
)

#: Types installes : les deux types historiques plus l'extension opt-in.
INSTALLED_TYPES = (
    "bug",
    "evolution",
    "docs",
    "investigation",
    "governance-tracking",
    "promotion",
    "human_operation",
)

#: Types de code : leur completion exige une livraison/acceptation reelle.
CODE_TYPES = ("bug", "evolution")

#: Reparations bornees, alignees sur la boucle qa -> build native (B verifie
#: deja la borne avant d'appeler C ; ce controle est une defense en profondeur).
MAX_REPAIRS = 2

#: Retenues natives : elles gardent l'etape et la revision.
HOLDS = ("blocked", "waiting_human")

#: Actions du registre B : une avance gardee, une retenue, sa reprise.
ACTIONS = ("advance", "hold", "release")

#: Nom reserve du verificateur des retenues/reprises : B l'appelle pour
#: `action` `hold` et `release`, comme `revision_transfer` pour un transfert.
HOLD_VERIFIER = "hold"

#: Verificateurs reserves installes par C en plus des 14 gardes.
RESERVED_VERIFIERS = (HOLD_VERIFIER,)

#: Environnements de deploiement observes.
ENVIRONMENTS = ("staging", "preprod")

#: Genres d'observation de deploiement : deploye, passe, echoue.
OBSERVATION_KINDS = ("deployed", "pass", "fail")

#: Collections qu'un snapshot peut declarer avoir collectees.
SOURCE_COLLECTIONS = (
    "milestones",
    "receipts",
    "observations",
    "decisions",
    "deliveries",
)

#: Codes de refus stables (le code d'acceptation est `accepted`).
REFUSAL_CODES = (
    "command-invalid",
    "completion-evidence-missing",
    "completion-not-accepted",
    "completion-review-missing",
    "evidence-invalid",
    "evidence-missing",
    "failure-missing",
    "freeze-missing",
    "guard-unsupported",
    "hold-advance",
    "hold-invalid",
    "hold-no-decision",
    "hold-redundant",
    "hold-unsupported",
    "item-mismatch",
    "item-missing",
    "merge-missing",
    "not-held",
    "observation-result-mismatch",
    "observation-unwitnessed",
    "repair-bound",
    "reviewer-missing",
    "reviewer-not-independent",
    "revision-mismatch",
    "revision-missing",
    "source-unavailable",
    "stage-stale",
    "terminal-reopening",
    "type-unsupported",
    "verdict-missing",
    "workflow-absent",
)


# --------------------------------------------------------------------------
# formes echangees avec B
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """Une commande de transition : intention + references, jamais un verdict.

    `action` est l'une de `ACTIONS` : `advance` (arete gardee), `hold`
    (retenue, `hold` nomme `blocked`/`waiting_human`) ou `release` (reprise).
    `references` porte les faits structures du client (B les appelle
    `evidence`) : des chaines (sha, observation, lot) ou des entiers ; tout le
    reste est refuse. `actor`/`source` ne sont pas rejuges ici : l'autorite est
    tenue par B dans sa transaction.
    """

    work_item_id: int
    command_id: str
    guard: str = ""
    from_stage: str = ""
    to_stage: str = ""
    actor: str = ""
    source: str = ""
    action: str = "advance"
    hold: str = ""
    references: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """Preuves lues par B, dans sa transaction, et passees a C.

    `workflow_ref`/`workflow_version` sont la revision Process pinnee (jamais
    la version du journal). `revision` est la tete de travail reelle du lot
    (gel, fusion, livraison). `receipts` ne porte que des recus **acceptes** ;
    `observations` des enregistrements reels distincts (deploiement staging ou
    preprod). `sources` dit ce que B a reellement collecte : une garde dont une
    collection manque refuse au lieu d'accepter.
    """

    item: Mapping[str, Any] = field(default_factory=dict)
    workflow_ref: str = ""
    workflow_stage: str = ""
    workflow_version: int = 0
    revision: str = ""
    milestones: tuple[Mapping[str, Any], ...] = ()
    receipts: tuple[Mapping[str, Any], ...] = ()
    observations: tuple[Mapping[str, Any], ...] = ()
    decisions_pending: int = 0
    deliveries: tuple[Mapping[str, Any], ...] = ()
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """Verdict de C : accepte, ou refuse avec un code stable et un message."""

    accepted: bool
    code: str
    message: str = ""
    stage: str = ""
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class Capability:
    """Une garde du registre fini, ses sources et ses references exigees."""

    guard: str
    purpose: str
    sources: tuple[str, ...]
    references: tuple[str, ...]


@dataclass(frozen=True)
class CompletionPolicy:
    """Ce qu'exige la completion d'un type : livraison et/ou revue."""

    type: str
    code: bool
    requires_delivery: bool
    requires_review: bool
    reason: str


@dataclass(frozen=True)
class GuardSpec:
    """Une garde : sources exigees, references requises/permises, controle."""

    name: str
    purpose: str
    sources: tuple[str, ...]
    required: tuple[str, ...]
    optional: tuple[str, ...]
    check: Callable[[Command, Snapshot], Decision]


# --------------------------------------------------------------------------
# petites lectures sures
# --------------------------------------------------------------------------

def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return -1


def _present(value: Any) -> bool:
    """Une reference existe : chaine non vide, ou entier (jamais un booleen)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return bool(_text(value))


def _ref(command: Command, key: str) -> str:
    return _text((command.references or {}).get(key))


def _loops(snapshot: Snapshot) -> int:
    return max(_as_int(snapshot.item.get("loops")), 0)


def _revision(snapshot: Snapshot) -> str:
    return _text(snapshot.revision)


def _milestone(snapshot: Snapshot, kind: str, sha: str = "") -> Mapping | None:
    for row in snapshot.milestones:
        if _text(row.get("kind")) != kind:
            continue
        if sha and _text(row.get("sha")) != sha:
            continue
        return row
    return None


def _verdict(snapshot: Snapshot, verdict: str, sha: str = "") -> Mapping | None:
    for row in snapshot.milestones:
        if _text(row.get("kind")) != "verdict":
            continue
        if _text(row.get("verdict")) != verdict:
            continue
        if sha and _text(row.get("sha")) != sha:
            continue
        return row
    return None


def _observation(snapshot: Snapshot, ident: str) -> Mapping | None:
    for row in snapshot.observations:
        if _text(row.get("id")) == ident:
            return row
    return None


def _witness(snapshot: Snapshot, observation_id: str) -> Mapping | None:
    """Le recu B accepte qui reference cette observation, s'il existe.

    Un recu explicitement non accepte ne prouve rien ; l'absence du drapeau
    vaut accepte (la collection ne porte que des recus acceptes).
    """
    for receipt in snapshot.receipts:
        if receipt.get("accepted") is False:
            continue
        evidence = receipt.get("evidence") or {}
        if isinstance(evidence, Mapping) and _text(
                evidence.get("observation")) == observation_id:
            return receipt
    return None


def _delivery(snapshot: Snapshot, ident: str = "") -> Mapping | None:
    for row in snapshot.deliveries:
        if ident and _text(row.get("id")) != ident:
            continue
        return row
    return None


def _failed_observation(snapshot: Snapshot, ident: str = "") -> Mapping | None:
    for row in snapshot.observations:
        if ident and _text(row.get("id")) != ident:
            continue
        if _text(row.get("kind")) == "fail":
            return row
    return None


def _closed(snapshot: Snapshot) -> bool:
    if snapshot.item.get("closed_at"):
        return True
    return _text(snapshot.item.get("state")) == "closed"


def revision_of(milestones) -> str:
    """La tete de travail native : gel, puis fusion, puis livraison."""
    for kind in ("frozen", "merged", "delivered"):
        for row in milestones or ():
            if _text(row.get("kind")) == kind and _text(row.get("sha")):
                return _text(row.get("sha"))
    return ""


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------

def _accept(command: Command, references: tuple = ()) -> Decision:
    return Decision(True, "accepted", "", command.to_stage, tuple(references))


def _refuse(code: str, message: str, snapshot: Snapshot) -> Decision:
    return Decision(False, code, message, snapshot.workflow_stage)


# --------------------------------------------------------------------------
# controles par garde
# --------------------------------------------------------------------------

def _check_planning(command: Command, snapshot: Snapshot) -> Decision:
    if not _text(snapshot.item.get("title")):
        return _refuse("evidence-missing", "lot sans titre : aucun plan natif",
                       snapshot)
    return _accept(command)


def _check_start_work(command: Command, snapshot: Snapshot) -> Decision:
    if not _text(snapshot.item.get("assignee")):
        return _refuse("evidence-missing", "aucun assigne reel sur le lot",
                       snapshot)
    return _accept(command)


def _check_review_requested(command: Command, snapshot: Snapshot) -> Decision:
    head = _ref(command, "head")
    if _milestone(snapshot, "frozen", head) is None:
        return _refuse("freeze-missing",
                       "aucun gel de la tete %s" % (head or "(absente)"), snapshot)
    reviewer = _text(snapshot.item.get("reviewer"))
    if not reviewer:
        return _refuse("reviewer-missing", "aucun relecteur designe", snapshot)
    if reviewer == _text(snapshot.item.get("assignee")):
        return _refuse("reviewer-not-independent",
                       "relecteur et auteur sont le meme agent", snapshot)
    return _accept(command, ("head",))


def _check_qa_started(command: Command, snapshot: Snapshot) -> Decision:
    head = _ref(command, "head") or _revision(snapshot)
    if not head:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    if _milestone(snapshot, "frozen", head) is None:
        return _refuse("freeze-missing",
                       "aucun gel de la revision %s" % head, snapshot)
    if not _text(snapshot.item.get("reviewer")):
        return _refuse("reviewer-missing", "aucun relecteur designe", snapshot)
    return _accept(command, ("head",) if _ref(command, "head") else ())


def _check_qa_repair(command: Command, snapshot: Snapshot) -> Decision:
    sha = _ref(command, "head") or _revision(snapshot)
    if not sha:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    if _verdict(snapshot, "blocked", sha) is None:
        return _refuse("verdict-missing",
                       "aucun verdict bloque sur la revision %s" % sha, snapshot)
    if _loops(snapshot) >= MAX_REPAIRS:
        return _refuse("repair-bound",
                       "reparations deja au maximum (%d)" % MAX_REPAIRS, snapshot)
    return _accept(command, ("head",) if _ref(command, "head") else ())


def _observation_proof(command: Command, snapshot: Snapshot, env: str,
                       ref_key: str, kind: str, sha: str,
                       *, repair: bool = False) -> Decision:
    """Une observation reelle, referencee par un recu accepte distinct."""
    if not sha:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    ident = _ref(command, ref_key)
    observation = _observation(snapshot, ident)
    if observation is None:
        return _refuse("source-unavailable",
                       "observation %s non installee" % (ident or "(absente)"),
                       snapshot)
    if _text(observation.get("env")) != env:
        return _refuse("observation-result-mismatch",
                       "observation %s hors de l'environnement %s"
                       % (ident, env), snapshot)
    if _text(observation.get("sha")) != sha:
        return _refuse("revision-mismatch",
                       "observation %s d'une autre revision" % ident, snapshot)
    if _text(observation.get("kind")) != kind:
        return _refuse("observation-result-mismatch",
                       "observation %s non %s" % (ident, kind), snapshot)
    if _witness(snapshot, ident) is None:
        return _refuse("observation-unwitnessed",
                       "aucun recu accepte ne reference l'observation %s : "
                       "une demande de deploiement ne prouve pas son succes"
                       % ident, snapshot)
    if repair and _loops(snapshot) >= MAX_REPAIRS:
        return _refuse("repair-bound",
                       "reparations deja au maximum (%d)" % MAX_REPAIRS, snapshot)
    return _accept(command, (ref_key, "observation:%s" % ident))


def _check_merged_and_staging_deployed(command: Command,
                                        snapshot: Snapshot) -> Decision:
    merge = _ref(command, "merge")
    if _milestone(snapshot, "merged", merge) is None:
        return _refuse("merge-missing",
                       "aucune fusion reelle de %s" % (merge or "(absente)"),
                       snapshot)
    return _observation_proof(command, snapshot, "staging", "staging",
                              "deployed", merge)


def _check_staging_pass(command: Command, snapshot: Snapshot) -> Decision:
    return _observation_proof(command, snapshot, "staging", "staging", "pass",
                              _revision(snapshot))


def _check_staging_failure(command: Command, snapshot: Snapshot) -> Decision:
    return _observation_proof(command, snapshot, "staging", "staging", "fail",
                              _revision(snapshot), repair=True)


def _check_preprod_deployed(command: Command, snapshot: Snapshot) -> Decision:
    return _observation_proof(command, snapshot, "preprod", "preprod",
                              "deployed", _revision(snapshot))


def _check_preprod_pass(command: Command, snapshot: Snapshot) -> Decision:
    return _observation_proof(command, snapshot, "preprod", "preprod", "pass",
                              _revision(snapshot))


def _check_preprod_failure(command: Command, snapshot: Snapshot) -> Decision:
    return _observation_proof(command, snapshot, "preprod", "preprod", "fail",
                              _revision(snapshot), repair=True)


def _check_repair_started(command: Command, snapshot: Snapshot) -> Decision:
    revision = _revision(snapshot)
    if not revision:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    ident = _ref(command, "failure")
    failure = _failed_observation(snapshot, ident)
    if failure is None:
        return _refuse("failure-missing",
                       "aucun echec enregistre %s" % (ident or "sur la revision"),
                       snapshot)
    if _text(failure.get("env")) not in ENVIRONMENTS:
        return _refuse("observation-result-mismatch",
                       "echec %s hors environnement de deploiement" % ident,
                       snapshot)
    if _text(failure.get("sha")) != revision:
        return _refuse("revision-mismatch",
                       "echec enregistre sur une autre revision", snapshot)
    if _witness(snapshot, _text(failure.get("id"))) is None:
        return _refuse("observation-unwitnessed",
                       "aucun recu accepte ne reference l'echec %s" % ident,
                       snapshot)
    if _loops(snapshot) >= MAX_REPAIRS:
        return _refuse("repair-bound",
                       "reparations deja au maximum (%d)" % MAX_REPAIRS, snapshot)
    return _accept(command, ("failure",) if ident else ())


def _check_stop_work(command: Command, snapshot: Snapshot) -> Decision:
    reason = _ref(command, "reason")
    if reason not in ("abandoned", "superseded"):
        return _refuse("evidence-invalid",
                       "raison d'arret attendue: abandoned ou superseded", snapshot)
    if reason == "superseded" and not _present(
            (command.references or {}).get("superseded_by")):
        return _refuse("evidence-missing",
                       "supersession sans lot remplacant", snapshot)
    return _accept(command, ("reason",))


def _check_work_completed(command: Command, snapshot: Snapshot) -> Decision:
    revision = _revision(snapshot)
    if not revision:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    policy = completion_policy(_text(snapshot.item.get("type")))
    if policy.requires_delivery:
        ident = _ref(command, "delivery")
        if not ident:
            return _refuse("evidence-missing",
                           "reference delivery requise pour un type de code",
                           snapshot)
        delivery = _delivery(snapshot, ident)
        if delivery is None:
            return _refuse("completion-evidence-missing",
                           "aucune livraison/acceptation reelle : l'ouverture "
                           "d'une PR de promotion seule ne complete pas", snapshot)
        if not delivery.get("accepted"):
            return _refuse("completion-not-accepted",
                           "livraison enregistree mais non acceptee", snapshot)
        if _text(delivery.get("sha")) != revision:
            return _refuse("revision-mismatch",
                           "livraison d'une autre revision que la tete", snapshot)
        return _accept(command, ("delivery",))
    if not _ref(command, "outcome"):
        return _refuse("completion-evidence-missing",
                       "resultat propre du type non-code requis", snapshot)
    if _verdict(snapshot, "ok", revision) is None:
        return _refuse("completion-review-missing",
                       "revue applicable absente ou non ok sur la revision",
                       snapshot)
    return _accept(command, ("outcome",))


# --------------------------------------------------------------------------
# registre fini
# --------------------------------------------------------------------------

_SPECS = (
    GuardSpec("planning", "plan natif du lot (titre, type installe)",
              (), (), (), _check_planning),
    GuardSpec("start_work", "prise en charge : assigne reel",
              (), (), (), _check_start_work),
    GuardSpec("review_requested",
              "gel de la tete et relecteur designe independant de l'auteur",
              ("milestones",), ("head",), (), _check_review_requested),
    GuardSpec("qa_started", "relecture engagee sur la revision gelee",
              ("milestones",), (), ("head",), _check_qa_started),
    GuardSpec("qa_repair", "verdict bloque et reparation encore permise",
              ("milestones",), (), ("head",), _check_qa_repair),
    GuardSpec("merged_and_staging_deployed",
              "fusion reelle et observation staging deployee de la meme revision",
              ("milestones", "receipts", "observations"),
              ("merge", "staging"), (), _check_merged_and_staging_deployed),
    GuardSpec("staging_pass", "observation staging pass de la revision",
              ("receipts", "observations"), ("staging",), (),
              _check_staging_pass),
    GuardSpec("staging_failure", "observation staging fail et reparation permise",
              ("receipts", "observations"), ("staging",), (),
              _check_staging_failure),
    GuardSpec("preprod_deployed", "observation preprod deployee de la revision",
              ("receipts", "observations"), ("preprod",), (),
              _check_preprod_deployed),
    GuardSpec("preprod_pass", "observation preprod pass de la revision",
              ("receipts", "observations"), ("preprod",), (),
              _check_preprod_pass),
    GuardSpec("preprod_failure", "observation preprod fail et reparation permise",
              ("receipts", "observations"), ("preprod",), (),
              _check_preprod_failure),
    GuardSpec("repair_started", "reparation engagee sur un echec observe",
              ("receipts", "observations"), ("failure",), (),
              _check_repair_started),
    GuardSpec("stop_work", "arret explicite (abandon/supersession)",
              (), ("reason",), ("superseded_by",), _check_stop_work),
    GuardSpec("work_completed", "completude par type sur preuves reelles",
              ("deliveries", "milestones"), (), ("delivery", "outcome", "pr"),
              _check_work_completed),
)

_SPECS_BY_NAME = {spec.name: spec for spec in _SPECS}


# --------------------------------------------------------------------------
# API pure
# --------------------------------------------------------------------------

def _command_problem(command: Any) -> str:
    if not isinstance(command, Command):
        return "commande absente"
    if not _text(command.command_id):
        return "command_id requis"
    try:
        uuid.UUID(command.command_id)
    except (ValueError, AttributeError, TypeError):
        return "command_id n'est pas un UUID"
    action = _text(command.action) or "advance"
    if action not in ACTIONS:
        return "action %r hors de %s" % (command.action, ", ".join(ACTIONS))
    if action == "advance":
        if not _text(command.guard):
            return "garde requise"
        if not _text(command.from_stage) or not _text(command.to_stage):
            return "etapes from/to requises"
        if command.from_stage == command.to_stage:
            return "etape inchangee"
        if _text(command.hold):
            return "une avance ne porte pas de retenue"
    elif action == "hold":
        if _text(command.hold) not in HOLDS:
            return "retenue %r hors de %s" % (command.hold, ", ".join(HOLDS))
    elif _text(command.hold):
        return "une reprise ne porte pas de retenue"
    refs = command.references or {}
    if not isinstance(refs, Mapping):
        return "references: objet attendu"
    for key, value in refs.items():
        if not _text(key):
            return "reference sans nom"
        if isinstance(value, bool):
            return "reference %r: un booleen client ne vaut pas une preuve" % key
        if not isinstance(value, (str, int)):
            return "reference %r: chaine ou entier attendu" % key
        if isinstance(value, str) and not value.strip():
            return "reference %r vide" % key
    return ""


def _accept_hold(snapshot: Snapshot, references: tuple = ()) -> Decision:
    """Une retenue/reprise acceptee garde l'etape courante."""
    return Decision(True, "accepted", "", snapshot.workflow_stage,
                    tuple(references))


def _verify_hold_action(command: Command, snapshot: Snapshot) -> Decision:
    """Entree en retenue ou reprise : jamais une avance deguisee."""
    if _closed(snapshot):
        return _refuse("terminal-reopening",
                       "lot terminal : aucune reouverture", snapshot)
    state = _text(snapshot.item.get("state"))
    if _text(command.action) == "release":
        if state not in HOLDS:
            return _refuse("not-held",
                           "lot %s : aucune retenue a lever"
                           % (state or "sans etat"), snapshot)
        return _accept_hold(snapshot)
    hold = _text(command.hold)
    if state == hold:
        return _refuse("hold-redundant",
                       "lot deja en retenue %s" % hold, snapshot)
    if hold == "waiting_human":
        if "decisions" not in set(snapshot.sources):
            return _refuse("source-unavailable",
                           "collection decisions non collectee", snapshot)
        if max(_as_int(snapshot.decisions_pending), 0) < 1:
            return _refuse("hold-no-decision",
                           "waiting_human sans decision native en attente",
                           snapshot)
    return _accept_hold(snapshot)


def verify(command: Command, snapshot: Snapshot) -> Decision:
    """Le verdict de C pour une commande, sur des preuves reelles. Sans exception.

    L'ordre est celui des portes : forme de la commande, lot, revision Process,
    puis selon l'action : une avance (etape coherence avec la ligne verrouillee,
    terminal, retenue en cours, type, sources, references, garde) ou une
    retenue/reprise (`hold`/`release`). La version du journal
    (`expected_version`) n'est pas lue ici : elle appartient a B.
    """
    if not isinstance(snapshot, Snapshot):
        return Decision(False, "command-invalid", "snapshot absent")
    problem = _command_problem(command)
    if problem:
        return Decision(False, "command-invalid", problem)
    if not snapshot.item:
        return _refuse("item-missing", "snapshot sans lot", snapshot)
    if _as_int(snapshot.item.get("id")) != command.work_item_id:
        return _refuse("item-mismatch", "le snapshot ne porte pas ce lot",
                       snapshot)
    if not _text(snapshot.workflow_ref):
        return _refuse("workflow-absent",
                       "projet sans workflow : aucune transition C", snapshot)
    if (_text(command.action) or "advance") != "advance":
        return _verify_hold_action(command, snapshot)
    if command.from_stage != snapshot.workflow_stage:
        return _refuse("stage-stale",
                       "etape de depart %r, etape native %r"
                       % (command.from_stage, snapshot.workflow_stage), snapshot)
    if _closed(snapshot):
        return _refuse("terminal-reopening",
                       "lot terminal : aucune reouverture", snapshot)
    if _text(snapshot.item.get("state")) in HOLDS:
        return _refuse("hold-advance",
                       "lot en retenue %s : une avance doit passer par une "
                       "reprise (release)" % snapshot.item.get("state"),
                       snapshot)
    spec = _SPECS_BY_NAME.get(command.guard)
    if spec is None:
        return Decision(False, "guard-unsupported",
                        "garde %r hors du registre fini" % command.guard)
    if _text(snapshot.item.get("type")) not in INSTALLED_TYPES:
        return _refuse("type-unsupported",
                       "type %r non installe" % snapshot.item.get("type"),
                       snapshot)
    missing_sources = [name for name in spec.sources
                       if name not in set(snapshot.sources)]
    if missing_sources:
        return _refuse("source-unavailable",
                       "collections non collectees: %s"
                       % ", ".join(missing_sources), snapshot)
    refs = command.references or {}
    unknown = [key for key in refs if key not in spec.required + spec.optional]
    if unknown:
        return _refuse("evidence-invalid",
                       "references inconnues: %s" % ", ".join(sorted(unknown)),
                       snapshot)
    missing = [key for key in spec.required if not _present(refs.get(key))]
    if missing:
        return _refuse("evidence-missing",
                       "references requises absentes: %s"
                       % ", ".join(missing), snapshot)
    return spec.check(command, snapshot)


def verify_hold(hold: str, snapshot: Snapshot) -> Decision:
    """Une retenue native : `blocked` retient, `waiting_human` exige une decision."""
    if not isinstance(snapshot, Snapshot):
        return Decision(False, "hold-unsupported", "snapshot absent")
    if hold not in HOLDS:
        return Decision(False, "hold-unsupported",
                        "retenue %r hors du registre" % hold,
                        snapshot.workflow_stage)
    if hold == "waiting_human":
        if "decisions" not in set(snapshot.sources):
            return _refuse("source-unavailable",
                           "collection decisions non collectee", snapshot)
        if max(_as_int(snapshot.decisions_pending), 0) < 1:
            return _refuse("hold-no-decision",
                           "waiting_human sans decision native en attente",
                           snapshot)
    return Decision(True, "accepted", "", snapshot.workflow_stage)


def capabilities() -> tuple[Capability, ...]:
    """La matrice de capacite : garde, sources exigees, references."""
    return tuple(Capability(spec.name, spec.purpose, spec.sources,
                            spec.required + spec.optional) for spec in _SPECS)


def completion_policy(kind: str) -> CompletionPolicy:
    """La politique de completion d'un type installe.

    Les types de code exigent une livraison/acceptation reelle et la revue
    applicable ; les autres leur propre resultat et la revue applicable. Aucun
    type ne complete sur une PR de promotion seule.
    """
    if kind in CODE_TYPES:
        return CompletionPolicy(kind, True, True, True,
                                "code : livraison/acceptation reelle et revue")
    if kind in INSTALLED_TYPES:
        return CompletionPolicy(kind, False, False, True,
                                "non-code : resultat propre et revue applicable ; "
                                "aucun pouvoir de gouvernance nouveau")
    raise KeyError(kind)


# --------------------------------------------------------------------------
# seam B (#1922, process_registry) : l'appelable serveur installe par garde
# --------------------------------------------------------------------------

def context_command(context: Any) -> Command:
    """La `Command` de B -> la commande pure de C (references = evidence)."""
    raw = getattr(context, "command", None)
    authority = getattr(raw, "authority", None)
    references = getattr(raw, "evidence", None) or {}
    return Command(
        work_item_id=_as_int(getattr(raw, "work_item_id", 0)),
        command_id=_text(getattr(raw, "command_id", "")),
        guard=_text(getattr(raw, "guard", "")),
        from_stage=_text(getattr(context, "from_stage", ""))
        or _text(getattr(raw, "from_stage", "")),
        to_stage=_text(getattr(context, "to_stage", ""))
        or _text(getattr(raw, "to_stage", "")),
        actor=_text(getattr(authority, "actor", "")),
        source=_text(getattr(authority, "source", "")),
        action=_text(getattr(raw, "action", "")) or "advance",
        hold=_text(getattr(raw, "hold", "")),
        references=dict(references) if isinstance(references, Mapping) else {},
    )


def _context_sources(context: Any) -> Mapping:
    """Les sources lues par B : attribut `sources`, lecteur du contexte, ou
    `read_sources` publie par `process_registry`. Aucune lecture SQL ici."""
    sources = getattr(context, "sources", None)
    if isinstance(sources, Mapping):
        return sources
    reader = getattr(context, "read_sources", None)
    if callable(reader):
        return dict(reader() or {})
    try:
        from . import process_registry as registry
    except ImportError:  # pragma: no cover - B pas encore fusionne
        return {}
    reader = getattr(registry, "read_sources", None)
    if callable(reader):
        return dict(reader(context) or {})
    return {}


def context_snapshot(context: Any) -> Snapshot:
    """Le `VerificationContext` de B -> le snapshot pur de C."""
    item = dict(getattr(context, "work_item", None) or {})
    sources = _context_sources(context) or {}
    milestones = tuple(sources.get("milestones", ()) or ())
    decisions = tuple(sources.get("decisions", ()) or ())
    return Snapshot(
        item=item,
        workflow_ref=_text(item.get("workflow_ref")),
        workflow_stage=_text(item.get("workflow_stage")),
        workflow_version=max(_as_int(item.get("workflow_version")), 0),
        revision=revision_of(milestones),
        milestones=milestones,
        receipts=tuple(sources.get("receipts", ()) or ()),
        observations=tuple(sources.get("observations", ()) or ()),
        decisions_pending=len(decisions),
        deliveries=tuple(sources.get("deliveries", ()) or ()),
        sources=tuple(name for name in SOURCE_COLLECTIONS if name in sources),
    )


_SERVER_VERIFIERS: dict[tuple, Callable] = {}


def server_verifier(guard: str, verification_cls: Any = None) -> Callable:
    """L'appelable serveur d'une garde : `VerificationContext -> Verification`.

    Sert aussi pour le nom reserve `HOLD_VERIFIER` (`hold`/`release`) : la
    decision se prend sur l'`action` portee par la commande. Meme objet rendu
    pour un meme couple (nom, classe de `Verification`) : `install_verifier` de
    B est idempotent pour un objet identique.
    """
    key = (guard, verification_cls)
    existing = _SERVER_VERIFIERS.get(key)
    if existing is not None:
        return existing

    def check(context: Any) -> Any:
        decision = verify(context_command(context), context_snapshot(context))
        cls = verification_cls
        if cls is None:
            from . import process_registry as registry
            cls = registry.Verification
        return cls(ok=decision.accepted,
                   reason=decision.message or decision.code,
                   facts={"code": decision.code, "stage": decision.stage,
                          "references": list(decision.references)})

    _SERVER_VERIFIERS[key] = check
    return check


def install_verifiers(registry: Any = None) -> tuple[str, ...]:
    """Installe les appelables serveur dans `process_registry` (B) : les 14
    gardes du registre fini, puis `RESERVED_VERIFIERS` (le nom reserve
    `hold`, pour les actions `hold` et `release`).

    Sans `registry`, importe `ameesh.process_registry` ; avec, utilise l'objet
    fourni (tests, autre hote). Rend les noms installes, dans cet ordre. Aucun
    verificateur par defaut cote B : sans cet appel, une avance, une retenue ou
    une reprise reste `verifier-uninstalled`.
    """
    if registry is None:
        from . import process_registry as registry
    verification_cls = getattr(registry, "Verification")
    names = GUARD_NAMES + RESERVED_VERIFIERS
    for name in names:
        registry.install_verifier(name, server_verifier(name, verification_cls))
    return names
