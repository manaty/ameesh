# SPDX-License-Identifier: AGPL-3.0-only
"""L87b: a human's durable pause, separate from budget/pressure and leases.

The authenticated API supplies actor/principal; the local CLI acts for its
own human. Clearing this flag never changes status or adds a resume prompt.
"""
from __future__ import annotations

import re

from . import storage
from .config import NAME_RE

REASON_MAX = 2000
HUMAN_RE = re.compile(r"human:[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")


class PauseRefused(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def change(db, name: str, *, paused: bool, reason: str, actor: str,
           principal: str | None = None) -> dict:
    """Change the hold and record by whom/for whom, once per transition.

    Native operator operation, absent from the mediated executor contract.
    Authorization belongs to the authenticated caller, never a request body
    or the server's agent identity.
    """
    principal = actor if principal is None else principal
    if (not isinstance(actor, str) or not HUMAN_RE.fullmatch(actor)
            or not isinstance(principal, str) or not HUMAN_RE.fullmatch(principal)):
        raise PauseRefused("bad_actor", "a human actor and principal are required")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise PauseRefused("bad_agent", "a valid agent name is required")
    if not isinstance(paused, bool):
        raise PauseRefused("bad_pause", "paused must be a boolean")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > REASON_MAX:
        raise PauseRefused("bad_reason", "reason: 1 to %d characters required" % REASON_MAX)
    if storage.is_remote(db):
        raise PauseRefused("native_only", "operator pause needs a native operator connection")
    result = storage.of(db).agents.operator_pause(
        name, paused=paused, reason=reason.strip(), actor=actor, principal=principal)
    if result is None:
        raise PauseRefused("unknown_agent", "unknown agent: %s" % name)
    if result.get("mode") != "execute":
        raise PauseRefused("external_agent", "an external session cannot be paused by the runner")
    return result


def cmd_cli(cfg, args, *, paused: bool) -> int:
    """Local human CLI. No client-supplied deputyship, no agent impersonation."""
    import json
    import sys
    from . import db as db_mod, identity, serve

    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        try:
            binding = identity.resolve_binding(cfg, db)
            if binding.name or binding.ok:
                raise PauseRefused("bad_actor", "an agent session cannot act as the local human")
            human = serve.owner(cfg, args.human)
            out = change(db, args.agent, paused=paused, reason=args.reason,
                         actor=human, principal=human)
        except (PauseRefused, ValueError) as exc:
            # Le même chemin sert « resume --operator » : nommer le geste
            # réellement demandé (N3 de la R2 sur #1462).
            print("ameesh operator %s: %s" % ("pause" if paused else "resume", exc),
                  file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print("%s: operator pause %s%s" % (
                args.agent, "set" if out["operator_paused"] else "cleared",
                "" if out["changed"] else " (unchanged)"))
        return 0
    finally:
        db.close()
