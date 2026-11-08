# SPDX-License-Identifier: AGPL-3.0-only
"""A2/A3: pure, explainable proposals; no lot, decision or repair is executed.

Signal metadata (including cause_key) is not a diagnosis or authority. Callers
supply structured facts separately, with the signal's evidence reference. A
proposed diagnosis stays with the pilot. Exact matches only suggest existing
lots; the fingerprint writer retains responsibility for atomic deduplication.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import re
from typing import Iterable

from .repair_signals import Signal

SCHEMA = "repair-triage/1"
SUMMARY_SCHEMA = "repair-triage-summary/1"
NATURES = ("product_failure", "negative_contract", "infrastructure",
           "expected_refusal", "evolution", "question", "non_blocking", "unknown")
CONFIDENCES = ("confirmed", "proposed", "unknown")
# No new priority policy: retain a supplied existing priority, otherwise P2.
_ROUTES = {"product_failure": "lot", "negative_contract": "ignored",
           "infrastructure": "unknown", "expected_refusal": "ignored",
           "evolution": "decision", "question": "unknown",
           "non_blocking": "basket", "unknown": "unknown"}
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")


@dataclass(frozen=True)
class QualificationFacts:
    """Explicit diagnostic facts, not fields extracted from untrusted text.

    ``evidence_ref`` must identify this signal. Discriminators are optional
    technical labels, never titles/logs. A negative contract is only qualified
    as such when already covered. An inconclusive test is infrastructure, never
    evidence of a product failure. Return codes alone have no meaning here.
    """
    nature: str = "unknown"
    confidence: str = "unknown"
    evidence_ref: str = ""
    component: str = ""
    test: str = ""
    reason: str = ""
    covered: bool = False
    inconclusive: bool = False
    existing_priority: int | None = None

    def __post_init__(self):
        if self.nature not in NATURES or self.confidence not in CONFIDENCES:
            raise ValueError("unsupported qualification category")
        for value in (self.component, self.test, self.reason):
            if not isinstance(value, str) or (value and not _TOKEN.fullmatch(value)):
                raise ValueError("a bounded technical discriminator is required")
        if not isinstance(self.evidence_ref, str):
            raise ValueError("an evidence reference is required")
        if type(self.covered) is not bool or type(self.inconclusive) is not bool:
            raise ValueError("qualification flags must be booleans")
        if self.existing_priority is not None and (
                type(self.existing_priority) is not int
                or self.existing_priority not in (1, 2, 3)):
            raise ValueError("existing priority must be 1, 2 or 3")


@dataclass(frozen=True)
class Triage:
    signal_id: str
    project: str
    environment: str
    source: str
    evidence_ref: str
    nature: str
    priority: int
    confidence: str
    route: str
    reason: str
    cause_key: str = ""
    component: str = ""
    test: str = ""
    discriminator: str = ""

    def record(self) -> dict:
        return {"schema": SCHEMA, **asdict(self)}


def qualify(signal: Signal, facts: QualificationFacts | None = None) -> Triage:
    """Propose a route without changing the signal's durable outcome.

    Every route is advisory, including lot/basket/decision. Neither priority,
    diagnosis nor a candidate match grants permission to execute a repair.
    Missing, proposed or unbound evidence stays explicitly unknown.
    """
    if not isinstance(signal, Signal):
        raise TypeError("a validated repair signal is required")
    if facts is not None and not isinstance(facts, QualificationFacts):
        raise TypeError("structured qualification facts are required")
    facts = facts or QualificationFacts()
    nature, confidence, reason = "unknown", "unknown", "missing_evidence"
    if facts.evidence_ref == signal.evidence_ref:
        nature, confidence, reason = facts.nature, facts.confidence, "structured_facts"
        if facts.inconclusive:
            nature, reason = "infrastructure", "inconclusive"
        elif nature == "negative_contract" and not facts.covered:
            nature, confidence, reason = "unknown", "unknown", "coverage_unproven"
        if confidence != "confirmed":
            reason = "diagnosis_unconfirmed"
    route = _ROUTES[nature] if confidence == "confirmed" else "unknown"
    # Unknown remains unknown even if a caller claims confirmed confidence.
    if nature == "unknown":
        confidence = "unknown"
    bound = facts.evidence_ref == signal.evidence_ref
    return Triage(signal.id, signal.project, signal.environment, signal.source,
                  signal.evidence_ref, nature, facts.existing_priority or 2,
                  confidence, route, reason,
                  signal.cause_key if bound else "",
                  facts.component if bound else "", facts.test if bound else "",
                  facts.reason if bound else "")


@dataclass(frozen=True)
class Candidate:
    """An existing lot with its qualified cause (not a free-text title)."""
    lot: int
    triage: Triage

    def __post_init__(self):
        if type(self.lot) is not int or self.lot <= 0:
            raise ValueError("a positive lot id is required")
        if not isinstance(self.triage, Triage):
            raise TypeError("a qualified candidate is required")


def _cause(item: Triage) -> tuple | None:
    # Generic command keys / return codes are insufficient to merge causes.
    # Require component + stable reason, and retain the test if supplied.
    if (item.confidence != "confirmed" or item.nature == "unknown"
            or not item.cause_key or not item.component or not item.discriminator):
        return None
    return (item.project, item.environment, item.source, item.nature,
            item.cause_key, item.component, item.test, item.discriminator)


def exact_candidates(item: Triage, candidates: Iterable[Candidate]) -> tuple[int, ...]:
    """Conservative prefilter, not a semantic merge or recurrence verdict.

    Version and occurrence do not identify a cause. Active-version correlation
    and terminal/open lot handling belong to U4/U7, never inferred here.
    """
    key = _cause(item)
    if key is None:
        return ()
    return tuple(sorted({candidate.lot for candidate in candidates
                         if _cause(candidate.triage) == key}))


def summary(items: Iterable[Triage]) -> dict:
    """Count qualification receipts, not defects or repair successes.

    Each signal identity is counted once; conflicting qualifications remain
    unknown rather than selecting the last input. Occurrences are not defects.
    """
    by_id = {}
    conflicts = set()
    for item in items:
        if not isinstance(item, Triage):
            raise TypeError("qualified signals are required")
        if item.signal_id in by_id and by_id[item.signal_id] != item:
            conflicts.add(item.signal_id)
        by_id[item.signal_id] = item
    natures, routes, confidences = Counter(), Counter(), Counter()
    for ident, item in by_id.items():
        natures["unknown" if ident in conflicts else item.nature] += 1
        routes["unknown" if ident in conflicts else item.route] += 1
        confidences["unknown" if ident in conflicts else item.confidence] += 1
    return {"schema": SUMMARY_SCHEMA, "signals": len(by_id),
            "conflicts": len(conflicts), "by_nature": dict(sorted(natures.items())),
            "by_route": dict(sorted(routes.items())),
            "by_confidence": dict(sorted(confidences.items())),
            "unit": "distinct_signal_identity", "effects": "none"}
