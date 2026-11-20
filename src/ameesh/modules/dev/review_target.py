# SPDX-License-Identifier: AGPL-3.0-only
"""#3250: refresh the exact review target, then pin it to a commit.

An explicit refspec updates even a single-branch clone. Fetch failures never
fall back to its old tracking ref. Repositories without remotes use their
local target. Call outside the freeze transaction when a refresh is needed.
"""
from __future__ import annotations

from ... import canon, plan_git


class TargetError(RuntimeError):
    """The target cannot be certified for this reading.

    #7941 : `terminal` dit qu'un `ls-remote` borne a prouvé que la ref n'existe
    plus à la forge. Un échec réseau, une forge muette ou une ref illisible
    restent transitoires (`False`) et ne doivent pas alarmer le pilote.
    """

    def __init__(self, message: str, *, terminal: bool = False):
        super().__init__(message)
        self.terminal = bool(terminal)


def _ref_missing(repo: str, remote: str, branch: str) -> bool:
    """True when `ls-remote` proves `remote` no longer publishes `branch`.

    `git ls-remote --exit-code` vaut 2 quand aucun ref ne correspond : la
    branche a disparu de la forge (fusionnée puis supprimée, renommée). Un
    échec réseau rend un autre code et ne prouve rien : on ne devine jamais
    une disparition (#7941)."""
    try:
        proc = canon._git(
            repo, "ls-remote", "--exit-code", "--heads", "--", remote,
            "refs/heads/%s" % branch, timeout=canon.FETCH_TIMEOUT)
    except canon.GitError:
        return False
    return proc.returncode == 2


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
            if _ref_missing(repo, name, branch):
                # #7941 : la branche n'est plus publiée (elle a été supprimée
                # après la fusion) : c'est un échec terminal, pas une panne
                # réseau, et il ne se réessaie pas indéfiniment.
                raise TargetError(
                    "cannot refresh target %s: branch disappeared at origin;"
                    " stale target refused" % text, terminal=True)
            raise TargetError("cannot refresh target %s: git fetch failed; stale target "
                              "refused" % text)
    elif not exact and not plan_git._commit(repo, ref):
        raise TargetError("target %s is not an exact commit or a valid branch" % text)
    commit = plan_git._commit(repo, ref)
    if not commit:
        raise TargetError("no readable target commit for %s" % text)
    return commit
