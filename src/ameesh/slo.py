# SPDX-License-Identifier: AGPL-3.0-only
"""SLO internes et escalades automatiques (canon §9, home#40, lot #789).

Source : `processus/lots.md` §9 « Délais internes (SLO) et escalades »
(home#40, fusionné le 2026-10-13), demande de l'owner du 13/10/2026. ameesh
mesure ces délais **à partir des événements des lots**, jamais sur déclaration
d'un agent.

Ce module est le moteur : la table des étapes, le délai applicable (cible, ou
délai court pour un lot P1/bloquant), le niveau d'escalade atteint (1×, 2×, 3×)
et les destinataires de chaque niveau. Les signaux qui ne vivent pas dans les
événements du lot (PR verte, déploiement, vérification du signaleur, file de
CI, pause d'un agent) sont dans la table avec leur source, mais ne sont pas
encore mesurés : `measured` le dit, et la tranche suivante les branchera.

Escalade (§9), à chaque dépassement :

1. au premier dépassement, alerte à l'assigné ;
2. au double du délai, alerte au pilote du projet (orchestrateur), qui
   réassigne ;
3. au triple — ou si le pilote est lui-même en retard — le bureau ou
   l'auditeur réassigne directement à un agent libre ;
4. pour un P1, le bureau traite, avec l'auditeur en copie. **Aucun
   dépassement de délai n'est notifié à l'owner** (décision de l'owner du
   18/10/2026 vers 08:00 : « rien sur les SLO ») : le niveau 4 vise le bureau
   et l'auditeur, jamais l'owner.

Un lot P1 (priorité 1, « high ») porte le délai court de la colonne
« P1 / bloquant » et vise le bureau et l'auditeur dès le premier dépassement ;
aucun dépassement n'est notifié à l'owner.
"""
from __future__ import annotations

import time
from typing import Iterable, NamedTuple

#: provenance de la table (preuve : canon §9, home#40)
SOURCE = "canon §9 (processus/lots.md, home#40, 2026-10-13)"
#: la source d'une étape : les événements du lot, ou un signal externe
LOT_EVENTS = "lot_events"
EXTERNAL = "external"
#: #2217 : une prise en charge (assignation, réassignation, prise `--claim`)
#: ouvre une grâce de ce délai. Un dépassement déjà couru avant la prise n'est
#: pas imputé au nouveau porteur : son escalade est suspendue le temps qu'il
#: prenne la main, sinon une réassignation pour porteur indisponible déclenche
#: aussitôt une nouvelle demande de réassignation, en boucle. Le retard reste
#: mesuré (`elapsed_s`), seule l'escalade est suspendue.
HANDOVER_GRACE_S = 30 * 60.0


class Step(NamedTuple):
    """Une étape du §9 : délai cible, délai P1 (None : pas de colonne P1)."""
    key: str
    label: str
    target_s: float
    critical_s: float | None
    source: str
    measured: bool


#: La table du §9, dans l'ordre du canon. `measured` : ameesh sait la mesurer
#: aujourd'hui depuis les événements du lot ; une étape non mesurée est
#: déclarée ici pour que le trou soit visible, pas silencieux.
#:
#: #2919 : `verdict_merge` porte la ligne « hors paquet » du canon (incident ou
#: déblocage de la CI, §11 bis : 30 min, 15 min en P1). Le chemin normal a une
#: cible dynamique, le p80 réel de la CI du paquet (`lot_steps(..., p80_s=...)`,
#: canon §9, décision owner #103509 option a, home#147) ; une unité d'un paquet
#: ouvert n'a en plus aucune cible d'escalade propre (`packet=True`).
STEPS: tuple = (
    Step("signal_assigned",
         "request or incident reported → lot assigned to an executor",
         3600, 900, LOT_EVENTS, True),
    Step("intake_started",
         "lot ready (intake) → started (build), if a capable agent is free",
         3600, 900, LOT_EVENTS, True),
    Step("build_quiet",
         "lot started with no new event (commit, milestone, note)",
         7200, 3600, LOT_EVENTS, True),
    # #3033 : mesurée depuis le journal du lot. Le canon (§3, §11 bis) place la
    # relecture AVANT la CI : la tête « verte » d'ameesh est la tête gelée, et
    # l'horloge de chaque relecteur part de sa désignation sur elle
    # (`review_designation_activation.pending_reviews`), jusqu'à son verdict.
    Step("green_verdict",
         "frozen head designated → reviewer's verdict delivered",
         7200, 1800, LOT_EVENTS, True),
    # #3288 : la correction d'un avis bloquant. Mesurée depuis le premier avis
    # `blocked` encore en vigueur sur le dernier gel, jusqu'au nouveau gel
    # (`review_designation_activation.due_fixes`, le même calcul que les avis
    # dus de #3033) : un lot en qa avec un avis bloquant n'attend plus un
    # relecteur, il attend la correction de son assigné. 2 h, 1 h en P1 ;
    # escalade §9 : ×1 assigné, ×2 pilote qui réassigne, ×3 bureau/auditeur.
    Step("blocked_freeze",
         "blocking verdict → new freeze (fix)",
         7200, 3600, LOT_EVENTS, True),
    Step("verdict_merge",
         "review GO → merge",
         1800, 900, LOT_EVENTS, True),
    Step("merge_prod",
         "fix merged → active in prod (deployed)",
         86400, 14400, EXTERNAL, False),
    Step("prod_verified",
         "fix deployed → verified by the reporter",
         7200, 3600, EXTERNAL, False),
    Step("ci_queue",
         "CI job queued",
         900, None, EXTERNAL, False),
    Step("agent_paused",
         "critical agent paused (budget, error)",
         600, None, EXTERNAL, False),
)
BY_KEY = {step.key: step for step in STEPS}

