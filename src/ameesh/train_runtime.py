# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit #1467 ports, without an executor hook or enabled configuration.

Admissions is a mandatory trusted provider: complete current queue facts,
dependencies, historical review proofs and quarantine under the target lease.
There is no fallback to a current registry, last verdict or journal note.
"""
from __future__ import annotations

from . import land, merge_queue, radar
from .train import TrainError, digest, identity, ordered


class Runtime:
    def __init__(self, *, ledger, repository, ci, fires, admissions, target,
                 policy, notify):
        target = merge_queue._remote_branch(target)
        if ledger.key != land.lock_key(repository.project, target) or fires.ledger is not ledger:
            raise TrainError("runtime adapters must share the exact target reservation")
        if admissions is None:
            raise TrainError("trusted historical admission provider required")
        if ci.slug != radar.github_slug(repository.project):
            raise TrainError("CI contract must name the runtime repository")
        self.ledger, self.repository, self.ci_adapter = ledger, repository, ci
        self.fires, self.admissions = fires, admissions
        self.target_branch, self.policy, self.notify = target, policy, notify

    def acquire(self, epoch):
        return self.ledger.acquire(epoch)

    def release(self, epoch):
        self.ledger.release(epoch)

    def load(self):
        return self.ledger.load()

    def save(self, state, expected, epoch):
        return self.ledger.save(state, expected, epoch)

    def target(self):
        return self.repository.target(self.target_branch)

    def entries(self):
        rows = [row for row in self.admissions.entries()
                if row["source"] == self.repository.source
                and merge_queue._remote_branch(row["target"]) == self.target_branch]
        if any(land.lock_key(self.repository.project, row["target"]) != self.ledger.key
               for row in rows):
            raise TrainError("queue target alias does not share the canonical train reservation")
        return rows

    def dependencies(self, rows):
        return self.admissions.dependencies(rows)

    def qualify(self, rows, base, mode):
        if mode != self.policy.ci_mode:
            raise TrainError("runtime and engine CI policy differ")
        # A new runner clone may know only the target. Fetch the exact lot
        # branches before the historical reader traverses their commits.
        self.repository.check_members(self.target_branch, rows)
        proof = self.admissions.qualify(rows, base, mode)
        if not isinstance(proof, dict) or not proof:
            raise TrainError("missing structured historical admission evidence")
        return proof

    def compose(self, base, rows, nonce):
        return self.repository.compose(self.target_branch, base, rows, nonce)

    def _held(self, epoch):
        if epoch is None or self.ledger.acquire(epoch) != epoch:
            raise TrainError("target reservation lost before train effect")

    def _belongs(self, plan):
        if (plan["source"], plan["target"]) != (self.repository.source, self.target_branch):
            raise TrainError("plan belongs to another runtime source or target")

    def _current(self, plan):
        self._belongs(plan)
        if self.target() != plan["base"]:
            raise TrainError("target changed before train effect")
        rows = self.entries()
        current = {row["work_item_id"]: row for row in rows}
        if len(current) != len(rows):
            raise TrainError("ambiguous current train admissions")
        for row in plan["members"]:
            other = current.get(row["work_item_id"])
            if other is None or identity(other) != identity(row):
                raise TrainError("member withdrawn, refrozen or requeued before train effect")
        admissible = ordered(plan["members"], self.dependencies(plan["members"]))
        if [identity(row) for row in admissible] != [identity(row) for row in plan["members"]]:
            raise TrainError("prerequisites changed before train effect")
        self.repository.check_members(self.target_branch, plan["members"])
        # JSON persistence changes tuples to arrays; their semantic proof is
        # identical, unlike a changed receipt, verdict or review requirement.
        if digest(self.qualify(plan["members"], plan["base"], self.policy.ci_mode)) \
                != digest(plan["proof"]):
            raise TrainError("admission evidence changed before train effect")

    def publish(self, plan, epoch):
        self._current(plan)
        self._held(epoch)
        self.repository.publish(plan, key=self.ledger.key, epoch=epoch,
                                owner=self.ledger.owner, workflows=self.ci_adapter.workflows)

    def start_ci(self, plan):
        self.ci_adapter.start(plan)

    def ci(self, plan):
        return self.ci_adapter.read(plan)

    def authorize(self, plan):
        return self.fires.authorize(plan)

    def promote(self, plan, fire, epoch):
        def validate():
            self._current(plan)
            ci = self.ci(plan)
            if ci.get("head") != plan["head"] or ci.get("tree") != plan["tree"] \
                    or ci.get("complete") is not True or ci.get("status") != "green":
                raise TrainError("current exact complete train CI is not green")

        self.fires.promote(plan, fire, epoch, validate=validate,
                           effect=lambda: self.repository.promote(
                               plan, key=self.ledger.key, epoch=epoch, owner=self.ledger.owner))

    def reconcile(self, plan, fire, epoch):
        self._belongs(plan)
        self._held(epoch)
        return self.fires.reconcile(plan, fire["id"], epoch, target=self.target)

    def quarantine(self, rows, reason, epoch):
        self._held(epoch)
        self.admissions.quarantine(rows, reason,
                                   lease=(self.ledger.key, self.ledger.owner, epoch))
