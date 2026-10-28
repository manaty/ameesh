# SPDX-License-Identifier: AGPL-3.0-only
"""#3533: published production ancestry is outside a packet's authorship.

The remote branch is resolved afresh, then its exact object is fetched. No
local branch or tracking ref can attest publication. Merge commits bringing
production back are not reachable from that branch and remain attributable.
"""
from __future__ import annotations

from . import plan_git, radar, review_authors


def production(cfg, item, repo):
    """Return an exact published scope, or None when none is configured.

    A configured but unreadable branch raises; callers retain ordinary
    attribution without production exclusions. Stale local evidence never
    substitutes for a failed remote read.
    """
    if cfg is None or not item or not repo:
        return None
    project = radar.settings_of(cfg).projects.get(str(item.get("app") or ""))
    branch = getattr(project, "production_branch", "")
    if not branch:
        return None
    ref = "refs/heads/" + branch
    listing = plan_git._git(repo, "ls-remote", "--exit-code", "--refs",
                            "origin", ref, timeout=plan_git.FETCH_TIMEOUT)
    advertised = (listing.stdout.decode("utf-8", "replace")
                  if listing.returncode == 0 else "")
    rows = [line.split() for line in (advertised or "").splitlines()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != ref:
        raise ValueError("production provenance: remote branch unreadable: " + ref)
    sha = plan_git.full_commit_id(rows[0][0])
    if not sha:
        raise ValueError("production provenance: invalid remote commit")
    proc = plan_git._git(repo, "fetch", "--quiet", "--no-tags",
                         "--no-write-fetch-head", "--", "origin", sha,
                         timeout=plan_git.FETCH_TIMEOUT)
    if proc.returncode or not plan_git.local_commit(repo, sha):
        raise ValueError("production provenance: exact remote commit unavailable: " + sha)
    return {"head": sha, "ref": ref, "reason": "published-production"}


def exclude(repo, commits, scopes, trace=None, **context):
    """Exclude only proven ancestry; record each removed input commit.

    `context` qualifies the trace rows (#3544: `carried_from`, the prior
    freeze whose range was carried). A commit already traced is not traced
    twice: the current range is read first and keeps its own row.
    """
    remaining = list(commits or ())
    for scope in scopes:
        scope = scope if isinstance(scope, dict) else {
            "head": scope, "reason": "admitted-member"}
        after = review_authors._not_in_target(repo, remaining, scope["head"])
        kept = set(after)
        if trace is not None:
            seen = {row["commit"] for row in trace}
            trace.extend(dict(scope, commit=sha, **context) for sha in remaining
                         if sha not in kept and sha not in seen)
        remaining = after
    return remaining


def manifest(exclusions):
    """Fragment for the composer's immutable packet manifest, from gate facts."""
    if not exclusions:
        return ""
    lines = ["## Provenance exclusions", "", "| Commit | Reason | Published ref / admitted head |",
             "| --- | --- | --- |"]
    for row in exclusions:
        reason = row["reason"]
        if row.get("carried_from"):
            reason += " (carried from freeze %s)" % row["carried_from"]
        lines.append("| %s | %s | %s @ %s |" % (
            row["commit"], reason, row.get("ref", "member"), row["head"]))
    return "\n".join(lines) + "\n"
