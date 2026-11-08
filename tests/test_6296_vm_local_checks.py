# SPDX-License-Identifier: AGPL-3.0-only
"""VM operator SSH policy: syntax and effective restrictions."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'deploy/vm-adapter/remote/sshd-match.example'


class SshPolicyTest(unittest.TestCase):
    def test_restrictions_are_in_the_match_but_user_environment_is_global(self):
        text = TEMPLATE.read_text()
        self.assertNotIn('PermitUserEnvironment', text)
        for directive in ('AllowAgentForwarding no', 'AllowStreamLocalForwarding no',
                          'AllowTcpForwarding no', 'PermitTunnel no', 'PermitListen none'):
            self.assertIn('    ' + directive + '\n', text)

    def test_rendered_configuration_parses_and_disables_forwarding(self):
        sshd = shutil.which('sshd')
        keygen = shutil.which('ssh-keygen')
        if not sshd or not keygen:
            self.skipTest('qualification sshd reportée : sshd/ssh-keygen absent')
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as directory:
            key = Path(directory) / 'hostkey'
            subprocess.run([keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                           check=True, capture_output=True, timeout=5)
            config = Path(directory) / 'sshd_config'
            config.write_text('HostKey %s\nPermitUserEnvironment no\n' % key
                              + TEMPLATE.read_text())
            parsed = subprocess.run([sshd, '-t', '-f', str(config)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            effective = subprocess.run([
                sshd, '-T', '-f', str(config), '-C',
                'user=vm-operator,host=localhost,addr=127.0.0.1'],
                capture_output=True, text=True, timeout=5)
            self.assertEqual(effective.returncode, 0, effective.stderr)
            lines = effective.stdout.splitlines()
            for expected in ('permituserenvironment no', 'allowagentforwarding no',
                             'allowstreamlocalforwarding no', 'allowtcpforwarding no',
                             'permittunnel no', 'permitlisten none'):
                self.assertIn(expected, lines)
