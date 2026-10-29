# SPDX-License-Identifier: AGPL-3.0-only
"""#3070 (canon §12) : un appareil indisponible ne retarde pas la prod.

Règle owner du 23/10/2026 : quand la qualification d'un lot attend une séance
sur un appareil déclaré indisponible, le test est réputé conforme ; le lot
avance sans cette séance, avec la trace « qualification physique reportée » et
un lot de vérification à jouer au retour de l'appareil, qui ne bloque rien.

Rien n'est deviné de la prose. Deux déclarations explicites seulement :

* **l'appareil** : une fiche Host du canon (`hotes/`) porte
  `indisponible_jusqu_au: AAAA-MM-JJ` (jour inclus) et `indisponible_motif` ;
  un appareil physique sans agent y a sa fiche, `policy.max_agents: 0`.
  Passé ce jour, la déclaration cesse d'elle-même : la porte ne se lève plus ;
* **la séance** : le lot `blocked` porte, depuis son entrée en `blocked`, une
  note `séance: <appareil>[, <appareil>…] — <cas non joués>` (forme écrite par
  `ameesh work move N blocked --device <appareil> --note "<cas>"` ; une retenue
  `hold: séance: …` vaut la même déclaration).

Le lot sort de `blocked` (`lift`) seulement si TOUT ce qui le retient est une
séance sur un appareil indisponible : chaque appareil nommé a une fiche et est
indisponible maintenant ; aucune dépendance bloquante ouverte, aucune demande
de décision ouverte, aucune autre retenue `hold:` en vigueur. Un blocage mixte
reste bloqué, et un doute (appareil inconnu, déclaration illisible) aussi. Le
lot revient à l'état qu'il avait avant `blocked` ; le lot de vérification est
créé dans la même transaction, sans assigné, priorité basse, début prévu le
lendemain du retour (la prise automatique l'attend), sans arête de dépendance.
"""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

import datetime
import re
import time

from . import storage, work
from .storage.interface import HOLD_NOTE_WHITESPACE, LotSessions

#: la note qui déclare la séance attendue (`séance:` ou `seance:`)
SESSION_PREFIX = "séance:"
_SESSION = re.compile(r"^s[ée]ance\s*:\s*(?P<devices>[^—;]*?)\s*(?:[—;]\s*(?P<cases>.*))?$",
                      re.I | re.S)
_DEVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
#: une note qui SE DONNE pour une séance : illisible, elle retient le lot
_SESSION_START = re.compile(r"^s[ée]ance\s*:", re.I)
_HOLD = re.compile("^" + re.escape(LotSessions.HOLD_PREFIX) + r"\s*", re.I)
_SPACE = "[" + HOLD_NOTE_WHITESPACE + "]"
_LIFT = re.compile(
    f"^(?:unhold|hold{_SPACE}+(?:l[iİı]fted|relea[sſ]ed|cleared)){_SPACE}*:", re.I)
#: la trace écrite sur le lot qui sort de `blocked` (canon §12)
TRACE = "qualification physique reportée"
#: titre des lots de vérification : ils attendent l'appareil, jamais levés
VERIFY_TITLE = "Vérification au retour"
#: états où le lot revient ; tout autre état d'avant `blocked` revient en build
RETURN_STATES = (lifecycle.REQUESTED, lifecycle.IN_PROGRESS, lifecycle.QA, lifecycle.VALIDATED, "merged")
ACTOR = "device-gates"
#: borne de la lecture des lots `blocked` d'une passe
BLOCKED_LIMIT = 1000


def session_note(devices, cases: str = "") -> str:
    """La note de déclaration d'une séance (`work move --device`)."""
    names = [d.strip() for d in devices if d and d.strip()]
    bad = [d for d in names if not _DEVICE.match(d)]
    if not names or bad:
        raise work.WorkError("--device: device name expected (letters, digits, . _ -), got %s"
                             % ", ".join(repr(d) for d in (bad or devices)))
    text = "%s %s" % (SESSION_PREFIX, ", ".join(dict.fromkeys(names)))
    cases = (cases or "").strip()
    return "%s — %s" % (text, cases) if cases else text


