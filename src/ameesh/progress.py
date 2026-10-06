# SPDX-License-Identifier: AGPL-3.0-only
"""Vue temps réel de l'avancement (lot L24, décision 0024).

  ameesh progress [--json] [--project P] [--since 24h] [--stale-after 6h]
  ameesh progress --html FICHIER [--project P] [--since 24h] [--stale-after 6h]

L'état courant et la frise du projet, **alimentés par ce qu'ameesh enregistre
lui-même** (transitions des lots `work_items`, actions sous porte, tours et
baux du registre, marqueur comptable, grand livre `turn_costs`) — jamais par
git ni par le board :

* **lots** : jalons datés demande → gel → verdict → fusion quand ils sont
  connus, état (actif, en revue, bloqué, approuvé, fusionné, fermé),
  blocages ; et (L29) ce que le lot attend et de qui (`waiting_for`), s'il
  stagne (`stale` : aucune activité depuis `--stale-after`, défaut 6 h),
  sa fiche du plan et son epic ;
* **epics** (L29) : les lots regroupés par epic du plan, avec la
  progression (lots fusionnés / total) ;
* **agents** : harnais, modèle, effort, état (travaille, repos, pause, arrêt),
  durée et tâche du tour en cours ;
* **jalons du projet** : les fiches `WorkPackage` de sorte `milestone` du
  canon (L29), avec leur progression ; aucun n'est inventé ;
* **budget** : dépense réelle des harnais payés au token, estimations des
  forfaits, jauges de forfait déjà lues par `ameesh cost`.

Le JSON suit le schéma versionné `ameesh-progress/1`
(`docs/PROGRESS.md`). `--html` écrit une page statique autonome (aucune
ressource réseau, aucun serveur) qui rend ce JSON en frise.

Ce module ne fait que lire : toutes les lectures de base passent par
`storage.of(db).progress` ; les jauges de forfait viennent de
`cost.CostBook` (journaux locaux des harnais, lecture seule).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import sys
import textwrap
import time
from typing import Iterable

from . import config as config_mod
from . import cost as cost_mod
from . import db as db_mod
from . import plan as plan_mod
from . import stagnation
from . import storage

SCHEMA = "ameesh-progress/1"
DEFAULT_SINCE = "24h"
#: bornes de lecture : une vue, pas un export complet de la base
MAX_LOTS = 500
MAX_ACTIONS = 500
#: longueur d'une tâche affichée (consigne en cours, libellé)
TASK_CHARS = 140

#: états du lot rendus par la vue (décision 0024)
LOT_STATES = ("active", "review", "blocked", "approved", "merged", "closed")
#: états d'agent rendus par la vue
AGENT_STATES = ("working", "idle", "paused", "stopped")

_FR_LOT = {"active": "actif", "review": "en revue", "blocked": "bloqué",
           "approved": "approuvé", "merged": "fusionné", "closed": "fermé"}
_FR_AGENT = {"working": "travaille", "idle": "repos", "paused": "pause",
             "stopped": "arrêt"}

#: connecteur de fusion (L5) : son approbation vaut verdict, sa confirmation fusion
MERGE_CONNECTOR = "git-merge"
_WAITING = ("blocked", "waiting_human")

#: ce que la vue ne sait pas encore lire (dit dans le JSON, jamais deviné)
MISSING = (
    "jalons du projet : fiches WorkPackage `milestone` du canon synchronisé "
    "(`ameesh canon sync`) ; sans date déclarée (`at_ts` null)",
    "jalons de lot : lus dans la table des jalons de lot (L10) quand un gel ou "
    "un verdict y est déclaré, sinon déduits du journal des transitions et des "
    "actions de fusion",
    "effort : réglage local de l'hôte de l'agent (`ameesh set`), absent de la "
    "base ; null pour un agent d'un autre hôte ou sans réglage",
    "jauges de forfait : lues dans les journaux locaux des harnais de cet hôte "
    "(`ameesh cost`) ; leur historique en base se lit par `ameesh cost gauges`",
)


class ProgressError(ValueError):
    """Option refusée (fenêtre illisible, fichier impossible à écrire)."""


# --------------------------------------------------------------------------
# fenêtre
# --------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdj])\s*$", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "j": 86400}


def parse_since(text: str | None, now: float) -> float:
    """`--since` : une durée (`90m`, `16h`, `2d`/`2j`) ou une date ISO 8601
    (`2026-10-05`, `2026-10-05T08:00`, avec ou sans fuseau ; sans fuseau =
    heure locale). Rend l'instant de début en secondes epoch."""
    text = (text or DEFAULT_SINCE).strip()
    match = _DURATION_RE.match(text)
    if match:
        return now - float(match.group(1)) * _UNITS[match.group(2).lower()]
    try:
        moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ProgressError("--since illisible : %r (durée 16h, 2d… ou date ISO)" % text)
    if moment.tzinfo is None:
        return time.mktime(moment.timetuple()) + moment.microsecond / 1e6
    return moment.timestamp()


