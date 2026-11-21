# SPDX-License-Identifier: AGPL-3.0-only
"""Replay the replayable role contracts after the staging migrations (#7986).

`#7982` decoupled the supervisor role's contract from the owner's station: the
staging sequence replays `deploy/sql/rejeu-superviseur.sql` right after
`migrate`, under the table owner's connection, before restarting any executor.
The same hole exists for every other role whose column/table contract is a
`deploy/sql/role-*.sql` script with no automatic caller: a migration that adds
a column or a table to the contract leaves the role without that right until an
administrator re-runs the script by hand.

This module carries the declared list of scripts that the migration owner can
replay. Each entry has its own SQL replay script, which:

* checks that the role exists and prints `absent, rejeu saute` otherwise;
* replays the grant/revoke part of the reviewed role script in replay mode
  (no `CREATE ROLE`, no `ALTER ROLE`, no password, no membership), and runs the
  same contract audit in the same transaction;
* refuses (non-zero) on a missing or extra right, naming it, and rolls back.

The scripts that cannot run under the migration owner are declared in
`EXEMPTIONS`, with the reason. The two role scripts already covered elsewhere
(the supervisor by #7982, the limited bureau login by #7873) are declared in
`REPLAYED_ELSEWHERE`. `tests/test_7986_rejeu_roles.py` requires every
`deploy/sql/role-*.sql` to appear in exactly one of those three sets.

Exit codes: 0 every replay succeeded or was skipped, 1 one contract was refused
(the violated right is named on stderr), 2 could not run (psql, database or
script unavailable).
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from . import config, db as db_mod


@dataclass(frozen=True)
class Replay:
    """One replayed role script and its SQL replay script."""

    role_script: str
    script: str
    #: default role names the script addresses, for the declared list/readers
    roles: tuple[str, ...] = ()
    #: extra psql -v variables required by the replay script
    variables: tuple[tuple[str, str], ...] = ()


#: The role scripts replayed by this module. Each one is a grant-only contract:
#: the migration owner (table owner, no CREATEROLE) can reapply it.
REPLAYS: tuple[Replay, ...] = (
    Replay(
        "role-superviseur-contenus.sql",
        "rejeu-superviseur-contenus.sql",
        ("ameesh_superviseur_contenus",),
    ),
    Replay(
        "role-approve.sql",
        "rejeu-approve.sql",
        ("ameesh_approve",),
    ),
    Replay(
        "role-module-writers.sql",
        "rejeu-module-writers.sql",
        ("ameesh_canon_sync", "ameesh_it_proof", "ameesh_it_qa"),
    ),
    Replay(
        "role-pod-provisioner.sql",
        "rejeu-pod-provisioner.sql",
        ("ameesh_pod_provisioner", "ameesh_pod_admission"),
    ),
)

#: Role scripts already replayed by another migration-path caller.
REPLAYED_ELSEWHERE: dict[str, str] = {
    "role-superviseur.sql":
        "rejeu-superviseur.sql after migrate (#7982, ameesh.droits_superviseur)",
    "role-bureau.sql":
        "rejeu-droits-limites.sql after migrate (#7873, ameesh.droits_limites; "
        "the login names are read from limited_logins)",
}

#: Role scripts that cannot be replayed by the migration owner, with the reason
#: that keeps the gap a declared administrator gesture rather than a hole.
EXEMPTIONS: dict[str, str] = {
    "role-vm-probe.sql":
        "superuser only: creates/alters the LOGIN vm_probe and the NOLOGIN "
        "vm_probe_writer, creates the SECURITY DEFINER functions owned by "
        "vm_probe_writer and audits pg_authid. The migration owner owns the "
        "tables but neither the functions nor CREATEROLE, so it cannot replay "
        "the script or run its audit; the bureau applies it with "
        "_contrat-vm-probe.sql (#6298/#7951).",
    "role-executor.sql":
        "future objects are covered by ALTER DEFAULT PRIVILEGES executed by the "
        "migration owner, so a migration does not leave a missing grant; the "
        "administrator wrapper role-executor.sh / poste.sh reapply_executor_role "
        "reapplies the script itself (#L56).",
    "role-release-controller.sql":
        "superuser enrollment: creates an isolated NOLOGIN owner and transfers "
        "SECURITY DEFINER functions to it. The migration owner is not that "
        "superuser and cannot replay the transfer (#L232b).",
    "role-auditeur.sql":
        "a permission gesture, not a column contract: creates/alters the "
        "auditor's LOGIN role and its memberships in the two supervisor "
        "contracts. It grants no column itself; the contracts it inherits are "
        "replayed above (#2340).",
}


def replay(cfg, sql_dir: str, item: Replay) -> tuple[int, str]:
    """Run one replay script; (exit code, one-line verdict)."""
    path = os.path.join(sql_dir, item.script)
    if not os.path.isfile(path):
        return 2, "%s : %s introuvable" % (item.role_script, path)
    try:
        driver = db_mod.PsqlDriver(cfg)
    except db_mod.Unavailable as exc:
        return 2, "%s : %s" % (item.role_script, exc)
    try:
        args: list[str] = []
        for key, value in item.variables:
            args += ["-v", "%s=%s" % (key, value)]
        args += ["-f", path]
        proc = driver._run(args)
    except db_mod.Unavailable as exc:
        return 2, "%s : %s" % (item.role_script, exc)
    finally:
        driver.close()
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode == 0:
        return 0, proc.stdout.strip() or "%s rejoue" % item.role_script
    detail = next((line.split("ERROR:", 1)[1].strip() for line in proc.stderr.splitlines()
                   if "ERROR:" in line), "psql code %d" % proc.returncode)
    # psql -v ON_ERROR_STOP : 3 = SQL error of the script, the rest did not run it
    code = 1 if proc.returncode == 3 else 2
    return code, "%s refuse : %s" % (item.role_script, detail)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay the replayable role contracts and audit them (staging, #7986)")
    parser.add_argument("--sql-dir", required=True,
                        help="deploy/sql of the snapshot being deployed")
    parser.add_argument("--only", action="append", default=[],
                        choices=[item.role_script for item in REPLAYS],
                        help="replay only this role script (repeatable)")
    args = parser.parse_args(argv)
    cfg = config.load(register_credentials=False)
    worst = 0
    for item in REPLAYS:
        if args.only and item.role_script not in args.only:
            continue
        try:
            code, verdict = replay(cfg, args.sql_dir, item)
        except db_mod.DbError as exc:
            code, verdict = 2, "%s : rejeu illisible : %s" % (item.role_script, exc)
        print(verdict, file=sys.stderr if code else sys.stdout)
        worst = max(worst, code)
    return worst


if __name__ == "__main__":
    sys.exit(main())
