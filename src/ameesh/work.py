# SPDX-License-Identifier: AGPL-3.0-only
"""work_items — la machine à états des lots, reprise d'un orchestrateur de
tickets existant (spec §5 point 4).

    intake → build → qa → merged → promoted

plus `blocked` et `waiting_human`, et l'état terminal `closed` (abandonné ou
remplacé par un autre lot : `close`, L29). La boucle `qa → build` est bornée à deux
allers-retours (comme dans le design d'origine) : au-delà, il faut bloquer le
lot ou attendre une décision humaine, sinon la boucle coûte des tokens sans fin.

Chaque transition laisse une ligne dans `work_item_events` (qui, quand,
pourquoi) : c'est le ledger de reprise après un redémarrage.

Plan de travail (L29) : un lot peut être rattaché à une fiche `WorkPackage`
du canon (`package_id`, et son parent `package_parent`) ; une fusion
constatée (porte `git-merge`, ou `ameesh work sync-github`) le ferme par
`close_merged`, idempotent et sans jamais rouvrir un lot fermé.

Le SQL est dans le stockage (`storage.of(db).work`, spec §10).
"""
from __future__ import annotations

from typing import Sequence

from . import storage
from .db import Db
from .storage.postgres import work as _pg

STATES = ("intake", "build", "qa", "merged", "promoted", "blocked", "waiting_human",
          "closed")
TERMINAL = ("promoted", "closed")
#: déjà fusionnés : une fusion constatée n'y change rien
MERGED_STATES = ("merged", "promoted")
#: raisons d'une fermeture explicite (`ameesh work close`)
CLOSE_REASONS = ("abandoned", "superseded")
MAX_QA_LOOPS = 2
FORWARD = {
    "intake": ("build",),
    "build": ("qa",),
    "qa": ("merged", "build"),
    "merged": ("promoted",),
    "promoted": (),
    "closed": (),
}

#: transitions autorisées, attentes comprises
TRANSITIONS = {
    "intake": {"build", "blocked", "waiting_human"},
    "build": {"qa", "blocked", "waiting_human"},
    "qa": {"merged", "build", "blocked", "waiting_human"},
    "merged": {"promoted", "blocked", "waiting_human"},
    "promoted": set(),
    "closed": set(),
    "blocked": {"intake", "build", "qa", "merged", "waiting_human"},
    "waiting_human": {"intake", "build", "qa", "merged", "blocked"},
}
WAITING = {"blocked", "waiting_human"}

#: colonnes rendues pour un lot (pilote Postgres ; alias de compatibilité)
ITEM_COLUMNS = _pg.ITEM_COLUMNS


class WorkError(RuntimeError):
    """Transition ou donnée refusée — message toujours actionnable."""


def add(
    db: Db,
    *,
    title: str,
    type: str = "evolution",  # noqa: A002 - nom de colonne de l'orchestrateur d'origine
    source: str = "",
    app: str = "",
    body: str = "",
    issue_ref: str | None = None,
    workstream: str | None = None,
    assignee: str | None = None,
    budget_usd: float | None = None,
    actor: str = "",
    package: str | None = None,
) -> dict:
    if type not in ("bug", "evolution"):
        raise WorkError("type inconnu : %r (bug ou evolution)" % type)
    if not (title or "").strip():
        raise WorkError("titre obligatoire")
    fiche = _package(db, package) if package else None
    return storage.of(db).work.add(
        type=type, source=source, app=app, title=title.strip(), body=body,
        issue_ref=issue_ref, workstream=workstream, assignee=assignee,
        budget_usd=budget_usd, note="création" + (" (plan : %s)" % package if package else ""),
        actor=actor, package_id=fiche["id"] if fiche else None,
        package_parent=fiche.get("parent") if fiche else None)


#: sortes de fiche auxquelles un lot se rattache (un jalon regroupe des epics)
LINKABLE_KINDS = ("lot", "epic")


