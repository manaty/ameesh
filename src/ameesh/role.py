# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh role status` — the holder, the epoch and the ranked candidates.

L165 (EP2), study `docs/design/etudes/election-pilote.md` §2-§3. Read-only: the
candidate hosts publish their standings from their runners (`election.
publish_self`), and this command prints the freshest ones. A stale standing is
shown as stale, never as an old score, and only fresh-ready candidates are
ranked (the others keep their diagnostic: host gate, canon, placement,
credential, budget).

`--json` prints one object per role (one per line), like `ameesh hosts --json`.
"""
from __future__ import annotations

import argparse
import json

from . import config, db as db_mod, election


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ameesh role")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        "status", help="holder, epoch and ranked candidates of elected roles")
    status.add_argument("role", nargs="?",
                        help="an elected role (agent name); otherwise all of them")
    status.add_argument("--json", action="store_true",
                        help="one JSON object per role (one per line)")
    args = parser.parse_args(argv)
    cfg = config.load()
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
