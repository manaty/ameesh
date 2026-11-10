# SPDX-License-Identifier: AGPL-3.0-only
"""#6786: a session without the user bus still recognises the executors."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import support  # noqa: F401
from ameesh.platform import process as P

UNIT = 'ameesh-runner-agent@worker.service'
GROUP = '/user.slice/user@1001.service/ameesh.slice/' + UNIT


class UserBusEnvTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(P.os.environ, {}, clear=True).start()

    def stat(self, runtime_mode=0o40700, bus_mode=0o140666, uid=None):
        uid = os.getuid() if uid is None else uid

        def lstat(path):
            mode = bus_mode if path.endswith('/bus') else runtime_mode
            return os.stat_result((mode, 0, 0, 1, uid, uid, 0, 0, 0, 0))
        return mock.patch.object(P.os, 'lstat', side_effect=lstat)

    def test_missing_runtime_dir_is_taken_from_logind(self):
        with self.stat():
            env = P._user_bus_env()
        self.assertEqual(env['XDG_RUNTIME_DIR'], '/run/user/%d' % os.getuid())

    def test_session_that_names_its_bus_is_left_alone(self):
        os.environ['XDG_RUNTIME_DIR'] = '/run/user/elsewhere'
        with self.stat():
            self.assertIsNone(P._user_bus_env())

    def test_runtime_dir_open_to_others_or_foreign_is_not_trusted(self):
        for kwargs in ({'runtime_mode': 0o40755}, {'uid': os.getuid() + 1},
                       {'bus_mode': 0o100666}):
            with self.subTest(**kwargs), self.stat(**kwargs):
                self.assertIsNone(P._user_bus_env())

    def test_missing_runtime_dir_adds_nothing(self):
        with mock.patch.object(P.os, 'lstat', side_effect=FileNotFoundError):
            self.assertIsNone(P._user_bus_env())


class HoldersWithoutUserBusTest(unittest.TestCase):
    """Witness of the lot: an ameesh daemon that /proc hides does not hold a
    worktree once systemd vouches for it, an unknown hidden process still does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for pid, group in (('91', GROUP), ('92', '/user.slice/unknown.scope')):
            root = Path(self.tmp.name, pid)
            root.mkdir()
            (root / 'status').write_text(
                'Name:\tameesh\nState:\tS\nPPid:\t1\nUid:\t'
                + '\t'.join([str(os.getuid())] * 4) + '\n')
            (root / 'stat').write_text(pid + ' (ameesh) ' + ' '.join(['S'] + ['0'] * 18 + ['77']))
            (root / 'cgroup').write_text('0::' + group + '\n')
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(P, 'PROC', self.tmp.name).start()
        mock.patch.object(P, '_proc_ok', return_value=True).start()
        mock.patch.object(P, 'DOUBT_RETRIES', 0).start()
        mock.patch.object(P, 'service_managers', return_value=frozenset()).start()
        mock.patch.object(P, '_user_bus_env',
                          return_value={'XDG_RUNTIME_DIR': '/run/user/1'}).start()
        self.calls = []
        mock.patch.object(subprocess, 'run', side_effect=self.systemctl).start()

    def systemctl(self, argv, **kwargs):
        self.calls.append((argv[1], kwargs.get('env')))
        if argv[1] != '--user' or not (kwargs.get('env') or {}).get('XDG_RUNTIME_DIR'):
            return subprocess.CompletedProcess(argv, 1, '', 'Failed to connect to bus')
        out = ('Id=%s\nMainPID=91\nControlGroup=%s\nTransient=no\n'
               'FragmentPath=/home/u/.config/systemd/user/ameesh-runner-agent@.service\n'
               % (UNIT, GROUP))
        return subprocess.CompletedProcess(argv, 0, out, '')

    def scan(self, pids):
        def listing(path):
            if path == self.tmp.name:
                return pids
            if path.endswith('/fd') and not path.endswith('/%d/fd' % os.getpid()):
                raise PermissionError('not dumpable')
            return []
        with mock.patch.object(P.os, 'listdir', side_effect=listing), \
                mock.patch.object(P.os, 'readlink', side_effect=PermissionError):
            return P._holders_proc(self.tmp.name, True, None, set())

    def test_hidden_ameesh_daemon_does_not_hold(self):
        self.assertEqual(self.scan(['91']), [])
        self.assertIn(('--user', {'XDG_RUNTIME_DIR': '/run/user/1'}), self.calls)
        self.assertIn(('--system', None), self.calls)

    def test_unknown_hidden_process_still_holds(self):
        with self.assertRaises(P.HoldersUnreadable) as caught:
            self.scan(['91', '92'])
        self.assertEqual(caught.exception.holders, ['92 ameesh'])


if __name__ == '__main__':
    unittest.main()
