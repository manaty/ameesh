"""Analyse profonde automatique de l'auditeur (L317c, demande du propriétaire
du 2026-10-12).

Deux déclencheurs réveillent l'auditeur tout de suite, sans attendre son
passage suivant :

* `bug_p1` (cœur) : un lot de type `bug` et de priorité 1 est ouvert par un
  humain (`human:<id>`, ou un humain connu) ou par son chat (`chat-<humain>`)
  — appelé par `work.add` ;
* `release_withdrawn` : une release est retirée — apporté par le module IT
  (L317a), qui appelle `request` ; le cœur n'a pas de notion de release.

La demande est une ligne du journal du lot (préfixe `REQUEST_PREFIX`) et un
courrier `event` urgent à chaque auditeur (`alert_briefs.auditors`). Le
premier rapport de l'auditeur est une note du lot qui commence par
`REPORT_PREFIX` (`ameesh work note <id> "Deep analysis: …"`) ; le délai entre
les deux est mesuré (`status`, `measure`), et l'alerte `deep_analysis_overdue`
se lève au-delà de `DEADLINE_S` sans rapport. La méthode est celle de
docs/AUDITEUR.md, « Analyse profonde ».
"""

from __future__ import annotations

import time

from . import storage

#: déclencheurs connus → libellé du courrier (`%s` : qui a déclenché)
TRIGGERS = {
    "bug_p1": "priority-1 bug opened by %s",
    "release_withdrawn": "release withdrawn by %s",
}
#: préfixe de la ligne du journal du lot qui porte la demande
REQUEST_PREFIX = "deep analysis requested"
#: préfixe (casse ignorée) de la note du lot qui porte le premier rapport
REPORT_PREFIX = "deep analysis:"
#: délai du premier rapport, mesuré depuis la demande
DEADLINE_S = 30 * 60.0
#: fenêtre relue pour l'alerte `deep_analysis_overdue`
OVERDUE_WINDOW_S = 24 * 3600.0
SENDER = "ameesh"
#: statuts du registre d'un auditeur qui ne lira pas le courrier : arrêté, ou
#: en pause (budget, compte : `blocked`)
STALLED_STATUSES = ("stopped", "blocked")
#: fin de la ligne de demande : les auditeurs à qui elle est partie
AUDITORS_MARK = "; auditor(s): "
NO_AUDITOR = "none registered"


def human_origin(db, actor: str, cfg=None) -> bool:
    """`actor` est-il un humain (`human:<id>`, ou un humain connu) ou le chat
    d'un humain (agent `chat-…` en mode externe) ? Une session qui porte une
    identité d'agent ne parle qu'en son nom : un `--actor` autre que cette
    identité n'est pas cru (#315 r1). Jamais d'exception."""
    from . import chat, identity, registry, work

    actor = (actor or "").strip()
    if not actor or actor == work.UNKNOWN_ACTOR:
        return False
    try:
        bound = identity.resolve_binding(cfg, db).name
    except Exception:
        return False
    if bound and actor != bound:
        return False
    if actor.startswith(work.HUMAN_PREFIX):
        return True
    if actor.startswith(chat.CHAT_PREFIX):
        try:
            return chat.is_chat_agent(registry.get(db, actor))
        except Exception:
            return False
    try:
        return work.known_human(db, actor, cfg) is not None
    except Exception:
        return False


def on_lot_created(db, item: dict, *, actor: str, cfg=None) -> dict | None:
    """Le déclencheur `bug_p1`, à la création d'un lot (`work.add`). None si
    le lot ne le déclenche pas. Ne lève jamais : la création du lot ne dépend
    pas du réveil de l'auditeur (l'échec est rendu dans `error`)."""
    if item.get("type") != "bug" or item.get("priority") != 1:
        return None
    if not human_origin(db, actor, cfg):
        return None
    try:
        return request(db, int(item["id"]), "bug_p1", actor=actor, cfg=cfg, item=item)
    except Exception as exc:  # noqa: BLE001 - la création du lot reste acquise
        return {"requested": False, "error": str(exc), "auditors": []}


