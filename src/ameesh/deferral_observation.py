# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded structured observation of a held turn (E3b, #7675).

The core records why a turn is held in ``agent_registry.status`` /
``status_text`` and in the operator-pause columns. This module maps those
facts to **one** closed ``ameesh-deferral/1`` block. It is deliberately
bounded:

* one block, for the current hold only, no history and no counter;
* a closed ``reason`` enum (``REASONS``): an unrecognised ``blocked`` text is
  ``other``, never a guessed cause;
* no free text, no prompt, no mail body and no payload: the human text stays
  in ``status_text`` and is not copied here. A pressure block reuses the
  already-public ``queue`` object (scope, age, next look), never an arbitrary
  error string.

``None`` means "no known deferral": it is not permission, not a promise that
a turn will run, and never a replacement for ``accounting.state``. When the
ledger evidence is ambiguous, the native reservation stays authoritative (see
``turn_observation``): the block never claims the work failed or was settled.

The same block is embedded in ``ameesh list --json``, ``ameesh show --json``,
``GET /api/v1/agents`` / ``/agents/{agent}`` and ``ameesh-turn-observation/1``.
"""
from __future__ import annotations

import time

from . import account_window, accounts, auth_failure, budget, provider_credits
from . import resources

SCHEMA = "ameesh-deferral/1"

#: ``state`` values of the block. A new state is additive in ``/1``; a
#: consumer must read an unknown state as "not proven available".
STATES = ("deferred",)

#: closed reason enum, in precedence order (the first matching fact wins).
#: ``other`` is a refusal to guess, not a category to rely on.
REASONS = (
    "operator",
    "pressure",
    "authentication",
    "provider_credits",
    "budget",
    "account",
    "account_window",
    "session",
    "database",
    "host",
    "workdir",
    "shutdown",
    "restart",
    "other",
)

#: status texts the runner persists. They are duplicated here rather than
#: importing ``runner`` (the observation must stay importable without the
#: executor); ``tests/test_7675_deferral.py`` asserts they still equal the
#: runner constants, so a rename cannot silently break the mapping.
SESSION_STATUS = "session lock held"
DATABASE_STATUS = "base injoignable"
HOST_STATUS = "hôte non prêt"
WORKDIR_STATUS = "dossier absent"
SHUTDOWN_PREFIX = "runner shutting down:"
RESTART_PREFIX = "outdated runner:"


def _epoch(value) -> float | None:
    """Epoch seconds from a timestamp column, or None (never a guess)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return float(timestamp())
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _first_epoch(*values) -> float | None:
    for value in values:
        epoch = _epoch(value)
        if epoch is not None:
            return epoch
    return None


def _blocked_reason(text: str) -> str:
    if text.startswith(auth_failure.STATUS_PREFIX):
        return "authentication"
    if text.startswith(provider_credits.STATUS_PREFIX):
        return "provider_credits"
    if text.startswith(budget.PAUSE_PREFIX):
        return "budget"
    if text.startswith(accounts.CONFIG_BLOCK_PREFIX):
        return "account"
    if account_window.is_short_window(text):
        return "account_window"
    if text.startswith(SESSION_STATUS):
        return "session"
    if text.startswith(DATABASE_STATUS):
        return "database"
    if text.startswith(HOST_STATUS):
        return "host"
    if text.startswith(WORKDIR_STATUS):
        return "workdir"
    return "other"


def _queued_reason(text: str) -> str | None:
    if text.startswith(SHUTDOWN_PREFIX):
        return "shutdown"
    if text.startswith(RESTART_PREFIX):
        return "restart"
    return None


def classify(row: dict | None, *, since_ts: float | None = None,
             now: float | None = None) -> dict | None:
    """The ``ameesh-deferral/1`` block of an action row, or None.

    ``row`` is an ``agent_registry`` row (or the overview/operations row):
    ``status``, ``status_text`` and the operator-pause fields are the only
    facts read. ``since_ts`` overrides the row's own start (callers that
    converted the timestamp already, e.g. ``turn observe``). ``now`` is for a
    deterministic age in tests; the default is wall clock.
    """
    if not isinstance(row, dict):
        return None
    status = row.get("status") or ""
    text = str(row.get("status_text") or "").strip()
    queue = resources.parse_queue_status(text)
    reason = None
    since = None
    if row.get("operator_paused") and status != "running":
        # an operator pause that has taken effect; `after_turn` on a running
        # agent is not a deferral yet.
        reason = "operator"
        since = _first_epoch(row.get("operator_pause_ts"),
                             row.get("operator_pause_at"))
    elif queue is not None:
        reason = "pressure"
    elif status == "blocked":
        reason = _blocked_reason(text)
    elif status == "queued":
        reason = _queued_reason(text)
    if reason is None:
        return None
    if since is None:
        since = _first_epoch(since_ts, row.get("status_reason_since_ts"),
                             row.get("status_reason_since"),
                             row.get("status_since_ts"), row.get("status_since"))
    moment = time.time() if now is None else float(now)
    block = {
        "schema": SCHEMA,
        "state": "deferred",
        "reason": reason,
        "since_ts": since,
        "age_s": (int(max(0.0, moment - since)) if since is not None else None),
    }
    if queue is not None:
        block["queue"] = queue
    return block
