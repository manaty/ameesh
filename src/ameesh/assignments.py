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
* `--new-lot "titre"` : crée le lot, assigné au destinataire, en un geste.

Sans option, rien ne change, hors l'avertissement. Un `--lot` qui ne
désigne aucun lot connu reste une simple étiquette du fil, comme avant.
"""
from __future__ import annotations

import os

from . import storage
from . import work as work_mod

#: variable qui déclare des orchestrateurs (comme `ameesh alerts --orchestrators`)
ORCHESTRATORS_ENV = "AMEESH_ALERT_ORCHESTRATORS"


class AssignmentError(RuntimeError):
    """Rattachement refusé : rien n'est déposé, le message dit quoi faire."""


def is_orchestrator(cfg, db, name: str) -> bool:
    """`name` est-il un orchestrateur ? Déclaré (`AMEESH_ALERT_ORCHESTRATORS`),
    agent qui a confié des lots dans les 30 derniers jours, ou fiche Agent du
    canon dont `roles` nomme un orchestrateur (`sous_utilisation.orchestrators`,
    lu en dernier : le canon coûte une lecture du dépôt)."""
    from . import sous_utilisation as su

    declared = {su._bare(n) for n in (os.environ.get(ORCHESTRATORS_ENV) or "").split(",")
                if n.strip()}
    if name in declared:
        return True
    try:
        if name in {su._bare(n) for n in storage.of(db).operations.assigners(
                since_s=su.ASSIGNERS_SINCE_S)}:
            return True
    except Exception:
        pass
    try:
        return name in su.orchestrators(cfg, db, [{"name": name}], declared=())
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
            raise AssignmentError("lot #%s déjà %s : rien à rattacher (nouveau lot : "
                                  "--new-lot \"titre\")" % (number, item["state"]))
        return item
    rows = storage.of(db).work.open_by_ref(text)
    if len(rows) > 1:
        raise AssignmentError("référence %r ambiguë : lots %s — précisez le numéro"
                              % (text, ", ".join("#%d" % int(r["id"]) for r in rows)))
    return rows[0] if rows else None


def link_message(cfg, db, *, sender: str, recipient: str, lot: str | None = None,
                 new_lot: str | None = None, branch: str | None = None) -> dict:
    """Ce que le message `sender` → `recipient` fait aux lots, AVANT son dépôt.

    Rend `{lot, work_item_id, assigned, created, branch, warnings, orchestrator}` :
    `work_item_id` est l'étiquette du fil (le numéro du lot s'il est connu,
    sinon `lot` tel quel). `branch` (déduite du message par l'appelant) est
    posée sur le lot qui n'en a pas. Lève AssignmentError si l'attribution
    gardée refuse (rien n'est alors déposé)."""
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
            # un `--lot` d'un autre agent reste une étiquette du fil (inchangé)
            return out
        out["orchestrator"] = True
    else:
        has_open = True
        out["orchestrator"] = is_orchestrator(cfg, db, sender)
    item = None
    try:
        if new_lot is not None:
            title = new_lot.strip()
            if not title:
                raise AssignmentError("--new-lot : titre vide")
            item = work_mod.add(db, title=title, assignee=recipient, actor=sender,
                                source="mail", cfg=cfg, branch=branch)
            out.update(created=True, assigned=True, branch=item.get("branch"))
        elif lot is not None:
            item = resolve_lot(db, lot)
            if item is None:
                out["warnings"].append(
                    "--lot %s ne désigne aucun lot ouvert : simple étiquette du fil "
                    "(lot à créer : --new-lot \"titre\", ou ameesh work add)" % lot)
            else:
                current = (item.get("assignee") or "").strip()
                if current != recipient:
                    if current and current != sender \
                            and not current.startswith(work_mod.HUMAN_PREFIX):
                        out["warnings"].append(
                            "lot #%d appartient à %s : non réassigné à %s (pour le "
                            "reprendre : ameesh work assign %d %s)"
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
                                               source="citée par le courrier")
                    out["branch"] = item.get("branch")
    except work_mod.WorkError as exc:
        raise AssignmentError(str(exc)) from exc
    if item is not None:
        out.update(lot=item, work_item_id=str(item["id"]))
    if not has_open and not out["assigned"]:
        out["warnings"].append(
            "%s n'a aucun lot ouvert : ce travail n'apparaîtra ni dans ameesh projects "
            "ni dans les alertes — rattachez-le (--lot <id|réf>) ou créez-le "
            "(--new-lot \"titre\")" % recipient)
    return out