def _package(db: Db, ident: str) -> dict:
    """La fiche WorkPackage `ident`, présente au canon synchronisé, sinon WorkError."""
    fiche = storage.of(db).packages.get(ident)
    if fiche is None:
        raise WorkError("fiche WorkPackage inconnue : %r (déclarez-la au canon puis "
                        "« ameesh canon sync »)" % ident)
    if not fiche.get("present"):
        raise WorkError("fiche WorkPackage %s retirée du canon" % ident)
    if fiche.get("kind") not in LINKABLE_KINDS:
        raise WorkError("fiche %s : un lot se rattache à un %s, pas à un %s"
                        % (ident, " ou un ".join(LINKABLE_KINDS), fiche.get("kind")))
    return fiche


def link(db: Db, item_id: int, package: str | None, *, actor: str = "") -> dict:
    """Rattache un lot existant à une fiche WorkPackage (None : le détache)."""
    if get(db, item_id) is None:
        raise WorkError("lot %s introuvable" % item_id)
    fiche = _package(db, package) if package else None
    row = storage.of(db).work.link_package(
        item_id, fiche["id"] if fiche else None, fiche.get("parent") if fiche else None,
        note="plan : %s" % package if package else "plan : détaché", actor=actor)
    if row is None:
        raise WorkError("lot %s introuvable" % item_id)
    return row


def close_merged(db: Db, item_id: int, *, sha: str = "", actor: str = "", source: str = "",
                 pr_ref: str | None = None, frozen_id: int | None = None) -> dict:
    """Fusion constatée : le lot passe `merged` et son jalon `merged` est posé.

    Idempotent : un lot déjà fusionné reste tel quel (`already`). Jamais de
    réouverture silencieuse : un lot fermé (abandonné, remplacé) n'est pas
    touché, le constat est rendu (`refused`) pour être dit à l'appelant. Rend
    `{id, result: merged|already|refused|refrozen, detail, item}`.

    `frozen_id` (preuve par le contenu) : le jalon `frozen` examiné ; si ce
    n'est plus le dernier gel du lot au moment d'écrire, rien n'est fermé
    (`refrozen`) — le nouveau gel n'a pas été examiné.
    """
    for _attempt in range(3):
        item = get(db, item_id)
        if item is None:
            raise WorkError("lot %s introuvable" % item_id)
        state = item["state"]
        if state in MERGED_STATES:
            return {"id": int(item_id), "result": "already", "item": item,
                    "detail": "déjà %s" % state}
        if state == "closed":
            return {"id": int(item_id), "result": "refused", "item": item,
                    "detail": "lot fermé (%s) : fusion constatée%s, aucune réouverture"
                              % (item.get("close_reason"), " (%s)" % pr_ref if pr_ref else "")}
        note = "fusion constatée%s%s%s" % (
            " par %s" % source if source else "", " — %s" % pr_ref if pr_ref else "",
            " (commit %s)" % sha[:12] if sha else "")
        row = storage.of(db).work.close_merged(
            item_id, current=state, sha=sha, actor=actor, note=note, pr_ref=pr_ref,
            frozen_id=frozen_id)
        if row is not None:
            return {"id": int(item_id), "result": "merged", "item": row,
                    "detail": "%s → merged" % state}
        if frozen_id is not None:
            freezes = [m for m in milestones(db, item_id) if m["kind"] == "frozen"]
            if not freezes or int(freezes[0]["id"]) != int(frozen_id):
                return {"id": int(item_id), "result": "refrozen", "item": get(db, item_id),
                        "detail": "nouveau gel déclaré entre-temps : non fermé, à réexaminer"}
    raise WorkError("lot %s déplacé entre-temps : réessayez" % item_id)


