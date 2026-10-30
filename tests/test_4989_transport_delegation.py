# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded two-key delegation; no host effects or credentials."""
import base64
import copy
import unittest
from unittest import mock

from tests import test_l232b_delegation as fixture_module

delegation=fixture_module.delegation


class LocalTransportTest(unittest.TestCase):
    def setUp(self):
        fixture=fixture_module.DelegationTest()
        fixture.setUp()
        self.plan=copy.deepcopy(fixture.plan)
        raw=base64.b64decode(self.plan['controller_key'].split()[1])
        self.local=dict(controller_source='127.0.0.1/32',
                        controller_key='ssh-ed25519 '+base64.b64encode(raw[:-32]+b'z'*32).decode())

    def test_exact_two_restricted_keys_and_signed_inventory(self):
        original=delegation.render(self.plan)
        self.plan['local_transport']=self.local
        bundle=delegation.render(self.plan)
        lines=bundle['files']['/etc/ameesh-release/authorized_keys']['content'].splitlines()
        self.assertEqual(len(lines),2)
        for line,entry in zip(lines,[self.plan,self.local]):
            self.assertEqual(line,'restrict,from="'+entry['controller_source']
                             +'",command="'+delegation.WRAPPER+'" '+entry['controller_key'])
        self.assertNotEqual(original['digest'],bundle['digest'])
        delegation.question(bundle)
        bundle['plan']['local_transport']['controller_source']='127.0.0.2/32'
        with self.assertRaises(delegation.DelegationError):
            delegation.question(bundle)

    def test_refuse_duplicate_key_broad_source_options_and_unknown_fields(self):
        for local in (dict(self.local,controller_source=self.plan['controller_source']),
                      dict(self.local,controller_key=self.plan['controller_key']),
                      dict(self.local,controller_source='127.0.0.0/8'),
                      dict(self.local,controller_source='::/0'),
                      dict(self.local,controller_key=self.local['controller_key']+' comment'),
                      dict(self.local,private_key='synthetic'),{},[self.local,self.local]):
            with self.subTest(local=local),self.assertRaises(delegation.DelegationError):
                delegation.render(dict(self.plan,local_transport=local))

    def test_ipv6_single_measured_address(self):
        bundle=delegation.render(dict(self.plan,local_transport=dict(self.local,controller_source='::1/128')))
        self.assertIn('from="::1/128"',bundle['files']['/etc/ameesh-release/authorized_keys']['content'])

    def test_bootstrap_question_names_local_source_and_key_digest(self):
        from tests import test_l232b_bootstrap_installer as installer_fixture
        bootstrap=installer_fixture.installer.bootstrap
        bundle=delegation.render(dict(self.plan,local_transport=self.local),account_ids={'uid':600,'gid':600})
        text=bootstrap.question('fixture-two-keys',bundle,expires_at=2000000000)
        self.assertIn('"local_transport":{"key_sha256":"'
                      +bootstrap.protocol.identity(self.local['controller_key'])
                      +'","source":"127.0.0.1/32"}',text)

    def test_effective_ssh_checked_for_each_source_and_second_source_can_refuse(self):
        from tests import test_l232b_bootstrap_installer as installer_fixture
        installer=installer_fixture.installer
        bundle=delegation.render(dict(self.plan,local_transport=self.local))
        host=object.__new__(installer.LinuxHost)
        output='\n'.join(['forcecommand '+delegation.WRAPPER,
            'authorizedkeysfile /etc/ameesh-release/authorized_keys','authorizedkeyscommand none',
            'authenticationmethods publickey','passwordauthentication no','kbdinteractiveauthentication no',
            'disableforwarding yes','permittty no','permituserrc no','maxsessions 1'])
        with mock.patch.object(installer.runtime,'trusted'),mock.patch.object(installer.Path,'is_file',return_value=True),mock.patch.object(host,'command',side_effect=['',output,output]) as command:
            self.assertTrue(host.validate_configuration(bundle))
            self.assertIn('addr=192.0.2.17',command.call_args_list[1].args[0][-1])
            self.assertIn('addr=127.0.0.1',command.call_args_list[2].args[0][-1])
        with mock.patch.object(installer.runtime,'trusted'),mock.patch.object(installer.Path,'is_file',return_value=True),mock.patch.object(host,'command',side_effect=['',output,'forcecommand wrong']):
            with self.assertRaises(installer.InstallError):
                host.validate_configuration(bundle)
