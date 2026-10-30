# SPDX-License-Identifier: AGPL-3.0-only
"""#3250: refresh the exact review target, then pin it to a commit.

An explicit refspec updates even a single-branch clone. Fetch failures never
fall back to its old tracking ref. Repositories without remotes use their
local target. Call outside the freeze transaction when a refresh is needed.
"""
from __future__ import annotations

from ... import canon, plan_git


class TargetError(RuntimeError):
    """The target cannot be certified for this reading."""


def resolve(repo: str, target: str, *, fetch: bool = True) -> str:
    text = str(target or "").strip()
    if not text:
        raise TargetError("no target branch known for the frozen range")
    # A full commit (or an explicitly qualified non-branch ref) is an exact
    # local target. Published branches use the fully qualified tracking ref;
    # a repository without remotes has only its local reference to certify.
    exact = plan_git.full_commit_id(text) or (
        text.startswith("refs/")
        and not text.startswith(("refs/heads/", "refs/remotes/")))
    # git_remotes() treats an unreadable configuration as an empty list. Here
    # only a successful empty read proves that no remote needs refreshing.
    remotes = "" if exact else canon._git_out(repo, "remote")
    if remotes is None:
        raise TargetError("cannot read remotes for target %s; stale target refused" % text)
    remote = (None if exact or not remotes.split()
              else canon.remote_branch(repo, text))
    ref = text
    if remote:
        ref = "refs/remotes/" + remote
        if not fetch:
            raise TargetError("target %s requires refresh outside the freeze transaction"
                              % text)
        name, branch = remote.split("/", 1)
        proc = canon._git(
            repo, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--",
            name, "+refs/heads/%s:%s" % (branch, ref),
            timeout=canon.FETCH_TIMEOUT)
        if proc.returncode:
            raise TargetError("cannot refresh target %s: git fetch failed; stale target "
                              "refused" % text)
    elif not exact and not plan_git._commit(repo, ref):
        raise TargetError("target %s is not an exact commit or a valid branch" % text)
    commit = plan_git._commit(repo, ref)
    if not commit:
        raise TargetError("no readable target commit for %s" % text)
    return commit
