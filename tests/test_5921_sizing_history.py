# SPDX-License-Identifier: AGPL-3.0-only
"""Sizing history: exact distributions, NULL coverage, UTC and N-1 writers."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.support import PgTestCase
from ameesh import storage
from ameesh.platform import turn_cgroup
from ameesh.storage.postgres import sizing_history


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


class CounterTest(unittest.TestCase):
    def test_counters_and_missing_peak(self):
        with tempfile.TemporaryDirectory() as root:
            group = Path(root) / 'turn-a'
            group.mkdir()
            (group / 'memory.peak').write_text('12345\n')
            (group / 'cpu.stat').write_text('user_usec 13\nusage_usec 42\nsystem_usec 29\n')
            self.assertEqual(turn_cgroup.usage('/turn-a', root_dir=root),
                             dict(memory_peak_bytes=12345, cpu_usage_usec=42))
            (group / 'memory.peak').unlink()
            (group / 'memory.current').write_text('100')
            self.assertIsNone(turn_cgroup.usage('/turn-a', root_dir=root)['memory_peak_bytes'])
            (group / 'cpu.stat').write_text('usage_usec -1')
            self.assertIsNone(turn_cgroup.usage('/turn-a', root_dir=root)['cpu_usage_usec'])
            self.assertEqual(turn_cgroup.usage('/../escape', root_dir=root),
                             dict(memory_peak_bytes=None, cpu_usage_usec=None))

    def test_runner_records_before_removal_and_keeps_cleanup_on_write_failure(self):
        from ameesh.runner import AgentWorker
        worker = AgentWorker.__new__(AgentWorker)
        import threading
        threading.Thread.__init__(worker)
        worker.name = 'tester'
        worker.db = mock.Mock()
        worker.runner = mock.Mock(mediated=False)
        worker.runner.container_runtime.return_value = None
        store = mock.Mock()
        order = []
        def record(*args, **kwargs):
            order.append('record')
            raise RuntimeError('database unavailable')
        store.turn_resources.record_usage.side_effect = record
        def kill(*args):
            order.append('kill')
            return True
        with mock.patch('ameesh.runner.storage.of', return_value=store), \
                mock.patch('ameesh.platform.turn_cgroup.usage', return_value={
                    'memory_peak_bytes': 123, 'cpu_usage_usec': 42}), \
                mock.patch('ameesh.platform.turn_cgroup.memory_peak', return_value=123), \
                mock.patch('ameesh.platform.turn_cgroup.kill', side_effect=kill), \
                mock.patch('ameesh.turn_processes.group_running', return_value=False), \
                mock.patch('ameesh.runner.log'):
            worker._release_resource('a', None, cgroup='/turn-a')
        self.assertEqual(order, ['record', 'kill'])
        store.turn_resources.close_turn.assert_called_once_with('a', orphan=False, containers=None)

    def test_background_records_before_natural_removal(self):
        from types import SimpleNamespace
        from ameesh import background
        runner = SimpleNamespace(db=mock.Mock(), mediated=False,
                                 container_runtime=lambda: None)
        tracker = background.Tracker(runner, log=lambda message: None)
        entry = background.Entry(agent='tester', turn_id='a', pgid=None,
                                 ended=0, deadline=1, grace_s=1, cgroup='/turn-a')
        store = mock.Mock()
        order = []
        store.turn_resources.record_usage.side_effect = lambda *a, **k: order.append('record')
        with mock.patch('ameesh.background.storage.of', return_value=store), \
                mock.patch('ameesh.platform.turn_cgroup.usage', return_value={
                    'memory_peak_bytes': 123, 'cpu_usage_usec': 42}), \
                mock.patch('ameesh.platform.turn_cgroup.remove',
                           side_effect=lambda *a: order.append('remove') or True):
            tracker._release_resource(entry, survives=False)
        self.assertEqual(order, ['record', 'remove'])
        store.turn_resources.record_usage.assert_called_once_with(
            'a', memory_peak_bytes=123, cpu_usage_usec=42)


class HistoryTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM host_resources')
        self.db.execute('DELETE FROM turn_resources')
        self.db.execute('DELETE FROM host_resource_daily')
        self.turns = storage.of(self.db).turn_resources

    def metrics(self, day, column='host_metrics'):
        row = self.db.query('SELECT ' + column + ' AS metrics FROM host_resource_daily '
                            'WHERE host = %s AND day = %s::date', ('test-host', day))[0]
        return decoded(row['metrics'])

    def test_host_exact_p95_utc_isolation_and_unknowns(self):
        for host, instant, cpu, memory in [
            ('test-host', '2026-01-02 00:30+01', 1, 100),
            ('test-host', '2026-01-01 23:40+00', 3, 300),
            ('test-host', '2026-01-02 00:01+00', 99, 900),
            ('other-host', '2026-01-01 23:40+00', 88, 800),
            ('test-host', '2026-01-01 23:50+00', None, None),
        ]:
            self.db.execute('INSERT INTO host_resources(host, sampled_at, app_cpu, '
                            'mem_total_bytes, mem_available_bytes, turns_in_progress, psi_mem_some) '
                            'VALUES (%s, %s::timestamptz, %s, 1000, %s, 2, 5)',
                            (host, instant, cpu, None if memory is None else 1000-memory))
        sizing_history.refresh_host(self.db, 'test-host', '2026-01-01')
        m = self.metrics('2026-01-01')
        self.assertEqual(m['samples'], 3)
        self.assertEqual(m['memory_samples'], 2)
        self.assertEqual(m['memory_max_bytes'], 300)
        self.assertAlmostEqual(m['memory_p95_bytes'], 290)
        self.assertAlmostEqual(m['app_cpu_p95'], 2.9)
        self.assertEqual(m['concurrency_max'], 2)
        self.assertEqual(m['psi_mem_some_max'], 5)
        self.assertIsNone(m['psi_io_some_p95'])

    def test_n1_turn_and_idempotent_usage(self):
        self.previous_insert('turn_resources', {
            'turn_id': 'old', 'agent': 'tester', 'host': 'test-host'},
            added=('memory_peak_bytes', 'cpu_usage_usec'))
        self.turns.open_turn('new', 'tester', 'test-host', pgid=None, label=None)
        self.turns.record_usage('new', memory_peak_bytes=500, cpu_usage_usec=200)
        self.turns.record_usage('new', memory_peak_bytes=None, cpu_usage_usec=None)
        old = self.turns.get('old')
        self.assertIsNone(old['memory_peak_bytes'])
        day = self.db.query("SELECT (now() AT TIME ZONE 'UTC')::date::text AS day")[0]['day']
        m = self.metrics(day, 'turn_metrics')
        self.assertEqual(m['turns'], 2)
        self.assertEqual(m['memory_samples'], 1)
        self.assertEqual(m['memory_max_bytes'], 500)
        self.assertEqual(m['cpu_usage_usec'], 200)
        with self.assertRaises(ValueError):
            self.turns.record_usage('new', memory_peak_bytes=-1, cpu_usage_usec=0)

    def test_record_refresh_and_retention_survives_raw_prune(self):
        self.db.execute("INSERT INTO host_resource_daily(host, day, host_metrics) VALUES "
                        "('test-host', (now() AT TIME ZONE 'UTC')::date - 89, '{\"samples\": 7}'), "
                        "('test-host', (now() AT TIME ZONE 'UTC')::date - 90, '{\"samples\": 8}')")
        storage.of(self.db).hosts.record({'host': 'test-host', 'app_cpu': 2,
                                         'mem_total_bytes': 1000, 'mem_available_bytes': 400})
        rows = self.db.query('SELECT host_metrics FROM host_resource_daily ORDER BY day')
        self.assertEqual(len(rows), 2)
        self.assertEqual(decoded(rows[0]['host_metrics'])['samples'], 7)
        self.assertEqual(decoded(rows[1]['host_metrics'])['memory_max_bytes'], 600)
        self.db.execute('DELETE FROM host_resources')
        self.assertEqual(len(self.db.query('SELECT host FROM host_resource_daily')), 2)
