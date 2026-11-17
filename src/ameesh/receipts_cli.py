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
  ameesh receipt sign <request.json> --key FILE --credential-id ID [--out FILE]
        sign a request with a file key (facade ed25519); the receipt carries
        human authority only under a cited accord (`request.accord`), for an
        operation the canon names on that authenticator (#7631);
  ameesh standing register <receipt.json> [--canon ID] [--by NAME] [--json]
        register a standing approval (grant, spec §8.3) from a `standing`
        receipt; a file-key receipt needs its accord (#7631);
  ameesh standing list [--all] [--json]
        grants with operations, cap, use and expiry; charter frame and accord
        when the receipt carries them (#7631);
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

from . import config as config_mod, db as db_mod, receipts, signing
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
            expected_operation=args.operation,
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
        if verdict.accord:
            print("accord   : %s#%d%s" % (verdict.accord["agent"], verdict.accord["seq"],
                                         " (file key)" if verdict.file_key else ""))
    else:
        print("receipt REFUSED [%s]: %s" % (verdict.code, verdict.reason), file=sys.stderr)
    return 0 if verdict.ok else 1


def cmd_sign(cfg: Config, args: argparse.Namespace) -> int:
    """#7631 : reçu `ed25519` d'une demande, signé par une clé fichier (0600)."""
    with open(args.file, "rb") as fh:
        try:
            request = json.loads(fh.read())
        except ValueError as exc:
            print("error: request: invalid JSON (%s)" % exc, file=sys.stderr)
            return 2
    try:
        receipts.check_request(request)
        seed = signing.read_private(args.key)
    except (receipts.ReceiptError, OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if "accord" not in request:
        print("warning: no accord in the request: the receipt carries no human authority "
              "from an agents' host", file=sys.stderr)
    receipt = {"v": receipts.RECEIPT_VERSION, "request": request, "facade": "ed25519",
               "credential_id": args.credential_id,
               "proof": {"signature": receipts.b64u_encode(
                   signing.sign(seed, receipts.challenge(request)))}}
    text = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def _env_policy(canon: str = "") -> receipts.Policy:
    origins = [o.strip() for o in os.environ.get("AMEESH_APPROVE_ORIGINS", "").split(",")
               if o.strip()]
    return receipts.Policy(rp_id=os.environ.get("AMEESH_APPROVE_RP_ID", ""),
                           origins=tuple(origins), canon=canon or "")


def cmd_standing_register(cfg: Config, args: argparse.Namespace) -> int:
    with open(args.file, "rb") as fh:
        raw = fh.read()
    by = args.by or os.environ.get("AGENT_MAIL_NAME") or "human:%s" % (
        os.environ.get("USER") or "?")
    db = _open(cfg)
    try:
        verdict, grant_id = receipts.register_standing(db, raw, _env_policy(args.canon),
                                                       registered_by=by)
    finally:
        db.close()
    if args.json:
        print(json.dumps(dict(verdict.as_dict(), grant_id=grant_id), ensure_ascii=False,
                         indent=2))
    elif grant_id is not None:
        print("grant #%d registered: %s %s until %s%s" % (
            grant_id, verdict.standing["connector"], ",".join(verdict.standing["operations"]),
            _moment(verdict.standing["until"]),
            ", accord %s#%d" % (verdict.accord["agent"], verdict.accord["seq"])
            if verdict.accord else ""))
    else:
        print("grant REFUSED [%s]: %s" % (verdict.code, verdict.reason), file=sys.stderr)
    return 0 if grant_id is not None else 1


def cmd_standing_list(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        rows = receipts.list_standing(db, include_inactive=args.all)
    finally:
        db.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0
    if not rows:
        print("no %sstanding approval" % ("" if args.all else "active "))
        return 0
    for row in rows:
        cap = ("%s/%s %s" % (row["consumed_amount"], row["max_amount"], row["currency"])
               if row.get("currency") else "no amount")
        charter = row.get("charter") or {}
        accord = row.get("accord") or {}
        print("#%d %s %s %s [%s] %s, %d use(s), until %s%s%s%s" % (
            row["id"], row["approver"], row["connector"], ",".join(row["operations"] or []),
            row["action_class"], cap, int(row.get("uses") or 0), _moment(row["until_ts"]),
            " (revoked by %s)" % row.get("revoked_by") if row.get("revoked_ts") else "",
            " — frame %s@%s" % (charter["frame"], charter["version"]) if charter else "",
            " — accord %s#%s" % (accord.get("agent"), accord.get("seq")) if accord else ""))
    return 0


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
    p_verify.add_argument("--operation", default=None,
                          help="operation of the approved action (#7631: required for a "
                               "file-key receipt under accord)")
    p_verify.add_argument("--consume", action="store_true",
                          help="consume the nonce (single use)")
    p_verify.add_argument("--by", default=None, metavar="NAME")
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_sign = receipt_sub.add_parser("sign", help="sign a request with a file key (ed25519)")
    p_sign.add_argument("file", help="the request (JSON), e.g. `action request --local`")
    p_sign.add_argument("--key", required=True, help="private key file (0600)")
    p_sign.add_argument("--credential-id", required=True,
                        help="credential_id of the key in the canon")
    p_sign.add_argument("--out", default=None, help="write the receipt here (0600)")
    p_sign.set_defaults(func=cmd_sign)

    p_standing = sub.add_parser("standing", help="standing approvals (spec §8.3)")
    standing_sub = p_standing.add_subparsers(dest="standing_command")
    p_reg = standing_sub.add_parser("register", help="register a grant from its receipt")
    p_reg.add_argument("file")
    p_reg.add_argument("--canon", default="", metavar="ID",
                       help="canon of the authenticator (L44; default: the default canon)")
    p_reg.add_argument("--by", default=None, metavar="NAME")
    p_reg.add_argument("--json", action="store_true")
    p_reg.set_defaults(func=cmd_standing_register)
    p_sl = standing_sub.add_parser("list", help="grants and their expiry")
    p_sl.add_argument("--all", action="store_true", help="include revoked and expired ones")
    p_sl.add_argument("--json", action="store_true")
    p_sl.set_defaults(func=cmd_standing_list)

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
