# SPDX-License-Identifier: AGPL-3.0-only
"""#3007: a pending decision no living notifier can push to its human.

`ameesh notify` pushes alerts to the responsible human on the channels of the
host that runs it (`notify.routes`, `notify.default`). In practice one host
(`pc-smichea`) holds the only routes and runs the service; when it is lost the
rest of the mesh keeps reading the shared database — pending and overdue
decisions, `ameesh decisions` — but nothing reaches the human, and no other
host has a route. The only witness was `ameesh notify --test human:<id>`
(exit 2, no channel), which an agent runs only if it thinks of it (incident
#3006, 2026-10-21: decision #123440 overdue and unanswered).

**Shared proof, not a local guess.** A host without routes is the normal case
on the VMs, so "no channel on THIS host" is not a gap: it raised a permanent
false alert while the post was healthy, and it said nothing the day the post
was lost (review of #3007, 2026-10-23). The notifier therefore records a
heartbeat in the shared base (`notify_liveness`, migration 0474) at every
pass, per host and per human `NotifyConfig.channels_for` **actually** reaches:
an empty route (`routes: {"human:x": []}`) excludes that human even when
`notify.default` exists, exactly as the push path does. This module reads that
snapshot: a gap is a pending-decision owner that **no heartbeat fresh within
`ALIVE_S`** covers. A stale heartbeat names the lost host and its silence; a
human no heartbeat ever named is reported uncovered while another notifier
lives. With no heartbeat recorded at all, nothing is proven and no alert is
raised (a fresh install, the CI bench): the signal appears with the first
notifier pass and disappears with its last.

`exploitation.alerts` turns each gap into a `notify_undeliverable` alert (so
it is in `ameesh alerts`, the API and the embedded interface), and
`ameesh decisions` prints the same warning above its queue.

**The brief runs off the post.** A brief emitted by the notifier itself would
never fire in the outage it reports (the notifier is the missing piece). The
runner's periodic pass on every host calls `brief_once`: it briefs the
orchestrators and the auditor by `event` mail, claims each (recipient, human)
in `notify_briefs` so two hosts do not duplicate and the reminder comes after
`alert_briefs.REMIND_AFTER_S`, and releases the claim if the mail cannot be
deposited. The alert resolves by itself when a heartbeat covers the human
again, or when no request is pending.

#3383: the delivery itself is no longer one host's service: the `alerts` pass
is a role timer (`ameesh role timers`), run by the lease holder on its own
routes. This alert then names a candidate without a route, or a lost holder
not yet replaced (docs/EXPLOITATION.md, « Alertes poussées »).
"""
from __future__ import annotations

import os
import time

#: type d'alerte (déclaré dans `exploitation.ALERT_TYPES`, étiqueté dans
#: `notify.TYPE_LABELS`) : aucun notificateur VIVANT ne couvre l'humain.
GAP_TYPE = "notify_undeliverable"
#: A notifier silent this long no longer covers its humans. The service loops
#: every 30 s by default: ten minutes is twenty missed passes, not a restart.
ALIVE_S = 600.0
#: schéma des alertes (le même que `exploitation.ALERT_SCHEMA`, sans cycle).
ALERT_SCHEMA = "ameesh-alert/1"


