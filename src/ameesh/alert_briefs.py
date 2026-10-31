"""Alertes adressées aux agents qui peuvent agir (L132, décision 0036).

`ameesh notify` pousse ses alertes à l'humain responsable. Trois d'entre elles
appellent un geste qu'un agent sait faire : `idle_capacity` (L118),
`stale_lot` et `plan_underused`. Elles partent aussi, par courrier `event`,

* aux orchestrateurs (ceux de l'alerte, de l'équipe concernée quand elle est
  connue), qui répartissent le travail ;
* à l'auditeur interne ([AUDITEUR.md](../../docs/AUDITEUR.md)), qui les suit
  — sauf `stale_lot` (L713) et `plan_underused` (#3274) : ces courriers ne le
  réveillent plus, il lit les lots stagnants et les forfaits sous-utilisés à
  sa passe (`ameesh alerts --json`) ; si plus aucun orchestrateur n'est
  vivant, le forfait sous-utilisé lui revient en relais (#3036).

#705 : pour un lot en `qa`, `stale_lot` va au relecteur DÉSIGNÉ (le verdict
lui est dû) ; sans relecteur désigné, l'alerte part aux orchestrateurs du
projet du lot et demande d'en désigner un (`ameesh work reviewer <id>
<agent>` — le lot est déjà en qa). La désignation à l'entrée en qa se fait,
elle, par `ameesh work move <id> qa --reviewer <agent>`.

#3007 : `notify_undeliverable` ne demande pas un geste sur un lot — elle dit
qu'aucun notificateur VIVANT ne couvre l'humain qui a des décisions en
attente. Elle part aux orchestrateurs de l'alerte et à l'auditeur, avec le
repli (canal de secours ou retour du notificateur). Son brief n'est **pas**
envoyé par le notificateur (ce serait le disparu qui parle) : la passe du
runner hors du poste le porte (`notify_gap.brief_once`), et
`NOTIFIER_EXCLUDED` l'en écarte côté `ameesh notify`.

Chaque courrier dit l'action attendue, avec la commande exacte. Une alerte
part une fois à sa levée, puis une seule relance si elle dure encore
`REMIND_AFTER_S` (6 h) plus tard ; les SLO ne repartent qu'au palier suivant
ou après changement d'état (#2602). Pour une alerte de jauge
(`plan_underused`), c'est une fois par **fenêtre** : son début est arrondi à
la minute et `reason` n'entre pas dans la clé, pour qu'une perte projetée qui
oscille autour du seuil ne refasse pas briefer à chaque retour (#3236). Ce
module rend les textes ; l'envoi et le dédoublonnage sont dans
`notify.Notifier`.
"""

from __future__ import annotations

import time

from . import sous_utilisation, vocab, work_lifecycle as lifecycle

#: types adressés aux agents (en plus de l'humain, s'ils sont dans `notify.types`)
BRIEFED_TYPES = ("staging_interrupted", "host_idle_reserved", "baseline_oversized", "host_release_failed",
                 "host_release_overdue", "host_release_no_runs",
                 "host_release_canon_unreadable", "idle_capacity", "stale_lot", "plan_underused", "estimate_bias",
                 "waiting_without_question", "module_deviation",
                 "critical_budget_pause", "idle_actionable_lot", "slo_breach",
                 # #1466 : une pause qui dure est un plafond périmé, un compte
                 # retiré ou une configuration que l'exécuteur ne lit pas ; les
                 # orchestrateurs peuvent vérifier, l'auditeur suit
                 "budget_pause_long",
                 # #3969 : un compte invalide sur l'hôte — l'équipe le répare
                 "account_config_invalid",
                 # #3961 : une pause opérateur restée posée (maintenance
                 # oubliée) ; les orchestrateurs la lèvent ou la confirment
                 "operator_pause_long", "operator_pause_host",
                 # #3036 : le courrier en souffrance part aux orchestrateurs
                 # VIVANTS ; le brief est ainsi suivi et re-tenté comme les
                 # autres, au lieu d'une remise unique à un hôte perdu.
                 "mail_undeliverable",
                 # #3007 : aucun notificateur vivant ne couvre l'humain qui a
                 # des décisions en attente — les agents doivent le savoir
                 "notify_undeliverable",
                 # #3733 : un agent bridé une bonne part de l'heure — le
                 # pilote confie ses relectures dues à une autre famille
                 "rate_limited_chronic")
#: #3007 : types qu'un notificateur ne briefe jamais lui-même — leur brief
#: dépend de sa propre liveness, il est porté par `notify_gap.brief_once`
#: (ménage du runner) pour survivre à la chute du poste.
NOTIFIER_EXCLUDED = ("notify_undeliverable",)
#: une relance, si l'alerte dure encore 6 h après le premier courrier
REMIND_AFTER_S = 6 * 3600.0
#: #3057 : un bail rendu depuis moins de ce délai (redémarrage d'exécuteur,
#: quelques secondes) n'est pas encore une perte : le brief attend, sans
#: escalade ni copie au fil ; trois passes du notificateur (30 s)
LEASE_GRACE_S = 90.0
#: rôles d'une fiche Agent du canon qui désignent l'auditeur
AUDITOR_ROLES = ("auditeur", "auditor")
#: nom de la persona de l'auditeur (docs/AUDITEUR.md, « Mise en service »)
AUDITOR_NAME = "auditeur"


def claims_closed_hosts(db) -> set:
    """#3036 : les hôtes dont les claims sont fermés.

    Un agent sur un hôte `draining`/`lost` (mise au repos, hôte perdu) ou sous
    une clôture de release (`host_release_fences`) ne peut pas prendre de
    nouveau bail, donc ne lira pas le courrier qu'on lui dépose. Les deux
    sources sont lues ici : le contrôle de cycle de vie (mode) et la porte
    calculée par `ameesh_host_gate_state` sur les relevés (`draining` couvre
    aussi une clôture de release). Une lecture en échec ne ferme rien : on ne
    devine pas qu'un hôte est mort."""
    from . import storage
    st = storage.of(db)
    names: set = set()
    try:
        names |= {str(row["host"]) for row in st.host_lifecycle.listing()
                  if row.get("mode") in ("draining", "lost")}
    except Exception:  # noqa: BLE001 - un contrôle illisible ne ferme pas un hôte
        pass
    try:
        names |= {str(row["host"]) for row in st.hosts.current(None)
                  if row.get("gate_state") == "draining"}
    except Exception:  # noqa: BLE001 - idem
        pass
    return names


def _unavailable(row: dict, closed: set) -> str:
    """#3036 : pourquoi un agent ne lira pas son courrier (`""` s'il le peut).

    Trois preuves : son hôte a les claims fermés ; il est `stopped`/`dead` (il
    ne prendra plus de tour) ; ou son bail n'est plus vivant (le runner qui le
    lisait est mort ou l'a rendu). L'état `queued` n'est pas une preuve en
    soi, seul le bail compte : un `queued` qui tient son bail lira, un
    `queued` sans bail vivant (hôte plein, `max_agents`, porte fermée) est
    écarté comme tout agent sans bail — rien ne dit quand il prendra un tour.
    Une ligne sans information de bail (appel hors base, test) n'est pas
    déclarée morte : on ne remplace pas une preuve absente par une
    supposition."""
    host = str(row.get("host") or "")
    if host and host in closed:
        return "host %s: claims closed" % host
    status = str(row.get("status") or "")
    if status in ("stopped", "dead"):
        return "status %s" % status
    if "lease_live" in row and not row.get("lease_live"):
        return "no live lease"
    return ""


def lease_grace(listing: list, closed: set, since: dict, now: float) -> tuple[list, set]:
    """#3057 : la grâce d'un bail tout juste rendu.

    Un exécuteur qui redémarre rend ses baux quelques secondes ; sans grâce,
    les briefs de cette fenêtre partent au palier suivant ou au fil du bureau,
    puis au destinataire à son retour (deux remises pour une absence de
    secondes). `since` (état du notificateur, nom → première passe vue sans
    bail) date l'absence. Rend le registre où un agent sans bail depuis moins
    de `LEASE_GRACE_S` compte vivant pour le routage, et l'ensemble de ces
    agents `held` : l'appelant ne leur remet rien encore. Seule l'absence de
    bail a une grâce : un hôte aux claims fermés ou un agent `stopped` est une
    perte prouvée. Une perte réelle n'est retardée que de la grâce."""
    out: list = []
    held: set = set()
    for row in listing:
        name = row["name"]
        if "lease_live" not in row or row.get("lease_live") \
                or _unavailable(dict(row, lease_live=True), closed):
            since.pop(name, None)
            out.append(row)
            continue
        first = float(since.setdefault(name, now))
        if now - first < LEASE_GRACE_S:
            held.add(name)
            out.append(dict(row, lease_live=True))
        else:
            out.append(row)
    names = {row["name"] for row in listing}
    for name in [n for n in since if n not in names]:
        del since[name]
    return out, held


