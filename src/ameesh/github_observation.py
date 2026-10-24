# SPDX-License-Identifier: AGPL-3.0-only
"""#2951: report silence of the shared GitHub observer from every notifier."""
from __future__ import annotations

from . import plan_github, storage


def _observer_hint(settings) -> str:
    """Où regarder, sans faire passer la configuration locale pour l'observateur.

    #3327 : l'observation est portée par la minuterie d'un rôle élu, installée
    sur chaque candidat. `github.host` reste le repère historique : sur une VM
    qui ne porte pas la clé `github`, il est vide, et l'absence de cette clé ne
    décrit pas l'hôte qui relève (#2957).
    """
    timer = "the elected role's timer (`ameesh role timers --check`)"
    if settings.host:
        return "%s and the designated observer github.host (%s)" % (timer, settings.host)
    return ("%s; github.host on the designated observer is not set in this host's "
            "local configuration (the reading is shared)" % timer)


def alerts(cfg, db, now: float, *, fresh_s: float) -> list[dict]:
    """A missing or stale complete queue reading is an observation failure.

    No GitHub call, designated-host check or local startup timer: restarting
    a notifier must not hide a missing reading. Never-observed repositories
    are reported on the first pass, without inventing a last-reading time.
    Without local projects, monitor repositories already in the shared state
    so a notifier on another host can detect the observer's silence.
    """
    from .exploitation import _alert, _stamp

    try:
        settings = plan_github.settings_of(cfg)
    except plan_github.SettingsError:
        return []  # The GitHub projector reports invalid configuration.
    repos = {repo.casefold(): repo for repo in settings.projects.values()}
    readings = {row["repo"].casefold(): row
                for row in storage.of(db).ci.queued_oldest()}
    if not repos:
        repos = {key: row["repo"] for key, row in readings.items()}
    out = []
    for key, repo in sorted(repos.items()):
        seen = readings.get(key, {}).get("seen_ts")
        age = max(0.0, now - float(seen)) if seen is not None else None
        if age is not None and age <= fresh_s:
            continue
        last = (_stamp(seen) + " (%d min ago)" % (age // 60)
                if seen is not None else "never recorded")
        out.append(_alert(
            "github_observation_stale", None, seen,
            round(age) if age is not None else None, round(fresh_s),
            "GitHub observation of %s: last complete reading %s; expected within "
            "%d min. Check %s, ameesh-notify on the observer host, and the gh "
            "token/authentication" % (
                repo, last, fresh_s // 60, _observer_hint(settings)),
            repo=repo, reason=key, last_seen_ts=seen))
    return out
