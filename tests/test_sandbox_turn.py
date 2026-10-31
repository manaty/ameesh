# SPDX-License-Identifier: AGPL-3.0-only
"""Dormant adapter: real S1/S3 transactions, fake provider, child protocol."""
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase, mock

from ameesh import sandbox_provider as p, turn_context, storage
from ameesh.sandbox_supervisor import SandboxSupervisor
from ameesh.sandbox_turn import SandboxTurn, authorize
from ameesh.storage.postgres import sandbox as s1, turn_admission as s3
from . import test_4872_turn_admission as admission_fixture
from .support import PgTestCase

IMAGE = 'registry/reviewer@sha256:' + 'a' * 64


def context():
    return turn_context.build(
        turn_id='turn-key', agent='agent', host='host', kind='mail', started_ts=1,
        session=None, work_item_id=None, lease_owner='owner', lease_epoch=7,
        lease_expires_ts=9999999999, requests=[dict(id=21, sender='requester',
            created_ts=1, payload={'schema':'ameesh-sandbox-request/1'})],
        harness='codex', model=None, budget_usd=2, spent_usd=0,
        spend_key='opaque-key-without-derived-parts',
        marker=dict(start=0, created_ts=1, turn_id='turn-key'), reservation=None)


class AuthorizationTest(TestCase):
    def test_authorization_uses_reserved_copy_even_pending_delivery(self):
        doc = context()
        self.assertIsNone(doc['requests'][0]['delivered_ts'])
        self.assertEqual(authorize(doc, 21)['schema'], 'ameesh-sandbox-request/1')
        for ident in (22, True, '21'):
            with self.assertRaises(s1.Refusal) as caught:
                authorize(doc, ident)
            self.assertEqual(caught.exception.code, 'unauthorized-request')
        doc['requests'][0]['payload'] = {}
        with self.assertRaises(s1.Refusal) as caught:
            authorize(doc, 21)
        self.assertEqual(caught.exception.code, 'not-a-sandbox-request')

    def test_current_reads_and_verifies_context(self):
        doc = context()
        with mock.patch('ameesh.turn_context.current', return_value=doc) as read, \
             mock.patch('ameesh.turn_context.verify') as verify:
            adapter = SandboxTurn.current(object(), environ={'test':'value'})
        read.assert_called_once_with(environ={'test':'value'})
        verify.assert_called_once_with(doc, adapter.db)
        doc['lease']['epoch'] = 100
        self.assertEqual(adapter.epoch, 7)
        self.assertEqual(adapter.context['lease']['epoch'], 7)


class AdapterTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ('tranche_relay_requests','sandbox_results','sandbox_turns','tranche_events',
                      'tranche_reservations','tranche_limits','account_reservations',
                      'account_budgets','turn_costs','spend_pending'):
            self.db.execute('delete from ' + table)
        storage.of(self.db).budgets.account_put('provider',100,actor='human:t')
        s3.tranche_create(self.db,'migration','project',50,starts_at='2026-01-01',
                          actor='human:t',admitted_ref='decision:1')
        self.envelope = admission_fixture.TurnAdmissionTest.snapshot(self)
        self.envelope.update(image_digest=IMAGE.split('@')[1], profile_ref='profile',
                             placement_ref='place')
        self.envelope['limits'].update(turns=1, tool_calls=10, capabilities={})
        self.adapter = SandboxTurn(self.db, context())
        self.provider = p.FakeJobProvider()
        self.spec = p.JobSpec(p.JobIdentity('turn-key','agent',7,'place','profile','default'),
                              IMAGE, 60, 3600)
        self.db.execute("update agent_registry set turns=0, status='running' where name='agent'")
        self.db.execute("insert into spend_pending (agent,start_index,turn_id) values ('agent',0,'turn-key')")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.adapter.close)

    def admit(self, adapter=None):
        return (adapter or self.adapter).admit(21, snapshot=self.envelope,
                                                billing_account='provider', tranche_id='migration')

    def event(self, *, recovery=False):
        events = []
        SandboxSupervisor(self.provider, events.append, orphan_after=10).step(
            self.adapter.snapshot(), self.spec, recovery=recovery)
        return events[0]

    def result(self):
        row = self.adapter.snapshot()
        return dict(schema='ameesh-sandbox-result/1', turn_id='turn-key', epoch=7,
                    scope=row['scope'], scope_digest=row['scope_digest'], result_id='result-key',
                    outcome='accepted', verdict='green', findings=[], evidence=[],
                    usage=dict(turns=1,tool_calls=3,seconds=12,model_calls=2),
                    cost={'usd':0}, sandbox_session=None, source=row['source'],
                    finished_at='2026-10-31T14:00:00Z', image_digest=IMAGE.split('@')[1])

    def complete_job(self):
        result = self.result()
        _, digest = s1._canonical(result)
        self.provider.append_log('default', self.spec.identity.name, json.dumps(result))
        self.provider.set_status('default', self.spec.identity.name,
            {'conditions':[{'type':'Complete','status':'True'}]},
            message=json.dumps({'result_sha256':digest}))

    def test_intent_before_child_and_opaque_key_replay(self):
        with self.assertRaises(s1.Refusal):
            self.adapter.start(self.spec, provider_factory='ameesh.sandbox_provider:FakeJobProvider',
                               state_dir=self.temp.name, orphan_after=10, timeout=5)
        self.admit()
        self.assertTrue(self.admit()['replayed'])
        self.assertEqual(self.adapter.snapshot()['spend_key'], context()['accounting']['spend_key'])
        import subprocess
        with mock.patch.dict(os.environ, {'AMEESH_DSN':'private-test-dsn',
                                         'PROVIDER_API_KEY':'private-test-key'}), \
             mock.patch('ameesh.sandbox_turn.subprocess.Popen', wraps=subprocess.Popen) as spawn:
            child = self.adapter.start(self.spec, provider_factory='ameesh.sandbox_provider:FakeJobProvider',
                                      state_dir=self.temp.name, orphan_after=10, timeout=5)
        self.assertNotIn('AMEESH_DSN', spawn.call_args.kwargs['env'])
        self.assertNotIn('PROVIDER_API_KEY', spawn.call_args.kwargs['env'])
        self.assertEqual(child.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.getpgid(child.process.pid), os.getpgrp())
        self.assertEqual(len(child.process.args), 4)
        self.assertNotIn(context()['accounting']['spend_key'], str(child.process.args))
        event, row = self.adapter.step()
        self.assertEqual(event['kind'], 'bound')
        self.assertEqual(row['job_uid'], event['job_uid'])
        event, row = self.adapter.step()
        self.assertEqual(event['kind'], 'heartbeat')
        handoff = child.path
        self.adapter.close()
        self.assertFalse(handoff.exists())

    def test_unauthorized_has_no_intent_or_reservation(self):
        with self.assertRaises(s1.Refusal):
            self.adapter.admit(99, snapshot=self.envelope, billing_account='provider', tranche_id='migration')
        self.assertFalse(self.db.query('select * from sandbox_turns'))
        self.assertFalse(self.db.query('select * from tranche_reservations'))

    def test_adopt_after_adapter_loss_before_bind_without_second_job(self):
        self.admit()
        first = self.event()  # create succeeded, adapter never applied bind
        replacement = SandboxTurn(self.db, context())
        self.assertTrue(self.admit(replacement)['replayed'])
        event = self.event()
        self.assertEqual(event['job_uid'], first['job_uid'])
        replacement.consume(event)
        self.assertEqual(len(self.provider.list('default', {})), 1)
        self.complete_job()
        receipt = replacement.consume(self.event())
        self.assertFalse(receipt['replayed'])
        self.assertEqual(self.db.query('select state from tranche_reservations')[0]['state'], 'kept')
        self.assertEqual(self.db.query("select turns from agent_registry where name='agent'")[0]['turns'],1)

    def test_cancel_before_create(self):
        self.admit()
        self.adapter.cancel()
        event = self.event()
        self.assertEqual(event['kind'], 'cancelled')
        self.adapter.consume(event)
        self.assertFalse(self.provider.list('default', {}))
        self.assertEqual(self.adapter.snapshot()['state'], 'cancelled')

    def test_cancel_after_create_before_bind_rejects_delayed_bind(self):
        self.admit()
        bound = self.event()
        self.adapter.cancel()
        with self.assertRaises(s1.Refusal) as caught:
            self.adapter.consume(bound)
        self.assertEqual(caught.exception.code, 'revoked')
        self.adapter.consume(self.event())
        self.provider.finish_delete('default', self.spec.identity.name)
        self.adapter.consume(self.event())
        self.assertEqual(self.adapter.snapshot()['state'], 'cancelled')
        self.assertIsNone(self.adapter.snapshot()['job_uid'])

    def test_cancel_after_bind_observes_before_settlement(self):
        self.admit()
        self.adapter.consume(self.event())
        self.adapter.cancel()
        self.adapter.consume(self.event())
        self.assertEqual(self.adapter.snapshot()['state'], 'cancelling')
        self.assertEqual(self.db.query('select state from tranche_reservations')[0]['state'], 'open')
        self.provider.finish_delete('default', self.spec.identity.name)
        self.adapter.consume(self.event())
        self.assertEqual(self.adapter.snapshot()['state'], 'cancelled')
        self.assertEqual(self.db.query('select state from tranche_reservations')[0]['state'], 'kept')

    def test_concurrent_result_replay_applies_once_with_s3(self):
        self.admit()
        self.adapter.consume(self.event())
        self.complete_job()
        event = self.event()
        connections = [self.connect(), self.connect()]
        gate = threading.Barrier(2)
        def apply(db):
            gate.wait(timeout=10)
            return SandboxTurn(db, context()).consume(event)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(apply, connections))
        finally:
            for db in connections:
                db.close()
        self.assertEqual(sum(r['replayed'] for r in receipts), 1)
        self.assertTrue(self.adapter.consume(event)['replayed'])
        self.assertEqual(self.db.query('select count(*) as n from sandbox_results')[0]['n'],1)
        self.assertEqual(self.db.query("select turns from agent_registry where name='agent'")[0]['turns'],1)
        self.assertEqual(self.db.query("select count(*) as n from tranche_events where action='kept'")[0]['n'],1)


    def test_second_request_cannot_replace_turn_authorization(self):
        doc = context()
        doc['requests'].append(dict(doc['requests'][0], id=22))
        adapter = SandboxTurn(self.db, doc)
        self.admit(adapter)
        with self.assertRaises(s1.Refusal) as caught:
            adapter.admit(22, snapshot=self.envelope,
                          billing_account='provider', tranche_id='migration')
        self.assertEqual(caught.exception.code, 'replay-mismatch')
        self.assertEqual(adapter.snapshot()['request_ids'], [21])

    def test_recovery_of_terminated_job_is_expired(self):
        self.admit()
        self.adapter.consume(self.event())
        self.complete_job()
        recovery = SandboxTurn.recover(self.db, self.adapter.turn_id)
        recovery.cancel()
        recovery.consume(self.event(recovery=True))
        self.assertEqual(recovery.snapshot()['state'], 'expired')
        self.assertFalse(self.db.query('select * from sandbox_results'))
        self.assertEqual(self.db.query('select state from tranche_reservations')[0]['state'], 'kept')

    def test_cancel_wins_over_an_already_read_result(self):
        self.admit()
        self.adapter.consume(self.event())
        self.complete_job()
        event = self.event()
        self.adapter.cancel()
        with self.assertRaises(s1.Refusal) as caught:
            self.adapter.consume(event)
        self.assertEqual(caught.exception.code, 'revoked')
        self.adapter.consume(self.event())
        self.assertFalse(self.db.query('select * from sandbox_results'))

    def test_timeout_discards_transport_but_preserves_intent(self):
        self.admit()
        child = mock.Mock()
        child.step.side_effect = TimeoutError('observation timeout')
        self.adapter.child = child
        with self.assertRaises(TimeoutError):
            self.adapter.step()
        child.close.assert_called_once()
        self.assertIsNone(self.adapter.child)
        self.assertEqual(self.adapter.snapshot()['state'], 'intent')

    def test_lost_lease_recovery_cannot_complete_or_close_replacement(self):
        self.admit()
        self.adapter.consume(self.event())
        self.db.execute("update agent_registry set lease_owner='replacement',lease_epoch=8 where name='agent'")
        recovery = SandboxTurn.recover(self.db, self.adapter.turn_id)
        recovery.cancel()
        recovery.consume(self.event(recovery=True))
        self.provider.finish_delete('default', self.spec.identity.name)
        recovery.consume(self.event(recovery=True))
        self.assertEqual(recovery.snapshot()['state'], 'lost')
        self.assertEqual(self.db.query("select turns from agent_registry where name='agent'")[0]['turns'],0)
        with self.assertRaises(s1.Refusal):
            recovery.consume(dict(kind='result',turn_id=self.adapter.turn_id,result=self.result()))
