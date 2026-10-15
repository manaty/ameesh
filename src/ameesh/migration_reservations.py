# SPDX-License-Identifier: AGPL-3.0-only
"""L122 target-SHA migration slots, L160g repository probe and legacy import.

Deux usages cohabitent ici : les créneaux de migration réservés par lot
(#711 : slot, cible repo/branche, targetSha, statut) et la sonde de dépôt /
import des réservations Markdown historiques (L160g). L'import exige une
correspondance explicite numéro de migration → lot du registre ; les
étiquettes et numéros d'issue historiques ne sont jamais interprétés comme
des identifiants de lot.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from . import merge_queue, migrations, storage, work
from .canon import GitError
from .db import DbError, is_unique_violation

_NUMBER = re.compile(r"^([0-9]{3,4})_[^/]+\.sql$")
_RESERVED = re.compile(r"\b(?:réservé|réservée|réservation|reserved)\b", re.IGNORECASE)
_INACTIVE = re.compile(r"\b(?:fusionné|fusionnée|fusion|merged|libéré|libérée|released|annulé|annulée|cancelled|canceled)\b", re.IGNORECASE)


def probe(db, project, branches) -> list[dict]:
    """Report SQL files added by a lot without that lot's reservation.

    Existing files in the common base are historical, so are not reported.
    No writes and no fetch; holders are checked in the project's repository.
    An unreadable registry produces a radar error, never a clean report.
    """
    if not project.migrations:
        return []
    from . import radar
    slug = migrations.repo_key(radar.github_slug(project))
    if not slug:
        raise GitError("project %s: migration reservation probe needs a GitHub repository" % project.name)
    found, holders = [], {}
    try:
        registry = storage.of(db).work
        for branch in branches:
            for path in branch.get("added") or ():
                if os.path.dirname(path) != project.migrations.rstrip("/"):
                    continue
                match = _NUMBER.fullmatch(os.path.basename(path))
                if not match:
                    continue
                number = int(match.group(1))
                if number not in holders:
                    holders[number] = registry.migration_holder(number, slug)
                holder = holders[number]
                if holder and int(holder["id"]) == int(branch["work_item"]):
                    continue
                found.append({"work_item": branch["work_item"], "target": branch["target"],
                              "path": path, "number": match.group(1), "repo": slug,
                              "holder": holder["id"] if holder else None})
    except Exception as exc:
        raise GitError("migration reservation registry unreadable: %s" % exc) from exc
    return found


def parse_markdown(text: str) -> list[dict]:
    """Read active rows from a Number | Lot | Who | State Markdown table.

    English and the legacy French statuses are accepted. Unknown statuses
    are refused for numbered rows, rather than silently dropping intentions.
    """
    rows, seen = [], set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("|"):
            continue
        cells = [c.strip().replace("**", "").replace("`", "")
                 for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if not cells or not re.fullmatch(r"[0-9]{1,4}", cells[0]):
            continue
        if len(cells) != 4 or not 1 <= int(cells[0]) <= 9999:
            raise work.WorkError("RESERVATIONS.md line %s: expected number | lot | who | state" % line_number)
        number = int(cells[0])
        if _INACTIVE.search(cells[3]):
            continue
        if not _RESERVED.search(cells[3]):
            raise work.WorkError("RESERVATIONS.md line %s: unknown reservation status %r" % (line_number, cells[3]))
        if number in seen:
            raise work.WorkError("RESERVATIONS.md: duplicate active reservation %s" % number)
        seen.add(number)
        rows.append({"number": number, "label": cells[1], "who": cells[2], "status": cells[3]})
    return rows


def import_reservations(db, rows, mapping: dict, *, repo: str, apply=False,
                        actor="", cfg=None) -> list[dict]:
    """Preview by default; apply a complete, validated mapping atomically.

    Row locks prevent concurrent reservations of the same lot from being
    overwritten. The existing unique index arbitrates repository/number
    collisions. Unlike a new reservation, this records a legacy intention
    whose SQL may already exist in a branch. No SQL migrations are executed.
    """
    slug = migrations.repo_key(repo)
    if not slug or migrations.layout_for(slug, cfg) is None:
        raise work.WorkError("repository %s: migrations layout unknown" % slug)
    if not isinstance(mapping, dict) or any(
            not re.fullmatch(r"[0-9]{1,4}", str(n)) or not isinstance(lot, int)
            or isinstance(lot, bool) for n, lot in mapping.items()):
        raise work.WorkError("mapping must be a JSON object of migration numbers to integer lot ids")
    try:
        mapped = {int(n): int(lot) for n, lot in mapping.items()}
    except (TypeError, ValueError, AttributeError) as exc:
        raise work.WorkError("mapping must be a JSON object of migration numbers to lot ids") from exc
    numbers = {r["number"] for r in rows}
    if len(mapped) != len(mapping) or len(numbers) != len(rows) or set(mapped) - numbers:
        raise work.WorkError("mapping contains inactive or unknown migration numbers, or duplicate rows")
    if any(lot <= 0 for lot in mapped.values()) or len(set(mapped.values())) != len(mapped):
        raise work.WorkError("each reservation needs a distinct positive lot id")

    def preview(conn):
        registry, result = storage.of(conn).work, []
        for row in rows:
            number, lot = row["number"], mapped.get(row["number"])
            state = "unmapped"
            if lot is not None:
                item = work.get(conn, lot)
                if not item or item["state"] in work.MERGED_STATES or item["state"] == "closed":
                    raise work.WorkError("lot #%s is missing or closed" % lot)
                if work.lot_repository(item, cfg) != slug:
                    raise work.WorkError("lot #%s belongs to another repository" % lot)
                current = item.get("migration")
                if current is not None and (current != number or item.get("migration_repo") != slug):
                    raise work.WorkError("lot #%s already has a different reservation" % lot)
                holder = registry.migration_holder(number, slug)
                if holder and int(holder["id"]) != lot:
                    raise work.WorkError("migration %s of %s already held by lot #%s" % (number, slug, holder["id"]))
                state = "existing" if current is not None else "reserve"
            result.append(dict(row, lot=lot, action=state))
        return result

    if not apply:
        return preview(db)
    if numbers - set(mapped):
        raise work.WorkError("apply needs an explicit lot mapping for every active reservation")
    with db.transaction() as tx:
        # Match named and legacy writers: repository before any lot row.
        tx.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                   "current_schema() || ':migration-repo:' || %s, 0))", ("github.com/" + slug,))
        for lot in sorted(mapped.values()):
            tx.query("SELECT id FROM work_items WHERE id = %s FOR UPDATE", (lot,))
        result = preview(tx)
        for row in result:
            if row["action"] == "existing":
                continue
            storage.of(tx).work.reserve_migration(
                row["lot"], slug, row["number"], 0, actor=actor,
                note="legacy RESERVATIONS.md import: migration %(number)s reserved (" + slug + ")")
            row["action"] = "reserved"
        return result

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


def _ancestor(repo, previous, target):
    try:
        result = subprocess.run(['git', '-C', repo, 'merge-base', '--is-ancestor', previous, target],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise work.WorkError('reservation ancestry unavailable; fetch target history') from exc
    if result.returncode not in (0, 1):
        raise work.WorkError('reservation ancestry unavailable; fetch target history')
    return result.returncode == 0


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
    if action == 'reserve' and reservation_ids:
        raise work.WorkError('reservation ids apply only to lifecycle transitions')
    for attempt in range(5):
        try:
            with db.transaction() as tx:
                api = storage.of(tx).migration_reservations
                # Inspect lifecycle and ancestry after serialization, so a
                # competing reserve/landed cannot invalidate the observation.
                api.lock_target(canonical, branch)
                expected, observed, requalified = None, None, None
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
                else:
                    observed = api.for_target(canonical, branch)
                    requalified = {}
                    for row in observed:
                        previous = row['target_sha']
                        if previous and previous != target_sha and _ancestor(checkout, previous, target_sha):
                            requalified[int(row['id'])] = (
                                'landed' if int(row['number']) in numbers else 'released')
                result = api.update(
                    item_id, slots, project, canonical, branch, target_sha, max(numbers or {0}) + 1,
                    action=action, actor=actor, expected=expected,
                    observed=observed, requalified=requalified)
                if isinstance(result, dict):
                    raise work.WorkError(result['error'])
                if action != 'release' and _tip(checkout, branch) != target_sha:
                    raise work.WorkError('target advanced during reservation; requalify')
                return [record(row) for row in result]
        except DbError as exc:
            # Pre-0419 clients lock their lot before the compatibility guard
            # can lock the repo. A deadlock victim rolls back, then retries in
            # the correct order; no duplicate is accepted during rollout.
            conflict = is_unique_violation(exc, 'work_migration_reservations_active_idx')
            if attempt == 4 or not (conflict or exc.sqlstate == '40P01'):
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
