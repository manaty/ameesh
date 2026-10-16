# SPDX-License-Identifier: AGPL-3.0-only
"""Workflows Process (#1915, tranche C) : snapshot reel lu pour les verificateurs.

B appelle `read_snapshot(db, item_id)` **dans sa transaction**, apres avoir
verrouille le lot : C lit les tables natives (lot, jalons, journal, demandes de
decision) et rend le `Snapshot` que `process_verifiers.verify` consomme. Aucune
ecriture, aucun SQL propre : les lectures passent par `storage.of(db)` et
restent donc dans la transaction de l'appelant.

Collections declarees ici : `milestones`, `events`, `decisions`, `deliveries`
(les jalons `delivered` de #988 sont les livraisons natives). `deployments` et
`authority` ne sont pas des sources natives lisibles seules : ils ne sont pas
declares, donc les gardes qui les exigent refusent `source-unavailable` au lieu
d'accepter — c'est le fail-closed du contrat.

`revision` est la tete de travail reelle du lot : gel, puis fusion, puis
livraison. Un lot sans workflow (colonnes absentes ou vides, avant #1922) rend
un snapshot dont `workflow_ref` est vide : `verify` refuse `workflow-absent`,
donc un projet sans workflow reste inchange.
"""
from __future__ import annotations

from . import storage
from .process_verifiers import Snapshot

#: Bornes de lecture, larges : une preuve utile ne doit pas etre tronquee.
EVENT_LIMIT = 500
MILESTONE_LIMIT = 200

#: Collections reellement collectees par ce lecteur.
READ_SOURCES = ("milestones", "events", "decisions", "deliveries")


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _version(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return max(int(value), 0)
    return 0


def _revision(milestones) -> str:
    """Gel, puis fusion, puis livraison : la tete de travail native du lot."""
    for kind in ("frozen", "merged", "delivered"):
        for row in milestones:
            if _text(row.get("kind")) != kind:
                continue
            sha = _text(row.get("sha"))
            if sha:
                return sha
    return ""


def _deliveries(milestones) -> tuple:
    """Les jalons `delivered` (#988) sont les livraisons natives acceptees."""
    rows = []
    for row in milestones:
        if _text(row.get("kind")) != "delivered":
            continue
        sha = _text(row.get("sha"))
        rows.append({"id": "delivered:%s" % (sha or row.get("id")),
                     "sha": sha, "accepted": True,
                     "actor": _text(row.get("actor"))})
    return tuple(rows)


def read_snapshot(db, item_id: int) -> Snapshot | None:
    """Le snapshot reel du lot, lu dans la transaction de l'appelant.

    Rend `None` si le lot n'existe pas. `sources` ne declare que les
    collections reellement collectees ; une garde qui en exige une autre
    refuse (`source-unavailable`) au lieu d'accepter.
    """
    store = storage.of(db)
    item = store.work.get(int(item_id))
    if item is None:
        return None
    milestones = tuple(store.work.milestones(int(item_id), MILESTONE_LIMIT))
    events = tuple(store.work.events(int(item_id), EVENT_LIMIT))
    pending = store.decisions.pending_for_lot(str(int(item_id)))
    return Snapshot(
        item=item,
        workflow_ref=_text(item.get("workflow_ref")),
        workflow_stage=_text(item.get("workflow_stage")),
        workflow_version=_version(item.get("workflow_version")),
        revision=_revision(milestones),
        milestones=milestones,
        events=events,
        decisions_pending=len(pending),
        deliveries=_deliveries(milestones),
        sources=READ_SOURCES,
    )
