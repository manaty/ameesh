# SPDX-License-Identifier: AGPL-3.0-only
"""L385: explicit prerequisites, service validation and work CLI."""
from __future__ import annotations

import json

from . import storage, work


def _id(value):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise work.WorkError("dependency lot identifiers must be positive integers")
    return value


def add(db, item_id, blocker_id, *, actor):
    _id(item_id)
    _id(blocker_id)
    if not isinstance(actor, str) or not actor.strip():
        raise work.WorkError("a dependency edit needs an identified actor")
    result = storage.of(db).work_dependencies.add(item_id, blocker_id, actor=actor)
    errors = {"self_dependency": "a lot cannot depend on itself",
              "unknown_lot": "dependency endpoints must be existing lots",
              "finished_lot": "new dependencies require two open lots",
              "dependency_cycle": "this dependency would create a cycle"}
    if result.get("error"):
        raise work.WorkError(errors[result["error"]])
    return result["added"]


def remove(db, item_id, blocker_id, *, actor):
    _id(item_id)
    _id(blocker_id)
    if not isinstance(actor, str) or not actor.strip():
        raise work.WorkError("a dependency edit needs an identified actor")
    if work.get(db, item_id) is None or work.get(db, blocker_id) is None:
        raise work.WorkError("dependency endpoints must be existing lots")
    return storage.of(db).work_dependencies.remove(item_id, blocker_id, actor=actor)


def grouped(db, item_ids):
    result = {}
    for edge in storage.of(db).work_dependencies.for_items(item_ids):
        result.setdefault(int(edge["work_item_id"]), []).append(edge)
    return result


def wait_label(wait):
    if not wait:
        return None
    if wait["what"] == "scheduled":
        return "planned start %s" % wait["start"]
    return "waiting on lots %s" % ", ".join("#%d" % i for i in wait["lots"])


def add_parser(sub, func):
    parser = sub.add_parser("dependencies", help="inspect or edit explicit blocking prerequisites")
    parser.add_argument("id", type=int, help="waiting lot")
    edit = parser.add_mutually_exclusive_group()
    edit.add_argument("--add", type=int, metavar="LOT", help="add a prerequisite lot")
    edit.add_argument("--remove", type=int, metavar="LOT", help="remove a prerequisite lot")
    parser.add_argument("--actor", default="")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=func)


def run(db, args):
    _id(args.id)
    if work.get(db, args.id) is None:
        raise work.WorkError("lot #%d not found" % args.id)
    changed = None
    if args.add is not None:
        changed = add(db, args.id, args.add, actor=args.actor)
    elif args.remove is not None:
        changed = remove(db, args.id, args.remove, actor=args.actor)
    edges = grouped(db, [args.id]).get(args.id, [])
    if args.json:
        print(json.dumps({"id": args.id, "changed": changed, "dependencies": edges},
                         ensure_ascii=False, indent=2))
    else:
        if changed is not None:
            print("lot #%d: dependency %s" % (args.id, "updated" if changed else "unchanged"))
        for edge in edges:
            print("#%d depends on #%d (%s, %s)" % (
                args.id, edge["blocker_id"], edge["blocker_state"],
                "blocking" if edge["blocking"] else "satisfied"))
        if not edges:
            print("lot #%d: no explicit prerequisites" % args.id)
    return 0
