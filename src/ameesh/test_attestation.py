# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned test evidence, independent of storage, Git and merge authority.

The execution-report producer supplies the payload; ``seal`` is not an authority
signature. Store the envelope outside the tested tree (including its manifest).
Consumers must independently resolve the candidate's exact tree and supply its
expected scope. Patch IDs and ancestry are deliberately never reuse criteria.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA = "ameesh-test-attestation/1"
SCOPE_FIELDS = (
    "plan", "selection_digest", "tests", "canon_digest", "registry_digest",
    "runner_digest", "profile", "drivers", "database_schema", "fixtures",
    "exclusions",
)
FIELDS = {"schema", "source_sha", "base_sha", "tree", "change_id", "lots",
          "scope", "results", "timestamp", "links", "patch_id"}


class AttestationError(ValueError):
    """Malformed or altered evidence; ``cause`` is suitable for reports."""

    def __init__(self, cause: str):
        self.cause = cause
        super().__init__(cause)


def canonical_json(value: Any) -> str:
    """UTF-8 JSON, sorted object keys, ordered arrays, no non-finite numbers."""
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(k) is str for k in item):
            for child in item.values():
                check(child)
            return
        raise AttestationError("invalid-json")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value):
    return type(value) is str and bool(value.strip())


def _oid(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise AttestationError("invalid-timestamp") from None


def _scope(scope):
    if type(scope) is not dict or set(scope) != set(SCOPE_FIELDS):
        raise AttestationError("invalid-scope")
    if type(scope["plan"]) is not dict or not scope["plan"]:
        raise AttestationError("invalid-plan")
    for key in ("selection_digest", "canon_digest", "registry_digest", "runner_digest"):
        if not (type(scope[key]) is str and re.fullmatch(r"[0-9a-f]{64}", scope[key])):
            raise AttestationError("invalid-" + key)
    for key in ("profile", "database_schema"):
        if not _text(scope[key]):
            raise AttestationError("invalid-" + key)
    for key in ("drivers", "fixtures", "exclusions"):
        if type(scope[key]) is not dict:
            raise AttestationError("invalid-" + key)
    tests = scope["tests"]
    if (type(tests) is not list or not tests or not all(_text(t) for t in tests)
            or len(set(tests)) != len(tests)):
        raise AttestationError("invalid-tests")
    canonical_json(scope)


def _payload(payload):
    if type(payload) is not dict or set(payload) != FIELDS:
        raise AttestationError("invalid-fields")
    if payload["schema"] != SCHEMA:
        raise AttestationError("unsupported-schema")
    for key in ("source_sha", "base_sha", "tree"):
        if not _oid(payload[key]):
            raise AttestationError("invalid-" + key)
    if not _text(payload["change_id"]):
        raise AttestationError("invalid-change-id")
    if payload["patch_id"] is not None and not _text(payload["patch_id"]):
        raise AttestationError("invalid-patch-id")
    lots = payload["lots"]
    if type(lots) is not list:
        raise AttestationError("invalid-lots")
    seen = set()
    for lot in lots:
        if (type(lot) is not dict or set(lot) != {"id", "sha"}
                or not _text(lot["id"]) or not _oid(lot["sha"]) or lot["id"] in seen):
            raise AttestationError("invalid-lots")
        seen.add(lot["id"])
    _scope(payload["scope"])
    results = payload["results"]
    if type(results) is not dict or set(results) != set(payload["scope"]["tests"]):
        raise AttestationError("invalid-results")
    if any(v not in ("passed", "failed", "inconclusive", "skipped") for v in results.values()):
        raise AttestationError("invalid-results")
    if type(payload["links"]) is not list or not all(_text(x) for x in payload["links"]):
        raise AttestationError("invalid-links")
    _timestamp(payload["timestamp"])
    canonical_json(payload)


def seal(payload: dict) -> dict:
    """Validate and detach a producer payload; attach its corruption digest."""
    _payload(payload)
    return {"payload": json.loads(canonical_json(payload)), "digest": digest(payload)}


def validate(envelope: dict) -> dict:
    """Return a detached payload, or a named error; does not authenticate origin."""
    if type(envelope) is not dict or set(envelope) != {"payload", "digest"}:
        raise AttestationError("invalid-envelope")
    if digest(envelope["payload"]) != envelope["digest"]:
        raise AttestationError("artifact-altered")
    _payload(envelope["payload"])
    return json.loads(canonical_json(envelope["payload"]))


@dataclass(frozen=True)
class ReuseResult:
    reusable: bool
    causes: tuple[str, ...]
    source_sha: str
    candidate_sha: str


def reuse(envelope: dict, *, candidate_sha: str, tree: str, scope: dict,
          now: datetime, max_age_seconds: float | None = None) -> ReuseResult:
    """Compare exact content and scope; age limit is supplied by canon policy.

    A new SHA with the same tree is allowed, retaining both provenance SHAs.
    Lots/base/change ID are provenance, not substitutes for the resolved tree.
    A non-conclusive result is never green, even when reuse is not a gate.
    """
    payload = validate(envelope)
    _scope(scope)
    if not _oid(candidate_sha) or not _oid(tree):
        raise AttestationError("invalid-candidate")
    if now.tzinfo is None:
        raise AttestationError("invalid-now")
    causes = []
    if payload["tree"] != tree:
        causes.append("tree-changed")
    for field in SCOPE_FIELDS:
        if canonical_json(payload["scope"][field]) != canonical_json(scope[field]):
            causes.append(field.replace("_", "-") + "-changed")
    if any(v != "passed" for v in payload["results"].values()):
        causes.append("tests-not-green")
    if payload["scope"]["exclusions"]:
        causes.append("tests-excluded")
    age = (now - _timestamp(payload["timestamp"])).total_seconds()
    if age < 0:
        causes.append("timestamp-in-future")
    if max_age_seconds is not None:
        if type(max_age_seconds) not in (int, float) or not 0 <= max_age_seconds < float("inf"):
            raise AttestationError("invalid-max-age")
        if age > max_age_seconds:
            causes.append("expired")
    return ReuseResult(not causes, tuple(causes), payload["source_sha"], candidate_sha)
