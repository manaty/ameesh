# SPDX-License-Identifier: AGPL-3.0-only
"""Zombie-only groups finish worker resources and live reviews (#472)."""
import contextlib
import io
import os
import subprocess
import sys
import time
import threading
import types
import unittest
from unittest import mock

from . import support
from ameesh import background, containers, platform, resources, runner, storage
from ameesh.runner import AgentWorker, Runner
from ameesh import turn_processes
from ameesh.platform import process as process_layer


def process(pid=4242, *, zombie=True):
    return {'pid': pid, 'ppid': 1, 'pgid': 4242, 'name': 'job',
            'start': 1.0, 'zombie': zombie}


class CloseResourceTest(unittest.TestCase):
    def close(self, table, *, survives=False, container_ids=None):
        worker = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(worker, name='a1')
        worker.db, worker._turn_open = object(), True
        runtime = mock.Mock()
        runtime.running_for_turn.return_value = container_ids or []
        worker.runner = types.SimpleNamespace(container_runtime=lambda: runtime)
        with mock.patch.object(platform, 'process_table',
                               **({'side_effect': table} if isinstance(table, Exception)
                                  else {'return_value': table})), \
                mock.patch.object(os, 'killpg', return_value=None), \
                mock.patch.object(storage, 'of') as store, \
                mock.patch.object(runner, 'log'):
            worker._close_turn_resource('turn', 4242, survives=survives)
        return store.return_value.turn_resources.close_turn.call_args.kwargs

    def test_zombie_only_group_closes_done(self):
        self.assertFalse(self.close([process()])['orphan'])

    def test_absent_group_closes_done(self):
        with mock.patch.object(platform, 'process_group_exists', return_value=False):
            self.assertFalse(self.close([])['orphan'])

    def test_live_member_keeps_orphan(self):
        self.assertTrue(self.close([process(), process(4243, zombie=False)])['orphan'])

    def test_container_or_failed_handoff_keeps_orphan(self):
        for kwargs in ({'survives': True}, {'container_ids': ['c1']}):
            with self.subTest(**kwargs):
                self.assertTrue(self.close([process()], **kwargs)['orphan'])

    def test_unknown_process_table_keeps_orphan(self):
        for error in (platform.NotAvailable('process table', 'denied'), PermissionError('denied')):
            with self.subTest(error=type(error)):
                self.assertTrue(self.close(error)['orphan'])


class LiveReviewTest(unittest.TestCase):
    def review(self, table):
        executor = Runner.__new__(Runner)
        executor.host, executor.cfg = 'host', types.SimpleNamespace()
        executor.db = object()
        executor._live_review_after = 0
        executor._boot_settle_ready = lambda _uptime: True
        rows = [{'id': 1, 'turn_id': 'turn', 'agent': 'a1', 'status': 'orphan',
                 'pgid': 4242, 'started_ts': 1.0, 'containers': []}]
        with mock.patch.object(resources, 'boot_time', return_value=1.0), \
                mock.patch.object(platform, 'process_table',
                                 **({'side_effect': table} if isinstance(table, Exception)
                                    or callable(table) else {'return_value': table})), \
                mock.patch.object(os, 'killpg', return_value=None), \
                mock.patch.object(platform, 'env_values', return_value={'values': {}, 'unread': []}), \
                mock.patch.object(containers, 'all_runtimes', return_value=([], [])), \
                mock.patch.object(storage, 'of') as store, \
                mock.patch.object(runner, 'log'):
            store.return_value.turn_resources.to_review.return_value = rows
            store.return_value.turn_resources.settle_unchanged.side_effect = \
                lambda dead: [row['turn_id'] for row in dead]
            return [row['turn_id'] for row in executor.settle_dead_turns()]

    def test_zombie_only_group_is_settled(self):
        self.assertEqual(self.review([process()]), ['turn'])

    def test_live_or_recycled_group_is_kept(self):
        self.assertEqual(self.review([process(4243, zombie=False)]), [])

    def test_unknown_process_table_is_kept(self):
        for error in (platform.NotAvailable('process table', 'denied'), PermissionError('denied')):
            with self.subTest(error=type(error)):
                self.assertEqual(self.review(error), [])

    def test_replacement_between_scans_is_kept(self):
        tables = iter([[process()], [process(4243, zombie=False)]])
        self.assertEqual(self.review(lambda **_: next(tables)), [])


