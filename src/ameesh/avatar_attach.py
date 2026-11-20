# SPDX-License-Identifier: AGPL-3.0-only
"""#7897: an attach on the preferred workstation requests a boundary move.

The local CLI has database access, just like a local attach. A relay-only
station without database access keeps using D3 and cannot place an avatar.
No lease is taken here: the target runner prepares the ordinary conversation
move, whose switch waits for the source turn to finish.
"""
from __future__ import annotations

import sys
import time

from . import conversation_move, election, registry, storage
from .agent_names import resolve


def _refusal(db, row, host):
    if (row.get("releve") != "preference" or row.get("mode") != "execute"
            or row.get("status") == "stopped" or row.get("host") == host):
        return "not a remote running avatar"
    # Declared rank zero is the workstation; availability rank zero could be
    # a fallback cloud host and must never turn its attach into a return.
    candidate = next((c for c in election.candidacy(db, row["name"])
                      if c["host"] == host), None)
    if not candidate or candidate.get("preference_rank") != 0 or not candidate["ready"]:
        return "workstation not present (B2)"
    from . import host_holds, relocation
    if host in host_holds.live(db):
        return "workstation on hold"
    if storage.of(db).handoffs.active(row["name"]):
        return "host handoff in progress"
    for guard in (relocation._preference_tenant_refusal,
                  relocation._preference_account_refusal):
        reason = guard(db, row, host)
        if reason:
            return reason
    return ""


def plan_return(db, name: str, host: str) -> dict | None:
    """Plan once under the source registry lock; unhealthy stations do nothing.

    `manual` records the explicit local attach gesture and lifts the failover
    hold. It uses the deferred plan, never the immediate operator switch.
    Recheck placement and B2 health after taking the lock, before any write.
    """
    name = resolve(name, db=db, operation="lease.attach")
    row = registry.get(db, name)
    if row is None or _refusal(db, row, host):
        return None
    source = row["host"]

    def recheck(tx, locked):
        if locked["host"] != source:
            return "source changed during attach"
        return _refusal(tx, locked, host)

    try:
        attempt = conversation_move.plan(
            db, name, host, requested_by="attach:%s" % host,
            reason="manual", recheck=recheck)
    except conversation_move.RecheckRefused:
        return None
    # plan can reread a concurrent attempt to another target. Never wait for
    # it as though it were this attach's return.
    if attempt and attempt["target_host"] == host:
        return attempt
    return None


def wait_return(db, move: dict, *, max_seconds: float, out=None) -> bool:
    """Wait for the target runner, bounded by the attach's own duration."""
    out = sys.stderr if out is None else out
    print("attach: returning to %s after the cloud turn; waiting for the local runner"
          % move["target_host"], file=out, flush=True)
    deadline = time.monotonic() + max_seconds
    moves = storage.of(db).conversation_moves
    while time.monotonic() < deadline:
        current = moves.get(move["move_id"])
        row = registry.get(db, move["agent"])
        if not current or current["state"] == "failed" or not row:
            print("attach: return could not complete; retry the attach", file=out)
            return False
        if (row["host"] == move["target_host"]
                and current["state"] == "verified"):
            # The first target turn has installed its native session.
            # Never attach to the source's session id during rehydration.
            return True
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    print("attach: return still pending; retry the attach", file=out)
    return False
