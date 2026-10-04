# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh action | decisions — actions sous porte (spec §7) et file des décisions (C10).

  ameesh action propose --connector C --operation O --project P --target T
                        [--args JSON | --args-file F] [--class C] [--amount N --currency EUR]
                        [--work-item ID] [--approver human:ID]… [--policy-version V]
                        [--receipt-class C]… [--by agent:ID] [--json]
  ameesh action show <id> [--json]          action, tentatives, journal
  ameesh action list [--state S] [--project P] [--limit N] [--json]
  ameesh action request <id> --approver human:ID [--assume-duplicate] [--ttl S]
        la demande à signer (ameesh-approve, ou authentificateur de test)
  ameesh action approve <id> (--receipt F | --standing) [--by NOM]
  ameesh action execute <id> [--by NOM] [--timeout S]
  ameesh action reconcile <id> [--force] [--by NOM]
  ameesh action retry <id> (--receipt F | --standing) [--by NOM]
  ameesh action replace <id> --receipt F [--by NOM]   (décision qui assume le doublon)
  ameesh action cancel <id> [--note TEXTE] [--by NOM]
  ameesh action recover [--grace S]         lancements interrompus → issue inconnue
  ameesh decisions [--for human:ID] [--json]

Vérification des reçus : `--rp-id`, `--origin` (défauts AMEESH_APPROVE_RP_ID,
AMEESH_APPROVE_ORIGINS), `--allow-facade`, `--level`. Connecteurs :
`shell-noop` écrit sous `--noop-dir` (défaut AMEESH_NOOP_DIR, sinon
<état>/noop) ; `git-merge` appelle `gh` (AMEESH_GH_BIN, AMEESH_BIN_DIR, PATH).

Codes de sortie : 0 succès (exécution confirmée) ; 1 refus ou erreur ;
2 usage ; 3 exécution en échec certain ; 4 issue inconnue.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

from . import actions, config as config_mod, connectors, db as db_mod, jcs, receipts
from .config import Config
from .db import Db


def _open(cfg: Config) -> Db:
    db = db_mod.connect(cfg)
    db_mod.require_schema(db)
    return db


def _moment(epoch) -> str:
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(epoch)))


def _actor(args: argparse.Namespace) -> str:
    if getattr(args, "by", None):
        return args.by
    name = os.environ.get("AGENT_MAIL_NAME")
    if name:
        return "agent:%s" % name
    return os.environ.get("AMEESH_ACTOR") or "cli:%s" % getpass.getuser()


def _policy(args: argparse.Namespace) -> receipts.Policy:
    base = actions.policy_from_env()
    origins = tuple(getattr(args, "origin", None) or ()) or base.origins
    facades = getattr(args, "allow_facade", None)
    return receipts.Policy(
        rp_id=getattr(args, "rp_id", None) or base.rp_id,
        origins=origins,
        allow_facades=frozenset(facades) if facades else receipts.HUMAN_FACADES,
        level=getattr(args, "level", None) or "standard",
    )


def _connector(cfg: Config, name: str, args: argparse.Namespace):
    options: dict = {}
    if name == "shell-noop":
        options["directory"] = (getattr(args, "noop_dir", None) or os.environ.get("AMEESH_NOOP_DIR")
                                or os.path.join(cfg.state_dir, "noop"))
    if getattr(args, "timeout", None):
        options["timeout"] = float(args.timeout)
    return connectors.get(name, **options)


def _read_receipt(path: str):
    with open(path, "rb") as fh:
        return fh.read()


