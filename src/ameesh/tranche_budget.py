# SPDX-License-Identifier: AGPL-3.0-only
"""Human-only tranche administration; admission remains an internal runner API."""
from __future__ import annotations

import json
import sys


def add_parser(sub):
    p = sub.add_parser('tranche',help='shared turn envelope (human-set, no implicit cap)')
    commands = p.add_subparsers(dest='tranche_command',required=True)
    show = commands.add_parser('show')
    show.add_argument('tranche_id',nargs='?')
    show.add_argument('--json',action='store_true')
    create = commands.add_parser('create')
    create.add_argument('tranche_id')
    create.add_argument('--project',required=True)
    create.add_argument('--usd',required=True)
    create.add_argument('--starts-at',required=True)
    create.add_argument('--ends-at')
    create.add_argument('--admitted-ref',required=True)
    create.add_argument('--tz',default='Europe/Paris')
    release = commands.add_parser('release')
    release.add_argument('reservation',type=int)
    release.add_argument('--why',required=True)
    for name in ('close','reconcile'):
        action = commands.add_parser(name)
        action.add_argument('tranche_id')
        action.add_argument('--why',required=True)


def command(cfg,db,args):
    from .budget import authorize, BudgetError
    from .storage.postgres import turn_admission as api
    what = args.tranche_command
    try:
        if what == 'show':
            rows = api.tranche_show(db,args.tranche_id)
            if args.json:
                print(json.dumps(rows,default=str,ensure_ascii=False,indent=2))
            else:
                for row in rows:
                    print('%s %s: %s/%s USD%s' % (row['id'],row['state'],row['used_usd'],
                          row['cap_usd'],' (overdraft; human reconciliation required)' if row['overdraft'] else ''))
            return 0
        actor = authorize(cfg,db)
        if what == 'create':
            api.tranche_create(db,args.tranche_id,args.project,args.usd,
                               starts_at=args.starts_at,ends_at=args.ends_at,
                               actor=actor,admitted_ref=args.admitted_ref,tz=args.tz)
        elif what == 'release':
            api.tranche_release(db,args.reservation,actor=actor,why=args.why)
        else:
            api.tranche_close(db,args.tranche_id,actor=actor,why=args.why,reconcile=what == 'reconcile')
        print('tranche %s: %s (by %s)' % (getattr(args,'tranche_id',None) or args.reservation,what,actor))
        return 0
    except (BudgetError, ValueError, KeyError) as exc:
        print(str(exc),file=sys.stderr)
        return 1
