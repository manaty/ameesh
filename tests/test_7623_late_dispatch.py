# SPDX-License-Identifier: AGPL-3.0-only
"""A part announced after full dispatch gets one durable automatic attempt."""
import contextlib
import json
import types
import unittest
from unittest import mock

from ameesh import ci, merge_queue, plan_github, work
from tests.support import PgTestCase


class LateDispatchTest(unittest.TestCase):
    def setUp(self):
        self.sha = 'a' * 40
        self.repo = 'acme/project'
        self.lot = dict(id=7623, branch='integration/packet', state='qa')
        self.inputs = dict(expected_sha=self.sha, base_sha='b' * 40)
        self.events = []
        self.order = []
        self.db = mock.Mock()

        @contextlib.contextmanager
        def transaction():
            self.order.append('begin')
            yield self.db
            self.order.append('commit')

        self.db.transaction.side_effect = transaction
        self.store = mock.Mock()
        self.store.work.lock.return_value = self.lot
        self.watcher = ci.Watcher(types.SimpleNamespace(), api=mock.Mock())
        self.watcher.api.return_value = dict(total_count=0, workflow_runs=[])
        self.patch(ci.storage, 'is_remote', return_value=False)
        self.patch(ci.storage, 'of', return_value=self.store)
        self.patch(work, 'events', side_effect=lambda *a, **k: self.events[:])
        self.patch(work, 'milestones', return_value=[dict(kind='frozen', sha=self.sha)])
        self.patch(work, 'note', side_effect=lambda db, lot, text: self.events.append(dict(note=text)))
        self.gate = self.patch(merge_queue, 'review_gate_for_item', return_value=dict(ok=True))
        self.head = self.patch(ci, '_head_of', return_value=self.sha)
        self.plan = self.patch(ci, '_dispatch_plan', return_value={'late.yml': self.inputs})
        self.gh = self.patch(plan_github, 'Gh').return_value

        def dispatch(*args):
            self.assertIn('commit', self.order)
            self.assertEqual(self.order[-1], 'begin')
            self.assertEqual(len(self.events), 1)
            return 0, '', ''

        self.gh.run.side_effect = dispatch

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def retry(self):
        return self.watcher._retry_missing_part(self.db, self.repo, self.lot,
                                               self.sha, 'late.yml', self.inputs)

    def test_late_part_dispatched_once_even_before_run_becomes_visible(self):
        self.assertTrue(self.retry())
        self.assertFalse(self.retry())
        self.gh.run.assert_called_once()
        args, payload = self.gh.run.call_args.args
        self.assertIn('repos/acme/project/actions/workflows/late.yml/dispatches', args)
        self.assertEqual(json.loads(payload), dict(ref=self.lot['branch'], inputs=self.inputs))

    def test_existing_run_of_any_status_prevents_dispatch(self):
        for status in ('queued', 'in_progress', 'completed'):
            with self.subTest(status=status):
                self.watcher.api.return_value = dict(total_count=1, workflow_runs=[dict(status=status)])
                self.assertFalse(self.retry())
        self.gh.run.assert_not_called()
        self.assertEqual(self.events, [])

    def test_ambiguous_post_keeps_intent_across_watcher_restart(self):
        self.gh.run.return_value = (1, '', 'timeout')
        self.gh.run.side_effect = None
        self.assertFalse(self.retry())
        self.watcher = ci.Watcher(types.SimpleNamespace(), api=mock.Mock())
        self.assertFalse(self.retry())
        self.gh.run.assert_called_once()

    def test_head_review_or_selection_change_refuses_without_reservation(self):
        self.head.return_value = 'c' * 40
        self.assertFalse(self.retry())
        self.head.return_value = self.sha
        self.gate.return_value = dict(ok=False)
        self.assertFalse(self.retry())
        self.gate.return_value = dict(ok=True)
        self.plan.return_value = {}
        self.assertFalse(self.retry())
        self.assertEqual(self.events, [])
        self.gh.run.assert_not_called()

    def test_branch_moved_after_reservation_is_not_dispatched(self):
        self.head.side_effect = [self.sha, 'c' * 40]
        self.assertFalse(self.retry())
        self.assertEqual(len(self.events), 1)
        self.gh.run.assert_not_called()

    def test_review_withdrawn_after_reservation_prevents_post(self):
        self.gate.side_effect = [dict(ok=True), dict(ok=False)]
        self.assertFalse(self.retry())
        self.assertEqual(len(self.events), 1)
        self.gh.run.assert_not_called()

    def test_run_appearing_after_reservation_prevents_post(self):
        self.watcher.api.side_effect = [dict(total_count=0, workflow_runs=[]),
                                       dict(total_count=1, workflow_runs=[dict(status='queued')])]
        self.assertFalse(self.retry())
        self.assertEqual(len(self.events), 1)
        self.gh.run.assert_not_called()

    def test_unreadable_run_response_is_not_absence(self):
        for response in (None, {}, dict(total_count=True, workflow_runs=[]),
                         dict(total_count=2, workflow_runs=[])):
            self.watcher.api.return_value = response
            self.assertFalse(self.retry())
        self.assertEqual(self.events, [])

    def test_cached_transport_is_revalidated_and_stale_absence_is_refused(self):
        calls = []

        class Transport(plan_github.GhApi):
            stale = True

            def get(self, path, *, ttl=None):
                calls.append(ttl)
                self.last_read = plan_github.ReadMeta(1.0, self.stale, 'cache')
                return dict(total_count=0, workflow_runs=[])

        transport = Transport(mock.Mock())
        self.watcher.api = transport.get
        with self.assertRaisesRegex(ci.GelError, 'fresh GitHub read'):
            self.retry()
        self.assertEqual(self.events, [])
        self.gh.run.assert_not_called()
        transport.stale = False
        self.assertTrue(self.retry())
        self.assertEqual(calls, [0, 0, 0])

    def test_dry_run_and_mediated_storage_keep_observation_only(self):
        self.watcher.dry_run = True
        self.assertFalse(self.retry())
        self.watcher.dry_run = False
        with mock.patch.object(ci.storage, 'is_remote', return_value=True):
            self.assertFalse(self.retry())
        self.db.transaction.assert_not_called()
        self.gh.run.assert_not_called()


