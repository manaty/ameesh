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
  observation referencee mais non installee refuse (`source-unavailable`) ;
* la **tete relue** (dernier gel, `reviewed_head_of`) et la **revision
  courante** (dernier jalon de revision : gel, fusion ou livraison,
  `revision_of`) sont deux valeurs distinctes : une revue ne vaut que sur la
  tete relue courante, un deploiement que sur la revision courante ; toute
  reference a une tete ou une fusion depassee refuse (`revision-mismatch`) ;
* la completion d'un type qui exige une revue (tous les types installes)
  **relie la livraison a la tete relue** qui a ete fusionnee/livree
  (`reviewed_head_for`) : une livraison acceptee sans verdict ok applicable ne
  complete pas (`completion-review-missing`). Pour un type non-code, le
  `outcome` est un **libelle client** non interpretable (aucune collection de
  resultats cote B) : il est exige non vide, et la completion reste reliee a la
  revue de la tete relue (N2 de la revue #1924).

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
`receipts`, `observations`, `decisions`, `deliveries`, `approvals` (#4006 :
projection locale GitHub, presente seulement si elle est fraiche).

Matrice de capacite : `capabilities()` distingue une garde **connue** du
contrat de son **installation** reelle dans le registre de B
(`installed_verifiers()`), des **sources reellement disponibles** et des
**types supportes** ; `capability_report()` publie le sous-ensemble executable
et le refus explicite de chaque autre (`verifier-uninstalled`,
`source-unavailable`, `type-unsupported`, `guard-unsupported`). Rien n'est
declare executable sans preuve : ni source simulee, ni installation supposee.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import re
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

#: Actions du registre B : une avance gardee, une retenue, sa reprise, un
#: transfert de revision Process (`transfer_revision`, #4007).
ACTIONS = ("advance", "hold", "release", "transfer")

#: Nom reserve du verificateur des retenues/reprises : B l'appelle pour
#: `action` `hold` et `release`, comme `revision_transfer` pour un transfert.
HOLD_VERIFIER = "hold"

#: #4007 : nom reserve du verificateur de transfert de revision Process ; il
#: vaut `process_registry.TRANSFER_VERIFIER` (B l'appelle depuis
#: `transfer_revision`, pour une premiere adoption comme pour un transfert).
TRANSFER_VERIFIER = "revision_transfer"

#: Verificateurs reserves installes par C en plus des 14 gardes.
RESERVED_VERIFIERS = (HOLD_VERIFIER, TRANSFER_VERIFIER)

#: #4007 : contrat publie du transfert. La source d'approbation et la version
#: du contrat sont des constantes du module, jamais des parametres : aucun
#: appelant (test compris) ne remplace la source ni n'abaisse la regle.
TRANSFER_CONTRACT = "revision-transfer/1"
#: Seule source d'approbation admise : la projection GitHub de l'issue source
#: (#4006) ; `api` n'est jamais une autorite.
TRANSFER_APPROVAL_SOURCE = "github"
#: References exigees d'un transfert : l'approbation citee, le lot et le
#: projet vises (lies a la ligne verrouillee par B).
TRANSFER_REFERENCES = ("approval", "lot", "project")

#: Le registre de B (#1922) refuse lui-meme une garde sans verificateur avec ce
#: code : la matrice de capacite le publie, mais C ne le rend jamais dans un
#: verdict (B refuse avant de l'appeler).
HOST_REFUSAL_CODES = ("verifier-uninstalled",)

#: Matrice des noms reserves : but, sources exigees, sources conditionnelles.
#: `decisions` n'est exige que pour `waiting_human` (d'ou la distinction avec
#: les sources exigees, qui valent pour toute entree du verificateur).
_RESERVED_SPECS = {
    HOLD_VERIFIER: (
        "retenues et reprises (hold/release) ; waiting_human exige une "
        "decision native reellement en attente",
        (),
        ("decisions",),
    ),
    TRANSFER_VERIFIER: (
        "adoption/transfert de revision Process : approbation courante "
        "de la revision exacte, liee au lot et au projet, approbateur "
        "independant",
        ("approvals",),
        (),
    ),
}

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
    "approvals",
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
    #: #4007 : revision Process visee par un transfert (`action="transfer"`) ;
    #: vide pour les autres actions, qui restent sur la revision du lot.
    workflow_ref: str = ""
    workflow_version: int = 0


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
    #: #4006 : projection locale GitHub, `{"projection": ..., "records":
    #: [...]}` ; vide quand B n'a pas collecte de projection fraiche.
    approvals: Mapping[str, Any] = field(default_factory=dict)
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """Verdict de C : accepte, ou refuse avec un code stable et un message."""

    accepted: bool
    code: str
    message: str = ""
    stage: str = ""
    references: tuple[str, ...] = ()
    #: #4007 : le code du pont d'approbation (#4006) derriere un refus de
    #: transfert (`approval-revoked`, ...) ; jamais un code Process nouveau.
    detail: str = ""
    #: #4007 : la preuve retenue par un transfert accepte, recopiee dans le
    #: recu de B (`verification.facts.proof`).
    proof: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Capability:
    """Une garde du registre fini : connue, sondee, executable ou refusee.

    Quatre etats distincts, jamais confondus :

    * `known` : la garde appartient au contrat fini (les 14 gardes de A) ;
      `reserved` : nom reserve installe par C hors de ces 14 (`hold`) ;
    * `installed` : etat reel du registre serveur de B si sonde (`None` = non
      sonde) ;
    * `available` / `missing` : sources exigees **reellement** disponibles ou
      absentes ; `sources_probed` dit si un jeu de sources a ete fourni (sans
      lui, aucune source exigee n'est reputee disponible) ;
    * `conditional_sources` : sources exigees seulement pour certaines entrees
      (ex. `decisions` pour `waiting_human`), jamais comptees dans `missing`.

    Fail-closed : `executable` n'est vrai que si l'installation est **prouvee**
    et, pour une garde qui exige des sources, que ces sources ont ete **lues**
    et sont disponibles ; sinon `refusal` nomme le refus exact.
    """

    guard: str
    purpose: str
    sources: tuple[str, ...]
    references: tuple[str, ...]
    known: bool = True
    reserved: bool = False
    installed: bool | None = None
    available: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    conditional_sources: tuple[str, ...] = ()
    sources_probed: bool = False

    @property
    def executable(self) -> bool:
        """Vrai seulement si l'installation est prouvee et, quand la garde
        exige des sources, qu'elles ont ete lues et sont disponibles (une
        source non lue n'est jamais reputee disponible)."""
        if self.installed is not True:
            return False
        if not self.sources:
            return True
        return self.sources_probed and not self.missing

    @property
    def refusal(self) -> str:
        """Le refus explicite de cette garde (`""` si elle est executable)."""
        if not self.known:
            return "guard-unsupported"
        if self.installed is not True:
            return HOST_REFUSAL_CODES[0]
        if self.sources and (not self.sources_probed or self.missing):
            return "source-unavailable"
        return ""


@dataclass(frozen=True)
class TypeCapability:
    """Un type installe et sa politique de completion ; `supported=False` est
    un refus explicite (`type-unsupported`), jamais une absence de reponse."""

    type: str
    supported: bool
    code: bool
    requires_delivery: bool
    requires_review: bool
    reason: str


@dataclass(frozen=True)
class CapabilityReport:
    """La matrice complete : gardes connues (sondees), noms reserves, types."""

    guards: tuple[Capability, ...]
    reserved: tuple[Capability, ...]
    types: tuple[TypeCapability, ...]

    def capability(self, name: str) -> Capability | None:
        """L'entree d'un nom connu (garde du contrat ou nom reserve)."""
        for cap in self.guards + self.reserved:
            if cap.guard == name:
                return cap
        return None

    def refusal(self, name: str) -> str:
        """`guard-unsupported` hors contrat, sinon le refus de la garde."""
        cap = self.capability(name)
        return "guard-unsupported" if cap is None else cap.refusal

    @property
    def executable_guards(self) -> tuple[str, ...]:
        """Le sous-ensemble executable : installe et sources presentes."""
        return tuple(cap.guard for cap in self.guards + self.reserved
                     if cap.executable)


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
    """Un entier sur, jamais une exception : -1 pour toute autre forme.

    ``str.isdigit`` est plus large que ``int`` : ``²`` le passe et ``int`` le
    refuse, et ``--3`` le passait par ``lstrip("-")`` alors que ``int`` le
    refuse aussi. Ces formes rendent -1 comme les autres entrees non entieres
    (post-fusion #1937) : une reference malformee doit produire un refus nomme
    (`evidence-invalid`), jamais une exception non attrapee.
    """
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return -1
    text = value.strip()
    digits = text[1:] if text.startswith("-") else text
    if not digits.isdigit():
        return -1
    try:
        return int(text)
    except ValueError:
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
    """La revision courante : les jalons reels priment la valeur declaree.

    Un snapshot dont les jalons sont collectes ne peut pas se declarer sur une
    autre revision que le dernier jalon reel (`revision-mismatch` ensuite) ;
    sans jalons, la revision declaree fait foi.
    """
    return revision_of(snapshot.milestones) or _text(snapshot.revision)


def _milestone(snapshot: Snapshot, kind: str, sha: str = "") -> Mapping | None:
    for row in snapshot.milestones:
        if _text(row.get("kind")) != kind:
            continue
        if sha and _text(row.get("sha")) != sha:
            continue
        return row
    return None


def _current_verdict(snapshot: Snapshot, sha: str) -> str:
    """Le **dernier** verdict de chaque acteur sur cette revision.

    Un verdict n'est une revue que s'il porte un acteur reel, distinct de
    l'assigne : l'auto-verdict de l'auteur (ok comme bloque) ne vaut pas
    revue, et un verdict sans acteur n'est pas attribuable. Le blocage d'un
    relecteur n'est leve que par un ok plus recent du meme relecteur ; le ok
    d'un autre ne le masque pas (N1 de la revue #1924, regle du registre
    #1756). Rend `"blocked"` si un blocage tient, sinon `"ok"` si une revue
    est ok, sinon `""`.
    """
    assignee = _text(snapshot.item.get("assignee"))
    latest: dict[str, str] = {}
    for row in snapshot.milestones or ():
        if (_text(row.get("kind")) != "verdict"
                or _text(row.get("sha")) != sha):
            continue
        actor = _text(row.get("actor"))
        if not actor or actor == assignee:
            continue
        latest[actor] = _text(row.get("verdict"))
    if "blocked" in latest.values():
        return "blocked"
    if "ok" in latest.values():
        return "ok"
    return ""


def _observation(snapshot: Snapshot, ident: str) -> Mapping | None:
    for row in snapshot.observations:
        if _text(row.get("id")) == ident:
            return row
    return None


def _receipt_facts(receipt: Mapping) -> Mapping:
    """Les faits structures d'un recu B (#1922).

    B projette `facts` (les faits du client) a cote du blob
    `process-receipt/1` garde sous `evidence` ; un recu brut non projete porte
    les memes faits dans `evidence.facts`. Rend un mapping vide si rien n'est
    lisible : aucun fait simule.
    """
    facts = receipt.get("facts")
    if isinstance(facts, Mapping):
        return facts
    evidence = receipt.get("evidence")
    if isinstance(evidence, Mapping):
        nested = evidence.get("facts")
        if isinstance(nested, Mapping):
            return nested
    return {}


def _witness(snapshot: Snapshot, observation_id: str) -> Mapping | None:
    """Le recu B accepte dont les faits referencent cette observation.

    Un recu explicitement non accepte ne prouve rien ; B ne projette que des
    recus acceptes (`command_id` non nul). Les faits sont lus dans la
    projection `facts` de B, jamais dans un `evidence.observation` direct
    (N7 de la revue #1924).
    """
    for receipt in snapshot.receipts:
        if receipt.get("accepted") is False:
            continue
        if _text(_receipt_facts(receipt).get("observation")) == observation_id:
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
    """Lot terminal : `closed` comme `promoted` (N5 de la revue #1924)."""
    if snapshot.item.get("closed_at"):
        return True
    return lifecycle.phase(snapshot.item) in (lifecycle.CLOSED, "promoted")


#: Jalons qui portent une revision reelle.
REVISION_KINDS = ("frozen", "merged", "delivered")


def revision_of(milestones) -> str:
    """La revision courante : le sha du **dernier** jalon de revision.

    L'ordre des jalons fait foi (id croissant) : un gel posterieur a une fusion
    est la nouvelle tete de travail, une fusion posterieure a un gel donne le
    commit fusionne/deploye. Un rang de genre ne prime jamais sur l'ordre : un
    gel ancien ne masque pas une fusion recente.
    """
    revision = ""
    for row in milestones or ():
        if (_text(row.get("kind")) in REVISION_KINDS
                and _text(row.get("sha"))):
            revision = _text(row.get("sha"))
    return revision


def reviewed_head_of(milestones) -> str:
    """La tete relue : le sha du dernier gel (jamais une fusion)."""
    head = ""
    for row in milestones or ():
        if _text(row.get("kind")) == "frozen" and _text(row.get("sha")):
            head = _text(row.get("sha"))
    return head


def merged_of(milestones) -> str:
    """Le dernier commit fusionne reel (sha), ou vide.

    #2317 (N5) : un jalon `merged` annulé par une correction `merged → build`
    (`annulled`, calculé par la source) ne compte pas — sa fusion n'est plus
    la revision livrée."""
    merged = ""
    for row in milestones or ():
        if row.get("annulled"):
            continue
        if _text(row.get("kind")) == "merged" and _text(row.get("sha")):
            merged = _text(row.get("sha"))
    return merged


def reviewed_head_for(milestones, revision: str) -> str:
    """La tete relue qui s'applique a une revision donnee.

    Le dernier gel anterieur a la fusion/livraison de cette revision est la
    tete jugee ; une revision encore gelee (aucune fusion/livraison
    posterieure) est sa propre tete relue ; sans lien prouvable, la revision
    elle-meme est exigee (fail-closed : la revue d'une autre tete ne vaut
    pas).
    """
    if not revision:
        return ""
    head = ""
    for row in milestones or ():
        kind = _text(row.get("kind"))
        sha = _text(row.get("sha"))
        if kind == "frozen" and sha:
            head = sha
        elif kind in ("merged", "delivered") and sha == revision:
            return head or revision
    return revision


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
    current = reviewed_head_of(snapshot.milestones)
    if head != current:
        return _refuse("revision-mismatch",
                       "gel %s depasse par la tete relue %s"
                       % (head, current or "(absente)"), snapshot)
    reviewer = _text(snapshot.item.get("reviewer"))
    if not reviewer:
        return _refuse("reviewer-missing", "aucun relecteur designe", snapshot)
    if reviewer == _text(snapshot.item.get("assignee")):
        return _refuse("reviewer-not-independent",
                       "relecteur et auteur sont le meme agent", snapshot)
    return _accept(command, ("head",))


def _check_qa_started(command: Command, snapshot: Snapshot) -> Decision:
    current = reviewed_head_of(snapshot.milestones)
    head = _ref(command, "head") or current or _revision(snapshot)
    if not head:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    if _milestone(snapshot, "frozen", head) is None:
        return _refuse("freeze-missing",
                       "aucun gel de la revision %s" % head, snapshot)
    if current and head != current:
        return _refuse("revision-mismatch",
                       "gel %s depasse par la tete relue %s"
                       % (head, current), snapshot)
    if not _text(snapshot.item.get("reviewer")):
        return _refuse("reviewer-missing", "aucun relecteur designe", snapshot)
    return _accept(command, ("head",) if _ref(command, "head") else ())


def _check_qa_repair(command: Command, snapshot: Snapshot) -> Decision:
    current = reviewed_head_of(snapshot.milestones)
    sha = _ref(command, "head") or current or _revision(snapshot)
    if not sha:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    # R2 de la revue #1924 : sans gel, la tete de la commande n'est comparable
    # a rien ; fail-closed comme qa_started.
    if _milestone(snapshot, "frozen", sha) is None:
        return _refuse("freeze-missing",
                       "aucun gel de la revision %s" % sha, snapshot)
    if current and sha != current:
        return _refuse("revision-mismatch",
                       "verdict de %s : la tete relue est %s"
                       % (sha, current), snapshot)
    if _current_verdict(snapshot, sha) != "blocked":
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
        # N4 de la revue #1924 : la collection est collectee (exigee par la
        # matrice), une observation absente est une preuve manquante, pas une
        # source indisponible.
        return _refuse("evidence-missing",
                       "observation %s inconnue de la collection collectee"
                       % (ident or "(absente)"), snapshot)
    # #4425 : une observation revoquee ou remplacee n'est jamais une preuve,
    # meme si un appelant a construit le snapshot sans passer par le lecteur
    # fail-closed de B. Un enregistrement sans etat (fixtures historiques) est
    # lu comme courant.
    state = _text(observation.get("state"))
    if state and state != "current":
        return _refuse("observation-result-mismatch",
                       "observation %s %s" % (ident, state), snapshot)
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
    current = _revision(snapshot)
    if merge != current:
        return _refuse("revision-mismatch",
                       "fusion %s depassee par %s"
                       % (merge, current or "(absente)"), snapshot)
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
    state = _text(failure.get("state"))
    if state and state != "current":
        return _refuse("observation-result-mismatch",
                       "echec %s %s" % (ident, state), snapshot)
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
    """Arret explicite : abandon, ou supersession par un lot distinct.

    N6 de la revue #1924 : C ne dispose d'aucune collection de lots, donc
    l'**existence** du lot remplacant n'est pas prouvable ici. Ce qui est
    lisible dans le snapshot est verifie : le remplacant est un identifiant de
    lot (entier positif, ou sa chaine) et ne peut pas etre le lot lui-meme
    (meme regle que `work.close` cote natif). Une collection
    `items` cote B permettra d'exiger l'existence reelle sans nouveau pouvoir
    de gouvernance.
    """
    reason = _ref(command, "reason")
    if reason not in ("abandoned", "superseded"):
        return _refuse("evidence-invalid",
                       "raison d'arret attendue: abandoned ou superseded", snapshot)
    if reason == "superseded":
        replaced_by = (command.references or {}).get("superseded_by")
        if not _present(replaced_by):
            return _refuse("evidence-missing",
                           "supersession sans lot remplacant", snapshot)
        replacement = _as_int(replaced_by)
        if replacement <= 0:
            return _refuse("evidence-invalid",
                           "lot remplacant %r : identifiant de lot attendu"
                           % (replaced_by,), snapshot)
        if replacement == _as_int(snapshot.item.get("id")):
            return _refuse("evidence-invalid",
                           "supersession du lot par lui-meme", snapshot)
    return _accept(command, ("reason",))


def _check_work_completed(command: Command, snapshot: Snapshot) -> Decision:
    """Completion par type, sur preuves reelles.

    N2 de la revue #1924 : pour un type non-code, `outcome` est un **libelle
    client** — B ne publie aucune collection de resultats, C n'en interprete
    donc pas le contenu. Le rattachement exigible est celui de la tete relue :
    tous les types installes exigent la revue, et la completion n'est acceptee
    que sur une revision dont la tete relue applicable porte un verdict
    courant `ok` (`completion-review-missing` sinon). Un libelle seul, sans
    revue applicable, ne complete pas.
    """
    revision = _revision(snapshot)
    if not revision:
        return _refuse("revision-missing",
                       "snapshot sans revision de travail", snapshot)
    policy = completion_policy(_text(snapshot.item.get("type")))
    references = ("outcome",)
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
        references = ("delivery",)
    elif not _ref(command, "outcome"):
        return _refuse("completion-evidence-missing",
                       "resultat propre du type non-code requis", snapshot)
    if policy.requires_review:
        head = reviewed_head_for(snapshot.milestones, revision)
        if _current_verdict(snapshot, head) != "ok":
            return _refuse("completion-review-missing",
                           "revue applicable absente ou non ok sur la tete "
                           "relue %s" % (head or "(absente)"), snapshot)
    return _accept(command, references)


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
              ("milestones", "receipts", "observations"), ("staging",), (),
              _check_staging_pass),
    GuardSpec("staging_failure", "observation staging fail et reparation permise",
              ("milestones", "receipts", "observations"), ("staging",), (),
              _check_staging_failure),
    GuardSpec("preprod_deployed", "observation preprod deployee de la revision",
              ("milestones", "receipts", "observations"), ("preprod",), (),
              _check_preprod_deployed),
    GuardSpec("preprod_pass", "observation preprod pass de la revision",
              ("milestones", "receipts", "observations"), ("preprod",), (),
              _check_preprod_pass),
    GuardSpec("preprod_failure", "observation preprod fail et reparation permise",
              ("milestones", "receipts", "observations"), ("preprod",), (),
              _check_preprod_failure),
    GuardSpec("repair_started", "reparation engagee sur un echec observe",
              ("milestones", "receipts", "observations"), ("failure",), (),
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
    elif action == "transfer":
        if _text(command.hold) or _text(command.guard):
            return "un transfert ne porte ni retenue ni garde"
        if not _text(command.workflow_ref):
            return "transfert sans workflow_ref vise"
        if (isinstance(command.workflow_version, bool)
                or not isinstance(command.workflow_version, int)
                or command.workflow_version < 0):
            return "workflow_version vise : entier >= 0 attendu"
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


# --------------------------------------------------------------------------
# transfert de revision Process (#4007, A de #2846)
# --------------------------------------------------------------------------

#: Une revision Process transferable est pinnee sur un commit complet :
#: `owner/depot@<sha de 40>:<chemin>` (la forme `pin` du pont #4006). Une
#: reference mobile ou abregee ne se lie a aucune approbation exacte.
_TRANSFER_PIN_RE = re.compile(
    r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100})"
    r"@([0-9a-fA-F]{40}):(\S+)")

