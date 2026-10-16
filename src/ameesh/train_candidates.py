# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only QA inventory through the explicit #1756 candidate reader.

A candidate is neither a queue generation nor historical review/CI evidence.
The published branch, exact provenance and policy still need qualification
before enrollment; no enrollment or runtime hook is installed here.
"""
from __future__ import annotations

import copy
from dataclasses import asdict

from . import lot_class, merge_queue, storage
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
        candidates = []
        # A UI page limit would silently omit ready lots at the cutoff.
        for item in self.ops.items(state="qa", assignee=None, limit=None):
            if item["app"] != self.project.name:
                continue
            target = merge_queue._remote_branch(item.get("branch_target") or self.target)
            marks = self.ops.milestones(item["id"], None)
            candidate = self.candidate(marks)
            if not isinstance(candidate, dict) or not isinstance(candidate.get("sha"), str) \
                    or not isinstance(candidate.get("causes"), list):
                raise TrainError("candidate reader returned no diagnostic for lot #%s" % item["id"])
            candidate = copy.deepcopy(candidate)
            causes = list(candidate["causes"])
            if not item.get("branch"):
                causes.append("missing-branch")
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
        if item is None or item["state"] != "qa" or item["app"] != self.project.name \
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
        historical = self.history.qualify_subject(subject, base, required=requirement.reviews)
        return dict(schema="ameesh-train-candidate-preparation/1", subject=subject,
                    policy=asdict(requirement), history=historical)
