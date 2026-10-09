# SPDX-License-Identifier: AGPL-3.0-only
"""Historique des sessions d'une persona (lot L52, décision 0032 §2).

Une persona est définie aussi par ses sessions, présentes et passées. Le
registre ne garde que la session courante ; ce module tient la trace de toutes
(`persona_sessions`, migration 0037) :

* `record_start` : une session devient la session courante de la persona
  (nouvelle, ou reprise) ; en v1, une seule session ouverte par persona, les
  autres sont donc closes (« remplacée par … ») ;
* `record_end` : la session prend fin (rotation, relais, arrêt) ;
* `sessions` : la liste, la plus récente d'abord.

Jamais bloquant : une erreur ici est journalisée par l'appelant, jamais fatale
pour l'agent.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime


def record_start(db, persona: str, session_id: str, *, account: str | None = None) -> None:
    rows = db.query("SELECT harness, host, session_work_item FROM agent_registry WHERE name = %s",
                    (persona,))
    row = rows[0] if rows else {}
    db.execute(
        "UPDATE persona_sessions SET ended_at = now(), end_reason = %s "
        "WHERE persona = %s AND session_id <> %s AND ended_at IS NULL",
        ("remplacée par %s" % session_id, persona, session_id))
    db.execute(
        "INSERT INTO persona_sessions (persona, session_id, harness, host, account, work_item) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (persona, session_id) DO UPDATE SET ended_at = NULL, end_reason = NULL, "
        "account = coalesce(EXCLUDED.account, persona_sessions.account), "
        "host = coalesce(EXCLUDED.host, persona_sessions.host)",
        (persona, session_id, row.get("harness"), row.get("host"), account,
         row.get("session_work_item")))


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
                                description="Sessions d'une persona, présentes et passées (L52).")
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
        print("aucune session connue pour %s" % args.persona)
        return 0
    for r in rows:
        print("%s  %-11s → %-11s %-9s %-14s %-10s %s%s" % (
            r["session_id"], _fmt(r["started_at"]),
            "en cours" if r["ended_at"] is None else _fmt(r["ended_at"]),
            r.get("harness") or "?", r.get("host") or "?", r.get("account") or "—",
            ("lot %s" % r["work_item"]) if r.get("work_item") else "",
            (" (%s)" % r["end_reason"]) if r.get("end_reason") else ""))
    return 0
