# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic target allocation, slot lifecycle and lot journal."""
from __future__ import annotations

from ..migration_reservations import MigrationReservations as Contract

_COLUMNS = 'id, work_item_id, slot, number, project, target_repo, target_branch, target_sha, status'


class MigrationReservations(Contract):
    def __init__(self, db):
        self.db = db

    def for_lot(self, item_id, slot=None):
        where, params = 'work_item_id = %s', (item_id,)
        if slot is not None:
            where += ' AND slot = %s'
            params += (slot,)
        return self.db.query('SELECT ' + _COLUMNS + ' FROM work_migration_reservations WHERE '
                             + where + ' ORDER BY number, slot', params)

    @staticmethod
    def _journal(tx, row, actor, action):
        note = 'migration slot %s %s: %s (%s/%s at %s)' % (
            row['slot'], action, row['number'], row['target_repo'],
            row['target_branch'], row['target_sha'] or 'legacy')
        tx.execute('INSERT INTO work_item_events(work_item_id, state, note, actor)'
                   ' SELECT id, state, %s, %s FROM work_items WHERE id = %s',
                   (note, actor, row['work_item_id']))
        tx.execute('UPDATE work_items SET updated_at = now() WHERE id = %s',
                   (row['work_item_id'],))

    def update(self, item_id, slots, project, repo, branch, sha, floor, *, action, actor,
               expected=None):
        with self.db.transaction() as tx:
            tx.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                       "current_schema() || ':migration:' || %s, 0))",
                       (project + ':' + repo + ':' + branch,))
            # Requalification journals invalidations on other lots too.
            # Lock every affected lot in id order before touching records;
            # different target scopes may share lots without a lock cycle.
            lots = tx.query('SELECT id, state, app FROM work_items WHERE id = %s OR id IN ('
                            ' SELECT work_item_id FROM work_migration_reservations'
                            ' WHERE project = %s AND target_repo = %s AND target_branch = %s'
                            ' AND status = %s AND target_sha <> %s AND %s = %s)'
                            ' ORDER BY id FOR UPDATE',
                            (item_id, project, repo, branch, 'reserved', sha, action, 'reserve'))
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
                if row and (row['project'], row['target_repo'], row['target_branch']) != (
                        project, repo, branch):
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
            if lot['state'] in ('merged', 'promoted', 'closed'):
                return {'error': 'lot already finished: no reservation'}
            if any(row['status'] == 'landed' for row in current.values()):
                return {'error': 'landed slot cannot reserve again'}
            live = [current.get(slot) for slot in slots]
            replay = [row for row in live if row and row['status'] == 'reserved'
                      and row['target_sha'] == sha]
            if replay:
                if len(replay) != len(slots) or [int(r['number']) for r in replay] != list(
                        range(int(replay[0]['number']), int(replay[0]['number']) + len(slots))):
                    return {'error': 'batch must replay the same contiguous slots in order'}
                return replay
            # Compute before writing: rejected/exhausted batches have no side effects.
            number = int(tx.query('SELECT greatest(%s, coalesce(max(number), 0) + 1) AS number'
                                  ' FROM work_migration_reservations WHERE project = %s'
                                  ' AND target_repo = %s AND target_branch = %s AND status = %s'
                                  ' AND (target_sha IS NULL OR target_sha = %s)',
                                  (floor, project, repo, branch, 'reserved', sha))[0]['number'])
            if number + len(slots) - 1 > 9999:
                return {'error': 'migration numbers exhausted (9999)'}
            stale = tx.query('UPDATE work_migration_reservations SET status = %s, updated_at = now()'
                             ' WHERE project = %s AND target_repo = %s AND target_branch = %s'
                             ' AND status = %s AND target_sha IS NOT NULL AND target_sha <> %s'
                             ' RETURNING ' + _COLUMNS,
                             ('released', project, repo, branch, 'reserved', sha))
            for row in stale:
                self._journal(tx, row, actor, 'invalidated')
            result = []
            for offset, slot in enumerate(slots):
                row = tx.query('INSERT INTO work_migration_reservations'
                               ' (work_item_id, slot, number, project, target_repo, target_branch, target_sha, status)'
                               ' VALUES (%s, %s, %s, %s, %s, %s, %s, %s)'
                               ' ON CONFLICT (work_item_id, slot) DO UPDATE SET number = excluded.number,'
                               ' target_sha = excluded.target_sha, status = excluded.status, updated_at = now()'
                               ' RETURNING ' + _COLUMNS,
                               (item_id, slot, number + offset, project, repo, branch, sha, 'reserved'))[0]
                self._journal(tx, row, actor, 'reserved')
                result.append(row)
            return result
