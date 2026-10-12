# SPDX-License-Identifier: AGPL-3.0-only
"""Installed, project-scoped lifecycle extensions (L317a).

The canon selects installed names, never executable import paths. Module
descriptors supply types, milestone schemas and generic lifecycle requirements.
"""
from __future__ import annotations

import re

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def installed():
    from . import it
    return {it.NAME: it}


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
        modules = spec.get("modules", [])
        if not isinstance(modules, list) or any(not isinstance(m, str) for m in modules):
            raise ValueError("project %s modules must be a list of installed names" % project)
        if len(set(modules)) != len(modules):
            raise ValueError("duplicate modules for project %s" % project)
        seen_types = set()
        for name in modules:
            if name not in registry:
                raise ValueError("unknown installed module: %s" % name)
            descriptor = registry[name].descriptor()
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
    parser = argparse.ArgumentParser(prog="ameesh module")
    parser.add_argument("name", choices=sorted(installed()))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    rest = args.arguments
    option_args = rest[:rest.index("--")] if "--" in rest else rest
    if any(arg in ("-h", "--help") for arg in option_args):
        return installed()[args.name].main(None, rest, actor="")
    cfg = config.load()
    db = db_mod.connect(cfg)
    try:
        actor, warning = work.resolve_actor(cfg, db)
        if warning:
            print(warning, file=sys.stderr)
        return installed()[args.name].main(db, rest, actor=actor)
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
