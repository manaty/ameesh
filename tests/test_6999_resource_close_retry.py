# SPDX-License-Identifier: AGPL-3.0-only
"""Resource close replay keeps its verdict and the accounting retry schedule."""
import threading
import unittest
from unittest import mock

from ameesh.runner import Runner


class RetryTest(unittest.TestCase):
    def test_repeated_outage_preserves_orphan_verdict_and_backoff(self):
        runner = Runner.__new__(Runner)
        runner.db = object()
        runner.db_retry_max = 4
        runner._resource_close_lock = threading.Lock()
        runner._resource_closes = {}
        containers = ['survivor']
        store = mock.Mock()
        store.turn_resources.close_turn.side_effect = [RuntimeError('outage'), None]
        with mock.patch('ameesh.runner.time.monotonic', return_value=100):
            runner.defer_resource_close('turn', orphan=True, containers=containers)
        containers.clear()  # queued evidence does not alias caller state
        with mock.patch('ameesh.runner.storage.of', return_value=store), \
                mock.patch('ameesh.runner.log_async'), \
                mock.patch('ameesh.runner.time.monotonic', return_value=102):
            self.assertEqual(runner.retry_resource_closes(), 0)
            self.assertEqual(runner._resource_closes['turn']['due'], 106)
        with mock.patch('ameesh.runner.storage.of', return_value=store), \
                mock.patch('ameesh.runner.time.monotonic', return_value=105):
            self.assertEqual(runner.retry_resource_closes(), 0)
            self.assertEqual(store.turn_resources.close_turn.call_count, 1)
        with mock.patch('ameesh.runner.storage.of', return_value=store), \
                mock.patch('ameesh.runner.time.monotonic', return_value=106):
            self.assertEqual(runner.retry_resource_closes(), 1)
        self.assertEqual(store.turn_resources.close_turn.call_args_list,
                         [mock.call('turn', orphan=True, containers=['survivor'])] * 2)
        self.assertFalse(runner._resource_closes)
