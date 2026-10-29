# SPDX-License-Identifier: AGPL-3.0-only
"""#2225 : carte Process du canon -> workflow natif `process-workflow/1`.

Une carte Process (`processus/*.md`) porte ses règles de travail en prose et,
depuis #1915, peut porter un bloc **`workflow:`** dans son frontmatter YAML :
la représentation canon d'un workflow exécutable. Ce module est le pont
**données seules** vers `process_workflow.compile_workflow` :

* il lit la carte (frontmatter), jamais le corps comme du code ;
* il encode la forme canon (étapes en table, transitions groupées, libellés
  d'affichage, alias de types, classification par étape) vers le schéma natif
  strict `process-workflow/1` ;
* il compile et refuse sans exécuter, sans base, sans état natif.

L'encodage (documenté dans `site/docs/guides/write-a-process.md`) :

* `stages` : une table `nom -> {core, status}`. `core` est un cœur natif
  (`CORE_STATES`) ; `status` est le libellé d'affichage, conservé **hors** de
  l'artefact compilé (côté présentation / `github.process.<projet>.labels`).
* `transitions` : `from` est une étape ou une **liste** d'étapes ; chaque
  source produit une arête exacte. Un doublon exact `(from, to, guard)` est
  refusé ; deux cibles distinctes sous la même garde depuis une même étape
  restent permises (les deux choix `planning` de la carte de référence).
* `types` : alias de projet appliqués avant validation
  (`governance` -> `governance-tracking`), sans doublon après alias.
* `projection` : classe des **étapes** ; l'encodeur les traduit en cœurs, en
  refusant une étape classée deux fois, une étape non classée, ou un même
  cœur classé dans deux groupes.

La commande de vérification d'un auteur de canon ::

    ameesh process check processus/ma-carte.md
    python -m ameesh.process_card check processus/ma-carte.md   # sans installation

Elle sort 0 si le bloc compile, 1 sur un refus nommé (`WorkflowRefusal`), 2
sur une erreur de lecture ou d'usage. `--json` imprime aussi le document natif
à admettre et les libellés, pour la configuration de l'hôte.

La carte elle-même reste une fiche Process ordinaire : `canon check` valide
`type: Process`, `title`, `responsible` et `updated` ; il ne valide pas ce bloc
`workflow:` (clé inconnue conservée). Le contrôle du workflow est celui-ci.

L103 (#4395) : une carte `Pipeline` (`pipelines/*.md`) porte le même bloc sous
`pipeline:` (alias `workflow:`) ; `pipeline_of_page`/`pipeline_block` le
lisent et `canon check` le compile avec `compile_block`.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any, Mapping

from .process_workflow import (CORE_STATES, SUPPORTED_SCHEMA, SUPPORTED_TYPES,
                               VERIFIERS, CompiledWorkflow, WorkflowRefusal,
                               compile_workflow)

#: les clés d'un bloc `workflow:` canon (aucune autre)
CANON_KEYS = ("schema", "initial", "types", "stages", "transitions", "projection")
STAGE_KEYS = ("core", "status")
TRANSITION_KEYS = ("from", "to", "guard")
PROJECTION_KEYS = ("project", "status_field", "product_field",
                   "issue_open", "issue_completed", "issue_not_planned")
PROJECTION_GROUPS = ("issue_open", "issue_completed", "issue_not_planned")

#: L103 (#4395) : type de la carte qui déclare un pipeline (module déclaratif),
#: et ses clés de bloc — `pipeline:`, puis `workflow:` en alias transitoire.
PIPELINE_TYPE = "Pipeline"
PIPELINE_KEYS = ("pipeline", "workflow")

#: alias de types de lots admis par le canon ; la valeur est le type natif.
TYPE_ALIASES = {"governance": "governance-tracking"}


def _refuse(code: str, path: str, message: str) -> None:
    raise WorkflowRefusal(code, path, message)


def _mapping(value: Any, path: str) -> Mapping:
    if not isinstance(value, Mapping):
        _refuse("value-type", path, "object expected")
    return value


def _keys(obj: Mapping, allowed: tuple[str, ...], path: str) -> None:
    for key in obj:
        if not isinstance(key, str) or key not in allowed:
            _refuse("key-unknown", "%s.%s" % (path, key),
                    "unknown key (accepted: %s)" % ", ".join(allowed))
    for key in allowed:
        if key not in obj:
            _refuse("key-missing", "%s.%s" % (path, key), "required key")


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _refuse("value-type", path, "string expected")
    if not value.strip():
        _refuse("value-empty", path, "empty string")
    return value


def _list(value: Any, path: str, *, nonempty: bool = True) -> list:
    if not isinstance(value, list):
        _refuse("value-type", path, "list expected")
    if nonempty and not value:
        _refuse("value-empty", path, "empty list")
    return value


def _stage_name(value: Any, path: str) -> str:
    name = _text(value, path)
    if name != name.strip():
        _refuse("stage-padded", path, "surrounding spaces in the stage name")
    return name


def _stage_core(value: Any, path: str) -> str:
    core = _text(value, path)
    if core not in CORE_STATES:
        _refuse("core-unknown", path, "core outside %s" % ", ".join(CORE_STATES))
    return core


def _stages(value: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Encode ``stages`` (table) en (nom -> cœur, nom -> libellé)."""
    path = "$.stages"
    obj = _mapping(value, path)
    if not obj:
        _refuse("value-empty", path, "empty stage table")
    cores: dict[str, str] = {}
    labels: dict[str, str] = {}
    folded: dict[str, str] = {}
    for raw_name, raw_stage in obj.items():
        name = _stage_name(raw_name, "%s.%s" % (path, raw_name))
        item = "%s.%s" % (path, name)
        stage = _mapping(raw_stage, item)
        _keys(stage, STAGE_KEYS, item)
        core = _stage_core(stage["core"], item + ".core")
        label = _text(stage["status"], item + ".status")
        if label != label.strip():
            _refuse("stage-padded", item + ".status",
                    "surrounding spaces in the display label")
        key = name.casefold()
        other = folded.get(key)
        if other is not None and other != name:
            _refuse("stage-case-duplicate", item,
                    "stage identical to %r up to case" % other)
        folded[key] = name
        if label in labels.values():
            _refuse("stage-label-duplicate", item + ".status",
                    "display label %r declared twice" % label)
        cores[name] = core
        labels[name] = label
    return cores, labels


