# SPDX-License-Identifier: AGPL-3.0-only
"""A2/A3: pure, explainable proposals; no lot or repair is executed.

Signal metadata (including cause_key) is not a diagnosis or authority. Callers
supply structured facts separately, with the signal's evidence reference. A
proposed diagnosis stays with the pilot. Exact matches only suggest existing
lots; the fingerprint writer retains responsibility for atomic deduplication.

U13/A9 adds the one-way bridge to the existing decision queue: a structured
repair plan is filed at most once, and only an exact recorded owner option
(with the signature the plan requires) counts as approval. Silence, a free
text or a reaction never do; the adapter itself executes nothing.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
import time
from typing import Iterable

from . import repair_signals
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


# --------------------------------------------------------------------------
# U13/A9 : raccordement d'un plan de réparation à la file de décisions
# --------------------------------------------------------------------------

#: schéma du plan proposé au propriétaire ; le contenu exact est dans le digest
PLAN_SCHEMA = "repair-plan/1"
#: schéma du reçu rendu par l'adaptateur (création ou lecture d'issue)
DECISION_SCHEMA = "repair-decision/1"
#: domaine de validation déclaré par l'adaptateur (PROJECT-PLAN.md, #4256) :
#: un projet peut y déclarer son remplaçant, sinon la file garde son
#: destinataire ordinaire et l'échéance escalade vers le propriétaire déclaré.
DECISION_DOMAIN = "repair"
#: une clé de plan est un identifiant durable fourni par U9, jamais un texte
_PLAN_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
#: seuls des identifiants de reçus U5 entrent dans le plan, jamais un journal
_SIGNAL_ID_RE = re.compile(r"rs1_[0-9a-f]{32}\Z")
#: bornes alignées sur decisions.py (l'adaptateur les vérifie avant d'écrire)
_OPTION_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}\Z")
_MAX_QUESTION = 4000
_MAX_LABEL = 600
_MAX_WHY = 1000
#: caractères de contrôle interdits (les sauts de ligne et tabulations du
#: texte restent lisibles, comme dans decisions.py)
_BAD_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class RepairDecisionError(RuntimeError):
    """Le plan ne peut pas être raccordé à la file ; rien n'est déposé."""


class RepairPlanConflict(RepairDecisionError):
    """Même clé de plan, contenu différent : l'ancienne demande est conservée."""