def _dead_note(dead: dict) -> str:
    """#3036 : « aucun destinataire vivant : X, Y » en tête du brief.

    Le texte est le même pour tous les destinataires vivants du palier ; il
    leur dit qui manque, pour qu'ils couvrent le geste au lieu de croire que
    l'alerte est partie à tout le monde."""
    if not dead:
        return ""
    return "No live recipient: %s. " % ", ".join(
        "%s (%s)" % (name, why) for name, why in sorted(dead.items()))


def _escalation_note(dead: dict, from_level: int, to_level: int) -> str:
    """#3036 : le palier visé n'avait personne qui lise ; on le dit et on
    adresse le palier suivant."""
    return ("No live recipient: %s. Escalated from level %d to level %d. "
            % (", ".join("%s (%s)" % (name, why) for name, why in sorted(dead.items()))
               or "none designated", int(from_level), int(to_level)))


def _split_live(out: dict, by_name: dict, closed: set,
                covered=()) -> tuple[dict, dict]:
    """#3036 : sépare les briefs des destinataires vivants de ceux qui ne
    liront pas. Un nom hors registre (les pseudo-destinataires `thread:…`)
    n'est pas filtré : il n'a pas de bail à contrôler. #3100 : un nom de
    `covered` (déjà remis) non plus : son courrier l'attend."""
    live: dict = {}
    dead: dict = {}
    for name, text in out.items():
        if name.startswith("thread:") or name in covered:
            live[name] = text
            continue
        row = by_name.get(name)
        if row is None:
            live[name] = text
            continue
        why = _unavailable(row, closed)
        if why:
            dead[name] = why
        else:
            live[name] = text
    return live, dead


def _bureau_externe(by_name: dict) -> bool:
    """Le bureau `externe` (session humaine, 0030) ne lit pas sa boîte : sa
    copie va au fil lisible du lot, jamais un courrier de plus."""
    return (by_name.get("bureau") or {}).get("mode") == "externe"


def _fallback_bureau(by_name: dict, text: str) -> dict:
    """#3036 : quand plus aucun agent ne peut lire, la route humaine est le
    bureau : sa boîte s'il est un agent normal, sinon le fil lisible du lot
    (la voie déjà employée pour le bureau externe). Sans bureau au registre,
    rien n'est inventé : l'appelant réessaiera au passage suivant."""
    if "bureau" not in by_name:
        return {}
    if _bureau_externe(by_name):
        return {"thread:bureau": text}
    return {"bureau": text}


def auditors(listing: list, canons=None, *, explicit_only=False) -> list:
    """Les auditeurs du registre : l'agent `auditeur`, et ceux dont la fiche
    Agent du canon porte `roles: [auditeur]` (ou `auditor`, `role:`).
    `explicit_only` exige ce rôle, sans autorisation implicite par le nom."""
    names = {row["name"] for row in listing}
    found = sous_utilisation.role_orchestrators(canons or [], AUDITOR_ROLES)
    if not explicit_only:
        found.add(AUDITOR_NAME)
    return sorted(n for n in found if n in names)


def _projects(by_name: dict, name) -> set:
    """L243 : les projets d'un agent (`projects`, sinon son équipe)."""
    from . import projects as projects_mod
    return set(projects_mod.agent_projects(by_name.get(name) or {}))


def _workstream(row: dict) -> str:
    """`workstream` d'abord ; `chantier` reste lu tant que les lecteurs chauds
    ne l'ont pas remplacé (L148, stage 3 C1)."""
    return row.get("workstream") or row.get("chantier") or ""


def _of_teams(orchestras, by_name: dict, teams: set) -> list:
    """Les orchestrateurs d'un des projets (ou sans projet) ; sans projet
    connu, tous.

    `orchestras` vient de `sous_utilisation.alert_orchestrators` (#1114) :
    l'historique d'attribution ne confère pas le rôle, donc un agent sans
    projet n'est ici qu'un hub explicitement déclaré (rôle au canon ou
    `AMEESH_ALERT_ORCHESTRATORS`), jamais un assigneur récent."""
    if not teams:
        return list(orchestras)
    return [o for o in orchestras if not _projects(by_name, o)
            or _projects(by_name, o) & teams]


def _of_project(orchestras, by_name: dict, project, teams: set) -> list:
    """#705 : les orchestrateurs du PROJET du lot (`team` ou `workstream`),
    à défaut ceux de l'équipe de l'assigné (routage d'origine), à défaut tous.
    L243 : l'agent est membre de chacun de ses projets (`_projects`)."""
    project = (project or "").strip()
    if project:
        matched = [o for o in orchestras
                   if project in _projects(by_name, o)
                   or project == _workstream(by_name.get(o) or {})]
        if matched:
            return matched
    return _of_teams(orchestras, by_name, teams)


def project_pilots(project: str, listing: list, *, cfg=None, db=None,
                   canons=None) -> list:
    """#2935 : les pilotes (orchestrateurs) d'un projet, ordre stable.

    Un échec de CI sur une branche de base n'appartient à aucun lot : il doit
    partir au pilote du projet, pas à personne. Le rôle vient du canon
    (`orchestrateur`) ; `_of_project` le restreint au projet, avec le repli
    « tous les orchestrateurs » quand aucun ne le déclare — mieux vaut un
    pilote d'un autre projet qu'aucun destinataire pour une base rouge. Le
    choix d'un seul destinataire est laissé à l'appelant (`[0]` : ordre
    stable)."""
    orchestras = sous_utilisation.alert_orchestrators(
        cfg, db, listing, canons=canons)
    by_name = {row["name"]: row for row in listing}
    return sorted(_of_project(orchestras, by_name, project, set()))


def _reviewer_missing(alert: dict) -> str:
    """#705 : un lot en qa SANS relecteur désigné n'attend personne ; le texte
    demande à l'orchestrateur du projet d'en désigner un (ou de relire lui-même)."""
    lot = int(alert["lot"])
    title = alert.get("title")
    v = vocab.active()
    return ("No reviewer designated: %s%s (%s) has been waiting for a verdict for %s "
            "with no designated reviewer, so the wait belongs to nobody. Expected action: "
            "designate the reviewer: ameesh work reviewer %d <agent>; or take the "
            "review yourself and give the verdict: ameesh work milestone %d verdict "
            "ok|blocked --sha <frozen head> --note \"…\"." % (
                v.label(lot), " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
                sous_utilisation._duration(alert.get("value") or 0), lot, lot))


def _verdict_due(alert: dict) -> str:
    """#705 : le relecteur DÉSIGNÉ d'un lot en qa qui stagne : le verdict lui est dû."""
    lot = int(alert["lot"])
    title = alert.get("title")
    v = vocab.active()
    return ("Verdict expected: %s%s (%s) has been waiting for its verdict for %s; you "
            "are its designated reviewer. Expected action: review it and give your verdict: "
            "ameesh work milestone %d verdict ok|blocked --sha <frozen head> --note \"…\"; "
            "if you cannot, say so on the %s (ameesh work note %d \"hold: <reason>\") so "
            "the project's orchestrator can hand the review over." % (
                v.label(lot), " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
                sous_utilisation._duration(alert.get("value") or 0), lot, v.noun, lot))


def _stale_move(alert: dict, lot: int, who: str | None) -> str:
    """L'action proposée pour un lot stagnant (#1773).

    Un lot en qa dont l'attente est la fusion (gel + verdict acquis) est un
    candidat de paquet/de file de fusion : relancer son assigné ou le
    réassigner ne fait pas avancer la fusion. Le brief le dit — sans retirer
    l'alerte (canon §4 : un lot sans mouvement se signale au pilote) ni les
    portes de clôture, qui restent offertes plus bas."""
    v = vocab.active()
    if alert.get("lot_state") in lifecycle.TO_VALIDATE and alert.get("waiting_for") == "fusion":
        return ("this %s is frozen with its verdict acquired: it is a candidate for its "
                "merge packet/queue — no author action, do not nudge its assignee or "
                "reassign it; the composer/pilot carries the merge and can declare the "
                "planned landing that stops the reminders: ameesh work landing %d "
                "--vehicle <vehicle lot> --until <YYYY-MM-DD>" % (v.noun, lot))
    if who:
        # #2016 : la commande porte le titulaire lu ici (`--if-holder`) : un
        # second agent parti du même brief est refusé si le lot a changé de
        # main entre-temps, au lieu de voler le lot au premier.
        return ("nudge %s: ameesh mail send %s \"…\" --lot %d, or reassign it: "
                "ameesh work assign %d <agent> --if-holder %s" % (who, who, lot, lot, who))
    external = (alert.get("external_executor") or "").strip() or None
    if external:
        # #3447 : réservé, pas sans assigné — le geste est celui de
        # l'exécutant, ou la levée préalable par le pilote.
        return ("this %s is reserved to %s (external executor, not an assignee): the "
                "executor acts; if the reservation is without object, the pilot clears "
                "it first: ameesh work external-executor %d --clear"
                % (v.noun, external, lot))
    # #1026 : un lot sans assigné se prend (`--claim`) : deux orchestrateurs
    # qui l'assignent en même temps ne peuvent plus se voler le lot.
    return "assign it: ameesh work assign %d <agent> --claim" % lot


