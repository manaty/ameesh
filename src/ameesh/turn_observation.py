# SPDX-License-Identifier: AGPL-3.0-only
"""Turn observation for supervisors (#2167, lot #4831).

`observe()` reads the facts (`turn_resources`, `spend_pending`, `turn_costs`,
`agent_registry`, and a mailbox row for a request) and returns the
`ameesh-turn-observation/1` document of the study
`docs/design/etudes/l2894-turn-context-api.md`, section 3. The same builder
serves `ameesh turn observe` (lot B2) and `GET /api/v1/turns/{turn}` /
`GET /api/v1/requests/{request}`; it never writes.

The document is **metadata only**: no message body, no `payload`, no
secret. The request block names the message, its state and the turn that
delivered it; the accounting block says what the ledger, the marker and the
reservation prove, never more.

Derived states (the contract):

* `turn.state`: `starting` (marker present, no resource row yet), `running`,
  `ended` (resource `done`, or a ledger row without a resource), `orphan`
  (resource marked orphan), `unknown` (the turn id is only known from a
  request's delivery, no fact remains).
* `request.state`: `pending` (not delivered, not withdrawn), `delivered`,
  or `withdrawn` (withdrawn by ameesh before delivery).
* `accounting.state`, in this order: a ledger row → `settled`, but a
  reservation `kept` or `released` wins (terminal outcome); else a marker →
  `pending`; else a reservation alone → `unknown` (never silently `done`);
  else `none`.
* `quiescent`: the exact conjunction over the **explicit allowlist**
  `settled|released|kept|none`: agent `idle`, accounting in that allowlist,
  and no turn in progress. `pending`, `unknown` and any future enum are not
  quiescent.
"""
from __future__ import annotations

import time

from . import account_budget, storage

SCHEMA = "ameesh-turn-observation/1"
#: `turn.state` values, in the study's vocabulary
TURN_STATES = ("starting", "running", "ended", "orphan", "unknown")
#: `request.state` values
REQUEST_STATES = ("pending", "delivered", "withdrawn")
#: `accounting.state` values
ACCOUNTING_STATES = ("settled", "pending", "none", "unknown", "kept", "released")
#: the accounting states that may be quiescent (an explicit allowlist: a
#: future enum value can never make `quiescent` true by accident)
QUIESCENT_ACCOUNTING = frozenset(("settled", "released", "kept", "none"))


class Unknown(LookupError):
    """The observed target does not exist (404, never an empty document)."""


def _request_block(row: dict | None) -> dict | None:
    if row is None:
        return None
    if row.get("withdrawn"):
        state = "withdrawn"
    elif row.get("delivered_ts") is None:
        state = "pending"
    else:
        state = "delivered"
    delivered = state == "delivered"
    return {
        "id": int(row["id"]),
        "agent": row["recipient"],
        "state": state,
        "created_ts": row.get("created_ts"),
        # a withdrawn message was never delivered: the instant ameesh removed
        # it is not a delivery, and it has no delivery turn.
        "delivered_ts": row.get("delivered_ts") if delivered else None,
        "delivered_turn_id": (row.get("delivered_turn_id") or None) if delivered else None,
    }


def _turn_block(turn: dict | None) -> dict | None:
    if turn is None or turn.get("id") is None:
        return None
    resource = turn.get("resource")
    marker = turn.get("marker")
    ledger = turn.get("ledger")
    if resource is not None:
        state = {"running": "running", "done": "ended",
                 "orphan": "orphan"}.get(resource.get("status"), "unknown")
    elif ledger is not None:
        state = "ended"
    elif marker is not None:
        state = "starting"
    else:
        state = "unknown"
    started_ts = ended_ts = None
    if resource is not None:
        started_ts = resource.get("started_ts")
        ended_ts = resource.get("ended_ts")
    elif ledger is not None:
        started_ts = ledger.get("started_ts")
        ended_ts = ledger.get("recorded_ts")
    elif marker is not None:
        started_ts = marker.get("created_ts")
    work_item_id = None
    for row in (ledger, marker):
        if row is not None and row.get("work_item_id") is not None:
            work_item_id = str(int(row["work_item_id"]))
            break
    return {"id": turn["id"], "state": state, "started_ts": started_ts,
            "ended_ts": ended_ts, "work_item_id": work_item_id}


