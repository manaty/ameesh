# SPDX-License-Identifier: AGPL-3.0-only
"""#5087: machine-readable, reversible hold on new review evidence."""
from __future__ import annotations

from . import storage, work


def carried_reviewers(db, lot: int, names) -> list[str]:
    """A mechanical carry cannot undo a withdrawal, including via an alias."""
    import json

    return [row["name"] for row in db.query(
        "SELECT n.name FROM jsonb_array_elements_text(%s::jsonb) n(name)"
        " WHERE NOT EXISTS (SELECT 1 FROM work_review_withdrawals w"
        " WHERE w.work_item_id = %s AND w.withdrawn"
        " AND w.reviewer = coalesce(ameesh_agent_primary_locked(n.name), n.name))",
        (json.dumps(list(names)), int(lot)))]


def change(cfg, db, name: str, *, reason: str, clear: bool = False,
           actor: str | None = None) -> dict:
    reason = (reason or "").strip()
    if not reason or len(reason) > 2000:
        raise work.WorkError("review hold: --reason must contain 1 to 2000 characters")
    who = work.corrector(cfg, db, actor, what="review hold", auditor=True)
    if storage.is_remote(db):
        raise work.WorkError("review hold requires a native operator connection")
    result = storage.of(db).agents.review_hold(name, reason=reason, actor=who, clear=clear)
    if result is None:
        raise work.WorkError("review hold: unknown agent: %s" % name)
    return result


def cmd_cli(cfg, args) -> int:
    import json
    import sys
    from . import db as db_mod

    db = db_mod.connect(cfg)
    try:
        db_mod.require_schema(db)
        result = change(cfg, db, args.name, reason=args.reason,
                        clear=args.clear, actor=args.actor)
    except work.WorkError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False) if args.json else
          "%s: review hold %s" % (result["name"], "cleared" if args.clear else "set"))
    return 0
