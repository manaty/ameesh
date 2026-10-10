# SPDX-License-Identifier: AGPL-3.0-only
"""Vue par projet : quels projets sont en cours, quel agent travaille sur quoi (L62).

  ameesh projects [--json] [--project P] [--all]

Pour chaque projet (l'équipe `team` de l'agent, à défaut son `chantier`) :

* ses agents, avec leur état (au travail, en pause, au repos, arrêté) et la
  raison d'une pause ou d'un arrêt, le lot en cours (le dernier lot ouvert
  cité par l'agent dans son courrier, à défaut le lot de sa session, à
  défaut le lot ouvert assigné le plus récent ; jamais un lot fusionné ou
  fermé), les non-lus, la dépense des 24 dernières heures et le mode de
  paiement (forfait ou au token) ;
* ses lots ouverts SANS agent pour les faire avancer (non assignés, assignés
  à un agent arrêté ou inconnu du registre) ;
* un signalement quand le projet a du travail ouvert mais aucun agent actif.

Le projet d'un lot est son `app`, à défaut son `workstream`, l'équipe de sa
fiche du plan, ou le projet de son assigné. Un projet sans agent actif ni
lot ouvert n'est montré qu'avec `--all` (le JSON le porte toujours, avec
`active: false`).

Le mode de paiement vient du `credential_mode` du canon (`subscription` =
forfait, `api-key` = au token) ; à défaut, des descripteurs de harnais
(`paid_per_token`). La dépense 24 h est celle du grand livre `turn_costs` :
réelle au token, ESTIMÉE pour un forfait.

Une seule lecture de base (`storage.of(db).projects.board`, une requête).
Le JSON suit le schéma `ameesh-projects/1` (docs/EXPLOITATION.md) ; la même
vue est en tête de `ameesh progress` (clé `projects`).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
import textwrap
import time

from . import config as config_mod
from . import db as db_mod
from . import storage

SCHEMA = "ameesh-projects/1"
#: borne de lecture des lots ouverts (une vue, pas un export)
MAX_LOTS = 500
#: longueur d'un titre de lot affiché
TITLE_CHARS = 48
#: longueur de la dernière avancée affichée (une ligne, L96)
LAST_UPDATE_CHARS = 120
#: libellé du projet d'un agent ou d'un lot qui n'en déclare aucun
NO_PROJECT = "(sans projet)"

#: états d'agent (mêmes valeurs que `ameesh progress`)
_FR_STATE = {"working": "au travail", "paused": "en pause", "idle": "au repos",
             "stopped": "arrêté"}
_STATE_ORDER = {"working": 0, "paused": 1, "idle": 2, "stopped": 3}
#: modes de paiement
_FR_PAYMENT = {"plan": "forfait", "token": "token", None: "?"}
#: pourquoi un lot ouvert n'a pas d'agent
_FR_WHY = {"unassigned": "non assigné", "stopped": "assigné à %s, arrêté",
           "unknown": "assigné à %s, inconnu du registre",
           "external": "assigné à %s, session externe"}


def _text(value) -> str:
    return " ".join(str(value or "").split())


def _short(text, limit: int = TITLE_CHARS) -> str:
    text = _text(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def agent_project(row: dict) -> str | None:
    """Le projet d'un agent : son équipe, à défaut son chantier."""
    return _text(row.get("team")) or _text(row.get("chantier")) or None


def lot_project(lot: dict, agent_projects: dict[str, str | None] | None = None) -> str | None:
    """Le projet d'un lot : `app`, `workstream`, équipe de la fiche, projet de l'assigné."""
    return (_text(lot.get("app")) or _text(lot.get("workstream"))
            or _text(lot.get("package_team"))
            or (agent_projects or {}).get(lot.get("assignee") or "") or None)


def payment(row: dict, paid_harnesses=None) -> str | None:
    """`plan` (forfait), `token` (payé au token) ou None (inconnu)."""
    mode = _text(row.get("credential_mode"))
    if mode == "subscription":
        return "plan"
    if mode == "api-key":
        return "token"
    harness = _text(row.get("harness"))
    if not harness or paid_harnesses is None:
        return None
    return "token" if harness in paid_harnesses else "plan"


def _paid_harnesses():
    from . import cost
    try:
        return cost.paid_harnesses_of()
    except (cost.CostError, OSError, ValueError):
        return None


#: candidats au lot en cours, du plus parlant au moins parlant : le dernier lot
#: cité par l'agent dans son courrier, celui de sa session, son lot assigné le
#: plus récent (correctif du 2026-10-11)
_LOT_SOURCES = (("mail_lot", "mail"), ("session_lot", "session"), ("assigned_lot", "assigned"))
#: un lot dans ces états n'est jamais « en cours »
_DONE = ("merged", "promoted", "closed")


