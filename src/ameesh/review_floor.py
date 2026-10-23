# SPDX-License-Identifier: AGPL-3.0-only
"""#3183 — le plancher d'avis d'un lot, posé par un pilote.

Le barème par chemin (`train_reviews`, `CRITICAL_SCOPE`) ne voit pas un outil
de clés, de secrets ou de prod ajouté hors des dossiers déclarés (#3169
nexlink : `scripts/actor-root.mjs` sous `**` = 1 avis). Le pilote juge alors
qu'il faut deux familles : `ameesh work reviewer <lot> <agent> --floor 2`.

Le plancher est une ligne réservée du journal du lot,
`reviews-floor: N — <motif>` ; il ne change pas la classe. Tous les lecteurs du
compte requis le lisent par la classe (`lot_class.of_lot`, clé
`reviews_floor`) : la porte (`merge_queue.required_verdicts`), la désignation
au gel et `paquet-candidats` exigent `max(barème, plancher)`. Le journal ne
dépend pas d'un gel : le plancher survit aux regels et suit le lot dans un
véhicule ; sur un véhicule, il s'applique à son delta propre
(`packet_reviews.need`).

Autorité : celle de `--actor` des corrections (humain, orchestrateur ou agent
de conception déclaré, `history=False`). Un plancher posé par un humain ne se
baisse que par un humain. Le préfixe est refusé à `work note` : seule cette
commande l'écrit.
"""
from __future__ import annotations

import re
from typing import Optional

from . import storage, work

#: le préfixe réservé de la ligne de journal
PREFIX = "reviews-floor:"
#: le plus haut plancher : deux familles distinctes (canon §2)
MAX_FLOOR = 2
_LINE = re.compile(r"^reviews-floor:\s*(\d+)\b")


def is_floor_note(text) -> bool:
    """Vrai pour une ligne qui se présente comme un plancher d'avis."""
    return str(text or "").lstrip().lower().startswith(PREFIX)


def parse(text) -> Optional[int]:
    """Le plancher d'une ligne réservée, ou None si elle est illisible."""
    match = _LINE.match(str(text or "").strip())
    if not match:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= MAX_FLOOR else None


def of_lot(db, lot: int, *, cfg=None) -> dict:
    """Le plancher courant du lot : `{floor, actor, at_ts, note}`.

    Les lignes se lisent dans l'ordre du journal ; une ligne illisible est
    ignorée. Un relèvement compte toujours (il n'ôte aucun avis). Une baisse
    ne compte que si son acteur est pilote (`work.claims_pilot`), et humain
    quand le plancher courant a été posé par un humain : d'autres écritures
    du journal (`move --note`, `assign`…) portent un texte libre, et une
    ligne copiée par l'assigné ne lève pas la porte. Lève si le journal est
    illisible : l'appelant décide (la classe échoue fermée)."""
    rows = storage.of(db).work.events_noted(PREFIX, 0.0, ids=[int(lot)]) or []
    current = {"floor": 0, "actor": "", "at_ts": None, "note": ""}
    for row in sorted(rows, key=lambda r: int(r.get("id") or 0)):
        value = parse(row.get("note"))
        if value is None:
            continue
        actor = str(row.get("actor") or "").strip()
        if value < current["floor"] and not _may_lower(cfg, db, actor, current["actor"]):
            continue
        current = {"floor": value, "actor": actor, "at_ts": row.get("created_ts"),
                   "note": str(row.get("note") or "")}
    return current


def _may_lower(cfg, db, actor: str, holder: str) -> bool:
    """Vrai si `actor` peut baisser un plancher posé par `holder`."""
    if holder.startswith(work.HUMAN_PREFIX):
        return actor.startswith(work.HUMAN_PREFIX)
    try:
        return work.claims_pilot(cfg if cfg is not None else getattr(db, "cfg", None),
                                 db, actor)
    except Exception:  # noqa: BLE001 - autorité illisible : la baisse ne compte pas
        return False


def apply(klass: dict, db, lot: int, *, cfg=None) -> dict:
    """Pose le plancher du lot sur une classe lue (`reviews_floor`, et sa
    source `reviews_floor_by`). Sans base (lecture hors registre), rien.
    Un journal illisible marque `reviews_floor_unreadable` : la porte exige
    alors deux avis plutôt que de supposer qu'aucun plancher n'est posé."""
    if db is None:
        return klass
    try:
        found = of_lot(db, lot, cfg=cfg)
    except Exception as exc:  # noqa: BLE001 - journal illisible : échec fermé
        klass["reviews_floor_unreadable"] = "%s: %s" % (type(exc).__name__,
                                                       str(exc)[:200])
        return klass
    if found["floor"]:
        klass["reviews_floor"] = found["floor"]
        klass["reviews_floor_by"] = found["actor"]
    return klass


def set_floor(cfg, db, lot: int, floor: int, *, reason: str = "",
              actor: Optional[str] = None) -> dict:
    """Pose (ou retire, `floor=0`) le plancher d'avis du lot, journalisé.

    Rend `{lot, floor, previous, actor, changed}`. WorkError si l'acteur n'est
    pas pilote, si le lot est fermé, ou si un plancher posé par un humain
    serait baissé par un agent."""
    if type(floor) is not int or not 0 <= floor <= MAX_FLOOR:
        raise work.WorkError("review floor must be 0, 1 or 2, not %r" % (floor,))
    who = work.corrector(cfg, db, actor, what="review floor", history=False)
    item = work.get(db, lot)
    if item is None:
        raise work.WorkError("lot %s not found" % lot)
    if item["state"] in ("merged", "promoted", "closed"):
        raise work.WorkError("lot %s is %s: no review floor to set" % (lot, item["state"]))
    if storage.is_remote(db):
        raise work.WorkError("review floor refused: a mediated session cannot write "
                             "it; the pilot sets it from its own host")
    previous = of_lot(db, lot, cfg=cfg)
    if floor < previous["floor"] and previous["actor"].startswith(work.HUMAN_PREFIX) \
            and not who.startswith(work.HUMAN_PREFIX):
        raise work.WorkError(
            "review floor refused: the floor %d was set by %s; only a human lowers it"
            % (previous["floor"], previous["actor"]))
    out = {"lot": int(lot), "floor": floor, "previous": previous["floor"],
           "actor": who, "changed": floor != previous["floor"]}
    if not out["changed"]:
        return out
    text = "%s %d" % (PREFIX, floor)
    why = " ".join((reason or "").split())
    if why:
        text += " — " + why
    storage.of(db).work.note(int(lot), item["state"], text, who)
    return out
