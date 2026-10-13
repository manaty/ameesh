# SPDX-License-Identifier: AGPL-3.0-only
"""Scoped bootstrap authority on disposable PostgreSQL; host effects simulated."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
from tests import support
from tests import test_l232b_delegation as fixture
from ameesh import authority,decisions,registry,signing,work

SPEC=importlib.util.spec_from_file_location('release_bootstrap',
    Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/bootstrap-release.py')
bootstrap=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


def bundle():
    plan=fixture.DelegationTest(); plan.setUp()
    return bootstrap.delegation.render(plan.plan,account_ids=dict(uid=987,gid=987))


class Authority:
    denied=set()
    def validate(self,operation,digest,step):
        return step not in self.denied


class Host:
    def __init__(self):
        self.receipts={}; self.effects=[]; self.lost=set(); self.fail=set(); self.before=None
    def perform(self,step,request):
        if self.before: self.before(step)
        if step not in self.receipts:
            self.effects.append(step)
            self.receipts[step]=dict(request,ok=step not in self.fail)
        if step in self.lost:
            self.lost.remove(step); raise OSError('private-canary')
        return self.receipts[step]


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'journal'
        self.bundle=bundle(); self.host=Host(); self.authority=Authority()
        self.authority.denied=set()
    def run_bootstrap(self,operation='bootstrap-fixture',review=None):
        return bootstrap.run(self.root,operation,review or self.bundle,
                             authority=self.authority,adapter=self.host)

    def test_enable_is_last_and_complete_replay_has_no_effects(self):
        self.assertEqual(self.run_bootstrap(),'installed')
        self.assertEqual(self.host.effects,list(bootstrap.STEPS))
        self.assertEqual(self.run_bootstrap(),'installed')
        self.assertEqual(self.host.effects,list(bootstrap.STEPS))

    def test_lost_reply_at_every_step_reconciles_one_effect(self):
        for step in bootstrap.STEPS:
            with self.subTest(step=step),tempfile.TemporaryDirectory() as temp:
                host=Host(); host.lost.add(step)
                def run(): return bootstrap.run(Path(temp)/'journal','bootstrap-fixture',self.bundle,
                                              authority=self.authority,adapter=host)
                with self.assertRaisesRegex(bootstrap.BootstrapError,'uncertain') as failure: run()
                self.assertNotIn('canary',str(failure.exception))
                self.assertEqual(run(),'installed')
                self.assertEqual(host.effects,list(bootstrap.STEPS))

    def test_failed_configuration_validation_never_enables_key(self):
        self.host.fail.add('validate')
        with self.assertRaisesRegex(bootstrap.BootstrapError,'held'): self.run_bootstrap()
        self.assertNotIn('enable',self.host.effects)
        self.assertTrue((self.root/'fixture-host.active.json').exists())

    def test_revocation_after_effect_keeps_intent_for_reconciliation(self):
        def revoke(step):
            if step=='account': self.authority.denied.add(step)
        self.host.before=revoke
        with self.assertRaisesRegex(bootstrap.BootstrapError,'authority'): self.run_bootstrap()
        record=json.loads((self.root/'bootstrap-fixture.json').read_text())
        self.assertEqual(record['intent'],'account')
        self.assertNotIn('account',record['receipts'])
        self.assertNotIn('enable',self.host.effects)
        self.authority.denied.clear(); self.host.before=None
        self.assertEqual(self.run_bootstrap(),'installed')
        self.assertEqual(self.host.effects.count('account'),1)

    def test_host_reservation_rejects_other_operation_and_changed_bundle(self):
        self.host.fail.add('validate')
        with self.assertRaises(bootstrap.BootstrapError): self.run_bootstrap()
        with self.assertRaisesRegex(bootstrap.BootstrapError,'another unresolved'): self.run_bootstrap(operation='other')
        changed=bootstrap.delegation.render(self.bundle['plan'],account_ids=dict(uid=988,gid=988))
        with self.assertRaisesRegex(bootstrap.BootstrapError,'another unresolved'): self.run_bootstrap(review=changed)

    def test_question_and_scopes_bind_numeric_ids_and_exact_destinations(self):
        text=bootstrap.question('bootstrap-fixture',self.bundle,expires_at=2000000000)
        self.assertLessEqual(len(text),decisions.MAX_QUESTION)
        scopes=bootstrap.scopes('bootstrap-fixture',self.bundle)
        self.assertIn('"uid":987',text)
        self.assertEqual(scopes['account'],bootstrap.account.scope_digest('bootstrap-fixture','a'*32,987,987))
        self.assertEqual(scopes['runtime'],bootstrap.protocol.identity(dict(operation='bootstrap-fixture',
            target='/opt/ameesh-release/runtime',digest='c'*64)))
        changed=bootstrap.delegation.render(self.bundle['plan'],account_ids=dict(uid=988,gid=988))
        self.assertNotEqual(scopes['account'],bootstrap.scopes('bootstrap-fixture',changed)['account'])
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.scopes('bootstrap-fixture',bootstrap.delegation.render(self.bundle['plan']))


class SignedBootstrapTest(support.PgTestCase):
    def setUp(self):
        super().setUp()
        self.now=time.time(); self.bundle=bundle(); self.operation='bootstrap-fixture'; self.seed=b'\x42'*32
        registry.upsert(self.db,'fixture-owner',harness='other',host=self.cfg.host)
        authority.register_key(self.db,'fixture-owner',signing.public_from_seed(self.seed),role='owner')
        registry.upsert(self.db,'fixture-controller',harness='other',host=self.cfg.host)
        lot=work.add(self.db,title='Synthetic bootstrap signature',assignee='fixture-controller')
        self.view=decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(lot['id']),
            question=bootstrap.question(self.operation,self.bundle,expires_at=int(self.now)+600),
            options=['%s=%s'%(item['key'],item['label']) for item in bootstrap.OPTIONS],needs_signature=True)
        self.adapter=bootstrap.SignedBootstrapAuthority(self.db,self.view['id'],self.operation,self.bundle,clock=lambda:self.now)
    def sign(self,text='install',ttl=900):
        digest=decisions.answer_digest(self.view,text,text,'human:fixture-owner')
        authority.create_approval(self.db,self.seed,approver='fixture-owner',action=decisions.APPROVAL_ACTION,
            artifact_kind=decisions.APPROVAL_KIND,artifact_hash=digest,ttl=ttl)
        decisions.answer(self.cfg,self.db,self.view['id'],text,human='human:fixture-owner',channel='cli',signed=True)
    def valid(self,step='account',digest=None):
        return self.adapter.validate(self.operation,digest or self.adapter.scopes[step],step)

    def test_signature_requires_exact_scopes_and_cannot_authorize_arbitrary_steps(self):
        self.assertFalse(self.valid()); self.sign()
        for step in bootstrap.STEPS: self.assertTrue(self.valid(step),step)
        self.assertFalse(self.valid(digest='f'*64))
        self.assertFalse(self.adapter.validate('other',self.adapter.scopes['account'],'account'))
        self.assertFalse(self.adapter.validate(self.operation,self.adapter.scopes['account'],'root-shell'))
        changed=bootstrap.account.scope_digest(self.operation,self.bundle['machine_id'],988,987)
        self.assertFalse(self.valid(digest=changed))

    def test_original_approval_expiry_deadline_and_revocation_are_live(self):
        self.sign(ttl=50); self.assertTrue(self.valid())
        self.now+=51; self.assertFalse(self.valid())
        self.now-=51; authority.revoke_key(self.db,'fixture-owner'); self.assertFalse(self.valid())

    def test_unsigned_decline_tampered_bundle_and_later_deadline_have_no_authority(self):
        self.sign(); original=decisions.get(self.db,self.view['id'])
        changed=original['question'].replace('"uid":987','"uid":988')
        with mock.patch.object(decisions,'get',return_value=dict(original,question=changed)):
            self.assertFalse(self.valid())
        answer=dict(original['answer'],channel='cli')
        with mock.patch.object(decisions,'get',return_value=dict(original,answer=answer)):
            self.assertFalse(self.valid())
        self.now+=601
        with mock.patch.object(decisions,'get',return_value=dict(original,due_ts=self.now+9999)):
            self.assertFalse(self.valid())

    def test_real_bootstrap_signature_drives_account_and_runtime_scope_callbacks(self):
        self.sign()
        from tests.test_l232b_account import Accounts
        system=Accounts()
        request=dict(operation=self.operation,machine_id=self.bundle['machine_id'],uid=987,gid=987,
                     scope_digest=self.adapter.scopes['account'])
        receipt=bootstrap.account.ensure(Path(self.tmp)/'account',request,authority=self.adapter,system=system)
        self.assertTrue(receipt['ok'])
        self.assertEqual(system.calls,['group','user'])
