#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""One-shot RESERVATIONS.md import; dry-run unless --apply is supplied."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ameesh import config, db, migration_reservations
from ameesh.work import WorkError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="reviewed RESERVATIONS.md")
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    parser.add_argument("--mapping", type=Path, help='JSON object: {"323": 123, "324": 456}')
    parser.add_argument("--apply", action="store_true", help="write all reservations in one transaction")
    parser.add_argument("--actor", default="", help="author recorded in the lot journal")
    args = parser.parse_args()
    if args.apply and not args.actor.strip():
        parser.error("--apply requires --actor for the lot journal")
    conn = None
    try:
        rows = migration_reservations.parse_markdown(args.file.read_text())
        mapping = json.loads(args.mapping.read_text()) if args.mapping else {}
        cfg = config.load()
        conn = db.connect(cfg)
        result = migration_reservations.import_reservations(
            conn, rows, mapping, repo=args.repo, apply=args.apply, actor=args.actor, cfg=cfg)
        print(json.dumps({"dry_run": not args.apply, "reservations": result}, indent=2))
        return 0
    except (WorkError, db.DbError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
