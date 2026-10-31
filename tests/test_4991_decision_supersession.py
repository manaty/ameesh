# SPDX-License-Identifier: AGPL-3.0-only
"""#4991: signed replacement, atomic authority revocation, immutable history."""
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
import time
import unittest

from ameesh import decisions, storage, harness_adoption
from ameesh import db as db_mod
from . import test_l124_decisions as fixtures

ALICE = fixtures.ALICE


class SupersessionTest(fixtures._Base):
    owner_key = fixtures.SignatureTest.owner_key

    def setUp(self):
        super().setUp()
        self.key = self.owner_key()

    def old_answer(self):
        old = self.ask(needs_signature=True)
        return self.answer(old['id'], 'a', key_path=self.key)['decision']

    def replacement(self, old):
        return self.ask(supersedes=old['id'], supersedes_option='a', question='Replace the exact former authority?')

    def test_only_signed_answer_replaces_exact_decision_and_keeps_history(self):
        old = self.old_answer()
        new = self.replacement(old)
        self.assertTrue(new['needs_signature'])
        self.assertEqual(new['supersedes'], old['id'])
        decisions.require_current(self.db, decisions.get(self.db, old['id']))
        with self.assertRaisesRegex(decisions.DecisionError, 'SIGNED answer required'):
            self.answer(new['id'], 'a')
        self.assertIsNone(decisions.get(self.db, old['id'])['superseded_by'])
        self.answer(new['id'], 'a', key_path=self.key)
        previous = decisions.get(self.db, old['id'])
        self.assertEqual(previous['answer'], old['answer'])
        self.assertEqual(previous['closed_ts'], old['closed_ts'])
        self.assertTrue(decisions.verify_proof(self.db, previous)[0])
        self.assertEqual(previous['superseded_by'], new['id'])
        with self.assertRaisesRegex(decisions.DecisionError, 'superseded by decision #%d' % new['id']):
            decisions.require_current(self.db, previous)
        events = self.db.query('SELECT * FROM decision_supersessions')
        self.assertEqual([(e['supersedes'], e['superseded_by']) for e in events], [(old['id'], new['id'])])
        self.assertEqual(events[0]['proof'], decisions.get(self.db, new['id'])['answer']['proof'])
        self.answer(new['id'], 'a', key_path=self.key)
        self.assertEqual(len(self.db.query('SELECT * FROM decision_supersessions')), 1)
        self.assertIn('superseded by decision #%d' % new['id'], '\n'.join(decisions.format_view(previous)))

    def test_pending_and_withdrawn_targets_refuse(self):
        old = self.ask()
        with self.assertRaisesRegex(decisions.DecisionError, 'pending uses withdraw'):
            self.replacement(old)
        decisions.withdraw(self.cfg, self.db, old['id'], requester='orch')
        with self.assertRaisesRegex(decisions.DecisionError, 'answered decision'):
            self.replacement(old)

    def test_withdrawn_replacement_does_not_replace_old_authority(self):
        old = self.old_answer()
        new = self.replacement(old)
        decisions.withdraw(self.cfg, self.db, new['id'], requester='orch')
        previous = decisions.get(self.db, old['id'])
        decisions.require_current(self.db, previous)
        self.assertIsNone(previous['superseded_by'])
        self.assertEqual(self.db.query('SELECT * FROM decision_supersessions'), [])
        self.assertIn('(not effective)', '\n'.join(decisions.format_view(decisions.get(self.db, new['id']))))

    def test_digest_binds_replaced_id_without_changing_legacy_digests(self):
        old = self.old_answer()
        new = self.replacement(old)
        digest = decisions.answer_digest(new, 'a', 'a', ALICE)
        self.assertNotEqual(digest, decisions.answer_digest(dict(new, supersedes=old['id'] + 1), 'a', 'a', ALICE))
        self.assertNotEqual(digest, decisions.answer_digest(dict(new, supersedes_option='b'), 'a', 'a', ALICE))
        legacy = dict(old)
        legacy.pop('supersedes')
        self.assertEqual(decisions.answer_digest(old, 'a', 'a', ALICE), decisions.answer_digest(legacy, 'a', 'a', ALICE))

    def test_replacement_requires_a_designated_existing_option(self):
        old = self.old_answer()
        for option in (None, '', 'absent'):
            with self.subTest(option=option), self.assertRaisesRegex(decisions.DecisionError, 'exact option key'):
                self.ask(supersedes=old['id'], supersedes_option=option)
        with self.assertRaisesRegex(decisions.DecisionError, 'requires --supersedes'):
            self.ask(supersedes_option='a')

    def assert_refusal_preserves_authority(self, text):
        old = self.old_answer()
        new = self.replacement(old)
        answered = self.answer(new['id'], text, key_path=self.key)['decision']
        self.assertEqual(answered['state'], 'answered')
        self.assertFalse(decisions.supersession_approved(answered))
        self.assertTrue(decisions.verify_proof(self.db, answered)[0])
        self.assertIsNone(decisions.get(self.db, old['id'])['superseded_by'])
        decisions.require_current(self.db, decisions.get(self.db, old['id']))
        self.assertEqual(self.db.query('SELECT * FROM decision_supersessions'), [])
        self.assertIn('not replaced', decisions.answer_text(answered))
        self.assertIn('(not effective)', '\n'.join(decisions.format_view(answered)))
        followup = self.replacement(old)
        self.answer(followup['id'], 'a', key_path=self.key)
        self.assertEqual(decisions.get(self.db, old['id'])['superseded_by'], followup['id'])

    def test_signed_refusal_option_keeps_old_authority_and_allows_later_replacement(self):
        self.assert_refusal_preserves_authority('b')

    def test_signed_free_text_keeps_old_authority_and_allows_later_replacement(self):
        self.assert_refusal_preserves_authority('non, ne remplace pas')

    def test_forged_proof_rolls_back_both_decisions(self):
        old = self.old_answer()
        new = self.replacement(old)
        with mock.patch.object(decisions, '_sign', return_value=old['answer']['proof']):
            with self.assertRaisesRegex(decisions.DecisionError, 'valid signed answer'):
                self.answer(new['id'], 'a', signed=True)
        self.assertEqual(decisions.get(self.db, new['id'])['state'], 'pending')
        self.assertIsNone(decisions.get(self.db, old['id'])['superseded_by'])
        self.assertEqual(self.db.query('SELECT * FROM decision_supersessions'), [])

    def test_two_candidates_have_one_winner_and_cannot_make_a_cycle(self):
        old = self.old_answer()
        first, second = self.replacement(old), self.replacement(old)
        self.answer(first['id'], 'a', key_path=self.key)
        with self.assertRaisesRegex(decisions.DecisionError, 'superseded by decision #%d' % first['id']):
            self.answer(second['id'], 'a', key_path=self.key)
        self.assertEqual(decisions.get(self.db, second['id'])['state'], 'pending')
        self.assertEqual(len(self.db.query('SELECT * FROM decision_supersessions')), 1)
        # A hand-corrupted request cannot introduce a forward edge or cycle.
        self.db.execute("UPDATE agent_mailbox SET payload=jsonb_set(payload, '{decision,supersedes}', %s::jsonb) WHERE id=%s",
                        (str(second['id']), second['id']))
        with self.assertRaisesRegex(decisions.DecisionError, 'earlier answered decision'):
            self.answer(second['id'], 'a', key_path=self.key)

    def test_closure_cannot_inject_unsigned_replacement_fields(self):
        new = self.ask()
        with self.assertRaisesRegex(decisions.DecisionError, 'cannot change the signed request'):
            storage.of(self.db).decisions.close(new['id'], status='answered', record={
                'state': 'answered', 'superseded_by': 999})
        self.assertEqual(decisions.get(self.db, new['id'])['state'], 'pending')

    def test_expired_signed_approval_cannot_activate_a_replacement(self):
        old = self.old_answer()
        new = self.replacement(old)
        proof = decisions._sign(self.db, new, 'a', 'a', ALICE, key_path=self.key, approver=None)
        future = time.time() + 86400
        with mock.patch.object(decisions, '_sign', return_value=proof), mock.patch.object(decisions.time, 'time', return_value=future):
            with self.assertRaisesRegex(decisions.DecisionError, 'valid signed answer'):
                self.answer(new['id'], 'a', signed=True)
        self.assertEqual(decisions.get(self.db, new['id'])['state'], 'pending')
        self.assertIsNone(decisions.get(self.db, old['id'])['superseded_by'])

    def test_concurrent_replacements_on_separate_connections_have_one_winner(self):
        old = self.old_answer()
        candidates = [self.replacement(old), self.replacement(old)]
        barrier = Barrier(2)
        def respond(candidate):
            conn = db_mod.connect(self.cfg)
            try:
                barrier.wait(timeout=10)
                try:
                    decisions.answer(self.cfg, conn, candidate['id'], 'a', human=ALICE,
                                     channel='cli', key_path=self.key)
                    return True
                except decisions.DecisionError as error:
                    self.assertIn('superseded by decision #', str(error))
                    return False
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(respond, candidates))
        self.assertEqual(sorted(results), [False, True])
        events = self.db.query('SELECT * FROM decision_supersessions')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['superseded_by'], decisions.get(self.db, old['id'])['superseded_by'])


