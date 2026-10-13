# SPDX-License-Identifier: AGPL-3.0-only
"""L122 target-SHA reservations and compatible work migration CLI extension."""
from __future__ import annotations

import json
import re
import subprocess

from . import merge_queue, migrations, storage, work
from .db import DbError, is_unique_violation

_SLOT = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,127}')
_SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')


def _git(repo, *args):
    try:
        return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise work.WorkError('target git inspection failed: %s' % ' '.join(args[:2])) from exc


def _tip(repo, branch):
    for ref in ('refs/remotes/origin/' + branch, 'refs/heads/' + branch):
        try:
            return _git(repo, 'rev-parse', '--verify', ref + '^{commit}')
        except work.WorkError:
            pass
    raise work.WorkError('target branch is missing; fetch it before reserving')


def _numbers(repo, sha, layout):
    names = _git(repo, 'ls-tree', '-r', '--name-only', sha, '--', layout.directory + '/')
    return {int(match.group(1)) for name in names.splitlines()
            if (match := re.match(r'^(\d+)_', name.rsplit('/', 1)[-1]))}


def record(row):
    return {'lot': int(row['work_item_id']), 'slot': row['slot'], 'id': int(row['id']),
            'number': int(row['number']), 'project': row['project'],
            'target': {'repo': row['target_repo'], 'branch': row['target_branch']},
            'targetSha': row['target_sha'], 'status': row['status']}


def listing(db, item_id, slots=None):
    if work.get(db, item_id) is None:
        raise work.WorkError('lot %s not found' % item_id)
    rows = storage.of(db).migration_reservations.for_lot(item_id)
    if slots:
        rows = [row for slot in slots for row in rows if row['slot'] == slot]
    return [record(row) for row in rows]


def update(db, item_id, slots, *, target_branch, target_sha, repo=None, action='reserve',
           actor='', cfg=None, reservation_ids=None):
    if not slots or len(set(slots)) != len(slots) or any(
            not _SLOT.fullmatch(slot) or slot == 'default' for slot in slots):
        raise work.WorkError('supply distinct symbolic slots; default is reserved for legacy L122')
    if action not in ('reserve', 'release', 'landed'):
        raise work.WorkError('unknown migration reservation action')
    if not target_branch or not target_sha or not _SHA.fullmatch(target_sha):
        raise work.WorkError('target branch and full lowercase target SHA required')
    item = work.get(db, item_id)
    if item is None:
        raise work.WorkError('lot %s not found' % item_id)
    project = item.get('app') or ''
    if not project:
        raise work.WorkError('lot needs a project before reserving migration slots')
    slug = work.lot_repository(item, cfg)
    checkout = work._checkout(slug, repo, cfg)
    layout = migrations.layout_for(slug, cfg)
    if layout is None:
        raise work.WorkError('repository migrations directory unknown')
    branch = merge_queue._remote_branch(target_branch)
    _git(checkout, 'check-ref-format', 'refs/heads/' + branch)
    canonical = merge_queue.source_of(checkout)
    if canonical.split('/', 1)[0].split(':', 1)[0] == 'github.com':
        canonical = canonical.lower()
    # Exact commit and target ref; caller fetches first. Never scan the
    # worktree, package, source branch or any other target for the floor.
    resolved = _git(checkout, 'rev-parse', '--verify', target_sha + '^{commit}')
    if resolved != target_sha:
        raise work.WorkError('target SHA must identify a commit exactly')
    if action != 'release' and _tip(checkout, branch) != target_sha:
        raise work.WorkError('target advanced past targetSha; fetch and requalify')
    numbers = _numbers(checkout, target_sha, layout)
    api = storage.of(db).migration_reservations
    expected = None
    if action != 'reserve':
        rows = api.for_lot(item_id)
        expected = {row['slot']: row for row in rows if row['slot'] in slots}
        if reservation_ids is not None and (len(reservation_ids) != len(slots) or any(
                expected.get(slot, {}).get('id') != identity
                for slot, identity in zip(slots, reservation_ids))):
            raise work.WorkError('reservation id does not match slot')
        for slot in slots:
            row = expected.get(slot)
            if not row or not row['target_sha']:
                raise work.WorkError('slot not reserved with a target SHA')
            if action == 'release' and row['target_sha'] != target_sha:
                raise work.WorkError('release targetSha does not match reservation')
            if action == 'landed':
                _git(checkout, 'merge-base', '--is-ancestor', row['target_sha'], target_sha)
                if int(row['number']) not in numbers:
                    raise work.WorkError('reserved migration number is absent from landing target')
    elif reservation_ids:
        raise work.WorkError('reservation ids apply only to lifecycle transitions')
    for attempt in range(5):
        try:
            with db.transaction() as tx:
                result = storage.of(tx).migration_reservations.update(
                    item_id, slots, project, canonical, branch, target_sha, max(numbers or {0}) + 1,
                    action=action, actor=actor, expected=expected)
                if isinstance(result, dict):
                    raise work.WorkError(result['error'])
                if action != 'release' and _tip(checkout, branch) != target_sha:
                    raise work.WorkError('target advanced during reservation; requalify')
                return [record(row) for row in result]
        except DbError as exc:
            # A legacy client may write its projection without taking the
            # target lock. The shared unique index still wins; recalculate.
            if attempt == 4 or not is_unique_violation(exc, 'work_migration_reservations_active_idx'):
                raise
    raise work.WorkError('reservation contention, retry')


def add_options(parser):
    parser.add_argument('--slot', action='append', help='symbolic migration slot (repeat in allocation order)')
    parser.add_argument('--target-branch', help='target branch (origin/ and refs/heads/ accepted)')
    parser.add_argument('--target-sha', help='full commit SHA observed on the fetched target')
    parser.add_argument('--list', action='store_true', help='read migration slot records without reserving')
    parser.add_argument('--landed', action='store_true', help='record slots landed at the target SHA')
    parser.add_argument('--reservation-id', action='append', type=int,
                        help='expected technical reservation id for each transition slot')


def run(db, args, cfg):
    if args.number is not None or (args.release and args.landed):
        raise work.WorkError('slot allocation cannot use --number or conflicting lifecycle actions')
    if args.list:
        if args.release or args.landed or args.target_branch or args.target_sha or args.reservation_id:
            raise work.WorkError('--list is read only; no lifecycle or target options')
        records = listing(db, args.id, args.slot)
    else:
        records = update(db, args.id, args.slot, target_branch=args.target_branch,
                         target_sha=args.target_sha, repo=args.repo, actor=args.actor, cfg=cfg,
                         action='release' if args.release else 'landed' if args.landed else 'reserve',
                         reservation_ids=args.reservation_id)
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        for row in records:
            print('lot #%s: slot %s migration %s %s (%s/%s at %s)' % (
                row['lot'], row['slot'], row['number'], row['status'], row['target']['repo'],
                row['target']['branch'], row['targetSha'] or 'legacy'))
    return 0
