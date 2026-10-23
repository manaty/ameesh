# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP4) — the pilot's gestures outside the database carry the lease epoch.

Study `docs/design/etudes/election-pilote.md` §4. For an elected role
(`releve: election` on its Agent card, EP1), the lease is the only authority
to act and its `lease_epoch` is the fencing token. When the holder's host
stops, another candidate claims the lease and increments the epoch; the old
session may still be alive (a resumed terminal, a process left behind) and
must not act any more.

The gestures below have effects that outlive the database write which
prepares them: a merge on GitHub, a merge queue that will push, an
assignment that wakes another agent, a recorded packet composition, and a
decision request routed to a human. Each one calls `gate()` first. For an
elected role this:

1. reads the agent's Agent card (`releve`, or the `AMEESH_ROLE` of an EP5
   timer);
2. refuses a session that is not the live lease-holder, naming the current
   holder (or "nobody") and its host/epoch;
3. journals the gesture *in the same transaction* as a `renew` of the lease
   (the append-only `ameesh_events` log, type `pilot.gesture`), so a stale
   epoch can never write the receipt either.

An EP5 timer (`ameesh role run`) may drop the agent identity; it passes
`AMEESH_ROLE` and the epoch it entered with. That pair is the fencing token:
a role without `AMEESH_LEASE_EPOCH` is refused, and the epoch is compared to
the live lease in `Gate.hold`'s transaction. A timer must never pass as a
human.

For an external call (a push, `gh pr merge`), the transaction cannot cover
the call itself: `Gate.hold(db, margin_s=…)` renews the lease just before it
with at least the call's worst-case duration, and refuses if the renewed
lease does not have that margin left. The residual race — a call that blocks
longer than its own timeout, after which the lease could be taken — is
documented in `docs/HOST-HANDOFFS.md`; GitHub's `--match-head-commit`, the
target lease and the compare-and-swap remain the last guards.

The event is the durable trace of the authorization; the effect follows it.
A human or the bureau has no agent lease to fence and passes through, as does
a host-pinned agent (`releve: hote`): its placement already prevents another
host from holding the lease, and this lot deliberately changes no existing
workflow for it. A runner process with neither `AGENT_MAIL_NAME` nor
`AMEESH_ROLE` passes through too; only a session or a role command opened
under a lease is fenced.

Nothing here contains a secret: the event carries the agent, gesture, ref,
epoch and holder host, never a token.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import agent_names, identity, registry, storage

#: the five gestures of the study; kept explicit so a typo can never create a
#: new event namespace by accident
GESTURES = ("merge", "queue", "assign", "packet", "decision")
#: event type written in `ameesh_events` (append-only, one year by default)
EVENT = "pilot.gesture"
#: lease duration used to hold the role while the gesture runs; the runner's
#: own default (its heartbeat renews at `lease_ttl / 3`)
DEFAULT_TTL_S = 300.0
#: extra time added to a lease renewed for an external call whose timeout is
#: `margin_s`: the call must have the whole margin left, not exactly it
RENEW_GRACE_S = 30.0


class GestureRefused(ValueError):
    """The session no longer holds its elected role at the epoch it was opened with."""


@dataclass(frozen=True)
class Gate:
    """An elected pilot allowed to act, at the epoch its session was opened with."""

    agent: str
    runner: str
    epoch: int
    gesture: str
    ref: str
    project: str | None = None
    host: str = ""
    #: the `AMEESH_ROLE` of an EP5 timer (empty for a regular agent session)
    role: str = ""
    #: why the binding is not a live lease (kept for the refusal message)
    reason: str = field(default="", compare=False)

    def hold(self, db, *, ttl_s: float = DEFAULT_TTL_S, margin_s: float = 0.0) -> int:
        """Renew the lease and journal the gesture, in the same transaction.

        A stale epoch cannot write the receipt: `registry.renew` is fenced by
        `(owner, epoch)` and locks the agent row, so an election that commits
        between this session's last renewal and this call is seen here. The
        effect may then run: the lease is live again for `ttl_s`, and a claim
        only takes a free or expired lease (EP1). Returns the event id.

        `margin_s` is the worst-case duration of an external call (a push, a
        `gh pr merge`, its read-back) made right after this transaction. The
        lease is renewed to cover that margin plus `RENEW_GRACE_S`, and the
        call is refused if the renewed lease does not have the margin left:
        the residual race (the call outliving its lease on a network hang) is
        documented in `docs/HOST-HANDOFFS.md`; GitHub's `--match-head-commit`
        and the target lease remain the last guards.
        """
        margin = max(0.0, float(margin_s))
        ttl = max(float(ttl_s), margin + RENEW_GRACE_S)
        with db.transaction() as tx:
            before = registry.get(tx, self.agent) or {}
            if before.get("status") == "stopped":
                raise GestureRefused(
                    "gesture %s refused for %s: the agent is stopped at epoch %s "
                    "(no gesture from a stopped role)"
                    % (self.gesture, self.agent, self.epoch))
            expires = registry.renew(tx, self.agent, self.runner, self.epoch, ttl)
            if expires is None:
                raise GestureRefused(_refusal(tx, self))
            # `renew` locks the agent row; a stop committed just before the
            # lock is visible here, and the lock keeps it from crossing the
            # receipt. `lease_agent` already checked it when the identity was
            # read, but a stop may have landed in between.
            row = registry.get(tx, self.agent) or {}
            if row.get("status") == "stopped":
                raise GestureRefused(
                    "gesture %s refused for %s: the agent is stopped at epoch %s "
                    "(no gesture from a stopped role)"
                    % (self.gesture, self.agent, self.epoch))
            left = float(expires) - time.time()
            if left < margin:
                raise GestureRefused(
                    "gesture %s refused for %s: only %.1fs of lease left at epoch %s, "
                    "the external call needs at least %.1fs"
                    % (self.gesture, self.agent, left, self.epoch, margin))
            data = {"agent": self.agent, "gesture": self.gesture, "ref": self.ref,
                    "epoch": self.epoch, "runner": self.runner,
                    "host": self.host}
            if self.role:
                data["role"] = self.role
            return storage.of(tx).events.record(
                EVENT, "agent:%s" % self.agent, self.project, data)


