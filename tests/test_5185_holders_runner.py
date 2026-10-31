# SPDX-License-Identifier: AGPL-3.0-only
"""Protected self-probes use the independent Linux scan and keep unknown holders guarded."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import support  # noqa: F401
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

    def scan(self, snapshot, cwd='/outside', ps=None):
        def listing(path):
            if path == self.tmp.name:
                return ['91']
            if path == str(Path(self.tmp.name, '91', 'fd')):
                raise PermissionError('protected runner')
            return []
        with mock.patch.object(P, 'executor_processes', return_value=snapshot), \
                mock.patch.object(P.os, 'listdir', side_effect=listing), \
                mock.patch.object(P.os, 'readlink', return_value=cwd):
            if ps is None:
                return P._holders_proc(self.tmp.name, True, None, set())
            return P._holders_psutil(ps, self.tmp.name, True, None, set())

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

    def denied_psutil(self):
        class AccessDenied(Exception):
            pass
        ps = mock.Mock(AccessDenied=AccessDenied)
        ps.Process.return_value.open_files.side_effect = AccessDenied()
        return ps

    def test_self_probe_denied_falls_back_for_verified_runner_and_observed_cwd(self):
        ps = self.denied_psutil()
        identity = P._executor_identity(91, self.group)
        self.assertEqual(self.scan({91: identity}, ps=ps), [])
        self.assertEqual(self.scan({91: identity}, cwd=self.tmp.name, ps=ps)[0]['why'],
                         'working directory')

    def test_self_probe_denied_does_not_exempt_unknown_unreadable_process(self):
        with self.assertRaises(P.HoldersUnreadable):
            self.scan({}, ps=self.denied_psutil())

    def test_self_probe_denied_without_proc_stays_unavailable(self):
        with mock.patch.object(P, '_proc_ok', return_value=False):
            with self.assertRaises(P.NotAvailable):
                P._holders_psutil(self.denied_psutil(), self.tmp.name, True, None, set())

    def test_proc_self_probe_denied_stays_unavailable(self):
        with mock.patch.object(P.os, 'listdir', side_effect=PermissionError()):
            with self.assertRaises(P.NotAvailable):
                P._holders_psutil(self.denied_psutil(), self.tmp.name, True, None, set())

    def artifact_paths(self, identities, cwd=None):
        def link(path):
            if path.endswith('/cwd') and cwd is not None:
                return cwd
            raise PermissionError('protected runner')
        with mock.patch.object(P, 'executor_processes', return_value=identities), \
                mock.patch.object(P, '_zombie', return_value=False), \
                mock.patch.object(P, '_link', side_effect=link):
            return P.user_paths()

    def test_artifacts_verified_runner_does_not_block_and_observed_path_is_kept(self):
        identity = P._executor_identity(91, self.group)
        result = self.artifact_paths({91: identity}, cwd=self.tmp.name)
        self.assertEqual(result.unreadable, [])
        self.assertIn(self.tmp.name, result.paths)

    def test_artifacts_unknown_unreadable_or_reused_pid_still_blocks(self):
        identity = P._executor_identity(91, self.group)
        for snapshot in ({}, {91: ('other-start', identity[1])}):
            with self.subTest(snapshot=snapshot):
                self.assertTrue(self.artifact_paths(snapshot).unreadable)