def request(db, item_id: int, trigger: str, *, actor: str, detail: str = "",
            cfg=None, item: dict | None = None, now: float | None = None) -> dict:
    """Demande une analyse profonde de l'auditeur sur le lot `item_id`.

    Écrit la demande au journal du lot et envoie un courrier `event` urgent à
    chaque auditeur du registre ; un auditeur arrêté ou en pause (`stopped`,
    `blocked` : il ne lira pas le courrier) est signalé dans la demande, et
    le courrier part aussi, urgent, aux orchestrateurs, qui ont le geste
    (`ameesh resume`, budget). Une demande du même déclencheur encore sans
    rapport et plus jeune que `DEADLINE_S` n'est pas répétée.

    Relecture, courriers et ligne du journal forment UNE transaction sous
    verrou propre au lot (#315 r1) : deux demandes simultanées n'en font
    qu'une, et un envoi en échec n'enregistre pas la demande (une nouvelle
    tentative repart). Rend `{requested, trigger, auditors, stalled,
    orchestrators, reason?}`."""
    from . import work

    if trigger not in TRIGGERS:
        raise ValueError("unknown deep analysis trigger: %r (%s)"
                         % (trigger, ", ".join(TRIGGERS)))
    item = item or work.get(db, item_id)
    if item is None:
        raise work.WorkError("lot %s not found" % item_id)
    now = time.time() if now is None else now
    with db.transaction() as tx:
        storage.of(tx).work.lock_deep_analysis(item_id)
        return _request_locked(tx, item, item_id, trigger, actor=actor, detail=detail,
                               cfg=cfg, now=now)


def _request_locked(db, item, item_id, trigger, *, actor, detail, cfg, now) -> dict:
    from . import mail, registry, sous_utilisation, work

    current = status(db, item_id, now=now)
    if current and current["last_trigger"] == trigger and current["report_ts"] is None \
            and now - current["last_requested_ts"] < DEADLINE_S:
        return {"requested": False, "trigger": trigger, "auditors": [],
                "reason": "already requested %s ago, no report yet"
                          % work.span(now - current["last_requested_ts"])}
    listing = registry.overview(db)
    names = _auditors(db, cfg, listing)
    by_name = {row["name"]: row for row in listing}
    stalled = [n for n in names if (by_name.get(n) or {}).get("status") in STALLED_STATUSES]
    orchestras = []
    if stalled or not names:
        orchestras = [n for n in sous_utilisation.orchestrators(
            cfg, db, listing, declared=sous_utilisation.declared_orchestrators())
            if n not in names]
    why = TRIGGERS[trigger] % (actor or "?")
    if detail:
        why += " (%s)" % detail
    text = message(item, why)
    payload = {"urgent": True, "deep_analysis": trigger, "lot": int(item_id)}
    for name in names:
        mail.send(db, SENDER, name, text, kind="event", work_item_id=str(item_id),
                  payload=payload)
    if orchestras:
        held = ("auditor(s) %s not running (%s): resume them, or have the analysis done"
                % (", ".join(stalled), ", ".join(
                    "%s %s" % (n, by_name[n].get("status")) for n in stalled))
                if stalled else "no auditor registered: have the analysis done")
        for name in orchestras:
            mail.send(db, SENDER, name, "%s\n%s." % (text, held), kind="event",
                      work_item_id=str(item_id), payload=dict(payload, stalled=stalled))
    note = "%s (%s): %s" % (REQUEST_PREFIX, trigger, why)
    if stalled:
        note += " [not running: %s; orchestrator(s) told: %s]" % (
            ", ".join(stalled), ", ".join(orchestras) or "none")
    # dernier : la liste des auditeurs, relue par `status` (seul un auditeur
    # nommé solde la mesure)
    note += "%s%s" % (AUDITORS_MARK, ", ".join(names) or NO_AUDITOR)
    storage.of(db).work.note(item_id, item["state"], note, SENDER)
    return {"requested": True, "trigger": trigger, "auditors": names,
            "stalled": stalled, "orchestrators": orchestras}