def _dump(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _line(row: dict) -> str:
    money = "" if row.get("amount") is None else " %s %s" % (row["amount"], row.get("currency"))
    return "%s  %-10s %-12s %-10s %-8s %s%s" % (
        row["action_id"], row["state"], row["connector"][:12], row["operation"][:10],
        row["class"][:8], (row.get("target") or "—")[:40], money)


# --------------------------------------------------------------------------
# action
# --------------------------------------------------------------------------

def cmd_propose(cfg: Config, args: argparse.Namespace) -> int:
    if args.args and args.args_file:
        print("erreur : --args ou --args-file, pas les deux", file=sys.stderr)
        return 2
    raw = args.args
    if args.args_file:
        with open(args.args_file, encoding="utf-8") as fh:
            raw = fh.read()
    try:
        values = jcs.loads(raw) if raw else {}
    except jcs.JcsError as exc:
        print("erreur : --args illisible : %s" % exc, file=sys.stderr)
        return 2
    proposer = args.by
    if not proposer and os.environ.get("AGENT_MAIL_NAME"):
        proposer = "agent:%s" % os.environ["AGENT_MAIL_NAME"]
    if not proposer:
        print("erreur : précisez --by agent:<id> (ou human:<id>)", file=sys.stderr)
        return 2
    connector = _connector(cfg, args.connector, args)
    db = _open(cfg)
    try:
        action = actions.propose(
            db, connector, project=args.project, operation=args.operation, args=values,
            target=args.target, proposed_by=proposer, action_class=args.action_class,
            amount=args.amount, currency=args.currency, work_item=args.work_item,
            approvers=args.approver or (), policy_version=args.policy_version,
            receipt_classes=(set(args.receipt_class) if args.receipt_class
                             else actions.DEFAULT_RECEIPT_CLASSES))
    finally:
        db.close()
    if args.json:
        _dump(action)
        return 0
    print("action %s proposée : %s %s sur %s (classe %s, %s)" % (
        action["action_id"], action["connector"], action["operation"],
        action.get("target") or "—", action["class"],
        "reçu requis" if action["requires_receipt"] else "sans reçu"))
    print("empreinte : %s" % action["digest"])
    return 0


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("erreur : action %s introuvable" % args.id, file=sys.stderr)
            return 1
        action["attempts_detail"] = actions.attempts(db, args.id)
        action["events"] = actions.events(db, args.id)
    finally:
        db.close()
    if args.json:
        _dump(action)
        return 0
    print("action    : %s (%s)" % (action["action_id"], action["state"]))
    print("projet    : %-16s lot : %s   proposée par %s" % (
        action["project"], action.get("work_item") or "—", action["proposed_by"]))
    print("opération : %s %s sur %s" % (action["connector"], action["operation"],
                                        action.get("target") or "—"))
    print("args      : %s" % jcs.dumps(action["args"]))
    print("classe    : %-12s reçu : %-8s déduplication : %s" % (
        action["class"], "requis" if action["requires_receipt"] else "non", action["dedupe"]))
    if action.get("amount") is not None:
        print("montant   : %s %s" % (action["amount"], action.get("currency")))
    print("empreinte : %s" % action["digest"])
    if action.get("approvers"):
        print("habilités : %s" % ", ".join(action["approvers"]))
    if action.get("replaces"):
        print("remplace  : %s (doublon assumé par %s)" % (action["replaces"],
                                                         action.get("replace_approver")))
    if action.get("replaced_by"):
        print("remplacée : par %s" % action["replaced_by"])
    if action.get("auth_kind"):
        print("autorisé  : %s %s" % (action["auth_kind"], action.get("auth_approver") or (
            "grant #%s" % action["auth_grant_id"] if action.get("auth_grant_id") else "")))
    if action.get("last_error"):
        print("erreur    : %s" % action["last_error"])
    print("créée     : %s   maj : %s" % (_moment(action.get("created_ts")),
                                         _moment(action.get("updated_ts"))))
    for attempt in action["attempts_detail"]:
        print("  tentative %d : %-10s %-9s %s %s" % (
            attempt["attempt_no"], attempt["state"], attempt["auth_kind"],
            attempt.get("approver") or ("grant #%s" % attempt["grant_id"]
                                        if attempt.get("grant_id") else ""),
            attempt.get("external_ref") or attempt.get("error") or ""))
    for event in action["events"]:
        print("  %s  %-14s %-16s %s" % (_moment(event.get("created_ts")), event["event"],
                                        (event.get("actor") or "—")[:16], event["note"]))
    return 0


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        rows = actions.list_actions(db, state=args.state, project=args.project, limit=args.limit)
    finally:
        db.close()
    if args.json:
        _dump(rows)
        return 0
    if not rows:
        print("aucune action")
        return 0
    for row in rows:
        print(_line(row))
    return 0


def cmd_request(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
    finally:
        db.close()
    if action is None:
        print("erreur : action %s introuvable" % args.id, file=sys.stderr)
        return 1
    request = actions.approval_request(
        action, args.approver, assume_duplicate=args.assume_duplicate, ttl=args.ttl,
        requested_by=args.requested_by)
    _dump(request)
    return 0


def cmd_approve(cfg: Config, args: argparse.Namespace) -> int:
    if bool(args.receipt) == bool(args.standing):
        print("erreur : --receipt FICHIER ou --standing (exactement un)", file=sys.stderr)
        return 2
    receipt = _read_receipt(args.receipt) if args.receipt else None
    db = _open(cfg)
    try:
        if args.command_name == "retry":
            action = actions.retry(db, args.id, receipt, standing=args.standing,
                                   policy=_policy(args), by=_actor(args))
        else:
            action = actions.approve(db, args.id, receipt, standing=args.standing,
                                     policy=_policy(args), by=_actor(args))
    finally:
        db.close()
    if args.json:
        _dump(action)
        return 0
    print("action %s : %s (%s)" % (action["action_id"], action["state"],
                                   action.get("last_note") or ""))
    return 0


def cmd_execute(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("erreur : action %s introuvable" % args.id, file=sys.stderr)
            return 1
        connector = _connector(cfg, action["connector"], args)
        result = actions.execute(db, args.id, connector, policy=_policy(args), by=_actor(args))
    finally:
        db.close()
    if args.json:
        _dump(result)
    else:
        print("action %s, tentative %d : %s%s%s" % (
            result["action_id"], result["attempt"], result["state"],
            " (%s)" % result["external_ref"] if result.get("external_ref") else "",
            " — %s" % result["detail"] if result.get("detail") else ""))
    return {"confirmed": 0, "failed": 3}.get(result["state"], 4)


def cmd_reconcile(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("erreur : action %s introuvable" % args.id, file=sys.stderr)
            return 1
        connector = _connector(cfg, action["connector"], args)
        result = actions.reconcile(db, args.id, connector, by=_actor(args), force=args.force)
    finally:
        db.close()
    if args.json:
        _dump(result)
    elif result["found"]:
        print("action %s réconciliée : %s%s" % (
            result["action_id"], result["state"],
            " (%s)" % result["external_ref"] if result.get("external_ref") else ""))
    else:
        print("action %s : issue introuvable (%s) — décision humaine requise "
              "(ameesh decisions)" % (result["action_id"], result["detail"]))
    return {"confirmed": 0, "failed": 3}.get(result["state"], 4)


def cmd_replace(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.replace(db, args.id, _read_receipt(args.receipt),
                                 policy=_policy(args), by=_actor(args))
    finally:
        db.close()
    if args.json:
        _dump(action)
        return 0
    print("action %s créée en remplacement de %s (doublon assumé par %s) : à approuver" % (
        action["action_id"], action["replaces"], action.get("replace_approver")))
    return 0


def cmd_cancel(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.cancel(db, args.id, by=_actor(args), note=args.note or "")
    finally:
        db.close()
    print("action %s annulée" % action["action_id"])
    return 0


def cmd_recover(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        moved = actions.recover(db, grace=args.grace, by=_actor(args))
    finally:
        db.close()
    for action_id in moved:
        print("action %s : lancement interrompu → issue inconnue" % action_id)
    if not moved:
        print("aucun lancement interrompu")
    return 0


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------

def cmd_decisions(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        queue = actions.decisions(db, human=args.for_human)
    finally:
        db.close()
    if args.json:
        _dump(queue)
        return 0
    total = sum(len(queue[key]) for key in ("approvals", "unknown", "interrupted", "waiting_human"))
    who = " pour %s" % args.for_human if args.for_human else ""
    if not total:
        print("aucune décision en attente%s" % who)
        return 0
    print("%d décision(s) en attente%s" % (total, who))
    if queue["approvals"]:
        print("\nactions à approuver :")
        for row in queue["approvals"]:
            print("  " + _line(row))
            print("      empreinte %s — proposée par %s" % (row["digest"], row["proposed_by"]))
    if queue["unknown"] or queue["interrupted"]:
        print("\nissues inconnues à trancher :")
        for row in queue["unknown"] + queue["interrupted"]:
            print("  " + _line(row))
            print("      %s" % row["hint"])
    if queue["waiting_human"]:
        print("\nlots en attente d'un humain :")
        for item in queue["waiting_human"]:
            print("  #%-5d %-12s %s" % (item["id"], (item.get("assignee") or "—")[:12],
                                        item["title"][:70]))
    return 0


# --------------------------------------------------------------------------
# analyse des arguments
# --------------------------------------------------------------------------

def _verify_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rp-id", default=None)
    parser.add_argument("--origin", action="append", default=[],
                        help="origine WebAuthn autorisée (répétable)")
    parser.add_argument("--allow-facade", action="append", default=[],
                        choices=list(receipts.FACADES))
    parser.add_argument("--level", choices=list(receipts.LEVELS), default="standard")


def _connector_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--noop-dir", default=None, help="dossier du connecteur shell-noop")
    parser.add_argument("--timeout", type=float, default=None, help="délai de l'appel (s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_action = sub.add_parser("action", help="actions sous porte")
    act = p_action.add_subparsers(dest="action_command")

    p = act.add_parser("propose")
    p.add_argument("--connector", required=True, choices=list(connectors.NAMES))
    p.add_argument("--operation", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--target", required=True, help="compte ou objet visé")
    p.add_argument("--args", default=None, help="objet JSON")
    p.add_argument("--args-file", default=None)
    p.add_argument("--class", dest="action_class", choices=list(actions.ACTION_CLASSES),
                   default=None, help="peut relever la classe du connecteur, jamais l'abaisser")
    p.add_argument("--amount", type=int, default=None, help="unités mineures")
    p.add_argument("--currency", default=None)
    p.add_argument("--work-item", type=int, default=None)
    p.add_argument("--approver", action="append", default=[])
    p.add_argument("--policy-version", default="1")
    p.add_argument("--receipt-class", action="append", default=[],
                   choices=list(actions.ACTION_CLASSES),
                   help="classes qui exigent un reçu (défaut irreversible, costly)")
    p.add_argument("--by", default=None)
    p.add_argument("--json", action="store_true")
    _connector_options(p)
    p.set_defaults(func=cmd_propose)

    p = act.add_parser("show")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = act.add_parser("list")
    p.add_argument("--state", choices=list(actions.STATES), default=None)
    p.add_argument("--project", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = act.add_parser("request")
    p.add_argument("id")
    p.add_argument("--approver", required=True)
    p.add_argument("--assume-duplicate", action="store_true")
    p.add_argument("--ttl", type=int, default=actions.DEFAULT_REQUEST_TTL)
    p.add_argument("--requested-by", default=None)
    p.set_defaults(func=cmd_request)

    for name in ("approve", "retry"):
        p = act.add_parser(name)
        p.add_argument("id")
        p.add_argument("--receipt", default=None)
        p.add_argument("--standing", action="store_true",
                       help="couverture par une approbation permanente bornée")
        p.add_argument("--by", default=None)
        p.add_argument("--json", action="store_true")
        _verify_options(p)
        p.set_defaults(func=cmd_approve, command_name=name)

    p = act.add_parser("execute")
    p.add_argument("id")
    p.add_argument("--by", default=None)
    p.add_argument("--json", action="store_true")
    _verify_options(p)
    _connector_options(p)
    p.set_defaults(func=cmd_execute)

    p = act.add_parser("reconcile")
    p.add_argument("id")
    p.add_argument("--force", action="store_true",
                   help="lancement interrompu : ne pas attendre l'échéance")
    p.add_argument("--by", default=None)
    p.add_argument("--json", action="store_true")
    _connector_options(p)
    p.set_defaults(func=cmd_reconcile)

    p = act.add_parser("replace")
    p.add_argument("id")
    p.add_argument("--receipt", required=True,
                   help="reçu qui assume le doublon (ameesh action request --assume-duplicate)")
    p.add_argument("--by", default=None)
    p.add_argument("--json", action="store_true")
    _verify_options(p)
    p.set_defaults(func=cmd_replace)

    p = act.add_parser("cancel")
    p.add_argument("id")
    p.add_argument("--note", default="")
    p.add_argument("--by", default=None)
    p.set_defaults(func=cmd_cancel)

    p = act.add_parser("recover")
    p.add_argument("--grace", type=float, default=actions.RECOVER_GRACE)
    p.add_argument("--by", default=None)
    p.set_defaults(func=cmd_recover)

    p_dec = sub.add_parser("decisions", help="file des décisions humaines")
    p_dec.add_argument("--for", dest="for_human", default=None, metavar="human:ID")
    p_dec.add_argument("--json", action="store_true")
    p_dec.set_defaults(func=cmd_decisions)
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
    except actions.ActionError as exc:
        print("refus [%s] : %s" % (exc.code, exc.reason), file=sys.stderr)
        return 1
    except db_mod.SchemaMissing as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("erreur : base injoignable : %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, receipts.ReceiptError, connectors.ConnectorError, ValueError,
            OSError) as exc:
        print("erreur : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
