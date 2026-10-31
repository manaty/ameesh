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

The renewal never shortens the lease: it takes at least the TTL the agent's
host uses (`policy.lease_ttl` of its Host card, else the executor
configuration — the runner's own rule) and never below the expiry currently
held. Renewing below that TTL would make the lease expire before the runner's
next heartbeat (`lease_ttl / 3`) and elect another pilot for a mere gesture
(#3115). It also does not hold the role longer than that rule: on a volatile
host (90 s), the 300 s default would keep a disappeared host's pilot alive
for 300 s and delay the election by as much; `ttl_s` only covers a host with
no known rule, and an external call's margin still wins (#3123).

The drill's `canary` (EP7 P3, `ameesh role canary`) goes through the same
gate and `hold` but has no effect at all: the lease renewal and its event are
its only writes. A real gesture used as a canary (`work assign` of a lot to
its current assignee) is refused by its business rule before the fence and
proves neither the refusal of a stale epoch nor the acceptance of a live one.

The event is the durable trace of the authorization; the effect follows it.
A human or the bureau has no agent lease to fence and passes through, as does
a host-pinned agent (`releve: hote`): its placement already prevents another
host from holding the lease, and this lot deliberately changes no existing
workflow for it. A runner process with neither `AGENT_MAIL_NAME` nor
`AMEESH_ROLE` passes through too; only a session or a role command opened
under a lease is fenced.

#4245: an executor restart on the SAME host (the #4086 reservation) moves
the epoch from E to E+2 without the role ever leaving the host; a timer
command opened at E (a packet composition, 20 to 60 min) was refused and
lost its attempt. An `AMEESH_ROLE` command at epoch E is therefore accepted
at E' > E when the lease's **same-host lineage** covers E: every holder since
`lease_lineage_epoch <= E` was on `lease_lineage_host`, which is the row's
host and the command's own (`AMEESH_ROLE_HOST`). The lineage is kept by a
trigger for every lease writer (migration 0503): a holder on another host
restarts it, so a lease that went elsewhere between E and E' is still
refused (#4087). While the role is free under a live restart reservation to
this host, the gate waits for the reclaim, bounded by the reservation, then
refuses. An accepted gate renews the live lease (its owner and epoch E') and
journals both epochs; `Gate.hold` re-checks the lineage under the row lock.
Agent sessions (`AGENT_MAIL_NAME`) keep the exact epoch: the runner hands
them the new one.

Nothing here contains a secret: the event carries the agent, gesture, ref,
epoch and holder host, never a token.
"""
from __future__ import annotations

import dataclasses
import os
import time
from dataclasses import dataclass, field

from . import agent_names, identity, registry, storage

#: the pilot gestures, the elected release pass, and the no-op `canary` (EP7 P3,
#: `ameesh role canary`, #3433); kept explicit so a typo can never create a
#: new event namespace by accident
GESTURES = ("merge", "queue", "assign", "packet", "decision", "canary", "release")
#: event type written in `ameesh_events` (append-only, one year by default)
EVENT = "pilot.gesture"
#: minimum lease duration used to hold the role while the gesture runs (the
#: runner's own default) when no Host card rules it; a Host card rule replaces
#: it (`Gate.host_lease_ttl`), so a volatile host is not held 300 s (#3123)
DEFAULT_TTL_S = 300.0
#: extra time added to a lease renewed for an external call whose timeout is
#: `margin_s`: the call must have the whole margin left, not exactly it
RENEW_GRACE_S = 30.0
#: #4245: polling period while a restart reservation to this host is pending
RESERVATION_POLL_S = 2.0


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
    #: the lease TTL the agent's host uses (`policy.lease_ttl` or `volatile`
    #: of its Host card, else the executor configuration): `hold` never renews
    #: below it, so a gesture cannot shorten the lease the runner renews
    lease_ttl: float = DEFAULT_TTL_S
    #: the Host card's own rule when it declares one, else None: `hold` uses
    #: it instead of `ttl_s`, which only covers hosts with no known rule
    #: (holding the 300 s default on a volatile host would delay its failover,
    #: #3123)
    host_lease_ttl: float | None = None
    #: #4245: the epoch the command entered with, when the gate accepted it at
    #: a later epoch of the same-host lineage (`epoch` is the live one renewed)
    entered_epoch: int = 0

    def hold(self, db, *, ttl_s: float = DEFAULT_TTL_S, margin_s: float = 0.0) -> int:
        """Renew the lease and journal the gesture, in the same transaction.

        A stale epoch cannot write the receipt: `registry.renew` is fenced by
        `(owner, epoch)` and locks the agent row, so an election that commits
        between this session's last renewal and this call is seen here. The
        effect may then run: the lease is live again for at least the host's
        own TTL (`lease_ttl`), or `ttl_s` when the host has no known rule, and
        a claim only takes a free or expired lease (EP1). Returns the event id.

        The renewal also never shortens an expiry already held: `renew` keeps
        the later of the current expiry and the new one under the row lock
        (`GREATEST`), so a Host card unreadable here, a configuration changed
        since the claim, or a heartbeat renewing between our read and the lock
        must not make the lease expire before the runner's next heartbeat and
        elect another pilot (#3115).

        `ttl_s` is the minimum to hold while the gesture runs when no Host
        card rules the duration (the executor configuration, 300 s by
        default). When one does (`host_lease_ttl`), the rule is what the
        runner's next heartbeat renews: holding the generic default on top of
        it would keep the role alive after a volatile host disappears and
        delay the election by as much (#3123). Only an external call's margin
        can then extend beyond the rule.

        `margin_s` is the worst-case duration of an external call (a push, a
        `gh pr merge`, its read-back) made right after this transaction. The
        lease is renewed to cover that margin plus `RENEW_GRACE_S`, and the
        call is refused if the renewed lease does not have the margin left:
        the residual race (the call outliving its lease on a network hang) is
        documented in `docs/HOST-HANDOFFS.md`; GitHub's `--match-head-commit`
        and the target lease remain the last guards.
        """
        margin = max(0.0, float(margin_s))
        held = margin + RENEW_GRACE_S
        if self.host_lease_ttl is not None:
            # #3123: renew to the host's rule, not to the 300 s default, so a
            # gesture cannot keep a volatile host's role for 300 s after it
            # disappears; the next heartbeat renews the same rule.
            ttl = max(float(self.host_lease_ttl), held)
        else:
            ttl = max(float(ttl_s), float(self.lease_ttl or 0.0), held)
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
            if self.entered_epoch and not _lineage_covers(
                    _lineage(tx, self.agent), self.entered_epoch, self.host):
                # #4245: under the row lock `renew` took; the lineage only
                # changes with the holder, the epoch or the host
                raise GestureRefused(_refusal(tx, dataclasses.replace(
                    self, epoch=self.entered_epoch,
                    reason="same-host lineage broken since epoch %s"
                    % self.entered_epoch)))
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
            if self.entered_epoch:
                data["epoch"] = self.entered_epoch
                data["lease_epoch"] = self.epoch
                data["lineage"] = "same-host"
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


def _positive(value, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def effective_ttl(cfg, host: str) -> tuple[float, float | None]:
    """The lease TTL the runner of `host` applies, and the Host card rule.

    The runner renews with `policy.lease_ttl` of the host's Host card (the
    shortest of the canons), `policy.volatile` (90 s), else the executor
    configuration (`AMEESH_LEASE_TTL`, 300 s) — `host_gate.lease_ttl_for`.
    Reading the same canons here (read-only, no database) gives `Gate.hold`
    the duration the next heartbeat expects: renewing below it would let the
    lease expire before that heartbeat and elect another pilot for a mere
    gesture (#3115). A canon that cannot be read leaves the configuration as
    the fallback; `Gate.hold` also keeps the expiry it already found.

    The second element is the Host card's own rule when one is known, else
    None: `Gate.hold` keeps the generic `ttl_s` for a host with no rule, and
    applies the rule instead otherwise, so a gesture does not hold a volatile
    host's role beyond its 90 s (#3123).
    """
    default = _positive(getattr(cfg, "lease_ttl", None), DEFAULT_TTL_S)
    if cfg is None:
        return DEFAULT_TTL_S, None
    try:
        from . import canon as canon_mod
        canons = canon_mod.load_configured(cfg)
    except Exception:  # noqa: BLE001 - the configuration remains the fallback
        canons = ()
    try:
        from .mediated_executor.host_gate import lease_ttl_rule
    except Exception:  # noqa: BLE001 - the configuration remains the fallback
        return default, None
    try:
        rule = _positive(lease_ttl_rule(canons, host), 0.0)
    except Exception:  # noqa: BLE001 - the configuration remains the fallback
        return default, None
    if not rule:
        return default, None
    return rule, rule


def _claims_lease() -> bool:
    """The session carries a runner id or a lease epoch: it claims a lease."""
    return any(os.environ.get(key) for key in
               ("AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH"))


def _is_database(db) -> bool:
    """A connection or transaction, not a test double without `query`/`execute`."""
    return callable(getattr(db, "query", None)) and callable(getattr(db, "execute", None))


def _lineage(db, name: str) -> dict:
    """#4245: the lease row with its same-host lineage (migration 0503).

    Empty when it cannot be read (a schema without the columns, a backend
    without SQL): no lineage is then presumed and the exact epoch rules.
    """
    if not _is_database(db):
        return {}
    try:
        rows = db.query(
            """
            SELECT host, lease_owner, lease_epoch, status,
                   lease_lineage_host, lease_lineage_epoch,
                   (lease_expires_at IS NOT NULL
                    AND lease_expires_at > clock_timestamp()) AS live
              FROM agent_registry WHERE name = %s
            """, (name,))
    except Exception:  # noqa: BLE001 - no lineage read, no stale epoch accepted
        return {}
    return dict(rows[0]) if rows else {}


def _lineage_covers(row: dict, epoch: int, host: str) -> bool:
    """#4245: the live lease descends from `epoch` without leaving `host`.

    Every holder since `lease_lineage_epoch` was on `lease_lineage_host`
    (trigger of 0503); a session of epoch E is covered at E' > E when that
    base is at most E and the lineage host is the row's and the session's.
    """
    try:
        current = int(row.get("lease_epoch") or 0)
        base = row.get("lease_lineage_epoch")
        base = None if base is None else int(base)
    except (TypeError, ValueError):
        return False
    return bool(host and base is not None and row.get("live")
                and row.get("lease_owner")
                and str(row.get("status") or "") != "stopped"
                and base <= int(epoch) < current
                and row.get("lease_lineage_host") == row.get("host") == host)


def _await_reclaim(db, name: str, host: str, *, sleep=None, clock=None) -> dict:
    """#4245: the role is free under a restart reservation to `host` (#4086):
    wait for the restarted executor's claim, at most for what is left of the
    reservation. Returns the lineage row read last (possibly still free)."""
    from . import role_handover_pg
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    row = _lineage(db, name)
    deadline = None
    while row and not (row.get("live") and row.get("lease_owner")):
        try:
            reserved = role_handover_pg.restart_reservation(db, name)
        except Exception:  # noqa: BLE001 - no reservation read, no wait
            reserved = None
        if not reserved or reserved.get("host") != host:
            break
        if deadline is None:
            deadline = clock() + max(0.0, float(reserved.get("left_s") or 0.0))
        if clock() >= deadline:
            break
        sleep(min(RESERVATION_POLL_S, max(0.0, deadline - clock())))
        row = _lineage(db, name)
    return row


def _same_host(db, name: str, epoch: int, *, sleep=None, clock=None) -> dict | None:
    """#4245: the live lease row when it covers `epoch` on this command's host
    (`AMEESH_ROLE_HOST`, set by `ameesh role run`), else None."""
    host = (os.environ.get("AMEESH_ROLE_HOST") or "").strip()
    if not host:
        return None
    row = _await_reclaim(db, name, host, sleep=sleep, clock=clock)
    return row if _lineage_covers(row, epoch, host) else None


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
    if int(row.get("lease_epoch") or 0) != epoch:
        # #4245: an executor restart on this host moved the epoch (and the
        # runner id); the command keeps the role if the lineage covers it
        lineage = _same_host(db, canonical, epoch)
        if lineage is not None:
            host = str(lineage.get("host") or "")
            ttl, rule = effective_ttl(_cfg(db, cfg), host)
            return Gate(canonical, str(lineage.get("lease_owner") or ""),
                        int(lineage["lease_epoch"]), gesture, (ref or "").strip(),
                        project, host=host, role=canonical,
                        reason="same-host lineage since epoch %s"
                        % lineage.get("lease_lineage_epoch"),
                        lease_ttl=ttl, host_lease_ttl=rule, entered_epoch=epoch)
        try:  # the wait may have seen a claim: name the holder read now
            row = registry.get(db, role) or row
        except Exception:  # noqa: BLE001 - the refusal below stands
            pass
    current_owner = str(row.get("lease_owner") or "")
    declared_owner = (os.environ.get("AMEESH_LEASE_OWNER")
                      or os.environ.get("AMEESH_RUNNER_ID")
                      or os.environ.get("AGENT_MESH_RUNNER_ID") or "").strip()
    # #4245: a stale epoch is refused below as such (the fence names the
    # holder), not as an owner mismatch: the caller reads it as a fence
    if (declared_owner and current_owner and declared_owner != current_owner
            and int(row.get("lease_epoch") or 0) == epoch):
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
    host = str(row.get("host") or "")
    ttl, rule = effective_ttl(_cfg(db, cfg), host)
    return Gate(canonical, current_owner, epoch, gesture, (ref or "").strip(),
                project, host=host, role=canonical,
                lease_ttl=ttl, host_lease_ttl=rule)


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
    host = str(row.get("host") or "")
    ttl, rule = effective_ttl(_cfg(db, cfg), host)
    return Gate(canonical, binding.runner_id, int(binding.epoch), gesture,
                (ref or "").strip(), project, host=host,
                reason=binding.reason,
                lease_ttl=ttl, host_lease_ttl=rule)


def guard(cfg, db, gesture: str, ref: str, *, project: str | None = None,
          ttl_s: float = DEFAULT_TTL_S) -> Gate | None:
    """`gate()` then `hold()`; for a gesture whose preparation has no DB write
    of its own (an external merge, a routing decision). Returns the Gate, or
    None when the caller is not an elected pilot."""
    g = gate(cfg, db, gesture, ref, project=project)
    if g is not None:
        g.hold(db, ttl_s=ttl_s)
    return g
