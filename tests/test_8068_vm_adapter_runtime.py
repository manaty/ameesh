# SPDX-License-Identifier: AGPL-3.0-only
"""The VM adapter hook uses the scanner's runtime on every host layout."""
import types
import unittest
from unittest.mock import patch

from ameesh import host_release


class RuntimeHookTest(unittest.TestCase):
    def test_server_runtime_and_arguments(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return types.SimpleNamespace(returncode=75, stdout='', stderr='')

        with patch.object(host_release.sys, 'executable', '/opt/ameesh/venv/bin/python'):
            result = host_release.run_hook(
                types.SimpleNamespace(lifecycle_hook_timeout=10),
                '@ameesh-python -m ameesh.vm_adapter request',
                'draining', 'vm-1', generation=7, runner=runner)
        self.assertEqual(calls[0][0], ['/opt/ameesh/venv/bin/python', '-m',
                                      'ameesh.vm_adapter', 'request', 'draining', 'vm-1'])
        self.assertEqual(calls[0][1]['env']['AMEESH_HOST_GENERATION'], '7')
        self.assertEqual(result['code'], 75)

    def test_other_commands_are_unchanged(self):
        self.assertEqual(host_release._argv('/usr/bin/python3 -m example'),
                         ['/usr/bin/python3', '-m', 'example'])
