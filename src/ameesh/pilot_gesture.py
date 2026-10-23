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

1. reads the agent's Agent card (`releve`);
2. refuses a session that is not the live lease-holder, naming the current
   holder (or "nobody") and its host/epoch;
3. journals the gesture *in the same transaction* as a `renew` of the lease
   (the append-only `ameesh_events` log, type `pilot.gesture`), so a stale
   epoch can never write the receipt either.

The event is the durable trace of the authorization; the effect follows it.
A human or the bureau has no agent lease to fence and passes through, as does
a host-pinned agent (`releve: hote`): its placement already prevents another
host from holding the lease, and this lot deliberately changes no existing
workflow for it. A runner process has no session identity (`AGENT_MAIL_NAME`)
and passes through too; only a session opened under a lease is fenced.

Nothing here contains a secret: the event carries the agent, gesture, ref,
epoch and holder host, never a token.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import identity, registry, storage

#: the five gestures of the study; kept explicit so a typo can never create a
#: new event namespace by accident
GESTURES = ("merge", "queue", "assign", "packet", "decision")
#: event type written in `ameesh_events` (append-only, one year by default)
EVENT = "pilot.gesture"
#: lease duration used to hold the role while the gesture runs; the runner's
#: own default (its heartbeat renews at `lease_ttl / 3`)
DEFAULT_TTL_S = 300.0


class GestureRefused(RuntimeError):
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
    #: why the binding is not a live lease (kept for the refusal message)
    reason: str = field(default="", compare=False)

    def hold(self, db, *, ttl_s: float = DEFAULT_TTL_S) -> int:
        """Renew the lease and journal the gesture, in the same transaction.

        A stale epoch cannot write the receipt: `registry.renew` is fenced by
        `(owner, epoch)` and locks the agent row, so an election that commits
        between this session's last renewal and this call is seen here. The
        effect may then run: the lease is live again for `ttl_s`, and a claim
        only takes a free or expired lease (EP1). Returns the event id.
        """
        with db.transaction() as tx:
            if registry.renew(tx, self.agent, self.runner, self.epoch, ttl_s) is None:
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
            return storage.of(tx).events.record(
                EVENT, "agent:%s" % self.agent, self.project,
                {"agent": self.agent, "gesture": self.gesture, "ref": self.ref,
                 "epoch": self.epoch, "runner": self.runner,
                 "host": self.host})


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


def gate(cfg, db, gesture: str, ref: str, *, project: str | None = None) -> Gate | None:
    """The fence to apply before `gesture`, or None when no fence applies.

    None for a human/bureau (no `AGENT_MAIL_NAME`), a non-elected agent, an
    unknown agent, or no database at all. Raises `GestureRefused` for a
    session whose named agent is elected but not the current lease-holder, or
    whose identity cannot be read while it claims a lease.
    """
    if db is None:
        return None
    if gesture not in GESTURES:
        raise ValueError("unknown pilot gesture %r (%s)" % (gesture, " | ".join(GESTURES)))
    name = (os.environ.get("AGENT_MAIL_NAME") or "").strip()
    if not name:
        return None
    try:
        row = registry.get(db, name)
    except Exception as exc:  # noqa: BLE001 - a claimed lease must fail closed
        if _claims_lease():
            raise GestureRefused(
                "gesture %s refused for %s: cannot read the registry to verify the "
                "lease epoch (%s)"
                % (gesture, name, " ".join(str(exc).split())[:160])) from exc
        return None
    if not row or row.get("releve") != "election":
        return None
    try:
        binding = identity.resolve_binding(_cfg(db, cfg), db)
    except Exception as exc:  # noqa: BLE001 - it claims a lease: fail closed
        raise GestureRefused(
            "gesture %s refused for %s: cannot verify the lease epoch (%s)"
            % (gesture, name, " ".join(str(exc).split())[:160])) from exc
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
