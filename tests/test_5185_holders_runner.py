# SPDX-License-Identifier: AGPL-3.0-only
"""Unreadable holders retain the systemd discovery failure and stay guarded."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import support  # noqa: F401
from ameesh import housekeeping
from ameesh.platform import process as P


class HoldersRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.group = '/user.slice/ameesh-runner-agent@worker.service'
        root = Path(self.tmp.name, '91')
        root.mkdir()
        (root / 'status').write_text(
            'Name:\tameesh\nState:\tS\nUid:\t' +
            '\t'.join([str(os.getuid())] * 4) + '\n')
        (root / 'stat').write_text('91 (ameesh) ' + ' '.join(['S'] + ['0'] * 18 + ['1234']))
        (root / 'cgroup').write_text('0::' + self.group + '/runner\n')
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(P, 'PROC', self.tmp.name).start()
        mock.patch.object(P, '_proc_ok', return_value=True).start()
        mock.patch.object(P, 'DOUBT_RETRIES', 0).start()
        mock.patch.object(P, 'service_managers', return_value=frozenset()).start()

    def scan(self, snapshot, cwd='/outside'):
        def listing(path):
            if path == self.tmp.name:
                return ['91']
            if path == str(Path(self.tmp.name, '91', 'fd')):
                raise PermissionError('protected runner')
            return []
        with mock.patch.object(P, 'executor_processes', return_value=snapshot), \
                mock.patch.object(P.os, 'listdir', side_effect=listing), \
                mock.patch.object(P.os, 'readlink', return_value=cwd):
            return P._holders_proc(self.tmp.name, True, None, set())

    def test_verified_runner_outside_worktree_does_not_hold_it(self):
        identity = P._executor_identity(91, self.group)
        self.assertIsNotNone(identity)
        self.assertEqual(self.scan({91: identity}), [])

    def test_observed_cwd_inside_worktree_still_holds_even_for_verified_runner(self):
        identity = P._executor_identity(91, self.group)
        self.assertEqual(self.scan({91: identity}, cwd=self.tmp.name)[0]['why'],
                         'working directory')

    def test_unknown_unreadable_process_still_holds(self):
        with self.assertRaises(P.HoldersUnreadable):
            self.scan({})

    def test_missing_user_bus_is_explained_without_exempting_unknown_process(self):
        for failure in (SimpleNamespace(returncode=1, stdout=''),
                        OSError('no bus'), subprocess.TimeoutExpired('systemctl', 5)):
            def show(argv, **kwargs):
                if '--user' in argv:
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                return SimpleNamespace(returncode=0, stdout='')
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch('subprocess.run', side_effect=show):
                snapshot = P.executor_processes()
                self.assertEqual(snapshot, {})
                with self.assertRaises(P.HoldersUnreadable) as caught:
                    self.scan(snapshot)
                with mock.patch.object(housekeeping.platform, 'holders',
                                       side_effect=caught.exception):
                    held, detail = housekeeping.worktree_holders(self.tmp.name)
                self.assertTrue(held)
                self.assertIn('systemctl --user unavailable', detail)
                self.assertIn('XDG_RUNTIME_DIR', detail)
                self.assertNotIn('no bus', detail)
