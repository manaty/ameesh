# SPDX-License-Identifier: AGPL-3.0-only
"""Déplacement d'un agent entre deux hôtes ADMIS (lot L31, décision 0028).

Entre deux tours seulement : quand l'hôte courant est **sous pression** et
qu'un autre hôte admis pour l'agent est **disponible** (dernier relevé sans
franchissement), l'agent passe là-bas. Le bail est rendu par l'exécuteur
courant (`relocate` change l'hôte d'exécution, ce qui le rend non réclamable
sur l'ancien hôte) puis repris par l'exécuteur de l'hôte d'arrivée.

La session native n'est conservée que si le stockage des sessions est partagé
entre les hôtes ; sinon l'agent est déplacé avec un résumé de reprise en tête
de sa consigne (rotation, 0025). Aucun déplacement au milieu d'un tour, et les
comptes/identifiants (L30) sont revérifiés sur l'hôte d'arrivée par son propre
`canon sync`.
"""
from __future__ import annotations

from . import resources, storage

#: message de reprise quand la session native n'est pas portable entre hôtes
FALLBACK_SUMMARY = (
    "Reprise après déplacement de l'hôte %s vers %s : la session précédente "
    "n'est pas reprise (stockage des sessions local à chaque hôte). Relis le "
    "fil de ton projet et l'état de ton lot avant de continuer."
)


def _score(reading: dict) -> tuple:
    """Classement d'un hôte disponible : le plus de mémoire, puis le moins de
    charge, puis le nom (stable)."""
    mem = reading.get("mem_available_bytes")
    load = reading.get("load1")
    return (-(mem if mem is not None else -1),
            load if load is not None else float("inf"))


def candidates(admitted_hosts, current_host: str, *, db,
               limits_for=None) -> list[dict]:
    """Hôtes admis autres que l'actuel, avec leur dernier relevé et pression.

    `limits_for(host)` rend les seuils de l'hôte (à défaut, les valeurs par
    défaut prudentes)."""
    out: list[dict] = []
    for host in admitted_hosts or []:
        if not host or host == current_host:
            continue
        reading = storage.of(db).hosts.latest(host)
        if reading is None:
            continue
        limits = limits_for(host) if limits_for is not None else None
        verdict = resources.pressure(reading, limits=limits)
        out.append({"host": host, "reading": reading, "pressure": verdict})
    return out


def plan(admitted_hosts, current_host: str, *, db, current_limits=None,
         limits_for=None, shared_sessions: bool = False) -> dict | None:
    """Plan de déplacement, ou None : l'hôte courant doit être sous pression et
    un hôte admis disponible doit exister (et être moins chargé)."""
    current_reading = storage.of(db).hosts.latest(current_host)
    if current_reading is None:
        return None
    current = resources.pressure(current_reading, limits=current_limits)
    if not current["blocked"]:
        return None
    dispo = [c for c in candidates(admitted_hosts, current_host, db=db, limits_for=limits_for)
             if not c["pressure"]["blocked"]]
    if not dispo:
        return None
    meilleur = min(dispo, key=lambda c: _score(c["reading"]) + (c["host"],))
    return {
        "target": meilleur["host"],
        "keep_session": bool(shared_sessions),
        "reason": "hôte %s sous pression (%s) ; %s disponible" % (
            current_host, ", ".join(b["key"] for b in current["breaches"]),
            meilleur["host"]),
        "current": current,
        "target_pressure": meilleur["pressure"],
    }


def move(db, row: dict, plan_dict: dict, *, current_host: str,
         owner: str, epoch: int) -> dict | None:
    """Applique le plan : change l'hôte d'exécution et la session de l'agent.

    `row` est la ligne du registre (nom, `canon_ref`, `placement_ref`),
    `admitted_hosts` déjà recopié par `canon sync`. `owner`/`epoch` sont le
    bail du worker : l'opération de stockage refuse une écriture d'un worker
    périmé, un tour en cours, ou une destination qui n'est plus admise."""
    name = row.get("name")
    if not name:
        return None
    keep = bool(plan_dict.get("keep_session"))
    summary = ""
    if not keep:
        summary = FALLBACK_SUMMARY % (current_host, plan_dict["target"])
    moved = storage.of(db).canon.relocate(
        name, current_host, target=plan_dict["target"],
        canon_ref=row.get("canon_ref") or "",
        diagnostic="déplacé de %s : placement à réévaluer sur %s (pression de l'hôte)"
                   % (current_host, plan_dict["target"]),
        ref=row.get("placement_ref"),
        keep_session=keep, owner=owner, epoch=epoch, summary=summary)
    if moved is None:
        return None
    return {"target": moved["host"], "keep_session": keep, "summary": summary,
            "reason": plan_dict["reason"]}
