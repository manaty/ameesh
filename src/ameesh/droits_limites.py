# SPDX-License-Identifier: AGPL-3.0-only
"""Replay the limited logins' grants after the staging migrations (#7873).

A migration that adds a relation, or a grant added to `_bureau-droits.sql`
after the role was installed (#7650: SELECT on module_schemas), reached the
hub only when an administrator re-ran `role-bureau.sql` by hand. The staging
sequence now runs `deploy/sql/rejeu-droits-limites.sql` right after
`migrate`, under the same connection (the table owner, never a superuser),
before any executor is restarted: GRANT and REVOKE of the reviewed scripts,
then the contract audit `_contrat-bureau.sql` in the same transaction. No
role is created and no password is touched; an absent role is skipped.

Exit codes: 0 replayed and audited (or skipped), 1 refused (audit or grant
failure, the violated right named on stderr), 2 could not run (psql or
database unavailable).
"""
from __future__ import annotations

import argparse
import os
import sys

from . import config, db as db_mod

SCRIPT = "rejeu-droits-limites.sql"
#: the limited logins whose grants are replayed, each with its own spec
ROLES = ("bureau_goal",)


def replay(cfg, sql_dir: str, role: str) -> tuple[int, str]:
    """Run the replay script for `role`; (exit code, one-line verdict)."""
    path = os.path.join(sql_dir, SCRIPT)
    if not os.path.isfile(path):
        return 2, "droits limites : %s introuvable" % path
    try:
        driver = db_mod.PsqlDriver(cfg)
    except db_mod.Unavailable as exc:
        return 2, "droits limites : %s" % exc
    try:
        proc = driver._run(["-v", "role=%s" % role, "-f", path])
    except db_mod.Unavailable as exc:
        return 2, "droits limites : %s" % exc
    finally:
        driver.close()
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        return 0, proc.stdout.strip() or "droits limites : %s rejoues" % role
    detail = next((line.split("ERROR:", 1)[1].strip() for line in proc.stderr.splitlines()
                   if "ERROR:" in line), "psql code %d" % proc.returncode)
    # psql -v ON_ERROR_STOP : 3 = erreur SQL du script, le reste ne l'a pas joué
    code = 1 if proc.returncode == 3 else 2
    return code, "droits limites : %s refuse : %s" % (role, detail)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the limited logins' grants and audit them (staging, #7873)")
    parser.add_argument("--sql-dir", required=True,
                        help="deploy/sql of the snapshot being deployed")
    parser.add_argument("--role", action="append",
                        help="limited login to replay (default: %s)" % ", ".join(ROLES))
    args = parser.parse_args(argv)
    cfg = config.load(register_credentials=False)
    worst = 0
    for role in args.role or ROLES:
        code, verdict = replay(cfg, args.sql_dir, role)
        print(verdict, file=sys.stderr if code else sys.stdout)
        worst = max(worst, code)
    return worst


if __name__ == "__main__":
    sys.exit(main())
