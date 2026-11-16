# SPDX-License-Identifier: AGPL-3.0-only
"""Worker failures identify their boundary without exposing exception values."""
from contextlib import ExitStack, redirect_stdout
import io
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
