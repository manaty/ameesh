# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in projection of a pinned Process lot onto its single issue (#2130, D).

Data only: the lot's ``workflow_ref``/``workflow_stage``/``workflow_version``
and an **admitted, compiled** artifact decide the issue state and its board
label. Nothing here reads or writes the database, runs a verifier or resolves a
canonical path: the caller supplies the mapping, exactly as slice B takes its
``spec`` from the caller (N5). Outside the opt-in — no admitted mapping, or an
unpinned lot — the historical L126 projection is untouched.

Rules
-----
* A pinned lot's ``work_items.state`` is a **core**, or a hold (``blocked``,
  ``waiting_human``) that keeps the stage. The core comes from the artifact
  (``stage.core``). For a stored ``delivered`` row, the compatibility phase
  retained in ``legacy_state`` is compared with that historical artifact core.
  A hold is read from the artifact; outside a hold the row phase must agree
  with it (``projection-core-mismatch``).
* A lot closed by the native path (``work close``: the row is ``closed`` while
  the stage's core is not) is decided by its native ``close_reason``:
  ``delivered`` completes it (#988, delivered outside the target),
  ``abandoned``/``superseded`` are not planned, and an absent or unknown reason
  refuses (``projection-classification``) rather than guessing.
* The artifact's ``projection`` classifies cores: ``issue_open`` -> ``open``
  (so ``merged`` — staging, preprod — stays **open**), ``issue_completed`` ->
  ``closed/completed``, ``issue_not_planned`` -> ``closed/not_planned``. The
  native ``closed`` core with ``close_reason=delivered`` is an actual
  completion (#988), not an abandonment.
* Missing or ambiguous facts refuse with a stable code; the caller leaves the
  issue untouched and never falls back to the legacy projection.

Refusals (``ProjectionRefusal.code``):

* ``projection-unmapped`` — no admitted mapping for the lot's ``(ref,
  version)``;
* ``projection-stage-missing`` — a pinned lot without ``workflow_stage``;
* ``projection-stage-undeclared`` — stage absent from the admitted artifact;
* ``projection-core-mismatch`` — the row core diverges from the artifact's;
* ``projection-classification`` — the core is classified nowhere (defence: the
  compiler cannot produce it);
* ``projection-label-missing`` — no admitted board label for the stage
  (defence: the mapping is validated to cover every stage).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .process_workflow import CompiledWorkflow
from . import work_lifecycle

#: Native holds: they keep the stage, and therefore the artifact's core.
HOLDS = ("blocked", "waiting_human")


class ProjectionRefusal(ValueError):
    """A refused projection: a stable code, a path, a readable message."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__("%s at %s: %s" % (code, path, message))
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True)
class ProcessMapping:
    """One admitted consumer mapping: a pinned revision and its board labels."""

    project: str
    ref: str
    version: int
    spec: CompiledWorkflow
    labels: Mapping[str, str]


@dataclass(frozen=True)
class ProjectedIssue:
    """What the single issue of a pinned lot must show."""

    state: str                  # open | closed
    state_reason: str | None    # completed | not_planned
    label: str                  # admitted board label of the current stage
    core: str                   # the core the projection decided from


def _version(value: Any) -> int:
    """The lot's or mapping's Process revision number; anything else is 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value >= 0 else 0


def pinned(item: Mapping[str, Any]) -> bool:
    """A lot is pinned when it carries a Process revision (``workflow_ref``)."""
    return bool(str(item.get("workflow_ref") or "").strip())


def mapping_for(mappings: Iterable[ProcessMapping],
                item: Mapping[str, Any]) -> ProcessMapping | None:
    """The admitted mapping of the lot's exact ``(ref, version)``, or None."""
    ref = str(item.get("workflow_ref") or "").strip()
    if not ref:
        return None
    version = _version(item.get("workflow_version"))
    for mapping in mappings or ():
        if mapping.ref == ref and mapping.version == version:
            return mapping
    return None


def project_issue(item: Mapping[str, Any], mapping: ProcessMapping | None) -> ProjectedIssue:
    """The issue state and board label of a pinned lot, or ``ProjectionRefusal``."""
    ref = str(item.get("workflow_ref") or "").strip()
    version = _version(item.get("workflow_version"))
    if not ref or mapping is None or mapping.ref != ref or mapping.version != version:
        raise ProjectionRefusal("projection-unmapped", "$.workflow_ref",
                                "no admitted mapping for %s@%d" % (ref or "(none)", version))
    stage = str(item.get("workflow_stage") or "").strip()
    if not stage:
        raise ProjectionRefusal("projection-stage-missing", "$.workflow_stage",
                                "pinned lot without a current stage")
    try:
        core = mapping.spec.core_of(stage)
    except KeyError:
        raise ProjectionRefusal("projection-stage-undeclared", "$.workflow_stage",
                                "stage %r absent from the admitted artifact" % stage)
    label = str(mapping.labels.get(stage) or "").strip()
    if not label:
        raise ProjectionRefusal("projection-label-missing", "$.labels",
                                "no admitted board label for stage %r" % stage)
    state = work_lifecycle.phase(item)
    if state in HOLDS or state == core:
        pass                        # the artifact's core decides the issue
    elif state == "closed":
        # #2130/D : un lot pinné fermé par le chemin natif (`work close`) garde
        # son étape ; le motif natif décide, jamais l'état de l'étape. Un motif
        # absent ou inconnu ne permet pas de trancher : refus nommé, aucune
        # écriture (l'exception `delivered` est la livraison hors cible, #988).
        reason = str(item.get("close_reason") or "").strip()
        if reason == "delivered":
            return ProjectedIssue(state="closed", state_reason="completed",
                                  label=label, core="closed")
        if reason in ("abandoned", "superseded"):
            return ProjectedIssue(state="closed", state_reason="not_planned",
                                  label=label, core="closed")
        raise ProjectionRefusal(
            "projection-classification", "$.close_reason",
            "native closed lot without a usable close_reason (%r): completed and "
            "not_planned cannot be told apart" % (reason or "(none)"))
    else:
        raise ProjectionRefusal(
            "projection-core-mismatch", "$.state",
            "row core %r diverges from the artifact core %r of stage %r"
            % (state, core, stage))
    projection = mapping.spec.projection
    if core in projection.issue_open:
        issue_state, reason = "open", None
    elif core in projection.issue_completed:
        issue_state, reason = "closed", "completed"
    elif core in projection.issue_not_planned:
        delivered = core == "closed" \
            and str(item.get("close_reason") or "").strip() == "delivered"
        issue_state, reason = "closed", ("completed" if delivered else "not_planned")
    else:
        raise ProjectionRefusal("projection-classification", "$.state",
                                "core %r is classified nowhere" % core)
    return ProjectedIssue(state=issue_state, state_reason=reason, label=label, core=core)