#: Ce que chaque niveau d'escalade vise (rôles ; les noms se résolvent dans
#: l'alerte, comme pour `stale_lot`).
LEVEL_TARGETS = {
    1: ("assignee",),
    2: ("pilot",),
    3: ("bureau", "auditeur"),
}
#: #2395 : le niveau 4 n'a pas de seuil propre — un P1 le déclenche. Il vise le
#: bureau et l'auditeur, jamais l'owner (canon §9, décision owner 18/10).
CRITICAL_TARGETS = ("bureau", "auditeur")


def step_of(key: str) -> Step:
    try:
        return BY_KEY[key]
    except KeyError:
        raise ValueError("unknown SLO step: %r" % key)


def target_s(step: Step, *, critical: bool = False) -> float:
    """Le délai applicable : la colonne P1 quand le lot est P1 et qu'elle existe."""
    if critical and step.critical_s is not None:
        return float(step.critical_s)
    return float(step.target_s)


def level(elapsed_s: float, target: float) -> int:
    """Niveau d'escalade atteint : 0 (tenu), 1 (1×), 2 (2×), 3 (3×)."""
    if target <= 0 or elapsed_s < target:
        return 0
    if elapsed_s >= 3 * target:
        return 3
    if elapsed_s >= 2 * target:
        return 2
    return 1


def targets(level_: int, *, critical: bool = False) -> tuple:
    """Les destinataires du niveau atteint ; un P1 y ajoute le bureau et
    l'auditeur (niveau 4, canon §9) — jamais l'owner."""
    out = list(LEVEL_TARGETS.get(int(level_), ()))
    if critical:
        for role in CRITICAL_TARGETS:
            if role not in out:
                out.append(role)
    return tuple(out)


# --------------------------------------------------------------------------
# mesure depuis les événements du lot
# --------------------------------------------------------------------------

def start_ts(events: Iterable[dict]) -> float | None:
    """L'instant mesuré du début du lot : le premier passage en `build`, ou le
    début mesuré (premier tour du porteur, `estimates.START_NOTE`) s'il est
    antérieur (#3162). Un lot dont le porteur travaille déjà reste parfois en
    intake : le début mesuré conclut alors `intake_started`. `None` sans
    aucun de ces faits."""
    from . import estimates

    stamps = [float(ev["created_ts"]) for ev in events
              if ev.get("created_ts") is not None
              and (ev.get("state") == "build"
                   or (ev.get("note") or "").startswith(estimates.START_NOTE))]
    return min(stamps) if stamps else None


def begun(events: Iterable[dict], jalons: dict | None = None) -> bool:
    """Le lot porte une preuve de début sans date de début (#3162) : une
    branche associée (`work.set_branch`, `attach_implicit_branch` : note
    `branch … (before: …)`, hors détachement) ou un gel (`jalons["frozen"]`).
    Il n'est plus « prêt, pas démarré » — aucune alerte —, mais l'instant de
    son début reste inconnu : aucune durée n'est inventée (#1982)."""
    from . import work

    if (jalons or {}).get("frozen") is not None:
        return True
    for ev in events:
        note = ev.get("note") or ""
        if (note.startswith("branch ") and " (before: " in note
                and not note.startswith(work.DETACH_NOTE)):
            return True
    return False


