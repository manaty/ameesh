"""Alertes adressées aux agents qui peuvent agir (L132, décision 0036).

`ameesh notify` pousse ses alertes à l'humain responsable. Trois d'entre elles
appellent un geste qu'un agent sait faire : `idle_capacity` (L118),
`stale_lot` et `plan_underused`. Elles partent aussi, par courrier `event`,

* aux orchestrateurs (ceux de l'alerte, de l'équipe concernée quand elle est
  connue), qui répartissent le travail ;
* à l'auditeur interne ([AUDITEUR.md](../../docs/AUDITEUR.md)), qui les suit.

Chaque courrier dit l'action attendue, avec la commande exacte. Une alerte
part une fois à sa levée, puis une seule relance si elle dure encore
`REMIND_AFTER_S` (6 h) plus tard. Ce module rend les textes ; l'envoi et le
dédoublonnage sont dans `notify.Notifier`.
"""

from __future__ import annotations

from . import sous_utilisation

#: types adressés aux agents (en plus de l'humain, s'ils sont dans `notify.types`)
BRIEFED_TYPES = ("idle_capacity", "stale_lot", "plan_underused", "estimate_bias")
#: une relance, si l'alerte dure encore 6 h après le premier courrier
REMIND_AFTER_S = 6 * 3600.0
#: rôles d'une fiche Agent du canon qui désignent l'auditeur
AUDITOR_ROLES = ("auditeur", "auditor")
#: nom de la persona de l'auditeur (docs/AUDITEUR.md, « Mise en service »)
AUDITOR_NAME = "auditeur"


def auditors(listing: list, canons=None) -> list:
    """Les auditeurs du registre : l'agent `auditeur`, et ceux dont la fiche
    Agent du canon porte `roles: [auditeur]` (ou `auditor`, `role:`)."""
    names = {row["name"] for row in listing}
    found = sous_utilisation.role_orchestrators(canons or [], AUDITOR_ROLES)
    found.add(AUDITOR_NAME)
    return sorted(n for n in found if n in names)


def _team(by_name: dict, name) -> str:
    return ((by_name.get(name) or {}).get("team") or "").strip()


def _of_teams(orchestras, by_name: dict, teams: set) -> list:
    """Les orchestrateurs d'une des équipes (ou sans équipe) ; sans équipe
    connue, tous."""
    if not teams:
        return list(orchestras)
    return [o for o in orchestras if not _team(by_name, o) or _team(by_name, o) in teams]


def _stale_lot(alert: dict) -> str:
    lot = int(alert["lot"])
    who = alert.get("agent")
    title = alert.get("title")
    head = "Lot stagnant : #%d%s (%s) sans activité depuis %s, %s%s." % (
        lot, " « %s »" % title if title else "", alert.get("lot_state") or "?",
        sous_utilisation._duration(alert.get("value") or 0),
        "assigné à %s" % who if who else "sans assigné",
        ", attend : %s" % alert["waiting_for"] if alert.get("waiting_for") else "")
    if who:
        move = ("relance %s : ameesh mail send %s \"…\" --lot %d, ou réattribue-le : "
                "ameesh work assign %d <agent>" % (who, who, lot, lot))
    else:
        move = "attribue-le : ameesh work assign %d <agent>" % lot
    return ("%s Action attendue : s'il est fusionné hors PR, ferme-le : ameesh work merged %d "
            "--sha <commit de fusion> ; sinon %s ; s'il n'a plus lieu d'être : ameesh work "
            "close %d --abandoned (ou --superseded-by <id>)." % (head, lot, move, lot))


def _plan_underused(alert: dict) -> str:
    idle = list(alert.get("idle_agents") or ())
    if idle:
        move = ("confie du travail aux agents au repos (%s), lié à un lot : ameesh mail send "
                "<agent> \"…\" --lot <id> (ou --new-lot \"titre\"), ou ameesh work assign "
                "<lot> <agent>" % ", ".join(idle))
    else:
        move = ("aucun agent %s n'est au repos : avance le travail sur ce harnais (ameesh "
                "work assign <lot> <agent>) ou réveille un agent arrêté (ameesh resume "
                "<agent>)" % (alert.get("harness") or "de ce compte"))
    return ("Forfait sous-employé : %s. Action attendue : %s ; lots ouverts sans agent : "
            "ameesh projects." % (alert.get("detail") or "?", move))


def _estimate_bias(alert: dict) -> str:
    """L157b : à l'estimateur et aux orchestrateurs."""
    ratio = alert.get("value")
    sens = ("sous-estime" if ratio is not None and float(ratio) > 1 else "surestime")
    return ("Biais d'estimation : %s. %s %s les lots. Action attendue : partir de "
            "l'estimation de référence (affichée par ameesh work add et ameesh work show, "
            "table publiée par l'auditeur) et ne s'en écarter qu'en le justifiant "
            "(--estimate-source), estimation décomposée en phases (agent, CI, attente de "
            "relecture, humain)." % (alert.get("detail") or "?", alert.get("agent") or "?",
                                    sens))


def _for_auditor(alert: dict) -> str:
    """`idle_capacity` à l'auditeur : le constat et l'action de l'alerte."""
    return "Capacité au repos : %s." % (alert.get("detail") or "?")


def briefs(alert: dict, listing: list, lot_titles: dict | None = None,
           auditor_names=()) -> dict:
    """`{agent: texte}` pour une alerte de `BRIEFED_TYPES` (vide sinon).

    Orchestrateurs : `idle_capacity`, ceux de L118 ; `stale_lot`, ceux de
    l'équipe de l'assigné ; `plan_underused`, ceux des équipes des agents au
    repos ; `estimate_bias` (L157b), ceux de l'équipe de l'estimateur, et
    l'estimateur lui-même s'il est un agent du registre. Auditeurs : toujours. Un agent à la fois orchestrateur et auditeur
    reçoit le texte d'orchestrateur."""
    kind = alert.get("type")
    if kind not in BRIEFED_TYPES:
        return {}
    by_name = {row["name"]: row for row in listing}
    orchestras = list(alert.get("orchestrators") or ())
    if kind == "idle_capacity":
        out = sous_utilisation.orchestrator_briefs(alert, listing, lot_titles)
        audit_text = _for_auditor(alert)
    else:
        if kind == "stale_lot":
            text = _stale_lot(alert)
            teams = {_team(by_name, alert.get("agent"))} - {""}
        elif kind == "estimate_bias":
            text = _estimate_bias(alert)
            teams = {_team(by_name, alert.get("agent"))} - {""}
        else:
            text = _plan_underused(alert)
            teams = {_team(by_name, n) for n in alert.get("idle_agents") or ()} - {""}
        out = {o: text for o in _of_teams(orchestras, by_name, teams)}
        if kind == "estimate_bias" and alert.get("agent") in by_name:
            out.setdefault(alert["agent"], text)       # l'estimateur lui-même
        audit_text = text
    for name in auditor_names or ():
        out.setdefault(name, audit_text)
    return out


def reminder(text: str, raised_ts: float, now: float) -> str:
    """Le texte de la relance : le même, précédé de l'âge de l'alerte."""
    return "Relance, alerte toujours en cours depuis %s : %s" % (
        sous_utilisation._duration(max(0.0, now - raised_ts)), text)
