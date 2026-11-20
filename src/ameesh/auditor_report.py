# SPDX-License-Identifier: AGPL-3.0-only
"""#7894: suppress unchanged short-pass reports, independently of the tick.

The auditor supplies the stable observations and judges whether an action is
due. Code compares them with the last *sent* report, never the last passage.
No report body or observation text is kept in the journal, only its digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from . import config, db as db_mod, events, identity, mail, registry, storage

EVENT = "auditor.report"
SKIPPED = "auditor.report_skipped"


def fingerprint(state: dict) -> str:
    """Validate the explicit observation contract; ignore order, not changes."""
    if not isinstance(state, dict) or set(state) != {
            "debit_check", "waits", "alerts", "idle_over_threshold"}:
        raise ValueError("state requires debit_check, waits, alerts, idle_over_threshold")
    if type(state["debit_check"]) is not int or state["debit_check"] not in (0, 2):
        raise ValueError("debit_check must be 0 or 2; unreadable debit cannot be quiet")
    if type(state["idle_over_threshold"]) is not bool:
        raise ValueError("idle_over_threshold must be boolean")
    for key in ("waits", "alerts"):
        if not isinstance(state[key], list):
            raise ValueError("%s must be a list" % key)
    for wait in state["waits"]:
        if (not isinstance(wait, dict) or set(wait) != {"id", "pilot", "gesture"}
                or any(not isinstance(v, str) or not v.strip() for v in wait.values())):
            raise ValueError("each wait requires nonempty string id, pilot, gesture")
    if any(not isinstance(v, str) or not v.strip() for v in state["alerts"]):
        raise ValueError("alert ids must be nonempty strings")
    normalized = dict(state)
    normalized["waits"] = sorted(
        {json.dumps(w, sort_keys=True, ensure_ascii=False) for w in state["waits"]})
    normalized["alerts"] = sorted(set(state["alerts"]))
    return hashlib.sha256(json.dumps(normalized, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def publish(db, *, agent: str, recipient: str, state: dict, body: str,
            status: str, activity: bool = False, needs_action: bool = False,
            summary: str | None = None) -> dict:
    digest = fingerprint(state)
    if not body.strip() or not status.strip():
        raise ValueError("body and status must be nonempty")
    if summary not in (None, "daily", "weekly"):
        raise ValueError("summary must be daily or weekly")
    key = "%s:%s" % (agent, recipient)
    previous = storage.of(db).events.recent_type(EVENT, None, 1, object=key)
    data = previous[0].get("data", {}) if previous else {}
    if isinstance(data, str):
        data = json.loads(data)
    reason = ("summary" if summary else "action" if needs_action else
              "activity" if activity else "debit" if state["debit_check"] else
              "changed" if data.get("fingerprint") != digest else "quiet")
    result = {"fingerprint": digest, "reason": reason, "summary": summary,
              "decision": "skipped" if reason == "quiet" else "sent"}
    if reason != "quiet":
        message = mail.send(db, agent, recipient, body, kind="event",
                            payload={"info": not needs_action})
        if message is None:
            raise RuntimeError("report mail was not deposited")
        result["mail_id"] = message
    events.record(db, SKIPPED if reason == "quiet" else EVENT, key, **result)
    registry.set_status_text(db, agent, status)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh auditor report")
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--body-file", type=Path, required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--activity", action="store_true",
                        help="a level-1 gesture or proposal was made this passage")
    parser.add_argument("--needs-action", action="store_true",
                        help="recipient must act; wake them instead of sending info")
    parser.add_argument("--summary", choices=("daily", "weekly"))
    args = parser.parse_args(argv)
    try:
        state = json.loads(args.state_file.read_text())
        fingerprint(state)
        body = args.body_file.read_text()
        cfg = config.load()
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db, defer=True)
            agent = identity.require_bound(cfg, db, what="auditor report").name
            result = publish(db, agent=agent, recipient=args.recipient, state=state,
                             body=body, status=args.status, activity=args.activity,
                             needs_action=args.needs_action, summary=args.summary)
            print(json.dumps(result, sort_keys=True))
        finally:
            db.close()
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, "auditor report: %s\n" % exc)
    return 0
