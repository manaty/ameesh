# SPDX-License-Identifier: AGPL-3.0-only
"""Refusals identify failing boundaries without emitting private inputs."""
import contextlib
import errno
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('restore_diagnostic',
    Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/restauration-release.py')
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)
SECRET = 'postgresql://owner:CANARY_PRIVATE@host/db'


class DiagnosticTest(unittest.TestCase):
    def cli_failure(self, action, error):
        args = ['restore', action]
        if action == 'install':
            args += ['--pg-version', '17', '--backup', '/etc/private/backup.json',
                     '--python', '/usr/bin/python3', '--restore-role', 'restore_test']
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', args), mock.patch.object(producer.os, 'geteuid', return_value=0), \
                mock.patch.object(producer, action, side_effect=error), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as caught:
                producer.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertNotIn(SECRET, output.getvalue())
        self.assertNotIn('Traceback', output.getvalue())
        return output.getvalue()

    def test_actions_keep_failure_status_and_mask_external_text(self):
        for action in ('probe', 'install', 'remove'):
            with self.subTest(action=action):
                text = self.cli_failure(action, RuntimeError(SECRET))
                self.assertIn('step=' + action, text)
                self.assertIn('RuntimeError: unexpected failure', text)

    def test_os_error_uses_errno_without_filename_or_strerror_payload(self):
        error = PermissionError(errno.EACCES, SECRET, SECRET)
        text = self.cli_failure('probe', error)
        self.assertIn('PermissionError: os error 13: Permission denied', text)

    def test_json_error_omits_document_and_message(self):
        text = self.cli_failure('probe', json.JSONDecodeError(SECRET, SECRET, 2))
        self.assertIn('invalid JSON at line 1 column 3', text)

    def test_command_failure_preserves_class_and_status_without_output_or_argv(self):
        error = subprocess.CalledProcessError(7, [SECRET], output=SECRET, stderr=SECRET)
        with mock.patch.object(producer.subprocess, 'run', side_effect=error):
            try:
                with producer.diagnostic('inspect-cluster'):
                    producer.command([SECRET])
            except producer.RestoreError as caught:
                text = self.cli_failure('probe', caught)
        self.assertIn('step=probe/inspect-cluster', text)
        self.assertIn('CalledProcessError: maintenance command exited with status 7', text)

    def test_local_guard_reason_and_nested_document_survive(self):
        try:
            with producer.diagnostic('backup-enrollment', '/etc/private/backup.json'):
                with producer.diagnostic('read-document', '/etc/private/backup.json'):
                    raise producer.RestoreError('private root document required')
        except producer.RestoreError as error:
            text = self.cli_failure('probe', error)
        self.assertIn('step=probe/backup-enrollment/read-document', text)
        self.assertIn('document="/etc/private/backup.json"', text)
        self.assertIn('RestoreError: private root document required', text)

    def test_invalid_reference_is_not_printed(self):
        for reference in (SECRET, '/tmp/' + SECRET):
            with self.subTest(reference=reference):
                try:
                    with producer.diagnostic('read-document', reference):
                        raise ValueError(SECRET)
                except ValueError as error:
                    text = self.cli_failure('probe', error)
                self.assertIn('<invalid path>', text)

    def test_probe_reaches_read_boundary_and_never_publishes_after_failure(self):
        with mock.patch.dict(sys.modules, {'psycopg.conninfo': mock.Mock()}), \
                mock.patch.object(producer, 'read_document', side_effect=FileNotFoundError(errno.ENOENT, SECRET)), \
                mock.patch.object(producer, 'atomic') as write:
            with self.assertRaises(FileNotFoundError) as caught:
                producer.probe()
        text = producer.refusal(caught.exception)
        self.assertIn('step=probe-enrollment', text)
        self.assertIn(str(producer.ENROLLMENT), text)
        self.assertNotIn(SECRET, text)
        write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
