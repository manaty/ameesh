# SPDX-License-Identifier: AGPL-3.0-only
"""The installed half of the contract witness (docs/api/module-contract-v1.md).

It implements only the version 1 Module interface: `NAME`, `descriptor`,
`CAPABILITIES`, `main(db, argv, *, actor)` and `audit(store, project)`. It
imports nothing from ameesh and calls only the named `ModuleStore`
operations of the contract; the `db` argument is never queried.
"""
import argparse
import json

NAME = "contract-witness"
TYPES = ("witness-check",)
CAPABILITIES = ("lifecycle", "cli", "audit")
#: The milestone kind the witness records and audits.
KIND = "witness-seen"


def descriptor(options=None):
    options = dict(options or {})
    unknown = sorted(set(options) - {"strict"})
    if unknown:
        raise ValueError("unknown contract-witness option(s): %s" % ", ".join(unknown))
    # A milestone kind is admitted only if its schema is declared here;
    # `strict` also requires a reviewer in the payload.
    strings = ["note", "reviewer"] if options.get("strict") else ["note"]
    return {"types": list(TYPES), "schemas": {KIND: {"strings": strings}}, "rules": []}


def main(db, argv, *, actor):
    parser = argparse.ArgumentParser(prog="ameesh module contract-witness")
    parser.add_argument("command", choices=("whoami",))
    args = parser.parse_args(argv)
    # Help exits in parse_args, so `db` may be None only before this line.
    print(json.dumps({"command": args.command, "actor": actor,
                      "connected": db is not None}, sort_keys=True))
    return 0


def audit(store, project):
    """One finding per state the witness can observe through the store."""
    findings = []
    if not store.enabled(project, NAME):
        findings.append(_finding(project, "witness-not-enabled"))
    if not store.admits(project, TYPES[0]):
        findings.append(_finding(project, "witness-type-not-admitted"))
    return findings


def record_and_read(store, item_id, *, actor):
    """Record then read back one milestone through the named operations."""
    recorded = store.record(item_id, NAME, KIND, "subject-1", {"note": "seen"}, actor=actor)
    latest = store.latest(item_id, NAME, KIND)
    history = store.milestones(item_id, limit=10)
    return recorded, latest, history


def _finding(project, rule):
    return {"type": "module_deviation", "module": NAME, "project": project,
            "rule": rule, "detail": "contract witness: %s" % rule}
