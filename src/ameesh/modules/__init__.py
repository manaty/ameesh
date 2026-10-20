# SPDX-License-Identifier: AGPL-3.0-only
"""Installed, project-scoped lifecycle extensions (L317a).

The canon selects installed names, never executable import paths. Module
descriptors supply types, milestone schemas and generic lifecycle requirements.

The built-in registry holds ameesh's own modules (``it``). An operator may also
install an external descriptor distribution in the same environment; it exposes
an ``ameesh.modules`` entry point whose name is the module name and whose target
is a module (or object) with ``NAME`` and ``descriptor(options)``. External
descriptors are installed code: the canon still selects them by name, and the
SQL module guard and per-project canon admission are unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
from importlib import metadata

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Python entry-point group for externally installed module descriptors.
ENTRY_POINT_GROUP = "ameesh.modules"

#: Keys a descriptor must supply; the SQL policy stores all three.
DESCRIPTOR_KEYS = ("types", "schemas", "rules")


def _builtin():
    from . import it
    return {it.NAME: it}


def _entry_points():
    """Installed external descriptors, across the supported Python versions."""
    points = metadata.entry_points()
    if hasattr(points, "select"):          # Python >= 3.10
        return points.select(group=ENTRY_POINT_GROUP)
    return points.get(ENTRY_POINT_GROUP, ())


def _distribution(entry):
    dist = getattr(entry, "dist", None)
    if dist is None:
        return None, None
    name = getattr(dist, "name", None)
    version = getattr(dist, "version", None)
    if name is None or version is None:
        info = getattr(dist, "metadata", None) or {}
        name = name or info.get("Name")
        version = version or info.get("Version")
    return name, version


def _validate_descriptor(name, descriptor):
    """Refuse a descriptor that cannot become a project policy row."""
    if not isinstance(descriptor, dict):
        raise ValueError("installed module %r: descriptor must be a mapping" % name)
    extra = set(descriptor) - set(DESCRIPTOR_KEYS)
    missing = set(DESCRIPTOR_KEYS) - set(descriptor)
    if extra or missing:
        raise ValueError("installed module %r: descriptor keys must be %s"
                         % (name, ", ".join(DESCRIPTOR_KEYS)))
    types = descriptor["types"]
    if (not isinstance(types, list) or not types
            or any(not isinstance(kind, str) or not NAME.fullmatch(kind)
                   for kind in types)
            or len(set(types)) != len(types)):
        raise ValueError("installed module %r: types must be unique non-empty names"
                         % name)
    if not isinstance(descriptor["schemas"], dict):
        raise ValueError("installed module %r: schemas must be a mapping" % name)
    rules = descriptor["rules"]
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        raise ValueError("installed module %r: rules must be a list of mappings" % name)
    return descriptor


def _digest(descriptor):
    payload = json.dumps(descriptor, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _external(entry):
    """Load and validate one external entry point; any failure is a refusal."""
    name = getattr(entry, "name", None)
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("invalid external module entry point: %r" % (name,))
    try:
        module = entry.load()
    except Exception as exc:  # installed code: its failure is a closed refusal
        raise ValueError("installed module %r cannot be loaded: %s" % (name, exc)) from exc
    declared = getattr(module, "NAME", None)
    if declared != name:
        raise ValueError("installed module %r declares NAME %r" % (name, declared))
    descriptor = getattr(module, "descriptor", None)
    if not callable(descriptor):
        raise ValueError("installed module %r has no descriptor(options)" % name)
    try:
        compiled = descriptor({})
    except Exception as exc:
        raise ValueError("installed module %r descriptor() failed: %s"
                         % (name, exc)) from exc
    _validate_descriptor(name, compiled)
    declared_types = getattr(module, "TYPES", None)
    if declared_types is not None and list(declared_types) != compiled["types"]:
        raise ValueError("installed module %r: TYPES differ from descriptor().types"
                         % name)
    return module, compiled


def installed():
    """Built-in plus externally installed descriptors, by name (fail closed)."""
    registry = _builtin()
    for entry in sorted(_entry_points(), key=lambda point: getattr(point, "name", "")):
        name = getattr(entry, "name", None)
        if name in registry:
            raise ValueError("duplicate installed module: %r" % (name,))
        module, _ = _external(entry)
        registry[name] = module
    return registry


def evidence():
    """Installation proof: source, distribution, entry point and descriptor digest."""
    out = []
    for name, module in sorted(_builtin().items()):
        out.append({"name": name, "source": "builtin", "distribution": None,
                    "version": None, "entry_point": None, "ok": True,
                    "descriptor_sha256": _digest(module.descriptor({}))})
    for entry in sorted(_entry_points(), key=lambda point: getattr(point, "name", "")):
        name = getattr(entry, "name", None)
        dist, version = _distribution(entry)
        row = {"name": name, "source": "external", "distribution": dist,
               "version": version, "entry_point": getattr(entry, "value", None)}
        try:
            _, compiled = _external(entry)
        except ValueError as exc:
            row.update(ok=False, error=str(exc))
        else:
            row.update(ok=True, descriptor_sha256=_digest(compiled))
        out.append(row)
    return out


def declarations(federation):
    """Validate and compile `extensions.ameesh.projects` without side effects."""
    extensions = (federation or {}).get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValueError("extensions must be a mapping")
    extension = extensions.get("ameesh", {})
    if not isinstance(extension, dict):
        raise ValueError("extensions.ameesh must be a mapping")
    projects = extension.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("extensions.ameesh.projects must be a mapping")
    result = []
    registry = installed()
    for project, spec in projects.items():
        if not isinstance(project, str) or not NAME.fullmatch(project):
            raise ValueError("invalid module project: %r" % (project,))
        if not isinstance(spec, dict) or set(spec) - {"modules"}:
            raise ValueError("project %s expects {modules: [installed names]}" % project)
        # L244 (#420): a project may pass options to a module, either as
        # `modules: [it]` (no option) or `modules: {it: {require_qa: [promoted]}}`.
        modules = spec.get("modules", [])
        if isinstance(modules, list):
            if any(not isinstance(m, str) for m in modules):
                raise ValueError("project %s modules must be a list of installed names" % project)
            if len(set(modules)) != len(modules):
                raise ValueError("duplicate modules for project %s" % project)
            options = {name: {} for name in modules}
        elif isinstance(modules, dict):
            options = {}
            for name, module_options in modules.items():
                if not isinstance(name, str):
                    raise ValueError("project %s modules must be a list of installed names "
                                     "or a {name: options} mapping" % project)
                if module_options is None:
                    module_options = {}
                if not isinstance(module_options, dict):
                    raise ValueError("project %s module %s options must be a mapping"
                                     % (project, name))
                options[name] = module_options
        else:
            raise ValueError("project %s modules must be a list of installed names "
                             "or a {name: options} mapping" % project)
        seen_types = set()
        for name, module_options in options.items():
            if name not in registry:
                raise ValueError("unknown installed module: %s" % name)
            descriptor = registry[name].descriptor(module_options)
            _validate_descriptor(name, descriptor)
            if seen_types.intersection(descriptor["types"]):
                raise ValueError("conflicting module types for project %s" % project)
            seen_types.update(descriptor["types"])
            result.append(dict(project=project, module=name, **descriptor))
    return result


def sync(db, canon):
    """Called only inside successful, trusted canon application."""
    from .storage import ModuleStore
    ModuleStore(db).replace(canon.state_key, declarations(canon.federation))


def main(argv):
    import argparse
    import sys
    from .. import config, db as db_mod, work

    if argv[:1] in (["--list"], ["list"]):
        print(json.dumps(evidence(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    try:
        registry = installed()
    except ValueError as exc:
        print("ameesh module: %s" % exc, file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(prog="ameesh module")
    parser.add_argument("name", choices=sorted(registry))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.arguments
    option_args = rest[:rest.index("--")] if "--" in rest else rest
    command = getattr(registry[args.name], "main", None)
    if command is None:
        print("module %s has no command" % args.name, file=sys.stderr)
        return 1
    if any(arg in ("-h", "--help") for arg in option_args):
        return command(None, rest, actor="")
    cfg = config.load()
    db = db_mod.connect(cfg)
    try:
        actor, warning = work.resolve_actor(cfg, db)
        if warning:
            print(warning, file=sys.stderr)
        return command(db, rest, actor=actor)
    except (ValueError, db_mod.DbError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()


def audit(db):
    """Collect findings only from installed modules active in the database."""
    from .storage import ModuleStore
    store = ModuleStore(db)
    registry = installed()
    out = []
    for entry in store.active():
        module = registry.get(entry["module"])
        hook = getattr(module, "audit", None)
        if hook is not None:
            out.extend(hook(store, entry["project"]))
    return out
