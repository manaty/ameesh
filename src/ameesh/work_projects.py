# SPDX-License-Identifier: AGPL-3.0-only
"""L118c: projects of lots created by mail, and journaled corrections."""
from . import projects, registry, storage, work


def mail_project(cfg, db, sender, recipient, explicit=None):
    """Explicit project, otherwise the sender's project, then the recipient's."""
    if explicit is not None:
        value = explicit.strip()
        if not value:
            raise work.WorkError("--app: empty project")
        return value
    canons = None
    for name in (sender, recipient):
        row = registry.get(db, name)
        if row is None:
            continue
        value = projects.agent_project(row)
        if value:
            return value
        if canons is None:
            canons = work.configured_canons(cfg)
        for canon in canons:
            if not canon.owns(row.get("canon")):
                continue
            agent = canon.agent(name)
            if agent and agent.team and agent.team.strip():
                return agent.team.strip()
    raise work.WorkError("--new-lot: no project known for sender %s or recipient %s; "
                         "pass --app <project>" % (sender, recipient))


def set_app(cfg, db, item_id, project, *, actor=""):
    """Correct a lot's project as a human, orchestrator or design agent."""
    actor = work.corrector(cfg, db, actor)
    project = project.strip()
    if not project:
        raise work.WorkError("set-app: empty project")
    item = storage.of(db).work.set_app(item_id, project, actor=actor)
    if item is None:
        raise work.WorkError("lot %s not found" % item_id)
    return item


def alerts(db):
    """One alert per open lot whose stored project is empty."""
    from .exploitation import _alert
    return [_alert("lot_without_project", row.get("assignee"), row["created_ts"], 1, 1,
                   "lot #%d has no project; correct it with ameesh work set-app %d <project>"
                   % (row["id"], row["id"]), lot=row["id"], title=row["title"],
                   lot_state=row["state"])
            for row in storage.of(db).work.without_project()]
