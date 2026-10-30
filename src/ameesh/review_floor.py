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

#3557 : poser ou relever le plancher sur un lot gelé **complète la désignation
de ce gel** jusqu'à `max(barème, plancher)` familles distinctes (les sièges
déjà désignés sont gardés, les manquants choisis par la même sélection
déterministe). La complétion est journalisée et sans courrier double ;
`work reviewers <lot> --apply` fait de même si le compte enregistré est
inférieur au plancher.

Autorité : celle de `--actor` des corrections (humain, orchestrateur ou agent
de conception déclaré, `history=False`). Un plancher posé par un humain ne se
baisse que par un humain. Seule cette commande écrit une ligne qui compte :
elle porte une preuve structurée (`evidence`) qu'aucune écriture à texte libre
du journal ne pose ; `work note` refuse en outre le préfixe.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import json
import re
from typing import Optional

from . import storage, work

#: le préfixe réservé de la ligne de journal
PREFIX = "reviews-floor:"
#: la preuve structurée d'une ligne écrite par `set_floor` (colonne
#: `work_item_events.evidence`, 0452) ; sans elle, la ligne ne compte pas
EVIDENCE_SCHEMA = "ameesh-review-floor/1"
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
    """Le plancher courant du lot : `{floor, actor, at_ts, note, human_floor}`.

    Seule compte une ligne **authentifiée** : écrite par `set_floor`, elle
    porte la preuve structurée `evidence` (`EVIDENCE_SCHEMA`, même plancher,
    même acteur) dans la colonne que les écritures à texte libre du journal
    (`move --note`, `add`, `assign`…) ne remplissent jamais ; une copie du
    texte, quel que soit l'acteur revendiqué, est ignorée (B1 cx2).
    Les lignes se lisent dans l'ordre du journal : une même valeur ne change
    pas de détenteur (B2 cx2), et un agent ne descend jamais sous le dernier
    plancher posé par un humain (`human_floor`). `cfg` est gardé pour les
    appelants. Lève si le journal est illisible : l'appelant décide (la
    classe échoue fermée)."""
    rows = storage.of(db).work.events_noted(PREFIX, 0.0, ids=[int(lot)]) or []
    current = {"floor": 0, "actor": "", "at_ts": None, "note": "", "human_floor": 0}
    for row in sorted(rows, key=lambda r: int(r.get("id") or 0)):
        value = parse(row.get("note"))
        actor = str(row.get("actor") or "").strip()
        if value is None or not _authentic(db, row.get("evidence"), value, actor):
            continue
        human = actor.startswith(work.HUMAN_PREFIX)
        if not human and value < current["human_floor"]:
            continue
        if human:
            current["human_floor"] = value
        if value == current["floor"] and not human:
            continue
        current.update(floor=value, actor=actor, at_ts=row.get("created_ts"),
                       note=str(row.get("note") or ""))
    return current


def _authentic(db, evidence, value: int, actor: str) -> bool:
    """Vrai si `evidence` est la preuve écrite par `set_floor` pour cette ligne.

    #4299 : un renommage réécrit l'acteur de la ligne, jamais la preuve ; les
    deux graphies d'une même fiche du registre sont le même acteur."""
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            return False
    return (isinstance(evidence, dict)
            and evidence.get("schema") == EVIDENCE_SCHEMA
            and type(evidence.get("floor")) is int and evidence["floor"] == value
            and bool(actor)
            and (str(evidence.get("actor") or "") == actor
                 or storage.of(db).agents.same_identity(evidence.get("actor"), actor)))


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
        # #3554 : le plancher automatique d'une baisse du canon (posé par la
        # classe, `reviews_floor`) ne se perd pas sous un plancher de pilote
        # plus bas que lui.
        floor = int(found["floor"])
        if floor >= int(klass.get("reviews_floor") or 0):
            klass["reviews_floor"] = floor
            klass["reviews_floor_by"] = found["actor"]
    return klass


