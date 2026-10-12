# SPDX-License-Identifier: AGPL-3.0-only
"""L232e: persistent drain/return requests; host effects use the handoff helper."""
import argparse
import json

from . import config, db as db_mod, estimates, storage, work
from .config import NAME_RE


def drain(cfg, db, host, *, duration=None, critical=()):
    if not isinstance(host, str) or not NAME_RE.fullmatch(host):
        raise ValueError("invalid host name")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int)
                                 or not 30 <= duration <= 7 * 86400):
        raise ValueError("drain duration outside 30 seconds..7 days")
    actor = work.corrector(cfg, db)
    return storage.of(db).host_lifecycle.drain(host, actor=actor, duration=duration, critical=critical)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ameesh host")
    commands = parser.add_subparsers(dest="command", required=True)
    withdraw = commands.add_parser("drain", help="close new turns and prepare agents' evacuation")
    withdraw.add_argument("host")
    withdraw.add_argument("--for", dest="duration", help="request return after this duration, e.g. 8h")
    restore = commands.add_parser("undrain", help="reopen the host and request agents' return")
    restore.add_argument("host")
    status = commands.add_parser("status", help="show durable drain and return progress")
    status.add_argument("host", nargs="?")
    args = parser.parse_args(argv)
    cfg = config.load()
    db = db_mod.connect(cfg)
    try:
        api = storage.of(db).host_lifecycle
        if args.command == "drain":
            duration = estimates.parse(args.duration) * 60 if args.duration else None
            result = drain(cfg, db, args.host, duration=duration)
        elif args.command == "undrain":
            result = api.undrain(args.host, actor=work.corrector(cfg, db))
            if result is None:
                raise ValueError("host has no drain request")
        else:
            result = [api.get(args.host)] if args.host else api.listing()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, db_mod.DbError) as exc:
        print("host request refused: " + str(exc))
        return 1
    finally:
        db.close()
