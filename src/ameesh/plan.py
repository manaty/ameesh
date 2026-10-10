# SPDX-License-Identifier: AGPL-3.0-only
"""Plan de travail (lot L29) : epics et progression ; annotation des lots.

Le plan est **déclaré au canon** (fiches `WorkPackage` : jalon → epic → lot,
recopiées dans `work_packages` par `canon sync`) ; l'**état d'exécution**
reste dans `work_items` (décision 0005). Ce module ne fait que lire et
dériver, pour `ameesh work list/show` et `ameesh progress` :

* **l'epic d'un lot** : la première fiche `epic` en remontant les parents
  depuis la fiche du lot ;
* **la progression d'un epic** (et d'un jalon) : ses unités — chaque fiche
  `lot` dessous, plus chaque lot (`work_items`) rattaché directement à
  l'epic — fusionnées sur le total ; une unité abandonnée sort du
  dénominateur ;
* **l'annotation de `work list` / `work show`** : epic, plus la stagnation
  et l'attente (quoi, qui), calculées par le module unique `stagnation`
  (partagé avec l'alerte `stale_lot` et `ameesh progress`).
"""
from __future__ import annotations

from typing import Iterable

from . import stagnation, storage
from . import work as work_mod

MERGE_CONNECTOR = "git-merge"


# --------------------------------------------------------------------------
# fiches : epic, jalon, progression
# --------------------------------------------------------------------------

def _ancestor(package_id: str | None, packages: dict, kind: str) -> str | None:
    seen: set = set()
    current = packages.get(package_id) if package_id else None
    while current is not None and current["id"] not in seen:
        if current.get("kind") == kind:
            return current["id"]
        seen.add(current["id"])
        current = packages.get(current.get("parent"))
    return None


def epic_of(package_id: str | None, packages: dict) -> str | None:
    """L'epic d'une fiche (elle-même si c'est un epic), ou None."""
    return _ancestor(package_id, packages, "epic")


def milestone_of(package_id: str | None, packages: dict) -> str | None:
    return _ancestor(package_id, packages, "milestone")


def item_unit_status(state: str) -> str:
    """merged | abandoned | open, pour un lot (`work_items`)."""
    if state in work_mod.MERGED_STATES:
        return "merged"
    if state == "closed":
        return "abandoned"
    return "open"


def package_status(items: list[dict]) -> str:
    """État d'une fiche `lot` d'après ses lots : pending (aucun lot créé),
    merged (tous ses lots vivants fusionnés), abandoned (tous fermés), open."""
    if not items:
        return "pending"
    live = [i for i in items if i.get("state") != "closed"]
    if not live:
        return "abandoned"
    if all(i.get("state") in work_mod.MERGED_STATES for i in live):
        return "merged"
    return "open"


def _tally(units: list[str]) -> dict:
    merged = units.count("merged")
    abandoned = units.count("abandoned")
    counted = len(units) - abandoned
    return {"lots_total": len(units), "lots_merged": merged, "lots_abandoned": abandoned,
            "lots_open": units.count("open"), "lots_pending": units.count("pending"),
            "progress": round(merged / counted, 4) if counted else None}


