# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit #1467 ports, without an enabled executor configuration.

Admissions is a mandatory trusted provider: complete current queue facts,
dependencies, historical review proofs and quarantine under the target lease.
There is no fallback to a current registry, last verdict or journal note.
"""
from __future__ import annotations

import time

from . import land, merge_queue, radar, train_diagnosis
from .train import SCHEMA, TrainError, digest, identity, ordered


class Runtime:
    def __init__(self, *, ledger, repository, ci, fires, admissions, target,
                 policy, notify, inventory=None, enrollment=None, migrations=None,
                 prevalidation=None):
        target = merge_queue._remote_branch(target)
        if ledger.key != land.lock_key(repository.project, target) or fires.ledger is not ledger:
            raise TrainError("runtime adapters must share the exact target reservation")
        if admissions is None:
            raise TrainError("trusted historical admission provider required")
        if ci.slug != radar.github_slug(repository.project):
            raise TrainError("CI contract must name the runtime repository")
        if (inventory is None) != (enrollment is None):
            raise TrainError("candidate inventory and enrollment must be supplied together")
        if enrollment is not None and (enrollment.admissions is not admissions
                or enrollment.preparation.repository is not repository
                or inventory.project is not repository.project):
            raise TrainError("candidate intake must share the runtime's exact qualification ports")
        self.ledger, self.repository, self.ci_adapter = ledger, repository, ci
        self.fires, self.admissions = fires, admissions
        self.target_branch, self.policy, self.notify = target, policy, notify
        self.inventory, self.enrollment = inventory, enrollment
        self.migrations = migrations
        #: #2644 : la prévalidation de la composition, appelée avant toute
        #: publication ; None = pas d'accroche (banc, mode sans train).
        if prevalidation is not None and not callable(prevalidation):
            raise TrainError("explicit train prevalidation port required")
        self.prevalidation = prevalidation
        self.enrollment_result = None

    def enroll(self, epoch):
        """Intake only before a new manifest, under its target reservation."""
        if self.enrollment is None:
            return
        self._held(epoch)
        candidates = self.inventory.read()["candidates"]
        self.enrollment_result = self.enrollment.enroll_many(candidates, self.target(),
            actor=self.ledger.owner, lease=(self.ledger.key, self.ledger.owner, epoch))
        self._held(epoch)
        deferred = self.enrollment_result["deferred"]
        # #3139: a refusal is a fact about a lot, not a generic notice. Persist
        # it in the train document and journal it lot by lot, or the pilot sees
        # only "candidate enrollment deferred" and cannot tell why a ready lot
        # never entered the packet.
        self._record_enrollment(deferred, epoch)
        for row in deferred:
            self.notify(
                "candidate enrollment deferred: lot #%s (gel %s) — %s"
                % (row.get("work_item_id"), (row.get("frozen_sha") or "?")[:12],
                   row.get("reason") or "no reason"), row)

    def _record_enrollment(self, deferred, epoch):
        """Persist this pass's refusals in the exact target train document.

        A train with no document yet still needs the record: an empty
        enrollment is precisely why a cutoff had nothing to compose. The
        container is a terminal ("empty") state, so the next pass reads it,
        enrolls again and replaces the record; a composed packet keeps the
        current record with its plan. An empty list clears stale refusals.
        """
        state = self.load()
        if state is None and not deferred:
            return
        checked = time.time()
        rows = [dict(lot=row.get("work_item_id"),
                     branch=row.get("branch") or "",
                     frozen_sha=row.get("frozen_sha") or "",
                     frozen_id=row.get("frozen_id"),
                     candidate_sha=row.get("candidate_sha") or "",
                     candidate_origin=row.get("candidate_origin") or "",
                     reason=row.get("reason") or "",
                     checked_ts=checked) for row in deferred]
        expected = 0
        if state is None:
            state = dict(schema=SCHEMA, source=self.repository.source,
                         target=self.target_branch, phase="empty")
        else:
            expected = int(state.get("revision", 0))
        state = dict(state, enrollment_deferred=rows,
                     enrollment_checked_ts=checked)
        try:
            self.save(state, expected, epoch)
        except TrainError as exc:
            # The refusal itself is still true and already qualified; losing
            # the record must not hide it from the journal.
            self.notify("candidate enrollment record not persisted: %s" % exc, state)

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

    def admitted(self, rows, slot):
        """#2965: rows admissible at the cutoff, dated by their admission.

        The proofs of this pass's enrollment are the fast path; a packet row
        replayed from storage reads the same gate instant. A manual admission
        is never re-dated by its freeze/verdicts and stays late when queued
        after the cutoff.
        """
        return self.admissions.admitted(rows, slot, self.target(),
                                        proofs=self.enrollment_result)

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

    def sync(self, base):
        return self.repository.main_sync(self.target_branch, base)

    def compose(self, base, rows, nonce, sync=None):
        return self.repository.compose(self.target_branch, base, rows, nonce, sync=sync)

    def validate_transports(self, plan):
        self._belongs(plan)
        return self.repository.validate_transports(plan)

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
        if plan.get("transports"):
            self.validate_transports(plan)

    def publish(self, plan, epoch):
        # #2644 : la prévalidation passe avant la publication, donc avant de
        # tenir le bail et de renouveler la réservation : un run long ne fait
        # pas expirer l'époque. Un rouge lève avant tout effet distant ; une
        # indisponibilité est signalée par le port et la CI reste la preuve.
        if self.prevalidation is not None:
            self.prevalidation(plan["head"], plan["base"])
        self._current(plan)
        self._held(epoch)
        if hasattr(self.ledger, "before_publish"):
            self.ledger.before_publish(plan, epoch)
        self.repository.publish(plan, key=self.ledger.key, epoch=epoch,
                                owner=self.ledger.owner, workflows=self.ci_adapter.workflows)

    def start_ci(self, plan):
        self.ci_adapter.start(plan)

    def ci(self, plan):
        evidence = self.ci_adapter.read(plan)
        if evidence.get("status") == "red" and evidence.get("complete") is True:
            evidence = dict(evidence, diagnosis=train_diagnosis.unresolved(plan, evidence))
        return evidence

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
        self._migrations_landed(plan)

    def reconcile(self, plan, fire, epoch):
        self._belongs(plan)
        self._held(epoch)
        receipt = self.fires.reconcile(plan, fire["id"], epoch, target=self.target)
        if receipt is not None:
            self._migrations_landed(plan)
        return receipt

    def _migrations_landed(self, plan):
        """Secondary bookkeeping after a confirmed promotion; never revokes it."""
        if self.migrations is None or not plan.get("migrations"):
            return
        try:
            errors = self.migrations.landed(plan, path=self.repository.clone.path)
        except Exception as exc:  # noqa: BLE001 - the merge fact stays written
            errors = [str(exc)]
        if errors:
            self.notify("migration landing record incomplete: " + "; ".join(errors), plan)

    def quarantine(self, rows, reason, epoch):
        self._held(epoch)
        self.admissions.quarantine(rows, reason,
                                   lease=(self.ledger.key, self.ledger.owner, epoch))
