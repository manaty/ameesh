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

import time
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
    externe: bool = False,
    cfg=None,
) -> dict:
    """Crée un lot en `intake`. Un assigné passe par l'attribution gardée
    (`check_assignee`, L37) : un agent non réveillable est refusé (WorkError)."""
    if type not in ("bug", "evolution"):
        raise WorkError("type inconnu : %r (bug ou evolution)" % type)
    if not (title or "").strip():
        raise WorkError("titre obligatoire")
    if assignee:
        # L46 : l'assigné enregistré est celui que la garde a normalisé
        assignee = check_assignee(db, assignee, externe=externe, cfg=cfg)["assignee"]
    elif externe:
        raise WorkError("--externe sans assigné : rien à forcer")
    fiche = _package(db, package) if package else None
    return storage.of(db).work.add(
        type=type, source=source, app=app, title=title.strip(), body=body,
        issue_ref=issue_ref, workstream=workstream, assignee=assignee,
        budget_usd=budget_usd, note="création" + (" (plan : %s)" % package if package else ""),
        actor=actor, package_id=fiche["id"] if fiche else None,
        package_parent=fiche.get("parent") if fiche else None)


# --------------------------------------------------------------------------
# attribution gardée (L37, règle 2 de la décision 0030)
# --------------------------------------------------------------------------

#: préfixe d'un assigné humain : un lot qui attend un humain n'a rien à réveiller
HUMAN_PREFIX = "human:"


def configured_canons(cfg) -> list:
    """Les canons configurés, chargés (L46, pour `known_human`) ; [] sans
    canon ou si l'un d'eux est illisible. Jamais d'exception."""
    if cfg is None:
        return []
    try:
        from . import canon as canon_mod
        if not canon_mod.configured(cfg):
            return []
        return list(canon_mod.from_config_all(cfg))
    except Exception:
        return []


def known_human(db: Db, name: str, cfg=None, *, canons=None) -> str | None:
    """L46 : `human:<id>` si le nom NU `name` désigne un humain connu, sinon
    None. Connu : humain déclaré de la configuration (`AMEESH_HUMANS`, dont le
    propriétaire), fiche `Member` d'un canon configuré (résolution stricte du
    canon : un Member unique, pas homonyme d'un agent), ou humain déjà résolu
    en base (responsable d'un agent ou d'un paquet du canon). Jamais
    d'exception : un canon illisible ne compte simplement pas. `canons` :
    une fonction qui rend les canons chargés (cache de l'appelant), sinon
    ils sont lus ici, au besoin."""
    name = (name or "").strip()
    if not name or ":" in name:
        return None
    human = HUMAN_PREFIX + name
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    if cfg is not None and name in (getattr(cfg, "human_names", None) or ()):
        return human
    try:
        if storage.of(db).agents.human_known(human):
            return human
    except Exception:
        pass
    for canon in (canons() if canons is not None else configured_canons(cfg)):
        try:
            if canon.resolve_human(human):
                return human
        except Exception:
            continue
    return None


#: L46 : ce qu'il faut faire d'un nom refusé
_ASSIGNEE_HINT = ("inscrivez l'agent (agent-runner register <nom> <harnais> …) pour "
                  "qu'ameesh le réveille, attribuez à un humain (human:<id>), ou, pour "
                  "une session externe qui a un responsable humain, ajoutez --externe")