def _refusal(db, gate: Gate) -> str:
    """The refusal names the current holder, its host and its epoch."""
    try:
        row = registry.get(db, gate.agent) or {}
        state = storage.of(db).leases.state(gate.agent) or {}
    except Exception:  # noqa: BLE001 - the message must never mask the refusal
        row, state = {}, {}
    holder = row.get("lease_owner") or "nobody"
    host = row.get("host") or "?"
    epoch = row.get("lease_epoch")
    live = bool(state.get("live")) if state else True
    if holder != "nobody" and not live:
        holder = "nobody (previous holder %s, lease expired)" % holder
    detail = gate.reason or "lease expired or replaced"
    return ("gesture %s refused for %s: this session is fenced by the lease epoch "
            "(%s); the role is now held by %s on %s at epoch %s — the current "
            "holder acts, not this session"
            % (gate.gesture, gate.agent, detail, holder, host,
               epoch if epoch is not None else "?"))


def _cfg(db, cfg):
    return cfg if cfg is not None else getattr(db, "cfg", None)


def _claims_lease() -> bool:
    """The session carries a runner id or a lease epoch: it claims a lease."""
    return any(os.environ.get(key) for key in
               ("AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH"))


def _is_database(db) -> bool:
    """A connection or transaction, not a test double without `query`/`execute`."""
    return callable(getattr(db, "query", None)) and callable(getattr(db, "execute", None))


def _role_gate(cfg, db, role: str, gesture: str, ref: str,
               project: str | None) -> Gate:
    """`AMEESH_ROLE` (EP5 `ameesh role run`): the epoch in the environment is
    the fencing token, even when the command carries no agent identity.

    A timer that drops `AMEESH_ROLE` or its `AMEESH_LEASE_EPOCH` must never
    pass as a human: the epoch is compared to the live lease in the
    transaction of `Gate.hold`; here we only read it to fail early and to
    name the holder. An `AMEESH_ROLE` without an epoch is refused before any
    database access.
    """
    epoch_text = (os.environ.get("AMEESH_LEASE_EPOCH")
                  or os.environ.get("AGENT_MESH_LEASE_EPOCH") or "").strip()
    if not epoch_text:
        raise GestureRefused(
            "gesture %s refused for role %s: AMEESH_ROLE without "
            "AMEESH_LEASE_EPOCH (a role command must present the epoch it "
            "entered with)" % (gesture, role))
    try:
        epoch = int(epoch_text)
    except (TypeError, ValueError):
        raise GestureRefused(
            "gesture %s refused for role %s: unreadable AMEESH_LEASE_EPOCH %r"
            % (gesture, role, epoch_text)) from None
    if epoch <= 0:
        raise GestureRefused(
            "gesture %s refused for role %s: invalid lease epoch %s"
            % (gesture, role, epoch))
    if db is None:
        raise GestureRefused(
            "gesture %s refused for role %s: no database to verify the lease epoch"
            % (gesture, role))
    try:
        row = registry.get(db, role)
    except Exception as exc:  # noqa: BLE001 - a claimed lease must fail closed
        raise GestureRefused(
            "gesture %s refused for role %s: cannot read the registry (%s)"
            % (gesture, role, " ".join(str(exc).split())[:160])) from exc
    if not row:
        raise GestureRefused(
            "gesture %s refused for role %s: no such agent in the registry"
            % (gesture, role))
    # `registry.get` resolves aliases: compare and renew under the canonical
    # name. A command that names a different agent than the role is refused,
    # never fenced under one identity and executed under another.
    canonical = str(row.get("name") or role)
    bound = (os.environ.get("AGENT_MAIL_NAME") or "").strip()
    if bound:
        try:
            bound = agent_names.resolve(bound, db=db,
                                        operation="pilot_gesture.role")
        except Exception:  # noqa: BLE001 - keep the spelling; comparison below
            pass
        if bound != canonical:
            raise GestureRefused(
                "gesture %s refused: role %s but the session names %s"
                % (gesture, canonical, bound))
    current_owner = str(row.get("lease_owner") or "")
    declared_owner = (os.environ.get("AMEESH_LEASE_OWNER")
                      or os.environ.get("AMEESH_RUNNER_ID")
                      or os.environ.get("AGENT_MESH_RUNNER_ID") or "").strip()
    if declared_owner and current_owner and declared_owner != current_owner:
        raise GestureRefused(
            "gesture %s refused for role %s: the live lease is held by %s, "
            "not by %s" % (gesture, canonical, current_owner, declared_owner))
    state = storage.of(db).leases.state(canonical) or {}
    reason = ""
    if not state.get("live"):
        reason = "lease expired"
    elif str(row.get("status") or "") == "stopped":
        reason = "role stopped"
    elif int(row.get("lease_epoch") or 0) != epoch:
        reason = "epoch %s in the registry, %s in the environment" % (
            row.get("lease_epoch"), epoch)
    if reason:
        raise GestureRefused(
            _refusal(db, Gate(canonical, current_owner, epoch, gesture,
                              (ref or "").strip(), project,
                              host=str(row.get("host") or ""), role=canonical,
                              reason=reason)))
    return Gate(canonical, current_owner, epoch, gesture, (ref or "").strip(),
                project, host=str(row.get("host") or ""), role=canonical)


