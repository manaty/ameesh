# SPDX-License-Identifier: AGPL-3.0-only
"""Read a persona's current state and bounded activity timeline (L59).

The CLI requires a verified runner or external session binding to the requested
persona. Unbound callers and cross-persona reads are refused by default.
Session transcripts remain in the session backup component.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime, timezone

DEFAULT_SINCE = "24h"
DEFAULT_LIMIT = 200
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$")
_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\r\n]+)")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b[\w-]*(?:pass(?:word)?|secret|token|api[_-]?key|authorization|credential)"
    r"[\w-]*[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)")
_SECRET_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|"
    r"github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,})")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 -]*PRIVATE KEY-----.*?(?:"
                          r"-----END [A-Z0-9 -]*PRIVATE KEY-----|$)", re.S)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;}]+")
_DELEGATION_OUTCOMES = {
    "soldee": "settled", "rendue": "returned", "remplacee": "replaced", "annulee": "cancelled",
}


def parse_since(text: str) -> float:
    """Parse durations such as 30m, 24h or 7d into seconds."""
    match = _DURATION.fullmatch((text or "").strip().lower())
    if not match:
        raise ValueError("invalid duration: %r (examples: 30m, 24h, 7d)" % text)
    seconds = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("duration must be finite and positive")
    return seconds


def _timestamp(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value).strip().replace(" ", "T", 1)
                                      .replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def redact(text: str) -> str:
    text = _PRIVATE_KEY.sub("[redacted private key]", str(text))
    text = _AUTHORIZATION.sub(lambda match: match[1] + "[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: match[1] + "[redacted]", text)
    return _SECRET_TOKEN.sub("[redacted]", text)


def _excerpt(text, limit: int = 160) -> str:
    text = " ".join(redact(str(text or "")).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _format_time(timestamp: float | None) -> str:
    return "?" if timestamp is None else datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


def events(db, agent: str, since_s: float, now: float | None = None,
           *, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Return the latest N events in chronological order, using one cutoff."""
    now = time.time() if now is None else now
    if limit < 1:
        raise ValueError("limit must be positive")
    cutoff_ts = now - since_s
    cutoff = datetime.fromtimestamp(cutoff_ts, timezone.utc).isoformat()
    out: list[dict] = []
    # L95 (0043): voided readings are excluded from cost reports and this timeline.
    for row in db.query(
            "SELECT id, recorded_at, harness, model, session, usd, input_tokens, output_tokens "
            "FROM turn_costs WHERE agent = %s AND void_reason IS NULL "
            "AND recorded_at >= %s::timestamptz "
            "ORDER BY recorded_at DESC, id DESC LIMIT %s", (agent, cutoff, limit)):
        out.append({"at": _timestamp(row["recorded_at"]), "kind": "turn", "turn": row["id"],
                    "text": "turn %s%s — $%.4f (%s input tokens, %s output tokens)" % (
                        row.get("harness") or "?", (" / " + row["model"]) if row.get("model") else "",
                        float(row.get("usd") or 0), row.get("input_tokens") or 0,
                        row.get("output_tokens") or 0), "session": row.get("session")})
    for row in db.query(
            "SELECT id, sender, recipient, kind, body, work_item_id, created_at, delivered_at "
            "FROM agent_mailbox WHERE (sender = %s OR recipient = %s) "
            "AND created_at >= %s::timestamptz "
            "ORDER BY created_at DESC, id DESC LIMIT %s", (agent, agent, cutoff, limit)):
        sent = row["sender"] == agent
        out.append({"at": _timestamp(row["created_at"]),
                    "kind": "message_sent" if sent else "message_received",
                    "text": "%s %s%s: %s" % (
                        "→" if sent else "←", row["recipient"] if sent else row["sender"],
                        " (lot %s)" % row["work_item_id"] if row.get("work_item_id") else "",
                        _excerpt(row.get("body"))), "message": row["id"],
                    "delivered": _timestamp(row.get("delivered_at"))})
    for row in db.query(
            "SELECT e.id, e.work_item_id, e.state, e.note, e.actor, e.created_at, w.title "
            "FROM work_item_events e JOIN work_items w ON w.id = e.work_item_id "
            "WHERE (e.actor = %s OR w.assignee = %s) AND e.created_at >= %s::timestamptz "
            "ORDER BY e.created_at DESC, e.id DESC LIMIT %s", (agent, agent, cutoff, limit)):
        out.append({"at": _timestamp(row["created_at"]), "kind": "lot", "event": row["id"],
                    "text": 'lot %s "%s" → %s%s%s' % (
                        row["work_item_id"], _excerpt(row.get("title"), 60), row.get("state") or "?",
                        "" if row.get("actor") in (None, agent) else " (by %s)" % row["actor"],
                        (": " + _excerpt(row["note"], 120)) if row.get("note") else ""),
                    "lot": row["work_item_id"]})
    for row in db.query(
            "SELECT m.id, m.work_item_id, m.kind, m.at, m.sha, m.actor, m.verdict, w.title "
            "FROM work_item_milestones m JOIN work_items w ON w.id = m.work_item_id "
            "WHERE (m.actor = %s OR w.assignee = %s) AND m.at >= %s::timestamptz "
            "ORDER BY m.at DESC, m.id DESC LIMIT %s", (agent, agent, cutoff, limit)):
        out.append({"at": _timestamp(row["at"]), "kind": "milestone", "milestone": row["id"],
                    "text": 'lot %s "%s": %s%s%s' % (
                        row["work_item_id"], _excerpt(row.get("title"), 60), row.get("kind") or "?",
                        (" " + str(row["sha"])[:12]) if row.get("sha") else "",
                        (" — verdict " + row["verdict"]) if row.get("verdict") else ""),
                    "lot": row["work_item_id"]})
    # An old delegation resolved inside this window is still recent activity.
    for row in db.query(
            "SELECT id, work_item_id, delegate, delegated_by, delegated_at, due_at, outcome, "
            "resolved_at FROM work_item_delegations WHERE (delegate = %s OR delegated_by = %s) "
            "AND (delegated_at >= %s::timestamptz OR resolved_at >= %s::timestamptz) "
            "ORDER BY GREATEST(delegated_at, resolved_at) DESC, id DESC LIMIT %s",
            (agent, agent, cutoff, cutoff, limit)):
        delegated = _timestamp(row["delegated_at"])
        if delegated is not None and delegated >= cutoff_ts:
            out.append({"at": delegated, "kind": "delegation", "delegation": row["id"],
                        "text": "lot %s delegated by %s to %s%s" % (
                            row["work_item_id"], row["delegated_by"], row["delegate"],
                            " (due %s)" % _format_time(_timestamp(row["due_at"])) if row.get("due_at") else ""),
                        "lot": row["work_item_id"]})
        resolved = _timestamp(row.get("resolved_at"))
        if resolved is not None and resolved >= cutoff_ts:
            outcome = _DELEGATION_OUTCOMES.get(row.get("outcome"), row.get("outcome") or "resolved")
            out.append({"at": resolved, "kind": "delegation_resolved", "delegation": row["id"],
                        "text": "lot %s delegation %s (%s → %s)" % (
                            row["work_item_id"], outcome, row["delegated_by"], row["delegate"]),
                        "lot": row["work_item_id"]})
    out = [event for event in out if event["at"] is not None]
    out.sort(key=lambda event: (event["at"], event["kind"],
                               event.get("turn") or event.get("message") or event.get("event")
                               or event.get("milestone") or event.get("delegation") or 0))
    return out[-limit:]