def _types(value: Any) -> tuple[tuple[str, ...], dict[str, str]]:
    path = "$.types"
    items = _list(value, path)
    out: list[str] = []
    aliases: dict[str, str] = {}
    for index, raw in enumerate(items):
        item = "%s[%d]" % (path, index)
        name = _text(raw, item)
        native = TYPE_ALIASES.get(name, name)
        if native not in SUPPORTED_TYPES:
            _refuse("type-unsupported", item,
                    "type outside the installable registry")
        if native in out:
            _refuse("type-duplicate", item, "type declared twice (after alias)")
        if name != native:
            aliases[name] = native
        out.append(native)
    return tuple(out), aliases


def _sources(value: Any, path: str) -> list[tuple[str, str]]:
    """Une source d'arête : une étape, ou une liste groupée d'étapes."""
    if isinstance(value, str):
        return [(_stage_name(value, path), path)]
    items = _list(value, path)
    out: list[tuple[str, str]] = []
    for index, raw in enumerate(items):
        item = "%s[%d]" % (path, index)
        out.append((_stage_name(raw, item), item))
    return out


def _transitions(value: Any, cores: Mapping[str, str]) -> list[dict[str, str]]:
    path = "$.transitions"
    items = _list(value, path, nonempty=False)
    out: list[dict[str, str]] = []
    exact: set[tuple[str, str, str]] = set()
    for index, raw in enumerate(items):
        item = "%s[%d]" % (path, index)
        edge = _mapping(raw, item)
        _keys(edge, TRANSITION_KEYS, item)
        target = _stage_name(edge["to"], item + ".to")
        guard = _text(edge["guard"], item + ".guard")
        if target not in cores:
            _refuse("edge-undeclared", item + ".to", "undeclared stage")
        if guard not in VERIFIERS:
            _refuse("guard-unknown", item + ".guard",
                    "guard outside the finite registry")
        for source, source_path in _sources(edge["from"], item + ".from"):
            if source not in cores:
                _refuse("edge-undeclared", source_path, "undeclared stage")
            triple = (source, target, guard)
            if triple in exact:
                _refuse("edge-duplicate", item, "edge already declared")
            exact.add(triple)
            out.append({"from": source, "to": target, "guard": guard})
    return out


