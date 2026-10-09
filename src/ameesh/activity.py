# SPDX-License-Identifier: AGPL-3.0-only
"""Journal d'activité d'une persona (lot L59, décision 0033 §9, étude v2 G1).

Un humain voit l'activité présente et passée des personas dont il est
responsable : `ameesh activity <persona>` fond en une chronologie ce que le
mesh sait déjà d'elle (tours et leur coût, messages reçus et envoyés,
événements et jalons de ses lots, délégations) et son état présent.

Le contenu des sessions (transcripts) n'est pas ici : il se lit depuis leur
sauvegarde (L53). L'accès par responsabilité (responsable, suppléants,
supérieurs) passe par l'identité des humains (volet D) ; en attendant, la
commande lit la base avec les droits de qui la lance.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime

DEFAULT_SINCE = "24h"
DEFAULT_LIMIT = 200
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdj])$")


def parse_since(text: str) -> float:
    """`30m`, `24h`, `7d` (ou `7j`) → secondes."""
    m = _DURATION.match((text or "").strip().lower())
    if not m:
        raise ValueError("durée illisible : %r (ex. 30m, 24h, 7d)" % text)
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400}[m.group(2)]


def _ts(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value).strip().replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _extrait(text, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def events(db, agent: str, since_s: float, now: float | None = None) -> list[dict]:
    """La chronologie de `agent` depuis `since_s` secondes, plus ancienne d'abord."""
    now = time.time() if now is None else now
    interval = "%d seconds" % int(since_s)
    out: list[dict] = []
    for r in db.query(
            "SELECT recorded_at, harness, model, session, usd, input_tokens, output_tokens "
            "FROM turn_costs WHERE agent = %s AND recorded_at >= now() - %s::interval",
            (agent, interval)):
        out.append({"at": _ts(r["recorded_at"]), "kind": "tour",
                    "text": "tour %s%s — %.4f $ (%s jetons entrés, %s sortis)" % (
                        r.get("harness") or "?",
                        (" / " + r["model"]) if r.get("model") else "",
                        float(r.get("usd") or 0), r.get("input_tokens") or 0,
                        r.get("output_tokens") or 0),
                    "session": r.get("session")})
    for r in db.query(
            "SELECT id, sender, recipient, kind, body, work_item_id, created_at, delivered_at "
            "FROM agent_mailbox WHERE (sender = %s OR recipient = %s) "
            "AND created_at >= now() - %s::interval", (agent, agent, interval)):
        sent = r["sender"] == agent
        out.append({"at": _ts(r["created_at"]), "kind": "message envoyé" if sent else "message reçu",
                    "text": "%s %s%s : %s" % (
                        "→" if sent else "←", r["recipient"] if sent else r["sender"],
                        " (lot %s)" % r["work_item_id"] if r.get("work_item_id") else "",
                        _extrait(r.get("body"))),
                    "message": r["id"],
                    "delivered": _ts(r.get("delivered_at"))})
    for r in db.query(
            "SELECT e.work_item_id, e.state, e.note, e.actor, e.created_at, w.title "
            "FROM work_item_events e JOIN work_items w ON w.id = e.work_item_id "
            "WHERE (e.actor = %s OR w.assignee = %s) AND e.created_at >= now() - %s::interval",
            (agent, agent, interval)):
        out.append({"at": _ts(r["created_at"]), "kind": "lot",
                    "text": "lot %s « %s » → %s%s%s" % (
                        r["work_item_id"], _extrait(r.get("title"), 60), r.get("state") or "?",
                        "" if r.get("actor") in (None, agent) else " (par %s)" % r["actor"],
                        (" : " + _extrait(r["note"], 120)) if r.get("note") else ""),
                    "lot": r["work_item_id"]})
    for r in db.query(
            "SELECT m.work_item_id, m.kind, m.at, m.sha, m.actor, m.verdict, w.title "
            "FROM work_item_milestones m JOIN work_items w ON w.id = m.work_item_id "
            "WHERE (m.actor = %s OR w.assignee = %s) AND m.at >= now() - %s::interval",
            (agent, agent, interval)):
        out.append({"at": _ts(r["at"]), "kind": "jalon",
                    "text": "lot %s « %s » : %s%s%s" % (
                        r["work_item_id"], _extrait(r.get("title"), 60), r.get("kind") or "?",
                        (" " + str(r["sha"])[:12]) if r.get("sha") else "",
                        (" — verdict " + r["verdict"]) if r.get("verdict") else ""),
                    "lot": r["work_item_id"]})
    for r in db.query(
            "SELECT work_item_id, delegate, delegated_by, delegated_at, due_at, outcome, "
            "resolved_at FROM work_item_delegations WHERE (delegate = %s OR delegated_by = %s) "
            "AND delegated_at >= now() - %s::interval", (agent, agent, interval)):
        out.append({"at": _ts(r["delegated_at"]), "kind": "délégation",
                    "text": "lot %s délégué par %s à %s%s%s" % (
                        r["work_item_id"], r["delegated_by"], r["delegate"],
                        (" (échéance %s)" % _horodatage(_ts(r["due_at"]))) if r.get("due_at") else "",
                        (" — " + r["outcome"]) if r.get("outcome") else ""),
                    "lot": r["work_item_id"]})
    out = [e for e in out if e["at"] is not None]
    out.sort(key=lambda e: e["at"])
    return out