#: Codes du pont d'approbation (#4006) -> codes Process deja publies. Aucun
#: code nouveau (un code nouveau passe d'abord par le pilote) : le code du
#: pont reste lisible dans `Decision.detail` (`facts["detail"]` du recu).
#: Toute famille `projection-*` vaut `source-unavailable`.
_TRANSFER_REFUSALS = {
    "approval-missing": "evidence-missing",
    "approval-stale": "revision-mismatch",
    "approval-superseded": "revision-mismatch",
    "approval-revoked": "evidence-invalid",
    "approval-expired": "evidence-invalid",
    "approval-ambiguous": "evidence-invalid",
    "approval-invalid": "evidence-invalid",
    "approval-source": "evidence-invalid",
    "approval-api-not-authority": "evidence-invalid",
}


def transfer_refusal_code(bridge_code: str) -> str:
    """Le code Process publie pour un code du pont d'approbation (#4006)."""
    if bridge_code.startswith("projection-"):
        return "source-unavailable"
    return _TRANSFER_REFUSALS.get(bridge_code, "evidence-invalid")


def _transfer_pin(ref: str) -> tuple[str, str, str] | None:
    match = _TRANSFER_PIN_RE.fullmatch(_text(ref))
    if match is None:
        return None
    return match.group(1), match.group(2).lower(), match.group(3)


