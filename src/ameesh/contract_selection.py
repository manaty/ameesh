# SPDX-License-Identifier: AGPL-3.0-only
"""Declarative contract selection (#6179).

Registry schema ``ameesh-contracts/1``: ``rules`` is a nonempty list of
{id, paths, operations, modules}. Paths use case-sensitive fnmatch globs
(``*`` includes slashes); an empty paths list declares a universal rule
whose operations must include every Git status (it applies even to an empty diff).
Operations are Git A/M/D/R/C/T/U statuses. Overlapping path rules are
ambiguous and trigger the conservative fallback; universal rules are additive.

The caller supplies the complete test-module inventory at the *same* candidate
commit (including its project-specific discovery conventions). Fallback selects
that entire inventory and requests full-suite execution; an empty inventory
refuses rather than returning a misleading empty plan. No tests are executed.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
import hashlib
import json
import re

from . import mechanics

SCHEMA = "ameesh-contracts/1"
OPERATIONS = frozenset("AMDRCTU")


class RegistryError(ValueError):
    """Invalid registry or untrustworthy selection inputs."""


def relative_path(value: str) -> bool:
    return (isinstance(value, str) and bool(value) and not value.startswith("/")
            and not any(c in value for c in "\\\x00\n\r:")
            and all(p not in ("", ".", "..") for p in value.split("/")))


@dataclass(frozen=True)
class Rule:
    id: str
    paths: tuple[str, ...]
    operations: tuple[str, ...]
    modules: tuple[str, ...]


@dataclass(frozen=True)
class Registry:
    rules: tuple[Rule, ...]
    digest: str


@dataclass(frozen=True)
class Change:
    operation: str
    path: str
    old_path: str | None = None


@dataclass(frozen=True)
class Selection:
    commit: str
    registry_digest: str | None
    modules: tuple[str, ...]
    # Sorted, immutable module -> reasons and triggering paths.
    evidence: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]
    fallback_reasons: tuple[str, ...]
    full_suite_required: bool

    def to_dict(self) -> dict:
        return {"schema": "ameesh-contract-selection/1", "commit": self.commit,
                "registry_digest": self.registry_digest, "modules": list(self.modules),
                "evidence": {m: {"reasons": list(r), "paths": list(p)}
                             for m, r, p in self.evidence},
                "fallback_reasons": list(self.fallback_reasons),
                "full_suite_required": self.full_suite_required}


def _strings(value, *, empty=False) -> tuple[str, ...]:
    if (not isinstance(value, list) or (not value and not empty)
            or any(not isinstance(v, str) or not v or v.strip() != v for v in value)
            or len(set(value)) != len(value)):
        raise RegistryError("unique nonempty strings required")
    return tuple(sorted(value))


def compile_registry(text: str) -> Registry:
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise RegistryError("duplicate registry key: " + key)
            out[key] = value
        return out
    try:
        data = json.loads(text, object_pairs_hook=unique)
    except (ValueError, TypeError) as exc:
        raise RegistryError(str(exc)) from exc
    if (not isinstance(data, dict) or set(data) != {"schema", "rules"}
            or data["schema"] != SCHEMA or not isinstance(data["rules"], list)
            or not data["rules"]):
        raise RegistryError("expected " + SCHEMA + " and nonempty rules")
    rules = []
    ids = set()
    for item in data["rules"]:
        if not isinstance(item, dict) or set(item) != {"id", "paths", "operations", "modules"}:
            raise RegistryError("invalid rule keys")
        name = item["id"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in ids:
            raise RegistryError("invalid or duplicate rule id")
        ids.add(name)
        paths = _strings(item["paths"], empty=True)
        operations = _strings(item["operations"])
        modules = _strings(item["modules"])
        if any(not relative_path(p) for p in paths) or not set(operations) <= OPERATIONS:
            raise RegistryError("invalid paths or operations")
        if not paths and set(operations) != OPERATIONS:
            raise RegistryError("universal rule requires all operations")
        if any(not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", m) for m in modules):
            raise RegistryError("invalid module name")
        rules.append(Rule(name, paths, operations, modules))
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return Registry(tuple(sorted(rules, key=lambda r: r.id)),
                    hashlib.sha256(canonical.encode()).hexdigest())


def select(registry: Registry | None, changes, inventory, *, commit: str,
           registry_error: str | None = None) -> Selection:
    """Select from a complete inventory bound by the caller to an exact SHA.

    Missing modules and invalid/unknown/ambiguous paths request a full run.
    Additional P0/P1 contracts must be unioned by the plan adapter, never removed.
    """
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise RegistryError("exact candidate commit required")
    if isinstance(inventory, str):
        raise RegistryError("module inventory must be a collection")
    try:
        supplied = tuple(inventory)
    except TypeError as exc:
        raise RegistryError("module inventory must be iterable") from exc
    if not supplied or any(not isinstance(m, str) or not m for m in supplied):
        raise RegistryError("nonempty candidate module inventory required")
    modules = tuple(sorted(set(supplied)))
    reasons: dict[str, set[str]] = {}
    paths: dict[str, set[str]] = {}
    fallback = set()
    triggers = set()

    def add(module, reason, path=None):
        reasons.setdefault(module, set()).add(reason)
        if path is not None:
            paths.setdefault(module, set()).add(path)

    if registry is None:
        fallback.add(registry_error or "registry-absent")
    else:
        missing = {m for r in registry.rules for m in r.modules} - set(modules)
        if missing:
            fallback.add("modules-absent:" + ",".join(sorted(missing)))
    for change in changes:
        if (not isinstance(change, Change) or change.operation not in OPERATIONS
                or not relative_path(change.path)
                or (change.operation in ("R", "C") and not relative_path(change.old_path))
                or (change.old_path is not None and not relative_path(change.old_path))):
            fallback.add("invalid-change")
            continue
        changed_paths = {change.path}
        if change.old_path is not None:
            changed_paths.add(change.old_path)
        triggers.update(changed_paths)
        if registry is None:
            continue
        for path in sorted(changed_paths):
            matches = [r for r in registry.rules if r.paths
                       and change.operation in r.operations
                       and any(fnmatchcase(path, p) for p in r.paths)]
            if len(matches) != 1:
                fallback.add(("unknown-path:" if not matches else "ambiguous-path:") + path)
            for rule in matches:
                for module in rule.modules:
                    add(module, "rule:" + rule.id, path)
    if registry is not None:
        for rule in registry.rules:
            if not rule.paths:
                for module in rule.modules:
                    add(module, "universal:" + rule.id)
    if not reasons:
        fallback.add("empty-selection")
    if fallback:
        for module in modules:
            for reason in fallback:
                add(module, reason)
            paths.setdefault(module, set()).update(triggers)
    selected = tuple(sorted(set(reasons) & set(modules)))
    return Selection(commit, registry.digest if registry else None, selected,
                     tuple((m, tuple(sorted(reasons[m])), tuple(sorted(paths.get(m, ()))))
                           for m in selected), tuple(sorted(fallback)), bool(fallback))


def load_and_select(repo: str, commit: str, policy, changes, inventory, *, deadline=None):
    """Read a regular registry blob at the candidate commit, never the worktree.

    ``policy`` is ``Mechanics.contract_selection``; N-1 (None) falls back.
    Unreadable Git/registry inputs are reported as conservative selections.
    """
    mechanics._exact_commit(repo, commit, deadline=deadline)
    registry = None
    error = "policy-absent"
    if policy is not None:
        try:
            registry = compile_registry(mechanics._regular_blob(
                repo, commit, policy.registry, deadline=deadline))
        except (mechanics.MechanicsError, RegistryError) as exc:
            error = "registry-invalid:" + str(exc)
    return select(registry, changes, inventory, commit=commit, registry_error=error)
