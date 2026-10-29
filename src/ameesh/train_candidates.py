# SPDX-License-Identifier: AGPL-3.0-only
"""QA inventory, qualification and explicit packet enrollment through #1756.

A candidate is neither a queue generation nor historical review/CI evidence.
The published branch, exact provenance and policy still need qualification
before enrollment; no provider or live runner profile is selected here.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import copy
from dataclasses import asdict

from . import lot_class, merge_queue, packet_guard, storage
from .train import TrainError
from .train_admissions import Requirement


class Inventory:
    def __init__(self, db, project, *, candidate):
        if not callable(candidate):
            raise TrainError("explicit freeze_candidate reader required")
        target = merge_queue._remote_branch(project.target)
        if not target:
            raise TrainError("explicit candidate inventory target required")
        self.ops, self.project = storage.of(db).work, project
        self.candidate, self.target = candidate, target

    def read(self):
        # Le lecteur de gel reçoit la dispense `-none` du pilote (#2681), lue
        # par la même fonction que la porte ; il ne recopie pas la règle.
        candidates = []
        # A UI page limit would silently omit ready lots at the cutoff.
        for item in (row for state in lifecycle.TO_VALIDATE
                     for row in self.ops.items(state=state, assignee=None, limit=None)):
            if item["app"] != self.project.name:
                continue
            target = merge_queue._remote_branch(item.get("branch_target") or self.target)
            marks = self.ops.milestones(item["id"], None)
            candidate = self.candidate(marks, waived=merge_queue.is_waived(item))
            if not isinstance(candidate, dict) or not isinstance(candidate.get("sha"), str) \
                    or not isinstance(candidate.get("causes"), list):
                raise TrainError("candidate reader returned no diagnostic for lot #%s" % item["id"])
            candidate = copy.deepcopy(candidate)
            causes = list(candidate["causes"])
            if not item.get("branch"):
                causes.append("missing-branch")
            elif packet_guard.is_packet_branch(item["branch"]):
                # #2908 : le porteur d'un paquet (`integration/**`) est le
                # véhicule, jamais une unité d'un autre paquet (même refus
                # que le composeur, packet_candidates).
                causes.append("packet-vehicle")
            if target != self.target:
                causes.append("target-mismatch")
            release = lot_class.release_reason(item, target)
            if release:
                causes.append("release: " + release)
            candidates.append(dict(work_item_id=item["id"], branch=item.get("branch") or "",
                                   target=target, candidate=candidate, causes=causes))
        return dict(schema="ameesh-train-candidates/1", project=self.project.name,
                    source=merge_queue.source_of(self.project.repo), target=self.target,
                    candidates=candidates)


class Preparation:
    """Read-only qualification before enrollment in the existing queue.

    No queue generation is fabricated, and a verdict-derived candidate cannot
    substitute for a declared freeze. The result is not a train admission.
    """

    def __init__(self, db, project, *, repository, history, policy):
        if history.db is not db or history.path != repository.clone.path:
            raise TrainError("candidate history must share storage and fetched Git objects")
        if repository.project is not project or not callable(policy):
            raise TrainError("explicit candidate repository and review policy required")
        self.ops, self.project = storage.of(db).work, project
        self.repository, self.history, self.policy = repository, history, policy

    def prepare(self, candidate, base):
        if candidate["causes"]:
            raise TrainError("candidate refused: " + "; ".join(candidate["causes"]))
        origin = candidate["candidate"]
        declared = origin.get("declared")
        if origin.get("origin") != "frozen" or not isinstance(declared, dict) \
                or declared.get("kind") != "frozen" or type(declared.get("id")) is not int:
            raise TrainError("candidate requires a real declared freeze before enrollment")
        item = self.ops.get(candidate["work_item_id"])
        target = merge_queue._remote_branch(self.project.target)
        if item is None or item["state"] not in lifecycle.TO_VALIDATE or item["app"] != self.project.name \
                or not item.get("branch") or item["branch"] != candidate["branch"] \
                or candidate["target"] != target \
                or merge_queue._remote_branch(item.get("branch_target") or target) != target:
            raise TrainError("candidate no longer belongs to this QA branch, project or target")
        if lot_class.release_reason(item, target):
            raise TrainError("release lots cannot enter an automatic train")
        subject = dict(work_item_id=item["id"], branch=item["branch"], target=target,
                       source=self.repository.source, frozen_id=declared["id"],
                       frozen_sha=origin["sha"])
        if self.repository.target(target) != base:
            raise TrainError("candidate preparation requires the exact current target base")
        try:
            published, path = merge_queue.branch_tip(
                self.project.repo, subject["branch"], self.repository.clone)
        except merge_queue.QueueError as exc:
            raise TrainError(str(exc)) from exc
        head = merge_queue.pinned(path, subject["frozen_sha"])
        if head is None or head != published:
            raise TrainError("candidate #%s branch=%s frozen=%s published=%s" % (
                item["id"], item["branch"], origin["sha"], published or "missing"))
        subject["frozen_sha"] = head
        requirement = self.policy(item, subject, base, self.history.path)
        if not isinstance(requirement, Requirement):
            raise TrainError("trusted policy returned no structured review requirement")
        requirement.check(subject, base)
        historical = self.history.qualify_subject(
            subject, base, required=requirement.reviews, klass=requirement.klass())
        return dict(schema="ameesh-train-candidate-preparation/1", subject=subject,
                    item=dict({key: item.get(key) for key in ("app", "branch", "branch_target", "type")},
                              resolved_target=target),
                    policy=asdict(requirement), history=historical)


def _deferred(candidate, error):
    """A refused candidate with the facts an operator needs (#3139).

    The reason alone cannot tell a pilot which freeze or branch was refused
    once the enrollment result is journaled without its state. Every field is
    read from the inventory candidate, never guessed from the work item. The
    declared freeze and the effective candidate are both kept: a verdict on a
    remise can name another head than the freeze the train requires.
    """
    origin = candidate.get("candidate") if isinstance(candidate.get("candidate"), dict) else {}
    declared = origin.get("declared") if isinstance(origin.get("declared"), dict) else {}
    frozen_id = declared.get("id")
    return dict(
        work_item_id=candidate.get("work_item_id"),
        branch=candidate.get("branch") or "",
        frozen_sha=declared.get("sha") or origin.get("sha") or "",
        frozen_id=frozen_id if type(frozen_id) is int else None,
        candidate_sha=origin.get("sha") or "",
        candidate_origin=origin.get("origin") or "",
        reason=str(error),
    )


def _recoverable(row, subject) -> bool:
    """#3560: the radar queue ejected this freeze's packet allocation for want
    of a lot CI. `queue_add` requeues it under the same locks; this read-only
    mirror lets the shadow plan present it as the active enrollment would."""
    if row is None or not row.get("packet_allocated") \
            or not storage.interface.ci_required_ejection(row):
        return False
    keys = ("target", "frozen_id", "frozen_sha", "source")
    return all(row.get(key) == subject.get(key) for key in keys)


class Enrollment:
    """Explicit packet enrollment; no runner or live queue is installed."""

    def __init__(self, preparation, admissions, *, initial=None):
        if admissions.history is not preparation.history or admissions.project is not preparation.project \
                or admissions.policy is not preparation.policy:
            raise TrainError("enrollment and admission must share exact qualification providers")
        self.preparation, self.admissions = preparation, admissions
        if initial is not None and not callable(initial):
            raise TrainError("explicit initial queue-history qualification reader required")
        self.initial = initial

    def enroll_many(self, candidates, base, *, actor, lease=None):
        ids = [row["work_item_id"] for row in candidates]
        if len(ids) != len(set(ids)):
            raise TrainError("ambiguous candidate enrollment batch")
        admitted, deferred = [], []
        for candidate in candidates:
            try:
                admitted.append(self.enroll(candidate, base, actor=actor, lease=lease))
            except TrainError as exc:
                deferred.append(_deferred(candidate, exc))
        return dict(admitted=admitted, deferred=deferred)

    def decide(self, candidate, base):
        """Read-only preparation and initial-history decision; no allocation.

        The shadow mode uses the same decision as the active enrollment: a
        candidate refused here (withdrawn freeze, unknown legacy history,
        moved branch, incomplete reviews) can never be presented as a packet
        member the train would enroll. ``enroll`` performs the write with the
        returned prepared subject and decision.
        """
        prepared = self.preparation.prepare(candidate, base)
        subject = prepared["subject"]
        # Historical queue_remove deleted its row without a typed receipt.
        # A qualified reader must resolve that legacy ambiguity; an absent
        # reader/unknown answer only permits replay of a recorded allocation.
        initial = (self.initial(self.preparation.history.db, subject=copy.deepcopy(subject))
                   if self.initial is not None else None)
        # `queue_add` can only replay a row that matches a recorded packet
        # allocation. Without that receipt, any existing row (e.g. an
        # ejection) makes the active enrollment refuse; mirror that here so
        # the shadow plan never presents it as a member.
        current = next((row for row in self.preparation.ops.queue(None)
                        if row["work_item_id"] == subject["work_item_id"]), None)
        if current is not None:
            initial = False
        return dict(prepared=prepared, initial=initial,
                    recover=_recoverable(current, subject))

    def _refusal(self, subject, initial):
        """#3560 (b): the exact cause of a refused enrollment, read after the
        refusal (`queue_add` only returns None). An existing row comes first:
        it forces `initial=False`, and an ejected one blocks every later
        enrollment until it is removed and the lot is frozen again, unless
        it is this freeze's allocation ejected for want of a lot CI, which
        `queue_add` requeues."""
        lot = subject["work_item_id"]
        try:
            current = next((row for row in self.preparation.ops.queue(None)
                            if row["work_item_id"] == lot), None)
        except Exception:  # noqa: BLE001 - the cause stays generic, the refusal holds
            current = None
        if current is not None:
            reason = str(current.get("reason") or "").strip()
            return ("an existing queue row blocks it (state %s, generation %s, freeze %s%s): "
                    "`ameesh work queue remove %s`, then freeze again on the same head"
                    % (current.get("state") or "?", current.get("generation"),
                       str(current.get("frozen_sha") or "")[:12],
                       ", reason: " + reason[:160] if reason else "", lot))
        try:
            kinds = {receipt["kind"] for receipt in self.preparation.history.db.query(
                "SELECT kind FROM integration_train_queue_receipts WHERE work_item_id = %s"
                " AND frozen_id IS NOT DISTINCT FROM %s", (int(lot), subject.get("frozen_id")))}
        except Exception:  # noqa: BLE001 - unreadable receipts: the generic cause
            kinds = None
        if kinds:
            return ("the train's allocation of this freeze was withdrawn (%s receipt): freeze "
                    "again to enroll it" % "/".join(sorted(kinds)))
        if kinds is not None and initial is not True:
            return ("queue-history-unknown (no receipt for this freeze and no qualified "
                    "initial absence)")
        return ("reservation lost, or the subject or freeze changed, or queue-history-unknown")

    def enroll(self, candidate, base, *, actor, lease=None):
        decision = self.decide(candidate, base)
        prepared, initial = decision["prepared"], decision["initial"]
        subject = prepared["subject"]
        row = self.preparation.ops.queue_add(subject["work_item_id"], target=subject["target"],
            frozen_sha=subject["frozen_sha"], frozen_id=subject["frozen_id"], source=subject["source"],
            packet=prepared["item"], packet_initial=initial is True,
            actor=actor, note="packet enrollment; complete packet CI remains due", lease=lease)
        if row is None:
            raise TrainError("packet enrollment #%s refused: %s"
                             % (subject["work_item_id"], self._refusal(subject, initial)))
        self.preparation.repository.check_members(subject["target"], [row])
        # This rereads the real generation and complete evidence after the
        # allocation transaction. The runtime repeats it before any effect.
        proof = self.admissions.qualify([row], base, "packet")
        return dict(entry=row, proof=proof)