def check_assignee(db: Db, assignee: str, *, externe: bool = False, cfg=None) -> dict:
    """L37 (0030, règle 2) : un lot ne va qu'à un agent qu'ameesh sait réveiller.

    Rend `{assignee, externe, responsible, warning}` ou lève WorkError avec la
    raison. Un humain (`human:…`) est toujours accepté. Un agent doit être
    réveillable (`registry.wakeable` : connu, `execute`, admis, responsable
    s'il est requis). `externe=True` (option `--externe`) force l'attribution
    à un agent EXTERNE, seulement s'il a un humain responsable : c'est lui qui
    portera l'attente et les alertes du lot. Un nom inconnu est toujours
    refusé. Un agent arrêté reste accepté, avec un avertissement (`warning`).

    L46 : un nom NU inconnu du registre mais qui désigne un humain connu
    (`known_human` : Member d'un canon, humain déclaré) est normalisé en
    `human:<id>`, avec un avertissement — `assignee` rendu est alors le nom
    normalisé, à enregistrer. Un canon momentanément invalide ne bloque pas
    l'attribution (avertissement seulement). Les refus disent quoi faire.
    """
    from . import registry

    name = (assignee or "").strip()
    if not name:
        raise WorkError("assigné vide")
    if name.startswith(HUMAN_PREFIX):
        if externe:
            raise WorkError("--externe vise un agent externe, pas un humain (%s)" % name)
        return {"assignee": name, "externe": False, "responsible": None, "warning": None}
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    row = registry.get(db, name)
    if row is None:
        human = known_human(db, name, cfg)
        if human and not externe:
            return {"assignee": human, "externe": False, "responsible": None,
                    "warning": "« %s » n'est pas un agent mais un humain connu : lot "
                               "attribué à %s" % (name, human)}
        raise WorkError("attribution refusée : %s n'est pas dans le registre (agent "
                        "inconnu) : personne ne travaillerait sur ce lot — %s"
                        % (name, _ASSIGNEE_HINT))
    mode = row.get("mode") or "execute"
    responsible = (row.get("responsible") or "").strip() or None
    if externe:
        if mode != "externe":
            raise WorkError("--externe refusé : %s est un agent %s (réveillable par "
                            "ameesh : attribuez-le sans --externe)" % (name, mode))
        if not responsible:
            raise WorkError("--externe refusé : l'agent externe %s n'a pas de responsable "
                            "humain pour porter le lot (0030) — désignez-le d'abord "
                            "(canon, ou ameesh set)" % name)
        return {"assignee": name, "externe": True, "responsible": responsible,
                "warning": None}
    ok, why, note = registry.wake_status(db, cfg, name)
    if not ok:
        if mode == "externe":
            hint = (" — son responsable peut forcer avec --externe" if responsible
                    else " — désignez d'abord son responsable humain, puis --externe")
        else:
            hint = (" — corrigez son inscription ou son admission (agent-runner register "
                    "…, canon sync), ou attribuez à un humain (human:<id>)")
        raise WorkError("attribution refusée : %s%s" % (why, hint))
    warnings = []
    if row.get("status") == "stopped":
        warnings.append("%s est arrêté (%s) : le lot attendra sa reprise"
                        % (name, row.get("stop_reason") or "raison inconnue"))
    if note:
        warnings.append(note)
    return {"assignee": name, "externe": False, "responsible": responsible,
            "warning": " ; ".join(warnings) or None}


def assign(db: Db, item_id: int, assignee: str, *, externe: bool = False, actor: str = "",
           cfg=None) -> dict:
    """Réassigne un lot ouvert (L37, `ameesh work assign`), sous la même garde
    que la création (`check_assignee`). Une ligne de journal dit l'ancien et
    le nouvel assigné. Rend `{item, check, previous}`."""
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s introuvable" % item_id)
    if item["state"] in MERGED_STATES or item["state"] == "closed":
        raise WorkError("lot %s déjà %s : rien à réassigner" % (item_id, item["state"]))
    previous = item.get("assignee")
    check = check_assignee(db, assignee, externe=externe, cfg=cfg)
    if previous == check["assignee"]:
        raise WorkError("lot %s déjà assigné à %s" % (item_id, previous))
    note = "assigné à %s%s (avant : %s)%s" % (
        check["assignee"],
        " — session externe, responsable %s" % check["responsible"] if check["externe"] else "",
        previous or "personne",
        # L40 (0030) : la réassignation efface la délégation en cours
        " ; délégation de %s annulée" % item["delegated_by"]
        if item.get("delegated_by") and item.get("due_ts") else "")
    row = storage.of(db).work.assign(item_id, check["assignee"], current=previous, note=note,
                                     actor=actor)
    if row is None:
        raise WorkError("lot %s modifié entre-temps : réessayez" % item_id)
    return {"item": row, "check": check, "previous": previous}


# --------------------------------------------------------------------------
# délégation à échéance (L40, point 5 de la décision 0030)
# --------------------------------------------------------------------------

#: expéditeur des événements qu'ameesh écrit lui-même (échéance d'une délégation)
SYSTEM_SENDER = "ameesh"
#: issues d'une délégation au registre `work_item_delegations`
DELEGATION_OUTCOMES = ("soldee", "rendue", "remplacee", "annulee")


