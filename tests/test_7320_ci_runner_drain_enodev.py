# SPDX-License-Identifier: AGPL-3.0-only
"""#7320: cgroup removal after kill must not strand a failed runner."""
import builtins
import errno
import itertools
import os
import subprocess
import unittest
from unittest import mock

from . import test_3317_ci_runner_drain_race as race

drain = race.drain
R1 = race.R1


class RemovedCgroupTest(unittest.TestCase):
    def setUp(self):
        self.fixture = race.FrozenDrainTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.host = self.fixture.host
        self.group = str(self.fixture.group)

    def test_events_removed_after_kill_still_acts_once(self):
        for number in (errno.ENOENT, errno.ENODEV):
            with self.subTest(errno=number):
                self.fixture.events.clear()
                self.fixture.alive = True
                real_open = builtins.open

                def open_(path, *args, **kwargs):
                    if str(path).endswith('/cgroup.events') and not self.fixture.alive:
                        raise OSError(number, os.strerror(number))
                    return real_open(path, *args, **kwargs)

                def kill(group):
                    self.fixture.kill(group)
                    drain.Host._kill_frozen(self.host, group)

                self.host._kill_frozen = kill
                with mock.patch('builtins.open', side_effect=open_):
                    self.assertTrue(self.host.act_if_idle('restart', R1))
                self.assertEqual(self.fixture.events,
                                 ['freeze', 'kill', 'thaw', 'restart'])

    def test_removed_before_kill_open_still_acts_once(self):
        real_open = os.open

        def open_(path, *args, **kwargs):
            if str(path).endswith('/cgroup.kill'):
                self.fixture.alive = False
                (self.fixture.group / 'cgroup.events').write_text('frozen 1\npopulated 0\n')
                raise OSError(errno.ENODEV, 'removed')
            return real_open(path, *args, **kwargs)

        self.host._kill_frozen = lambda group: drain.Host._kill_frozen(self.host, group)
        with mock.patch.object(drain.os, 'open', side_effect=open_):
            self.assertTrue(self.host.act_if_idle('restart', R1))
        self.assertEqual(self.fixture.events, ['freeze', 'thaw', 'restart'])

    def test_enodev_open_with_populated_cgroup_refuses_restart(self):
        real_open = os.open

        def open_(path, *args, **kwargs):
            if str(path).endswith('/cgroup.kill'):
                raise OSError(errno.ENODEV, 'kill unavailable')
            return real_open(path, *args, **kwargs)

        self.host._kill_frozen = lambda group: drain.Host._kill_frozen(self.host, group)
        self.host.act = mock.Mock()
        with mock.patch.object(drain.os, 'open', side_effect=open_), \
             mock.patch.object(drain.time, 'monotonic', side_effect=itertools.count(0, 6)):
            with self.assertRaisesRegex(OSError, R1 + '.*populated=0'):
                self.host.act_if_idle('restart', R1)
        self.host.act.assert_not_called()
        self.assertTrue(self.fixture.alive)
        self.assertEqual(self.fixture.events, ['freeze', 'thaw'])

    def test_removal_during_kill_write_or_close_checks_empty(self):
        for phase in ('write', 'close'):
            for number in (errno.ENOENT, errno.ENODEV):
                with self.subTest(phase=phase, errno=number):
                    stream = mock.MagicMock()
                    error = OSError(number, 'removed')
                    if phase == 'write':
                        stream.__enter__.return_value.write.side_effect = error
                    else:
                        stream.__exit__.side_effect = error
                    with mock.patch.object(drain.os, 'open', return_value=123), \
                         mock.patch.object(drain.os, 'fdopen', return_value=stream), \
                         mock.patch.object(self.host, '_wait_event') as wait:
                        drain.Host._kill_frozen(self.host, self.group)
                    wait.assert_called_once_with(self.group, 'populated', '0', removed_ok=True)

    def test_removal_is_not_accepted_before_kill(self):
        with mock.patch('builtins.open', side_effect=OSError(errno.ENODEV, 'removed')):
            with self.assertRaises(OSError):
                self.host._wait_event(self.group, 'frozen', '1')

    def test_permission_error_does_not_mean_removed(self):
        with mock.patch('builtins.open', side_effect=PermissionError(errno.EACCES, 'denied')):
            with self.assertRaises(PermissionError):
                self.host._wait_event(self.group, 'populated', '0', removed_ok=True)

    def test_other_error_after_kill_recovers_empty_unit_and_names_it(self):
        def kill(group):
            self.fixture.kill(group)
            raise OSError('events read failed')

        self.host._kill_frozen = kill
        with mock.patch.object(self.host, '_wait_event'):
            with self.assertRaisesRegex(OSError, R1 + '.*restart completed'):
                self.host.act_if_idle('restart', R1)
        self.assertEqual(self.fixture.events, ['freeze', 'kill', 'thaw', 'restart'])

    def test_survivor_is_not_restarted_after_error(self):
        self.host._kill_frozen = mock.Mock(side_effect=OSError('kill incomplete'))

        def wait(group, key, value, removed_ok=False):
            if removed_ok:
                raise OSError('still populated')

        with mock.patch.object(self.host, '_wait_event', side_effect=wait):
            with self.assertRaisesRegex(OSError, R1 + '.*still populated'):
                self.host.act_if_idle('restart', R1)
        self.assertTrue(self.fixture.alive)
        self.assertEqual(self.fixture.events, ['freeze', 'thaw'])

    def test_thaw_error_after_kill_recovers_empty_unit(self):
        def thaw(group, value):
            if value == '0':
                raise PermissionError('thaw failed')
            self.fixture.set_freezer(group, value)

        self.host._set_freezer = thaw
        with mock.patch.object(self.host, '_wait_event'):
            with self.assertRaisesRegex(OSError, R1 + '.*restart completed'):
                self.host.act_if_idle('restart', R1)
        self.assertEqual(self.fixture.events, ['freeze', 'kill', 'restart'])

    def test_removed_during_thaw_still_stops_once(self):
        def thaw(group, value):
            if value == '0':
                raise OSError(errno.ENODEV, 'removed')
            self.fixture.set_freezer(group, value)

        self.host._set_freezer = thaw
        self.assertTrue(self.host.act_if_idle('stop', R1))
        self.assertEqual(self.fixture.events, ['freeze', 'kill', 'stop'])

    def test_error_after_kill_recovers_requested_stop(self):
        def kill(group):
            self.fixture.kill(group)
            raise OSError('events read failed')

        self.host._kill_frozen = kill
        with mock.patch.object(self.host, '_wait_event'):
            with self.assertRaisesRegex(OSError, R1 + '.*stop completed'):
                self.host.act_if_idle('stop', R1)
        self.assertEqual(self.fixture.events, ['freeze', 'kill', 'thaw', 'stop'])

    def test_restart_failure_names_unit_and_is_not_retried(self):
        self.host.act = mock.Mock(side_effect=subprocess.TimeoutExpired('systemctl', 60))
        with self.assertRaisesRegex(OSError, R1):
            self.host.act_if_idle('restart', R1)
        self.host.act.assert_called_once_with('restart', R1)