def _verify_transfer(command: Command, snapshot: Snapshot) -> Decision:
    """Adoption ou transfert de revision Process, sur approbation reelle.

    Contrat `TRANSFER_CONTRACT` : la revision visee (`workflow_ref`) est un
    pin exact `owner/depot@<sha>:<chemin>` ; la projection `approvals` (#4006)
    porte une approbation **courante** de cet artefact exact ; la commande la
    cite (`approval` : id ou comment_id) et nomme le lot (`lot`) et le projet
    (`project`) de la ligne verrouillee par B ; l'approbateur n'est ni
    l'acteur de la commande ni l'assigne du lot. Rien n'est accepte par
    defaut : source absente, approbation absente, perimee, revoquee ou d'une
    autre revision refusent, avec le code du pont dans `detail`.
    """
    if _closed(snapshot):
        return _refuse("terminal-reopening",
                       "lot terminal : aucun transfert de revision", snapshot)
    pin = _transfer_pin(command.workflow_ref)
    if pin is None:
        return _refuse("evidence-invalid",
                       "workflow_ref %r : pin owner/depot@<sha de 40>:<chemin> "
                       "attendu" % command.workflow_ref, snapshot)
    if "approvals" not in set(snapshot.sources):
        return _refuse("source-unavailable",
                       "collection approvals non collectee (projection absente "
                       "ou perimee)", snapshot)
    refs = command.references or {}
    unknown = [key for key in refs if key not in TRANSFER_REFERENCES]
    if unknown:
        return _refuse("evidence-invalid",
                       "references inconnues: %s" % ", ".join(sorted(unknown)),
                       snapshot)
    missing = [key for key in TRANSFER_REFERENCES if not _present(refs.get(key))]
    if missing:
        return _refuse("evidence-missing",
                       "references requises absentes: %s" % ", ".join(missing),
                       snapshot)
    lot = _as_int(snapshot.item.get("id"))
    if _as_int(refs.get("lot")) != lot:
        return _refuse("item-mismatch",
                       "transfert cite le lot %r, lot verrouille %s"
                       % (refs.get("lot"), lot), snapshot)
    project = _text(snapshot.item.get("app"))
    if not project or _ref(command, "project") != project:
        return _refuse("item-mismatch",
                       "transfert cite le projet %r, lot du projet %r"
                       % (refs.get("project"), project), snapshot)

    from . import process_approvals as approvals_mod
    repo, revision, path = pin
    try:
        projection = approvals_mod.projection_from_source(snapshot.approvals)
        approval = approvals_mod.require_approval(
            projection, repo=repo, revision=revision, path=path,
            source=TRANSFER_APPROVAL_SOURCE)
    except approvals_mod.ApprovalError as exc:
        decision = _refuse(transfer_refusal_code(exc.code),
                           "%s : %s" % (exc.code, exc.message), snapshot)
        return Decision(decision.accepted, decision.code, decision.message,
                        decision.stage, detail=exc.code)
    cited = _ref(command, "approval")
    if cited not in (approval.id, str(approval.comment_id)):
        return _refuse("evidence-invalid",
                       "approbation citee %r : l'approbation courante de "
                       "l'artefact est %s" % (cited, approval.id), snapshot)
    author = approval.author.lower()
    if projection.approvers and author not in projection.approvers:
        return _refuse("evidence-invalid",
                       "auteur %s hors des approbateurs de la projection"
                       % author, snapshot)
    actors = {_text(command.actor).lower(),
              _text(snapshot.item.get("assignee")).lower()} - {""}
    if author in actors:
        return _refuse("reviewer-not-independent",
                       "approbateur %s : acteur du transfert ou assigne du lot"
                       % author, snapshot)
    meta = snapshot.approvals.get("projection") or {}
    record = approval.as_dict()
    proof = {
        "contract": TRANSFER_CONTRACT,
        "approval_source": TRANSFER_APPROVAL_SOURCE,
        "authority": _text(meta.get("authority")),
        "lot": lot,
        "project": project,
        "workflow_ref": command.workflow_ref,
        "workflow_version": int(command.workflow_version),
        "approval": {key: record[key] for key in (
            "id", "comment_id", "author", "created_at", "url",
            "body_sha256", "artifact")},
        "projection": {key: meta.get(key) for key in (
            "digest", "source_digest", "generated_at", "expires_at",
            "repo", "issue")},
    }
    return Decision(True, "accepted", "", command.to_stage,
                    TRANSFER_REFERENCES, proof=proof)


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
    if _text(command.action) == "transfer":
        # une premiere adoption n'a pas encore de revision : le transfert
        # passe avant `workflow-absent`.
        return _verify_transfer(command, snapshot)
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