def present(db, agent: str) -> dict | None:
    rows = db.query(
        "SELECT name, status, status_text, harness, host, session_id, responsible, team, mode, "
        "lease_owner, lease_expires_at, turns, last_turn_at, last_error, stop_reason "
        "FROM agent_registry WHERE name = %s", (agent,))
    return rows[0] if rows else None


def _horodatage(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def render(agent: str, now_row: dict | None, items: list[dict], since: str) -> str:
    lines = []
    if now_row:
        lines.append("%s — %s%s ; harnais %s sur %s ; responsable %s ; tours %s" % (
            agent, now_row.get("status") or "?",
            (" (%s)" % _extrait(now_row["status_text"], 80)) if now_row.get("status_text") else "",
            now_row.get("harness") or "?", now_row.get("host") or "?",
            now_row.get("responsible") or "—", now_row.get("turns") or 0))
        if now_row.get("lease_owner"):
            lines.append("  bail : %s" % now_row["lease_owner"])
        if now_row.get("last_error"):
            lines.append("  dernière erreur : %s" % _extrait(now_row["last_error"], 160))
    lines.append("activité depuis %s : %d événement(s)" % (since, len(items)))
    for e in items:
        lines.append("  %s  %-15s %s" % (_horodatage(e["at"]), e["kind"], e["text"]))
    return "\n".join(lines)


def _controle_identite(cfg, source: str, persona: str) -> str | None:
    """None si l'humain du jeton peut voir l'activité de `persona`, sinon la raison."""
    from . import authz, canon as canon_mod, oidc
    token = (sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()).strip()
    try:
        iss = str(oidc.split(token)[1].get("iss", "")).rstrip("/")
        provider = next((p for p in oidc.providers(cfg) if p.issuer == iss), None)
        if provider is None:
            return "émetteur %r non configuré (identity.providers)" % iss
        claims = oidc.verify(token, provider)
    except (oidc.OidcError, OSError, KeyError) as exc:
        return "jeton refusé : %s" % exc
    canons = canon_mod.load_configured(cfg)
    human = oidc.human_for(canons, claims)
    if not human:
        return "aucun membre du canon pour cet e-mail"
    ok, raison = authz.can_view_activity(canons, human, persona)
    return None if ok else raison


def main(argv) -> int:
    from . import db as db_mod
    from .config import load as load_config
    p = argparse.ArgumentParser(prog="ameesh activity",
                                description="Journal d'activité d'une persona (L59).")
    p.add_argument("agent")
    p.add_argument("--since", default=DEFAULT_SINCE, help="période : 30m, 24h, 7d (défaut 24h)")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help="au plus N événements, les plus récents (défaut 200)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--identity", metavar="JETON|-",
                   help="jeton d'identité OIDC de l'humain qui consulte : l'accès est alors "
                        "réservé au responsable, à ses suppléants et à ses supérieurs (D2)")
    args = p.parse_args(argv)
    try:
        since_s = parse_since(args.since)
    except ValueError as exc:
        print("ameesh activity : %s" % exc, file=sys.stderr)
        return 2
    cfg = load_config()
    if args.identity:
        refus = _controle_identite(cfg, args.identity, args.agent)
        if refus:
            print("ameesh activity : accès refusé — %s" % refus, file=sys.stderr)
            return 4
    db = db_mod.connect(cfg)
    try:
        row = present(db, args.agent)
        if row is None:
            print("ameesh activity : persona inconnue %s" % args.agent, file=sys.stderr)
            return 2
        items = events(db, args.agent, since_s)
    finally:
        db.close()
    items = items[-max(1, args.limit):]
    if args.json:
        print(json.dumps({"agent": args.agent, "since": args.since,
                          "present": {k: (v.isoformat() if isinstance(v, datetime) else v)
                                      for k, v in row.items()},
                          "events": items}, ensure_ascii=False, default=str))
    else:
        print(render(args.agent, row, items, args.since))
    return 0
