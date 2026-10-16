# SPDX-License-Identifier: AGPL-3.0-only
"""Le courrier qui confie du travail est lié aux lots (lot L118).

Constat du 2026-10-10 : l'orchestrateur confiait le travail par courrier
(`ameesh mail send`) sans enregistrer de lot. `ameesh projects` montrait
alors des agents « sans lot » qui travaillaient, les alertes de
sous-utilisation se trompaient et la frise restait vide.

* Un orchestrateur (même détection que `orchestrator_held`, L94 :
  déclaré, rôle au canon, ou agent qui a confié des lots) qui écrit à un
  agent SANS lot ouvert est averti (sortie d'erreur et fil du message).
* `--lot <id|réf>` : le lot est rattaché au fil, et assigné au destinataire
  par l'attribution gardée (L37) quand il n'a pas d'agent assigné (libre ou
  tenu par un humain). Un lot déjà tenu par un AGENT n'est jamais remis par
  la seule étiquette du courrier : ni celui d'un autre (le courrier d'un
  relecteur ne vole pas le lot de l'auteur), ni celui de l'expéditeur
  lui-même (#1066, incident #1046 : un courrier d'information a emporté le
  lot qu'il citait). Un rôle déclaré n'y change rien (#859) : la remise est
  un geste explicite, `ameesh work assign`. Un lot SANS agent assigné reste
  distribuable par l'historique de distribution (L94 ; le bureau distribue
  ainsi, canon §4) — ou par le geste explicite `--assign` (#964), ouvert à
  tout expéditeur.
  #858 : un refus de la garde qui ne tient qu'au DESTINATAIRE (profil
  divergé en attendant le prochain canon sync, responsable manquant, agent
  externe…) ne fait pas perdre le message — il est déposé, rattaché au lot,
  l'attribution est différée et l'expéditeur est averti.
* `--new-lot "titre"` : crée le lot, assigné au destinataire, en un geste ;
  #858 : si le destinataire n'est pas attribuable à l'instant, le lot est
  créé sans assigné et le message part quand même, avec l'avertissement.

Sans option, rien ne change, hors l'avertissement. Un `--lot` qui ne
désigne aucun lot connu reste une simple étiquette du fil, comme avant.
Correctif du 2026-10-11 : pour tout expéditeur, un `--lot` qui désigne un
lot ouvert (numéro ou étiquette unique) est enregistré par son numéro.
Correctif du 2026-10-13 (#1066) : la remise implicite du lot tenu par
l'expéditeur lui-même est refusée comme celle du lot d'un autre agent.
Ajout #964 : `--assign` est le geste explicite qui autorise l'attribution
d'un lot LIBRE (ou tenu par un humain) par un expéditeur que l'historique
ne détecte pas comme orchestrateur ; il ne prend jamais le lot d'un agent,
même le sien (la remise passe par `ameesh work assign`).
"""
from __future__ import annotations

import os

from . import storage
from . import work as work_mod

#: variable qui déclare des orchestrateurs (comme `ameesh alerts --orchestrators`)
ORCHESTRATORS_ENV = "AMEESH_ALERT_ORCHESTRATORS"


class AssignmentError(RuntimeError):
    """Rattachement refusé : rien n'est déposé, le message dit quoi faire."""


def is_orchestrator(cfg, db, name: str, *, roles=None, history: bool = True) -> bool:
    """`name` est-il un orchestrateur ? Déclaré (`AMEESH_ALERT_ORCHESTRATORS`),
    agent qui a confié des lots dans les 30 derniers jours, ou fiche Agent du
    canon dont `roles` nomme un orchestrateur (`sous_utilisation.orchestrators`,
    lu en dernier : le canon coûte une lecture du dépôt). `roles` : les rôles
    du canon qui comptent (défaut : orchestrateur ; `work.CORRECTOR_ROLES`
    ajoute les agents de conception). `history=False` (#740) : les lots confiés
    ne comptent pas, seule une déclaration compte."""
    from . import sous_utilisation as su

    declared = {su._bare(n) for n in (os.environ.get(ORCHESTRATORS_ENV) or "").split(",")
                if n.strip()}
    if name in declared:
        return True
    try:
        if history and name in {su._bare(n) for n in storage.of(db).operations.assigners(
                since_s=su.ASSIGNERS_SINCE_S)}:
            return True
    except Exception:
        pass
    try:
        canons = None
        if roles is not None:
            # Correction roles are authority from this agent's own canon,
            # never from a namesake card in another configured canon.
            from . import canon, registry
            row = registry.get(db, name)
            canons = [c for c in (canon.load_configured(cfg) if cfg is not None else [])
                      if row is not None and c.owns(row.get("canon"))]
        return name in su.orchestrators(cfg, db, [{"name": name}], declared=(),
                                        canons=canons, roles=roles or su.ORCHESTRATOR_ROLES,
                                        history=history)
    except Exception:
        return False