def _one_orchestrator(orchestras, by_name: dict, lot) -> list:
    """Un seul orchestrateur pour un lot sans assigné (#1026).

    Parmi ceux dont l'exécuteur est vivant (`sous_utilisation._awake_runner`),
    un seul, choisi par le numéro du lot : stable d'une passe à l'autre
    (relance comprise) et réparti. Aucun exécuteur vivant : tous — ils sont
    arrêtés, le premier revenu agit, et la prise du lot est de toute façon
    atomique (`ameesh work assign --claim`)."""
    names = sorted(orchestras)
    if not names:
        return []
    awake = [n for n in names if sous_utilisation._awake_runner(by_name.get(n) or {})]
    if not awake:
        return names
    return [awake[int(lot or 0) % len(awake)]]


def _stale_lot(alert: dict) -> str:
    lot = int(alert["lot"])
    who = alert.get("agent")
    external = (alert.get("external_executor") or "").strip() or None
    title = alert.get("title")
    v = vocab.active()
    head = "Stale %s: #%d%s (%s) no activity for %s, %s%s." % (
        v.noun, lot, " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
        sous_utilisation._duration(alert.get("value") or 0),
        "assigned to %s" % who if who
        else "reserved to %s (external executor)" % external if external
        else "unassigned",
        ", waiting for: %s" % alert["waiting_for"] if alert.get("waiting_for") else "")
    move = _stale_move(alert, lot, who)
    # L368: a dependency declared towards an open parent lot is a planned wait,
    # which already suppresses the alert and its 6 h reminder. The brief must
    # offer it, otherwise a stacked lot is triaged in vain every 6 h (#1642) ;
    # #1261 le disait déjà « the alert stops while the wait is planned ».
    hold = ("if the %s is parked on another open %s, declare the dependency that stops "
            "the reminders (the alert stops while the wait is planned): "
            "ameesh work dependencies %d --add <blocker>; if a package vehicle is to "
            "carry it, declare the landing: ameesh work landing %d --vehicle "
            "<vehicle lot> --until <YYYY-MM-DD>" % (v.noun, v.noun, lot, lot))
    # #1664 : une porte hors mesh (fenêtre opérateur, décision humaine sans
    # demande ouverte) n'est portée par aucun lot ; le pilote la consigne en
    # hold, qui éteint la relance comme une dépendance déclarée.
    park = ("if it is parked on a gate no open %s carries (operator window, human "
            "decision), the pilot records the hold that stops the reminders: "
            "ameesh work note %d \"hold: <reason>\" (the pilot lifts it with an "
            "\"unhold: <reason>\" note or a state change)" % (v.noun, lot))
    return ("%s Expected action: if it was merged outside a PR, close it: ameesh work merged %d "
            "--sha <merge commit>; if it was delivered outside the target (report, note, head "
            "reviewed on a branch, no merge), close it: ameesh work close %d --delivered "
            "--sha <reviewed head>; %s; %s; otherwise %s; if it is no longer needed: ameesh work "
            "close %d --abandoned (or --superseded-by <id>, or --done if it has no branch)."
            % (head, lot, lot, hold, park, move, lot))


def _plan_underused(alert: dict) -> str:
    v = vocab.active()
    idle = list(alert.get("idle_agents") or ())
    if idle:
        move = ("give work to the idle agents (%s), tied to a %s: ameesh mail send "
                "<agent> \"…\" --lot <id> (or --new-lot \"title\"), or ameesh work assign "
                "<lot> <agent>" % (", ".join(idle), v.noun))
    else:
        move = ("no %s agent is idle: move work onto this harness (ameesh "
                "work assign <lot> <agent>) or wake a stopped agent (ameesh resume "
                "<agent>)" % (alert.get("harness") or "of this account"))
    return ("Underused plan: %s. Expected action: %s; open %s without an agent: "
            "ameesh projects." % (alert.get("detail") or "?", move, v.plural))


def _estimate_bias(alert: dict) -> str:
    """L157b : à l'estimateur et aux orchestrateurs."""
    ratio = alert.get("value")
    sens = ("underestimates" if ratio is not None and float(ratio) > 1 else "overestimates")
    return ("Estimate bias: %s. %s %s %s. Expected action: start from "
            "the reference estimate (shown by ameesh work add and ameesh work show, "
            "table published by the auditor) and deviate from it only with a justification "
            "(--estimate-source), estimate broken down into phases (agent, CI, waiting for "
            "review, human)." % (alert.get("detail") or "?", alert.get("agent") or "?",
                                 sens, vocab.active().plural))


def _waiting_without_question(alert: dict) -> str:
    """L233: to the assignee and the orchestrators of its team."""
    lot = int(alert["lot"])
    title = alert.get("title")
    v = vocab.active()
    return ("%s%s is in waiting_human but no decision request is open: the owner sees "
            "a %s waiting for them and has no question to answer. Expected action: ask the "
            "owner through the queue — ameesh decide ask --lot %d --question \"…\" "
            "--option a=\"…\" [--recommend a --why \"…\"] — or, if nothing is expected from "
            "him, move the %s back: ameesh work move %d <state>. A decision written in a "
            "mail (to bureau or anyone) never counts." % (
                v.label(lot, capital=True), " « %s »" % title if title else "",
                v.noun, lot, v.noun, lot))


def _idle_actionable_lot(alert: dict) -> str:
    """#408 (règle 3) : à l'auditeur seul, qui relance (niveau 1)."""
    lot = int(alert["lot"])
    who = alert.get("agent") or "?"
    title = alert.get("title")
    v = vocab.active()
    if alert.get("runner_live"):
        nudge = ("agent-mail send %s \"…\" --lot %d (what is expected on the %s, or "
                 "ask it to record why it cannot advance: ameesh work note %d \"hold: "
                 "<reason>\")" % (who, lot, v.noun, lot))
    else:
        nudge = "ameesh resume %s (its runner is not live), then agent-mail send %s " \
                "\"…\" --lot %d" % (who, who, lot)
    return ("Idle with an actionable %s: %s holds #%d%s (%s), and %d wake(s) of its runner "
            "left the %s unchanged. Expected action, level 1 (reversible), not just naming "
            "it: nudge it now: %s; if it cannot take the %s, reassign it: ameesh work "
            "assign %d <agent>. Never twice within an hour for the same agent." % (
                v.noun, who, lot, " \"%s\"" % title if title else "", alert.get("lot_state") or "?",
                int(alert.get("wakes") or 0), v.noun, nudge, v.noun, lot))


def _packet_ci_text(ci: dict) -> str:
    """#3487 : l'état de la CI du paquet, dans le geste SLO.

    Le lot porté n'attend plus sa propre CI ; le brief dit où en est celle du
    paquet (en cours, en file, pas encore démarrée, verte, rouge, annulée) pour
    que le palier visé sache s'il doit pousser le composeur ou traiter un
    rouge. Un état illisible ne fait pas taire le geste : il nomme le paquet.
    """
    state = str((ci or {}).get("state") or "unknown")
    run = "run %s" % ci["run_id"] if (ci or {}).get("run_id") else "its CI"
    if state == "running":
        return " (%s is still running)" % run
    if state == "pending":
        return " (its full CI has not started yet)"
    if state == "green":
        return " (%s is green; the packet merge is pending)" % run
    if state == "red":
        return (" (%s is red: a unit must be fixed or removed before the packet "
                "merges)" % run)
    if state in ("cancelled", "superseded"):
        return " (%s was %s; a new run is expected)" % (run, state)
    return " (its CI is not readable here)"


