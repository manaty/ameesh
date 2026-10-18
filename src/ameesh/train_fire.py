# SPDX-License-Identifier: AGPL-3.0-only
"""Exact promotion authority for #1467, selected by the compiled canon.

A fire covers the complete persisted plan, including admission proofs and
ordered member generations. It is a database receipt, never a journal note.
Grant/withdrawal use the existing declared-role/session authority guard.

Withdrawal and promotion race on the target row. A successful withdrawal
prevents the effect. If promotion wins, its claim is committed before the
effect and withdrawal reports that reconciliation is needed. A process or
network failure cannot roll this claim back into an active authorization.
The explicit pilot command lives in train_cli; no implicit pilot,
configuration activation, or runtime hook is added here.
"""
from __future__ import annotations

import json

from . import assignments, storage, work
from .train import SCHEMA, TrainError, digest, identity
from .train_review import SHA


def binding(plan: dict) -> str:
    """Reject partial/diagnostic manifests, then bind every persisted field."""
    try:
        if plan["schema"] != SCHEMA or plan["diagnostic"] is not False \
                or not all(SHA.fullmatch(plan[k]) for k in ("base", "head", "tree")) \
                or not all(isinstance(plan[k], str) and plan[k].strip()
                           for k in ("source", "target", "branch", "id", "nonce")) \
                or not plan["members"] or not isinstance(plan["proof"], dict) or not plan["proof"]:
            raise ValueError("incomplete plan")
        members = [identity(row) for row in plan["members"]]
        if len({row[0] for row in members}) != len(members):
            raise ValueError("duplicate member")
        return digest(plan)
    except (KeyError, TypeError, ValueError) as exc:
        raise TrainError("complete non-diagnostic persisted fire manifest expected") from exc


class Fires:
    def __init__(self, cfg, ledger):
        if storage.is_remote(ledger.db):
            raise TrainError("train fire requires local serialized storage")
        self.cfg, self.ledger = cfg, ledger
        self.db, self.ops = ledger.db, ledger.ops

    def _pilot(self, actor):
        try:
            return work.corrector(self.cfg, self.db, actor, history=False)
        except work.WorkError as exc:
            raise TrainError("train fire requires a pilot with a declared role: %s" % exc) from exc

    def _still_pilot(self, actor):
        # This checks the authenticated grant's actor, not the runner's own
        # session. Never authorize by the history of assigned/delegated lots.
        return actor.startswith(work.HUMAN_PREFIX) or assignments.is_orchestrator(
            self.cfg, self.db, actor, roles=work.CORRECTOR_ROLES, history=False)

    def give(self, plan, *, actor=None):
        who = self._pilot(actor)
        document = dict(schema="ameesh-train-fire/1", plan=json.loads(json.dumps(plan)),
                        binding=binding(plan), actor=who)
        receipt = self.ops.train_fire_give(self.ledger.key, document)
        if receipt is None:
            raise TrainError("fire plan changed or already has an active/uncertain promotion")
        return receipt

    def withdraw(self, plan, fire_id, *, actor=None):
        who = self._pilot(actor)
        fingerprint = binding(plan)
        current = self.ops.train_fire_read(self.ledger.key, plan["id"])
        if not current or current["id"] != fire_id or binding(current["plan"]) != fingerprint:
            raise TrainError("withdrawal does not name the current exact fire receipt")
        receipt = self.ops.train_fire_change(self.ledger.key, fire_id, "revoked", actor=who)
        if receipt is None:
            raise TrainError("fire no longer active: promotion may be in progress; reconcile first")
        return receipt

    def authorize(self, plan):
        fingerprint = binding(plan)
        receipt = self.ops.train_fire_read(self.ledger.key, plan["id"])
        if not receipt or receipt["state"] != "active":
            return None
        if receipt.get("schema") != "ameesh-train-fire/1" \
                or binding(receipt.get("plan", {})) != fingerprint \
                or receipt.get("binding") != fingerprint:
            raise TrainError("fire receipt covers another exact manifest")
        if not self._still_pilot(receipt["actor"]):
            raise TrainError("fire giver no longer has a declared pilot role")
        return receipt

    def promote(self, plan, fire, epoch, *, validate, effect):
        """Adapter for Ports.promote; callbacks must fail closed.

        validate re-reads admission, current complete CI and repository guards
        after the claim. effect promotes the exact plan via Repository using
        ledger.key/owner/epoch. Neither callback may enqueue an asynchronous
        merge; it must complete or leave this durable claim for inspection.
        """
        current = self.authorize(plan)
        if not current or current != fire:
            raise TrainError("fire withdrawn or superseded before promotion")
        claimed = self.ops.train_fire_change(self.ledger.key, fire["id"], "promoting",
                                             actor=self.ledger.owner,
                                             owner=self.ledger.owner, epoch=epoch)
        if claimed is None:
            raise TrainError("withdrawal, plan change or stale lease prevented promotion")
        # No finally/reactivation: either callback can lose an external
        # response or the process can die. The persisted claim must survive.
        validate()
        if not self._still_pilot(fire["actor"]):
            raise TrainError("pilot role changed before promotion")
        if self.ledger.acquire(epoch) != epoch:
            raise TrainError("target reservation lost before promotion")
        effect()
        completed = self.ops.train_fire_change(self.ledger.key, fire["id"], "landed",
                                               actor=self.ledger.owner,
                                               owner=self.ledger.owner, epoch=epoch)
        if completed is None:
            raise TrainError("promotion outcome needs reconciliation; fire claim retained")

    def reconcile(self, plan, fire_id, epoch, *, target):
        """Observe a fresh exact target after restart; never issue a push.

        Seeing the base does not prove that an old in-flight push cannot
        finish. Keep the claim uncertain until the effect can be established.
        target must fetch via Repository.target, not read a cached ref.
        """
        fingerprint = binding(plan)
        current = self.ops.train_fire_read(self.ledger.key, plan["id"])
        if not current or current["id"] != fire_id \
                or binding(current["plan"]) != fingerprint \
                or current["state"] not in ("promoting", "landed"):
            raise TrainError("no matching persisted promotion receipt to reconcile")
        observed = target()
        if observed != plan["head"]:
            if observed != plan["base"]:
                raise TrainError("target diverged from uncertain train; inspect before proceeding")
            return None
        if current["state"] == "landed":
            return current
        completed = self.ops.train_fire_change(self.ledger.key, fire_id, "landed",
                                               actor=self.ledger.owner,
                                               owner=self.ledger.owner, epoch=epoch)
        if completed is None:
            raise TrainError("target observed but receipt lease/intent changed; reconcile again")
        return completed