def parse_session(text) -> dict | None:
    """`{devices, cases}` d'une note `séance: …`, ou None (autre note ou
    déclaration illisible : aucun appareil, ou un nom invalide)."""
    match = _SESSION.match(str(text or "").strip())
    if not match:
        return None
    names = [n for n in re.split(r"[,\s]+", match.group("devices") or "") if n]
    if not names or not all(_DEVICE.match(n) for n in names):
        return None
    return {"devices": list(dict.fromkeys(names)),
            "cases": (match.group("cases") or "").strip()}


def episode(events) -> dict | None:
    """Le séjour courant du lot en `blocked`, lu dans son journal.

    Rend `{before, devices, cases, holds, unreadable}` : l'état d'avant
    `blocked`, les appareils déclarés depuis l'entrée (union, dans l'ordre),
    les cas non joués, les autres retenues `hold:` encore en vigueur (non
    levées) et les déclarations de séance illisibles (#3070 B2 : une seule
    suffit à garder le lot bloqué, même à côté d'une déclaration lisible) ;
    None si le lot n'est pas `blocked` ou si rien ne s'y donne pour une séance."""
    ordered = sorted(events or (), key=lambda e: (float(e.get("created_ts") or 0.0),
                                                   int(e.get("id") or 0)))
    start, before, previous = None, None, None
    for index, event in enumerate(ordered):
        state = event.get("state")
        if state == lifecycle.BLOCKED and previous != lifecycle.BLOCKED:
            start, before = index, previous
        previous = state
    if start is None or previous != lifecycle.BLOCKED:
        return None
    devices: list[str] = []
    cases: list[str] = []
    holds: list[dict] = []
    unreadable: list[str] = []
    for event in ordered[start:]:
        text = str(event.get("note") or "").strip()
        if _LIFT.match(text):
            holds = []
            continue
        held = _HOLD.match(text)
        body = text[held.end():] if held else text
        declared = parse_session(body)
        if declared is not None:
            devices.extend(d for d in declared["devices"] if d not in devices)
            if declared["cases"] and declared["cases"] not in cases:
                cases.append(declared["cases"])
        elif _SESSION_START.match(body):
            unreadable.append(text)
        elif held:
            holds = [{"by": event.get("actor") or None, "note": text}]
    if not devices and not unreadable:
        return None
    return {"before": before, "devices": devices, "cases": "; ".join(cases),
            "holds": holds, "unreadable": unreadable}


def hosts_by_name(canons) -> dict:
    """`{appareil: Host}` des fiches Host des canons configurés ; le premier
    canon qui nomme l'appareil le garde. Une fiche sans indisponibilité
    déclarée est un appareil disponible."""
    out: dict = {}
    for canon in canons or ():
        for host in getattr(canon, "hosts", None) or ():
            out.setdefault(host.title, host)
    return out


def assess(item: dict, events, *, hosts: dict, dependencies=(), decisions=(),
           now: float) -> dict | None:
    """Ce qui retient un lot `blocked` qui déclare une séance, ou None.

    `hosts` : `{nom: Host}` (`hosts_by_name`) ; `dependencies` : les
    arêtes du lot (`work_dependencies.for_items`) ; `decisions` : ses
    demandes de décision en attente. Rend `{lot, before, back_to, devices,
    cases, others, liftable}` : `devices` dit pour chaque appareil s'il est
    connu, indisponible, et jusqu'à quand ; `others` liste les autres
    blocages (texte court) ; `liftable` est vrai seulement sans autre blocage
    et avec tous les appareils indisponibles maintenant."""
    if item.get("state") != lifecycle.BLOCKED:
        return None
    found = episode(events)
    if found is None:
        return None
    devices, others = [], []
    for name in found["devices"]:
        host = hosts.get(name)
        if host is None:
            devices.append({"name": name, "known": False, "unavailable": False})
            others.append("device %s has no Host fiche in the canon" % name)
            continue
        out = {"name": name, "known": True,
               "unavailable": bool(host.unavailable_at(now)),
               "until": host.unavailable_until, "reason": host.unavailable_reason}
        devices.append(out)
        if not out["unavailable"]:
            others.append("session on %s, available%s" % (
                name, " since %s" % host.unavailable_until if host.unavailable_until else ""))
    for edge in dependencies or ():
        if edge.get("blocking"):
            others.append("open dependency on lot #%s" % edge["blocker_id"])
    for request in decisions or ():
        others.append("open decision request #%s" % request.get("id"))
    for hold in found["holds"]:
        others.append("hold by %s" % (hold["by"] or "unknown"))
    for text in found["unreadable"]:
        others.append("unreadable session declaration %r" % text)
    if str(item.get("workflow_ref") or ""):
        # #1922 : un lot pinné ne bouge que par le registre des processus
        others.append("pinned to Process revision %s" % item["workflow_ref"])
    before = found["before"]
    return {"lot": int(item["id"]), "before": before,
            "back_to": before if before in RETURN_STATES else lifecycle.IN_PROGRESS,
            "devices": devices, "cases": found["cases"], "others": others,
            "liftable": not others and not str(item.get("title") or "").startswith(VERIFY_TITLE)}