def _span(seconds: float) -> str:
    """Durée courte et lisible (R12), sans dépendre de `notify` (cycle)."""
    seconds = int(max(0.0, float(seconds)))
    if seconds < 120:
        return "%ds" % seconds
    if seconds < 7200:
        return "%dmin" % (seconds // 60)
    if seconds < 172800:
        return "%dh%02d" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd" % (seconds // 86400)


def _fresh(rows, human, now: float, alive_s: float) -> dict | None:
    """Le battement frais le plus récent de `human` (None sinon).

    Une route vide n'écrit aucun battement (`beat` filtre par
    `channels_for`) : il n'y a donc pas de sentinelle d'exclusion à relire.
    """
    best = None
    for row in rows:
        if row.get("human") != human:
            continue
        seen = float(row.get("seen_ts") or 0.0)
        if seen < now - alive_s:
            continue
        if best is None or seen > float(best.get("seen_ts") or 0.0):
            best = row
    return best


def _last(rows, human) -> dict | None:
    """Le dernier battement de `human`, même périmé (pour le détail)."""
    best = None
    for row in rows:
        if row.get("human") != human:
            continue
        if best is None or float(row.get("seen_ts") or 0.0) > float(
                best.get("seen_ts") or 0.0):
            best = row
    return best


def gaps_for(pending, rows, now: float, *, alive_s: float = ALIVE_S) -> list[dict]:
    """Un écart par propriétaire de décision qu'aucun battement frais ne couvre.

    `pending` : les vues de `decisions.pending` (ou `queue()["pending"]`, mêmes
    champs : `id`, `owner`, `created_ts`, `overdue`). Une demande sans
    propriétaire (`owner` vide) est laissée aux autres passes : la résolution
    du destinataire par `notify.Router` demanderait aussi l'agent et le lot, et
    un faux constat serait pire que le silence. Ordre stable par humain.

    `rows` : tout l'instantané `notify_liveness` (`host`, `human`, `seen_ts`),
    périmés compris. Table vide : aucun constat (rien n'a jamais été prouvé).
    """
    if not rows:
        return []
    now = float(now)
    grouped: dict[str, list] = {}
    for view in pending or ():
        human = view.get("owner")
        if not human:
            continue
        grouped.setdefault(str(human), []).append(view)
    out = []
    for human in sorted(grouped):
        if _fresh(rows, human, now, alive_s) is not None:
            continue
        views = grouped[human]
        views.sort(key=lambda v: (float(v.get("created_ts") or 0.0),
                                  int(v.get("id") or 0)))
        ids = [int(v["id"]) for v in views]
        overdue = [int(v["id"]) for v in views if v.get("overdue")]
        oldest = float(views[0].get("created_ts") or 0.0)
        age = max(0.0, now - oldest) if oldest else 0.0
        if oldest and now - oldest < alive_s:
            # Une demande toute neuve peut précéder le prochain battement du
            # notificateur (30 s) : laisser à un notificateur vivant le temps
            # de la couvrir avant de dire qu'aucun ne l'a jamais couverte.
            # Vaut aussi quand une ligne périmée existe (N2, dev4) : elle ne
            # rouvre pas `notifier_stale` à la naissance d'une demande.
            continue
        last = _last(rows, human)
        if last is None:
            prefix = ("no notifier has ever reported covering %s while another "
                      "notifier is alive" % human)
            since = oldest or None
            reason = "uncovered"
            last_host = last_seen = last_human = None
        else:
            last_seen = float(last["seen_ts"])
            last_host = str(last.get("host") or "") or None
            last_human = str(last.get("human") or "") or None
            prefix = ("no living notifier covers %s: last heartbeat from host %s "
                      "was %s ago" % (human, last_host or "?",
                                      _span(now - last_seen)))
            since = last_seen
            reason = "notifier_stale"
        detail = (
            "%s: %d decision request(s) pending (%d overdue, oldest #%d, %s) — "
            "nothing is pushed to %s; the alerts role timer runs on the lease "
            "holder while its own liveness beat is fresh, otherwise on the first "
            "routed candidate (#3568, #4468): declare this human's notify route on "
            "every candidate and run `ameesh role timers --install` there "
            "(docs/EXPLOITATION.md, \"Alertes poussées\"); the requests stay "
            "readable with `ameesh decisions`"
            % (prefix, len(ids), len(overdue), ids[0], _span(age), human))
        out.append({"human": human, "last_host": last_host,
                    "last_seen": last_seen, "last_human": last_human,
                    "since": since, "count": len(ids), "overdue": overdue,
                    "decisions": ids, "oldest": ids[0], "age_s": age,
                    "reason": reason, "detail": detail})
    return out


def gaps(db, pending, now: float | None = None) -> list[dict]:
    """L'instantané partagé, puis les écarts. Jamais fatal à une passe."""
    now = time.time() if now is None else float(now)
    if db is None:
        return []
    try:
        from . import storage
        rows = storage.of(db).notify_liveness.latest()
    except Exception:  # noqa: BLE001 — un constat non lisible n'est pas une preuve
        return []
    return gaps_for(pending, rows, now)


def alert_of(gap: dict, orchestrators=None) -> dict:
    """L'alerte `ameesh-alert/1` d'un écart (exploitation et passe de brief)."""
    out = {"schema": ALERT_SCHEMA, "type": GAP_TYPE, "agent": None,
           "since": None if gap.get("since") is None else round(float(gap["since"]), 3),
           "value": gap["count"], "threshold": 0, "detail": gap["detail"],
           "human": gap["human"], "responsible": gap["human"],
           "last_host": gap.get("last_host"), "last_seen": gap.get("last_seen"),
           "last_human": gap.get("last_human"),
           "decisions": list(gap["decisions"]), "overdue": list(gap["overdue"]),
           "reason": gap["reason"]}
    if orchestrators is not None:
        out["orchestrators"] = list(orchestrators)
    return out


def beat(cfg, db, ncfg, now: float | None = None) -> int:
    """Le battement de CE notificateur : les humains que `channels_for` joint.

    Une route vide (`routes: {"human:x": []}`) exclut explicitement l'humain,
    même quand `notify.default` existe : c'est la règle du push, et la preuve
    partagée doit la refléter, sinon un notificateur sain sans canal pour cet
    humain masquerait indéfiniment la perte du poste (revue #3007, B1).
    Les humains connus viennent de la configuration de l'hôte, des clés de
    `notify.routes` et des demandes de décision en attente ; `channels_for`
    tranche pour chacun.
    """
    from . import decisions, notify, storage
    now = time.time() if now is None else float(now)
    humans = set()
    # `cfg.humans` est la chaîne CSV de la configuration ; `human_names` en
    # donne les noms (revue #3007, B2 : itérer la chaîne battait « a », « l »…).
    for raw in getattr(cfg, "human_names", ()) or ():
        human = notify.normalize_human(raw)
        if human:
            humans.add(human)
    humans.update(str(human) for human in (ncfg.routes or {}) if human)
    try:
        for view in decisions.pending(db, now):
            owner = view.get("owner")
            if owner:
                humans.add(str(owner))
    except Exception:  # noqa: BLE001 — un battement ne dépend pas des demandes
        pass
    covered = sorted(human for human in humans if ncfg.channels_for(human))
    if not covered:
        return 0
    host = str(getattr(cfg, "host", "") or "?")
    return storage.of(db).notify_liveness.beat(host, covered, now=now)


def brief_body(text: str, previous_ts, since, now: float) -> str:
    """Le texte du brief, relance seulement pour le MÊME épisode (#3913).

    `previous_ts` est l'envoi précédent de `(destinataire, humain)` ; `since`
    est le début de l'épisode (dernier battement pour `notifier_stale`, la
    plus ancienne demande pour `uncovered`). Une réclamation antérieure au
    début de l'épisode appartient à un trou déjà résorbé : le texte part sans
    « Reminder, alert still active for … », qui ferait croire à une panne
    continue et non à un nouvel incident. Dans le même épisode, l'âge annoncé
    est celui de l'épisode (`since`), pas celui du dernier brief : « active
    depuis 15 h 53 » ne décrit plus l'espacement des relances.
    """
    if previous_ts is None:
        return text
    if since is None:
        from . import alert_briefs
        return alert_briefs.reminder(text, previous_ts, now)
    if float(previous_ts) < float(since):
        return text
    from . import alert_briefs
    return alert_briefs.reminder(text, since, now)


def brief_once(cfg, db, now: float | None = None) -> dict:
    """Brief hors du poste : les orchestrateurs et l'auditeur, sans doublon.

    Appelée par le ménage du runner sur chaque hôte (VM comprises) : le brief
    ne dépend donc pas du notificateur qu'il signale. Une réclamation partagée
    par `(destinataire, humain)` espace les envois de
    `alert_briefs.REMIND_AFTER_S` ; un envoi échoué rend la réclamation pour
    la passe suivante. Rend `{"sent": [...], "skipped": n, "failed": [...]}`.
    """
    now = time.time() if now is None else float(now)
    report = {"sent": [], "skipped": 0, "failed": []}
    if db is None:
        return report
    try:
        from . import alert_briefs, canon as canon_mod, decisions, fil, mail, storage, \
            sous_utilisation, work
        pending = decisions.pending(db, now)
        gaps = gaps_for(pending, storage.of(db).notify_liveness.latest(), now)
        if not gaps:
            return report
        listing = storage.of(db).operations.listing()
        try:
            canons = canon_mod.load_configured(cfg) if getattr(cfg, "canon", None) else []
        except Exception:  # noqa: BLE001 — sans canon, l'auditeur par son nom suffit
            canons = []
        auditors = alert_briefs.auditors(listing, canons)
        # #3036 : un agent sans bail vivant ou sur un hôte aux claims fermés
        # ne lira pas le brief ; `briefs` l'écarte et replie sur le fil du
        # bureau au lieu de compter une remise perdue (N1 dev1/cx1).
        closed = alert_briefs.claims_closed_hosts(db)
        # #3007 N1 (dev4) : les destinataires du brief sont ceux de l'alerte,
        # orchestrateurs DÉCLARÉS (`AMEESH_ALERT_ORCHESTRATORS`, défaut de
        # `--orchestrators`) et rôles du canon, pas les seuls rôles du canon.
        declared = tuple(
            name.strip() for name in
            (os.environ.get("AMEESH_ALERT_ORCHESTRATORS") or "").split(",")
            if name.strip())
        orchestrators = sorted(sous_utilisation.alert_orchestrators(
            cfg, db, listing, declared=declared, canons=canons))
        since = now - alert_briefs.REMIND_AFTER_S
        for gap in gaps:
            alert = alert_of(gap, orchestrators)
            texts = alert_briefs.briefs(
                alert, listing, auditor_names=auditors,
                orchestrator_names=set(orchestrators),
                closed_hosts=closed)
            for recipient, text in sorted(texts.items()):
                claim = storage.of(db).notify_liveness.claim_brief(
                    recipient, gap["human"], since=since, now=now)
                if not claim["claimed"]:
                    report["skipped"] += 1
                    continue
                body = brief_body(text, claim.get("previous_ts"),
                                  gap.get("since"), now)
                payload = {"alert": GAP_TYPE, "human": gap["human"],
                           "decisions": list(gap["decisions"]),
                           "reason": gap["reason"]}
                try:
                    if recipient == "thread:bureau":
                        # #3036 : plus aucun agent vivant à joindre — la route
                        # humaine est le fil lisible, pas une boîte fictive
                        # (B1 dev1/cx1 : `thread:bureau` n'est pas un agent).
                        if fil.record(cfg, db, sender=work.SYSTEM_SENDER,
                                      recipients=["bureau"], text=body,
                                      project=getattr(cfg, "project", None),
                                      meta=payload) is None:
                            raise RuntimeError(
                                "readable thread did not take the brief")
                    else:
                        mail.send(db, work.SYSTEM_SENDER, recipient, body,
                                  kind="event", payload=payload)
                except Exception as exc:  # noqa: BLE001 — un destinataire ne bloque pas
                    storage.of(db).notify_liveness.release_brief(recipient, gap["human"])
                    report["failed"].append({"recipient": recipient,
                                             "human": gap["human"], "error": str(exc)})
                else:
                    report["sent"].append({"recipient": recipient,
                                           "human": gap["human"],
                                           "reminder": claim.get("previous_ts") is not None,
                                           "decisions": list(gap["decisions"])})
    except Exception as exc:  # noqa: BLE001 — la passe du runner ne tombe pas
        report["failed"].append({"recipient": None, "human": None, "error": str(exc)})
    return report
