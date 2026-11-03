# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded dump failure cleans private partials without exposing diagnostics."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('backup5681', ROOT / 'deploy/mise-a-jour/sauvegarder-prive.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class DumpTimeoutTest(unittest.TestCase):
    def test_invalid_timeout_refused_before_config_or_process(self):
        for value in (0, -1, 3601, True, 1.5, '1.5', 'secret', '0'):
            with self.subTest(value=value), mock.patch.object(backup, 'parameters_from') as config:
                with self.assertRaises(backup.BackupError):
                    backup.dump('absent', 'absent', timeout=value)
                config.assert_not_called()
        self.assertEqual(backup.dump_timeout('3600'), 3600)
        self.assertEqual(backup.dump_timeout(1), 1)

    def test_timeout_removes_partial_and_never_publishes_or_leaks(self):
        @contextmanager
        def connection(parameters, private):
            yield 'public-dsn', {}

        def child(args, **kwargs):
            self.assertEqual(kwargs['timeout'], 3600)
            Path(args[-1]).write_bytes(b'partial')
            raise subprocess.TimeoutExpired(args, 3600, stderr=b'SECRET-DSN')

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'archive.dump'
            with mock.patch.object(backup, 'private_connection', connection), \
                    mock.patch.object(backup, 'client_major', return_value=17), \
                    mock.patch.object(backup.subprocess, 'run', side_effect=child):
                with self.assertRaisesRegex(backup.BackupError, '3600 s; 7 bytes written') as error:
                    backup.dump_parameters({}, output, source_major=17)
            self.assertNotIn('SECRET', str(error.exception))
            self.assertEqual(list(Path(directory).iterdir()), [])