def _slo_breach(alert: dict, *, level: int | None = None) -> str:
    """#789 tranche 2 (canon §9) : une étape interne du lot a dépassé son
    délai ; le texte dit l'étape, le retard, et le geste du niveau atteint
    (1× l'assigné, 2× le pilote, 3× le bureau ou l'auditeur, bureau et
    auditeur pour un P1). #2395 : aucun dépassement n'est notifié à l'owner
    (décision owner 18/10 : « rien sur les SLO »).

    #3036 : `level` force le niveau du texte quand le palier atteint n'avait
    aucun destinataire vivant et que le brief part au palier suivant — le
    geste demandé doit être celui du palier qui reçoit, pas celui du palier
    muet.

    #2231 : quand l'attente est une fusion (`merge*`) ou un déblocage
    (`unblock`), le porteur n'est pas le blocage — le brief vise le pilote/le
    composeur et ne prescrit ni relance ni réassignation du porteur.

    #3487 : quand l'attente est un paquet ouvert (`packet`), le lot est porté
    par un paquet composé ; le geste nomme le paquet, sa branche et l'état de
    sa CI complète, et ne prescrit jamais de réassigner le porteur — il pousse
    le pilote/le composeur à voir le paquet jusqu'à sa fusion.

    #4572 : quand `waiting.candidate` nomme une cause côté auteur
    (`freeze-not-head`, `conflict`…), le composeur ne peut rien : le geste
    nomme l'auteur et ce qu'il doit faire (regeler, résoudre le conflit,
    obtenir l'avis de la remise). Le texte « le composeur le porte » reste
    réservé au lot réellement prêt (`ready`) ; une lecture incomplète renvoie
    à la liste calculée sans nommer personne au hasard.

    #2378 : de même pour une demande de décision ouverte et échue
    (`waiting.decision`) : le geste est de vérifier l'échéance avec le
    demandeur (renouveler ou retirer la demande) ; le décideur a le rappel de
    sa demande, l'alerte SLO ne le relance pas (canon §9, owner 18/10).

    #3288 : `blocked_freeze` est due par l'assigné (le débiteur de la
    correction), pas par le relecteur : ×1 l'assigné corrige et regel, ×2 le
    pilote réassigne, ×3 le bureau/l'auditeur réassigne à un agent libre de la
    famille de l'auteur.

    #3447 : un lot réservé à un exécutant externe n'est pas « unassigned » —
    le texte nomme l'exécutant (bureau, owner) et le fait que ce n'est pas une
    attribution."""
    lot = int(alert["lot"])
    title = alert.get("title")
    level = int(alert.get("level") or 1) if level is None else int(level)
    step = alert.get("step_label") or alert.get("step") or "?"
    who = alert.get("agent")
    external = alert.get("external_executor")
    v = vocab.active()
    head = ("SLO breach, level %d: %s%s (%s), step `%s` late by %s (target %s)%s."
            % (level, v.label(lot), " \"%s\"" % title if title else "",
               alert.get("lot_state") or "?", step,
               sous_utilisation._duration(alert.get("value") or 0),
               sous_utilisation._duration(alert.get("threshold") or 0),
               (", owed by reviewer %s" % who if alert.get("step") == "green_verdict"
                else ", fix owed by %s" % who if alert.get("step") == "blocked_freeze"
                else ", assigned to %s" % who if who
                else ", reserved to %s (external executor)" % external if external
                else ", unassigned")))
    # #2016 : quand la commande est proposée, elle porte le titulaire lu
    # (`--if-holder`) : le pilote ou le bureau parti de ce brief est refusé si
    # le lot a changé de main entre-temps, au lieu d'écraser le premier geste.
    holder = " --if-holder %s" % who if who else ""
    waiting = alert.get("waiting") or {}
    what = waiting.get("what") if isinstance(waiting, dict) else None
    # #2511 : le lot cible le dépôt canon (manaty/home) ; un paquet ameesh ne
    # le porte pas, la fusion est celle du bureau.
    canon = bool(isinstance(waiting, dict) and waiting.get("canon"))
    # #2378 : une demande ouverte et échue (sinon le lot serait en attente
    # prévue) ; les cibles sont passées au pilote à toute étape
    # (`exploitation._slo_targets`), le brief les suit. Sans demande,
    # l'attente `decision` garde le texte ordinaire.
    decision = waiting.get("decision") if what == "decision" else None
    if decision is None and (what == "decision"
                             or alert.get("step") != "verdict_merge"):
        # The targets move to the pilot on `verdict_merge` only
        # (`exploitation._slo_targets`): the brief follows them, so a text
        # addressed to the pilot never reaches the assignee.
        what = None
    # #4572 : la cause du refus d'admission au paquet, lue par la passe SLO
    # avec les codes de `paquet-candidats` (`exploitation._slo_breaches`).
    # Côté auteur (`freeze-not-head`, conflit…), le composeur ne peut rien :
    # le brief nomme l'auteur et son geste. « Le composeur le porte » reste
    # réservé au lot réellement prêt (`ready`) et pas encore composé.
    cause = waiting.get("candidate") if isinstance(waiting, dict) else None
    if what in ("merge", "merge-approval", "merge-outcome"):
        if canon:
            if level >= 3:
                move = ("bureau: merge it directly — its `canon/…` branch targets the canon "
                        "repository (manaty/home), which an ameesh package does not carry; "
                        "auditor: see that the bureau does it, do not reassign or nudge the "
                        "holder")
            else:
                move = ("bureau: merge it directly — its `canon/…` branch targets the canon "
                        "repository (manaty/home), outside the ameesh package; the "
                        "pilot/composer cannot carry it, do not wait for them")
        elif cause and cause.get("code") and cause.get("owner") == "author":
            author = who or alert.get("responsible") or "the author"
            code = str(cause["code"])
            detail = str(cause.get("detail") or "")
            seen = " — %s" % detail if detail else ""
            gesture = str(cause.get("gesture") or "")
            if level >= 3:
                move = ("bureau or auditor: the %s is frozen with its review verdict "
                        "acquired, but a package cannot carry it — cause `%s`%s; the "
                        "author %s still owes the fix (%s) — push the author and the "
                        "pilot, reassign the %s to a free agent of the same family if "
                        "the author cannot act; do not wait for the composer"
                        % (v.noun, code, seen, author, gesture, v.noun))
            elif level >= 2:
                move = ("pilot: the %s is frozen with its review verdict acquired, but a "
                        "package cannot carry it — cause `%s`%s; the author %s owes the fix "
                        "(%s) — push them; if they cannot act, hand the %s to a free agent "
                        "of the same family (ameesh work assign %d <agent>%s); the composer "
                        "cannot carry it before"
                        % (v.noun, code, seen, author, gesture, v.noun, lot, holder))
            else:
                move = ("author %s: the %s is frozen with its review verdict acquired, but "
                        "a package cannot carry it — cause `%s`%s; %s; the composer cannot "
                        "advance it until this is cleared"
                        % (author, v.noun, code, seen, gesture))
        elif cause and cause.get("unknown"):
            # Lecture incomplète sur cet hôte (dépôt absent, refs manquantes) :
            # on ne nomme ni l'auteur ni le composeur au hasard ; le brief
            # renvoie à la liste calculée, qui fait foi à la composition.
            if level >= 3:
                move = ("bureau or auditor: the %s is frozen with its review verdict "
                        "acquired; this host cannot read the candidate refusal — check "
                        "`scripts/paquet-candidats.py` before pushing the composer, the "
                        "%s may be waiting on its author (freeze-not-head, conflict)"
                        % (v.noun, v.noun))
            else:
                move = ("pilot: the %s is frozen with its review verdict acquired; this "
                        "host cannot read the candidate refusal — check "
                        "`scripts/paquet-candidats.py` before acting; the %s may be "
                        "waiting on its author (freeze-not-head, conflict), and the "
                        "composer can only carry a ready %s" % (v.noun, v.noun, v.noun))
        elif alert.get("vehicle_exit_ts") is not None:
            # #4369 : retiré du paquet par sa recomposition ; le temps passé
            # dans le véhicule n'est pas compté, l'horloge part de la sortie.
            left = time.strftime("%H:%M", time.gmtime(float(alert["vehicle_exit_ts"])))
            move = ("%s: the %s left its packet at recomposition (%sZ; the clock runs "
                    "from there, not from the GO) — do not reassign or nudge its holder; "
                    "%s return it to build with the cause (ameesh work move %d build) "
                    "or include it in the next package"
                    % ("bureau or auditor" if level >= 3 else "pilot", v.noun, left,
                       "push the composer/pilot to" if level >= 3 else "either", lot))
        elif level >= 3:
            move = ("bureau or auditor: the %s waits for its merge vehicle/package — "
                    "the composer/pilot carries it, and reassigning or nudging its holder "
                    "does not advance it; push the pilot/composer to include it in the next "
                    "package" % v.noun)
        else:
            move = ("pilot: the %s is frozen with its review verdict acquired and waits "
                    "for its merge vehicle/package — the composer/pilot carries it; do not "
                    "reassign or nudge its holder, include it in the next package" % v.noun)
    elif decision is not None:
        decider = waiting.get("owner") or "its decider"
        due = waiting.get("due_ts")
        late = (" (deadline %s passed)" % time.strftime("%Y-%m-%d %H:%M",
                                                         time.localtime(float(due)))
                if due is not None else "")
        # Canon §9 (owner 18/10) : rien sur les SLO pour l'owner — le rappel
        # du décideur est celui de la demande (`decision_overdue`), pas un
        # geste dicté par l'alerte SLO.
        if level >= 3:
            move = ("bureau or auditor: the %s is `waiting_human` on decision #%d for "
                    "%s%s — reassigning or nudging its holder does not advance it; push "
                    "the pilot to check the deadline with the requester, who renews or "
                    "withdraws the request (ameesh decide show %d)"
                    % (v.noun, decision, decider, late, decision))
        else:
            move = ("pilot: the %s is `waiting_human` on decision #%d for %s%s — do not "
                    "reassign its holder; check the deadline with the requester, who "
                    "renews or withdraws the request (ameesh decide show %d); the "
                    "decider gets the request's own reminder, not an SLO nudge"
                    % (v.noun, decision, decider, late, decision))
    elif what == "unblock":
        if level >= 3:
            move = ("bureau or auditor: the %s is `blocked` and the pilot owns the "
                    "unblock (or the recorded decision) — do not reassign its holder; "
                    "push the pilot to unblock it or to say why the %s waits"
                    % (v.noun, v.noun))
        else:
            move = ("pilot: the %s is `blocked` and waits for its unblock/decision — "
                    "do not reassign its holder; unblock it or record the decision "
                    "(ameesh work note %d \"hold: <reason>\")" % (v.noun, lot))
    elif what == "packet":
        # #3487 : le lot est porté par un paquet composé ; son attente est la CI
        # complète du paquet et sa fusion. Le geste nomme le paquet et sa CI,
        # jamais une réassignation du porteur (qui n'avancerait rien).
        vehicle = waiting.get("vehicle")
        packet = ("#%s" % vehicle if vehicle is not None
                  else (waiting.get("who") or "its packet"))
        branch = str(waiting.get("branch") or "")
        where = " (%s)" % branch if branch else ""
        ci = _packet_ci_text(waiting.get("ci") or {})
        if level >= 3:
            move = ("bureau or auditor: the %s is carried by packet %s%s and waits for "
                    "the packet's full CI%s — do not reassign or nudge its holder; push "
                    "the pilot/composer to see the packet through (the %s merges with "
                    "it)" % (v.noun, packet, where, ci, v.noun))
        else:
            move = ("pilot: the %s is carried by packet %s%s and waits for the packet's "
                    "full CI%s — do not reassign or nudge its holder; the composer/pilot "
                    "carries the packet merge" % (v.noun, packet, where, ci))
    elif alert.get("step") == "green_verdict":
        # #3033 : l'avis est dû par le relecteur désigné, pas par le porteur ;
        # le geste est de rendre le verdict, puis de remplacer le relecteur.
        sha = str(alert.get("sha") or "")
        verdict = "ameesh work milestone %d verdict ok|blocked --sha %s" % (
            lot, sha or "<frozen sha>")
        if level >= 3:
            move = ("bureau or auditor: the pilot has not replaced reviewer %s — designate "
                    "another reviewer of an independent family now (ameesh work reviewer "
                    "%d <agent>, then ameesh work reviewer %d %s --remove)"
                    % (who or "?", lot, lot, who or "<reviewer>"))
        elif level >= 2:
            move = ("pilot: reviewer %s has not delivered the verdict — nudge them, or "
                    "designate another reviewer of an independent family (ameesh work "
                    "reviewer %d <agent>, then ameesh work reviewer %d %s --remove); do not "
                    "reassign the %s's holder" % (who or "?", lot, lot,
                                                  who or "<reviewer>", v.noun))
        else:
            move = ("reviewer %s: review the frozen head and render your verdict now (%s); "
                    "if you cannot, tell the pilot so another reviewer is designated"
                    % (who or "?", verdict))
    elif alert.get("step") == "blocked_freeze":
        # #3288 : un avis bloquant n'attend pas un relecteur mais la correction
        # de l'assigné ; le canon §9 escalade de l'assigné au pilote, puis au
        # bureau/à l'auditeur. #3487 : même horloge pour un lot revenu en build
        # après son GO, et le niveau 3 ne prescrit jamais de réassigner le
        # porteur qui porte le correctif (la réassignation perd son contexte).
        blocker = str(alert.get("blocked_by") or "")
        reason = ("the fix owed after returning to build" if alert.get("returned")
                  else "the blocking verdict of %s" % (blocker or "a reviewer"))
        if level >= 3:
            move = ("bureau or auditor: %s is still owed by %s, who carries the fix — do "
                    "not reassign the holder (a reassignment loses the context of the "
                    "fix); push the holder and the pilot to deliver the new freeze, and "
                    "ask the pilot to record a hold if it cannot advance (a pilot hold is "
                    "what suspends this clock)" % (reason, who or "the assignee"))
        elif level >= 2:
            move = ("pilot: %s — push %s; if the holder cannot deliver it, take the fix "
                    "yourself or hand it over to a free agent of the same family (ameesh "
                    "work assign %d <agent>%s, then agent-mail send <agent> \"…\" --lot "
                    "%d)" % (reason, who or "the assignee", lot, holder, lot))
        else:
            move = ("assignee: deliver %s and freeze the new head (`ameesh work "
                    "milestone %d frozen --sha <new head>`); if you moved the %s to "
                    "build, re-enter qa (`ameesh work move %d qa`); if you cannot, say "
                    "so (`ameesh work note %d \"hold: <reason>\"`) so the pilot hands "
                    "the fix over" % (reason, lot, v.noun, lot, lot))
    elif external:
        # #3447 : le lot est réservé, pas assigné — le geste n'est ni une
        # relance d'assigné ni une réassignation directe (refusée par
        # `work assign`) mais l'action de l'exécutant ou la levée préalable.
        if level >= 3:
            move = ("bureau or auditor: %s is reserved to %s (external executor, not "
                    "an assignee) — this is not an assignment; the executor acts, or the "
                    "pilot clears the reservation before any reassignment (ameesh work "
                    "external-executor %d --clear)" % (v.label(lot), external, lot))
        elif level >= 2:
            move = ("pilot: %s is reserved to %s (external executor) — do not assign "
                    "it as is; the executor acts, or clear the reservation before "
                    "reassigning (ameesh work external-executor %d --clear)"
                    % (v.label(lot), external, lot))
        else:
            move = ("%s (external executor): %s is reserved to you; act on it, or tell "
                    "the pilot it is not for you (the pilot clears the reservation with "
                    "ameesh work external-executor %d --clear)"
                    % (external, v.label(lot), lot))
    elif level >= 3:
        move = ("bureau or auditor: reassign it now to a free agent (ameesh work assign "
                "%d <agent>%s, then agent-mail send <agent> \"…\" --lot %d); do not wait "
                "for the pilot if it is itself late" % (lot, holder, lot))
    elif level >= 2:
        move = ("pilot: reassign it or unblock it (ameesh work assign %d <agent>%s, then "
                "agent-mail send <agent> \"…\" --lot %d)" % (lot, holder, lot))
    else:
        # #3274 : un `hold:` de l'assigné n'arrête que `sans_tour` (24 h,
        # `exploitation.ORPHAN_SELF_HOLD_S`) ; seule la retenue d'un pilote
        # suspend le SLO (#1664, `planned_waits.pilot_holds`). Le texte le dit,
        # sinon l'assigné croit l'horloge arrêtée et ne voit pas l'escalade.
        move = ("assignee: act on it, or record why it cannot advance (ameesh work note "
                "%d \"hold: <reason>\") — a silent %s is not a hold. Your own hold only "
                "postpones the `sans_tour` reminder for 24 h; it does not suspend this "
                "SLO clock — only a pilot's hold does" % (lot, v.noun))
    if alert.get("critical"):
        move += ("; the %s is P1: the bureau handles it with the auditor (canon §9, "
                 "niveau 4), and no SLO delay is notified to the owner — nothing waits "
                 "for a human that an agent can do" % v.noun)
    return "%s Expected action: %s." % (head, move)


