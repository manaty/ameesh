# SPDX-License-Identifier: AGPL-3.0-only
"""#3320 — an agent host runs without a GnuPG agent.

After a successful `vm.sh agents installer`, an empty or absent
`~/.gnupg/private-keys-v1.d` masks the user GnuPG sockets and kills
gpg-agent; a private key, or an unreadable state, leaves everything alone and
logs the doubt. The step is idempotent and never touches the owner's
workstation (`poste.sh`).
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from tests.test_1252_vm_agents_install import VmAgentsHarness

ROOT = Path(__file__).resolve().parents[1]
POSTE = ROOT / 'deploy/mise-a-jour/poste.sh'

SOCKETS = ('gpg-agent.socket', 'gpg-agent-ssh.socket', 'gpg-agent-extra.socket',
           'gpg-agent-browser.socket', 'keyboxd.socket')


class AgentGpgTest(VmAgentsHarness):
    """The GnuPG step of `installer`: after the code, only when no key."""

    def private_keys(self):
        return self.vmhome / '.gnupg/private-keys-v1.d'

    def systemctl_log(self):
        path = self.state / 'systemctl.log'
        return path.read_text() if path.exists() else ''

    def gpgconf_calls(self):
        path = self.state / 'gpgconf.log'
        return path.read_text().splitlines() if path.exists() else []

    def assert_masked(self, p):
        lines = [line for line in self.systemctl_log().splitlines()
                 if line.startswith('--user mask --now ')]
        self.assertEqual(len(lines), 1, self.systemctl_log())
        self.assertEqual(lines[0].split()[3:], list(SOCKETS))
        self.assertEqual(self.gpgconf_calls(), ['--kill gpg-agent'])
        self.assertIn('sockets GnuPG masqués', p.stdout)

    def test_absent_private_key_directory_masks_and_kills(self):
        self.assertFalse(self.private_keys().exists())
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assert_masked(p)

    def test_empty_private_key_directory_masks_and_kills(self):
        self.private_keys().mkdir(parents=True)
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assert_masked(p)

    def test_a_private_key_leaves_the_agent_and_sockets_alone(self):
        self.private_keys().mkdir(parents=True)
        (self.private_keys() / 'key-1.key').write_text('private')
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('mask', self.systemctl_log())
        self.assertEqual(self.gpgconf_calls(), [])
        self.assertIn('clé privée', p.stdout)
        self.assertIn('laissés en place', p.stdout)
        # L'installation reste complète : le code est en place et le retour
        # possible.
        self.assertEqual(self.head(self.src), self.target)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)

    def test_an_unexpected_private_key_path_keeps_the_doubt(self):
        self.private_keys().parent.mkdir(parents=True)
        self.private_keys().write_text('not a directory')
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('mask', self.systemctl_log())
        self.assertEqual(self.gpgconf_calls(), [])
        self.assertIn('doute conservé', p.stderr)

    def test_a_gnupg_home_that_is_not_a_directory_keeps_the_doubt(self):
        (self.vmhome / '.gnupg').write_text('not a directory')
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('mask', self.systemctl_log())
        self.assertEqual(self.gpgconf_calls(), [])
        self.assertIn('doute conservé', p.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root ignore le bit de traversée")
    def test_an_unreadable_gnupg_home_keeps_the_doubt(self):
        # #3320 B1 : ~/.gnupg en 000 avec une clé dessous. « test -e » et
        # « test -L » échouent en EACCES et feraient conclure « absent ».
        self.private_keys().mkdir(parents=True)
        (self.private_keys() / 'secret.key').write_text('private')
        gnupg = self.private_keys().parent
        gnupg.chmod(0o000)
        self.addCleanup(gnupg.chmod, 0o700)
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('mask', self.systemctl_log())
        self.assertEqual(self.gpgconf_calls(), [])
        self.assertIn('doute conservé', p.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root ignore le bit de traversée")
    def test_an_unreadable_private_keys_directory_keeps_the_doubt(self):
        self.private_keys().mkdir(parents=True)
        (self.private_keys() / 'secret.key').write_text('private')
        self.private_keys().chmod(0o000)
        self.addCleanup(self.private_keys().chmod, 0o700)
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn('mask', self.systemctl_log())
        self.assertEqual(self.gpgconf_calls(), [])
        self.assertIn('doute conservé', p.stderr)

    def test_mask_runs_after_a_successful_installation_only(self):
        (self.state / 'pip-fail-at').write_text('1')
        p = self.run_vm('installer', self.target)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn('mask', self.systemctl_log())
        self.assertEqual(self.gpgconf_calls(), [])

    def test_mask_follows_the_last_pip(self):
        p = self.run_vm('installer', self.target)
        self.assertEqual(p.returncode, 0, p.stderr)
        events = (self.state / 'events.log').read_text().split()
        self.assertIn('pip', events)
        self.assertEqual(events.count('mask'), 1, events)
        self.assertLess(max(i for i, event in enumerate(events) if event == 'pip'),
                        events.index('mask'))

    def test_relaunching_install_replays_the_step_idempotently(self):
        first = self.run_vm('installer', self.target)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_vm('installer', self.target)
        self.assertEqual(second.returncode, 0, second.stderr)
        lines = [line for line in self.systemctl_log().splitlines()
                 if line.startswith('--user mask --now ')]
        self.assertEqual(len(lines), 2, self.systemctl_log())
        self.assertEqual(self.gpgconf_calls(), ['--kill gpg-agent'] * 2)
        self.assertEqual((self.parts / 'previous-sha').read_text().strip(), self.old)
        self.assertIn('code ' + self.target[:9] + ' déjà installé', second.stdout)

    def test_poste_sh_never_masks_gnupg_sockets(self):
        # pc-smichea est le poste de l'owner : son gpg-agent tient ses clés.
        self.assertNotIn('private-keys-v1.d', POSTE.read_text())
        self.assertNotIn('gpgconf --kill', POSTE.read_text())
