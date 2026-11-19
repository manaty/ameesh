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

from . import (NAME, RESERVED_NAMES, _builtin, _check_types, _digest,
               _distribution, _entry_points, _external, _validate_descriptor)

#: Capabilities a module may declare in ``CAPABILITIES``.
CAPABILITIES = ("lifecycle", "cli", "audit", "workspaces", "release")


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
    action_connectors: tuple[str, ...] = ()
    #: #7663: release model names the module provides through ``release_model``.
    release_models: tuple[str, ...] = ()
    #: P1a #7649: logical schema name, declared migrations and schema
    #: requirements. A module without a migrations folder has no schema.
    schema: str | None = None
    migrations: tuple = ()
    requires: tuple[str, ...] = ()

    def as_dict(self):
        result = {"name": self.name, "version": self.version, "source": self.source,
                "capabilities": list(self.capabilities), "cli": self.cli,
                "storage": {"types": list(self.storage)},
                "distribution": self.distribution, "entry_point": self.entry_point,
                "descriptor_sha256": self.descriptor_sha256,
                "card_ref": self.card_ref,
                "schema": self.schema,
                "migrations": [migration.as_dict() for migration in self.migrations],
                "requires": list(self.requires)}
        if self.action_connectors:
            result["action_connectors"] = list(self.action_connectors)
        if self.release_models:
            result["release_models"] = list(self.release_models)
        return result


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
    release_models = getattr(module, "RELEASE_MODELS", None) or ()
    if release_models:
        # #7663: a module that provides release models must return their
        # provider object lazily; the core never imports the engine.
        if not callable(getattr(module, "release_model", None)):
            raise ValueError("installed module %r: RELEASE_MODELS without "
                             "release_model(name)" % name)
        provided.append("release")
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
    # P1a #7649 : faits de schéma et de migrations, lus sur le descripteur
    # installé (aucun import du module par le cœur).
    from .. import migrations as migrations_mod
    declaration = migrations_mod.module_metadata(name, module)
    return Module(name=name, version=version, source=source,
                  capabilities=capabilities,
                  cli=("ameesh module %s" % name) if "cli" in capabilities else None,
                  storage=tuple(compiled["types"]), distribution=distribution,
                  entry_point=entry_point, descriptor_sha256=_digest(compiled),
                  action_connectors=tuple(sorted(getattr(module, "ACTION_CONNECTORS", {}))),
                  release_models=tuple(sorted(getattr(module, "RELEASE_MODELS", ()) or ())),
                  schema=declaration["schema"],
                  migrations=declaration["migrations"],
                  requires=declaration["requires"])


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
            _action_declarations({**implementations, name: module})
            described = _describe(name, module, compiled, source="external",
                                  version=version, distribution=dist,
                                  entry_point=row["entry_point"])
            implementations[name] = module
            modules.append(described)
        except ValueError as exc:
            refused.append(dict(row, error=str(exc)))
    discovery = (implementations, tuple(modules), tuple(refused))
    return _with_canon(discovery, canon) if canon is not None else discovery


def _with_canon(discovery, canon):
    """`discovery` plus the Pipeline cards of ``canon`` (L103 T2, #4396).

    A valid card becomes a `PipelineModule` of source ``canon``; nothing is
    imported. A card is refused (third item, ``source: "canon"``) when it is
    invalid (including the reserved `list` name) or shares its name with
    another card (``reason: "invalid"``, already a `pipeline-*` finding), or
    when its name is already a discovered module (``reason: "duplicate"``).
    Installed names are never shadowed."""
    implementations, modules, refused = discovery
    implementations, modules, refused = dict(implementations), list(modules), list(refused)
    taken = set(implementations) | {row["name"] for row in refused}
    cards = [p for p in (getattr(canon, "pipelines", None) or ()) if p.name]
    counts = Counter(p.name for p in cards)
    for pipeline in sorted(cards, key=lambda p: (p.name, p.fiche.path)):
        row = {"name": pipeline.name, "source": "canon", "distribution": None,
               "version": None, "entry_point": None, "card_ref": pipeline.fiche.ref}
        if pipeline.name in RESERVED_NAMES:
            refused.append(dict(row, reason="invalid",
                                error="reserved module name: %r (Pipeline card %s); "
                                      "`ameesh module list` keeps that name"
                                      % (pipeline.name, pipeline.fiche.ref)))
            continue
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


def _connector_providers(*, external=False):
    """Delivery providers remain built-in; actions can include installed modules."""
    return _discover()[0] if external else dict(_builtin())


def _action_declarations(providers):
    found = {}
    for provider, module in sorted(providers.items()):
        declarations = getattr(module, "ACTION_CONNECTORS", {})
        if not isinstance(declarations, dict):
            raise ValueError("module %r: ACTION_CONNECTORS must be a mapping" % provider)
        for name, connector in declarations.items():
            if not isinstance(name, str) or not NAME.fullmatch(name) or not callable(connector):
                raise ValueError("module %r: invalid action connector %r" % (provider, name))
            if (name in CORE_ACTION_CONNECTORS or name in found
                    or (name == "git-merge" and module is not _builtin().get("dev"))):
                raise ValueError("duplicate action connector: %r (module %s)" % (name, provider))
            found[name] = (provider, connector)
    return found


