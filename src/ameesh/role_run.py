# SPDX-License-Identifier: AGPL-3.0-only
"""L165 (EP5) — ``ameesh role run``: a mechanical command under a role's lease.

Study ``docs/design/etudes/election-pilote.md`` §6. The packet composer, the
GitHub watch and the CI watches are code; their timers are installed on every
candidate host, and only the host that holds the role's agent lease may run
them. This module is that gate, and nothing more:

* it **never claims** a lease. The lease belongs to the agent's runner (EP3);
  a timer that claimed would take the turn of a governance agent. A host that
  does not hold a live lease is on standby: exit 0, no effect;
* it reads the lease with the database clock, and asks for the *host of the
  current holder* and, when given, the **epoch**. The epoch is passed to the
  command (``AMEESH_LEASE_EPOCH``) so that an effect outside the database can
  be fenced by EP4 in the same transaction as its write;
* it is only the **entry** check. EP4's ``pilot_gesture`` is the single
  transaction guard of a pilot's gestures (merge, queue, assignment, packet,
  decision routing); this module never writes a lease, an epoch or an event.

The command is an argument vector, never a shell string. Its exit code is
returned unchanged. The wrapper's own refusals are:

* ``0`` — standby (another host holds the lease, the lease is expired, the
  role is stopped, or the remaining lease is below ``--min-left``): the timer
  did nothing, as expected on a standby host;
* ``1`` — the lease could not be read (database unavailable): the timer must
  be noticed, never silently considered standby;
* ``2`` — the invocation is invalid (unknown role, no host, no command).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import config as config_mod
from . import db as db_mod

#: environment given to the command. The epoch is the fencing token of EP4.
ENV_ROLE = "AMEESH_ROLE"
ENV_HOST = "AMEESH_ROLE_HOST"
ENV_EPOCH = "AMEESH_LEASE_EPOCH"
ENV_LEFT = "AMEESH_LEASE_LEFT"
ENV_OWNER = "AMEESH_LEASE_OWNER"

#: a role the wrapper may run; its name is an agent name (aliases resolved).
_SCHEMA = "ameesh-role-run/1"

#: the row read by the gate. The database clock is read in the same statement:
#: a host clock that drifts cannot make a dead lease look alive.
_READ_SQL = """
    SELECT name, host, releve, candidats, status,
           lease_owner, lease_epoch,
           extract(epoch from lease_expires_at)::float8 AS lease_expires_ts,
           extract(epoch from clock_timestamp())::float8  AS now_ts
      FROM agent_registry
     WHERE name = (SELECT ameesh_resolve_agent_name(%s, %s))