@dataclass(frozen=True)
class RepairPlan:
    """Plan structuré soumis à une décision du propriétaire (A6/A9).

    ``key`` est l'identité durable du plan (U9), pas celle d'un signal : un
    rejeu du même plan rend la même demande. ``approve`` nomme la seule option
    qui autorise le plan ; tout autre choix, un texte libre, un silence ou une
    réaction ne l'autorisent jamais. ``signals`` ne porte que des identifiants
    de reçus U5 — aucun corps, pile ou journal brut.
    """
    key: str
    project: str
    environment: str
    lot: int
    question: str
    options: tuple[tuple[str, str], ...]
    approve: str
    recommend: str = ""
    why: str = ""
    signals: tuple[str, ...] = ()
    needs_signature: bool = False
    due_ts: float | None = None

    def __post_init__(self):
        if not isinstance(self.key, str) or not _PLAN_KEY_RE.fullmatch(self.key):
            raise ValueError("plan key: a bounded durable identifier is required")
        try:
            repair_signals._project(self.project)
            repair_signals._environment(self.environment)
        except ValueError as exc:
            raise ValueError("plan project or environment: %s" % exc) from exc
        if type(self.lot) is not int or self.lot <= 0:
            raise ValueError("plan lot: a positive work item number is required")
        if (not isinstance(self.question, str) or not self.question.strip()
                or len(self.question) > _MAX_QUESTION
                or _BAD_CONTROL_RE.search(self.question)):
            raise ValueError("plan question: bounded readable text is required")
        if not isinstance(self.options, tuple) or not self.options:
            raise ValueError("plan options: a nonempty tuple of (key, label) is required")
        keys = []
        for option in self.options:
            if (not isinstance(option, tuple) or len(option) != 2
                    or not isinstance(option[0], str) or not _OPTION_KEY_RE.fullmatch(option[0])
                    or not isinstance(option[1], str) or not option[1].strip()
                    or len(option[1]) > _MAX_LABEL or _BAD_CONTROL_RE.search(option[1])):
                raise ValueError("plan option: a bounded key and label are required")
            if option[0].lower() in keys:
                raise ValueError("plan option: duplicate key")
            keys.append(option[0].lower())
        if not isinstance(self.approve, str) or self.approve not in [o[0] for o in self.options]:
            raise ValueError("plan approve: the exact key of one option is required")
        if self.recommend and self.recommend not in [o[0] for o in self.options]:
            raise ValueError("plan recommend: the exact key of one option is required")
        if (not isinstance(self.why, str) or len(self.why) > _MAX_WHY
                or _BAD_CONTROL_RE.search(self.why) or (self.why and not self.recommend)):
            raise ValueError("plan why: bounded text, only with a recommendation")
        if not isinstance(self.signals, tuple):
            raise ValueError("plan signals: a tuple of U5 receipt ids is required")
        if len(set(self.signals)) != len(self.signals) or not all(
                isinstance(s, str) and _SIGNAL_ID_RE.fullmatch(s) for s in self.signals):
            raise ValueError("plan signals: distinct rs1_ receipt ids are required")
        if type(self.needs_signature) is not bool:
            raise ValueError("plan signature requirement: an explicit boolean is required")
        if self.due_ts is not None:
            try:
                due = float(self.due_ts)
            except (OverflowError, TypeError, ValueError):
                due = float("nan")
            if (isinstance(self.due_ts, bool)
                    or not isinstance(self.due_ts, (int, float))
                    or not math.isfinite(due) or due <= 0):
                raise ValueError("plan deadline: a positive finite instant is required")

    def content(self) -> dict:
        """Le contenu exact que le propriétaire autorise (sans texte brut)."""
        return {
            "schema": PLAN_SCHEMA, "key": self.key, "project": self.project,
            "environment": self.environment, "lot": self.lot,
            "question": self.question,
            "options": [{"key": key, "label": label} for key, label in self.options],
            "approve": self.approve, "recommend": self.recommend or None,
            "why": self.why or None, "signals": sorted(self.signals),
            "needs_signature": self.needs_signature,
            "due_ts": round(float(self.due_ts), 3) if self.due_ts is not None else None,
        }

    def digest(self) -> str:
        """Empreinte stable du contenu : un plan modifié n'hérite pas du choix."""
        material = json.dumps(self.content(), ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def payload(self) -> dict:
        """Métadonnées bornées rangées dans la demande (aucun texte de journal)."""
        return {"schema": PLAN_SCHEMA, "key": self.key, "digest": self.digest(),
                "project": self.project, "environment": self.environment,
                "lot": self.lot, "signals": list(self.signals),
                "approve": self.approve}

    def option_values(self) -> list[str]:
        """Les options au format attendu par ``decisions.ask``."""
        return ["%s=%s" % (key, label) for key, label in self.options]


def _plan_row(db, plan: RepairPlan):
    """La demande déjà déposée pour cette clé de plan, ou None.

    Bornée au lot par l'index ``agent_mailbox_work_item_idx`` : la file ne
    balaie jamais toute la boîte pour retrouver un plan.
    """
    from . import decisions, storage
    for row in storage.of(db).decisions.for_work_item(str(plan.lot)):
        metadata = decisions._payload(row).get("repair_plan")
        if isinstance(metadata, dict) and metadata.get("key") == plan.key:
            return row
    return None


def file_plan_decision(cfg, db, plan: RepairPlan, *, requester: str,
                       now: float | None = None) -> dict:
    """Dépose au plus UNE demande pour ce plan, puis rend la demande.

    Le verrou consultatif (clé de plan) et la création sont dans la même
    transaction : deux appels concurrents ne déposent pas deux demandes. Une
    demande déjà déposée, même close, est rendue telle quelle ; si le contenu
    du plan a changé sous la même clé, l'adaptateur refuse au lieu de réutiliser
    l'ancienne autorité. L'échéance, le rappel et l'escalade restent ceux de
    ``decisions.ask`` et de ``validation_deadlines``.
    """
    from . import decisions, storage, work
    if not isinstance(plan, RepairPlan):
        raise TypeError("a RepairPlan is required")
    if storage.is_remote(db):
        raise RepairDecisionError("owner decisions are filed by the mesh server only")
    if not isinstance(requester, str) or not requester.strip() or len(requester) > 200:
        raise RepairDecisionError("a bounded requester identity is required")
    now = time.time() if now is None else float(now)
    item = work.get(db, plan.lot)
    if item is None:
        raise RepairDecisionError("work item #%d not found" % plan.lot)
    item_project = str(item.get("app") or "").strip()
    if item_project and item_project != plan.project:
        raise RepairDecisionError(
            "plan project %r does not match work item #%d project %r"
            % (plan.project, plan.lot, item_project))
    with db.transaction() as tx:
        tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                 ("ameesh-repair-plan:" + plan.key,))
        existing = _plan_row(tx, plan)
        if existing is not None:
            metadata = decisions._payload(existing).get("repair_plan") or {}
            if metadata.get("digest") != plan.digest():
                raise RepairPlanConflict(
                    "plan key %r was already filed with another content" % plan.key)
            return {"schema": DECISION_SCHEMA, "plan": plan.key,
                    "decision": decisions.view_of(existing, now), "created": False}
        try:
            view = decisions.ask(
                cfg, tx, requester=requester, question=plan.question,
                options=plan.option_values(), lot=str(plan.lot),
                recommend=plan.recommend or None, why=plan.why or None,
                due_ts=plan.due_ts, needs_signature=plan.needs_signature,
                domain=DECISION_DOMAIN, repair_plan=plan.payload(), now=now)
        except decisions.DecisionError as exc:
            raise RepairDecisionError(str(exc)) from exc
        return {"schema": DECISION_SCHEMA, "plan": plan.key, "decision": view,
                "created": True}


