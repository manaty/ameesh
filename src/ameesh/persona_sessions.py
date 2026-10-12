# SPDX-License-Identifier: AGPL-3.0-only
"""Persona session history (L52, decision 0032 §2).

The registry keeps the current session; this module tracks current and past
sessions in `persona_sessions` (migration 0066). V1 has one open session per
persona: a new one closes its predecessor. Resuming reopens the same row.
No transcript content is stored here. Callers log history failures without
interrupting the agent.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime


def record_start(db, persona: str, session_id: str, *, account: str | None = None) -> None:
    # One statement keeps replacement and insertion atomic on both drivers.
    # Lock and read only the accepted registry session: an external hook may
    # supply an ID that L36/L46 deliberately refuses to install.
    db.execute(
        "WITH current_session AS ("
        "SELECT name AS persona, session_id, harness, host, session_account, session_work_item "
        "FROM agent_registry WHERE name = %s AND session_id = %s FOR UPDATE"
        "), closed_sessions AS ("
        "UPDATE persona_sessions AS history SET ended_at = now(), "
        "end_reason = 'replaced by ' || current_session.session_id "
        "FROM current_session WHERE history.persona = current_session.persona "
        "AND history.session_id <> current_session.session_id AND history.ended_at IS NULL "
        "RETURNING history.id"
        ") INSERT INTO persona_sessions (persona, session_id, harness, host, account, work_item) "
        "SELECT persona, session_id, harness, host, coalesce(%s, session_account), session_work_item "
        "FROM current_session WHERE true "
        "ON CONFLICT (persona, session_id) DO UPDATE SET ended_at = NULL, end_reason = NULL, "
        "account = coalesce(EXCLUDED.account, persona_sessions.account), "
        "host = coalesce(EXCLUDED.host, persona_sessions.host)",
        (persona, session_id, account))


def record_end(db, persona: str, session_id: str | None, reason: str) -> None:
    if not session_id:
        return
    db.execute(
        "UPDATE persona_sessions SET ended_at = now(), end_reason = %s "
        "WHERE persona = %s AND session_id = %s AND ended_at IS NULL",
        (reason, persona, session_id))


def sessions(db, persona: str, limit: int = 50) -> list[dict]:
    return db.query(
        "SELECT session_id, harness, host, account, work_item, started_at, ended_at, end_reason "
        "FROM persona_sessions WHERE persona = %s ORDER BY started_at DESC, id DESC LIMIT %s",
        (persona, int(limit)))


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%m-%d %H:%M")
    return str(value)[:16].replace("T", " ")


def main(argv) -> int:
    """`ameesh sessions <persona> [--limit N] [--json]`."""
    from . import db as db_mod
    from .config import load as load_config
    p = argparse.ArgumentParser(prog="ameesh sessions",
                                description="Current and past sessions of a persona (L52).")
    p.add_argument("persona")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config()
    db = db_mod.connect(cfg)
    try:
        rows = sessions(db, args.persona, args.limit)
    finally:
        db.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, default=str))
        return 0
    if not rows:
        print("no known sessions for %s" % args.persona)
        return 0
    for r in rows:
        print("%s  %-11s → %-11s %-9s %-14s %-10s %s%s" % (
            r["session_id"], _fmt(r["started_at"]),
            "active" if r["ended_at"] is None else _fmt(r["ended_at"]),
            r.get("harness") or "?", r.get("host") or "?", r.get("account") or "—",
            ("lot %s" % r["work_item"]) if r.get("work_item") else "",
            (" (%s)" % r["end_reason"]) if r.get("end_reason") else ""))
    return 0