def _projection(value: Any, cores: Mapping[str, str]) -> dict[str, Any]:
    """Classe des étapes -> classification native des cœurs."""
    path = "$.projection"
    obj = _mapping(value, path)
    _keys(obj, PROJECTION_KEYS, path)
    project = _text(obj["project"], path + ".project")
    status_field = _text(obj["status_field"], path + ".status_field")
    product_field = _text(obj["product_field"], path + ".product_field")
    if status_field == product_field:
        _refuse("projection-conflict", path,
                "status_field and product_field are the same field")

    classified: set[str] = set()
    core_group: dict[str, str] = {}
    out: dict[str, Any] = {"project": project, "status_field": status_field,
                           "product_field": product_field}
    for group in PROJECTION_GROUPS:
        group_path = "%s.%s" % (path, group)
        names = _list(obj[group], group_path, nonempty=False)
        group_cores: list[str] = []
        for index, raw in enumerate(names):
            item = "%s[%d]" % (group_path, index)
            name = _stage_name(raw, item)
            if name not in cores:
                _refuse("classification-mismatch", item,
                        "stage not declared in stages")
            if name in classified:
                _refuse("classification-overlap", item,
                        "stage classified twice")
            classified.add(name)
            core = cores[name]
            other = core_group.get(core)
            if other is not None and other != group:
                _refuse("classification-core-conflict", group_path,
                        "core %r classified in %s and %s" % (core, other, group))
            core_group[core] = group
            if core not in group_cores:
                group_cores.append(core)
        out[group] = group_cores

    missing = [name for name in cores if name not in classified]
    if missing:
        _refuse("classification-incomplete", path,
                "unclassified stage(s): %s" % ", ".join(missing))
    return out


@dataclass(frozen=True)
class CanonWorkflow:
    """Le bloc canon encodé : document natif, libellés, alias appliqués."""

    document: dict
    source_schema: str
    stage_labels: dict
    type_aliases: dict


def encode_canon_workflow(workflow: Mapping | str | bytes) -> CanonWorkflow:
    """Encode un bloc `workflow:` canon vers le document natif strict.

    Refuse tout ce que le schéma natif refuserait, plus ce que la forme canon
    ajoute : libellé d'affichage manquant ou dupliqué, étape inconnue dans une
    classification, cœur classé dans deux groupes, doublon après alias de type.
    """
    if isinstance(workflow, (str, bytes, bytearray)):
        _refuse("value-type", "$", "object expected (prose is not a workflow)")
    doc = _mapping(workflow, "$")
    _keys(doc, CANON_KEYS, "$")
    schema = _text(doc["schema"], "$.schema")
    initial = _stage_name(doc["initial"], "$.initial")
    types, aliases = _types(doc["types"])
    cores, labels = _stages(doc["stages"])
    if initial not in cores:
        _refuse("initial-undeclared", "$.initial",
                "initial stage not declared in stages")
    transitions = _transitions(doc["transitions"], cores)
    projection = _projection(doc["projection"], cores)
    native = {
        "schema": SUPPORTED_SCHEMA,
        "initial": initial,
        "types": list(types),
        "stages": [{"status": name, "core": core} for name, core in cores.items()],
        "transitions": transitions,
        "projection": projection,
    }
    return CanonWorkflow(document=native, source_schema=schema,
                         stage_labels=labels, type_aliases=aliases)


@dataclass(frozen=True)
class CheckResult:
    """Résultat d'une vérification de carte : artefact, encodage, révision."""

    path: str
    ref: str
    version: int
    artifact: CompiledWorkflow | None
    encoding: CanonWorkflow | None
    source_schema: str = ""

    @property
    def has_workflow(self) -> bool:
        return self.artifact is not None


