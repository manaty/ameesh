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

from ameesh import work_lifecycle as lifecycle

import re
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
#: #3487 : une exception déclarée pour un lot réellement porté par un paquet
#: suit la cadence de ce paquet (créneau #2335 + p80), pas sa CI propre : le
#: lot se fusionne avec le paquet, et le geste nomme le paquet et sa CI.
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
    # escalade §9 : ×1 assigné, ×2 pilote, ×3 bureau/auditeur. #3487 : la même
    # étape mesure le correctif d'un lot revenu en build après son GO ; le
    # porteur porte le correctif, et le niveau 3 ne prescrit pas de le
    # réassigner (la réassignation perdrait son contexte).
    Step("blocked_freeze",
         "fix owed after a review → new freeze",
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

#: #4002 : la note que `packet_link.record` écrit sur le porteur à chaque
#: composition enregistrée (test de concordance dans test_789_slo).
PACKET_COMPOSITION_NOTE = "packet composition recorded: "


def start_ts(events: Iterable[dict]) -> float | None:
    """L'instant mesuré du début du lot : le premier passage en `build`, ou le
    début mesuré (premier tour du porteur, `estimates.START_NOTE`) s'il est
    antérieur (#3162). Un lot dont le porteur travaille déjà reste parfois en
    intake : le début mesuré conclut alors `intake_started`. #4002 : un
    porteur de paquet reste en intake jusqu'au gel, la composition en cours ;
    sa première composition enregistrée (`PACKET_COMPOSITION_NOTE`) est
    son début. Sans aucune composition, l'alerte de démarrage reste due.
    `None` sans aucun de ces faits."""
    from . import estimates

    starts = (estimates.START_NOTE, PACKET_COMPOSITION_NOTE)
    stamps = [float(ev["created_ts"]) for ev in events
              if ev.get("created_ts") is not None
              and (ev.get("state") == lifecycle.IN_PROGRESS
                   or (ev.get("note") or "").startswith(starts))]
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


#: #4704 : la mention de la note de création d'un lot né réservé à un
#: exécutant externe (`work add --external-executor`), distincte de la note de
#: déclaration tardive (`storage.interface.EXTERNAL_EXECUTOR_NOTE`).
RESERVED_AT_CREATION = "reserved to external executor "


def _external_executor_ts(item: dict, events: Iterable[dict]) -> float | None:
    """#4704 : l'instant où un lot a un exécutant externe (bureau, owner), ou
    None s'il n'en a jamais porté.

    Un lot réservé n'a pas d'`assignee` : sans cette lecture,
    `signal_assigned` (« signalé → assigné ») courait depuis sa création et
    escaladait indéfiniment, même en build (#4696, alerte SLO #166099). La
    date est celle de la déclaration (`work.external-executor`, note
    `EXTERNAL_EXECUTOR_NOTE`) ; un lot né réservé porte la mention dans sa note
    de création, donc la création le date. Une réservation lue seulement sur le
    champ courant, sans trace datée au journal, est bornée par le passage en
    build ou le début mesuré : un lot que son exécutant a commencé a un
    exécutant. La création reste le dernier repli."""
    from .storage.interface import EXTERNAL_EXECUTOR_NOTE

    events = list(events)
    reserved = None
    created_reserved = False
    for ev in events:
        note = ev.get("note") or ""
        if note.startswith(EXTERNAL_EXECUTOR_NOTE):
            at = ev.get("created_ts")
            stamp = None if at is None else float(at)
            if stamp is not None and (reserved is None or stamp < reserved):
                reserved = stamp
        elif note.startswith("created") and RESERVED_AT_CREATION in note:
            created_reserved = True
    current = bool(str(item.get("external_executor") or "").strip())
    if not (current or created_reserved or reserved is not None):
        return None
    started = start_ts(events)
    if created_reserved:
        base = item.get("created_ts")
    elif reserved is not None:
        base = reserved
    else:
        base = started if started is not None else item.get("created_ts")
    if base is None:
        return None
    base = float(base)
    # « au plus tard » : le passage en build ou le début mesuré borne une
    # réservation déclarée après coup (l'exécutant travaillait déjà).
    if started is not None and float(started) < base:
        return float(started)
    return base


def assigned_ts(item: dict, events: Iterable[dict]) -> float | None:
    """L'instant où le lot a un exécutant.

    L'attribution gardée (`assigned to X (before: nobody)`) est un événement ;
    un lot créé déjà assigné (`work add --assignee`, `--new-lot` d'un
    orchestrateur) l'est dès sa création, sans événement. #2217 : une prise
    automatique de la file (`backlog.AUTO_TAKE_NOTE`) ou de l'équipe
    (`backlog.LOT_TAKE_NOTE`) est aussi une première attribution — elle ne
    s'applique qu'à un lot sans preneur. Sans elle, un lot pris par la file
    restait « jamais assigné » : `signal_assigned` courait depuis sa création
    et l'alerte demandait de le réassigner juste après sa prise (#660).

    #4704 : une réservation à un exécutant externe (`external_executor`, #3447)
    est une attribution elle aussi — le lot n'aura jamais d'`assignee`.
    `_external_executor_ts` en donne la date (déclaration, création, à défaut
    build/début mesuré) ; la plus ancienne preuve gagne, comme pour une
    réassignation.

    #3424 : la première attribution reste acquise quand le lot est rendu à la
    file. Le retour (`work.RETURN_NOTE`, `handoff.RETURN_NOTE`) et le début
    mesuré (`estimates.START_NOTE`) ne sont écrits que pour un lot qui a eu un
    porteur ; une réassignation qui nomme le porteur remplacé (`before: Y`
    avec Y non vide) le prouve aussi. Une attribution datée qui suit une de
    ces preuves est une reprise, pas la première : la création reste la date
    de l'étape. Le journal garde donc le fait que le lot a été attribué, même
    si `assignee` est vide aujourd'hui : un lot créé déjà assigné puis rendu
    ne redevient plus « jamais assigné » et `signal_assigned`, déjà conclu,
    ne rouvre pas l'alerte de niveau 3 « reassign it now » (#3223)."""
    from . import backlog, estimates, handoff, work

    events = list(events)
    #: notes qu'un lot sans porteur ne peut pas avoir au journal : elles
    #: prouvent qu'un exécutant a tenu le lot, sans dater sa première
    #: attribution.
    held_notes = (work.RETURN_NOTE, handoff.RETURN_NOTE, estimates.START_NOTE)
    first = None
    held = False
    held_at = None
    for ev in events:
        note = ev.get("note") or ""
        at = ev.get("created_ts")
        stamp = None if at is None else float(at)
        if note.startswith(held_notes):
            held = True
            if stamp is not None and (held_at is None or stamp < held_at):
                held_at = stamp
            continue
        if note.startswith(("assigned to ", "assigné à ")):
            previous = ""
            if "(before: " in note:
                previous = note.split("(before: ", 1)[1].split(")", 1)[0].strip()
            if previous and previous != "nobody":
                # #3424 : la réassignation nomme le porteur qu'elle remplace.
                held = True
                if stamp is not None and (held_at is None or stamp < held_at):
                    held_at = stamp
            if previous != "nobody":
                # un marqueur absent ou une réassignation ne datent pas le
                # début de l'étape : seule `(before: nobody)` est une première
                # attribution
                continue
        elif not note.startswith((backlog.LOT_TAKE_NOTE, backlog.AUTO_TAKE_NOTE)):
            continue
        if stamp is not None and (first is None or stamp < first):
            first = stamp
    # #3424 : une reprise datée APRÈS un rendu (ou un début mesuré) n'est pas
    # la première attribution — le lot avait déjà un porteur à sa création.
    stamps = []
    if first is not None and not (held_at is not None and held_at < first):
        stamps.append(first)
    # #4704 : la réservation externe date l'exécutant ; si le lot a ensuite
    # été assigné à un agent (réservation levée), la plus ancienne preuve
    # garde la première attribution (#3424).
    external = _external_executor_ts(item, events)
    if external is not None:
        stamps.append(external)
    if stamps:
        return min(stamps)
    if item.get("assignee") or held:
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


#: #4369 : marque ajoutée à la note de composition du porteur
#: (`PACKET_COMPOSITION_NOTE`) quand la recomposition retire des unités
#: (`WorkItems.record_packet`) : « ; removed: #12, #34 ».
VEHICLE_EXIT_MARK = "; removed: "


def vehicle_exits(events: Iterable[dict]) -> dict[int, float]:
    """`{lot: dernier instant de sortie d'un véhicule}` (#4369).

    Membre d'une composition enregistrée et ouverte, le lot est à l'étape
    « véhicule » : son retard est celui du paquet, l'unité n'alerte pas
    (#2919). Retiré par une recomposition, il revient à `verdict_merge` ; le
    temps passé dans le véhicule n'est pas le sien, l'horloge repart de la
    sortie. `events` : journaux de porteurs ; seules leurs notes de
    composition portant la marque comptent."""
    out: dict[int, float] = {}
    for ev in events or ():
        note = str(ev.get("note") or "")
        at = ev.get("created_ts")
        if at is None or not note.startswith(PACKET_COMPOSITION_NOTE):
            continue
        _, mark, tail = note.partition(VEHICLE_EXIT_MARK)
        if not mark:
            continue
        for word in tail.replace(",", " ").split():
            if word.startswith("#") and word[1:].isdigit():
                lot = int(word[1:])
                out[lot] = max(out.get(lot, float("-inf")), float(at))
    return out


def last_hold_release(events: Iterable[dict],
                      hold_releases: Iterable[float] = ()) -> float | None:
    """La dernière levée d'une retenue, d'état ou de pilote (#3555).

    Une retenue d'état (`blocked`, `waiting_human`) est datée par
    `hold_release_ts` (#2916) ; un hold explicite de pilote (#1664) par les
    levées autorisées que l'appelant passe (`planned_waits.pilot_hold_releases`).
    Les deux suspendent les étapes d'après le gel : le temps parqué n'est pas
    un retard du porteur, l'horloge repart de la dernière levée. `None` sans
    retenue levée."""
    lifted = hold_release_ts(events)
    for stamp in hold_releases or ():
        value = float(stamp)
        if lifted is None or value > lifted:
            lifted = value
    return lifted


def current_hold_start(events: Iterable[dict], state: str | None) -> float | None:
    """L'instant où la retenue courante a commencé (`None` hors retenue).

    Un lot `blocked` ou `waiting_human` est retenu depuis la dernière
    transition d'un état hors retenue vers un état de retenue ; une transition
    entre deux états de retenue (`blocked` → `waiting_human`) ne rouvre pas
    l'horloge, comme `hold_release_ts` lit la même continuité. Le temps couru
    avant cette entrée reste acquis : les étapes d'après le gel s'arrêtent à
    cet instant, elles ne s'effacent pas (#3555). `None` sans retenue
    courante."""
    if state not in HOLDS:
        return None
    ordered = sorted(events or (),
                     key=lambda ev: (float(ev.get("created_ts") or 0.0),
                                     int(ev.get("id") or 0)))
    started = None
    previous = None
    for event in ordered:
        seen = event.get("state")
        at = event.get("created_ts")
        if seen in HOLDS and previous not in HOLDS and at is not None:
            started = float(at)
        previous = seen
    return started


#: #4561 : les notes qui datent une attente de la garde d'empreinte (#3185)
#: sur le lot retenu. Une attente est un ÉPISODE : une note d'ouverture (les
#: bloqueurs), d'éventuelles notes de changement d'ensemble, une note de
#: clôture datée. L'épisode est la mémoire de la levée : un blocage suivant en
#: ouvre un autre, et une note de clôture ne se confond jamais avec la
#: précédente (son horodatage fait partie du texte).
FOOTPRINT_WAIT_NOTE = "en attente d'empreinte : "
FOOTPRINT_RELEASE_NOTE = "empreinte libérée : "
_LOT_REF_RE = re.compile(r"#(\d+)")


def _footprint_ids(blockers: Iterable) -> list[int]:
    """Les ids des bloqueurs, en acceptant les lignes (`{"id": …}`) et les
    ids nus (l'épisode rend déjà des entiers)."""
    out = set()
    for lot in blockers or ():
        ident = lot.get("id") if hasattr(lot, "get") else lot
        if ident is not None:
            out.add(int(ident))
    return sorted(out)


def _footprint_stamp(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(at)))


