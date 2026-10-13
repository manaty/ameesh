# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic owner signatures and decisions in disposable test-bench schemas."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from ameesh import authority, decisions, registry, signing, storage, work
from tests.support import PgTestCase

PATH = Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/autorite-release.py'
SPEC = importlib.util.spec_from_file_location('release_authority', PATH)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseAuthorityTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.manifest = dict(host='enrolled-test', commit='a' * 40,
                             release='a' * 40 + '-' + 'b' * 64,
                             database='mesh-test', units=['ameesh@test.service'],
                             policy='enrolled-test-policy', rollback_until=int(self.now) + 100)
        # Publicly known fixture seed: NEVER use this as an enrolled real key.
        self.seed = b'\x42' * 32
        registry.upsert(self.db, 'fixture-owner', harness='other', host=self.cfg.host)
        authority.register_key(self.db, 'fixture-owner', signing.public_from_seed(self.seed), role='owner')
        registry.upsert(self.db, 'fixture-controller', harness='other', host=self.cfg.host)
        self.lot = work.add(self.db, title='Synthetic L232 release authority', assignee='fixture-controller')
        self.view = decisions.ask(
            self.cfg, self.db, requester='fixture-controller', lot=str(self.lot['id']),
            question=release.question('test-release', self.manifest, expires_at=int(self.now) + 600),
            options=['%s=%s' % (item['key'], item['label']) for item in release.OPTIONS],
            needs_signature=True)
        self.adapter = release.SignedDecisionAuthority(
            self.db, self.view['id'], 'test-release', self.manifest, clock=lambda: self.now)

    def signed_answer(self, text='apply', ttl=900):
        digest = decisions.answer_digest(self.view, text, text, 'human:fixture-owner')
        # The validator runs on the test clock: the approval must expire a fixed
        # `ttl` after it, never relative to a wall-clock instant the test clock
        # does not share (a slow setUp otherwise leaves the expiry ahead).
        authority.create_approval(self.db, self.seed, approver='fixture-owner',
                                  action=decisions.APPROVAL_ACTION, artifact_kind=decisions.APPROVAL_KIND,
                                  artifact_hash=digest, ttl=(self.now + ttl) - time.time())
        return decisions.answer(self.cfg, self.db, self.view['id'], text,
                                human='human:fixture-owner', channel='cli', signed=True)['decision']

    def validate(self, step='activate', operation='test-release', digest=None):
        return self.adapter.validate(operation, digest or release.digest(self.manifest), step)

    def test_pending_and_unsigned_answers_cannot_authorize(self):
        self.assertFalse(self.validate())
        with mock.patch.object(decisions, 'get', return_value=dict(self.view, state='answered', answer={
                'option': 'apply', 'text': 'apply', 'channel': 'cli'})):
            self.assertFalse(self.validate())

    def test_real_signature_and_consumption_authorize_only_bound_operation(self):
        self.signed_answer()
        for step in release.STEPS:
            self.assertTrue(self.validate(step), step)
        self.assertFalse(self.validate(operation='another-release'))
        self.assertFalse(self.validate(digest='c' * 64))
        self.assertFalse(self.validate(step='arbitrary-shell'))

    def test_signed_decline_is_not_approval(self):
        self.signed_answer('leave')
        self.assertFalse(self.validate())

    def test_live_key_revocation_and_replacement_invalidate_every_step(self):
        self.signed_answer()
        self.assertTrue(self.validate())
        authority.revoke_key(self.db, 'fixture-owner')
        self.assertFalse(self.validate('rollback'))
        authority.register_key(self.db, 'fixture-owner', signing.public_from_seed(b'\x43' * 32), role='owner')
        self.assertFalse(self.validate())

    def test_signed_scope_expiry_not_mutable_decision_due_date(self):
        self.signed_answer()
        original = decisions.get(self.db, self.view['id'])
        with mock.patch.object(decisions, 'get', return_value=dict(original, due_ts=self.now - 1000)):
            self.assertTrue(self.validate())
        self.now += 600
        with mock.patch.object(decisions, 'get', return_value=dict(original, due_ts=self.now + 99999)):
            self.assertFalse(self.validate('rollback'))

    def test_original_approval_expiry_also_bounds_execution(self):
        self.signed_answer(ttl=50)
        self.assertTrue(self.validate())
        self.now += 51
        self.assertFalse(self.validate())

    def test_controller_uses_real_signature_and_rechecks_after_fence(self):
        from tests.test_l232b_release_controller import controller, Host
        self.signed_answer()
        host = Host()
        self.assertEqual(controller.run(Path(self.tmp) / 'controller-ok', 'test-release', self.manifest,
                                        authority=self.adapter, adapter=host), 'accepted')
        host = Host()
        def revoke(step):
            if step == 'fence':
                authority.revoke_key(self.db, 'fixture-owner')
        host.before = revoke
        with self.assertRaisesRegex(controller.ControllerError, 'authority denied'):
            controller.run(Path(self.tmp) / 'controller-held', 'test-release', self.manifest,
                           authority=self.adapter, adapter=host)
        self.assertEqual(host.calls, ['prepare', 'backup', 'pin', 'fence'])
        self.assertTrue(host.has_fence)
        self.assertEqual(host.current, 'old-code-and-venv')

    def test_tampered_signed_question_has_no_authority(self):
        self.signed_answer()
        original = decisions.get(self.db, self.view['id'])
        changed = original['question'].replace('enrolled-test', 'other-host')
        with mock.patch.object(decisions, 'get', return_value=dict(original, question=changed)):
            self.assertFalse(self.validate())

    def test_tampered_approval_expiry_cannot_extend_signature(self):
        answered = self.signed_answer()
        approval_id = answered['answer']['proof']['approval_id']
        self.db.execute('UPDATE mesh_approvals SET expires_at = expires_at + interval \'1 day\' WHERE id = %s',
                        (approval_id,))
        self.assertFalse(self.validate())

    def test_approval_replaced_between_reads_cannot_extend_signed_expiry(self):
        answered = self.signed_answer(ttl=50)
        proof = answered['answer']['proof']
        api = storage.of(self.db)
        rows = api.approvals.recent(action=decisions.APPROVAL_ACTION,
                                    artifact_hash=proof['digest'], limit=20)
        row = dict(rows[0])
        parts = row['signed_payload'].split('|', 2)
        document = json.loads(parts[2])
        document['expires'] += 1_000_000_000
        row['signed_payload'] = '|'.join(parts[:2]) + '|' + json.dumps(document)
        row['expires_ts'] += 1000
        self.now += 60
        with mock.patch.object(type(api.approvals), 'recent', side_effect=[rows, [row]]):
            self.assertFalse(self.validate())

    def test_corrupt_signature_and_foreign_nonce_consumption_are_refused(self):
        answered = self.signed_answer()
        proof = answered['answer']['proof']
        self.db.execute('UPDATE mesh_consumed_nonces SET consumed_by = %s', ('decision:999999',))
        self.assertFalse(self.validate())
        self.db.execute('UPDATE mesh_consumed_nonces SET consumed_by = %s', ('decision:%d' % self.view['id'],))
        self.assertTrue(self.validate())
        self.db.execute('UPDATE mesh_approvals SET signature = %s WHERE id = %s',
                        (authority.b64(b'\x00' * 64), proof['approval_id']))
        self.assertFalse(self.validate())

    def test_apparent_authority_with_another_answer_does_not_verify(self):
        self.signed_answer('leave')
        original = decisions.get(self.db, self.view['id'])
        answer = dict(original['answer'], option='apply', text='apply')
        with mock.patch.object(decisions, 'get', return_value=dict(original, answer=answer)):
            self.assertFalse(self.validate())


if __name__ == '__main__':
    unittest.main()
