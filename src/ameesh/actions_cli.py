# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh action | decisions — gated actions (spec §7) and the decision queue (C10, L124).

  ameesh action propose --connector C --operation O --project P --target T
                        [--args JSON | --args-file F] [--class C] [--amount N --currency EUR]
                        [--work-item ID] [--approver human:ID]… [--policy-version V]
                        [--receipt-class C]… [--by agent:ID] [--json]
  ameesh action show <id> [--json]          action, attempts, log
  ameesh action list [--state S] [--project P] [--limit N] [--json]
  ameesh action request <id> --approver human:ID [--assume-duplicate] [--requested-by M]
                        [--json]
        files the request with ameesh-approve (POST /requests, service token)
        and prints the link to open on the phone (also written to the thread)
  ameesh action request <id> --approver human:ID --local [--assume-duplicate] [--ttl S]
        the raw request to be signed by a test authenticator or a tool
  ameesh action fetch-receipt <id> [--request-id R] [--wait S] [--out F] [--by NAME]
        fetches the receipt (GET /receipts/<id>), VERIFIES it, then attaches it to the action
        (approve; retry after failed; replace for an assumed duplicate)
  ameesh action approve <id> (--receipt F | --standing) [--by NAME]
  ameesh action execute <id> [--by NAME] [--timeout S]
  ameesh action reconcile <id> [--force] [--by NAME]
  ameesh action retry <id> (--receipt F | --standing) [--by NAME]
  ameesh action replace <id> --receipt F [--by NAME]   (decision that assumes the duplicate)
  ameesh action cancel <id> [--note TEXT] [--by NAME]
  ameesh action recover [--grace S]         interrupted launches → unknown outcome
  ameesh decisions [--for human:ID] [--all] [--json]
        decision requests from agents (L124, `ameesh decide`), actions to
        approve, unknown outcomes, work items waiting for a human

Receipt verification: `--rp-id`, `--origin` (defaults AMEESH_APPROVE_RP_ID,
AMEESH_APPROVE_ORIGINS), `--allow-facade`, `--level`. Connectors:
`shell-noop` writes under `--noop-dir` (default AMEESH_NOOP_DIR, otherwise
<state>/noop); `git-merge` calls `gh` (AMEESH_GH_BIN, AMEESH_BIN_DIR, PATH).

ameesh-approve (client): AMEESH_APPROVE_URL (https, or http on the loopback)
and AMEESH_APPROVE_TOKEN_FILE (service token, file mode 0600); the token is
never printed nor written to the thread.

Exit codes: 0 success (execution confirmed); 1 refusal or error;
2 usage; 3 execution definitely failed; 4 unknown outcome (result of an
execution or a reconciliation); 5 receipt not signed yet (fetch-receipt).
A refusal is written as `refused [code]: reason` on stderr; `execute` on an
action whose outcome is unknown is a state refusal: `[state]`.
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
        print("error: --args or --args-file, not both", file=sys.stderr)
        return 2
    raw = args.args
    if args.args_file:
        with open(args.args_file, encoding="utf-8") as fh:
            raw = fh.read()
    try:
        values = jcs.loads(raw) if raw else {}
    except jcs.JcsError as exc:
        print("error: unreadable --args: %s" % exc, file=sys.stderr)
        return 2
    proposer = args.by
    if not proposer and os.environ.get("AGENT_MAIL_NAME"):
        proposer = "agent:%s" % os.environ["AGENT_MAIL_NAME"]
    if not proposer:
        print("error: specify --by agent:<id> (or human:<id>)", file=sys.stderr)
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
    print("action %s proposed: %s %s on %s (class %s, %s)" % (
        action["action_id"], action["connector"], action["operation"],
        action.get("target") or "—", action["class"],
        "receipt required" if action["requires_receipt"] else "no receipt"))
    print("digest: %s" % action["digest"])
    return 0