def footprint_wait_note(blockers: Iterable[dict], *,
                        at: float | None = None) -> str:
    """La note d'ouverture (ou de changement) d'une attente d'empreinte :
    « en attente d'empreinte : #12, #34 ». `at` y ajoute son horodatage, pour
    qu'un ensemble de bloqueurs qui revient ouvre un épisode neuf au journal
    (une note de texte identique serait dédoublonnée)."""
    text = FOOTPRINT_WAIT_NOTE + ", ".join("#%d" % ident
                                           for ident in _footprint_ids(blockers))
    if at is not None:
        text += " (%s)" % _footprint_stamp(at)
    return text


def footprint_release_note(blockers: Iterable[dict], *, at: float) -> str:
    """La note de clôture datée d'une attente : « empreinte libérée : #… »."""
    return (FOOTPRINT_RELEASE_NOTE
            + ", ".join("#%d" % ident for ident in _footprint_ids(blockers))
            + " (%s)" % _footprint_stamp(at))


def footprint_episode(events: Iterable[dict]) -> dict:
    """L'épisode d'attente d'empreinte porté par le journal du lot (#4561).

    Rend `{open, ids, last_ids, released_ts}` : `open` (le dernier fait est une
    ouverture ou un changement, aucune clôture depuis), `ids` l'UNION des
    bloqueurs nommés dans l'épisode, `last_ids` ceux de sa dernière note et
    `released_ts` l'instant de clôture du dernier épisode.

    La levée se date sur l'union, jamais sur la seule dernière note : après
    `{A, B}` puis `{B}`, garder la dernière note perdrait A, et la levée serait
    datée sur B, avant la vraie libération (B1 de cx2 sur 1f182041f). Le
    changement d'ensemble se compare à `last_ids`, jamais à l'union : dès qu'un
    bloqueur sort, l'union ne redevient plus égale à l'ensemble courant et
    chaque passe réécrirait la note (#5115). La clôture, elle, est datée une
    seule fois et ne bouge plus (P1 de cx1 sur b8d7a9901)."""
    opened = False
    ids: set = set()
    last_ids: set = set()
    released = None
    ordered = sorted(events or (),
                     key=lambda ev: (float(ev.get("created_ts") or 0.0),
                                     int(ev.get("id") or 0)))
    for event in ordered:
        note = str(event.get("note") or "")
        at = event.get("created_ts")
        if at is None:
            continue
        if note.startswith(FOOTPRINT_WAIT_NOTE):
            found = {int(match) for match in _LOT_REF_RE.findall(note)}
            if not found:
                continue
            if not opened:
                ids = set()
                opened = True
            ids.update(found)
            last_ids = found
        elif note.startswith(FOOTPRINT_RELEASE_NOTE) and opened:
            opened = False
            released = float(at)
    return {"open": opened, "ids": sorted(ids), "last_ids": sorted(last_ids),
            "released_ts": released}


