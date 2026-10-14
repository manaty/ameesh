# SPDX-License-Identifier: AGPL-3.0-only
"""Limite d'en-cours (§4 du canon, owner 14/10/2026) : au plus
`work.BUILD_LIMIT` lots en `build` par agent.

  ameesh work wip [--apply] [--limit N] [--json] [--actor A]
  ameesh work return <id> [--reason T] [--actor A]

**Le balayage** (`sweep`, servi par `ameesh work wip`) rend à la file
d'équipe les lots d'un agent au-delà du plafond, en commençant par les MOINS
RÉCEMMENT ACTIFS (`last_activity_ts` : ligne du lot, journal, jalons, actions,
courrier lié). Il est **idempotent** : un second passage ne trouve plus rien,
n'écrit rien et n'envoie pas de courrier. `--apply` écrit ; sans lui, c'est un
`--dry-run` qui montre ce qui serait rendu, sans mutation.

Le balayage est branché sur `ameesh notify` (`--wip-max-returns N`, ou
`AMEESH_WIP_MAX_RETURNS`) mais **coupé par défaut** (0) : l'activation du
balayage MUTATEUR (`--wip-apply`) vient après les portes et le déploiement
(mandat du 14/10, lot #1359), et aucun balayage du mesh actif n'a lieu à ce
stade. Sans `--wip-apply`, un passage de notify est un `--dry-run` (il ne rend
rien) ; le `--dry-run` global de notify le force aussi. Le geste explicite
`ameesh work wip --apply` reste le seul balayage mutant disponible ici.

Le retour est une **restitution CAS** (`work.unassign`) : le lot doit encore
porter l'assigné lu, sinon rien n'est écrit ; le journal du lot dit qui rend,
pourquoi et l'état de l'agent. Branche, tête de branche, gels et avis
(jalons) restent : le lot garde son état `build`, donc un agent libre le
reprend avec sa branche (`unassigned_lots` le sert avant les `intake` de même
priorité). Le courrier part à l'agent ; les lots d'un humain (`human:…`) ne
sont pas touchés, un humain n'est pas un agent.

**L'exception pilote** est explicite et par lot : `ameesh work assign <id>
<agent> --wip-exception "raison"` (humain ou orchestrateur/agent de
conception, `work.is_pilot`) écrit le marqueur `work.WIP_EXCEPTION_MARK` dans
la dernière attribution ; le balayage laisse alors ce lot en place. Une
réassignation postérieure sans marqueur l'efface. Jamais d'exemption
silencieuse de qui a assigné le lot.
"""
from __future__ import annotations

import json
import sys
import time

from . import storage, work
from .db import Db

SENDER = "ameesh"
#: lots rendus au plus par passage (borne le courrier et les écritures)
DEFAULT_MAX_RETURNS = 10
#: lots ouverts lus par le balayage (les plus anciennement actifs d'abord)
OPEN_LIMIT = 1000
#: début de la note de journal d'une restitution (un lot `build` ainsi rendu
#: redevient prenable : `storage.interface.RETURNED_NOTE`)
RETURN_NOTE = work.RETURN_NOTE


def return_message(item: dict, held: int) -> str:
    """Le courrier à l'agent dont un lot retourne à la file."""
    branch = (item.get("branch") or "").strip()
    head = (item.get("branch_head") or "").strip()
    kept = ""
    if branch:
        kept = ("It keeps its branch %s%s and its reviews: a free agent of your team "
                "can take it as it is (ameesh work list --state build).\n"
                % (branch, " (%s)" % head[:12] if head else ""))
    return ("WIP limit (§4): lot #%d returned to your team queue — you held %d lots in "
            "build, at most %d are kept (the least recently active go back first).\n"
            "%s"
            "You can hand a lot back yourself: ameesh work return <id>.\n"
            "If the pilot wants you to keep it, the exception is explicit: ameesh work "
            "assign %d %s --wip-exception \"why\"." % (
                int(item["id"]), int(held), work.BUILD_LIMIT, kept,
                int(item["id"]), item.get("assignee") or "you"))


def _activity(row: dict) -> float:
    return float(row.get("last_activity_ts") or row.get("updated_ts") or 0.0)