def _installed_names(registry: Any) -> set[str] | None:
    """Les noms installes dans le registre serveur de B, ou `None` si non
    sondable (registre absent, ou sans introspection publiee)."""
    if registry is None:
        return None
    probe = getattr(registry, "installed_verifiers", None)
    mapping = probe() if callable(probe) else getattr(
        registry, "INSTALLED_VERIFIERS", None)
    if not isinstance(mapping, Mapping):
        return None
    return {_text(name) for name in mapping}


def _available_names(sources: Any) -> set[str] | None:
    """Les collections reellement disponibles, ou `None` si non sonde."""
    if sources is None:
        return None
    return set(available_sources(sources))


def _capability(name: str, purpose: str, required: tuple,
                references: tuple, *, installed: set[str] | None = None,
                available: set[str] | None = None, reserved: bool = False,
                conditional_sources: tuple = ()) -> Capability:
    """Une entree de la matrice, avec ses deux sondes : installation,
    sources."""
    if available is None:
        seen, missing = (), ()
    else:
        seen = tuple(src for src in required if src in available)
        missing = tuple(src for src in required if src not in available)
    return Capability(
        guard=name, purpose=purpose, sources=tuple(required),
        references=tuple(references), known=True, reserved=reserved,
        installed=None if installed is None else name in installed,
        available=seen, missing=missing,
        conditional_sources=tuple(conditional_sources),
        sources_probed=available is not None)