def _footprint_exact_release(named: Iterable[int],
                             satisfied: dict | None) -> float | None:
    """La levée exacte, si TOUS les bloqueurs nommés ont un jalon datable."""
    satisfied = satisfied or {}
    stamps = []
    for ident in named:
        stamp = satisfied.get(ident, satisfied.get(str(ident)))
        if stamp is None:
            return None
        stamps.append(float(stamp))
    return max(stamps) if stamps else None


def footprint_blockers(item: dict, lots: Iterable[dict],
                       paths_by_lot: dict, policies: dict | None = None) -> list[dict]:
    """Les lots qui retiennent `item` par la garde d'empreinte (#3185, #4561).

    Même règle que la garde d'attribution : un lot du même dépôt (`app`), qui
    OCCUPE son empreinte (confié ou démarré, non fusionné) et dont un chemin
    chevauche celui du lot retenu. Seul un lot `intake` SANS porteur est
    retenu : la garde ne refuse pas la réassignation d'un lot en cours. Rend
    `[{id, state, assignee, updated_ts, last_activity_ts}]`, triés par id.

    #4818 : `policies` (`{app: politique}`) porte les chemins partagés du
    dépôt (ignorés) et son plafond de lots en vol : sans chevauchement, un
    dépôt au plafond retient le lot par tous ses occupants (`reason: cap`,
    lots exemptés exceptés : `work_footprints.is_exempt`). Le marqueur `.` chevauche tout."""
    if str(item.get("state") or "") != lifecycle.REQUESTED:
        return []
    if str(item.get("assignee") or "").strip():
        return []
    mine = paths_by_lot.get(int(item["id"])) or ()
    app = str(item.get("app") or "").strip()
    if not app:
        return []
    from . import work_footprints

    policy = (policies or {}).get(app) or {}
    shared = policy.get("shared_paths") or ()
    out = []
    occupants = []
    for other in lots or ():
        other_id = int(other["id"])
        if other_id == int(item["id"]):
            continue
        if str(other.get("app") or "").strip() != app:
            continue
        state = str(other.get("state") or "")
        if not state or state in lifecycle.COMPLETED:
            continue
        if not (str(other.get("assignee") or "").strip()
                or state != lifecycle.REQUESTED):
            continue
        row = {"id": other_id, "state": state,
               "assignee": other.get("assignee") or None,
               "updated_ts": other.get("updated_ts"),
               "last_activity_ts": other.get("last_activity_ts")}
        if not work_footprints.is_exempt(other):
            occupants.append(row)
        theirs = paths_by_lot.get(other_id) or ()
        if not mine or not theirs:
            continue
        if work_footprints.held_pairs(mine, theirs, shared):
            out.append(row)
    cap = policy.get("max_in_flight")
    if (not out and cap and len(occupants) >= int(cap)
            and not work_footprints.is_exempt(item)):
        out = [dict(row, reason="cap") for row in occupants]
    return sorted(out, key=lambda blocker: blocker["id"])


