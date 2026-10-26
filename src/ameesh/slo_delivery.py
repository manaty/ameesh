# SPDX-License-Identifier: AGPL-3.0-only
"""#3512: read-only completion evidence for delayed merge alerts."""
from __future__ import annotations

from . import db as db_mod, lot_class, plan_git, storage

#: #3632: a carrier in one of these states has delivered its composed units.
_DELIVERED_CARRIERS = ("merged", "promoted")


def _packet_delivery(db, row: dict, marks) -> bool:
    """#3632: a merged vehicle is delivery evidence for its frozen unit.

    The composition records the exact head it merged (`source_sha`). When the
    carrier is merged and the unit's current effective freeze is that composed
    head, the frozen content was merged with the carrier: unlike the Git probe,
    this proof needs neither a readable clone, a declared target, nor a fresh
    `origin/<target>` (#2309 has no `branch_target`).

    The proof is stale if the unit's effective freeze moved after the
    composition, or if an observed branch head carries work absent from it.
    A carrier closed for another reason is not delivery evidence. Only the
    database is read; an unreadable packet table is not a delivery proof.
    """
    try:
        candidate = plan_git.freeze_candidate(marks)
    except Exception:  # unreadable marks are not delivery evidence
        return False
    current = plan_git.commit_id((candidate or {}).get("sha"))
    if not current:
        return False
    # A later observed head can contain work absent from the last freeze.
    head = plan_git.commit_id(row.get("branch_head"))
    if head and not plan_git.same_commit(head, current):
        return False
    try:
        members = storage.of(db).work.packet_members(item_ids=[int(row["id"])])
    except Exception:  # table absent or unreadable: no proof, the alert stays
        return False
    for member in members:
        if str(member.get("package_state") or "") not in _DELIVERED_CARRIERS:
            continue
        # The composed head is the exact content the vehicle merged. The
        # recorded freeze can move after the composition (`record` re-reads
        # it); without Git, only the composed head itself proves delivery.
        # An empty `source_sha` (legacy row) falls back to the recorded freeze.
        source = plan_git.commit_id(member.get("source_sha"))
        carried = source or plan_git.commit_id(member.get("frozen_sha"))
        if carried and plan_git.same_commit(current, carried):
            return True
    return False


def merge_observed(cfg, db, row: dict, marks=None, *, overview=None) -> bool:
    """A completed lot, a valid merge milestone, or its delivered frozen content.

    Packet composition/state can lag behind Git. A merged vehicle delivers its
    frozen unit by itself (#3632); otherwise reuse the content probe (including
    squash/cherry-pick and revert checks) on the declared published target. No
    state correction, fetch of target refs, or inferred packet membership.
    Missing evidence leaves the alert active.
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
        # #3632: a merged vehicle already delivered its frozen unit; this is a
        # database proof and does not depend on a clone or a declared target.
        if _packet_delivery(db, row, marks):
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
