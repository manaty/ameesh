# SPDX-License-Identifier: AGPL-3.0-only
"""Workflows Process (#1915, tranche 1) : compilation STRICTE d'une révision.

Le canon fournit des **données** : ce module les compile en un artefact gelé
(`CompiledWorkflow`) et refuse tout le reste. Il n'exécute rien, ne lit ni
n'écrit aucun état natif, ne connaît ni SQL ni migration, et n'importe aucun
chemin venu du canon : une expression, un chemin d'import ou une clé inconnue
sont des refus, jamais des capacités.

Schéma fermé (aucune autre clé n'est acceptée) ::

    schema       "process-workflow/1"
    initial      statut de départ, déclaré dans stages
    types        types de lots couverts, chacun dans SUPPORTED_TYPES
    stages       [{status, core}] — statut Process -> cœur natif (CORE_STATES)
    transitions  [{from, to, guard}] — garde dans le registre fini VERIFIERS
    projection   {project, status_field, product_field,
                  issue_open, issue_completed, issue_not_planned}

Les trois listes de `projection` nomment des **cœurs** natifs : elles
classent exactement les cœurs déclarés par les étapes, sans recouvrement ;
`promoted` est terminal « completed », `closed` terminal « not_planned »,
les autres restent ouverts. `blocked` et `waiting_human` sont des retenues
(holds) qui gardent l'étape courante : ce ne sont pas des cœurs d'étape.

Une même garde peut mener à **plusieurs cibles distinctes** depuis une même
étape (la carte de référence #1379 le fait : `draft -> backlog` et
`draft -> sprint` sous `planning`). L'arête est choisie par la commande
(`to_stage`), et le registre natif vérifie le couple exact
(`to_stage`, `guard`) depuis `from_stage` ; seul un doublon **exact**
`(from, to, guard)` est refusé.

Refus (codes stables, portés par `WorkflowRefusal.code`) :

* lecture     `json-invalid`, `key-duplicate` ;
* forme       `value-type`, `value-empty`, `key-unknown`, `key-missing`,
              `schema-unsupported`, `ref-invalid`, `version-invalid` ;
* étapes      `stage-duplicate`, `stage-padded`, `stage-case-duplicate`,
              `core-unknown`, `initial-undeclared`, `stage-unreachable` ;
* types       `type-duplicate`, `type-unsupported` ;
* arêtes      `edge-duplicate`, `edge-undeclared`,
              `guard-unknown`, `terminal-reopening` ;
* projection  `classification-overlap`, `classification-incomplete`,
              `classification-mismatch`, `projection-conflict`.

L'artefact conserve la révision Process **exacte** qui l'a produit
(`workflow_ref`, `workflow_version`) : compiler un autre document, ou le même
document sous une autre révision, donne un artefact distinct ; aucune
adoption automatique d'une nouvelle révision n'existe ici.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import json
from dataclasses import dataclass
from typing import Any, Mapping

SUPPORTED_SCHEMA = "process-workflow/1"

#: Cœurs natifs qu'une étape Process peut projeter. `blocked` et
#: `waiting_human` n'en sont pas : ce sont des retenues qui gardent l'étape.
CORE_STATES = (
    lifecycle.REQUESTED,
    lifecycle.IN_PROGRESS,
    lifecycle.QA,
    "merged",
    "promoted",
    lifecycle.CLOSED,
)

#: Cœurs terminaux et leur issue : un cœur terminal ne se rouvre jamais.
TERMINAL_CORES = {"promoted": "completed", lifecycle.CLOSED: "not_planned"}

#: Registre fini des vérificateurs du contrat consommateur (#1379). Le
#: compilateur ne les exécute pas : il refuse une garde hors de ce registre.
VERIFIER_NAMES = (
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
VERIFIERS = frozenset(VERIFIER_NAMES)

#: Types de lots installables, opt-in par projet (#1379).
SUPPORTED_TYPES = (
    "bug",
    "evolution",
    "docs",
    "investigation",
    "governance-tracking",
    "promotion",
    "human_operation",
)

DOCUMENT_KEYS = ("schema", "initial", "types", "stages", "transitions", "projection")
STAGE_KEYS = ("status", "core")
TRANSITION_KEYS = ("from", "to", "guard")
PROJECTION_KEYS = ("project", "status_field", "product_field",
                   "issue_open", "issue_completed", "issue_not_planned")


class WorkflowRefusal(ValueError):
    """Refus de compilation : un code stable et un chemin dans le document.

    `code` est la raison discriminante (voir la liste du module), `path` la
    position JSON-ish (`$.stages[2].core`), `message` le détail lisible.
    """

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__("%s at %s: %s" % (code, path, message))
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True)
class Stage:
    """Un statut Process et le cœur natif qu'il projette."""

    status: str
    core: str


