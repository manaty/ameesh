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
Added #964: `--assign` is the explicit gesture that lets a sender the history
does not detect as an orchestrator hand over a FREE lot (or one held by a
human); it never takes a lot held by an agent, not even its own (handing over
goes through `ameesh work assign`).
"""
from __future__ import annotations

import os

from . import storage
from .db import DbError
from . import work as work_mod

#: variable qui déclare des orchestrateurs (comme `ameesh alerts --orchestrators`)
ORCHESTRATORS_ENV = "AMEESH_ALERT_ORCHESTRATORS"


class AssignmentError(RuntimeError):
    """Rattachement refusé : rien n'est déposé, le message dit quoi faire."""


def is_orchestrator(cfg, db, name: str, *, roles=None, history: bool = True,
                    strict: bool = False) -> bool:
    """`name` est-il un orchestrateur ? Déclaré (`AMEESH_ALERT_ORCHESTRATORS`),
    fiche Agent de son canon propriétaire dont `roles` nomme un orchestrateur
    (`sous_utilisation.orchestrators`), ou — tant que son organisation ne
    déclare aucun rôle (L242c) — agent qui a confié des lots dans les 30
    derniers jours. `roles` : les rôles du canon qui comptent (défaut :
    orchestrateur ; `work.CORRECTOR_ROLES` ajoute les agents de conception).
    `history=False` (#740) : les lots confiés ne comptent pas, seule une
    déclaration compte.

    #3375 : `strict=True` distingue « pas un orchestrateur » d'une **autorité
    illisible**. Le défaut garde la tolérance des alertes (une panne du
    registre ou du canon ne doit pas les faire taire) ; la porte du paquet lit
    en strict, car y ignorer un rôle ferait composer un lot réservé. En strict,
    une résolution d'alias, une lecture de registre ou de canon qui lève, un
    canon configuré non chargé ou lu en partie (`fully_read`) lèvent au lieu de
    conclure « non » : le doute refuse.
    """
    from .agent_names import resolve as resolve_agent_name
    try:
        name = resolve_agent_name(name, db=db, operation="assignments.is_orchestrator")
    except Exception:
        # Registre indisponible (base hors service, doublure de test #424) :
        # on garde l'orthographe reçue, comme avant la résolution d'alias.
        if strict:
            raise
        pass
    from . import sous_utilisation as su

    declared = {resolve_agent_name(su._bare(n), db=db, operation="authority.orchestrator")
                for n in (os.environ.get(ORCHESTRATORS_ENV) or "").split(",")
                if n.strip()}
    if name in declared:
        return True
    try:
        from . import canon, registry
        canons = canon.load_configured(cfg) if cfg is not None else []
        if strict:
            if cfg is None:
                raise RuntimeError(
                    "no configuration: the canon and its agent roles are unreadable")
            entries = canon.configured(cfg)
            if len(canons) < len(entries):
                raise RuntimeError(
                    "canon unreadable: %d of %d configured canon(s) loaded"
                    % (len(canons), len(entries)))
            partial = [str(getattr(c, "id", "?")) for c in canons
                       if not su.fully_read(c)]
            if partial:
                raise RuntimeError("canon partially read: %s" % ", ".join(partial))
        row = registry.get(db, name) if db is not None else None
    except Exception:
        if strict:
            raise
        canons, row = [], None
    row = dict(row or {}, name=name)
    try:
        return name in su.orchestrators(cfg, db, [row], declared=(), canons=canons,
                                        roles=roles or su.ORCHESTRATOR_ROLES,
                                        history=history)
    except Exception:
        if strict:
            raise
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
    except DbError as exc:
        # #5099 : schéma en retard pendant une alerte de migration. La
        # garde reste fermée pour l’attribution, sans perdre le courrier.
        if exc.sqlstate not in ("42703", "42P01"):
            raise
        return "assignment check unavailable (schema not aligned)"
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


