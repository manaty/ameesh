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
4. pour un P1, ou si rien n'est possible sans l'owner, notification à l'owner.

Un lot P1 (priorité 1, « high ») porte le délai court de la colonne
« P1 / bloquant » et notifie l'owner dès le premier dépassement.
"""
from __future__ import annotations

import time
from typing import Iterable, NamedTuple

#: provenance de la table (preuve : canon §9, home#40)
SOURCE = "canon §9 (processus/lots.md, home#40, 2026-10-13)"
#: la source d'une étape : les événements du lot, ou un signal externe
LOT_EVENTS = "lot_events"
EXTERNAL = "external"


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
#: Le niveau 4 n'a pas de seuil propre : un P1 le déclenche, sinon « rien n'est
#: possible sans l'owner » se constate au niveau 3.
OWNER = "owner"


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
    """Les destinataires du niveau atteint ; un P1 y ajoute l'owner (§9.4)."""
    out = list(LEVEL_TARGETS.get(int(level_), ()))
    if critical and OWNER not in out:
        out.append(OWNER)
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
    orchestrateur) l'est dès sa création, sans événement."""
    first = None
    for ev in events:
        note = ev.get("note") or ""
        if note.startswith("assigned to ") and "(before: nobody)" in note:
            at = ev.get("created_ts")
            if at is not None and (first is None or float(at) < first):
                first = float(at)
    if first is not None:
        return first
    if item.get("assignee"):
        return float(item["created_ts"]) if item.get("created_ts") is not None else None
    return None


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
              now: float | None = None) -> list:
    """Les étapes mesurables d'un lot, une ligne par étape applicable.

    `jalons` vient de `progress._work_item_milestones` — le SEUL lecteur des
    jalons de lot (gel, verdict, fusion) ; `events` du journal du lot. Une
    étape terminée est rendue avec `done_ts` et `level` 0 ; une étape en cours
    court jusqu'à `now`. Une étape non mesurable n'est pas inventée : elle est
    absente (la table la déclare, `STEPS`).
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
        if started is not None:
            out.append(_row(step, requested, now=now, critical=critical,
                            done_ts=round(started, 3),
                            elapsed_s=int(max(0.0, started - float(requested)))))
        else:
            # pas encore démarré : le délai court depuis que le lot est prêt
            ready = assigned or requested
            out.append(_row(step, ready, now=now, critical=critical))

    if state == "build":
        step = step_of("build_quiet")
        last = last_event_ts(item, events) or item.get("created_ts")
        if last is not None:
            out.append(_row(step, last, now=now, critical=critical))

    verdict, kind = jalons.get("verdict"), jalons.get("verdict_kind")
    merged = jalons.get("merged")
    if kind == "ok" and verdict is not None:
        step = step_of("verdict_merge")
        if merged is not None:
            out.append(_row(step, verdict, now=now, critical=critical,
                            done_ts=round(float(merged), 3),
                            elapsed_s=int(max(0.0, float(merged) - float(verdict)))))
        elif state not in ("merged", "promoted", "closed"):
            out.append(_row(step, verdict, now=now, critical=critical))

    return out


def breaches(rows: Iterable[dict]) -> list:
    """Les lignes en dépassement (niveau ≥ 1), les plus graves d'abord."""
    out = [r for r in rows if int(r.get("level") or 0) >= 1]
    out.sort(key=lambda r: (-int(r["level"]), -int(r["overdue_s"])))
    return out