@dataclass(frozen=True)
class Transition:
    """Une arête gardée : `guard` est un nom du registre fini."""

    source: str
    target: str
    guard: str


@dataclass(frozen=True)
class Projection:
    """Projection vers l'issue : champs, et classification des cœurs."""

    project: str
    status_field: str
    product_field: str
    issue_open: tuple[str, ...]
    issue_completed: tuple[str, ...]
    issue_not_planned: tuple[str, ...]


@dataclass(frozen=True)
class CompiledWorkflow:
    """Artefact gelé : données seulement, révision Process conservée."""

    schema: str
    initial: str
    types: tuple[str, ...]
    stages: tuple[Stage, ...]
    transitions: tuple[Transition, ...]
    projection: Projection
    workflow_ref: str
    workflow_version: int

    def statuses(self) -> tuple[str, ...]:
        return tuple(stage.status for stage in self.stages)

    def stage(self, status: str) -> Stage:
        for stage in self.stages:
            if stage.status == status:
                return stage
        raise KeyError(status)

    def core_of(self, status: str) -> str:
        return self.stage(status).core

    def transitions_from(self, status: str) -> tuple[Transition, ...]:
        return tuple(t for t in self.transitions if t.source == status)

    def guards(self) -> tuple[str, ...]:
        seen: list[str] = []
        for transition in self.transitions:
            if transition.guard not in seen:
                seen.append(transition.guard)
        return tuple(seen)


# --------------------------------------------------------------------------
# lecture : les clés dupliquées d'un texte JSON sont des refus, pas un choix
# --------------------------------------------------------------------------

def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise WorkflowRefusal("key-duplicate", "$.%s" % key,
                                  "clé répétée à la lecture du JSON")
        seen[key] = value
    return seen