"""


class RoleError(ValueError):
    """The role cannot be read, or the invocation is invalid."""


def _read(db, name: str) -> dict | None:
    """One role row, with the database clock, without taking a lock.

    This is the timer's entry check, not the fencing of a pilot gesture: EP4
    owns that guard (`pilot_gesture`), which renews and writes the epoch in
    the transaction of the effect. Here a stale read only means the timer
    starts or waits; no effect is written by the wrapper itself.
    """
    if not isinstance(name, str) or not name.strip():
        raise RoleError("a non-empty agent name is required")
    rows = db.query(_READ_SQL, (name, "role-run"))
    return rows[0] if rows else None


def _report(row: dict, *, epoch: int | None = None) -> dict:
    expires = row.get("lease_expires_ts")
    now = row.get("now_ts")
    left = None if expires is None or now is None else float(expires) - float(now)
    stopped = str(row.get("status") or "") == "stopped"
    held = bool(row.get("lease_owner")) and expires is not None \
        and now is not None and float(expires) > float(now) and not stopped
    report = {
        "schema": _SCHEMA,
        "role": row["name"],
        "host": row.get("host"),
        "releve": row.get("releve"),
        "candidates": list(row.get("candidats") or ()),
        "holder": row.get("host") if held else None,
        "owner": row.get("lease_owner"),
        "epoch": row.get("lease_epoch"),
        "expires_ts": expires,
        "left_s": None if left is None else round(left, 3),
        "stopped": stopped,
        "held": held,
    }
    if epoch is not None:
        report["expected_epoch"] = int(epoch)
        if not held:
            report["code"] = "no-live-lease"
        elif row.get("lease_epoch") is None \
                or int(row["lease_epoch"]) != int(epoch):
            report["code"] = "stale-epoch"
        else:
            report["code"] = ""
    return report


def live(db, name: str, *, epoch: int | None = None) -> dict:
    """The observable lease of ``name``, without taking any lock.

    ``held`` is true only for a live lease of a non-stopped role. The
    ``epoch`` argument is an optional fencing token: when given, the report is
    refused as ``stale-epoch`` if it does not match. A role with no row raises
    :class:`RoleError`; a database error propagates (the caller must not
    conclude that a role is free).
    """
    row = _read(db, name)
    if row is None:
        raise RoleError("unknown role (no agent_registry row): %s" % name)
    return _report(row, epoch=epoch)


def holder(db, name: str, host: str, *, epoch: int | None = None) -> bool:
    """Does ``host`` hold a live lease of ``name`` (and ``epoch`` when given)?

    Never raises on a missing row or an unknown role: a train passage or a
    timer treats every unreadable role as "not the holder". A database error
    still propagates — the caller must not mistake a broken database for a
    free role.
    """
    if not host:
        return False
    try:
        report = live(db, name, epoch=epoch)
    except RoleError:
        return False
    return bool(report["held"] and report["host"] == host
                and not report.get("code"))


def _detail(code: str, report: dict) -> str:
    if code == "no-live-lease":
        return "no live lease (owner %s, expires %s)" % (
            report.get("owner") or "none", report.get("expires_ts"))
    if code == "stale-epoch":
        return "current epoch %s, expected %s" % (
            report.get("epoch"), report.get("expected_epoch"))
    if code == "role-stopped":
        return "role is stopped"
    if code == "lease-too-short":
        return "%s left" % report.get("left_s")
    return code


def _record(report: dict, *, host: str, argv: list[str] | None = None,
            result: str, code: str = "", detail: str = "") -> dict:
    out = dict(report)
    out.update(result=result, code=code, detail=detail, this_host=host)
    if argv is not None:
        out["command"] = list(argv)
    return out


def run(cfg, args) -> int:
    """The `ameesh role run ROLE -- command` entry point."""
    name = args.role
    host = (getattr(cfg, "host", None) or "").strip()
    argv = list(args.command)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print("ameesh role run: a command is required after `--`", file=sys.stderr)
        return 2
    if not host:
        print("ameesh role run: this host has no name (AMEESH_HOST or config host)",
              file=sys.stderr)
        return 2

    def emit(report, *, result, stream=sys.stdout):
        if args.json:
            print(json.dumps(_record(report, host=host, argv=argv, result=result,
                                     code=report.get("code") or "",
                                     detail=report.get("detail") or ""),
                             ensure_ascii=False, sort_keys=True), file=stream)
        else:
            print("role %s on %s: %s%s" % (
                report.get("role") or name, host, result,
                " (%s)" % (report.get("detail") or report.get("code"))
                if (report.get("detail") or report.get("code")) else ""), file=stream)

    try:
        db = db_mod.connect(cfg)
    except db_mod.DbError as exc:
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 1
    try:
        try:
            report = live(db, name)
        except RoleError as exc:
            print("ameesh role run: %s" % exc, file=sys.stderr)
            return 2
        if report["stopped"]:
            report["code"] = "role-stopped"
        elif not report["held"]:
            report["code"] = "no-live-lease"
        elif report["host"] != host:
            report["code"] = "other-host"
        elif args.min_left and (report.get("left_s") is None
                                or float(report["left_s"]) < float(args.min_left)):
            report["code"] = "lease-too-short"
        if report.get("code"):
            report["detail"] = _detail(report["code"], report)
            emit(report, result="standby")
            return 0
        if args.dry_run:
            emit(report, result="would-run")
            return 0
        env = os.environ.copy()
        # The timer is not the role agent's turn: an inherited identity must
        # not let the command act (or renew a lease) as that agent. Only the
        # epoch we pass is a fencing token; the runner credentials are gone.
        for key in ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                    "AMEESH_TURN_ID", "AMEESH_LEASE_EPOCH", "AMEESH_LEASE_OWNER",
                    "AMEESH_LEASE_LEFT", "AMEESH_ROLE", "AMEESH_ROLE_HOST"):
            env.pop(key, None)
        env.update({
            # a child loads the same host, even when its AMEESH_HOST was unset
            "AMEESH_HOST": host,
            ENV_ROLE: report["role"],
            ENV_HOST: host,
            ENV_EPOCH: str(report.get("epoch") or ""),
            ENV_LEFT: str(int(report.get("left_s") or 0)),
            ENV_OWNER: str(report.get("owner") or ""),
        })
        try:
            proc = subprocess.run(argv, env=env, check=False)
        except FileNotFoundError as exc:
            print("ameesh role run: command not found: %s" % exc.filename,
                  file=sys.stderr)
            return 127
        except OSError as exc:
            print("ameesh role run: cannot start the command: %s" % exc,
                  file=sys.stderr)
            return 126
        # The exit code of the command is the wrapper's. A report is printed
        # only on stderr in JSON mode, so a command that speaks JSON keeps its
        # stdout intact.
        if args.json:
            print(json.dumps(_record(report, host=host, argv=argv, result="ran",
                                     code="", detail=""),
                             ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(proc.returncode)
    except db_mod.DbError as exc:
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 1
    finally:
        try:
            db.close()
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh role run",
        description="run a mechanical command only on the host that holds the "
                    "role's live lease (L165/EP5)",
    )
    parser.add_argument("role", help="agent name of the role (aliases accepted)")
    parser.add_argument("--min-left", type=float, default=0.0,
                        help="refuse to start below S seconds of lease left "
                             "(default 0)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the decision; run nothing")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per decision (schema ameesh-role-run/1)")
    return parser


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    """Options before the first ``--``, the command after it.

    The command is an argument vector: it may carry its own flags, so the
    wrapper stops parsing options at the separator (``--only`` is for
    ``ameesh notify``, never for ``role run``).
    """
    if "--" not in argv:
        return list(argv), []
    index = argv.index("--")
    return list(argv[:index]), list(argv[index + 1:])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    head, command = _split(argv)
    args = build_parser().parse_args(head)
    args.command = command
    try:
        cfg = config_mod.load()
    except Exception as exc:  # configuration illisible : rien ne tourne
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 1
    try:
        return run(cfg, args)
    except RoleError as exc:
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 2
    except db_mod.DbError as exc:
        print("ameesh role run: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