# --------------------------------------------------------------------------
# lots
# --------------------------------------------------------------------------

def _jalons_de_lot(item: dict, events: Iterable[dict], actions: Iterable[dict],
                   declared: Iterable[dict] | None = None) -> dict:
    """Les jalons d'un lot : requested / frozen / verdict / merged.

    SEUL point de lecture des jalons de lot. Deux sources, même résultat :

    * `declared` — les lignes de la table des jalons de lot (L10,
      `work_item_milestones` : `kind`, `at_ts`, `actor`, `verdict`) ; quand
      le lot y a au moins un gel ou un verdict DÉCLARÉ, gels et verdicts
      viennent de là ; `requested` et `merged` en viennent dès qu'ils y sont ;
    * sinon, déduction depuis le journal des transitions (`work_item_events`,
      dans l'ordre d'écriture) : gel = entrée en `qa`, verdict = sortie de
      `qa` vers `merged` (ok) ou vers `build` (blocked).

    Dans les deux cas, l'approbation de l'action de fusion du lot
    (`MERGE_CONNECTOR`) compte comme un verdict `ok` (approbateur =
    relecteur) et sa fin confirmée comme une fusion si aucune n'est connue.

    Rend :

    * `requested`, `frozen` (PREMIER gel), `verdict` (verdict retenu = le
      plus récent), `merged` : instants ou None ;
    * `last_frozen` : le DERNIER gel (un lot regelé repasse en revue) ;
    * `verdict_kind` : `ok` | `blocked` | None, et `reviewer` : son auteur ;
    * `verdict_current` : le verdict retenu est-il postérieur au dernier gel ?
    * `blocked_verdicts` : nombre de verdicts bloquants ;
    * `blocks` : nombre de mises en attente (`blocked`, `waiting_human`) ;
    * `source` : `milestones` (table L10) ou `events` (déduction).
    """
    requested = item.get("created_ts")
    merged = None
    blocks = 0
    prev = None
    ev_freezes: list = []
    ev_verdicts: list = []                # (instant, ok|blocked, auteur)
    for ev in events:
        state, at = ev.get("state"), ev.get("created_ts")
        if prev is None:
            prev = state
            if requested is None:
                requested = at
            continue
        if state == prev:
            continue                      # une note, pas une transition
        if state == "qa":
            ev_freezes.append(at)
        if prev == "qa" and state in ("build", "merged"):
            ev_verdicts.append((at, "ok" if state == "merged" else "blocked",
                                ev.get("actor") or None))
        if state == "merged" and merged is None:
            merged = at
        if state in _WAITING and prev not in _WAITING:
            blocks += 1
        prev = state

    rows = [r for r in (declared or []) if r.get("at_ts") is not None]
    if any(r.get("kind") in ("frozen", "verdict") for r in rows):
        source = "milestones"
        freezes = sorted(r["at_ts"] for r in rows if r.get("kind") == "frozen")
        verdicts = [(r["at_ts"], r.get("verdict") or "ok", r.get("actor") or None)
                    for r in rows if r.get("kind") == "verdict"]
    else:
        source = "events"
        freezes, verdicts = ev_freezes, ev_verdicts
    # Une source à la fois, pour des instants cohérents entre eux (le jalon
    # automatique `merged` et l'événement de la même transition diffèrent de
    # quelques microsecondes) : la table entière quand elle porte des
    # déclarations, sinon le journal, la table ne comblant que ses trous.
    auto = {}
    for r in rows:
        if r.get("kind") in ("requested", "merged") and r.get("kind") not in auto:
            auto[r["kind"]] = r["at_ts"]          # lignes en ordre chronologique
    if source == "milestones":
        requested = auto.get("requested", requested)
        merged = auto.get("merged", merged)
    else:
        requested = requested if requested is not None else auto.get("requested")
        merged = merged if merged is not None else auto.get("merged")

    for act in actions:
        if act.get("connector") != MERGE_CONNECTOR:
            continue
        if act.get("approved_ts"):
            verdicts.append((act["approved_ts"], "ok",
                             act.get("auth_approver") or act.get("last_actor") or None))
        if act.get("state") == "confirmed" and merged is None:
            merged = act.get("finished_ts")

    verdicts.sort(key=lambda v: v[0])
    last = verdicts[-1] if verdicts else None
    last_frozen = freezes[-1] if freezes else None
    return {
        "requested": requested,
        "frozen": freezes[0] if freezes else None,
        "last_frozen": last_frozen,
        "verdict": last[0] if last else None,
        "verdict_kind": last[1] if last else None,
        "reviewer": last[2] if last else None,
        "verdict_current": bool(last) and (last_frozen is None or last[0] >= last_frozen),
        "merged": merged,
        "blocked_verdicts": sum(1 for v in verdicts if v[1] == "blocked"),
        "blocks": blocks,
        "source": source,
    }