def label(view: dict) -> str:
    """Une ligne lisible de l'évaluation (`work show`, `work device-gates`)."""
    parts = []
    for device in view["devices"]:
        if not device["known"]:
            parts.append("%s (unknown device)" % device["name"])
        elif device["unavailable"]:
            parts.append("%s unavailable until %s%s" % (
                device["name"], device["until"],
                " (%s)" % device["reason"] if device.get("reason") else ""))
        else:
            parts.append("%s available" % device["name"])
    text = "session " + (", ".join(parts) or "declaration unreadable")
    if view["liftable"]:
        return text + " → %s, test deemed compliant: back to %s at the next pass" % (
            TRACE, view["back_to"])
    if view["others"]:
        return text + " → stays blocked: %s" % "; ".join(view["others"])
    return text + " → stays blocked (verification lot: waits for the device)"


def _return_day(view: dict) -> str:
    last = max(d["until"] for d in view["devices"] if d.get("until"))
    return (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat()


def _trace(view: dict, verify_id: int | None) -> str:
    devices = ", ".join("%s indisponible jusqu'au %s%s" % (
        d["name"], d["until"], " (%s)" % d["reason"] if d.get("reason") else "")
        for d in view["devices"])
    text = "%s : %s ; test réputé conforme (canon §12)" % (TRACE, devices)
    if view["cases"]:
        text += " ; cas non joués : %s" % view["cases"]
    if verify_id is not None:
        text += " ; lot de vérification #%d" % verify_id
    return text


def _verify_body(item: dict, view: dict) -> str:
    return "\n".join([
        "Qualification physique reportée du lot #%d (%s), canon §12%s." % (
            int(item["id"]), item.get("title") or "",
            " ; ticket %s" % item["issue_ref"] if item.get("issue_ref") else ""),
        "",
        "Appareils : %s." % ", ".join(
            "%s (indisponible jusqu'au %s)" % (d["name"], d["until"]) for d in view["devices"]),
        "Cas non joués : %s." % (view["cases"] or "non précisés"),
        "",
        "À jouer au retour de l'appareil ; ce lot ne bloque rien. Un écart "
        "trouvé devient un correctif ordinaire.",
    ])


def _assess_read(db, item: dict, *, hosts: dict, now: float) -> dict | None:
    """`assess` sur ce que la base dit maintenant du lot (journal complet,
    arêtes, demandes de décision), lu par `db` (la transaction de la levée)."""
    if item.get("state") != lifecycle.BLOCKED:
        return None
    ident = int(item["id"])
    st = storage.of(db)
    return assess(item, work.events(db, ident, None), hosts=hosts,
                  dependencies=st.work_dependencies.for_items([ident]),
                  decisions=st.decisions.pending_for_lot(str(ident)), now=now)


def of_item(db, item: dict, *, canons, now: float | None = None) -> dict | None:
    """L'évaluation d'un seul lot (`work show`), ou None hors séance déclarée."""
    return _assess_read(db, item, hosts=hosts_by_name(canons),
                        now=time.time() if now is None else float(now))


def candidates(db, *, canons, now: float | None = None) -> list[dict]:
    """Les évaluations de tous les lots `blocked` qui déclarent une séance."""
    now = time.time() if now is None else float(now)
    st = storage.of(db)
    blocked = work.list_items(db, state=lifecycle.BLOCKED, limit=BLOCKED_LIMIT)
    if not blocked:
        return []
    hosts = hosts_by_name(canons)
    ids = [int(row["id"]) for row in blocked]
    edges: dict = {}
    for edge in st.work_dependencies.for_items(ids):
        edges.setdefault(int(edge["work_item_id"]), []).append(edge)
    out = []
    for row in blocked:
        ident = int(row["id"])
        view = assess(row, work.events(db, ident, None), hosts=hosts,
                      dependencies=edges.get(ident, ()),
                      decisions=st.decisions.pending_for_lot(str(ident)), now=now)
        if view is not None:
            view["title"] = row.get("title")
            out.append(view)
    return out


def lift_one(db, ident: int, *, hosts: dict, now: float,
             actor: str = ACTOR) -> dict | None:
    """Sort un lot de `blocked` : lot de vérification puis transition, dans
    une transaction ; None (rien d'écrit) si le lot n'est plus levable.

    #3070 B1 : l'évaluation qui décide est refaite DANS la transaction, sous
    les verrous des écritures concurrentes, jamais reprise d'une lecture
    antérieure. Ordre graphe puis lot, celui de `work_dependencies.add` : le
    verrou de graphe sérialise les éditions d'arêtes et les fermetures de
    prérequis ; la ligne du lot `FOR UPDATE` (`work.lock`, #2744) tient les
    notes (`work.note` la prend), les transitions et toute insertion au journal
    (sa clé étrangère attend le verrou). Ce qui a été écrit avant les verrous
    est relu ; ce qui vient après attend la fin de la levée."""

    class _NotLiftable(Exception):
        pass

    try:
        with db.transaction() as tx:
            storage.of(tx).work_dependencies.lock()
            item = storage.of(tx).work.lock(int(ident))
            view = None if item is None else _assess_read(tx, item, hosts=hosts, now=now)
            if view is None or not view["liftable"]:
                raise _NotLiftable
            verify = work.add(
                tx, title="%s de %s : séance reportée de #%d — %s" % (
                    VERIFY_TITLE, ", ".join(d["name"] for d in view["devices"]),
                    ident, item.get("title") or ""),
                body=_verify_body(item, view), app=item.get("app") or "",
                workstream=item.get("workstream") or None, priority=3,
                source="device-gates #%d" % ident, actor=actor)
            storage.of(tx).roadmap.plan_item(int(verify["id"]),
                                             {"start": _return_day(view)},
                                             source=ACTOR, actor=actor)
            row = storage.of(tx).work.move(ident, view["back_to"], current=lifecycle.BLOCKED,
                                           loops=0, note=_trace(view, int(verify["id"])),
                                           actor=actor)
            if row is None:
                raise _NotLiftable
    except _NotLiftable:
        return None
    return {"lot": ident, "state": view["back_to"], "verify": int(verify["id"])}


def lift(db, *, canons, now: float | None = None, actor: str = ACTOR,
         dry_run: bool = False) -> list[dict]:
    """Lève les portes : chaque lot `liftable` sort de `blocked` (`lift_one`).

    La lecture groupée ne fait que choisir les candidats : chaque levée
    réévalue son lot sous verrou (`lift_one`). En dry-run, rend les
    évaluations levables sans rien écrire."""
    now = time.time() if now is None else float(now)
    hosts = hosts_by_name(canons)
    done = []
    for view in candidates(db, canons=canons, now=now):
        if not view["liftable"]:
            continue
        if dry_run:
            done.append({"lot": view["lot"], "state": view["back_to"], "dry_run": True})
            continue
        result = lift_one(db, view["lot"], hosts=hosts, now=now, actor=actor)
        if result is not None:
            done.append(result)
    return done