def resolve_lot(db, ref: str) -> dict | None:
    """Le lot OUVERT désigné par `ref` : son numéro (`12`, `#12`), sinon une
    référence unique (issue, fiche du plan, branche, premier mot du titre).
    None si rien ne correspond ; AssignmentError si la référence est ambiguë
    ou si le lot numéroté est déjà livré ou fermé."""
    text = (ref or "").strip()
    number = text[1:] if text.startswith("#") else text
    if number.isdigit():
        item = work_mod.get(db, int(number))
        if item is None:
            return None
        if item["state"] in work_mod.MERGED_STATES or item["state"] == "closed":
            raise AssignmentError("lot #%s already %s: nothing to link (new lot: "
                                  "--new-lot \"title\")" % (number, item["state"]))
        return item
    rows = storage.of(db).work.open_by_ref(text)
    if len(rows) > 1:
        raise AssignmentError("ambiguous reference %r: lots %s — give the number"
                              % (text, ", ".join("#%d" % int(r["id"]) for r in rows)))
    return rows[0] if rows else None


def _assignee_refusal(db, name: str, cfg) -> str | None:
    """#858 : la raison pour laquelle la garde d'attribution (L37) refuse
    `name` comme assigné, ou None s'il est attribuable.

    Ne juge QUE le destinataire : un refus du lot lui-même (référence
    ambiguë, lot déjà livré, estimation manquante, projet inconnu) n'est pas
    de cette nature et reste fatal avant tout dépôt."""
    try:
        work_mod.check_assignee(db, name, cfg=cfg)
    except work_mod.WorkError as exc:
        return str(exc)
    return None


def _assignment_deferred(item: dict, recipient: str, why: str) -> str:
    """#858 : l'avertissement d'une attribution différée — le message part
    quand même, rattaché au lot, qui reste sans nouvel assigné."""
    return ("lot #%d not assigned to %s: %s — the message is dropped anyway and "
            "linked to lot #%d (the lot stays %s); to hand it over once %s can be "
            "woken: ameesh work assign %d %s"
            % (int(item["id"]), recipient, why, int(item["id"]),
               (item.get("assignee") or "").strip() or "unassigned",
               recipient, int(item["id"]), recipient))


