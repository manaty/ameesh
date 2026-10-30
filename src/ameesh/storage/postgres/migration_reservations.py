# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic target allocation, slot lifecycle and lot journal."""
from __future__ import annotations

from ameesh import work_lifecycle as lifecycle

from ..migration_reservations import MigrationReservations as Contract

_COLUMNS = 'id, work_item_id, slot, number, project, target_repo, target_branch, target_sha, status'


class MigrationReservations(Contract):
    def __init__(self, db):
        self.db = db

    def lock_target(self, repo, branch):
        # Unknown legacy targets span every branch: all writers take the
        # repository lock first, including the SQL compatibility guard.
        self.db.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                        "current_schema() || ':migration-repo:' || %s, 0))", (repo,))
        self.db.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                        "current_schema() || ':migration:' || %s, 0))", (repo + ':' + branch,))

    def for_lot(self, item_id, slot=None):
        where, params = 'work_item_id = %s', (item_id,)
        if slot is not None:
            where += ' AND slot = %s'
            params += (slot,)
        return self.db.query('SELECT ' + _COLUMNS + ' FROM work_migration_reservations WHERE '
                             + where + ' ORDER BY number, slot', params)

    def for_target(self, repo, branch):
        return self.db.query('SELECT ' + _COLUMNS + ' FROM work_migration_reservations'
                             ' WHERE target_repo = %s AND (target_branch = %s OR target_branch IS NULL)'
                             ' AND status = %s ORDER BY id', (repo, branch, 'reserved'))

    @staticmethod
    def _journal(tx, row, actor, action):
        note = 'migration slot %s %s: %s (%s/%s at %s)' % (
            row['slot'], action, row['number'], row['target_repo'],
            row['target_branch'] or 'unknown legacy target', row['target_sha'] or 'legacy')
        tx.execute('INSERT INTO work_item_events(work_item_id, state, note, actor)'
                   " SELECT id, CASE WHEN state = 'delivered' THEN coalesce(to_jsonb(work_items)->>'legacy_state', state) ELSE state END, %s, %s FROM work_items WHERE id = %s",
                   (note, actor, row['work_item_id']))
        tx.execute('UPDATE work_items SET updated_at = now() WHERE id = %s',
                   (row['work_item_id'],))

    def update(self, item_id, slots, project, repo, branch, sha, floor, *, action, actor,
               expected=None, observed=None, requalified=None):
        with self.db.transaction() as tx:
            api = MigrationReservations(tx)
            api.lock_target(repo, branch)
            # Lock affected lots in id order before record edits. Git
            # observations were collected under the same repository lock.
            lots = tx.query('SELECT id, state, app FROM work_items WHERE id = %s OR id IN ('
                            ' SELECT work_item_id FROM work_migration_reservations'
                            ' WHERE target_repo = %s AND (target_branch = %s OR target_branch IS NULL)'
                            ' AND status = %s AND target_sha <> %s AND %s = %s)'
                            ' ORDER BY id FOR UPDATE',
                            (item_id, repo, branch, 'reserved', sha, action, 'reserve'))
            lot = next((row for row in lots if int(row['id']) == item_id), None)
            if lot is None:
                return {'error': 'unknown lot'}
            if (lot['app'] or '') != project:
                return {'error': 'lot project changed; retry'}
            rows = tx.query('SELECT ' + _COLUMNS + ' FROM work_migration_reservations'
                            ' WHERE work_item_id = %s FOR UPDATE', (item_id,))
            current = {row['slot']: row for row in rows if row['slot'] in slots}
            for slot in slots:
                row = current.get(slot)
                if row and (row['target_repo'], row['target_branch']) != (repo, branch):
                    return {'error': 'slot belongs to a different target'}
                if action != 'reserve':
                    if not row or row['target_sha'] is None:
                        return {'error': 'slot not reserved with a target SHA'}
                    if expected is None or expected.get(slot) != row:
                        return {'error': 'reservation changed; retry'}
                    if action == 'release' and row['status'] == 'landed':
                        return {'error': 'landed slot cannot be released'}
                    if action == 'landed' and row['status'] == 'released':
                        return {'error': 'released slot cannot land'}
            if action != 'reserve':
                status = 'released' if action == 'release' else 'landed'
                result = []
                for slot in slots:
                    row = current[slot]
                    if row['status'] != status:
                        row = tx.query('UPDATE work_migration_reservations SET status = %s,'
                                       ' updated_at = now() WHERE id = %s RETURNING ' + _COLUMNS,
                                       (status, row['id']))[0]
                        self._journal(tx, row, actor, status)
                    result.append(row)
                return result
            if lot['state'] in lifecycle.COMPLETED:
                return {'error': 'lot already finished: no reservation'}
            active = api.for_target(repo, branch)
            if observed is None or active != observed:
                return {'error': 'target reservation observations changed; retry'}
            changes = requalified or {}
            if any(status not in ('reserved', 'landed') for status in changes.values()):
                return {'error': 'requalification cannot release a reservation'}
            for row in current.values():
                if row['status'] == 'landed' or changes.get(int(row['id'])) == 'landed':
                    return {'error': 'landed slot cannot reserve again'}
                if (row['status'] == 'reserved' and row['target_sha'] != sha
                        and changes.get(int(row['id'])) != 'reserved'):
                    return {'error': 'target does not descend from slot reservation'}
            replay = [current.get(slot) for slot in slots]
            live = [row for row in replay if row and row['status'] == 'reserved' and row['target_sha'] == sha]
            if live:
                if len(live) != len(slots) or [int(r['number']) for r in live] != list(
                        range(int(live[0]['number']), int(live[0]['number']) + len(slots))):
                    return {'error': 'batch must replay the same contiguous slots in order'}
                return live
            remaining = [int(row['number']) for row in active if changes.get(int(row['id'])) != 'landed']
            number = max(int(floor), max(remaining or [0]) + 1)
            if number + len(slots) - 1 > 9999:
                return {'error': 'migration numbers exhausted (9999)'}
            for row in active:
                status = changes.get(int(row['id']))
                if status == 'landed':
                    changed = tx.query('UPDATE work_migration_reservations SET status = %s, updated_at = now()'
                                       ' WHERE id = %s RETURNING ' + _COLUMNS, (status, row['id']))[0]
                    self._journal(tx, changed, actor, 'landed')
            result = []
            for offset, slot in enumerate(slots):
                row = tx.query('INSERT INTO work_migration_reservations'
                               ' (work_item_id, slot, number, project, target_repo, target_branch, target_sha, status)'
                               ' VALUES (%s, %s, %s, %s, %s, %s, %s, %s)'
                               ' ON CONFLICT (work_item_id, slot) DO UPDATE SET number = excluded.number,'
                               ' project = excluded.project, target_sha = excluded.target_sha,'
                               ' status = excluded.status, updated_at = now() RETURNING ' + _COLUMNS,
                               (item_id, slot, number + offset, project, repo, branch, sha, 'reserved'))[0]
                self._journal(tx, row, actor, 'reserved')
                result.append(row)
            return result
