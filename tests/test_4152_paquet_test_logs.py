# SPDX-License-Identifier: AGPL-3.0-only
"""#4152 : preuve complète conservée après suppression de l'arbre de tests."""
import os
import pathlib
import subprocess
import shutil
import tempfile
import types
import unittest
from unittest import mock

from .test_3807_paquet_auto import paquet_auto


class SeriesLogsTest(unittest.TestCase):
    def test_traceback_private_retained_and_linked_after_worktree_removed(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as tree:
            output = 'ERROR: test_db\n' + 'contexte\n' * 100 + 'psql: error: no password supplied\n'
            with mock.patch('ameesh.config.load', return_value=types.SimpleNamespace(state_dir=state)), \
                    mock.patch.object(paquet_auto, 'project_python', return_value='python'), \
                    mock.patch.object(paquet_auto, 'run_command', return_value=subprocess.CompletedProcess([], 1, b'', output.encode())):
                run = paquet_auto.run_tests(pathlib.Path(tree), 'unité #4097', ['tests.test_db'], timeout=30)
            shutil.rmtree(tree)
            path = pathlib.Path(run.log_path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn(output.strip(), path.read_text())
            self.assertIn(str(path), '\n'.join(paquet_auto._inconclusive_lines([run])))
            self.assertNotIn(tree, str(path))

    def test_rotation_keeps_recent_logs_and_removes_expired(self):
        with tempfile.TemporaryDirectory() as state:
            root = pathlib.Path(state) / 'roles/mesh-design/test-logs'
            root.mkdir(parents=True)
            old = root / 'series-old.log'
            old.write_text('old')
            os.utime(old, (1, 1))
            recent = root / 'series-recent.log'
            recent.write_text('recent')
            with mock.patch('ameesh.config.load', return_value=types.SimpleNamespace(state_dir=state)):
                run = paquet_auto.save_test_log(paquet_auto.TestRun('contrats', [], 0, 'OK', 1))
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(pathlib.Path(run.log_path).exists())