def _critical_budget_pause(alert: dict) -> str:
    """L242b (5) : aux orchestrateurs de l'équipe et aux auditeurs (urgent)."""
    return ("Critical agent paused on budget: %s. Expected action: check the accounts "
            "(ameesh accounts list) and the mesh caps (ameesh budget); free an account or "
            "raise a cap with the owner, or hand its urgent work to another agent."
            % (alert.get("detail") or "?"))


def _account_config_invalid(alert: dict) -> str:
    """#3969 : aux orchestrateurs de l'équipe et aux auditeurs."""
    return ("No valid harness account on the host: %s. Expected action: apply the fix "
            "given for each account (chmod 700 of a profile directory, a login), check "
            "with ameesh doctor on that host; the agent resumes on its own at the next "
            "check, no `ameesh resume` needed." % (alert.get("detail") or "?"))


def _budget_pause_long(alert: dict) -> str:
    """#1466 : une pause budget qui dure n'est plus ordinaire. Aux
    orchestrateurs de l'équipe et aux auditeurs."""
    return ("Agent paused on budget for a long time: %s. Expected action: check the caps "
            "(ameesh budget), the accounts (ameesh accounts list) and the configuration "
            "the runner reads (ameesh doctor; a profile can diverge from the host's "
            "config.json); free an account, fix the profile, or have the runner restart, "
            "then `ameesh resume` the agent if its runner is not live."
            % (alert.get("detail") or "?"))