def action_connectors():
    """`{name: (provider, connector)}` of the action connectors modules declare
    in ``ACTION_CONNECTORS``; a name taken twice, or by the core, is refused.

    L100 T1 (#4382): the registry is the core's only way to the development
    module's action connectors (`git-merge`); the core keeps `shell-noop`."""
    return _action_declarations(_connector_providers(external=True))


def action_policy(db, project, provider):
    """Applied policy read through the module interface, never its implementation."""
    from .storage import ModuleStore
    return ModuleStore(db).action_policy(project, provider)


def reserved_gesture_detectors():
    """`{operation: (provider, detector)}` of the built-in modules that bring
    the detection of a reserved gesture done outside the gate (#7684).

    A module declares the operations in ``RESERVED_GESTURES`` and serves
    ``detect_reserved_gesture(operation, *, since_ts, until_ts)``; the core
    keeps only the view (`reserved_gestures.journal`) and never imports the
    implementation. A detector that is declared but not callable, or an
    operation declared twice, is refused: a detection hole must not be silent.
    """
    found = {}
    for provider, module in sorted(_builtin().items()):
        declared = getattr(module, "RESERVED_GESTURES", ()) or ()
        if not isinstance(declared, (list, tuple)):
            raise ValueError("module %r: RESERVED_GESTURES must be a list of operations"
                             % provider)
        if not declared:
            continue
        detector = getattr(module, "detect_reserved_gesture", None)
        if not callable(detector):
            raise ValueError("module %r: RESERVED_GESTURES without detect_reserved_gesture"
                             % provider)
        for operation in declared:
            if not isinstance(operation, str) or not NAME.fullmatch(operation):
                raise ValueError("module %r: invalid reserved gesture %r"
                                 % (provider, operation))
            if operation in found:
                raise ValueError("duplicate reserved gesture detector: %r"
                                 % (operation,))
            found[operation] = (provider, detector)
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


def _providers(declared, getter_name, what):
    """`{name: (module_name, getter)}` of one kind of built-in provider.

    Release, notification and train contracts are core runtime paths, like the
    delivery and merge connectors: only built-in tooling modules are read here.
    An installed entry point is loaded by discovery and selected by the canon,
    never by this path. A name declared twice is refused rather than silently
    ordered. ``declared`` is the module attribute listing the names,
    ``getter_name`` the lazy getter returning one provider."""
    found = {}
    for provider, module in sorted(_builtin().items()):
        names = getattr(module, declared, ()) or ()
        if not isinstance(names, (list, tuple)) or any(
                not isinstance(name, str) or not NAME.fullmatch(name) for name in names):
            raise ValueError("module %r: %s must be unique names" % (provider, declared))
        if len(set(names)) != len(names):
            raise ValueError("module %r: %s must be unique names" % (provider, declared))
        getter = getattr(module, getter_name, None)
        if names and not callable(getter):
            raise ValueError("module %r: %s without %s(name)"
                             % (provider, declared, getter_name))
        for name in names:
            if name in found:
                raise ValueError("duplicate %s: %r (modules %s and %s)"
                                 % (what, name, found[name][0], provider))
            found[name] = (provider, getter)
    return found


def _declared_strings(declared, what):
    """`tuple` of unique non-empty strings a built-in module declares (#7813).

    A schema name (``nexlink-fingerprints/1``) or a reserved name of decision
    0026 is a contract string, not a provider: the core reads it to accept an
    old name beside its own generic one, or to keep a name out of approve's RP
    IDs. Only built-in tooling modules are read here, like ``_providers``: the
    bare profile declares none. A name declared twice is refused rather than
    silently ordered."""
    found = {}
    for provider, module in sorted(_builtin().items()):
        names = getattr(module, declared, ()) or ()
        if (not isinstance(names, (list, tuple)) or any(
                not isinstance(name, str) or not name.strip()
                or any(char.isspace() for char in name) for name in names)
                or len(set(names)) != len(names)):
            raise ValueError("module %r: %s must be unique non-empty names"
                             % (provider, declared))
        for name in names:
            if name in found:
                raise ValueError("duplicate %s: %r (modules %s and %s)"
                                 % (what, name, found[name], provider))
            found[name] = provider
    return tuple(sorted(found))


def _release_providers():
    """`{model: (module_name, getter)}` of the built-in release providers."""
    return _providers("RELEASE_MODELS", "release_model", "release model")


def release_models():
    """Names of the release models the built-in modules provide, sorted."""
    return tuple(sorted(_release_providers()))


