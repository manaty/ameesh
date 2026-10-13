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


def creation_project(cfg, db, creator, explicit=None, *, parent=None, new_project=False):
    """Omitted project inherits; an explicit blank cannot discard a known project.
    #506: an explicit project must be known, unless `new_project` introduces it."""
    if explicit is not None and explicit.strip():
        if not new_project:
            require_known(cfg, db, explicit.strip())
        return explicit.strip()
    inherited = agent_project(cfg, db, creator)
    if not inherited and parent:
        inherited = (parent.get("app") or "").strip()
    if explicit is not None and inherited:
        raise work.WorkError("--app: empty project; creator belongs to %s" % inherited)
    return inherited if explicit is None else ""


def known_projects(cfg, db):
    """#495: the projects an explicit project may name, as L243 knows them
    (`github.projects`, lots' projects), plus the configured canons' work
    package teams and agent projects."""
    from . import canon_sync
    names = set(canon_sync.known_projects(db, cfg))
    for canon in work.configured_canons(cfg):
        names.update(p.team for p in canon.packages if p.team)
        for agent in canon.agents:
            names.update(agent.effective_projects)
    return {name.strip() for name in names if name and name.strip()}


def require_known(cfg, db, project):
    """#506: refuse a project that `known_projects` does not list, with the
    close names and the two ways forward (declare it, or --new-project)."""
    known = known_projects(cfg, db)
    if project in known:
        return
    from . import undeliverable
    near = undeliverable.suggestions(project, [{"name": name} for name in sorted(known)])
    raise work.WorkError(
        "unknown project %r (--app): neither a configured canon's nor `github.projects` "
        "nor a lot's project; %s; declare it first (`github.projects` in the host "
        "configuration, or a canon), or pass --new-project to introduce it on purpose"
        % (project, "close names: " + ", ".join("%s (%s)" % pair for pair in near)
           if near else "no close name"))


def introduction(cfg, db, project, actor):
    """#506: the history line of a lot that introduces its project with
    --new-project ('' when the project is already known). Read before the
    lot is written: once written, the lot makes its project known."""
    if project in known_projects(cfg, db):
        return ""
    return "project %r introduced by %s (--new-project)" % (project, actor or "unknown")


def mail_project(cfg, db, sender, recipient, explicit=None, *, new_project=False):
    """Explicit project, otherwise the sender's project, then the recipient's.
    #506: an explicit project must be known, unless `new_project` introduces it."""
    if explicit is not None:
        value = explicit.strip()
        if not value:
            raise work.WorkError("--app: empty project")
        if not new_project:
            require_known(cfg, db, value)
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
