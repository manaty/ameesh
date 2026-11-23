# SPDX-License-Identifier: AGPL-3.0-only
import unittest
from unittest import mock

from ameesh import config, host_lifecycle


class DrainPolicyTest(unittest.TestCase):
    def setUp(self):
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch('ameesh.canon.configured', return_value=False))
        self.stack.enter_context(mock.patch('ameesh.work.configured_canons', return_value=[]))
        self.stack.enter_context(mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()))
        self.hook = self.stack.enter_context(mock.patch('ameesh.host_release.hook_for', return_value='hook'))
        self.queue = self.stack.enter_context(mock.patch('ameesh.storage.postgres.vm_adapter.Queue'))
        self.queue.return_value.drain_policy.return_value = dict(
            idle_after_s=1800, min_on_s=1800, min_off_s=60, wake_delay_s=900)
        self.store = self.stack.enter_context(mock.patch('ameesh.host_lifecycle.storage.of'))
        self.cfg = config.Config()

    def drain(self, duration):
        return host_lifecycle.drain(self.cfg, mock.Mock(), 'vm', duration=duration,
                                    actor='human:test')

    def test_hook_rejects_short_duration_and_accepts_minimum(self):
        with self.assertRaisesRegex(ValueError, 'minimum 2760 seconds'):
            self.drain(2759)
        self.store.return_value.host_lifecycle.drain.assert_not_called()
        self.drain(2760)
        self.store.return_value.host_lifecycle.drain.assert_called_once()

    def test_no_hook_or_no_duration_does_not_require_policy(self):
        self.hook.return_value = None
        self.drain(60)
        self.hook.return_value = 'hook'
        self.drain(None)
        self.queue.assert_not_called()

    def test_missing_policy_fails_closed(self):
        self.queue.return_value.drain_policy.return_value = None
        with self.assertRaisesRegex(ValueError, 'unavailable or stale'):
            self.drain(3600)
        self.store.return_value.host_lifecycle.drain.assert_not_called()