def message(item: dict, why: str) -> str:
    """Le courrier à l'auditeur : le lot, la chaîne à remonter, le délai."""
    return (
        "Deep analysis requested (L317c): %s — lot #%d \"%s\". Go up the chain and "
        "record it on the lot: which lot delivered the defect (commit, PR), which "
        "review let it pass (verdict, reviewer), which gate was missing; end with one "
        "follow-up lot per missing gate. First report on the lot within %d min, "
        "measured: ameesh work note %d \"Deep analysis: …\". Method: docs/AUDITEUR.md, "
        "« Analyse profonde »."
        % (why, int(item["id"]), item.get("title") or "?", int(DEADLINE_S // 60),
           int(item["id"])))


def _auditors(db, cfg, listing=None) -> list[str]:
    from . import alert_briefs, registry, work

    if listing is None:
        listing = registry.overview(db)
    return alert_briefs.auditors(listing, work.configured_canons(cfg))


def _is_request(row: dict) -> bool:
    return (row.get("note") or "").startswith(REQUEST_PREFIX)


def _is_report(row: dict, auditors) -> bool:
    """Une note `Deep analysis: …` d'un auditeur nommé par la demande (ou de
    la persona auditeur) : celle d'un autre acteur ne solde pas la mesure
    (#315 r1)."""
    from .alert_briefs import AUDITOR_NAME

    actor = (row.get("actor") or "").strip()
    return (row.get("note") or "").strip().casefold().startswith(REPORT_PREFIX) \
        and actor != SENDER and (actor in auditors or actor == AUDITOR_NAME)


def _named(note: str) -> set:
    """Les auditeurs nommés par une ligne de demande."""
    if AUDITORS_MARK not in note:
        return set()
    names = note.rsplit(AUDITORS_MARK, 1)[1].strip()
    if names == NO_AUDITOR:
        return set()
    return {n.strip() for n in names.split(",") if n.strip()}


def _trigger(note: str) -> str:
    rest = note[len(REQUEST_PREFIX):].lstrip()
    if rest.startswith("("):
        return rest[1:].split(")", 1)[0]
    return ""


def status(db, item_id: int, *, now: float | None = None, events=None) -> dict | None:
    """La dernière demande d'analyse du lot et son premier rapport, ou None.

    `{trigger, requested_ts, report_ts, reporter, delay_s, overdue,
    last_trigger, last_requested_ts}` : `delay_s` est le délai du rapport, ou
    l'attente en cours sans rapport ; `overdue` : au-delà de `DEADLINE_S`.
    Une demande répétée avant le rapport ne remet pas la mesure à zéro : elle
    court depuis la première (#315 r1) ; `last_*` disent la dernière."""
    now = time.time() if now is None else now
    rows = events if events is not None else storage.of(db).work.events(item_id, 500)
    rows = sorted(rows, key=lambda r: (r.get("created_ts") or 0, r.get("id") or 0))
    found = None
    for row in rows:
        if _is_request(row):
            if found is None or found["report_ts"] is not None:
                found = {"trigger": _trigger(row["note"]), "requested_ts": row["created_ts"],
                         "report_ts": None, "reporter": None, "auditors": set()}
            found.update(last_trigger=_trigger(row["note"]),
                         last_requested_ts=row["created_ts"])
            found["auditors"] |= _named(row["note"])
        elif found is not None and found["report_ts"] is None \
                and _is_report(row, found["auditors"]):
            found.update(report_ts=row["created_ts"], reporter=row.get("actor"))
    if found is None:
        return None
    found["auditors"] = sorted(found["auditors"])
    end = found["report_ts"] if found["report_ts"] is not None else now
    found["delay_s"] = max(0.0, float(end) - float(found["requested_ts"]))
    found["overdue"] = found["delay_s"] > DEADLINE_S
    return found


def measure(db, *, since_ts: float, now: float | None = None) -> list[dict]:
    """Les demandes depuis `since_ts`, une par lot (la dernière), avec leur
    délai : la mesure du « premier rapport dans les 30 min »."""
    now = time.time() if now is None else now
    out = []
    for item_id in sorted({int(r["work_item_id"]) for r in
                           storage.of(db).work.events_noted(REQUEST_PREFIX, since_ts)}):
        found = status(db, item_id, now=now)
        if found is not None:
            out.append(dict(found, lot=item_id))
    return out


def overdue(db, now: float | None = None) -> list[dict]:
    """Les demandes des dernières 24 h restées sans rapport au-delà du délai."""
    now = time.time() if now is None else now
    return [m for m in measure(db, since_ts=now - OVERDUE_WINDOW_S, now=now)
            if m["report_ts"] is None and m["overdue"]]