def _automatic_sleep(alert: dict) -> bool:
    """#4525 : la pause qui dure est le sommeil automatique du découpage."""
    from . import file_decoupage
    return bool(alert.get("sleep") or file_decoupage.is_sleep_reason(alert.get("reason")))


def quiet_payloads(alert: dict, names, by_name: dict) -> dict:
    """#5044 : par destinataire, les clés passives de son courrier (`mail.PASSIVE_KEYS`).

    Le sommeil automatique n'attend aucun geste : il part sans réveiller. Une
    alerte SLO copiée à plusieurs coordinateurs n'en réveille qu'un, stable
    d'une passe à l'autre (`_one_orchestrator`) ; les autres la lisent en copie
    à leur tour suivant, et le palier suivant reprend si rien ne bouge. Un P1
    (`urgent`) réveille tout le monde, comme avant."""
    if alert.get("urgent"):
        return {}
    names = [name for name in names if not name.startswith("thread:")]
    kind = alert.get("type")
    if kind in ("operator_pause_long", "operator_pause_host") and _automatic_sleep(alert):
        return {name: {"info": True} for name in names}
    if kind != "slo_breach" or len(names) < 2:
        return {}
    holder = alert.get("agent")
    woken = holder if holder in names else \
        _one_orchestrator(names, by_name, alert.get("lot"))[0]
    return {name: {"cc": woken} for name in names if name != woken}


def _operator_pause(alert: dict) -> str:
    """#3961 : une pause opérateur qui dure (un agent, ou la plupart des
    agents d'un hôte). Aux orchestrateurs de l'équipe et aux auditeurs.

    #4525 : le sommeil automatique du découpage (`SLEEP_REASON`) n'est pas une
    maintenance. L'alerte le reconnaît et dit qu'il est levé au premier lot
    démarrable, sans demander à l'owner de mettre fin à une maintenance.
    #4610 : la levée est aussi automatique sur un lot attribué ou un courrier
    du pilote ou d'un orchestrateur.
    """
    v = vocab.active()
    if _automatic_sleep(alert):
        return ("Automatic sleep still in place: %s. This is the automatic sleep of an "
                "exhausted roadmap, not a maintenance: it is lifted by the first "
                "startable %s, an assigned actionable %s, or pilot/orchestrator "
                "mail, and at the latest when its deadline expires. No "
                "operator action is expected; the owner is not asked to end any "
                "maintenance." % (alert.get("detail") or "?", v.noun, v.noun))
    return ("Operator pause still in place: %s. Expected action: check with whoever set it "
            "(ameesh show <agent>) whether the maintenance is over; if so, lift it "
            "(ameesh resume <agent> --operator --reason <reason>), otherwise hand the "
            "agent's urgent %s to another agent." % (alert.get("detail") or "?", v.plural))


def _rate_limited_chronic(alert: dict) -> str:
    """#3733 : des épisodes de débit courts, effacés par un succès, couvrent
    une bonne part de l'heure. Aux orchestrateurs de l'équipe et aux
    auditeurs ; pas à l'agent, qui attend sa reprise."""
    agent = alert.get("agent") or "?"
    return ("Agent rate-limited for a large share of the last hour: %s. Expected action: "
            "the runner resumes it by itself, but its work does not move — hand its "
            "reviews due and urgent %s to an agent of another account or family "
            "(ameesh work pending-reviews %s; ameesh work reviewer <lot> <agent>); check "
            "its recent input volume (ameesh cost turns --agent %s) and rotate its "
            "session earlier if each turn rereads a large context."
            % (alert.get("detail") or "?", vocab.active().plural, agent, agent))


def _notify_undeliverable(alert: dict) -> str:
    """#3007 : aucun notificateur vivant ne couvre l'humain qui a des décisions
    en attente. Aux orchestrateurs et à l'auditeur : la poussée n'a pas eu
    lieu ; #3383 : le geste est la route de l'humain sur chaque candidat du
    rôle qui porte la minuterie `alerts`. Le brief est porté hors du poste (`notify_gap.brief_once`)."""
    return ("No living notifier covers the human with pending decisions: %s. Expected "
            "action (#3383): the alerts pass is a role timer, run by the lease holder; "
            "declare the human's notify route on every candidate of the role and run "
            "`ameesh role timers --install` there (`--check` names the gap); keep "
            "the pending requests visible in the %s threads meanwhile — no agent can "
            "push them without a channel. This alert resolves by itself when a notifier "
            "beats again for that human, or when nothing is pending."
            % (alert.get("detail") or "no notifier heartbeat", vocab.active().noun))


def _for_auditor(alert: dict) -> str:
    """`idle_capacity` à l'auditeur : le constat et l'action de l'alerte ; avec
    des lots démarrables ouverts sans agent, l'écart est le sien (L165b,
    niveau 1). #1608 : un lot qu'une attente planifiée retient n'est pas
    démarrable, on ne lui demande pas de l'assigner."""
    v = vocab.active()
    text = "Idle capacity: %s." % (alert.get("detail") or "?")
    waiting = (" %s are waiting with idle agents: if this still holds at your next "
               "pass, 30 min or more after this mail, fix it yourself (level 1, reversible): "
               "ameesh work assign <lot> <agent> (an idle agent of the %s's team, highest "
               "priority first), then agent-mail send <agent> \"…\" --lot <lot> to "
               "wake it; name it in your report." % (v.title_plural, v.noun))
    if alert.get("startable_lots"):
        text += waiting
    elif alert.get("external_lots"):
        # #3447 : ces lots ne s'assignent pas — l'alerte nomme l'exécutant
        # externe (bureau, owner) ; le pilote lève la réservation s'il la
        # juge sans objet.
        text += (" No startable %s: %s — the pilot declared them; their external executor "
                 "acts, do not assign an agent. If the reservation is without object, ask "
                 "the pilot to clear it (ameesh work external-executor <lot> --clear)."
                 % (v.noun, ", ".join("#%s (%s)" % (row.get("lot"), row.get("executor"))
                                      for row in alert.get("external_lots") or ())))
    elif alert.get("lots") and "startable_lots" not in alert:
        # Producteur d'avant #1608 : la liste `lots` n'était pas classée.
        text += waiting
    return text


def _target_names(role: str, alert: dict, by_name: dict, orchestras, teams: set,
                  auditor_names=()) -> list:
    """#3036 : les noms que porte un rôle d'escalade du §9, avant vivacité.

    `assignee` : l'agent du lot ; `pilot` : les orchestrateurs de l'équipe (le
    `targets` du moteur SLO dit le rôle, pas les noms) ; `bureau` : le bureau,
    sauf `externe` (0030 : il ne lit pas sa boîte, sa copie va au fil) ;
    `auditeur` : les auditeurs configurés. L'ordre est stable, sans doublon."""
    if role == "assignee":
        name = alert.get("agent")
        return ([name] if name in by_name
                and not (name == "bureau" and _bureau_externe(by_name)) else [])
    if role == "pilot":
        # #2602 : le bureau `externe` ne lit pas sa boîte, même s'il est listé
        # parmi les pilotes ou les auditeurs ; sa copie est au fil.
        return [name for name in _of_teams(orchestras, by_name, teams)
                if not (name == "bureau" and _bureau_externe(by_name))]
    if role == "bureau":
        bureau = by_name.get("bureau")
        return ["bureau"] if bureau and bureau.get("mode") != "externe" else []
    if role == "auditeur":
        return [name for name in auditor_names or ()
                if name in by_name
                and not (name == "bureau" and _bureau_externe(by_name))]
    return []


