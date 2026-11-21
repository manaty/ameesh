# SPDX-License-Identifier: AGPL-3.0-only
"""Replay the supervisor role's contract after the staging migrations (#7982).

`deploy/sql/role-superviseur.sql` grants the supervisor role its contract of
columns relation by relation. A migration that adds a column to that contract
(0602: `agent_watches.last_fired_at`, `.last_fired_reason`, `.last_attempt_at`)
reached the shared database with the packet, but nothing replayed the script:
its only replay was the `role` step of `poste.sh`, i.e. the owner's station
update. With the station absent (reported, updated on its return), the three
new columns stayed without GRANT and every supervisor read of them failed.

The staging sequence now runs `deploy/sql/rejeu-superviseur.sql` right after
`migrate`, under the same connection as the migrations (the table and database
owner, never a superuser), before any executor is restarted. The script replays
`role-superviseur.sql` in replay mode: REVOKE/GRANT of the reviewed contract and
the `_contrat-superviseur.sql` audit in the same transaction. No role is created
or altered and no password is touched; an absent role is skipped (its creation
remains `role-superviseur.sql`, an administrator gesture). A refused contract
(missing or extra right, missing contract column, default ACL, membership)
cancels the transaction and names the violation on stderr.

Exit codes: 0 replayed and audited (or skipped), 1 refused (audit or grant
failure, the violated right named on stderr), 2 could not run (psql, database
or script unavailable).
"""
from __future__ import annotations

import argparse
import os
import sys

from . import config, db as db_mod

SCRIPT = "rejeu-superviseur.sql"
DEFAULT_ROLE = "ameesh_superviseur"


def replay(cfg, sql_dir: str, role: str = DEFAULT_ROLE) -> tuple[int, str]:
    """Run the replay script for `role`; (exit code, one-line verdict)."""
    path = os.path.join(sql_dir, SCRIPT)
    if not os.path.isfile(path):
        return 2, "superviseur : %s introuvable" % path
    try:
        driver = db_mod.PsqlDriver(cfg)
    except db_mod.Unavailable as exc:
        return 2, "superviseur : %s" % exc
    try:
        proc = driver._run(["-v", "role=%s" % role, "-f", path])
    except db_mod.Unavailable as exc:
        return 2, "superviseur : %s" % exc
    finally:
        driver.close()
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        return 0, proc.stdout.strip() or "superviseur : %s rejoue" % role
    detail = next((line.split("ERROR:", 1)[1].strip() for line in proc.stderr.splitlines()
                   if "ERROR:" in line), "psql code %d" % proc.returncode)
    # psql -v ON_ERROR_STOP : 3 = erreur SQL du script, le reste ne l'a pas joué
    code = 1 if proc.returncode == 3 else 2
    return code, "superviseur : %s refuse : %s" % (role, detail)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the supervisor role's contract and audit it (staging, #7982)")
    parser.add_argument("--sql-dir", required=True,
                        help="deploy/sql of the snapshot being deployed")
    parser.add_argument("--role", default=DEFAULT_ROLE,
                        help="supervisor role to replay (default: %s)" % DEFAULT_ROLE)
    args = parser.parse_args(argv)
    cfg = config.load(register_credentials=False)
    try:
        code, verdict = replay(cfg, args.sql_dir, args.role)
    except db_mod.DbError as exc:
        print("superviseur : rejeu illisible : %s" % exc, file=sys.stderr)
        return 2
    print(verdict, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