class DurableDispatchTest(PgTestCase):
    def test_two_watchers_share_one_committed_intent(self):
        import threading
        lot = work.add(self.db, title='Late CI part', app='t7623', actor='human:proprio')
        self.db.execute("UPDATE work_items SET state = 'qa', branch = %s WHERE id = %s",
                        ('integration/packet', lot['id']))
        lot = work.get(self.db, lot['id'])
        sha = 'a' * 40
        inputs = dict(expected_sha=sha)
        barrier = threading.Barrier(2, timeout=30)
        results, errors = [], []
        gh = mock.Mock()

        def post(*args):
            # Read through a different connection: intent is durable before POST.
            other = self.connect()
            try:
                self.assertEqual(sum(e['note'].startswith('ci-missing-part ')
                                     for e in work.events(other, lot['id'], limit=None)), 1)
            finally:
                other.close()
            return 0, '', ''

        gh.run.side_effect = post

        def attempt():
            db = self.connect()
            try:
                watcher = ci.Watcher(self.cfg, api=mock.Mock(return_value=dict(total_count=0, workflow_runs=[])))
                barrier.wait()
                results.append(watcher._retry_missing_part(db, 'acme/project', lot, sha,
                                                          'late.yml', inputs))
            except BaseException as exc:
                errors.append(exc)
            finally:
                db.close()

        with mock.patch.object(work, 'milestones', return_value=[dict(kind='frozen', sha=sha)]), \
                mock.patch.object(merge_queue, 'review_gate_for_item', return_value=dict(ok=True)), \
                mock.patch.object(ci, '_head_of', return_value=sha), \
                mock.patch.object(ci, '_dispatch_plan', return_value={'late.yml': inputs}), \
                mock.patch.object(plan_github, 'Gh', return_value=gh):
            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=40)
            self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        gh.run.assert_called_once()
