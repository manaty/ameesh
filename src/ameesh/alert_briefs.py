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
BRIEFED_TYPES = ("idle_capacity", "stale_lot", "plan_underused", "estimate_bias",
                 "waiting_without_question", "module_deviation",
                 "critical_budget_pause", "idle_actionable_lot")
#: une relance, si l'alerte dure encore 6 h après le premier courrier
REMIND_AFTER_S = 6 * 3600.0
#: rôles d'une fiche Agent du canon qui désignent l'auditeur
AUDITOR_ROLES = ("auditeur", "auditor")
#: nom de la persona de l'auditeur (docs/AUDITEUR.md, « Mise en service »)
AUDITOR_NAME = "auditeur"


def auditors(listing: list, canons=None, *, explicit_only=False) -> list:
    """Les auditeurs du registre : l'agent `auditeur`, et ceux dont la fiche
    Agent du canon porte `roles: [auditeur]` (ou `auditor`, `role:`).
    `explicit_only` exige ce rôle, sans autorisation implicite par le nom."""
    names = {row["name"] for row in listing}
    found = sous_utilisation.role_orchestrators(canons or [], AUDITOR_ROLES)
    if not explicit_only:
        found.add(AUDITOR_NAME)
    return sorted(n for n in found if n in names)


def _team(by_name: dict, name) -> str:
    return ((by_name.get(name) or {}).get("team") or "").strip()


def _workstream(row: dict) -> str:
    """`workstream` d'abord ; `chantier` reste lu tant que les lecteurs chauds
    ne l'ont pas remplacé (L148, stage 3 C1)."""
    return row.get("workstream") or row.get("chantier") or ""


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
    head = "Stale lot: #%d%s (%s) no activity for %s, %s%s." % (
        lot, " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
        sous_utilisation._duration(alert.get("value") or 0),
        "assigned to %s" % who if who else "unassigned",
        ", waiting for: %s" % alert["waiting_for"] if alert.get("waiting_for") else "")
    if who:
        move = ("nudge %s: ameesh mail send %s \"…\" --lot %d, or reassign it: "
                "ameesh work assign %d <agent>" % (who, who, lot, lot))
    else:
        move = "assign it: ameesh work assign %d <agent>" % lot
    return ("%s Expected action: if it was merged outside a PR, close it: ameesh work merged %d "
            "--sha <merge commit>; otherwise %s; if it is no longer needed: ameesh work "
            "close %d --abandoned (or --superseded-by <id>)." % (head, lot, move, lot))


def _plan_underused(alert: dict) -> str:
    idle = list(alert.get("idle_agents") or ())
    if idle:
        move = ("give work to the idle agents (%s), tied to a lot: ameesh mail send "
                "<agent> \"…\" --lot <id> (or --new-lot \"title\"), or ameesh work assign "
                "<lot> <agent>" % ", ".join(idle))
    else:
        move = ("no %s agent is idle: move work onto this harness (ameesh "
                "work assign <lot> <agent>) or wake a stopped agent (ameesh resume "
                "<agent>)" % (alert.get("harness") or "of this account"))
    return ("Underused plan: %s. Expected action: %s; open lots without an agent: "
            "ameesh projects." % (alert.get("detail") or "?", move))


def _estimate_bias(alert: dict) -> str:
    """L157b : à l'estimateur et aux orchestrateurs."""
    ratio = alert.get("value")
    sens = ("underestimates" if ratio is not None and float(ratio) > 1 else "overestimates")
    return ("Estimate bias: %s. %s %s lots. Expected action: start from "
            "the reference estimate (shown by ameesh work add and ameesh work show, "
            "table published by the auditor) and deviate from it only with a justification "
            "(--estimate-source), estimate broken down into phases (agent, CI, waiting for "
            "review, human)." % (alert.get("detail") or "?", alert.get("agent") or "?",
                                 sens))


def _waiting_without_question(alert: dict) -> str:
    """L233: to the assignee and the orchestrators of its team."""
    lot = int(alert["lot"])
    title = alert.get("title")
    return ("Lot #%d%s is in waiting_human but no decision request is open: the owner sees "
            "a lot waiting for them and has no question to answer. Expected action: ask the "
            "owner through the queue — ameesh decide ask --lot %d --question \"…\" "
            "--option a=\"…\" [--recommend a --why \"…\"] — or, if nothing is expected from "
            "him, move the lot back: ameesh work move %d <state>. A decision written in a "
            "mail (to bureau or anyone) never counts." % (
                lot, " « %s »" % title if title else "", lot, lot))


