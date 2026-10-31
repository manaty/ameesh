# SPDX-License-Identifier: AGPL-3.0-only
"""L98 tranche 1 (#4308): the Module interface and the registry of modules.

The core reads modules through this registry, never by importing a module
implementation (L97 boundary, ``tests/test_generic_core.py``). A module is
discovered either built in (``it``) or through an ``ameesh.modules`` entry
point (#2847); each one is described by a read-only `Module`: name, version,
declared capabilities, CLI entry point and storage entry point.

L103 T2 (#4396): a valid `Pipeline` card of the canon (`pipelines/*.md`) is a
declarative module of source ``canon``: data, never imported code. Its
`PipelineModule` adapter stores no work item type (descriptor
``{types: [], schemas: {}, rules: []}``) and serves lifecycle commands through
the core (L103 T3, #4397).

Discovery is read-only and fails closed per entry: a refused entry point is
reported with its error and never becomes a `Module`. The bare profile (no
built-in, no entry point) yields an empty registry without error.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from . import (NAME, _builtin, _check_types, _digest, _distribution, _entry_points,
               _external, _validate_descriptor)

#: Capabilities a module may declare in ``CAPABILITIES``.
CAPABILITIES = ("lifecycle", "cli", "audit", "workspaces")


@dataclass(frozen=True)
class Module:
    """Descriptor of one discovered module (no behaviour, only facts)."""
    name: str
    version: str | None
    source: str                      # "builtin", "external" or "canon"
    capabilities: tuple[str, ...]
    cli: str | None                  # command line served, or None
    storage: tuple[str, ...]         # work item types stored for the module
    distribution: str | None = None
    entry_point: str | None = None
    descriptor_sha256: str = ""
    card_ref: str | None = None      # L103 T2: canon_ref of a Pipeline card

    def as_dict(self):
        return {"name": self.name, "version": self.version, "source": self.source,
                "capabilities": list(self.capabilities), "cli": self.cli,
                "storage": {"types": list(self.storage)},
                "distribution": self.distribution, "entry_point": self.entry_point,
                "descriptor_sha256": self.descriptor_sha256,
                "card_ref": self.card_ref}


class PipelineModule:
    """L103 T2 (#4396): the module face of a valid `Pipeline` card.

    Same surface as an installed descriptor (``NAME``, ``descriptor(options)``),
    built from canon data: the card's types are the ones its workflow covers,
    not types stored by the module, so the descriptor declares none. Its
    ``main`` delegates to the generic core CLI; there is no card-provided code
    or audit hook."""

    CAPABILITIES = ("lifecycle", "cli")
    TYPES = ()

    def __init__(self, pipeline):
        self.NAME = pipeline.name
        self.CARD_REF = pipeline.fiche.ref
        self.pipeline = pipeline

    def main(self, db, argv, *, actor):
        from .. import pipeline_cli
        return pipeline_cli.main(db, argv, actor=actor, module=self)

    def descriptor(self, options=None):
        return {"types": [], "schemas": {}, "rules": []}


@dataclass(frozen=True)
class Registry:
    modules: tuple[Module, ...] = ()
    refused: tuple[dict, ...] = field(default_factory=tuple)

    def names(self):
        return [module.name for module in self.modules]

    def get(self, name):
        return next((module for module in self.modules if module.name == name), None)


def _capabilities(name, module):
    """Declared ``CAPABILITIES``, else the ones the module's attributes provide."""
    provided = ["lifecycle"]
    if callable(getattr(module, "main", None)):
        provided.append("cli")
    if callable(getattr(module, "audit", None)):
        provided.append("audit")
    if callable(getattr(module, "workspaces", None)):
        # L101 T4 (#4357): a tooling module owns the workspaces façade; the core
        # reads it through the registry, never by importing the implementation.
        provided.append("workspaces")
    declared = getattr(module, "CAPABILITIES", None)
    if declared is None:
        return tuple(provided)
    if (not isinstance(declared, (list, tuple))
            or any(item not in CAPABILITIES for item in declared)
            or len(set(declared)) != len(declared)):
        raise ValueError("installed module %r: CAPABILITIES must be unique names among %s"
                         % (name, ", ".join(CAPABILITIES)))
    missing = set(declared) - set(provided)
    if missing:
        raise ValueError("installed module %r declares %s without providing it"
                         % (name, ", ".join(sorted(missing))))
    return tuple(item for item in CAPABILITIES if item in declared)


def _describe(name, module, compiled, *, source, version, distribution=None,
              entry_point=None):
    capabilities = _capabilities(name, module)
    return Module(name=name, version=version, source=source,
                  capabilities=capabilities,
                  cli=("ameesh module %s" % name) if "cli" in capabilities else None,
                  storage=tuple(compiled["types"]), distribution=distribution,
                  entry_point=entry_point, descriptor_sha256=_digest(compiled))


def _core_version():
    from .. import version
    return version()


def _discover(*, builtin=True, canon=None):
    """`(implementations, Module rows, refusals)` for the discovery.

    Same refusals as `ameesh.modules.installed()`, but per entry: a refused
    entry point is listed in the third item instead of raising. `declarations`
    reads the implementations here to compile per-project options without
    reopening `installed()` and its all-or-nothing failure. With a ``canon``,
    its Pipeline cards are added as modules of source ``canon`` (`_with_canon`)."""
    implementations, modules, refused = {}, [], []
    builtins = _builtin() if builtin else {}
    for name, module in sorted(builtins.items()):
        compiled = _validate_descriptor(name, module.descriptor({}), allow_empty=True)
        _check_types(name, module, compiled)
        implementations[name] = module
        modules.append(_describe(name, module, compiled, source="builtin",
                                  version=_core_version()))
    entries = sorted(_entry_points(), key=lambda point: getattr(point, "name", ""))
    counts = Counter(getattr(entry, "name", None) for entry in entries)
    for entry in entries:
        name = getattr(entry, "name", None)
        dist, version = _distribution(entry)
        row = {"name": name, "distribution": dist, "version": version,
               "entry_point": getattr(entry, "value", None)}
        if name in builtins or counts[name] > 1:
            refused.append(dict(row, error="duplicate installed module: %r" % (name,)))
            continue
        try:
            module, compiled = _external(entry)
            implementations[name] = module
            modules.append(_describe(name, module, compiled, source="external",
                                     version=version, distribution=dist,
                                     entry_point=row["entry_point"]))
        except ValueError as exc:
            refused.append(dict(row, error=str(exc)))
    discovery = (implementations, tuple(modules), tuple(refused))
    return _with_canon(discovery, canon) if canon is not None else discovery


def _with_canon(discovery, canon):
    """`discovery` plus the Pipeline cards of ``canon`` (L103 T2, #4396).

    A valid card becomes a `PipelineModule` of source ``canon``; nothing is
    imported. A card is refused (third item, ``source: "canon"``) when it is
    invalid or shares its name with another card (``reason: "invalid"``,
    already a `pipeline-*` finding), or when its name is already a discovered
    module (``reason: "duplicate"``). Installed names are never shadowed."""
    implementations, modules, refused = discovery
    implementations, modules, refused = dict(implementations), list(modules), list(refused)
    taken = set(implementations) | {row["name"] for row in refused}
    cards = [p for p in (getattr(canon, "pipelines", None) or ()) if p.name]
    counts = Counter(p.name for p in cards)
    for pipeline in sorted(cards, key=lambda p: (p.name, p.fiche.path)):
        row = {"name": pipeline.name, "source": "canon", "distribution": None,
               "version": None, "entry_point": None, "card_ref": pipeline.fiche.ref}
        if pipeline.name in taken or counts[pipeline.name] > 1:
            # two cards of one name are `pipeline-duplicate` already: invalid
            refused.append(dict(row, reason=("duplicate" if pipeline.name in taken
                                             else "invalid"),
                                error="duplicate module: %r (Pipeline card %s)"
                                      % (pipeline.name, pipeline.fiche.ref)))
            continue
        if not pipeline.valid or not NAME.fullmatch(pipeline.name):
            refused.append(dict(row, reason="invalid",
                                error="Pipeline card %s is invalid: %s"
                                      % (pipeline.fiche.ref,
                                         pipeline.error or "see canon check")))
            continue
        module = PipelineModule(pipeline)
        compiled = _validate_descriptor(pipeline.name, module.descriptor({}),
                                        allow_empty=True)
        implementations[pipeline.name] = module
        modules.append(Module(name=pipeline.name, version=None, source="canon",
                              capabilities=PipelineModule.CAPABILITIES,
                              cli="ameesh module %s" % pipeline.name,
                              storage=(), descriptor_sha256=_digest(compiled),
                              card_ref=pipeline.fiche.ref))
    return implementations, tuple(modules), tuple(refused)


def discover(*, builtin=True, canon=None):
    """All discovered modules, by name; ``builtin=False`` is the bare profile.

    Same refusals as `ameesh.modules.installed()`, but per entry: a refused
    entry point is listed in `Registry.refused` instead of raising. With a
    ``canon``, its valid Pipeline cards are modules of source ``canon``."""
    _, modules, refused = _discover(builtin=builtin, canon=canon)
    return Registry(modules, refused)


def get(name):
    """A built-in module implementation by name, or None in the bare profile.

    L101 T4 (#4357): the core obtains a built-in module's façade through the
    registry (``registry.get("dev")``), never by importing the implementation
    (L97 boundary). Only built-ins are read here: an installed entry point is
    loaded by discovery and selected by the canon, not by a core runtime path.
    """
    return _builtin().get(name)


#: L99 T2 (#4350): delivery connectors owned by the core itself (`work.deliver`)
CORE_CONNECTORS = ("document", "done")
#: L100 T1 (#4382): action connectors owned by the core itself (`connectors.get`)
CORE_ACTION_CONNECTORS = ("shell-noop",)


def _connector_providers():
    """Built-in providers of action and delivery connectors: the lifecycle
    modules plus the development tooling (`dev`, now a built-in module).
    Installed entry points are not read: a connector path is reviewed core
    code."""
    return dict(_builtin())


def action_connectors():
    """`{name: (provider, connector)}` of the action connectors modules declare
    in ``ACTION_CONNECTORS``; a name taken twice, or by the core, is refused.

    L100 T1 (#4382): the registry is the core's only way to the development
    module's action connectors (`git-merge`); the core keeps `shell-noop`."""
    found = {}
    for provider, module in sorted(_connector_providers().items()):
        for name, connector in sorted((getattr(module, "ACTION_CONNECTORS", None)
                                       or {}).items()):
            if not NAME.fullmatch(name) or not callable(connector):
                raise ValueError("module %r: invalid action connector %r"
                                 % (provider, name))
            if name in CORE_ACTION_CONNECTORS or name in found:
                raise ValueError("duplicate action connector: %r" % (name,))
            found[name] = (provider, connector)
    return found


def delivery_connectors():
    """`{name: (provider, deliver)}` of the connectors modules declare in
    ``DELIVERY_CONNECTORS``; a name taken twice, or by the core, is refused."""
    found = {}
    for provider, module in sorted(_connector_providers().items()):
        for name, deliver in sorted((getattr(module, "DELIVERY_CONNECTORS", None)
                                     or {}).items()):
            if not NAME.fullmatch(name) or not callable(deliver):
                raise ValueError("module %r: invalid delivery connector %r"
                                 % (provider, name))
            if name in CORE_CONNECTORS or name in found:
                raise ValueError("duplicate delivery connector: %r" % (name,))
            found[name] = (provider, deliver)
    return found


def merge_connector():
    """Name of the connector of a merge on the target (gate actions, `merged`)."""
    module = get("dev")
    return getattr(module, "MERGE_CONNECTOR", "git-merge")


def enabled(db, project, name):
    """Whether a module is active for this project (generic lifecycle admission)."""
    from .storage import ModuleStore
    return ModuleStore(db).enabled(project, name)


def admits(db, project, kind):
    """Whether a module active for ``project`` stores work items of ``kind``.

    L98 T3 (#4406): read for the core (`work add`), without module semantics."""
    from .storage import ModuleStore
    return ModuleStore(db).admits(project, kind)


def milestones(db, item_id, limit=100):
    """The module milestones recorded on a work item, newest first (`work show`)."""
    from .storage import ModuleStore
    return ModuleStore(db).milestones(item_id, limit)


def _active_payload():
    """Active module rows, from `project_modules` or the local canon.

    L98 T2a: `project_modules` is the applied truth; the configured canons
    supply the scope and the descriptor compared by `doctor`. No database
    (or an unreadable one) falls back to the canon compiled locally."""
    import sys
    from . import active as active_modules
    from .. import config, db as db_mod
    from .. import canon as canon_mod
    cfg = config.load()
    canons = []
    try:
        canons = canon_mod.load_configured(cfg)
    except Exception as exc:  # a reading never prevents the local fallback
        print("warning: canon not read (%s)"
              % (" ".join(str(exc).split())[:160],), file=sys.stderr)
    try:
        db = db_mod.connect(cfg)
    except db_mod.Unavailable as exc:
        db = None
        print("warning: database not reachable (%s); reading the local canon"
              % (" ".join(str(exc).split())[:160],), file=sys.stderr)
    try:
        if db is not None:
            try:
                payload = active_modules(db, canons)
            except db_mod.DbError as exc:
                print("warning: database unreadable (%s); reading the local canon"
                      % (" ".join(str(exc).split())[:160],), file=sys.stderr)
                payload = active_modules(None, canons)
        else:
            payload = active_modules(None, canons)
    finally:
        if db is not None:
            db.close()
    return payload, bool(canons), db is not None


def _canon_registry():
    """The discovered modules plus the configured canons' Pipeline cards.

    L103 T2 (#4396): `ameesh modules` reads the canon by default; a canon that
    cannot be read only warns, and the installed modules are still listed."""
    import sys
    from .. import config
    from .. import canon as canon_mod
    try:
        canons = canon_mod.load_configured(config.load())
    except Exception as exc:  # a reading never prevents the installed listing
        print("warning: canon not read (%s)"
              % (" ".join(str(exc).split())[:160],), file=sys.stderr)
        canons = []
    discovery = _discover()
    for canon in canons:
        discovery = _with_canon(discovery, canon)
    return Registry(discovery[1], discovery[2])


def main(argv):
    """`ameesh modules [--json] [--active] [--no-canon]` — discovered or active
    modules."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="ameesh modules",
        description="Modules: discovered (built in, ameesh.modules entry points and "
                    "the canon's Pipeline cards), "
                    "with version and capabilities, or active (applied project "
                    "policies); read only.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--active", action="store_true",
                        help="active project modules (project_modules, or the local "
                             "canon without a database)")
    parser.add_argument("--no-canon", action="store_true",
                        help="installed modules only, without the canon's Pipeline "
                             "cards (L103)")
    args = parser.parse_args(argv)
    if args.active:
        payload, have_canon, have_db = _active_payload()
        if not payload["active"] and not have_canon and not have_db:
            print("ameesh modules --active: no canon and no database", file=sys.stderr)
            return 1
        for warning in payload["warnings"]:
            print("warning: %s" % warning["message"], file=sys.stderr)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        elif not payload["active"]:
            print("no active module")
        else:
            for row in payload["active"]:
                scope = row["scope"] or "-"
                if row.get("team"):
                    scope = "%s:%s" % (scope, row["team"])
                print("%-10s %-14s %-12s %-10s %-10s %s" % (
                    row["canon"], scope, row["project"], row["module"],
                    row["version"] or "-", row["descriptor_sha256"]))
        return 0
    registry = discover() if args.no_canon else _canon_registry()
    if args.json:
        print(json.dumps({"modules": [module.as_dict() for module in registry.modules],
                          "refused": list(registry.refused)},
                         ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if not registry.modules and not registry.refused:
            print("no module")
        for module in registry.modules:
            print("%-16s %-12s %-8s %s" % (module.name, module.version or "-",
                                           module.source,
                                           ",".join(module.capabilities)))
        for row in registry.refused:
            print("%-16s refused: %s" % (row["name"], row["error"]))
    return 1 if registry.refused else 0



def development_workflow():
    """The built-in dev module's workflow, without importing it in the core."""
    provider = get("dev")
    if provider is None:
        raise ValueError("development workflow requires the dev module")
    return provider.workflow()