def _lot_of(row: dict) -> dict | None:
    """Le lot en cours d'un agent : le premier candidat OUVERT de `_LOT_SOURCES`
    (la lecture ne rend que des lots ouverts ; l'état est revérifié ici)."""
    for prefix, source in _LOT_SOURCES:
        if row.get(prefix + "_id") is not None and row.get(prefix + "_state") not in _DONE:
            return {"id": int(row[prefix + "_id"]), "title": row.get(prefix + "_title") or "",
                    "state": row.get(prefix + "_state"), "source": source}
    return None


def build_agent(row: dict, now: float, paid_harnesses=None) -> dict:
    from . import progress

    state = progress.agent_state(row)
    if state == "working":
        since = row.get("turn_started_ts") or row.get("status_since_ts")
    else:
        since = row.get("status_since_ts") or row.get("updated_ts")
    reason = None
    if state == "stopped":
        parts = [p for p in (_text(row.get("stop_reason")), _text(row.get("status_text"))) if p]
        reason = " — ".join(parts) or None
    elif state == "paused":
        reason = _text(row.get("status_text")) or None
    lot = _lot_of(row)
    # L96 : la dernière avancée lue dans le fil (première ligne non vide de son
    # dernier message) et depuis quand il est sur sa tâche
    last_update = None
    if row.get("last_update_ts") is not None:
        first = next((l for l in str(row.get("last_update_body") or "").splitlines()
                      if l.strip()), "")
        last_update = {"ts": round(float(row["last_update_ts"]), 3),
                       "text": _short(first, LAST_UPDATE_CHARS),
                       "lot": int(row["last_update_lot"])
                       if str(row.get("last_update_lot") or "").isdigit() else None}
    on_task = row.get("on_task_since_ts") if lot else None
    task = None
    if state == "working" and lot is None:
        task = _short(row.get("status_text"), 80) or None
    return {
        "name": row["name"],
        "project": agent_project(row),
        "state": state,
        "reason": reason,
        "since_ts": round(float(since), 3) if since else None,
        "mode": row.get("mode") or "execute",
        "harness": row.get("harness") or None,
        "host": row.get("host") or None,
        "payment": payment(row, paid_harnesses),
        "lot": lot,
        "task": task,
        "open_lots": int(row.get("open_lots") or 0),
        "unread": int(row.get("unread") or 0),
        "last_update": last_update,
        "on_task_since_ts": round(float(on_task), 3) if on_task else None,
        "usd_24h": round(float(row.get("usd_24h") or 0.0), 6),
        "turns_24h": int(row.get("turns_24h") or 0),
        "active": state != "stopped",
    }


def build(board: dict, *, now: float | None = None, project: str | None = None,
          paid_harnesses=None, max_lots: int = MAX_LOTS) -> dict:
    """La vue `ameesh-projects/1` depuis la lecture `projects.board`."""
    now = time.time() if now is None else float(now)
    rows = list(board.get("agents") or [])
    lot_rows = list(board.get("lots") or [])
    agents = [build_agent(r, now, paid_harnesses) for r in rows]
    by_name = {a["name"]: a for a in agents}
    agent_projects = {a["name"]: a["project"] for a in agents}

    groups: dict = {}

    def group(name):
        if name not in groups:
            groups[name] = {"name": name, "agents": [], "lots_open": 0,
                            "lots_without_agent": []}
        return groups[name]

    for agent in agents:
        group(agent["project"])["agents"].append(agent)
    for lot in lot_rows:
        g = group(lot_project(lot, agent_projects))
        g["lots_open"] += 1
        who = lot.get("assignee") or None
        agent = by_name.get(who) if who else None
        if not who:
            why = "unassigned"
        elif agent is None:
            why = "unknown"
        elif agent["state"] == "stopped":
            why = "stopped"
        elif agent["mode"] == "externe" and agent["state"] != "working":
            why = "external"
        else:
            continue
        g["lots_without_agent"].append({
            "id": int(lot["id"]), "title": lot.get("title") or "", "state": lot.get("state"),
            "assignee": who, "why": why,
            "updated_ts": round(float(lot["updated_ts"]), 3) if lot.get("updated_ts") else None,
        })

    projects = []
    for g in groups.values():
        g["agents"].sort(key=lambda a: (_STATE_ORDER.get(a["state"], 9), a["name"]))
        counts = {s: 0 for s in _STATE_ORDER}
        for a in g["agents"]:
            counts[a["state"]] = counts.get(a["state"], 0) + 1
        active_agents = sum(1 for a in g["agents"] if a["active"])
        warnings = []
        if g["lots_open"] and not active_agents:
            warnings.append("aucun agent actif pour %d lot(s) ouvert(s)" % g["lots_open"])
        elif g["agents"] and not active_agents:
            warnings.append("aucun agent actif")
        if g["lots_without_agent"]:
            warnings.append("%d lot(s) ouvert(s) sans agent" % len(g["lots_without_agent"]))
        g.update({
            "active": bool(active_agents or g["lots_open"]),
            "counts": counts,
            "agents_active": active_agents,
            "unread": sum(a["unread"] for a in g["agents"]),
            "usd_24h": round(sum(a["usd_24h"] for a in g["agents"]), 6),
            "usd_24h_token": round(sum(a["usd_24h"] for a in g["agents"]
                                       if a["payment"] == "token"), 6),
            "warnings": warnings,
        })
        projects.append(g)
    if project:
        projects = [p for p in projects if p["name"] == project]
    # les projets actifs d'abord, puis ceux qui travaillent le plus
    projects.sort(key=lambda p: (not p["active"], -p["counts"]["working"],
                                 -p["agents_active"], p["name"] is None, p["name"] or ""))
    total = int(lot_rows[0].get("total") or len(lot_rows)) if lot_rows else 0
    return {
        "schema": SCHEMA,
        "generated_ts": round(now, 3),
        "generated_at": _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
                           .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "project": project or None,
        "projects": projects,
        "truncated": ({"lots": {"shown": len(lot_rows), "total": total, "limit": int(max_lots)}}
                      if total > len(lot_rows) else {}),
    }


