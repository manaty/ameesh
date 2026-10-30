# SPDX-License-Identifier: AGPL-3.0-only
from tests import support
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('release_delegation',
        Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/delegation-release.py')
delegation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delegation)


class DelegationTest(unittest.TestCase):
    def setUp(self):
        key = struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+b'a'*32
        self.plan = dict(host='fixture-host',machine_id='a'*32,
                controller_source='192.0.2.17/32',
                controller_key='ssh-ed25519 '+base64.b64encode(key).decode(),
                host_key_sha256='b'*64,runtime_tree_sha256='c'*64,
                source_tree_sha256='d'*64,
                policy=dict(host='fixture-host',units=['ameesh-runner@fixture.service'],
                    controller_unit='ameesh-release-controller.service',manager={'kind':'system'},restart_seconds=5))

    def test_render_is_pure_deterministic_and_hashes_every_exact_file(self):
        with mock.patch('subprocess.run',side_effect=AssertionError('host execution refused')):
            bundle = delegation.render(self.plan)
        self.assertEqual(bundle,delegation.render(self.plan))
        self.assertEqual(bundle['digest'],delegation.restart.protocol.identity(
            {key:value for key,value in bundle.items() if key!='digest'}))
        for path,item in bundle['files'].items():
            self.assertEqual(item['owner'],'root')
            self.assertEqual(item['group'],'root')
            self.assertEqual(item['sha256'],hashlib.sha256(item['content'].encode()).hexdigest())
            self.assertFalse(item['mode'] & 0o022)
            self.assertEqual(bundle['revoke']['remove_only_matching_sha256'][path],item['sha256'])

    def test_sudo_grants_only_exact_isolated_dispatcher_arguments(self):
        bundle = delegation.render(self.plan)
        sudo = bundle['files']['/etc/sudoers.d/ameesh-release']['content']
        self.assertEqual([line for line in sudo.splitlines() if 'NOPASSWD:' in line],
            ['ameesh-release ALL=(root) NOPASSWD: /opt/ameesh-release/runtime/bin/python -I -B /opt/ameesh-release/lib/hote-release.py'])
        self.assertNotIn('*',sudo)
        self.assertIn('env_reset,!setenv',sudo)
        self.assertNotIn('systemctl',sudo)
        self.assertNotIn('/bin/sh',sudo)

    def test_ssh_key_and_account_force_same_wrapper_and_disable_alternate_channels(self):
        files = delegation.render(self.plan)['files']
        keys = files['/etc/ameesh-release/authorized_keys']['content']
        self.assertEqual(files['/etc/ameesh-release/authorized_keys']['mode'],0o644)
        self.assertTrue(keys.startswith('restrict,from="192.0.2.17/32",command="'+delegation.WRAPPER+'" '))
        self.assertEqual(keys.count('\n'),1)
        sshd = files['/etc/ssh/sshd_config.d/60-ameesh-release.conf']['content']
        for line in ('AuthorizedKeysCommand none','AuthenticationMethods publickey',
                     'ForceCommand '+delegation.WRAPPER,'DisableForwarding yes',
                     'PermitTTY no','PermitUserRC no','MaxSessions 1'):
            self.assertIn(line,sshd)
        self.assertTrue(sshd.endswith('Match all\n'))

    def test_wrapper_refuses_remote_shell_text_or_arguments_before_sudo(self):
        wrapper = delegation.render(self.plan)['files'][delegation.WRAPPER]['content']
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'wrapper'
            path.write_text(wrapper)
            syntax = subprocess.run(['/bin/sh','-n',str(path)],capture_output=True)
            self.assertEqual(syntax.returncode,0)
            for args,env in ((['unexpected'],{}),([],{'SSH_ORIGINAL_COMMAND':'sudo arbitrary private-canary'})):
                result = subprocess.run(['/bin/sh',str(path),*args],env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,1)
                self.assertEqual(json.loads(result.stdout),{'error':'host_action_unavailable'})
                self.assertNotIn('canary',result.stdout+result.stderr)

    def test_untrusted_key_options_comments_newlines_and_bad_wire_format_are_refused(self):
        for key in ('command="evil" '+self.plan['controller_key'],
                    self.plan['controller_key']+' comment',self.plan['controller_key']+'\nevil',
                    'ssh-rsa AAAA','ssh-ed25519 AAAA'):
            with self.subTest(key=key):
                with self.assertRaises(delegation.DelegationError):
                    delegation.render(dict(self.plan,controller_key=key))

    def test_host_scope_source_injection_and_unpinned_artifacts_are_refused(self):
        for name,value in (('host','different-host'),('machine_id','not-a-machine-id'),
                           ('controller_source','192.0.2.17/24'),('controller_source','host.example.test'),
                           ('controller_source','192.0.2.17/32";evil'),
                           ('runtime_tree_sha256','HEAD'),('source_tree_sha256',None),
                           ('host_key_sha256','new host key'),('private_key','private-canary')):
            with self.subTest(name=name):
                with self.assertRaises(delegation.DelegationError):
                    delegation.render(dict(self.plan,**{name:value}))

    def test_question_binds_all_artifacts_host_policy_and_revoke_plan(self):
        bundle = delegation.render(self.plan)
        text = delegation.question(bundle)
        for value in (bundle['digest'],self.plan['machine_id'],self.plan['host'],
                      self.plan['host_key_sha256'],bundle['policy_id']):
            self.assertIn(value,text)
        self.assertIn('No installation is authorized',text)
        for name in ('live release fences','helper journal','controller journal'):
            self.assertIn(name,text)
        for mutate in (lambda item:item.update(version=True),
                       lambda item:item['files'][delegation.WRAPPER].update(content='exec evil'),
                       lambda item:item['revoke'].update(preserve=[])):
            changed = json.loads(json.dumps(bundle))
            mutate(changed)
            with self.assertRaisesRegex(delegation.DelegationError,'changed'):
                delegation.question(changed)

    def test_policy_change_changes_bundle_and_signed_question(self):
        prior = delegation.render(self.plan)
        self.plan['policy']['restart_seconds'] = 30
        after = delegation.render(self.plan)
        self.assertNotEqual(prior['digest'],after['digest'])
        self.assertNotEqual(prior['policy_id'],after['policy_id'])
        self.assertNotEqual(delegation.question(prior),delegation.question(after))
