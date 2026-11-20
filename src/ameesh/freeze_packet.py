# SPDX-License-Identifier: AGPL-3.0-only
"""#7892 — au gel, avertir l'auteur et le pilote d'un membre d'un porteur poussé.

Constat de l'auditeur (#228274 §6 b), vu deux fois le 19/11 : #7694 (paquet
h-20261119-11) et #7865 (h-20261119-12) ont été regelés alors que leur porteur
était déjà poussé. La CI du porteur avait tourné sur une source qui n'était
plus la tête du lot (« composed source … does not carry its frozen head »),
puis la recomposition a dû attendre la fin de la tentative du tic.

Un lot membre d'un paquet y est composé à une **source** (le second parent de
son commit de fusion, `work_packet_members.source_sha`) et le porteur a une
tête poussée (`packet_head.protected_head` : son gel, ou la tête de branche
vue en avance après ce gel). Un nouveau gel de l'unité sur une autre tête est
légitime — c'est souvent le correctif d'un rouge — mais, à la recomposition,
`keep_composition` (#4581) ne reprend un membre que sur sa source composée :
le lot sort du paquet et va à la coupure suivante.

Ce module ne refuse rien : il rend les porteurs concernés pour que le gel les
inscrive dans sa note (avertissement de l'auteur, imprimé par la CLI) et
prévienne le pilote du projet dans le même geste. Une lecture impossible ne
change jamais le gel ; l'incertitude ne fabrique pas d'avertissement.

« Déjà poussé » se prouve comme partout ailleurs par la tête protégée du
porteur, et par l'ascendance de la composition enregistrée dans cette tête
quand le dépôt la permet (le paquet a pu grandir sans être recomposé). Une
composition enregistrée après cette tête n'est pas poussée : aucun avis.
"""
from __future__ import annotations

import hashlib

from ameesh import work_lifecycle as lifecycle

#: marque machine de la note d'un gel : ligne écrite par le gate, jamais par
#: `--note` (le filtre `FREEZE_MACHINE_LINE` la retire d'une note d'appelant).
MARK = "ameesh-freeze-packet-pushed:"


def _text(value) -> str:
    return str(value or "").strip()


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def carries(repo: str, composed: str, pushed: str) -> bool:
    """La composition `composed` est-elle contenue dans la tête poussée ?

    L'égalité suffit (cas courant : la composition EST la tête poussée). Une
    lecture du dépôt permet l'ascendance quand le porteur a grandi depuis sa
    composition ; un dépôt ou un commit illisible ne conclut pas.
    """
    from . import packet_head, plan_git

    if not composed or not pushed:
        return False
    if packet_head.same_commit(composed, pushed):
        return True
    if not repo:
        return False
    try:
        left = plan_git.local_commit(repo, composed)
        right = plan_git.local_commit(repo, pushed)
        if not left or not right:
            return False
        if left == right:
            return True
        return plan_git._git(repo, "merge-base", "--is-ancestor",
                             left, right).returncode == 0
    except Exception:  # noqa: BLE001 - un doute ne fabrique pas d'avis
        return False


def _pushed_head(db, member: dict, repo: str) -> str:
    """La tête poussée du porteur qui porte la composition, ou "".

    Les preuves partagées de #4501 sont le gel du porteur et sa tête de
    branche vue en avance. On retient celle qui contient la composition : un
    gel plus récent qu'un retour en arrière de la branche reste une poussée
    prouvée. À défaut (une composition poussée que ni le gel ni le balayage
    du porteur n'ont encore vue), une ref de suivi distante qui la contient
    prouve la poussée : elle ne peut venir que du remote.
    """
    from . import packet_head, plan_git, work

    branch = (_text(member.get("package_branch"))
              or _text(member.get("carrier_branch")))
    carrier = work.get(db, _int(member.get("package_id"))) or {}
    frozen = _text(member.get("package_frozen_sha"))
    observed = _text(carrier.get("branch_head"))
    protected = _text(packet_head.protected_head(
        frozen, member.get("package_frozen_ts") or 0,
        observed, carrier.get("branch_head_ts") or 0))
    composed = _text(member.get("head_sha"))
    if not composed:
        return ""
    for candidate in (protected, frozen, observed):
        if candidate and carries(repo, composed, candidate):
            return candidate
    if not repo or not branch:
        return ""
    try:
        for remote in plan_git._git_remotes(repo):
            tip = plan_git.local_commit(
                repo, "refs/remotes/%s/%s" % (remote, branch))
            if tip and carries(repo, composed, tip):
                return tip
    except Exception:  # noqa: BLE001 - une ref illisible ne prouve rien
        return ""
    return ""