def _validate_pin(ref: str, version: int) -> None:
    if not isinstance(ref, str) or not ref.strip():
        _refuse("ref-invalid", "$.workflow_ref",
                "Process reference (commit/path) required")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        _refuse("version-invalid", "$.workflow_version",
                "integer >= 0 expected")


def workflow_of_page(text: str) -> Mapping | None:
    """Le bloc `workflow:` d'une carte Process, ou None (carte en prose).

    Le frontmatter est lu par le lecteur du canon (`canon.split_frontmatter`
    et `canon.load_yaml`) : clés dupliquées refusées, dates normalisées. Un
    bloc absent ou en prose (`workflow: "…"`) laisse la carte inchangée.
    """
    data = _frontmatter(text, "Process")
    workflow = data.get("workflow")
    if workflow is None or isinstance(workflow, str):
        return None
    if not isinstance(workflow, Mapping):
        _refuse("value-type", "$.workflow", "object expected (or prose)")
    return workflow


def pipeline_block(data: Mapping) -> Mapping:
    """L103 (#4395) : le bloc d'une carte `Pipeline` lu dans son frontmatter.

    La clé est `pipeline:` ; `workflow:` reste lu comme alias transitoire.
    Les deux à la fois sont refusés (`key-conflict`), aucune des deux aussi
    (`key-missing`) : une carte Pipeline sans bloc n'est pas un module. Le bloc
    a la forme canon d'un bloc `workflow:` de carte Process (même encodeur)."""
    present = [key for key in PIPELINE_KEYS if data.get(key) is not None]
    if len(present) > 1:
        _refuse("key-conflict", "$",
                "`pipeline:` and its transitional alias `workflow:` both "
                "declared (keep `pipeline:`)")
    if not present:
        _refuse("key-missing", "$.pipeline",
                "required key (the pipeline block; `workflow:` is accepted "
                "as a transitional alias)")
    key = present[0]
    block = data[key]
    if not isinstance(block, Mapping):
        _refuse("value-type", "$.%s" % key,
                "object expected (prose is not a pipeline)")
    return block


def pipeline_of_page(text: str) -> Mapping:
    """L103 (#4395) : le bloc d'une carte `Pipeline` (`pipelines/*.md`).

    Même lecture que `workflow_of_page` (lecteur du canon), mais le type est
    `Pipeline` et le bloc est obligatoire (`pipeline_block`)."""
    data = _frontmatter(text, PIPELINE_TYPE)
    return pipeline_block(data)


def _frontmatter(text: str, kind: str) -> Mapping:
    from . import canon

    try:
        frontmatter = canon.split_frontmatter(text)
    except canon.YamlError as exc:
        _refuse("frontmatter-invalid", "$", str(exc))
    if frontmatter is None:
        _refuse("frontmatter-missing", "$",
                "YAML frontmatter required (type: %s, then %s:)"
                % (kind, "workflow" if kind == "Process" else "pipeline"))
    try:
        data = canon.load_yaml(frontmatter)
    except canon.YamlError as exc:
        _refuse("frontmatter-invalid", "$", str(exc))
    if not isinstance(data, Mapping):
        _refuse("value-type", "$", "frontmatter object expected")
    if data.get("type") != kind:
        _refuse("value-type", "$.type",
                "`type: %s` expected on a %s card" % (kind, kind))
    return data


def compile_block(workflow: Mapping, *, ref: str,
                  version: int = 0) -> tuple[CompiledWorkflow, CanonWorkflow | None]:
    """Compile un bloc (forme canon, ou native si `stages` est une liste) :
    l'artefact et l'encodage canon (None pour la forme native)."""
    if isinstance(workflow.get("stages"), list):
        return compile_workflow(workflow, ref=ref, version=version), None
    encoding = encode_canon_workflow(workflow)
    return compile_workflow(encoding.document, ref=ref, version=version), encoding


