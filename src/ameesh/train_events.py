# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit train wake-up transport; producers are installed separately.

Call ``emit`` on the connection/transaction that persists the event. PostgreSQL
delivers the hint only after commit. ``receive`` only schedules a passage;
neither a verdict nor CI/merge authority can travel in this envelope.
"""
from __future__ import annotations

import json

from . import config, merge_queue, plan_git, storage

CHANNEL = "ameesh_train"
SCHEMA = "ameesh-train-wakeup/1"
KINDS = ("ci-terminal", "verdict", "fire")
MAX_BYTES = 4096
FIELDS = frozenset(("schema", "namespace", "kind", "project", "target"))


def _validate(signal):
    if not isinstance(signal, dict) or signal.keys() != FIELDS:
        raise ValueError("exact train wake-up envelope required")
    if any(not isinstance(value, str) for value in signal.values()):
        raise ValueError("train wake-up fields must be strings")
    if signal["schema"] != SCHEMA or signal["kind"] not in KINDS:
        raise ValueError("unknown train wake-up schema or kind")
    if not config.SCHEMA_RE.fullmatch(signal["namespace"]) \
            or not config.NAME_RE.fullmatch(signal["project"]):
        raise ValueError("explicit SQL namespace and project required")
    target = signal["target"]
    if plan_git.check_ref(target, "train wake-up target") != target \
            or merge_queue._remote_branch(target) != target:
        raise ValueError("canonical train wake-up target required")


def emit(db, *, kind, project, target):
    """No commit, target inference or proof lookup: caller owns the transaction.

    The SQL namespace prevents test/staging schemas sharing a database-wide
    NOTIFY channel from waking each other's trains. This is routing, not trust.
    """
    signal = dict(schema=SCHEMA, namespace=db.cfg.schema,
                  kind=kind, project=project, target=target)
    _validate(signal)
    payload = json.dumps(signal, ensure_ascii=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_BYTES:
        raise ValueError("train wake-up envelope too large")
    storage.of(db).wakeups.notify(CHANNEL, payload)


def receive(service, item):
    """Filter one untrusted notification without SQL, Git or CI calls.

    Malformed/unrelated hints are silent. A matching signal still has to pass
    Service.event's exact project/target filter and stop guard; the service
    reconstructs its runtime and proofs on its own fresh connection.
    """
    if not isinstance(item, dict) or item.get("channel") != CHANNEL:
        return False
    payload = item.get("payload")
    if not isinstance(payload, str) or len(payload) > MAX_BYTES:
        return False
    try:
        if len(payload.encode("utf-8")) > MAX_BYTES:
            return False
        signal = json.loads(payload)
        _validate(signal)
    except (ValueError, RecursionError, UnicodeError):
        return False
    if signal["namespace"] != service.cfg.schema:
        return False
    return service.event(kind=signal["kind"], project=signal["project"],
                         target=signal["target"])