def pushed_carriers(db, lot: int, head: str, *, repo: str = "") -> list[dict]:
    """Les porteurs ouverts déjà poussés dont `lot` est l'unité, à avertir.

    Rend `[{package_id, branch, pushed, composed, source}]`, un par porteur.
    Sont écartés : un porteur terminé, une composition non poussée, une
    nouvelle tête qui est encore la source composée (le membre reste au
    paquet) et toute lecture impossible.
    """
    from . import packet_head, packet_link, work

    lot = _int(lot)
    head = _text(head)
    if not lot or not head:
        return []
    try:
        members = packet_link.for_item(db, lot) or ()
    except Exception:  # noqa: BLE001 - appartenance illisible : aucun avis
        return []
    found: list[dict] = []
    seen: set[int] = set()
    for member in members:
        try:
            package_id = _int((member or {}).get("package_id"))
            if not package_id or package_id in seen:
                continue
            if _text((member or {}).get("package_state")) in lifecycle.COMPLETED:
                continue
            composed = _text((member or {}).get("head_sha"))
            source = _text((member or {}).get("source_sha"))
            if not composed:
                continue
            # La nouvelle tête est encore celle de la composition : le membre
            # n'en sort pas, et l'effet annoncé serait faux.
            if source and packet_head.same_commit(source, head):
                continue
            pushed = _pushed_head(db, member, repo)
            if not carries(repo, composed, pushed):
                continue
            seen.add(package_id)
            branch = (_text((member or {}).get("package_branch"))
                      or _text((member or {}).get("carrier_branch")))
            found.append({"package_id": package_id, "branch": branch,
                          "pushed": pushed, "composed": composed,
                          "source": source})
        except Exception:  # noqa: BLE001 - un porteur illisible n'est pas un avis
            continue
    return found


def effect(lot: int, head: str, match: dict) -> str:
    """La phrase d'effet commune à la note et au courrier du pilote."""
    return ("lot #%d is composed in packet carrier #%d (%s) whose pushed head is "
            "%s; freezing %s takes it out of the carrier at recomposition — next "
            "cut (#4581)"
            % (int(lot), int(match["package_id"]),
               match.get("branch") or "?", _text(match.get("pushed"))[:12],
               _text(head)[:12]))


def note_lines(lot: int, head: str, matches) -> list[str]:
    """Les lignes machine à inscrire dans la note du jalon gelé."""
    return ["%s %s" % (MARK, effect(lot, head, match))
            for match in matches or ()]


def pilot_names(cfg, db, project: str) -> list[str]:
    """Les pilotes du projet, par la règle partagée des briefs (#2935)."""
    from . import alert_briefs, canon as canon_mod, storage

    try:
        listing = storage.of(db).operations.listing()
        canons = (canon_mod.load_configured(cfg)
                  if cfg is not None and getattr(cfg, "canon", None) else [])
        return list(alert_briefs.project_pilots(
            _text(project), listing, cfg=cfg, db=db, canons=canons) or ())
    except Exception:  # noqa: BLE001 - rôle illisible : pas de destinataire
        return []


def pilot_body(lot: int, head: str, matches, *, actor: str = "") -> str:
    by = " par %s" % _text(actor) if _text(actor) else ""
    return "\n".join([
        "Gel d'une unité d'un porteur déjà poussé (canon §11 bis, #7892).",
        "",
        "Lot: #%d — tête gelée: %s%s" % (int(lot), _text(head)[:12], by),
        *["- %s" % effect(lot, head, match) for match in matches or ()],
        "",
        "Le gel reste valide (souvent le correctif d'un rouge). À la "
        "recomposition, le lot sort du paquet et va à la coupure suivante : "
        "ne pas attendre la fin de la tentative de CI en cours sur ce porteur.",
        "Aucun geste n'est demandé de plus au pilote.",
    ])


def notify_pilots(db, item: dict, head: str, matches, *, actor: str = "") -> list[str]:
    """Prévient les pilotes du projet dans le geste de gel (au mieux).

    Un courrier par pilote, dédoublonné par (lot, porteur, tête) : un regel de
    la même tête ne redépose pas le même avis. Rend les destinataires joints ;
    un échec d'envoi ne défait jamais le gel.
    """
    from . import mail, work

    lot = _int((item or {}).get("id"))
    head = _text(head)
    if not lot or not head or not matches:
        return []
    cfg = getattr(db, "cfg", None)
    project = _text((item or {}).get("app"))
    sent: list[str] = []
    body = pilot_body(lot, head, matches, actor=actor)
    for recipient in pilot_names(cfg, db, project):
        recipient = _text(recipient)
        if not recipient or recipient == _text(actor) \
                or recipient.startswith(work.HUMAN_PREFIX) \
                or recipient == work.SYSTEM_SENDER:
            continue
        # La tête gelée fait partie de la clé (relecture cx2, #7892) : deux
        # gels H1 puis H2 du même membre, porteur poussé inchangé, sont deux
        # avis distincts — le corps les nomme, la clé doit les distinguer. Le
        # reste de la situation est condensé pour rester sous la limite de
        # 200 caractères de la clé. Un rejeu de la même tête retrouve le même
        # corps et ne redépose rien.
        situation = "\n".join([
            "actor:%s" % _text(actor),
            *["%d:%s" % (int(m["package_id"]), _text(m.get("pushed")))
              for m in sorted(matches, key=lambda m: int(m["package_id"]))],
        ])
        key = "freeze-packet-pushed:%d:%s:%s" % (
            lot, head,
            hashlib.sha256(situation.encode("utf-8")).hexdigest()[:24])
        try:
            mail.send(db, work.SYSTEM_SENDER, recipient, body, kind="event",
                      work_item_id=str(lot),
                      payload={"freeze_packet_pushed": {
                          "lot": lot, "head": head,
                          "carriers": [
                              {"package_id": int(m["package_id"]),
                               "branch": m.get("branch") or "",
                               "pushed": m.get("pushed") or "",
                               "composed": m.get("composed") or "",
                               "source": m.get("source") or ""}
                              for m in matches]}},
                      idempotency_key=key)
            sent.append(recipient)
        except Exception:  # noqa: BLE001 - le gel est écrit ; l'avis se répare
            continue
    return sent