def assigned_ts(item: dict, events: Iterable[dict]) -> float | None:
    """L'instant où le lot a un exécutant.

    L'attribution gardée (`assigned to X (before: nobody)`) est un événement ;
    un lot créé déjà assigné (`work add --assignee`, `--new-lot` d'un
    orchestrateur) l'est dès sa création, sans événement. #2217 : une prise
    automatique de la file (`backlog.AUTO_TAKE_NOTE`) ou de l'équipe
    (`backlog.LOT_TAKE_NOTE`) est aussi une première attribution — elle ne
    s'applique qu'à un lot sans preneur. Sans elle, un lot pris par la file
    restait « jamais assigné » : `signal_assigned` courait depuis sa création
    et l'alerte demandait de le réassigner juste après sa prise (#660)."""
    from . import backlog

    first = None
    for ev in events:
        note = ev.get("note") or ""
        if note.startswith("assigned to "):
            if "(before: nobody)" not in note:
                continue
        elif not note.startswith((backlog.LOT_TAKE_NOTE, backlog.AUTO_TAKE_NOTE)):
            continue
        at = ev.get("created_ts")
        if at is not None and (first is None or float(at) < first):
            first = float(at)
    if first is not None:
        return first
    if item.get("assignee"):
        return float(item["created_ts"]) if item.get("created_ts") is not None else None
    return None


def handover_prefixes() -> tuple:
    """Les débuts de note qui confient le lot à un porteur (#2217).

    Une seule source pour les attributions : `handoff.assign_prefixes`
    (assignation, prise automatique de la file ou de l'équipe, reprise d'un
    lot en pause budget), moins le rendu à la file (`RETURN_NOTE` : le lot
    n'a plus de porteur, il n'y a pas de prise à dater) ; la délégation
    (`delegated to …`) transfère aussi le porteur."""
    from . import handoff

    return tuple(p for p in handoff.assign_prefixes()
                 if p != handoff.RETURN_NOTE) + ("delegated to ",)


def handover_ts(item: dict, events: Iterable[dict]) -> float | None:
    """L'instant de la dernière prise en charge du lot (#2217).

    Toutes les notes d'attribution comptent (`handover_prefixes`) :
    `work assign`/`--claim`, prise automatique de la file ou de l'équipe,
    reprise après une pause budget, délégation. `None` quand le lot n'a pas
    de porteur courant (rendu à la file) ou quand le journal n'en porte
    aucune (lot créé déjà assigné par `work add --assignee`) : il n'y a alors
    pas de prise à dater, donc pas de grâce à ouvrir."""
    if not str(item.get("assignee") or "").strip():
        return None
    prefixes = handover_prefixes()
    last = None
    for ev in events:
        note = ev.get("note") or ""
        if not note.startswith(prefixes):
            continue
        at = ev.get("created_ts")
        if at is not None and (last is None or float(at) > last):
            last = float(at)
    return last


#: #2916 : les états de retenue (`work.WAITING`), où le lot n'attend pas un
#: exécutant mais la levée de la retenue. Le délai intake ne court pas
#: pendant une retenue.
HOLDS = ("blocked", "waiting_human")


def hold_release_ts(events: Iterable[dict], *,
                    started: float | None = None) -> float | None:
    """Le dernier instant où le lot est sorti d'une retenue (#2916).

    Un lot `blocked` ou `waiting_human` n'attend pas un preneur : il attend la
    levée de sa retenue (déblocage, décision de l'owner). Le temps passé sous
    retenue n'est donc pas un retard intake ; au retour en intake, l'horloge
    repart de ce retour, comme de la levée d'une dépendance (#2551).

    Les transitions sont lues dans l'ordre d'écriture `(created_ts, id)` : une
    note qui répète l'état n'est pas une transition. `None` sans retenue. Un
    lot déjà démarré garde sa mesure : seules les levées antérieures au début
    comptent, une retenue ajoutée après le début ne réécrit pas le passé.
    """
    ordered = sorted(events or (),
                     key=lambda ev: (float(ev.get("created_ts") or 0.0),
                                     int(ev.get("id") or 0)))
    released = None
    previous = None
    for event in ordered:
        state = event.get("state")
        at = event.get("created_ts")
        if previous in HOLDS and state not in HOLDS and at is not None:
            stamp = float(at)
            # #2921 : une levée postérieure au premier build ne réécrit pas la
            # mesure acquise — on garde la dernière levée antérieure au début.
            if started is None or stamp <= float(started):
                released = stamp
        previous = state
    return released