class SnapshotSafetyTest(unittest.TestCase):
    def stat(self, pid):
        return io.StringIO(f"{pid} (job) Z 1 4242 " + "0 " * 17 + "1")

    def fallback(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(process_layer, '_psutil', return_value=None))
        stack.enter_context(mock.patch.object(process_layer, '_proc_ok', return_value=True))
        stack.enter_context(mock.patch.object(process_layer, 'boot_time', return_value=1))
        return stack

    def test_unreadable_member_keeps_group_open(self):
        with self.fallback(), \
                mock.patch.object(os, 'listdir', return_value=['4242']), \
                mock.patch('builtins.open', side_effect=PermissionError('stat denied')):
            self.assertTrue(turn_processes.group_running(4242))

    def test_fork_replacements_between_enumeration_and_stat_keep_group_open(self):
        # Each last live member forks before its stat is read, then exits.
        # The second enumeration includes its replacement, now also a zombie;
        # PID 4244 is still live but was created after that enumeration.
        with self.fallback(), \
                mock.patch.object(os, 'listdir', side_effect=[['4242'], ['4242', '4243']]), \
                mock.patch('builtins.open', side_effect=lambda path, **_: self.stat(path.split('/')[-2])), \
                mock.patch.object(os, 'killpg', return_value=None):
            self.assertTrue(turn_processes.group_running(4242))

    def test_a_member_vanishing_in_the_first_scan_is_omitted(self):
        # a member vanishing mid-scan is gone and cannot fork again: the
        # strict scan keeps its other rows, and the stable zombie decides
        def stat(path, **_):
            pid = path.split('/')[-2]
            if pid != '4242':
                raise FileNotFoundError('replacement already exited')
            return self.stat(pid)
        with self.fallback(), \
                mock.patch.object(os, 'listdir', side_effect=[['4242', '4243'], ['4242']]), \
                mock.patch('builtins.open', side_effect=stat), \
                mock.patch.object(os, 'killpg', return_value=None):
            self.assertFalse(turn_processes.group_running(4242))

    def test_psutil_unreadable_status_or_group_keeps_open(self):
        for status, error in ((None, None), ('Z', PermissionError('denied'))):
            with self.subTest(status=status, error=error):
                ps = types.SimpleNamespace(STATUS_ZOMBIE='Z', Error=RuntimeError, pids=lambda: [4242])
                ps.Process = lambda _: types.SimpleNamespace(as_dict=lambda **_: {
                    'pid': 4242, 'ppid': 1, 'name': 'job', 'create_time': 1, 'status': status})
                with mock.patch.object(process_layer, '_psutil', return_value=ps), \
                        mock.patch.object(os, 'getpgid', return_value=4242, side_effect=error):
                    self.assertTrue(turn_processes.group_running(4242))

    def test_psutil_unreadable_member_keeps_group_open(self):
        ps = types.SimpleNamespace(STATUS_ZOMBIE='Z', Error=RuntimeError, pids=lambda: [4242],
                                   NoSuchProcess=type('NoSuchProcess', (RuntimeError,), {}),
                                   Process=mock.Mock(side_effect=RuntimeError('denied')))
        with mock.patch.object(process_layer, '_psutil', return_value=ps):
            self.assertTrue(turn_processes.group_running(4242))

    def test_psutil_vanished_member_is_omitted(self):
        no_such = type('NoSuchProcess', (RuntimeError,), {})
        ps = types.SimpleNamespace(STATUS_ZOMBIE='Z', Error=RuntimeError, NoSuchProcess=no_such,
                                   pids=lambda: [4242],
                                   Process=mock.Mock(side_effect=no_such('gone')))
        with mock.patch.object(process_layer, '_psutil', return_value=ps), \
                mock.patch.object(os, 'killpg', side_effect=ProcessLookupError('absent')) as probe:
            self.assertFalse(turn_processes.group_running(4242))
        probe.assert_called_once_with(4242, 0)

    def test_signal_zero_unknown_keeps_open_and_absent_closes(self):
        for error, expected in ((PermissionError('denied'), True),
                                (ProcessLookupError('absent'), False)):
            with self.subTest(error=error), \
                    mock.patch.object(platform, 'process_table', return_value=[]), \
                    mock.patch.object(os, 'killpg', side_effect=error) as probe:
                self.assertEqual(turn_processes.group_running(4242), expected)
                probe.assert_called_once_with(4242, 0)

    def test_group_missed_by_both_enumerations_keeps_open(self):
        with mock.patch.object(platform, 'process_table', return_value=[]), \
                mock.patch.object(os, 'killpg', return_value=None):
            self.assertTrue(turn_processes.group_running(4242))


