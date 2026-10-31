# SPDX-License-Identifier: AGPL-3.0-only
"""Real PostgreSQL concurrency and rollback for the dormant S1 boundary."""
import copy
import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock

from ameesh import agent_rename, jcs, registry, work
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

    def test_result_id_placeholder_is_data_on_both_drivers(self):
        from ameesh import db as db_mod
        for driver in ('psycopg', 'psql'):
            with self.subTest(driver=driver):
                if driver == 'psycopg' and importlib.util.find_spec('psycopg') is None:
                    self.skipTest('psycopg is not installed')
                db = db_mod.connect(self.cfg, driver=driver)
                self.addCleanup(db.close)
                for table in ('sandbox_results', 'sandbox_turns', 'spend_pending',
                              'turn_resources', 'account_reservations'):
                    self.db.execute('delete from ' + table)
                self.admit()
                sandbox.ameesh_sandbox_bind(db, 'turn-1', 'runner', 7, 'uid')
                result = self.result()
                result['result_id'] = 'result-%s-1'
                before = self.db.query("select count(*) as n from ameesh_events where type='sandbox.result'")[0]['n']
                receipt = sandbox.ameesh_sandbox_complete(
                    db, 'turn-1', 'runner', 7, result, settle=self.settle)
                replay = sandbox.ameesh_sandbox_complete(
                    db, 'turn-1', 'runner', 7, result, settle=self.settle)
                self.assertEqual(receipt['result_id'], result['result_id'])
                self.assertEqual(replay, dict(receipt, replayed=True))
                self.assertEqual(self.db.query("select state from account_reservations")[0]['state'], 'kept')
                self.assertEqual(self.db.query("select count(*) as n from ameesh_events where type='sandbox.result'")[0]['n'], before + 1)
                event = self.db.query("select data from ameesh_events where type='sandbox.result' order by id desc limit 1")[0]
                self.assertEqual(event['data']['result_id'], result['result_id'])

    def test_event_sql_failure_preserves_bind_complete_and_replay(self):
        self.db.execute("alter table ameesh_events add constraint test_sandbox_event_failure"
                        " check (type !~ '^sandbox[.]') not valid")
        self.addCleanup(self.db.execute,
                        "alter table ameesh_events drop constraint test_sandbox_event_failure")
        from ameesh import db as db_mod
        for driver in ('psycopg', 'psql'):
            with self.subTest(driver=driver):
                if driver == 'psycopg' and importlib.util.find_spec('psycopg') is None:
                    self.skipTest('psycopg is not installed')
                db = db_mod.connect(self.cfg, driver=driver)
                self.addCleanup(db.close)
                self.db.execute("delete from sandbox_results")
                self.db.execute("delete from sandbox_turns")
                self.db.execute("delete from spend_pending")
                self.db.execute("delete from turn_resources")
                self.db.execute("delete from account_reservations")
                self.admit()
                with self.assertLogs(sandbox.__name__, level='WARNING'):
                    sandbox.ameesh_sandbox_bind(db, 'turn-1', 'runner', 7, 'uid')
                    receipt = sandbox.ameesh_sandbox_complete(
                        db, 'turn-1', 'runner', 7, self.result(), settle=self.settle)
                replay = sandbox.ameesh_sandbox_complete(
                    db, 'turn-1', 'runner', 7, self.result(), settle=self.settle)
                self.assertEqual(replay, dict(receipt, replayed=True))
                self.assertEqual(self.db.query("select state from sandbox_turns")[0]['state'], 'succeeded')
                self.assertEqual(self.db.query("select state from account_reservations")[0]['state'], 'kept')

    def test_event_sql_failure_preserves_cancel(self):
        self.db.execute("alter table ameesh_events add constraint test_sandbox_event_failure"
                        " check (type !~ '^sandbox[.]') not valid")
        self.addCleanup(self.db.execute,
                        "alter table ameesh_events drop constraint test_sandbox_event_failure")
        with self.assertLogs(sandbox.__name__, level='WARNING'):
            got = sandbox.ameesh_sandbox_cancel(self.db, 'turn-1', 'runner', 7,
                                               observed=True, settle=self.settle)
        self.assertEqual(got['state'], 'cancelled')
        self.assertEqual(self.db.query("select state from sandbox_turns")[0]['state'], 'cancelled')

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
                ('missing',lambda r: r['usage'].update(tokens=1)),
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
        # §9 precedence: a wrong echoed epoch beats a wrong scope (#4881).
        stale = self.result()
        stale['epoch'] = 8
        stale['scope'] = dict(self.scope, pr=99)
        stale['scope_digest'] = 'wrong'
        self.refusal('stale', self.complete, stale)
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

    def test_generic_close_refuses_a_sandbox_turn_by_identity(self):
        self.bind(); self.complete()
        self.refusal('wrong-completion',registry.end_turn,self.db,'sand','runner',7,
                     status='idle',status_text=None,error=None,cost_usd=None,
                     turn_id='turn-1')

    def test_generic_close_of_an_unattached_local_turn_is_allowed(self):
        # The local turn's turn_resources row may be missing (the runner never
        # fails a turn for that); closing it must not depend on the ledger's
        # latest row (#4881, auditor N).
        self.bind(); self.complete()
        self.assertFalse(self.db.query(
            "select * from turn_resources where turn_id = 'local-2'"))
        self.assertTrue(registry.end_turn(self.db,'sand','runner',7,status='idle',
                                         status_text=None,error=None,cost_usd=None,
                                         turn_id='local-2'))

    def test_sandbox_session_is_typed_when_the_profile_admits_one(self):
        # The admission snapshot is immutable: re-create the fixture row rather
        # than update its limits, as the production guard demands.
        row = self.db.query('select * from sandbox_turns')[0]
        row['limits'] = dict(row['limits'], session='reviewer')
        self.db.execute('delete from sandbox_turns')
        self.db.execute('insert into sandbox_turns select * from'
                        ' jsonb_populate_record(null::sandbox_turns, %s::jsonb)',
                        (json.dumps(row),))
        self.bind()
        wrong = self.result(); wrong['sandbox_session'] = 42
        self.refusal('missing', self.complete, wrong)
        good = self.result(); good['sandbox_session'] = 'reviewer-session-1'
        self.complete(good)
        self.assertEqual(self.db.query(
            'select sandbox_session from sandbox_turns')[0]['sandbox_session'],
            'reviewer-session-1')

    def test_cancel_event_uses_the_design_name(self):
        self.bind()
        before = self.db.query("select count(*) as n from ameesh_events"
                               " where type = 'sandbox.cancelled'")[0]['n']
        sandbox.ameesh_sandbox_cancel(self.db,'turn-1','runner',7)
        sandbox.ameesh_sandbox_cancel(self.db,'turn-1','runner',7,observed=True,
                                      settle=self.settle)
        after = self.db.query("select count(*) as n from ameesh_events"
                              " where type = 'sandbox.cancelled'")[0]['n']
        self.assertEqual(after, before + 2)
        self.assertFalse(self.db.query(
            "select * from ameesh_events where type = 'sandbox.cancel'"))

    def test_stale_lease_wins_over_the_stored_result(self):
        # §4 steps 2-3 and §9: revocation and a stale lease are the authority;
        # a stored result is replayed only while this owner/epoch holds the
        # lease. Pinned, not reversed: the design's order is fixed (#4881).
        self.bind()
        self.complete()
        self.db.execute("update agent_registry set lease_expires_at ="
                        " now() - interval '1 second' where name = 'sand'")
        self.refusal('stale', self.complete)
        self.refusal('stale', sandbox.ameesh_sandbox_cancel, self.db,
                     'turn-1', 'runner', 7)
        self.assertEqual(self.db.query(
            'select count(*) as n from sandbox_results')[0]['n'], 1)

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

    def test_pending_prompt_closes_queued_without_losing_prompt(self):
        self.bind()
        registry.set_pending_prompt(self.db, 'sand', 'next request')
        self.complete()
        self.assertEqual(self.db.query(
            "select status,pending_prompt from agent_registry where name = 'sand'")[0],
            dict(status='queued',pending_prompt='next request'))

    def test_rename_preserves_identity_and_normalizes_later_alias_writes(self):
        with mock.patch.object(work, 'corrector', return_value='human:owner'):
            self.db.execute("update agent_registry set status = 'idle',"
                            " lease_expires_at = now() - interval '1 second' where name = 'sand'")
            agent_rename.rename(self.cfg,self.db,'sand','sand-renamed',reason='sandbox test')
            try:
                row = self.db.query('select * from sandbox_turns')[0]
                self.assertEqual(row['agent'],'sand-renamed')
                self.assertEqual(row['owner'],'runner')
                self.db.execute("update agent_registry set status = 'running',"
                                " lease_expires_at = now() + interval '1 hour' where name = 'sand-renamed'")
                self.bind()  # _live follows the same identity after the rename.
                with self.assertRaises(Exception):
                    self.db.execute("update sandbox_turns set agent = 'unrelated' where turn_id = 'turn-1'")
                self.db.execute("update sandbox_turns set state = 'cancelled' where turn_id = 'turn-1'")
                row.update(turn_id='alias-turn',spend_key='alias-spend',job_name='alias-job',
                           attempt=2,agent='sand')
                self.db.execute('insert into sandbox_turns select * from'
                                ' jsonb_populate_record(null::sandbox_turns,%s::jsonb)',
                                (json.dumps(row),))
                self.assertEqual(self.db.query(
                    "select agent from sandbox_turns where turn_id = 'alias-turn'")[0]['agent'],
                    'sand-renamed')
            finally:
                self.db.execute("update agent_registry set status = 'idle',"
                                " lease_expires_at = now() - interval '1 second' where name = 'sand-renamed'")
                agent_rename.rename(self.cfg,self.db,'sand-renamed','sand',reason='restore fixture')
