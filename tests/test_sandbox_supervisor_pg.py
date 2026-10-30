# SPDX-License-Identifier: AGPL-3.0-only
"""Runner event handoff to real S1 transactions; no cluster or activation."""
import json

from ameesh import sandbox_provider as p
from ameesh.sandbox_supervisor import SandboxSupervisor
from ameesh.storage.postgres import sandbox
from . import test_4867_sandbox as fixture
from .support import PgTestCase

IMAGE = 'registry/reviewer@sha256:' + 'a' * 64


class SupervisorStorageTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ('sandbox_results', 'sandbox_turns', 'spend_pending',
                      'turn_resources', 'account_reservations'):
            self.db.execute('delete from ' + table)
        self.register('sand', 'deepseek')
        self.db.execute("update agent_registry set lease_owner='runner', lease_epoch=7,"
                        " lease_expires_at=now() + interval '1 hour', turns=0,"
                        " session_id='native-session', status='running' where name='sand'")
        identity = p.JobIdentity('turn-1', 'sand', 7, 'place', 'profile', 'default')
        self.spec = p.JobSpec(identity, IMAGE, 120, 3600)
        self.scope = dict(repository='example/repo', pr=12, head='a'*40,
                          base='main', generation=1, attempt=1)
        self.digest = 'scope-digest'
        self.source = dict(contract='ameesh-sandbox/1')
        self.db.execute('insert into sandbox_turns (turn_id, spend_key, agent, owner,'
                        ' lease_epoch, scope, scope_digest, retry_family, scope_generation,'
                        ' attempt, max_attempts, profile_ref, profile_digest, placement_ref,'
                        ' placement_digest, canon_ref, limits, image_digest, provider,'
                        ' namespace, job_name, deadline_ts, source) values'
                        " ('turn-1','spend-1','sand','runner',7,%s::jsonb,%s,'family',1,1,2,"
                        " 'profile','pd','place','pld','canon',%s::jsonb,%s,"
                        " 'kubernetes','default',%s,now() + interval '10 minutes',%s::jsonb)",
                        (json.dumps(self.scope), self.digest,
                         json.dumps(dict(turns=1, tool_calls=10, turn_seconds=60,
                                         tools=[], session='none', capabilities=[])),
                         IMAGE, identity.name, json.dumps(self.source)))
        self.db.execute("insert into spend_pending (agent,start_index,turn_id) values ('sand',0,'turn-1')")
        self.db.execute("insert into turn_resources (turn_id,agent,host) values ('turn-1','sand','host')")
        self.db.execute("insert into account_reservations (billing_account,agent,usd,period,spend_key)"
                        " values ('test','sand',2,current_date,'spend-1')")
        self.provider = p.FakeJobProvider()
        self.events = []

    def snapshot(self):
        return self.db.query("select * from sandbox_turns where turn_id='turn-1'")[0]

    def step(self, **kwargs):
        # Deliberately restart the supervisor between every observation.
        supervisor = SandboxSupervisor(self.provider, self.events.append, orphan_after=10)
        return supervisor.step(self.snapshot(), self.spec, **kwargs)

    def bind(self):
        event = self.step()
        self.assertEqual(event.kind, 'bound')
        sandbox.ameesh_sandbox_bind(self.db, 'turn-1', 'runner', 7, event.job_uid)

    def complete_job(self):
        result = fixture.SandboxTest.result(self)
        _, digest = sandbox._canonical(result)
        self.provider.append_log('default', self.spec.identity.name, json.dumps(result))
        self.provider.set_status('default', self.spec.identity.name,
                                 {'conditions': [{'type': 'Complete', 'status': 'True'}]},
                                 message=json.dumps({'result_sha256': digest}))

    def settle(self, tx, row, outcome):
        fixture.SandboxTest.settle(tx, row, outcome)

    def test_restart_result_submit_replay_closes_once(self):
        self.bind()
        self.complete_job()
        event = self.step()
        self.assertEqual(event.kind, 'result')
        receipt = sandbox.ameesh_sandbox_complete(
            self.db, 'turn-1', 'runner', 7, event.result, settle=self.settle)
        replay = sandbox.ameesh_sandbox_complete(
            self.db, 'turn-1', 'runner', 7, event.result, settle=self.settle)
        self.assertFalse(receipt['replayed'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.db.query("select turns from agent_registry where name='sand'")[0]['turns'], 1)
        self.assertEqual(self.step().kind, 'terminal')
        self.assertEqual(self.db.query('select * from spend_pending'), [])

    def test_fenced_cancel_result_race_and_async_observation(self):
        self.bind()
        self.complete_job()
        result = self.step().result
        sandbox.ameesh_sandbox_cancel(self.db, 'turn-1', 'runner', 7)
        with self.assertRaises(sandbox.Refusal) as refused:
            sandbox.ameesh_sandbox_complete(self.db, 'turn-1', 'runner', 7,
                                           result, settle=self.settle)
        self.assertEqual(refused.exception.code, 'revoked')
        self.assertEqual(self.step().kind, 'cancelled')
        sandbox.ameesh_sandbox_cancel(self.db, 'turn-1', 'runner', 7,
                                     observed=True, settle=self.settle)
        self.assertEqual(self.snapshot()['state'], 'cancelled')
        self.assertEqual(self.db.query("select turns from agent_registry where name='sand'")[0]['turns'], 1)

    def test_unknown_deletion_stays_live_until_observed(self):
        self.bind()
        sandbox.ameesh_sandbox_cancel(self.db, 'turn-1', 'runner', 7)
        self.assertEqual(self.step().state, 'cancelling')
        self.assertEqual(self.snapshot()['state'], 'cancelling')
        self.assertEqual(self.db.query("select turns from agent_registry where name='sand'")[0]['turns'], 0)
        self.provider.finish_delete('default', self.spec.identity.name)
        self.assertEqual(self.step().state, 'cancelled')
        sandbox.ameesh_sandbox_cancel(self.db, 'turn-1', 'runner', 7,
                                     observed=True, settle=self.settle)
        self.assertEqual(self.snapshot()['state'], 'cancelled')

    def test_lost_epoch_sweep_never_closes_replacement_lease(self):
        self.bind()
        self.db.execute("update agent_registry set lease_owner='new-runner', lease_epoch=8,"
                        " session_id='replacement', turns=0 where name='sand'")
        sandbox.ameesh_sandbox_cancel(self.db, 'turn-1', 'runner', 7,
                                     recovery=True, orphaned=True)
        self.assertEqual(self.step(recovery=True).state, 'orphaned')
        self.provider.finish_delete('default', self.spec.identity.name)
        event = self.step(recovery=True)
        self.assertEqual(event.state, 'lost')
        sandbox.ameesh_sandbox_cancel(self.db, 'turn-1', 'runner', 7, recovery=True,
                                     observed=True, terminal_state=event.state, settle=self.settle)
        row = self.db.query("select lease_owner,lease_epoch,session_id,turns from agent_registry where name='sand'")[0]
        self.assertEqual((row['lease_owner'], row['lease_epoch'], row['session_id'], row['turns']),
                         ('new-runner', 8, 'replacement', 0))
