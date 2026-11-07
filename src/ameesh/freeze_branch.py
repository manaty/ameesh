# SPDX-License-Identifier: AGPL-3.0-only
"""#6161 : associer au gel la seule branche publiée portant sa tête."""
from __future__ import annotations

import subprocess

MISSING_MARK = "ameesh-freeze-missing-branch:"


def published_branch(repo: str, sha: str) -> tuple[str | None, str]:
    """Lecture réseau avant la transaction ; seule une tête exacte fait preuve.

    Un ancêtre commun ne permet pas de choisir la branche de travail. Les refs
    locales périmées, tags et alias origin/HEAD ne sont jamais des candidats.
    """
    from .work import WorkError

    warning = ("%s no branch associated: the composer will not see this lot; "
               "set it with ameesh work branch <id> <branch>" % MISSING_MARK)
    if not repo:
        return None, warning + " (no readable repository)"
    try:
        result = subprocess.run(
            ["git", "-C", repo, "ls-remote", "--heads", "origin"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, warning + " (remote heads unavailable)"
    if result.returncode:
        return None, warning + " (remote heads unavailable)"
    candidates = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            return None, warning + " (invalid remote heads response)"
        head, ref = fields
        if head.lower() == sha.lower() and ref.startswith("refs/heads/"):
            candidates.add(ref.removeprefix("refs/heads/"))
    candidates = sorted(candidates)
    if len(candidates) > 1:
        raise WorkError("freeze refused (branch-ambiguous): several published branches "
                        "point to %s: %s; associate one with ameesh work branch "
                        "<id> <branch> before freezing" % (sha[:12], ", ".join(candidates)))
    return (candidates[0], "") if candidates else (None, warning + " (no published tip matches)")


def attach_hook(item_id: int, branch: str, sha: str, actor: str, following):
    """Association et gel sous la même transaction, sans écraser un choix."""
    def hook(tx, milestone, after_commit):
        from . import work
        attached = work.attach_implicit_branch(
            tx, item_id, branch, source="inferred at freeze %s (#6161)" % sha[:12], actor=actor)
        if attached is None:
            current = work.get(tx, item_id)
            if not current or current.get("branch") != branch:
                raise work.WorkError(
                    "freeze refused (branch-not-associated): branch changed or was detached; "
                    "associate the branch explicitly before freezing")
        if following is not None:
            following(tx, milestone, after_commit)
    return hook