def link_message(cfg, db, *, sender: str, recipient: str, lot: str | None = None,
                 new_lot: str | None = None, branch: str | None = None,
                 estimate=None, app: str | None = None, new_project: bool = False,
                 assign: bool = False, force: str | None = None,
                 parent_lot: int | None = None) -> dict:
    """Ce que le message `sender` → `recipient` fait aux lots, AVANT son dépôt.

    Rend `{lot, work_item_id, assigned, created, branch, warnings, orchestrator}` :
    `work_item_id` est l'étiquette du fil (le numéro du lot s'il est connu,
    sinon `lot` tel quel). `branch` (déduite du message par l'appelant) est
    posée sur le lot qui n'en a pas. Lève AssignmentError quand le LOT ne peut
    pas être rattaché (rien n'est alors déposé).

    #858 : un refus de la garde d'attribution qui ne tient qu'au DESTINATAIRE
    (profil divergé en attendant le prochain canon sync, responsable manquant,
    agent externe…) ne fait plus perdre le message : il est déposé, rattaché au
    lot, l'attribution est différée et l'avertissement dit pourquoi. Un
    message perdu emportait le réveil ; un réveil différé se rattrape à la
    prochaine réclamation.

    `assign` (#964) : le geste explicite `--assign` du courrier. Sans lui, un
    `--lot` n'assigne que venant d'un expéditeur que la détection
    d'orchestrateur reconnaît (dont l'historique de distribution, L94) ; il
    reste sinon une étiquette de fil. Avec lui, tout expéditeur peut confier
    un lot LIBRE (ou tenu par un humain), jamais un lot tenu par un agent —
    même le sien (#1066) : la remise passe par `ameesh work assign`.

    L157 : `estimate` est la durée estimée du lot créé par `new_lot` ; elle
    est obligatoire quand l'expéditeur est un agent (refus, rien n'est
    déposé), un avertissement pour un humain.

    #540 : un lot que le destinataire ne peut recevoir (il a déjà un lot non
    démarré) va dans la file d'équipe et le message n'est pas déposé
    (AssignmentError) ; sauf `force`, la raison d'un `--force` motivé.

    #1193: `parent_lot` is the explicit parent of the suite lot created by
    `new_lot` — the blocking edge is written together with the creation
    (refused without `new_lot`)."""
    out = {"lot": None, "work_item_id": lot, "assigned": False, "created": False,
           "branch": None, "warnings": [], "orchestrator": False}
    if recipient == "all" or ":" in recipient:
        return out
    if parent_lot is not None and new_lot is None:
        raise AssignmentError("--parent-lot goes with --new-lot (the parent of the suite)")
    if new_lot is None:
        # le cas courant (aucune option, destinataire déjà sur un lot) ne lit
        # rien d'autre : la détection d'un orchestrateur peut lire le canon
        has_open = bool(work_mod.open_lots(db, recipient))
        if lot is None and has_open:
            return out
        # #964 : `--assign` est le geste explicite qui dispense de la
        # détection d'orchestrateur. Sans lui, l'historique de distribution
        # (L94) garde le droit de confier un lot LIBRE — c'est ainsi que le
        # bureau distribue (canon §4, #859) ; un lot tenu par un agent, lui,
        # n'est jamais remis par l'étiquette, même à un orchestrateur
        # déclaré (#1066, plus bas).
        orchestrator = is_orchestrator(cfg, db, sender)
        if not assign and not orchestrator:
            # une étiquette qui désigne un lot ouvert est rendue par son
            # numéro, pour tout expéditeur : le fil, l'activité du lot et le
            # lot en cours (`ameesh projects`) la lisent. Sinon, étiquette
            # libre (inchangé).
            if lot is not None:
                try:
                    item = resolve_lot(db, lot)
                except AssignmentError:
                    item = None
                if item is not None:
                    out.update(lot=item, work_item_id=str(item["id"]))
            return out
        out["orchestrator"] = orchestrator
    else:
        has_open = True
        out["orchestrator"] = is_orchestrator(cfg, db, sender)
    item = None
    try:
        if new_lot is not None:
            from . import estimates
            title = new_lot.strip()
            if not title:
                raise AssignmentError("--new-lot: empty title")
            minutes = estimates.parse(estimate) if estimate not in (None, "") else None
            warn = estimates.check(db, sender, minutes,
                                   option="--estimate 2h alongside --new-lot")
            from . import work_projects
            project = work_projects.mail_project(cfg, db, sender, recipient, app,
                                                 new_project=new_project)
            # #506: read before the lot exists (it makes its project known)
            introduced = (work_projects.introduction(cfg, db, project, sender)
                          if new_project and app is not None else "")
            # #858 : un destinataire momentanément non réveillable ne fait pas
            # perdre le message : le lot est créé SANS assigné, l'attribution
            # est différée et l'expéditeur est averti (même règle que --lot).
            refusal = _assignee_refusal(db, recipient, cfg)
            item = work_mod.add(db, title=title,
                                assignee=None if refusal else recipient, actor=sender,
                                source="mail", app=project, cfg=cfg,
                                branch=None if refusal else branch,
                                estimate=minutes, note=introduced, force=force,
                                parent_lot=parent_lot)
            if not refusal and item.get("queue_warning"):
                raise AssignmentError(_queued(item))
            out.update(created=True, assigned=not refusal, branch=item.get("branch"))
            if refusal:
                out["warnings"].append(_assignment_deferred(item, recipient, refusal))
            if warn:
                out["warnings"].append(estimates.missing(item["id"]))
            # canon §2 (15/10/2026) : une suite de relecture (N, P2, P3) ne
            # crée pas de lot, elle va au backlog — le courrier l'avertit,
            # sans rien refuser (`--new-lot` reste possible).
            if item.get("route_hint"):
                out["warnings"].append(item["route_hint"])
        elif lot is not None:
            item = resolve_lot(db, lot)
            if item is None:
                out["warnings"].append(
                    "--lot %s matches no open lot: kept as a plain thread label "
                    "(to create the lot: --new-lot \"title\", or ameesh work add)" % lot)
            else:
                current = (item.get("assignee") or "").strip()
                if current != recipient:
                    # #1066 (incident #1046) : un lot déjà tenu par un AGENT
                    # ne se remet pas par la seule étiquette `--lot` — ni
                    # celui d'un autre (le courrier d'un relecteur ne vole pas
                    # le lot de l'auteur), ni celui de l'expéditeur lui-même.
                    # #859 : un rôle déclaré n'y change rien ; un lot encore
                    # libre reste distribuable par l'historique (L94). La
                    # remise est un geste explicite (`ameesh work assign`).
                    if current and not current.startswith(work_mod.HUMAN_PREFIX):
                        if current == sender:
                            out["warnings"].append(
                                "lot #%d is yours: linked without reassignment to %s "
                                "(to hand it over: ameesh work assign %d %s)"
                                % (int(item["id"]), recipient, int(item["id"]),
                                   recipient))
                        else:
                            out["warnings"].append(
                                "lot #%d belongs to %s: not reassigned to %s (to "
                                "take it over: ameesh work assign %d %s)"
                                % (int(item["id"]), current, recipient, int(item["id"]),
                                   recipient))
                    else:
                        # #858 : un refus de la garde d'attribution qui ne
                        # tient qu'au DESTINATAIRE (profil divergé en attendant
                        # le prochain canon sync, responsable manquant, agent
                        # externe…) est différé : le message part quand même,
                        # rattaché au lot, l'attribution est reportée.
                        # R1 mesh-design (13/10 23:10) : une erreur du LOT ou
                        # de la course CAS (« already merged », « changed in
                        # the meantime ») n'est PAS un refus d'attribution —
                        # elle remonte (AssignmentError, rien n'est déposé) au
                        # lieu d'un faux avertissement différé. On ne juge donc
                        # le destinataire qu'avant l'appel, et `assign` n'est
                        # pas enveloppé.
                        refusal = _assignee_refusal(db, recipient, cfg)
                        if refusal:
                            out["warnings"].append(
                                _assignment_deferred(item, recipient, refusal))
                        else:
                            done = work_mod.assign(db, int(item["id"]), recipient,
                                                   actor=sender, cfg=cfg, force=force)
                            item = done["item"]
                            if done.get("queue_warning"):
                                raise AssignmentError(_queued(dict(item, **done)))
                            out["assigned"] = True
                            if done["check"].get("warning"):
                                out["warnings"].append(done["check"]["warning"])
                if branch and not item.get("branch") \
                        and (item.get("assignee") or "") == recipient:
                    item = work_mod.set_branch(db, int(item["id"]), branch, actor=sender,
                                               source="cited by the mail")
                    out["branch"] = item.get("branch")
    except work_mod.WorkError as exc:
        raise AssignmentError(str(exc)) from exc
    if item is not None:
        out.update(lot=item, work_item_id=str(item["id"]))
    # #1407 : l'avertissement ne vaut que pour un dépôt SANS lien. Un `--lot`
    # résolu est un rattachement effectif (le fil et l'activité du lot le
    # lisent) même quand la garde d'attribution ne confie pas le lot au
    # destinataire ; seule une étiquette sans lot ouvert reste sans lien.
    if not has_open and not out["assigned"] and item is None \
            and not _is_chat(db, recipient):
        out["warnings"].append(
            "%s has no open lot: this work will show up neither in ameesh projects "
            "nor in alerts — link it (--lot <id|ref>) or create it "
            "(--new-lot \"title\")" % recipient)
    return out


def _queued(item: dict) -> str:
    """#540 : le lot est allé dans la file d'équipe, pas au destinataire ; le
    message n'est pas déposé (il confierait un lot que L165 peut donner à un
    autre)."""
    return ("%s; resend without --lot, or once the recipient has started that lot"
            % item["queue_warning"])


def _is_chat(db, name: str) -> bool:
    """L123 : le chat du propriétaire n'a jamais de lot ; lui écrire (réponse
    d'un orchestrateur) ne confie pas de travail."""
    from . import registry
    from .chat import CHAT_PREFIX, is_chat_agent
    return name.startswith(CHAT_PREFIX) and is_chat_agent(registry.get(db, name))
