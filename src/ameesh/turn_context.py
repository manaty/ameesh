# SPDX-License-Identifier: AGPL-3.0-only
"""The turn-context document handed to a harness (#2167, lot #2894).

The runner allocates one turn id before the first side effect of a turn and
uses it unchanged for the mail reservation, the accounting marker, the account
reservation and every fact written about the turn.  Before the harness can
act, it writes ``<state>/<agent>/turn-context.json`` and points the turn's
environment at it with ``AMEESH_TURN_CONTEXT``.  The document is the harness's
statement of its own turn: identity, live lease, the requests this turn showed
it, the agent, and the accounting state opened for it.

Contract ``ameesh-turn-context/1`` -- only additive optional fields within the
version.  Timestamps are epoch seconds as floats.  The document carries no
secret in what the runner contributes: the builder lists the fields it
accepts, and the writer refuses an exact value of a known secret environment
variable there.  `requests` is the mail the turn showed the harness (already
the agent's mail), not a runner-supplied field.

The file is written to an exclusive temporary file in the agent's state
directory, flushed, then atomically renamed over the target (mode ``0600`` in
a ``0700`` directory; a symlink at the target is replaced, never followed).
It is removed at the end of every turn, including a turn whose harness could
not be launched.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from decimal import Decimal
from typing import Any, Iterable

SCHEMA = "ameesh-turn-context/1"
#: file name, under the agent's state directory
FILENAME = "turn-context.json"
#: environment variable carrying the absolute path of the document
ENV = "AMEESH_TURN_CONTEXT"
FILE_MODE = 0o600
DIR_MODE = 0o700

#: environment names whose value is a secret by construction
SECRET_NAMES = frozenset((
    "AMEESH_DSN",
    "AGENT_MESH_DSN",
    "AMEESH_DATABASE_URL",
    "AGENT_MESH_DATABASE_URL",
    "AMEESH_EXEC_TOKEN",
    "AMEESH_RELAY_TOKEN",
    "AMEESH_RELAY_SECRET",
))
#: suffixes of variables whose value is a secret (provider keys, tokens...)
SECRET_SUFFIXES = (
    "_API_KEY",
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_PASSWD",
    "_PRIVATE_KEY",
    "_CREDENTIALS",
    "_DSN",
)
#: ignore shorter values: a one-letter variable is not a usable secret and
#: would make the exact-value comparison meaningless
MIN_SECRET_LEN = 8


class ContextError(ValueError):
    """The document does not satisfy the turn-context contract."""


def path(state_dir: str) -> str:
    """The absolute document path for an agent's state directory."""
    return os.path.join(os.path.abspath(state_dir), FILENAME)


def environment_secrets(environ: dict | None = None) -> set[str]:
    """Exact values of the known secret environment variables.

    The writer refuses to put one of them in the runner-supplied fields.  This
    is the last line of defence: the builder already accepts only named,
    non-secret fields; a request body is the agent's mail and is not scanned
    (it is already in the harness prompt and, like the mailbox, in a `0600`
    file of the agent's state directory).
    """
    env = os.environ if environ is None else environ
    values: set[str] = set()
    for name, value in env.items():
        if not value or len(value) < MIN_SECRET_LEN:
            continue
        upper = name.upper()
        if upper in SECRET_NAMES or upper.endswith(SECRET_SUFFIXES):
            values.add(value)
    return values


