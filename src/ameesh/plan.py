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

from . import work_lifecycle as lifecycle
from .modules import registry
from . import stagnation, storage
from . import work as work_mod

MERGE_CONNECTOR = registry.merge_connector()


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


def item_unit_status(state: str, close_reason: str | None = None) -> str:
    """merged | abandoned | open, pour un lot (`work_items`). Un lot livré hors
    cible (`closed` + `delivered`, #988) compte comme livré, pas comme
    abandonné : sa livraison a bien eu lieu."""
    if state in work_mod.MERGED_STATES:
        return "merged"
    if state == "closed":
        return "merged" if close_reason == "delivered" else "abandoned"
    return "open"


def package_status(items: list[dict]) -> str:
    """État d'une fiche `lot` d'après ses lots : pending (aucun lot créé),
    merged (tous ses lots vivants fusionnés ou livrés hors cible), abandoned
    (tous fermés), open."""
    if not items:
        return "pending"
    statuses = [item_unit_status(i.get("state") or "", i.get("close_reason"))
                for i in items]
    if all(status == "abandoned" for status in statuses):
        return "abandoned"
    if all(status == "merged" for status in statuses if status != "abandoned"):
        return "merged"
    return "open"


def _activated_unit(item: dict, activation: dict | None) -> str:
    """#5244 : l'unité d'un lot une fois l'étape d'activation connue : `active`
    (fait : preuve ou reçu), `waiting` (livré, pas encore activé), `unmeasured`
    (livré, aucune preuve lisible, donc pas fait) ; sinon comme avant."""
    entry = (activation or {}).get(int(item["id"]))
    if not entry or entry["step"] in ("open", "abandoned"):
        return item_unit_status(item.get("state") or "", item.get("close_reason"))
    if entry["step"] == "unmeasured":
        return "unmeasured"
    return "active" if entry["counted"] else "waiting"


def _activated_package(items: list[dict], activation: dict | None) -> str:
    """L'unité d'une fiche `lot` : son lot le moins avancé la retient."""
    if not items:
        return "pending"
    units = {_activated_unit(i, activation) for i in items} - {"abandoned"}
    for unit in ("open", "waiting", "merged", "unmeasured", "active"):
        if unit in units:
            return unit
    return "abandoned"


def _tally(units: list[str]) -> dict:
    waiting = units.count("waiting")
    active = units.count("active")
    unmeasured = units.count("unmeasured")
    merged = units.count("merged") + active + waiting + unmeasured
    abandoned = units.count("abandoned")
    counted = len(units) - abandoned
    return {"lots_total": len(units), "lots_merged": merged, "lots_abandoned": abandoned,
            "lots_open": units.count("open"), "lots_pending": units.count("pending"),
            "lots_active": active, "lots_waiting_activation": waiting,
            "lots_unmeasured": unmeasured,
            "progress": round((merged - waiting - unmeasured) / counted, 4) if counted else None}


def summarize(packages: Iterable[dict], items: Iterable[dict],
              activation: dict | None = None) -> dict:
    """Epics et jalons du plan, avec leur progression.

    `packages` : fiches (id, kind, title, parent, responsible, status…) ;
    `items` : lots rattachés (id, package_id, state). Rend
    `{"epics": [...], "milestones": [...], "lots": {id_fiche: statut}}`.
    #5244 : avec `activation` (`activation.compute`), `progress` ne compte
    que les lots activés ; `lots_merged` reste le décompte de la fusion.
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
        statuses = [_activated_package(by_package.get(p["id"], []), activation)
                    for p in lots] + [_activated_unit(i, activation) for i in direct]
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
                   "step": _activated_package(by_package.get(p["id"], []), activation),
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
             threshold: float = stagnation.DEFAULT_THRESHOLD_S,
             cfg=None, review_class: dict | None = None) -> list[dict]:
    """Ajoute à chaque lot : `epic`, `waiting_for` (`{what, who, label}`),
    `stale`, `last_activity_ts` — calculés par le module `stagnation`, comme
    l'alerte `stale_lot` et `ameesh progress` — et `delegation` (L40,
    `work.delegation_view`).

    `cfg` et `review_class` (#1822) : le contexte de classe de revue transmis à
    `stagnation.describe` (un lot léger attend la fusion, pas un verdict)."""
    packages = {p["id"]: p for p in storage.of(db).packages.all(include_absent=True)}
    described = stagnation.describe(db, rows, now=now, threshold=threshold,
                                    cfg=cfg, review_class=review_class)
    for row in rows:
        info = described[int(row["id"])]
        row["epic"] = epic_of(row.get("package_id"), packages)
        row["waiting_for"] = info["waiting"]
        row["last_activity_ts"] = info["last_activity_ts"]
        row["stale"] = info["stale"]
        row["planned_wait"] = info["planned_wait"]
        row["dependencies"] = info["dependencies"]
        # L40 (0030) : délégation à échéance (« délégué par X, échéance dans … »)
        row["delegation"] = work_mod.delegation_view(row, now)
    return rows


def describe_age(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        return "%d h" % (seconds // 3600)
    return "%d d %d h" % (seconds // 86400, (seconds % 86400) // 3600)
