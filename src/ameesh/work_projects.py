# SPDX-License-Identifier: AGPL-3.0-only
"""L118c: projects of lots created by mail, and journaled corrections."""
from . import projects, registry, storage, work


def agent_project(cfg, db, name):
    """Registry first, then an unambiguous project in the agent's own canon."""
    row = registry.get(db, name)
    if row is None:
        return ""
    value = projects.agent_project(row)
    if value:
        return value
    values = set()
    for canon in work.configured_canons(cfg):
        if canon.owns(row.get("canon")):
            for agent in canon.agents:
                if agent.title == name and agent.team and agent.team.strip():
                    values.add(agent.team.strip())
    if len(values) > 1:
        raise work.WorkError("ambiguous project for %s" % name)
    return next(iter(values), "")


def creation_project(cfg, db, creator, explicit=None, *, parent=None):
    """Omitted project inherits; an explicit blank cannot discard a known project."""
    if explicit is not None and explicit.strip():
        return explicit.strip()
    inherited = agent_project(cfg, db, creator)
    if not inherited and parent:
        inherited = (parent.get("app") or "").strip()
    if explicit is not None and inherited:
        raise work.WorkError("--app: empty project; creator belongs to %s" % inherited)
    return inherited if explicit is None else ""


def mail_project(cfg, db, sender, recipient, explicit=None):
    """Explicit project, otherwise the sender's project, then the recipient's."""
    if explicit is not None:
        value = explicit.strip()
        if not value:
            raise work.WorkError("--app: empty project")
        return value
    for name in (sender, recipient):
        value = agent_project(cfg, db, name)
        if value:
            return value
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


def backfill_app(cfg, db, *, actor="", dry_run=False):
    """Journal only uniquely inferred projects; never replace a concurrent correction."""
    actor = work.corrector(cfg, db, actor)
    results = []
    for row in storage.of(db).work.without_project():
        project = ""
        try:
            if row.get("assignee"):
                project = agent_project(cfg, db, row["assignee"])
            if not project:
                creator = storage.of(db).work.creator(row["id"])
                if creator:
                    project = agent_project(cfg, db, creator)
        except work.WorkError as exc:
            results.append({"id": row["id"], "status": "skipped", "reason": str(exc)})
            continue
        if not project:
            results.append({"id": row["id"], "status": "skipped", "reason": "no known project"})
            continue
        status = "planned"
        if not dry_run:
            item = storage.of(db).work.set_app(row["id"], project, actor=actor,
                                               if_empty=True, assignee=row.get("assignee"))
            status = "updated" if item else "skipped"
        results.append({"id": row["id"], "project": project, "status": status})
    return results
