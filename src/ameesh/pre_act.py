# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed pre-act check for a named lot and a fixed observation start.

The start must be captured before any scheduled wait, not when the check runs.
The receipt documents a point-in-time check; it is not an authorization or a
lock on subsequent mail and must be followed immediately by the guarded act.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
import uuid

from . import config, db as db_mod, identity, planned_waits, storage

_STOP = re.compile(r"\b(?:stop|arr[êe]t|suspend(?:re|u|ue|us|ues|ez|ons)?|"
                   r"annul(?:er|e|é|ée|ez)|halt)\b", re.I)


class Refused(Exception):
    pass


def parse_since(value):
    try:
        date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None or date.utcoffset() is None:
            raise ValueError("timezone required")
        return date.timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise Refused("--since must be an ISO timestamp with timezone") from exc


def inspect(tx, lot, actor, since, kind, target="", pregeste_id=None, *, cfg=None):
    """Read the journal and mailbox and record a receipt in one transaction."""
    item = tx.query("SELECT state, extract(epoch from now())::float8 AS now_ts "
                    "FROM work_items WHERE id = %s FOR NO KEY UPDATE", (lot,))
    if not item:
        raise Refused("lot not found")
    if since > float(item[0]["now_ts"]):
        raise Refused("--since is in the future")
    if item[0]["state"] in ("blocked", "waiting_human"):
        raise Refused("lot is %s" % item[0]["state"])
    events = tx.query(
        "SELECT id, work_item_id, state, note, actor, "
        "extract(epoch from created_at)::float8 AS created_ts "
        "FROM work_item_events WHERE work_item_id = %s ORDER BY id", (lot,))
    # Keep every state transition, even one written by the actor. Ignore that
    # actor's own hold, lift and STOP text, but retain the event's state.
    hold_events = []
    for event in events:
        note = event.get("note") or ""
        own = (event.get("actor") or "").removeprefix("agent:") == actor.removeprefix("agent:")
        hold_events.append(dict(event, note="" if own else note))
        if own:
            continue
        stop_hold = planned_waits.is_hold_note(note) and bool(_STOP.search(note))
        if float(event["created_ts"]) >= since and (stop_hold or (not planned_waits.is_hold_note(note)
                                                                  and _STOP.search(note))):
            raise Refused("hold or STOP in lot event #%s" % event["id"])
    def preserve_stop(event, hold):
        who = (event.get("actor") or "").removeprefix("agent:")
        author = (hold.get("by") or "").removeprefix("agent:")
        return (who == actor.removeprefix("agent:") and author != who
                and bool(_STOP.search(hold.get("note") or "")))

    holds = planned_waits.active_holds(
        tx, hold_events, cfg=cfg, strict=True, preserve_on_transition=preserve_stop).get(lot, {})
    for hold in holds.values():
        if _STOP.search(hold.get("note") or ""):
            raise Refused("active STOP hold by %s" % hold.get("by"))

    # Include both structured lot mail and prose references, and inspect STOPs
    # even when delivered: a session may have consumed mail during the wait.
    reference = r"(^|[^0-9])#%s([^0-9]|$)" % lot
    messages = tx.query(
        "SELECT id, sender, recipient, body, delivered_at IS NULL AS unread, "
        "extract(epoch from created_at)::float8 AS ts, "
        "payload->'decision'->>'state' AS decision_state, "
        "payload->'decision'->'withdrawn'->>'ts' AS withdrawn_ts "
        "FROM agent_mailbox WHERE work_item_id = %s OR body ~ %s ORDER BY id",
        (str(lot), reference))
    for message in messages:
        if (message.get("sender") or "").removeprefix("agent:") == actor.removeprefix("agent:"):
            continue
        if message.get("recipient") == actor and message.get("unread"):
            raise Refused("unread lot mail #%s" % message["id"])
        if float(message["ts"]) >= since and _STOP.search(message.get("body") or ""):
            raise Refused("STOP in lot mail #%s" % message["id"])
        if message.get("decision_state") == "withdrawn":
            try:
                withdrawn = float(message["withdrawn_ts"])
                if not math.isfinite(withdrawn):
                    raise ValueError("non-finite timestamp")
            except (TypeError, ValueError):
                raise Refused("withdrawn decision #%s has no readable timestamp" % message["id"])
            if withdrawn >= since:
                raise Refused("withdrawn decision #%s" % message["id"])

    note = "pre-act: kind=%s actor=%s since=%s" % (kind, actor, since)
    pregeste_id = pregeste_id or str(uuid.uuid4())
    evidence = json.dumps({"type": "pre-act/1", "kind": kind, "lot": lot,
                           "actor": actor, "target": target,
                           "pregeste_id": pregeste_id})
    tx.execute("INSERT INTO work_item_events "
               "(work_item_id, state, note, actor, evidence, created_at) "
               "VALUES (%s, %s, %s, %s, %s::jsonb, clock_timestamp())",
               (lot, item[0]["state"], note, actor, evidence))
    tx.execute("UPDATE work_items SET updated_at = now() WHERE id = %s", (lot,))
    return pregeste_id


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ameesh gate pre-act")
    parser.add_argument("--lot", type=int, required=True)
    parser.add_argument("--kind", choices=("kms", "key", "prod"), required=True)
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--since", help="ISO timestamp with timezone captured before waiting")
    window.add_argument("--wait-until", help="wait until ISO timestamp, then check and execute")
    parser.add_argument("--target", default="", help="target of the effect")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="optional command after --, executed immediately after the check")
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if args.command and not command:
        parser.error("a command is required after --")
    db = None
    try:
        if args.lot <= 0:
            raise Refused("invalid lot")
        if args.kind == "prod" and not args.target.strip():
            raise Refused("--target is required for prod")
        dispatch = args.kind == "prod" and command[:3] == ["gh", "workflow", "run"]
        if dispatch and any(arg == "pregeste" or arg.startswith("pregeste=")
                            for arg in command[3:]):
            raise Refused("dispatch must not supply a pregeste input")
        if args.wait_until:
            if not command:
                raise Refused("--wait-until requires a command after --")
            deadline = parse_since(args.wait_until)
            since = time.time()
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(remaining)
        else:
            since = parse_since(args.since)
        cfg = config.load()
        db = db_mod.open_db(cfg)
        if storage.is_remote(db):
            raise Refused("transactional database connection required")
        binding = identity.resolve_binding(cfg, db)
        if not binding.ok or not binding.name:
            raise Refused("valid agent identity required")
        check_started = time.monotonic()
        with db.transaction(isolation="READ COMMITTED") as tx:
            pregeste_id = inspect(tx, args.lot, binding.name, since, args.kind, args.target.strip(), cfg=cfg)
        db.close()
        db = None
        print("pre-act: receipt recorded for lot #%d (%s), pregeste=%s" %
              (args.lot, args.kind, pregeste_id))
        if command:
            sys.stdout.flush()
            if time.monotonic() - check_started > 30:
                raise Refused("pre-act check took more than 30 seconds")
            if dispatch:
                command += ["-f", "pregeste=" + pregeste_id]
            os.execvp(command[0], command)
        return 0
    except Exception as exc:  # fail closed on unavailable or unreadable data
        print("pre-act refused: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if db is not None:
            db.close()
