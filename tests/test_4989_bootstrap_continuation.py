# SPDX-License-Identifier: AGPL-3.0-only
"""Native filesystem continuation with real signed PostgreSQL supersession."""
import base64
import json
import shutil
import time
from unittest import mock

from tests import support
from tests import test_4323_backend_administrateur as fixture
from ameesh import decisions, authority, registry, signing, work

backend_mod = fixture.backend_mod
bootstrap = fixture.bootstrap


class SignedContinuationTest(fixture.Fixture, support.PgTestCase):
    sign = fixture.SignedRefusalTest.sign

    def ask(self, question, options, **extra):
        return decisions.ask(self.cfg, self.db, requester='fixture-controller', lot=str(self.lot['id']),
            question=question, options=['%s=%s' % (item['key'], item['label']) for item in options],
            needs_signature=True, **extra)

    def setUp(self):
        support.PgTestCase.setUp(self)
        fixture.Fixture.setUp(self)
        self.now=time.time(); self.seed=b'\x43'*32
        registry.upsert(self.db,'fixture-owner',harness='other',host=self.cfg.host)
        authority.register_key(self.db,'fixture-owner',signing.public_from_seed(self.seed),role='owner')
        registry.upsert(self.db,'fixture-controller',harness='other',host=self.cfg.host)
        self.lot=work.add(self.db,title='Synthetic bootstrap continuation',assignee='fixture-controller')
        self.question=bootstrap.question(self.operation,self.bundle,expires_at=int(self.now)+600)
        self.stage_bundle(self.bundle,self.question)
        self.view=self.ask(self.question,bootstrap.OPTIONS)
        self.backend.db=self.db
        self.sign()
        self.old_decision = self.view['id']
        self.old_operation = self.operation
        self.system.is_ready = False
        with self.assertRaises(bootstrap.BootstrapError):
            self.backend.install(self.operation, self.old_decision)
        self.old_journal = self.backend.journal() / (self.operation + '.json')
        self.old_bytes = self.old_journal.read_bytes()
        old_stage = self.stage
        self.operation = 'continued-bootstrap'
        self.stage = old_stage.parent / self.operation
        shutil.copytree(old_stage, self.stage)
        plan = dict(self.bundle['plan'])
        raw = base64.b64decode(plan['controller_key'].split()[1])
        plan['local_transport'] = dict(controller_source='127.0.0.1/32',
            controller_key='ssh-ed25519 ' + base64.b64encode(raw[:-32] + b'z'*32).decode())
        observed = {p:self.backend.observe(p) for p in self.bundle['files']}
        self.bundle = bootstrap.delegation.render(plan, account_ids=dict(uid=987, gid=987),
                                                  previous_files=observed)
        text = bootstrap.question(self.operation, self.bundle, expires_at=int(self.now)+600)
        self.stage_bundle(self.bundle, text)
        lock = self.root / 'etc/ameesh-release/runtime-seal.lock'
        lock.write_text(''); lock.chmod(0o600)
        self.lock_inode = lock.stat().st_ino
        self.continuation = self.backend.continuation_question(self.operation, self.old_operation,
                                                               self.old_decision)
        self.view = self.ask(self.continuation['question'], bootstrap.OPTIONS,
                             supersedes=self.old_decision, supersedes_option='install')
        self.system.is_ready = True

    def test_signed_replacement_reuses_verified_effects_and_preserves_old_receipts(self):
        self.sign()
        self.assertEqual(self.backend.continue_install(self.operation, self.view['id'])['outcome'], 'installed')
        self.assertEqual(self.old_journal.read_bytes(), self.old_bytes)
        self.assertEqual(self.system.calls, ['group', 'user'])
        self.assertEqual(len(self.root.joinpath('etc/ameesh-release/authorized_keys').read_text().splitlines()), 2)
        self.assertFalse((self.backend.journal() / 'fixture-host.active.json').exists())
        self.assertEqual(self.backend.continue_install(self.operation, self.view['id'])['outcome'], 'installed')
        with self.assertRaisesRegex(ValueError, 'superseded'):
            self.backend.install(self.old_operation, self.old_decision)
        self.assertEqual(self.old_journal.read_bytes(), self.old_bytes)
        self.assertEqual((self.root / 'etc/ameesh-release/runtime-seal.lock').stat().st_ino, self.lock_inode)

    def test_unsigned_and_missing_reciprocal_link_cannot_transfer_reservation(self):
        with self.assertRaises(ValueError):
            self.backend.continue_install(self.operation, self.view['id'])
        # Only creation of the fixed lock file is possible; old reservation stays.
        reservation = self.backend.journal() / 'fixture-host.active.json'
        self.assertEqual(json.loads(reservation.read_bytes())['operation'], self.old_operation)
        self.assertEqual(self.old_journal.read_bytes(), self.old_bytes)
        self.sign()
        original = decisions.get
        def missing(db, decision, *args):
            value = original(db, decision, *args)
            return dict(value, superseded_by=None) if decision == self.old_decision else value
        with mock.patch.object(decisions, 'get', side_effect=missing):
            with self.assertRaises(ValueError):
                self.backend.continue_install(self.operation, self.view['id'])
        self.assertEqual(json.loads(reservation.read_bytes())['operation'], self.old_operation)

    def test_changed_predecessor_and_inherited_account_are_never_adopted(self):
        self.sign()
        record = json.loads(self.old_bytes)
        record['receipts']['runtime']['ok'] = False
        bootstrap.protocol.save(self.old_journal, record)
        with self.assertRaises(ValueError):
            self.backend.continue_install(self.operation, self.view['id'])
        self.old_journal.write_bytes(self.old_bytes)
        self.system.account_value['shell'] = '/bin/bash'
        with self.assertRaisesRegex(ValueError, 'inherited'):
            self.backend.continue_install(self.operation, self.view['id'])
        self.assertFalse(self.root.joinpath('etc/ameesh-release/authorized_keys').exists())
        self.assertEqual(self.old_journal.read_bytes(), self.old_bytes)

    def test_refused_start_cannot_bind_receipts_to_an_unrelated_signed_decision(self):
        unrelated = self.ask('Approve an unrelated administrative scope', bootstrap.OPTIONS)
        self.sign(unrelated)
        with self.assertRaises(ValueError):
            self.backend.install(self.old_operation, unrelated['id'])
        with self.assertRaisesRegex(ValueError, 'signed previous'):
            self.backend.continuation_question(self.operation, self.old_operation, unrelated['id'])
        self.assertEqual(self.old_journal.read_bytes(), self.old_bytes)

    def test_duplicate_genuine_old_authority_requires_explicit_reconciliation(self):
        duplicate = self.ask(self.backend.prepared(self.old_operation).question, bootstrap.OPTIONS)
        self.sign(duplicate)
        # A signed mandate is executable even without an install/start entry.
        self.assertFalse(any(e.get('decision') == duplicate['id']
                             for e in self.backend.gestes.entries()))
        with self.assertRaisesRegex(ValueError, 'multiple previous'):
            self.backend.continuation_question(self.operation, self.old_operation, self.old_decision)
        self.assertFalse(self.root.joinpath('etc/ameesh-release/authorized_keys').exists())
        self.sign()
        with self.assertRaisesRegex(ValueError, 'multiple previous'):
            self.backend.continue_install(self.operation, self.view['id'])
        self.assertEqual(self.old_journal.read_bytes(), self.old_bytes)

    def test_revoked_signature_and_missing_old_reservation_refuse(self):
        self.sign()
        authority.revoke_key(self.db, 'fixture-owner')
        with self.assertRaises(ValueError):
            self.backend.continue_install(self.operation, self.view['id'])
        reservation = self.backend.journal() / 'fixture-host.active.json'
        self.assertEqual(json.loads(reservation.read_bytes())['operation'], self.old_operation)
        self.assertFalse(self.root.joinpath('etc/ameesh-release/authorized_keys').exists())

    def test_missing_old_reservation_refuses_before_new_journal(self):
        self.sign()
        reservation = self.backend.journal() / 'fixture-host.active.json'
        reservation.unlink()
        with self.assertRaisesRegex(ValueError, 'reservation unavailable'):
            self.backend.continue_install(self.operation, self.view['id'])
        self.assertFalse((self.backend.journal() / (self.operation + '.json')).exists())
        self.assertEqual(self.old_journal.read_bytes(), self.old_bytes)

    def test_lost_enable_reply_replays_new_intent_without_rewriting_previous(self):
        self.sign()
        original = self.system.activate_ssh_configuration
        lost = []
        def interrupted(bundle):
            original(bundle)
            if not lost:
                lost.append(True)
                raise OSError('lost reload reply')
            return True
        with mock.patch.object(self.system, 'activate_ssh_configuration', side_effect=interrupted):
            with self.assertRaises(ValueError):
                self.backend.continue_install(self.operation, self.view['id'])
            self.assertEqual(self.backend.continue_install(self.operation, self.view['id'])['outcome'], 'installed')
        self.assertEqual(self.old_journal.read_bytes(), self.old_bytes)
        self.assertEqual(self.system.calls, ['group', 'user'])
