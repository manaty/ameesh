# SPDX-License-Identifier: AGPL-3.0-only
"""Real-time progress view (lot L24, decision 0024).

  ameesh progress [--json] [--project P] [--since 24h] [--stale-after 6h] [--no-gantt]
  ameesh progress --html FILE [--project P] [--since 24h] [--stale-after 6h] [--no-gantt]
  ameesh progress --serve [--port 8765] [--project P] [--since 24h] [--stale-after 6h] [--no-gantt]

The project's current state and timeline, **fed by what ameesh records
itself** (lot transitions in `work_items`, gated actions, registry turns and
leases, accounting marker, `turn_costs` ledger) — never by git or the board:

* **projects** (L62, first): the `ameesh projects` view — per project, its
  agents, their state, their current lot, unread mail, 24 h spend, plan or
  token, and its open lots without an agent;
* **lots**: dated milestones request → freeze → verdict → merge when known,
  state (active, in review, blocked, approved, merged, closed), blocks; and
  (L29) what the lot is waiting for and from whom (`waiting_for`), whether
  it is stale (`stale`: no activity for `--stale-after`, default 6 h), its
  plan entry and its epic;
* **epics** (L29): lots grouped by plan epic, with progress (merged lots /
  total);
* **agents**: harness, model, effort, state (working, idle, paused,
  stopped), duration and task of the current turn;
* **project milestones**: the canon's `WorkPackage` entries of kind
  `milestone` (L29), with their progress; none is invented;
* **budget**: pay-per-token spend and value consumed on plans, never added
  together (L95); real spend from balances versus the estimate; input,
  cache-read and output tokens; failed turns shown separately; every window
  of every declared account.
* **roadmap** (L96, key `roadmap`, schema `ameesh-roadmap/1`): Gantt of
  epics, tasks, milestones and commitments, planned versus actual, today
  line, delays and source of each item; shown by default, `--no-gantt`
  removes it.
* **versions** (L160f, #497, key `versions`, schema `ameesh-versions/1`):
  per project and product (one per version-tag stream of its repository),
  the last published version and its date (GitHub tags and releases) and
  the next release — its open release lots, its planned date (the plan of
  the lots, otherwise the p80 of the L157 reference table), its state and
  its content (the release notes of the release branch, otherwise the lots
  merged since the last tag grouped by theme, their PR read from the
  database). GitHub is read **read-only** with the host's token
  (`GH_TOKEN`/`GITHUB_TOKEN`, `gh` only as a local fallback), cached 5 min
  and shared by `--serve`; every datum says its source and read time, an
  unreadable GitHub is said in `versions.missing` and never fails the view.
  `--no-versions` does not read it at all.

The JSON follows the versioned schema `ameesh-progress/1`
(`docs/PROGRESS.md`). `--html` writes a self-contained static page (no
network resource, no server) that renders this JSON as a timeline; `--serve`
(L304) serves that page and `/api/progress.json` locally, read-only, with a
Refresh button (`progress_serve`).

This module only reads: every database read goes through
`storage.of(db).progress`; plan gauges come from `cost.CostBook` (local
harness logs, read-only).
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
from . import estimates as estimates_mod
from . import plan as plan_mod
from . import stagnation
from . import storage
from . import versions as versions_mod
from . import work as work_mod

SCHEMA = "ameesh-progress/1"
DEFAULT_SINCE = "24h"
#: bornes de lecture : une vue, pas un export complet de la base
MAX_LOTS = 500
MAX_ACTIONS = 500
#: longueur d'une tâche affichée (consigne en cours, libellé)
TASK_CHARS = 140

#: #1476 : cible de la durée bout en bout d'une suite complète (demande du
#: bureau, 15/10/2026), en secondes, et fenêtre de la médiane affichée (jours)
#: avec sa borne de lecture par dépôt
CI_SUITE_TARGET_S = 15 * 60
CI_SUITE_WINDOW_D = 7
CI_SUITE_LIMIT = 500
CI_SUITE_SCHEMA = "ameesh-ci-suite/1"

#: états du lot rendus par la vue (décision 0024)
LOT_STATES = ("active", "review", "blocked", "approved", "merged", "closed")
#: états d'agent rendus par la vue
AGENT_STATES = ("working", "idle", "paused", "stopped")
#: `budget.PAUSE_PREFIX` (sans importer budget ici)
BUDGET_PAUSE_PREFIX = "budget : "

_FR_LOT = {"active": "active", "review": "in review", "blocked": "blocked",
           "approved": "approved", "merged": "merged", "closed": "closed"}
_FR_AGENT = {"working": "working", "idle": "idle", "paused": "paused",
             "stopped": "stopped"}

#: connecteur de fusion (L5) : son approbation vaut verdict, sa confirmation fusion
MERGE_CONNECTOR = "git-merge"
_WAITING = ("blocked", "waiting_human")

#: ce que la vue ne sait pas encore lire (dit dans le JSON, jamais deviné)
MISSING = (
    "project milestones: `milestone` WorkPackage entries of the synced canon "
    "(`ameesh canon sync`); their date comes from the entry (`date`, `delivery`) or "
    "from `ameesh work plan <entry>`; without a date, `at_ts` is null",
    "roadmap (L96): planned dates set by `ameesh work plan` or in the canon, "
    "commitments from `ameesh plan add`; a date only mentioned in conversation "
    "and never recorded is not there",
    "lot milestones: read from the lot milestone table (L10) when a freeze or "
    "a verdict is declared there, otherwise inferred from the transition log and "
    "merge actions",
    "effort: local setting on the agent's host (`ameesh set`), not in the "
    "database; null for an agent on another host or without a setting",
    "plan gauges: read from the local harness logs of this host "
    "(`ameesh cost`); their history in the database is read with `ameesh cost gauges`",
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
        raise ProgressError("unreadable --since: %r (duration 16h, 2d... or ISO date)" % text)
    if moment.tzinfo is None:
        return time.mktime(moment.timetuple()) + moment.microsecond / 1e6
    return moment.timestamp()


# --------------------------------------------------------------------------
# lots
# --------------------------------------------------------------------------

def _work_item_milestones(item: dict, events: Iterable[dict], actions: Iterable[dict],
                          declared: Iterable[dict] | None = None) -> dict:
    """Les jalons d'un lot : requested / frozen / verdict / merged / delivered.

    SEUL point de lecture des jalons de lot. Deux sources, même résultat :

    * `declared` — les lignes de la table des jalons de lot (L10,
      `work_item_milestones` : `kind`, `at_ts`, `actor`, `verdict`) ; quand
      le lot y a au moins un gel ou un verdict DÉCLARÉ, gels et verdicts
      viennent de là ; `requested`, `merged` et `delivered` (#988) en viennent
      dès qu'ils y sont ;
    * sinon, déduction depuis le journal des transitions (`work_item_events`,
      dans l'ordre d'écriture) : gel = entrée en `qa`, verdict = sortie de
      `qa` vers `merged` (ok) ou vers `build` (blocked).

    Dans les deux cas, l'approbation de l'action de fusion du lot
    (`MERGE_CONNECTOR`) compte comme un verdict `ok` (approbateur =
    relecteur) et sa fin confirmée comme une fusion si aucune n'est connue.

    Rend :

    * `requested`, `frozen` (PREMIER gel), `verdict` (verdict retenu = le
      plus récent), `merged`, `delivered` : instants ou None ;
    * `last_frozen` : le DERNIER gel (un lot regelé repasse en revue) ;
    * `verdict_kind` : `ok` | `blocked` | None, et `reviewer` : l'auteur du
      verdict courant ; sinon le relecteur désigné du lot
      (`work_items.reviewer`, #705) ; sinon None — un verdict périmé ne
      désigne personne (#1318) ;
    * `verdict_current` : le verdict retenu porte-t-il sur le dernier gel ?
      Un avis nomme sa tête (`sha`) : il reste valide tant qu'un gel — même
      renouvelé plus tard — nomme le même commit ; un avis d'une autre tête
      ne compte pas ; sans SHA des deux côtés, comparaison des instants ;
    * `verdict_required` : False quand le pilote a marqué « verdict non
      requis » (`work.REVIEWER_NONE`, #1979) — la revue n'attend plus rien ;
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
        freezes = [{"at_ts": r["at_ts"], "sha": (r.get("sha") or "").strip()}
                   for r in rows if r.get("kind") == "frozen"]
        verdicts = [{"at_ts": r["at_ts"], "kind": r.get("verdict") or "ok",
                     "actor": r.get("actor") or None, "sha": (r.get("sha") or "").strip()}
                    for r in rows if r.get("kind") == "verdict"]
    else:
        source = "events"
        freezes = [{"at_ts": at, "sha": ""} for at in ev_freezes]
        verdicts = [{"at_ts": at, "kind": kind, "actor": actor, "sha": ""}
                    for (at, kind, actor) in ev_verdicts]
    # Une source à la fois, pour des instants cohérents entre eux (le jalon
    # automatique `merged` et l'événement de la même transition diffèrent de
    # quelques microsecondes) : la table entière quand elle porte des
    # déclarations, sinon le journal, la table ne comblant que ses trous.
    auto = {}
    for r in rows:
        if (r.get("kind") in ("requested", "merged", "delivered")
                and r.get("kind") not in auto):
            auto[r["kind"]] = r["at_ts"]          # lignes en ordre chronologique
    if source == "milestones":
        requested = auto.get("requested", requested)
        merged = auto.get("merged", merged)
    else:
        requested = requested if requested is not None else auto.get("requested")
        merged = merged if merged is not None else auto.get("merged")
    # #988 : un lot livré hors cible est clos par `close --delivered` ; le jalon
    # `delivered` (sinon l'instant de fermeture) date la livraison, que `merged`
    # ne porte pas.
    delivered = auto.get("delivered")
    if delivered is None and item.get("state") == "closed" \
            and item.get("close_reason") == "delivered":
        delivered = item.get("closed_ts")

    for act in actions:
        if act.get("connector") != MERGE_CONNECTOR:
            continue
        if act.get("approved_ts"):
            verdicts.append({"at_ts": act["approved_ts"], "kind": "ok",
                             "actor": act.get("auth_approver") or act.get("last_actor") or None,
                             "sha": (act.get("sha") or "").strip()})
        if act.get("state") == "confirmed" and merged is None:
            merged = act.get("finished_ts")

    freezes.sort(key=lambda f: f["at_ts"])
    verdicts.sort(key=lambda v: v["at_ts"])
    last = verdicts[-1] if verdicts else None
    last_frozen = freezes[-1] if freezes else None
    verdict_current = _verdict_applies(last, last_frozen)
    # #705 : avant tout verdict — ou après un regel, le verdict précédent ne
    # vaut plus — le relecteur DÉSIGNÉ (`work move … qa --reviewer`) est celui
    # qu'on attend ; quand un verdict courant existe, c'est son auteur qui a
    # réellement relu. #1318 : un verdict périmé ne désigne personne — sans
    # relecteur désigné on attend « un relecteur (non désigné) », pas l'auteur
    # du verdict que le gel a rendu caduc.
    designated = (item.get("reviewer") or "").strip() or None
    reviewer = (last["actor"] if last else None) if verdict_current else designated
    # #1979 : le pilote peut dispenser de re-verdict (résolution de composition
    # mécanique, canon §11 bis/#87) en marquant la colonne du relecteur
    # (`work.waive_review`) : l'attente passe alors à la fusion, sans faux
    # « waiting: verdict » ni rappel à un relecteur.
    verdict_required = designated != work_mod.REVIEWER_NONE
    return {
        "requested": requested,
        "frozen": freezes[0]["at_ts"] if freezes else None,
        "last_frozen": last_frozen["at_ts"] if last_frozen else None,
        "verdict": last["at_ts"] if last else None,
        "verdict_kind": last["kind"] if last else None,
        "reviewer": reviewer,
        "verdict_current": verdict_current,
        "verdict_required": verdict_required,
        "merged": merged,
        "delivered": delivered,
        "blocked_verdicts": sum(1 for v in verdicts if v["kind"] == "blocked"),
        "blocks": blocks,
        "source": source,
    }


def _same_commit(a: str | None, b: str | None) -> bool:
    """Deux identifiants qui nomment le même commit (préfixe admis, casse ignorée).
    Un identifiant vide n'est jamais « le même commit »."""
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def _verdict_applies(verdict: dict | None, freeze: dict | None) -> bool:
    """Le verdict retenu porte-t-il sur la tête du dernier gel ?

    Un avis nomme sa tête (`sha`) : il reste valide tant qu'un gel — même
    renouvelé plus tard — nomme le même commit, et un avis d'une autre tête ne
    compte pas. Sans SHA des deux côtés (déduction par le journal, action de
    fusion), on retombe sur l'instant : l'avis doit suivre le dernier gel."""
    if verdict is None:
        return False
    if freeze is None:
        return True
    if _same_commit(verdict.get("sha"), freeze.get("sha")):
        return True
    if not (verdict.get("sha") and freeze.get("sha")):
        return verdict["at_ts"] >= freeze["at_ts"]
    return False


def lot_state(work_state: str, jalons: dict) -> str:
    """État affiché d'un lot (décision 0024) : en revue, il n'est approuvé
    que par un verdict `ok` qui porte sur le DERNIER gel (même SHA) — ou,
    #1979, par la marque « verdict non requis » du pilote (résolution de
    composition mécanique, canon §11 bis/#87)."""
    if work_state in ("merged", "promoted"):
        return "merged"
    if work_state == "closed":
        return "closed"
    if work_state in _WAITING:
        return "blocked"
    if work_state == "qa":
        current = jalons.get("verdict_current")
        if current is None:                 # jalons partiels : repli par les instants
            last_frozen = jalons.get("last_frozen", jalons.get("frozen")) or 0
            current = (jalons.get("verdict") or 0) >= last_frozen
        # #1979 : dispensé de re-verdict, le lot n'est pas « en revue » : il
        # est prêt à fusionner, comme après un verdict `ok` courant. Un verdict
        # bloquant COURANT garde la main (une correction est due), comme dans
        # `stagnation.waiting`.
        if not (jalons.get("verdict_kind") == "blocked" and current) \
                and jalons.get("verdict_required", True) is False:
            return "approved"
        if jalons.get("verdict_kind") == "ok" and current:
            return "approved"
        return "review"
    return "active"


def _declared_milestones(st, ids: list[int]) -> dict:
    """Les jalons de ces lots dans la table de L10 (`work_item_milestones`),
    par lot ; `_work_item_milestones` les préfère à la déduction dès qu'un gel ou un
    verdict y est déclaré."""
    out: dict = {}
    for row in st.lot_milestones(ids):
        out.setdefault(int(row["work_item_id"]), []).append(row)
    return out


#: L148 (stage 2c): old spellings kept for the callers not yet renamed.
_jalons_de_lot = _work_item_milestones
_jalons_declares = _declared_milestones


def _round(ts):
    return None if ts is None else round(float(ts), 3)


def build_lot(item: dict, events: list[dict], actions: list[dict],
              declared: list[dict] | None = None, *, packages: dict | None = None,
              now: float | None = None,
              stale_after: float = stagnation.DEFAULT_THRESHOLD_S,
              messages_ts: float | None = None,
              events_ts: float | None = None,
              planned_wait: dict | None = None,
              dependencies: list[dict] | None = None,
              agent_states: dict[str, str] | None = None,
              agent_externals: dict[str, str | None] | None = None) -> dict:
    jalons = _work_item_milestones(item, events, actions, declared)
    packages = packages or {}
    package = packages.get(item.get("package_id"))
    closed_at = next((r.get("at_ts") for r in (declared or []) if r.get("kind") == "closed"),
                     None)
    # `events` peut ne porter que les transitions (#1647) : `events_ts` rend
    # le dernier instant du journal, notes comprises.
    last = stagnation.last_activity(item, events, declared or [], actions, messages_ts,
                                    events_ts=events_ts)
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
        # #1979 : une revue dispensée n'a pas de relecteur — l'état `approved`
        # le dit ; le jeton de la colonne ne sort pas dans la vue.
        "reviewer": (None if jalons["reviewer"] == work_mod.REVIEWER_NONE
                     else jalons["reviewer"]),
        "milestones": {k: _round(jalons[k])
                       for k in ("requested", "frozen", "verdict", "merged", "delivered")},
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
        "waiting_for": stagnation.with_availability(
            stagnation.waiting(item, jalons, actions,
                               responsible=(package or {}).get("responsible")),
            agent_states, agent_externals),
        "last_activity_ts": _round(last),
        "stale": None if planned_wait else
                 stagnation.stale(item.get("state") or "", last, now, stale_after),
        "planned_wait": planned_wait,
        "dependencies": dependencies or [],
        # L40 (0030) : délégation à échéance (champ ajouté)
        "delegation": work_mod.delegation_view(item, now),
        # L157 : durée estimée, réel mesuré (début → fusion ou livraison hors
        # cible, #988), écart (champ ajouté)
        "estimate": estimates_mod.view(
            item, merged_ts=jalons.get("merged") or jalons.get("delivered"), now=now),
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
    # L242b (4) : une pause budget se lit « paused », quel que soit le statut
    # (jamais « stopped » faute de bail pendant la pause)
    if status not in ("stopped", "dead") \
            and (row.get("status_text") or "").startswith(BUDGET_PAUSE_PREFIX):
        return "paused"
    if status == "running":
        return "working" if row.get("lease_live") else "stopped"
    if row.get("operator_paused") and status not in ("stopped", "dead"):
        return "paused"
    if status == "blocked":
        return "paused"                 # pause budget ou comptable (L13)
    if status in ("stopped", "dead"):
        return "stopped"
    return "idle"                       # idle, queued


def paused_on_budget(row: dict) -> bool:
    """#1070 : l'agent est en pause BUDGET — la pause annoncée par son
    exécuteur (`status` `blocked`, texte « budget : … »).

    Distincte d'un `blocked` d'une autre cause (hôte non prêt,
    authentification), que `agent_state` range lui aussi dans « paused » :
    un bilan qui compte les agents en pause budget lit ce prédicat, pas le
    préfixe d'un texte tronqué à l'affichage. La règle est celle de
    `handoff.paused` (L233b), la seule définition de la pause budget : on la
    reprend ici au lieu d'en entretenir une seconde."""
    from . import handoff
    return handoff.paused(row)


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
        "project": (row.get("team") or row.get("workstream")
                    or row.get("chantier") or None),
        "state": state,
        "status": row.get("status") or None,
        "status_text": _excerpt(row.get("status_text"), 160),
        # #1070 : la pause budget se distingue d'un `blocked` d'une autre cause
        # sans déchiffrer le texte
        "paused_on_budget": paused_on_budget(row),
        "since_ts": _round(since),
        "turn": turn,
        "turns": int(row.get("turns") or 0),
        "unread": int(row.get("unread") or 0),
        "pending_prompt": bool(row.get("has_pending_prompt")),
        "lease_live": bool(row.get("lease_live")),
        # L37 (0030) : mode et raison d'arrêt structurée
        "mode": row.get("mode") or "execute",
        "stop_reason": row.get("stop_reason") if state == "stopped" else None,
        "operator_pause": ({
            "reason": row.get("operator_pause_reason"),
            "by": row.get("operator_pause_by"),
            "for": row.get("operator_pause_for"),
            "since_ts": _round(row.get("operator_pause_ts")),
            "after_turn": state == "working",
        } if row.get("operator_paused") else None),
    }


# --------------------------------------------------------------------------
# budget
# --------------------------------------------------------------------------

def _effective_pace(gauges: list, accounts: list[dict] | None) -> dict:
    """{(harnais, fenêtre) : (multiplicateur, plafond effectif)} (#1227 N3).

    `accounts.report` porte le plafond effectif par compte (#1176) ;
    `book.gauges()` ne nomme pas le compte de son relevé. On ne s'en sert donc
    que quand un seul compte déclaré du harnais porte la fenêtre : plusieurs
    comptes, ou aucune information, gardent le défaut (90 %, ×1) — jamais un
    plafond attribué au hasard.
    """
    keys = {(g.harness, g.key) for g in gauges}
    found: dict = {}
    for row in accounts or []:
        harness = row.get("harness")
        multiplier = float(row.get("pace_multiplier") or 1.0)
        for gauge in row.get("gauges") or []:
            key = (harness, gauge.get("key"))
            if key not in keys:
                continue
            ceiling = float(gauge.get("pace_ceiling") or cost_mod.PACE_CEILING)
            if key in found and found[key] != (multiplier, ceiling):
                found[key] = None            # plusieurs comptes : ambigu
            else:
                found.setdefault(key, (multiplier, ceiling))
    return found


def build_budget(cost_rows: list[dict], gauges: list, now: float, *,
                 paid_harnesses: Iterable[str] | None = None,
                 hourly_cap: float = cost_mod.DEFAULT_HOURLY_USD,
                 real: list[dict] | None = None,
                 accounts: list[dict] | None = None) -> dict:
    """La section `budget` de l'instantané.

    L95 : la dépense **payée au token** (`paid_usd`, de vrais dollars) et la
    **valeur consommée sur les forfaits** (`plan_value_usd`, équivalent
    théorique au barème, qui ne coûte rien de plus par tour) ne s'additionnent
    plus pour l'affichage ; `total_usd` reste dans le JSON pour les lecteurs
    d'avant. `real` : dépense réelle tirée des soldes, face à l'estimation
    (`balance.compare`). `accounts` : tous les comptes déclarés de l'hôte,
    toutes leurs fenêtres, la capacité perdue à la prochaine remise à zéro et
    le prochain compte choisi (`accounts.report`, L74).

    `plans` : un plan par jauge. Pour un harnais **sans** compte déclaré, la
    jauge vient de `gauges` et le rythme est nominal (1.0) ; pour un harnais à
    comptes déclarés, le plan est celui du compte **actif** — sa jauge
    (`accounts.report`, `_gauges`) et son `pace_multiplier`, jamais un
    croisement des deux (#1028 P2). Le rendu texte écarte ces harnais, déjà
    montrés compte par compte.
    """
    # Les harnais payés au token viennent des descripteurs (L16) : plus de liste
    # fermée dans le code. `None` = les relire maintenant.
    paid = cost_mod.paid_harnesses_of() if paid_harnesses is None else tuple(paid_harnesses)
    spend = {key: {"total_usd": 0.0, "paid_usd": 0.0, "plan_value_usd": 0.0}
             for key in ("window", "1h", "24h")}
    by_agent = []
    for row in cost_rows:
        is_paid = (row.get("harness") or "") in paid
        for key, col in (("window", "usd_window"), ("1h", "usd_1h"), ("24h", "usd_24h")):
            value = float(row.get(col) or 0.0)
            spend[key]["total_usd"] += value
            spend[key]["paid_usd" if is_paid else "plan_value_usd"] += value
        turns = int(row.get("turns") or 0)
        failed = int(row.get("failed_turns") or 0)
        inputs = int(row.get("input_tokens") or 0)
        cached = int(row.get("cached_input_tokens") or 0)
        by_agent.append({
            "agent": row["agent"], "harness": row.get("harness"),
            "model": row.get("model") or None, "paid": is_paid,
            "usd": round(float(row.get("usd_window") or 0.0), 6),
            # L95 : un tour sans aucun usage (échec du harnais, tour
            # interrompu) n'est pas du travail : compté à part
            "turns": turns - failed,
            "failed_turns": failed,
            "input_tokens": inputs,
            "cached_input_tokens": cached,
            # entrée hors cache : Codex compte le cache DANS `input_tokens`,
            # les autres harnais à part (`TurnUsage.reread_tokens`)
            "fresh_input_tokens": (max(inputs - cached, 0)
                                   if row.get("harness") == "codex" else inputs),
            "output_tokens": int(row.get("output_tokens") or 0),
        })
    for bucket in spend.values():
        for key in bucket:
            bucket[key] = round(bucket[key], 6)
    # #1075/#1028 : le plan d'un harnais à comptes déclarés est celui de son
    # compte ACTIF — ses jauges (`accounts.report`, `_gauges`) et son rythme
    # (`pace_multiplier` du profil). Appliquer le rythme du compte actif aux
    # jauges du dossier par défaut mélangerait deux comptes : `used`,
    # `pace_cap` et `exceeded` ne décriraient plus aucun compte (R1 #1028,
    # P2). Le rendu texte écarte ces harnais, déjà montrés compte par compte ;
    # le JSON garde la même valeur qu'`accounts.report`.
    # #1227 N3 : le plafond effectif du compte (#1176, relèvement de fin de
    # fenêtre compris) accompagne son rythme ; les jauges du dossier par
    # défaut ne reçoivent le réglage déclaré que s'il n'est pas ambigu (un
    # seul compte porte la fenêtre) — jamais un plafond attribué au hasard.
    actifs = {row["harness"]: row for row in (accounts or []) if row.get("active")}
    settings = _effective_pace(gauges, accounts)
    plans = []
    for g in gauges:
        if g.harness in actifs:
            continue                    # rendu compte par compte ci-dessous
        multiplier, ceiling = (settings.get((g.harness, g.key))
                               or (1.0, cost_mod.PACE_CEILING))
        plans.append(_plan_view(g, now, multiplier, ceiling))
    for harness, row in actifs.items():
        multiplicateur = float(row.get("pace_multiplier") or 1.0)
        ceilings = {g.get("key"): float(g.get("pace_ceiling") or cost_mod.PACE_CEILING)
                    for g in row.get("gauges") or []}
        plans.extend(_plan_view(g, now, multiplicateur,
                                ceilings.get(g.key, cost_mod.PACE_CEILING))
                     for g in row.get("_gauges") or [])
    return {
        "currency": "USD",
        "paid_harnesses": list(paid),
        "hourly_cap_usd": float(hourly_cap),
        "spend": spend,
        "real": list(real or []),
        "by_agent": [r for r in by_agent if r["usd"] or r["turns"] or r["failed_turns"]],
        "plans": plans,
        "accounts": [_account_view(row) for row in (accounts or [])],
    }


def _plan_view(g: cost_mod.Gauge, now: float, multiplier: float,
               ceiling: float = cost_mod.PACE_CEILING) -> dict:
    """Un plan de `budget.plans` : la jauge et le rythme du même compte."""
    return {
        "harness": g.harness, "key": g.key, "used": round(g.used_at(now), 4),
        "pace_cap": round(g.pace_cap(now, multiplier, ceiling), 4),
        "elapsed": round(g.elapsed(now), 4),
        "resets_ts": _round(g.resets_at), "window_s": g.window_s,
        # #1561 : quand le relevé a été produit (None : source non datée)
        "read_at": _round(getattr(g, "read_at", None)),
        "exceeded": g.exceeded(now, multiplier=multiplier, ceiling=ceiling),
    }


def _account_view(row: dict) -> dict:
    """Un compte de `accounts.report` pour l'instantané (aucun secret : le
    type et l'emplacement seulement)."""
    return {
        "harness": row["harness"], "account": row["account"],
        "active": bool(row.get("active")), "forced": bool(row.get("forced")),
        "ok": bool(row.get("ok")), "reason": row.get("reason") or "",
        "next": bool(row.get("next")), "why": row.get("why") or "",
        # #1561 : relevé périmé (non vérifié) ; ni perte ni échéance annoncées
        "stale": bool(row.get("stale")),
        "expires_in_s": row.get("expires_in_s"),
        "gauges": [{
            "key": g["key"], "used": round(float(g["used"]), 4),
            "last_used": round(float(g.get("last_used", g["used"])), 4),
            "pace_cap": round(float(g["cap"]), 4), "elapsed": round(float(g["elapsed"]), 4),
            "resets_ts": _round(g.get("resets_at")),
            "reset_passed": bool(g.get("reset_passed")),
            "read_at": _round(g.get("read_at")),
        } for g in row.get("gauges") or []],
        "losses": [{"key": l["key"], "resets_ts": _round(l["resets_at"]),
                    "in_s": int(l["in_s"]), "lost": round(float(l["lost"]), 4)}
                   for l in row.get("losses") or []],
    }


def _real_spend(db, paid: Iterable[str], since_ts: float, now: float) -> list[dict]:
    """Dépense réelle (soldes) face à l'estimation, sur la fenêtre et sur 24 h
    (L95). Un relevé illisible n'empêche jamais l'instantané."""
    from . import balance as balance_mod
    paid = tuple(paid)
    try:
        start = min(since_ts, now - 86400.0)
        rows = storage.of(db).operations.balances(provider=None,
                                                  since_s=max(0.0, now - start) + 1.0)
        out = []
        periods = [("24h", now - 86400.0)]
        if abs(since_ts - (now - 86400.0)) > 60.0:
            periods.insert(0, ("window", since_ts))   # fenêtre autre que 24 h
        for key, begin in periods:
            for entry in balance_mod.compare(db, paid, begin, now, rows=rows):
                out.append(dict(entry, period=key))
        return out
    except db_mod.DbError:
        raise
    except (KeyError, TypeError, ValueError):
        return []


# --------------------------------------------------------------------------
# l'instantané
# --------------------------------------------------------------------------

def _ci_priority(row: dict) -> bool:
    """#1468 : le pool prioritaire de runners couvre `integration/**` et
    `release/**` (source ou cible d'une PR) ; une poussée sur `main` suit la
    fusion d'un paquet déjà validé sur sa branche d'intégration et n'est pas
    le chemin critique de la cible ; une exécution manuelle sur `main` non
    plus (elle ne prend pas le pool prioritaire)."""
    return any((row.get(ref) or "").startswith(("release/", "integration/"))
               for ref in ("branch", "base"))


def build_ci_suite(rows: list[dict]) -> dict:
    """#1476 : durée médiane (et p80) des suites complètes conclues, par
    dépôt, et la part couverte par le pool prioritaire.

    La durée est celle du veilleur CI (`ci_runs`) : du premier relevé de
    l'exécution à celui de sa conclusion. L'instant de création GitHub n'est
    pas gardé ; l'écart est borné par l'intervalle de `ameesh notify`, et il
    porte des deux côtés, donc la médiane reste juste à un relevé près.
    """
    by_repo: dict = {}
    for row in rows:
        by_repo.setdefault(row.get("repo") or "?", []).append(row)
    repos = []
    for repo, runs in sorted(by_repo.items()):
        durees = [r["duration_s"] for r in runs if r.get("duration_s") is not None]
        prio = [r["duration_s"] for r in runs
                if r.get("duration_s") is not None and _ci_priority(r)]
        last = max(runs, key=lambda r: (r.get("concluded_ts") or 0, r.get("run_id") or 0))
        repos.append({
            "repo": repo,
            "runs": len(durees),
            "median_s": estimates_mod.quantile(durees, 0.5),
            "p80_s": estimates_mod.quantile(durees, 0.8),
            "last_s": last.get("duration_s"),
            "last_concluded_ts": last.get("concluded_ts"),
            "priority_runs": len(prio),
            "priority_median_s": estimates_mod.quantile(prio, 0.5),
        })
    return {"schema": CI_SUITE_SCHEMA, "window_days": CI_SUITE_WINDOW_D,
            "target_s": CI_SUITE_TARGET_S, "repos": repos}


def _truncation(rows: list[dict], limit: int) -> dict | None:
    """`{shown, total, limit}` si la borne de rendu a coupé, sinon None."""
    total = int(rows[0].get("total") or len(rows)) if rows else 0
    if total <= len(rows):
        return None
    return {"shown": len(rows), "total": total, "limit": int(limit)}


def milestone_projects(packages: Iterable[dict], items: Iterable[dict]) -> dict:
    """L304 : les projets (`app`) des lots rattachés sous chaque jalon du plan,
    tous lots confondus (la fenêtre n'y change rien)."""
    by_id = {p["id"]: p for p in packages}
    out: dict = {}
    for item in items:
        ms = plan_mod.milestone_of(item.get("package_id"), by_id)
        if ms is not None and item.get("app"):
            out.setdefault(ms, set()).add(item["app"])
    return out


def build_overview(projects: list[dict], lots: list[dict], milestones: list[dict],
                   ms_projects: dict, decisions: list[dict]) -> list[dict]:
    """L304 : la carte de chaque projet, dans l'ordre de `projects` (actifs
    d'abord). Son jalon est le prochain jalon non atteint qui porte un de ses
    lots (le plus tôt daté, les non datés après) ; aucun : null."""
    cards = []
    for p in projects:
        name = p.get("name")
        open_ms = [m for m in milestones if name in ms_projects.get(m["id"], ())
                   and m.get("progress") != 1]
        open_ms.sort(key=lambda m: (m.get("date") is None, m.get("date") or "", m["id"]))
        ms = open_ms[0] if open_ms else None
        cards.append({
            "project": name,
            "active": bool(p.get("active")),
            "lots_open": int(p.get("lots_open") or 0),
            "lots_merged_in_window": sum(1 for l in lots if l.get("project") == name
                                         and l.get("state") == "merged"),
            "agents_active": int(p.get("agents_active") or 0),
            "agents_working": int((p.get("counts") or {}).get("working") or 0),
            "spend_24h_usd": round(float(p.get("usd_24h") or 0.0), 2),
            "decisions_pending": sum(1 for d in decisions if d.get("project") == name),
            "milestone": ({k: ms.get(k) for k in ("id", "title", "date", "progress",
                                                  "lots_merged", "lots_total")}
                          if ms else None),
            "warnings": list(p.get("warnings") or []),
        })
    return cards


def snapshot(db, cfg=None, *, since: str | None = None, project: str | None = None,
             book: cost_mod.CostBook | None = None, now: float | None = None,
             max_lots: int = MAX_LOTS, max_actions: int = MAX_ACTIONS,
             stale_after: float | None = None, gantt: bool = True,
             github: "versions_mod.Reader | None" = None) -> dict:
    """L'instantané `ameesh-progress/1` (voir docs/PROGRESS.md).

    Les bornes (`max_lots`, `max_actions`) ne limitent que le RENDU : les
    lots ouverts passent avant les fusionnés, et les jalons et l'état d'un
    lot affiché sont lus sans borne (son journal complet, toutes ses
    actions). Toute coupure est dite dans `truncated`.

    `github` (L160f) : le lecteur GitHub des versions, partagé par `--serve`
    (cache) ; None : les versions ne sont pas lues, la vue le dit.
    """
    now = time.time() if now is None else float(now)
    since_ts = parse_since(since, now)
    store = storage.of(db)
    st = store.progress
    items = st.lots(since_ts=since_ts, project=project, limit=max_lots)
    items.sort(key=lambda i: (i.get("created_ts") or 0, int(i["id"])))
    ids = [int(i["id"]) for i in items]
    events: dict = {}
    # #1647 : transitions seules ; le dernier instant du journal vient d'une
    # agrégation, jamais de la lecture des notes.
    for ev in st.lot_transitions(ids):
        events.setdefault(int(ev["work_item_id"]), []).append(ev)
    last_events = {int(r["work_item_id"]): float(r["last_ts"])
                   for r in st.lot_event_last(ids)}
    by_item: dict = {}
    for row in st.lot_actions(ids):
        by_item.setdefault(int(row["work_item"]), []).append(row)
    declared = _declared_milestones(st, ids)
    package_rows = st.packages()
    packages = {p["id"]: p for p in package_rows}
    threshold = stagnation.stale_after() if stale_after is None else float(stale_after)
    messages = {int(r["work_item_id"]): r.get("last_ts") for r in st.lot_messages(ids)}
    states, externals = stagnation.availability_of(st.agents(None))
    from . import planned_waits
    from . import work_dependencies
    dependencies = work_dependencies.grouped(db, ids)
    holds = planned_waits.read(db, items, now=now, packages=packages,
                              dependencies=[e for edges in dependencies.values() for e in edges])
    lots = [build_lot(item, events.get(int(item["id"]), []),
                      by_item.get(int(item["id"]), []), declared.get(int(item["id"])),
                      packages=packages, now=now, stale_after=threshold,
                      messages_ts=messages.get(int(item["id"])),
                      events_ts=last_events.get(int(item["id"])), agent_states=states,
                      planned_wait=holds.get(int(item["id"])),
                      dependencies=dependencies.get(int(item["id"]), []),
                      agent_externals=externals)
            for item in items]
    # #1822 : un lot léger sans verdict courant attend la fusion, pas un
    # verdict ; la classe n'est lue — une fois par passage — que pour ces lots.
    from . import lot_class
    classes: dict = {}
    for lot, item in zip(lots, items):
        if (lot.get("waiting_for") or {}).get("what") != "verdict":
            continue
        cls = stagnation.review_class_of(db, item, cfg=cfg, memo=classes,
                                         milestones=declared.get(int(item["id"]), []))
        if cls != lot_class.LIGHT:
            continue
        rid = int(item["id"])
        acts = by_item.get(rid, [])
        jalons = _work_item_milestones(item, events.get(rid, []), acts, declared.get(rid))
        lot["waiting_for"] = stagnation.with_availability(
            stagnation.waiting(item, jalons, acts,
                               responsible=(packages.get(item.get("package_id"))
                                            or {}).get("responsible"),
                               review_class=cls),
            states, externals)
    package_items = st.package_items()
    plan = plan_mod.summarize(package_rows, package_items)
    action_rows = st.actions(since_ts=since_ts, project=project, limit=max_actions)
    truncated = {}
    for key, rows, limit in (("lots", items, max_lots), ("actions", action_rows, max_actions)):
        cut = _truncation(rows, limit)
        if cut:
            truncated[key] = cut
    action_rows.sort(key=lambda r: (r.get("created_ts") or 0, r["action_id"]))

    agent_rows = st.agents(project)
    agents = [build_agent(r, now, read_effort(cfg, r)) for r in agent_rows]
    from . import conversation
    conversation.attach_progress(db, agents)
    cost_rows = st.costs(since_ts=since_ts,
                         agents=[r["name"] for r in agent_rows] if project else None)
    if book is None:
        book = cost_mod.CostBook(state_dir=cfg.state_dir if cfg else None, db=db)
    try:
        # lecture seule (L61) : l'historique des jauges est tenu par l'exécuteur
        gauges = book.gauges(record=False)
    except (OSError, ValueError):
        gauges = []
    # L95 : tous les comptes déclarés, toutes leurs fenêtres (lecture seule)
    accounts: list = []
    if cfg is not None:
        from . import accounts as accounts_mod
        try:
            accounts = accounts_mod.report(cfg, db, book, now=now, record=False)
        except accounts_mod.AccountError:
            accounts = []
    paid = cost_mod.paid_harnesses_of()
    real = _real_spend(db, paid, since_ts, now)
    # L70 : plafonds en vigueur (base > configuration de l'hôte > défaut)
    limits = None
    if cfg is not None:
        from . import budget as budget_mod
        limits = budget_mod.current(cfg, db).as_dict()
        hourly = limits["per_hour_usd"] or 0.0
    else:
        hourly = cost_mod.DEFAULT_HOURLY_USD
    # L62 : la vue par projet en tête (une requête de plus, agrégée)
    from . import projects as projects_mod
    by_project = projects_mod.snapshot(db, project=project, now=now)
    # L96 : la feuille de route en Gantt (prévu face au réel), affichée par
    # défaut ; `--no-gantt` la retire
    roadmap = None
    if gantt:
        from . import roadmap as roadmap_mod
        roadmap = roadmap_mod.build(db, now=now, project=project)
    # L304 : les demandes de décision en attente, et la carte de chaque projet
    from . import decisions as decisions_mod
    pending = decisions_mod.pending(db, now)
    if project:
        pending = [d for d in pending if d.get("project") == project]
    overview = build_overview(by_project["projects"], lots, plan["milestones"],
                              milestone_projects(package_rows, package_items), pending)
    # L160f (#497) : les versions des produits et leurs prochaines releases,
    # lues en lecture seule sur GitHub ; jamais une erreur, un manque est dit
    try:
        version_view = versions_mod.collect(store, db, cfg, project=project,
                                            reader=github, now=now, packages=packages,
                                            package_items=package_items)
    except Exception as exc:  # noqa: BLE001 - une vue ne tombe pas pour GitHub
        version_view = versions_mod.empty(
            "versions: unreadable (%s)" % " ".join(str(exc).split())[:200])

    # #1476 : la durée médiane des suites complètes, par dépôt (veilleur CI) ;
    # la cible de 15 min est portée par la vue, jamais inventée.
    ci_suite = build_ci_suite(
        st.ci_suite(since_ts=now - CI_SUITE_WINDOW_D * 86400.0, limit=CI_SUITE_LIMIT))

    return {
        "schema": SCHEMA,
        "generated_ts": _round(now),
        "generated_at": _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
                           .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "host": getattr(cfg, "host", None),
        "project": project or None,
        "window": {"from_ts": _round(since_ts), "to_ts": _round(now)},
        # L62 (champ ajouté) : projets, agents et lot en cours, lots sans agent
        "projects": by_project["projects"],
        # L96 (champ ajouté) : feuille de route, schéma ameesh-roadmap/1 ; null
        # avec --no-gantt
        "roadmap": roadmap,
        # L304 (champs ajoutés) : une carte par projet, décisions en attente
        "overview": overview,
        "decisions": pending,
        # L160f (#497) : versions publiées et prochaines releases par produit
        "versions": version_view,
        # #1476 (champ ajouté) : durée médiane des suites complètes, cible 15 min
        "ci_suite": ci_suite,
        "lots": lots,
        "agents": agents,
        "milestones": plan["milestones"],
        "epics": plan["epics"],
        "stale_after_s": int(threshold),
        "actions": [build_action(r) for r in action_rows],
        "budget": dict(build_budget(cost_rows, gauges, now, hourly_cap=hourly,
                                    paid_harnesses=paid, real=real, accounts=accounts),
                       limits=limits),
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
    return "%d d %d h" % (seconds // 86400, (seconds % 86400) // 3600)


def _ci_suite_lines(ci: dict, width: int) -> list[str]:
    """#1476 : la durée médiane des suites complètes, par dépôt, face à la
    cible ; la part couverte par le pool prioritaire de #1468 est dite."""
    target = int(ci.get("target_s") or CI_SUITE_TARGET_S)
    out = [textwrap.fill("CI FULL SUITE (median over %d d, target ≤ %s)" % (
        int(ci.get("window_days") or CI_SUITE_WINDOW_D), _duration(target)), width)]
    for r in ci.get("repos", []):
        if not r.get("runs"):
            out.append(textwrap.fill("%s: no concluded full suite" % r["repo"], width,
                                     subsequent_indent="  "))
            continue
        if r["median_s"] <= target:
            verdict = "target met"
        elif r.get("priority_median_s") is not None and r["priority_median_s"] <= target:
            verdict = "target met on the priority pool only"
        else:
            verdict = "target not met"
        out.append(textwrap.fill(
            "%s: median %s · p80 %s (%d runs, %s)" % (
                r["repo"], _duration(r["median_s"]), _duration(r.get("p80_s")),
                r["runs"], verdict), width, subsequent_indent="  "))
        if r.get("priority_runs"):
            out.append(textwrap.fill(
                "priority pool (integration/release): median %s over %d runs" % (
                    _duration(r.get("priority_median_s")), r["priority_runs"]),
                width, initial_indent="  ", subsequent_indent="    "))
    return out


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
    line("progress%s — %s (window %s)" % (
        " of %s" % snap["project"] if snap.get("project") else "",
        _hm(now, now), _duration(hours * 3600)))

    if "projects" in snap:
        # L62 : qui travaille sur quoi, avant le détail des lots
        from . import projects as projects_mod
        out.append("")
        line("PROJECTS (%d active)" % sum(1 for p in snap["projects"] if p["active"]))
        out.extend(projects_mod.format_lines(
            {"generated_ts": now, "projects": snap["projects"]}, width,
            show_inactive=bool(snap.get("project")), indent="  "))

    if snap.get("versions"):
        # L160f (#497) : dernières versions publiées et prochaines releases
        out.append("")
        out.extend(versions_mod.format_lines(snap["versions"], width))

    if (snap.get("ci_suite") or {}).get("repos"):
        # #1476 : durée médiane des suites complètes, cible de 15 min
        out.append("")
        out.extend(_ci_suite_lines(snap["ci_suite"], width))

    if snap.get("roadmap"):
        # L96 : la feuille de route (Gantt texte), avant le détail des lots
        from . import roadmap as roadmap_mod
        out.append("")
        out.extend(roadmap_mod.format_gantt(snap["roadmap"], width).splitlines())

    out.append("")
    line("LOTS (%d)" % len(snap["lots"]))
    for key, label in (("lots", "lots"), ("actions", "actions")):
        cut = (snap.get("truncated") or {}).get(key)
        if cut:
            line("TRUNCATED: %d of %d %s shown (limit %d)" % (
                cut["shown"], cut["total"], label, cut["limit"]), "  ")
    if not snap["lots"]:
        line("no lot in the window", "  ")
    for lot in snap["lots"]:
        m = lot["milestones"]
        state = _FR_LOT.get(lot["state"], lot["state"])
        stale = lot.get("stale")
        if stale:
            # un lot sans activité n'est pas dessiné comme actif
            state = "STALE %s (%s)" % (_duration(stale["idle_s"]), state)
        closed = lot.get("closed")
        if closed:
            state = ("delivered outside the target" if closed.get("reason") == "delivered"
                     else "abandoned" if closed.get("reason") == "abandoned"
                     else "superseded by #%s" % closed.get("superseded_by"))
        line("#%s %s — %s%s" % (lot["id"], state, lot["title"],
                                " [%s]" % lot["epic"] if lot.get("epic") else ""), "  ")
        line("requested %s · frozen %s · verdict %s%s · merged %s%s" % (
            _hm(m["requested"], now), _hm(m["frozen"], now), _hm(m["verdict"], now),
            " (%s)" % lot["verdict"] if lot["verdict"] else "", _hm(m["merged"], now),
            " · delivered %s" % _hm(m["delivered"], now) if m.get("delivered") else ""),
            "    ")
        extra = []
        if lot["assignee"]:
            extra.append("author %s" % lot["assignee"])
        if lot["reviewer"]:
            extra.append("reviewer %s" % lot["reviewer"])
        if lot["blocked_verdicts"] or lot["blocks"]:
            extra.append("blocks %d" % ((lot["blocked_verdicts"] or 0) + lot["blocks"]))
        if extra:
            line(" · ".join(extra), "    ")
        if lot.get("planned_wait"):
            from .work_dependencies import wait_label
            line(wait_label(lot["planned_wait"]), "    ")
        if lot.get("waiting_for"):
            line("waiting for: %s" % lot["waiting_for"]["label"], "    ")
        if lot.get("delegation"):
            line(lot["delegation"]["label"], "    ")
        est = lot.get("estimate")
        if est and (est.get("minutes") or est.get("actual_minutes") is not None):
            line("duration: %s" % est["label"], "    ")

    epics = snap.get("epics") or []
    if epics:
        out.append("")
        line("EPICS (%d)" % len(epics))
        for epic in epics:
            counted = epic["lots_total"] - epic["lots_abandoned"]
            line("%s — %s: %d/%d lots merged%s" % (
                epic["id"], epic["title"], epic["lots_merged"], counted,
                " (%d abandoned)" % epic["lots_abandoned"] if epic["lots_abandoned"] else ""),
                "  ")

    out.append("")
    line("AGENTS (%d)" % len(snap["agents"]))
    if not snap["agents"]:
        line("no agent", "  ")
    for agent in snap["agents"]:
        since = agent["since_ts"]
        line("%s — %s%s" % (agent["name"], _FR_AGENT.get(agent["state"], agent["state"]),
                            " for %s" % _duration(now - since) if since else ""), "  ")
        line("%s · %s · effort %s" % (agent["harness"] or "?", agent["model"] or "default",
                                      agent["effort"] or "default"), "    ")
        turn = agent.get("turn")
        if turn:
            line("turn: %s" % (turn["task"] or turn["label"] or "running"), "    ")
        elif agent["state"] in ("paused", "stopped") and agent["status_text"]:
            line("reason: %s" % agent["status_text"], "    ")
        hold = agent.get("operator_pause")
        if hold:
            line("operator pause%s: %s (by %s for %s)" % (
                " after this turn" if hold["after_turn"] else "",
                hold["reason"], hold["by"], hold["for"]), "    ")

    out.append("")
    if snap["milestones"]:
        line("MILESTONES (%d)" % len(snap["milestones"]))
        for ms in snap["milestones"]:
            progress = ("%d/%d lots merged" % (
                ms["lots_merged"], ms["lots_total"] - ms.get("lots_abandoned", 0))
                if "lots_total" in ms else "")
            line("%s %s%s" % (ms["date"] if ms.get("date") else "no date",
                              ms.get("title", ""), " — %s" % progress if progress else ""), "  ")
    else:
        line("MILESTONES: none declared in the canon")

    out.append("")
    budget = snap["budget"]
    sp = budget["spend"]
    line("BUDGET (USD)")
    line("spent (pay-per-token, estimated): 1 h %.2f · 24 h %.2f · window %.2f "
         "(cap %.2f/h)" % (
        sp["1h"]["paid_usd"], sp["24h"]["paid_usd"], sp["window"]["paid_usd"],
        budget["hourly_cap_usd"]), "  ")
    lim = budget.get("limits")
    if lim:
        # L70 : plafonds en vigueur et leur source (`ameesh budget`)
        line("caps: %s/h (%s) · %s/day (%s)%s" % (
            "%.2f" % lim["per_hour_usd"] if lim.get("per_hour_usd") else "none",
            lim["per_hour_source"],
            "%.2f" % lim["per_day_usd"] if lim.get("per_day_usd") else "none",
            lim["per_day_source"],
            " · %d agent(s) with their own cap" % len(lim["agents"]) if lim["agents"] else ""),
            "  ")
    # L95 : la valeur des forfaits n'est pas une dépense, jamais additionnée
    line("value consumed on plans (list-price equivalent, not paid per "
         "turn): 1 h %.2f · 24 h %.2f · window %.2f" % (
             sp["1h"].get("plan_value_usd", 0.0), sp["24h"].get("plan_value_usd", 0.0),
             sp["window"].get("plan_value_usd", 0.0)), "  ")
    from . import balance as balance_mod
    for entry in budget.get("real") or []:
        line(balance_mod.describe(entry), "  ")
    for row in budget.get("by_agent") or []:
        line("%s (%s%s): %d turn(s)%s · input %s · cache read %s · output %s · $%.2f" % (
            row["agent"], row["harness"] or "?", ", paid" if row["paid"] else "",
            row["turns"], " + %d failed" % row["failed_turns"] if row.get("failed_turns")
            else "", _tokens(row.get("fresh_input_tokens", row["input_tokens"])),
            _tokens(row["cached_input_tokens"]), _tokens(row["output_tokens"]),
            row["usd"]), "  ")
    covered = {a["harness"] for a in budget.get("accounts") or []}
    for acc in budget.get("accounts") or []:
        line("account %s/%s%s%s: %s" % (
            acc["harness"], acc["account"], " (active)" if acc["active"] else "",
            " — next choice" if acc["next"] else "",
            ", ".join(_gauge_text(g) for g in acc["gauges"]) or "no gauge read"), "  ")
        if acc["reason"]:
            line("↳ %s" % acc["reason"], "    ")
        if acc["losses"]:
            line("↳ lost at reset if nothing changes: %s" % ", ".join(
                "%s %.0f%% in %s" % (l["key"], l["lost"] * 100, _duration(l["in_s"]))
                for l in acc["losses"]), "    ")
    for plan in budget["plans"]:
        if plan["harness"] in covered:
            continue                # déjà montré compte par compte
        line("plan %s %s: %.0f%% (pace %.0f%%)%s" % (
            plan["harness"], plan["key"], plan["used"] * 100, plan["pace_cap"] * 100,
            " — EXCEEDED" if plan["exceeded"] else ""), "  ")
    if not budget["plans"] and not budget.get("accounts"):
        line("plans: no gauge read on this host", "  ")
    return "\n".join(out)


def _tokens(n) -> str:
    """Un nombre de jetons lisible : 1 234, 12,3 k, 4,56 M."""
    n = int(n or 0)
    if n >= 1_000_000:
        return "%.2f M" % (n / 1e6)
    if n >= 10_000:
        return "%.1f k" % (n / 1e3)
    return str(n)


def _gauge_text(g: dict) -> str:
    text = "%s %.0f%% (pace %.0f%%)" % (g["key"], g["used"] * 100, g["pace_cap"] * 100)
    if g.get("reset_passed"):
        text += " reset passed"
    return text



# --------------------------------------------------------------------------
# vue de référence : page HTML statique autonome
# --------------------------------------------------------------------------

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress_page.html")
_DATA_MARK = "/*__AMEESH_PROGRESS_DATA__*/null"
_MODE_MARK = '/*__AMEESH_PROGRESS_MODE__*/"static"'
_CSP_MARK = "default-src 'none';"


def render_html(snap: dict | None, *, served: bool = False) -> str:
    """La page de référence avec l'instantané embarqué (aucune ressource externe).

    `served` (L304) : page de `ameesh progress --serve`, rendue sans données
    (`snap` None) : elle lit `/api/progress.json` avec le jeton, au chargement
    puis à chaque Refresh ; la politique de sécurité ajoute donc
    `connect-src 'self'` (rien d'autre). Statique, le bouton Refresh est
    désactivé et la page ne peut rien demander au réseau.

    Le JSON est inséré dans un `<script type="application/json">` : `<`, `>`
    et `&` y sont échappés en `\\u003c`… pour qu'aucun texte (titre de lot,
    consigne) ne puisse fermer la balise ni devenir du HTML.
    """
    with open(PAGE, encoding="utf-8") as fh:
        page = fh.read()
    data = (json.dumps(snap, ensure_ascii=False, sort_keys=True)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    if _DATA_MARK not in page or _MODE_MARK not in page or _CSP_MARK not in page:
        raise ProgressError("page template has no data placeholder")
    page = page.replace(_MODE_MARK, '"served"' if served else '"static"', 1)
    if served:
        page = page.replace(_CSP_MARK, _CSP_MARK + " connect-src 'self';", 1)
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
        raise ProgressError("cannot write %s (%s)" % (path, exc))


# --------------------------------------------------------------------------
# entrée
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh progress", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="ameesh-progress/1 schema")
    parser.add_argument("--html", metavar="FILE", default=None,
                        help="write the static timeline page (self-contained, offline)")
    parser.add_argument("--serve", action="store_true",
                        help="serve the page locally (127.0.0.1, read-only, token), L304")
    parser.add_argument("--port", type=int, default=None,
                        help="port for --serve (default 8765)")
    parser.add_argument("--project", default=None, help="project (app, workstream, team)")
    parser.add_argument("--since", default=DEFAULT_SINCE,
                        help="window start: duration (16h, 2d) or ISO date (default 24h)")
    parser.add_argument("--no-gantt", action="store_true",
                        help="omit the Gantt roadmap (shown by default, L96)")
    parser.add_argument("--no-versions", action="store_true",
                        help="do not read GitHub tags and releases (L160f, #497)")
    parser.add_argument("--stale-after", default=None,
                        help="a lot is stale after this long without activity (default "
                             "AMEESH_STALE_AFTER, else 6h)")
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
        if args.port is not None and not args.serve:
            raise ProgressError("--port only goes with --serve")
        if args.serve:
            if args.html or args.json:
                raise ProgressError("--serve combines with neither --html nor --json")
            from . import progress_serve
            port = progress_serve.DEFAULT_PORT if args.port is None else args.port
            if not 0 <= port <= 65535:
                raise ProgressError("--port out of range: %s" % port)
            return progress_serve.serve(cfg, port=port, since=args.since,
                                        stale_after=threshold, gantt=not args.no_gantt,
                                        project=args.project,
                                        versions=not args.no_versions)
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db, defer=True)
            # L160f (#497) : un lecteur GitHub par exécution (cache mémoire),
            # adossé au cache partagé de l'hôte (#848) entre exécutions
            reader = None if args.no_versions else versions_mod.Reader(
                cache_dir=cfg.state_dir)
            # L61 : lectures regroupées (db.batched) — deux allers-retours
            snap = db_mod.batched(db, lambda db: snapshot(
                db, cfg, since=args.since, project=args.project, stale_after=threshold,
                gantt=not args.no_gantt, github=reader))
        finally:
            db.close()
        if args.html:
            write_html(args.html, snap)
            if not args.json:
                print("page written: %s" % args.html)
        if args.json:
            print(json.dumps(snap, ensure_ascii=False, indent=2, sort_keys=True))
        elif not args.html:
            print(format_text(snap))
        return 0
    except ProgressError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.DbError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
