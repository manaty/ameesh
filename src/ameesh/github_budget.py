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


def is_poll(argv):
    if not argv:
        return False
    if argv[0] == "api":
        # gh defaults to POST with form fields or input. Do not misclassify
        # writes (or GraphQL, a separate bucket) as REST polling.
        if any(a.split("=", 1)[0] in ("-f", "-F", "--field", "--raw-field", "--input")
               or a.startswith(("-f", "-F")) for a in argv[1:]):
            return False
        for i, arg in enumerate(argv[1:], 1):
            if arg in ("-X", "--method") and (i + 1 >= len(argv) or argv[i + 1].upper() != "GET"):
                return False
            if arg.startswith(("--method=", "-X")) and arg not in ("-X", "--method"):
                if arg.removeprefix("--method=").removeprefix("-X").upper() != "GET":
                    return False
        return not any(a.rstrip("/").split("?")[0] in ("graphql", "/graphql",
                       "https://api.github.com/graphql") for a in argv[1:])
    return len(argv) > 1 and (argv[0], argv[1]) in {
        ("run", "list"), ("run", "view"), ("pr", "list"), ("pr", "view"),
        ("pr", "checks"), ("issue", "list"), ("issue", "view"),
        ("repo", "view"), ("repo", "list"), ("workflow", "list"),
        ("workflow", "view"), ("release", "list"), ("release", "view")}


def reserve_reason(argv, *, env=None, now=None):
    env = os.environ if env is None else env
    state_dir = env.get("AMEESH_GITHUB_STATE")
    if not state_dir or not is_poll(argv):
        return None
    from . import plan_github
    reads = plan_github.SharedReads(state_dir, priority=env.get("AMEESH_GITHUB_PRIORITY", "poll"))
    until = reads.blocked(time.time() if now is None else now)
    if until:
        return "GitHub REST rate limit reserve: polling deferred until %s (non-conclusive)" % (
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
