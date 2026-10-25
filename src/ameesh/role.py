# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh role status` — the holder, the epoch and the ranked candidates.

L165 (EP2), study `docs/design/etudes/election-pilote.md` §2-§3. Read-only: the
candidate hosts publish their standings from their runners (`election.
publish_self`), and this command prints the freshest ones. A stale standing is
shown as stale, never as an old score, and only fresh-ready candidates are
ranked (the others keep their diagnostic: host gate, canon, placement,
credential, budget).

`--json` prints one object per role (one per line), like `ameesh hosts --json`.

`ameesh role canary` — L165 (EP7 P3, #3433): a fenced gesture with no effect.
The drill needs a gesture that passes or fails on the lease epoch alone; a
real one (`work assign` on a witness lot) is refused by its own business rules
before the fence and proves nothing. The canary acts under the environment's
identity like any gesture (`AMEESH_ROLE` + `AMEESH_LEASE_EPOCH` of an EP5
timer, or `AGENT_MAIL_NAME` bound by its runner), calls `pilot_gesture.gate`
then `Gate.hold`: the lease renewal and ONE `pilot.gesture` event
(`gesture = ref = canary`) are its only writes, in one transaction. No lot,
mail, merge, queue, decision, packet or wake.

Exit codes: `0` accepted (the event id is printed); `1` refused — stale or
expired epoch, role without epoch, stopped role, unreadable registry: the
message names the holder, its host and its epoch, and nothing is journaled;
`2` no lease to fence (a human, the bureau, a host-pinned agent): nothing is
journaled either.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import config, db as db_mod, election, pilot_gesture

#: the drill's gesture and ref, kept stable for `data->>'gesture' = 'canary'`
CANARY = "canary"
#: `gate` returned None: the environment holds no elected lease
NO_LEASE = ("no lease to fence: neither AMEESH_ROLE nor an elected agent bound "
            "by its runner (a human, the bureau or a host-pinned agent); "
            "nothing journaled")


def canary(cfg, db) -> tuple[int, dict]:
    """The canary gesture under the environment's lease: (exit code, report).

    The refusal cannot journal anything (that is the fence): its proof is the
    exit code, the message and the absence of a `canary` event.
    """
    try:
        fence = pilot_gesture.gate(cfg, db, CANARY, CANARY)
        if fence is None:
            return 2, {"ok": False, "error": NO_LEASE}
        event = fence.hold(db)
    except pilot_gesture.GestureRefused as exc:
        return 1, {"ok": False, "error": str(exc)}
    return 0, {"ok": True, "role": fence.role or fence.agent, "epoch": fence.epoch,
               "host": fence.host, "runner": fence.runner, "event": event}


def _print_canary(code: int, report: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    elif report.get("ok"):
        print("canary accepted: role %s at epoch %s on %s (runner %s), event %s"
              % (report["role"], report["epoch"], report["host"] or "?",
                 report["runner"] or "?", report["event"]))
    else:
        print("role canary: %s" % report["error"], file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh role")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status", help="holder, epoch and ranked candidates of elected roles")
    status.add_argument("role", nargs="?",
                        help="an elected role (agent name); otherwise all of them")
    status.add_argument("--json", action="store_true",
                        help="one JSON object per role (one per line)")
    probe = commands.add_parser(
        "canary", help="no-op gesture fenced by the environment's lease epoch "
                       "(EP7 P3): 0 accepted, 1 refused, 2 no lease")
    probe.add_argument("--json", action="store_true",
                       help="{ok, role, epoch, host, runner, event} or {ok, error}")
    args = parser.parse_args(argv)
    cfg = config.load()
    if args.command == "canary":
        try:
            db = db_mod.connect(cfg)
        except db_mod.DbError as exc:
            return _print_canary(1, {"ok": False, "error": str(exc)}, args.json)
        try:
            return _print_canary(*canary(cfg, db), args.json)
        except db_mod.DbError as exc:
            return _print_canary(1, {"ok": False, "error": str(exc)}, args.json)
        finally:
            db.close()
    db = db_mod.connect(cfg)
    try:
        names = [args.role] if args.role else election.elected_names(db)
        if not names:
            if args.json:
                return 0
            print("no elected role declared (releve: election)")
            return 0
        reports = [election.status(db, name) for name in names]
        if args.json:
            for report in reports:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            for index, report in enumerate(reports):
                if index:
                    print()
                print("\n".join(election.format_status(report)))
        return 0
    except (ValueError, db_mod.DbError) as exc:
        print("role status: %s" % exc)
        return 1
    finally:
        db.close()