def cmd_show(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("error: action %s not found" % args.id, file=sys.stderr)
            return 1
        action["attempts_detail"] = actions.attempts(db, args.id)
        action["events"] = actions.events(db, args.id)
    finally:
        db.close()
    if args.json:
        _dump(action)
        return 0
    print("action    : %s (%s)" % (action["action_id"], action["state"]))
    print("project   : %-16s work item: %s   proposed by %s" % (
        action["project"], action.get("work_item") or "—", action["proposed_by"]))
    print("operation : %s %s on %s" % (action["connector"], action["operation"],
                                        action.get("target") or "—"))
    print("args      : %s" % jcs.dumps(action["args"]))
    print("class     : %-12s receipt: %-8s deduplication: %s" % (
        action["class"], "required" if action["requires_receipt"] else "no", action["dedupe"]))
    if action.get("amount") is not None:
        print("amount    : %s %s" % (action["amount"], action.get("currency")))
    print("digest    : %s" % action["digest"])
    if action.get("approvers"):
        print("approvers : %s" % ", ".join(action["approvers"]))
    if action.get("replaces"):
        print("replaces  : %s (duplicate assumed by %s)" % (action["replaces"],
                                                         action.get("replace_approver")))
    if action.get("replaced_by"):
        print("replaced  : by %s" % action["replaced_by"])
    if action.get("auth_kind"):
        print("authorized: %s %s" % (action["auth_kind"], action.get("auth_approver") or (
            "grant #%s" % action["auth_grant_id"] if action.get("auth_grant_id") else "")))
    if action.get("last_error"):
        print("error     : %s" % action["last_error"])
    print("created   : %s   updated: %s" % (_moment(action.get("created_ts")),
                                         _moment(action.get("updated_ts"))))
    for attempt in action["attempts_detail"]:
        print("  attempt %d: %-10s %-9s %s %s" % (
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
        print("no actions")
        return 0
    for row in rows:
        print(_line(row))
    return 0


def _requester(args: argparse.Namespace, action: dict) -> str:
    """Qui demande : --requested-by, sinon l'agent de la session, sinon le proposant.
    Une session d'agent ne demande qu'en son propre nom."""
    session = os.environ.get("AGENT_MAIL_NAME")
    if session and args.requested_by and args.requested_by != "agent:%s" % session:
        raise actions.ActionError(actions.INVALID, "this session is agent:%s: --requested-by "
                                  "%s is not its identity" % (session, args.requested_by))
    return args.requested_by or ("agent:%s" % session if session else action["proposed_by"])


def request_approval(cfg: Config, db: Db, action: dict, *, approver: str, requested_by: str,
                     assume_duplicate: bool = False) -> dict:
    """Files the approval request of `action` with ameesh-approve and records
    it (action event and thread, with the link); the service's reply.

    Shared by `ameesh action request` and `POST /api/v1/actions/{id}/
    approval-request` (L87). It files a request and returns its link, nothing
    else: the receipt is signed on ameesh-approve's origin and attached by
    `fetch-receipt`. `actions.ActionError` (stored digest inconsistent) and
    `approve_client.ApproveClientError` (service, configuration, mismatch)
    pass through."""
    # l'empreinte que le service fera signer, recalculée ici depuis l'action
    expected = (actions.assume_duplicate_digest(action) if assume_duplicate
                else actions.digest(action))
    if actions.digest(action) != action["digest"]:
        raise actions.ActionError(actions.INVALID, "stored digest of %s is inconsistent: "
                                  "no request filed" % action["action_id"])
    reply = approve_client.create_request(
        cfg, action_id=action["action_id"], approver=approver,
        requested_by=requested_by, assume_duplicate=assume_duplicate)
    mismatch = [key for key, value in (
        ("action_id", action["action_id"]), ("approver", approver),
        ("assume_duplicate", bool(assume_duplicate)), ("digest", expected),
        ("action_digest", action["digest"])) if reply.get(key) != value]
    if mismatch:
        raise approve_client.ApproveClientError(
            "mismatch", "ameesh-approve answered for another request (%s): ignored"
            % ", ".join(mismatch))
    actions.record_approval_request(db, action, reply, by=requested_by)
    return reply


def cmd_request(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("error: action %s not found" % args.id, file=sys.stderr)
            return 1
        requested_by = _requester(args, action)
        if args.local:
            _dump(actions.approval_request(
                action, args.approver, assume_duplicate=args.assume_duplicate, ttl=args.ttl,
                requested_by=requested_by))
            return 0
        reply = request_approval(cfg, db, action, approver=args.approver,
                                 requested_by=requested_by,
                                 assume_duplicate=args.assume_duplicate)
    finally:
        db.close()
    if args.json:
        _dump(reply)
        return 0
    print("request %s filed with ameesh-approve for %s%s" % (
        reply["request_id"], args.approver,
        " (DECISION THAT ASSUMES THE DUPLICATE)" if args.assume_duplicate else ""))
    print("link to open on their phone (single use, until %s):" % _moment(
        reply.get("link_exp")))
    print("  %s" % reply["link"])
    print("signed digest: %s" % reply["digest"])
    print("next: ameesh action fetch-receipt %s" % action["action_id"])
    return 0


def _attach(db: Db, action: dict, raw: bytes, args: argparse.Namespace) -> dict:
    """Vérifie le reçu récupéré (C7) puis l'attache à l'action."""
    policy = _policy(args)
    try:
        parsed = receipts.parse_receipt(raw)
    except receipts.ReceiptError as exc:
        raise actions.ActionError(receipts.FORMAT, "unreadable receipt: %s" % exc) from exc
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
        raise actions.ActionError(verdict.code, "receipt refused: %s" % verdict.reason)
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
            print("error: action %s not found" % args.id, file=sys.stderr)
            return 1
        request_id = args.request_id or actions.last_approval_request(db, args.id)
        if not request_id:
            print("error: no request filed for %s (ameesh action request), or "
                  "--request-id req_…" % args.id, file=sys.stderr)
            return 1
        deadline = time.monotonic() + max(0.0, float(args.wait or 0))
        while True:
            status, payload = approve_client.fetch_receipt(cfg, request_id)
            if status != "pending" or time.monotonic() >= deadline:
                break
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        if status == "pending":
            print("request %s: not signed yet (link valid until %s)" % (
                request_id, _moment(payload.get("link_exp"))))
            return 5
        if status == "expired":
            print("refused [expired]: request %s expired without a receipt — file a new request "
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
    print("receipt of request %s verified and attached: action %s %s (%s)" % (
        request_id, result["action_id"], result["state"], result.get("last_note") or ""))
    if args.out:
        print("receipt saved: %s" % args.out)
    return 0


def cmd_approve(cfg: Config, args: argparse.Namespace) -> int:
    if bool(args.receipt) == bool(args.standing):
        print("error: --receipt FILE or --standing (exactly one)", file=sys.stderr)
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
    print("action %s: %s (%s)" % (action["action_id"], action["state"],
                                   action.get("last_note") or ""))
    return 0


def cmd_execute(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("error: action %s not found" % args.id, file=sys.stderr)
            return 1
        connector = _connector(cfg, action["connector"], args)
        result = actions.execute(db, args.id, connector, policy=_policy(args), by=_actor(args))
    finally:
        db.close()
    if args.json:
        _dump(result)
    else:
        print("action %s, attempt %d: %s%s%s" % (
            result["action_id"], result["attempt"], result["state"],
            " (%s)" % result["external_ref"] if result.get("external_ref") else "",
            " — %s" % result["detail"] if result.get("detail") else ""))
    return {"confirmed": 0, "failed": 3}.get(result["state"], 4)


def cmd_reconcile(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.get(db, args.id)
        if action is None:
            print("error: action %s not found" % args.id, file=sys.stderr)
            return 1
        connector = _connector(cfg, action["connector"], args)
        result = actions.reconcile(db, args.id, connector, by=_actor(args), force=args.force)
    finally:
        db.close()
    if args.json:
        _dump(result)
    elif result["found"]:
        print("action %s reconciled: %s%s" % (
            result["action_id"], result["state"],
            " (%s)" % result["external_ref"] if result.get("external_ref") else ""))
    else:
        print("action %s: outcome not found (%s) — human decision required "
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
    print("action %s created to replace %s (duplicate assumed by %s): to approve" % (
        action["action_id"], action["replaces"], action.get("replace_approver")))
    return 0


def cmd_cancel(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        action = actions.cancel(db, args.id, by=_actor(args), note=args.note or "")
    finally:
        db.close()
    print("action %s cancelled" % action["action_id"])
    return 0


def cmd_recover(cfg: Config, args: argparse.Namespace) -> int:
    db = _open(cfg)
    try:
        moved = actions.recover(db, grace=args.grace, by=_actor(args))
    finally:
        db.close()
    for action_id in moved:
        print("action %s: interrupted launch → unknown outcome" % action_id)
    if not moved:
        print("no interrupted launch")
    return 0


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------

def decisions_view(db, *, human: str | None = None, include_closed: bool = False,
                   now: float | None = None, viewer: str | None = None) -> dict:
    """The object of `ameesh decisions --json` (also `GET /api/v1/decisions`,
    L84): the queue of `human` (all when None); `viewer`, when given, only
    sees the prose decisions addressed to the mesh or to them."""
    from . import decisions as decisions_mod
    now = time.time() if now is None else now
    queue = actions.decisions(db, human=human)
    # L124 : les demandes de décision des agents, tous projets confondus
    requests = decisions_mod.queue(db, human=human, include_closed=include_closed, now=now)
    # L233: decisions written in prose in mail, not filed as requests
    prose = decisions_mod.prose_decisions(db, now)
    if viewer:
        prose = [p for p in prose
                 if p["recipient"] in decisions_mod.PROSE_RECIPIENTS
                 + (viewer, decisions_mod.OWNER_ANY)]
    queue["requests"] = requests["pending"]
    queue["prose"] = prose
    if include_closed:
        queue["requests_closed"] = requests["closed"]
    return queue


def cmd_decisions(cfg: Config, args: argparse.Namespace) -> int:
    from . import decisions as decisions_mod
    from . import notify_gap
    now = time.time()
    # whose mail may be shown: --for, and in the owner's chat (L123) its human
    # by default — a chat never shows excerpts of mail addressed to another
    viewer = args.for_human or (decisions_mod.current_human()
                                if os.environ.get(decisions_mod.CHAT_ENV) else None)
    db = _open(cfg)
    try:
        queue = decisions_view(db, human=args.for_human, include_closed=args.all, now=now,
                               viewer=viewer)
        # #3007 : la file ne prouve pas que l'humain a été joint. Les
        # battements partagés des notificateurs disent si un vivant le couvre ;
        # sinon, le dire ici — c'est là qu'un agent regarde les demandes en
        # attente, et `ameesh notify --test` ne le dit qu'à qui pense à lancer.
        gaps = [] if args.json else notify_gap.gaps(db, queue["requests"], now=now)
    finally:
        db.close()
    if args.json:
        _dump(queue)
        return 0
    # un lot tenu par une demande est montré avec elle, pas deux fois
    held = {r["lot"] for r in queue["requests"]}
    # L233: a lot in waiting_human with no open request has no question for
    # the owner — it is shown apart, and is not counted as a decision
    waiting = [item for item in queue["waiting_human"] if item["id"] not in held]
    total = len(queue["requests"]) + sum(
        len(queue[key]) for key in ("approvals", "unknown", "interrupted"))
    who = " for %s" % args.for_human if args.for_human else ""
    if not total:
        print("no pending decision%s" % who)
    else:
        print("%d pending decision(s)%s" % (total, who))
    for gap in gaps:
        print("warning: %s" % gap["detail"])
    if queue["requests"]:
        print("\nrequests from agents (oldest first):")
        for view in queue["requests"]:
            for line in decisions_mod.format_view(view, now):
                print("  " + line)
    if not total:
        _prose_and_unasked(queue["prose"], waiting, now)
        _closed_requests(queue.get("requests_closed") or [], now)
        return 0
    if queue["approvals"]:
        print("\nactions to approve:")
        for row in queue["approvals"]:
            print("  " + _line(row))
            print("      digest %s — proposed by %s" % (row["digest"], row["proposed_by"]))
    if queue["unknown"] or queue["interrupted"]:
        print("\nunknown outcomes to settle:")
        for row in queue["unknown"] + queue["interrupted"]:
            print("  " + _line(row))
            print("      %s" % row["hint"])
    _prose_and_unasked(queue["prose"], waiting, now)
    _closed_requests(queue.get("requests_closed") or [], now)
    return 0


def _prose_and_unasked(prose: list[dict], waiting: list[dict], now: float) -> None:
    """L233: what is NOT a decision request, and why — decisions written in
    prose in mail, and lots in waiting_human with no question asked."""
    from . import decisions as decisions_mod
    if prose:
        print("\ndecisions written in prose in mail — not requests, nothing to answer here "
              "(offer to have them filed):")
        for row in prose:
            for line in decisions_mod.format_prose(row, now):
                print("  " + line)
    if waiting:
        print("\nlots in waiting_human with no question asked (no open request; their "
              "assignee must ask: ameesh decide ask --lot <id>):")
        for item in waiting:
            print("  #%-5d %-12s %s" % (item["id"], (item.get("assignee") or "—")[:12],
                                        item["title"][:70]))


def _closed_requests(rows: list[dict], now: float) -> None:
    """`--all` : les demandes closes (répondues, retirées ou closes sans
    réponse), les plus récentes d'abord."""
    if not rows:
        return
    from . import decisions as decisions_mod
    print("\nanswered, withdrawn or closed without an answer (most recent first):")
    for view in rows:
        for line in decisions_mod.format_view(view, now):
            print("  " + line)


# --------------------------------------------------------------------------
# analyse des arguments
# --------------------------------------------------------------------------

def _verify_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rp-id", default=None)
    parser.add_argument("--origin", action="append", default=[],
                        help="allowed WebAuthn origin (repeatable)")
    parser.add_argument("--allow-facade", action="append", default=[],
                        choices=list(receipts.FACADES))
    parser.add_argument("--level", choices=list(receipts.LEVELS), default="standard")


def _connector_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--noop-dir", default=None, help="directory of the shell-noop connector")
    parser.add_argument("--timeout", type=float, default=None, help="call timeout (s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ameesh", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_action = sub.add_parser("action", help="gated actions")
    act = p_action.add_subparsers(dest="action_command")

    p = act.add_parser("propose")
    p.add_argument("--connector", required=True, choices=list(connectors.NAMES))
    p.add_argument("--operation", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--target", required=True, help="targeted account or object")
    p.add_argument("--args", default=None, help="JSON object")
    p.add_argument("--args-file", default=None)
    p.add_argument("--class", dest="action_class", choices=list(actions.ACTION_CLASSES),
                   default=None, help="may raise the connector's class, never lower it")
    p.add_argument("--amount", type=int, default=None, help="minor units")
    p.add_argument("--currency", default=None)
    p.add_argument("--work-item", type=int, default=None)
    p.add_argument("--approver", action="append", default=[])
    p.add_argument("--policy-version", default="1")
    p.add_argument("--receipt-class", action="append", default=[],
                   choices=list(actions.ACTION_CLASSES),
                   help="classes that require a receipt (default irreversible, costly)")
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

    p = act.add_parser("request", help="approval request (ameesh-approve)")
    p.add_argument("id")
    p.add_argument("--approver", required=True)
    p.add_argument("--assume-duplicate", action="store_true",
                   help="decision that assumes the duplicate of an action with an unknown outcome")
    p.add_argument("--requested-by", default=None,
                   help="agent:<id> or human:<id> (default: the session, otherwise the proposer)")
    p.add_argument("--local", action="store_true",
                   help="without ameesh-approve: the raw request to sign (tests, tools)")
    p.add_argument("--ttl", type=int, default=actions.DEFAULT_REQUEST_TTL,
                   help="lifetime of the raw request (--local)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_request)

    p = act.add_parser("fetch-receipt", help="fetch, verify and attach the receipt")
    p.add_argument("id")
    p.add_argument("--request-id", default=None,
                   help="req_… (default: the last request filed for the action)")
    p.add_argument("--wait", type=float, default=0.0,
                   help="wait up to S seconds for the signature")
    p.add_argument("--out", default=None, help="also save the receipt (0600)")
    p.add_argument("--by", default=None)
    p.add_argument("--json", action="store_true")
    _verify_options(p)
    p.set_defaults(func=cmd_fetch_receipt)

    for name in ("approve", "retry"):
        p = act.add_parser(name)
        p.add_argument("id")
        p.add_argument("--receipt", default=None)
        p.add_argument("--standing", action="store_true",
                       help="coverage by a bounded standing approval")
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
                   help="interrupted launch: do not wait for the deadline")
    p.add_argument("--by", default=None)
    p.add_argument("--json", action="store_true")
    _connector_options(p)
    p.set_defaults(func=cmd_reconcile)

    p = act.add_parser("replace")
    p.add_argument("id")
    p.add_argument("--receipt", required=True,
                   help="receipt that assumes the duplicate (ameesh action request --assume-duplicate)")
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

    p_dec = sub.add_parser("decisions", help="human decision queue")
    p_dec.add_argument("--for", dest="for_human", default=None, metavar="human:ID")
    p_dec.add_argument("--all", action="store_true",
                       help="also answered, withdrawn or closed without an answer "
                            "(most recent)")
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
        print("refused [%s]: %s" % (exc.code, exc.reason), file=sys.stderr)
        return 1
    except approve_client.ApproveClientError as exc:
        print("refused [%s]: ameesh-approve: %s" % (exc.code, exc.message), file=sys.stderr)
        return 1
    except db_mod.SchemaMissing as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except db_mod.Unavailable as exc:
        print("error: database unreachable: %s" % exc, file=sys.stderr)
        return 1
    except (db_mod.DbError, receipts.ReceiptError, connectors.ConnectorError, ValueError,
            OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
