# SPDX-License-Identifier: AGPL-3.0-only
"""Blocking staging migration check on the database's real pending list (#7368).

The generic gate (`ameesh staging-migrations`, #7364) classifies an *explicit*
inventory read from one frozen Git commit; it never reads a database. The
staging sequence must give it the migrations that are actually pending on the
database it is about to migrate -- not a Git delta (which can contain files
already applied) and not the whole migrations directory (which would judge
history).

This applicator-side reader computes that list read-only from
`schema_migrations`, maps each snapshot file back to its repository-relative
path, and asks the gate to classify exactly those frozen files. A refusal
stops the sequence before any SQL write and before any executor is restarted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import config, db as db_mod, migrations, staging_migrations


def pending_paths(db, directory: str | None = None) -> tuple[list[str], list[int]]:
    """Repository paths of the migrations this code still has to apply.

    Read-only: `migrations.check` only reads `schema_migrations`. An applied
    migration whose checksum changed is refused (the file is immutable), while
    versions applied by a newer schema that this code does not know are
    reported for the trace, not turned into a refusal (a deliberate rollback
    of the code is not a pending migration).
    """
    directory = migrations.MIGRATIONS_DIR if directory is None else directory
    missing, modified, unknown = migrations.check(db, directory)
    if modified:
        raise staging_migrations.Refused(
            "applied migration(s) modified after application: %s"
            % ", ".join(migration.label for migration in modified))
    paths = [migrations.REPO_PATH + "/" + os.path.basename(migration.path)
             for migration in missing]
    return paths, sorted(unknown)


def check(repo: str, sha: str, project: str, *, cfg=None, pending_cfg=None,
          directory: str | None = None) -> dict:
    """The gate's report on the database's real pending inventory, read-only.

    `cfg` carries the signed-decision proof channel; `pending_cfg` is the
    database whose ledger is read (a secondary base has its own config, while
    decisions stay on the mesh). Both may be None for the usual configuration.
    """
    proof_cfg = cfg or config.load(register_credentials=False)
    ledger_cfg = pending_cfg or proof_cfg
    ledger = db_mod.connect(ledger_cfg, writer=False)
    try:
        paths, unknown = pending_paths(ledger, directory)
    finally:
        ledger.close()
    report = staging_migrations.check_inventory(
        repo, sha, project, paths, cfg=proof_cfg)
    return dict(report, schema="ameesh-staging-migration-preflight/1",
                pending=paths, unknown_applied=unknown)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only staging preflight on the database's real pending migrations")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True, help="full frozen commit SHA")
    parser.add_argument("--project", default="ameesh",
                        help="project owning carrier lots (default: ameesh)")
    parser.add_argument("--config",
                        help="ordinary ameesh config carrying signed decisions (default: environment)")
    parser.add_argument("--pending-config",
                        help="config of the database whose migration ledger is read (default: --config)")
    parser.add_argument("--directory",
                        help="migration directory (default: this snapshot's src/ameesh/migrations)")
    args = parser.parse_args(argv)
    try:
        cfg = config.load_file(args.config) if args.config else None
        pending_cfg = config.load_file(args.pending_config) if args.pending_config else None
        report = check(args.repo, args.sha, args.project, cfg=cfg,
                       pending_cfg=pending_cfg, directory=args.directory)
    except (staging_migrations.Refused, migrations.DbError, db_mod.DbError,
            OSError, UnicodeError, ValueError, RuntimeError) as exc:
        print(json.dumps({"allowed": False, "reason": str(exc)}, ensure_ascii=False))
        print("staging migration preflight refused: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False))
    if not report["allowed"]:
        details = [row.get("reason") or "%s: %s" % (row.get("path"), row.get("classification"))
                   for row in report["files"] if not row.get("allowed")]
        print("staging migration preflight refused: %s" % "; ".join(details),
              file=sys.stderr)
        return 1
    print("staging migration preflight: %d pending migration(s) accepted"
          % len(report["pending"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
