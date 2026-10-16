# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only QA inventory through the explicit #1756 candidate reader.

A candidate is neither a queue generation nor historical review/CI evidence.
The published branch, exact provenance and policy still need qualification
before enrollment; no enrollment or runtime hook is installed here.
"""
from __future__ import annotations

import copy

from . import lot_class, merge_queue, storage
from .train import TrainError


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