def capabilities(*, registry: Any = None, sources: Any = None,
                 include_reserved: bool = False) -> tuple[Capability, ...]:
    """La matrice de capacite : gardes connues, installation sondee, sources
    reellement disponibles.

    Sans argument : le contrat pur (les 14 gardes connues), installation et
    sources **non sondees** (`installed=None`, `available=missing=()`), donc
    rien n'est declare executable. `registry` (le module `process_registry` de
    B, ou tout objet publiant `installed_verifiers()`) marque chaque garde
    installee ou non. `sources` (noms de collections, mapping, `Snapshot` ou
    contexte de B) marque les sources exigees disponibles et manquantes, telles
    quelles, jamais simulees. `include_reserved` ajoute les noms reserves
    installes par C (`hold`), marques `reserved=True`.
    """
    installed = _installed_names(registry)
    available = _available_names(sources)
    caps = [_capability(spec.name, spec.purpose, spec.sources,
                        spec.required + spec.optional,
                        installed=installed, available=available)
            for spec in _SPECS]
    if include_reserved:
        for name in RESERVED_VERIFIERS:
            purpose, required, conditional = _RESERVED_SPECS[name]
            caps.append(_capability(name, purpose, required, (),
                                    installed=installed, available=available,
                                    reserved=True,
                                    conditional_sources=conditional))
    return tuple(caps)


