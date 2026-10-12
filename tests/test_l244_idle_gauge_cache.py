# SPDX-License-Identifier: AGPL-3.0-only
"""Offline cache regressions for the idle runner's repeated CostBooks."""
import os
import shutil
import unittest
from unittest import mock

from ameesh import cost
from ameesh.gauge_cache import GaugeCache
from .test_cost import Sandbox


class CrossBookGaugeTest(Sandbox, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cache = GaugeCache(max_entries=4, max_accounts=2)
        patcher = mock.patch.object(cost, 'CLAUDE_GAUGES', self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stream(self, name='a', account='primary', used=0.42):
        self.agent(name, 'claude')
        self.events(name, {'type': cost.ACCOUNT_MARKER, 'harness': 'claude',
                           'account': account, 'ts': self.now},
                    {'type': 'rate_limit_event', 'rate_limit_info': {
                        'unifiedWindows': {'five_hour': {
                            'utilization': used, 'resetsAt': self.now + 3600}}}})
        return os.path.join(self.state, name, 'events.jsonl')

    def gauges(self, account='primary'):
        return self.book().claude_gauges(account, 'primary')

    def test_new_books_reuse_unchanged_observations_without_sharing_result_lists(self):
        self.stream()
        with mock.patch.object(cost, '_claude_account_tail',
                               wraps=cost._claude_account_tail) as read:
            self.gauges().clear()
            self.gauges().clear()
            self.assertAlmostEqual(self.gauges()[0].used, 0.42)
            self.assertEqual(read.call_count, 1)

    def test_append_is_visible_to_next_book_and_accounts_stay_separate(self):
        self.stream()
        self.gauges()
        self.stream(account='secondary', used=0.89)
        self.assertAlmostEqual(self.gauges('secondary')[0].used, 0.89)
        self.assertAlmostEqual(self.gauges()[0].used, 0.42)

    def test_same_size_rewrite_restored_mtime_replacement_and_truncation(self):
        path = self.stream()
        self.gauges()
        stat = os.stat(path)
        with open(path, 'r+b') as stream:
            content = stream.read().replace(b'0.42', b'0.89')
            stream.seek(0)
            stream.write(content)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertAlmostEqual(self.gauges()[0].used, 0.89)
        with open(path + '.new', 'wb') as stream:
            stream.write(content.replace(b'0.89', b'0.31'))
        os.utime(path + '.new', ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(path + '.new', path)
        self.assertAlmostEqual(self.gauges()[0].used, 0.31)
        with open(path, 'wb'):
            pass
        self.assertEqual(self.gauges(), [])

    def test_removed_agent_directories_do_not_accumulate_unbounded_entries(self):
        for i in range(20):
            name = 'agent-%d' % i
            self.stream(name=name)
            self.assertAlmostEqual(self.gauges()[0].used, 0.42)
            shutil.rmtree(os.path.join(self.state, name))
            self.assertLessEqual(len(self.cache), 4)
        self.assertEqual(len(self.cache), 4)
        self.assertEqual(self.gauges(), [])

    def test_many_account_markers_bypass_cache_without_losing_observations(self):
        for account in ('primary', 'secondary', 'tertiary'):
            self.stream(account=account)
        with mock.patch.object(cost, '_claude_account_tail',
                               wraps=cost._claude_account_tail) as read:
            self.assertAlmostEqual(self.gauges('tertiary')[0].used, 0.42)
            self.assertAlmostEqual(self.gauges()[0].used, 0.42)
            self.assertEqual(read.call_count, 2)
        self.assertEqual(len(self.cache), 0)

    def test_read_failure_cannot_return_an_old_cached_observation(self):
        path = self.stream()
        self.gauges()
        self.stream(used=0.89)
        with mock.patch.object(cost, '_claude_account_tail', side_effect=OSError('unreadable')):
            self.assertEqual(self.gauges(), [])
        self.assertIsNone(self.cache.get(path))
        self.assertAlmostEqual(self.gauges()[0].used, 0.89)

    def test_budget_thresholds_are_recomputed_as_time_passes(self):
        self.stream(used=0.42)
        self.assertFalse(self.gauges()[0].exceeded(self.now))
        self.assertTrue(self.gauges()[0].exceeded(self.now - 9000))
        self.assertFalse(self.gauges()[0].exceeded(self.now + 3601))