def lot_state(work_state: str, jalons: dict) -> str:
    """État affiché d'un lot (décision 0024) : en revue, il n'est approuvé
    que par un verdict `ok` postérieur au DERNIER gel."""
    if work_state in ("merged", "promoted"):
        return "merged"
    if work_state == "closed":
        return "closed"
    if work_state in _WAITING:
        return "blocked"
    if work_state == "qa":
        last_frozen = jalons.get("last_frozen", jalons.get("frozen")) or 0
        if jalons.get("verdict_kind") == "ok" and (jalons.get("verdict") or 0) >= last_frozen:
            return "approved"
        return "review"
    return "active"


def _jalons_declares(st, ids: list[int]) -> dict:
    """Les jalons de ces lots dans la table de L10 (`work_item_milestones`),
    par lot ; `_jalons_de_lot` les préfère à la déduction dès qu'un gel ou un
    verdict y est déclaré."""
    out: dict = {}
    for row in st.lot_milestones(ids):
        out.setdefault(int(row["work_item_id"]), []).append(row)
    return out


def _round(ts):
    return None if ts is None else round(float(ts), 3)


def build_lot(item: dict, events: list[dict], actions: list[dict],
              declared: list[dict] | None = None, *, packages: dict | None = None,
              now: float | None = None,
              stale_after: float = stagnation.DEFAULT_THRESHOLD_S,
              messages_ts: float | None = None) -> dict:
    jalons = _jalons_de_lot(item, events, actions, declared)
    packages = packages or {}
    package = packages.get(item.get("package_id"))
    closed_at = next((r.get("at_ts") for r in (declared or []) if r.get("kind") == "closed"),
                     None)
    last = stagnation.last_activity(item, events, declared or [], actions, messages_ts)
    now = time.time() if now is None else float(now)
    return {
        "id": int(item["id"]),
        "title": item.get("title") or "",
        "type": item.get("type") or "",
        "project": item.get("app") or None,
        "workstream": item.get("workstream") or None,
        "issue_ref": item.get("issue_ref") or None,
        "state": lot_state(item.get("state") or "", jalons),
        "work_state": item.get("state") or "",
        "assignee": item.get("assignee") or None,
        "reviewer": jalons["reviewer"],
        "milestones": {k: _round(jalons[k])
                       for k in ("requested", "frozen", "verdict", "merged")},
        "last_frozen_ts": _round(jalons["last_frozen"]),
        "verdict": jalons["verdict_kind"],
        "milestones_source": jalons["source"],
        "blocked_verdicts": jalons["blocked_verdicts"],
        "blocks": jalons["blocks"],
        "qa_loops": int(item.get("loops") or 0),
        "updated_ts": _round(item.get("updated_ts")),
        "budget_usd": item.get("budget_usd"),
        "spent_usd": item.get("spent_usd"),
        "actions": [a.get("action_id") for a in actions],
        # L29 : plan, fermeture, attente, stagnation (champs ajoutés)
        "package": item.get("package_id") or None,
        "epic": plan_mod.epic_of(item.get("package_id"), packages),
        "pr_ref": item.get("pr_ref") or None,
        "closed": ({"reason": item.get("close_reason"),
                    "superseded_by": item.get("superseded_by"),
                    "at_ts": _round(closed_at)}
                   if item.get("state") == "closed" else None),
        "waiting_for": stagnation.waiting(
            item, jalons, actions, responsible=(package or {}).get("responsible")),
        "last_activity_ts": _round(last),
        "stale": stagnation.stale(item.get("state") or "", last, now, stale_after),
    }


