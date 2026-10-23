# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit pilot commands for #1467; no runner installation or activation.

Run with ``python -m ameesh.train_cli --project NAME
show|give|withdraw|abandon``. The pilot reviews the JSON manifest returned by
show and supplies that exact file to give/withdraw/abandon. Reading a new
current plan never silently grants it. Withdrawal needs neither the canon
loader nor a functioning CI/Git adapter.

A persisted plan that was never fired has no receipt to withdraw: ``abandon``
is its terminal gesture. It refuses an active/uncertain fire (withdraw or
reconcile first), writes the terminal phase with the pilot actor, and releases
the target lease it had to take (or adopts when that lease has expired). The
next cutoff may then compose a new plan.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config, db as db_mod, merge_queue, radar, train_fire, train_store, work
from .train import TERMINAL, TrainError, digest


def _pilot(cfg, db):
    """The declared pilot of the bound session, never a caller-supplied actor."""
    try:
        return work.corrector(cfg, db, None, history=False)
    except work.WorkError as exc:
        raise TrainError("train abandon requires a pilot with a declared role: %s"
                         % exc) from exc


def _abandon(ledger, plan, *, actor, reason, now):
    """Durably terminate one exact non-fired plan and give its lease back."""
    fingerprint = train_fire.binding(plan)
    state = ledger.load()
    if state is None:
        raise TrainError("no persisted train for this project target")
    phase = state.get("phase")
    if phase in TERMINAL:
        raise TrainError("train is already terminal (%s): nothing to abandon" % phase)
    if phase == "promoting":
        # The push/merge may have happened: only reconciliation can tell.
        raise TrainError("uncertain promotion: abandon refused; reconcile the target first")
    current = state.get("plan")
    if not isinstance(current, dict) or digest(current) != fingerprint:
        raise TrainError("abandon must name the exact persisted plan")
    fire = ledger.ops.train_fire_read(ledger.key, current["id"])
    if fire and fire.get("state") in ("active", "promoting", "landed"):
        raise TrainError("plan carries a fire receipt (%s): withdraw it with --receipt first"
                         % fire["state"])
    # The target lease is the same reservation as a merge: take it, or adopt an
    # expired one, only for the terminal write, then give it back.
    epoch = ledger.acquire(None)
    if epoch is None:
        raise TrainError("target reservation is held by another live train; "
                         "abandon once its lease expires")
    try:
        state["phase"] = "invalidated"
        state["abandoned"] = dict(actor=actor, at_ts=now, reason=reason,
                                  previous_phase=phase, plan=current["id"],
                                  fire=(fire or {}).get("id"))
        saved = ledger.abandon(state, int(state["revision"]), epoch)
    finally:
        ledger.release(epoch)
    return dict(phase=saved["phase"], revision=saved["revision"], plan=saved["plan"],
                abandoned=saved["abandoned"],
                fire=ledger.ops.train_fire_read(ledger.key, current["id"]))


def execute(cfg, db, project, action, *, manifest=None, receipt=None, reason=None):
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
        result = dict(phase=state["phase"], revision=state["revision"], plan=plan,
                      fire=ledger.ops.train_fire_read(ledger.key, plan["id"]))
        if state.get("abandoned"):
            result["abandoned"] = state["abandoned"]
        return result
    if action not in ("give", "withdraw", "abandon"):
        raise TrainError("train command: show | give | withdraw | abandon expected")
    train_fire.binding(manifest)
    # No --actor override: the existing authority guard resolves the bound
    # pilot session. Storage compares the whole manifest under the target lock.
    if action == "give":
        return fires.give(manifest)
    if action == "abandon":
        return _abandon(ledger, manifest, actor=_pilot(cfg, db),
                        reason=reason or "", now=time.time())
    if isinstance(receipt, bool) or not isinstance(receipt, int) or receipt <= 0:
        raise TrainError("positive exact fire receipt required for withdrawal")
    return fires.withdraw(manifest, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("show", help="print persisted plan and current fire receipt")
    for action in ("give", "withdraw", "abandon"):
        command = commands.add_parser(action)
        command.add_argument("--manifest", required=True,
                             help="JSON file containing exactly the reviewed plan object")
        if action == "withdraw":
            command.add_argument("--receipt", type=int, required=True)
        if action == "abandon":
            command.add_argument("--reason", default="",
                                 help="why this never-fired plan is abandoned (journaled)")
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
                             receipt=getattr(args, "receipt", None),
                             reason=getattr(args, "reason", None))
        finally:
            db.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (TrainError, db_mod.DbError, radar.SettingsError, ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
