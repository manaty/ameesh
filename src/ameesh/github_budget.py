# SPDX-License-Identifier: AGPL-3.0-only
"""#6649: local, read-only admission of routine GitHub polls.

Uses response headers already observed by the shared client; never probes
/rate_limit. Explicit merge/gate calls may use the reserve, never an exhausted
bucket. Direct absolute-path gh/curl calls remain outside this guard.
"""
from __future__ import annotations

import os
import time

RESERVE_FRACTION = .2
POLL_INTERVAL = 300.0


#: buckets that keep a reserve for explicit gate/merge reads
RESERVE_RESOURCES = ("core", "graphql")

#: routine gh reads and the API bucket each one spends: the pr/issue/repo
#: commands and `release list` query GraphQL, the others REST core.
POLL_RESOURCES = {
    ("run", "list"): "core", ("run", "view"): "core",
    ("workflow", "list"): "core", ("workflow", "view"): "core",
    ("release", "view"): "core", ("release", "list"): "graphql",
    ("pr", "list"): "graphql", ("pr", "view"): "graphql", ("pr", "checks"): "graphql",
    ("issue", "list"): "graphql", ("issue", "view"): "graphql",
    ("repo", "view"): "graphql", ("repo", "list"): "graphql"}


def poll_resource(argv):
    """The bucket a routine gh read spends (`core`, `graphql`), else None."""
    if not argv:
        return None
    if argv[0] == "api":
        # gh defaults to POST with form fields or input. Do not misclassify
        # writes as polling; a GraphQL query (even a read) spends `graphql`.
        if any(a.split("=", 1)[0] in ("-f", "-F", "--field", "--raw-field", "--input")
               or a.startswith(("-f", "-F")) for a in argv[1:]):
            return None
        for i, arg in enumerate(argv[1:], 1):
            if arg in ("-X", "--method") and (i + 1 >= len(argv) or argv[i + 1].upper() != "GET"):
                return None
            if arg.startswith(("--method=", "-X")) and arg not in ("-X", "--method"):
                if arg.removeprefix("--method=").removeprefix("-X").upper() != "GET":
                    return None
        if any(a.rstrip("/").split("?")[0] in ("graphql", "/graphql",
               "https://api.github.com/graphql") for a in argv[1:]):
            return "graphql"
        return "core"
    return POLL_RESOURCES.get(tuple(argv[:2])) if len(argv) > 1 else None


def is_poll(argv):
    return poll_resource(argv) is not None


def reserve_reason(argv, *, env=None, now=None):
    env = os.environ if env is None else env
    state_dir = env.get("AMEESH_GITHUB_STATE")
    resource = poll_resource(argv) if state_dir else None
    if not resource:
        return None
    from . import plan_github
    reads = plan_github.SharedReads(state_dir, priority=env.get("AMEESH_GITHUB_PRIORITY", "poll"))
    now = time.time() if now is None else now
    until = reads.blocked(now, resource)
    if until:
        return "GitHub %s rate limit %s: polling deferred until %s (non-conclusive)" % (
            resource, "reserve" if reads.reserve_only(now, resource) else "exhausted",
            time.strftime("%H:%M:%SZ", time.gmtime(until)))
    return None


def low_quotas(state_dir, *, now=None):
    from . import plan_github
    now = time.time() if now is None else now
    out = []
    if not state_dir:
        return out
    for resource, quota in plan_github.SharedReads(state_dir).quotas().items():
        limit, remaining, reset = (quota.get(k) for k in ("limit", "remaining", "reset"))
        if (isinstance(limit, int) and limit > 0 and isinstance(remaining, int)
                and remaining < limit * RESERVE_FRACTION
                and isinstance(reset, (int, float)) and reset > now):
            out.append(dict(quota, resource=resource))
    return out
