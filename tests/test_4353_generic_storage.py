# SPDX-License-Identifier: AGPL-3.0-only
"""L99 T5: persisted delivery core, lossless stages and transactional rollback."""
from pathlib import Path
import unittest
from unittest import mock

from ameesh import work, work_lifecycle as lifecycle, plan_github, storage, decisions
from ameesh.db import DbError
from .support import PgTestCase

MIGRATIONS = Path(__file__).parents[1] / 'src/ameesh/migrations'
MIGRATION = (MIGRATIONS / '0518_generic_delivery_state.sql').read_text()


class DeliveryDecisionTest(unittest.TestCase):
    def test_aggregate_github_label_keeps_public_merged_name(self):
        for stage in ('merged', 'promoted'):
            with self.subTest(stage=stage):
                self.assertEqual(plan_github.lot_state([
                    dict(state='delivered', workflow_stage=stage, legacy_state=stage)
                ]), ('merged', 'closed'))

    def test_merged_delivery_can_wait_for_a_decision(self):
        item = dict(id=1, state='delivered', legacy_state='merged')
        with mock.patch.object(work, 'move', return_value={'state': 'waiting_human'}) as move:
            self.assertEqual(decisions._hold_lot(None, item, note='decision', actor='test'), 'waiting_human')
        move.assert_called_once_with(None, 1, 'waiting_human', note='decision', actor='test')

    def test_promoted_delivery_is_not_reopened_for_a_decision(self):
        item = dict(id=1, state='delivered', legacy_state='promoted')
        with mock.patch.object(work, 'move') as move, mock.patch.object(work, 'note'):
            self.assertEqual(decisions._hold_lot(None, item, note='decision', actor='test'), 'promoted')
        move.assert_not_called()


