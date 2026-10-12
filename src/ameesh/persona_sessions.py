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
import sys
from datetime import datetime
from functools import wraps

from .db import bind, sql_literal


def record_start(db, persona: str, session_id: str, *, account: str | None = None,
                 best_effort: bool = False) -> None:
    # One statement keeps replacement and insertion atomic on both drivers.
    # Lock and read only the accepted registry session: an external hook may
    # supply an ID that L36/L46 deliberately refuses to install.
    _write(db, best_effort,
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


def record_end(db, persona: str, session_id: str | None, reason: str,
               *, best_effort: bool = False) -> None:
    if not session_id:
        return
    _write(db, best_effort,
        "UPDATE persona_sessions SET ended_at = now(), end_reason = %s "
        "WHERE persona = %s AND session_id = %s AND ended_at IS NULL",
        (reason, persona, session_id))


def _write(db, best_effort, sql, params):
    if not best_effort:
        db.execute(sql, params)
        return
    # A PL/pgSQL exception block is a server-side savepoint on both drivers.
    # psql exits on an SQL error, so client-side SAVEPOINT recovery cannot
    # preserve the primary registry write. Bind then quote the whole statement
    # as data for EXECUTE; session IDs never become PL/pgSQL source.
    # OTHERS excludes query_canceled: history timeouts must also stay inside
    # this subtransaction so the accepted registry transition can commit.
    body = (
        "BEGIN PERFORM set_config('ameesh.session_history_error', '', true); "
        "BEGIN EXECUTE " + sql_literal(bind(sql, params)) + "; "
        "EXCEPTION WHEN query_canceled OR OTHERS THEN "
        "PERFORM set_config('ameesh.session_history_error', SQLERRM, true); "
        "END; END")
    db.execute("DO " + sql_literal(body))
    rows = db.query("SELECT current_setting('ameesh.session_history_error') AS error")
    if rows and rows[0]["error"]:
        print("ameesh: session history could not be recorded: " + rows[0]["error"],
              file=sys.stderr)


def track_change(reason: str, *, returns_none: bool = False):
    """Keep accepted registry/session-history writes under the same row lock."""
    def decorate(method):
        @wraps(method)
        def tracked(store, name, *args, **kwargs):
            with store.db.transaction() as tx:
                before = tx.query(
                    "SELECT session_id FROM agent_registry WHERE name = %s FOR UPDATE",
                    (name,))
                result = method(type(store)(tx), name, *args, **kwargs)
                if not before or (not returns_none and not result):
                    return result
                after = tx.query("SELECT session_id FROM agent_registry WHERE name = %s",
                                 (name,))
                current = after[0]["session_id"] if after else None
                if current:
                    record_start(tx, name, current, best_effort=True)
                elif before[0]["session_id"]:
                    record_end(tx, name, before[0]["session_id"], reason, best_effort=True)
                return result
        return tracked
    return decorate


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
