# SPDX-License-Identifier: AGPL-3.0-only
"""#3512: read-only completion evidence for delayed merge alerts."""
from __future__ import annotations

from . import db as db_mod, lot_class, plan_git, storage


def merge_observed(cfg, db, row: dict, marks=None, *, overview=None) -> bool:
    """A completed lot, a valid merge milestone, or its delivered frozen content.

    Packet composition/state can lag behind Git. Reuse the content probe
    (including squash/cherry-pick and revert checks) on the declared published
    target. No state correction, fetch of target refs, or inferred packet
    membership. Missing evidence leaves the alert active.
    """
    if row.get("state") in ("merged", "promoted", "closed"):
        return True
    if db_mod.is_dry_pass(db):
        return False
    try:
        if marks is None:
            marks = storage.of(db).work.milestones(int(row["id"]), None)
        # Only the work reader certifies annulment status. Batched progress
        # marks omit it; their merge/correction history is handled by SLO itself.
        if any(m.get("kind") == "merged" and m.get("annulled") is False for m in marks):
            return True
        freeze = plan_git.latest_freeze(marks)
        sha = plan_git.full_commit_id((freeze or {}).get("sha"))
        if not sha:
            return False
        # A later observed head can contain work absent from the old freeze.
        if row.get("branch_head") and not plan_git.same_commit(row["branch_head"], sha):
            return False
        repo, commit = lot_class.repo_for_freeze(db, cfg, row, sha, overview=overview)
        if not repo:
            return False
        target = lot_class.default_base(cfg, row, repo, declared_only=True)
        if not target:
            return False
        # A local target can contain an unpublished merge: only origin counts.
        target = "refs/remotes/origin/" + plan_git.short_branch(target)
        if not plan_git.local_commit(repo, target):
            return False
        return bool(plan_git.probe(
            repo, commit, target, lot=int(row["id"]),
            frozen_at=freeze.get("at_ts"),
            frozen_absent=plan_git.freeze_absent(freeze.get("note")))["merged"])
    except Exception:  # unreadable evidence does not silence an outstanding alert
        return False
