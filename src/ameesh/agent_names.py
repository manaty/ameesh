# SPDX-License-Identifier: AGPL-3.0-only
"""L243b: one agent identity, primary name and alternative spellings.

Directory aliases in identity.py are display hints and are unrelated. Agent
aliases come from cards/registry, never from the cwd or a caller's environment.
Unknown names are returned unchanged so each caller keeps its existing refusal.
"""
from __future__ import annotations

import logging

from . import storage


def resolve(name: str, *, db=None, canon=None, cfg=None, operation: str = "lookup", row=None) -> str:
    """Resolve an agent spelling to its primary, without granting authority.

    Database resolutions are audited whenever the spelling changes. For an
    offline canon, require a unique identity: ambiguity raises rather than
    choosing the first card. Human references and broadcast stay untouched.
    """
    if not name or name == "all" or name.startswith("human:"):
        return name
    known = getattr(db, "_agent_primary_names", None) if db is not None else None
    if row is not None:
        primary = row.get("name") or name
    elif known is not None and name in known:
        return name
    elif db is not None:
        primary = storage.of(db).agents.resolve_name(name, operation=operation)
    else:
        primary = None
    if primary is not None:
        if known is not None and (row is not None or primary != name):
            # Only verified primaries, never alias mappings. The mailbox CLI
            # resets this set for every command; runners never enable it.
            known.add(primary)
        if primary != name:
            logging.getLogger("ameesh.identity").warning(
                "agent alias %s -> %s (%s)", name, primary, operation)
        return primary
    if canon is None and cfg is not None:
        from . import canon as canon_mod
        from types import SimpleNamespace
        if canon_mod.configured(cfg):
            canons = canon_mod.from_config_all(cfg)
            canon = SimpleNamespace(agents=[a for c in canons for a in c.agents])
    if canon is not None:
        matches = [a for a in canon.agents
                   if name == a.title or name in (getattr(a, "aliases", None) or [])]
        if len({a.title for a in matches}) > 1:
            raise ValueError("ambiguous agent name: %s" % name)
        return matches[0].title if matches else name
    return name


def conflicts(db, canon) -> list:
    """Cross-canon/registry namespace checks before declarative writes.

    SQL guards recheck under per-name locks to close concurrent admission.
    """
    from .canon import Finding, ERROR
    findings = []
    for agent in canon.agents:
        for name in [agent.title] + list(agent.aliases or []):
            row = storage.of(db).agents.get(name)
            key = storage.of(db).keys.info(name) if name != agent.title else None
            if ((row is not None and row["name"] != agent.title)
                    or (key or {}).get("holder") == "human"):
                findings.append(Finding(
                    "agent-alias-conflict", ERROR,
                    "name or alias %s already belongs to another registered identity" % name,
                    agent=agent.title, member=agent.fiche.member, path=agent.fiche.path,
                    untrusted=canon.untrusted))
    return findings