def _footprint_deferred(item: dict, recipient: str, exc) -> str:
    """#4186 : l'avertissement d'une attribution différée par un chevauchement
    d'empreinte — le message part quand même, le lot reste libre et attend la
    fusion du lot en cours. #4818 : ou par le plafond de lots en vol du dépôt."""
    if getattr(exc, "cap", None) is not None:
        return ("lot #%d not assigned to %s: its repository reached its in-flight cap "
                "(%d/%d lots in progress) — the message is dropped anyway and linked to "
                "lot #%d, which stays unassigned and waits for a merge; to hand it over "
                "then: ameesh work assign %d %s"
                % (int(item["id"]), recipient, exc.in_flight, exc.cap, int(item["id"]),
                   int(item["id"]), recipient))
    overlaps = exc.overlaps
    parts = ", ".join(
        "#%d (%s%s: %s ~ %s)" % (
            int(row["id"]), row.get("state") or "?",
            ", %s" % row["assignee"] if row.get("assignee") else "",
            row.get("path"), row.get("other_path")) for row in overlaps)
    return ("lot #%d not assigned to %s: its footprint overlaps lot(s) in progress "
            "in the same repository (%s) — the message is dropped anyway and linked "
            "to lot #%d, which stays unassigned and waits for the merge; to hand it "
            "over then: ameesh work assign %d %s"
            % (int(item["id"]), recipient, parts, int(item["id"]),
               int(item["id"]), recipient))


def _branch_owner(db, branch: str, exclude: int | None = None):
    """#1646 : le lot qui porte DÉJÀ cette branche, sinon None.

    Une branche citée par un courrier peut être celle du parent d'un lot
    empilé : la poser sur ce lot le ferait clore à la fusion du parent, sans
    son propre contenu. Un lot terminal compte aussi : sa branche a déjà
    atterri, la reposer fermerait le nouveau lot aussitôt."""
    sql = "SELECT id, state, title FROM work_items WHERE branch = %s"
    params: tuple = (branch,)
    if exclude is not None:
        sql += " AND id != %s"
        params += (int(exclude),)
    rows = db.query(sql + " ORDER BY id LIMIT 1", params)
    return rows[0] if rows else None


def _cited_branch_warning(owner: dict, branch: str, lot_id: int, recipient: str) -> str:
    return ("branch %s already belongs to lot #%d (%s): not set on lot #%d — a branch "
            "closes its own lot; to ride it anyway, set it explicitly: ameesh work "
            "assign %d %s --branch %s"
            % (branch, int(owner["id"]), owner.get("state") or "?",
               int(lot_id), int(lot_id), recipient, branch))


