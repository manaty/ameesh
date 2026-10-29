# SPDX-License-Identifier: AGPL-3.0-only
"""L98 tranche 1 (#4308): the Module interface and the registry of modules.

The core reads modules through this registry, never by importing a module
implementation (L97 boundary, ``tests/test_generic_core.py``). A module is
discovered either built in (``it``) or through an ``ameesh.modules`` entry
point (#2847); each one is described by a read-only `Module`: name, version,
declared capabilities, CLI entry point and storage entry point.

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
    source: str                      # "builtin" or "external"
    capabilities: tuple[str, ...]
    cli: str | None                  # command line served, or None
    storage: tuple[str, ...]         # work item types stored for the module
    distribution: str | None = None
    entry_point: str | None = None
    descriptor_sha256: str = ""

    def as_dict(self):
        return {"name": self.name, "version": self.version, "source": self.source,
                "capabilities": list(self.capabilities), "cli": self.cli,
                "storage": {"types": list(self.storage)},
                "distribution": self.distribution, "entry_point": self.entry_point,
                "descriptor_sha256": self.descriptor_sha256}


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


def discover(*, builtin=True):
    """All discovered modules, by name; ``builtin=False`` is the bare profile.

    Same refusals as `ameesh.modules.installed()`, but per entry: a refused
    entry point is listed in `Registry.refused` instead of raising."""
    modules, refused = [], []
    builtins = _builtin() if builtin else {}
    for name, module in sorted(builtins.items()):
        compiled = _validate_descriptor(name, module.descriptor({}), allow_empty=True)
        _check_types(name, module, compiled)
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
            modules.append(_describe(name, module, compiled, source="external",
                                     version=version, distribution=dist,
                                     entry_point=row["entry_point"]))
        except ValueError as exc:
            refused.append(dict(row, error=str(exc)))
    return Registry(tuple(modules), tuple(refused))


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


def admits(db, project, kind):
    """Whether a module active for ``project`` stores work items of ``kind``.

    L98 T3 (#4406): read for the core (`work add`), without module semantics."""
    from .storage import ModuleStore
    return ModuleStore(db).admits(project, kind)


def milestones(db, item_id, limit=100):
    """The module milestones recorded on a work item, newest first (`work show`)."""
    from .storage import ModuleStore
    return ModuleStore(db).milestones(item_id, limit)


def main(argv):
    """`ameesh modules [--json]`: read-only list of the discovered modules."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="ameesh modules",
        description="Discovered modules (built in and ameesh.modules entry points), "
                    "with version and capabilities; read only.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    registry = discover()
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
