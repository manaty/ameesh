# SPDX-License-Identifier: AGPL-3.0-only
"""#711: target-only floors, atomic slots, legacy coexistence and lifecycle."""
from __future__ import annotations

import json
import os
import threading
import time
from unittest import mock

from ameesh import migration_reservations as reservations, migrations, storage, work
from ameesh.db import DbError

from .support import PgTestCase
from .test_l122_numeros_de_migration import _commit, _depot, _git, NEXLINK


class ScopedReservationsTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.repo = _depot(os.path.join(self.tmp, 'repo'), 'manaty/nexlink', NEXLINK.directory,
                           ['375_base.sql', '376_base.sql'], {'unrelated': ['9000_future.sql']})
        self.sha = reservations._git(self.repo, 'rev-parse', 'HEAD')
        self.a = self.lot('a')
        self.b = self.lot('b')

    def lot(self, name, app='nexlink'):
        if app is None:
            # Historical nullable app, before the current registry contract.
            self.db.execute('ALTER TABLE work_items ALTER COLUMN app DROP NOT NULL')
            def restore():
                self.db.execute("UPDATE work_items SET app = '' WHERE app IS NULL")
                self.db.execute('ALTER TABLE work_items ALTER COLUMN app SET NOT NULL')
            self.addCleanup(restore)
        return work.add(self.db, title=name, issue_ref='manaty/nexlink#1', app=app, actor='mesh-design')

    def reserve(self, lot=None, slots=('schema',), **kw):
        return reservations.update(self.db, (lot or self.a)['id'], list(slots), repo=self.repo,
                                   target_branch=kw.pop('target_branch', 'develop'),
                                   target_sha=kw.pop('target_sha', self.sha), actor='dev', **kw)

    def advance(self, *names):
        _commit(self.repo, NEXLINK.directory, *names)
        self.sha = reservations._git(self.repo, 'rev-parse', 'HEAD')
        return self.sha

    def test_target_only_floor_multi_slot_json_and_idempotence(self):
        first = self.reserve(slots=('schema', 'backfill'))
        self.assertEqual([row['number'] for row in first], [377, 378])
        self.assertEqual(first, self.reserve(slots=('schema', 'backfill')))
        self.assertEqual(set(first[0]), {'lot', 'slot', 'id', 'number', 'project', 'target', 'targetSha', 'status'})
        self.assertEqual(first[0]['target'], {'repo': 'github.com/manaty/nexlink', 'branch': 'develop'})
        self.assertEqual(first[0]['targetSha'], self.sha)
        self.assertEqual(self.reserve(self.b)[0]['number'], 379)
        self.assertEqual(self.reserve(self.lot('other', 'other'))[0]['number'], 380)
        self.assertEqual(work.get(self.db, self.a['id'])['migration'], None)
        self.assertIsInstance(first[0]['id'], int)

    def test_normalized_source_and_branch_share_allocator(self):
        _git(self.repo, 'remote', 'set-url', 'origin', 'https://github.com/MANATY/NEXLINK.git')
        self.assertEqual(self.reserve(target_branch='origin/develop')[0]['number'], 377)
        self.assertEqual(self.reserve(self.b, target_branch='refs/heads/develop')[0]['number'], 378)
        _git(self.repo, 'checkout', '-qb', 'release')
        self.assertEqual(self.reserve(self.b, slots=('release',), target_branch='release')[0]['number'], 377)
        _git(self.repo, 'remote', 'set-url', 'origin', 'ssh://git@github.com:2222/MANATY/NEXLINK.git')
        port = self.reserve(self.lot('port'))[0]
        self.assertEqual(port['target']['repo'], 'github.com:2222/manaty/nexlink')
        self.assertEqual(port['number'], 377)

    def test_legacy_reservations_and_new_slots_use_one_allocator(self):
        work.set_branch(self.db, self.b['id'], 'agent/b', target='origin/develop', actor='dev')
        legacy = work.reserve_migration(self.db, self.b['id'], 400, repo=self.repo, actor='dev')
        self.assertEqual(legacy['migration'], 400)
        self.assertEqual(self.reserve()[0]['number'], 401)
        default = reservations.listing(self.db, self.b['id'])[0]
        self.assertEqual((default['slot'], default['number'], default['target']['branch']), ('default', 400, 'develop'))
        work.reserve_migration(self.db, self.b['id'], release=True)
        self.assertEqual(reservations.listing(self.db, self.b['id'])[0]['status'], 'released')
        # Legacy next-free scans other source branches as before, plus slots.
        high = self.reserve(self.lot('high'), slots=('high',), target_branch='unrelated',
                            target_sha=reservations._git(self.repo, 'rev-parse', 'unrelated'))[0]
        self.assertEqual(high['number'], 9001)
        self.assertEqual(work.reserve_migration(self.db, self.lot('legacy')['id'], repo=self.repo)['migration'], 9002)

    def test_upgrade_backfills_legacy_records_and_replays_additively(self):
        work.set_branch(self.db, self.a['id'], 'agent/a', target='refs/heads/develop', actor='dev')
        work.reserve_migration(self.db, self.a['id'], 400, repo=self.repo)
        work.reserve_migration(self.db, self.b['id'], 401, repo=self.repo)
        work.close(self.db, self.b['id'], abandoned=True, actor='dev')
        # Depuis #956, la clôture libère la réservation legacy : on rétablit
        # l'état d'avant #956 (un lot terminal garde son numéro, le
        # déclencheur lui rend son dépôt) — c'est cet héritage que la
        # migration projette en « released ».
        self.db.execute('UPDATE work_items SET migration = 401 WHERE id = %d' % self.b['id'])
        self.db.execute('TRUNCATE work_migration_reservations RESTART IDENTITY')
        migration = next(m for m in migrations.discover() if m.name == 'migration_reservation_slots')
        self.assertEqual(migrations.non_additive(migration), [])
        self.db.script(migration.sql)
        first = reservations.listing(self.db, self.a['id'])
        self.assertEqual(first[0]['status'], 'reserved')
        self.assertEqual(first[0]['target'], {'repo': 'github.com/manaty/nexlink', 'branch': 'develop'})
        self.assertIsNone(first[0]['targetSha'])
        self.assertEqual(reservations.listing(self.db, self.b['id'])[0]['status'], 'released')
        self.db.script(migration.sql)
        self.assertEqual(reservations.listing(self.db, self.a['id']), first)

    def test_unique_index_blocks_legacy_explicit_collision(self):
        work.set_branch(self.db, self.b['id'], 'agent/b', target='develop', actor='dev')
        self.reserve()
        with self.assertRaises(DbError):
            work.reserve_migration(self.db, self.b['id'], 377, repo=self.repo)
        self.assertIsNone(work.get(self.db, self.b['id'])['migration'])

    def test_target_advance_releases_other_lots_and_requalifies_slots(self):
        old = self.reserve(slots=('schema', 'backfill'))
        self.reserve(self.b)
        self.advance('380_other.sql')
        with self.assertRaisesRegex(work.WorkError, 'advanced'):
            self.reserve(target_sha=old[0]['targetSha'])
        refreshed = self.reserve(slots=('schema', 'backfill'))
        self.assertEqual([r['number'] for r in refreshed], [381, 382])
        self.assertEqual([r['id'] for r in refreshed], [r['id'] for r in old])
        self.assertEqual(reservations.listing(self.db, self.b['id'])[0]['status'], 'released')
        self.assertEqual(self.reserve(self.b)[0]['number'], 383)

    def test_landed_requires_numbers_in_descendant_target_and_keeps_history(self):
        first = self.reserve(slots=('schema', 'backfill'))
        with self.assertRaisesRegex(work.WorkError, 'absent'):
            self.reserve(slots=('schema', 'backfill'), action='landed')
        self.advance('377_schema.sql', '378_backfill.sql')
        landed = self.reserve(slots=('schema', 'backfill'), action='landed',
                              reservation_ids=[r['id'] for r in first])
        self.assertEqual([r['status'] for r in landed], ['landed', 'landed'])
        self.assertEqual(landed, self.reserve(slots=('schema', 'backfill'), action='landed'))
        self.assertEqual(self.reserve(self.b)[0]['number'], 379)
        with self.assertRaisesRegex(work.WorkError, 'landed'):
            self.reserve(slots=('schema', 'backfill'), action='release', target_sha=first[0]['targetSha'])
        with self.assertRaisesRegex(work.WorkError, 'landed'):
            self.reserve(slots=('schema', 'backfill'))

    def test_release_and_rereserve_then_finished_lot(self):
        first = self.reserve()[0]
        with self.assertRaisesRegex(work.WorkError, 'id does not match'):
            self.reserve(action='release', reservation_ids=[first['id'] + 1])
        self.assertEqual(self.reserve(action='release')[0]['status'], 'released')
        self.assertEqual(self.reserve(action='release')[0]['status'], 'released')
        self.assertEqual(self.reserve()[0]['number'], first['number'])
        work.close(self.db, self.a['id'], abandoned=True, note='done', actor='dev')
        with self.assertRaisesRegex(work.WorkError, 'finished'):
            self.reserve(slots=('new',))
        # Explicit cleanup remains possible after closing the lot.
        self.assertEqual(self.reserve(action='release')[0]['status'], 'released')

    def test_batch_rejections_and_exhaustion_are_atomic(self):
        self.reserve(slots=('one',))
        before = reservations.listing(self.db, self.a['id'])
        for slots in [('one', 'two'), ('x', 'x'), ('default',), ('invalid space',)]:
            with self.assertRaises(work.WorkError):
                self.reserve(slots=slots)
            self.assertEqual(reservations.listing(self.db, self.a['id']), before)
        self.advance('9998_end.sql')
        with self.assertRaisesRegex(work.WorkError, 'exhausted'):
            self.reserve(self.b, slots=('one', 'two'))
        self.assertEqual(reservations.listing(self.db, self.a['id']), before)
        self.assertEqual(reservations.listing(self.db, self.b['id']), [])

    def test_batch_database_failure_rolls_back_records_and_journal(self):
        events = len(work.events(self.db, self.a['id']))
        self.db.script("""CREATE FUNCTION reject_second_slot() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.slot = 'fail' THEN RAISE EXCEPTION 'injected failure'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER reject_second BEFORE INSERT ON work_migration_reservations
            FOR EACH ROW EXECUTE FUNCTION reject_second_slot();""")
        with self.assertRaises(DbError):
            self.reserve(slots=('ok', 'fail'))
        self.assertEqual(reservations.listing(self.db, self.a['id']), [])
        self.assertEqual(len(work.events(self.db, self.a['id'])), events)

    def test_target_moves_during_transaction_rolls_back(self):
        actual = reservations._tip
        calls = []
        def moving(repo, branch):
            calls.append(1)
            return actual(repo, branch) if len(calls) == 1 else 'f' * 40
        with mock.patch.object(reservations, '_tip', side_effect=moving):
            with self.assertRaisesRegex(work.WorkError, 'during reservation'):
                self.reserve(slots=('one', 'two'))
        self.assertEqual(reservations.listing(self.db, self.a['id']), [])

    def test_concurrent_batches_are_disjoint_and_contiguous(self):
        self.db.execute('UPDATE work_items SET app = %s WHERE id = %s', ('other', self.b['id']))
        barrier = threading.Barrier(2)
        results, errors = [], []
        def allocate(lot):
            conn = self.connect()
            try:
                barrier.wait(timeout=10)
                results.append(reservations.update(conn, lot['id'], ['one', 'two'], repo=self.repo,
                                                   target_branch='develop', target_sha=self.sha, actor='dev'))
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()
        threads = [threading.Thread(target=allocate, args=(lot,)) for lot in (self.a, self.b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted([r['number'] for r in batch] for batch in results), [[377, 378], [379, 380]])

    def test_legacy_null_target_and_project_protect_named_allocator(self):
        legacy = self.lot('legacy unknown project', None)
        work.reserve_migration(self.db, legacy['id'], 377, repo=self.repo)
        self.assertEqual(self.reserve()[0]['number'], 378)
        work.set_branch(self.db, legacy['id'], 'agent/legacy', target='develop', actor='dev')
        self.db.execute('UPDATE work_items SET app = %s WHERE id = %s', ('nexlink', legacy['id']))
        self.assertEqual(reservations.listing(self.db, legacy['id'])[0]['number'], 377)

    def test_upgrade_null_target_and_project_reservations_protect_named_allocator(self):
        legacy = self.lot('legacy without project', None)
        work.reserve_migration(self.db, legacy['id'], 377, repo=self.repo)
        self.db.execute('TRUNCATE work_migration_reservations RESTART IDENTITY')
        migration = next(m for m in migrations.discover() if m.name == 'migration_reservation_slots')
        self.db.script(migration.sql)
        self.assertEqual(self.reserve()[0]['number'], 378)

    def test_unknown_legacy_target_cannot_take_named_number(self):
        self.reserve()
        legacy = self.lot('legacy without project', None)
        with self.assertRaises(DbError):
            work.reserve_migration(self.db, legacy['id'], 377, repo=self.repo)
        self.assertIsNone(work.get(self.db, legacy['id'])['migration'])
        # A pre-0419 client writes the old projection without a repo lock.
        # The compatibility guard still refuses the duplicate atomically.
        with self.assertRaises(DbError) as refused:
            self.db.execute('UPDATE work_items SET migration = 377 WHERE id = %s', (legacy['id'],))
        self.assertEqual(refused.exception.sqlstate, '23505')
        self.assertEqual(refused.exception.constraint, 'work_migration_reservations_active_idx')
        self.assertIsNone(work.get(self.db, legacy['id'])['migration'])

    def test_reservation_after_landing_recognizes_arrival_before_explicit_landed(self):
        self.reserve()
        self.advance('377_schema.sql')
        self.assertEqual(self.reserve(self.b)[0]['number'], 378)
        self.assertEqual(reservations.listing(self.db, self.a['id'])[0]['status'], 'landed')
        self.assertEqual(self.reserve(action='landed')[0]['status'], 'landed')

    def test_stale_clone_never_releases_newer_reservation(self):
        old = self.sha
        newer = self.advance('376_more.sql')
        first = self.reserve()[0]
        _git(self.repo, 'update-ref', 'refs/heads/develop', old)
        self.sha = old
        self.assertEqual(self.reserve(self.b)[0]['number'], 378)
        self.assertEqual(reservations.listing(self.db, self.a['id']), [first])
        with self.assertRaisesRegex(work.WorkError, 'descend'):
            self.reserve()
        self.assertEqual(reservations.listing(self.db, self.a['id'])[0]['targetSha'], newer)

    def test_reservation_and_landed_race_share_one_lifecycle(self):
        self.reserve()
        self.advance('377_schema.sql')
        barrier = threading.Barrier(2)
        results, errors = [], []
        def run(lot, action):
            conn = self.connect()
            try:
                barrier.wait(timeout=10)
                results.append(reservations.update(conn, lot['id'], ['schema'], repo=self.repo,
                                                   target_branch='develop', target_sha=self.sha,
                                                   action=action, actor='dev'))
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()
        threads = [threading.Thread(target=run, args=(self.a, 'landed')),
                   threading.Thread(target=run, args=(self.b, 'reserve'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(reservations.listing(self.db, self.a['id'])[0]['status'], 'landed')
        self.assertEqual(reservations.listing(self.db, self.b['id'])[0]['number'], 378)

    def test_named_and_unknown_legacy_reservations_serialize_before_lot_rows(self):
        _git(self.repo, 'branch', '-D', 'unrelated')
        barrier = threading.Barrier(2)
        results, errors = [], []
        def run(named):
            conn = self.connect()
            try:
                barrier.wait(timeout=10)
                if named:
                    number = reservations.update(conn, self.a['id'], ['schema'], repo=self.repo,
                                                 target_branch='develop', target_sha=self.sha, actor='dev')[0]['number']
                else:
                    number = work.reserve_migration(conn, self.b['id'], repo=self.repo)['migration']
                results.append(number)
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()
        threads = [threading.Thread(target=run, args=(named,)) for named in (True, False)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [377, 378])

    def _legacy_writer_concurrent_with_named(self, operation):
        work.set_branch(self.db, self.a['id'], 'agent/a', target='develop', actor='dev')
        work.reserve_migration(self.db, self.a['id'], 400, repo=self.repo)
        errors, results, pid = [], [], []
        ready = threading.Event()

        def write():
            conn = self.connect()
            try:
                with conn.transaction() as tx:
                    tx.execute("SET LOCAL lock_timeout = '5s'")
                    pid.append(tx.query('SELECT pg_backend_pid() AS pid')[0]['pid'])
                    ready.set()
                    results.append(operation(storage.of(tx).work))
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        # Connection 1 holds the named allocator's repository lock. Connection
        # 2 must wait there BEFORE locking the lot, so connection 1 can reserve
        # a named slot on that lot without a cycle (or either caller retrying).
        thread = threading.Thread(target=write)
        try:
            with self.db.transaction() as tx:
                tx.execute("SET LOCAL lock_timeout = '5s'")
                storage.of(tx).migration_reservations.lock_target('github.com/manaty/nexlink', 'develop')
                thread.start()
                self.assertTrue(ready.wait(timeout=5))
                deadline = time.monotonic() + 5
                while not tx.query("SELECT 1 FROM pg_locks WHERE pid = %s"
                                   " AND locktype = 'advisory' AND NOT granted", (pid[0],)):
                    self.assertFalse(errors, errors)
                    self.assertLess(time.monotonic(), deadline, 'writer never waited for repository lock')
                    time.sleep(0.01)
                rows = reservations.update(tx, self.a['id'], ['schema'], repo=self.repo,
                                           target_branch='develop', target_sha=self.sha, actor='dev')
                self.assertEqual(rows[0]['number'], 401)
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNotNone(results[0])

    def test_set_app_and_named_allocation_lock_repository_before_lot(self):
        self._legacy_writer_concurrent_with_named(
            lambda api: api.set_app(self.a['id'], 'other', actor='dev'))

    def test_set_branch_and_named_allocation_lock_repository_before_lot(self):
        self._legacy_writer_concurrent_with_named(
            lambda api: api.set_branch(self.a['id'], 'agent/new', 'release', note='branch', actor='dev'))

    def test_close_and_named_allocation_lock_repository_before_lot(self):
        self._legacy_writer_concurrent_with_named(
            lambda api: api.close(self.a['id'], current=self.a['state'], reason='abandoned',
                                  superseded_by=None, note='close', actor='dev'))

    def test_merge_and_named_allocation_lock_repository_before_lot(self):
        self._legacy_writer_concurrent_with_named(
            lambda api: api.close_merged(self.a['id'], current=self.a['state'], sha=self.sha,
                                         pr_ref=None, note='merge', actor='dev'))

    def test_move_and_named_allocation_lock_repository_before_lot(self):
        self._legacy_writer_concurrent_with_named(
            lambda api: api.move(self.a['id'], 'build', current=self.a['state'], loops=0,
                                 note='move', actor='dev'))

    def test_release_and_named_allocation_lock_repository_before_lot(self):
        self._legacy_writer_concurrent_with_named(
            lambda api: api.reserve_migration(self.a['id'], None, None, 1, release=True,
                                              issue_ref=None,
                                              note='release %(number)s', actor='dev'))

    def test_repository_move_and_named_allocation_lock_old_repository_before_lot(self):
        self._legacy_writer_concurrent_with_named(
            lambda api: api.reserve_migration(self.a['id'], 'manaty/other', 400, 1,
                                              issue_ref='manaty/nexlink#1',
                                              note='move %(number)s', actor='dev'))
        self.assertEqual(work.get(self.db, self.a['id'])['migration_repo'], 'manaty/other')

    def test_repository_changed_after_observation_aborts_writer_without_new_lock(self):
        work.reserve_migration(self.db, self.a['id'], 400, repo=self.repo)
        observed, moved = threading.Event(), threading.Event()
        errors = []

        def write():
            conn = self.connect()
            try:
                with conn.transaction() as tx:
                    query = tx.query
                    def pause(sql, params=()):
                        rows = query(sql, params)
                        if sql == 'SELECT migration_repo FROM work_items WHERE id = %s':
                            observed.set()
                            if not moved.wait(timeout=5):
                                raise RuntimeError('repository change did not finish')
                        return rows
                    with mock.patch.object(tx, 'query', side_effect=pause):
                        storage.of(tx).work.set_app(self.a['id'], 'other', actor='dev')
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        thread = threading.Thread(target=write)
        thread.start()
        try:
            self.assertTrue(observed.wait(timeout=5))
            storage.of(self.db).work.reserve_migration(self.a['id'], 'manaty/other', 400, 1,
                                                       issue_ref='manaty/nexlink#1',
                                                       note='move %(number)s', actor='dev')
        finally:
            moved.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DbError)
        self.assertIn('repository changed; retry', str(errors[0]))
        item = work.get(self.db, self.a['id'])
        self.assertEqual((item['app'], item['migration_repo']), ('nexlink', 'manaty/other'))

    def test_refrozen_merge_rolls_back_projection_lifecycle(self):
        work.reserve_migration(self.db, self.a['id'], 400, repo=self.repo)
        self.reserve()
        before = reservations.listing(self.db, self.a['id'])
        events = work.events(self.db, self.a['id'])
        result = storage.of(self.db).work.close_merged(
            self.a['id'], current=self.a['state'], sha=self.sha, actor='dev',
            note='stale freeze', pr_ref=None, frozen_id=-1)
        self.assertIsNone(result)
        self.assertEqual(work.get(self.db, self.a['id'])['state'], self.a['state'])
        self.assertEqual(reservations.listing(self.db, self.a['id']), before)
        self.assertEqual(work.events(self.db, self.a['id']), events)

    def test_cli_json_list_and_legacy_output_compatible(self):
        args = ('work', 'migration', str(self.a['id']))
        proc = self.mesh(*args, '--slot', 'schema', '--slot', 'backfill', '--target-branch', 'develop',
                         '--target-sha', self.sha, '--repo', self.repo, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        records = json.loads(proc.stdout)
        proc = self.mesh(*args, '--list', '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), records)
        proc = self.mesh(*args, '--list', '--release')
        self.assertNotEqual(proc.returncode, 0)
        proc = self.mesh('work', 'migration', str(self.b['id']), '--repo', self.repo, '--json')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['migration'], 9001)