def type_capability(kind: str) -> TypeCapability:
    """La politique de completion d'un type ; un type hors `INSTALLED_TYPES`
    rend un refus explicite `type-unsupported`, jamais une exception."""
    if kind in INSTALLED_TYPES:
        policy = completion_policy(kind)
        return TypeCapability(policy.type, True, policy.code,
                              policy.requires_delivery, policy.requires_review,
                              policy.reason)
    return TypeCapability(str(kind), False, False, False, False,
                          "type-unsupported: %r hors des types installes"
                          % (kind,))


def type_capabilities() -> tuple[TypeCapability, ...]:
    """Les types supportes et leur politique, dans l'ordre d'installation."""
    return tuple(type_capability(kind) for kind in INSTALLED_TYPES)


def capability_report(*, registry: Any = None, sources: Any = None
                      ) -> CapabilityReport:
    """La matrice complete : 14 gardes connues sondees, nom reserve `hold`,
    types supportes. `executable_guards` est le sous-ensemble declare
    executable ; `refusal(name)` nomme le refus explicite des autres."""
    caps = capabilities(registry=registry, sources=sources,
                        include_reserved=True)
    return CapabilityReport(
        guards=tuple(cap for cap in caps if not cap.reserved),
        reserved=tuple(cap for cap in caps if cap.reserved),
        types=type_capabilities())