def sweep(cfg, db: Db, *, dry_run: bool = False, limit: int = DEFAULT_MAX_RETURNS,
          actor: str = SENDER) -> list[dict]:
    """Un passage du balayage ; rend les lots rendus (ou prévus en `dry_run`) :
    `[{item, agent, message_id, dry_run, activity_ts}]`.

    Les agents sont parcourus dans l'ordre des noms, leurs lots en surplus du
    moins récemment actif au plus récent ; `limit` borne les retours d'un
    passage (0 : aucun). Un lot déjà rendu (ou repris entre-temps) ne compte
    pas."""
    from . import mail

    out: list = []
    if limit is not None and int(limit) <= 0:
        return out
    st = storage.of(db)
    builds: dict = {}
    for row in st.operations.open_lots_activity(OPEN_LIMIT):
        assignee = (row.get("assignee") or "").strip()
        if (row.get("state") != "build" or not assignee
                or assignee.startswith(work.HUMAN_PREFIX)):
            continue                    # un humain n'est pas un agent
        builds.setdefault(assignee, []).append(row)
    for agent in sorted(builds):
        lots = builds[agent]
        if len(lots) <= work.BUILD_LIMIT:
            continue
        excepted = set(st.work.wip_exceptions([int(l["id"]) for l in lots],
                                              work.WIP_EXCEPTION_MARK, work.ASSIGN_NOTES))
        free = [l for l in lots if int(l["id"]) not in excepted]
        excess = len(free) - work.BUILD_LIMIT
        if excess <= 0:
            continue
        free.sort(key=lambda l: (_activity(l), int(l["id"])))
        for row in free:
            if excess <= 0 or (limit is not None and len(out) >= int(limit)):
                break
            item = work.get(db, int(row["id"]))
            if item is None:            # fermé entre la lecture et le retour
                continue
            if dry_run:
                excess -= 1
                out.append({"item": item, "agent": agent, "message_id": None,
                            "dry_run": True, "activity_ts": _activity(row)})
                continue
            note = ("%s by %s (§4: %d lots in build, at most %d kept) — least recently "
                    "active" % (RETURN_NOTE, actor, len(lots), work.BUILD_LIMIT))
            done = st.work.unassign(int(item["id"]), current=agent, note=note, actor=actor)
            if done is None:            # le lot a bougé : rien écrit, on réessaiera
                continue
            excess -= 1
            message_id = mail.send(
                db, SENDER, agent, return_message(done, len(lots)), kind="event",
                work_item_id=str(done["id"]),
                payload={"wip_return": {"lot": int(done["id"]), "agent": agent,
                                        "limit": work.BUILD_LIMIT,
                                        "activity_ts": _activity(row)}})
            out.append({"item": done, "agent": agent, "message_id": message_id,
                        "dry_run": False, "activity_ts": _activity(row)})
    return out


# --------------------------------------------------------------------------
# CLI (`ameesh work wip`, `ameesh work return`, branchée par mesh_cli)
# --------------------------------------------------------------------------

def notify_settings(args) -> dict | None:
    """§4 : réglages du balayage branché sur `ameesh notify` ; None si le
    balayage est coupé (`--wip-max-returns 0`, le défaut : pas de balayage du
    mesh actif à ce stade). `--wip-apply` seul le rend mutant ; sans lui, le
    passage de notify est un dry-run."""
    most = int(getattr(args, "wip_max_returns", 0) or 0)
    if most <= 0:
        return None
    return {"limit": most, "apply": bool(getattr(args, "wip_apply", False))}


def add_parsers(work_sub, func) -> None:
    p = work_sub.add_parser(
        "wip", help="WIP limit (§4 canon): at most %d lots in build per agent, "
                    "surplus returned to the team queue" % work.BUILD_LIMIT)
    p.add_argument("--apply", action="store_true",
                   help="return the lots (default: dry run, nothing written)")
    p.add_argument("--limit", type=int, default=DEFAULT_MAX_RETURNS,
                   help="at most N lots returned per pass (default %d; 0: none)"
                        % DEFAULT_MAX_RETURNS)
    p.add_argument("--json", action="store_true")
    p.add_argument("--actor", default="",
                   help="default: the session's bound identity (AGENT_MAIL_NAME)")
    p.set_defaults(func=func)
    p_ret = work_sub.add_parser(
        "return", help="return a lot to the team queue (§4): assignment removed, "
                       "branch/SHA/reviews kept, a free agent can take it again")
    p_ret.add_argument("id", type=int)
    p_ret.add_argument("--reason", default="",
                       help="why it is handed back (journaled)")
    p_ret.add_argument("--actor", default="",
                       help="default: the session's bound identity; the assignee "
                            "itself, a human, or a pilot (orchestrator/design)")
    p_ret.set_defaults(func=func)


def run(db: Db, args, cfg=None) -> int:
    if getattr(args, "work_command", None) == "return":
        try:
            item = work.return_to_queue(db, args.id, actor=args.actor, cfg=cfg,
                                        reason=args.reason)
        except work.WorkError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        print("lot #%d returned to the team queue (state %s, branch %s)" % (
            item["id"], item["state"], item.get("branch") or "—"))
        print("branch, head and reviews kept; a free agent of the team can take it "
              "(ameesh work list --state build)")
        return 0
    limit = int(getattr(args, "limit", DEFAULT_MAX_RETURNS) or 0)
    rows = sweep(cfg, db, dry_run=not args.apply, limit=limit,
                 actor=args.actor or SENDER)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0
    if not rows:
        print("WIP: no lot beyond %d in build" % work.BUILD_LIMIT)
        return 0
    now = time.time()
    for row in rows:
        item = row["item"]
        age = ""
        if row.get("activity_ts"):
            age = " — last activity %s ago" % work.span(now - float(row["activity_ts"]))
        print("%slot #%d returned to the queue by %s%s" % (
            "[dry-run] " if row["dry_run"] else "", item["id"], row["agent"], age))
    if not args.apply:
        print("dry run: nothing written (--apply returns these lots)")
    return 0
