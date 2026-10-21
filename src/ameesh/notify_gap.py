# SPDX-License-Identifier: AGPL-3.0-only
"""#3007: a pending decision the human cannot be pushed about from this host.

`ameesh notify` pushes an alert to the responsible human on the channels of
THIS host's configuration (`notify.routes`, `notify.default`). In practice one
host (`pc-smichea`) holds the only routes (desktop, ntfy, Slack) and runs the
service. When that host is lost the rest of the mesh keeps reading the shared
database — pending decisions, overdue decisions, `ameesh decisions` — but
nothing reaches the human, and no other host has a route. The only witness
was `ameesh notify --test human:<id>` (exit 2, no channel), which an agent
runs only if it thinks of it (incident #3006, 2026-10-21: decision #123440
overdue and unanswered, no channel from the healthy mesh).

The expected outcome of #3007 is either a fallback channel on a healthy host
or the absence of a route signalled where an agent sees it. This module does
the second, from the pending decisions that `ameesh alerts` and
`ameesh decisions` already read: it names, per human and per host, the fact
that nothing is pushed here, with the requests, the age, the overdue count
and the fallback gesture. `exploitation.alerts` turns each gap into a
`notify_undeliverable` alert (so it is in `ameesh alerts`, the API and the
embedded interface), and `ameesh decisions` prints the same warning above its
queue. The alert resolves by itself when a route for that human exists on the
host, or when no request is pending.

The observation is LOCAL on purpose: a host with a route does not raise it,
and the text says « on this host ». A mesh-wide claim would need a shared
notifier heartbeat, which the mesh does not have (the notify state lives in
`<state>/notify/state.json`, per host). The fallback itself — a second
notifier with an ntfy route on a healthy host — is an operator gesture,
documented in docs/EXPLOITATION.md, « Alertes poussées ».
"""
from __future__ import annotations

import time

#: type d'alerte (déclaré dans `exploitation.ALERT_TYPES`, étiqueté dans
#: `notify.TYPE_LABELS`) : la route manque ICI, pas nécessairement ailleurs.
GAP_TYPE = "notify_undeliverable"


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


def _channel_state(cfg) -> tuple:
    """(NotifyConfig | None, raison d'invalidité | None).

    Une configuration illisible ne peut pas pousser non plus : elle est
    signalée comme le manque de route, avec sa raison (jamais une valeur :
    `notify.NotifyConfigError` ne cite pas les secrets)."""
    try:
        from . import notify
        return notify.parse_config(getattr(cfg, "notify", None)), None
    except Exception as exc:  # noqa: BLE001 — une passe d'alerte ne tombe pas
        reason = " ".join(str(exc).split()) or type(exc).__name__
        return None, reason


def gaps(cfg, pending, now: float | None = None) -> list[dict]:
    """Un écart par humain que la configuration de CET hôte ne joint pas.

    `pending` : les vues de `decisions.pending` (ou `queue()["pending"]`,
    mêmes champs : `id`, `owner`, `created_ts`, `overdue`). Une demande sans
    propriétaire (`owner` vide : `human:*`) est laissée aux autres passes :
    la résolution du destinataire par `notify.Router` demanderait aussi
    l'agent et le lot, et un faux constat serait pire que le silence.
    Ordre stable par humain."""
    now = time.time() if now is None else float(now)
    ncfg, invalid = _channel_state(cfg)
    grouped: dict[str, list] = {}
    for view in pending or ():
        human = view.get("owner")
        if not human:
            continue
        grouped.setdefault(str(human), []).append(view)
    host = str(getattr(cfg, "host", "") or "?")
    out = []
    for human in sorted(grouped):
        views = grouped[human]
        if invalid is None and ncfg.channels_for(human):
            continue
        views.sort(key=lambda v: (float(v.get("created_ts") or 0.0),
                                  int(v.get("id") or 0)))
        ids = [int(v["id"]) for v in views]
        overdue = [int(v["id"]) for v in views if v.get("overdue")]
        since = float(views[0].get("created_ts") or 0.0)
        age = max(0.0, now - since) if since else 0.0
        if invalid is not None:
            prefix = "invalid notify configuration on host %s (%s)" % (host, invalid)
            reason = "invalid_config"
        else:
            prefix = ("no notification channel on host %s for %s "
                      "(notify.routes / notify.default)" % (host, human))
            reason = "no_channel"
        detail = (
            "%s: %d decision request(s) pending (%d overdue, oldest #%d, %s) — "
            "nothing is pushed to %s from this host; restore ameesh-notify on the "
            "host that holds the routes, or configure a fallback route on a healthy "
            "host and start it (docs/EXPLOITATION.md, \"Alertes poussées\"); the "
            "requests stay readable with `ameesh decisions`"
            % (prefix, len(ids), len(overdue), ids[0], _span(age), human))
        out.append({"human": human, "host": host, "since": since or None,
                    "count": len(ids), "overdue": overdue, "decisions": ids,
                    "oldest": ids[0], "age_s": age, "reason": reason,
                    "detail": detail})
    return out