def gate(cfg, db, gesture: str, ref: str, *, project: str | None = None) -> Gate | None:
    """The fence to apply before `gesture`, or None when no fence applies.

    None for a human/bureau (no `AGENT_MAIL_NAME` and no `AMEESH_ROLE`), a
    non-elected agent, an unknown agent, or no database at all. Raises
    `GestureRefused` for a session whose named agent is elected but not the
    current lease-holder, for an `AMEESH_ROLE` command whose epoch is missing
    or stale, or whose identity cannot be read while it claims a lease. A real
    database failure (`DbError`, or an exception on a connection with
    `query`/`execute`) fails closed; a test double without them is not a
    database and is not fenced.
    """
    if gesture not in GESTURES:
        raise ValueError("unknown pilot gesture %r (%s)" % (gesture, " | ".join(GESTURES)))
    role = (os.environ.get("AMEESH_ROLE") or "").strip()
    if role:
        return _role_gate(cfg, db, role, gesture, ref, project)
    if db is None:
        return None
    name = (os.environ.get("AGENT_MAIL_NAME") or "").strip()
    if not name:
        return None
    try:
        row = registry.get(db, name)
    except Exception as exc:  # noqa: BLE001 - a real database failure fails closed
        from .db import DbError
        if _claims_lease() and (isinstance(exc, DbError) or _is_database(db)):
            raise GestureRefused(
                "gesture %s refused for %s: cannot read the registry to verify the "
                "lease epoch (%s)"
                % (gesture, name, " ".join(str(exc).split())[:160])) from exc
        return None
    if not row or row.get("releve") != "election":
        return None
    try:
        binding = identity.resolve_binding(_cfg(db, cfg), db)
    except Exception as exc:  # noqa: BLE001 - a real database failure fails closed
        from .db import DbError
        if isinstance(exc, DbError) or _is_database(db):
            raise GestureRefused(
                "gesture %s refused for %s: cannot verify the lease epoch (%s)"
                % (gesture, name, " ".join(str(exc).split())[:160])) from exc
        return None
    # `registry.get` resolves aliases: the lease is read under the canonical
    # name, not the spelling the caller's environment happens to use.
    canonical = str(row.get("name") or name)
    # A session bound only by `mail bind` (or a bare `AGENT_MAIL_NAME`) is not
    # the runner's lease-holder: an elected role may act only under its lease.
    if binding.source != "runner" or not binding.ok or not binding.name:
        raise GestureRefused(
            _refusal(db, Gate(canonical, binding.runner_id, binding.epoch or 0, gesture,
                              (ref or "").strip(), project,
                              host=str(row.get("host") or ""),
                              reason=binding.reason or "session is not lease-bound")))
    if binding.name != canonical:
        # `identity.resolve_binding` resolves aliases too; keep the guard
        # explicit rather than fencing a different agent by accident.
        raise GestureRefused(
            "gesture %s refused: session names %s, the lease belongs to %s"
            % (gesture, name, binding.name))
    return Gate(canonical, binding.runner_id, int(binding.epoch), gesture,
                (ref or "").strip(), project, host=str(row.get("host") or ""),
                reason=binding.reason)


def guard(cfg, db, gesture: str, ref: str, *, project: str | None = None,
          ttl_s: float = DEFAULT_TTL_S) -> Gate | None:
    """`gate()` then `hold()`; for a gesture whose preparation has no DB write
    of its own (an external merge, a routing decision). Returns the Gate, or
    None when the caller is not an elected pilot."""
    g = gate(cfg, db, gesture, ref, project=project)
    if g is not None:
        g.hold(db, ttl_s=ttl_s)
    return g