def _text(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None:
        if nullable:
            return None
        raise ContextError("%s is required" % name)
    if not isinstance(value, str):
        raise ContextError("%s must be a string" % name)
    if not value and not nullable:
        raise ContextError("%s is required" % name)
    return value


def _number(value: Any, name: str, *, nullable: bool = False) -> float | None:
    if value is None:
        if nullable:
            return None
        raise ContextError("%s is required" % name)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ContextError("%s must be a number" % name)
    return float(value)


def _object(value: Any, name: str, *, nullable: bool = False) -> dict | None:
    if value is None:
        if nullable:
            return None
        raise ContextError("%s is required" % name)
    if not isinstance(value, dict):
        raise ContextError("%s must be an object" % name)
    return value


def _lot(value: Any) -> str | None:
    """A lot reference as text (nullable), like `agent_mailbox.work_item_id`."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _request(row: dict) -> dict:
    payload = row.get("payload")
    if payload is None:
        payload = {}
    return {
        "id": int(row["id"]),
        "sender": str(row.get("sender") or ""),
        "kind": str(row.get("kind") or "notify"),
        "created_ts": _number(row.get("created_ts"), "requests[].created_ts"),
        "delivered_ts": _number(row.get("delivered_ts"), "requests[].delivered_ts",
                                nullable=True),
        "work_item_id": _lot(row.get("work_item_id")),
        "redelivered": bool(row.get("deja_consigne")),
        "body": str(row.get("body") or ""),
        "payload": payload if isinstance(payload, dict) else {},
    }


def _marker(marker: dict | None) -> dict | None:
    if marker is None:
        return None
    return {
        "start_index": int(marker["start"]),
        "created_ts": _number(marker.get("created_ts"), "accounting.marker.created_ts"),
        "model": marker.get("model") or None,
        "work_item_id": _lot(marker.get("work_item_id")),
        "turn_id": marker.get("turn_id") or None,
    }


def _reservation(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "billing_account": str(row["billing_account"]),
        "usd": float(row["usd"]),
        "period": str(row["period"]) if row.get("period") is not None else None,
        "state": str(row["state"]) if row.get("state") is not None else None,
    }


def build(*, turn_id: str, agent: str, host: str, kind: str, started_ts: float,
          session: str | None, work_item_id: Any, lease_owner: str,
          lease_epoch: int, lease_expires_ts: float | None,
          requests: Iterable[dict], harness: str, model: str | None,
          budget_usd: float | None, spent_usd: float | None,
          spend_key: str | None, marker: dict | None,
          reservation: dict | None, generated_ts: float | None = None) -> dict:
    """Build and validate the ``ameesh-turn-context/1`` document."""
    document = {
        "schema": SCHEMA,
        "generated_ts": time.time() if generated_ts is None else float(generated_ts),
        "turn": {
            "id": str(turn_id),
            "agent": str(agent),
            "host": str(host),
            "kind": str(kind),
            "started_ts": _number(started_ts, "turn.started_ts"),
            "session": session or None,
            "work_item_id": _lot(work_item_id),
        },
        "lease": {
            "owner": str(lease_owner),
            "epoch": int(lease_epoch),
            "expires_ts": _number(lease_expires_ts, "lease.expires_ts",
                                  nullable=True),
        },
        "requests": [_request(row) for row in requests],
        "agent": {
            "name": str(agent),
            "harness": str(harness),
            "model": model or None,
            "budget_usd": _number(budget_usd, "agent.budget_usd", nullable=True),
            "spent_usd": _number(spent_usd, "agent.spent_usd", nullable=True),
        },
        "accounting": {
            "spend_key": spend_key or None,
            "marker": _marker(marker),
            "reservation": _reservation(reservation),
        },
    }
    validate(document)
    return document


def validate(document: Any) -> None:
    """Refuse anything that is not a complete ``ameesh-turn-context/1``.

    Unknown extra keys are tolerated (the version admits additive optional
    fields); missing or mistyped contract fields are not.
    """
    document = _object(document, "document")
    if document.get("schema") != SCHEMA:
        raise ContextError("unknown or missing schema (expected %s)" % SCHEMA)
    _number(document.get("generated_ts"), "generated_ts")

    turn = _object(document.get("turn"), "turn")
    _text(turn.get("id"), "turn.id")
    _text(turn.get("agent"), "turn.agent")
    _text(turn.get("host"), "turn.host")
    _text(turn.get("kind"), "turn.kind")
    _number(turn.get("started_ts"), "turn.started_ts")
    _text(turn.get("session"), "turn.session", nullable=True)
    _lot(turn.get("work_item_id"))

    lease = _object(document.get("lease"), "lease")
    _text(lease.get("owner"), "lease.owner")
    epoch = lease.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        raise ContextError("lease.epoch must be an integer")
    _number(lease.get("expires_ts"), "lease.expires_ts", nullable=True)

    requests = document.get("requests")
    if not isinstance(requests, list):
        raise ContextError("requests must be a list")
    for index, row in enumerate(requests):
        _request_validate(row, index)

    agent = _object(document.get("agent"), "agent")
    _text(agent.get("name"), "agent.name")
    _text(agent.get("harness"), "agent.harness")
    _text(agent.get("model"), "agent.model", nullable=True)
    _number(agent.get("budget_usd"), "agent.budget_usd", nullable=True)
    _number(agent.get("spent_usd"), "agent.spent_usd")

    accounting = _object(document.get("accounting"), "accounting")
    _text(accounting.get("spend_key"), "accounting.spend_key", nullable=True)
    marker = _object(accounting.get("marker"), "accounting.marker", nullable=True)
    if marker is not None:
        start = marker.get("start_index")
        if isinstance(start, bool) or not isinstance(start, int):
            raise ContextError("accounting.marker.start_index must be an integer")
        _number(marker.get("created_ts"), "accounting.marker.created_ts")
        _text(marker.get("model"), "accounting.marker.model", nullable=True)
        _lot(marker.get("work_item_id"))
        _text(marker.get("turn_id"), "accounting.marker.turn_id")
        if marker.get("turn_id") != turn.get("id"):
            raise ContextError("accounting.marker.turn_id must be the turn id")
    reservation = _object(accounting.get("reservation"), "accounting.reservation",
                          nullable=True)
    if reservation is not None:
        _text(reservation.get("billing_account"), "accounting.reservation.billing_account")
        _number(reservation.get("usd"), "accounting.reservation.usd")
        _text(reservation.get("period"), "accounting.reservation.period")
        _text(reservation.get("state"), "accounting.reservation.state")


def _request_validate(row: Any, index: int) -> None:
    row = _object(row, "requests[%d]" % index)
    ident = row.get("id")
    if isinstance(ident, bool) or not isinstance(ident, int):
        raise ContextError("requests[%d].id must be an integer" % index)
    _text(row.get("sender"), "requests[%d].sender" % index)
    _text(row.get("kind"), "requests[%d].kind" % index)
    _number(row.get("created_ts"), "requests[%d].created_ts" % index)
    _number(row.get("delivered_ts"), "requests[%d].delivered_ts" % index,
            nullable=True)
    _lot(row.get("work_item_id"))
    if not isinstance(row.get("redelivered"), bool):
        raise ContextError("requests[%d].redelivered must be a boolean" % index)
    if not isinstance(row.get("body"), str):
        raise ContextError("requests[%d].body must be a string" % index)
    _object(row.get("payload"), "requests[%d].payload" % index)


def _refuse_secrets(serialized: str, secrets: Iterable[str] | None) -> None:
    for value in secrets or ():
        if not value:
            continue
        if value in serialized:
            # Never echo the value: the error is written to the runner log.
            raise ContextError("the turn context would contain a secret value; "
                               "refusing to write it")
        # JSON escapes quotes, backslashes and control characters: compare the
        # escaped form too, so a credential containing one is still caught.
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        if escaped != value and escaped in serialized:
            raise ContextError("the turn context would contain a secret value; "
                               "refusing to write it")


def write(state_dir: str, document: dict, *, secrets: Iterable[str] | None = None) -> str:
    """Write the document atomically and return its absolute path.

    The secret guard covers what the **runner** contributes (turn, lease,
    agent, accounting).  `requests` is the mail this turn showed the harness:
    it is already the agent's mail, and a body quoting a credential must not
    block the turn that delivers it (the file stays `0600` in the agent's
    state directory, like the mailbox)."""
    validate(document)
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")) + "\n"
    structural = {key: value for key, value in document.items() if key != "requests"}
    _refuse_secrets(json.dumps(structural, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")), secrets)
    os.makedirs(state_dir, mode=DIR_MODE, exist_ok=True)
    target = path(state_dir)
    temporary = os.path.join(
        os.path.dirname(target),
        ".%s.%d.%s.tmp" % (FILENAME, os.getpid(), uuid.uuid4().hex))
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    handle = os.fdopen(os.open(temporary, flags, FILE_MODE), "w", encoding="utf-8")
    try:
        with handle:
            os.fchmod(handle.fileno(), FILE_MODE)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def remove(state_dir: str) -> None:
    """Remove the document; absent is a success. A symlink is unlinked as a
    link (its target is never touched)."""
    try:
        os.unlink(path(state_dir))
    except FileNotFoundError:
        return


def current(*, expect_turn: str | None = None, environ=None) -> dict:
    """Read the calling turn, before any database access.

    A resident turn pointer never falls back to the process's lifetime id.
    Callers reusing an authorization must supply its captured turn id.
    """
    env = os.environ if environ is None else environ
    try:
        filename = env.get(ENV)
        if not filename or not os.path.isabs(filename):
            raise ContextError("not-in-turn")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(filename, flags), encoding="utf-8") as handle:
            document = json.load(handle)
        validate(document)
        turn_id = document["turn"]["id"]
        if expect_turn is not None and turn_id != expect_turn:
            raise ContextError("turn-mismatch")
        pointer = env.get("AMEESH_TURN_ID_FILE")
        if pointer:
            with open(pointer, encoding="utf-8") as handle:
                active = handle.read().strip()
        else:
            active = env.get("AMEESH_TURN_ID", "")
        if not active or active != turn_id:
            raise ContextError("stale-turn")
        owner = env.get("AMEESH_RUNNER_ID") or env.get("AGENT_MESH_RUNNER_ID")
        epoch = env.get("AMEESH_LEASE_EPOCH") or env.get("AGENT_MESH_LEASE_EPOCH")
        lease = document["lease"]
        if (env.get("AGENT_MAIL_NAME") != document["turn"]["agent"]
                or owner != lease["owner"] or epoch is None
                or int(epoch) != lease["epoch"]):
            raise ContextError("lease-mismatch")
        return document
    except ContextError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError):
        # Never echo file contents, paths, credentials or parser diagnostics.
        raise ContextError("unreadable-context") from None


def verify(document: dict, db) -> None:
    """Recheck the live lease and pending turn using the mailbox fence."""
    from . import registry
    name = document["turn"]["agent"]
    lease = document["lease"]
    ok, _ = registry.lease_matches(db, name, lease["owner"], lease["epoch"])
    if not ok:
        raise ContextError("lease-mismatch")
    marker = registry.pending_spend_get(db, name)
    if not marker or marker.get("turn_id") != document["turn"]["id"]:
        raise ContextError("stale-turn")

def cmd_current(argv: list[str]) -> int:
    """Reference reader for the supported per-turn authorization contract."""
    import argparse
    import sys
    from . import turn_context
    parser = argparse.ArgumentParser(prog="ameesh turn current")
    parser.add_argument("--local", action="store_true",
                        help="skip the database recheck (file and identity checks still apply)")
    parser.add_argument("--expect-turn", help="captured turn id required when reusing authorization")
    parser.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        document = turn_context.current(expect_turn=args.expect_turn)
        if not args.local:
            from . import config as config_mod, backend as backend_mod
            try:
                cfg = config_mod.load()
            except (ValueError, OSError):
                print("turn current: invalid-configuration", file=sys.stderr)
                return 2
            import dataclasses
            bk, _ = backend_mod.open_backend(dataclasses.replace(cfg, read_only=True))
            if bk.kind != "pg":
                raise turn_context.ContextError("database-unavailable")
            from . import sandbox_observation
            try:
                turn_context.verify(document, bk.db)
                sandbox = sandbox_observation.get(bk.db, document['turn']['id'])
                if sandbox is not None:
                    document['sandbox'] = sandbox
            finally:
                bk.db.close()
    except turn_context.ContextError as exc:
        print("turn current: %s" % exc, file=sys.stderr)
        return 3
    except Exception:
        print("turn current: verification-unavailable", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(document, ensure_ascii=False))
    else:
        print("%s %s" % (document["turn"]["agent"], document["turn"]["id"]))
    return 0
