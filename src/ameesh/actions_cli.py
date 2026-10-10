# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh action | decisions — actions sous porte (spec §7) et file des décisions (C10).

  ameesh action propose --connector C --operation O --project P --target T
                        [--args JSON | --args-file F] [--class C] [--amount N --currency EUR]
                        [--work-item ID] [--approver human:ID]… [--policy-version V]
                        [--receipt-class C]… [--by agent:ID] [--json]
  ameesh action show <id> [--json]          action, tentatives, journal
  ameesh action list [--state S] [--project P] [--limit N] [--json]
  ameesh action request <id> --approver human:ID [--assume-duplicate] [--requested-by M]
                        [--json]
        dépose la demande auprès d'ameesh-approve (POST /requests, jeton de service)
        et affiche le lien à ouvrir sur le téléphone (écrit aussi dans le fil)
  ameesh action request <id> --approver human:ID --local [--assume-duplicate] [--ttl S]
        la demande brute à signer par un authentificateur de test ou un outil
  ameesh action fetch-receipt <id> [--request-id R] [--wait S] [--out F] [--by NOM]
        récupère le reçu (GET /receipts/<id>), le VÉRIFIE, puis l'attache à l'action
        (approve ; retry après failed ; replace pour un doublon assumé)
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

ameesh-approve (client) : AMEESH_APPROVE_URL (https, ou http sur la boucle
locale) et AMEESH_APPROVE_TOKEN_FILE (jeton de service, fichier 0600) ; le
jeton n'est jamais affiché ni écrit dans le fil.

