# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit pilot commands for #1467; no runner installation or activation.

Run with ``python -m ameesh.train_cli --project NAME
show|give|withdraw|abandon|requalify``. The pilot reviews the JSON manifest
returned by show and supplies that exact file to give/withdraw/abandon.
Reading a new current plan never silently grants it. Withdrawal needs neither
the canon loader nor a functioning CI/Git adapter.

``requalify`` (#3366) is the mechanical pin gesture: when the canon moved and
its diff does not touch ``processus/mecanique.yaml`` or ``federation.yaml``,
it writes the gate's audited commit to every configuration file that declares
``train.projects.<p>.canon_commit`` and traces the move on the lot named by
``--lot``. A rules diff refuses.

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

from . import (config, db as db_mod, merge_queue, radar, train_fire,
               train_requalify, train_store, work)
from .train import TERMINAL, TrainError, digest


def _pilot(cfg, db, action="train abandon"):
    """The declared pilot of the bound session, never a caller-supplied actor."""
    try:
        return work.corrector(cfg, db, None, history=False)
    except work.WorkError as exc:
        raise TrainError("%s requires a pilot with a declared role: %s"
                         % (action, exc)) from exc


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
        plan = state.get("plan") or {}
        fire = None
        if plan:
            train_fire.binding(plan)
            fire = ledger.ops.train_fire_read(ledger.key, plan["id"])
        # #3139: a train may hold only the enrollment record (no packet yet).
        # The pilot reads the refusals here, lot by lot, without the code.
        result = dict(phase=state["phase"], revision=state["revision"], plan=plan,
                      fire=fire,
                      enrollment_deferred=state.get("enrollment_deferred") or [],
                      enrollment_checked_ts=state.get("enrollment_checked_ts"))
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
    requalify = commands.add_parser(
        "requalify", help="mechanically move the train canon pin to the gate commit")
    requalify.add_argument("--lot", type=int, required=True,
                           help="lot receiving the journal trace of the move")
    requalify.add_argument("--dry-run", action="store_true",
                           help="print the move; write nothing")
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
        if args.action == "requalify":
            db = db_mod.open_db(cfg)
            try:
                actor = _pilot(cfg, db, "train requalify")
                result = train_requalify.execute(
                    cfg, db, args.project, lot=args.lot, actor=actor,
                    dry_run=args.dry_run)
            finally:
                db.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
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
    except (TrainError, db_mod.DbError, radar.SettingsError, ValueError, OSError,
            work.WorkError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
