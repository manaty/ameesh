# SPDX-License-Identifier: AGPL-3.0-only
"""Current queue facts plus exact historical evidence for #1467.

The reviewed policy provider is mandatory: the canon's path classification
alone does not encode the review count for SQL/security/production. Neither
a journal note nor a caller-supplied receipt is used as a policy decision.
The provider also supplies its classified reading of the exact frozen diff;
admission rechecks the declared class and count against that same reading, so
a critical SQL/security/production lot cannot enter with one family and be
withdrawn after the freeze. No provider, runner hook or configuration is
enabled by importing this module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from . import land, lot_class, merge_queue, review, storage, work
from .train import MemberDeferred, TrainError, identity
from .train_review import SHA

UNAVAILABLE = set(work.MERGED_STATES) | {"closed"} | work.WAITING
_RISKS = ("light", "normal", "sensitive")


def _classified(files):
    """Validate the classified files of the exact frozen diff.

    The policy reads the diff itself; admission rechecks that its declared
    class and review count cannot be weaker than that same reading. A missing
    or malformed reading refuses: an absent classification is never a light
    one.
    """
    if not isinstance(files, (tuple, list)) or not files:
        raise TrainError("classified frozen paths required")
    out = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "class", "rule"}:
            raise TrainError("malformed classified frozen path")
        path, cls, rule = entry["path"], entry["class"], entry["rule"]
        if not isinstance(path, str) or not path or cls not in _RISKS:
            raise TrainError("malformed classified frozen path")
        if rule is not None and (not isinstance(rule, (list, tuple)) or not rule
                                 or any(not isinstance(p, str) or not p for p in rule)):
            raise TrainError("malformed classified frozen path")
        out.append({"path": path, "class": cls, "rule": rule})
    if len({entry["path"] for entry in out}) != len(out):
        raise TrainError("duplicate classified frozen path")
    return out


@dataclass(frozen=True)
class Requirement:
    lot: int
    base: str
    head: str
    risk: str
    reviews: int
    policy_commit: str
    #: the policy's reading of the exact frozen diff: [{path, class, rule}].
    #: Admission recomputes the class minimum (critical SQL/security/prod
    #: paths need two distinct families) instead of trusting the count alone.
    files: tuple = ()

    def check(self, row, base):
        if (self.lot, self.base, self.head) != (row["work_item_id"], base, row["frozen_sha"]):
            raise TrainError("review policy covers another lot, base or frozen head")
        if not isinstance(self.policy_commit, str) or not SHA.fullmatch(self.policy_commit):
            raise TrainError("versioned review policy required")
        if type(self.reviews) is not int or self.risk not in _RISKS \
                or self.reviews not in ({0} if self.risk == "light" else {1, 2}):
            raise TrainError("explicit known non-release review requirement expected")
        files = _classified(self.files)
        worst = max((entry["class"] for entry in files), key=review.RANK.__getitem__)
        if review.RANK[worst] > review.RANK[self.risk]:
            raise TrainError("review policy understates the class of the frozen content")
        minimum = merge_queue.required_verdicts({"class": self.risk, "files": files})
        if self.reviews < minimum:
            raise TrainError("review policy requires %d independent review(s) for the frozen "
                             "class, not %d" % (minimum, self.reviews))


class Admissions:
    def __init__(self, db, project, history, *, policy, targeted_ci=None):
        if policy is None:
            raise TrainError("trusted versioned review policy provider required")
        if history.db is not db:
            raise TrainError("queue and historical proofs must use the same storage")
        self.db, self.project, self.history = db, project, history
        self.ops = storage.of(db).work
        self.source = merge_queue.source_of(project.repo)
        self.policy, self.targeted_ci = policy, targeted_ci

    def entries(self):
        # Read all targets: filtering SQL on 'main' would hide 'origin/main'.
        # The runtime verifies their shared canonical target reservation.
        return [row for row in self.ops.queue(None)
                if row["state"] == "queued" and row["source"] == self.source
                and row["item_state"] not in UNAVAILABLE]

    def dependencies(self, rows):
        return storage.of(self.db).work_dependencies.for_items(
            [int(row["work_item_id"]) for row in rows]) if rows else []

    def _current(self, row, current):
        fresh = current.get(row["work_item_id"])
        if fresh is None or identity(fresh) != identity(row) or fresh["branch"] != row["branch"]:
            raise TrainError("admission withdrawn, moved, refrozen or requeued")
        item = self.ops.get(row["work_item_id"])
        if item is None or item["state"] in UNAVAILABLE:
            raise TrainError("lot no longer open for train admission")
        if item["app"] != self.project.name or item["branch"] != row["branch"]:
            raise TrainError("admission no longer belongs to this project or branch")
        target = item.get("branch_target") or self.project.target
        if not target or merge_queue._remote_branch(target) != merge_queue._remote_branch(row["target"]):
            raise TrainError("lot target changed since admission")
        if lot_class.release_reason(item, row["target"]):
            raise TrainError("release lots cannot enter an automatic train")
        return item

    def qualify(self, rows, base, mode):
        if mode not in ("packet", "targeted") or not rows:
            raise TrainError("nonempty train and explicit CI mode required")
        entries = self.entries()
        current = {row["work_item_id"]: row for row in entries}
        if len(current) != len(entries) or len({r["work_item_id"] for r in rows}) != len(rows):
            raise TrainError("ambiguous train admissions")
        proofs = []
        for row in rows:
            try:
                proof = self._member_proof(row, current, base, mode)
            except TrainError as exc:
                # The engine defers this admission and its dependents only
                # while composing. The same refusal after publication must
                # still drain the immutable packet, never shrink its plan.
                raise MemberDeferred(row, "admission #%s frozen=%s: %s" % (
                    row["work_item_id"], row["frozen_sha"], exc)) from exc
            proofs.append(proof)
        return dict(schema="ameesh-train-admissions/1", source=self.source,
                    base=base, mode=mode, members=proofs)

    def _member_proof(self, row, current, base, mode):
        item = self._current(row, current)
        requirement = self.policy(item, row, base, self.history.path)
        if not isinstance(requirement, Requirement):
            raise TrainError("trusted policy returned no structured review requirement")
        requirement.check(row, base)
        historical = self.history.qualify(row, base, required=requirement.reviews)
        proof = dict(admission=identity(row), policy=asdict(requirement), history=historical)
        if mode == "targeted":
            ci = self.targeted_ci(row) if self.targeted_ci is not None else None
            if not isinstance(ci, dict) or ci.get("head") != row["frozen_sha"] \
                    or ci.get("status") != "green" or ci.get("complete") is not True \
                    or not ci.get("runs"):
                raise TrainError("exact targeted lot CI evidence required")
            proof["targeted_ci"] = ci
        return proof

    def quarantine(self, rows, reason, *, lease):
        current = {row["work_item_id"]: row for row in self.entries()}
        # Validate the entire request before any mutation. The storage CAS
        # then checks generation/latest freeze and locks the live target lease.
        for row in rows:
            self._current(row, current)
            if lease[0] != land.lock_key(self.project, row["target"]):
                raise TrainError("exclusion requires this admission's target reservation")
        for row in rows:
            result = self.ops.queue_update(
                row["work_item_id"], state="ejected", reason=reason, lease=lease,
                entry=(row["generation"], row["target"], row["frozen_id"], row["frozen_sha"]),
                note="integration train: " + reason, actor=lease[1])
            if result is None:
                raise TrainError("exclusion refused: admission or target reservation changed")