def _slo_briefs(alert: dict, by_name: dict, orchestras, teams: set,
                auditor_names, closed: set, covered=()) -> dict:
    """#3036 : destinataires d'un `slo_breach`, filtrés par vivacité, avec
    passage au palier suivant quand le palier atteint n'a personne qui lise.

    Palier atteint : les `targets` du moteur SLO. Aucun vivant : on essaie le
    palier suivant de l'échelle §9 (assigné → pilote → bureau/auditeur), puis
    la route humaine (copie au fil du bureau). Le brief dit toujours qui
    manquait (« No live recipient: X (no live lease). ») et le texte du geste
    est celui du palier qui reçoit, pas celui du palier muet.

    #3057 (route voulue) : un lot sans assigné, ou assigné au bureau
    `externe` qui ne lit pas sa boîte, n'a personne au palier 1 ; il part au
    pilote dès le niveau 1, comme un assigné mort. Un palier 1 muet laisserait
    le lot sans lecteur jusqu'au double du délai, et le pilote est celui qui
    peut l'affecter (§9 : assigné → pilote). Le brief nomme le bureau externe
    parmi les absents au lieu de « none designated ».

    #3100 : `covered` (les destinataires déjà remis, `to` du registre des
    briefs) compte comme vivant : le courrier est dans leur boîte, lu au
    retour ; un bail qui cligne (redémarrage d'exécuteur, clôture de release)
    ne fait ni escalader ni nommer absent un palier déjà prévenu."""
    covered = set(covered or ())
    roles = set(alert.get("targets") or ("assignee",))
    text = _slo_breach(alert)
    raw: dict = {}
    for role in roles:
        for name in _target_names(role, alert, by_name, orchestras, teams,
                                  auditor_names):
            raw.setdefault(name, role)
    live, dead = _split_live({name: text for name in raw}, by_name, closed, covered)
    if "assignee" in roles and alert.get("agent") == "bureau" \
            and _bureau_externe(by_name):
        dead.setdefault("bureau", "external desk, reads the %s thread"
                       % vocab.active().noun)
    if live:
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    current = (3 if roles & {"bureau", "auditeur"}
               else 2 if "pilot" in roles else 1)
    for next_level in range(current + 1, 4):
        next_text = _slo_breach(alert, level=next_level)
        if next_level == 2:
            names = _target_names("pilot", alert, by_name, orchestras, teams,
                                  auditor_names)
        else:
            names = (_target_names("bureau", alert, by_name, orchestras, teams,
                                   auditor_names)
                     + _target_names("auditeur", alert, by_name, orchestras, teams,
                                     auditor_names))
        candidates = {name: next_text for name in dict.fromkeys(names)}
        live, more = _split_live(candidates, by_name, closed, covered)
        dead.update(more)
        if live:
            note = _escalation_note(dead, current, next_level)
            return {name: note + body for name, body in live.items()}
        if next_level == 3 and _bureau_externe(by_name):
            # Le bureau externe est la route humaine du dernier palier : sa
            # copie va au fil lisible, sans courrier de plus.
            return {"thread:bureau": _escalation_note(dead, current, 3) + next_text}
    # Plus aucun agent au registre ne peut lire : route humaine (fil du
    # bureau), en nommant les absents. Sans bureau, rien n'est inventé — le
    # passage suivant réessaiera (l'alerte reste dans `ameesh alerts`).
    fallback = _fallback_bureau(by_name, _slo_breach(alert, level=3))
    live, more = _split_live(fallback, by_name, closed, covered)
    dead.update(more)
    if live:
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    if "bureau" in by_name:
        return {"thread:bureau": _dead_note(dead) + _slo_breach(alert, level=3)}
    return {}


def _mail_undeliverable_briefs(alert: dict, by_name: dict, auditor_names,
                               closed: set, dead_out: dict | None = None) -> dict:
    """#3036 : le courrier en souffrance part aux orchestrateurs VIVANTS de
    l'équipe ; à défaut aux auditeurs, puis au bureau, puis au fil lisible.

    Le destinataire mort (`alert["agent"]`) n'est jamais visé ; un
    orchestrateur sur un hôte perdu ne peut pas relivrer, il ne doit pas
    compter comme prévenu. `dead_out` reçoit les écartés (nom → raison) pour
    que le notificateur réessaie à la passe suivante, quand l'un revient."""
    from . import undeliverable
    text = undeliverable.brief_text(alert)
    names = [name for name in alert.get("orchestrators") or ()
             if name != alert.get("agent")]
    live, dead = _split_live({name: text for name in names}, by_name, closed)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    fallback = {name: text for name in auditor_names or () if name in by_name}
    live, more = _split_live(fallback, by_name, closed)
    dead.update(more)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    fallback = _fallback_bureau(by_name, text)
    live, more = _split_live(fallback, by_name, closed)
    dead.update(more)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    if dead_out is not None:
        dead_out.update(dead)
    if "bureau" in by_name:
        return {"thread:bureau": _dead_note(dead) + text}
    return {}


