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
    Step("green_verdict",
         "green PR → review verdict delivered",
         7200, 1800, EXTERNAL, False),
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

def _first_event(events: Iterable[dict], state: str) -> float | None:
    """Le premier instant où le lot est vu dans cet état (transition ou note)."""
    stamps = [ev.get("created_ts") for ev in events if ev.get("state") == state]
    stamps = [float(s) for s in stamps if s is not None]
    return min(stamps) if stamps else None


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


def _row(step: Step, start, *, now: float, critical: bool, done_ts=None, **extra) -> dict:
    start = float(start)
    # une étape terminée ne court plus : son délai est acquis, quel qu'il soit
    end = float(done_ts) if done_ts is not None else float(now)
    elapsed = max(0.0, end - start)
    target = target_s(step, critical=critical)
    lvl = 0 if done_ts is not None else level(elapsed, target)
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
    row.update(extra)
    return row


def lot_steps(item: dict, jalons: dict, events: Iterable[dict], *,
              now: float | None = None, packet_slot: float | None = None,
              dependencies: Iterable[dict] = ()) -> list:
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

    #2217 : quand le lot vient d'être pris en charge (dernière ligne
    `assigned to … (before: …)` il y a moins de `HANDOVER_GRACE_S`), les
    dépassements déjà courus ne sont pas imputés au nouveau porteur : leur
    escalade est suspendue (`level` 0, `targets` vides, `overdue_s` 0) et la
    ligne porte `handover_grace_s` (secondes restantes). La mesure reste
    visible (`elapsed_s`, `target_s`) ; après la grâce, l'escalade reprend au
    niveau que le retard justifie.

    #2551 : `dependencies` sont les arêtes de prérequis du lot
    (`work_dependencies.grouped`, enrichies de `satisfied_ts` — fusion ou
    clôture du bloqueur — par l'appelant). Une dépendance bloquante ouverte ne
    produit pas de ligne `intake_started` ; toutes satisfaites, le départ est
    la levée du dernier bloqueur (`intake_ready_ts`).
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

    started = _first_event(events, "build")
    if requested is not None and state not in ("merged", "promoted", "closed"):
        step = step_of("intake_started")
        ready = intake_ready_ts(assigned, requested, dependencies, started=started)
        if ready is None:
            # #2551 : dépendance bloquante ouverte — le lot n'est pas prêt,
            # aucun délai ne court : son porteur ne peut rien faire avant la
            # levée du prérequis. La mesure repart de cette levée.
            pass
        elif started is not None:
            out.append(_row(step, ready, now=now, critical=critical,
                            done_ts=round(started, 3),
                            elapsed_s=int(max(0.0, started - float(ready)))))
        elif state == "intake":
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
        if merged is not None:
            out.append(_row(step, verdict, now=now, critical=critical,
                            done_ts=round(float(merged), 3),
                            elapsed_s=int(max(0.0, float(merged) - float(verdict)))))
        elif state not in ("merged", "promoted", "closed"):
            start = float(verdict)
            extra = {}
            if packet_slot is not None and float(packet_slot) > start:
                # #2335/A2 : la fusion ne peut entrer qu'au prochain paquet ;
                # l'horloge GO → fusion part de ce créneau, pas du GO.
                start = float(packet_slot)
                extra["packet_slot"] = round(start, 3)
            out.append(_row(step, start, now=now, critical=critical, **extra))

    handover = handover_ts(item, events)
    if handover is not None and state not in ("merged", "promoted", "closed"):
        remaining = HANDOVER_GRACE_S - (now - handover)
        if 0 < remaining <= HANDOVER_GRACE_S:
            for row in out:
                if int(row.get("level") or 0) < 1:
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
