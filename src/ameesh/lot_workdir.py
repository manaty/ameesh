# SPDX-License-Identifier: AGPL-3.0-only
"""L243-4: choose a lot's directory without changing the agent's primary cwd."""
from __future__ import annotations

import dataclasses
import os

from . import canon, projects, storage, work


class WorkdirError(ValueError):
    """The destination cannot safely run a project session."""


def project(db, lot) -> str:
    if not lot or not str(lot).isdigit():
        return ""
    item = work.get(db, int(lot))
    if item is None:
        raise WorkdirError("unknown lot #%s" % lot)
    # app is the lot's repository project; team may describe its staffing.
    if item.get("app") or item.get("team"):
        return (item.get("app") or item.get("team")).strip()
    for key in ("package_id", "package_parent"):
        if item.get(key):
            package = storage.of(db).packages.get(item[key])
            if package and package.get("team"):
                return package["team"].strip()
    return ""


def destination(cfg, agent, lot_project, *, canons=None) -> str | None:
    names = projects.agent_projects(agent)
    primary = names[0] if names else ""
    if not lot_project or lot_project == primary:
        return agent.get("cwd")
    if lot_project not in names:
        raise WorkdirError("project %s is not declared for %s" %
                           (lot_project, agent["name"]))
    canons = canon.load_configured(cfg) if canons is None else canons
    paths = set()
    for card in canons:
        if not card.owns(agent.get("canon")):
            continue
        host = card.host(agent.get("host") or cfg.host)
        if host is None:
            continue
        # A work_dirs entry describes the primary cwd, not every repository.
        policy = dataclasses.replace(host.policy, work_dirs=None)
        path = policy.work_dir(lot_project, agent["name"])
        if path:
            paths.add(os.path.abspath(os.path.expanduser(path)))
    if len(paths) != 1:
        raise WorkdirError("project %s needs one unambiguous Host work root" % lot_project)
    path = paths.pop()
    if agent.get("cwd") and os.path.realpath(path) == os.path.realpath(agent["cwd"]):
        raise WorkdirError("project %s resolves to the primary working directory" % lot_project)
    return path


def problem(cfg, agent, lot_project, *, canons=None) -> str | None:
    """Reason to leave a secondary lot in the queue; None when ready."""
    names = projects.agent_projects(agent)
    if not lot_project or (names and lot_project == names[0]):
        return None
    if getattr(cfg, "host_mediated", False) and agent.get("host") == cfg.host:
        return "mediated executor has no per-project depot"
    try:
        path = destination(cfg, agent, lot_project, canons=canons)
    except (WorkdirError, canon.CanonError) as exc:
        return str(exc)
    return None if path and os.path.isdir(path) else "Host working directory absent: %r" % path