def _complete_designation(cfg, db, lot: int, floor: int):
    """#3557 : complète la désignation du dernier gel après le plancher.

    Rien quand le plancher est retiré, quand l'hôte ne lit pas de canon (la
    désignation automatique n'y existe pas) ou quand le lot n'a pas de gel :
    `designate` rend alors `unfrozen` sans écrire. Le travail de désignation
    reste hors de la transaction du plancher : la ligne du plancher est un
    fait acquis, même si la complétion échoue ou attend une famille."""
    if int(floor) <= 0:
        return None
    from . import review_designation_activation as activation

    if not activation.enabled(cfg):
        return None
    return activation.designate(cfg, db, int(lot))


def set_floor(cfg, db, lot: int, floor: int, *, reason: str = "",
              actor: Optional[str] = None, complete: bool = True) -> dict:
    """Pose (ou retire, `floor=0`) le plancher d'avis du lot, journalisé.

    Rend `{lot, floor, previous, actor, changed}` ; `changed` est vrai dès
    qu'une ligne est écrite, y compris une recharge humaine à valeur égale.
    WorkError si l'acteur n'est pas pilote, si le lot est fermé, ou si un
    plancher posé par un humain serait baissé par un agent.

    #3557 : sur un lot gelé, un plancher qui dépasse le compte enregistré
    **complète la désignation de ce gel** (sièges conservés, familles
    manquantes choisies par la sélection déterministe et notifiées) ; le
    résultat de cette complétion est rendu sous `designation`. `complete=False`
    écrit le plancher seul (rejeu d'une version antérieure, `--apply` fera le
    reste)."""
    if type(floor) is not int or not 0 <= floor <= MAX_FLOOR:
        raise work.WorkError("review floor must be 0, 1 or 2, not %r" % (floor,))
    who = work.corrector(cfg, db, actor, what="review floor", history=False)
    item = work.get(db, lot)
    if item is None:
        raise work.WorkError("lot %s not found" % lot)
    if item["state"] in lifecycle.COMPLETED:
        raise work.WorkError("lot %s is %s: no review floor to set" % (lot, item["state"]))
    if storage.is_remote(db):
        raise work.WorkError("review floor refused: a mediated session cannot write "
                             "it; the pilot sets it from its own host")
    previous = of_lot(db, lot, cfg=cfg)
    human = who.startswith(work.HUMAN_PREFIX)
    if floor < previous["human_floor"] and not human:
        raise work.WorkError(
            "review floor refused: a human set the floor %d; only a human lowers it"
            % previous["human_floor"])
    # #3196 : une recharge humaine de la même valeur doit écrire sa ligne.
    # Sans elle, `human_floor` restait à 0 et un pilote pouvait ensuite poser
    # 0 sur un plancher que l'humain venait de confirmer.
    out = {"lot": int(lot), "floor": floor, "previous": previous["floor"],
           "actor": who,
           "changed": floor != previous["floor"]
                      or (human and floor > previous["human_floor"])}
    if out["changed"]:
        text = "%s %d" % (PREFIX, floor)
        why = " ".join((reason or "").split())
        if why:
            text += " — " + why
        # #3183 (B1 cx2) : la preuve structurée authentifie la ligne ; aucune
        # écriture à texte libre du journal ne remplit `evidence`.
        evidence = {"schema": EVIDENCE_SCHEMA, "floor": floor, "actor": who}
        with db.transaction() as tx:
            # #3557 : le verrou de la ligne du lot sérialise avec
            # `add_milestone` (qui le prend avant d'insérer un gel et de lire
            # le plancher). Un gel concurrent lit donc le plancher écrit ici
            # ou la complétion qui suit voit son jalon achevé — jamais un gel
            # à l'ancien compte sans complétion.
            tx.execute("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE",
                       (int(lot),))
            tx.execute("INSERT INTO work_item_events (work_item_id, state, note, actor, evidence)"
                       " VALUES (%s, %s, %s, %s, %s::jsonb)",
                       (int(lot), lifecycle.phase(item), text, who, json.dumps(evidence)))
            tx.execute("UPDATE work_items SET updated_at = now() WHERE id = %s", (int(lot),))
    if complete:
        # #3557 : une recharge de la même valeur retente aussi la complétion
        # (le plancher a pu être écrit par une version sans complétion).
        out["designation"] = _complete_designation(cfg, db, int(lot), floor)
    return out
