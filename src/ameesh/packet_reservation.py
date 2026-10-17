# SPDX-License-Identifier: AGPL-3.0-only
"""#1680: maintain an existing target lease while packet CI is outstanding.

The train acquires its normal land lease before composing. ``begin`` adopts
that live epoch before publication (which can trigger push CI), without taking
another lease. The caller persists the JSON ticket in its existing state and
schedules ``renew`` on fresh scoped connections. Renewal never reads CI.

The absolute deadline and CI contract come from the qualified caller; this
module supplies neither a CI duration policy nor a scheduler. A terminal event
is only a hint: ``observe`` calls the supplied exact, fresh CI/read-back port.
Uncertain dispatch and successful CI keep the reservation until reconciliation
or landing. No runner, workflow, CLI hook or configuration is enabled here.
"""
from __future__ import annotations

import math
import re
import uuid
from dataclasses import asdict, dataclass, fields, replace

from . import land, storage

SCHEMA = "ameesh-packet-reservation/1"
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ReservationError(RuntimeError):
    """No live reservation or qualified terminal proof: do not write."""


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value):
        raise ReservationError("finite %s required" % name)
    return float(value)


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ReservationError("explicit %s required" % name)
    return value


@dataclass(frozen=True)
class Ticket:
    key: str
    owner: str
    epoch: int
    branch: str
    base: str
    head: str
    ci_contract: str
    deadline: float
    checked_at: float
    ttl_s: float
    schema: str = SCHEMA

    def __post_init__(self):
        if self.schema != SCHEMA:
            raise ReservationError("unknown packet reservation schema")
        for name in ("key", "owner", "branch"):
            _text(getattr(self, name), name)
        if not self.branch.startswith("integration/"):
            raise ReservationError("packet integration branch required")
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch < 1:
            raise ReservationError("existing fencing epoch required")
        for name in ("base", "head"):
            if not isinstance(getattr(self, name), str) or not _SHA.fullmatch(getattr(self, name)):
                raise ReservationError("full %s SHA required" % name)
        if not isinstance(self.ci_contract, str) or not _DIGEST.fullmatch(self.ci_contract):
            raise ReservationError("qualified CI contract digest required")
        for name in ("deadline", "checked_at", "ttl_s"):
            _number(getattr(self, name), name)
        if self.ttl_s <= 0 or self.deadline <= self.checked_at:
            raise ReservationError("positive TTL and future absolute deadline required")

    def to_json(self):
        return asdict(self)

    @classmethod
    def from_json(cls, value):
        if not isinstance(value, dict):
            raise ReservationError("packet reservation object required")
        if set(value) != {field.name for field in fields(cls)}:
            raise ReservationError("incomplete or unknown packet reservation fields")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ReservationError("incomplete or unknown packet reservation fields") from exc


def _ticket(value):
    return value if isinstance(value, Ticket) else Ticket.from_json(value)


def begin(db, project, target, *, owner, epoch, branch, base, head, ci_contract,
          deadline, now, ttl_s=None):
    """Adopt the train's live (key, owner, epoch); never acquire/reacquire it.

    The key is exactly the installed ``land.lock_key``. In particular, a ticket
    must not silently switch namespaces when #1374 normalizes target aliases.
    Deployment/composition must reconcile any outstanding older-key tickets.
    """
    ticket = Ticket(key=land.lock_key(project, _text(target, "target")),
                    owner=owner, epoch=epoch, branch=branch, base=base, head=head,
                    ci_contract=ci_contract, deadline=deadline, checked_at=now,
                    ttl_s=land.LOCK_TTL_S if ttl_s is None else ttl_s)
    return renew(db, ticket, now=now)


def next_renew(ticket):
    """Maintenance deadline only; waking here must not run a CI passage."""
    ticket = _ticket(ticket)
    return min(ticket.checked_at + ticket.ttl_s / 3, ticket.deadline)


def renew(db, ticket, *, now):
    """One bounded SQL renewal, preserving owner and epoch, without CI reads."""
    ticket = _ticket(ticket)
    now = _number(now, "now")
    if now < ticket.checked_at:
        raise ReservationError("reservation clock moved backwards")
    ops = storage.of(db).work
    if now >= ticket.deadline:
        ops.land_unlock(ticket.key, ticket.owner, ticket.epoch)
        raise ReservationError("packet reservation absolute deadline expired")
    ttl = min(ticket.ttl_s, ticket.deadline - now)
    epoch = ops.land_lock(ticket.key, ticket.owner, ttl, epoch=ticket.epoch)
    if epoch != ticket.epoch:
        raise ReservationError("packet target lease missing, expired or replaced")
    return replace(ticket, checked_at=now)


def observe(db, ticket, *, now, read):
    """Reconcile an event through a trusted fresh observation port.

    ``read(Ticket)`` must read the qualified complete CI contract (including
    latest run attempts), or the landing receipt/remote read-back. The event
    payload is never evidence. It returns branch/base/head/ci_contract, kind
    pending|passed|failed|landed, and complete=True for terminal observations.
    No raw per-workflow success or stale run can release the target.
    """
    if not callable(read):
        raise ReservationError("fresh qualified observation port required")
    ticket = renew(db, ticket, now=now)
    facts = read(ticket)
    if not isinstance(facts, dict) or any(facts.get(k) != getattr(ticket, k)
            for k in ("branch", "base", "head", "ci_contract")):
        raise ReservationError("observation does not bind the exact packet and CI contract")
    kind = facts.get("kind")
    if kind not in ("pending", "passed", "failed", "landed"):
        raise ReservationError("unknown packet observation")
    if kind != "pending" and facts.get("complete") is not True:
        raise ReservationError("incomplete packet observation")
    if kind in ("pending", "passed"):
        return {"state": "retained", "ticket": ticket.to_json()}
    if not storage.of(db).work.land_unlock(ticket.key, ticket.owner, ticket.epoch):
        raise ReservationError("target lease changed before terminal release")
    return {"state": "released", "reason": kind}


class WriterLease:
    """A single-lot writer uses the same target lease as an outstanding packet.

    Acquire before local merge construction, refresh immediately before the
    remote mutation, release in finally. This is not a second merge scheduler.
    It does not intercept an independently launched git/gh binary.
    """
    def __init__(self, db, project, target):
        self.ops = storage.of(db).work
        self.key = land.lock_key(project, target)
        self.owner = "merge-pr:" + uuid.uuid4().hex
        self.epoch = None

    def acquire(self):
        self.epoch = self.ops.land_lock(self.key, self.owner, land.LOCK_TTL_S)
        if self.epoch is None:
            raise ReservationError("target %s is reserved; no merge or push" % self.key)

    def refresh(self):
        if self.epoch is None or self.ops.land_lock(
                self.key, self.owner, land.LOCK_TTL_S, epoch=self.epoch) != self.epoch:
            raise ReservationError("target reservation lost before push")

    def release(self):
        if self.epoch is not None:
            self.ops.land_unlock(self.key, self.owner, self.epoch)
            self.epoch = None
