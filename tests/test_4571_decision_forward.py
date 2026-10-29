# SPDX-License-Identifier: AGPL-3.0-only
"""#4571: human requests survive dead-mail handling; repair historical forwards."""
import time
import os
import io
from types import SimpleNamespace
from unittest import mock

from ameesh import authority, decisions, mail, registry, signing, storage, undeliverable
from .test_l124_decisions import _Base, ALICE


class HumanDecisionForwardTest(_Base):
    def test_human_mail_is_neither_dead_nor_expired_and_forward_is_refused(self):
        view = self.ask()
        mail.send(self.db, 'orch', ALICE, 'human note')
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - interval '16 minutes'"
                        " WHERE recipient = %s", (ALICE,))
        box = storage.of(self.db).mailbox
        # Raw dead mail retains ordinary human mail; #4519 filters only
        # declared human boxes when building alerts. Decisions stay excluded.
        self.assertEqual([(r['recipient'], r['n']) for r in box.dead_letters()],
                         [(ALICE, 1)])
        self.assertEqual(undeliverable.alerts(self.cfg, self.db, [], time.time()), [])
        for dry in (True, False):
            with self.assertRaisesRegex(undeliverable.Refused, 'human mailbox'):
                undeliverable.forward(self.cfg, self.db, ALICE, 'dev1',
                                      actor='orch', dry_run=dry)
            self.assertEqual(box.expire_dead_letters(ALICE, {}, time.time(),
                                                     dry_run=dry), [])
        with self.assertRaisesRegex(storage.MailboxRefused, 'human mailbox'):
            box.forward(ALICE, 'dev1', 'orch')
        self.assertEqual(decisions.get(self.db, view['id'])['state'], 'pending')
        with mock.patch('ameesh.decisions.agent_in_ancestry', return_value=None):
            result = self.answer(view['id'], 'a')
        self.assertEqual(result['decision']['state'], 'answered')

    def historical_forward(self, view):
        # Reproduce the old forward exactly enough to exercise the stored pair.
        ident = view['id']
        [copy] = self.db.query(
            "INSERT INTO agent_mailbox(sender, recipient, body, kind, payload, work_item_id, meta)"
            " SELECT sender, 'dev1', body, kind, payload, work_item_id,"
            " jsonb_build_object('forwarded_from', jsonb_build_object('agent', recipient,"
            " 'message_id', id)) FROM agent_mailbox WHERE id = %s RETURNING id", (ident,))
        self.db.execute(
            "UPDATE agent_mailbox SET delivered_at = now(), status = 'delivered',"
            " meta = jsonb_build_object('forwarded', jsonb_build_object('to', 'dev1',"
            " 'message_id', %s::bigint)) WHERE id = %s", (copy['id'], ident))
        return copy['id']

    def test_repair_restores_original_and_withdraws_copy_without_changing_question(self):
        view = self.ask()
        original = storage.of(self.db).decisions.get(view['id'])
        copy = self.historical_forward(view)
        result = storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        self.assertEqual(result, {'decision': view['id'], 'withdrawn_copy': copy})
        restored = storage.of(self.db).decisions.get(view['id'])
        self.assertEqual(restored['recipient'], ALICE)
        self.assertEqual(restored['payload'], original['payload'])
        self.assertIsNone(restored['closed_ts'])
        self.assertEqual(decisions.get(self.db, copy)['state'], 'withdrawn')
        with self.assertRaises(ValueError):
            storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        with mock.patch('ameesh.decisions.agent_in_ancestry', return_value=None):
            result = self.answer(view['id'], 'a')
        self.assertEqual(result['decision']['state'], 'answered')

    def test_repair_refuses_answered_copy_without_reopening_original(self):
        view = self.ask()
        copy = self.historical_forward(view)
        storage.of(self.db).decisions.close(copy, status='answered',
                                           record={'state': 'answered', 'answer': {'text': 'a'}})
        with self.assertRaisesRegex(ValueError, 'already answered'):
            storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        self.assertIsNotNone(storage.of(self.db).decisions.get(view['id'])['closed_ts'])

    def test_forward_filters_decisions_even_when_addressed_to_an_agent(self):
        view = self.ask()
        self.db.execute("UPDATE agent_mailbox SET recipient = 'dead-agent' WHERE id = %s",
                        (view['id'],))
        mail.send(self.db, 'orch', 'dead-agent', 'ordinary mail')
        preview = undeliverable.forward(self.cfg, self.db, 'dead-agent', 'dev1',
                                        actor='orch', dry_run=True)
        self.assertNotIn(view['id'], [r['id'] for r in preview['messages']])
        moved = storage.of(self.db).mailbox.forward('dead-agent', 'dev1', 'orch')
        self.assertEqual(len(moved), 1)
        self.assertNotEqual(moved[0]['original_id'], view['id'])
        self.assertIsNone(storage.of(self.db).decisions.get(view['id'])['closed_ts'])

    def test_repaired_original_still_requires_and_accepts_owner_signature(self):
        view = self.ask(needs_signature=True)
        self.historical_forward(view)
        storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        with self.assertRaisesRegex(decisions.DecisionError, 'SIGNED answer required'):
            self.answer(view['id'], 'a')
        registry.upsert(self.db, 'alice', harness='other', host=self.cfg.host)
        private, public, _ = signing.write_keypair(os.path.join(self.tmp, 'owner-key'), 'alice')
        authority.register_key(self.db, 'alice', signing.read_public(public), role='owner')
        result = self.answer(view['id'], 'a', key_path=private)
        ok, detail = decisions.verify_proof(self.db, result['decision'])
        self.assertTrue(ok, detail)

    def test_repair_refuses_forwarded_copy_and_preserves_both_rows(self):
        view = self.ask()
        copy = self.historical_forward(view)
        self.db.execute("UPDATE agent_mailbox SET meta = meta ||"
                        " '{\"forwarded\": {\"message_id\": 999}}'::jsonb WHERE id = %s", (copy,))
        with self.assertRaisesRegex(ValueError, 'forward chain'):
            storage.of(self.db).decisions.repair_forward(view['id'], actor='orch')
        self.assertIsNotNone(storage.of(self.db).decisions.get(view['id'])['closed_ts'])
        self.assertIsNone(storage.of(self.db).decisions.get(copy)['closed_ts'])

    def test_repair_cli_uses_bound_identity(self):
        view = self.ask()
        self.historical_forward(view)
        # Keep the fixture connection alive after the CLI closes its handle.
        handle = mock.Mock(wraps=self.db)
        handle.close = mock.Mock()
        output = io.StringIO()
        with mock.patch('ameesh.decisions._open', return_value=handle), \
                mock.patch('ameesh.decisions.identity.resolve_binding',
                           return_value=SimpleNamespace(ok=True, name='orch')), \
                mock.patch('sys.stdout', output):
            self.assertEqual(decisions.cmd_repair_forward(self.cfg, [str(view['id'])]), 0)
        self.assertIn('restored to pending', output.getvalue())
        self.assertIsNone(storage.of(self.db).decisions.get(view['id'])['closed_ts'])

    def test_repair_cli_refuses_missing_identity_without_touching_messages(self):
        view = self.ask()
        self.historical_forward(view)
        handle = mock.Mock(wraps=self.db)
        handle.close = mock.Mock()
        with mock.patch('ameesh.decisions._open', return_value=handle), \
                mock.patch('ameesh.decisions.identity.resolve_binding',
                           return_value=SimpleNamespace(ok=False)):
            with self.assertRaisesRegex(decisions.DecisionError, 'agent identity'):
                decisions.cmd_repair_forward(self.cfg, [str(view['id'])])
        self.assertIsNotNone(storage.of(self.db).decisions.get(view['id'])['closed_ts'])