def intake_ready_ts(assigned: float | None, requested: float | None,
                    dependencies: Iterable[dict] = (), *,
                    started: float | None = None) -> float | None:
    """L'instant où le lot est prêt pour `intake_started` (#2551).

    Une dépendance bloquante ouverte retient le lot : son porteur ne peut rien
    faire avant sa levée, donc le lot n'est pas « prêt, pas démarré » et aucun
    délai ne court (`None`). Toutes les dépendances satisfaites, le départ est
    le plus tardif de l'attribution (ou de la demande) et de la levée du
    dernier bloqueur — les jalons `merged` ou `closed` du bloqueur, portés par
    `satisfied_ts`. Le délai mesure alors l'attente d'un preneur, pas le temps
    où le travail dépendait d'un autre lot.

    Un lot déjà démarré (`started`) garde sa mesure : seules les levées
    antérieures au début comptent, et une dépendance ajoutée après le début ne
    réécrit pas le passé.
    """
    base = assigned if assigned is not None else requested
    if base is None:
        return None
    edges = list(dependencies or ())
    if started is None and any(edge.get("blocking") for edge in edges):
        return None
    stamps = [float(edge["satisfied_ts"]) for edge in edges
              if edge.get("satisfied_ts") is not None]
    if started is not None:
        stamps = [stamp for stamp in stamps if stamp <= float(started)]
    return max([float(base)] + stamps)


def last_event_ts(item: dict, events: Iterable[dict]) -> float | None:
    """Dernière activité écrite (ligne du lot ou événement)."""
    stamps = [item.get("updated_ts"), item.get("created_ts")]
    stamps += [ev.get("created_ts") for ev in events]
    stamps = [float(s) for s in stamps if s is not None]
    return max(stamps) if stamps else None


def _row(step: Step, start, *, now: float, critical: bool, done_ts=None,
         target: float | None = None, escalate: bool = True, **extra) -> dict:
    """Une ligne de mesure ; `target` remplace la cible de la table (p80 d'un
    paquet) et `escalate=False` laisse la ligne mesurée mais sans escalade
    propre (l'unité d'un paquet ouvert, #2919)."""
    start = float(start)
    # une étape terminée ne court plus : son délai est acquis, quel qu'il soit
    end = float(done_ts) if done_ts is not None else float(now)
    elapsed = max(0.0, end - start)
    target = target_s(step, critical=critical) if target is None else float(target)
    lvl = 0 if (done_ts is not None or not escalate) else level(elapsed, target)
    row = {
        "step": step.key,
        "label": step.label,
        "source": step.source,
        "measured": step.measured,
        "started_ts": round(start, 3),
        "elapsed_s": int(elapsed),
        "target_s": int(target),
        "critical": bool(critical),
        "level": lvl,
        "overdue_s": int(elapsed - target) if lvl else 0,
        "targets": targets(lvl, critical=critical) if lvl else (),
    }
    if done_ts is not None:
        row["done_ts"] = round(float(done_ts), 3)
    if not escalate:
        row["escalation"] = False
    row.update(extra)
    return row