def host_capabilities(*, sources: Any = None) -> CapabilityReport:
    """La matrice du serveur courant : sonde le registre de B installe
    (`ameesh.process_registry`). Sans B, l'installation n'est pas sondee, donc
    rien n'est executable (fail-closed)."""
    registry = None
    try:
        from . import process_registry as registry  # noqa: F811
    except ImportError:  # pragma: no cover - B pas encore fusionne
        registry = None
    return capability_report(registry=registry, sources=sources)


def completion_policy(kind: str) -> CompletionPolicy:
    """La politique de completion d'un type installe.

    Les types de code exigent une livraison/acceptation reelle et la revue
    applicable ; les autres leur propre resultat et la revue applicable. Aucun
    type ne complete sur une PR de promotion seule. Pour un type non-code, le
    resultat est le libelle client `outcome` (aucune collection de resultats
    cote B) : sa verification est le rattachement a la tete relue (N2).
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
        workflow_ref=_text(getattr(raw, "workflow_ref", "")),
        workflow_version=_as_int(getattr(raw, "workflow_version", 0)),
    )


def _context_sources(context: Any) -> Mapping:
    """Les sources lues par B : attribut `sources`, lecteur du contexte, ou
    `read_sources(context) -> Mapping` publie par `process_registry` (convention
    confirmee par le pilote le 16/10, lue dans sa transaction, sources
    reellement disponibles seulement). Aucune lecture SQL ici."""
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


def available_sources(source: Any) -> tuple[str, ...]:
    """Les collections reellement disponibles, sans lire la base ici.

    Accepte un `Snapshot`, un contexte de B (via `context.sources`,
    `context.read_sources()` ou `process_registry.read_sources(context)` : la
    lecture reste chez B, dans sa transaction), un mapping de collections
    (`{nom: lignes}`), ou un iterable de noms. Rend `()` quand rien n'est
    disponible : aucune source simulee n'est jamais declaree.
    """
    if source is None:
        return ()
    if isinstance(source, Snapshot):
        return tuple(source.sources)
    if isinstance(source, Mapping):
        return tuple(_text(name) for name in source)
    declared = getattr(source, "sources", None)
    if isinstance(declared, Mapping):
        return tuple(_text(name) for name in declared)
    if isinstance(declared, (list, tuple, set, frozenset)):
        return tuple(_text(name) for name in declared)
    if hasattr(source, "work_item") or callable(
            getattr(source, "read_sources", None)):
        return tuple(_context_sources(source) or {})
    if isinstance(source, str):
        return (_text(source),) if _text(source) else ()
    try:
        return tuple(_text(name) for name in source)
    except TypeError:
        return ()


def context_snapshot(context: Any) -> Snapshot:
    """Le `VerificationContext` de B -> le snapshot pur de C."""
    item = dict(getattr(context, "work_item", None) or {})
    sources = _context_sources(context) or {}
    milestones = tuple(sources.get("milestones", ()) or ())
    decisions = tuple(sources.get("decisions", ()) or ())
    approvals = sources.get("approvals") or {}
    if not isinstance(approvals, Mapping):
        approvals = {}
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
        approvals=dict(approvals),
        sources=tuple(name for name in SOURCE_COLLECTIONS if name in sources),
    )


def approval_records(snapshot: Snapshot) -> tuple[Mapping[str, Any], ...]:
    """#4006 : les approbations normalisees du snapshot (jamais un defaut).

    Un snapshot sans projection fraiche rend une collection vide ; la garde
    qui exige une approbation doit d'abord exiger la source `approvals`
    (`source-unavailable`) avant d'interpreter cette collection.
    """
    value = snapshot.approvals
    records = value.get("records") if isinstance(value, Mapping) else None
    if not isinstance(records, (list, tuple)):
        return ()
    return tuple(record for record in records if isinstance(record, Mapping))


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
        facts = {"code": decision.code, "stage": decision.stage,
                 "references": list(decision.references)}
        if decision.detail:
            facts["detail"] = decision.detail
        if decision.proof:
            facts["proof"] = dict(decision.proof)
        return cls(ok=decision.accepted,
                   reason=decision.message or decision.code,
                   facts=facts)

    _SERVER_VERIFIERS[key] = check
    return check


def install_verifiers(registry: Any = None) -> tuple[str, ...]:
    """Installe les appelables serveur dans `process_registry` (B) : les 14
    gardes du registre fini, puis `RESERVED_VERIFIERS` (le nom reserve
    `hold`, pour les actions `hold` et `release`, et `revision_transfer`,
    pour `transfer_revision`, #4007 ; son recu : `installation_receipt`).

    Sans `registry`, importe `ameesh.process_registry` ; avec, utilise l'objet
    fourni (tests, autre hote). Rend les noms installes, dans cet ordre. Aucun
    verificateur par defaut cote B : sans cet appel, une avance, une retenue,
    une reprise ou un transfert reste `verifier-uninstalled`.
    """
    if registry is None:
        from . import process_registry as registry
    verification_cls = getattr(registry, "Verification")
    names = GUARD_NAMES + RESERVED_VERIFIERS
    for name in names:
        registry.install_verifier(name, server_verifier(name, verification_cls))
    return names


#: Schema du recu d'installation d'un verificateur reserve (#4007).
INSTALL_RECEIPT_SCHEMA = "process-verifier-install/1"


def installation_receipt(registry: Any = None) -> dict:
    """Le recu d'installation de `revision_transfer` (#4007), sans effet.

    Dit ce qui est **reellement** installe dans le registre de B : le nom
    reserve, si l'appelable installe est bien celui de C (`installed`), le
    contrat et la source d'approbation (constantes du module ; l'autorite
    vient de la configuration, #4259, et figure dans la preuve), et
    l'empreinte SHA-256 des sources qui decident (ce module et le pont
    #4006). Un appelable etranger sous ce nom rend `installed=False` : le
    recu ne prouve jamais une installation qu'il n'a pas vue.
    """
    import hashlib

    if registry is None:
        from . import process_registry as registry
    probe = getattr(registry, "installed_verifiers", None)
    mapping = probe() if callable(probe) else getattr(
        registry, "INSTALLED_VERIFIERS", None)
    current = mapping.get(TRANSFER_VERIFIER) if isinstance(mapping, Mapping) else None
    verification_cls = getattr(registry, "Verification", None)
    ours = (current is not None and verification_cls is not None
            and current is _SERVER_VERIFIERS.get((TRANSFER_VERIFIER,
                                                  verification_cls)))
    from . import process_approvals as approvals_mod
    digests = {}
    for name, path in ((__name__, __file__),
                       (approvals_mod.__name__, approvals_mod.__file__)):
        with open(path, "rb") as handle:
            digests[name] = hashlib.sha256(handle.read()).hexdigest()
    return {
        "schema": INSTALL_RECEIPT_SCHEMA,
        "verifier": TRANSFER_VERIFIER,
        "installed": bool(ours),
        "contract": TRANSFER_CONTRACT,
        "approval_source": TRANSFER_APPROVAL_SOURCE,
        "references": list(TRANSFER_REFERENCES),
        "sources": ["approvals"],
        "version": approvals_mod._version(),
        "source_sha256": digests,
    }
