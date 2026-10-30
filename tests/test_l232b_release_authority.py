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
    def test_signed_supersession_refuses_old_controller_with_replacement_id(self):
        self.signed_answer()
        new=decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(self.lot['id']),
            question=self.view['question'],options=['%s=%s'%(item['key'],item['label']) for item in release.OPTIONS],
            supersedes=self.view['id'],supersedes_option='apply')
        self.assertTrue(self.validate())
        self.sign_view(new,'apply')
        for step in release.STEPS:
            self.assertFalse(self.validate(step),step)
        from tests.test_l232b_release_controller import controller, Host
        with self.assertRaisesRegex(controller.ControllerError,'superseded by decision #%d'%new['id']):
            controller.run(Path(self.tmp)/'superseded','test-release',self.manifest,
                           authority=self.adapter,adapter=Host())

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

    def sign_view(self, view, text, ttl=900):
        # Same option `decisions.answer` records and signs: bounded native form
        # only (raw key or the two signer-owner.sh mentions of #3443/#3454).
        option = decisions.match_option(view, text)
        digest = decisions.answer_digest(view, text, option, 'human:fixture-owner')
        # The validator runs on the test clock: the approval must expire a fixed
        # `ttl` after it, never relative to a wall-clock instant the test clock
        # does not share (a slow setUp otherwise leaves the expiry ahead).
        authority.create_approval(self.db, self.seed, approver='fixture-owner',
                                  action=decisions.APPROVAL_ACTION, artifact_kind=decisions.APPROVAL_KIND,
                                  artifact_hash=digest, ttl=(self.now + ttl) - time.time())
        return decisions.answer(self.cfg, self.db, view['id'], text,
                                human='human:fixture-owner', channel='cli', signed=True)['decision']

    def signed_answer(self, text='apply', ttl=900):
        return self.sign_view(self.view, text, ttl)

    def another_view(self):
        return decisions.ask(
            self.cfg, self.db, requester='fixture-controller', lot=str(self.lot['id']),
            question=release.question('test-release', self.manifest, expires_at=int(self.now) + 600),
            options=['%s=%s' % (item['key'], item['label']) for item in release.OPTIONS],
            needs_signature=True)

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

    def test_bureau_mention_authorizes_the_real_controller_run(self):
        # #4788: signer-owner.sh signs the option plus its fixed mention; the
        # whole text stays recorded and digested, the bounded mention only
        # names the option. Every step (fence and recovery included) must work.
        mention = 'apply — saisi par le bureau, délégation §10'
        signed = self.signed_answer(mention)
        self.assertEqual(signed['answer']['text'], mention)
        self.assertEqual((signed['answer']['option'], signed['answer']['option_label']),
                         ('apply', release.OPTIONS[0]['label']))
        for step in release.STEPS:
            self.assertTrue(self.validate(step), step)
        from tests.test_l232b_release_controller import controller, Host
        host = Host()
        self.assertEqual(controller.run(Path(self.tmp) / 'controller-mention', 'test-release',
                                        self.manifest, authority=self.adapter, adapter=host),
                         'accepted')
        self.assertEqual(host.current, self.manifest['release'])

    def test_relayed_owner_mention_is_the_same_bounded_authority(self):
        mention = ('apply — réponse de l\'owner relayée par le bureau, source '
                   '526a0a74-f00e-4639-b4d1-d11066d51f99')
        self.signed_answer(mention)
        self.assertEqual(decisions.get(self.db, self.view['id'])['answer']['text'], mention)
        self.assertTrue(self.validate('prepare'))
        self.assertTrue(self.validate('recovery_unfence'))

    def test_foreign_or_free_text_never_becomes_the_signed_option(self):
        # Only the two exact signer-owner.sh forms denote the option: no loose
        # prefix, no extra explanation, no foreign/case-altered mention.
        cases = ('apply — saisi par le bureau',
                 'apply, mais seulement après la revue',
                 'apply — Réponse de l\'owner relayée par le bureau, source x',
                 'apply — réponse de l\'owner relayée par le bureau, source ' + 'x' * 201)
        for text in cases:
            with self.subTest(text=text):
                view = self.another_view()
                signed = self.sign_view(view, text)
                self.assertIsNone(signed['answer']['option'])
                adapter = release.SignedDecisionAuthority(
                    self.db, view['id'], 'test-release', self.manifest, clock=lambda: self.now)
                self.assertFalse(adapter.validate('test-release', release.digest(self.manifest),
                                                  'prepare'))

    def test_mentioned_answer_text_and_recorded_option_must_agree(self):
        mention = 'apply — saisi par le bureau, délégation §10'
        self.signed_answer(mention)
        original = decisions.get(self.db, self.view['id'])
        divergent = (
            dict(original['answer'], option='leave'),
            dict(original['answer'], text='leave — saisi par le bureau, délégation §10'),
        )
        for answer in divergent:
            with self.subTest(answer=answer), mock.patch.object(
                    decisions, 'get', return_value=dict(original, answer=answer)):
                self.assertFalse(self.validate())

    def test_mentioned_authority_expiry_and_revocation_are_live(self):
        self.signed_answer('apply — saisi par le bureau, délégation §10')
        self.assertTrue(self.validate())
        self.now += 601
        self.assertFalse(self.validate('recovery_unfence'))
        self.now -= 601
        authority.revoke_key(self.db, 'fixture-owner')
        self.assertFalse(self.validate('prepare'))

    def test_mentioned_authority_with_altered_approval_is_refused(self):
        signed = self.signed_answer('apply — saisi par le bureau, délégation §10')
        approval_id = signed['answer']['proof']['approval_id']
        self.db.execute('UPDATE mesh_approvals SET signature = %s WHERE id = %s',
                        (authority.b64(b'\x00' * 64), approval_id))
        self.assertFalse(self.validate())

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

    def test_a_signature_shorter_than_the_declared_window_never_authorizes(self):
        # P2 witness: with the standard one-hour signature over a longer declared
        # window, every step (fence and recovery included) used to be refused
        # only after one hour, leaving the host fenced. The mismatch is refused
        # before the first effect instead.
        self.signed_answer(ttl=50)
        for step in ('prepare', 'backup', 'pin', 'fence', 'rollback', 'recovery_unfence'):
            self.assertFalse(self.validate(step), step)

    def test_signature_and_declared_window_bound_execution_together(self):
        self.signed_answer()
        self.assertTrue(self.validate('prepare'))
        self.now += 599
        self.assertTrue(self.validate('prepare'))
        self.now += 2
        self.assertFalse(self.validate('prepare'))

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
