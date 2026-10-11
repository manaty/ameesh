# SPDX-License-Identifier: AGPL-3.0-only
"""Feuille de route : dates prévues, engagements datés, Gantt (lot L96).

  ameesh work plan <tâche|fiche> [--debut J] [--fin J] [--livraison J] [--source S]
  ameesh plan add "quoi" --pour J [--projet P] [--porteur X] [--lot N] [--fiche F]
                  [--source-type conversation|decision|thread|task|other] [--source S]
                  [--depend-de REF …] [--decision]
  ameesh plan list [--all] [--json]
  ameesh plan accept <id> [--pour J] | done <id> | cancel <id> [--note …]
  ameesh plan propose [--from DOSSIER] [--no-canon] [--no-tasks] [--record] [--json]
  ameesh plan show [--json] [--project P] [--width N]

Demande du propriétaire (2026-10-10) : « voir sur quel sujet les agents
avancent, et un Gantt avec la roadmap, les dates de déploiement prévues » ;
« ameesh doit se servir de ce genre de décision [« on fera ça lundi »] pour
établir la roadmap ».

* **Dates prévues** : une tâche (`work_items`) et une fiche du plan (epic,
  jalon) portent un début, une fin et une livraison (ou un déploiement)
  prévus. Pour une fiche, les dates posées dans ameesh priment sur celles du
  canon (clés `start`, `end`, `delivery`, ou `date` d'un jalon).
* **Engagements** : quoi, pour quand, porteur, projet, tâche ou fiche
  rattachée, et la source (conversation, décision du canon, message du fil,
  tâche) avec son identifiant. `--decision` : un **jalon de décision**, une
  question ouverte qui attend un humain (sa date peut manquer).
* **Propositions** (`plan propose`) : lues dans les décisions du canon (et
  leurs conséquences : « lots Lxx », « même vague que… », « lundi ») et dans
  le corps des tâches ouvertes (une date écrite seulement là). Ce sont des
  PROPOSITIONS : rien n'est créé sans `--record`, et une proposition
  enregistrée reste `proposed` jusqu'à `plan accept`.
* **Gantt** (`plan show`, et la section `roadmap` de `ameesh progress`) :
  epics, tâches, jalons et engagements sur une frise, le prévu face au réel
  (jalons réels déjà mesurés : demandée, en cours, soumise, verdict,
  livrée), la ligne du jour, les retards, et la source de chaque élément.

Ce module appartient au cœur générique : il ne nomme aucun état ni jalon de
métier ; la traduction du cycle actuel tient dans `roadmap_dev.CORRESPONDANCES`.
Le JSON suit le schéma `ameesh-roadmap/1` (docs/PROGRESS.md).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shutil
import sys
import textwrap
import time

from . import config as config_mod
from . import db as db_mod
from . import estimates as estimates_mod
from . import plan as plan_mod
from . import roadmap_dev as dev
from . import storage
from . import work as work_mod

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
LABELS = {"demandee": "demandée", "debut": "en cours", "soumise": "soumise",
          "verdict": "verdict", "livree": "livrée", "abandonnee": "abandonnée",
          "start": "début", "end": "fin", "delivery": "livraison"}
_FR_STATE = {"en_cours": "en cours", "a_valider": "à valider", "validee": "validée",
             "bloquee": "bloquée", "livree": "livrée", "abandonnee": "abandonnée",
             "demandee": "demandée"}
_FR_SOURCE = {"conversation": "conversation", "decision": "décision", "thread": "fil",
              "task": "tâche", "other": "autre", "canon": "canon", "ameesh": "ameesh",
              "mixte": "canon et ameesh"}
_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

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
    try:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            raise ValueError(raw)
        return _dt.date.fromisoformat(raw).isoformat()
    except ValueError:
        raise RoadmapError("date illisible : %r (AAAA-MM-JJ, aujourd'hui, demain, lundi…%s)"
                           % (raw, ", ou - pour effacer" if allow_clear else ""))


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
        raise RoadmapError("rien à planifier : --debut, --fin ou --livraison (ou --source ; "
                           "--estimate pour la durée d'une tâche)")
    text = str(target).strip().lstrip("#")
    st = storage.of(db)
    if text.isdigit():
        current = (st.roadmap.item_plans([int(text)]) or [None])[0]
        if current is None:
            raise RoadmapError("tâche %s introuvable" % text)
        _check_order(dict(current, **{"planned_" + k: v for k, v in dates.items()}))
        row = st.roadmap.plan_item(int(text), dates, source=source, actor=actor)
        if row is None:
            raise RoadmapError("tâche %s introuvable" % text)
        return {"target": int(text), "kind": "task", "plan": row}
    package = st.packages.get(text)
    if package is None:
        raise RoadmapError("ni tâche ni fiche WorkPackage : %r (une tâche : son numéro ; une "
                           "fiche : son identifiant, synchronisée par « ameesh canon sync »)"
                           % text)
    _check_order(dict(package, **{"planned_" + k: v for k, v in dates.items()}))
    row = st.roadmap.plan_package(text, dates, source=source, actor=actor)
    if row is None:
        raise RoadmapError("fiche %s introuvable" % text)
    return {"target": text, "kind": "package", "plan": row}


def _check_order(row: dict) -> None:
    start, end = _date(row.get("planned_start")), _date(row.get("planned_end"))
    if start and end and start > end:
        raise RoadmapError("début prévu (%s) après la fin prévue (%s)" % (start, end))


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
        raise RoadmapError("engagement vide : dites quoi")
    if source_kind not in SOURCE_KINDS:
        raise RoadmapError("type de source inconnu : %r (%s)" % (source_kind,
                                                                 ", ".join(SOURCE_KINDS)))
    day = parse_day(due, now=now) if due not in (None, "") else None
    if day is None and not decision:
        raise RoadmapError("un engagement a une date : --pour AAAA-MM-JJ (ou lundi, demain…)")
    st = storage.of(db)
    item_id = None
    if lot not in (None, ""):
        text = str(lot).strip().lstrip("#")
        item = work_mod.get(db, int(text)) if text.isdigit() else None
        if item is None:
            raise RoadmapError("tâche %s introuvable" % lot)
        item_id = int(item["id"])
        project = project or item.get("app") or item.get("workstream") or None
        owner = owner or item.get("assignee") or None
    if package:
        if st.packages.get(package) is None:
            raise RoadmapError("fiche WorkPackage inconnue : %r" % package)
    return st.roadmap.add_commitment({
        "what": what, "due_on": day, "kind": "decision" if decision else "commitment",
        "status": "open", "owner": owner or None, "project": project or None,
        "work_item_id": item_id, "package_id": package or None, "source_kind": source_kind,
        "source_ref": source_ref or None, "depends_on": [str(d) for d in depends_on or ()],
        "note": note or "", "created_by": actor or ""})


def _commitment(db, ident) -> dict:
    rows = storage.of(db).roadmap.commitments(ids=[int(ident)])
    if not rows:
        raise RoadmapError("engagement %s introuvable" % ident)
    return rows[0]


def accept(db, ident, *, due=None, actor: str = "", now: float | None = None) -> dict:
    """Valide une proposition : elle devient un engagement ouvert. Sans date
    (proposition tirée d'une décision sans échéance), `--pour` la fixe."""
    row = _commitment(db, ident)
    if row["status"] != "proposed":
        raise RoadmapError("engagement %s : %s, pas une proposition" % (ident, row["status"]))
    values: dict = {"status": "open"}
    if due not in (None, ""):
        values["due_on"] = parse_day(due, now=now)
    if not values.get("due_on") and not row.get("due_on") and row["kind"] != "decision":
        raise RoadmapError("proposition %s sans date : --pour AAAA-MM-JJ" % ident)
    if actor:
        values["note"] = (row.get("note") + " ; " if row.get("note") else "") + \
            "validée par %s" % actor
    out = storage.of(db).roadmap.set_commitment(int(ident), current=("proposed",),
                                                values=values)
    if out is None:
        raise RoadmapError("engagement %s modifié entre-temps : réessayez" % ident)
    return out


def settle(db, ident, status: str, *, note: str = "", actor: str = "") -> dict:
    """`done` (tenu) ou `cancelled` (abandonné, ou proposition rejetée)."""
    if status not in ("done", "cancelled"):
        raise RoadmapError("statut %r refusé" % status)
    row = _commitment(db, ident)
    if row["status"] not in LIVE_STATUSES:
        raise RoadmapError("engagement %s déjà %s" % (ident, row["status"]))
    text = " ; ".join(t for t in (row.get("note"), note.strip() if note else "",
                                  "par %s" % actor if actor else "") if t)
    out = storage.of(db).roadmap.set_commitment(
        int(ident), current=LIVE_STATUSES, values={"status": status, "note": text})
    if out is None:
        raise RoadmapError("engagement %s modifié entre-temps : réessayez" % ident)
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
                    declared: list[dict] | None) -> dict:
    """Les jalons RÉELS génériques d'une tâche, depuis ce qu'ameesh mesure
    déjà (journal, table des jalons, actions) : `{demandee, debut, soumise,
    verdict, verdict_kind, livree}` en secondes epoch (None si pas atteint)."""
    from . import progress  # import tardif : progress importe ce module

    jalons = progress._jalons_de_lot(item, events, actions, declared)
    started = next((e.get("created_ts") for e in events if dev.en_cours(e.get("state"))), None)
    out = {key: jalons.get(dev.jalon(key)) for key in ("demandee", "soumise", "verdict",
                                                       "livree")}
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
               now: float | None = None) -> dict:
    state = item.get("state")
    delivered = dev.livree(state)
    abandoned = dev.abandonnee(state)
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
            late = dict(c["late"], what="engagement #%d" % c["id"])
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
        "commitments": [c["id"] for c in commitments],
        # L157 : durée estimée, réel mesuré (début → livraison), écart
        "estimate": estimates_mod.view(item, merged_ts=real.get("livree") if delivered
                                       else None, now=now),
    }