def build_action(row: dict) -> dict:
    return {
        "id": row["action_id"],
        "project": row.get("project"),
        "work_item": row.get("work_item"),
        "connector": row.get("connector"),
        "operation": row.get("operation"),
        "class": row.get("class"),
        "state": row.get("state"),
        "proposed_by": row.get("proposed_by"),
        "attempts": int(row.get("attempts") or 0),
        "created_ts": _round(row.get("created_ts")),
        "approved_ts": _round(row.get("approved_ts")),
        "launched_ts": _round(row.get("launched_ts")),
        "finished_ts": _round(row.get("finished_ts")),
    }


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------

def agent_state(row: dict) -> str:
    """travaille | repos | pause | arrêt, depuis le statut et le bail."""
    status = row.get("status") or ""
    if status == "running":
        return "working" if row.get("lease_live") else "stopped"
    if status == "blocked":
        return "paused"                 # pause budget ou comptable (L13)
    if status in ("stopped", "dead"):
        return "stopped"
    return "idle"                       # idle, queued


def _excerpt(text: str | None, limit: int = TASK_CHARS) -> str | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    return text if len(text) <= limit else text[:limit - 1] + "…"


def read_effort(cfg, row: dict) -> str | None:
    """L'effort réglé par `ameesh set` : état local de l'hôte de l'agent."""
    if cfg is None or (row.get("host") or "") != (cfg.host or ""):
        return None
    try:
        with open(os.path.join(cfg.agent_dir(row["name"]), "effort"), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except (OSError, ValueError):
        return None


def build_agent(row: dict, now: float, effort: str | None = None) -> dict:
    state = agent_state(row)
    turn = None
    if state == "working":
        started = row.get("turn_started_ts") or row.get("updated_ts")
        turn = {
            "started_ts": _round(started),
            "duration_s": int(max(0.0, now - float(started))) if started else None,
            "label": _excerpt(row.get("status_text") or row.get("turn_label"), 80),
            "task": _excerpt(row.get("current_prompt")),
        }
        since = started
    elif state == "idle":
        since = row.get("last_turn_ts") or row.get("updated_ts")
    else:
        since = row.get("updated_ts")
    return {
        "name": row["name"],
        "harness": row.get("harness") or None,
        "model": row.get("model") or row.get("turn_model") or None,
        "effort": effort,
        "host": row.get("host") or None,
        "project": row.get("team") or row.get("chantier") or None,
        "state": state,
        "status": row.get("status") or None,
        "status_text": _excerpt(row.get("status_text"), 160),
        "since_ts": _round(since),
        "turn": turn,
        "turns": int(row.get("turns") or 0),
        "unread": int(row.get("unread") or 0),
        "pending_prompt": bool(row.get("has_pending_prompt")),
        "lease_live": bool(row.get("lease_live")),
    }


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------

def build_budget(cost_rows: list[dict], gauges: list, now: float, *,
                 paid_harnesses: Iterable[str] | None = None,
                 hourly_cap: float = cost_mod.DEFAULT_HOURLY_USD) -> dict:
    # Les harnais payés au token viennent des descripteurs (L16) : plus de liste
    # fermée dans le code. `None` = les relire maintenant.
    paid = cost_mod.paid_harnesses_of() if paid_harnesses is None else tuple(paid_harnesses)
    spend = {key: {"total_usd": 0.0, "paid_usd": 0.0} for key in ("window", "1h", "24h")}
    by_agent = []
    for row in cost_rows:
        is_paid = (row.get("harness") or "") in paid
        for key, col in (("window", "usd_window"), ("1h", "usd_1h"), ("24h", "usd_24h")):
            value = float(row.get(col) or 0.0)
            spend[key]["total_usd"] += value
            if is_paid:
                spend[key]["paid_usd"] += value
        by_agent.append({
            "agent": row["agent"], "harness": row.get("harness"),
            "model": row.get("model") or None, "paid": is_paid,
            "usd": round(float(row.get("usd_window") or 0.0), 6),
            "turns": int(row.get("turns") or 0),
            "input_tokens": int(row.get("input_tokens") or 0),
            "cached_input_tokens": int(row.get("cached_input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
        })
    for bucket in spend.values():
        for key in bucket:
            bucket[key] = round(bucket[key], 6)
    return {
        "currency": "USD",
        "paid_harnesses": list(paid),
        "hourly_cap_usd": float(hourly_cap),
        "spend": spend,
        "by_agent": [r for r in by_agent if r["usd"] or r["turns"]],
        "plans": [{
            "harness": g.harness, "key": g.key, "used": round(g.used, 4),
            "pace_cap": round(g.pace_cap(now), 4), "elapsed": round(g.elapsed(now), 4),
            "resets_ts": _round(g.resets_at), "window_s": g.window_s,
            "exceeded": g.exceeded(now),
        } for g in gauges],
    }


# --------------------------------------------------------------------------
# l'instantané
# --------------------------------------------------------------------------

def _truncation(rows: list[dict], limit: int) -> dict | None:
    """`{shown, total, limit}` si la borne de rendu a coupé, sinon None."""
    total = int(rows[0].get("total") or len(rows)) if rows else 0
    if total <= len(rows):
        return None
    return {"shown": len(rows), "total": total, "limit": int(limit)}


def snapshot(db, cfg=None, *, since: str | None = None, project: str | None = None,
             book: cost_mod.CostBook | None = None, now: float | None = None,
             max_lots: int = MAX_LOTS, max_actions: int = MAX_ACTIONS,
             stale_after: float | None = None) -> dict:
    """L'instantané `ameesh-progress/1` (voir docs/PROGRESS.md).

    Les bornes (`max_lots`, `max_actions`) ne limitent que le RENDU : les
    lots ouverts passent avant les fusionnés, et les jalons et l'état d'un
    lot affiché sont lus sans borne (son journal complet, toutes ses
    actions). Toute coupure est dite dans `truncated`.
    """
    now = time.time() if now is None else float(now)
    since_ts = parse_since(since, now)
    st = storage.of(db).progress
    items = st.lots(since_ts=since_ts, project=project, limit=max_lots)
    items.sort(key=lambda i: (i.get("created_ts") or 0, int(i["id"])))
    ids = [int(i["id"]) for i in items]
    events: dict = {}
    for ev in st.lot_events(ids):
        events.setdefault(int(ev["work_item_id"]), []).append(ev)
    by_item: dict = {}
    for row in st.lot_actions(ids):
        by_item.setdefault(int(row["work_item"]), []).append(row)
    declared = _jalons_declares(st, ids)
    package_rows = st.packages()
    packages = {p["id"]: p for p in package_rows}
    threshold = stagnation.stale_after() if stale_after is None else float(stale_after)
    messages = {int(r["work_item_id"]): r.get("last_ts") for r in st.lot_messages(ids)}
    lots = [build_lot(item, events.get(int(item["id"]), []),
                      by_item.get(int(item["id"]), []), declared.get(int(item["id"])),
                      packages=packages, now=now, stale_after=threshold,
                      messages_ts=messages.get(int(item["id"])))
            for item in items]
    plan = plan_mod.summarize(package_rows, st.package_items())
    action_rows = st.actions(since_ts=since_ts, project=project, limit=max_actions)
    truncated = {}
    for key, rows, limit in (("lots", items, max_lots), ("actions", action_rows, max_actions)):
        cut = _truncation(rows, limit)
        if cut:
            truncated[key] = cut
    action_rows.sort(key=lambda r: (r.get("created_ts") or 0, r["action_id"]))

    agent_rows = st.agents(project)
    agents = [build_agent(r, now, read_effort(cfg, r)) for r in agent_rows]
    cost_rows = st.costs(since_ts=since_ts,
                         agents=[r["name"] for r in agent_rows] if project else None)
    if book is None:
        book = cost_mod.CostBook(state_dir=cfg.state_dir if cfg else None, db=db)
    try:
        gauges = book.gauges()
    except (OSError, ValueError):
        gauges = []
    hourly = getattr(cfg, "budget_usd_per_hour", None) or cost_mod.DEFAULT_HOURLY_USD

    return {
        "schema": SCHEMA,
        "generated_ts": _round(now),
        "generated_at": _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
                           .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "host": getattr(cfg, "host", None),
        "project": project or None,
        "window": {"from_ts": _round(since_ts), "to_ts": _round(now)},
        "lots": lots,
        "agents": agents,
        "milestones": plan["milestones"],
        "epics": plan["epics"],
        "stale_after_s": int(threshold),
        "actions": [build_action(r) for r in action_rows],
        "budget": build_budget(cost_rows, gauges, now, hourly_cap=hourly),
        "truncated": truncated,
        "missing": list(MISSING),
    }


# --------------------------------------------------------------------------
# rendu texte (terminal étroit)
# --------------------------------------------------------------------------

def _hm(ts, now: float) -> str:
    if not ts:
        return "—"
    moment = time.localtime(ts)
    if time.strftime("%Y-%m-%d", moment) == time.strftime("%Y-%m-%d", time.localtime(now)):
        return time.strftime("%H:%M", moment)
    return time.strftime("%d/%m %H:%M", moment)


def _duration(seconds) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        minutes = (seconds % 3600) // 60
        return ("%d h %02d" % (seconds // 3600, minutes)) if minutes else "%d h" % (seconds // 3600)
    return "%d j %d h" % (seconds // 86400, (seconds % 86400) // 3600)


def format_text(snap: dict, width: int | None = None) -> str:
    """La sortie lisible : une colonne, coupée à la largeur du terminal."""
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    width = max(36, min(int(width), 100))
    now = snap["generated_ts"]
    out: list[str] = []

    def line(text: str, indent: str = "") -> None:
        out.extend(textwrap.wrap(text, width, initial_indent=indent,
                                 subsequent_indent=indent + "  ") or [indent])

    hours = (snap["window"]["to_ts"] - snap["window"]["from_ts"]) / 3600.0
    line("avancement%s — %s (fenêtre %s)" % (
        " de %s" % snap["project"] if snap.get("project") else "",
        _hm(now, now), _duration(hours * 3600)))

    out.append("")
    line("LOTS (%d)" % len(snap["lots"]))
    for key, label in (("lots", "lots"), ("actions", "actions")):
        cut = (snap.get("truncated") or {}).get(key)
        if cut:
            line("TRONQUÉ : %d %s affichés sur %d (borne %d)" % (
                cut["shown"], label, cut["total"], cut["limit"]), "  ")
    if not snap["lots"]:
        line("aucun lot dans la fenêtre", "  ")
    for lot in snap["lots"]:
        m = lot["milestones"]
        state = _FR_LOT.get(lot["state"], lot["state"])
        stale = lot.get("stale")
        if stale:
            # un lot sans activité n'est pas dessiné comme actif
            state = "STAGNANT %s (%s)" % (_duration(stale["idle_s"]), state)
        closed = lot.get("closed")
        if closed:
            state = ("abandonné" if closed.get("reason") == "abandoned"
                     else "remplacé par #%s" % closed.get("superseded_by"))
        line("#%s %s — %s%s" % (lot["id"], state, lot["title"],
                                " [%s]" % lot["epic"] if lot.get("epic") else ""), "  ")
        line("demande %s · gel %s · verdict %s%s · fusion %s" % (
            _hm(m["requested"], now), _hm(m["frozen"], now), _hm(m["verdict"], now),
            " (%s)" % lot["verdict"] if lot["verdict"] else "", _hm(m["merged"], now)),
            "    ")
        extra = []
        if lot["assignee"]:
            extra.append("auteur %s" % lot["assignee"])
        if lot["reviewer"]:
            extra.append("revue %s" % lot["reviewer"])
        if lot["blocked_verdicts"] or lot["blocks"]:
            extra.append("blocages %d" % ((lot["blocked_verdicts"] or 0) + lot["blocks"]))
        if extra:
            line(" · ".join(extra), "    ")
        if lot.get("waiting_for"):
            line("attend : %s" % lot["waiting_for"]["label"], "    ")

    epics = snap.get("epics") or []
    if epics:
        out.append("")
        line("EPICS (%d)" % len(epics))
        for epic in epics:
            counted = epic["lots_total"] - epic["lots_abandoned"]
            line("%s — %s : %d/%d lots fusionnés%s" % (
                epic["id"], epic["title"], epic["lots_merged"], counted,
                " (%d abandonné(s))" % epic["lots_abandoned"] if epic["lots_abandoned"] else ""),
                "  ")

    out.append("")
    line("AGENTS (%d)" % len(snap["agents"]))
    if not snap["agents"]:
        line("aucun agent", "  ")
    for agent in snap["agents"]:
        since = agent["since_ts"]
        line("%s — %s%s" % (agent["name"], _FR_AGENT.get(agent["state"], agent["state"]),
                            " depuis %s" % _duration(now - since) if since else ""), "  ")
        line("%s · %s · effort %s" % (agent["harness"] or "?", agent["model"] or "défaut",
                                      agent["effort"] or "défaut"), "    ")
        turn = agent.get("turn")
        if turn:
            line("tour : %s" % (turn["task"] or turn["label"] or "en cours"), "    ")
        elif agent["state"] in ("paused", "stopped") and agent["status_text"]:
            line("raison : %s" % agent["status_text"], "    ")

    out.append("")
    if snap["milestones"]:
        line("JALONS (%d)" % len(snap["milestones"]))
        for ms in snap["milestones"]:
            progress = ("%d/%d lots fusionnés" % (
                ms["lots_merged"], ms["lots_total"] - ms.get("lots_abandoned", 0))
                if "lots_total" in ms else "")
            line("%s %s%s" % (_hm(ms.get("at_ts"), now) if ms.get("at_ts") else "sans date",
                              ms.get("title", ""), " — %s" % progress if progress else ""), "  ")
    else:
        line("JALONS : aucun déclaré au canon")

    out.append("")
    budget = snap["budget"]
    sp = budget["spend"]
    line("BUDGET (USD)")
    line("payé au token : 1 h %.2f · 24 h %.2f · fenêtre %.2f (plafond %.2f/h)" % (
        sp["1h"]["paid_usd"], sp["24h"]["paid_usd"], sp["window"]["paid_usd"],
        budget["hourly_cap_usd"]), "  ")
    line("total estimé : 1 h %.2f · 24 h %.2f · fenêtre %.2f" % (
        sp["1h"]["total_usd"], sp["24h"]["total_usd"], sp["window"]["total_usd"]), "  ")
    for plan in budget["plans"]:
        # espace insécable avant « % » : la coupure ne l'isole jamais
        line("forfait %s %s : %.0f\u00a0%% (rythme %.0f\u00a0%%)%s" % (
            plan["harness"], plan["key"], plan["used"] * 100, plan["pace_cap"] * 100,
            " — DÉPASSÉ" if plan["exceeded"] else ""), "  ")
    if not budget["plans"]:
        line("forfaits : aucune jauge lue sur cet hôte", "  ")
    return "\n".join(out)


# --------------------------------------------------------------------------
# vue de référence : page HTML statique autonome
# --------------------------------------------------------------------------

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress_page.html")
_DATA_MARK = "/*__AMEESH_PROGRESS_DATA__*/null"


def render_html(snap: dict) -> str:
    """La page de référence avec l'instantané embarqué (aucune ressource externe).

    Le JSON est inséré dans un `<script type="application/json">` : `<`, `>`
    et `&` y sont échappés en `\\u003c`… pour qu'aucun texte (titre de lot,
    consigne) ne puisse fermer la balise ni devenir du HTML.
    """
    with open(PAGE, encoding="utf-8") as fh:
        page = fh.read()
    data = (json.dumps(snap, ensure_ascii=False, sort_keys=True)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    if _DATA_MARK not in page:
        raise ProgressError("gabarit de page sans emplacement de données")
    return page.replace(_DATA_MARK, data, 1)


def write_html(path: str, snap: dict) -> None:
    html = render_html(snap)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ProgressError("écriture impossible : %s (%s)" % (path, exc))


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh progress", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="schéma ameesh-progress/1")
    parser.add_argument("--html", metavar="FICHIER", default=None,
                        help="écrit la page statique de la frise (autonome, hors réseau)")
    parser.add_argument("--project", default=None, help="projet (app, workstream, équipe)")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help="début de la fenêtre : durée (16h, 2d) ou date ISO (défaut 24h)")
    parser.add_argument("--stale-after", default=None,
                        help="lot stagnant sans activité depuis cette durée (défaut "
                             "AMEESH_STALE_AFTER, sinon 6h)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    cfg = config_mod.load()
    try:
        parse_since(args.since, time.time())
        try:
            threshold = stagnation.stale_after(args.stale_after)
        except stagnation.StaleError as exc:
            raise ProgressError("--stale-after : %s" % exc)
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db)
            snap = snapshot(db, cfg, since=args.since, project=args.project,
                            stale_after=threshold)
        finally:
            db.close()
        if args.html:
            write_html(args.html, snap)
            if not args.json:
                print("page écrite : %s" % args.html)
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2, sort_keys=True))
        elif not args.html:
            print(format_text(snap))
        return 0
    except ProgressError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 2
    except db_mod.SchemaMissing as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