def present(db, agent: str) -> dict | None:
    rows = db.query(
        "SELECT name, status, status_text, harness, host, session_id, responsible, team, mode, "
        "lease_owner, lease_expires_at, turns, last_turn_at, last_error, stop_reason "
        "FROM agent_registry WHERE name = %s", (agent,))
    return rows[0] if rows else None


def render(agent: str, current: dict | None, items: list[dict], since: str) -> str:
    lines = []
    if current:
        lines.append("%s — %s%s; harness %s on %s; responsible %s; turns %s" % (
            agent, current.get("status") or "?",
            (" (%s)" % _excerpt(current["status_text"], 80)) if current.get("status_text") else "",
            current.get("harness") or "?", current.get("host") or "?",
            current.get("responsible") or "—", current.get("turns") or 0))
        if current.get("lease_owner"):
            lines.append("  lease: %s" % current["lease_owner"])
        if current.get("last_error"):
            lines.append("  last error: %s" % _excerpt(current["last_error"], 160))
    lines.append("activity over %s: %d event(s)" % (since, len(items)))
    lines.extend("  %s  %-19s %s" % (_format_time(event["at"]), event["kind"], event["text"])
                 for event in items)
    return "\n".join(lines)


def main(argv) -> int:
    from . import db as db_mod, identity
    from .config import load as load_config
    parser = argparse.ArgumentParser(prog="ameesh activity", description="Persona activity log (L59).")
    parser.add_argument("agent")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="period: 30m, 24h, 7d (default: 24h)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="at most N recent events (default: 200)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        since_s = parse_since(args.since)
        if args.limit < 1:
            raise ValueError("limit must be positive")
        # Reject a duration outside the timestamp range before connecting.
        datetime.fromtimestamp(time.time() - since_s, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        print("ameesh activity: %s" % exc, file=sys.stderr)
        return 2
    cfg = load_config()
    db = db_mod.connect(cfg)
    try:
        binding = identity.resolve_binding(cfg, db)
        if (not binding.ok or binding.source not in ("runner", "session")
                or binding.name != args.agent):
            print("ameesh activity: a verified session bound to the requested persona is required",
                  file=sys.stderr)
            return 2
        row = present(db, args.agent)
        if row is None:
            print("ameesh activity: unknown persona %s" % args.agent, file=sys.stderr)
            return 2
        row = {key: redact(value) if isinstance(value, str) else value
               for key, value in row.items()}
        items = events(db, args.agent, since_s, limit=args.limit)
    finally:
        db.close()
    if args.json:
        print(json.dumps({"agent": args.agent, "since": args.since,
                          "present": {key: value.isoformat() if isinstance(value, datetime) else value
                                      for key, value in row.items()},
                          "events": items}, ensure_ascii=False, default=str))
    else:
        print(render(args.agent, row, items, args.since))
    return 0
