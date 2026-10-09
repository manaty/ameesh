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

L52b (étude « sessions parallèles », voie B) : une persona occupée peut ouvrir
une **session parallèle** sur un autre lot. Elle est portée par une **fille**,
un agent éphémère `<persona>.l<lot>` qui hérite de la persona son responsable,
son équipe, ses capacités, son harnais et son profil (recopiés à la création,
puis à chaque `canon sync`), et qui a son propre bail et sa propre session.
Ses sessions sont inscrites ici **sous la persona** (`agent` dit quelle ligne
du registre les portait) : un humain voit une persona, pas ses filles.
`open_child` l'ouvre (`ameesh sessions open <persona> --lot N`), borné par
`max_parallel_sessions` (3 par défaut).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

from . import storage
from .config import NAME_RE

#: L52b : échéance par défaut d'une fille (elle vit jusqu'à la fin de son lot ;
#: l'échéance n'est qu'un garde-fou, au plus celle des éphémères : 7 jours)
CHILD_TTL_DEFAULT = "7d"

_LOT_RE = re.compile(r"^[0-9]{1,18}$")


class ChildError(Exception):
    """Ouverture d'une session parallèle refusée."""


def child_name(persona: str, work_item: str) -> str:
    """Nom de la fille d'une persona sur un lot : `<persona>.l<lot>`."""
    return "%s.l%s" % (persona, work_item)


def _owner(db, name: str) -> tuple[str, str]:
    """(persona, agent) : une fille inscrit ses sessions sous sa persona."""
    rows = db.query("SELECT parent_persona FROM agent_registry WHERE name = %s", (name,))
    parent = rows[0].get("parent_persona") if rows else None
    return (parent or name), name


def record_start(db, name: str, session_id: str, *, account: str | None = None) -> None:
    """`name` : la ligne du registre qui ouvre la session (persona ou fille).
    Seules ses propres sessions sont closes (« remplacée par … ») : la session
    d'une fille ne remplace pas celle de sa persona, ni l'inverse."""
    rows = db.query("SELECT harness, host, session_work_item, parent_persona "
                    "FROM agent_registry WHERE name = %s", (name,))
    row = rows[0] if rows else {}
    persona = row.get("parent_persona") or name
    db.execute(
        "UPDATE persona_sessions SET ended_at = now(), end_reason = %s "
        "WHERE persona = %s AND coalesce(agent, persona) = %s AND session_id <> %s "
        "AND ended_at IS NULL",
        ("remplacée par %s" % session_id, persona, name, session_id))
    db.execute(
        "INSERT INTO persona_sessions "
        "(persona, session_id, harness, host, account, work_item, agent) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (persona, session_id) DO UPDATE SET ended_at = NULL, end_reason = NULL, "
        "account = coalesce(EXCLUDED.account, persona_sessions.account), "
        "host = coalesce(EXCLUDED.host, persona_sessions.host)",
        (persona, session_id, row.get("harness"), row.get("host"), account,
         row.get("session_work_item"), name))


def record_end(db, name: str, session_id: str | None, reason: str) -> None:
    if not session_id:
        return
    persona, _agent = _owner(db, name)
    db.execute(
        "UPDATE persona_sessions SET ended_at = now(), end_reason = %s "
        "WHERE persona = %s AND session_id = %s AND ended_at IS NULL",
        (reason, persona, session_id))


def sessions(db, persona: str, limit: int = 50) -> list[dict]:
    """Toutes les sessions de la persona, les siennes et celles de ses filles."""
    return db.query(
        "SELECT session_id, harness, host, account, work_item, started_at, ended_at, "
        "end_reason, coalesce(agent, persona) AS agent "
        "FROM persona_sessions WHERE persona = %s ORDER BY started_at DESC, id DESC LIMIT %s",
        (persona, int(limit)))


