# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh receipt | authenticator — reçus d'approbation (spec §8).

  ameesh receipt verify <fichier.json> [--rp-id R] [--origin O]… [--allow-facade F]…
                        [--level standard|eleve] [--digest sha256:…] [--action-id A]
                        [--kind action|standing|any] [--decision approve|deny|any]
                        [--canon ID] [--consume --by NOM] [--json]
        vérifie un reçu ameesh-receipt/1 contre le registre de confiance (celui
        du canon de l'objet approuvé, --canon ; défaut : le canon par défaut) ; sans
        --consume, le nonce est seulement contrôlé (non consommé) ; --consume
        exige --digest et --action-id ;
  ameesh authenticator list [--approver human:ID] [--all] [--expect-commit SHA]
                            [--canon ID] [--json]
        le registre des authentificateurs (copie de travail du canon).

`--rp-id` et `--origin` ont pour défaut AMEESH_APPROVE_RP_ID et
AMEESH_APPROVE_ORIGINS (origines séparées par des virgules). La façade ed25519
n'est admise que si on la nomme (`--allow-facade ed25519`) : elle ne fait pas
autorité humaine par défaut.
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
    db = db_mod.connect(cfg)
    db_mod.require_schema(db)
    return db


def _moment(epoch) -> str:
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(epoch)))


def cmd_verify(cfg: Config, args: argparse.Namespace) -> int:
    if args.consume and not (args.by and args.digest and args.action_id):
        print("erreur : --consume exige --by NOM, --digest et --action-id "
              "(un reçu se consomme pour une action précise)", file=sys.stderr)
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
        print("reçu VALIDE : %s par %s (%s, niveau %s)" % (
            verdict.decision, verdict.approver, verdict.facade, verdict.level))
        print("objet    : %s" % (verdict.action_id or "grant %s" % json.dumps(
            verdict.standing, ensure_ascii=False)))
        if verdict.digest:
            print("empreinte: %s" % verdict.digest)
        print("échéance : %s   nonce : %s" % (
            _moment(verdict.exp), "consommé" if verdict.consumed else "disponible"))
    else:
        print("reçu REFUSÉ [%s] : %s" % (verdict.code, verdict.reason), file=sys.stderr)
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
        print("aucun authentificateur")
        return 0
    print("%-22s %-13s %-24s %-9s %-17s %-16s %s" % (
        "APPROBATEUR", "FAÇADE", "CREDENTIAL", "NIVEAU", "CLÉ", "ENRÔLÉ", "ÉTAT"))
    for row in rows:
        state = "révoqué %s" % _moment(row.get("revoked_ts")) if row.get("revoked_ts") else "actif"
        if row["off_canon"]:
            state += " — HORS CANON ATTENDU"
        print("%-22s %-13s %-24s %-9s %-17s %-16s %s" % (
            row["approver"][:22], row["facade"], row["credential_id"][:24], row["level"],
            row["key_fingerprint"][:16] + "…", _moment(row.get("enrolled_ts")), state))
        print("%22s canon : %s%s" % ("", row.get("canon_ref") or "—",
                                     " (déclaré par le canon %s)" % row["canon"]
                                     if row.get("canon") else ""))
    return 1 if off else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_receipt = sub.add_parser("receipt", help="reçus d'approbation")
    receipt_sub = p_receipt.add_subparsers(dest="receipt_command")
    p_verify = receipt_sub.add_parser("verify", help="vérifier un reçu ameesh-receipt/1")
    p_verify.add_argument("file")
    p_verify.add_argument("--rp-id", default=None)
    p_verify.add_argument("--origin", action="append", default=[],
                          help="origine autorisée (répétable)")
    p_verify.add_argument("--allow-facade", action="append", default=[],
                          choices=list(receipts.FACADES),
                          help="façade admise (répétable ; défaut webauthn, device-es256)")
    p_verify.add_argument("--level", choices=list(receipts.LEVELS), default="standard")
    p_verify.add_argument("--digest", default=None, help="empreinte attendue (sha256:…)")
    p_verify.add_argument("--action-id", default=None)
    p_verify.add_argument("--kind", choices=["action", "standing", "any"], default="any")
    p_verify.add_argument("--decision", choices=["approve", "deny", "any"], default="approve")
    p_verify.add_argument("--expect-commit", default=None,
                          help="l'authentificateur doit venir de ce commit du canon")
    p_verify.add_argument("--canon", default="", metavar="ID",
                          help="canon de l'objet approuvé (L44) : seuls ses authentificateurs "
                               "sont admis ; défaut : le canon par défaut")
    p_verify.add_argument("--consume", action="store_true",
                          help="consommer le nonce (usage unique)")
    p_verify.add_argument("--by", default=None)
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_auth = sub.add_parser("authenticator", help="registre des authentificateurs")
    auth_sub = p_auth.add_subparsers(dest="authenticator_command")
    p_list = auth_sub.add_parser("list")
    p_list.add_argument("--approver", default=None)
    p_list.add_argument("--all", action="store_true", help="avec les révoqués")
    p_list.add_argument("--expect-commit", default=None,
                        help="signaler les lignes qui ne viennent pas de ce commit")
    p_list.add_argument("--canon", default=None, metavar="ID",
                        help="seulement les authentificateurs de ce canon (L44 ; \"\" : le "
                             "canon par défaut)")
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
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, receipts.ReceiptError, ValueError, OSError) as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
