# SPDX-License-Identifier: AGPL-3.0-only
"""Commande `ameesh turn observe` (#2167, lot #4874).

  ameesh turn observe (--turn <turn-id> | --request <mailbox-id>) [--json]
        la lecture d'un superviseur : états du tour et de la requête, agent,
        bail, comptabilité et `quiescent` (schéma `ameesh-turn-observation/1`).

Le document est celui de `GET /api/v1/turns/{turn}` et
`GET /api/v1/requests/{request}` : le constructeur est partagé
(`ameesh.turn_observation`), cette commande n'y ajoute que le rendu et les
codes de sortie. Session en lecture seule, métadonnées seules.

Sorties : `0` le document ; `3` cible inconnue (rien sur stdout qu'on puisse
prendre pour une observation) ; `2` usage ; `1` base injoignable.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys

#: les formes des routes de `serve` (`_TURN_RE`, `_MAIL_RE`) : un identifiant
#: mal formé est une erreur d'usage, jamais une cible inconnue
_TURN_RE = re.compile(r"^[0-9a-f]{32}$")
_MAIL_RE = re.compile(r"^[0-9]{1,19}$")

#: code de sortie d'une cible inconnue
EXIT_UNKNOWN = 3


def _turn_id(value: str) -> str:
    if not _TURN_RE.match(value):
        raise argparse.ArgumentTypeError("a turn id is 32 hexadecimal characters")
    return value


def _request_id(value: str) -> int:
    if not _MAIL_RE.match(value):
        raise argparse.ArgumentTypeError("a request id is a mailbox id (digits)")
    return int(value)


def _text(document: dict) -> str:
    """Le rendu humain : les états, une ligne par bloc ; `--json` fait contrat."""
    lines = []
    request = document["request"]
    if request is not None:
        lines.append("request   #%s to %s: %s" % (request["id"], request["agent"],
                                                  request["state"]))
    turn = document["turn"]
    lines.append("turn      %s" % ("—" if turn is None
                                   else "%s: %s" % (turn["id"], turn["state"])))
    agent = document["agent"]
    lines.append("agent     %s: %s%s" % (agent["name"], agent["status"],
                                         ", turn in progress"
                                         if agent["turn_in_progress"] else ""))
    lease = document["lease"]
    lines.append("lease     %s" % ("live, %s epoch %s" % (lease["owner"], lease["epoch"])
                                   if lease["live"] else "not live"))
    lines.append("accounting %s" % document["accounting"]["state"])
    lines.append("quiescent %s" % ("yes" if document["quiescent"] else "no"))
    return "\n".join(lines)


def cmd_observe(cfg, args) -> int:
    from . import turn_observation
    from .mesh_cli import _open

    db = _open(dataclasses.replace(cfg, read_only=True))
    try:
        document = turn_observation.observe(db, turn=args.turn, request=args.request)
    except turn_observation.Unknown as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_UNKNOWN
    finally:
        db.close()
    print(json.dumps(document, ensure_ascii=False) if args.json else _text(document))
    return 0


def _cmd_turn(cfg, args) -> int:
    print("usage: ameesh turn observe (--turn ID | --request ID) [--json]",
          file=sys.stderr)
    return 2


def add_parsers(sub) -> None:
    """Branche `turn …` sur le parseur de `mesh_cli`."""
    parser = sub.add_parser("turn", help="#2167: turn observation for supervisors")
    commands = parser.add_subparsers(dest="turn_command")
    observe = commands.add_parser(
        "observe", help="one turn or request: states, accounting, quiescent; read-only")
    target = observe.add_mutually_exclusive_group(required=True)
    target.add_argument("--turn", type=_turn_id, default=None, metavar="ID",
                        help="the turn id")
    target.add_argument("--request", type=_request_id, default=None, metavar="ID",
                        help="the mailbox id of the request")
    observe.add_argument("--json", action="store_true",
                         help="schema ameesh-turn-observation/1")
    observe.set_defaults(func=cmd_observe)
    parser.set_defaults(func=_cmd_turn)