def _outcome(plan: RepairPlan, view: dict | None, state: str, approved: bool,
             reason: str, *, detail: str = "") -> dict:
    evidence = view or {}
    answer = evidence.get("answer") or {}
    validation = evidence.get("validation") or {}
    return {
        "schema": DECISION_SCHEMA, "plan": plan.key,
        "decision": evidence.get("id"),
        "decision_state": evidence.get("state"),
        "state": state, "approved": bool(approved), "reason": reason,
        "detail": detail,
        "answer_option": answer.get("option"),
        "due_ts": evidence.get("due_ts"),
        "overdue": bool(evidence.get("overdue")),
        "escalated": validation.get("status") == "escalated",
    }


def plan_decision(db, plan: RepairPlan, *, now: float | None = None) -> dict:
    """L'issue autorisée d'un plan : jamais une approbation par défaut.

    ``state`` vaut ``absent`` (aucune demande), ``pending`` (avant l'échéance,
    sans réponse), ``approved`` (option exacte et preuve valide) ou
    ``not_approved`` (silence à l'échéance, autre option, texte libre, retrait,
    clôture sans réponse, ou contenu du plan changé). Seul ``approved=True``
    autorise la suite ; un appelant ne doit jamais lire ``not_approved``,
    ``pending`` ou un champ du plan comme un accord.
    """
    from . import decisions, storage
    if not isinstance(plan, RepairPlan):
        raise TypeError("a RepairPlan is required")
    if storage.is_remote(db):
        raise RepairDecisionError("owner decisions are read by the mesh server only")
    now = time.time() if now is None else float(now)
    row = _plan_row(db, plan)
    if row is None:
        return _outcome(plan, None, "absent", False, "no_request")
    view = decisions.view_of(row, now)
    metadata = decisions._payload(row).get("repair_plan") or {}
    if metadata.get("digest") != plan.digest():
        return _outcome(plan, view, "not_approved", False, "plan_content_changed")
    if view["state"] == "pending":
        if view.get("overdue"):
            return _outcome(plan, view, "not_approved", False, "no_answer_at_deadline")
        return _outcome(plan, view, "pending", False, "awaiting_owner")
    if view["state"] == "answered":
        try:
            # A replaced signed answer is no authority, even if its option is
            # the authorizing one (the append-only log is the proof).
            decisions.require_current(db, view)
        except decisions.DecisionError:
            return _outcome(plan, view, "not_approved", False, "authority_not_current")
        answer = view.get("answer") or {}
        if answer.get("option") != plan.approve:
            reason = ("free_text_without_approval" if answer.get("option") is None
                      else "other_option")
            return _outcome(plan, view, "not_approved", False, reason)
        if view.get("needs_signature"):
            ok, detail = decisions.verify_proof(db, view)
            if not ok:
                return _outcome(plan, view, "not_approved", False,
                                "invalid_signature", detail=detail)
            return _outcome(plan, view, "approved", True, "signed_owner_option")
        return _outcome(plan, view, "approved", True, "owner_option")
    if view["state"] == "withdrawn":
        return _outcome(plan, view, "not_approved", False, "withdrawn")
    if view["state"] == "closed":
        return _outcome(plan, view, "not_approved", False, "closed_without_answer")
    return _outcome(plan, view, "not_approved", False, "unexpected_state")
