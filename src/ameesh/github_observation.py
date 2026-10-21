# SPDX-License-Identifier: AGPL-3.0-only
"""#2951: report silence of the shared GitHub observer from every notifier."""
from __future__ import annotations

from . import plan_github, storage


def alerts(cfg, db, now: float, *, fresh_s: float) -> list[dict]:
    """A missing or stale complete queue reading is an observation failure.

    No GitHub call, designated-host check or local startup timer: restarting
    a notifier must not hide a missing reading. Never-observed repositories
    are reported on the first pass, without inventing a last-reading time.
    """
    from .exploitation import _alert, _stamp

    try:
        settings = plan_github.settings_of(cfg)
    except plan_github.SettingsError:
        return []  # The GitHub projector reports invalid configuration.
    repos = {repo.casefold(): repo for repo in settings.projects.values()}
    if not repos:
        return []
    readings = {row["repo"].casefold(): row
                for row in storage.of(db).ci.queued_oldest()}
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
            "%d min. Check github.host (%s), ameesh-notify on that host, and "
            "the gh token/authentication" % (
                repo, last, fresh_s // 60, settings.host or "not designated"),
            repo=repo, reason=key, last_seen_ts=seen))
    return out
