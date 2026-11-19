# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed pre-act check for a named lot and a fixed observation start.

The start must be captured before any scheduled wait, not when the check runs.
The receipt documents a point-in-time check; it is not an authorization or a
lock on subsequent mail and must be followed immediately by the guarded act.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import sys
import time
import uuid

from . import config, db as db_mod, identity, planned_waits, storage


class Refused(Exception):
    pass


def database_now(db):
    """La borne de lecture lue sur l'horloge de la base (#7775).

    Les `created_at` du journal et du courrier sont écrits par la base ; une
    borne prise sur l'horloge de l'hôte peut donc manquer un STOP (hôte en
    retard) ou en inventer un (hôte en avance). On lit `now()` dans la même
    horloge que les événements, avant l'attente, puis on ferme la connexion.
    """
    rows = db.query("SELECT extract(epoch from now())::float8 AS now_ts")
    try:
        value = float(rows[0]["now_ts"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise Refused("database clock unavailable") from exc
    if not math.isfinite(value):
        raise Refused("database clock unreadable")
    return value


def parse_since(value):
    try:
        date = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None or date.utcoffset() is None:
            raise ValueError("timezone required")
        return date.timestamp()
    except (TypeError, ValueError, OverflowError) as exc:
        raise Refused("--since must be an ISO timestamp with timezone") from exc


def inspect(tx, lot, actor, since, kind, target="", pregeste_id=None, *,
            request_id="", cfg=None, dry_run=False):
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
        stop_hold = planned_waits.is_stop_hold(note)
        if float(event["created_ts"]) >= since and (
                stop_hold or (not planned_waits.is_hold_note(note)
                              and planned_waits.contains_stop(note))):
            raise Refused("hold or STOP in lot event #%s" % event["id"])
    def preserve_stop(event, hold):
        who = (event.get("actor") or "").removeprefix("agent:")
        author = (hold.get("by") or "").removeprefix("agent:")
        return (who == actor.removeprefix("agent:") and author != who
                and planned_waits.is_stop_hold(hold.get("note") or ""))

    holds = planned_waits.active_holds(
        tx, hold_events, cfg=cfg, strict=True, preserve_on_transition=preserve_stop).get(lot, {})
    for hold in holds.values():
        if planned_waits.is_stop_hold(hold.get("note") or ""):
            raise Refused("active STOP hold by %s" % hold.get("by"))

    # Include both structured lot mail and prose references, and inspect STOPs
    # even when delivered: a session may have consumed mail during the wait.
    reference = r"(^|[^0-9])#%s([^0-9]|$)" % lot
    messages = tx.query(
        "SELECT id, sender, recipient, body, kind, "
        "payload->'decision_answer'->>'decision' AS answer_decision, "
        "payload->'decision_answer'->>'text' AS answer_text, "
        "delivered_at IS NULL AS unread, "
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
        body = message.get("body") or ""
        # Generated decision replies append the original question verbatim.
        # Inspect the answer and option label, but not that quoted question.
        if message.get("kind") == "reply" and message.get("answer_decision"):
            header = body.split("\n", 1)[0]
            # The title preceding the final answer marker is quoted context too.
            answer_header = header.rsplit(": option ", 1)[-1] if ": option " in header else ""
            body = answer_header + "\n" + (message.get("answer_text") or "")
        if float(message["ts"]) >= since and planned_waits.contains_stop(body):
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

    if dry_run:
        return None

    note = "pre-act: kind=%s actor=%s since=%s" % (kind, actor, since)
    pregeste_id = pregeste_id or str(uuid.uuid4())
    record = {"type": "pre-act/1", "kind": kind, "lot": lot,
              "actor": actor, "target": target, "pregeste_id": pregeste_id}
    if request_id:
        record["request_id"] = request_id
    evidence = json.dumps(record, sort_keys=True)
    tx.execute("INSERT INTO work_item_events "
               "(work_item_id, state, note, actor, evidence, created_at) "
               "VALUES (%s, %s, %s, %s, %s::jsonb, clock_timestamp())",
               (lot, item[0]["state"], note, actor, evidence))
    tx.execute("UPDATE work_items SET updated_at = now() WHERE id = %s", (lot,))
    return pregeste_id


#: Evidence written by the imposed credential path (#7767): the receipt above
#: proves the check, these two events prove the act it guarded. A start without
#: an end is an interrupted or unrecorded act, which the audit reports.
EFFECT_TYPE = "pre-act/2"
EFFECT_SCHEMA = "pre-act-effect/1"


def _state_of(tx, lot):
    rows = tx.query("SELECT state FROM work_items WHERE id = %s", (lot,))
    if not rows:
        raise Refused("lot not found")
    return rows[0]["state"]


def _record_effect(tx, lot, actor, evidence):
    state = _state_of(tx, lot)
    phase = evidence.get("phase", "?")
    note = "pre-act effect: %s kind=%s" % (phase, evidence.get("kind", "?"))
    tx.execute("INSERT INTO work_item_events "
               "(work_item_id, state, note, actor, evidence, created_at) "
               "VALUES (%s, %s, %s, %s, %s::jsonb, clock_timestamp())",
               (lot, state, note, actor, json.dumps(evidence, sort_keys=True)))


def _argv_digest(argv):
    return hashlib.sha256(json.dumps(list(argv), separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def record_effect_start(tx, lot, actor, pregeste_id, request_id, kind, target,
                        rule, argv, started):
    """Write the start of a guarded act, tied to its receipt.

    Only a digest and a short, bounded head of the command are kept: the
    argument list can name a file and is not needed to audit the act.
    """
    if not pregeste_id or not request_id:
        raise Refused("a receipt and a request id are required")
    evidence = {
        "type": EFFECT_TYPE,
        "schema": EFFECT_SCHEMA,
        "phase": "start",
        "pregeste_id": pregeste_id,
        "request_id": request_id,
        "kind": kind,
        "lot": lot,
        "actor": actor,
        "target": target,
        "rule": rule,
        "argv_sha256": _argv_digest(argv),
        "argv_head": [str(arg)[:120] for arg in list(argv)[:4]],
        "started": started,
    }
    _record_effect(tx, lot, actor, evidence)


def record_effect_end(tx, lot, actor, pregeste_id, request_id, kind, target,
                      exit_code, timed_out, started, finished,
                      stdout_sha256=None, stderr_sha256=None):
    """Write the end of a guarded act; the start must already be recorded."""
    if not pregeste_id or not request_id:
        raise Refused("a receipt and a request id are required")
    evidence = {
        "type": EFFECT_TYPE,
        "schema": EFFECT_SCHEMA,
        "phase": "end",
        "pregeste_id": pregeste_id,
        "request_id": request_id,
        "kind": kind,
        "lot": lot,
        "actor": actor,
        "target": target,
        "exit": exit_code,
        "timed_out": bool(timed_out),
        "started": started,
        "finished": finished,
        "duration_ms": int(round((float(finished) - float(started)) * 1000)),
    }
    if stdout_sha256 is not None:
        evidence["stdout_sha256"] = stdout_sha256
    if stderr_sha256 is not None:
        evidence["stderr_sha256"] = stderr_sha256
    _record_effect(tx, lot, actor, evidence)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ameesh gate pre-act")
    parser.add_argument("--lot", type=int, required=True)
    parser.add_argument("--kind", choices=("kms", "key", "prod"), required=True)
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--since", help="ISO timestamp with timezone captured before waiting")
    window.add_argument("--wait-until", help="wait until ISO timestamp, then check and execute")
    parser.add_argument("--target", default="", help="target of the effect")
    parser.add_argument("--dry-run", action="store_true", help="check without recording a receipt or executing a command")
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
        if args.dry_run and command:
            raise Refused("--dry-run cannot execute a command")
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
        else:
            since = parse_since(args.since)
        cfg = config.load()
        if args.wait_until:
            # The reading bound must share the clock of the created_at it is
            # compared to. Read it in the database, before waiting, then close
            # the connection: the check reopens a fresh one (#7775).
            db = db_mod.open_db(cfg)
            if storage.is_remote(db):
                raise Refused("transactional database connection required")
            since = database_now(db)
            db.close()
            db = None
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(remaining)
        db = db_mod.open_db(cfg)
        if storage.is_remote(db):
            raise Refused("transactional database connection required")
        binding = identity.resolve_binding(cfg, db)
        if not binding.ok or not binding.name:
            raise Refused("valid agent identity required")
        check_started = time.monotonic()
        with db.transaction(isolation="READ COMMITTED") as tx:
            pregeste_id = inspect(tx, args.lot, binding.name, since, args.kind, args.target.strip(), cfg=cfg, dry_run=args.dry_run)
        db.close()
        db = None
        if args.dry_run:
            print("pre-act: check passed for lot #%d (%s); no receipt recorded" % (args.lot, args.kind))
        else:
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