def close(db: Db, item_id: int, *, abandoned: bool = False, superseded_by: int | None = None,
          note: str = "", actor: str = "") -> dict:
    """Ferme un lot qui ne sera pas fusionné : abandonné, ou remplacé par un
    autre lot. Jalon `closed` ; rien ne reste ouvert par oubli."""
    if bool(abandoned) == (superseded_by is not None):
        raise WorkError("préciser --abandoned OU --superseded-by <id>")
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s introuvable" % item_id)
    if superseded_by is not None:
        if int(superseded_by) == int(item_id):
            raise WorkError("un lot ne se remplace pas lui-même")
        if get(db, superseded_by) is None:
            raise WorkError("lot remplaçant %s introuvable" % superseded_by)
    state = item["state"]
    if state in MERGED_STATES or state == "closed":
        raise WorkError("lot %s déjà %s : rien à fermer" % (item_id, state))
    reason = "abandoned" if abandoned else "superseded"
    text = ("abandonné" if abandoned else "remplacé par #%s" % superseded_by) + (
        " — %s" % note.strip() if note and note.strip() else "")
    row = storage.of(db).work.close(
        item_id, current=state, reason=reason,
        superseded_by=int(superseded_by) if superseded_by is not None else None,
        actor=actor, note=text)
    if row is None:
        raise WorkError("lot %s déplacé entre-temps : réessayez" % item_id)
    return row


def get(db: Db, item_id: int) -> dict | None:
    return storage.of(db).work.get(item_id)


def list_items(db: Db, *, state: str | None = None, assignee: str | None = None,
               limit: int = 50) -> list[dict]:
    if state and state not in STATES:
        raise WorkError("état inconnu : %r (%s)" % (state, ", ".join(STATES)))
    return storage.of(db).work.items(state=state, assignee=assignee, limit=limit)


def move(db: Db, item_id: int, state: str, *, note: str = "", actor: str = "") -> dict:
    """Déplace un lot en vérifiant la transition, la boucle QA et le terminal."""
    if state not in STATES:
        raise WorkError("état inconnu : %r (%s)" % (state, ", ".join(STATES)))
    if state == "closed":
        raise WorkError("fermer un lot : ameesh work close <id> --abandoned | "
                        "--superseded-by <id>")
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s introuvable" % item_id)
    current = item["state"]
    if current == state:
        raise WorkError("lot %s déjà en %s" % (item_id, state))
    if state not in TRANSITIONS[current]:
        raise WorkError(
            "transition refusée : %s → %s (autorisées depuis %s : %s)"
            % (current, state, current, ", ".join(sorted(TRANSITIONS[current])) or "aucune"))
    returning_loop = current == "qa" and state == "build"
    if returning_loop and int(item["loops"]) >= MAX_QA_LOOPS:
        raise WorkError(
            "boucle qa → build épuisée (%d) : mettre le lot en blocked ou waiting_human"
            % MAX_QA_LOOPS)

    row = storage.of(db).work.move(
        item_id, state, current=current, loops=1 if returning_loop else 0,
        note=note or ("%s → %s" % (current, state)), actor=actor)
    if row is None:
        raise WorkError("lot %s déplacé entre-temps : réessayez" % item_id)
    return row


def note(db: Db, item_id: int, text: str, *, actor: str = "") -> bool:
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s introuvable" % item_id)
    if not (text or "").strip():
        raise WorkError("note vide")
    storage.of(db).work.note(item_id, item["state"], text.strip(), actor)
    return True


def events(db: Db, item_id: int, limit: int = 100) -> list[dict]:
    return storage.of(db).work.events(item_id, limit)


#: les jalons d'un lot : `requested` et `merged` sont automatiques (0013),
#: `frozen` et `verdict` se déclarent par la CLI.
MILESTONE_KINDS = ("requested", "frozen", "verdict", "merged", "closed")
MANUAL_KINDS = ("frozen", "verdict")
VERDICTS = ("ok", "blocked")