def lot_steps(item: dict, jalons: dict, events: Iterable[dict], *,
              now: float | None = None, packet_slot: float | None = None,
              dependencies: Iterable[dict] = (), packet: bool = False,
              p80_s: float | None = None, outside_packet: bool = False,
              ci_green_ts: float | None = None,
              hold_releases: Iterable[float] = (),
              reviews: Iterable[dict] = (),
              fixes: Iterable[dict] = ()) -> list:
    """Les étapes mesurables d'un lot, une ligne par étape applicable.

    `jalons` vient de `progress._work_item_milestones` — le SEUL lecteur des
    jalons de lot (gel, verdict, fusion) ; `events` du journal du lot. Une
    étape terminée est rendue avec `done_ts` et `level` 0 ; une étape en cours
    court jusqu'à `now`. Une étape non mesurable n'est pas inventée : elle est
    absente (la table la déclare, `STEPS`).

    #2335/A2 : `packet_slot` (premier créneau de paquet après le GO) décale le
    départ de `verdict_merge` en cours de route. Le canon §11 bis ne permet pas
    de fusionner un lot hors paquet : les 30 premières minutes après le GO ne
    sont pas un retard. La ligne porte `packet_slot` pour que l'alerte dise
    d'où part la mesure ; une fusion déjà faite garde sa durée réelle.

    #3341 : hors paquet (exception `incident|unblock` déclarée), la fusion
    attend aussi la CI verte sur la tête gelée exacte. `ci_green_ts` est
    l'instant où cette CI est devenue verte (lu par l'appelant dans `ci_runs`,
    jamais supposé ici) ; le départ est `max(GO, ci_green_ts)`. Une CI rouge,
    encore en cours, annulée ou absente (pas de vert) ne fait pas partir
    l'horloge : aucune ligne `verdict_merge`, donc aucune alerte — la mesure
    ne pointe une attente que le porteur peut faire avancer. Une fusion déjà
    faite garde sa durée réelle. Le chemin paquet garde son départ
    (`packet_slot`/p80) : `ci_green_ts` ne le change pas.

    #2562 : le délai intake part au plus tôt du début planifié (minuit
    local). Avant cette date, aucune ligne intake en cours ne court.

    #2217 : quand le lot vient d'être pris en charge (dernière ligne
    `assigned to … (before: …)` il y a moins de `HANDOVER_GRACE_S`), les
    dépassements déjà courus ne sont pas imputés au nouveau porteur : leur
    escalade est suspendue (`level` 0, `targets` vides, `overdue_s` 0) et la
    ligne porte `handover_grace_s` (secondes restantes). La mesure reste
    visible (`elapsed_s`, `target_s`) ; après la grâce, l'escalade reprend au
    niveau que le retard justifie.

    #2919 : `p80_s`, quand il est mesuré, remplace la cible de la table pour
    « GO → fusion » sur le **chemin normal** (tout lot à GO courant sans
    exception). C'est le p80 réel de la CI du paquet (`progress.ci_suite`,
    fenêtre de 7 jours). Sans mesure, le repli est la cible de la ligne
    paquet du canon (30 min, colonne P1 « — »), que le lot soit dans un
    paquet ouvert ou non. `p80_s=None` ne distingue donc pas une exception
    déclarée : l'appelant passe `outside_packet=True` pour elle, et la cible
    de la table reprend sa colonne P1 (15 min) et son escalade depuis le GO.
    Les seuils ×2/×3 s'appliquent à la cible retenue. `packet=True` marque en
    plus une unité d'un paquet **ouvert** (attente `packet` de
    `planned_waits`) : elle n'a **aucune cible d'escalade propre** — `level`
    0, `targets` vides, `overdue_s` 0 —, le composeur du paquet ou le pilote
    porte la fusion (canon §9, décision owner #103509 option a, home#147). La
    mesure reste lisible (`elapsed_s`, `target_s`, `packet`, `target_source`)
    pour le taux de respect.

    #2551 : `dependencies` sont les arêtes de prérequis du lot
    (`work_dependencies.grouped`, enrichies de `satisfied_ts` — fusion ou
    clôture du bloqueur — par l'appelant). Une dépendance bloquante ouverte ne
    produit pas de ligne `intake_started` ; toutes satisfaites, le départ est
    la levée du dernier bloqueur (`intake_ready_ts`).

    #2916 : une retenue (`blocked`, `waiting_human`) suspend le délai intake,
    comme une dépendance ouverte : au retour en intake, l'horloge repart de
    la dernière sortie de retenue (`hold_release_ts`), pas de l'attribution
    d'origine. Sinon une décision de l'owner rendue après deux jours faisait
    crier « réattribuer maintenant » sur un lot que son porteur reprenait.
    #2916/#1225 : `hold_releases` porte les levées des holds explicites de
    pilote (`hold:`/`unhold:`, #1664), autorisées par l'appelant
    (`planned_waits.pilot_hold_releases`) — un cas réel nexlink. Le temps
    parqué sous un hold de pilote n'est pas un retard du porteur.

    #3033 : `reviews` sont les avis encore dus sur ce lot
    (`review_designation_activation.pending_reviews` : `name`,
    `designated_ts`). Chacun donne une ligne `green_verdict` en cours, au nom
    de son relecteur (`reviewer`) : l'escalade vise le relecteur au premier
    dépassement, puis le pilote, qui le relance ou en désigne un autre. Un
    verdict rendu sur la tête gelée retire l'avis de la lecture, donc la
    ligne ; un nouveau gel le remet, depuis la nouvelle désignation. La grâce
    de prise en charge (#2217) ne s'y applique pas : elle protège le porteur
    du lot, pas un relecteur.

    #3288 : `fixes` sont les corrections dues après un avis bloquant
    (`review_designation_activation.due_fixes` : `name` (le débiteur,
    l'assigné), `blocked_ts`, `blocked_by`, `sha`). Chacune donne une ligne
    `blocked_freeze` en cours, depuis le **premier** avis bloquant encore en
    vigueur sur le dernier gel : l'escalade §9 vise l'assigné, puis le pilote
    qui réassigne, puis le bureau/l'auditeur. La grâce de prise en charge
    s'applique ici en revanche : le débiteur est bien le porteur du lot, et
    une réassignation ne doit pas déclencher une nouvelle demande de
    réassignation dans la minute. Une correction livrée (nouveau gel) ou
    annulée par un verdict inverse du même relecteur retire la ligne.
    """
    events = list(events)
    now = time.time() if now is None else float(now)
    critical = int(item.get("priority") or 0) == 1
    state = item.get("state")
    out = []

    requested = jalons.get("requested") or item.get("created_ts")
    assigned = assigned_ts(item, events)
    if requested is not None:
        step = step_of("signal_assigned")
        if assigned is not None:
            out.append(_row(step, requested, now=now, critical=critical,
                            done_ts=round(assigned, 3),
                            elapsed_s=int(max(0.0, assigned - float(requested)))))
        else:
            out.append(_row(step, requested, now=now, critical=critical))

    # #3162 : le début mesuré conclut l'étape comme le passage en build, même
    # quand le lot est resté en intake.
    started = start_ts(events)
    if requested is not None and state not in ("merged", "promoted", "closed"):
        step = step_of("intake_started")
        ready = intake_ready_ts(assigned, requested, dependencies, started=started)
        # #2916 : le temps passé sous retenue n'est pas un retard intake ; au
        # retour, l'horloge repart de la levée (dernière sortie de retenue).
        # #1225 : les holds explicites de pilote (#1664) comptent aussi ; leurs
        # levées sont autorisées par l'appelant et filtrées par `started` ici.
        released = hold_release_ts(events, started=started)
        for stamp in hold_releases or ():
            value = float(stamp)
            if started is not None and value > float(started):
                continue
            if released is None or value > float(released):
                released = value
        if ready is not None and released is not None:
            ready = max(float(ready), released)
        # #2562 : un report planifié ne constitue pas un retard intake.
        from .plan import day_start_ts
        planned = day_start_ts(item.get("planned_start"))
        if ready is not None and planned is not None:
            ready = max(float(ready), planned)
        if ready is None:
            # #2551 : dépendance bloquante ouverte — le lot n'est pas prêt,
            # aucun délai ne court : son porteur ne peut rien faire avant la
            # levée du prérequis. La mesure repart de cette levée.
            pass
        elif started is not None:
            out.append(_row(step, ready, now=now, critical=critical,
                            done_ts=round(started, 3),
                            elapsed_s=int(max(0.0, started - float(ready)))))
        elif begun(events, jalons):
            # #3162 : branche associée ou gel — le lot est commencé, mais son
            # début n'est pas daté : ni retard ni durée inventés (#1982).
            pass
        elif state == "intake" and now >= float(ready):
            # prêt, pas encore démarré : le délai court depuis que le lot est
            # prêt. Passé intake sans événement `build` (#1982), la phase est
            # franchie mais non mesurable : aucune ligne, donc aucun retard
            # inventé — seules l'origine réelle et les durées mesurées restent.
            out.append(_row(step, ready, now=now, critical=critical))

    if state == "build":
        step = step_of("build_quiet")
        last = last_event_ts(item, events) or item.get("created_ts")
        if last is not None:
            out.append(_row(step, last, now=now, critical=critical))

    verdict, kind = jalons.get("verdict"), jalons.get("verdict_kind")
    merged = jalons.get("merged")
    if kind == "ok" and verdict is not None and jalons.get("verdict_current", True):
        step = step_of("verdict_merge")
        # #2919 : le p80 mesuré remplace la cible de la table sur le chemin
        # normal (escalade ×2/×3 relative à lui). Sans mesure, le repli est la
        # cible de la ligne paquet (30 min, colonne P1 « — ») ; une exception
        # déclarée (`outside_packet`) garde la cible de la table, colonne P1
        # comprise (15 min pour un P1), depuis le GO.
        p80 = None
        if not outside_packet and p80_s is not None:
            try:
                candidate = float(p80_s)
            except (TypeError, ValueError):
                candidate = 0.0
            if candidate > 0:
                p80 = candidate
        target = p80
        if target is None and not outside_packet:
            target = float(step.target_s)
        extra = {}
        if p80 is not None:
            extra["target_source"] = "p80"
        elif outside_packet:
            extra["target_source"] = "table"
        else:
            extra["target_source"] = "fallback"
        if packet:
            extra["packet"] = True
        if merged is not None:
            out.append(_row(step, verdict, now=now, critical=critical,
                            done_ts=round(float(merged), 3),
                            elapsed_s=int(max(0.0, float(merged) - float(verdict))),
                            target=target, escalate=not packet, **extra))
        elif state not in ("merged", "promoted", "closed"):
            start = float(verdict)
            if outside_packet:
                # #3341 : hors paquet, la CI de la tête gelée doit être verte
                # avant que la fusion puisse avancer. Sans vert (absente,
                # encore en cours, annulée ou rouge), l'horloge ne part pas et
                # aucune ligne n'est produite : l'attente n'est pas imputable
                # au porteur. Avec un vert, le départ est max(GO, vert).
                if ci_green_ts is None:
                    pass
                else:
                    green = float(ci_green_ts)
                    start = max(start, green)
                    extra["ci_green_ts"] = round(green, 3)
                    out.append(_row(step, start, now=now, critical=critical,
                                    target=target, escalate=not packet, **extra))
            else:
                if packet_slot is not None and float(packet_slot) > start:
                    # #2335/A2 : la fusion ne peut entrer qu'au prochain paquet ;
                    # l'horloge GO → fusion part de ce créneau, pas du GO.
                    start = float(packet_slot)
                    extra["packet_slot"] = round(start, 3)
                out.append(_row(step, start, now=now, critical=critical,
                                target=target, escalate=not packet, **extra))

    if state not in ("merged", "promoted", "closed"):
        step = step_of("green_verdict")
        for review in reviews or ():
            name = str(review.get("name") or "").strip()
            since = review.get("designated_ts")
            if not name or since is None:
                continue
            out.append(_row(step, since, now=now, critical=critical, reviewer=name))

    if state not in ("merged", "promoted", "closed"):
        step = step_of("blocked_freeze")
        for fix in fixes or ():
            since = fix.get("blocked_ts")
            if since is None:
                continue
            # #3288 : le débiteur est l'assigné ; `blocked_by` et `sha` servent
            # le texte de l'alerte, pas la cible de l'escalade.
            out.append(_row(step, since, now=now, critical=critical,
                            blocked_by=str(fix.get("blocked_by") or ""),
                            sha=str(fix.get("sha") or "")))

    handover = handover_ts(item, events)
    if handover is not None and state not in ("merged", "promoted", "closed"):
        remaining = HANDOVER_GRACE_S - (now - handover)
        if 0 < remaining <= HANDOVER_GRACE_S:
            for row in out:
                if int(row.get("level") or 0) < 1 or row.get("reviewer"):
                    continue
                # #2217 : le retard a couru avant la prise en charge ; le
                # nouveau porteur n'a pas à recevoir une demande de
                # réassignation aussitôt. La mesure (elapsed_s, target_s)
                # reste lisible, l'escalade reprend après la grâce.
                row["handover_grace_s"] = int(remaining)
                row["level"] = 0
                row["targets"] = ()
                row["overdue_s"] = 0

    return out


def breaches(rows: Iterable[dict]) -> list:
    """Les lignes en dépassement (niveau ≥ 1), les plus graves d'abord."""
    out = [r for r in rows if int(r.get("level") or 0) >= 1]
    out.sort(key=lambda r: (-int(r["level"]), -int(r["overdue_s"])))
    return out