class AdoptionSupersessionTest(unittest.TestCase):
    def test_superseded_adoption_is_refused_before_host_actions(self):
        view = dict(id=11, state='answered', answer={'option': 'adopt'}, superseded_by=12)
        replacement = dict(
            id=12, state='answered', needs_signature=True, supersedes=11,
            supersedes_option='a', options=[{'key': 'a'}],
            answer={'by': ALICE, 'channel': 'signed', 'option': 'a', 'text': 'a',
                    'ts': 1000.0, 'proof': {'digest': 'proof'}},
            created_ts=900.0, closed_ts=1000.0, question='Replace?')
        event = dict(superseded_by=12, person=ALICE,
                     occurred_at=datetime.fromtimestamp(1000.0, timezone.utc),
                     proof={'digest': 'proof'})

        def fake_get(db, identifier):
            return view if identifier == 11 else replacement

        with mock.patch.object(decisions, 'get', side_effect=fake_get), \
                mock.patch.object(decisions, 'supersession_event', return_value=event), \
                mock.patch.object(decisions, 'verify_proof', return_value=(True, '')) as verify:
            allowed, why = harness_adoption._owner_proof_still_valid(object(), {'decision_id': 11})
        self.assertFalse(allowed)
        self.assertIn('decision #11 superseded by decision #12', why)
        # The old decision's own proof is never consulted; only the
        # replacement's signed answer is, by the authority gate.
        verify.assert_called_once_with(mock.ANY, replacement)
