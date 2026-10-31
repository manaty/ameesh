# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only `ameesh sandbox list|show` (#2202 S5)."""
import dataclasses
import json
import sys

from . import sandbox_observation as observation
from .turn_observe import _turn_id


def _limit(value):
    import argparse
    try:
        result = int(value)
        if 1 <= result <= observation.MAX_LIMIT:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('limit must be between 1 and 1000')


def cmd_read(cfg, args):
    from .mesh_cli import _open
    db = _open(dataclasses.replace(cfg, read_only=True))
    try:
        if args.sandbox_command == 'show':
            document = observation.show(db, args.turn)
            turns = [document]
        else:
            document = observation.listing(db, agent=args.agent, state=args.state,
                                           scope=args.scope, limit=args.limit)
            turns = document['turns']
    except observation.Unknown as exc:
        print(str(exc), file=sys.stderr)
        return 3
    finally:
        db.close()
    if args.json:
        print(json.dumps(document, ensure_ascii=False))
    else:
        for turn in turns:
            print(json.dumps(turn, ensure_ascii=False, indent=2))
    return 0


def add_parsers(sub):
    parser = sub.add_parser('sandbox', help='read sandbox turns, receipts and tranche state')
    commands = parser.add_subparsers(dest='sandbox_command', required=True)
    listing = commands.add_parser('list', help='list sandbox turns (read-only)')
    listing.add_argument('--agent')
    listing.add_argument('--state', choices=observation.STATES)
    listing.add_argument('--scope', help='exact scope digest')
    listing.add_argument('--limit', type=_limit, default=100)
    show = commands.add_parser('show', help='read one sandbox turn')
    show.add_argument('turn', type=_turn_id)
    for command in (listing, show):
        command.add_argument('--json', action='store_true')
        command.set_defaults(func=cmd_read)
