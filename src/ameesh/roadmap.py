# SPDX-License-Identifier: AGPL-3.0-only
"""Roadmap: planned dates, dated commitments, Gantt (lot L96).

  ameesh work plan <task|package> [--start D] [--end D] [--delivery D] [--source S]
  ameesh plan add "what" --due D [--project P] [--owner X] [--lot N] [--card F]
                  [--source-type conversation|decision|thread|task|other] [--source S]
                  [--depends-on REF ...] [--decision] [--proposed] [--key KEY]
  ameesh plan get <eN|N> [--json]
  ameesh plan update <eN|N> [--what TEXT] [--due D] [--note-file FILE|-] [...]
  ameesh plan list [--project P] [--all] [--json]
  ameesh plan export [--project P] [--all] [--output FILE|-]
  ameesh plan import FILE|- [--apply] [--update] [--json]
  ameesh plan validate [FILE|-] [--project P] [--json]
  ameesh plan accept <id> [--due D] | done <id> | cancel <id> [--note ...]
  ameesh plan propose [--from DIR] [--no-canon] [--no-tasks] [--record] [--json]
  ameesh plan show [--json] [--project P] [--width N]

D is a day: YYYY-MM-DD, today, tomorrow, or a weekday (monday or lundi =
the NEXT one).

* **Planned dates**: a task (`work_items`) and a plan package (epic,
  milestone) carry a planned start, end and delivery (or deployment). For a
  package, dates set in ameesh take precedence over the canon ones (keys
  `start`, `end`, `delivery`, or a milestone's `date`).
* **Commitments**: what, by when, owner, project, attached task or
  package, and the source (conversation, canon decision, thread message,
  task) with its identifier. `--decision`: a **decision milestone**, an
  open question waiting for a human (its date may be missing).
* **Proposals** (`plan propose`): read from canon decisions (and their
  consequences: lots Lxx, their dependencies, weekdays) and from open task
  bodies (a date written only there). They are PROPOSALS: nothing is created
  without `--record`, and a recorded proposal stays `proposed` until
  `plan accept`.
* **Gantt** (`plan show`, and the `roadmap` section of `ameesh progress`):
  epics, tasks, milestones and commitments on a timeline, planned versus
  actual (actual milestones already measured: requested, in progress,
  submitted, verdict, delivered), the today line, delays, and the source of
  each item.

Gantt JSON follows `ameesh-roadmap/1`; editable JSON follows `ameesh-plan/1`.
Import previews changes unless --apply is passed. See docs/EXPLOITATION.md.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import textwrap
import time
from typing import Iterable

from . import activation
from . import config as config_mod
from . import db as db_mod
from . import estimates as estimates_mod
from . import plan as plan_mod
from . import roadmap_dev as dev
from . import storage
from . import work as work_mod
from .deprecated import DeprecatedAlias, HiddenAliasParser, UniqueAppend

SCHEMA = "ameesh-roadmap/1"
#: borne de lecture des tâches de la frise
MAX_ITEMS = 300
#: une tâche livrée ou abandonnée reste sur la frise pendant cette fenêtre
DEFAULT_RECENT_S = 14 * 86400.0
#: fenêtre de la frise autour d'aujourd'hui (jours), élargie aux dates connues
#: dans ces bornes
MIN_BEFORE_DAYS, MIN_AFTER_DAYS = 7, 14
MAX_BEFORE_DAYS, MAX_AFTER_DAYS = 90, 180

KINDS = ("commitment", "decision")
STATUSES = ("proposed", "open", "done", "cancelled")
LIVE_STATUSES = ("proposed", "open")
SOURCE_KINDS = ("conversation", "decision", "thread", "task", "other")
DATE_KEYS = ("start", "end", "delivery")

#: jalons génériques réels d'une tâche, dans l'ordre de la frise
REAL_MILESTONES = ("demandee", "debut", "soumise", "verdict", "livree")
LABELS = {"demandee": "requested", "debut": "in progress", "soumise": "submitted",
          "verdict": "verdict", "livree": "delivered", "abandonnee": "abandoned",
          "start": "start", "end": "end", "delivery": "delivery"}
_FR_STATE = {"en_cours": "in progress", "a_valider": "to review", "validee": "accepted",
             "bloquee": "blocked", "livree": "delivered", "abandonnee": "abandoned",
             "demandee": "requested"}
_FR_SOURCE = {"conversation": "conversation", "decision": "decision", "thread": "thread",
              "task": "task", "other": "other", "canon": "canon", "ameesh": "ameesh",
              "mixte": "canon and ameesh"}
#: weekdays read in decisions and accepted as input (French, then English)
_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_WEEKDAYS_EN = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: une valeur d'option qui EFFACE une date
CLEAR = ("-", "aucune", "none")


class RoadmapError(work_mod.WorkError):
    """Saisie refusée (date illisible, élément inconnu) — message actionnable."""


# --------------------------------------------------------------------------
# jours
# --------------------------------------------------------------------------

def today(now: float | None = None) -> _dt.date:
    """Le jour local de `now`."""
    return _dt.date.fromtimestamp(time.time() if now is None else float(now))


def parse_day(text, *, now: float | None = None, allow_clear: bool = False) -> str | None:
    """Un jour : ISO (`2026-10-12`), `aujourd'hui`, `demain`, ou un jour de la
    semaine (`lundi` = le PROCHAIN lundi, jamais aujourd'hui). Rend le jour
    ISO ; avec `allow_clear`, `-`/`aucune` rend None (date effacée)."""
    raw = str(text or "").strip()
    low = raw.lower()
    if allow_clear and low in CLEAR:
        return None
    base = today(now)
    if low in ("aujourd'hui", "aujourdhui", "today"):
        return base.isoformat()
    if low in ("demain", "tomorrow"):
        return (base + _dt.timedelta(days=1)).isoformat()
    if low in _WEEKDAYS:
        return next_weekday(base, _WEEKDAYS.index(low)).isoformat()
    if low in _WEEKDAYS_EN:
        return next_weekday(base, _WEEKDAYS_EN.index(low)).isoformat()
    try:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            raise ValueError(raw)
        return _dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        raise RoadmapError("unreadable date: %r (YYYY-MM-DD, today, tomorrow, monday…%s)"
                           % (raw, ", or - to clear" if allow_clear else ""))


def next_weekday(base: _dt.date, weekday: int) -> _dt.date:
    """Le prochain `weekday` (0 = lundi) STRICTEMENT après `base`."""
    ahead = (weekday - base.weekday()) % 7 or 7
    return base + _dt.timedelta(days=ahead)


def day_ts(day: str | None) -> float | None:
    """Minuit local du jour ISO."""
    return plan_mod.day_start_ts(day)


def _day_of(ts) -> _dt.date | None:
    return None if ts is None else _dt.date.fromtimestamp(float(ts))


def _iso(day: _dt.date | None) -> str | None:
    return day.isoformat() if day else None


def _date(text: str | None) -> _dt.date | None:
    if not text:
        return None
    try:
        return _dt.date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------
# dates prévues : `ameesh work plan`
# --------------------------------------------------------------------------

_UNSET = object()


_AUTHORITY = ("only a human, an orchestrator or a design agent may: a person at a "
              "terminal passes --actor human:<id>, an agent asks the orchestrator")


def _day(value) -> str | None:
    return str(value)[:10] if value else None


def _hand_source(db, current: dict, dates: dict, source: str | None,
                 actor: str) -> tuple[str | None, str | None]:
    """The source of a date set by hand on a task (#704), and a warning when
    that date stays unprotected (#740).

    Without `--source`, the stored source is kept as it is. When the planner
    (`work schedule --apply`) dated the task, that source is the planner's, and
    its next pass would overwrite the date set by hand: the date then records
    who set it, if the start or the end really changes (set or cleared;
    resubmitting the current dates pins nothing) and if the actor may pin a
    date: a human, an orchestrator or a design agent (`work.pin_authority`).
    Another agent's date is written and stays the planner's, with a warning.
    A source the caller passes is a claim on the dates too: one that takes
    them from the planner or gives them back, or that writes or replaces a
    "set by" marker, needs the same authority; without it the stored source
    stays, with a warning. Only the start and the end count: the planner
    never writes the delivery date."""
    from .lot_schedule import MARKER, SOURCE  # lot_schedule imports work, not this module

    stored = str(current.get("planned_source") or "")
    cfg = getattr(db, "cfg", None)
    warnings = []
    if source is not None and source != stored and (
            source.startswith(SOURCE) != stored.startswith(SOURCE)
            or source.startswith(MARKER) or stored.startswith(MARKER)):
        if work_mod.pin_authority(cfg, db, actor) is None:
            warnings.append("source %r not recorded: it would change who owns the dates, "
                            "and %s may not (%s)" % (source, actor or "this session",
                                                     _AUTHORITY))
            source = None
    if source is not None or not stored.startswith(SOURCE):
        return source, "; ".join(warnings) or None
    if not any(key in dates and _day(dates[key]) != _day(current.get("planned_" + key))
               for key in ("start", "end")):
        return None, "; ".join(warnings) or None
    who = work_mod.pin_authority(cfg, db, actor)
    if who is None:
        warnings.append("date not protected: %s may not pin it (%s), so the next work "
                        "schedule --apply may move it" % (actor or "this session",
                                                          _AUTHORITY))
        return None, "; ".join(warnings)
    return MARKER + who, "; ".join(warnings) or None


def plan(db, target, *, start=_UNSET, end=_UNSET, delivery=_UNSET, source: str | None = None,
         actor: str = "", now: float | None = None) -> dict:
    """Pose les dates prévues d'une tâche (`target` entier ou `#N`) ou d'une
    fiche WorkPackage (`target` = son identifiant). Une option absente laisse
    la date telle quelle ; `-` l'efface. Rend `{target, kind, plan}`."""
    dates = {}
    for key, value in (("start", start), ("end", end), ("delivery", delivery)):
        if value is not _UNSET:
            dates[key] = parse_day(value, now=now, allow_clear=True)
    if not dates and source is None:
        raise RoadmapError("nothing to plan: --start, --end or --delivery (or --source; "
                           "--estimate for a task's duration)")
    text = str(target).strip().lstrip("#")
    st = storage.of(db)
    if text.isdigit():
        # #740: the row is written only as it was read (a schedule applied in
        # between would leave its source on the date set here): read again
        for _attempt in range(3):
            current = (st.roadmap.item_plans([int(text)]) or [None])[0]
            if current is None:
                raise RoadmapError("task %s not found" % text)
            _check_order(dict(current, **{"planned_" + k: v for k, v in dates.items()}))
            hand, warning = _hand_source(db, current, dates, source, actor)
            row = st.roadmap.plan_item(int(text), dates, source=hand, actor=actor,
                                       expect=current)
            if row is not None:
                break
        else:
            raise RoadmapError("task %s changed while it was being planned: run it again"
                               % text)
        out = {"target": int(text), "kind": "task", "plan": row}
        if warning:
            out["warning"] = warning
        return out
    package = st.packages.get(text)
    if package is None:
        raise RoadmapError("neither a task nor a WorkPackage: %r (a task: its number; a "
                           "package: its identifier, synced by \"ameesh canon sync\")"
                           % text)
    _check_order(dict(package, **{"planned_" + k: v for k, v in dates.items()}))
    row = st.roadmap.plan_package(text, dates, source=source, actor=actor)
    if row is None:
        raise RoadmapError("package %s not found" % text)
    return {"target": text, "kind": "package", "plan": row}


def _check_order(row: dict) -> None:
    start, end = _date(row.get("planned_start")), _date(row.get("planned_end"))
    if start and end and start > end:
        raise RoadmapError("planned start (%s) is after the planned end (%s)" % (start, end))


# --------------------------------------------------------------------------
# engagements : `ameesh plan add|accept|done|cancel|list`
# --------------------------------------------------------------------------

def add_commitment(db, what: str, *, due=None, decision: bool = False, owner: str | None = None,
                   project: str | None = None, lot=None, package: str | None = None,
                   source_kind: str = "conversation", source_ref: str | None = None,
                   depends_on=(), note: str = "", actor: str = "",
                   now: float | None = None) -> dict:
    """Enregistre un engagement daté (ou un jalon de décision, `decision`).

    Un engagement rattaché à une tâche (`lot`) en prend le projet par défaut ;
    il est tenu quand la tâche est livrée."""
    what = " ".join(str(what or "").split())
    if not what:
        raise RoadmapError("empty commitment: say what")
    if source_kind not in SOURCE_KINDS:
        raise RoadmapError("unknown source type: %r (%s)" % (source_kind,
                                                                 ", ".join(SOURCE_KINDS)))
    day = parse_day(due, now=now) if due not in (None, "") else None
    if day is None and not decision:
        raise RoadmapError("a commitment needs a date: --due YYYY-MM-DD (or monday, tomorrow…)")
    st = storage.of(db)
    item_id = None
    if lot not in (None, ""):
        text = str(lot).strip().lstrip("#")
        item = work_mod.get(db, int(text)) if text.isdigit() else None
        if item is None:
            raise RoadmapError("task %s not found" % lot)
        item_id = int(item["id"])
        project = project or item.get("app") or item.get("workstream") or None
        owner = owner or item.get("assignee") or None
    if package:
        if st.packages.get(package) is None:
            raise RoadmapError("unknown WorkPackage: %r" % package)
    return st.roadmap.add_commitment({
        "what": what, "due_on": day, "kind": "decision" if decision else "commitment",
        "status": "open", "owner": owner or None, "project": project or None,
        "work_item_id": item_id, "package_id": package or None, "source_kind": source_kind,
        "source_ref": source_ref or None,
        # L148 : `--depends-on` et `--depend-de` peuvent viser la même valeur,
        # dans n'importe quel ordre ; on ne stocke qu'une fois, dans l'ordre.
        "depends_on": list(dict.fromkeys(str(d) for d in depends_on or ())),
        "note": note or "", "created_by": actor or ""})


def _commitment(db, ident) -> dict:
    rows = storage.of(db).roadmap.commitments(ids=[int(ident)])
    if not rows:
        raise RoadmapError("commitment %s not found" % ident)
    return rows[0]


def accept(db, ident, *, due=None, actor: str = "", now: float | None = None) -> dict:
    """Valide une proposition : elle devient un engagement ouvert. Sans date
    (proposition tirée d'une décision sans échéance), `--due` la fixe."""
    row = _commitment(db, ident)
    if row["status"] != "proposed":
        raise RoadmapError("commitment %s: %s, not a proposal" % (ident, row["status"]))
    values: dict = {"status": "open"}
    if due not in (None, ""):
        values["due_on"] = parse_day(due, now=now)
    if not values.get("due_on") and not row.get("due_on") and row["kind"] != "decision":
        raise RoadmapError("proposal %s is undated: --due YYYY-MM-DD" % ident)
    if actor:
        values["note"] = (row.get("note") + "; " if row.get("note") else "") + \
            "accepted by %s" % actor
    out = storage.of(db).roadmap.set_commitment(int(ident), current=("proposed",),
                                                values=values, expect=row)
    if out is None:
        raise RoadmapError("commitment %s changed meanwhile: retry" % ident)
    return out


def settle(db, ident, status: str, *, note: str = "", actor: str = "") -> dict:
    """`done` (tenu) ou `cancelled` (abandonné, ou proposition rejetée)."""
    if status not in ("done", "cancelled"):
        raise RoadmapError("status %r refused" % status)
    row = _commitment(db, ident)
    if row["status"] not in LIVE_STATUSES:
        raise RoadmapError("commitment %s already %s" % (ident, row["status"]))
    text = "; ".join(t for t in (row.get("note"), note.strip() if note else "",
                                 "by %s" % actor if actor else "") if t)
    out = storage.of(db).roadmap.set_commitment(
        int(ident), current=LIVE_STATUSES, values={"status": status, "note": text}, expect=row)
    if out is None:
        raise RoadmapError("commitment %s changed meanwhile: retry" % ident)
    return out


# --------------------------------------------------------------------------
# la frise : `ameesh plan show`, section `roadmap` de `ameesh progress`
# --------------------------------------------------------------------------

def _source_view(kind: str | None, ref: str | None, label: str | None = None) -> dict | None:
    if not kind and not ref:
        return None
    ref = ref or None
    url = ref if ref and re.match(r"^https?://", ref) else None
    if not label:
        word = _FR_SOURCE.get(kind or "", kind or "")
        # « conversation du 10/10 » plutôt que « conversation conversation du 10/10 »
        label = ref if ref and word and ref.lower().startswith(word.lower()) else \
            ("%s %s" % (word, ref or "")).strip()
    return {"kind": kind, "ref": ref, "url": url, "label": label}


def real_milestones(item: dict, events: list[dict], actions: list[dict],
                    declared: list[dict] | None, *,
                    agents: Iterable[str] | None = None) -> dict:
    """Les jalons RÉELS génériques d'une tâche, depuis ce qu'ameesh mesure
    déjà (journal, table des jalons, actions) : `{demandee, debut, soumise,
    verdict, verdict_kind, livree}` en secondes epoch (None si pas atteint).
    `agents` : le registre, pour la correspondance `seb` / `human:seb` (#2346)."""
    from . import progress  # import tardif : progress importe ce module

    jalons = progress._jalons_de_lot(item, events, actions, declared, agents=agents)
    started = next((e.get("created_ts") for e in events if dev.en_cours(e.get("state"))), None)
    out = {key: jalons.get(dev.jalon(key)) for key in ("demandee", "soumise", "verdict",
                                                       "livree")}
    # #988 : un lot livré hors cible porte `delivered`, pas `merged`
    out["livree"] = out["livree"] or jalons.get("delivered")
    out["debut"] = started
    out["verdict_kind"] = jalons.get("verdict_kind")
    return {k: (round(float(v), 3) if isinstance(v, (int, float)) and k != "verdict_kind"
                else v) for k, v in out.items()}


def _late(target: _dt.date | None, done_day: _dt.date | None, base: _dt.date,
          what: str) -> dict | None:
    """Retard sur une date prévue : en cours (`open`) ou constaté à la
    livraison (`delivered`). None à l'heure."""
    if target is None:
        return None
    if done_day is None:
        if base > target:
            return {"what": what, "due": target.isoformat(), "days": (base - target).days,
                    "open": True}
        return None
    if done_day > target:
        return {"what": what, "due": target.isoformat(), "days": (done_day - target).days,
                "open": False}
    return None


def build_task(item: dict, real: dict, *, base: _dt.date, epic: str | None,
               commitments: list[dict], shown_state: str | None,
               now: float | None = None, activation: dict | None = None) -> dict:
    state = item.get("state")
    delivered = dev.livree(state, item.get("close_reason"))
    abandoned = dev.abandonnee(state, item.get("close_reason"))
    planned = {k: item.get("planned_" + k) for k in DATE_KEYS}
    done_day = _day_of(real.get("livree")) if delivered else None
    target_key = "delivery" if planned["delivery"] else ("end" if planned["end"] else None)
    late = None
    if not abandoned and target_key:
        late = _late(_date(planned[target_key]), done_day, base, LABELS[target_key])
    if late is None and not abandoned and not delivered and planned["start"] \
            and real.get("debut") is None and base > _date(planned["start"]):
        late = {"what": LABELS["start"], "due": planned["start"],
                "days": (base - _date(planned["start"])).days, "open": True}
    for c in commitments:
        if late is None and c.get("late"):
            late = dict(c["late"], what="commitment #%d" % c["id"])
    generic = "abandonnee" if abandoned else ("livree" if delivered else
                                               dev.etat_generique(shown_state))
    source = _source_view("plan", item.get("planned_source"),
                          item.get("planned_source")) if item.get("planned_source") else None
    return {
        "kind": "task", "id": int(item["id"]), "title": item.get("title") or "",
        "project": item.get("app") or item.get("workstream") or None,
        "epic": epic, "package": item.get("package_id") or None,
        "assignee": item.get("assignee") or None, "state": generic,
        "waiting_decision": dev.attend_decision(state),
        "planned": dict(planned, source=item.get("planned_source") or None,
                        by=item.get("planned_by") or None),
        "real": real, "late": late, "source": source,
        # #5244 : l'étape atteinte après la livraison (`activation`), null si ouverte
        "activation": activation if activation and activation.get("label") else None,
        "commitments": [c["id"] for c in commitments],
        # L157 : durée estimée, réel mesuré (début → livraison), écart
        "estimate": estimates_mod.view(item, merged_ts=real.get("livree") if delivered
                                       else None, now=now),
    }


def _commitment_view(row: dict, base: _dt.date, items: dict,
                     activation: dict | None = None) -> dict:
    item = items.get(row.get("work_item_id")) if row.get("work_item_id") else None
    # #5244 : un engagement sur un lot est tenu quand le lot est activé
    step = (activation or {}).get(int(item["id"])) if item else None
    fulfilled = row["status"] == "done" or bool(
        step["counted"] if step else item and dev.livree(item.get("state")))
    due = _date(row.get("due_on"))
    late = None
    if row["status"] == "open" and row["kind"] == "commitment" and not fulfilled \
            and due and base > due:
        late = {"what": "commitment", "due": due.isoformat(), "days": (base - due).days,
                "open": True}
    return {
        "kind": row["kind"], "id": row["id"], "what": row["what"], "due": row.get("due_on"),
        "status": row["status"], "owner": row.get("owner"), "project": row.get("project"),
        "lot": row.get("work_item_id"), "package": row.get("package_id"),
        "depends_on": row.get("depends_on") or [], "fulfilled": fulfilled,
        "late": late, "note": row.get("note") or "",
        "source": _source_view(row.get("source_kind"), row.get("source_ref")),
        "created_ts": row.get("created_ts"),
    }


def _package_view(pkg: dict, tally: dict, real_span: tuple, base: _dt.date) -> dict:
    planned = plan_mod.planned_dates(pkg)
    target = _date(planned["delivery"] or planned["end"])
    complete = tally.get("progress") == 1 or (tally.get("lots_total", 0) > 0 and
                                              tally.get("lots_open", 0) == 0 and
                                              tally.get("lots_pending", 0) == 0)
    late = None
    if target and not complete and base > target:
        late = {"what": LABELS["delivery"] if planned["delivery"] else LABELS["end"],
                "due": target.isoformat(), "days": (base - target).days, "open": True}
    origin = planned.pop("source")
    source = (_source_view("canon", pkg.get("canon_ref")) if origin in ("canon", "mixte")
              else _source_view("ameesh", pkg.get("planned_source") or pkg.get("planned_by")))
    return {
        "kind": pkg.get("kind"),
        "id": pkg["id"], "title": pkg.get("title") or "", "parent": pkg.get("parent"),
        "responsible": pkg.get("responsible"), "planned": dict(planned, source=origin),
        "real": {"debut": real_span[0], "livree": real_span[1]},
        "progress": tally.get("progress"), "units_total": tally.get("lots_total", 0),
        "units_delivered": tally.get("lots_merged", 0),
        "units_active": tally.get("lots_active", 0),
        "units_waiting_activation": tally.get("lots_waiting_activation", 0),
        "late": late, "source": source,
        "canon_ref": pkg.get("canon_ref"),
    }


def build(db, *, now: float | None = None, project: str | None = None,
          recent_s: float = DEFAULT_RECENT_S, max_items: int = MAX_ITEMS) -> dict:
    """La feuille de route `ameesh-roadmap/1` : epics, jalons, tâches,
    engagements et jalons de décision, avec la fenêtre de la frise."""
    from . import progress  # import tardif : progress importe ce module

    now = time.time() if now is None else float(now)
    base = today(now)
    st = storage.of(db)
    # #2346 : un nom nu du registre reste un agent ; `human:<nom>` est l'autre
    # acteur, pour que `lot_state` ne diverge pas de progress.
    agents = {r["name"] for r in st.progress.agents(None) if r.get("name")}
    done_states = (dev.CORRESPONDANCES["etats_livres"]
                   + dev.CORRESPONDANCES["etats_abandonnes"])
    filters = {"project": project} if project else {}
    rows = st.roadmap.items(since_ts=now - recent_s, limit=max_items,
                            done_states=done_states, **filters)
    ids = [int(r["id"]) for r in rows]
    events: dict = {}
    # #1647 : les jalons réels ne lisent que les transitions, jamais les notes.
    for ev in st.progress.lot_transitions(ids):
        events.setdefault(int(ev["work_item_id"]), []).append(ev)
    actions: dict = {}
    for act in st.progress.lot_actions(ids):
        actions.setdefault(int(act["work_item"]), []).append(act)
    declared: dict = {}
    for ms in st.progress.lot_milestones(ids):
        declared.setdefault(int(ms["work_item_id"]), []).append(ms)
    packages = st.progress.packages()
    by_pkg = {p["id"]: p for p in packages}
    package_items = st.progress.package_items()

    commitment_rows = [c for c in st.roadmap.commitments()
                       if c["status"] in LIVE_STATUSES
                       or (c.get("closed_ts") or 0) >= now - recent_s]
    items_by_id = {int(r["id"]): r for r in rows}
    missing = sorted({int(c["work_item_id"]) for c in commitment_rows
                      if c.get("work_item_id") and int(c["work_item_id"]) not in items_by_id})
    for extra in (work_mod.get(db, i) for i in missing):
        if extra:
            items_by_id[int(extra["id"])] = extra
    # #5244 : une seule lecture des preuves, pour les fiches, tâches et engagements
    steps = activation.compute(db, list({int(i["id"]): i for i in (
        *package_items, *items_by_id.values())}.values()))
    summary = plan_mod.summarize(packages, package_items, steps)
    commitments = [_commitment_view(c, base, items_by_id, steps) for c in commitment_rows]
    if project:
        commitments = [c for c in commitments if c["project"] in (None, project)]
    by_item: dict = {}
    for c in commitments:
        if c["lot"]:
            by_item.setdefault(c["lot"], []).append(c)

    tasks = []
    for item in rows:
        iid = int(item["id"])
        evs, acts = events.get(iid, []), actions.get(iid, [])
        real = real_milestones(item, evs, acts, declared.get(iid), agents=agents)
        jalons = progress._jalons_de_lot(item, evs, acts, declared.get(iid),
                                         agents=agents)
        shown = progress.lot_state(item.get("state") or "", jalons)
        tasks.append(build_task(item, real, base=base,
                                epic=plan_mod.epic_of(item.get("package_id"), by_pkg),
                                commitments=[c for c in by_item.get(iid, [])
                                             if c["kind"] == "commitment"],
                                shown_state=shown, now=now, activation=steps.get(iid)))

    # epics et jalons du plan : prévu (fiche) face au réel (ses tâches)
    spans: dict = {}
    for t in tasks:
        holders = {t["epic"], t["package"]}
        pkg = by_pkg.get(t["package"]) if t["package"] else None
        holders.add(plan_mod.milestone_of(t["package"], by_pkg) if pkg else None)
        for h in holders - {None}:
            first, last = spans.get(h, (None, None))
            start = t["real"].get("debut") or t["real"].get("demandee")
            first = start if first is None or (start and start < first) else first
            if t["real"].get("livree"):
                last = max(last or 0, t["real"]["livree"])
            spans[h] = (first, last)
    tallies = {e["id"]: e for e in summary["epics"]}
    tallies.update({m["id"]: m for m in summary["milestones"]})
    epics = [_package_view(by_pkg[e["id"]], e, spans.get(e["id"], (None, None)), base)
             for e in summary["epics"]]
    milestones = [_package_view(by_pkg[m["id"]], m, spans.get(m["id"], (None, None)), base)
                  for m in summary["milestones"]]
    if project:
        teams = {p["id"] for p in packages if p.get("team") == project}
        keep = teams | {t["epic"] for t in tasks} | {t["package"] for t in tasks}
        epics = [e for e in epics if e["id"] in keep]
        milestones = [m for m in milestones if m["id"] in keep or any(
            e["parent"] == m["id"] for e in epics)]

    decisions = [c for c in commitments if c["kind"] == "decision"
                 and c["status"] in LIVE_STATUSES]
    decisions += [{"kind": "decision", "id": None, "lot": t["id"], "what": t["title"],
                   "due": None, "status": "open", "owner": t["assignee"],
                   "project": t["project"], "source": _source_view("task", "#%d" % t["id"]),
                   "late": None, "fulfilled": False, "depends_on": []}
                  for t in tasks if t["waiting_decision"]]

    window = _window(base, tasks, epics + milestones, commitments)
    late = [{"kind": e["kind"], "id": e["id"], "title": e.get("title") or e.get("what"),
             **e["late"]}
            for e in epics + milestones + tasks + commitments
            if e.get("late") and e["late"].get("open")]
    return {
        "schema": SCHEMA,
        "generated_ts": round(now, 3),
        "today": base.isoformat(),
        "project": project or None,
        "window": window,
        "epics": epics,
        "milestones": milestones,
        "tasks": tasks,
        "commitments": [c for c in commitments if c["kind"] == "commitment"],
        "decisions": decisions,
        "late": late,
        "truncated": ({"tasks": {"shown": len(rows), "total": int(rows[0].get("total") or 0),
                                 "limit": int(max_items)}}
                      if rows and int(rows[0].get("total") or 0) > len(rows) else {}),
    }


def _window(base: _dt.date, tasks, packages, commitments) -> dict:
    days = []
    for t in tasks:
        days += [_date(v) for k, v in t["planned"].items() if k in DATE_KEYS]
        days += [_day_of(v) for k, v in t["real"].items() if k != "verdict_kind"]
    for p in packages:
        days += [_date(p["planned"].get(k)) for k in DATE_KEYS]
    days += [_date(c.get("due")) for c in commitments]
    days = [d for d in days if d]
    lo = min(days + [base - _dt.timedelta(days=MIN_BEFORE_DAYS)])
    hi = max(days + [base + _dt.timedelta(days=MIN_AFTER_DAYS)])
    lo = max(lo, base - _dt.timedelta(days=MAX_BEFORE_DAYS))
    hi = min(hi, base + _dt.timedelta(days=MAX_AFTER_DAYS))
    return {"from": lo.isoformat(), "to": hi.isoformat(),
            "from_ts": day_ts(lo.isoformat()),
            "to_ts": day_ts((hi + _dt.timedelta(days=1)).isoformat())}


# --------------------------------------------------------------------------
# retards : alerte `engagement_overdue`
# --------------------------------------------------------------------------

def overdue(db, *, now: float | None = None) -> list[dict]:
    """Les engagements et dates prévues PASSÉS sans que l'élément soit atteint
    (engagement ouvert non tenu, tâche non livrée, fiche non terminée). Une
    ligne : `{reason, what, due, days, commitment, lot, package, owner,
    assignee, responsible}` — `reason` : `engagement` ou `date_prevue`.

    Une date prévue ne lève que si une personne l'a posée et si le lot est
    encore piloté par son porteur : les dates calculées par la table de
    référence (`planned_source` « reference p80… ») sont re-datées à la passe
    suivante de `work schedule --apply`, jamais alertées ; un lot en attente
    (bloqué, en relecture, ou retenu par une attente planifiée) ne l'est pas
    non plus. Un lot daté et engagé ne produit qu'une alerte : l'engagement
    explicite l'emporte sur la date.

    Lecture légère (aucun journal) : l'alerte tourne à chaque passe."""
    now = time.time() if now is None else float(now)
    base = today(now)
    st = storage.of(db)
    done_states = (dev.CORRESPONDANCES["etats_livres"]
                   + dev.CORRESPONDANCES["etats_abandonnes"])
    rows = [r for r in st.roadmap.items(since_ts=now, limit=MAX_ITEMS,
                                        done_states=done_states)
            if not dev.livree(r.get("state")) and not dev.abandonnee(r.get("state"))]
    items = {int(r["id"]): r for r in rows}
    out: list[dict] = []
    for row in st.roadmap.commitments(statuses=("open",)):
        if row["kind"] != "commitment":
            continue
        due = _date(row.get("due_on"))
        if not due or base <= due:
            continue
        lot = row.get("work_item_id")
        item = items.get(lot) if lot else None
        if lot and item is None:
            item = work_mod.get(db, lot)
            if item and dev.abandonnee(item.get("state")):
                continue
            # #5244 : tâche activée : engagement tenu (livrer ne suffit pas)
            if item and dev.livree(item.get("state")) \
                    and activation.compute(db, [item])[int(item["id"])]["counted"]:
                continue
        out.append({"reason": "engagement", "what": row["what"], "due": due.isoformat(),
                    "days": (base - due).days, "commitment": row["id"], "lot": lot,
                    "package": row.get("package_id"), "owner": row.get("owner"),
                    "assignee": (item or {}).get("assignee"), "project": row.get("project")})
    # #2342 : un lot daté et engagé est une seule alerte (l'engagement est la
    # promesse explicite) ; sinon la date posée par une personne reste la sienne.
    engaged = {r["lot"] for r in out if r["reason"] == "engagement" and r["lot"] is not None}
    from .lot_schedule import SOURCE  # lot_schedule imports work, not this module
    dated = []
    for item in rows:
        key = "planned_delivery" if item.get("planned_delivery") else (
            "planned_end" if item.get("planned_end") else None)
        due = _date(item.get(key)) if key else None
        if not due or base <= due or int(item["id"]) in engaged:
            continue
        # a date computed by the schedule is re-dated by its next pass; a lot
        # waiting (held, under review, waiting for its package or a dependency)
        # has no gesture on its date, so it is not a date-driven alert
        if not dev.active(item.get("state")):
            continue
        # The scheduler only writes start/end; delivery remains a manual
        # date even when those fields still carry the scheduler's source.
        if key != "planned_delivery" and str(item.get("planned_source") or "").startswith(SOURCE):
            continue
        dated.append((item, key, due))
    waits = {}
    if dated:
        from . import planned_waits
        waits = planned_waits.read(db, [item for item, _, _ in dated], now=now)
    for item, key, due in dated:
        if int(item["id"]) in waits:
            continue
        out.append({"reason": "date_prevue", "what": "%s of task #%d \"%s\"" % (
            LABELS["delivery" if key == "planned_delivery" else "end"], int(item["id"]),
            item.get("title") or ""), "due": due.isoformat(), "days": (base - due).days,
            "commitment": None, "lot": int(item["id"]), "package": item.get("package_id"),
            "owner": None, "assignee": item.get("assignee"),
            "project": item.get("app") or item.get("workstream")})
    packages = st.progress.packages()
    summary = plan_mod.summarize(packages, st.progress.package_items())
    by_pkg = {p["id"]: p for p in packages}
    for entry in summary["epics"] + summary["milestones"]:
        dates = plan_mod.planned_dates(by_pkg[entry["id"]])
        due = _date(dates["delivery"] or dates["end"])
        if not due or base <= due or not (entry.get("lots_open") or entry.get("lots_pending")):
            continue
        out.append({"reason": "date_prevue", "what": "%s of %s \"%s\"" % (
            LABELS["delivery"] if dates["delivery"] else LABELS["end"], entry["id"],
            entry.get("title") or ""), "due": due.isoformat(), "days": (base - due).days,
            "commitment": None, "lot": None, "package": entry["id"],
            "owner": entry.get("responsible"), "assignee": None, "project": None})
    out.sort(key=lambda r: (r["due"], r["reason"], r["lot"] or 0, r["commitment"] or 0,
                            r["package"] or ""))
    return out


# --------------------------------------------------------------------------
# rendu texte : le Gantt en caractères
# --------------------------------------------------------------------------

#: caractères de la frise (légende de `format_gantt`)
GLYPHS = {"planned": "░", "real": "█", "late": "▓", "delivery": "◆", "delivered": "●",
          "due": "◇", "decision": "?", "today": "│"}
LEGEND = ("░ planned · █ actual (in progress → delivered) · ▓ actual past the planned date · "
          "◆ planned delivery · ● delivered · ◇ due date · ? decision awaited · │ today")


def _short(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:max(1, limit - 1)] + "…"


def _dm(day) -> str:
    d = _date(day) if not isinstance(day, _dt.date) else day
    return d.strftime("%d/%m") if d else "—"


class _Bar:
    """Une ligne de frise : un caractère par colonne de `step` jours."""

    def __init__(self, lo: _dt.date, cols: int, step: int):
        self.lo, self.cols, self.step = lo, cols, step
        self.cells = [" "] * cols

    def col(self, day: _dt.date | None) -> int | None:
        if day is None:
            return None
        c = (day - self.lo).days // self.step
        return c if 0 <= c < self.cols else None

    def span(self, a: _dt.date | None, b: _dt.date | None, glyph: str,
             over: tuple = (" ",)) -> None:
        if a is None or b is None:
            return
        lo = max(0, (a - self.lo).days // self.step)
        hi = min(self.cols - 1, (b - self.lo).days // self.step)
        for c in range(lo, hi + 1):
            if self.cells[c] in over:
                self.cells[c] = glyph

    def mark(self, day: _dt.date | None, glyph: str) -> None:
        c = self.col(day)
        if c is not None:
            self.cells[c] = glyph

    def text(self) -> str:
        return "".join(self.cells)


def _task_bar(bar: _Bar, t: dict, base: _dt.date) -> None:
    p = t["planned"]
    ps, pe, pd = _date(p.get("start")), _date(p.get("end")), _date(p.get("delivery"))
    first = ps or (pe and _day_of(t["real"].get("demandee")))
    bar.span(first, pe or pd, GLYPHS["planned"])
    start = _day_of(t["real"].get("debut") or t["real"].get("demandee"))
    end = _day_of(t["real"].get("livree")) or (None if t["state"] == "abandonnee" else base)
    target = pd or pe
    if start and end:
        if target and end > target:
            bar.span(start, target, GLYPHS["real"], over=(" ", GLYPHS["planned"]))
            bar.span(target + _dt.timedelta(days=1), end, GLYPHS["late"],
                     over=(" ", GLYPHS["planned"]))
        else:
            bar.span(start, end, GLYPHS["real"], over=(" ", GLYPHS["planned"]))
    bar.mark(pd, GLYPHS["delivery"])
    bar.mark(_day_of(t["real"].get("livree")), GLYPHS["delivered"])


def _package_bar(bar: _Bar, p: dict, base: _dt.date) -> None:
    pl = p["planned"]
    ps, pe, pd = _date(pl.get("start")), _date(pl.get("end")), _date(pl.get("delivery"))
    bar.span(ps, pe or pd, GLYPHS["planned"])
    start = _day_of(p["real"].get("debut"))
    # le réel d'une fiche court jusqu'à sa dernière livraison si tout est
    # livré, sinon jusqu'à aujourd'hui
    end = _day_of(p["real"].get("livree")) if p.get("progress") == 1 else base
    if start:
        bar.span(start, end or base, GLYPHS["real"], over=(" ", GLYPHS["planned"]))
    bar.mark(pd, GLYPHS["delivery"] if not (p.get("progress") == 1) else GLYPHS["delivered"])


def _flag(e: dict) -> str:
    late = e.get("late")
    if late and late.get("open"):
        return "LATE %d d (%s %s)" % (late["days"], late["what"], _dm(late["due"]))
    if late:
        return "delivered %d d late" % late["days"]
    return ""


def format_gantt(view: dict, width: int | None = None, *, who: dict | None = None) -> str:
    """Le Gantt en texte : une ligne de frise par élément, puis une ligne de
    détail (dates prévues, état, retard, source)."""
    if width is None:
        width = shutil.get_terminal_size((100, 24)).columns
    width = max(36, min(int(width), 180))
    base = _date(view["today"])
    lo, hi = _date(view["window"]["from"]), _date(view["window"]["to"])
    label_w = min(30, max(18, width // 3))
    cols = max(10, width - label_w - 1)
    days = (hi - lo).days + 1
    step = max(1, -(-days // cols))
    cols = min(cols, -(-days // step))
    out: list[str] = []

    def wrap(text: str, indent: str = "") -> None:
        out.extend(textwrap.wrap(text, width, initial_indent=indent,
                                 subsequent_indent=indent + "  ") or [indent])

    wrap("ROADMAP%s — today %s · 1 column = %d day%s" % (
        " of %s" % view["project"] if view.get("project") else "", _dm(base), step,
        "s" if step > 1 else ""))
    wrap(LEGEND)
    if who is not None:
        out.append("")
        out.extend(format_who(who, width))
    # axe : une date toutes les ~10 colonnes, la ligne du jour
    axis = [" "] * cols
    c = 0
    while c < cols:
        label = _dm(lo + _dt.timedelta(days=c * step))
        if c + len(label) <= cols:
            axis[c:c + len(label)] = list(label)
        c += max(10, len(label) + 4)
    today_col = (base - lo).days // step if lo <= base <= hi else None
    if today_col is not None and today_col < cols:
        axis[today_col] = GLYPHS["today"]
    out.append("")
    out.append(" " * (label_w + 1) + "".join(axis))

    def row(label: str, bar: _Bar, detail: list[str]) -> None:
        if today_col is not None and today_col < cols and bar.cells[today_col] == " ":
            bar.cells[today_col] = GLYPHS["today"]
        out.append(_short(label, label_w).ljust(label_w) + " " + bar.text())
        text = " · ".join(d for d in detail if d)
        if text:
            wrap(text, "    ")

    def planned_text(p: dict) -> str:
        parts = ["%s %s" % (LABELS[k], _dm(p.get(k))) for k in DATE_KEYS if p.get(k)]
        return ("planned: " + ", ".join(parts)) if parts else "no planned date"

    sections = (
        ("MILESTONES", view["milestones"]),
        ("EPICS", view["epics"]),
    )
    for title, rows in sections:
        if not rows:
            continue
        out.append("")
        out.append(title)
        for p in rows:
            bar = _Bar(lo, cols, step)
            _package_bar(bar, p, base)
            src = p.get("source") or {}
            row("%s %s" % (p["id"], p["title"]), bar, [
                planned_text(p["planned"]),
                "%d/%d delivered, %d not active yet" % (
                    p["units_delivered"], p["units_total"], p["units_waiting_activation"])
                if p["units_total"] and p.get("units_waiting_activation")
                else "%d/%d delivered" % (p["units_delivered"], p["units_total"])
                if p["units_total"] else "",
                _flag(p), "source: %s" % src["label"] if src.get("label") else ""])
    out.append("")
    out.append("TASKS (%d)" % len(view["tasks"]))
    if not view["tasks"]:
        out.append("  no open or dated task")
    for t in view["tasks"]:
        bar = _Bar(lo, cols, step)
        _task_bar(bar, t, base)
        real = t["real"]
        reached = [LABELS[k] + " " + _dm(_day_of(real[k])) for k in REAL_MILESTONES
                   if real.get(k)]
        src = t.get("source") or {}
        row("#%d %s" % (t["id"], t["title"]), bar, [
            _FR_STATE.get(t["state"], t["state"]),
            (t.get("activation") or {}).get("label") or "",
            t["assignee"] or "",
            planned_text(t["planned"]),
            ("actual: " + ", ".join(reached)) if reached else "",
            (t.get("estimate") or {}).get("label") if (t.get("estimate") or {}).get("minutes")
            else "",
            _flag(t),
            "source: %s" % src["label"] if src.get("label") else "",
            "commitment(s) %s" % ", ".join("e%d" % i for i in t["commitments"])
            if t["commitments"] else ""])
    if view["commitments"]:
        out.append("")
        out.append("COMMITMENTS (%d)" % len(view["commitments"]))
        for c in view["commitments"]:
            bar = _Bar(lo, cols, step)
            bar.mark(_date(c["due"]), GLYPHS["delivered"] if c["fulfilled"] else GLYPHS["due"])
            src = c.get("source") or {}
            status = {"proposed": "PROPOSED (to review)", "open": "open",
                      "done": "kept", "cancelled": "cancelled"}[c["status"]]
            if c["fulfilled"] and c["status"] == "open":
                status = "kept (task delivered)"
            row("e%d %s" % (c["id"], c["what"]), bar, [
                "due %s" % _dm(c["due"]) if c["due"] else "undated", status,
                c.get("owner") or "", "project %s" % c["project"] if c.get("project") else "",
                "task #%d" % c["lot"] if c.get("lot") else "",
                "depends on %s" % ", ".join(c["depends_on"]) if c["depends_on"] else "",
                _flag(c), "source: %s" % src["label"] if src.get("label") else ""])
    if view["decisions"]:
        out.append("")
        out.append("AWAITED DECISIONS (%d)" % len(view["decisions"]))
        for d in view["decisions"]:
            bar = _Bar(lo, cols, step)
            bar.mark(_date(d.get("due")) or base, GLYPHS["decision"])
            src = d.get("source") or {}
            label = ("e%d " % d["id"]) if d.get("id") else ("#%d " % d["lot"])
            row(label + d["what"], bar, [
                "due %s" % _dm(d["due"]) if d.get("due") else "undated",
                "PROPOSED (to review)" if d.get("status") == "proposed" else "",
                d.get("owner") or "",
                "source: %s" % src["label"] if src.get("label") else ""])
    cut = (view.get("truncated") or {}).get("tasks")
    if cut:
        out.append("")
        wrap("TRUNCATED: %d of %d tasks read (limit %d)" % (cut["shown"], cut["total"],
                                                            cut["limit"]))
    if view["late"]:
        out.append("")
        wrap("LATE (%d): %s" % (len(view["late"]), "; ".join(
            "%s %s (%d d)" % ({"task": "#%s" % l["id"], "commitment": "e%s" % l["id"]}
                             .get(l["kind"], str(l["id"])), _short(l["title"], 40), l["days"])
            for l in view["late"])))
    return "\n".join(out)


# --------------------------------------------------------------------------
# « qui avance sur quoi »
# --------------------------------------------------------------------------

def who(db, *, now: float | None = None, project: str | None = None) -> dict:
    """Pour chaque agent : projet, tâche en cours, dernière avancée lue dans
    le fil, depuis combien de temps il travaille (vue `projects`, L62)."""
    from . import projects
    return projects.snapshot(db, project=project, now=now)


def format_who(view: dict, width: int) -> list[str]:
    now = view["generated_ts"]
    out = ["WHO IS WORKING ON WHAT"]
    agents = [a for p in view["projects"] for a in p["agents"] if a["active"]]
    if not agents:
        out.append("  no active agent")
    for a in agents:
        doing = ("#%s %s" % (a["lot"]["id"], a["lot"]["title"])) if a.get("lot") else (
            a.get("task") or "—")
        since = a.get("on_task_since_ts") or a.get("since_ts")
        head = "%s [%s] %s — %s%s" % (
            a["name"], a["project"] or "no project", {"working": "working",
                                                      "paused": "paused",
                                                      "idle": "idle"}.get(a["state"],
                                                                          a["state"]),
            doing, " (for %s)" % work_mod.span(now - since) if since else "")
        out.extend(textwrap.wrap(head, width, initial_indent="  ", subsequent_indent="    "))
        last = a.get("last_update")
        if last:
            out.extend(textwrap.wrap("last progress (%s ago): %s" % (
                work_mod.span(now - last["ts"]), last["text"]), width,
                initial_indent="    ", subsequent_indent="      "))
    return out


# --------------------------------------------------------------------------
# entrée : `ameesh plan …`
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = HiddenAliasParser(
        prog="ameesh plan", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="plan_command", required=True)

    p_add = sub.add_parser("add", help="record a dated commitment (or --decision)")
    p_add.add_argument("what", help="what (one sentence)")
    p_add.add_argument("--due", dest="due", default=None, metavar="DAY",
                       help="by when: YYYY-MM-DD, today, tomorrow, monday…")
    p_add.add_argument("--pour", dest="due", action=DeprecatedAlias, canonical="--due",
                       default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_add.add_argument("--project", dest="project", default=None)
    p_add.add_argument("--projet", dest="project", action=DeprecatedAlias,
                       canonical="--project", default=argparse.SUPPRESS,
                       help=argparse.SUPPRESS)
    p_add.add_argument("--owner", dest="owner", default=None,
                       help="who owns it (agent or human:<id>)")
    p_add.add_argument("--porteur", dest="owner", action=DeprecatedAlias, canonical="--owner",
                       default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_add.add_argument("--lot", dest="lot", default=None,
                       help="existing task it is attached to")
    p_add.add_argument("--tache", dest="lot", action=DeprecatedAlias, canonical="--lot",
                       default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_add.add_argument("--card", dest="package", default=None, help="WorkPackage")
    p_add.add_argument("--fiche", dest="package", action=DeprecatedAlias, canonical="--card",
                       default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_add.add_argument("--source-type", dest="source_kind", default="conversation",
                       choices=SOURCE_KINDS)
    p_add.add_argument("--source", dest="source_ref", default=None,
                       help="source identifier or link (conversation, decision, message)")
    p_add.add_argument("--depends-on", dest="depends_on", action=UniqueAppend, default=[],
                       help="dependency (task #N, commitment eN, lot Lxx); "
                            "gate:<name> holds the lot (L368) until the due day; repeatable")
    p_add.add_argument("--depend-de", dest="depends_on", action=DeprecatedAlias,
                       canonical="--depends-on", append=True, default=argparse.SUPPRESS,
                       help=argparse.SUPPRESS)
    p_add.add_argument("--decision", action="store_true",
                       help="decision milestone: an open question waiting for a human")
    p_add.add_argument("--proposed", action="store_true",
                       help="record a proposal, without activating it; date optional")
    p_add.add_argument("--key", default=None, help="stable key for idempotent creation")
    notes = p_add.add_mutually_exclusive_group()
    notes.add_argument("--note", default="")
    notes.add_argument("--note-file", metavar="FILE", help="UTF-8 note; - reads stdin")
    p_add.add_argument("--actor", default="")
    p_add.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="open commitments and proposals")
    p_list.add_argument("--all", action="store_true", help="also kept and cancelled ones")
    p_list.add_argument("--project", default=None, help="project and global commitments")
    p_list.add_argument("--json", action="store_true")

    p_get = sub.add_parser("get", help="read one commitment, its complete note and editing token")
    p_get.add_argument("id", help="e18 or 18")
    p_get.add_argument("--json", action="store_true")

    p_update = sub.add_parser("update", help="edit a proposal or open commitment")
    p_update.add_argument("id", help="e18 or 18")
    for option in ("what", "due", "owner", "project", "lot", "card", "source"):
        p_update.add_argument("--" + option, default=argparse.SUPPRESS,
                             help="new value; - clears a nullable field")
    p_update.add_argument("--kind", choices=KINDS, default=argparse.SUPPRESS)
    p_update.add_argument("--source-type", choices=SOURCE_KINDS, default=argparse.SUPPRESS)
    deps = p_update.add_mutually_exclusive_group()
    deps.add_argument("--depends-on", action=UniqueAppend, default=argparse.SUPPRESS,
                      help="replace dependencies with these references; repeatable")
    deps.add_argument("--clear-dependencies", action="store_true")
    notes = p_update.add_mutually_exclusive_group()
    notes.add_argument("--note", default=argparse.SUPPRESS)
    notes.add_argument("--note-file", metavar="FILE", help="UTF-8 note; - reads stdin")
    p_update.add_argument("--if-match", metavar="ETAG", help="editing token from plan get")
    p_update.add_argument("--json", action="store_true")

    p_export = sub.add_parser("export", help="export editable ameesh-plan/1 JSON")
    p_export.add_argument("--project", default=None)
    p_export.add_argument("--all", action="store_true", help="include terminal commitments")
    p_export.add_argument("--output", default="-", metavar="FILE", help="default: stdout")

    p_import = sub.add_parser("import", help="preview a JSON import; --apply writes atomically")
    p_import.add_argument("file", metavar="FILE", help="ameesh-plan/1 JSON; - reads stdin")
    mode = p_import.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="apply the complete validated file")
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="preview (default)")
    p_import.set_defaults(apply=False)
    p_import.add_argument("--update", action="store_true", help="allow edits to existing keys")
    p_import.add_argument("--actor", default="")
    p_import.add_argument("--json", action="store_true")

    p_validate = sub.add_parser("validate", help="validate stored dependencies or an import file")
    p_validate.add_argument("file", nargs="?", metavar="FILE", help="optional JSON; - reads stdin")
    p_validate.add_argument("--project", default=None, help="stored commitments of a project")
    p_validate.add_argument("--json", action="store_true")

    p_acc = sub.add_parser("accept", help="accept a proposal")
    p_acc.add_argument("id", help="e18 or 18")
    p_acc.add_argument("--due", dest="due", default=None, metavar="DAY")
    p_acc.add_argument("--pour", dest="due", action=DeprecatedAlias, canonical="--due",
                       default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    p_acc.add_argument("--actor", default="")
    for name, help_ in (("done", "commitment kept"),
                        ("cancel", "commitment dropped, or proposal rejected")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("id", help="e18 or 18")
        p.add_argument("--note", default="")
        p.add_argument("--actor", default="")

    p_prop = sub.add_parser("propose", help="proposals drawn from decisions and tasks")
    p_prop.add_argument("--from", dest="from_dir", action="append", default=[],
                        metavar="DIR",
                        help="folder of Decision notes (Markdown) to read besides the canon")
    p_prop.add_argument("--no-canon", action="store_true", help="do not read the canon")
    p_prop.add_argument("--no-tasks", action="store_true",
                        help="do not read open task bodies")
    p_prop.add_argument("--record", action="store_true",
                        help="record the proposals (status proposed, to review)")
    p_prop.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="the roadmap as a text Gantt")
    p_show.add_argument("--project", default=None)
    p_show.add_argument("--width", type=int, default=None)
    p_show.add_argument("--json", action="store_true", help="ameesh-roadmap/1 schema")
    return parser


def _print_commitment(c: dict) -> None:
    print("e%-4d %-10s %-9s %-10s %s%s%s" % (
        c["id"], c.get("due_on") or "undated", c["status"],
        "decision" if c["kind"] == "decision" else "commitment", c["what"],
        " [task #%d]" % c["work_item_id"] if c.get("work_item_id") else "",
        " (source: %s %s)" % (_FR_SOURCE.get(c["source_kind"], c["source_kind"]),
                               c["source_ref"]) if c.get("source_ref") else ""))


def _read_plan_text(filename: str) -> str:
    """Bound file/stdin reads before taking any database locks."""
    from .roadmap_edit import MAX_BYTES
    try:
        if filename == "-":
            text = sys.stdin.read(MAX_BYTES + 1)
        else:
            # Universal newlines would rewrite CRLF notes to LF: a note is data.
            with Path(filename).open(encoding="utf-8", newline="") as stream:
                text = stream.read(MAX_BYTES + 1)
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise RoadmapError("file exceeds 10 MiB")
        return text
    except (OSError, UnicodeError) as exc:
        raise RoadmapError("cannot read %s: %s" % (filename, exc)) from None


def _write_atomic(filename: str, text: str) -> None:
    """Write the export beside its target, then replace it in one step."""
    target = Path(filename)
    # A symlinked target is written through, as a plain ``write_text`` did:
    # replacing the link itself would silently turn it into a regular file.
    if target.is_symlink():
        target = Path(os.path.realpath(target))
    tmp = None
    try:
        # New exports honour the umask. Replacements inherit the target's
        # permissions from creation, before any private content is written.
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            mode = 0o666
        fd = None
        for _ in range(16):
            candidate = str(target.parent / ("%s.%s.tmp" % (target.name, secrets.token_hex(8))))
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            except FileExistsError:
                continue
            tmp = candidate
            break
        if fd is None:
            raise OSError("cannot create a temporary file beside %s" % target)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists():
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    except OSError as exc:
        if tmp is not None and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise RoadmapError("cannot write %s: %s" % (filename, exc)) from None


def run(db, args, *, cfg=None, now: float | None = None) -> int:
    from . import roadmap_edit as edit

    command = args.plan_command
    if command == "add":
        fields = dict(what=args.what, due=args.due, kind="decision" if args.decision else "commitment",
                      status="proposed" if args.proposed else "open", owner=args.owner,
                      project=args.project, lot=args.lot, card=args.package,
                      source_type=args.source_kind, source=args.source_ref,
                      depends_on=args.depends_on, note=_read_plan_text(args.note_file)
                      if args.note_file else args.note)
        if args.key is not None:
            fields["key"] = args.key
        row = edit.create(db, fields, actor=args.actor, now=now)
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2))
        else:
            print("commitment e%d %s: %s%s" % (
                row["id"], "unchanged" if row.get("unchanged") else "recorded", row["what"],
                " due %s" % row["due_on"] if row["due_on"] else ""))
        return 0
    if command == "list":
        rows = edit.listing(db, project=args.project, statuses=None if args.all else LIVE_STATUSES)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("no commitment")
        for row in rows:
            _print_commitment(row)
        return 0
    if command in ("get", "update"):
        if command == "get":
            row = edit.get(db, args.id)
        else:
            fields = {name: getattr(args, name) for name in edit.EDITABLE_FIELDS if hasattr(args, name)}
            for key in ("due", "owner", "project", "lot", "card", "source"):
                if fields.get(key) == "-":
                    fields[key] = None
            if args.clear_dependencies:
                fields["depends_on"] = []
            if args.note_file:
                fields["note"] = _read_plan_text(args.note_file)
            warnings = []
            row = edit.update(db, args.id, fields, if_match=args.if_match, now=now,
                              warnings=warnings)
            for warning in warnings:
                print("warning: " + json.dumps(warning, ensure_ascii=False), file=sys.stderr)
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2))
        else:
            _print_commitment(row)
            for key in ("owner", "project", "package_id", "source_kind", "source_ref", "proposal_key", "etag"):
                print("%s: %s" % (key, row.get(key) or "—"))
            print("depends_on: %s" % ", ".join(row.get("depends_on") or []))
            print("note:\n%s" % row["note"])
        return 0
    if command == "export":
        text = json.dumps(edit.export(db, project=args.project, all_states=args.all), ensure_ascii=False, indent=2) + "\n"
        if args.output == "-":
            print(text, end="")
        else:
            _write_atomic(args.output, text)
        return 0
    if command in ("import", "validate"):
        if command == "validate" and not args.file:
            report = edit.validate(db, project=args.project)
        elif command == "validate":
            try:
                if args.project:
                    raise RoadmapError("--project applies to stored commitments; omit it with a file")
                body = edit.document(_read_plan_text(args.file))
                preview = edit.import_plan(db, body, update_existing=True)
                report = dict(preview, schema="ameesh-plan-validation/1", valid=True, errors=[])
            except RoadmapError as exc:
                detail = exc.report if isinstance(exc, edit.InvalidDependencies) else {
                    "valid": False, "errors": [{"code": "invalid_plan", "message": str(exc)}], "warnings": []}
                report = dict(detail, schema="ameesh-plan-validation/1")
        else:
            body = edit.document(_read_plan_text(args.file))
            report = edit.import_plan(db, body, apply=args.apply, update_existing=args.update, actor=args.actor)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        elif command == "validate":
            print("dependencies valid" if report.get("valid", True) else "invalid dependencies")
            for issue in report.get("errors", []) + report.get("warnings", []):
                print(json.dumps(issue, ensure_ascii=False))
        else:
            print("import applied" if report["applied"] else "import preview (no writes; use --apply)")
            for change in report["changes"]:
                print("%s %s" % (change["action"], change["key"]))
            for warning in report["warnings"]:
                print("warning: " + json.dumps(warning, ensure_ascii=False))
        return 0 if report.get("valid", True) else 1
    if command == "accept":
        row = accept(db, edit.ident(args.id), due=args.due, actor=args.actor, now=now)
        print("proposal e%d accepted: %s%s" % (row["id"], row["what"], " due %s"
                                               % row["due_on"] if row["due_on"] else ""))
        return 0
    if command in ("done", "cancel"):
        row = settle(db, edit.ident(args.id), "done" if command == "done" else "cancelled",
                     note=args.note, actor=args.actor)
        print("commitment e%d %s" % (row["id"], "kept" if row["status"] == "done"
                                     else "cancelled"))
        return 0
    if command == "propose":
        from . import roadmap_propose
        return roadmap_propose.run(db, args, cfg=cfg, now=now)
    if command == "show":
        view = build(db, now=now, project=args.project)
        if args.json:
            view = dict(view, who=who(db, now=now, project=args.project))
            print(json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        print(format_gantt(view, args.width, who=who(db, now=now, project=args.project)))
        return 0
    print("error: unknown subcommand: %s" % command, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    cfg = config_mod.load()
    try:
        db = db_mod.connect(cfg)
        try:
            db_mod.require_schema(db)
            return run(db, args, cfg=cfg)
        finally:
            db.close()
    except work_mod.WorkError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
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