def parse_workflow(text: str | bytes) -> dict:
    """Lit un document JSON en refusant toute clé dupliquée, à tout niveau."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    if not isinstance(text, str):
        raise WorkflowRefusal("value-type", "$", "texte JSON attendu")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except WorkflowRefusal:
        raise
    except ValueError as exc:
        raise WorkflowRefusal("json-invalid", "$", "JSON illisible : %s" % exc) from exc


# --------------------------------------------------------------------------
# validation de forme
# --------------------------------------------------------------------------

def _mapping(value: Any, path: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise WorkflowRefusal("value-type", path, "objet attendu")
    return value


def _keys(obj: Mapping, allowed: tuple[str, ...], path: str) -> None:
    for key in obj:
        if not isinstance(key, str) or key not in allowed:
            raise WorkflowRefusal("key-unknown", "%s.%s" % (path, key),
                                  "clé inconnue (acceptées : %s)" % ", ".join(allowed))
    for key in allowed:
        if key not in obj:
            raise WorkflowRefusal("key-missing", "%s.%s" % (path, key), "clé requise")


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise WorkflowRefusal("value-type", path, "chaîne attendue")
    if not value.strip():
        raise WorkflowRefusal("value-empty", path, "chaîne vide")
    return value


def _list(value: Any, path: str, *, nonempty: bool = True) -> list:
    if not isinstance(value, list):
        raise WorkflowRefusal("value-type", path, "liste attendue")
    if nonempty and not value:
        raise WorkflowRefusal("value-empty", path, "liste vide")
    return value


def _types(value: Any) -> tuple[str, ...]:
    path = "$.types"
    items = _list(value, path)
    out: list[str] = []
    for index, raw in enumerate(items):
        name = _text(raw, "%s[%d]" % (path, index))
        if name not in SUPPORTED_TYPES:
            raise WorkflowRefusal("type-unsupported", "%s[%d]" % (path, index),
                                  "type hors du registre installable")
        if name in out:
            raise WorkflowRefusal("type-duplicate", "%s[%d]" % (path, index),
                                  "type déclaré deux fois")
        out.append(name)
    return tuple(out)


def _stages(value: Any) -> tuple[Stage, ...]:
    path = "$.stages"
    items = _list(value, path)
    out: list[Stage] = []
    seen: set[str] = set()
    folded: dict[str, str] = {}
    for index, raw in enumerate(items):
        item = "%s[%d]" % (path, index)
        obj = _mapping(raw, item)
        _keys(obj, STAGE_KEYS, item)
        status = _text(obj["status"], item + ".status")
        core = _text(obj["core"], item + ".core")
        if core not in CORE_STATES:
            raise WorkflowRefusal("core-unknown", item + ".core",
                                  "cœur hors de %s" % ", ".join(CORE_STATES))
        if status != status.strip():
            raise WorkflowRefusal("stage-padded", item + ".status",
                                  "espaces en bord dans le statut")
        key = status.casefold()
        other = folded.get(key)
        if other is not None and other != status:
            raise WorkflowRefusal("stage-case-duplicate", item + ".status",
                                  "statut identique à la casse près à %s" % other)
        if status in seen:
            raise WorkflowRefusal("stage-duplicate", item + ".status",
                                  "statut déclaré deux fois")
        seen.add(status)
        folded[key] = status
        out.append(Stage(status=status, core=core))
    return tuple(out)


def _transitions(value: Any, stages: tuple[Stage, ...]) -> tuple[Transition, ...]:
    path = "$.transitions"
    items = _list(value, path, nonempty=False)
    declared = {stage.status for stage in stages}
    out: list[Transition] = []
    exact: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(items):
        item = "%s[%d]" % (path, index)
        obj = _mapping(raw, item)
        _keys(obj, TRANSITION_KEYS, item)
        source = _text(obj["from"], item + ".from")
        target = _text(obj["to"], item + ".to")
        guard = _text(obj["guard"], item + ".guard")
        if source not in declared:
            raise WorkflowRefusal("edge-undeclared", item + ".from",
                                  "étape non déclarée")
        if target not in declared:
            raise WorkflowRefusal("edge-undeclared", item + ".to",
                                  "étape non déclarée")
        if guard not in VERIFIERS:
            raise WorkflowRefusal("guard-unknown", item + ".guard",
                                  "garde hors du registre fini")
        triple = (source, target, guard)
        if triple in exact:
            raise WorkflowRefusal("edge-duplicate", item, "arête déjà déclarée")
        exact.add(triple)
        out.append(Transition(source=source, target=target, guard=guard))
    return tuple(out)


def _core_list(value: Any, path: str) -> tuple[str, ...]:
    items = _list(value, path, nonempty=False)
    out: list[str] = []
    for index, raw in enumerate(items):
        core = _text(raw, "%s[%d]" % (path, index))
        if core not in CORE_STATES:
            raise WorkflowRefusal("core-unknown", "%s[%d]" % (path, index),
                                  "cœur hors de %s" % ", ".join(CORE_STATES))
        out.append(core)
    return tuple(out)


def _projection(value: Any, stages: tuple[Stage, ...]) -> Projection:
    path = "$.projection"
    obj = _mapping(value, path)
    _keys(obj, PROJECTION_KEYS, path)
    project = _text(obj["project"], path + ".project")
    status_field = _text(obj["status_field"], path + ".status_field")
    product_field = _text(obj["product_field"], path + ".product_field")
    if status_field == product_field:
        raise WorkflowRefusal("projection-conflict", path,
                              "status_field et product_field sont le même champ")
    open_cores = _core_list(obj["issue_open"], path + ".issue_open")
    completed = _core_list(obj["issue_completed"], path + ".issue_completed")
    not_planned = _core_list(obj["issue_not_planned"], path + ".issue_not_planned")

    classified = list(open_cores) + list(completed) + list(not_planned)
    if len(set(classified)) != len(classified):
        raise WorkflowRefusal("classification-overlap", path,
                              "un cœur est classé deux fois")
    for core in completed:
        if TERMINAL_CORES.get(core) != "completed":
            raise WorkflowRefusal("classification-mismatch", path + ".issue_completed",
                                  "%r n'est pas un cœur terminal « completed »" % core)
    for core in not_planned:
        if TERMINAL_CORES.get(core) != "not_planned":
            raise WorkflowRefusal("classification-mismatch", path + ".issue_not_planned",
                                  "%r n'est pas un cœur terminal « not_planned »" % core)
    for core in open_cores:
        if core in TERMINAL_CORES:
            raise WorkflowRefusal("classification-mismatch", path + ".issue_open",
                                  "cœur terminal %r classé ouvert" % core)
    used = {stage.core for stage in stages}
    extra = sorted(set(classified) - used)
    if extra:
        raise WorkflowRefusal("classification-mismatch", path,
                              "cœurs classés sans étape : %s" % ", ".join(extra))
    missing = sorted(used - set(classified))
    if missing:
        raise WorkflowRefusal("classification-incomplete", path,
                              "cœurs d'étape non classés : %s" % ", ".join(missing))
    return Projection(project=project, status_field=status_field,
                      product_field=product_field, issue_open=open_cores,
                      issue_completed=completed, issue_not_planned=not_planned)


def _graph(stages: tuple[Stage, ...], transitions: tuple[Transition, ...],
           initial: str) -> None:
    core_of = {stage.status: stage.core for stage in stages}
    for index, transition in enumerate(transitions):
        if core_of[transition.source] in TERMINAL_CORES:
            raise WorkflowRefusal("terminal-reopening", "$.transitions[%d].from" % index,
                                  "un cœur terminal ne se rouvre pas")
    out: dict[str, list[str]] = {}
    for transition in transitions:
        out.setdefault(transition.source, []).append(transition.target)
    seen = {initial}
    queue = [initial]
    while queue:
        for target in out.get(queue.pop(), ()):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    unreachable = [stage.status for stage in stages if stage.status not in seen]
    if unreachable:
        raise WorkflowRefusal("stage-unreachable", "$.stages",
                              "inaccessibles depuis %r : %s"
                              % (initial, ", ".join(unreachable)))


# --------------------------------------------------------------------------
# API pure
# --------------------------------------------------------------------------

def _ref(ref: Any) -> str:
    if not isinstance(ref, str) or not ref.strip():
        raise WorkflowRefusal("ref-invalid", "$.workflow_ref",
                              "référence Process (commit/chemin) requise")
    return ref


def _version(version: Any) -> int:
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise WorkflowRefusal("version-invalid", "$.workflow_version",
                              "entier >= 0 attendu")
    return version


def compile_workflow(document: Mapping | str | bytes, *,
                     ref: str, version: int = 0) -> CompiledWorkflow:
    """Compile un document de workflow Process en artefact gelé.

    `document` est un objet (mapping) ou un texte JSON — dans ce dernier cas
    toute clé dupliquée est refusée. `ref` et `version` pincent la révision
    Process adoptée et sont conservés tels quels dans l'artefact.
    """
    workflow_ref = _ref(ref)
    workflow_version = _version(version)
    if isinstance(document, (str, bytes, bytearray)):
        document = parse_workflow(document)
    doc = _mapping(document, "$")
    _keys(doc, DOCUMENT_KEYS, "$")
    schema = _text(doc["schema"], "$.schema")
    if schema != SUPPORTED_SCHEMA:
        raise WorkflowRefusal("schema-unsupported", "$.schema",
                              "schéma %r inconnu (attendu %r)"
                              % (schema, SUPPORTED_SCHEMA))
    types = _types(doc["types"])
    stages = _stages(doc["stages"])
    initial = _text(doc["initial"], "$.initial")
    if initial not in {stage.status for stage in stages}:
        raise WorkflowRefusal("initial-undeclared", "$.initial",
                              "statut initial non déclaré dans stages")
    transitions = _transitions(doc["transitions"], stages)
    projection = _projection(doc["projection"], stages)
    _graph(stages, transitions, initial)
    return CompiledWorkflow(schema=schema, initial=initial, types=types,
                            stages=stages, transitions=transitions,
                            projection=projection, workflow_ref=workflow_ref,
                            workflow_version=workflow_version)


def compile_project_workflow(document: Any, *,
                             ref: str, version: int = 0) -> CompiledWorkflow | None:
    """Compile le workflow d'un projet, ou `None` s'il n'en a pas.

    Un projet sans workflow (document absent, prose seule, ou clé `workflow`
    absente ou en prose) est **inchangé** : rien n'est compilé, rien n'est
    exigé. Un workflow déclaré en données est compilé strictement.
    """
    if document is None or not isinstance(document, Mapping):
        return None
    workflow = document.get("workflow")
    if workflow is None or isinstance(workflow, str):
        return None
    return compile_workflow(workflow, ref=ref, version=version)