#: les jalons affichés, du plus récent au plus ancien : (nom, colonne d'instant,
#: colonne de durée) — l'ordre est celui de la frise lue à l'envers.
MILESTONES = (
    ("fusion", "merged_ts", "review_to_merge_s"),
    ("revue", "reviewed_ts", "freeze_to_review_s"),
    ("gel", "frozen_ts", "request_to_freeze_s"),
    ("demande", "requested_ts", None),
)

#: les durées affichées et leur colonne (la forme de `delays` est stable, L24)
MILESTONE_DURATIONS = (
    ("demande → gel", "request_to_freeze_s"),
    ("gel → revue", "freeze_to_review_s"),
    ("gel → fusion", "freeze_to_merge_s"),
    ("total", "total_s"),
)


def milestones(db: Db, item_id: int, limit: int = 100) -> list[dict]:
    """Les jalons d'un lot, du plus récent au plus ancien."""
    return storage.of(db).work.milestones(item_id, limit)


def milestone(
    db: Db,
    item_id: int,
    kind: str,
    *,
    sha: str = "",
    actor: str = "",
    verdict: str | None = None,
    note: str = "",
) -> dict:
    """Déclare un jalon que la machine à états ne peut pas déduire.

    `frozen` (la branche est gelée pour la revue) et `verdict` (`ok` ou
    `blocked`, avec le `sha` relu) sont les deux seuls : `requested` et `merged`
    sont écrits automatiquement depuis les transitions (0013), les déclarer à la
    main serait un mensonge sur la frise.
    """
    if kind not in MILESTONE_KINDS:
        raise WorkError("jalon inconnu : %r (%s)" % (kind, ", ".join(MILESTONE_KINDS)))
    if kind not in MANUAL_KINDS:
        raise WorkError(
            "le jalon %s est automatique (transition ou fermeture du lot) : %s se "
            "déclarent seuls" % (kind, ", ".join(MANUAL_KINDS)))
    if kind == "verdict":
        if verdict not in VERDICTS:
            raise WorkError("verdict manquant ou inconnu : %r (%s)"
                            % (verdict, ", ".join(VERDICTS)))
    elif verdict is not None:
        raise WorkError("un verdict n'a de sens que pour le jalon `verdict`")
    if get(db, item_id) is None:
        raise WorkError("lot %s introuvable" % item_id)
    return storage.of(db).work.add_milestone(
        item_id, kind, sha=sha.strip(), actor=actor, verdict=verdict, note=note.strip())


def delays(db: Db, limit: int = 200, *, ids: Sequence[int] | None = None) -> list[dict]:
    """Les délais par lot (R19), du plus récemment modifié au plus ancien.

    `ids` restreint le balayage à ces lots : `work list` le passe pour que les
    délais d'une ligne filtrée ne dépendent pas de la fenêtre globale des lots
    les plus récents (un lot ancien garde sa frise — revue codex2, B3).
    """
    return storage.of(db).work.delays(limit, ids=ids)


def timeline(db: Db, item_id: int) -> dict | None:
    """Les jalons et durées d'un lot, ou None."""
    return storage.of(db).work.timeline(item_id)


def last_milestone(row: dict) -> tuple[str, float | None]:
    """Le dernier jalon atteint par un lot, et son instant (epoch).

    « demande » quand le lot n'est pas encore gelé, puis « gel », « revue »
    (verdict rendu) et « fusion ». Sert à dater ce qu'on attend : l'âge du
    dernier jalon est le temps que le lot passe dans sa phase courante.
    """
    for name, at_key, _duration in MILESTONES:
        if row.get(at_key):
            return name, float(row[at_key])
    return "demande", float(row["requested_ts"]) if row.get("requested_ts") else None


def durations(row: dict) -> list[tuple[str, float | None]]:
    """Les durées de la frise, dans l'ordre : demande → gel → revue → fusion.

    Les compteurs (`verdicts`, `blocked_verdicts`) restent dans la ligne : ce
    ne sont pas des durées, et les confondre afficherait « 1s » pour un verdict.
    """
    return [(label, row.get(key)) for label, key in MILESTONE_DURATIONS]