def briefs(alert: dict, listing: list, lot_titles: dict | None = None,
           auditor_names=(), *, orchestrator_names=(),
           closed_hosts=(), dead_out: dict | None = None, covered=()) -> dict:
    """`{agent: texte}` pour une alerte de `BRIEFED_TYPES` (vide sinon).

    Orchestrateurs : `idle_capacity`, ceux de L118 ; `stale_lot`, ceux de
    l'équipe de l'assigné (sinon celle du lot, #917) — ou du projet du lot
    quand aucun relecteur n'est désigné (#705) —, ou **un seul** quand le lot
    n'a pas d'assigné
    (#1026 : sans équipe, le brief partait à tous et deux orchestrateurs
    traitaient le même lot) ; `plan_underused`, ceux des équipes des agents au
    repos ; `estimate_bias` (L157b), ceux de l'équipe de l'estimateur, et
    l'estimateur lui-même s'il est un agent du registre. `module_deviation` :
    rôle explicite et projet, sans repli ; un constat de portée hôte
    (`project` absent, `dev` workspaces) n'a pas de fil de projet et part au
    responsable humain de son hôte ou agent (`notify.Router`), jamais en
    diffusion aux agents. `stale_lot` d'un lot en qa : le relecteur DÉSIGNÉ
    reçoit en outre le verdict qui lui est dû (#705).
    Auditeurs : pour tous les types sauf `stale_lot` (L713) et
    `plan_underused` (#3274) — lus à la passe de l'auditeur, sans réveil ;
    sans aucun orchestrateur vivant, `plan_underused` se replie sur l'auditeur
    (#3036). Un agent à la fois orchestrateur et auditeur reçoit un seul texte.

    #3036 : `closed_hosts` (les hôtes de `claims_closed_hosts`) et le champ
    `lease_live` des lignes du registre écartent les destinataires qui ne
    liront pas ; un `slo_breach` sans vivant au palier passe au palier suivant
    puis au fil du bureau, `mail_undeliverable`/`idle`/`underused` aux
    auditeurs puis au bureau. Le brief nomme les absents ("No live recipient:
    …") pour que les vivants couvrent le geste. `dead_out`, quand l'appelant
    le fournit, reçoit ces absents (nom → raison) : le notificateur garde la
    liste pour réévaluer à la passe suivante et remettre à qui est revenu.
    `covered` (#3100, `slo_breach`) : les destinataires déjà remis, comptés
    vivants pour le palier même si leur bail cligne à cette passe."""
    kind = alert.get("type")
    if kind not in BRIEFED_TYPES:
        return {}
    by_name = {row["name"]: row for row in listing}
    orchestras = list(alert.get("orchestrators") or ())
    closed = set(closed_hosts or ())
    if kind == "module_deviation":
        text = alert.get("action") or alert.get("detail") or "Module procedure deviation"
        project = alert.get("project")
        if not project:
            # Host-scoped finding: no project owns it, so no project thread
            # and no agent brief. `notify.Router` sends it to the responsible
            # human of its host or agent; never broadcast it to every agent.
            return {}
        out = {o: text for o in orchestras
               if o in orchestrator_names and o in by_name
               and _workstream(by_name[o]) == project}
        for name in auditor_names or ():
            if name in by_name and _workstream(by_name[name]) in ("", project):
                out.setdefault(name, text)
        live, dead = _split_live(out, by_name, closed)
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    elif kind == "mail_undeliverable":
        return _mail_undeliverable_briefs(alert, by_name, auditor_names, closed,
                                          dead_out=dead_out)
    elif kind == "idle_actionable_lot":
        out = {}
        audit_text = _idle_actionable_lot(alert)
    elif kind == "idle_capacity":
        out = sous_utilisation.orchestrator_briefs(alert, listing, lot_titles)
        audit_text = _for_auditor(alert)
    elif kind == "slo_breach":
        # #789 t2 (canon §9) : le niveau atteint règle les destinataires
        # (`targets` du moteur SLO) ; l'auditeur n'est briefé qu'au niveau 3,
        # sinon le niveau 1 doublerait `stale_lot` pour du bruit.
        # #3036 : le rôle ne suffit pas — un agent sans bail vivant ou sur un
        # hôte aux claims fermés ne lira pas ; le palier suivant prend le
        # relais, et le brief dit qui manquait.
        teams = _projects(by_name, alert.get("agent"))
        # #3036 : le pilote se résout ici aussi quand l'alerte ne porte pas
        # `orchestrators` (niveau 1 d'une passe sans autre lot de niveau 2) ;
        # le notificateur fournit les rôles vivants du canon.
        return _slo_briefs(alert, by_name, orchestras or orchestrator_names,
                           teams, auditor_names, closed, covered)
    else:
        extra: dict[str, str] = {}
        no_reviewer = False
        if kind == "staging_interrupted":
            text = alert["detail"]
            teams = set()
        elif kind == "stale_lot":
            waiting = alert.get("waiting") or {}
            reviewer = waiting.get("who") if waiting.get("what") == "verdict" else None
            # #705 : en qa, l'attente est celle du relecteur DÉSIGNÉ ; sans
            # désignation, elle n'est à personne et l'orchestrateur du projet
            # doit en désigner un.
            no_reviewer = bool(waiting.get("what") == "verdict" and not reviewer)
            text = _reviewer_missing(alert) if no_reviewer else _stale_lot(alert)
            teams = _projects(by_name, alert.get("agent"))
            if not teams:
                # #917 : sans assigné connu, l'équipe du lot — sinon un lot
                # ameesh réveillerait les orchestrateurs nexlink, et
                # inversement (une alerte reçue pousse à assigner, ce qui
                # entretient la liste des orchestrateurs par historique).
                teams = {(alert.get("lot_team") or "").strip()} - {""}
            # #2076 : plusieurs relecteurs désignés, chacun briefé pour son
            # verdict ; #917 garde l'équipe du lot quand l'assigné est inconnu.
            for name in str(reviewer or "").split(","):
                name = name.strip()
                if name in by_name:
                    extra[name] = _verdict_due(alert)
        elif kind == "estimate_bias":
            text = _estimate_bias(alert)
            teams = _projects(by_name, alert.get("agent"))
            if not teams:
                # #3502 : un estimateur sans équipe connue (hors registre, ou
                # fiche sans projet) ne doit pas réveiller TOUS les
                # orchestrateurs — l'orchestrateur Nexlink recevait ainsi les
                # estimateurs ameesh `claude3`, `bureau`, `inconnu` et
                # `human:smichea`. Le brief va à l'estimateur s'il est au
                # registre, sinon à l'auditeur (repli bureau) : c'est lui qui
                # publie la table et peut corriger le biais à la source.
                orchestras = []
        elif kind == "waiting_without_question":
            text = _waiting_without_question(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind == "critical_budget_pause":
            text = _critical_budget_pause(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind == "account_config_invalid":
            text = _account_config_invalid(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind == "budget_pause_long":
            text = _budget_pause_long(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind == "rate_limited_chronic":
            text = _rate_limited_chronic(alert)
            teams = _projects(by_name, alert.get("agent"))
        elif kind in ("operator_pause_long", "operator_pause_host"):
            text = _operator_pause(alert)
            teams = ({p for n in alert.get("agents") or () for p in _projects(by_name, n)}
                     if kind == "operator_pause_host" else _projects(by_name, alert.get("agent")))
        elif kind in ("host_idle_reserved", "baseline_oversized",
                     "host_release_failed", "host_release_overdue",
                     "host_release_no_runs", "host_release_canon_unreadable"):
            text = alert["detail"]
            teams = set()
        elif kind == "notify_undeliverable":
            # #3007 : aucun notificateur vivant ne couvre l'humain ; tous les
            # orchestrateurs de l'alerte et l'auditeur la reçoivent.
            text = _notify_undeliverable(alert)
            teams = set()
        else:
            text = _plan_underused(alert)
            teams = {p for n in alert.get("idle_agents") or ()
                     for p in _projects(by_name, n)}
        if no_reviewer:
            out = {o: text for o in _of_project(orchestras, by_name,
                                                alert.get("project"), teams)}
        else:
            out = {o: text for o in _of_teams(orchestras, by_name, teams)}
        if kind == "stale_lot" and not alert.get("agent"):
            # #1026 : un lot sans assigné n'a pas d'équipe connue, donc `teams`
            # est vide et `_of_teams` rend tous les orchestrateurs. Deux d'entre
            # eux ont traité le même brief en 60 s (course #894/#895) : un seul
            # destinataire.
            # B1 bureau (15/10) : le vivier est d'abord restreint au PROJET du
            # lot (#705 `_of_project` ; l'alerte porte `project`), sans quoi un
            # lot ameesh peut n'être briefé qu'à un orchestrateur d'un autre
            # projet, qui ne peut ni l'assigner ni le suivre — avant #1026 le
            # brief partait à tous, donc l'orchestrateur du projet le recevait.
            pool = _of_project(list(out), by_name, alert.get("project"), teams)
            chosen = _one_orchestrator(pool, by_name, alert.get("lot"))
            out = {name: out[name] for name in chosen}
        if kind in ("estimate_bias", "waiting_without_question") \
                and alert.get("agent") in by_name:
            out.setdefault(alert["agent"], text)       # l'estimateur, l'assigné lui-même
        out.update(extra)
        audit_text = text
    # L713 : le lot stagnant ne réveille plus l'auditeur par courrier ; il le
    # lit à sa passe (`ameesh alerts --json`). Les orchestrateurs gardent
    # l'avis, et un auditeur qui est aussi orchestrateur de l'équipe le reçoit
    # par cette voie (un seul texte). #3274 : `plan_underused` aussi est lu à
    # la passe de l'auditeur — donner du travail aux agents au repos est le
    # geste des orchestrateurs, pas le sien ; s'il ne reste aucun orchestrateur
    # vivant, l'auditeur prend le relais plus bas (#3036).
    if kind not in ("stale_lot", "plan_underused"):
        for name in auditor_names or ():
            out.setdefault(name, audit_text)
    if kind in ("critical_budget_pause", "budget_pause_long", "rate_limited_chronic",
                "account_config_invalid", "operator_pause_long"):
        out.pop(alert.get("agent"), None)       # en pause : il ne le lirait pas
    if kind == "operator_pause_host":
        for name in alert.get("agents") or ():
            out.pop(name, None)
    # #3036 : un destinataire sans bail vivant (ou sur un hôte aux claims
    # fermés) ne lira pas ; il n'est pas compté prévenu. Le brief dit qui
    # manquait aux destinataires restants. Si plus aucun agent ne lit, la
    # route humaine est le bureau (boîte s'il est un agent normal, fil lisible
    # s'il est `externe`) ; `stale_lot` excepté, qui ne réveille pas
    # l'auditeur (L713) et attend le retour d'un orchestrateur.
    live, dead = _split_live(out, by_name, closed)
    if kind == "plan_underused" and not live:
        # #3274 : plus aucun orchestrateur vivant — l'auditeur prend le relais
        # (#3036), puis le bureau ou le fil par le repli commun. Avec un
        # orchestrateur vivant, le forfait sous-utilisé attend la passe de
        # l'auditeur, qui le lit sans être réveillé.
        relay = {name: audit_text for name in auditor_names or ()}
        live, more = _split_live(relay, by_name, closed)
        dead.update(more)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    if kind == "stale_lot":
        return {}
    fallback = _fallback_bureau(by_name, audit_text)
    live, more = _split_live(fallback, by_name, closed)
    dead.update(more)
    if live:
        if dead_out is not None:
            dead_out.update(dead)
        note = _dead_note(dead)
        return {name: note + body for name, body in live.items()}
    if dead_out is not None:
        dead_out.update(dead)
    if "bureau" in by_name:
        return {"thread:bureau": _dead_note(dead) + audit_text}
    return {}


def reminder(text: str, raised_ts: float, now: float) -> str:
    """Le texte de la relance : le même, précédé de l'âge de l'alerte."""
    return "Reminder, alert still active for %s: %s" % (
        sous_utilisation._duration(max(0.0, now - raised_ts)), text)