class Automatic:
    """No named fire when the pinned canon authorizes a green packet.

    Engine's existing ``promoting`` intent is the durable claim. This uses
    the same target lease, validation and reconciliation ports as named fires;
    it creates neither a pilot receipt nor another promotion state machine.
    """

    def __init__(self, ledger, rules):
        if storage.is_remote(ledger.db):
            raise TrainError("automatic train promotion requires local serialized storage")
        if rules.merge_named_fire is not False or rules.merge_repeat_freeze is not False \
                or rules.merge_on_green_packet is not True \
                or not SHA.fullmatch(rules.commit):
            raise TrainError("unsupported compiled automatic packet merge policy")
        self.ledger, self.rules = ledger, rules

    def authorize(self, plan):
        document = dict(schema="ameesh-train-automatic/1", plan=plan,
                        binding=binding(plan), policy_commit=self.rules.commit,
                        merge=dict(named_fire=self.rules.merge_named_fire,
                                   repeat_freeze=self.rules.merge_repeat_freeze,
                                   on_green_packet=self.rules.merge_on_green_packet,
                                   post_merge_red=self.rules.merge_post_merge_red))
        return dict(document, id=digest(document))

    def _intent(self, plan, receipt):
        current = self.ledger.load()
        expected = self.authorize(plan)
        if not current or current.get("phase") not in ("promoting", "landed") \
                or digest(current.get("plan")) != digest(plan) \
                or digest(current.get("fire")) != digest(expected) \
                or digest(receipt) != digest(expected):
            raise TrainError("no matching persisted automatic promotion intent")
        return current

    def promote(self, plan, receipt, epoch, *, validate, effect):
        if self._intent(plan, receipt)["phase"] != "promoting":
            raise TrainError("automatic promotion already completed")
        # Engine persisted this exact claim under the shared target lease.
        # Any exception leaves it uncertain; a new pass observes the target
        # instead of issuing the push again.
        validate()
        if self.ledger.acquire(epoch) != epoch:
            raise TrainError("target reservation lost before automatic promotion")
        self._intent(plan, receipt)
        effect()

    def reconcile(self, plan, receipt_id, epoch, *, target):
        receipt = self.authorize(plan)
        if receipt_id != receipt["id"]:
            raise TrainError("automatic promotion belongs to another compiled policy or plan")
        self._intent(plan, receipt)
        if self.ledger.acquire(epoch) != epoch:
            raise TrainError("target reservation lost before automatic reconciliation")
        observed = target()
        if observed != plan["head"]:
            if observed != plan["base"]:
                raise TrainError("target diverged from uncertain automatic promotion")
            return None
        return dict(receipt, state="landed")