def check_page(path: str, *, ref: str | None = None, version: int = 0) -> CheckResult:
    """Lit une carte, encode son workflow et le compile (sans état natif)."""
    workflow_ref = ref if ref is not None else "file:%s" % path
    _validate_pin(workflow_ref, version)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except UnicodeDecodeError as exc:
        _refuse("frontmatter-invalid", "$",
                "card is not valid UTF-8: %s" % exc)
    workflow = workflow_of_page(text)
    if workflow is None:
        return CheckResult(path=path, ref=workflow_ref, version=version,
                           artifact=None, encoding=None)
    artifact, encoding = compile_block(workflow, ref=workflow_ref, version=version)
    return CheckResult(path=path, ref=workflow_ref, version=version,
                       artifact=artifact, encoding=encoding,
                       source_schema=encoding.source_schema if encoding else artifact.schema)


def artifact_document(artifact: CompiledWorkflow) -> dict:
    """Le document natif équivalent à un artefact compilé (pour `--json`)."""
    return {
        "schema": artifact.schema,
        "initial": artifact.initial,
        "types": list(artifact.types),
        "stages": [{"status": stage.status, "core": stage.core}
                   for stage in artifact.stages],
        "transitions": [{"from": transition.source, "to": transition.target,
                         "guard": transition.guard}
                        for transition in artifact.transitions],
        "projection": {
            "project": artifact.projection.project,
            "status_field": artifact.projection.status_field,
            "product_field": artifact.projection.product_field,
            "issue_open": list(artifact.projection.issue_open),
            "issue_completed": list(artifact.projection.issue_completed),
            "issue_not_planned": list(artifact.projection.issue_not_planned),
        },
    }


def result_dict(result: CheckResult) -> dict:
    """Résultat sérialisable : document natif, libellés, résumé compilé."""
    out: dict[str, Any] = {"path": result.path, "workflow_ref": result.ref,
                           "workflow_version": result.version}
    if result.artifact is None:
        out["workflow"] = None
        return out
    artifact = result.artifact
    document = artifact_document(artifact)
    out.update({
        "source_schema": result.source_schema,
        "document": document,
        "labels": dict(result.encoding.stage_labels) if result.encoding else {},
        "type_aliases": dict(result.encoding.type_aliases) if result.encoding else {},
        "initial": artifact.initial,
        "types": list(artifact.types),
        "stages": document["stages"],
        "edges": len(artifact.transitions),
        "projection": document["projection"],
    })
    return out


def _print_human(result: CheckResult) -> None:
    if result.artifact is None:
        print("%s: no structured workflow (prose-only Process card); "
              "nothing to compile" % result.path)
        return
    artifact = result.artifact
    print("%s: OK — %d stages, %d transitions, %d types"
          % (result.path, len(artifact.stages), len(artifact.transitions),
             len(artifact.types)))
    print("  source schema: %s" % (result.source_schema or "(native)"))
    print("  initial: %s" % artifact.initial)
    labels = result.encoding.stage_labels if result.encoding else {}
    stages = []
    for stage in artifact.stages:
        label = labels.get(stage.status)
        stages.append("%s/%s%s" % (stage.status, stage.core,
                                   ' "%s"' % label if label else ""))
    print("  stages: %s" % ", ".join(stages))
    print("  types: %s" % ", ".join(artifact.types))
    projection = artifact.projection
    print("  projection: open=[%s] completed=[%s] not_planned=[%s]"
          % (", ".join(projection.issue_open),
             ", ".join(projection.issue_completed),
             ", ".join(projection.issue_not_planned)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh process",
        description="Check the executable workflow block of a canon Process card.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check", help="encode and compile the workflow block of a processus/ page")
    check.add_argument("path", help="Process card (Markdown with YAML frontmatter)")
    check.add_argument("--ref", default=None,
                       help="Process revision reference (default: file:<path>)")
    check.add_argument("--version", type=int, default=0,
                       help="Process revision version (integer >= 0; default 0)")
    check.add_argument("--json", action="store_true",
                       help="print the compiled summary and native document as JSON")
    args = parser.parse_args(argv)

    try:
        result = check_page(args.path, ref=args.ref, version=args.version)
    except WorkflowRefusal as exc:
        print("%s: %s" % (args.path, exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print("%s: cannot read: %s" % (args.path, exc.strerror or exc),
              file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result_dict(result), ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