def _idle_actionable_lot(alert: dict) -> str:
    """#408 (règle 3) : à l'auditeur seul, qui relance (niveau 1)."""
    lot = int(alert["lot"])
    who = alert.get("agent") or "?"
    title = alert.get("title")
    if alert.get("runner_live"):
        nudge = ("agent-mail send %s \"…\" --lot %d (what is expected on the lot, or "
                 "ask it to record why it cannot advance: ameesh work note %d \"hold: "
                 "<reason>\")" % (who, lot, lot))
    else:
        nudge = "ameesh resume %s (its runner is not live), then agent-mail send %s " \
                "\"…\" --lot %d" % (who, who, lot)
    return ("Idle with an actionable lot: %s holds #%d%s (%s), and %d wake(s) of its runner "
            "left the lot unchanged. Expected action, level 1 (reversible), not just naming "
            "it: nudge it now: %s; if it cannot take the lot, reassign it: ameesh work "
            "assign %d <agent>. Never twice within an hour for the same agent." % (
                who, lot, " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
                int(alert.get("wakes") or 0), nudge, lot))


def _critical_budget_pause(alert: dict) -> str:
    """L242b (5) : aux orchestrateurs de l'équipe et aux auditeurs (urgent)."""
    return ("Critical agent paused on budget: %s. Expected action: check the accounts "
            "(ameesh accounts list) and the mesh caps (ameesh budget); free an account or "
            "raise a cap with the owner, or hand its urgent work to another agent."
            % (alert.get("detail") or "?"))


def _for_auditor(alert: dict) -> str:
    """`idle_capacity` à l'auditeur : le constat et l'action de l'alerte ; avec
    des lots ouverts sans agent, l'écart est le sien (L165b, niveau 1)."""
    text = "Idle capacity: %s." % (alert.get("detail") or "?")
    if alert.get("lots"):
        text += (" Lots are waiting with idle agents: if this still holds at your next "
                 "pass, 30 min or more after this mail, fix it yourself (level 1, reversible): ameesh "
                 "work assign <lot> <agent> (an idle agent of the lot's team, highest "
                 "priority first), then agent-mail send <agent> \"…\" --lot <lot> to "
                 "wake it; name it in your report.")
    return text


def briefs(alert: dict, listing: list, lot_titles: dict | None = None,
           auditor_names=(), *, orchestrator_names=()) -> dict:
    """`{agent: texte}` pour une alerte de `BRIEFED_TYPES` (vide sinon).

    Orchestrateurs : `idle_capacity`, ceux de L118 ; `stale_lot`, ceux de
    l'équipe de l'assigné ; `plan_underused`, ceux des équipes des agents au
    repos ; `estimate_bias` (L157b), ceux de l'équipe de l'estimateur, et
    l'estimateur lui-même s'il est un agent du registre. `module_deviation` :
    rôle explicite et projet, sans repli. Auditeurs : toujours pour les autres
    types. Un agent à la fois orchestrateur et auditeur reçoit un seul texte."""
    kind = alert.get("type")
    if kind not in BRIEFED_TYPES:
        return {}
    by_name = {row["name"]: row for row in listing}
    orchestras = list(alert.get("orchestrators") or ())
    if kind == "module_deviation":
        text = alert.get("action") or alert.get("detail") or "Module procedure deviation"
        project = alert.get("project")
        if not project:
            return {}
        out = {o: text for o in orchestras
               if o in orchestrator_names and o in by_name
               and _workstream(by_name[o]) == project}
        for name in auditor_names or ():
            if name in by_name and _workstream(by_name[name]) in ("", project):
                out.setdefault(name, text)
        return out
    elif kind == "idle_actionable_lot":
        out = {}
        audit_text = _idle_actionable_lot(alert)
    elif kind == "idle_capacity":
        out = sous_utilisation.orchestrator_briefs(alert, listing, lot_titles)
        audit_text = _for_auditor(alert)
    else:
        if kind == "stale_lot":
            text = _stale_lot(alert)
            teams = {_team(by_name, alert.get("agent"))} - {""}
        elif kind == "estimate_bias":
            text = _estimate_bias(alert)
            teams = {_team(by_name, alert.get("agent"))} - {""}
        elif kind == "waiting_without_question":
            text = _waiting_without_question(alert)
            teams = {_team(by_name, alert.get("agent"))} - {""}
        elif kind == "critical_budget_pause":
            text = _critical_budget_pause(alert)
            teams = {_team(by_name, alert.get("agent"))} - {""}
        else:
            text = _plan_underused(alert)
            teams = {_team(by_name, n) for n in alert.get("idle_agents") or ()} - {""}
        out = {o: text for o in _of_teams(orchestras, by_name, teams)}
        if kind in ("estimate_bias", "waiting_without_question") \
                and alert.get("agent") in by_name:
            out.setdefault(alert["agent"], text)       # l'estimateur, l'assigné lui-même
        audit_text = text
    for name in auditor_names or ():
        out.setdefault(name, audit_text)
    if kind == "critical_budget_pause":
        out.pop(alert.get("agent"), None)       # en pause : il ne le lirait pas
    return out


def reminder(text: str, raised_ts: float, now: float) -> str:
    """Le texte de la relance : le même, précédé de l'âge de l'alerte."""
    return "Reminder, alert still active for %s: %s" % (
        sous_utilisation._duration(max(0.0, now - raised_ts)), text)