def open_child(db, persona: str, work_item: str, *, ttl_seconds: float,
               max_parallel: int = 3, cwd: str | None = None,
               canons: list | None = None) -> dict:
    """Ouvre la session parallèle de `persona` sur le lot `work_item` (L52b).

    Idempotent : une fille vivante sur ce lot est rendue telle quelle
    (`existing` vrai). Refus (`ChildError`) : lot illisible, persona inconnue,
    éphémère ou fille elle-même, arrêtée, sans responsable, nom pris par un
    canon ou par un autre agent, ou plafond de filles vivantes atteint.
    """
    work_item = str(work_item or "").strip()
    if not _LOT_RE.match(work_item):
        raise ChildError("lot illisible : %r (attendu un numéro de lot)" % work_item)
    name = child_name(persona, work_item)
    if not NAME_RE.match(name):
        raise ChildError("nom de fille invalide ou trop long : %s" % name)
    rows = db.query("SELECT name, ephemeral, parent_persona, status, responsible "
                    "FROM agent_registry WHERE name = %s", (persona,))
    if not rows:
        raise ChildError("persona inconnue du registre : %s" % persona)
    mother = rows[0]
    if mother.get("ephemeral") or mother.get("parent_persona"):
        raise ChildError("%s est un éphémère : seule une persona ouvre des sessions "
                         "parallèles" % persona)
    if mother.get("status") == "stopped":
        raise ChildError("la persona %s est arrêtée" % persona)
    if not (mother.get("responsible") or "").strip():
        raise ChildError("la persona %s n'a pas d'humain responsable résolu" % persona)
    for one in canons or []:
        if one is not None and one.readable and one.agent(name) is not None:
            raise ChildError("%s a une fiche dans le canon : ce nom n'est pas libre" % name)
    ops = storage.of(db).ephemerals
    children = ops.children(persona)
    for child in children:
        if child["name"] == name:
            if child.get("alive"):
                return dict(child, existing=True)
            raise ChildError("la fille %s existe déjà mais n'est plus vivante (échue ou "
                             "arrêtée) : retirez-la avant d'en rouvrir une" % name)
    alive = [c for c in children if c.get("alive")]
    if len(alive) >= max(0, int(max_parallel)):
        raise ChildError("%s a déjà %d session(s) parallèle(s) (%s) : plafond "
                         "max_parallel_sessions = %d" % (
                             persona, len(alive), ", ".join(c["name"] for c in alive),
                             max_parallel))
    created = ops.create_child(name, persona, work_item, cwd=cwd, ttl_seconds=ttl_seconds)
    if created is None:
        if ops.exists(name):
            raise ChildError("un agent %s existe déjà (hors de cette persona)" % name)
        raise ChildError("la persona %s a changé pendant l'ouverture : réessayez" % persona)
    return dict(created, existing=False)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%m-%d %H:%M")
    return str(value)[:16].replace("T", " ")


