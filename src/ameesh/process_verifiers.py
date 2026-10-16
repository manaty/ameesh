# SPDX-License-Identifier: AGPL-3.0-only
"""Workflows Process (#1915, tranche C) : verificateurs finis sur preuves reelles.

Le composant C decide, pour une commande de transition, si les **preuves
reelles** exigees par la garde et par le type du lot sont reunies. Il est pur :
il ne lit ni n'ecrit aucune base, n'execute aucune expression venue d'un canon
et ne modifie pas le compilateur A (#1918, gele). B (#1922) l'appelle **dans sa
transaction**, apres avoir verrouille le lot, recontrole l'acteur, la source et
l'epoque d'autorite et construit un `Snapshot` depuis ses tables et sources
reelles ; C rend une `Decision`, B persiste etape/version/recu.

Regles de decision :

* la requete ne porte que des **references** (sha, execution, deploiement,
  decision, lot) : un booleen client, une valeur d'un autre type ou une cle
  inconnue sont des refus (`evidence-invalid`) ; jamais un accord par defaut ;
* une collection exigee par la garde absente du snapshot est un refus
  (`source-unavailable`) : un verificateur indisponible refuse sa transition,
  il ne l'accepte pas en silence ;
* le snapshot est la seule source de verite ; C ne relit pas la requete pour
  decider au-dela des references qu'elle nomme.

Registre fini (contrat #1379, commentaire 6207021753) : `GUARD_NAMES`. Types
installes opt-in : `INSTALLED_TYPES` ; les types de code (`CODE_TYPES`)
exigent une livraison/acceptation reelle, les autres leur propre resultat et
la revue applicable. `promotion` n'accorde aucun pouvoir de gouvernance et
l'ouverture d'une PR de promotion seule ne complete jamais un lot.

Holds : `blocked` retient l'etape et la revision ; `waiting_human` exige une
decision native reellement en attente (`decisions_pending >= 1`), jamais un
simple drapeau. Les reparations sont bornees par `MAX_REPAIRS` ; aucune
reouverture terminale.

Sources d'un snapshot (`Snapshot.sources`) : `milestones`, `events`, `ci`,
`decisions`, `authority`, `deliveries`, `deployments`. `item`, `workflow_*` et
`revision` sont des scalaires du snapshot, pas des collections.
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

#: Reparations bornees (compteur natif `loops`), comme la machine a etats.
MAX_REPAIRS = 2

#: Retenues natives : elles gardent l'etape et la revision.
HOLDS = ("blocked", "waiting_human")

#: Collections qu'un snapshot peut declarer avoir collectees.
SOURCE_COLLECTIONS = (
    "milestones",
    "events",
    "ci",
    "decisions",
    "authority",
    "deliveries",
    "deployments",
)

#: Codes de refus stables (le code d'acceptation est `accepted`).
REFUSAL_CODES = (
    "authority-refused",
    "command-invalid",
    "completion-evidence-missing",
    "completion-not-accepted",
    "completion-review-missing",
    "deployment-missing",
    "deployment-result-mismatch",
    "deployment-source-refused",
    "evidence-invalid",
    "evidence-missing",
    "failure-missing",
    "freeze-missing",
    "guard-unsupported",
    "hold-no-decision",
    "hold-unsupported",
    "item-mismatch",
    "item-missing",
    "merge-missing",
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
    "version-stale",
    "workflow-absent",
)


# --------------------------------------------------------------------------
# formes echangees avec B
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """Une commande de transition : intention + references, jamais un verdict.

    `references` ne porte que des chaines (sha, execution, deploiement,
    decision) ou des entiers (numero de lot) ; tout le reste est refuse.
    """

    work_item_id: int
    command_id: str
    guard: str
    from_stage: str
    to_stage: str
    expected_version: int
    actor: str = ""
    source: str = ""
    references: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    """Etat reel lu par B dans sa transaction, et collections collectees.

    `revision` est la tete de travail reelle du lot (gel, fusion) ; les
    enregistrements de deploiement doivent la nommer. `sources` dit ce que B a
    reellement collecte : une garde dont une collection manque refuse.
    """

    item: Mapping[str, Any] = field(default_factory=dict)
    workflow_ref: str = ""
    workflow_stage: str = ""
    workflow_version: int = 0
    revision: str = ""
    milestones: tuple[Mapping[str, Any], ...] = ()
    events: tuple[Mapping[str, Any], ...] = ()
    ci: tuple[Mapping[str, Any], ...] = ()
    decisions_pending: int = 0
    authority: Mapping[str, Any] = field(default_factory=dict)
    deliveries: tuple[Mapping[str, Any], ...] = ()
    deployments: tuple[Mapping[str, Any], ...] = ()
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """Verdict de C : accepte, ou refuse avec un code stable et un message."""

    accepted: bool
    code: str
    message: str = ""
    stage: str = ""
    version: int = 0
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


def _ref_int(command: Command, key: str) -> int:
    return _as_int((command.references or {}).get(key))


def _loops(snapshot: Snapshot) -> int:
    return max(_as_int(snapshot.item.get("loops")), 0)


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


def _deployment(snapshot: Snapshot, env: str, *, ident: str = "",
                sha: str = "") -> Mapping | None:
    for row in snapshot.deployments:
        if _text(row.get("env")) != env:
            continue
        if ident and _text(row.get("id")) != ident:
            continue
        if sha and _text(row.get("sha")) != sha:
            continue
        return row
    return None


def _failed_deployment(snapshot: Snapshot, ident: str = "") -> Mapping | None:
    for env in ("staging", "preprod"):
        row = _deployment(snapshot, env, ident=ident)
        if row is not None and _text(row.get("conclusion")) == "fail":
            return row
    return None


def _delivery(snapshot: Snapshot, ident: str = "") -> Mapping | None:
    for row in snapshot.deliveries:
        if ident and _text(row.get("id")) != ident:
            continue
        return row
    return None


def _deploy_source_allowed(snapshot: Snapshot, deployment: Mapping) -> bool:
    allowed = snapshot.authority.get("deploy_sources") if snapshot.authority else None
    if not allowed:
        return False
    source = _text(deployment.get("source"))
    return bool(source) and source in {_text(entry) for entry in allowed}


def _closed(snapshot: Snapshot) -> bool:
    if snapshot.item.get("closed_at"):
        return True
    return _text(snapshot.item.get("state")) == "closed"


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------

def _accept(command: Command, references: tuple = ()) -> Decision:
    return Decision(True, "accepted", "", command.to_stage,
                    command.expected_version + 1, tuple(references))


def _refuse(code: str, message: str, snapshot: Snapshot) -> Decision:
    return Decision(False, code, message, snapshot.workflow_stage,
                    _as_int(snapshot.workflow_version))


def _no_deploy_source(snapshot: Snapshot) -> Decision:
    return _refuse("deployment-source-refused",
                   "source de deploiement absente ou non autorisee", snapshot)


def _revision(snapshot: Snapshot) -> str:
    return _text(snapshot.revision)


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
                       "aucun gel de la revision %s" % (head or "(absente)"),
                       snapshot)
    if not _text(snapshot.item.get("reviewer")):
        return _refuse("reviewer-missing", "aucun relecteur designe", snapshot)
    return _accept(command, ("head",) if head else ())


def _check_qa_repair(command: Command, snapshot: Snapshot) -> Decision:
    sha = _ref(command, "head") or _revision(snapshot)
    if not sha:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    if _verdict(snapshot, "blocked", sha) is None:
        return _refuse("verdict-missing",
                       "aucun verdict bloque sur la revision %s"
                       % (sha or "(absente)"), snapshot)
    if _loops(snapshot) >= MAX_REPAIRS:
        return _refuse("repair-bound",
                       "reparations deja au maximum (%d)" % MAX_REPAIRS, snapshot)
    return _accept(command, ("head",) if _ref(command, "head") else ())


def _check_merged_and_staging_deployed(command: Command,
                                        snapshot: Snapshot) -> Decision:
    merge = _ref(command, "merge")
    if _milestone(snapshot, "merged", merge) is None:
        return _refuse("merge-missing",
                       "aucune fusion reelle de %s" % (merge or "(absente)"),
                       snapshot)
    ident = _ref(command, "staging")
    deployment = _deployment(snapshot, "staging", ident=ident, sha=merge)
    if deployment is None:
        return _refuse("deployment-missing",
                       "aucun deploiement staging de la revision fusionnee",
                       snapshot)
    if not _deploy_source_allowed(snapshot, deployment):
        return _no_deploy_source(snapshot)
    return _accept(command, ("merge", "staging"))


def _environment_result(command: Command, snapshot: Snapshot, env: str,
                        ref_key: str, expected: str, *, repair: bool) -> Decision:
    ident = _ref(command, ref_key)
    deployment = _deployment(snapshot, env, ident=ident)
    if deployment is None:
        return _refuse("deployment-missing",
                       "aucun deploiement %s %s" % (env, ident or "de la revision"),
                       snapshot)
    revision = _revision(snapshot)
    if not revision:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    if _text(deployment.get("sha")) != revision:
        return _refuse("revision-mismatch",
                       "deploiement %s sur une autre revision" % env, snapshot)
    if not _deploy_source_allowed(snapshot, deployment):
        return _no_deploy_source(snapshot)
    if _text(deployment.get("conclusion")) != expected:
        return _refuse("deployment-result-mismatch",
                       "deploiement %s non %s" % (env, expected), snapshot)
    if repair and _loops(snapshot) >= MAX_REPAIRS:
        return _refuse("repair-bound",
                       "reparations deja au maximum (%d)" % MAX_REPAIRS, snapshot)
    return _accept(command, (ref_key,))


def _check_staging_pass(command: Command, snapshot: Snapshot) -> Decision:
    return _environment_result(command, snapshot, "staging", "staging", "pass",
                               repair=False)


def _check_staging_failure(command: Command, snapshot: Snapshot) -> Decision:
    return _environment_result(command, snapshot, "staging", "staging", "fail",
                               repair=True)


def _check_preprod_pass(command: Command, snapshot: Snapshot) -> Decision:
    return _environment_result(command, snapshot, "preprod", "preprod", "pass",
                               repair=False)


def _check_preprod_failure(command: Command, snapshot: Snapshot) -> Decision:
    return _environment_result(command, snapshot, "preprod", "preprod", "fail",
                               repair=True)


def _check_preprod_deployed(command: Command, snapshot: Snapshot) -> Decision:
    ident = _ref(command, "preprod")
    deployment = _deployment(snapshot, "preprod", ident=ident)
    if deployment is None:
        return _refuse("deployment-missing",
                       "aucun deploiement preprod %s" % (ident or "de la revision"),
                       snapshot)
    revision = _revision(snapshot)
    if not revision:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    if _text(deployment.get("sha")) != revision:
        return _refuse("revision-mismatch",
                       "deploiement preprod sur une autre revision", snapshot)
    if not _deploy_source_allowed(snapshot, deployment):
        return _no_deploy_source(snapshot)
    return _accept(command, ("preprod",))


def _check_repair_started(command: Command, snapshot: Snapshot) -> Decision:
    ident = _ref(command, "failure")
    failure = _failed_deployment(snapshot, ident)
    if failure is None:
        return _refuse("failure-missing",
                       "aucun echec enregistre %s" % (ident or "sur la revision"),
                       snapshot)
    revision = _revision(snapshot)
    if not revision:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    if _text(failure.get("sha")) != revision:
        return _refuse("revision-mismatch",
                       "echec enregistre sur une autre revision", snapshot)
    if not _deploy_source_allowed(snapshot, failure):
        return _no_deploy_source(snapshot)
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
    authority = snapshot.authority or {}
    if _text(authority.get("actor")) != command.actor:
        return _refuse("authority-refused",
                       "acteur different de l'autorite recontrolee", snapshot)
    if not authority.get("can_stop"):
        return _refuse("authority-refused",
                       "cet acteur n'a pas le droit d'arreter le lot", snapshot)
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
              "fusion reelle et deploiement staging de la meme revision",
              ("milestones", "deployments", "authority"),
              ("merge",), ("staging",), _check_merged_and_staging_deployed),
    GuardSpec("staging_pass", "passage staging reel de la revision",
              ("deployments", "authority"), ("staging",), (),
              _check_staging_pass),
    GuardSpec("staging_failure", "echec staging reel et reparation permise",
              ("deployments", "authority"), ("staging",), (),
              _check_staging_failure),
    GuardSpec("preprod_deployed", "deploiement preprod de la revision",
              ("deployments", "authority"), ("preprod",), (),
              _check_preprod_deployed),
    GuardSpec("preprod_pass", "passage preprod reel de la revision",
              ("deployments", "authority"), ("preprod",), (),
              _check_preprod_pass),
    GuardSpec("preprod_failure", "echec preprod reel et reparation permise",
              ("deployments", "authority"), ("preprod",), (),
              _check_preprod_failure),
    GuardSpec("repair_started", "reparation engagee sur un echec enregistre",
              ("deployments", "authority"), (), ("failure",),
              _check_repair_started),
    GuardSpec("stop_work", "arret explicite par autorite (abandon/supersession)",
              ("authority",), ("reason",), ("superseded_by",), _check_stop_work),
    GuardSpec("work_completed", "completude par type sur preuves reelles",
              ("deliveries", "milestones"), (), ("delivery", "outcome", "pr"),
              _check_work_completed),
)

_SPECS_BY_NAME = {spec.name: spec for spec in _SPECS}


# --------------------------------------------------------------------------
# API
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
    if not _text(command.actor):
        return "acteur requis"
    if not _text(command.source):
        return "source requise"
    if isinstance(command.expected_version, bool) or not isinstance(
            command.expected_version, int) or command.expected_version < 0:
        return "expected_version: entier >= 0 attendu"
    if not _text(command.from_stage) or not _text(command.to_stage):
        return "etapes from/to requises"
    if command.from_stage == command.to_stage:
        return "etape inchangee"
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


def verify(command: Command, snapshot: Snapshot) -> Decision:
    """Le verdict de C pour une commande, sur un snapshot reel. Jamais d'exception.

    L'ordre est celui des portes : forme de la commande, garde du registre,
    lot, workflow, version/etape (CAS recontrole), terminal, type, sources,
    references, puis la garde elle-meme.
    """
    if not isinstance(snapshot, Snapshot):
        return Decision(False, "command-invalid", "snapshot absent")
    problem = _command_problem(command)
    if problem:
        return Decision(False, "command-invalid", problem)
    spec = _SPECS_BY_NAME.get(command.guard)
    if spec is None:
        return Decision(False, "guard-unsupported",
                        "garde %r hors du registre fini" % command.guard)
    if not snapshot.item:
        return _refuse("item-missing", "snapshot sans lot", snapshot)
    if _as_int(snapshot.item.get("id")) != command.work_item_id:
        return _refuse("item-mismatch", "le snapshot ne porte pas ce lot",
                       snapshot)
    if not _text(snapshot.workflow_ref):
        return _refuse("workflow-absent",
                       "projet sans workflow : aucune transition C", snapshot)
    if command.expected_version != _as_int(snapshot.workflow_version):
        return _refuse("version-stale",
                       "expected_version %s, version native %s"
                       % (command.expected_version, snapshot.workflow_version),
                       snapshot)
    if command.from_stage != snapshot.workflow_stage:
        return _refuse("stage-stale",
                       "etape de depart %r, etape native %r"
                       % (command.from_stage, snapshot.workflow_stage), snapshot)
    if _closed(snapshot):
        return _refuse("terminal-reopening",
                       "lot terminal : aucune reouverture", snapshot)
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
                        snapshot.workflow_stage, _as_int(snapshot.workflow_version))
    if hold == "waiting_human":
        if "decisions" not in set(snapshot.sources):
            return _refuse("source-unavailable",
                           "collection decisions non collectee", snapshot)
        if max(_as_int(snapshot.decisions_pending), 0) < 1:
            return _refuse("hold-no-decision",
                           "waiting_human sans decision native en attente",
                           snapshot)
    return Decision(True, "accepted", "", snapshot.workflow_stage,
                    _as_int(snapshot.workflow_version))


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