def snapshot(db, *, project: str | None = None, now: float | None = None,
             paid_harnesses=None, max_lots: int = MAX_LOTS) -> dict:
    """Lit la base (UNE requête) et rend la vue `ameesh-projects/1`."""
    board = storage.of(db).projects.board(max_lots=max_lots)
    for key in ("agents", "lots"):
        if isinstance(board.get(key), str):      # pilote qui rend le json brut
            board[key] = json.loads(board[key])
    if paid_harnesses is None:
        paid_harnesses = _paid_harnesses()
    return build(board, now=now, project=project, paid_harnesses=paid_harnesses,
                 max_lots=max_lots)


# --------------------------------------------------------------------------
# rendu texte
# --------------------------------------------------------------------------

def _span(seconds) -> str:
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh%02d" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dj" % (seconds // 86400)


def project_label(name: str | None) -> str:
    return name or NO_PROJECT


def state_label(agent: dict, now: float) -> str:
    label = _FR_STATE.get(agent["state"], agent["state"])
    if agent["mode"] == "externe":
        label += " (ext)"
    if agent.get("since_ts"):
        label += " " + _span(now - agent["since_ts"])
    return label


def _headline(p: dict) -> str:
    c = p["counts"]
    parts = ["%d %s" % (c[s], _FR_STATE[s]) for s in ("working", "paused", "idle", "stopped")
             if c.get(s)]
    head = "%s — %d agent(s)%s" % (project_label(p["name"]), len(p["agents"]),
                                   " : " + ", ".join(parts) if parts else "")
    extra = []
    if p["lots_open"]:
        extra.append("%d lot(s) ouvert(s)" % p["lots_open"])
    if p["unread"]:
        extra.append("%d non lu(s)" % p["unread"])
    if p["usd_24h"]:
        extra.append("24 h %.2f\u00a0$ (token %.2f\u00a0$)" % (p["usd_24h"], p["usd_24h_token"]))
    return head + (" · " + " · ".join(extra) if extra else "")


#: en dessous de cette largeur, un agent s'écrit sur deux lignes (pas de tableau)
NARROW_WIDTH = 90


def _last_update(a: dict, now: float) -> str:
    """« dernière avancée il y a 12m : … » (L96)."""
    last = a["last_update"]
    return "avancée il y a %s : %s" % (_span(now - last["ts"]), last["text"])


def _doing(a: dict, now: float | None = None) -> str:
    if a["lot"]:
        doing = "#%s %s" % (a["lot"]["id"], a["lot"]["title"])
        if a["open_lots"] > 1:
            doing += " (+%d)" % (a["open_lots"] - 1)
        if a.get("on_task_since_ts") and now is not None:
            doing += " (depuis %s)" % _span(now - a["on_task_since_ts"])
        return doing
    if a["task"]:
        return "tour : " + a["task"]
    return "—"


def format_lines(view: dict, width: int | None = None, *, show_inactive: bool = False,
                 indent: str = "") -> list[str]:
    """Les lignes de la vue (sans titre) : un bloc par projet, coupé à `width`.

    Large : un tableau par projet. Étroit (< `NARROW_WIDTH`) : deux lignes par
    agent (état, paiement, dépense, non-lus ; puis le lot en cours)."""
    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns
    width = max(36, min(int(width), 160))
    narrow = width < NARROW_WIDTH
    now = view["generated_ts"]
    out: list[str] = []

    def wrap(text: str, first: str, rest: str | None = None) -> None:
        out.extend(textwrap.wrap(text, width, initial_indent=first,
                                 subsequent_indent=rest if rest is not None else first + "  ")
                   or [first])

    shown = [p for p in view["projects"] if p["active"] or show_inactive]
    hidden = [p for p in view["projects"] if not (p["active"] or show_inactive)]
    if not shown:
        out.append(indent + "aucun projet en cours")
    name_w = min(max([len(a["name"]) for p in shown for a in p["agents"]] + [6]), 20)
    for p in shown:
        if out:
            out.append("")
        wrap(_headline(p), indent)
        for warning in p["warnings"]:
            wrap("! " + warning, indent + "  ")
        if p["agents"] and not narrow:
            out.append(indent + "  %-*s %-19s %-7s %8s %4s  %s" % (
                name_w, "AGENT", "ÉTAT", "PAIE", "24H $", "NL", "LOT EN COURS"))
        for a in p["agents"]:
            pay = _FR_PAYMENT.get(a["payment"], "?")
            if narrow:
                # espace insécable avant « $ » : la coupure ne l'isole jamais
                wrap("%s — %s · %s · %.2f\u00a0$ · %d non lu(s)" % (
                    a["name"], state_label(a, now), pay, a["usd_24h"], a["unread"]),
                    indent + "  ")
                if _doing(a) != "—":
                    wrap(_doing(a, now), indent + "    ")
                if a.get("last_update"):
                    wrap(_last_update(a, now), indent + "    ")
                if a["reason"]:
                    wrap("raison : " + a["reason"], indent + "    ")
                continue
            head = indent + "  %-*s %-19s %-7s %8.2f %4d  " % (
                name_w, a["name"][:name_w], state_label(a, now)[:19], pay, a["usd_24h"],
                a["unread"])
            out.append(head + _short(_doing(a, now), max(12, width - len(head))))
            if a.get("last_update"):
                pad = indent + "  " + " " * (name_w + 1)
                out.append(pad + _short(_last_update(a, now), max(20, width - len(pad))))
            if a["reason"]:
                pad = indent + "  " + " " * (name_w + 1)
                out.append(pad + _short("raison : " + a["reason"], max(20, width - len(pad))))
        for lot in p["lots_without_agent"]:
            why = _FR_WHY.get(lot["why"], lot["why"])
            if "%s" in why:
                why = why % lot["assignee"]
            wrap("sans agent : #%s %s — %s (%s)" % (
                lot["id"], lot["state"], _short(lot["title"], 40), why), indent + "  ")
    if hidden:
        out.append("")
        wrap("à l'arrêt (sans agent actif ni lot ouvert) : " + ", ".join(
            "%s (%s)" % (project_label(p["name"]), ", ".join(a["name"] for a in p["agents"]))
            for p in hidden), indent)
    cut = (view.get("truncated") or {}).get("lots")
    if cut:
        wrap("TRONQUÉ : %d lots ouverts lus sur %d (borne %d)" % (
            cut["shown"], cut["total"], cut["limit"]), indent)
    return out


def format_text(view: dict, width: int | None = None, *, show_inactive: bool = False) -> str:
    projects = [p for p in view["projects"] if p["active"]]
    agents = sum(len(p["agents"]) for p in view["projects"])
    lots = sum(p["lots_open"] for p in view["projects"])
    title = "projets — %s (%d en cours, %d agent(s), %d lot(s) ouvert(s))" % (
        time.strftime("%H:%M", time.localtime(view["generated_ts"])), len(projects),
        agents, lots)
    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns
    lines = textwrap.wrap(title, max(36, min(int(width), 160)), subsequent_indent="  ")
    return "\n".join(lines + [""] + format_lines(view, width, show_inactive=show_inactive))


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh projects", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="schéma ameesh-projects/1")
    parser.add_argument("--project", default=None, help="un seul projet (équipe)")
    parser.add_argument("--all", action="store_true",
                        help="montre aussi les projets à l'arrêt (sans agent actif ni lot)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    cfg = config_mod.load()
    try:
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db)
            view = snapshot(db, project=args.project)
        finally:
            db.close()
    except db_mod.SchemaMissing as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_text(view, show_inactive=args.all or bool(args.project)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
