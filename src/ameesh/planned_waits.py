# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit planned waits for lots (L368); never infer dependencies from prose.

A wait is declared, never guessed: a future planned start, an open commitment,
an explicit blocking dependency — or an explicit **hold** (#1664), the note a
pilot writes when the lot is parked on a gate no open lot carries (operator
window, human decision). The hold stops the stale reminders and the SLO
breaches, like any other planned wait, until the lot changes state or the hold
is explicitly lifted. Only a pilot's hold counts here (`pilot_holds`), and the
write path authenticates that claim (`work.check_wait_note`); the runner wake
keeps its own, older convention (`LotSessions.HOLD_PREFIX`).

#2378: a lot in `waiting_human` with an open decision request (`ameesh decide
ask`) waits for its decider until the request's deadline; past the deadline,
the ordinary alerts come back.

#3498: a pilot's hold may declare its external executor and the gesture
expected (`hold: attente bureau : <geste>`, `hold: attente owner : <geste>`,
optionally dated `(due 2026-10-25 12:00 UTC)` or `[réveil programmé: …]`).
`external_hold` reads that declaration; the debit keeps the lot out of the
agent waits and reports the reminder to the bureau or owner.

#4235: a pilot hold may declare an **observation window** — a wanted, dated
wait (`hold: fenêtre : <motif>`, with its deadline armed by `--wake-at`) —
read by `observation_hold`; the debit lists it apart until the deadline.
"""
from __future__ import annotations

import datetime
import re
import time

from . import plan, storage, work_lifecycle as lifecycle
from .storage.interface import HOLD_NOTE_WHITESPACE, LotSessions

_LOT = re.compile(r"^(?:(?:task|lot)\s*#?\s*|#)?([0-9]{1,18})$", re.I)
_PACKAGE = re.compile(r"^(?:lot|package|workpackage)\s+(\S+)$", re.I)

#: external hold: the commitment names the gate it waits for (`gate:<name>`,
#: or `external:<name>`), and holds the lot until the end of its due day.
_GATE = re.compile(r"^(?:gate|external):(\S+)$", re.I)

#: #2335 : une PR de paquet est une branche `integration/**` ; son manifeste
#: (corps du lot porteur) nomme les unités avec leur tête gelée — `#1234 @
#: abcdef0`, `#1234 abcdef0`, `#1234@abcdef0`, la forme des manifestes réels
#: (#1342, #2085, #2270, #2277, #2347). Un `#N` cité en prose (« #2203
#: exclu », « garder #1024 », « admis mais exclu ») n'est **pas** une unité :
#: le taire masquerait justement l'alerte d'un lot qui attend son auteur.
#: Une référence collée à un mot ou à un chemin (`manaty/ameesh#1697`) est
#: écartée de même.
_PACKET_BRANCH = "integration/"
_UNIT = re.compile(r"(?<![\w/])#([0-9]{1,18})\s*@?\s*([0-9a-fA-F]{7,40})\b")


def hold_end(due_on):
    """End of the due day (the day counts in full), or None without a date.

    A hold always ends: past this instant the ordinary inactivity rule
    applies again, like `engagement_overdue`, which also counts the due day
    in full and only fires the next day.
    """
    try:
        day = datetime.date.fromisoformat(str(due_on)[:10])
        return plan.day_start_ts((day + datetime.timedelta(days=1)).isoformat())
    except (TypeError, ValueError, OverflowError):
        return None

#: #1664 : le préfixe de la note de retenue — le même que celui du réveil
#: (règle 1, `LotSessions.HOLD_PREFIX`).
_HOLD = re.compile("^" + re.escape(LotSessions.HOLD_PREFIX), re.I)
#: les notes de levée explicite ; un changement d'état du lot lève aussi.
_SPACE = "[" + HOLD_NOTE_WHITESPACE + "]"
# Expliciter aussi les variantes Unicode de IGNORECASE absentes d'ARE.
_LIFT = re.compile(
    f"^(?:unhold|hold{_SPACE}+(?:l[iİı]fted|relea[sſ]ed|cleared)){_SPACE}*:", re.I)
#: #1664 : motifs donnés au stockage pour la lecture groupée ciblée
#: (`Progress.lot_hold_events`) — exactement `_HOLD` et `_LIFT` (syntaxe `re`
#: côté repli, ARE côté pilote PostgreSQL) : toutes les notes hold/levée,
#: aucune note ordinaire. Le filtre exact reste ici.
HOLD_NOTE_PATTERNS = (_HOLD.pattern, _LIFT.pattern)
#: #3373 : le mot « hold » suivi d'un séparateur (`hold :`, `hold (pilote) :`,
#: `Hold —`) — une retenue apparente qui n'en est pas une : le préfixe exact
#: est `hold:` sans blanc. Un trait d'union collé (`hold-up:`) reste une note
#: ordinaire, comme le veut le motif exact.
_HOLD_LIKE = re.compile(r"^hold(?=[\s:(])", re.I)


def is_hold_note(text) -> bool:
    """#1664 : la note qui pose une retenue (`hold: <raison>`). Le préfixe est
    celui du réveil (règle 1) ; seule la retenue d'un pilote compte pour la
    stagnation (`pilot_holds`)."""
    return bool(_HOLD.match(str(text or "").strip()))


def is_lift_note(text) -> bool:
    """#1664 : la note qui lève explicitement une retenue (`unhold:`,
    `hold lifted:`/`released:`/`cleared:`)."""
    return bool(_LIFT.match(str(text or "").strip()))


def is_hold_lookalike(text) -> bool:
    """#3373 : une graphie de retenue qui n'en est pas une.

    `hold :`, `hold (pilote) :`, `Hold —` ne portent pas le préfixe exact
    `hold:` : rien ne s'arrête, et l'auteur croit pourtant le lot retenu. Le
    chemin d'écriture refuse ces variantes avec la forme exacte ; les vraies
    notes de retenue et de levée, et les notes ordinaires (`hold-up:`,
    `holding pattern`), ne sont pas visées."""
    value = str(text or "").strip()
    return bool(_HOLD_LIKE.match(value)) and not is_hold_note(value) \
        and not is_lift_note(value)


#: #3498 : un hold peut nommer son exécutant externe (bureau ou owner) et le
#: geste attendu — `hold: attente bureau : <geste>`, `hold: attente owner :
#: <geste>`, `hold: attente externe (bureau) : <geste>`. #3773 : il peut aussi
#: déclarer un tiers extérieur sans nommer le bureau ou l'owner — `hold:
#: attente externe : revue Apple`. Le tiers est alors dans le geste et
#: l'exécutant rendu est `externe`. Une échéance facultative `(due
#: 2026-10-25 12:00 UTC)` ou `(échéance …)`, avant ou après le geste, borne la
#: relance ; sans elle, le débit relance après son seuil.
_EXTERNAL_HEAD = re.compile(
    r"(?<![\w])attente\s+"
    r"(?:externe\s*[\(\[]?\s*)?"
    r"(?:de\s+la\s+|du\s+|de\s+l'|de\s+)?"
    r"(?P<who>bureau|owner|propri[eé]taire|externe)"
    r"\s*(?:[\)\]]\s*)?"
    r"(?P<tail>.*)", re.I | re.S)
_EXTERNAL_DUE = re.compile(
    r"\s*[\(\[]\s*(?:échéance|echeance|due|au\s+plus\s+tard|jusqu'au|avant\s+le)"
    r"\s*:?\s*(?P<date>[^\)\]]+?)\s*[\)\]]", re.I)
_EXTERNAL_SEP = re.compile(r"^\s*:\s*(?P<gesture>.+)$", re.S)
_EXTERNAL_DASH = re.compile(r"^\s*[—–-]\s*(?P<gesture>.+)$", re.S)
_EXTERNAL_WAKE = re.compile(r"\[\s*réveil programmé\s*:\s*(?P<date>[^\]]+)\]",
                            re.I)
_EXTERNAL_TOKEN = re.compile(r"(?:^|\s)wake_at=\S+", re.I)
_EXTERNAL_EXECUTORS = {"bureau": "bureau", "owner": "owner",
                       "propriétaire": "owner", "proprietaire": "owner",
                       "externe": "externe"}


def external_hold(text) -> dict | None:
    """#3498 : la déclaration d'attente externe portée par un hold, ou None.

    Rend `{executor, gesture, due}` : `executor` est `bureau`, `owner`
    (`propriétaire` est normalisé) ou `externe` pour un tiers non nommé
    (#3773, `hold: attente externe : revue Apple`), `gesture` le texte attendu
    après le séparateur, `due` la date déclarée (texte brut, pour
    `debit.parse_wake_due`) ou None. Une déclaration sans geste n'est pas une
    déclaration : le hold reste une attente ordinaire, et rien n'est deviné
    depuis la prose (« attente du bureau depuis 3 h » ne déclare pas)."""
    note = str(text or "").strip()
    match = _EXTERNAL_HEAD.search(note)
    if not match:
        return None
    who = _EXTERNAL_EXECUTORS.get(match.group("who").strip().casefold())
    if not who:
        return None
    tail = match.group("tail")
    head_due = _EXTERNAL_DUE.match(tail)
    if head_due:
        tail = tail[head_due.end():]
    separator = _EXTERNAL_SEP.match(tail) or _EXTERNAL_DASH.match(tail)
    if not separator:
        return None
    gesture = _EXTERNAL_DUE.sub("", separator.group("gesture"))
    gesture = _EXTERNAL_WAKE.sub("", gesture)
    gesture = _EXTERNAL_TOKEN.sub("", gesture).strip(" ;,.")
    if not gesture:
        return None
    due = _EXTERNAL_DUE.search(note) or _EXTERNAL_WAKE.search(note)
    return {"executor": who, "gesture": gesture,
            "due": due.group("date").strip() if due else None}


#: #4235 : une **fenêtre d'observation** est une attente voulue et **datée**,
#: déclarée dans le hold du pilote : `hold: fenêtre : <motif>` (ou `hold:
#: fenêtre d'observation : …`, `hold: observation : …`), avec le préambule du
#: journal (`hold: (pilote …) Fenêtre d'observation voulue : …`). Son échéance
#: est celle que `--wake-at` écrit (`[réveil programmé: …]`), ou une date
#: déclarée `(due …)`/`(échéance …)`. Sans échéance écrite, ce n'est pas une
#: fenêtre : le débit ne devine pas quand l'attente finit, et le hold reste
#: une attente ordinaire.
_OBSERVATION_HOLD = re.compile(
    r"^hold:\s*(?:\([^)\n]*\)\s*)?"
    r"(?P<head>fen[êe]tre(?:\s+d['’]observation)?|observation)\b"
    r"(?P<tail>.*)$", re.I | re.S)


def observation_hold(text) -> dict | None:
    """#4235 : la déclaration d'une fenêtre d'observation, ou None.

    Rend `{motif, due}` : `motif` est ce qui est attendu à l'échéance (le
    texte déclaré, sans la date), `due` la date écrite (texte brut, pour
    `debit.parse_wake_due`). Une note sans motif ou sans échéance ne déclare
    rien (le sens sûr : elle reste une attente d'agent), et une graphie de
    hold (`hold :`) n'est pas un hold."""
    note = str(text or "").strip()
    match = _OBSERVATION_HOLD.match(note)
    if not match:
        return None
    found = _EXTERNAL_DUE.search(note) or _EXTERNAL_WAKE.search(note)
    if not found:
        return None
    motif = _EXTERNAL_DUE.sub("", match.group("tail"))
    motif = _EXTERNAL_WAKE.sub("", motif)
    motif = _EXTERNAL_TOKEN.sub("", motif).strip(" \t:;,.—–-")
    if not motif:
        return None
    return {"motif": motif, "due": found.group("date").strip()}


def explicit_holds(events, *, allowed=None) -> dict[int, dict]:
    """Les holds lus dans le journal d'un lot (#1664) — la **dernière** retenue.

    Un hold tient tant que le lot ne change pas d'état et qu'aucune note de
    levée (`unhold:`, `hold lifted:`…) ne suit ; les notes ordinaires (triage,
    relevé) ne l'effacent pas. `allowed` restreint qui peut poser ou lever le
    hold : un prédicat (nom → bool) ou un ensemble de noms ; None = lecture
    brute, sans contrôle d'auteur. Les transitions d'état sont lues dans
    l'ordre d'écriture `(created_ts, id)` du journal. Le dernier hold posé
    porte l'auteur et la date affichés.

    #3219 : c'est une lecture d'affichage, pas l'ensemble des retenues actives
    — un hold couvert par celui d'un autre auteur reste actif et continue de
    retenir. La lecture d'autorité (levée, fermeture par le contenu) est
    `active_holds`."""
    ordered: dict[int, list[dict]] = {}
    for event in events or ():
        ordered.setdefault(int(event["work_item_id"]), []).append(event)
    out: dict[int, dict] = {}
    for ident, lot_events in ordered.items():
        held = None
        previous = None
        for event in sorted(lot_events, key=lambda e: (float(e.get("created_ts") or 0.0),
                                                       int(e.get("id") or 0))):
            state = event.get("state")
            if previous is not None and state != previous:
                held = None                 # le lot a repris : ce qui le tenait est fini
            previous = state
            text = str(event.get("note") or "").strip()
            if not text:
                continue
            who = event.get("actor") or ""
            if allowed is not None and not (allowed(who) if callable(allowed)
                                            else who in allowed):
                continue
            if is_hold_note(text):
                held = {"what": "hold", "by": who or None,
                        "since": event.get("created_ts")}
            elif is_lift_note(text):
                held = None
        if held:
            out[ident] = held
    return out


def _pilot_allowed(db, cfg, *, strict: bool = False):
    """Le prédicat d'autorisation d'un hold de pilote (#1664, #2916).

    Même autorisation que la correction d'état et la désignation du relecteur
    (`work.corrector`, rôles `work.CORRECTOR_ROLES`) : un humain, ou un agent
    orchestrateur/conception déclaré (`AMEESH_ALERT_ORCHESTRATORS`, fiche Agent
    du canon), sans l'historique des lots confiés. Un hold d'un autre acteur
    garde son effet sur le réveil (règle 1) mais ne supprime pas l'alerte.

    #3375 : `strict=True` distingue un acteur **non pilote** d'une **autorité
    illisible**. Le défaut (`False`) garde la tolérance des alertes et de la
    stagnation ; la porte du paquet lit en strict, car ignorer un hold de
    pilote y ferait composer un lot réservé. En strict, une panne du registre,
    du canon ou de la résolution d'alias lève, au lieu de conclure « non ».
    """
    from . import assignments, work            # imports tardifs : pas de cycle
    verdict: dict[str, bool] = {}

    def allowed(who: str) -> bool:
        if who not in verdict:
            try:
                verdict[who] = bool(who) and (
                    who.startswith(work.HUMAN_PREFIX)
                    or assignments.is_orchestrator(cfg, db, who,
                                                   roles=work.CORRECTOR_ROLES,
                                                   history=False, strict=strict))
            except Exception:
                if strict:
                    raise                # autorité indéterminée : le doute refuse
                verdict[who] = False     # autorisation illisible : pas de hold
        return verdict[who]

    return allowed


def lift_authority(db, cfg=None, *, strict: bool = False):
    """Le prédicat d'autorisation d'une levée de hold (#3219), partagé.

    `active_holds` et la reconstruction des retenues de fermeture du débit
    (#3497) doivent lire la **même** autorité : un auteur ne lève que sa
    retenue, un pilote (humain, orchestrateur ou conception) lève celle du lot.
    `cfg` par défaut : celui de `db`."""
    cfg = getattr(db, "cfg", None) if cfg is None else cfg
    return _pilot_allowed(db, cfg, strict=strict)


def active_holds(db, events, *, cfg=None,
                 strict: bool = False) -> dict[int, dict[str, dict]]:
    """#3219 : les retenues actives de chaque lot, par auteur.

    Les holds d'un lot forment un **ensemble**, pas une pile dont on ne lirait
    que le dernier : chaque hold actif retient de lui-même. Un hold tient
    jusqu'à une levée ou un changement d'état ; une levée ne retire que le
    hold de son propre auteur — sauf celle d'un pilote (humain, orchestrateur
    ou conception), qui lève toute la retenue du lot (#1664). Un hold posé
    avant ou après celui d'un autre auteur continue donc de retenir, même
    après la levée du sien.

    `pilot` dit si l'auteur est un pilote **à la lecture** (`_pilot_allowed`,
    la même autorisation que la stagnation). `note` est le texte de la pose
    (motif), pour citer la retenue sans relire le journal.

    Rend `{lot: {auteur: {what, by, since, note, pilot}}}`."""
    ordered: dict[int, list[dict]] = {}
    for event in events or ():
        ordered.setdefault(int(event["work_item_id"]), []).append(event)
    if not ordered or not any(is_hold_note(event.get("note"))
                              for event in events or ()):
        return {}
    allowed = lift_authority(db, cfg, strict=strict)
    out: dict[int, dict[str, dict]] = {}
    for ident, lot_events in ordered.items():
        held: dict[str, dict] = {}
        previous = None
        for event in sorted(lot_events, key=lambda e: (float(e.get("created_ts") or 0.0),
                                                       int(e.get("id") or 0))):
            state = event.get("state")
            if previous is not None and state != previous:
                held = {}                   # le lot a repris : tout est fini
            previous = state
            text = str(event.get("note") or "").strip()
            if not text:
                continue
            who = event.get("actor") or ""
            if is_hold_note(text):
                held[who] = {"what": "hold", "by": who or None,
                             "since": event.get("created_ts"), "note": text,
                             "pilot": bool(allowed(who))}
            elif is_lift_note(text):
                if allowed(who):
                    held = {}               # un pilote lève toute la retenue
                else:
                    held.pop(who, None)     # l'auteur ne lève que la sienne
        if held:
            out[ident] = held
    return out


def pilot_holds(db, events, *, cfg=None,
                strict: bool = False) -> dict[int, dict]:
    """Les holds posés par un pilote (#1664) — les seuls comptés par la
    stagnation.

    `events` est le flux ciblé lu par le stockage
    (`Progress.lot_hold_events` : transitions d'état + notes hold/levée), ou
    un journal complet ; les deux donnent le même hold. La lecture
    d'autorisation est celle de `_pilot_allowed`. #3219 : un hold de pilote
    reste actif même recouvert par le hold ordinaire d'un autre auteur ; le
    dernier hold de pilote actif porte l'auteur et la date affichés.
    #3375 : `strict=True` propage une autorité illisible au lieu de conclure
    « non pilote » (la porte du paquet lit ainsi)."""
    events = list(events or ())
    if not any(is_hold_note(event.get("note")) for event in events):
        return {}
    out: dict[int, dict] = {}
    for lot, holds in active_holds(db, events, cfg=cfg, strict=strict).items():
        pilots = [hold for hold in holds.values() if hold.get("pilot")]
        if not pilots:
            continue
        chosen = max(pilots, key=lambda hold: float(hold.get("since") or 0.0))
        out[lot] = {"what": "hold", "by": chosen.get("by"),
                    "since": chosen.get("since")}
    return out


def pilot_hold_releases(db, events, *, cfg=None) -> dict[int, list]:
    """#2916 : les instants où un hold de pilote a été levé, par lot.

    Même journal et même autorisation que `pilot_holds` (`hold:` d'un pilote,
    levé par `unhold:`/`hold lifted:`/`cleared:` ou par un changement d'état).
    Le délai intake repart de la dernière levée antérieure au début du lot
    (#2921) : le temps parqué sous un hold de pilote n'est pas un retard
    imputable au porteur. Un `hold:` d'un acteur ordinaire ne compte pas — il
    ne suspend pas la stagnation non plus. Toutes les levées sont rendues :
    l'appelant les filtre par `started` avant de choisir la dernière.
    """
    events = list(events or ())
    if not any(is_hold_note(event.get("note")) for event in events):
        return {}
    cfg = getattr(db, "cfg", None) if cfg is None else cfg
    allowed = _pilot_allowed(db, cfg)
    ordered: dict[int, list[dict]] = {}
    for event in events:
        ordered.setdefault(int(event["work_item_id"]), []).append(event)
    out: dict[int, list] = {}
    for ident, lot_events in ordered.items():
        try:
            held = False
            previous = None
            released: list = []
            for event in sorted(lot_events,
                                key=lambda e: (float(e.get("created_ts") or 0.0),
                                               int(e.get("id") or 0))):
                state = event.get("state")
                at = event.get("created_ts")
                if previous is not None and state != previous:
                    if held and at is not None:
                        released.append(float(at))
                    held = False
                previous = state
                text = str(event.get("note") or "").strip()
                if not text:
                    continue
                if is_hold_note(text):
                    if allowed(event.get("actor") or ""):
                        held = True
                elif is_lift_note(text) and held:
                    if not allowed(event.get("actor") or ""):
                        continue
                    held = False
                    if at is not None:
                        released.append(float(at))
        except (TypeError, ValueError):
            # #1107 : un journal illisible est isolé par lot ; le calcul SLO
            # du lot portera lui-même l'alerte `lot_unreadable`.
            continue
        if released:
            out[ident] = released
    return out


def ancestors(package_id, packages):
    """The package and its parents, cycle-safe; absent cards are not a plan."""
    seen = set()
    while package_id and package_id not in seen:
        package = packages.get(package_id)
        if not package or not package.get("present", True):
            break
        seen.add(package_id)
        yield package_id
        package_id = package.get("parent")


def decision_waits(requests, *, now) -> dict[int, dict]:
    """#2378 : les lots qui attendent une décision ouverte et non échue.

    `requests` : les demandes en attente (`decisions.pending`). Le lot attend
    son décideur jusqu'à l'échéance la plus proche de ses demandes ouvertes ;
    une seule demande échue (ou sans échéance) rend le signal : l'humain est
    en retard, ce n'est plus une attente prévue."""
    by_lot: dict[int, list] = {}
    for request in requests or ():
        lot = request.get("lot")
        if isinstance(lot, int) and not isinstance(lot, bool):
            by_lot.setdefault(lot, []).append(request)
    out = {}
    for lot, pending in by_lot.items():
        dues = [request.get("due_ts") for request in pending]
        if any(due is None or now >= float(due) for due in dues):
            continue
        first = min(pending, key=lambda r: (float(r["due_ts"]), int(r["id"])))
        out[lot] = {"what": "decision", "decision": int(first["id"]),
                    "owner": first.get("owner"),
                    "due_ts": round(float(first["due_ts"]), 3),
                    "until": time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(float(first["due_ts"])))}
    return out


def packet_carriers(rows):
    """#2335 : les porteurs de paquet ouverts, indexés par lot.

    Un porteur est un lot ouvert dont la branche est `integration/**` : c'est
    la PR que le composeur pousse et que le pilote fusionne. Le porteur ne
    porte pas de code ; son corps est le manifeste des unités qu'il transporte.
    """
    out = {}
    for row in rows or ():
        if row.get("state") in lifecycle.COMPLETED:
            continue
        branch = str(row.get("branch") or "").strip()
        if not branch.startswith(_PACKET_BRANCH):
            continue
        try:
            out[int(row["id"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return out


#: #2599 : commits lus au plus sur la branche d'un porteur (borne de coût)
MAX_PACKET_COMMITS = 500


def _dry_pass(db) -> bool:
    """La connexion est-elle une passe à blanc de `db.batched` ?

    Une passe à blanc n'envoie aucune requête : la lecture des trailers
    (git) attend la vraie passe, sinon elle est refaite à chaque niveau de
    préchargement (#2599). La classe `_Recorder` de `db.batched` est la seule
    marque d'une passe à blanc."""
    from . import db as db_mod

    return isinstance(db, db_mod._Recorder)


def _carrier_ref(repo, carrier):
    """La ref lisible de la branche du porteur, sans fetch.

    La branche publiée d'abord, puis la branche locale, puis la tête observée
    (`branch_head`) : un clone qui n'a pas la branche peut quand même avoir
    le commit du dernier relevé. Aucune ref : None."""
    from . import plan_git

    branch = str(carrier.get("branch") or "").strip()
    refs = []
    if branch:
        refs += [plan_git.published_target(repo, branch), branch,
                 "refs/remotes/origin/" + branch, "refs/heads/" + branch]
    head = str(carrier.get("branch_head") or "").strip()
    if head:
        refs.append(head)
    for ref in refs:
        if plan_git._commit(repo, ref):
            return ref
    return None


def packet_trailers(repo, carrier, target=None, *, head=None):
    """#2599 : les lots liés par les trailers `ameesh-work:` de la branche.

    Le corps du lot porteur peut manquer la liste « #N <tête> » (paquets
    h-2300 et h-0100) ; les commits de composition `vehicule …` portent, eux,
    leur `ameesh-work: <lot>`. On ne lit que les commits **au-dessus de la
    cible** (`<merge-base>..<branche>`) : les trailers hérités de `main` ne
    désignent pas les unités de ce paquet. `head` (#2837) lit un commit exact
    — la tête observée du porteur quand le dépôt a été retrouvé par elle — au
    lieu d'une ref de clone qui peut être en retard. Rend un ensemble, ou None
    quand la branche n'est pas lisible — un doute ne tait aucune alerte."""
    from . import plan_git

    ref = head or _carrier_ref(repo, carrier)
    if ref is None:
        return None
    rev = ref
    if target:
        target = plan_git.published_target(repo, target)
        base = plan_git._out(repo, "merge-base", ref, target)
        if not base:
            return None
        rev = "%s..%s" % (base, ref)
    text = plan_git._out(repo, "log", "--max-count=%d" % MAX_PACKET_COMMITS,
                         "--format=%B%x00", rev)
    if text is None:
        return None
    found: set[int] = set()
    for message in text.split("\x00"):
        found.update(plan_git.cited_lots(message))
    return found


def _carrier_repo(db, cfg, carrier, *, overview=None):
    """Le dépôt local et la tête exacte où lire la composition (#2599, #2837).

    `radar.projects` d'abord : l'hôte opérateur connaît le dépôt du projet, et
    la ref publiée qu'il y lit est la plus fraîche (tête rendue vide : c'est
    `packet_trailers` qui choisit la ref). Un hôte d'agents ne l'a pas
    forcément ; on y retrouve alors un clone qui résout la tête observée
    (`branch_head`), la ref publiée de la branche ou la branche locale — la
    même découverte que `lot_class.repo_for_freeze` (#2564), sans quoi un
    porteur composé sur cet hôte restait invisible et ses unités levaient un
    faux retard `verdict_merge`. La tête résolue est rendue avec le dépôt :
    lire ce commit exact plutôt qu'une ref de clone en retard garantit qu'un
    lot retiré du véhicule ne reste pas attaché. Sans dépôt, aucun
    attachement : le doute ne tait aucune alerte."""
    from . import lot_class

    repo = lot_class.repository(cfg, carrier)
    if repo:
        return repo, ""
    if db is None:
        return None, ""
    branch = str(carrier.get("branch") or "").strip()
    refs = [str(carrier.get("branch_head") or "").strip()]
    if branch:
        refs += ["refs/remotes/origin/" + branch, branch]
    for ref in refs:
        if not ref:
            continue
        try:
            repo, commit = lot_class.repo_for_freeze(
                db, cfg, carrier, ref, overview=overview)
        except Exception:  # noqa: BLE001 - registre ou clone illisible : doute
            continue
        if repo:
            return repo, str(commit or "")
    return None, ""


def packet_attachments(cfg, open_items, rows=(), *, db=None):
    """#2599 : `{porteur: lots}` lus dans les trailers de sa branche.

    `cfg` est la configuration de l'hôte qui lit : le dépôt vient de
    `radar.projects.<app>.repo` (`lot_class.repository`). `db` (#2837) sert
    de repli quand cet hôte n'a pas les réglages radar : un clone du registre
    qui résout la tête du porteur suffit à lire ses commits de composition.
    Le census est `open_items` plus `rows` (même univers que `packet_waits`).
    Sans dépôt, sans cible ou sans branche lisible, le porteur n'a pas
    d'attachement — sa liste de corps reste la seule preuve, et aucune alerte
    n'est tue par un doute (le sens sûr)."""
    from . import lot_class, plan_git

    universe: dict[int, dict] = {}
    for row in open_items or ():
        universe[int(row["id"])] = row
    for row in rows or ():
        universe.setdefault(int(row["id"]), row)
    carriers = packet_carriers(universe.values())
    if not carriers:
        return {}
    repos = {cid: lot_class.repository(cfg, carrier) if cfg is not None else None
             for cid, carrier in carriers.items()}
    overview = None
    if db is not None and any(repo is None for repo in repos.values()):
        try:
            from . import registry
            overview = registry.overview(db)
        except Exception:  # noqa: BLE001 - registre illisible : pistes locales seules
            overview = []
    out: dict[int, set[int]] = {}
    for cid, carrier in carriers.items():
        try:
            repo, head = ((repos[cid], "") if repos[cid]
                          else _carrier_repo(db, cfg, carrier, overview=overview))
            if not repo:
                continue
            target = lot_class.default_base(cfg, carrier, repo)
            if not target:
                target = plan_git.default_target(repo)
            if not target:
                continue
            members = packet_trailers(repo, carrier, target, head=head or None)
        except Exception:  # noqa: BLE001 - dépôt illisible : aucune conclusion
            continue
        if members:
            out[cid] = members
    return out


def _packet_wait(carrier):
    """L'attente d'un lot inclus dans un paquet ouvert (forme `planned_wait`)."""
    out = {"what": "packet", "vehicle": int(carrier["id"])}
    branch = str(carrier.get("branch") or "").strip()
    if branch:
        out["branch"] = branch
    head = str(carrier.get("branch_head") or "").strip()
    if head:
        out["head"] = head
    return out


def recorded_packet_attachments(st, open_items, rows):
    """#4082: read current packet membership without a local Git clone.

    Membership is replaced by record_packet on recomposition. Bind it to the
    observed carrier head and unit head, so stale records cannot hide work
    excluded from the current packet or added since its composition.
    """
    from . import plan_git

    carriers = packet_carriers(open_items)
    if not carriers:
        return {}
    try:
        members = st.work.packet_members(package_ids=list(carriers),
                                         item_ids=[int(r["id"]) for r in rows])
        if not members:
            return {}
        marks = {}
        for mark in st.progress.lot_milestones(
                sorted({int(m["work_item_id"]) for m in members})):
            marks.setdefault(int(mark["work_item_id"]), []).append(mark)
    except Exception:  # unreadable membership never suppresses an alert
        return {}
    units = {int(r["id"]): r for r in rows}
    out = {}
    for member in members:
        cid, uid = int(member["package_id"]), int(member["work_item_id"])
        carrier, unit = carriers.get(cid), units.get(uid)
        if carrier is None or unit is None or unit.get("state") not in lifecycle.TO_VALIDATE:
            continue
        # #4111: composition precedes the branch sweep. Until a head is
        # observed, use the last freeze, then the recorded composition itself.
        # An observed head always wins: a later adaptation invalidates the record.
        carrier_head = (carrier.get("branch_head")
                        or member.get("package_frozen_sha")
                        or member.get("head_sha"))
        if not plan_git.same_commit(member.get("head_sha"), carrier_head):
            continue
        frozen = latest_freeze(marks.get(uid, ())) or {}
        current = unit.get("branch_head") or frozen.get("sha")
        source = member.get("source_sha") or member.get("frozen_sha")
        if not current or not source or not plan_git.same_commit(current, source):
            continue
        if frozen and not plan_git.same_commit(frozen["sha"], source):
            continue
        out.setdefault(cid, set()).add(uid)
    return out


def packet_waits(rows, open_items=None, attachments=None):
    """#2335 : `{lot: wait}` pour tout lot ouvert nommé par un paquet.

    Deux preuves : le manifeste du porteur — une unité nommée avec sa tête
    gelée (`#<lot> <hex>` / `#<lot> @ <hex>`) — et, depuis #2599, les
    `attachments` lues dans les trailers `ameesh-work:` des commits de sa
    branche (`packet_attachments`), qui complètent un corps sans liste. Un
    simple `#N` en prose est ignoré (B1 de la relecture dev7) : un lot exclu
    du paquet doit continuer d'alerter, sinon son attente serait tue par un
    porteur qui ne le transporte pas.

    Un lot inclus attend la **fusion du paquet**, pas un exécuteur : cette
    attente est planifiée et tait la stagnation comme le SLO (#2335/A1).
    """
    universe: dict[int, dict] = {}
    for row in open_items or ():
        universe[int(row["id"])] = row
    for row in rows or ():
        universe.setdefault(int(row["id"]), row)
    carriers = packet_carriers(universe.values())
    if not carriers:
        return {}
    open_ids = {int(r["id"]) for r in universe.values()
                if r.get("state") not in lifecycle.COMPLETED}
    declared: dict[int, dict] = {}
    for cid, carrier in carriers.items():
        text = "%s\n%s" % (carrier.get("title") or "", carrier.get("body") or "")
        for match in _UNIT.finditer(text):
            ident = int(match.group(1))
            if ident in open_ids and ident != cid:
                declared.setdefault(ident, carrier)
        for ident in (attachments or {}).get(cid, ()):
            try:
                ident = int(ident)
            except (TypeError, ValueError):
                continue
            if ident in open_ids and ident != cid:
                declared.setdefault(ident, carrier)
    out: dict[int, dict] = {}
    for row in rows or ():
        ident = int(row["id"])
        if row.get("state") in lifecycle.COMPLETED:
            continue
        carrier = declared.get(ident)
        if carrier is not None:
            out[ident] = _packet_wait(carrier)
    return out


def scheduled_p1(row, plan) -> bool:
    """#5151 : la date de ce lot est-elle une projection du planificateur sur
    un lot de priorité 1 ? `work schedule` (source « reference p80 … ») date
    un lot à son tour de file ; pour un P1, cette date ne vaut pas attente
    (un P1 ne dort pas derrière sa propre projection). Une date posée par une
    personne reste une attente, même en P1 ; sans priorité lue, la date vaut
    (le sens prudent des appelants qui ne la lisent pas)."""
    from .lot_schedule import SOURCE  # lot_schedule imports work, not this module
    try:
        priority = int((row or {}).get("priority") or 0)
    except (TypeError, ValueError):
        return False
    source = str((plan or {}).get("planned_source")
                 or (row or {}).get("planned_source") or "")
    return priority == 1 and source.startswith(SOURCE)


def resolve(rows, dates, commitments, packages, open_items, *, now, dependencies=(),
            landings=(), holds=None, decisions=(), packets=None):
    """Pure resolver. Only an open execution lot can block another lot.

    #5151: a future planned start written by `work schedule` on a P1 lot
    (`scheduled_p1`) is not a wait; one set by a person still is.

    Commitments apply to their explicit work item or to their plan package
    (including descendants). Dependencies are an anchored numeric lot
    reference or an exact WorkPackage identifier. Unknown and self references
    cannot suppress an alert. Proposed/cancelled/done commitments are ignored.

    An open commitment whose `depends_on` names an external gate
    (`gate:<name>` or `external:<name>`) *holds* its lot until the end of the
    commitment's due day: the gate is not a lot, so nothing else can release
    it, and the date is what makes the hold end by itself.

    #1945: a declared landing (`landings`, rows of `work_landings` enriched
    with the vehicle's state and last freeze) is an admission PINNED to one
    freeze. It waits only while the vehicle is open, not `blocked`, its last
    freeze is exactly `pinned_sha`, and the finite day `until_on` is not past
    (its day counts in full, like a hold). A new freeze, a missing freeze or a
    blocked vehicle gives the signal back until the pilot renews the
    declaration explicitly. A scheduled start, a dependency or a gate stays
    ahead: the more explicit wait wins. Nothing is inferred from prose or git.

    #5028/#5037: a future scheduled wake (`wake_ts`) is a planned wait even
    without a hold, in `build` too; it is only the **fallback**, read after
    the explicit waits (pilot hold, decision, packet, dependency, gate) and
    after the declared landing, so it never displays "scheduled wake" over a
    hold's holder or gate (N1 of #5028's review).

    `holds` (#1664, `pilot_holds`) est un hold déclaré, prioritaire sur une
    dépendance : le lot est parqué, pas en attente d'un lot.

    `decisions` (#2378) : les demandes de décision en attente
    (`decisions.pending`). Un lot `waiting_human` dont les demandes ouvertes
    ne sont pas échues attend son décideur (`decision_waits`), après un hold
    du pilote et avant une dépendance ; échue, la demande ne tait plus rien.

    `packets` (#2335, `packet_waits`) est l'appartenance à un paquet ouvert :
    le lot attend la fusion du porteur, pas un exécuteur ; cette attente passe
    avant une dépendance (le paquet est le véhicule réel).
    """
    open_ids = {int(r["id"]) for r in open_items
                if r.get("state") not in lifecycle.COMPLETED}
    landing_by_item = {int(l["work_item_id"]): l for l in landings}
    package_lots = {}
    for row in open_items:
        if int(row["id"]) not in open_ids:
            continue
        for package_id in ancestors(row.get("package_id"), packages):
            package_lots.setdefault(package_id, set()).add(int(row["id"]))
    direct, by_package = {}, {}
    gate_item, gate_package = {}, {}
    for edge in dependencies:
        if edge.get("blocking"):
            direct.setdefault(int(edge["work_item_id"]), set()).add(int(edge["blocker_id"]))
    for commitment in commitments:
        if commitment.get("status") != "open":
            continue
        blockers = set()
        gate = None
        for reference in commitment.get("depends_on") or []:
            if not isinstance(reference, str):
                continue
            reference = reference.strip()
            match = _GATE.fullmatch(reference)
            if match:
                gate = gate or match[1]
                continue
            match = _LOT.fullmatch(reference)
            if match:
                blockers.update({int(match[1])} & open_ids)
            else:
                package_ref = _PACKAGE.fullmatch(reference)
                blockers.update(package_lots.get(package_ref[1] if package_ref else reference, ()))
        if commitment.get("work_item_id") is not None:
            direct.setdefault(int(commitment["work_item_id"]), set()).update(blockers)
        elif commitment.get("package_id"):
            by_package.setdefault(commitment["package_id"], set()).update(blockers)
        end = hold_end(commitment.get("due_on")) if gate else None
        if end is not None and now < end:
            held = {"gate": gate, "until": str(commitment.get("due_on"))[:10]}
            if commitment.get("id") is not None:
                held["commitment"] = commitment["id"]
            if commitment.get("work_item_id") is not None:
                gate_item.setdefault(int(commitment["work_item_id"]), held)
            elif commitment.get("package_id"):
                gate_package.setdefault(commitment["package_id"], held)
    waits = {}
    # #1664 : les holds du pilote (dict lot -> hold), distincts du `held` de
    # porte par lot utilisé plus bas (collision de noms à l'union avec main).
    pilot = holds or {}
    asked = decision_waits(decisions, now=now)
    packet = packets or {}
    for row in rows:
        ident = int(row["id"])
        if row.get("state") in lifecycle.COMPLETED:
            continue
        start = dates.get(ident, {}).get("planned_start")
        start_package = None
        if start and scheduled_p1(row, dates.get(ident)):
            start = None
        if not start:
            for package_id in ancestors(row.get("package_id"), packages):
                start = plan.planned_dates(packages[package_id])["start"]
                if start:
                    start_package = package_id
                    break
        start_ts = plan.day_start_ts(start)
        if start_ts is not None and start_ts > now:
            waits[ident] = {"what": "scheduled", "start": start}
            if start_package:
                waits[ident]["package"] = start_package
            continue
        if ident in pilot:
            waits[ident] = dict(pilot[ident])
            continue
        if row.get("state") == "waiting_human" and ident in asked:
            waits[ident] = dict(asked[ident])
            continue
        if ident in packet:
            waits[ident] = dict(packet[ident])
            continue
        blockers = set(direct.get(ident, ()))
        for package_id in ancestors(row.get("package_id"), packages):
            blockers.update(by_package.get(package_id, ()))
        blockers.discard(ident)
        if blockers:
            waits[ident] = {"what": "dependency", "lots": sorted(blockers)}
            continue
        held = gate_item.get(ident)
        if held is None:
            for package_id in ancestors(row.get("package_id"), packages):
                held = gate_package.get(package_id)
                if held:
                    break
        if held:
            waits[ident] = dict(held, what="hold")
            continue
        # #1945 (revue B) : l'admission est épinglée au gel du véhicule. Elle
        # n'éteint que si le véhicule est ouvert, non bloqué, et si son dernier
        # gel est EXACTEMENT la tête épinglée ; l'échéance (journée finie,
        # comptée en entier) doit être future. Nouveau gel, gel absent, véhicule
        # bloqué, retrait ou échéance dépassée : le signal stale revient, et le
        # pilote renouvelle explicitement la déclaration sur la nouvelle tête.
        # #2171 N3 : le lot parké doit être EN REVUE (qa) — un atterrissage ne
        # remplace que l'attente de fusion ; sorti de qa (build, blocked,
        # waiting_human), le signal revient.
        landing = landing_by_item.get(ident)
        if (landing and row.get("state") in lifecycle.TO_VALIDATE
                and landing.get("admitted") is not False):
            vehicle = int(landing["vehicle_id"])
            open_vehicle = landing.get("open")
            if open_vehicle is None:
                open_vehicle = vehicle in open_ids
            etat = landing.get("vehicle_state")
            pinned = (landing.get("pinned_sha") or "").strip()
            tete = (landing.get("head") or "").strip()
            end = hold_end(landing.get("until_on"))
            if not (vehicle == ident or not open_vehicle or etat == "blocked"
                    or not pinned or not tete or pinned != tete
                    or end is None or now >= end):
                wait = {"what": "landing", "vehicle": vehicle,
                        "until": str(landing.get("until_on"))[:10], "head": pinned}
                if etat:
                    wait["step"] = etat
                waits[ident] = wait
                continue
        # #5028/#5037 : un réveil futur est une attente, même sans hold et en
        # build — mais une attente de **repli**, après les attentes explicites
        # (hold du pilote, décision, paquet, dépendance, porte) et
        # l'atterrissage déclaré. Il ne remplace donc plus « hold by X » ou
        # « external hold until … (gate) » (N1 de la revue de #5028).
        wake = row.get("wake_ts")
        if wake is not None and float(wake) > now:
            start = datetime.datetime.fromtimestamp(
                float(wake), datetime.timezone.utc).isoformat()
            waits[ident] = {"what": "scheduled", "start": start,
                            "wake_ts": float(wake)}
    return waits


def latest_freeze(rows):
    """Le dernier gel d'un lot : `(at, id)` maximal, jamais l'ordre de lecture.

    #2171 N2 : `set_landing` épingle avec `ORDER BY at DESC, id DESC` ; tous les
    lecteurs doivent départager un ex aequo de la même façon, sinon la tête
    comparée à `pinned_sha` peut différer à tort (sens sûr : l'alerte revient).
    Les jalons non `frozen` ou sans sha ne comptent pas."""
    freezes = [r for r in rows if r.get("kind") == "frozen"
               and (r.get("sha") or "").strip()]
    if not freezes:
        return None
    return max(freezes, key=lambda r: (float(r.get("at_ts") or 0),
                                       int(r.get("id") or 0)))


def _admitted(st, rows, landings):
    """#2171 N3 (revue) : l'admission se relit sur le LOT, pas seulement au geste.

    Le chemin groupé ne doit pas parquer un lot qui a perdu son gel ou son
    verdict acquis depuis la déclaration (nouveau gel sans verdict, retour en
    `qa` après un build) : même vérité que `progress._jalons_de_lot`, donc le
    signal stale revient comme dans `progress`. Trois lectures groupées, pour
    les seuls lots déclarés."""
    if not landings:
        return []
    from . import progress
    ids = sorted({int(l["work_item_id"]) for l in landings})
    by_id = {int(r["id"]): r for r in rows}
    transitions, actions, declared = {}, {}, {}
    for row in st.progress.lot_transitions(ids):
        transitions.setdefault(int(row["work_item_id"]), []).append(row)
    for row in st.progress.lot_actions(ids):
        actions.setdefault(int(row["work_item_id"]), []).append(row)
    for row in st.progress.lot_milestones(ids):
        declared.setdefault(int(row["work_item_id"]), []).append(row)
    out = []
    for landing in landings:
        ident = int(landing["work_item_id"])
        item = by_id.get(ident)
        if item is None:
            out.append(landing)
            continue
        jalons = progress._jalons_de_lot(
            item, transitions.get(ident, []), actions.get(ident, []),
            declared.get(ident, []))
        admitted = (jalons.get("last_frozen") is not None
                    and jalons.get("verdict_kind") == "ok"
                    and bool(jalons.get("verdict_current")))
        out.append(dict(landing, admitted=admitted))
    return out


def _with_heads(st, landings):
    """#1945 : la tête exacte du véhicule est son dernier gel, lu en base.

    Une lecture groupée pour tous les véhicules déclarés, comparée à chaque
    passe à la tête épinglée dans `work_landings` (`pinned_sha`) : un nouveau
    gel ne prolonge donc rien sans renouvellement explicite. Le départage à
    égalité d'instant est celui de `latest_freeze` (#2171 N2)."""
    rows = list(landings)
    vehicles = sorted({int(l["vehicle_id"]) for l in rows})
    by_vehicle: dict[int, list] = {}
    if vehicles:
        for mark in st.progress.lot_milestones(vehicles):
            by_vehicle.setdefault(int(mark["work_item_id"]), []).append(mark)
    heads = {vid: latest_freeze(marks) for vid, marks in by_vehicle.items()}
    out = []
    for landing in rows:
        head = heads.get(int(landing["vehicle_id"]))
        out.append(dict(landing, head=head["sha"].strip()) if head else landing)
    return out


def read(db, rows, *, now=None, packages=None, open_items=None, dependencies=None,
         holds=None, decisions=None, packets=None):
    """Grouped, unbounded truth reads; no query per candidate or dependency.

    `decisions` (#2378) : les demandes en attente, lues ici en une lecture
    groupée seulement quand un lot est en `waiting_human` ; `()` les ignore.

    `holds` peut être fourni par un appelant qui a déjà lu le flux ciblé
    (`pilot_holds`) : sinon il est lu ici, en une lecture groupée (#1664) qui
    ne transfère que les transitions et les notes hold/levée — jamais le
    journal entier ni le texte des notes ordinaires.

    `packets` (#2335) : marquer les unités d'un porteur `integration/**`
    ouvert comme attente de paquet. `None` (défaut) réutilise le census déjà
    fourni (`open_items`) ou déjà lu pour les engagements ; `True` le force ;
    `False` l'interdit. Un appelant qui n'a pas le census ne paie donc pas une
    lecture `open_lots_activity(None)` en plus (N1 de la relecture dev7).

    #2599 : quand un porteur est lu, les `ameesh-work:` de sa branche
    (`packet_attachments`) complètent le corps, qui peut manquer la liste
    « #N <tête> ». Dépôt ou branche illisibles : aucun attachement, l'alerte
    reste due (le sens sûr).

    #4082 : la composition enregistrée en base complète ces preuves sans
    clone local ; elle n'est valable qu'aux têtes composées encore courantes.
    Sa suppression par recomposition ou la fermeture du porteur la lève.
    """
    if not rows:
        return {}
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    ids = [int(r["id"]) for r in rows]
    if dependencies is None:
        dependencies = st.work_dependencies.for_items(ids)
    if holds is None:
        holds = pilot_holds(db, st.progress.lot_hold_events(ids, HOLD_NOTE_PATTERNS))
    dates = {int(r["id"]): r for r in st.roadmap.item_plans(ids)}
    commitments = st.roadmap.commitments(statuses=["open"])
    packages = (packages if packages is not None else
                {p["id"]: p for p in st.packages.all(include_absent=True)})
    supplied = open_items is not None
    if open_items is None:
        if packets is True or any(c.get("depends_on") for c in commitments):
            open_items = st.operations.open_lots_activity(None)
        else:
            open_items = []
    elif packets is True and not open_items:
        open_items = st.operations.open_lots_activity(None)
    want_packets = bool(packets) if packets is not None else (supplied or bool(open_items))
    attached = (packet_attachments(getattr(db, "cfg", None), open_items, rows,
                                   db=db)
                if want_packets and not _dry_pass(db) else {})
    attached = {cid: set(members) for cid, members in attached.items()}
    if want_packets and not _dry_pass(db):
        for cid, members in recorded_packet_attachments(st, open_items, rows).items():
            attached.setdefault(cid, set()).update(members)
    packet = packet_waits(rows, open_items, attachments=attached) if want_packets else {}
    landings = _with_heads(st, _admitted(
        st, rows, st.work_dependencies.landings_for([int(r["id"]) for r in rows])))
    if decisions is None:
        decisions = ()
        if any(r.get("state") == "waiting_human" for r in rows):
            from . import decisions as decisions_mod  # import tardif : decisions importe work
            decisions = decisions_mod.pending(db, now)
    return resolve(rows, dates, commitments, packages, open_items, now=now,
                   dependencies=dependencies, landings=landings, holds=holds,
                   decisions=decisions, packets=packet)
