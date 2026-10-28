# SPDX-License-Identifier: AGPL-3.0-only
"""#4113 : une perte de bail arrête le paquet avant l'opération suivante."""
import argparse
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from tests.test_3807_paquet_auto import paquet_auto as auto
from ameesh import config, db, pilot_gesture


class PacketEpochTests(unittest.TestCase):
    def setUp(self):
        def child(cmd, **kwargs):
            try:
                with mock.patch('sys.argv', ['-c', cmd[3]]):
                    exec(cmd[2], {})
                return subprocess.CompletedProcess(cmd, 0, b'', b'')
            except Exception as exc:
                return subprocess.CompletedProcess(cmd, 1, b'', str(exc).encode())
        self.child = mock.patch.object(auto, 'run_command', side_effect=child)
        self.child.start()
        self.addCleanup(self.child.stop)
        python = mock.patch.object(auto, 'project_python', return_value='project-python')
        python.start()
        self.addCleanup(python.stop)

    def test_system_python_parent_uses_project_child_without_importing_project(self):
        script = Path(auto.__file__).resolve()
        code = """
import importlib.util, sys
from pathlib import Path
from unittest import mock
spec = importlib.util.spec_from_file_location('packet_auto_system', sys.argv[1])
auto = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auto
spec.loader.exec_module(auto)
with mock.patch.object(auto, 'project_python', return_value='project-python'), \
     mock.patch.object(auto, 'run_command', return_value=auto.subprocess.CompletedProcess([], 0)) as run, \
     mock.patch.dict(auto.os.environ, {'AMEESH_ROLE': 'composer'}):
    auto.check_epoch(4113, Path('.'))
assert run.call_args.args[0][0] == 'project-python'
assert run.call_args.kwargs['timeout'] == auto.EPOCH_CHECK_TIMEOUT == 150
assert 'ameesh.db' not in sys.modules
"""
        proc = subprocess.run(['/usr/bin/python3', '-S', '-c', code, str(script)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_stale_force_refused_before_branch_cleanup_or_tests(self):
        args = argparse.Namespace(repo='.', name='epoch', target='main', carrier=4113,
                                  force=True, no_packet_record=False)
        with mock.patch.object(auto, 'validate_target'), \
                mock.patch.object(auto, 'validate_branch'), \
                mock.patch.object(auto, 'check_epoch', side_effect=auto.PaquetAutoError('stale')), \
                mock.patch.object(auto, 'fresh_base') as base, \
                mock.patch.object(auto, 'clear_local_branch') as clear, \
                mock.patch.object(auto, 'run_freeze_checks') as tests:
            with self.assertRaisesRegex(auto.PaquetAutoError, 'stale'):
                auto.compose_packet(args)
        base.assert_not_called()
        clear.assert_not_called()
        tests.assert_not_called()

    def test_epoch_changes_after_one_unit_no_second_composition(self):
        guard = mock.Mock(side_effect=[None, auto.PaquetAutoError('epoch changed')])
        with mock.patch.object(auto, 'ensure_source'), \
                mock.patch.object(auto, 'rev', side_effect=['base', 'unit']), \
                mock.patch.object(auto, 'git', return_value=subprocess.CompletedProcess([], 1)), \
                mock.patch.object(auto, '_compose', return_value=subprocess.CompletedProcess([], 0)) as compose:
            with self.assertRaisesRegex(auto.PaquetAutoError, 'epoch changed'):
                auto.compose_candidates(Path('.'),
                    [auto.Candidate(1, 'a' * 40), auto.Candidate(2, 'b' * 40)],
                    4113, compose_script=Path('compose.py'), record_package=True,
                    guard=guard, log=lambda _: None)
        self.assertEqual(compose.call_count, 1)

    def test_epoch_changes_during_unit_test_no_next_test_or_contract(self):
        composition = auto.Composition(
            units=[auto.Unit(1, 'a', 'b'), auto.Unit(2, 'c', 'd')],
            discarded=[], head='d')
        guard = mock.Mock(side_effect=[None, auto.PaquetAutoError('epoch changed')])
        with mock.patch.object(auto, 'unit_test_modules', return_value=['tests.test_one']), \
                mock.patch.object(auto, 'run_tests', return_value=auto.TestRun(
                    label='unit', modules=['tests.test_one'], returncode=0,
                    seconds=0, output='Ran 1 test\nOK')) as tests, \
                mock.patch.object(auto, 'contract_modules') as contracts:
            with self.assertRaisesRegex(auto.PaquetAutoError, 'epoch changed'):
                auto.check_local_tests(Path('.'), composition, timeout=10,
                    select=lambda *_: ({}, ''), guard=guard, log=lambda _: None)
        self.assertEqual(tests.call_count, 1)
        contracts.assert_not_called()

    def test_preflight_uses_packet_gate_and_closes_connection_on_refusal(self):
        conn = mock.Mock()
        with mock.patch.dict(auto.os.environ, {'AMEESH_ROLE': 'composer'}), \
                mock.patch.object(config, 'load', return_value='cfg'), \
                mock.patch.object(db, 'connect', return_value=conn), \
                mock.patch.object(pilot_gesture, 'gate',
                    side_effect=pilot_gesture.GestureRefused('stale epoch')) as gate:
            with self.assertRaisesRegex(auto.PaquetAutoError, 'stale epoch'):
                auto.check_epoch(4113, Path('.'))
        gate.assert_called_once_with('cfg', conn, 'packet', 'packet compose #4113')
        conn.close.assert_called_once()

    def test_database_failure_is_not_ignored(self):
        with mock.patch.dict(auto.os.environ, {'AMEESH_ROLE': 'composer'}), \
                mock.patch.object(config, 'load', return_value='cfg'), \
                mock.patch.object(db, 'connect', side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(auto.PaquetAutoError, 'offline'):
                auto.check_epoch(4113, Path('.'))

    def test_stale_recomposition_preserves_existing_tree(self):
        with mock.patch.object(auto, 'git') as git:
            with self.assertRaisesRegex(auto.PaquetAutoError, 'stale'):
                auto.recompose(Path('.'), 'base', [], 4113,
                    compose_script=Path('compose.py'), record_package=True,
                    guard=mock.Mock(side_effect=auto.PaquetAutoError('stale')))
        git.assert_not_called()

    def test_live_preflight_neither_renews_nor_records_a_gesture(self):
        conn = mock.Mock()
        fence = mock.Mock()
        with mock.patch.dict(auto.os.environ, {'AMEESH_ROLE': 'composer'}), \
                mock.patch.object(config, 'load', return_value='cfg'), \
                mock.patch.object(db, 'connect', return_value=conn), \
                mock.patch.object(pilot_gesture, 'gate', return_value=fence):
            auto.check_epoch(4113, Path('.'))
        fence.hold.assert_not_called()
        conn.close.assert_called_once()