def span(seconds) -> str:
    """Une durée lisible : « 45 s », « 12 min », « 2 h 05 », « 1 j 3 h »."""
    seconds = int(max(0, round(float(seconds or 0))))
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        minutes = (seconds % 3600) // 60
        return ("%d h %02d" % (seconds // 3600, minutes)) if minutes \
            else "%d h" % (seconds // 3600)
    return "%d j %d h" % (seconds // 86400, (seconds % 86400) // 3600)


def parse_within(within) -> float:
    """`--within` : secondes, ou une durée « 30m », « 2h » (format de
    `stagnation.parse_duration`)."""
    from . import stagnation

    if isinstance(within, (int, float)) and not isinstance(within, bool):
        if float(within) <= 0:
            raise WorkError("échéance nulle ou négative : %r" % within)
        return float(within)
    try:
        return stagnation.parse_duration(str(within or ""))
    except stagnation.StaleError as exc:
        raise WorkError("--within : %s" % exc) from exc


def delegate(db: Db, item_id: int, agent: str, *, within, actor: str = "",
             cfg=None) -> dict:
    """L40 (0030, point 5) : `ameesh work delegate <id> <agent> --within 30m`.

    Le délégant est `actor`, à défaut l'assigné actuel (un `human:…` est
    admis) ; c'est à lui que le lot reviendra. Le délégué passe par
    l'attribution gardée (`check_assignee`, mêmes refus que `work assign`) et
    doit être un AGENT : une échéance se mesure en tours. Une nouvelle
    délégation remplace la précédente (et son échéance). Un événement
    (`kind=event`, lié au lot) réveille le délégué.

    Rend `{item, delegation, replaced, check, previous, message_id, warning}`.
    """
    within_s = parse_within(within)
    item = get(db, item_id)
    if item is None:
        raise WorkError("lot %s introuvable" % item_id)
    if item["state"] in MERGED_STATES or item["state"] == "closed":
        raise WorkError("lot %s déjà %s : rien à déléguer" % (item_id, item["state"]))
    name = (agent or "").strip()
    if name.startswith(HUMAN_PREFIX):
        raise WorkError("délégation refusée : %s est un humain ; une délégation à "
                        "échéance va à un agent réveillable (pour un humain : "
                        "ameesh work assign)" % name)
    previous = item.get("assignee")
    delegator = (actor or "").strip() or (previous or "").strip()
    if not delegator:
        raise WorkError("délégation refusée : le lot %s n'a pas d'assigné ; précisez "
                        "--actor (le délégant, à qui le lot reviendra)" % item_id)
    if delegator == name:
        raise WorkError("délégation refusée : %s se déléguerait le lot à lui-même" % name)
    if not delegator.startswith(HUMAN_PREFIX):
        from . import registry

        if registry.get(db, delegator) is None:
            raise WorkError("délégation refusée : le délégant %s n'est pas dans le "
                            "registre ; le lot ne pourrait revenir à personne" % delegator)
    check = check_assignee(db, name, cfg=cfg)
    if check["assignee"].startswith(HUMAN_PREFIX):
        # L46 : un nom nu normalisé en humain n'est pas un délégué possible
        raise WorkError("délégation refusée : %s désigne un humain (%s) ; une délégation "
                        "à échéance va à un agent réveillable (pour un humain : ameesh "
                        "work assign)" % (name, check["assignee"]))
    note = "délégué à %s par %s, échéance dans %s (avant : %s)" % (
        name, delegator, span(within_s), previous or "personne")
    out = storage.of(db).work.delegate(
        item_id, name, current=previous, delegated_by=delegator, within_s=within_s,
        note=note, actor=delegator)
    if out is None:
        raise WorkError("lot %s modifié entre-temps : réessayez" % item_id)
    out.update(check=check, previous=previous, message_id=None, warning=check.get("warning"))
    lot = out["item"]
    body = ("Lot #%d « %s » : %s te le délègue, à prendre en charge dans %s. Sans tour "
            "de ta part sur ce lot à l'échéance, il revient à %s."
            % (int(lot["id"]), lot.get("title") or "", delegator, span(within_s), delegator))
    from . import mail

    try:
        out["message_id"] = mail.send(
            db, delegator, name, body, kind="event", work_item_id=str(lot["id"]),
            payload={"delegation": {"lot": int(lot["id"]), "delegated_by": delegator,
                                    "due_ts": lot.get("due_ts")}})
    except Exception as exc:  # noqa: BLE001 - la délégation est écrite ; le dire
        out["warning"] = "événement non déposé pour %s (%s) : prévenez-le" % (name, exc)
    return out


def _resolution_note(outcome: str, row: dict) -> str | None:
    """La ligne de journal d'une délégation échue (écrite avec son issue)."""
    if outcome == "rendue":
        return ("délégation échue : retour à %s (aucun tour de %s sur le lot depuis la "
                "délégation)" % (row["delegated_by"], row["delegate"]))
    if outcome == "soldee":
        return "délégation soldée : %s a travaillé sur le lot avant l'échéance" % row["delegate"]
    return None


def expire_delegations(db: Db, now: float | None = None, *, actor: str = SYSTEM_SENDER,
                       dry_run: bool = False) -> list[dict]:
    """L40 (0030, point 5) : traite les délégations dont l'échéance est passée.

    Pour chaque délégation en cours échue (`due_at < now`) : sans AUCUN tour
    du délégué sur ce lot depuis son début (ni transition, note, jalon,
    action ou message lié de sa part), le lot revient au délégant (issue
    `rendue`, journal « délégation échue : retour à X », événement dans la
    boîte du délégant, qui le réveille) ; avec du travail constaté, la
    délégation est soldée (échéance effacée). Un lot fermé ou réassigné
    entre-temps : délégation annulée, sans bruit.

    Appelée à chaque passe de l'exécuteur ; deux exécuteurs simultanés ne
    rendent jamais deux fois (verrou de ligne et recontrôle, voir le
    stockage). `dry_run` : ce qui serait fait, rien n'est écrit. Rend une
    ligne par délégation traitée, avec `outcome` (et `message_id`).
    """

    now = time.time() if now is None else float(now)
    st = storage.of(db).work
    out: list[dict] = []
    for row in st.due_delegations(now):
        if dry_run:
            if row["state"] in MERGED_STATES or row["state"] == "closed" \
                    or row.get("assignee") != row["delegate"]:
                outcome = "annulee"
            else:
                outcome = "soldee" if row.get("worked") else "rendue"
            out.append(dict(row, outcome=outcome, dry_run=True, message_id=None))
            continue
        done = st.resolve_delegation(int(row["delegation_id"]), now_ts=now, actor=actor,
                                     describe=_resolution_note)
        if done is None:
            continue  # traitée entre-temps (autre exécuteur)
        done["message_id"] = None
        delegator = done["delegated_by"]
        if done["outcome"] == "rendue" and not delegator.startswith(HUMAN_PREFIX):
            from . import mail

            body = ("Lot #%d « %s » : délégation échue, il te revient. %s n'a fait aucun "
                    "tour sur ce lot avant l'échéance."
                    % (int(done["work_item_id"]), done.get("title") or "", done["delegate"]))
            try:
                done["message_id"] = mail.send(
                    db, SYSTEM_SENDER, delegator, body, kind="event",
                    work_item_id=str(done["work_item_id"]),
                    payload={"delegation_expired": {
                        "lot": int(done["work_item_id"]), "delegate": done["delegate"],
                        "due_ts": done.get("due_ts")}})
            except Exception as exc:  # noqa: BLE001 - le retour est fait ; l'alerte suit
                done["warning"] = "événement non déposé pour %s (%s)" % (delegator, exc)
        out.append(done)
    return out


def mark_delegate_turn(db: Db, agent: str, lots) -> list[int]:
    """L40 : un tour de `agent` commence sur ces lots (lot du tour, lots des
    messages du tour). Ceux qui lui sont délégués reçoivent leur premier tour,
    une seule fois : la délégation ne reviendra pas au délégant."""
    ids = sorted({int(str(lot).strip()) for lot in lots or ()
                  if str(lot or "").strip().isdigit()})
    if not ids:
        return []
    return storage.of(db).work.mark_delegate_turn(
        agent, ids, "délégation : tour de %s sur le lot" % agent)


def delegation_view(item: dict, now: float | None = None) -> dict | None:
    """La délégation d'un lot, pour `work list`/`work show`/`progress` (L40).

    None hors délégation (ou lot fermé). Sinon `{delegated_by, delegate,
    delegated_ts, due_ts, due_in_s, overdue, settled, label}` ; `due_in_s`
    est négatif en retard ; `settled` : délégation soldée (le délégué a
    travaillé, l'échéance est effacée).
    """

    if not item.get("delegated_by") or item.get("state") in MERGED_STATES \
            or item.get("state") == "closed":
        return None
    now = time.time() if now is None else float(now)
    due = item.get("due_ts")
    due_in = int(round(float(due) - now)) if due is not None else None
    overdue = due_in is not None and due_in < 0
    who = "délégué par %s" % item["delegated_by"]
    if due is None:
        label = "%s, échéance soldée (travail constaté)" % who
    elif overdue:
        label = "%s, en retard de %s" % (who, span(-due_in))
    else:
        label = "%s, échéance dans %s" % (who, span(due_in))
    return {"delegated_by": item["delegated_by"], "delegate": item.get("assignee"),
            "delegated_ts": item.get("delegated_ts"), "due_ts": due,
            "due_in_s": due_in, "overdue": overdue, "settled": due is None,
            "label": label}


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
