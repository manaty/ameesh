# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit pilot commands for #1467; no runner installation or activation.

Run with ``python -m ameesh.train_cli --project NAME show|give|withdraw``.
The pilot reviews the JSON manifest returned by show and supplies that exact
file to give/withdraw. Reading a new current plan never silently grants it.
Withdrawal needs neither the canon loader nor a functioning CI/Git adapter.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config, db as db_mod, merge_queue, radar, train_fire, train_store
from .train import TrainError


def execute(cfg, db, project, action, *, manifest=None, receipt=None):
    target = merge_queue._remote_branch(project.target)
    if not target:
        raise TrainError("explicit project target required")
    ledger = train_store.Ledger(db, project, target, "pilot-command")
    fires = train_fire.Fires(cfg, ledger)
    if action == "show":
        state = ledger.load()
        if state is None:
            raise TrainError("no persisted train for this project target")
        plan = state.get("plan", {})
        train_fire.binding(plan)
        return dict(phase=state["phase"], revision=state["revision"], plan=plan,
                    fire=ledger.ops.train_fire_read(ledger.key, plan["id"]))
    if action not in ("give", "withdraw"):
        raise TrainError("train command: show | give | withdraw expected")
    train_fire.binding(manifest)
    # No --actor override: the existing authority guard resolves the bound
    # pilot session. Storage compares the whole manifest under the target lock.
    if action == "give":
        return fires.give(manifest)
    if isinstance(receipt, bool) or not isinstance(receipt, int) or receipt <= 0:
        raise TrainError("positive exact fire receipt required for withdrawal")
    return fires.withdraw(manifest, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("show", help="print persisted plan and current fire receipt")
    for action in ("give", "withdraw"):
        command = commands.add_parser(action)
        command.add_argument("--manifest", required=True,
                             help="JSON file containing exactly the reviewed plan object")
        if action == "withdraw":
            command.add_argument("--receipt", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
        project = radar.settings_of(cfg).projects.get(args.project)
        if project is None:
            raise TrainError("project absent from radar.projects: %s" % args.project)
        manifest = None
        if args.action != "show":
            with open(args.manifest, encoding="utf-8") as source:
                manifest = json.load(source)
            train_fire.binding(manifest)
        db = db_mod.open_db(cfg)
        try:
            result = execute(cfg, db, project, args.action, manifest=manifest,
                             receipt=getattr(args, "receipt", None))
        finally:
            db.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TrainError, db_mod.DbError, radar.SettingsError, ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
