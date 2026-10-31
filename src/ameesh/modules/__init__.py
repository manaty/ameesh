# SPDX-License-Identifier: AGPL-3.0-only
"""Installed, project-scoped lifecycle extensions (L317a).

The canon selects installed names, never executable import paths. Module
descriptors supply types, milestone schemas and generic lifecycle requirements.

The built-in registry holds ameesh's own modules (``dev``, ``it``). ``dev`` is
development tooling: it declares no work item type and is always active, while
``it`` is a lifecycle module the canon selects per project. An operator may also
install an external descriptor distribution in the same environment; it exposes
an ``ameesh.modules`` entry point whose name is the module name and whose target
is a module (or object) with ``NAME`` and ``descriptor(options)``. External
descriptors are installed code: the canon still selects them by name, and the
SQL module guard and per-project canon admission are unchanged.

L98 T2a (#4407): a project's modules resolve from three scopes, widest first:
``extensions.ameesh.modules`` (the whole canon), ``extensions.ameesh.teams.<team>``
(the projects its packages and agents carry as ``team``) and
``extensions.ameesh.projects.<project>``. The narrowest scope that names a
module wins its options; a project always wins. The compiled rows remain per
project, so ``project_modules`` and its SQL guard need no migration.

L98 T2b (#4408): ``AMEESH_MODULES=bare`` is the bare profile at run time: no
built-in module, the core alone (entry points are still read). It is a CI and
test profile, never a host option (decision 2, #4405): ``doctor`` refuses it.

L103 T2 (#4396): a valid ``Pipeline`` card of the canon is a declarative module
(source ``canon``, ``registry.PipelineModule``): the canon may select it by name
like an installed module. It stores no work item type, is never imported code,
and may not take the name of an installed module.

The core reads modules through ``registry`` (L98), never by importing a module
implementation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from importlib import metadata

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Python entry-point group for externally installed module descriptors.
ENTRY_POINT_GROUP = "ameesh.modules"

#: Keys a descriptor must supply; the SQL policy stores all three.
DESCRIPTOR_KEYS = ("types", "schemas", "rules")

#: L98 T2b (#4408): run-time switch of the module profile (``bare`` or unset).
PROFILE_ENV = "AMEESH_MODULES"
PROFILES = ("default", "bare")


def profile():
    """The module profile in force: ``default``, ``bare``, or the unknown value.

    An unknown value keeps the default profile (a typo never strips a host of
    its modules); ``doctor`` reports it."""
    return os.environ.get(PROFILE_ENV, "").strip() or "default"


def _builtin():
    if profile() == "bare":
        return {}
    from . import dev, it, work_types
    return {dev.NAME: dev, it.NAME: it, work_types.NAME: work_types}


def always_active():
    """Built-in modules active across the mesh, without a project selection.

    L101 T4: ``dev`` declares no work item type, so the canon cannot select it
    and it never gets a ``project_modules`` row. Such a module marks itself
    ``ALWAYS_ACTIVE = True``; :func:`audit` runs its hook once, host-scoped,
    after the project modules. External entry points are never read here: they
    stay selectable lifecycle extensions.
    """
    return tuple(sorted(name for name, module in _builtin().items()
                        if getattr(module, "ALWAYS_ACTIVE", False)))


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


def _validate_descriptor(name, descriptor, *, allow_empty=False):
    """Refuse a descriptor that cannot become a project policy row.

    A built-in tooling module may declare no work item type (``dev``, L101 T4):
    ``allow_empty`` is for the trusted built-ins only. An installed external
    descriptor is a lifecycle extension and must declare at least one type."""
    if not isinstance(descriptor, dict):
        raise ValueError("installed module %r: descriptor must be a mapping" % name)
    extra = set(descriptor) - set(DESCRIPTOR_KEYS)
    missing = set(DESCRIPTOR_KEYS) - set(descriptor)
    if extra or missing:
        raise ValueError("installed module %r: descriptor keys must be %s"
                         % (name, ", ".join(DESCRIPTOR_KEYS)))
    types = descriptor["types"]
    if (not isinstance(types, list) or (not types and not allow_empty)
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


def _check_types(name, module, descriptor):
    """A static TYPES tuple, when present, must match every compiled policy.

    A descriptor whose types legitimately vary with project options omits
    TYPES: the SQL policy carries the compiled types per project either way."""
    declared = getattr(module, "TYPES", None)
    if declared is not None and list(declared) != descriptor["types"]:
        raise ValueError("installed module %r: TYPES differ from descriptor(options) types"
                         % name)


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
    _check_types(name, module, compiled)
    return module, compiled


def installed(canon=None):
    """Built-in plus externally installed descriptors, by name (fail closed).

    L103 T2 (#4396): with a ``canon``, its valid Pipeline cards are added as
    `registry.PipelineModule` adapters; a card taking an installed name (or
    another card's) is refused, and an invalid card is left out (its
    `pipeline-*` finding already fails `canon check`)."""
    registry = _builtin()
    for entry in sorted(_entry_points(), key=lambda point: getattr(point, "name", "")):
        name = getattr(entry, "name", None)
        if name in registry:
            raise ValueError("duplicate installed module: %r" % (name,))
        module, _ = _external(entry)
        registry[name] = module
    if canon is not None:
        from .registry import _with_canon
        implementations, _modules, refused = _with_canon((registry, (), ()), canon)
        for row in refused:
            if row.get("reason") == "duplicate":
                raise ValueError(row["error"])
        registry = implementations
    return registry


def evidence():
    """Installation proof, with the same refusals as `installed()` per entry."""
    out = []
    builtin = _builtin()
    for name, module in sorted(builtin.items()):
        out.append({"name": name, "source": "builtin", "distribution": None,
                    "version": None, "entry_point": None, "ok": True,
                    "descriptor_sha256": _digest(module.descriptor({}))})
    entries = sorted(_entry_points(), key=lambda point: getattr(point, "name", ""))
    counts = Counter(getattr(entry, "name", None) for entry in entries)
    for entry in entries:
        name = getattr(entry, "name", None)
        dist, version = _distribution(entry)
        row = {"name": name, "source": "external", "distribution": dist,
               "version": version, "entry_point": getattr(entry, "value", None)}
        if name in builtin or counts[name] > 1:
            row.update(ok=False, error="duplicate installed module: %r" % (name,))
        else:
            try:
                _, compiled = _external(entry)
            except ValueError as exc:
                row.update(ok=False, error=str(exc))
            else:
                row.update(ok=True, descriptor_sha256=_digest(compiled))
        out.append(row)
    return out


@dataclass(frozen=True)
class Resolution:
    """Compiled project policies and the refusals that did not block them.

    ``rows`` is one mapping per active ``(project, module)`` pair — the same
    rows ``project_modules`` stores — carrying the winning ``scope`` (and the
    ``team`` name for a team scope). ``warnings`` lists refused entry points
    that no scope activates: the canon stays usable and only says so."""

    rows: tuple
    warnings: tuple


def _selection(raw, label):
    """A module selection: ``[name, …]`` or ``{name: options}``, ordered."""
    if isinstance(raw, list):
        if any(not isinstance(name, str) for name in raw):
            raise ValueError("%s modules must be a list of installed names" % label)
        if len(set(raw)) != len(raw):
            raise ValueError("duplicate modules for %s" % label)
        return {name: {} for name in raw}
    if isinstance(raw, dict):
        options = {}
        for name, module_options in raw.items():
            if not isinstance(name, str):
                raise ValueError("%s modules must be a list of installed names "
                                 "or a {name: options} mapping" % label)
            if module_options is None:
                module_options = {}
            if not isinstance(module_options, dict):
                raise ValueError("%s module %s options must be a mapping"
                                 % (label, name))
            options[name] = module_options
        return options
    raise ValueError("%s modules must be a list of installed names "
                     "or a {name: options} mapping" % label)


def _scopes(federation):
    """`(federation, teams, projects)` selections of `extensions.ameesh`."""
    extensions = (federation or {}).get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValueError("extensions must be a mapping")
    extension = extensions.get("ameesh", {})
    if not isinstance(extension, dict):
        raise ValueError("extensions.ameesh must be a mapping")
    # L98 T2a: the whole canon may enable a module; every scope is read the
    # same way (`[name]` or `{name: options}`), the narrowest one winning.
    federation_modules = _selection(extension.get("modules", []), "federation")
    teams_raw = extension.get("teams", {})
    if not isinstance(teams_raw, dict):
        raise ValueError("extensions.ameesh.teams must be a mapping")
    teams = {}
    for team, spec in teams_raw.items():
        if not isinstance(team, str) or not NAME.fullmatch(team):
            raise ValueError("invalid module team: %r" % (team,))
        if not isinstance(spec, dict) or set(spec) - {"modules"}:
            raise ValueError("team %s expects {modules: [installed names]}" % team)
        teams[team] = _selection(spec.get("modules", []), "team %s" % team)
    projects_raw = extension.get("projects", {})
    if not isinstance(projects_raw, dict):
        raise ValueError("extensions.ameesh.projects must be a mapping")
    projects = {}
    for project, spec in projects_raw.items():
        if not isinstance(project, str) or not NAME.fullmatch(project):
            raise ValueError("invalid module project: %r" % (project,))
        if not isinstance(spec, dict) or set(spec) - {"modules"}:
            raise ValueError("project %s expects {modules: [installed names]}" % project)
        # L244 (#420): a project may pass options to a module, either as
        # `modules: [it]` (no option) or `modules: {it: {require_qa: [promoted]}}`.
        projects[project] = _selection(spec.get("modules", []), "project %s" % project)
    return federation_modules, teams, projects


def _team_projects(canon):
    """`{team: {projects}}` read from the canon's packages and agents.

    A team is the `team` field of the plan's packages and of the agents; the
    projects it covers are those names plus every project of an agent of that
    team (`projects:` when declared, else its own `team`)."""
    by_team = {}
    for package in getattr(canon, "packages", ()) or ():
        team = getattr(package, "team", None)
        if team:
            by_team.setdefault(team, set()).add(team)
    for agent in getattr(canon, "agents", ()) or ():
        team = getattr(agent, "team", None)
        if not team:
            continue
        projects = getattr(agent, "effective_projects", None)
        if projects is None:
            projects = [team]
        by_team.setdefault(team, set()).update(project for project in projects if project)
    return by_team


def _project_universe(canon, declared, teams, team_projects):
    """Every project the canon knows: useful for the federation scope."""
    projects = set(declared)
    for team in teams:
        projects.update(team_projects.get(team, ()))
    if canon is not None:
        for package in getattr(canon, "packages", ()) or ():
            if getattr(package, "team", None):
                projects.add(package.team)
        for agent in getattr(canon, "agents", ()) or ():
            for project in getattr(agent, "effective_projects", ()) or ():
                if project:
                    projects.add(project)
    return projects


def resolve(federation, canon=None, *, discovery=None):
    """Validate and compile module activation into per-project rows.

    `canon` supplies the team → projects mapping (its packages and agents);
    without it a team declaration is refused, since a name alone cannot say
    which projects it covers. `discovery` is the result of
    `registry._discover()` when a caller already loaded it."""
    federation_modules, teams, projects = _scopes(federation)
    if teams and canon is None:
        raise ValueError("extensions.ameesh.teams needs the canon (its packages and "
                         "agents carry the teams)")
    from .registry import PipelineModule, _discover
    implementations, modules, refused = discovery or _discover(canon=canon)
    by_name = {module.name: module for module in modules}
    refused_by_name, shadowing = {}, []
    for row in refused:
        if row.get("reason") == "duplicate":
            # L103 T2: a card taking a discovered name never refuses that
            # module; it is reported whether or not a scope selects the name.
            shadowing.append(row)
            continue
        refused_by_name.setdefault(row["name"], row)
    requested = set(federation_modules)
    for selection in teams.values():
        requested.update(selection)
    for selection in projects.values():
        requested.update(selection)
    # L98 T2a decision 3: a refused entry point only blocks the canon when a
    # scope activates it; otherwise it is a warning (`module-refused`).
    for name in sorted(requested):
        if name in refused_by_name:
            raise ValueError("installed module %r is activated but refused: %s"
                             % (name, refused_by_name[name]["error"]))
        if name not in by_name:
            raise ValueError("unknown installed module: %s (neither installed nor a "
                             "valid Pipeline card)" % name)
    warnings = []
    for name, row in sorted(refused_by_name.items()):
        # L103 T2: an invalid Pipeline card is already a `pipeline-*` finding.
        if name not in requested and row.get("reason") != "invalid":
            warnings.append({
                "code": "module-refused", "name": name,
                "distribution": row.get("distribution"), "version": row.get("version"),
                "error": row["error"],
                "message": "installed module %r is refused and not activated: %s"
                           % (name, row["error"])})
    for row in shadowing:
        warnings.append({
            "code": "pipeline-module-duplicate", "name": row["name"],
            "source": "canon", "card_ref": row.get("card_ref"),
            "distribution": None, "version": None, "error": row["error"],
            "message": "Pipeline card %s takes the name of a discovered module %r: "
                       "the card is not a module" % (row.get("card_ref"), row["name"])})
    team_projects = _team_projects(canon) if canon is not None else {}
    universe = _project_universe(canon, projects, teams, team_projects)
    result = []
    for project in sorted(universe):
        selected = {}

        def choose(selection, scope, team=None):
            for name, options in selection.items():
                selected[name] = (scope, team, options)

        choose(federation_modules, "federation")
        for team in sorted(teams):
            if project in team_projects.get(team, ()):
                choose(teams[team], "team", team)
        choose(projects.get(project, {}), "project")
        if not selected:
            continue
        if not NAME.fullmatch(project):
            raise ValueError("invalid module project: %r" % (project,))
        seen_types = set()
        for name, (scope, team, options) in selected.items():
            module = implementations[name]
            descriptor = module.descriptor(options)
            # L103 T2: a declarative module stores no type (rule of #4357),
            # yet the canon selects it; an installed one must declare types.
            _validate_descriptor(name, descriptor,
                                 allow_empty=isinstance(module, PipelineModule))
            _check_types(name, module, descriptor)
            if seen_types.intersection(descriptor["types"]):
                raise ValueError("conflicting module types for project %s" % project)
            seen_types.update(descriptor["types"])
            row = dict(project=project, module=name, scope=scope, **descriptor)
            if team is not None:
                row["team"] = team
            result.append(row)
    return Resolution(tuple(result), tuple(warnings))


def declarations(federation, canon=None, *, discovery=None):
    """Validate and compile module activation without side effects."""
    return list(resolve(federation, canon, discovery=discovery).rows)


def sync(db, canon):
    """Called only inside successful, trusted canon application."""
    from .storage import ModuleStore
    ModuleStore(db).replace(canon.state_key, declarations(canon.federation, canon))


def active(db=None, canons=()):
    """Active project modules: applied rows when a database is given.

    `project_modules` is the truth of what is applied; the readable canon
    supplies each row's scope and the current descriptor. Without a database
    the same rows are compiled from the local canon. Returns
    ``{"active": [...], "refused": [...], "warnings": [...]}``."""
    from .registry import _discover, _with_canon
    base = _discover()
    refused = base[2]
    by_name = {module.name: module for module in base[1]}
    resolved = {}
    labels = {}
    warnings = []
    for canon in canons:
        label = canon.id or "default"
        labels[canon.state_key] = label
        # L103 T2: each canon adds its own Pipeline cards to the discovery.
        discovery = _with_canon(base, canon)
        for module in discovery[1]:
            by_name.setdefault(module.name, module)
        try:
            resolution = resolve(canon.federation, canon, discovery=discovery)
        except ValueError as exc:
            warnings.append({"code": "module-declaration", "canon": label,
                             "error": str(exc),
                             "message": "canon %s: %s" % (label, exc)})
            continue
        for row in resolution.rows:
            resolved[(canon.state_key, row["project"], row["module"])] = (canon, row)
        warnings.extend(resolution.warnings)
    rows = []
    if db is not None:
        from .storage import ModuleStore
        for stored_row in ModuleStore(db).stored():
            key = (stored_row.get("canon") or "", stored_row["project"],
                   stored_row["module"])
            declared = resolved.get(key)
            module = by_name.get(stored_row["module"])
            rows.append(dict(
                canon=labels.get(key[0], stored_row.get("canon") or "default"),
                scope=declared[1]["scope"] if declared else None,
                team=declared[1].get("team") if declared else None,
                project=stored_row["project"], module=stored_row["module"],
                version=module.version if module is not None else None,
                descriptor_sha256=_digest({"types": stored_row["types"],
                                           "schemas": stored_row["schemas"],
                                           "rules": stored_row["rules"]})))
    else:
        for (canon, row) in resolved.values():
            module = by_name.get(row["module"])
            rows.append(dict(
                canon=canon.id or "default", scope=row["scope"],
                team=row.get("team"), project=row["project"], module=row["module"],
                version=module.version if module is not None else None,
                descriptor_sha256=_digest({"types": row["types"],
                                           "schemas": row["schemas"],
                                           "rules": row["rules"]})))
    rows.sort(key=lambda row: (row["canon"], row["project"], row["module"]))
    return {"active": rows, "refused": [dict(row) for row in refused],
            "warnings": warnings}


def doctor_lines(db, canons=()):
    """The doctor's `modules` section: `(ok, lines)`.

    L98 T2a: an active module that is not installed (or is refused) is an
    error; a stored row that no longer matches the readable canon, and a
    refused entry point that no scope activates, are warnings. The check is
    read-only and never blocks a canon sync."""
    from .registry import _discover, _with_canon
    base = _discover()
    refused = base[2]
    by_name = {module.name: module for module in base[1]}
    refused_by_name = {row["name"]: row for row in refused}
    lines, ok, warnings = [], True, 0
    # L98 T2b decision 2: the bare profile is for CI and tests; a host running
    # it has neither housekeeping nor worktrees.
    current = profile()
    if current == "bare":
        ok = False
        lines.append("modules    : KO — bare profile (%s=bare): CI and test profile "
                     "only, never a host" % PROFILE_ENV)
    elif current not in PROFILES:
        warnings += 1
        lines.append("warning    : unknown %s=%r, default profile kept"
                     % (PROFILE_ENV, current))
    resolved = {}
    seen_refused = set()
    for canon in canons:
        label = canon.id or "default"
        # L103 T2: a stored Pipeline module is known through its canon's card.
        discovery = _with_canon(base, canon)
        for module in discovery[1]:
            by_name.setdefault(module.name, module)
        try:
            resolution = resolve(canon.federation, canon, discovery=discovery)
        except ValueError as exc:
            ok = False
            lines.append("modules    : KO — canon %s: %s" % (label, exc))
            continue
        for row in resolution.rows:
            resolved[(canon.state_key, row["project"], row["module"])] = (canon, row)
        for warning in resolution.warnings:
            if warning["code"] == "pipeline-module-duplicate":
                warnings += 1
                lines.append("warning    : canon %s: %s" % (label, warning["message"]))
                continue
            if warning["name"] in seen_refused:
                continue
            seen_refused.add(warning["name"])
            warnings += 1
            lines.append("warning    : module %s refused and not activated: %s"
                         % (warning["name"], warning["error"]))
    # `dev` is always active without a project row; it must still be installed.
    for name in always_active():
        if name not in by_name:
            ok = False
            lines.append("modules    : KO — always-active module %s is not installed" % name)
    if not canons:
        for name, row in sorted(refused_by_name.items()):
            if name in seen_refused:
                continue
            seen_refused.add(name)
            warnings += 1
            lines.append("warning    : module %s refused and not activated: %s"
                         % (name, row["error"]))
    from .storage import ModuleStore
    stored = ModuleStore(db).stored()
    stored_keys = set()
    for row in stored:
        key = (row.get("canon") or "", row["project"], row["module"])
        stored_keys.add(key)
        if row["module"] not in by_name:
            ok = False
            if row["module"] in refused_by_name:
                reason = "refused: %s" % refused_by_name[row["module"]]["error"]
            else:
                reason = "not installed"
            lines.append("modules    : KO — project %s: active module %s is %s"
                         % (row["project"], row["module"], reason))
            continue
        declared = resolved.get(key)
        if declared is None:
            warnings += 1
            lines.append("warning    : project %s: stored module %s is not declared by a "
                         "readable canon — canon sync to redo"
                         % (row["project"], row["module"]))
            continue
        _canon, declared_row = declared
        if (_digest({"types": row["types"], "schemas": row["schemas"],
                     "rules": row["rules"]})
                != _digest({"types": declared_row["types"],
                            "schemas": declared_row["schemas"],
                            "rules": declared_row["rules"]})):
            warnings += 1
            lines.append("warning    : project %s: installed descriptor of %s differs from "
                         "project_modules — canon sync to redo"
                         % (row["project"], row["module"]))
    for key, (_canon, row) in resolved.items():
        if key not in stored_keys:
            warnings += 1
            lines.append("warning    : project %s: module %s is declared but not applied — "
                         "canon sync to redo" % (row["project"], row["module"]))
    if lines:
        lines.insert(0, "modules    : %d active project module(s), %s"
                     % (len(stored), "KO" if not ok else "%d warning(s)" % warnings))
    else:
        lines.append("modules    : %d active project module(s), no problem" % len(stored))
    return ok, lines


def main(argv):
    import argparse
    import sys
    from .. import config, db as db_mod, work

    if argv[:1] in (["--list"], ["list"]):
        print(json.dumps(evidence(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    try:
        from .. import canon as canon_mod
        from .registry import _discover, _with_canon
        cfg = config.load()
        discovery = _discover()
        # Installed commands (including help) remain usable without a canon.
        # Canon cards cannot shadow these names; canon check reports collisions.
        if not argv or argv[0] not in discovery[0]:
            for loaded in canon_mod.load_configured(cfg):
                discovery = _with_canon(discovery, loaded)
        for row in discovery[2]:
            if row.get('source') != 'canon' or row.get('reason') == 'duplicate':
                raise ValueError(row['error'])
        registry = discovery[0]
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
    if (any(arg in ("-h", "--help") for arg in option_args)
            or getattr(registry[args.name], "OWNS_CONNECTION", False)):
        # L100 T5 (#4386): a module whose commands open their own database
        # and resolve their own actor (`dev`) gets neither from here.
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
    """Collect findings from installed modules active in the database.

    Project-scoped modules come from ``project_modules``. A built-in module
    marked ``ALWAYS_ACTIVE`` (`dev`) has no row there: its hook runs once,
    host-scoped (``project=None``), after them — never once per project, so a
    host finding is not repeated for every project of the mesh."""
    from .storage import ModuleStore
    store = ModuleStore(db)
    registry = installed()
    host_scoped = set(always_active())
    out = []
    for entry in store.active():
        if entry["module"] in host_scoped:
            continue                 # host-scoped, run once below
        module = registry.get(entry["module"])
        hook = getattr(module, "audit", None)
        if hook is not None:
            out.extend(hook(store, entry["project"]))
    for name in sorted(host_scoped):
        module = registry.get(name)
        hook = getattr(module, "audit", None) if module is not None else None
        if hook is not None:
            out.extend(hook(store, None))
    return out