def footprint_wait(events: Iterable[dict], blockers: Iterable[dict], *,
                   satisfied: dict | None = None) -> dict | None:
    """L'attente d'empreinte d'un lot, pour `lot_steps` (#4561).

    `blockers` : les lots occupants qui le retiennent maintenant
    (`footprint_blockers`) — l'attente est en cours, aucun pas SLO ne court.
    Sans bloqueur courant, l'épisode (`footprint_episode`) nomme ceux qui
    l'ont retenu ; sa clôture date le redémarrage de l'horloge. Elle est lue
    des jalons `merged`/`closed` des bloqueurs quand TOUS en ont un, mais
    jamais au-delà de la clôture déjà écrite : un bloqueur sorti de
    l'empreinte puis fusionné plus tard ne déplace pas la levée (P1 de cx2
    sur 1b7e58d00).

    Si la clôture n'est pas encore datable, la levée est **inconnue**
    (`undated`) : l'attente reste suspendue, sans durée inventée (#1982) et
    sans repartir de l'instant de chaque passe (B2 de cx2 sur 1f182041f).

    Rend `{lots, released_ts, named, undated, open}` ; `None` sans attente."""
    blockers = list(blockers or ())
    if blockers:
        return {"lots": blockers, "released_ts": None, "undated": False,
                "named": _footprint_ids(blockers), "open": True}
    episode = footprint_episode(events)
    named = episode["ids"]
    if not named:
        return None
    exact = _footprint_exact_release(named, satisfied)
    if not episode["open"]:
        # La clôture déjà écrite est la borne : un bloqueur sorti de
        # l'empreinte PUIS fusionné plus tard ne déplace plus la levée. La
        # fusion, quand elle précède la passe de clôture, reste la date
        # exacte (la plus tôt) : min(clôture, jalons).
        release = episode["released_ts"]
        if release is None:
            release = exact
        elif exact is not None:
            release = min(float(release), exact)
        if release is None:
            return {"lots": [], "released_ts": None, "named": named,
                    "undated": True, "open": False}
        return {"lots": [], "released_ts": release, "named": named,
                "undated": False, "open": False}
    # L'épisode est ouvert et plus aucun bloqueur ne chevauche : la clôture
    # est observée maintenant. Une date exacte (jalons) suffit ; sinon
    # l'appelant écrit la note de clôture et passe son instant.
    if exact is not None:
        return {"lots": [], "released_ts": exact, "named": named,
                "undated": False, "open": True}
    return {"lots": [], "released_ts": None, "named": named,
            "undated": True, "open": True}


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


