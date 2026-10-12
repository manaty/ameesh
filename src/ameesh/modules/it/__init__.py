# SPDX-License-Identifier: AGPL-3.0-only
"""IT lifecycle: releases, publications and observed artifact execution."""
from __future__ import annotations

NAME = "it"
TYPES = ("release", "publication")


def descriptor():
    return {
        "types": list(TYPES),
        "schemas": {
            "delivery": {"strings": ["artifact", "sha256"], "subject_field": "sha256",
                         "patterns": {"sha256": "^[0-9a-f]{64}$"}},
            "proof": {"strings": ["artifact", "sha256", "output"],
                      "arrays": ["command"], "equals": {"exit_code": 0},
                      "subject_field": "sha256", "binding_kind": "delivery",
                      "writer_role": "ameesh_it_proof",
                      "patterns": {"sha256": "^[0-9a-f]{64}$"}},
        },
        "rules": [{"types": list(TYPES), "states": ["merged", "promoted"],
                   "milestones": {"verdict": ["ok"]},
                   "subject_kind": "delivery", "evidence_kind": "proof"}],
    }


def item(db, item_id):
    from ... import work
    from ..storage import ModuleStore
    row = work.get(db, item_id)
    if row is None:
        raise ValueError("lot %s does not exist" % item_id)
    project = row.get("app") or row.get("workstream") or ""
    if row["type"] not in TYPES or not ModuleStore(db).enabled(project, NAME):
        raise ValueError("lot %s requires an active IT module and release/publication type" % item_id)
    return row


def declare(db, item_id, artifact, *, actor):
    from .execution import digest
    from ..storage import ModuleStore
    item(db, item_id)
    path, sha = digest(artifact)
    return ModuleStore(db).record(item_id, NAME, "delivery", sha,
                                  {"artifact": path, "sha256": sha}, actor=actor)


def prove(db, item_id, artifact, *, actor, runner=None, arguments=(), timeout=60, execution_user=None,
          snapshot_root=None, snapshot_limits=None):
    from .execution import execute
    from ..storage import ModuleStore
    item(db, item_id)
    store = ModuleStore(db)
    delivery = store.latest(item_id, NAME, "delivery")
    if delivery is None:
        raise ValueError("declare the delivered artifact before running its proof")
    evidence = execute(artifact, runner=runner, arguments=arguments, timeout=timeout,
                       execution_user=execution_user, snapshot_root=snapshot_root,
                       expected_sha=delivery["subject"], snapshot_limits=snapshot_limits)
    if evidence["sha256"] != delivery["subject"]:
        raise ValueError("executed artifact differs from the declared delivery")
    # The database rechecks this exact declaration under the item lock.
    return store.record(item_id, NAME, "proof", evidence["sha256"], evidence,
                        actor=actor, binding_id=delivery["id"])


def main(db, argv, *, actor):
    import argparse
    import json
    from ..storage import ModuleStore
    split = argv.index("--") if "--" in argv else len(argv)
    options, arguments = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(prog="ameesh module it")
    sub = parser.add_subparsers(dest="operation", required=True)
    for name in ("delivery", "proof"):
        command = sub.add_parser(name)
        command.add_argument("lot", type=int)
        command.add_argument("--artifact", required=True)
        if name == "proof":
            command.add_argument("--runner", help="optional interpreter/compatibility executable")
            command.add_argument("--timeout", type=float, default=60)
    show = sub.add_parser("show")
    show.add_argument("lot", type=int)
    args = parser.parse_args(options)
    if arguments and args.operation != "proof":
        parser.error("artifact arguments are only valid for proof")
    if args.operation == "show":
        row = ModuleStore(db).milestones(args.lot)
    elif args.operation == "delivery":
        row = declare(db, args.lot, args.artifact, actor=actor)
    else:
        from .proof_service import request
        project_row = item(db, args.lot)
        project = project_row.get("app") or project_row.get("workstream") or ""
        row = request(project, args.lot, args.artifact, runner=args.runner,
                      arguments=arguments, timeout=args.timeout)
    print(json.dumps(row, ensure_ascii=False, default=str))
    return 0


def audit(store, project):
    """Point 12: a release/publication without current execution evidence."""
    out = []
    for row in store.audit_evidence(project, NAME, TYPES, "delivery", "proof"):
        if row["proved"]:
            continue
        declaration = row["declaration_id"]
        missing = "delivery declaration missing" if declaration is None else "execution proof missing or stale"
        detail = ("IT procedure deviation: %s lot #%s in project %s: %s. "
                  "Declare the delivered artifact and record successful execution for its current "
                  "SHA-256 with ameesh module it delivery/proof; audit never executes it."
                  % (row["type"], row["id"], project, missing))
        out.append({"type": "module_deviation", "module": NAME,
                    "rule": "execution-proof", "project": project, "lot": row["id"],
                    "title": row["title"], "agent": None, "assignee": row["assignee"],
                    "lot_state": row["state"],
                    "declaration_id": declaration, "subject": row["subject"],
                    "since": row["declaration_ts"] or row["created_ts"],
                    "value": 1, "threshold": 0,
                    "reason": "it:execution-proof:%s" % (declaration or "undeclared"),
                    "detail": detail, "action": detail})
    return out
