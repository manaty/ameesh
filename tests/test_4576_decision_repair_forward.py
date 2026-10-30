# SPDX-License-Identifier: AGPL-3.0-only
"""#4576: repair a forwarded decision whose copy was already withdrawn."""
import io
import json
from types import SimpleNamespace
from unittest import mock

from ameesh import decisions, storage

from .test_l124_decisions import _Base


class RepairForwardWithdrawnCopyTest(_Base):
    def historical_forward(self, view):
        # Reproduce the old forward exactly enough to exercise the stored pair.
        [copy] = self.db.query(
            "INSERT INTO agent_mailbox(sender, recipient, body, kind, payload, work_item_id, meta)"
            " SELECT sender, 'dev1', body, kind, payload, work_item_id,"
            " jsonb_build_object('forwarded_from', jsonb_build_object('agent', recipient,"
            " 'message_id', id)) FROM agent_mailbox WHERE id = %s RETURNING id", (view['id'],))
        self.db.execute(
            "UPDATE agent_mailbox SET delivered_at = now(), status = 'delivered',"
            " meta = jsonb_build_object('forwarded', jsonb_build_object('to', 'dev1',"
            " 'message_id', %s::bigint)) WHERE id = %s", (copy['id'], view['id']))
        return copy['id']

    def withdraw_copy(self, copy):
        decisions.withdraw(self.cfg, self.db, copy, requester='orch',
                           why='copie renvoyée au bureau')

    def test_repair_accepts_withdrawn_copy_and_keeps_its_trace(self):
        view = self.ask()
        original_payload = storage.of(self.db).decisions.get(view['id'])['payload']
        copy = self.historical_forward(view)
        self.withdraw_copy(copy)
        withdrawn_view = decisions.get(self.db, copy)
        kept_payload = storage.of(self.db).decisions.get(copy)['payload']
        kept_meta = self.db.query("SELECT meta FROM agent_mailbox WHERE id = %s",
                                  (copy,))[0]['meta']
        result = storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        restored = storage.of(self.db).decisions.get(view['id'])
        kept = storage.of(self.db).decisions.get(copy)
        self.assertEqual(result, {'decision': view['id'], 'withdrawn_copy': copy,
                                  'copy_already_withdrawn': True})
        # The original keeps its question, options, deadline and signature ask.
        self.assertEqual(restored['payload'], original_payload)
        self.assertEqual(decisions.get(self.db, view['id'])['state'], 'pending')
        self.assertIsNone(restored['closed_ts'])
        # The copy is untouched: its withdrawal record is the audit.
        self.assertEqual(kept['payload'], kept_payload)
        self.assertEqual(decisions.get(self.db, copy)['state'], 'withdrawn')
        self.assertEqual(decisions.get(self.db, copy)['withdrawn'],
                         withdrawn_view['withdrawn'])
        self.assertEqual(self.db.query("SELECT meta FROM agent_mailbox WHERE id = %s",
                                       (copy,))[0]['meta'], kept_meta)
        meta = self.db.query("SELECT meta FROM agent_mailbox WHERE id = %s",
                             (view['id'],))[0]['meta']
        self.assertNotIn('forwarded', meta)
        self.assertEqual(meta['forward_repaired']['copy'], copy)
        with mock.patch('ameesh.decisions.agent_in_ancestry', return_value=None):
            answer = self.answer(view['id'], 'a')
        self.assertEqual(answer['decision']['state'], 'answered')

    def test_repair_refuses_answered_original(self):
        view = self.ask()
        copy = self.historical_forward(view)
        self.db.execute(
            "UPDATE agent_mailbox SET status = 'answered',"
            " payload = jsonb_set(payload, '{decision}', (payload->'decision') || %s::jsonb)"
            " WHERE id = %s",
            (json.dumps({'state': 'answered', 'answer': {'text': 'a'}}), view['id']))
        with self.assertRaisesRegex(ValueError, 'already answered'):
            storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        self.assertEqual(decisions.get(self.db, view['id'])['state'], 'answered')
        self.assertIsNone(storage.of(self.db).decisions.get(copy)['closed_ts'])

    def test_repair_refuses_original_withdrawn_at_origin(self):
        view = self.ask()
        copy = self.historical_forward(view)
        self.db.execute(
            "UPDATE agent_mailbox SET payload = jsonb_set(payload, '{decision}',"
            " (payload->'decision') || %s::jsonb) WHERE id = %s",
            (json.dumps({'state': 'withdrawn',
                         'withdrawn': {'by': 'orch', 'why': 'before the forward'}}),
             view['id']))
        with self.assertRaisesRegex(ValueError, 'already answered or withdrawn'):
            storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        self.assertIsNotNone(storage.of(self.db).decisions.get(view['id'])['closed_ts'])
        self.assertIsNone(storage.of(self.db).decisions.get(copy)['closed_ts'])

    def test_repair_refuses_changed_question_with_withdrawn_copy(self):
        view = self.ask()
        copy = self.historical_forward(view)
        self.withdraw_copy(copy)
        self.db.execute(
            "UPDATE agent_mailbox SET payload ="
            " jsonb_set(payload, '{decision,question}', to_jsonb(%s::text)) WHERE id = %s",
            ('Question changée après le renvoi', view['id']))
        with self.assertRaisesRegex(ValueError, 'changed copy'):
            storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        self.assertIsNotNone(storage.of(self.db).decisions.get(view['id'])['closed_ts'])
        self.assertEqual(decisions.get(self.db, copy)['state'], 'withdrawn')

    def test_cli_reports_copy_already_withdrawn(self):
        view = self.ask()
        copy = self.historical_forward(view)
        self.withdraw_copy(copy)
        handle = mock.Mock(wraps=self.db)
        handle.close = mock.Mock()
        output = io.StringIO()
        with mock.patch('ameesh.decisions._open', return_value=handle), \
                mock.patch('ameesh.decisions.identity.resolve_binding',
                           return_value=SimpleNamespace(ok=True, name='orch')), \
                mock.patch('sys.stdout', output):
            self.assertEqual(decisions.cmd_repair_forward(self.cfg, [str(view['id'])]), 0)
        self.assertIn('was already withdrawn (kept)', output.getvalue())
        self.assertEqual(decisions.get(self.db, view['id'])['state'], 'pending')