def link_message(cfg, db, *, sender: str, recipient: str, lot: str | None = None,
                 new_lot: str | None = None, branch: str | None = None,
                 estimate=None, app: str | None = None, new_project: bool = False,
                 assign: bool = False, force: str | None = None,
                 parent_lot: int | None = None) -> dict:
    """Ce que le message `sender` → `recipient` fait aux lots, AVANT son dépôt.

    Rend `{lot, work_item_id, assigned, created, branch, warnings, orchestrator}` :
    `work_item_id` est l'étiquette du fil (le numéro du lot s'il est connu,
    sinon `lot` tel quel). `branch` (déduite du message par l'appelant) est
    posée sur le lot qui n'en a pas, sauf si elle est déjà celle d'un autre
    lot (#1646 : un avertissement dit alors quoi faire). Lève AssignmentError
    quand le LOT ne peut pas être rattaché (rien n'est alors déposé).

    #858 : un refus de la garde d'attribution qui ne tient qu'au DESTINATAIRE
    (profil divergé en attendant le prochain canon sync, responsable manquant,
    agent externe…) ne fait plus perdre le message : il est déposé, rattaché au
    lot, l'attribution est différée et l'avertissement dit pourquoi. Un
    message perdu emportait le réveil ; un réveil différé se rattrape à la
    prochaine réclamation.

    `assign` (#964): the message's explicit `--assign` gesture. Without it, a
    `--lot` only assigns when it comes from a sender the orchestrator
    detection recognises (including the distribution history, L94); otherwise
    it stays a plain thread label. With it, any sender can hand over a FREE
    lot (or one held by a human), never a lot held by an agent — not even its
    own (#1066): handing over goes through `ameesh work assign`.

    L157 : `estimate` est la durée estimée du lot créé par `new_lot` ; elle
    est obligatoire quand l'expéditeur est un agent (refus, rien n'est
    déposé), un avertissement pour un humain.

    #540 : un lot que le destinataire ne peut recevoir (il a déjà un lot non
    démarré) va dans la file d'équipe et le message n'est pas déposé
    (AssignmentError) ; sauf `force`, la raison d'un `--force` motivé.

    #1193: `parent_lot` is the explicit parent of the suite lot created by
    `new_lot` — the blocking edge is written together with the creation
    (refused without `new_lot`)."""
    from .agent_names import resolve as resolve_agent_name
    sender = resolve_agent_name(sender, db=db, operation="assignments.link_message")
    recipient = resolve_agent_name(recipient, db=db, operation="assignments.link_message")
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
        # #964: `--assign` is the explicit gesture that does without the
        # orchestrator detection. Without it, the distribution history (L94)
        # keeps the right to hand over a FREE lot — that is how the bureau
        # distributes (canon §4, #859); a lot held by an agent is never handed
        # over by the label, even to a declared orchestrator (#1066, below).
        # #1701 N2: with `--assign` the gesture decides alone, so the
        # detection (a canon read) is not consulted. `out["orchestrator"]`
        # has no consumer; it reports the detection only when it ran.
        orchestrator = is_orchestrator(cfg, db, sender) if not assign else False
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
            # #1646 : une branche déjà portée par un autre lot n'est pas
            # reposée (elle clorait ce lot à la fusion du sien).
            owner = _branch_owner(db, branch) if branch else None
            # #858 : un destinataire momentanément non réveillable ne fait pas
            # perdre le message : le lot est créé SANS assigné, l'attribution
            # est différée et l'expéditeur est averti (même règle que --lot).
            refusal = _assignee_refusal(db, recipient, cfg)
            creation = dict(title=title,
                            assignee=None if refusal else recipient, actor=sender,
                            source="mail", app=project, cfg=cfg,
                            branch=None if (refusal or owner) else branch,
                            estimate=minutes, note=introduced, force=force,
                            parent_lot=parent_lot)
            overlapped = None
            try:
                item = work_mod.add(db, **creation)
            except storage.FootprintOverlap as exc:
                # #4186 : comme `ameesh work add --assignee`, un lot attribué
                # qui chevauche un lot en cours naît libre et attend sa fusion ;
                # le message part quand même, rattaché au lot (#858).
                overlapped = exc
                creation["assignee"] = None
                item = work_mod.add(db, **creation)
            if not refusal and overlapped is None and item.get("queue_warning"):
                raise AssignmentError(_queued(item))
            out.update(created=True, branch=item.get("branch"),
                       assigned=not refusal and overlapped is None)
            if refusal:
                out["warnings"].append(_assignment_deferred(item, recipient, refusal))
            elif overlapped is not None:
                out["warnings"].append(_footprint_deferred(item, recipient, overlapped))
            if owner is not None:
                out["warnings"].append(_cited_branch_warning(
                    owner, branch, int(item["id"]), recipient))
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
                if assign:
                    # #1701 N1: the explicit gesture names the lot to assign;
                    # a reference matching no open lot is refused before any
                    # deposit instead of degrading to a plain thread label.
                    raise AssignmentError(
                        "--assign: --lot %s matches no open lot (nothing is deposited; "
                        "to create the lot: --new-lot \"title\", or ameesh work add)" % lot)
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
                    owner = _branch_owner(db, branch, exclude=int(item["id"]))
                    if owner is not None:
                        out["warnings"].append(_cited_branch_warning(
                            owner, branch, int(item["id"]), recipient))
                    else:
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
    from .agent_names import resolve as resolve_agent_name
    name = resolve_agent_name(name, db=db, operation="assignments._is_chat")
    from . import registry
    from .chat import CHAT_PREFIX, is_chat_agent
    return name.startswith(CHAT_PREFIX) and is_chat_agent(registry.get(db, name))