def main_open(argv) -> int:
    """`ameesh sessions open <persona> --lot N [--ttl 7d] [--cwd D] [--prompt P] [--json]`.

    Depuis une session d'agent, on n'ouvre de session parallèle que pour
    soi-même (identité posée par l'exécuteur, comme `agent spawn`) ; un humain,
    hors session d'agent, l'ouvre pour n'importe quelle persona."""
    from . import canon as canon_mod
    from . import canon_sync, identity, registry
    from . import db as db_mod
    from .config import load as load_config
    p = argparse.ArgumentParser(prog="ameesh sessions open",
                                description="Ouvre une session parallèle d'une persona "
                                            "sur un lot (L52b).")
    p.add_argument("persona")
    p.add_argument("--lot", required=True, help="numéro du lot de la session")
    p.add_argument("--ttl", default=CHILD_TTL_DEFAULT,
                   help="échéance de garde-fou (défaut %s, au plus 7 jours)" % CHILD_TTL_DEFAULT)
    p.add_argument("--cwd", default=None,
                   help="dossier de travail de la fille (défaut : celui de la persona ; "
                        "préférez un worktree propre au lot)")
    p.add_argument("--prompt", default=None, help="première consigne de la fille")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config()
    try:
        ttl = canon_sync.parse_ttl(args.ttl)
    except canon_sync.SpawnError as exc:
        print("refus : %s" % exc, file=sys.stderr)
        return 2
    canons: list = []
    for index, entry in enumerate(canon_mod.configured(cfg)):
        try:
            canons.append(canon_mod.from_config(cfg) if index == 0
                          else canon_mod.from_config(cfg, entry=entry))
        except canon_mod.CanonError:
            pass
    cwd = os.path.abspath(os.path.expanduser(args.cwd)) if args.cwd else None
    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        if os.environ.get("AGENT_MAIL_NAME"):
            binding = identity.resolve_binding(cfg, db)
            if not binding.ok or binding.name != args.persona:
                print("refus : cette session est %s ; elle n'ouvre de session parallèle "
                      "que pour elle-même" % binding.describe(), file=sys.stderr)
                return 1
        try:
            row = open_child(db, args.persona, args.lot, ttl_seconds=ttl, cwd=cwd,
                             max_parallel=cfg.max_parallel_sessions, canons=canons)
        except ChildError as exc:
            print("refus : %s" % exc, file=sys.stderr)
            return 1
        if args.prompt and not row.get("existing"):
            registry.set_pending_prompt(db, row["name"], args.prompt)
        mother_cwd = db.query("SELECT cwd FROM agent_registry WHERE name = %s",
                              (args.persona,))
        child_cwd = db.query("SELECT cwd FROM agent_registry WHERE name = %s", (row["name"],))
    finally:
        db.close()
    shared = bool(mother_cwd and child_cwd and mother_cwd[0].get("cwd")
                  and mother_cwd[0]["cwd"] == child_cwd[0].get("cwd"))
    if args.json:
        print(json.dumps(dict(row, shared_cwd=shared), ensure_ascii=False, default=str))
        return 0
    if row.get("existing"):
        print("session parallèle %s déjà ouverte (lot %s)" % (row["name"],
                                                             row.get("session_work_item")))
        return 0
    print("session parallèle %s ouverte pour %s sur le lot %s : responsable %s, hôte %s, "
          "échéance %s" % (
              row["name"], args.persona, row.get("session_work_item"),
              row.get("responsible") or "—", row.get("host") or "—",
              datetime.fromtimestamp(row["ephemeral_expires_ts"]).strftime("%Y-%m-%d %H:%M")))
    if row.get("placement_ok") is False:
        print("placement refusé (non réclamable) : %s"
              % (row.get("placement_diagnostic") or "?"))
    if shared:
        print("attention : la fille partage le dossier de travail de %s (%s) ; deux "
              "sessions dans un même dossier se marchent dessus — préférez --cwd "
              "<worktree du lot>" % (args.persona, child_cwd[0]["cwd"]), file=sys.stderr)
    return 0


def main(argv) -> int:
    """`ameesh sessions <persona> [--limit N] [--json]` ; `ameesh sessions open …`."""
    from . import db as db_mod
    from .config import load as load_config
    if argv and argv[0] == "open" and len(argv) > 1:
        return main_open(argv[1:])
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
        print("%s  %-11s → %-11s %-9s %-14s %-10s %s%s%s" % (
            r["session_id"], _fmt(r["started_at"]),
            "en cours" if r["ended_at"] is None else _fmt(r["ended_at"]),
            r.get("harness") or "?", r.get("host") or "?", r.get("account") or "—",
            ("lot %s" % r["work_item"]) if r.get("work_item") else "",
            (" [%s]" % r["agent"]) if r.get("agent") and r["agent"] != args.persona else "",
            (" (%s)" % r["end_reason"]) if r.get("end_reason") else ""))
    return 0