class GenericDeliveryTest(PgTestCase):
    def test_phase_readers_and_footprint_journal_preserve_delivery_stage(self):
        from ameesh import process_registry
        for stage in ('merged', 'promoted'):
            with self.subTest(stage=stage):
                lot = self.lot(stage)
                self.db.execute("UPDATE work_items SET state = %s WHERE id = %s", (stage, lot))
                self.assertEqual(process_registry.registry_state(self.db, lot).state, stage)
                storage.of(self.db).work_footprints.replace(
                    lot, 'manual', ['src/example.py'], actor='test')
                event = self.db.query(
                    'SELECT state FROM work_item_events WHERE work_item_id = %s ORDER BY id DESC LIMIT 1',
                    (lot,))[0]
                self.assertEqual(event['state'], stage)

    def lot(self, title='generic delivery'):
        return work.add(self.db, title=title, app='demo')['id']

    def test_old_writers_are_normalized_without_losing_stage(self):
        for stage in ('merged', 'promoted'):
            lot = self.lot(stage)
            self.db.execute('UPDATE work_items SET state = %s WHERE id = %s', (stage, lot))
            row = work.get(self.db, lot)
            self.assertEqual((row['state'], row['legacy_state'], row['workflow_stage']),
                             ('delivered', stage, stage))

    def test_promotion_and_correction_keep_one_delivery_core(self):
        lot = self.lot()
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (lot,))
        promoted = work.move(self.db, lot, 'promoted')
        self.assertEqual((promoted['state'], promoted['legacy_state']),
                         ('delivered', 'promoted'))
        self.assertIsNotNone(promoted['closed_ts'])
        corrected = work.move(self.db, lot, 'merged', correct='wrong promotion')
        self.assertEqual((corrected['state'], corrected['legacy_state']),
                         ('delivered', 'merged'))
        self.assertIsNone(corrected['closed_ts'])
        reopened = work.move(self.db, lot, 'build', correct='wrong merge')
        self.assertEqual(reopened['state'], 'build')
        self.assertIsNone(work.get(self.db, lot)['workflow_stage'])

    def test_promote_cli_uses_the_development_workflow(self):
        lot = self.lot()
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (lot,))
        result = self.mesh('work', 'promote', str(lot))
        self.assertEqual(result.returncode, 0, result.stderr)
        row = work.get(self.db, lot)
        self.assertEqual((row['state'], row['legacy_state']), ('delivered', 'promoted'))

    def test_promoted_cannot_return_without_explicit_correction(self):
        lot = self.lot()
        self.db.execute("UPDATE work_items SET state = 'promoted' WHERE id = %s", (lot,))
        with self.assertRaises(work.WorkError):
            work.move(self.db, lot, 'build')
        with self.assertRaises(work.WorkError):
            work.move(self.db, lot, 'merged')

    def test_invalid_pinned_compatibility_state_is_rejected(self):
        lot = self.lot()
        with self.assertRaises(DbError):
            with self.db.transaction() as tx:
                tx.execute("UPDATE work_items SET state = 'delivered', legacy_state = 'delivered',"
                           " workflow_ref = 'custom@1', workflow_stage = 'integrated' WHERE id = %s", (lot,))

    def test_custom_pinned_stage_is_preserved(self):
        lot = self.lot()
        self.db.execute("UPDATE work_items SET state = 'merged', workflow_ref = 'custom@1',"
                        " workflow_stage = 'integrated', workflow_version = 1 WHERE id = %s", (lot,))
        row = work.get(self.db, lot)
        self.assertEqual((row['state'], row['legacy_state'], row['workflow_stage']),
                         ('delivered', 'merged', 'integrated'))
        with self.assertRaises(work.WorkError):
            work.move(self.db, lot, 'promoted')

    def test_first_merge_records_one_milestone_and_promotion_does_not_duplicate(self):
        lot = self.lot()
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (lot,))
        work.move(self.db, lot, 'promoted')
        work.move(self.db, lot, 'merged', correct='wrong promotion')
        rows = self.db.query("SELECT id FROM work_item_milestones WHERE work_item_id = %s"
                             " AND kind = 'merged'", (lot,))
        self.assertEqual(len(rows), 1)

    def test_sql_reviewer_audit_preserves_phase_and_merge_annulment(self):
        lot = self.lot()
        self.db.execute("UPDATE work_items SET state = 'merged', reviewer = 'reviewer',"
                        " reviewers = ARRAY['reviewer'] WHERE id = %s", (lot,))
        self.db.execute("UPDATE work_items SET assignee = 'reviewer' WHERE id = %s", (lot,))
        events = work.events(self.db, lot)
        self.assertEqual(events[0]['actor'], 'db:reviewers_sync')
        self.assertEqual(events[0]['state'], 'merged')
        work.move(self.db, lot, 'build', correct='wrong merge')
        marks = [m for m in work.milestones(self.db, lot) if m['kind'] == 'merged']
        self.assertTrue(marks[0]['annulled'])

    def test_fingerprint_can_recur_after_generic_delivery(self):
        first = self.lot()
        self.db.execute("UPDATE work_items SET source = 'empreinte:4353', state = 'merged'"
                        " WHERE id = %s", (first,))
        second = self.lot()
        self.db.execute("UPDATE work_items SET source = 'empreinte:4353' WHERE id = %s", (second,))
        with self.assertRaises(DbError):
            with self.db.transaction() as tx:
                tx.execute("UPDATE work_items SET state = 'build' WHERE id = %s", (first,))

    def test_merge_connector_writes_stage_and_core_together(self):
        lot = self.lot()
        result = storage.of(self.db).work.close_merged(
            lot, current="intake", sha="a" * 40, actor="test", note="merge", pr_ref="")
        self.assertEqual((result['state'], result['legacy_state']), ('delivered', 'merged'))

    def test_storage_cas_accepts_core_and_rejects_stale_phase(self):
        lot = self.lot()
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (lot,))
        store = storage.of(self.db).work
        changed = store.move(lot, 'promoted', current='delivered', loops=0, note='promote', actor='test')
        self.assertEqual(changed['legacy_state'], 'promoted')
        self.assertIsNone(store.move(lot, 'build', current='merged', loops=0,
                                     note='stale correction', actor='test'))

    def test_old_filters_select_individual_stages(self):
        ids = {}
        for stage in ('merged', 'promoted'):
            ids[stage] = self.lot(stage)
            self.db.execute('UPDATE work_items SET state = %s WHERE id = %s', (stage, ids[stage]))
        for stage, lot in ids.items():
            self.assertEqual([r['id'] for r in work.list_items(self.db, state=stage)], [lot])
        self.assertEqual({r['id'] for r in work.list_items(self.db, state='delivered')}, set(ids.values()))

    def test_github_projection_is_unchanged(self):
        for stage in ('merged', 'promoted'):
            old = dict(id=17, state=stage, type='bug', priority=1, pr_ref='demo/repo#42')
            new = dict(old, state='delivered', legacy_state=stage)
            self.assertEqual(plan_github.lot_labels(old), plan_github.lot_labels(new))
            self.assertEqual(plan_github.lot_issue_state(old), plan_github.lot_issue_state(new))
            options = dict(repo='demo/repo', public=False, issue_of=lambda _: None)
            self.assertEqual(plan_github._closing(old, **options), plan_github._closing(new, **options))

    def test_operator_rollback_and_forward_restore_current_data(self):
        ids = []
        for stage in ('merged', 'promoted'):
            lot = self.lot(stage)
            ids.append(lot)
            self.db.execute('UPDATE work_items SET state = %s WHERE id = %s', (stage, lot))
        marks = self.db.query('SELECT * FROM work_item_milestones ORDER BY id')
        with self.db.transaction() as tx:
            self.assertEqual(tx.query('SELECT version FROM schema_migrations WHERE version = 518'), [{'version': 518}])
            tx.execute((MIGRATIONS / 'rollback/0518_generic_delivery_state.sql').read_text())
            rows = tx.query('SELECT state, workflow_stage FROM work_items WHERE id IN (%s,%s) ORDER BY id', tuple(ids))
            self.assertEqual(rows, [dict(state='merged', workflow_stage=None), dict(state='promoted', workflow_stage=None)])
            self.assertEqual(tx.query('SELECT * FROM work_item_milestones ORDER BY id'), marks)
            self.assertEqual(tx.query('SELECT version FROM schema_migrations WHERE version = 518'), [])
            tx.execute(MIGRATION)
            rows = tx.query('SELECT state, legacy_state FROM work_items WHERE id IN (%s,%s) ORDER BY id', tuple(ids))
            self.assertEqual(rows, [dict(state='delivered', legacy_state='merged'), dict(state='delivered', legacy_state='promoted')])

    def test_migration_round_trip_preserves_rows_and_milestones(self):
        lot = self.lot()
        self.db.execute("UPDATE work_items SET state = 'promoted' WHERE id = %s", (lot,))
        with self.db.transaction() as tx:
            # Reconstruct the actual pre-switch constraints/functions in the
            # disposable test schema. The outer rollback restores the fixture.
            tx.execute('DROP TRIGGER aa_work_items_generic_state_sync ON work_items')
            tx.execute('ALTER TABLE work_items DROP CONSTRAINT work_items_state_check')
            tx.execute('ALTER TABLE work_items DROP CONSTRAINT work_items_legacy_state_matches')
            tx.execute('ALTER TABLE work_items DROP CONSTRAINT work_items_workflow_stage_needs_ref')
            tx.execute("UPDATE work_items SET state = legacy_state, workflow_stage ="
                       " CASE WHEN workflow_ref IS NULL THEN NULL ELSE workflow_stage END")
            tx.execute('ALTER TABLE work_items DROP COLUMN legacy_state')
            for name in ('0463_work_milestones_effective_merge.sql',
                         '0467_migration_slot_landed_on_close.sql'):
                tx.execute((MIGRATIONS / name).read_text())
            tx.execute("ALTER TABLE work_items ADD CONSTRAINT work_items_state_check"
                       " CHECK (state IN ('intake','build','qa','validated','merged',"
                       "'promoted','blocked','waiting_human','closed'))")
            before = tx.query('SELECT id, state, workflow_ref, workflow_stage FROM work_items ORDER BY id')
            marks = tx.query('SELECT * FROM work_item_milestones ORDER BY id')
            # Explicit SQL savepoints work with both psql and psycopg drivers.
            tx.execute('SAVEPOINT delivery_migration')
            try:
                tx.execute(MIGRATION)
                row = tx.query('SELECT state, legacy_state, workflow_stage FROM work_items WHERE id = %s', (lot,))[0]
                self.assertEqual(row, {'state': 'delivered', 'legacy_state': 'promoted', 'workflow_stage': 'promoted'})
                self.assertEqual(tx.query('SELECT * FROM work_item_milestones ORDER BY id'), marks)
            finally:
                tx.execute('ROLLBACK TO SAVEPOINT delivery_migration')
                tx.execute('RELEASE SAVEPOINT delivery_migration')
            self.assertEqual(tx.query('SELECT id, state, workflow_ref, workflow_stage FROM work_items ORDER BY id'), before)
            tx.execute(MIGRATION)
            self.assertEqual(tx.query('SELECT state FROM work_items WHERE id = %s', (lot,))[0]['state'], 'delivered')


if __name__ == '__main__':
    unittest.main()
