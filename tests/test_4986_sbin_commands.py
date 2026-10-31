# SPDX-License-Identifier: AGPL-3.0-only
"""Linux administrative tools must work when only /usr/sbin provides them."""
import ast
from pathlib import Path
import unittest
from unittest import mock

from tests.test_l232b_bootstrap_installer import installer


class SbinCommandsTest(unittest.TestCase):
    def host(self):
        with mock.patch.object(installer.account.os, 'geteuid', return_value=0):
            return installer.LinuxHost()

    def test_validate_with_visudo_only_in_sbin(self):
        host = self.host()
        calls = []
        effective = '\n'.join([
            'forcecommand ' + installer.bootstrap.delegation.WRAPPER,
            'authorizedkeysfile /etc/ameesh-release/authorized_keys',
            'authorizedkeyscommand none', 'authenticationmethods publickey',
            'passwordauthentication no', 'kbdinteractiveauthentication no',
            'disableforwarding yes', 'permittty no', 'permituserrc no',
            'maxsessions 1',
        ])

        def command(args):
            calls.append(args)
            self.assertIn(args[0], ('/usr/sbin/visudo', '/usr/sbin/sshd'))
            return '' if args[0].endswith('visudo') else effective

        host.command = command
        with mock.patch.object(installer.Path, 'is_file',
                               autospec=True,
                               side_effect=lambda path: str(path).startswith('/usr/sbin/')), \
                mock.patch.object(installer.runtime, 'trusted') as trusted:
            self.assertTrue(host.validate_configuration(dict(
                host='fixture-host', plan=dict(
                    controller_source='192.0.2.1/32',
                    controller_key='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFh'))))
            self.assertTrue(host.validate_restored_configuration({}))
        self.assertEqual(calls[0], ['/usr/sbin/visudo', '-c', '-f',
                                    '/etc/sudoers.d/ameesh-release'])
        self.assertIn(['/usr/sbin/visudo', '-c'], calls)
        trusted.assert_any_call('/usr/sbin/visudo')

    def test_untrusted_visudo_is_not_executed(self):
        host = self.host()
        host.command = mock.Mock()
        with mock.patch.object(installer.runtime, 'trusted', side_effect=ValueError('untrusted')):
            with self.assertRaises(ValueError):
                host.validate_configuration({})
        host.command.assert_not_called()

    def test_no_fixed_bin_invocation_of_administrative_tools(self):
        tools = {'visudo', 'useradd', 'usermod', 'userdel', 'groupadd',
                 'groupmod', 'groupdel', 'runuser', 'sshd'}
        root = Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour'
        violations = []
        for path in sorted(root.glob('*.py')):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                # Fixed argv lists must use the portable sbin path. Candidate
                # tuples resolving sshd in both directories remain permitted.
                if (isinstance(node, ast.List) and node.elts
                        and isinstance(node.elts[0], ast.Constant)
                        and node.elts[0].value in {'/usr/bin/' + tool for tool in tools}):
                    violations.append(f'{path.name}:{node.lineno}: {node.elts[0].value}')
        self.assertEqual(violations, [])
