# SPDX-License-Identifier: AGPL-3.0-only
"""A5 (#6636): project policy, pure admission and explicit observation refusals.

Canon location: ``projects.<project>.repair_policy`` in mechanics.PATH::

    schema: repair-policy/1
    paused: false
    rules: []

This is the initial, observation-only declaration. A future authorized rule
requires every field of Rule below (lists in YAML, resources a mapping of
cpu_seconds and memory_bytes). No project inherits another project's policy.
Unknown fields/versions or incomplete rules disable the whole policy.

The durable admission switch is ``paused`` at the exact canon commit. Reload
with load() before each NEW admission; this module never cancels an engaged
action. Existing execution protocols own safe completion, leases and actual
resource enforcement. Consumers must also intersect this policy with existing
runner capabilities and budgets; this reader raises none of those limits.
Admission is no substitute for review/merge gates.

Signals are data only. Kind, severity, actual diff paths, capabilities and
resource estimates come from the caller's separately established repair plan,
not authority claimed in a signal. No signal field is merged into the policy.
The reader accepts N-1 mechanics documents without this optional block; a
future unknown policy version remains observation-only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import re

from . import repair_signals, review

SCHEMA = "repair-policy/1"
POLICY_KEYS = ("schema", "paused", "rules")


@dataclass(frozen=True)
class Resources:
    cpu_seconds: int
    memory_bytes: int


@dataclass(frozen=True)
class Rule:
    name: str
    sources: tuple[str, ...]
    environments: tuple[str, ...]
    kinds: tuple[str, ...]
    severities: tuple[str, ...]
    paths: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    capabilities: tuple[str, ...]
    max_attempts: int
    max_seconds: int
    resources: Resources


@dataclass(frozen=True)
class Policy:
    project: str
    rules: tuple[Rule, ...] = ()
    paused: bool = True
    reason: str = "policy-absent"
    detail: str = "No project repair policy: observation only"
    commit: str = ""

    def to_dict(self) -> dict:
        """Diagnostic snapshot, including observation/refusal reason."""
        return dict(asdict(self), schema=SCHEMA)


@dataclass(frozen=True)
class Request:
    """Actual proposed plan; attempt is one-based, budgets are totals."""

    kind: str
    severity: str
    paths: tuple[str, ...]
    capabilities: tuple[str, ...]
    attempt: int
    max_seconds: int
    resources: Resources


@dataclass(frozen=True)
class Admission:
    allowed: bool
    code: str
    detail: str
    rule: str = ""


def _strings(value, field, *, empty=False, patterns=False):
    if not isinstance(value, list) or (not value and not empty):
        raise ValueError(field + ": explicit list required")
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip() \
                or any(ord(c) < 32 or ord(c) == 127 for c in item):
            raise ValueError(field + ": nonempty bounded labels required")
        if len(item) > 300:
            raise ValueError(field + ": label too long")
        if patterns:
            if not _path(item):
                raise ValueError(field + ": repository-relative patterns required")
            review.compile_glob(item)
    if len(set(value)) != len(value):
        raise ValueError(field + ": duplicate entries")
    return tuple(value)


def _path(path):
    return (isinstance(path, str) and bool(path) and path == path.strip()
            and not path.startswith("/") and "\\" not in path and ":" not in path
            and not any(ord(c) < 32 or ord(c) == 127 for c in path)
            and all(part not in ("", ".", "..") for part in path.split("/")))


def _positive(value):
    return type(value) is int and value > 0


def _resources(value):
    if not isinstance(value, dict) or set(value) != {"cpu_seconds", "memory_bytes"} \
            or not all(_positive(v) for v in value.values()):
        raise ValueError("resources: positive cpu_seconds and memory_bytes required")
    return Resources(**value)


def compile_policy(value, *, project: str) -> Policy:
    """Compile canon data only. Invalid input is a named observation state."""
    if value is None:
        return Policy(project)
    if value == {}:
        return Policy(project, reason="policy-empty", detail="Empty policy: observation only")
    if not isinstance(value, dict):
        return Policy(project, reason="policy-invalid", detail="Policy must be a mapping")
    if value.get("schema") != SCHEMA:
        return Policy(project, reason="policy-version-unknown",
                      detail="Unsupported repair policy schema: observation only")
    try:
        if set(value) != set(POLICY_KEYS):
            raise ValueError("policy: unknown or missing field")
        if type(value["paused"]) is not bool or not isinstance(value["rules"], list):
            raise ValueError("policy: explicit boolean paused and list rules required")
        rules = []
        for data in value["rules"]:
            if not isinstance(data, dict) or set(data) != set(Rule.__dataclass_fields__):
                raise ValueError("rule: unknown or missing field")
            name = data["name"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
                raise ValueError("rule: invalid name")
            if any(r.name == name for r in rules):
                raise ValueError("rule: duplicate name")
            fields = {key: _strings(data[key], key, empty=key in ("excluded_paths", "capabilities"),
                                    patterns=key in ("paths", "excluded_paths"))
                      for key in ("sources", "environments", "kinds", "severities",
                                  "paths", "excluded_paths", "capabilities")}
            if not set(fields["sources"]) <= set(repair_signals.SOURCES):
                raise ValueError("sources: unsupported source")
            if not all(_positive(data[k]) for k in ("max_attempts", "max_seconds")):
                raise ValueError("rule: positive attempt and time limits required")
            rules.append(Rule(name=name, **fields, max_attempts=data["max_attempts"],
                              max_seconds=data["max_seconds"], resources=_resources(data["resources"])))
        if not rules:
            return Policy(project, paused=value["paused"], reason="policy-empty",
                          detail="No repair rules: observation only")
        return Policy(project, tuple(rules), value["paused"], reason="", detail="")
    except (ValueError, TypeError, re.error) as exc:
        return Policy(project, reason="policy-invalid", detail=str(exc))


def _matches(path, patterns):
    return any(review.compile_glob(p).fullmatch(path) for p in patterns)


def admit(policy: Policy, signal: repair_signals.Signal, request: Request) -> Admission:
    """Pure check. A single complete rule must authorize the entire plan.

    Refusals explain the first failing condition of every candidate rule;
    several rules cannot pool their capabilities, scope or budgets.
    """
    if policy.reason:
        return Admission(False, policy.reason, policy.detail)
    if policy.paused:
        return Admission(False, "admissions-paused", "Durable canon switch stops new admissions")
    if not isinstance(signal, repair_signals.Signal) or signal.project != policy.project:
        return Admission(False, "project-mismatch", "Signal must belong to the policy project")
    if not isinstance(request, Request) or not request.paths \
            or not isinstance(request.paths, tuple) or not all(_path(p) for p in request.paths) \
            or any(any(c in p for c in "*?[]") for p in request.paths) \
            or not isinstance(request.capabilities, tuple) \
            or not all(isinstance(c, str) and c for c in request.capabilities) \
            or not _positive(request.attempt) or not _positive(request.max_seconds) \
            or not isinstance(request.resources, Resources) \
            or not _positive(request.resources.cpu_seconds) \
            or not _positive(request.resources.memory_bytes):
        return Admission(False, "request-invalid", "Complete plan with concrete paths and positive budgets required")
    failures = []
    for rule in policy.rules:
        checks = (
            (signal.source in rule.sources, "source-not-allowed"),
            (signal.environment in rule.environments, "environment-not-allowed"),
            (request.kind in rule.kinds, "kind-not-allowed"),
            (request.severity in rule.severities, "severity-not-allowed"),
            (not any(_matches(p, rule.excluded_paths) for p in request.paths), "path-excluded"),
            (all(_matches(p, rule.paths) for p in request.paths), "path-not-allowed"),
            (set(request.capabilities) <= set(rule.capabilities), "capability-not-allowed"),
            (request.attempt <= rule.max_attempts, "attempt-limit"),
            (request.max_seconds <= rule.max_seconds, "time-limit"),
            (request.resources.cpu_seconds <= rule.resources.cpu_seconds, "cpu-limit"),
            (request.resources.memory_bytes <= rule.resources.memory_bytes, "memory-limit"),
        )
        refused = next((code for ok, code in checks if not ok), "")
        if not refused:
            return Admission(True, "allowed", "Entire plan authorized by canon rule", rule.name)
        failures.append((rule.name, refused))
    return Admission(False, failures[0][1] if failures else "policy-empty",
                     "; ".join(name + ": " + reason for name, reason in failures)
                     or "No repair rules: observation only")


def load(repo: str, commit: str, project: str) -> Policy:
    """Reload the exact canon commit for a new admission, without a cache.

    Unreadable canon, unknown project and unsupported N+1 mechanics documents
    disable admission while leaving observation independent of this reader.
    """
    from . import canon, mechanics

    try:
        compiled = mechanics.load(repo, commit, project)
    except (canon.CanonError, OSError, ValueError):
        return Policy(project, reason="policy-unreadable",
                      detail="Exact project canon policy could not be read")
    policy = compiled.repair_policy or Policy(project)
    return replace(policy, commit=compiled.commit)