def summarize(packages: Iterable[dict], items: Iterable[dict]) -> dict:
    """Epics et jalons du plan, avec leur progression.

    `packages` : fiches (id, kind, title, parent, responsible, status…) ;
    `items` : lots rattachés (id, package_id, state). Rend
    `{"epics": [...], "milestones": [...], "lots": {id_fiche: statut}}`.
    """
    by_id = {p["id"]: p for p in packages}
    by_package: dict = {}
    for item in items:
        by_package.setdefault(item.get("package_id"), []).append(item)
    lot_status = {pid: package_status(by_package.get(pid, []))
                  for pid, p in by_id.items() if p.get("kind") == "lot"}

    def units_under(root: str) -> tuple[list[dict], list[dict], list[str]]:
        lots = [p for p in by_id.values() if p.get("kind") == "lot"
                and _ancestor(p.get("parent"), by_id, by_id[root]["kind"]) == root]
        holders = [root] + [p["id"] for p in by_id.values() if p.get("kind") == "epic"
                            and by_id[root]["kind"] == "milestone"
                            and milestone_of(p["id"], by_id) == root]
        direct = [i for h in holders for i in by_package.get(h, [])]
        statuses = [lot_status[p["id"]] for p in lots] + [
            item_unit_status(i.get("state") or "") for i in direct]
        return lots, direct, statuses

    epics = []
    for pid in sorted(p for p in by_id if by_id[p].get("kind") == "epic"):
        epic = by_id[pid]
        lots, direct, statuses = units_under(pid)
        epics.append(dict(
            id=pid, title=epic.get("title"), milestone=milestone_of(pid, by_id),
            responsible=epic.get("responsible"), status=epic.get("status"),
            canon_ref=epic.get("canon_ref"),
            planned=planned_dates(epic),         # L96
            lots=[{"id": p["id"], "title": p.get("title"), "status": lot_status[p["id"]],
                   "work_items": [int(i["id"]) for i in by_package.get(p["id"], [])]}
                  for p in sorted(lots, key=lambda p: p["id"])],
            work_items=[int(i["id"]) for i in direct],
            **_tally(statuses)))
    milestones = []
    for pid in sorted(p for p in by_id if by_id[p].get("kind") == "milestone"):
        ms = by_id[pid]
        _lots, direct, statuses = units_under(pid)
        # L96 : la date du jalon (posée dans ameesh, sinon déclarée au canon)
        day = planned_dates(ms)["delivery"]
        milestones.append(dict(
            id=pid, title=ms.get("title"), at_ts=day_start_ts(day), date=day,
            status=ms.get("status"),
            responsible=ms.get("responsible"), canon_ref=ms.get("canon_ref"),
            epics=[e["id"] for e in epics if e["milestone"] == pid],
            **_tally(statuses)))
    return {"epics": epics, "milestones": milestones, "lots": lot_status}


# --------------------------------------------------------------------------
# dates prévues d'une fiche (L96)
# --------------------------------------------------------------------------

#: dates d'une fiche : (clé, colonne posée dans ameesh, colonne du canon)
PACKAGE_DATES = (("start", "planned_start", "start_on"), ("end", "planned_end", "end_on"),
                 ("delivery", "planned_delivery", "delivery_on"))


def planned_dates(package: dict | None) -> dict:
    """Les dates prévues effectives d'une fiche : celles posées dans ameesh
    (`ameesh work plan <fiche>`) priment, sinon celles du canon. Rend
    `{start, end, delivery, source}` — `source` : `ameesh`, `canon`, `mixte`
    ou None."""
    package = package or {}
    out: dict = {}
    origins = set()
    for key, mine, canon in PACKAGE_DATES:
        if package.get(mine):
            out[key] = package[mine]
            origins.add("ameesh")
        elif package.get(canon):
            out[key] = package[canon]
            origins.add("canon")
        else:
            out[key] = None
    out["source"] = (origins.pop() if len(origins) == 1 else "mixte") if origins else None
    return out


def day_start_ts(day: str | None) -> float | None:
    """Minuit local d'un jour ISO, en secondes epoch (None si pas de jour)."""
    if not day:
        return None
    import datetime as _dt
    import time as _time
    try:
        moment = _dt.date.fromisoformat(str(day)[:10])
    except ValueError:
        return None
    return float(_time.mktime(moment.timetuple()))


# --------------------------------------------------------------------------
# annotation des lignes de `work list` / `work show`
# --------------------------------------------------------------------------

def annotate(db, rows: list[dict], *, now: float | None = None,
             threshold: float = stagnation.DEFAULT_THRESHOLD_S) -> list[dict]:
    """Ajoute à chaque lot : `epic`, `waiting_for` (`{what, who, label}`),
    `stale`, `last_activity_ts` — calculés par le module `stagnation`, comme
    l'alerte `stale_lot` et `ameesh progress` — et `delegation` (L40,
    `work.delegation_view`)."""
    packages = {p["id"]: p for p in storage.of(db).packages.all(include_absent=True)}
    described = stagnation.describe(db, rows, now=now, threshold=threshold)
    for row in rows:
        info = described[int(row["id"])]
        row["epic"] = epic_of(row.get("package_id"), packages)
        row["waiting_for"] = info["waiting"]
        row["last_activity_ts"] = info["last_activity_ts"]
        row["stale"] = info["stale"]
        # L40 (0030) : délégation à échéance (« délégué par X, échéance dans … »)
        row["delegation"] = work_mod.delegation_view(row, now)
    return rows


def describe_age(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        return "%d h" % (seconds // 3600)
    return "%d j %d h" % (seconds // 86400, (seconds % 86400) // 3600)
