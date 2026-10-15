# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned review requirements for train admissions (#1467).

The caller explicitly selects an audited canon commit. The checkout, registry
and lot notes supply no policy. A separate per-project declaration of review
counts is mandatory; the existing risk classification alone is insufficient.
Importing this module installs no policy or runner.
"""
from __future__ import annotations

import re

from . import canon, lot_class, resolutions, review
from .train import TrainError
from .train_admissions import Requirement
from .train_review import SHA


def _git(path, *args):
    proc = resolutions._run(path, *args)
    if proc.returncode:
        raise TrainError("cannot read versioned review policy or lot history")
    return resolutions._text(proc)


class Versioned:
    """Read ``review_policies.train_reviews.projects.<project>.rules``.

    Each rule has nonempty ``paths`` and ``reviews`` (1 or 2). Every changed
    non-light path needs coverage. Overlapping rules use the largest count;
    positive counts also raise an otherwise light path to sensitive. Rules
    must be qualified by the pilot before selecting their canon commit.
    Unknown projects never borrow another project's policy or a fallback.
    """

    def __init__(self, path, commit, project):
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise TrainError("exact audited canon commit required")
        if not isinstance(project, str) or not project.strip():
            raise TrainError("explicit review policy project required")
        if _git(path, "rev-parse", "--verify", commit + "^{commit}") != commit:
            raise TrainError("review policy must name a commit, not another Git object")
        source = _git(path, "ls-tree", commit, "--", "federation.yaml").split()
        if len(source) != 4 or source[0] not in ("100644", "100755") \
                or source[1] != "blob" or source[3] != "federation.yaml":
            raise TrainError("regular versioned federation.yaml required")
        try:
            document = canon.load_yaml(_git(path, "show", commit + ":federation.yaml"))
            policies = document["review_policies"]
            classes = policies["risk_classes"]["projects"][project]
            counts = policies["train_reviews"]
            if counts["schema"] != "ameesh-train-reviews-policy/1":
                raise TrainError("unknown train review policy schema")
            declaration = counts["projects"][project]
        except (KeyError, TypeError, canon.YamlError) as exc:
            raise TrainError("explicit versioned project classification and review counts required") from exc
        self.policy, problems = review.parse(classes)
        if problems or not isinstance(classes, dict) or "default" not in classes:
            raise TrainError("complete unambiguous risk classification required")
        if not isinstance(declaration, dict) or set(declaration) != {"rules"} \
                or not isinstance(declaration["rules"], list):
            raise TrainError("explicit train review rules required; no implicit default")
        rules = []
        for rule in declaration["rules"]:
            if not isinstance(rule, dict) or set(rule) != {"paths", "reviews"} \
                    or type(rule["reviews"]) is not int or rule["reviews"] not in (1, 2) \
                    or not isinstance(rule["paths"], list) or not rule["paths"] \
                    or any(not isinstance(p, str) or not p.strip() for p in rule["paths"]):
                raise TrainError("review rules require paths and an explicit count of 1 or 2")
            try:
                rules.append((tuple(review.compile_glob(p) for p in rule["paths"]), rule["reviews"]))
            except re.error as exc:
                raise TrainError("invalid train review path pattern") from exc
        self.rules = tuple(rules)
        self.commit, self.project = commit, project

    def __call__(self, item, row, base, path):
        head = row["frozen_sha"]
        if item.get("app") != self.project or item.get("id") != row["work_item_id"]:
            raise TrainError("review policy belongs to another lot or project")
        if lot_class.release_reason(item, row["target"]):
            raise TrainError("release lots cannot enter an automatic train")
        if not isinstance(base, str) or not SHA.fullmatch(base) \
                or not isinstance(head, str) or not SHA.fullmatch(head):
            raise TrainError("exact base and frozen head required for review policy")
        if _git(path, "rev-parse", "--is-shallow-repository") != "false":
            raise TrainError("complete Git history required for review policy")
        _git(path, "merge-base", base, head)
        try:
            paths = canon.git_range_paths(path, base, head)
            if any(changed != review.normalize_path(changed) or "\ufffd" in changed
                   for changed in paths):
                raise TrainError("Git path cannot be normalized without changing its identity")
            review.classify(self.policy, paths)  # validates every Git path
        except (canon.GitError, review.ReviewError) as exc:
            raise TrainError("cannot classify exact lot content: %s" % exc) from exc
        risk, reviews = "light", 0
        for changed in paths:
            cls, _ = self.policy.class_of(changed)
            count = max((n for patterns, n in self.rules
                         if any(pattern.fullmatch(changed) for pattern in patterns)), default=0)
            if cls != "light" and not count:
                raise TrainError("review count unknown for non-light path: %s" % changed)
            if count:
                cls = "sensitive"
            if review.RANK[cls] > review.RANK[risk]:
                risk = cls
            reviews = max(reviews, count)
        requirement = Requirement(row["work_item_id"], base, head, risk, reviews, self.commit)
        requirement.check(row, base)
        return requirement
