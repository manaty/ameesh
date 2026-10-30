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
        # These synthetic bundles carry no seal. The test must not discover
        # a real host enrollment and attempt to acquire its root-only lock.
        absent=Path(self.temp.name)/'absent-seal.json'
        patcher=mock.patch.object(bootstrap.delegation,'SEAL_DOCUMENT',str(absent))
        patcher.start(); self.addCleanup(patcher.stop)
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
    def test_signed_leave_and_free_text_keep_old_enable_authorized(self):
        self.sign()
        for text in ('leave','non, ne remplace pas'):
            with self.subTest(text=text):
                new=decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(self.lot['id']),
                    question=self.view['question'],options=['%s=%s'%(item['key'],item['label']) for item in bootstrap.OPTIONS],
                    supersedes=self.view['id'],supersedes_option='install')
                self.sign(text,view=new)
                self.assertTrue(self.valid('enable'))
                self.assertIsNone(decisions.get(self.db,self.view['id'])['superseded_by'])

    def test_signed_supersession_refuses_every_old_step_including_enable(self):
        self.sign()
        old=decisions.get(self.db,self.view['id'])
        new=decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(self.lot['id']),
            question=old['question'],options=['%s=%s'%(item['key'],item['label']) for item in bootstrap.OPTIONS],
            supersedes=old['id'],supersedes_option='install')
        self.assertTrue(self.valid('enable'))
        self.sign(view=new)
        for step in bootstrap.STEPS:
            self.assertFalse(self.valid(step),step)
        with self.assertRaisesRegex(bootstrap.BootstrapError,'superseded by decision #%d'%new['id']):
            bootstrap._run(Path(self.tmp)/'superseded',self.operation,self.bundle,
                          authority=self.adapter,adapter=Host())
        replacement=bootstrap.SignedBootstrapAuthority(self.db,new['id'],self.operation,self.bundle,clock=lambda:self.now)
        self.assertTrue(replacement.validate(self.operation,replacement.scopes['enable'],'enable'))

    def setUp(self):
        super().setUp()
        self.now=time.time(); self.bundle=bundle(); self.operation='bootstrap-fixture'; self.seed=b'\x42'*32
        registry.upsert(self.db,'fixture-owner',harness='other',host=self.cfg.host)
        authority.register_key(self.db,'fixture-owner',signing.public_from_seed(self.seed),role='owner')
        registry.upsert(self.db,'fixture-controller',harness='other',host=self.cfg.host)
        lot=work.add(self.db,title='Synthetic bootstrap signature',assignee='fixture-controller')
        self.lot=lot
        self.view=decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(lot['id']),
            question=bootstrap.question(self.operation,self.bundle,expires_at=int(self.now)+600),
            options=['%s=%s'%(item['key'],item['label']) for item in bootstrap.OPTIONS],needs_signature=True)
        self.adapter=bootstrap.SignedBootstrapAuthority(self.db,self.view['id'],self.operation,self.bundle,clock=lambda:self.now)
    def sign(self,text='install',ttl=900,view=None):
        view=view or self.view
        option=decisions.match_option(view,text)
        digest=decisions.answer_digest(view,text,option,'human:fixture-owner')
        # The validator runs on the test clock: the approval must expire a fixed
        # `ttl` after it, never relative to a wall-clock instant the test clock
        # does not share (a slow setUp otherwise leaves the expiry ahead).
        authority.create_approval(self.db,self.seed,approver='fixture-owner',action=decisions.APPROVAL_ACTION,
            artifact_kind=decisions.APPROVAL_KIND,artifact_hash=digest,ttl=(self.now+ttl)-time.time())
        decisions.answer(self.cfg,self.db,view['id'],text,human='human:fixture-owner',channel='cli',signed=True)
    def valid(self,step='account',digest=None):
        return self.adapter.validate(self.operation,digest or self.adapter.scopes[step],step)

    def test_signed_seal_cannot_be_replaced_by_another_sha_or_unsigned_pin(self):
        from tests.test_3526_runtime_seal import SealTest, seal, empty_tree
        files=SealTest(); files.setUp(); self.addCleanup(files.doCleanups)
        manifest=dict(files.document,machine_id=self.bundle['machine_id'])
        plan=dict(self.bundle['plan'],
                  runtime_tree_sha256=manifest['trees']['runtime']['digest'],
                  source_tree_sha256=manifest['trees']['lib']['digest'])
        def rendered(doc):
            return bootstrap.delegation.render(plan,account_ids=dict(uid=987,gid=987),
                enrollment={bootstrap.delegation.SEAL_DOCUMENT:seal.canonical(doc).decode()})
        self.bundle=rendered(manifest)
        self.view=decisions.ask(self.cfg,self.db,requester='fixture-controller',new_lot='Synthetic root seal',
            question=bootstrap.question(self.operation,self.bundle,expires_at=int(self.now)+600),
            options=['%s=%s'%(item['key'],item['label']) for item in bootstrap.OPTIONS],needs_signature=True)
        self.adapter=bootstrap.SignedBootstrapAuthority(self.db,self.view['id'],self.operation,self.bundle,clock=lambda:self.now)
        self.assertFalse(self.valid('configuration'))
        self.sign(); self.assertTrue(self.valid('configuration'))
        changed=rendered(dict(manifest,sha='c'*40))
        replacement=bootstrap.SignedBootstrapAuthority(self.db,self.view['id'],self.operation,changed,clock=lambda:self.now)
        self.assertFalse(replacement.validate(self.operation,replacement.scopes['configuration'],'configuration'))
        authority.revoke_key(self.db,'fixture-owner')
        self.assertFalse(self.valid('configuration'))

    def test_signature_requires_exact_scopes_and_cannot_authorize_arbitrary_steps(self):
        self.assertFalse(self.valid()); self.sign()
        for step in bootstrap.STEPS: self.assertTrue(self.valid(step),step)
        self.assertFalse(self.valid(digest='f'*64))
        self.assertFalse(self.adapter.validate('other',self.adapter.scopes['account'],'account'))
        self.assertFalse(self.adapter.validate(self.operation,self.adapter.scopes['account'],'root-shell'))
        changed=bootstrap.account.scope_digest(self.operation,self.bundle['machine_id'],988,987)
        self.assertFalse(self.valid(digest=changed))

    def test_original_approval_expiry_deadline_and_revocation_are_live(self):
        self.sign(); self.assertTrue(self.valid())
        self.now+=601; self.assertFalse(self.valid())
        self.now-=601; authority.revoke_key(self.db,'fixture-owner'); self.assertFalse(self.valid())

    def test_approval_dying_before_the_declared_window_has_no_authority(self):
        # The signed approval covers the whole declared window (P2): a shorter
        # ttl never authorises the bootstrap it declares.
        self.sign(ttl=50); self.assertFalse(self.valid())

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

    def test_bureau_mention_authorizes_install_and_revoke(self):
        # #4788: signer-owner.sh keeps its fixed mention; both bootstrap kinds
        # (install and the separate revoke) consume decisions.match_option,
        # never the full text as if it were the option.
        mention='install — saisi par le bureau, délégation §10'
        self.sign(mention)
        answer=decisions.get(self.db,self.view['id'])['answer']
        self.assertEqual((answer['text'],answer['option']),(mention,'install'))
        for step in bootstrap.STEPS: self.assertTrue(self.valid(step),step)
        view=decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(self.lot['id']),
            question=bootstrap.revoke_question(self.operation,self.bundle,expires_at=int(self.now)+600),
            options=['%s=%s'%(item['key'],item['label']) for item in bootstrap.REVOKE_OPTIONS],
            needs_signature=True)
        revoke='revoke — saisi par le bureau, délégation §10'
        self.sign(revoke,view=view)
        authority_obj=bootstrap.SignedBootstrapRevokeAuthority(self.db,view['id'],self.operation,
                                                               self.bundle,clock=lambda:self.now)
        self.assertTrue(authority_obj.validate(self.operation,authority_obj.scopes['revoke'],'revoke'))