def _agent_block(row: dict) -> dict:
    return {
        "name": row["name"],
        "status": row.get("status") or "unknown",
        "status_since_ts": row.get("status_since_ts"),
        "status_text": row.get("status_text"),
        "harness": row.get("harness"),
        "model": row.get("model"),
        "session_id": row.get("session_id"),
        "turns": int(row.get("turns") or 0),
        "budget_usd": row.get("budget_usd"),
        "spent_usd": row.get("spent_usd"),
        "turn_in_progress": bool(row.get("status") == "running"
                                 and row.get("lease_live")),
        "last_turn_ts": row.get("last_turn_ts"),
    }


def _ledger_block(row: dict) -> dict:
    return {
        "turn_id": row.get("turn_id"),
        "spend_key": row.get("spend_key"),
        "billing_account": row.get("billing_account"),
        "usd": row.get("usd"),
        "model": row.get("model"),
        "recorded_ts": row.get("recorded_ts"),
        "source": row.get("source"),
    }


def _accounting(db, turn: dict | None) -> dict:
    if turn is None:
        return {"state": "none", "spend_key": None, "marker": None,
                "reservation": None, "ledger": None}
    marker = turn.get("marker")
    ledger = turn.get("ledger")
    spend_key = (ledger or {}).get("spend_key") or (marker or {}).get("spend_key")
    reservation = account_budget.reservation(db, spend_key) if spend_key else None
    if ledger is not None:
        state = "settled"
        # a kept/released reservation is terminal and wins over "settled":
        # accounting is closed one way or the other, never silently retried.
        if reservation is not None and reservation.get("state") in ("kept", "released"):
            state = reservation["state"]
    elif marker is not None:
        state = "pending"
    elif reservation is not None:
        state = "unknown"
    else:
        state = "none"
    return {
        "state": state,
        "spend_key": spend_key,
        "marker": None if marker is None else {
            "created_ts": marker.get("created_ts"),
            "model": marker.get("model"),
            "turn_id": marker.get("turn_id"),
        },
        "reservation": None if reservation is None else {
            "billing_account": reservation.get("billing_account"),
            "usd": reservation.get("usd"),
            "period": reservation.get("period"),
            "state": reservation.get("state"),
        },
        "ledger": None if ledger is None else _ledger_block(ledger),
    }


def observe(db, *, turn: str | None = None, request: int | None = None,
            now: float | None = None) -> dict:
    """The `ameesh-turn-observation/1` document of one turn or one request.

    Exactly one of `turn` (the turn id) or `request` (the mailbox id) is
    given. Raises `Unknown` when the target has no fact at all; a request
    without a delivery turn is observable, with `turn: null`.
    """
    if (turn is None) == (request is None):
        raise ValueError("observe: exactly one of turn or request is required")
    facts = storage.of(db).turn_observation.read(turn=turn, request=request)
    if facts is None:
        raise Unknown("unknown request" if request is not None else "unknown turn")
    observed_ts = time.time() if now is None else float(now)
    request_block = _request_block(facts["request"])
    turn_block = _turn_block(facts["turn"])
    agent_block = _agent_block(facts["agent"])
    accounting = _accounting(db, facts["turn"])
    agent = facts["agent"]
    quiescent = (agent_block["status"] == "idle"
                 and accounting["state"] in QUIESCENT_ACCOUNTING
                 and not agent_block["turn_in_progress"])
    return {
        "schema": SCHEMA,
        "observed_ts": observed_ts,
        "by": ({"request": int(request)} if request is not None
               else {"turn": str(turn)}),
        "request": request_block,
        "turn": turn_block,
        "agent": agent_block,
        "lease": {
            "owner": agent.get("lease_owner"),
            "epoch": (int(agent["lease_epoch"])
                      if agent.get("lease_epoch") is not None else None),
            "live": bool(agent.get("lease_live")),
            "expires_ts": agent.get("lease_expires_ts"),
        },
        "accounting": accounting,
        "quiescent": quiescent,
    }
