# SPDX-License-Identifier: AGPL-3.0-only
"""#3256 C : mesure de l'attente d'un lot relu et partage en paquets disjoints.

Le canon §11 bis (décision owner du 28/10/2026, spec
`docs/design/3256-debit-file.md` §4) ouvre un **second paquet en parallèle**
quand la médiane d'attente `qa_merge` de la veille dépasse
`paquet.attente_mediane_s`. Ce module porte les deux calculs déterministes que
le composeur (`scripts/paquet-horaire.py`) et `ameesh debit` partagent :

* [`daily_waits`] : pour chaque lot composé dans un paquet, le temps écoulé
  entre son dernier verdict `ok` sur la tête gelée et l'ouverture de son paquet
  (la création du porteur), agrégé par jour local du canon (Europe/Paris) ;
* [`partition`] : les lots relus sont répartis en deux groupes **indépendants**
  — un lot ne figure que dans un groupe, et deux lots qui touchent un même
  fichier modifié, ou dont l'un dépend de l'autre, restent dans le même
  groupe. Le graphe est partitionné par composantes connexes ; une seule
  composante (ou aucune) ne donne qu'un paquet.

Aucune écriture, aucun réseau : les entrées viennent de la base (membres de
paquet, jalons) et de l'inventaire calculé (`packet_candidates`, qui fournit
les fichiers modifiés de chaque lot). Le module ne compose ni ne fusionne.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SCHEMA = "ameesh-packet-parallel/1"
#: fenêtre des jours rendus par `daily_waits` (le seuil lit la veille)
DAYS = 7


def _zone(zone) -> ZoneInfo:
    if isinstance(zone, ZoneInfo):
        return zone
    return ZoneInfo(str(zone or "UTC"))


def _local_day(ts: float, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(float(ts), zone).date().isoformat()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1,
                max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _same_sha(left, right) -> bool:
    a = str(left or "").strip().lower()
    b = str(right or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 7 and len(b) >= 7 and (a.startswith(b) or b.startswith(a))


def _verdict_ok(marks, frozen_sha: str, composed_ts: float) -> float | None:
    """Le dernier verdict `ok` de la tête gelée, au plus tard à la composition.

    Un lot dont la tête gelée ne porte pas de verdict `ok` (dispense
    `verdict not required`, avis bloquant, gel d'une autre tête) ne compte pas
    dans la mesure : il n'a pas été « relu » au sens du §4.
    """
    latest = None
    for mark in marks or ():
        if mark.get("kind") != "verdict" or (mark.get("verdict") or "") != "ok":
            continue
        at = mark.get("at_ts")
        if at is None or float(at) > float(composed_ts):
            continue
        if not _same_sha(mark.get("sha"), frozen_sha):
            continue
        if latest is None or float(at) > latest:
            latest = float(at)
    return latest


def daily_waits(packets, milestones, *, zone, now: float,
                days: int = DAYS) -> dict:
    """L'attente `qa_merge` par jour local, du verdict `ok` à la composition.

    `packets` : les lignes de `work_packet_members` utiles, avec au moins
    `work_item_id`, `frozen_sha` et `composed_ts` (l'ouverture du porteur,
    `work_items.created_at`) ; `milestones` : les jalons de ces lots. Pour un
    lot recomposé, la **première** ouverture de paquet fait foi (le temps
    d'attente ne se rallonge pas d'une recomposition).

    Rend `{days: [{day, count, median_s, p90_s, max_s}], yesterday, count,
    unmatched}` ; `median_s` vaut `None` sans lot mesuré. Les jours vont du
    plus ancien au plus récent ; `yesterday` est le jour local précédant
    `now`, même sans mesure (`count: 0`).
    """
    zone = _zone(zone)
    now = float(now)
    window_days = max(1, int(days))
    today = datetime.fromtimestamp(now, zone).date()
    start_day = today - timedelta(days=window_days - 1)
    window_start = datetime.combine(start_day, datetime.min.time(),
                                    tzinfo=zone).timestamp()
    by_lot: dict[int, dict] = {}
    for row in packets or ():
        lot = int(row.get("work_item_id") or 0)
        if lot <= 0:
            continue
        composed = row.get("composed_ts")
        if composed is None:
            continue
        composed = float(composed)
        if composed > now or composed < window_start:
            continue
        found = {"frozen_sha": str(row.get("frozen_sha") or "").strip(),
                 "composed_ts": composed}
        current = by_lot.get(lot)
        if current is None or composed < current["composed_ts"]:
            by_lot[lot] = found
    marks_by_lot: dict[int, list[dict]] = {}
    for mark in milestones or ():
        marks_by_lot.setdefault(int(mark.get("work_item_id") or 0), []).append(mark)
    waits: list[tuple[float, float]] = []
    unmatched = 0
    for lot, row in by_lot.items():
        verdict = _verdict_ok(marks_by_lot.get(lot, ()), row["frozen_sha"],
                              row["composed_ts"])
        if verdict is None:
            unmatched += 1
            continue
        wait = row["composed_ts"] - verdict
        if wait < 0:
            unmatched += 1
            continue
        waits.append((row["composed_ts"], wait))
    grouped: dict[str, list[float]] = {}
    for composed, wait in waits:
        day = _local_day(composed, zone)
        grouped.setdefault(day, []).append(wait)
    out_days = []
    for offset in range(window_days):
        day = (start_day + timedelta(days=offset)).isoformat()
        values = grouped.get(day, [])
        out_days.append({
            "day": day,
            "count": len(values),
            "median_s": (statistics.median(values) if values else None),
            "p90_s": _percentile(values, 0.9),
            "max_s": (max(values) if values else None),
        })
    yesterday = (today - timedelta(days=1)).isoformat()
    yesterday_row = next((row for row in out_days if row["day"] == yesterday),
                         {"day": yesterday, "count": 0, "median_s": None,
                          "p90_s": None, "max_s": None})
    return {"schema": SCHEMA, "zone": str(zone), "days": out_days,
            "yesterday": yesterday_row, "count": len(waits),
            "window_days": window_days, "unmatched": unmatched}


def trigger(measure, threshold_s, *, now: float, zone) -> dict:
    """Le second paquet est-il ouvert ? La veille doit dépasser le seuil.

    Rend `{enabled, median_s, count, day, threshold_s, why}`. Aucun repli :
    une mesure absente, une veille sans lot mesuré ou un seuil illisible
    n'ouvrent rien (`enabled: false`), et `why` le dit.
    """
    zone = _zone(zone)
    yesterday = datetime.fromtimestamp(float(now), zone).date() - timedelta(days=1)
    day = yesterday.isoformat()
    try:
        threshold = float(threshold_s)
    except (TypeError, ValueError):
        return {"enabled": False, "median_s": None, "count": 0, "day": day,
                "threshold_s": None, "why": "seuil `paquet.attente_mediane_s` absent"}
    if not math.isfinite(threshold) or threshold <= 0:
        return {"enabled": False, "median_s": None, "count": 0, "day": day,
                "threshold_s": None, "why": "seuil `paquet.attente_mediane_s` invalide"}
    row = (measure or {}).get("yesterday") or {}
    if str(row.get("day") or "") != day:
        # une mesure d'un autre jour n'est pas la veille : rien n'est supposé
        return {"enabled": False, "median_s": None, "count": int(row.get("count") or 0),
                "day": day, "threshold_s": threshold,
                "why": "mesure d'un autre jour (%s)" % (row.get("day") or "aucune")}
    count = int(row.get("count") or 0)
    median = row.get("median_s")
    if count <= 0 or median is None:
        return {"enabled": False, "median_s": None, "count": count, "day": day,
                "threshold_s": threshold, "why": "aucun lot relu mesuré la veille"}
    median = float(median)
    return {"enabled": median > threshold, "median_s": median, "count": count,
            "day": day, "threshold_s": threshold,
            "why": ("médiane de la veille %s > seuil %s" % (median, threshold)
                    if median > threshold else
                    "médiane de la veille %s ≤ seuil %s" % (median, threshold))}


class _UnionFind:
    def __init__(self, values):
        self.parent = {value: value for value in values}

    def find(self, value):
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != root:
            self.parent[value], value = root, self.parent[value]
        return root

    def union(self, left, right):
        a, b = self.find(left), self.find(right)
        if a != b:
            # ordre stable : le plus petit identifiant reste racine
            if b < a:
                a, b = b, a
            self.parent[b] = a


def _files(entry) -> set[str]:
    return {str(name) for name in entry.get("files") or () if str(name)}


def _dependencies(entry) -> set[int]:
    out = set()
    for value in entry.get("dependencies") or ():
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return out


def partition(entries, order=None) -> dict:
    """Partage les lots relus en deux paquets disjoints, ou un seul.

    `entries` : les lots retenus, avec `work_item`, `files` (chemins modifiés,
    lus par `packet_candidates`) et `dependencies` (identifiants de
    prérequis). Deux lots qui partagent un fichier, ou dont l'un dépend de
    l'autre, sont unis. Les composantes connexes sont ensuite réparties en
    deux groupes équilibrés, dans l'ordre de `order` (la liste prête : P1
    d'abord), pour que la priorité reste lisible.

    Rend `{schema, first, second, components, shared_files}`. `second` est
    vide quand le graphe n'a qu'une composante (les lots ne sont pas
    disjoints) : il n'y a alors qu'un paquet. Aucun lot ne figure dans les
    deux groupes.
    """
    by_lot: dict[int, dict] = {}
    for entry in entries or ():
        lot = int(entry.get("work_item") or 0)
        if lot > 0:
            by_lot[lot] = entry
    ranked = [int(lot) for lot in (order or ()) if int(lot) in by_lot]
    ranked += [lot for lot in sorted(by_lot) if lot not in set(ranked)]
    if not ranked:
        return {"schema": SCHEMA, "first": [], "second": [], "components": [],
                "shared_files": []}
    missing = [lot for lot in ranked if "files" not in by_lot[lot]]
    if missing:
        # Un inventaire sans la liste des fichiers ne prouve pas la
        # disjonction : un seul paquet, jamais un partage supposé.
        return {"schema": SCHEMA, "first": list(ranked), "second": [],
                "components": [list(ranked)], "shared_files": [],
                "why": "fichiers modifiés illisibles pour %s"
                       % ", ".join("#%d" % lot for lot in missing)}
    union = _UnionFind(ranked)
    owners: dict[str, int] = {}
    for lot in ranked:
        for path in _files(by_lot[lot]):
            other = owners.get(path)
            if other is None:
                owners[path] = lot
            else:
                union.union(other, lot)
    chosen = set(ranked)
    for lot in ranked:
        for blocker in _dependencies(by_lot[lot]):
            if blocker in chosen:
                union.union(blocker, lot)
    components: dict[int, list[int]] = {}
    for lot in ranked:
        components.setdefault(union.find(lot), []).append(lot)
    ordered = sorted(components.values(),
                     key=lambda lots: (ranked.index(min(lots, key=ranked.index)),
                                       min(lots)))
    if len(ordered) < 2:
        return {"schema": SCHEMA, "first": list(ranked), "second": [],
                "components": ordered, "shared_files": []}
    first: list[int] = []
    second: list[int] = []
    for component in ordered:
        # équilibre le nombre de lots ; à égalité, le premier groupe
        if len(first) <= len(second):
            first.extend(component)
        else:
            second.extend(component)
    rank = {lot: index for index, lot in enumerate(ranked)}
    first.sort(key=lambda lot: rank[lot])
    second.sort(key=lambda lot: rank[lot])
    return {"schema": SCHEMA, "first": first, "second": second,
            "components": ordered, "shared_files": []}
