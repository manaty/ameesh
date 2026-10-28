# SPDX-License-Identifier: AGPL-3.0-only
"""Enrolled administrator backend (#4323): real disposable files, simulated host.

Accounts, services and SSH reloads are the installer fixture's simulations;
the signed refusals run the real `SignedBootstrapAuthority` on a disposable
PostgreSQL schema. No real root, account, SSH or systemd effect is executed.
"""
from tests import support
from tests import test_l232b_bootstrap_installer as installer_fixture
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import time
import unittest
from unittest import mock
from ameesh import authority,decisions,registry,signing,work

SPEC=importlib.util.spec_from_file_location('admin_backend',
    Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/backend-administrateur.py')
backend_mod=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(backend_mod)
bootstrap=backend_mod.bootstrap
KEY='/etc/ameesh-release/authorized_keys'


def tree_state(root):
    state={}
    for directory,dirs,names in os.walk(root):
        for name in dirs+names:
            path=Path(directory)/name; info=path.lstat()
            data=path.read_bytes() if path.is_file() else b''
            state[str(path)]=(info.st_mode,info.st_mtime_ns,hashlib.sha256(data).hexdigest())
    return state


class Fixture:
    """Installer fixture plus the staged bundle the backend reads."""

    def setUp(self):
        base=installer_fixture.InstallerTest(); base.setUp(); self.addCleanup(base.doCleanups)
        self.base=base; self.root=base.root; self.system=base.system; self.authority=base.authority
        self.bundle=base.bundle; self.operation=base.operation
        self.stage=self.root/('var/lib/ameesh-release-bootstrap/'+self.operation)
        self.root.joinpath('var/lib/ameesh-release-bootstrap').chmod(0o700)
        self.question=bootstrap.question(self.operation,self.bundle,expires_at=2000000000)
        self.stage_bundle(self.bundle,self.question)
        self.enrollment=dict(version=1,host=self.bundle['host'],machine_id=self.bundle['machine_id'])
        self.backend=backend_mod.Backend(system=self.system,enrollment=self.enrollment,
                                         root=self.root,code='e'*64)

    def stage_bundle(self,bundle,question):
        for name,text in (('bundle.json',json.dumps(bundle,sort_keys=True)),('question.txt',question)):
            path=self.stage/name; path.unlink(missing_ok=True)
            path.write_text(text); path.chmod(0o600)

    def gestes(self):
        return self.backend.gestes.entries()


class BackendTest(Fixture,unittest.TestCase):
    def test_dry_run_renders_the_exact_plan_without_any_effect(self):
        before=tree_state(self.root)
        plan=self.backend.plan(self.operation,7,authority=self.authority)
        self.assertEqual(tree_state(self.root),before)
        self.assertFalse((self.root/'var/lib/ameesh-release-bootstrap/.admin').exists())
        self.assertTrue(plan['dry_run']); self.assertTrue(plan['authorized'])
        self.assertEqual([item['step'] for item in plan['steps']],list(bootstrap.STEPS))
        self.assertEqual({item['scope_digest'] for item in plan['steps']},
                         {bootstrap.scopes(self.operation,self.bundle)[step] for step in bootstrap.STEPS})
        steps={item['step']:item['effects'] for item in plan['steps']}
        self.assertTrue(steps['preflight']['machine_id'])
        self.assertTrue(steps['preflight']['runtime']); self.assertTrue(steps['preflight']['sources'])
        self.assertEqual(steps['layout']['/var/lib/ameesh-release/controller'],'create 0700')
        # The fixture pre-creates it 0700: the real layout would refuse it.
        self.assertEqual(steps['layout']['/etc/ameesh-release'],'mismatch')
        self.assertTrue(steps['runtime']['/opt/ameesh-release/runtime'].startswith('publish '))
        self.assertEqual(steps['account']['group'],'create')
        self.assertEqual(steps['enable'][KEY],'write 0644')
        self.assertNotIn(KEY,steps['configuration'])
        self.assertTrue(all(value=='unchanged' for value in plan['revoke']['files'].values()))
        self.assertEqual(self.system.calls,[]); self.assertEqual(self.system.reloads,0)
        # The rendered plan never carries document bodies.
        text=json.dumps(plan)
        for info in self.bundle['files'].values(): self.assertNotIn(info['content'],text)

    def test_dry_run_reports_missing_authority_per_step(self):
        self.authority.denied={'enable'}
        plan=self.backend.plan(self.operation,7,authority=self.authority)
        self.assertFalse(plan['authorized'])
        self.assertEqual([item['step'] for item in plan['steps'] if not item['authorized']],['enable'])

    def test_install_appends_a_chained_journal_and_replays_without_effect(self):
        result=self.backend.install(self.operation,7,authority=self.authority)
        self.assertEqual(result['outcome'],'installed')
        self.assertTrue((self.root/KEY.lstrip('/')).exists())
        entries=self.gestes()
        self.assertEqual([item['phase'] for item in entries],
                         ['start']+['intent','receipt']*len(bootstrap.STEPS)+['installed'])
        self.assertTrue(all(item['code_sha256']=='e'*64 for item in entries))
        self.assertTrue(all(item['bundle_digest']==self.bundle['digest'] for item in entries))
        self.assertEqual(entries[0]['prev'],backend_mod.GENESIS)
        journal=(self.root/'var/lib/ameesh-release-bootstrap/.admin/gestes.jsonl')
        self.assertFalse(journal.stat().st_mode & 0o077)
        for info in self.bundle['files'].values(): self.assertNotIn(info['content'],journal.read_text())
        self.assertEqual(self.backend.install(self.operation,7,authority=self.authority)['outcome'],'installed')
        self.assertEqual(self.system.calls,['group','user']); self.assertEqual(self.system.reloads,1)
        plan=self.backend.plan(self.operation,7,authority=self.authority)
        self.assertTrue(plan['complete']); self.assertTrue(all(item['done'] for item in plan['steps']))
        self.assertEqual(plan['revoke']['files'][KEY],'remove')

    def test_tampered_or_truncated_journal_refuses_every_later_gesture(self):
        self.backend.install(self.operation,7,authority=self.authority)
        journal=self.root/'var/lib/ameesh-release-bootstrap/.admin/gestes.jsonl'
        lines=journal.read_text().splitlines(True)
        for altered in (lines[:3]+lines[4:],[lines[0].replace('"start"','"installed"')]+lines[1:]):
            with self.subTest():
                journal.write_text(''.join(altered))
                with self.assertRaisesRegex(backend_mod.BackendError,'chain broken'):
                    self.backend.install(self.operation,7,authority=self.authority)
        journal.write_text(''.join(lines))
        self.assertEqual(len(self.gestes()),len(lines))

    def test_revoke_removes_the_key_first_and_keeps_runtime_account_and_journals(self):
        self.backend.install(self.operation,7,authority=self.authority)
        self.assertEqual(self.backend.revoke(self.operation,8,authority=self.authority)['outcome'],'revoked')
        self.assertFalse((self.root/KEY.lstrip('/')).exists())
        for logical in self.bundle['files']:
            if self.bundle['previous_files'][logical] is None:
                self.assertFalse((self.root/logical.lstrip('/')).exists(),logical)
        self.assertTrue((self.root/'opt/ameesh-release/runtime/bin/python').exists())
        self.assertIsNotNone(self.system.account_value)
        phases=[(item['command'],item['phase']) for item in self.gestes()]
        self.assertEqual(phases[-2:],[('revoke','intent'),('revoke','receipt')])
        plan=self.backend.plan(self.operation,7,authority=self.authority)
        self.assertTrue(all(value=='unchanged' for value in plan['revoke']['files'].values()))

    def test_revoke_without_authority_changes_nothing_and_is_journaled(self):
        self.backend.install(self.operation,7,authority=self.authority)
        self.authority.denied={'revoke'}
        with self.assertRaises(Exception): self.backend.revoke(self.operation,8,authority=self.authority)
        self.assertTrue((self.root/KEY.lstrip('/')).exists())
        self.assertEqual(self.gestes()[-1]['phase'],'uncertain')

    def test_install_without_authority_has_no_host_effect(self):
        self.authority.denied=set(bootstrap.ACTIONS)
        with self.assertRaises(bootstrap.BootstrapError):
            self.backend.install(self.operation,7,authority=self.authority)
        self.assertFalse((self.root/'opt/ameesh-release').exists())
        self.assertFalse((self.root/KEY.lstrip('/')).exists())
        self.assertEqual([item['phase'] for item in self.gestes()],['start','held'])
        self.assertEqual(self.gestes()[-1]['category'],'BootstrapError')

    def test_bundle_of_another_host_or_altered_question_refuses(self):
        self.stage_bundle(self.bundle,self.question.replace('"uid":987','"uid":988'))
        with self.assertRaisesRegex(backend_mod.BackendError,'question'):
            self.backend.plan(self.operation,7,authority=self.authority)
        other=backend_mod.Backend(system=self.system,root=self.root,code='e'*64,
                                  enrollment=dict(self.enrollment,host='other-host'))
        self.stage_bundle(self.bundle,self.question)
        with self.assertRaisesRegex(backend_mod.BackendError,'another host'):
            other.install(self.operation,7,authority=self.authority)
        self.system.machine_id='f'*32
        with self.assertRaisesRegex(backend_mod.BackendError,'another host'):
            self.backend.plan(self.operation,7,authority=self.authority)

    def test_no_enrolled_registry_or_decision_refuses_before_any_effect(self):
        with self.assertRaisesRegex(backend_mod.BackendError,'registry'):
            self.backend.install(self.operation,7)
        self.backend.db=object()
        for decision in (0,-1,'7',None):
            with self.assertRaisesRegex(backend_mod.BackendError,'decision'):
                self.backend.install(self.operation,decision)
        self.assertFalse((self.root/'var/lib/ameesh-release-bootstrap/.admin').exists())

    def test_cli_accepts_only_an_operation_and_a_decision(self):
        for argv in ([],['shell','x'],['install','x'],['install','x','--decision','0'],
                     ['install','x','--decision','7','--root','/tmp'],['inventory','x','--decision','7'],
                     ['plan','x','--decision','-7']):
            with self.subTest(argv=argv),self.assertRaises(backend_mod.BackendError):
                backend_mod.parse(argv)
        self.assertEqual(backend_mod.parse(['plan','op-1','--decision','7']),('plan','op-1',7))
        for operation in ('../etc','.admin','/etc/x','a b'):
            with self.subTest(operation=operation),self.assertRaisesRegex(backend_mod.BackendError,'operation'):
                self.backend.stage(operation)
        with mock.patch.object(backend_mod.os,'geteuid',return_value=1001), \
                mock.patch('sys.stderr'),mock.patch('sys.stdout'):
            self.assertEqual(backend_mod.main(['plan','op-1','--decision','7']),2)

    def test_prepare_observes_the_inventory_itself_and_never_rerenders(self):
        for name in ('bundle.json','question.txt'): (self.stage/name).unlink()
        value=dict(plan=self.bundle['plan'],account_ids=dict(uid=987,gid=987),expires_at=2000000000)
        path=self.stage/'plan.json'; path.write_text(json.dumps(value)); path.chmod(0o600)
        inventory=self.backend.inventory(self.operation)
        self.assertTrue(all(item is None for item in inventory['files'].values()))
        result=self.backend.prepare(self.operation)
        self.assertEqual(result['bundle_digest'],self.bundle['digest'])
        self.assertEqual((self.stage/'question.txt').read_text(),result['question'])
        self.assertEqual(self.backend.prepare(self.operation)['bundle_digest'],self.bundle['digest'])
        # A changed host state after preparation never re-renders the bundle.
        wrapper=self.root/bootstrap.delegation.WRAPPER.lstrip('/'); wrapper.write_text('# prior\n'); wrapper.chmod(0o755)
        with self.assertRaisesRegex(backend_mod.BackendError,'already prepared'):
            self.backend.prepare(self.operation)
        path.write_text(json.dumps(dict(value,shell='/bin/sh'))); path.chmod(0o600)
        with self.assertRaisesRegex(backend_mod.BackendError,'exact staged plan'):
            self.backend.inventory(self.operation)
        path.chmod(0o644)
        with self.assertRaisesRegex(backend_mod.BackendError,'staged plan unavailable|private'):
            self.backend.inventory(self.operation)


class SignedRefusalTest(Fixture,support.PgTestCase):
    """Mandate absent, unsigned or expired: the real authority refuses."""

    def setUp(self):
        support.PgTestCase.setUp(self)
        Fixture.setUp(self)
        self.now=time.time(); self.seed=b'\x43'*32
        registry.upsert(self.db,'fixture-owner',harness='other',host=self.cfg.host)
        authority.register_key(self.db,'fixture-owner',signing.public_from_seed(self.seed),role='owner')
        registry.upsert(self.db,'fixture-controller',harness='other',host=self.cfg.host)
        lot=work.add(self.db,title='Synthetic admin backend signature',assignee='fixture-controller')
        self.question=bootstrap.question(self.operation,self.bundle,expires_at=int(self.now)+600)
        self.stage_bundle(self.bundle,self.question)
        self.view=decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(lot['id']),
            question=self.question,options=['%s=%s'%(item['key'],item['label']) for item in bootstrap.OPTIONS],
            needs_signature=True)
        self.backend.db=self.db

    def sign(self,signed=True):
        text='install'
        if signed:
            digest=decisions.answer_digest(self.view,text,text,'human:fixture-owner')
            authority.create_approval(self.db,self.seed,approver='fixture-owner',action=decisions.APPROVAL_ACTION,
                artifact_kind=decisions.APPROVAL_KIND,artifact_hash=digest,ttl=(self.now+900)-time.time())
        decisions.answer(self.cfg,self.db,self.view['id'],text,human='human:fixture-owner',channel='cli',signed=signed)

    def signed(self,clock=None):
        return bootstrap.SignedBootstrapAuthority(self.db,self.view['id'],self.operation,self.bundle,
                                                  clock=clock or (lambda:self.now))

    def assert_refused(self,decision,authority=None):
        with self.assertRaises(bootstrap.BootstrapError):
            self.backend.install(self.operation,decision,authority=authority)
        self.assertFalse((self.root/'opt/ameesh-release').exists())
        self.assertFalse((self.root/KEY.lstrip('/')).exists())
        self.assertEqual(self.system.calls,[])
        self.assertEqual(self.gestes()[-1]['phase'],'held')

    def test_absent_mandate_refuses(self):
        self.assert_refused(self.view['id']+1000)
        self.assert_refused(self.view['id'])
        self.assertFalse(self.backend.plan(self.operation,self.view['id'])['authorized'])

    def test_unsigned_mandate_refuses(self):
        with mock.patch.object(decisions,'verify_proof',return_value=(False,'unsigned')):
            self.assert_refused(self.view['id'],self.signed())
        original=decisions.get(self.db,self.view['id'])
        unsigned=dict(original,state='answered',answer=dict(option='install',text='install',channel='cli'))
        with mock.patch.object(decisions,'get',return_value=unsigned):
            self.assert_refused(self.view['id'],self.signed())

    def test_expired_mandate_refuses_and_live_one_installs(self):
        self.sign()
        self.assert_refused(self.view['id'],self.signed(clock=lambda:self.now+601))
        self.assertFalse(self.backend.plan(self.operation,self.view['id'],
                                           authority=self.signed(clock=lambda:self.now+601))['authorized'])
        self.assertTrue(self.backend.plan(self.operation,self.view['id'],authority=self.signed())['authorized'])
        self.assertEqual(self.backend.install(self.operation,self.view['id'],authority=self.signed())['outcome'],
                         'installed')
        authority.revoke_key(self.db,'fixture-owner')
        with self.assertRaises(Exception):
            self.backend.revoke(self.operation,self.view['id'],authority=self.signed())
        self.assertTrue((self.root/KEY.lstrip('/')).exists())


if __name__=='__main__':
    unittest.main()
