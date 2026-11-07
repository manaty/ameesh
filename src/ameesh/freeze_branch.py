# SPDX-License-Identifier: AGPL-3.0-only
"""#6161 : associer au gel la seule branche publiée portant sa tête."""
from __future__ import annotations

import subprocess

MISSING_MARK = "ameesh-freeze-missing-branch:"


def missing_warning(item_id: int | None = None) -> str:
    """Shared machine marker and a shell-safe, concrete lot identifier."""
    lot = str(item_id) if item_id is not None else "LOT_ID"
    return ("%s no branch associated: the composer will not see this lot; "
            "set it with ameesh work branch %s BRANCH_NAME" % (MISSING_MARK, lot))


def published_branch(repo: str, sha: str, *, item_id: int | None = None) -> tuple[str | None, str]:
    """Lecture réseau avant la transaction ; seule une tête exacte fait preuve.

    Un ancêtre commun ne permet pas de choisir la branche de travail. Les refs
    locales périmées, tags et alias distants HEAD ne sont jamais des candidats.
    """
    from .work import WorkError

    lot = str(item_id) if item_id is not None else "LOT_ID"
    remedy = "ameesh work branch %s BRANCH_NAME" % lot
    warning = missing_warning(item_id)
    if not repo:
        return None, warning + " (no readable repository)"
    candidates = set()
    try:
        remotes = subprocess.run(
            ["git", "-C", repo, "remote"], capture_output=True, text=True,
            timeout=20, check=False)
        if remotes.returncode:
            return None, warning + " (remotes unavailable)"
        names = remotes.stdout.splitlines()
        if not names:
            return None, warning + " (no configured remote)"
        for remote in names:
            result = subprocess.run(
                ["git", "-C", repo, "ls-remote", "--heads", "--", remote],
                capture_output=True, text=True, timeout=20, check=False)
            if result.returncode:
                return None, warning + " (remote heads unavailable)"
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) != 2:
                    return None, warning + " (invalid remote heads response)"
                head, ref = fields
                if head.lower() == sha.lower() and ref.startswith("refs/heads/"):
                    candidates.add(ref.removeprefix("refs/heads/"))
    except (OSError, subprocess.TimeoutExpired):
        return None, warning + " (remote heads unavailable)"
    candidates = sorted(candidates)
    if len(candidates) > 1:
        raise WorkError("freeze refused (branch-ambiguous): several published branches "
                        "point to %s: %s; associate one with %s before freezing"
                        % (sha[:12], ", ".join(candidates), remedy))
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
