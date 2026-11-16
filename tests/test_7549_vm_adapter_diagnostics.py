# SPDX-License-Identifier: AGPL-3.0-only
"""Worker failures identify their boundary without exposing exception values."""
from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ameesh import vm_adapter as vm


class WorkerDiagnosticsTest(unittest.TestCase):
    def run_worker(self, failing=None, error=None, credentials=None, missing_env=False):
        output = io.StringIO()
        database = Mock()
        policy = {'io_timeout_s': 1, 'targets': {'first': {}, 'second': {}}}
        patches = {
            'config.load': Mock(return_value={}),
            'db_mod.connect': Mock(return_value=database),
            'enrollment': Mock(return_value=policy),
            'private_json': Mock(side_effect=[{}, credentials or {'secret_key': 'private-token'}]),
            'Scaleway': Mock(),
            'tick_host': Mock(return_value='inactive'),
        }
        if failing:
            patches[failing].side_effect = error
        with ExitStack() as stack:
            stack.enter_context(patch.dict(vm.os.environ, {'CREDENTIALS_DIRECTORY': '/private'}))
            if missing_env:
                vm.os.environ.pop('CREDENTIALS_DIRECTORY', None)
            for name, replacement in patches.items():
                stack.enter_context(patch('ameesh.vm_adapter.' + name, replacement))
            stack.enter_context(redirect_stdout(output))
            code = vm.main(['worker', '--enrollment', '/private/enrollment'])
        return code, output.getvalue(), patches, database

    def test_each_startup_boundary_reports_class_and_never_ticks(self):
        for failing, stage in [('config.load', 'config'), ('db_mod.connect', 'base'),
                               ('enrollment', 'enrollment'), ('private_json', 'enrollment'),
                               ('Scaleway', 'provider')]:
            with self.subTest(stage=stage, failing=failing):
                code, output, mocks, database = self.run_worker(
                    failing, RuntimeError('password=secret postgres://user:secret@host'))
                self.assertEqual(code, 1)
                self.assertIn('at ' + stage + ': RuntimeError;', output)
                self.assertNotIn('secret', output)
                self.assertNotIn('postgres://', output)
                mocks['tick_host'].assert_not_called()
                mocks['Scaleway'].return_value.action.assert_not_called()
                if stage != 'config' and failing != 'db_mod.connect':
                    database.close.assert_called_once()

    def test_credentials_read_failure_and_fixed_refusal(self):
        for error, expected in [(OSError('private-token'), 'OSError'),
                                (vm.Refused('invalid private JSON'), 'invalid private JSON')]:
            code, output, mocks, _ = self.run_worker(
                'private_json', [ {}, error ])
            self.assertEqual(code, 1)
            self.assertIn('at credentials: ' + expected + ';', output)
            self.assertNotIn('private-token', output)
            mocks['Scaleway'].assert_not_called()
            mocks['tick_host'].assert_not_called()

    def test_missing_credential_key_is_named_without_values(self):
        code, output, mocks, _ = self.run_worker(credentials={'other': 'private-token'})
        self.assertEqual(code, 1)
        self.assertIn('at provider: KeyError (missing key: secret_key)', output)
        self.assertNotIn('private-token', output)
        mocks['tick_host'].assert_not_called()

    def test_tick_failure_keeps_loop_running_and_hides_payload(self):
        code, output, mocks, _ = self.run_worker(
            'tick_host', [KeyError('password=private-token'), 'inactive'])
        self.assertEqual(code, 0)
        self.assertIn('first: adapter unavailable at tick: KeyError; claims remain closed', output)
        self.assertIn('second: inactive', output)
        self.assertNotIn('private-token', output)
        self.assertEqual(mocks['tick_host'].call_count, 2)

    def test_tick_fixed_refusal_is_named(self):
        code, output, _, _ = self.run_worker('tick_host', vm.Refused('provider identity mismatch'))
        self.assertEqual(code, 0)
        self.assertIn('at tick: provider identity mismatch;', output)

    def test_missing_environment_key_is_named(self):
        code, output, mocks, _ = self.run_worker(missing_env=True)
        self.assertEqual(code, 1)
        self.assertIn('at credentials: KeyError (missing key: CREDENTIALS_DIRECTORY)', output)
        mocks['Scaleway'].assert_not_called()
        mocks['tick_host'].assert_not_called()


class PrivateJsonDiagnosticsTest(unittest.TestCase):
    def test_each_refused_condition_names_cause_without_values(self):
        cases = [
            ({'st_mode': stat.S_IFIFO | 0o600}, 'must be regular'),
            ({'st_uid': os.geteuid() + 1}, 'uid must match effective uid'),
            ({'st_mode': stat.S_IFREG | 0o640}, 'group or other permission bits'),
            ({'st_mode': stat.S_IFREG | 0o604}, 'group or other permission bits'),
            ({'st_nlink': 2}, 'link count must be one'),
            ({'st_size': 65537}, 'size exceeds limit'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private-secret-path'
            path.write_text('{"secret_key": "private-token"}')
            path.chmod(0o600)
            for changes, expected in cases:
                metadata = dict(st_mode=stat.S_IFREG | 0o600, st_uid=os.geteuid(),
                                st_nlink=1, st_size=32)
                metadata.update(changes)
                with self.subTest(changes=changes), patch.object(
                        vm.os, 'fstat', return_value=SimpleNamespace(**metadata)):
                    with self.assertRaises(vm.Refused) as caught:
                        vm.private_json(path)
                    self.assertIn(expected, str(caught.exception))
                    self.assertNotIn('private-token', str(caught.exception))
                    self.assertNotIn(str(path), str(caught.exception))

    def test_worker_identifies_file_and_condition(self):
        for stage in ('enrollment', 'credentials'):
            error = vm.Refused('private file uid must match effective uid')
            failures = error if stage == 'enrollment' else [{}, error]
            code, output, mocks, _ = WorkerDiagnosticsTest().run_worker(
                'private_json', failures)
            self.assertEqual(code, 1)
            self.assertIn('at ' + stage + ': private file uid must match effective uid;', output)
            mocks['tick_host'].assert_not_called()