class UnreadableScanRetryTest(unittest.TestCase):
    """A strict pair that cannot be read is retried before giving up.

    `running_groups` answers None — every requested group stays open — when a
    scan fails. An unreadable member (permission, missing status) is a stable
    failure, but a transient one must not hide every dead group until the
    next review, so the pair is tried `SNAPSHOT_ATTEMPTS` times.
    """

    @staticmethod
    def _unreadable():
        return platform.NotAvailable('process table', 'unreadable member')

    def test_an_unreadable_first_scan_is_retried(self):
        with mock.patch.object(platform, 'process_table',
                               side_effect=[self._unreadable(), [process()], [process()]]) as scan:
            self.assertEqual(turn_processes.running_groups([4242]), set())
        self.assertEqual(scan.call_count, 3)

    def test_an_unreadable_second_scan_restarts_a_fresh_pair(self):
        # what the first scan read before the failure must not leak into the retry
        vivant, zombie = [process(4242, zombie=False)], [process()]
        with mock.patch.object(platform, 'process_table',
                               side_effect=[vivant, self._unreadable(), zombie, zombie]) as scan:
            self.assertEqual(turn_processes.running_groups([4242]), set())
        self.assertEqual(scan.call_count, 4)

    def test_a_retried_pair_still_keeps_a_live_group_open(self):
        vivant = [process(4242, zombie=False)]
        with mock.patch.object(platform, 'process_table',
                               side_effect=[self._unreadable(), vivant, vivant]):
            self.assertEqual(turn_processes.running_groups([4242]), {4242})

    def test_every_unreadable_pair_fails_closed(self):
        with mock.patch.object(platform, 'process_table',
                               side_effect=self._unreadable()) as scan:
            self.assertIsNone(turn_processes.running_groups([4242]))
        self.assertEqual(scan.call_count, turn_processes.SNAPSHOT_ATTEMPTS)


@contextlib.contextmanager
def external_zombie():
    code = ('import os,time,signal,sys\n'
            'pid=os.fork()\n'
            'if pid==0:\n os.setpgid(0,0)\n os._exit(0)\n'
            'def stop(*_):\n os.waitpid(pid,0)\n sys.exit(0)\n'
            'signal.signal(signal.SIGTERM,stop)\n'
            'print(pid,flush=True)\n'
            'time.sleep(60)\n')
    parent = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True)
    try:
        pid = int(parent.stdout.readline())
        deadline = time.monotonic() + 10
        while not any(row['pid'] == pid and row['zombie'] for row in platform.process_table()):
            if time.monotonic() >= deadline:
                raise AssertionError('child did not become a zombie')
            time.sleep(.02)
        yield pid
    finally:
        parent.terminate()  # its handler reaps its own zombie before exiting
        try:
            parent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            parent.kill()
            parent.wait()
        parent.stdout.close()


@contextlib.contextmanager
def isolated_process_enumeration(pid):
    """Read the real fixture's stat without unrelated host fork/exit churn."""
    listdir = os.listdir
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(os, 'listdir', side_effect=lambda path:
            [str(pid)] if path == process_layer.PROC else listdir(path)))
        ps = process_layer._psutil()
        if ps is not None:
            stack.enter_context(mock.patch.object(ps, 'pids', return_value=[pid]))
        yield


@unittest.skipUnless(hasattr(os, 'fork') and sys.platform.startswith('linux'), 'Linux zombie fixture')
class RealZombieResourceTest(support.PgTestCase):
    def test_worker_closes_resource_after_tracker_finds_no_survivors(self):
        with external_zombie() as pgid, isolated_process_enumeration(pgid):
            self.assertTrue(AgentWorker._group_alive(pgid))
            worker = AgentWorker.__new__(AgentWorker)
            threading.Thread.__init__(worker, name='a1')
            worker.db = self.db
            worker.runner = types.SimpleNamespace(host=self.cfg.host, container_runtime=lambda: None)
            tracker = background.Tracker(worker.runner, log=lambda *_: None)
            self.assertIsNone(tracker.survivors('worker-zombie', pgid))
            worker._open_turn_resource('worker-zombie', pgid)
            worker._close_turn_resource('worker-zombie', pgid)
            self.assertEqual(storage.of(self.db).turn_resources.get('worker-zombie')['status'], 'done')

    def test_live_review_settles_zombie_only_resource(self):
        with external_zombie() as pgid, isolated_process_enumeration(pgid):
            self.assertTrue(AgentWorker._group_alive(pgid))
            turns = storage.of(self.db).turn_resources
            turns.open_turn('review-zombie', 'a1', self.cfg.host, pgid=pgid, label='test')
            turns.mark_orphan('review-zombie')
            executor = Runner(self.cfg, self.db, once=True)
            with mock.patch.object(resources, 'boot_time', return_value=time.time()-86400), \
                    mock.patch.object(executor, '_boot_settle_ready', return_value=True), \
                    mock.patch.object(platform, 'env_values', return_value={'values': {}, 'unread': []}), \
                    mock.patch.object(containers, 'all_runtimes', return_value=([], [])):
                self.assertIn('review-zombie', [row['turn_id'] for row in executor.settle_dead_turns()])
            self.assertEqual(turns.get('review-zombie')['status'], 'done')
