# SPDX-License-Identifier: AGPL-3.0-only
"""Real PostgreSQL concurrency and rollback for the dormant S1 boundary."""
import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from ameesh import jcs, registry
from ameesh.storage.postgres import sandbox
from .support import PgTestCase


class SandboxTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.events_before = self.db.query("select count(*) as n from ameesh_events where type = 'sandbox.result'")[0]['n']
        self.db.execute('delete from sandbox_results')
        self.db.execute('delete from sandbox_turns')
        self.db.execute('delete from spend_pending')
        self.db.execute('delete from turn_resources')
        self.db.execute('delete from account_reservations')
        self.register('sand', 'deepseek')
        self.db.execute("update agent_registry set lease_owner = 'runner', lease_epoch = 7,"
                        " lease_expires_at = now() + interval '1 hour', turns = 0,"
                        " session_id = 'native-session', status = 'running' where name = 'sand'")
        self.admit()

    def admit(self):
        self.scope = dict(repository='example/repo', pr=12, head='a'*40,
                          base='main', generation=1, attempt=1)
        self.digest = 'sha256:' + hashlib.sha256(jcs.canonicalize(self.scope)).hexdigest()
        self.source = dict(release='test', commit='b'*40, contract='ameesh-sandbox/1')
        self.db.execute('insert into sandbox_turns (turn_id, spend_key, agent, owner,'
                        ' lease_epoch, scope, scope_digest, retry_family, scope_generation,'
                        ' attempt, max_attempts, profile_ref, profile_digest, placement_ref,'
                        ' placement_digest, canon_ref, limits, image_digest, provider,'
                        ' namespace, job_name, deadline_ts, source) values'
                        " ('turn-1','spend-1','sand','runner',7,%s::jsonb,%s,'family',1,1,2,"
                        " 'profile','pd','place','pld','canon',%s::jsonb,'sha256:image',"
                        " 'kubernetes','default','job-1',now() + interval '10 minutes',%s::jsonb)",
                        (json.dumps(self.scope), self.digest,
                         json.dumps(dict(turns=1, tool_calls=10, turn_seconds=60,
                                         tools=['repository.read'], session='none', capabilities={'deploy':False})),
                         json.dumps(self.source)))
        self.db.execute("insert into spend_pending (agent,start_index,turn_id) values ('sand',0,'turn-1')")
        self.db.execute("insert into turn_resources (turn_id,agent,host) values ('turn-1','sand','host')")
        self.db.execute("insert into account_reservations (billing_account,agent,usd,period,spend_key)"
                        " values ('test','sand',2,current_date,'spend-1')")

    def result(self):
        return dict(schema='ameesh-sandbox-result/1', turn_id='turn-1', epoch=7,
                    scope=self.scope, scope_digest=self.digest, result_id='result-1',
                    outcome='accepted', verdict='green', findings=[], evidence=[],
                    usage=dict(turns=1,tool_calls=3,seconds=12,model_calls=2),
                    cost=dict(usd=0.84), sandbox_session=None, source=self.source,
                    finished_at='2026-10-30T08:00:00Z')

    @staticmethod
    def settle(tx, row, outcome):
        # S3 contract double: one transactional write, never a nested commit.
        tx.execute("update account_reservations set state = 'kept',"
                   " closed_by = %s where spend_key = %s and state = 'open'",
                   (outcome, row['spend_key']))

    def bind(self):
        return sandbox.ameesh_sandbox_bind(self.db, 'turn-1','runner',7,'uid-1')

    def complete(self, result=None, **kwargs):
        return sandbox.ameesh_sandbox_complete(self.db,'turn-1','runner',7,
                                               self.result() if result is None else result,
                                               settle=kwargs.get('settle', self.settle))

    def refusal(self, code, func, *args, **kwargs):
        with self.assertRaises(sandbox.Refusal) as caught:
            func(*args, **kwargs)
        self.assertEqual(code,caught.exception.code)

    def test_exactly_once_replay_after_terminal_and_duplicate(self):
        self.bind()
        receipt = self.complete()
        self.db.execute("update sandbox_turns set state = 'succeeded' where turn_id = 'turn-1'")
        self.assertEqual(self.complete(), dict(receipt,replayed=True))
        changed = self.result(); changed['verdict'] = 'red'
        self.refusal('duplicate',self.complete,changed)
        self.assertEqual(self.db.query("select turns,session_id from agent_registry where name = 'sand'")[0],
                         dict(turns=1,session_id='native-session'))
        self.assertEqual(self.db.query('select count(*) as n from sandbox_results')[0]['n'],1)
        self.assertFalse(self.db.query("select * from spend_pending where agent = 'sand'"))
        self.assertEqual(self.db.query('select status from turn_resources')[0]['status'],'done')
        self.assertEqual(self.db.query('select count(*) as n from ameesh_events where type = %s',
                                       ('sandbox.result',))[0]['n'],self.events_before + 1)

    def test_bind_idempotent_and_conflict(self):
        self.assertEqual(self.bind(),self.bind())
        self.refusal('job-conflict',sandbox.ameesh_sandbox_bind,self.db,'turn-1','runner',7,'uid-2')

    def test_rollback_if_settlement_fails(self):
        self.bind()
        def fail(tx,row,outcome):
            self.settle(tx,row,outcome)
            raise RuntimeError('injected settlement failure')
        with self.assertRaises(RuntimeError):
            self.complete(settle=fail)
        self.assertFalse(self.db.query('select * from sandbox_results'))
        self.assertEqual(self.db.query('select state from account_reservations')[0]['state'],'open')
        self.assertTrue(self.db.query('select * from spend_pending'))
        with self.assertRaises(RuntimeError):
            self.complete(settle=None)
        self.assertEqual(self.db.query("select turns from agent_registry where name = 'sand'")[0]['turns'],0)

    def test_cancel_fence_then_observation(self):
        self.bind()
        cancel = lambda **kw: sandbox.ameesh_sandbox_cancel(self.db,'turn-1','runner',7,**kw)
        self.assertEqual(cancel()['state'],'cancelling')
        self.refusal('revoked',self.complete)
        self.assertEqual(cancel(orphaned=True)['state'],'orphaned')
        self.assertTrue(self.db.query('select * from spend_pending'))
        self.assertEqual(cancel(observed=True,settle=self.settle)['state'],'cancelled')
        self.refusal('not-cancellable',cancel)
        self.assertFalse(self.db.query('select * from spend_pending'))

    def test_intent_without_uid_may_hide_created_job(self):
        self.assertEqual(sandbox.ameesh_sandbox_cancel(self.db,'turn-1','runner',7)['state'],'cancelling')
        self.refusal('revoked',self.bind)

    def test_result_wins_cancel(self):
        self.bind(); self.complete()
        self.refusal('already-completed',sandbox.ameesh_sandbox_cancel,self.db,'turn-1','runner',7)

    def test_refusal_precedence_and_validation(self):
        self.bind()
        for code, change in (
                ('scope-mismatch', lambda r: r.update(scope_digest='wrong')),
                ('missing',lambda r: r['usage'].pop('tool_calls')),
                ('limit-exceeded',lambda r: r['usage'].update(tool_calls=11)),
                ('profile-refused',lambda r: r.update(tools=['deploy'])),
                ('profile-refused',lambda r: r.update(capabilities={'deploy':True})),
                ('profile-refused',lambda r: r.update(sandbox_session='foreign')),
                ('source-mismatch',lambda r: r['source'].update(commit='wrong')),
                ('missing',lambda r: r.update(raw_transcript='private')),
                ('missing',lambda r: r.update(findings=[{'severity':'N','code':'x','summary':'ok','transcript':'private'}])),
                ('contract-mismatch',lambda r: r.update(schema='v2'))):
            with self.subTest(code=code):
                result = copy.deepcopy(self.result()); change(result)
                self.refusal(code,self.complete,result)
        self.refusal('missing', self.complete, {'bad':float('nan')})
        self.db.execute("update agent_registry set lease_expires_at = now() - interval '1 second' where name = 'sand'")
        self.refusal('stale',self.complete)
        self.db.execute("update sandbox_turns set cancel_requested_at = now() where turn_id = 'turn-1'")
        self.refusal('revoked',self.complete)
        self.refusal('unknown-turn',sandbox.ameesh_sandbox_complete,self.db,'absent','runner',7,{})

    def test_marker_for_replacement_turn_survives(self):
        self.bind()
        self.db.execute("update spend_pending set turn_id = 'turn-2' where agent = 'sand'")
        self.complete()
        self.assertEqual(self.db.query('select turn_id from spend_pending')[0]['turn_id'],'turn-2')

    def test_generic_close_cannot_bypass_result(self):
        self.refusal('wrong-completion',registry.end_turn,self.db,'sand','runner',7,
                     status='idle',status_text=None,error=None,cost_usd=None)

    def race(self, actions):
        gate = Barrier(len(actions))
        def run(action):
            db = self.connect()
            try:
                gate.wait(timeout=10)
                try:
                    return action(db)
                except sandbox.Refusal as exc:
                    return exc.code
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            return list(pool.map(run,actions))

    def test_concurrent_completion_replays_and_counts_once(self):
        self.bind()
        def done(db):
            return sandbox.ameesh_sandbox_complete(db,'turn-1','runner',7,self.result(),settle=self.settle)
        results = self.race([done,done])
        self.assertEqual(sorted(r['replayed'] for r in results),[False,True])
        self.assertEqual(self.db.query("select turns from agent_registry where name = 'sand'")[0]['turns'],1)

    def test_concurrent_cancel_vs_result_has_one_winner(self):
        self.bind()
        results = self.race([
            lambda db: sandbox.ameesh_sandbox_complete(db,'turn-1','runner',7,self.result(),settle=self.settle),
            lambda db: sandbox.ameesh_sandbox_cancel(db,'turn-1','runner',7)])
        self.assertTrue('revoked' in results or 'already-completed' in results,results)
        if 'revoked' in results:
            self.assertFalse(self.db.query('select * from sandbox_results'))
        else:
            self.assertEqual(self.db.query('select count(*) as n from sandbox_results')[0]['n'],1)

    def test_generic_close_after_result_requires_a_new_local_turn(self):
        self.bind(); self.complete()
        self.refusal('wrong-completion',registry.end_turn,self.db,'sand','runner',7,
                     status='idle',status_text=None,error=None,cost_usd=None)
        self.db.execute("insert into turn_resources (turn_id,agent,host) values ('local-2','sand','host')")
        self.assertTrue(registry.end_turn(self.db,'sand','runner',7,status='idle',
                                         status_text=None,error=None,cost_usd=None))

    def test_admission_snapshot_cannot_be_rewritten(self):
        with self.assertRaises(Exception):
            self.db.execute("update sandbox_turns set scope_digest = 'new' where turn_id = 'turn-1'")
        self.bind()
        with self.assertRaises(Exception):
            self.db.execute("update sandbox_turns set job_uid = 'replacement' where turn_id = 'turn-1'")

    def test_orphan_sweep_never_mutates_new_lease_or_turn(self):
        self.bind()
        self.db.execute("update agent_registry set lease_owner = 'new-runner', lease_epoch = 8,"
                        " status = 'running', turns = 5 where name = 'sand'")
        self.db.execute("update spend_pending set turn_id = 'new-turn' where agent = 'sand'")
        self.refusal('stale', self.complete)
        result = sandbox.ameesh_sandbox_cancel(
            self.db,'turn-1','runner',7,observed=True,recovery=True,
            terminal_state='expired',settle=self.settle)
        self.assertEqual(result['state'],'expired')
        row = self.db.query("select lease_owner,lease_epoch,status,turns from agent_registry where name = 'sand'")[0]
        self.assertEqual(row,dict(lease_owner='new-runner',lease_epoch=8,status='running',turns=5))
        self.assertEqual(self.db.query('select turn_id from spend_pending')[0]['turn_id'],'new-turn')
        self.assertEqual(self.db.query('select status from turn_resources')[0]['status'],'done')

    def test_concurrent_binding_never_replaces_uid(self):
        results = self.race([
            lambda db: sandbox.ameesh_sandbox_bind(db,'turn-1','runner',7,'uid-A'),
            lambda db: sandbox.ameesh_sandbox_bind(db,'turn-1','runner',7,'uid-B')])
        self.assertEqual(results.count('job-conflict'),1)
        uid = self.db.query('select job_uid from sandbox_turns')[0]['job_uid']
        self.assertIn(dict(turn_id='turn-1',job_uid=uid),results)

    def test_late_without_stored_result(self):
        # Fixture replacement preserves the production snapshot guard.
        row = self.db.query('select * from sandbox_turns')[0]
        row['deadline_ts'] = '2000-01-01T00:00:00Z'
        self.db.execute('delete from sandbox_turns')
        self.db.execute('insert into sandbox_turns select * from'
                        ' jsonb_populate_record(null::sandbox_turns, %s::jsonb)',
                        (json.dumps(row),))
        self.refusal('late',self.complete)
        self.db.execute("update sandbox_turns set cancel_requested_at = now()")
        self.refusal('revoked',self.complete)

    def test_concurrent_different_results_refuse_duplicate(self):
        self.bind()
        other = self.result(); other['verdict'] = 'red'
        results = self.race([
            lambda db: sandbox.ameesh_sandbox_complete(db,'turn-1','runner',7,self.result(),settle=self.settle),
            lambda db: sandbox.ameesh_sandbox_complete(db,'turn-1','runner',7,other,settle=self.settle)])
        self.assertEqual(results.count('duplicate'),1)
        self.assertEqual(self.db.query("select turns from agent_registry where name = 'sand'")[0]['turns'],1)

    def test_paused_agent_closes_queued(self):
        self.bind()
        self.db.execute("update agent_registry set operator_paused = true where name = 'sand'")
        self.complete()
        self.assertEqual(self.db.query("select status from agent_registry where name = 'sand'")[0]['status'],'queued')
