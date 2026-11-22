# SPDX-License-Identifier: AGPL-3.0-only
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import ci, plan_github


class WatcherRowsTest(unittest.TestCase):
    def test_missing_and_failed_runs_do_not_stop_later_runs(self):
        rows = [dict(run_id=i, head_sha='a', branch='main') for i in (1, 2, 3)]
        store = mock.Mock()
        store.ci.running.return_value = rows
        store.ci.conclude.return_value = True
        store.work.open_for_sweep.return_value = []
        def api(path):
            run_id = int(path.rsplit('/', 1)[-1])
            if run_id == 1:
                raise plan_github.GithubError('gone', status=404)
            if run_id == 2:
                raise plan_github.GithubError('server', status=500)
            return {'status': 'completed', 'conclusion': 'success'}
        logs = []
        watcher = ci.Watcher(SimpleNamespace(), api=api, log=logs.append)
        watcher.followed_workflows = lambda repo: []
        watcher.bases = lambda repo: []
        watcher._listed = lambda repo, status: []
        watcher._unblock = lambda *a: []
        watcher._catch_up = lambda *a: []
        watcher._refresh_bases = lambda *a: None
        watcher._failures = lambda *a, **kw: []
        concluded = []
        watcher._conclude = lambda db, repo, row, **kw: concluded.append(row['run_id']) or []
        with mock.patch.object(ci.storage, 'of', return_value=store):
            with self.assertRaises(ci.CiError):
                watcher._watch(object(), 'owner/repo')
        store.ci.conclude.assert_called_once_with('owner/repo', 1, 'cancelled')
        self.assertEqual(concluded, [3])
        self.assertTrue(any('disappeared' in line for line in logs))

    def test_gone_410_is_cancelled_only_once(self):
        store = mock.Mock()
        store.ci.running.return_value = [dict(run_id=4, head_sha='a', branch='main')]
        store.ci.conclude.return_value = False
        store.work.open_for_sweep.return_value = []
        def api(path):
            raise plan_github.GithubError('gone', status=410)
        logs = []
        watcher = ci.Watcher(SimpleNamespace(), api=api, log=logs.append)
        watcher.followed_workflows = lambda repo: []
        watcher.bases = lambda repo: []
        watcher._listed = lambda *a: []
        watcher._unblock = lambda *a: []
        watcher._catch_up = lambda *a: []
        watcher._refresh_bases = lambda *a: None
        watcher._guard = lambda *a: None
        watcher._undispatched_parts = lambda *a: []
        watcher._map = watcher._disks = lambda *a: None
        with mock.patch.object(ci.storage, 'of', return_value=store):
            watcher._watch(object(), 'owner/repo')
        self.assertFalse(logs)
        store.ci.conclude.assert_called_once_with('owner/repo', 4, 'cancelled')

class WatcherAlertTest(unittest.TestCase):
    def test_three_failures_alert_once_per_incident_and_success_resets(self):
        store = mock.Mock()
        store.ci.watch_failure.side_effect = [
            {'watch_failures': n, 'since': 123} for n in (1, 2, 3, 4)]
        watcher = ci.Watcher(SimpleNamespace(state_dir=None), api=object())
        watcher.repos = lambda: ['owner/repo']
        watcher._disk_refusal_messages = lambda *a: []
        watcher._project_pilot = lambda *a: 'pilot'
        watcher.watch = mock.Mock(side_effect=[RuntimeError('bad')] * 4 + [[]])
        with mock.patch.object(ci.storage, 'of', return_value=store), \
                mock.patch.object(ci, '_send') as send, \
                mock.patch.object(plan_github, 'flush_reads'):
            for _ in range(5):
                watcher.tick(object())
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args[2], send.call_args_list[1].args[2])
        self.assertNotIn('/', send.call_args_list[0].kwargs['idempotency_key'])
        self.assertEqual(send.call_args_list[0].kwargs['idempotency_key'],
                         send.call_args_list[1].kwargs['idempotency_key'])
        store.ci.watch_success.assert_called_once_with('owner/repo')

from tests.support import PgTestCase
from ameesh import storage


class WatcherFailureStoreTest(PgTestCase):
    def test_failure_count_survives_new_store_and_success_resets(self):
        store = storage.of(self.db).ci
        self.assertEqual(store.watch_failure('o/r')['watch_failures'], 1)
        second = storage.of(self.db).ci.watch_failure('o/r')
        self.assertEqual(second['watch_failures'], 2)
        store.watch_success('o/r')
        self.assertEqual(store.watch_failure('o/r')['watch_failures'], 1)