def release_model(name):
    """The provider object of one release model, or ``None`` when no module
    provides it (#7663).

    The module returns its implementation lazily; importing this function never
    imports a release engine, so the bare profile and discovery stay light. The
    core only knows the model name from its configuration (``ProjectRelease``)
    and reads the provider here, never by importing ``ameesh.modules.nexlink``."""
    found = _release_providers().get(name)
    if found is None:
        return None
    _provider, getter = found
    return getter(name)


def _notify_providers():
    """`{kind: (module_name, getter)}` of the built-in notification channels
    (#7793)."""
    return _providers("NOTIFY_CHANNELS", "notify_channel", "notification channel")


def notify_channel_kinds():
    """Names of the notification channel kinds the built-in modules provide."""
    return tuple(sorted(_notify_providers()))


def notify_channel(kind):
    """The provider object of one notification channel kind, or ``None``
    (#7793).

    The module returns its implementation lazily; importing this function never
    imports a channel implementation, so the bare profile and discovery stay
    light. The core reads channel configuration and sends through this provider,
    never by importing a module package."""
    found = _notify_providers().get(kind)
    if found is None:
        return None
    _provider, getter = found
    return getter(kind)


def train_contract_names():
    """Names of the train CI contracts the built-in modules provide (#7794)."""
    return tuple(sorted(_providers("TRAIN_CONTRACTS", "train_contract",
                                   "train contract")))


def train_contract(name):
    """The factory of one module train CI contract, or ``None`` (#7794).

    ``train_assembly`` selects a contract by the name the host configuration
    gives (``ci_contract``); a project contract lives in its module and is
    imported lazily by the module getter, never by the core."""
    found = _providers("TRAIN_CONTRACTS", "train_contract", "train contract").get(name)
    if found is None:
        return None
    _provider, getter = found
    return getter(name)


def fingerprint_schemas():
    """Historical fingerprint page schemas the built-in modules declare (#7813).

    The core reads its generic ``ameesh-fingerprints/1`` page; a module whose
    application still serves an older name declares it here, so that page stays
    read without changing the application."""
    return _declared_strings("FINGERPRINT_SCHEMAS", "fingerprint page schema")


def dispatch_plan_schemas():
    """Historical CI dispatch-plan schemas the built-in modules declare (#7813).

    The core expects its generic ``ameesh-dispatch-due-parts/1`` plan; a module
    whose repository planner still emits an older name declares it here, so the
    planner stays unchanged on its side."""
    return _declared_strings("DISPATCH_PLAN_SCHEMAS", "dispatch plan schema")


def reserved_zones():
    """Zones a built-in module reserves as never an approve RP ID (#7813).

    Decision 0026 keeps the team-host zone reserved: a parent RP ID would
    cover every team host of the zone. The core compiles no client name; the
    module declares it and ameesh-approve reads it here, beside the host
    configuration's own ``reserved_zones``."""
    return _declared_strings("RESERVED_ZONES", "reserved zone")


def reserved_hosts():
    """Hosts a built-in module reserves as never an approve RP ID (#7813).

    Same contract as :func:`reserved_zones`, for the approval apex of decision
    0026 and any host a module keeps out of RP IDs."""
    return _declared_strings("RESERVED_HOSTS", "reserved host")


def release_reserved_prefixes():
    """Note prefixes reserved to a module's release commands (`work.note`)."""
    prefixes = set()
    for provider, module in sorted(_builtin().items()):
        declared = getattr(module, "RESERVED_PREFIXES", ()) or ()
        if not isinstance(declared, (list, tuple)) or any(
                not isinstance(prefix, str) or not prefix for prefix in declared):
            raise ValueError("module %r: RESERVED_PREFIXES must be non-empty strings"
                             % provider)
        prefixes.update(declared)
    return tuple(sorted(prefixes))


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


def _annotate_module_rows(rows, modules):
    """P1a #7649: `schema_actual` and per-migration `applied` when the base is
    readable. Read-only; without a database the declared fields stand alone."""
    if not any(getattr(module, "migrations", ()) for module in modules):
        return
    import sys
    from .. import config, db as db_mod, migrations as migrations_mod
    try:
        cfg = config.load()
        db = db_mod.connect(cfg, writer=False)
    except (db_mod.Unavailable, OSError, ValueError) as exc:
        print("warning: database not reachable (%s); applied module migrations "
              "unknown" % (" ".join(str(exc).split())[:160],), file=sys.stderr)
        return
    try:
        schemas = migrations_mod.module_schemas(modules, cfg.schema)
        state = migrations_mod.check_modules(db, schemas)
        checks = {check.module: check for check in state.checks}
        for row in rows:
            check = checks.get(row["name"])
            if check is None:
                continue
            row["schema_actual"] = check.registered_schema
            for migration in row["migrations"]:
                migration["applied"] = migration["version"] in check.applied
    except (db_mod.DbError, ValueError) as exc:
        print("warning: module schemas unreadable (%s)"
              % (" ".join(str(exc).split())[:160],), file=sys.stderr)
    finally:
        db.close()


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
        rows = [module.as_dict() for module in registry.modules]
        _annotate_module_rows(rows, registry.modules)
        print(json.dumps({"modules": rows,
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
