# SPDX-License-Identifier: AGPL-3.0-only
"""#5223 — l'enfant non-dumpable d'un service ameesh vérifié ne bloque plus le ménage.

Constat (auditeur, 01/11 02:56Z, agents-2) : les minuteries ameesh-role-*
tirent chaque minute ; `ameesh role run` est le MainPID approuvé par #5185,
mais son enfant `ameesh notify --once` se rend non-dumpable (#5083) sans être
MainPID : `/proc/<pid>/fd` refusé, doute, et `worktree_holders` gardait les
61 worktrees de lots finis. L'enfant direct, dans le cgroup exact de l'unité
et sous l'interpréteur de son parent, n'est plus un doute ; un petit-enfant,
un autre cgroup (les tours d'un exécuteur, `<unité>/runner`), un autre
programme ou un parent non vérifié le restent.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import support  # noqa: F401
from ameesh.platform import process as P

PYTHON = '/home/ameesh/.local/share/ameesh/venv-x/bin/python3'


class NotifyChildHolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.group = '/user.slice/user-1001.slice/user@1001.service/app.slice/ameesh-role-issues.service'
        self.pids = []
        # 91 : `ameesh role run`, MainPID de l'unité ; 92 : son `notify --once`
        self.proc('91', ppid='1', cgroup=self.group,
                  argv=[PYTHON, '/venv/bin/ameesh', 'role', 'run', 'mesh-design'])
        self.proc('92', ppid='91', cgroup=self.group,
                  argv=[PYTHON, '/venv/bin/ameesh', 'notify', '--once', '--only', 'issues'])
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(P, 'PROC', self.tmp.name).start()
        mock.patch.object(P, '_proc_ok', return_value=True).start()
        mock.patch.object(P, 'DOUBT_RETRIES', 0).start()
        mock.patch.object(P, 'service_managers', return_value=frozenset()).start()

    def proc(self, pid, *, ppid, cgroup, argv):
        root = Path(self.tmp.name, pid)
        root.mkdir(exist_ok=True)
        (root / 'status').write_text(
            'Name:\tameesh\nState:\tS\nPPid:\t%s\nUid:\t%s\n'
            % (ppid, '\t'.join([str(os.getuid())] * 4)))
        (root / 'stat').write_text('%s (ameesh) ' % pid
                                   + ' '.join(['S'] + ['0'] * 18 + ['1234']))
        (root / 'cgroup').write_text('0::' + cgroup + '\n')
        (root / 'comm').write_text('ameesh\n')
        (root / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')
        if pid not in self.pids:
            self.pids.append(pid)

    def snapshot(self):
        identity = P._executor_identity(91, self.group)
        self.assertIsNotNone(identity)
        return {91: identity}

    def scan(self, snapshot):
        def listing(path):
            if path == self.tmp.name:
                return list(self.pids)
            if path in {str(Path(self.tmp.name, pid, 'fd')) for pid in self.pids}:
                raise PermissionError('non-dumpable')
            return []
        with mock.patch.object(P, 'executor_processes', return_value=snapshot), \
                mock.patch.object(P.os, 'listdir', side_effect=listing), \
                mock.patch.object(P.os, 'readlink', side_effect=PermissionError()):
            return P._holders_proc(self.tmp.name, True, None, set())

    def test_enfant_notify_du_service_verifie_n_est_pas_un_doute(self):
        self.assertEqual(self.scan(self.snapshot()), [])

    def test_parent_non_verifie_reste_un_doute(self):
        with self.assertRaises(P.HoldersUnreadable) as caught:
            self.scan({})
        self.assertIn('92 ameesh', str(caught.exception.holders))

    def test_petit_enfant_reste_un_doute(self):
        # une commande lancée par un tour (enfant de l'enfant), même cgroup
        self.proc('93', ppid='92', cgroup=self.group,
                  argv=[PYTHON, '/venv/bin/ameesh', 'notify', '--once'])
        with self.assertRaises(P.HoldersUnreadable) as caught:
            self.scan(self.snapshot())
        self.assertEqual(caught.exception.holders, ['93 ameesh'])

    def test_autre_cgroup_reste_un_doute(self):
        # l'enfant d'un exécuteur vit dans `<unité>/runner`, avec les tours
        self.proc('92', ppid='91', cgroup=self.group + '/runner',
                  argv=[PYTHON, '/venv/bin/ameesh', 'notify', '--once'])
        with self.assertRaises(P.HoldersUnreadable):
            self.scan(self.snapshot())

    def test_autre_programme_reste_un_doute(self):
        self.proc('92', ppid='91', cgroup=self.group, argv=['/usr/bin/autre', '--x'])
        with self.assertRaises(P.HoldersUnreadable):
            self.scan(self.snapshot())

    def test_pid_parent_reutilise_reste_un_doute(self):
        identity = P._executor_identity(91, self.group)
        with self.assertRaises(P.HoldersUnreadable):
            self.scan({91: ('autre-debut', identity[1])})

    def artifact_unreadable(self, snapshot):
        with mock.patch.object(P, 'executor_processes', return_value=snapshot), \
                mock.patch.object(P, '_zombie', return_value=False), \
                mock.patch.object(P, '_link', side_effect=PermissionError('non-dumpable')), \
                mock.patch.object(P.os, 'lstat', return_value=mock.Mock(st_uid=os.getuid())), \
                mock.patch.object(P.os, 'listdir', return_value=list(self.pids)):
            return P.user_paths().unreadable

    def test_artefacts_meme_regle(self):
        self.assertEqual(self.artifact_unreadable(self.snapshot()), [])
        self.assertEqual(sorted(self.artifact_unreadable({})), ['91 ameesh', '92 ameesh'])


if __name__ == '__main__':
    unittest.main()
