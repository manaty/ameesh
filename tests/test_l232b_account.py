# SPDX-License-Identifier: AGPL-3.0-only
from tests import support
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('release_account',
        Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/compte-release.py')
account = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(account)


class Authority:
    allowed=True
    def validate(self,operation,digest,step):
        return self.allowed and step=='account'


class Accounts:
    machine_id='a'*32
    available=True
    def __init__(self):
        self.group_value,self.account_value = None,None
        self.calls=[]
        self.lost=set()
    def machine(self): return self.machine_id
    def free(self,uid,gid): return self.available
    def group(self): return self.group_value
    def account(self): return self.account_value
    def create_group(self,gid):
        self.calls.append('group')
        self.group_value=dict(name=account.NAME,gid=gid,members=[])
        if 'group' in self.lost:
            self.lost.remove('group'); raise account.AccountError('simulated lost group response')
    def create_account(self,uid,gid):
        self.calls.append('user')
        self.account_value=dict(name=account.NAME,uid=uid,gid=gid,home=account.HOME,
            shell='/bin/sh',password_disabled=True,supplementary_groups=[])
        if 'user' in self.lost:
            self.lost.remove('user'); raise account.AccountError('simulated lost user response')


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'journal'
        self.system,self.authority=Accounts(),Authority()
        self.request=dict(operation='bootstrap-fixture',machine_id='a'*32,uid=987,gid=987,
                          scope_digest=account.scope_digest('bootstrap-fixture','a'*32,987,987))
    def ensure(self,request=None):
        return account.ensure(self.root,request or self.request,authority=self.authority,system=self.system)

    def test_success_and_replay_create_one_exact_account_without_home_mutation(self):
        receipt=self.ensure()
        self.assertEqual(self.ensure(),receipt)
        self.assertEqual(self.system.calls,['group','user'])
        self.assertEqual(receipt['account']['home'],'/var/lib/ameesh-release/ssh')
        self.assertEqual(receipt['account']['supplementary_groups'],[])
        self.assertTrue(receipt['account']['password_disabled'])

    def test_lost_group_and_account_response_reconcile_without_repeat(self):
        for step in ('group','user'):
            with self.subTest(step=step),tempfile.TemporaryDirectory() as temp:
                system=Accounts(); system.lost.add(step)
                def ensure(): return account.ensure(Path(temp)/'journal',self.request,authority=self.authority,system=system)
                with self.assertRaises(account.AccountError): ensure()
                self.assertTrue(ensure()['ok'])
                self.assertEqual(system.calls,['group','user'])

    def test_failed_receipt_save_reconciles_created_identity(self):
        original=account.protocol.save
        def lost(path,record):
            if record['phase']=='complete': raise OSError('simulated lost journal write')
            return original(path,record)
        with mock.patch.object(account.protocol,'save',side_effect=lost):
            with self.assertRaises(OSError): self.ensure()
        self.assertTrue(self.ensure()['ok'])
        self.assertEqual(self.system.calls,['group','user'])

    def test_missing_identity_after_uncertain_command_is_not_recreated(self):
        for step in ('group','user'):
            with self.subTest(step=step),tempfile.TemporaryDirectory() as temp:
                system=Accounts(); system.lost.add(step)
                def ensure(): return account.ensure(Path(temp)/'journal',self.request,authority=self.authority,system=system)
                with self.assertRaises(account.AccountError): ensure()
                if step=='group': system.group_value=None
                else: system.account_value=None
                before=list(system.calls)
                with self.assertRaisesRegex(account.AccountError,'unresolved'): ensure()
                self.assertEqual(system.calls,before)

    def test_unowned_account_group_or_taken_numeric_identity_is_refused(self):
        for name,value in (('account_value',{'name':'unowned'}),('group_value',{'name':'unowned'}),('available',False)):
            system=Accounts(); setattr(system,name,value)
            with self.assertRaisesRegex(account.AccountError,'unowned'):
                account.ensure(self.root,self.request,authority=self.authority,system=system)
            self.assertEqual(system.calls,[])

    def test_changed_uid_shell_groups_and_password_state_block_replay(self):
        self.ensure()
        original=dict(self.system.account_value)
        for name,value in (('uid',1234),('shell','/bin/bash'),('supplementary_groups',[0]),('password_disabled',False)):
            self.system.account_value=dict(original,**{name:value})
            with self.assertRaisesRegex(account.AccountError,'identity changed'): self.ensure()
        self.assertEqual(self.system.calls,['group','user'])

    def test_host_change_revocation_and_bad_enrollment_prevent_all_creation(self):
        self.system.machine_id='c'*32
        with self.assertRaisesRegex(account.AccountError,'authority'): self.ensure()
        self.system.machine_id='a'*32; self.authority.allowed=False
        with self.assertRaisesRegex(account.AccountError,'authority'): self.ensure()
        self.authority.allowed=True
        for name,value in (('uid',0),('gid',True),('uid',60001),('operation','../escape'),('shell','/bin/bash')):
            with self.assertRaises(account.AccountError): self.ensure(dict(self.request,**{name:value}))
        self.assertEqual(self.system.calls,[])

    def test_revocation_after_group_observation_stops_before_user_creation(self):
        original=self.system.create_group
        def revoke(gid):
            original(gid); self.authority.allowed=False
        self.system.create_group=revoke
        with self.assertRaisesRegex(account.AccountError,'authority'): self.ensure()
        self.assertEqual(self.system.calls,['group'])
        self.assertIsNone(self.system.account_value)

    def test_digest_for_other_numeric_identity_cannot_authorize_creation(self):
        with self.assertRaisesRegex(account.AccountError,'scope digest'):
            self.ensure(dict(self.request,uid=988))
        self.assertEqual(self.system.calls,[])

    def test_linux_command_arguments_fixed_environment_and_generic_errors(self):
        with mock.patch.object(account.os,'geteuid',return_value=0):
            system=account.LinuxAccounts()
        result=mock.Mock(stdout='')
        # Ubuntu/Debian install groupadd and useradd in /usr/sbin; Arch exposes
        # /usr/sbin as a system symlink. Both are called by absolute path and
        # never resolved through the inherited PATH (fixed env below).
        with mock.patch.object(account.subprocess,'run',return_value=result) as run:
            system.create_group(987)
            args=run.call_args.args[0]
            self.assertEqual(args[0],'/usr/sbin/groupadd')
            self.assertEqual(args[1:4],['--system','--gid','987'])
            self.assertEqual(args[-1],'ameesh-release')
            self.assertEqual(run.call_args.kwargs['env'],{'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
        with mock.patch.object(account.subprocess,'run',return_value=result) as run:
            system.create_account(987,987)
            args=run.call_args.args[0]
            self.assertEqual(args[0],'/usr/sbin/useradd')
            self.assertEqual(args[-1],'ameesh-release')
            self.assertIn('--no-log-init',args)
            self.assertIn('--no-create-home',args)
            self.assertEqual(run.call_args.kwargs['env'],{'PATH':'/usr/bin:/bin','LANG':'C','LC_ALL':'C'})
        with mock.patch.object(account.subprocess,'run',side_effect=OSError('private-canary')):
            with self.assertRaises(account.AccountError) as failure: system.create_group(987)
        self.assertNotIn('canary',str(failure.exception))