def _commitment_view(row: dict, base: _dt.date, items: dict) -> dict:
    item = items.get(row.get("work_item_id")) if row.get("work_item_id") else None
    fulfilled = row["status"] == "done" or bool(item and dev.livree(item.get("state")))
    due = _date(row.get("due_on"))
    late = None
    if row["status"] == "open" and row["kind"] == "commitment" and not fulfilled \
            and due and base > due:
        late = {"what": "engagement", "due": due.isoformat(), "days": (base - due).days,
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
        "units_delivered": tally.get("lots_merged", 0), "late": late, "source": source,
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
    done_states = (dev.CORRESPONDANCES["etats_livres"]
                   + dev.CORRESPONDANCES["etats_abandonnes"])
    rows = st.roadmap.items(since_ts=now - recent_s, limit=max_items, done_states=done_states)
    if project:
        rows = [r for r in rows if project in (r.get("app"), r.get("workstream"))]
    ids = [int(r["id"]) for r in rows]
    events: dict = {}
    for ev in st.progress.lot_events(ids):
        events.setdefault(int(ev["work_item_id"]), []).append(ev)
    actions: dict = {}
    for act in st.progress.lot_actions(ids):
        actions.setdefault(int(act["work_item"]), []).append(act)
    declared: dict = {}
    for ms in st.progress.lot_milestones(ids):
        declared.setdefault(int(ms["work_item_id"]), []).append(ms)
    packages = st.progress.packages()
    by_pkg = {p["id"]: p for p in packages}
    summary = plan_mod.summarize(packages, st.progress.package_items())

    commitment_rows = [c for c in st.roadmap.commitments()
                       if c["status"] in LIVE_STATUSES
                       or (c.get("closed_ts") or 0) >= now - recent_s]
    items_by_id = {int(r["id"]): r for r in rows}
    missing = sorted({int(c["work_item_id"]) for c in commitment_rows
                      if c.get("work_item_id") and int(c["work_item_id"]) not in items_by_id})
    for extra in (work_mod.get(db, i) for i in missing):
        if extra:
            items_by_id[int(extra["id"])] = extra
    commitments = [_commitment_view(c, base, items_by_id) for c in commitment_rows]
    if project:
        commitments = [c for c in commitments if c["project"] in (None, project)
                       or (c["lot"] in items_by_id)]
    by_item: dict = {}
    for c in commitments:
        if c["lot"]:
            by_item.setdefault(c["lot"], []).append(c)

    tasks = []
    for item in rows:
        iid = int(item["id"])
        evs, acts = events.get(iid, []), actions.get(iid, [])
        real = real_milestones(item, evs, acts, declared.get(iid))
        jalons = progress._jalons_de_lot(item, evs, acts, declared.get(iid))
        shown = progress.lot_state(item.get("state") or "", jalons)
        tasks.append(build_task(item, real, base=base,
                                epic=plan_mod.epic_of(item.get("package_id"), by_pkg),
                                commitments=[c for c in by_item.get(iid, [])
                                             if c["kind"] == "commitment"],
                                shown_state=shown, now=now))

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
            if item and (dev.livree(item.get("state")) or dev.abandonnee(item.get("state"))):
                continue                  # tâche livrée : engagement tenu
        out.append({"reason": "engagement", "what": row["what"], "due": due.isoformat(),
                    "days": (base - due).days, "commitment": row["id"], "lot": lot,
                    "package": row.get("package_id"), "owner": row.get("owner"),
                    "assignee": (item or {}).get("assignee"), "project": row.get("project")})
    for item in rows:
        key = "planned_delivery" if item.get("planned_delivery") else (
            "planned_end" if item.get("planned_end") else None)
        due = _date(item.get(key)) if key else None
        if not due or base <= due:
            continue
        out.append({"reason": "date_prevue", "what": "%s de la tâche #%d « %s »" % (
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
        out.append({"reason": "date_prevue", "what": "%s de %s « %s »" % (
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
LEGEND = ("░ prévu · █ réel (en cours → livrée) · ▓ réel après la date prévue · "
          "◆ livraison prévue · ● livrée · ◇ échéance · ? décision attendue · │ aujourd'hui")


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
        return "RETARD %d j (%s %s)" % (late["days"], late["what"], _dm(late["due"]))
    if late:
        return "livrée avec %d j de retard" % late["days"]
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

    wrap("FEUILLE DE ROUTE%s — aujourd'hui %s · 1 colonne = %d jour%s" % (
        " de %s" % view["project"] if view.get("project") else "", _dm(base), step,
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
        return ("prévu : " + ", ".join(parts)) if parts else "sans date prévue"

    sections = (
        ("JALONS", view["milestones"]),
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
                "%d/%d livrée(s)" % (p["units_delivered"], p["units_total"])
                if p["units_total"] else "",
                _flag(p), "source : %s" % src["label"] if src.get("label") else ""])
    out.append("")
    out.append("TÂCHES (%d)" % len(view["tasks"]))
    if not view["tasks"]:
        out.append("  aucune tâche ouverte ni datée")
    for t in view["tasks"]:
        bar = _Bar(lo, cols, step)
        _task_bar(bar, t, base)
        real = t["real"]
        reached = [LABELS[k] + " " + _dm(_day_of(real[k])) for k in REAL_MILESTONES
                   if real.get(k)]
        src = t.get("source") or {}
        row("#%d %s" % (t["id"], t["title"]), bar, [
            _FR_STATE.get(t["state"], t["state"]),
            t["assignee"] or "",
            planned_text(t["planned"]),
            ("réel : " + ", ".join(reached)) if reached else "",
            (t.get("estimate") or {}).get("label") if (t.get("estimate") or {}).get("minutes")
            else "",
            _flag(t),
            "source : %s" % src["label"] if src.get("label") else "",
            "engagement(s) %s" % ", ".join("e%d" % i for i in t["commitments"])
            if t["commitments"] else ""])
    if view["commitments"]:
        out.append("")
        out.append("ENGAGEMENTS (%d)" % len(view["commitments"]))
        for c in view["commitments"]:
            bar = _Bar(lo, cols, step)
            bar.mark(_date(c["due"]), GLYPHS["delivered"] if c["fulfilled"] else GLYPHS["due"])
            src = c.get("source") or {}
            status = {"proposed": "PROPOSÉ (à valider)", "open": "ouvert",
                      "done": "tenu", "cancelled": "annulé"}[c["status"]]
            if c["fulfilled"] and c["status"] == "open":
                status = "tenu (tâche livrée)"
            row("e%d %s" % (c["id"], c["what"]), bar, [
                "pour %s" % _dm(c["due"]) if c["due"] else "sans date", status,
                c.get("owner") or "", "projet %s" % c["project"] if c.get("project") else "",
                "tâche #%d" % c["lot"] if c.get("lot") else "",
                "dépend de %s" % ", ".join(c["depends_on"]) if c["depends_on"] else "",
                _flag(c), "source : %s" % src["label"] if src.get("label") else ""])
    if view["decisions"]:
        out.append("")
        out.append("DÉCISIONS ATTENDUES (%d)" % len(view["decisions"]))
        for d in view["decisions"]:
            bar = _Bar(lo, cols, step)
            bar.mark(_date(d.get("due")) or base, GLYPHS["decision"])
            src = d.get("source") or {}
            label = ("e%d " % d["id"]) if d.get("id") else ("#%d " % d["lot"])
            row(label + d["what"], bar, [
                "pour %s" % _dm(d["due"]) if d.get("due") else "sans date",
                "PROPOSÉ (à valider)" if d.get("status") == "proposed" else "",
                d.get("owner") or "",
                "source : %s" % src["label"] if src.get("label") else ""])
    cut = (view.get("truncated") or {}).get("tasks")
    if cut:
        out.append("")
        wrap("TRONQUÉ : %d tâches lues sur %d (borne %d)" % (cut["shown"], cut["total"],
                                                             cut["limit"]))
    if view["late"]:
        out.append("")
        wrap("EN RETARD (%d) : %s" % (len(view["late"]), " ; ".join(
            "%s %s (%d j)" % ({"task": "#%s" % l["id"], "commitment": "e%s" % l["id"]}
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
    out = ["QUI AVANCE SUR QUOI"]
    agents = [a for p in view["projects"] for a in p["agents"] if a["active"]]
    if not agents:
        out.append("  aucun agent actif")
    for a in agents:
        doing = ("#%s %s" % (a["lot"]["id"], a["lot"]["title"])) if a.get("lot") else (
            a.get("task") or "—")
        since = a.get("on_task_since_ts") or a.get("since_ts")
        head = "%s [%s] %s — %s%s" % (
            a["name"], a["project"] or "sans projet", {"working": "au travail",
                                                       "paused": "en pause",
                                                       "idle": "au repos"}.get(a["state"],
                                                                               a["state"]),
            doing, " (depuis %s)" % work_mod.span(now - since) if since else "")
        out.extend(textwrap.wrap(head, width, initial_indent="  ", subsequent_indent="    "))
        last = a.get("last_update")
        if last:
            out.extend(textwrap.wrap("dernière avancée (il y a %s) : %s" % (
                work_mod.span(now - last["ts"]), last["text"]), width,
                initial_indent="    ", subsequent_indent="      "))
    return out


# --------------------------------------------------------------------------
# entrée : `ameesh plan …`
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh plan", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="plan_command", required=True)

    p_add = sub.add_parser("add", help="enregistrer un engagement daté (ou --decision)")
    p_add.add_argument("what", help="quoi (une phrase)")
    p_add.add_argument("--pour", dest="due", default=None,
                       help="pour quand : AAAA-MM-JJ, aujourd'hui, demain, lundi…")
    p_add.add_argument("--projet", "--project", dest="project", default=None)
    p_add.add_argument("--porteur", "--owner", dest="owner", default=None,
                       help="qui le porte (agent ou human:<id>)")
    p_add.add_argument("--lot", "--tache", dest="lot", default=None,
                       help="tâche existante à laquelle il se rattache")
    p_add.add_argument("--fiche", dest="package", default=None, help="fiche WorkPackage")
    p_add.add_argument("--source-type", dest="source_kind", default="conversation",
                       choices=SOURCE_KINDS)
    p_add.add_argument("--source", dest="source_ref", default=None,
                       help="identifiant ou lien de la source (conversation, décision, message)")
    p_add.add_argument("--depend-de", dest="depends_on", action="append", default=[],
                       help="dépendance (tâche #N, engagement eN, lot Lxx) ; répétable")
    p_add.add_argument("--decision", action="store_true",
                       help="jalon de décision : une question ouverte qui attend un humain")
    p_add.add_argument("--note", default="")
    p_add.add_argument("--actor", default="")
    p_add.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="engagements ouverts et propositions")
    p_list.add_argument("--all", action="store_true", help="aussi les tenus et annulés")
    p_list.add_argument("--json", action="store_true")

    p_acc = sub.add_parser("accept", help="valider une proposition")
    p_acc.add_argument("id", type=int)
    p_acc.add_argument("--pour", dest="due", default=None)
    p_acc.add_argument("--actor", default="")
    for name, help_ in (("done", "engagement tenu"),
                        ("cancel", "engagement abandonné, ou proposition rejetée")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("id", type=int)
        p.add_argument("--note", default="")
        p.add_argument("--actor", default="")

    p_prop = sub.add_parser("propose", help="propositions tirées des décisions et des tâches")
    p_prop.add_argument("--from", dest="from_dir", action="append", default=[],
                        help="dossier de fiches Decision (Markdown) à lire en plus du canon")
    p_prop.add_argument("--no-canon", action="store_true", help="ne pas lire le canon")
    p_prop.add_argument("--no-tasks", action="store_true",
                        help="ne pas lire le corps des tâches ouvertes")
    p_prop.add_argument("--record", action="store_true",
                        help="enregistrer les propositions (statut proposed, à valider)")
    p_prop.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="la feuille de route en Gantt texte")
    p_show.add_argument("--project", default=None)
    p_show.add_argument("--width", type=int, default=None)
    p_show.add_argument("--json", action="store_true", help="schéma ameesh-roadmap/1")
    return parser


def _print_commitment(c: dict) -> None:
    print("e%-4d %-10s %-9s %-10s %s%s%s" % (
        c["id"], c.get("due_on") or "sans date", c["status"],
        "décision" if c["kind"] == "decision" else "engagement", c["what"],
        " [tâche #%d]" % c["work_item_id"] if c.get("work_item_id") else "",
        " (source : %s %s)" % (_FR_SOURCE.get(c["source_kind"], c["source_kind"]),
                               c["source_ref"]) if c.get("source_ref") else ""))


def run(db, args, *, cfg=None, now: float | None = None) -> int:
    command = args.plan_command
    if command == "add":
        row = add_commitment(
            db, args.what, due=args.due, decision=args.decision, owner=args.owner,
            project=args.project, lot=args.lot, package=args.package,
            source_kind=args.source_kind, source_ref=args.source_ref,
            depends_on=args.depends_on, note=args.note, actor=args.actor, now=now)
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2))
        else:
            print("engagement e%d enregistré : %s%s" % (
                row["id"], row["what"], " pour le %s" % row["due_on"] if row["due_on"] else ""))
        return 0
    if command == "list":
        rows = storage.of(db).roadmap.commitments(statuses=None if args.all else LIVE_STATUSES)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("aucun engagement")
        for row in rows:
            _print_commitment(row)
        return 0
    if command == "accept":
        row = accept(db, args.id, due=args.due, actor=args.actor, now=now)
        print("proposition e%d validée : %s%s" % (row["id"], row["what"], " pour le %s"
                                                  % row["due_on"] if row["due_on"] else ""))
        return 0
    if command in ("done", "cancel"):
        row = settle(db, args.id, "done" if command == "done" else "cancelled",
                     note=args.note, actor=args.actor)
        print("engagement e%d %s" % (row["id"], "tenu" if row["status"] == "done"
                                     else "annulé"))
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
    print("erreur : sous-commande inconnue : %s" % command, file=sys.stderr)
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
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
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
