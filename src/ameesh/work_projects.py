# SPDX-License-Identifier: AGPL-3.0-only
"""Project inheritance for lots created through the existing mail command."""
from . import projects, registry, work


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


def mail_project(cfg, db, sender, recipient):
    """Sender first, then recipient; preserve unclassified creation if unknown."""
    for name in (sender, recipient):
        value = agent_project(cfg, db, name)
        if value:
            return value
    return ""
