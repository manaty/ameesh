# SPDX-License-Identifier: AGPL-3.0-only
"""L243b: one agent identity, primary name and alternative spellings.

Directory aliases in identity.py are display hints and are unrelated. Agent
aliases come from cards/registry, never from the cwd or a caller's environment.
Unknown names are returned unchanged so each caller keeps its existing refusal.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging

from . import storage

#: #8152 : noms primaires confirmés pendant la passe courante d'un worker
#: (None hors passe). Une ligne lue du registre confirme son nom à cet instant ;
#: une passe au repos appelait sinon `ameesh_resolve_agent_name` quatre fois
#: pour le même nom (43 M appels chez IMA, manaty/ameesh#6379). Propre au fil,
#: oublié à la fin de la passe : un renommage est vu à la passe suivante.
_PASS_PRIMARIES: contextvars.ContextVar = contextvars.ContextVar(
    "ameesh_pass_primaries", default=None)


@contextlib.contextmanager
def pass_scope():
    """#8152 : la portée d'une passe ; `resolve` y garde les primaires vus."""
    token = _PASS_PRIMARIES.set(set())
    try:
        yield
    finally:
        _PASS_PRIMARIES.reset(token)


def note_row(row) -> None:
    """#8152 : une ligne `agent_registry` lue dans la passe confirme son nom."""
    known = _PASS_PRIMARIES.get()
    if known is not None and row and row.get("name"):
        known.add(row["name"])


def primary_known(name: str) -> bool:
    """#8168 : ce nom est-il déjà confirmé primaire dans la passe courante ?

    `registry.get` s'en sert pour relire un primaire sans repasser par
    `ameesh_resolve_agent_name` : la première lecture de la passe résout et
    audite, les suivantes lisent la ligne directement. Hors passe, faux : on
    ne retient rien d'un passage à l'autre (un renommage reste vu)."""
    known = _PASS_PRIMARIES.get()
    return known is not None and name in known


def resolve(name: str, *, db=None, canon=None, cfg=None, operation: str = "lookup", row=None) -> str:
    """Resolve an agent spelling to its primary, without granting authority.

    Database resolutions are audited whenever the spelling changes. For an
    offline canon, require a unique identity: ambiguity raises rather than
    choosing the first card. Human references and broadcast stay untouched.
    """
    if not name or name == "all" or name.startswith("human:"):
        return name
    in_pass = _PASS_PRIMARIES.get()
    if row is None and in_pass is not None and name in in_pass:
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
