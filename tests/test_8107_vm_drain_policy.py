# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import unittest
from unittest import mock

from ameesh import config, host_lifecycle


class DrainPolicyTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch('ameesh.canon.configured', return_value=False))
        self.stack.enter_context(mock.patch('ameesh.work.configured_canons', return_value=[]))
        self.stack.enter_context(mock.patch('ameesh.host_lifecycle.critical_names', return_value=set()))
        self.hook = self.stack.enter_context(mock.patch('ameesh.host_release.hook_for', return_value='hook'))
        self.queue = self.stack.enter_context(mock.patch('ameesh.storage.postgres.vm_adapter.Queue'))
        self.queue.return_value.observed_wake_delay.return_value = None
        self.store = self.stack.enter_context(mock.patch('ameesh.host_lifecycle.storage.of'))
        self.cfg = config.Config()

    def drain(self, duration, **kwargs):
        return host_lifecycle.drain(self.cfg, mock.Mock(), 'vm', duration=duration,
                                    actor='human:test', **kwargs)

    def test_bound_and_default_wake(self):
        with self.assertRaisesRegex(ValueError, 'minimum 2700 seconds'):
            self.drain(1800)
        self.store.return_value.host_lifecycle.drain.assert_not_called()
        self.drain(2700)
        self.store.return_value.host_lifecycle.drain.assert_called_once()

    def test_observed_wake_and_override(self):
        self.queue.return_value.observed_wake_delay.return_value = 1200
        with self.assertRaisesRegex(ValueError, 'minimum 3600 seconds'):
            self.drain(3599)
        self.drain(1800, force_short_reason='urgent maintenance')
        self.assertEqual(self.store.return_value.host_lifecycle.drain.call_args.kwargs['force_short_reason'],
                         'urgent maintenance')
        with self.assertRaisesRegex(ValueError, 'non-empty reason'):
            self.drain(1800, force_short_reason='  ')

    def test_no_hook_or_no_duration_does_not_require_observation(self):
        self.hook.return_value = None
        self.drain(60)
        self.hook.return_value = 'hook'
        self.drain(None)
        self.queue.assert_not_called()
