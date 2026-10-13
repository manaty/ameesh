# SPDX-License-Identifier: AGPL-3.0-only
"""Le courrier qui confie du travail est lié aux lots (lot L118).

Constat du 2026-10-10 : l'orchestrateur confiait le travail par courrier
(`ameesh mail send`) sans enregistrer de lot. `ameesh projects` montrait
alors des agents « sans lot » qui travaillaient, les alertes de
sous-utilisation se trompaient et la frise restait vide.

* Un orchestrateur (même détection que `orchestrator_held`, L94 :
  déclaré, rôle au canon, ou agent qui a confié des lots) qui écrit à un
  agent SANS lot ouvert est averti (sortie d'erreur et fil du message).
* `--lot <id|réf>` : le lot est rattaché au destinataire, et lui est
  assigné s'il ne l'est pas déjà, par l'attribution gardée (L37). Un lot
  qui appartient à un AUTRE agent n'est jamais repris en silence : le
  courrier d'un relecteur sur le lot de l'auteur ne doit pas le lui voler.
  #859 : un lot DÉJÀ assigné à un agent ne se réattribue que sur un rôle
  déclaré (canon ou `AMEESH_ALERT_ORCHESTRATORS`, `is_orchestrator` avec
  `history=False`) ou d'un humain ; l'historique de distribution (L94) ne
  suffit pas — un agent qui a confié un lot ne peut pas, par un `--lot`,
  donner le sien ni celui d'un autre. Un lot SANS assigné reste
  distribuable par l'historique (le bureau distribue ainsi, canon §4).
* `--new-lot "titre"` : crée le lot, assigné au destinataire, en un geste.

Sans option, rien ne change, hors l'avertissement. Un `--lot` qui ne
désigne aucun lot connu reste une simple étiquette du fil, comme avant.
Correctif du 2026-10-11 : pour tout expéditeur, un `--lot` qui désigne un
lot ouvert (numéro ou étiquette unique) est enregistré par son numéro.
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


def _may_reassign(cfg, db, sender: str) -> bool:
    """#859 : qui peut réattribuer par courrier un lot DÉJÀ assigné à un agent.
    Un humain (préfixe `human:` ou nom connu de la configuration ou du canon),
    ou un agent dont le RÔLE est déclaré orchestrateur (fiche du canon,
    `AMEESH_ALERT_ORCHESTRATORS`). L'historique de distribution (L94) ne suffit
    pas : sinon tout agent ayant confié un lot reprendrait ceux des autres."""
    name = (sender or "").strip()
    if name.startswith(work_mod.HUMAN_PREFIX):
        return True
    try:
        if work_mod.known_human(db, name, cfg):
            return True
    except Exception:
        pass
    return is_orchestrator(cfg, db, name, history=False)


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


def link_message(cfg, db, *, sender: str, recipient: str, lot: str | None = None,
                 new_lot: str | None = None, branch: str | None = None,
                 estimate=None, app: str | None = None, new_project: bool = False) -> dict:
    """Ce que le message `sender` → `recipient` fait aux lots, AVANT son dépôt.

    Rend `{lot, work_item_id, assigned, created, branch, warnings, orchestrator}` :
    `work_item_id` est l'étiquette du fil (le numéro du lot s'il est connu,
    sinon `lot` tel quel). `branch` (déduite du message par l'appelant) est
    posée sur le lot qui n'en a pas. Lève AssignmentError si l'attribution
    gardée refuse (rien n'est alors déposé).

    L157 : `estimate` est la durée estimée du lot créé par `new_lot` ; elle
    est obligatoire quand l'expéditeur est un agent (refus, rien n'est
    déposé), un avertissement pour un humain."""
    out = {"lot": None, "work_item_id": lot, "assigned": False, "created": False,
           "branch": None, "warnings": [], "orchestrator": False}
    if recipient == "all" or ":" in recipient:
        return out
    if new_lot is None:
        # le cas courant (aucune option, destinataire déjà sur un lot) ne lit
        # rien d'autre : la détection d'un orchestrateur peut lire le canon
        has_open = bool(work_mod.open_lots(db, recipient))
        if lot is None and has_open:
            return out
        if not is_orchestrator(cfg, db, sender):
            # un `--lot` d'un autre agent n'assigne rien ; mais une étiquette
            # qui désigne un lot ouvert est rendue par son numéro, pour tout
            # expéditeur : le fil, l'activité du lot et le lot en cours
            # (`ameesh projects`) la lisent. Sinon, étiquette libre (inchangé).
            if lot is not None:
                try:
                    item = resolve_lot(db, lot)
                except AssignmentError:
                    item = None
                if item is not None:
                    out.update(lot=item, work_item_id=str(item["id"]))
            return out
        out["orchestrator"] = True
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
            item = work_mod.add(db, title=title, assignee=recipient, actor=sender,
                                source="mail", app=project, cfg=cfg, branch=branch,
                                estimate=minutes, note=introduced)
            out.update(created=True, assigned=True, branch=item.get("branch"))
            if warn:
                out["warnings"].append(estimates.missing(item["id"]))
        elif lot is not None:
            item = resolve_lot(db, lot)
            if item is None:
                out["warnings"].append(
                    "--lot %s matches no open lot: kept as a plain thread label "
                    "(to create the lot: --new-lot \"title\", or ameesh work add)" % lot)
            else:
                current = (item.get("assignee") or "").strip()
                if current != recipient:
                    # #859 : un lot déjà tenu par un AGENT ne se réattribue
                    # par un `--lot` que sur un rôle déclaré (ou d'un humain).
                    # L'historique de distribution (L94) ne donne que le droit
                    # de confier un lot encore libre ; il ne permet pas de
                    # donner le sien ni de reprendre celui d'un autre.
                    bloque = (bool(current)
                              and not current.startswith(work_mod.HUMAN_PREFIX)
                              and not _may_reassign(cfg, db, sender))
                    if bloque:
                        out["warnings"].append(
                            "lot #%d is already assigned to %s: not reassigned to %s "
                            "by mail (--lot hands an unassigned lot; to hand it "
                            "over: ameesh work assign %d %s)"
                            % (int(item["id"]), current, recipient, int(item["id"]),
                               recipient))
                    elif current and current != sender \
                            and not current.startswith(work_mod.HUMAN_PREFIX):
                        out["warnings"].append(
                            "lot #%d belongs to %s: not reassigned to %s (to "
                            "take it over: ameesh work assign %d %s)"
                            % (int(item["id"]), current, recipient, int(item["id"]),
                               recipient))
                    else:
                        done = work_mod.assign(db, int(item["id"]), recipient, actor=sender,
                                               cfg=cfg)
                        item = done["item"]
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
    if not has_open and not out["assigned"] and not _is_chat(db, recipient):
        out["warnings"].append(
            "%s has no open lot: this work will show up neither in ameesh projects "
            "nor in alerts — link it (--lot <id|ref>) or create it "
            "(--new-lot \"title\")" % recipient)
    return out


def _is_chat(db, name: str) -> bool:
    """L123 : le chat du propriétaire n'a jamais de lot ; lui écrire (réponse
    d'un orchestrateur) ne confie pas de travail."""
    from . import registry
    from .chat import CHAT_PREFIX, is_chat_agent
    return name.startswith(CHAT_PREFIX) and is_chat_agent(registry.get(db, name))
