# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh receipt | authenticator — approval receipts (spec §8).

  ameesh receipt verify <file.json> [--rp-id R] [--origin O]… [--allow-facade F]…
                        [--level standard|eleve] [--digest sha256:…] [--action-id A]
                        [--kind action|standing|any] [--decision approve|deny|any]
                        [--canon ID] [--consume --by NAME] [--json]
        verify an ameesh-receipt/1 receipt against the trust registry (the one
        of the approved object's canon, --canon; default: the default canon);
        without --consume, the nonce is only checked (not consumed); --consume
        requires --digest and --action-id;
  ameesh authenticator list [--approver human:ID] [--all] [--expect-commit SHA]
                            [--canon ID] [--json]
        the authenticator registry (working copy of the canon).

`--rp-id` and `--origin` default to AMEESH_APPROVE_RP_ID and
AMEESH_APPROVE_ORIGINS (comma-separated origins). The ed25519 facade is only
accepted when named (`--allow-facade ed25519`): by default it does not carry
human authority.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import config as config_mod, db as db_mod, receipts
from .config import Config
from .db import Db


def _open(cfg: Config) -> Db:
    # L61 : la vérification de schéma part avec la première requête
    return db_mod.open_db(cfg)


def _moment(epoch) -> str:
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(epoch)))


def cmd_verify(cfg: Config, args: argparse.Namespace) -> int:
    if args.consume and not (args.by and args.digest and args.action_id):
        print("error: --consume requires --by NAME, --digest and --action-id "
              "(a receipt is consumed for one specific action)", file=sys.stderr)
        return 2
    with open(args.file, "rb") as fh:
        raw = fh.read()
    origins = args.origin or [o.strip() for o in os.environ.get(
        "AMEESH_APPROVE_ORIGINS", "").split(",") if o.strip()]
    policy = receipts.Policy(
        rp_id=args.rp_id or os.environ.get("AMEESH_APPROVE_RP_ID", ""),
        origins=tuple(origins),
        allow_facades=frozenset(args.allow_facade) if args.allow_facade else receipts.HUMAN_FACADES,
        level=args.level,
        canon_commit=args.expect_commit or "",
        # L44 (0031) : le canon de l'objet approuvé ('' = canon par défaut)
        canon=args.canon or "",
    )
    db = _open(cfg)
    try:
        verdict = receipts.verify_receipt(
            db, raw, policy,
            kind=None if args.kind == "any" else args.kind,
            expected_digest=args.digest, expected_action_id=args.action_id,
            expect_decision=None if args.decision == "any" else args.decision,
            consume_by=args.by if args.consume else None,
        )
    finally:
        db.close()
    if args.json:
        print(json.dumps(verdict.as_dict(), ensure_ascii=False, indent=2))
    elif verdict.ok:
        print("receipt VALID: %s by %s (%s, level %s)" % (
            verdict.decision, verdict.approver, verdict.facade, verdict.level))
        print("object   : %s" % (verdict.action_id or "grant %s" % json.dumps(
            verdict.standing, ensure_ascii=False)))
        if verdict.digest:
            print("digest   : %s" % verdict.digest)
        print("expires  : %s   nonce: %s" % (
            _moment(verdict.exp), "consumed" if verdict.consumed else "available"))
    else:
        print("receipt REFUSED [%s]: %s" % (verdict.code, verdict.reason), file=sys.stderr)
    return 0 if verdict.ok else 1


def cmd_authenticator_list(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        rows = receipts.list_authenticators(
            db, approver=args.approver, include_revoked=args.all,
            canon=None if args.canon is None else args.canon)
        off = set()
        if args.expect_commit:
            off = {row["id"] for row in receipts.mismatched_authenticators(
                db, args.expect_commit, include_revoked=args.all)}
    finally:
        db.close()
    for row in rows:
        row["off_canon"] = row["id"] in off
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 1 if off else 0
    if not rows:
        print("no authenticators")
        return 0
    print("%-22s %-13s %-24s %-9s %-17s %-16s %s" % (
        "APPROVER", "FACADE", "CREDENTIAL", "LEVEL", "KEY", "ENROLLED", "STATE"))
    for row in rows:
        state = "revoked %s" % _moment(row.get("revoked_ts")) if row.get("revoked_ts") else "active"
        if row["off_canon"]:
            state += " — NOT FROM EXPECTED CANON"
        print("%-22s %-13s %-24s %-9s %-17s %-16s %s" % (
            row["approver"][:22], row["facade"], row["credential_id"][:24], row["level"],
            row["key_fingerprint"][:16] + "…", _moment(row.get("enrolled_ts")), state))
        print("%22s canon: %s%s" % ("", row.get("canon_ref") or "—",
                                     " (declared by canon %s)" % row["canon"]
                                     if row.get("canon") else ""))
    return 1 if off else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_receipt = sub.add_parser("receipt", help="approval receipts")
    receipt_sub = p_receipt.add_subparsers(dest="receipt_command")
    p_verify = receipt_sub.add_parser("verify", help="verify an ameesh-receipt/1 receipt")
    p_verify.add_argument("file")
    p_verify.add_argument("--rp-id", default=None)
    p_verify.add_argument("--origin", action="append", default=[],
                          help="allowed origin (repeatable)")
    p_verify.add_argument("--allow-facade", action="append", default=[],
                          choices=list(receipts.FACADES),
                          help="accepted facade (repeatable; default webauthn, device-es256)")
    p_verify.add_argument("--level", choices=list(receipts.LEVELS), default="standard")
    p_verify.add_argument("--digest", default=None, help="expected digest (sha256:…)")
    p_verify.add_argument("--action-id", default=None)
    p_verify.add_argument("--kind", choices=["action", "standing", "any"], default="any")
    p_verify.add_argument("--decision", choices=["approve", "deny", "any"], default="approve")
    p_verify.add_argument("--expect-commit", default=None,
                          help="the authenticator must come from this canon commit")
    p_verify.add_argument("--canon", default="", metavar="ID",
                          help="canon of the approved object (L44): only its authenticators "
                               "are accepted; default: the default canon")
    p_verify.add_argument("--consume", action="store_true",
                          help="consume the nonce (single use)")
    p_verify.add_argument("--by", default=None, metavar="NAME")
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_auth = sub.add_parser("authenticator", help="authenticator registry")
    auth_sub = p_auth.add_subparsers(dest="authenticator_command")
    p_list = auth_sub.add_parser("list")
    p_list.add_argument("--approver", default=None)
    p_list.add_argument("--all", action="store_true", help="include revoked ones")
    p_list.add_argument("--expect-commit", default=None,
                        help="flag rows that do not come from this commit")
    p_list.add_argument("--canon", default=None, metavar="ID",
                        help="only the authenticators of this canon (L44; \"\": the "
                             "default canon)")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_authenticator_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    cfg = config_mod.load()
    try:
        return args.func(cfg, args)
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, receipts.ReceiptError, ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