def _after_vehicle(start: float, exited: float | None, extra: dict) -> float:
    """#4369 : `verdict_merge` repart de la sortie du véhicule, si elle suit."""
    if exited is None or exited <= start:
        return start
    extra["vehicle_exit_ts"] = round(exited, 3)
    return exited


def _after_dependency(start: float, ready: float | None, extra: dict) -> float:
    """#5214 : `verdict_merge` repart de la fusion de la dépendance bloquante."""
    if ready is None or ready <= start:
        return start
    extra["dependency_merged_ts"] = round(ready, 3)
    return ready


def lot_steps(item: dict, jalons: dict, events: Iterable[dict], *,
              now: float | None = None, packet_slot: float | None = None,
              dependencies: Iterable[dict] = (), packet: bool = False,
              p80_s: float | None = None, outside_packet: bool = False,
              ci_green_ts: float | None = None,
              hold_releases: Iterable[float] = (),
              reviews: Iterable[dict] = (),
              fixes: Iterable[dict] = (),
              vehicle_exit_ts: float | None = None,
              packet_pause: dict | None = None,
              footprint: dict | None = None) -> list:
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

    #4369 : `vehicle_exit_ts` (sortie du véhicule, `vehicle_exits`) décale
    de même le départ de `verdict_merge` en cours : retiré d'un paquet, le lot
    ne paie pas le temps passé dedans.

    #5214 : une dépendance bloquante encore ouverte retient la fusion : aucune
    ligne `verdict_merge` ne court (l'attente est celle du bloqueur). Levée,
    elle décale le départ à sa fusion (jalon `merged`/`closed`, `satisfied_ts`)
    si elle suit le GO ; une fusion déjà faite garde sa durée réelle.

    #4444 : `packet_pause` (la pause déclarée du paquet horaire que
    `packet_slot` a déjà franchie) est seulement nommée sur la ligne.

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

    #3487 : un lot qui a déclaré une exception (`outside_packet=True`) mais
    que le paquet porte réellement suit la cadence du paquet, pas sa CI
    propre : l'appelant lui passe `packet_slot` (#2335) et `p80_s`, et garde
    `outside_packet=False` ; seule son escalade reste due (l'exception est le
    signal d'un lot à suivre). Le geste qui en sort nomme le paquet et sa CI
    (`alert_briefs`), jamais une réassignation du porteur.

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

    #3333 : une retenue suspend `blocked_freeze` comme le délai intake
    (#2916). Tant que le lot est `blocked` ou `waiting_human`, son assigné
    n'a pas de correction à livrer : il attend la levée, aucune ligne ne
    court. Une fois la retenue levée (`hold_release_ts`, ou un hold de pilote
    levé et passé par `hold_releases`), l'horloge repart de la levée si elle
    suit le blocage. Le temps passé sous retenue n'est donc pas un retard du
    porteur, et un lot parqué volontairement ne fait plus crier « réassigner
    maintenant » vers le bureau à la levée.

    #3487 : un lot revenu en `build` après un GO courant porte un correctif
    dû. Le GO → fusion ne court plus — la tête relue n'est plus le travail
    courant — et l'étape du correctif (`blocked_freeze`) repart de ce retour
    en `build` ; `build_quiet` ne double plus cette mesure. L'alerte dit que
    le porteur porte le correctif : au niveau 3, elle ne prescrit jamais de le
    réassigner (la réassignation perd son contexte).

    #3555 : une retenue (`blocked`, `waiting_human`) ou un hold explicite de
    pilote levé (#1664) suspend aussi les étapes d'après le gel
    (`green_verdict`, `verdict_merge` ; `blocked_freeze` l'était déjà par
    #3333). Sous retenue d'état, l'horloge s'arrête à l'entrée en retenue :
    le temps déjà couru reste acquis, ce qui laisse au pilote l'alerte d'un
    lot déjà en retard qu'il doit débloquer, mais le temps du parcage ne
    compte pas. À la levée, l'horloge repart de la levée ; un lot sans
    retenue garde son alerte. Une étape déjà terminée garde sa durée réelle,
    moins la retenue qui a suivi son départ.

    #4561 : `footprint` (`footprint_wait`) est l'attente de la garde
    d'empreinte (#3185) d'un lot `intake` sans porteur. Tant qu'un lot
    occupant chevauche le sien, aucun pas « signalé → assigné » ni « prêt →
    démarré » ne court : `work assign` le refuserait. Après la levée, les deux
    horloges repartent de la fusion (ou clôture) du dernier bloqueur, jamais de
    la création : le temps où le lot n'était pas attribuable n'est pas un
    retard de son porteur. Une levée qu'aucun jalon ne date laisse les deux
    pas suspendus (`undated`), sans durée inventée. `footprint["named"]`
    nomme les bloqueurs retenus.
    """
    events = list(events)
    fixes = list(fixes or ())
    now = time.time() if now is None else float(now)
    critical = int(item.get("priority") or 0) == 1
    state = item.get("state")
    out = []

    # #4561 : l'attente de la garde d'empreinte suspend les deux pas d'avant
    # le départ ; leur horloge repart de la levée, jamais de la création. Une
    # levée non datée reste suspendue (aucune durée inventée, #1982).
    footprint = dict(footprint or {})
    blocked = bool(footprint.get("lots")) or bool(footprint.get("undated"))
    fp_released = footprint.get("released_ts")
    fp_named = [int(i) for i in (footprint.get("named") or ())]

    def _fp_start(start: float, end: float | None = None) -> tuple[float, dict]:
        if fp_released is None:
            return start, {}
        value = float(fp_released)
        if value <= start or (end is not None and value > end):
            return start, {}
        extra = {"footprint_released_ts": round(value, 3)}
        if fp_named:
            extra["footprint_lots"] = fp_named
        return value, extra

    requested = jalons.get("requested") or item.get("created_ts")
    assigned = assigned_ts(item, events)
    if requested is not None:
        step = step_of("signal_assigned")
        if assigned is not None:
            start, extra = _fp_start(float(requested), assigned)
            out.append(_row(step, start, now=now, critical=critical,
                            done_ts=round(assigned, 3),
                            elapsed_s=int(max(0.0, assigned - start)), **extra))
        elif not blocked:
            start, extra = _fp_start(float(requested))
            out.append(_row(step, start, now=now, critical=critical, **extra))

    # #3162 : le début mesuré conclut l'étape comme le passage en build, même
    # quand le lot est resté en intake.
    started = start_ts(events)
    if requested is not None and state not in lifecycle.COMPLETED and not blocked:
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
        # #4561 : l'empreinte libérée repousse aussi le « prêt » de l'intake.
        if ready is not None:
            ready, fp_extra = _fp_start(float(ready), started)
        else:
            fp_extra = {}
        if ready is None:
            # #2551 : dépendance bloquante ouverte — le lot n'est pas prêt,
            # aucun délai ne court : son porteur ne peut rien faire avant la
            # levée du prérequis. La mesure repart de cette levée.
            pass
        elif started is not None:
            out.append(_row(step, ready, now=now, critical=critical,
                            done_ts=round(started, 3),
                            elapsed_s=int(max(0.0, started - float(ready))),
                            **fp_extra))
        elif begun(events, jalons):
            # #3162 : branche associée ou gel — le lot est commencé, mais son
            # début n'est pas daté : ni retard ni durée inventés (#1982).
            pass
        elif state == lifecycle.REQUESTED and now >= float(ready):
            # prêt, pas encore démarré : le délai court depuis que le lot est
            # prêt. Passé intake sans événement `build` (#1982), la phase est
            # franchie mais non mesurable : aucune ligne, donc aucun retard
            # inventé — seules l'origine réelle et les durées mesurées restent.
            out.append(_row(step, ready, now=now, critical=critical, **fp_extra))

    # #5214 : un lot validé dont une dépendance bloquante n'est pas levée ne
    # peut pas fusionner : son attente est celle du bloqueur, pas la sienne, et
    # aucune ligne GO → fusion ne court (comme le délai intake, #2551). Une
    # arête levée reste dans le graphe avec `blocking` faux et sa date
    # (`satisfied_ts`) : si elle suit le GO, l'horloge part de là, le porteur
    # ne paie pas l'attente d'un autre lot. Une arête non bloquante sans date
    # ne retient ni ne décale rien.
    blocking_deps = [edge for edge in dependencies or () if edge.get("blocking")]
    dep_open = any(edge.get("satisfied_ts") is None for edge in blocking_deps)
    dep_ready = max((float(edge["satisfied_ts"])
                     for edge in dependencies or ()
                     if edge.get("satisfied_ts") is not None), default=None)

    verdict, kind = jalons.get("verdict"), jalons.get("verdict_kind")
    merged = jalons.get("merged")
    # #3487 : un lot revenu en build après son GO courant porte un correctif
    # dû. Le retour en build (transition, pas une note) date l'épisode ;
    # l'étape du correctif le mesure et le GO → fusion ne court plus.
    returned = None
    if (state == lifecycle.IN_PROGRESS and kind == "ok" and verdict is not None
            and jalons.get("verdict_current", True)):
        latest_build = None
        previous = None
        for ev in sorted(events, key=lambda e: (float(e.get("created_ts") or 0.0),
                                                int(e.get("id") or 0))):
            seen = ev.get("state")
            if (seen == lifecycle.IN_PROGRESS and previous != lifecycle.IN_PROGRESS
                    and ev.get("created_ts") is not None):
                latest_build = float(ev["created_ts"])
            previous = seen
        if latest_build is not None and latest_build >= float(verdict):
            returned = latest_build

    # #3936 : un lot release (tête ou cible `release/**`, type release) ne
    # gèle plus après un retour en build ou un avis bloquant : la porte des
    # familles refuse son gel (plage des lots déjà relus, trois familles) et
    # sa fusion est le geste du pilote sur la tête verte (canon §2). Aucun
    # nouveau gel n'est donc dû ; seul `build_quiet` mesure son silence.
    from . import lot_class
    if lot_class.release_vehicle(item):
        returned = None
        fixes = []

    if state == lifecycle.IN_PROGRESS and returned is None and not fixes:
        # #3487 : le correctif dû (avis bloquant ou retour en build) a sa
        # propre horloge ; `build_quiet` ne la double pas.
        step = step_of("build_quiet")
        last = last_event_ts(item, events) or item.get("created_ts")
        if last is not None:
            out.append(_row(step, last, now=now, critical=critical))

    # #3555 : une retenue (état ou hold de pilote) suspend les étapes d'après
    # le gel ; la dernière levée, lue une fois, repousse leur départ. Sous
    # retenue d'état, l'horloge s'arrête à l'entrée en retenue : le temps déjà
    # couru reste acquis (un lot déjà en retard alerte le pilote pour qu'il le
    # débloque), le temps du parcage ne compte pas.
    lifted = last_hold_release(events, hold_releases)
    exited = vehicle_exit_ts
    stopped = current_hold_start(events, state)
    running_now = now if stopped is None else min(now, stopped)

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
            # #3555 : le temps parqué avant la fusion ne compte pas ; une
            # fusion déjà faite garde sa durée réelle, moins la retenue.
            start = float(verdict)
            parked = {}
            if lifted is not None and start < lifted <= float(merged):
                start = lifted
                parked["hold_released_ts"] = round(lifted, 3)
            out.append(_row(step, start, now=now, critical=critical,
                            done_ts=round(float(merged), 3),
                            elapsed_s=int(max(0.0, float(merged) - start)),
                            target=target, escalate=not packet, **extra,
                            **parked))
        elif state not in (
            *lifecycle.DELIVERED,
            lifecycle.CLOSED,
            lifecycle.IN_PROGRESS,
        ):
            # #3487 : revenu en build, le GO ne court plus (cf. plus haut).
            # #3555 : sous retenue, `running_now` arrête l'horloge à l'entrée.
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
                    if lifted is not None and lifted > start:
                        start = lifted
                        extra["hold_released_ts"] = round(lifted, 3)
                    start = _after_vehicle(start, exited, extra)
                    start = _after_dependency(start, dep_ready, extra)
                    if not dep_open:
                        out.append(_row(step, start, now=running_now,
                                        critical=critical, target=target,
                                        escalate=not packet, **extra))
            else:
                if packet_slot is not None and float(packet_slot) > start:
                    # #2335/A2 : la fusion ne peut entrer qu'au prochain paquet ;
                    # l'horloge GO → fusion part de ce créneau, pas du GO.
                    start = float(packet_slot)
                    extra["packet_slot"] = round(start, 3)
                    if packet_pause:
                        # #4444 : le créneau suit une pause déclarée du
                        # paquet horaire ; la cause est nommée sur la ligne.
                        extra["packet_pause"] = dict(packet_pause)
                if lifted is not None and lifted > start:
                    # #3555 : la levée de retenue suit le créneau ; l'horloge
                    # repart d'elle, le parcage n'est pas un retard.
                    start = lifted
                    extra["hold_released_ts"] = round(lifted, 3)
                start = _after_vehicle(start, exited, extra)
                start = _after_dependency(start, dep_ready, extra)
                if not dep_open:
                    out.append(_row(step, start, now=running_now, critical=critical,
                                    target=target, escalate=not packet, **extra))

    if state not in lifecycle.COMPLETED:
        step = step_of("green_verdict")
        for review in reviews or ():
            name = str(review.get("name") or "").strip()
            since = review.get("designated_ts")
            if not name or since is None:
                continue
            extra = {}
            if lifted is not None and lifted > float(since):
                # #3555 : le relecteur n'est pas relancé pour du temps parqué.
                extra["hold_released_ts"] = round(lifted, 3)
                since = lifted
            out.append(_row(step, since, now=running_now, critical=critical,
                            reviewer=name, **extra))

    if state not in lifecycle.COMPLETED and state not in HOLDS:
        step = step_of("blocked_freeze")
        # #3333 : la dernière levée de retenue (état ou hold de pilote), lue
        # une fois pour les trois étapes d'après le gel (#3555).
        for fix in fixes or ():
            since = fix.get("blocked_ts")
            if since is None:
                continue
            extra = {}
            if lifted is not None and lifted > float(since):
                # #3333 : le temps parqué n'est pas un retard de la correction.
                extra["hold_released_ts"] = round(lifted, 3)
                since = lifted
            # #3288 : le débiteur est l'assigné ; `blocked_by` et `sha` servent
            # le texte de l'alerte, pas la cible de l'escalade.
            out.append(_row(step, since, now=now, critical=critical,
                            blocked_by=str(fix.get("blocked_by") or ""),
                            sha=str(fix.get("sha") or ""), **extra))
        if returned is not None and not fixes:
            # #3487 : le correctif est dû depuis le retour en build (ou depuis
            # la levée d'une retenue qui l'a suivi). La ligne dit que c'est le
            # porteur qui porte le correctif, pas un avis bloquant.
            since = returned
            extra = {}
            if lifted is not None and lifted > float(since):
                extra["hold_released_ts"] = round(lifted, 3)
                since = lifted
            out.append(_row(step, since, now=now, critical=critical,
                            returned=True, **extra))

    handover = handover_ts(item, events)
    if handover is not None and state not in lifecycle.COMPLETED:
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