Codes de sortie : 0 succès (exécution confirmée) ; 1 refus ou erreur ;
2 usage ; 3 exécution en échec certain ; 4 issue inconnue (résultat d'une
exécution ou d'une réconciliation) ; 5 reçu pas encore signé (fetch-receipt).
Un refus s'écrit `refus [code] : raison` sur stderr ; `execute` sur une
action dont l'issue est inconnue est un refus d'état : `[state]`.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

from . import actions, approve_client, config as config_mod, connectors, db as db_mod
from . import jcs, receipts
from .config import Config
from .db import Db


def _open(cfg: Config) -> Db:
    # L61 : la vérification de schéma part avec la première requête
    return db_mod.open_db(cfg)


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


def _requester(args: argparse.Namespace, action: dict) -> str:
    """Qui demande : --requested-by, sinon l'agent de la session, sinon le proposant.
    Une session d'agent ne demande qu'en son propre nom."""
    session = os.environ.get("AGENT_MAIL_NAME")
    if session and args.requested_by and args.requested_by != "agent:%s" % session:
        raise actions.ActionError(actions.INVALID, "cette session est agent:%s : --requested-by "
                                  "%s n'est pas son identité" % (session, args.requested_by))
    return args.requested_by or ("agent:%s" % session if session else action["proposed_by"])


def cmd_request(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("erreur : action %s introuvable" % args.id, file=sys.stderr)
            return 1
        requested_by = _requester(args, action)
        if args.local:
            _dump(actions.approval_request(
                action, args.approver, assume_duplicate=args.assume_duplicate, ttl=args.ttl,
                requested_by=requested_by))
            return 0
        # l'empreinte que le service fera signer, recalculée ici depuis l'action
        expected = (actions.assume_duplicate_digest(action) if args.assume_duplicate
                    else actions.digest(action))
        if actions.digest(action) != action["digest"]:
            raise actions.ActionError(actions.INVALID, "empreinte stockée de %s incohérente : "
                                      "aucune demande" % args.id)
        reply = approve_client.create_request(
            cfg, action_id=action["action_id"], approver=args.approver,
            requested_by=requested_by, assume_duplicate=args.assume_duplicate)
        mismatch = [key for key, value in (
            ("action_id", action["action_id"]), ("approver", args.approver),
            ("assume_duplicate", bool(args.assume_duplicate)), ("digest", expected),
            ("action_digest", action["digest"])) if reply.get(key) != value]
        if mismatch:
            raise approve_client.ApproveClientError(
                "mismatch", "ameesh-approve a répondu pour une autre demande (%s) : ignorée"
                % ", ".join(mismatch))
        actions.record_approval_request(db, action, reply, by=requested_by)
    finally:
        db.close()
    if args.json:
        _dump(reply)
        return 0
    print("demande %s déposée auprès d'ameesh-approve pour %s%s" % (
        reply["request_id"], args.approver,
        " (DÉCISION QUI ASSUME LE DOUBLON)" if args.assume_duplicate else ""))
    print("lien à ouvrir sur son téléphone (usage unique, jusqu'à %s) :" % _moment(
        reply.get("link_exp")))
    print("  %s" % reply["link"])
    print("empreinte signée : %s" % reply["digest"])
    print("ensuite : ameesh action fetch-receipt %s" % action["action_id"])
    return 0


def _attach(db: Db, action: dict, raw: bytes, args: argparse.Namespace) -> dict:
    """Vérifie le reçu récupéré (C7) puis l'attache à l'action."""
    policy = _policy(args)
    try:
        parsed = receipts.parse_receipt(raw)
    except receipts.ReceiptError as exc:
        raise actions.ActionError(receipts.FORMAT, "reçu illisible : %s" % exc) from exc
    # l'empreinte attendue est RECALCULÉE depuis l'action : celle de l'action,
    # ou celle de la décision qui assume son doublon (domaine distinct)
    duplicate = parsed.request.get("digest") == actions.assume_duplicate_digest(action)
    expected = actions.assume_duplicate_digest(action) if duplicate else actions.digest(action)
    retry = not duplicate and action["state"] in ("failed", "unknown")
    # un refus signé (deny) n'annule qu'une action à approuver (approve le traite)
    decision = "approve" if duplicate or retry else None
    # L44 (0031) : contre les authentificateurs du canon de l'action
    verdict = receipts.verify_receipt(
        db, raw, actions.policy_for(action, policy), kind="action", expected_digest=expected,
        expected_action_id=action["action_id"], expect_decision=decision, consume_by=None)
    if not verdict.ok:
        raise actions.ActionError(verdict.code, "reçu refusé : %s" % verdict.reason)
    by = _actor(args)
    if duplicate:
        return actions.replace(db, action["action_id"], raw, policy=policy, by=by)
    if retry:
        return actions.retry(db, action["action_id"], raw, policy=policy, by=by)
    return actions.approve(db, action["action_id"], raw, policy=policy, by=by)


def _write_private(path: str, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)             # un fichier déjà là garde sinon ses permissions
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def cmd_fetch_receipt(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("erreur : action %s introuvable" % args.id, file=sys.stderr)
            return 1
        request_id = args.request_id or actions.last_approval_request(db, args.id)
        if not request_id:
            print("erreur : aucune demande déposée pour %s (ameesh action request), ou "
                  "--request-id req_…" % args.id, file=sys.stderr)
            return 1
        deadline = time.monotonic() + max(0.0, float(args.wait or 0))
        while True:
            status, payload = approve_client.fetch_receipt(cfg, request_id)
            if status != "pending" or time.monotonic() >= deadline:
                break
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        if status == "pending":
            print("demande %s : pas encore signée (lien valable jusqu'à %s)" % (
                request_id, _moment(payload.get("link_exp"))))
            return 5
        if status == "expired":
            print("refus [expired] : demande %s échue sans reçu — nouvelle demande "
                  "(ameesh action request)" % request_id, file=sys.stderr)
            return 1
        result = _attach(db, action, payload, args)
        if args.out:
            _write_private(args.out, payload)
    finally:
        db.close()
    if args.json:
        _dump(result)
        return 0
    print("reçu de la demande %s vérifié et attaché : action %s %s (%s)" % (
        request_id, result["action_id"], result["state"], result.get("last_note") or ""))
    if args.out:
        print("reçu enregistré : %s" % args.out)
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

    p = act.add_parser("request", help="demande d'approbation (ameesh-approve)")
    p.add_argument("id")
    p.add_argument("--approver", required=True)
    p.add_argument("--assume-duplicate", action="store_true",
                   help="décision qui assume le doublon d'une action d'issue inconnue")
    p.add_argument("--requested-by", default=None,
                   help="agent:<id> ou human:<id> (défaut : la session, sinon le proposant)")
    p.add_argument("--local", action="store_true",
                   help="sans ameesh-approve : la demande brute à signer (tests, outils)")
    p.add_argument("--ttl", type=int, default=actions.DEFAULT_REQUEST_TTL,
                   help="durée de la demande brute (--local)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_request)

    p = act.add_parser("fetch-receipt", help="récupérer, vérifier et attacher le reçu")
    p.add_argument("id")
    p.add_argument("--request-id", default=None,
                   help="req_… (défaut : la dernière demande déposée pour l'action)")
    p.add_argument("--wait", type=float, default=0.0,
                   help="attendre la signature jusqu'à S secondes")
    p.add_argument("--out", default=None, help="enregistrer aussi le reçu (0600)")
    p.add_argument("--by", default=None)
    p.add_argument("--json", action="store_true")
    _verify_options(p)
    p.set_defaults(func=cmd_fetch_receipt)

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
    except approve_client.ApproveClientError as exc:
        print("refus [%s] : ameesh-approve : %s" % (exc.code, exc.message), file=sys.stderr)
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
