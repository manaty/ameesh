# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh attach-grant : grant d'attach distant (#7119, avatar D1).

  ameesh attach-grant request <agent> --host H --device-key K [--tenant T]
                      [--approver human:X] [--canon C] [--json]
  ameesh attach-grant who <agent> [--json]
  ameesh attach-grant audit [--agent A] [--limit N] [--json]

`request` demande à ameesh-approve un grant de 5 min, à usage unique, lié à
la clé d'appareil du poste (`--device-key` : credential_id d'un
authentificateur `device-es256` de l'approbateur) ; l'humain le signe sur le
lien rendu, en WebAuthn. Le reçu est consommé par l'hôte de l'agent, jamais
ici. `who` dit qui peut s'attacher (responsible, liste `avatar.attach_authorized`
de la carte) et les entrées écartées ; `audit` lit le journal des grants et
des refus.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import approve_client, attach_grant, canon as canon_mod, config as config_mod
from . import db as db_mod


def _canon(cfg, agent: str):
    for canon in canon_mod.load_configured(cfg):
        if canon.agent(agent) is not None:
            return canon
    return None


def _requester(approver: str) -> str:
    """Qui demande : l'agent lié à la session, sinon l'approbateur lui-même."""
    name = os.environ.get("AGENT_MAIL_NAME")
    return "agent:%s" % name if name else approver


def cmd_request(cfg, args) -> int:
    canon = _canon(cfg, args.agent)
    approver = args.approver
    if approver is None:
        humans, _problems = attach_grant.authorized(canon, args.agent) if canon else ([], [])
        if len(humans) != 1:
            print("attach-grant: --approver human:<id> required (%s)"
                  % (", ".join(humans) or "nobody may attach to %s" % args.agent),
                  file=sys.stderr)
            return 2
        approver = humans[0]
    code, reason = attach_grant.check_authorized(canon, args.agent, approver)
    if code:
        print("attach-grant: refused [%s]: %s" % (code, reason), file=sys.stderr)
        return 1
    try:
        data = approve_client.create_attach_request(
            cfg, agent=args.agent, host=args.host, tenant=args.tenant or "",
            device_key=args.device_key, approver=approver,
            requested_by=_requester(approver),
            canon=args.canon if args.canon is not None else attach_grant.canon_key(canon))
    except approve_client.ApproveClientError as exc:
        print("attach-grant: %s" % exc.message, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, ensure_ascii=False, sort_keys=True))
        return 0
    print("grant %s for %s on %s, device %s, until %s"
          % (data["attach_id"], data["agent"], data["host"], data["device_key"][:16],
             time.strftime("%H:%M:%S", time.localtime(data["exp"]))))
    print("sign on your phone: %s" % data["link"])
    print("receipt: %s" % data["request_id"])
    return 0


def cmd_who(cfg, args) -> int:
    canon = _canon(cfg, args.agent)
    humans, problems = attach_grant.authorized(canon, args.agent)
    if args.json:
        print(json.dumps({"agent": args.agent, "authorized": humans, "problems": problems},
                         ensure_ascii=False, sort_keys=True))
    else:
        print("%s: %s" % (args.agent, ", ".join(humans) or "nobody"))
        for problem in problems:
            print("  - %s" % problem)
    return 0 if humans else 1


def cmd_audit(cfg, args) -> int:
    db = db_mod.open_db(cfg)
    try:
        rows = attach_grant.audit(db, agent=args.agent, limit=args.limit)
    finally:
        db.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    for row in rows:
        print("%s  %-7s %-22s %-22s %-24s %s%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["at_ts"])),
            row["outcome"], row["code"], row["human"] or "—", row["agent"] or "—",
            row["host"] or "—", ("  " + row["reason"]) if row["outcome"] == "refused" else ""))
    if not rows:
        print("no attach grant recorded")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ameesh attach-grant", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_request = sub.add_parser("request", help="ask ameesh-approve for a 5 min grant")
    p_request.add_argument("agent")
    p_request.add_argument("--host", required=True, help="host the agent is pinned on")
    p_request.add_argument("--tenant", default="", help="tenant on a shared host")
    p_request.add_argument("--device-key", required=True,
                           help="credential_id of the station's device-es256 key")
    p_request.add_argument("--approver", default=None, help="human:<id> signing the grant")
    p_request.add_argument("--canon", default=None, help="canon id ('' = default)")
    p_request.add_argument("--json", action="store_true")
    p_who = sub.add_parser("who", help="who may attach to an agent")
    p_who.add_argument("agent")
    p_who.add_argument("--json", action="store_true")
    p_audit = sub.add_parser("audit", help="journal of grants and refusals")
    p_audit.add_argument("--agent", default=None)
    p_audit.add_argument("--limit", type=int, default=50)
    p_audit.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    cfg = config_mod.load()
    return {"request": cmd_request, "who": cmd_who, "audit": cmd_audit}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
