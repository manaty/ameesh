# SPDX-License-Identifier: AGPL-3.0-only
"""#4601: delivered envelopes must not invent pending decisions or answers."""
from ameesh import decisions
from .test_l124_decisions import _Base


class ClosedDecisionTest(_Base):
    def test_delivered_request_is_closed_without_a_fabricated_answer(self):
        view = self.ask()
        # Historical mail forwarding marked the original delivered but kept
        # its payload pending. The replacement request remains live.
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now(),"
                        " status = 'delivered' WHERE id = %s", (view['id'],))
        replacement = self.ask()
        closed = decisions.get(self.db, view['id'])
        self.assertEqual(closed['state'], 'closed')
        self.assertFalse(closed['overdue'])
        self.assertIsNone(closed['answer'])
        self.assertIsNone(closed['withdrawn'])
        queue = decisions.queue(self.db, human=closed['owner'], include_closed=True)
        self.assertEqual([r['id'] for r in queue['pending']], [replacement['id']])
        self.assertEqual(queue['closed'][0]['state'], 'closed')
        rendered = '\n'.join(decisions.format_view(closed))
        self.assertIn('without an answer or withdrawal', rendered)
        self.assertNotIn('answer: ameesh decide', rendered)
        self.assertNotIn('answered by', rendered)
        for operation in (
            lambda: decisions.withdraw(self.cfg, self.db, view['id'], requester='orch'),
            lambda: self.answer(view['id'], 'a'),
        ):
            with self.assertRaisesRegex(decisions.DecisionError,
                                        'closed without an answer or withdrawal'):
                operation()
        self.assertEqual(decisions.get(self.db, replacement['id'])['state'], 'pending')
        self.assertEqual(self.lot_state(), 'waiting_human')

    def test_closed_legacy_payload_does_not_invent_an_answer(self):
        view = decisions.view_of({'id': 1, 'closed_ts': 10, 'payload': {'decision': {}}})
        self.assertEqual(view['state'], 'closed')

    def test_real_answers_and_withdrawals_keep_their_state(self):
        for state, record in (('answered', {'by': 'human:alice', 'text': 'a'}),
                              ('withdrawn', {'by': 'orch'})):
            key = 'answer' if state == 'answered' else 'withdrawn'
            view = decisions.view_of({'id': 1, 'closed_ts': 10,
                                      'payload': {'decision': {'state': state, key: record}}})
            self.assertEqual(view['state'], state)
            self.assertEqual(view[key], record)
