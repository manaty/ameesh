# SPDX-License-Identifier: AGPL-3.0-only
"""#7535: alert the pilot when a delivered base leaves stacked lots behind."""
from __future__ import annotations

from . import alert_briefs, mail, plan_git, registry, storage, work


def after_merge(db, base, *, repo=None):
    """An alert failure must never undo the base's recorded delivery."""
    try:
        threads = []
        with db.transaction() as tx:
            notify(tx, base, repo=repo, after_commit=threads)
        for record in threads:
            record(db.cfg, db)
        return ""
    except Exception as exc:  # noqa: BLE001 - delivery has already committed
        warning = "stacked delivery alert failed for #%s: %s: %s" % (
            base["id"], type(exc).__name__, str(exc)[:240])
        try:
            work.note(db, int(base["id"]), warning, actor=work.SYSTEM_SENDER)
        except Exception:  # noqa: BLE001 - the caller still receives the warning
            pass
        return warning


def notify(db, base, *, repo=None, after_commit=None):
    """The journal and mail commit together, after the base merge transaction.

    Retargeting also requires a GitHub write and migration-slot arbitration.
    Use the requested fallback: tell the project pilot about all three,
    without partially changing a validated lot. A repeated merge observation
    does not call this hook, so it cannot send the same alert twice.
    """
    branch = base.get("branch")
    app = base.get("app")
    if not branch or not app:
        return
    cfg = getattr(db, "cfg", None)
    target = work._project_target(cfg, app)
    if not target and repo:
        target = plan_git.default_target(repo)
    if not target or (plan_git.short_branch(base.get("branch_target") or target)
                      != plan_git.short_branch(target)):
        return
    branch = plan_git.short_branch(branch)
    rows = storage.of(db).work.stacked_on(app, [
        branch, "origin/" + branch, "refs/heads/" + branch,
        "refs/remotes/origin/" + branch])
    if not rows:
        return
    pilots = alert_briefs.project_pilots(app, registry.overview(db), cfg=cfg, db=db)
    for row in rows:
        reservations = row.get("migrations") or []
        migration = row.get("migration")
        details = str(reservations) if reservations else (
            str(migration) if migration is not None else "none recorded")
        text = (
            "Base lot #%s delivered to %s; stacked lot #%s (%s) still targets %s. "
            "Pilot action: move the lot target and its PR base (%s) to %s, "
            "and move/check all migration reservations on that target "
            "(including named slots; resolve collisions before packaging). "
            "Reservations: %s."
            % (base["id"], target, row["id"], row.get("issue_ref") or "no issue",
               row["branch_target"], row.get("pr_ref") or "no PR recorded", target,
               details))
        destinations = ", ".join(pilots) or "no project pilot found"
        storage.of(db).work.note(
            int(row["id"]), row["state"], text + " Alert: " + destinations,
            work.SYSTEM_SENDER)
        for pilot in pilots:
            mail.send(db, work.SYSTEM_SENDER, pilot, text, kind="event",
                      work_item_id=str(row["id"]), after_commit=after_commit,
                      payload={"stacked_delivery": {"base": int(base["id"]),
                                                   "lot": int(row["id"]),
                                                   "target": target}})
