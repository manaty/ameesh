# SPDX-License-Identifier: AGPL-3.0-only
"""Atomic two-envelope admission and uncertain settlement on real PostgreSQL."""
import copy
import dataclasses
import os
import time
from pathlib import Path
from unittest import mock
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from ameesh import storage
from ameesh.storage.postgres import turn_admission as api, sandbox
from .support import PgTestCase


class TurnAdmissionTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ('tranche_relay_requests','sandbox_results','sandbox_turns','tranche_events','tranche_reservations',
                      'tranche_limits','account_reservations','account_budgets','turn_costs'):
            self.db.execute('delete from ' + table)
        storage.of(self.db).budgets.account_put('provider',100,actor='human:t')
        api.tranche_create(self.db,'migration','project',50,starts_at='2026-01-01',
                           actor='human:t',admitted_ref='decision:1')

    def admit(self, key='key', agent='agent', usd=2, **kwargs):
        return api.ameesh_admit_turn(kwargs.pop('db',self.db),agent,'provider',usd,key,
                                     'migration','sandbox' if kwargs.get('sandbox') else 'runner',
                                     turn_id='turn-' + key,**kwargs)

    def refusal(self, code, fn, *args, **kwargs):
        with self.assertRaises(sandbox.Refusal) as caught:
            fn(*args,**kwargs)
        self.assertEqual(code,caught.exception.code)

    def limits(self, amount):
        self.db.execute('update tranche_limits set cap_usd = %s',(amount,))

    def reservation(self,key='key'):
        return self.db.query('select * from tranche_reservations where spend_key = %s',(key,))[0]

    def spend(self, key, usd, *, relay=False, void=False):
        self.db.execute('insert into turn_costs (agent,harness,model,usd,turn_id,source,'
                        'billing_account,spend_key,void_reason) values'
                        " ('agent','deepseek','model',%s,%s,%s,'provider',%s,%s)",
                        (usd,'turn-' + key,'relay' if relay else 'harness',None if relay else key,
                         'unknown' if void else None))

    def snapshot(self,agent='agent'):
        self.register(agent,'deepseek')
        self.db.execute("update agent_registry set lease_owner = 'owner',lease_epoch = 7,"
                        " lease_expires_at = now() + interval '1 hour' where name = %s",(agent,))
        return dict(owner='owner',lease_epoch=7,request_ids=[],scope=dict(repository='a/b',
                    pr=1,head='a'*40,base='main',generation=1),max_attempts=2,
                    profile_ref='profile',profile_digest='pd',placement_ref='host',
                    placement_digest='pld',canon_ref='c'*40,limits=dict(usd_per_turn=2,
                    turn_seconds=60,tools=[],capabilities={},session='none'),image_digest='sha256:image',
                    provider='kubernetes',namespace='default',source=dict(contract='ameesh-sandbox/1'),
                    max_concurrent_jobs=10)

    def test_concurrent_bounds_and_account_rollback(self):
        self.limits(6)
        conns = [self.connect() for _ in range(8)]
        gate = Barrier(8)
        def go(i):
            gate.wait(timeout=15)
            try:
                return self.admit('key%d'%i,'a%d'%i,db=conns[i])
            except sandbox.Refusal as e:
                return e.code
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(go,range(8)))
        finally:
            for db in conns: db.close()
        self.assertEqual(sum(isinstance(r,dict) for r in rows),3)
        self.assertEqual(rows.count('tranche-exhausted'),5)
        self.assertEqual(self.db.query('select count(*) as n from account_reservations')[0]['n'],3)
        self.limits(50)
        storage.of(self.db).budgets.account_put('provider',6,actor='human:t')
        self.refusal('account-exhausted',self.admit,'no-room','new-agent')
        self.assertFalse(self.db.query("select * from tranche_reservations where spend_key = 'no-room'"))

    def test_same_key_race_and_replay_after_closed_full(self):
        conns=[self.connect(),self.connect()]; gate=Barrier(2)
        def go(db):
            gate.wait(timeout=15)
            return self.admit(db=db)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(go,conns))
        finally:
            for db in conns: db.close()
        self.assertEqual(sum(r['replayed'] for r in results),1)
        api.tranche_close(self.db,'migration',actor='human:t',why='stop')
        self.assertTrue(self.admit()['replayed'])
        self.refusal('replay-mismatch',self.admit,usd=3)
        self.assertEqual(self.db.query('select count(*) as n from account_reservations')[0]['n'],1)

    def test_unknown_kept_and_human_release_both_envelopes(self):
        got=self.admit(); api.ameesh_settle_turn(self.db,'key',None)
        self.assertEqual(self.reservation()['state'],'kept')
        self.assertEqual(float(self.reservation()['usd']),2)
        self.assertEqual(self.db.query('select state from account_reservations')[0]['state'],'kept')
        self.refusal('human-required',api.tranche_release,self.db,got['tranche_reservation_id'],
                     actor='agent',why='guess')
        api.tranche_release(self.db,got['tranche_reservation_id'],actor='human:t',why='checked externally')
        self.assertEqual(self.reservation()['state'],'released')
        self.assertEqual(self.db.query('select state from account_reservations')[0]['state'],'released')
        self.assertEqual(self.db.query("select reason from tranche_events where action = 'release'")[0]['reason'],
                         'checked externally')

    def test_actual_release_overdraft_and_settlement_idempotency(self):
        self.admit(); self.spend('key',0.5)
        api.ameesh_settle_turn(self.db,'key',0.5)
        api.ameesh_settle_turn(self.db,'key',1.5)
        self.assertEqual(float(self.reservation()['usd']),0.5)
        self.admit('over'); self.spend('over',3)
        api.ameesh_settle_turn(self.db,'over',3)
        self.assertEqual(float(self.reservation('over')['usd']),3)
        self.refusal('tranche-unavailable',self.admit,'blocked')
        api.tranche_close(self.db,'migration',actor='human:t',why='verified overspend',reconcile=True)
        self.admit('again')

    def test_account_refusal_rolls_back_old_reservation_kept(self):
        self.admit()
        storage.of(self.db).budgets.account_put('provider',2,actor='human:t')
        self.refusal('account-exhausted',self.admit,'second')
        self.assertEqual(self.db.query('select state from account_reservations')[0]['state'],'open')
        self.assertEqual(self.reservation()['state'],'open')

    def test_sandbox_atomic_intent_attempts_and_response_loss(self):
        snapshot=self.snapshot()
        one=self.admit(sandbox=snapshot)
        self.assertEqual(one['attempt'],1)
        self.assertEqual(one['scope']['attempt'],1)
        self.assertTrue(self.admit(sandbox=snapshot)['replayed'])
        wrong=copy.deepcopy(snapshot); wrong['source']['contract']='other'
        self.refusal('replay-mismatch',self.admit,sandbox=wrong)
        self.db.execute("update sandbox_turns set state = 'lost'")
        two=self.admit('retry',sandbox=snapshot)
        self.assertEqual(two['attempt'],2)
        self.db.execute("update sandbox_turns set state = 'lost'")
        self.refusal('retry-exhausted',self.admit,'third',sandbox=snapshot)
        api.tranche_close(self.db,'migration',actor='human:t',why='stop')
        self.assertTrue(self.admit('retry',sandbox=snapshot)['replayed'])
        self.assertEqual(self.db.query('select count(*) as n from tranche_reservations')[0]['n'],2)

    def test_sandbox_cost_uses_relay_only_and_missing_is_kept(self):
        snap=self.snapshot(); self.admit(sandbox=snap)
        self.spend('key',99) # Job/local reported spend cannot substitute for relay measurement
        with self.db.transaction() as tx:
            row=tx.query('select * from sandbox_turns')[0]
            got=api.sandbox_settle(tx,row,'succeeded')
        self.assertIsNone(got['usd'])
        self.assertEqual(self.reservation()['state'],'kept')

    def test_callback_transaction_rolls_back_both_envelopes(self):
        self.admit(sandbox=self.snapshot()); self.spend('key',0.6,relay=True)
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as tx:
                row=tx.query('select * from sandbox_turns')[0]
                self.assertAlmostEqual(api.sandbox_settle(tx,row,'succeeded')['usd'],0.6)
                raise RuntimeError('failed receipt')
        self.assertEqual(self.reservation()['state'],'open')
        self.assertEqual(self.db.query('select state from account_reservations')[0]['state'],'open')
        with self.db.transaction() as tx:
            self.assertAlmostEqual(api.sandbox_settle(tx,tx.query('select * from sandbox_turns')[0],
                                                      'succeeded')['usd'],0.6)

    def test_snapshot_and_invalid_money_fail_without_reservations(self):
        for usd in (0,-1,float('nan'),float('inf'),0.0000001,True):
            self.refusal('invalid-cost',self.admit,usd=usd)
        snapshot=self.snapshot(); snapshot['limits']['usd_per_turn']=1
        self.refusal('profile-refused',self.admit,sandbox=snapshot)
        self.assertFalse(self.db.query('select * from account_reservations'))

    def test_cli_is_human_only_audited_and_visible(self):
        env=self.env(AMEESH_HUMANS='chief',USER='chief')
        denied=self.mesh('budget','tranche','create','second','--project','project',
                         '--usd','10','--starts-at','2026-01-01','--admitted-ref','decision:2',
                         env=self.env(AGENT_MAIL_NAME='agent'))
        self.assertEqual(denied.returncode,1,denied.stderr)
        self.assertFalse(api.tranche_show(self.db,'second'))
        got=self.mesh('budget','tranche','create','second','--project','project',
                      '--usd','10','--starts-at','2026-01-01','--admitted-ref','decision:2',env=env)
        self.assertEqual(got.returncode,0,got.stderr)
        shown=self.mesh('budget','tranche','show','second','--json',env=env)
        self.assertEqual(shown.returncode,0,shown.stderr)
        self.assertEqual(json.loads(shown.stdout)[0]['admitted_by'],'human:chief')
        close=self.mesh('budget','tranche','close','second','--why','done',env=env)
        self.assertEqual(close.returncode,0,close.stderr)
        self.assertEqual(api.tranche_show(self.db,'second')[0]['state'],'closed')

    def test_relay_cost_attribution_requires_token_turn_and_fenced_owner(self):
        from ameesh import relay
        self.admit(sandbox=self.snapshot())
        writer=object.__new__(relay.Relay)
        writer.db=self.db
        writer._db_lock=threading.Lock()
        row=dict(agent='agent',harness='deepseek',turn='header-spoof',model='model',
                 session=None,usd=0.7,input_tokens=1,cached_input_tokens=0,output_tokens=1,
                 executor='exec',lease_owner='owner',lease_epoch=7,turn_id='turn-key')
        self.assertTrue(writer._write(row))
        self.assertEqual(self.db.query('select turn_id,billing_account from turn_costs')[0],
                         dict(turn_id='turn-key',billing_account='provider'))
        self.db.execute('delete from turn_costs')
        row['turn_id']=None
        self.assertTrue(writer._write(row))
        self.assertIsNone(self.db.query('select billing_account from turn_costs')[0]['billing_account'])
        self.db.execute('delete from turn_costs')
        row.update(turn_id='turn-key',lease_epoch=6)
        self.assertTrue(writer._write(row))
        self.assertIsNone(self.db.query('select billing_account from turn_costs')[0]['billing_account'])

    def test_s1_completion_settles_receipt_and_reservations_atomically(self):
        snap=self.snapshot()
        got=self.admit(sandbox=snap)
        sandbox.ameesh_sandbox_bind(self.db,'turn-key','owner',7,'uid')
        self.spend('key',0.6,relay=True)
        result=dict(schema='ameesh-sandbox-result/1',turn_id='turn-key',epoch=7,
                    scope=got['scope'],scope_digest=got['scope_digest'],result_id='result',
                    outcome='accepted',verdict='green',findings=[],evidence=[],
                    usage=dict(turns=1,tool_calls=0,seconds=1,model_calls=1),cost=dict(usd=99),
                    sandbox_session=None,source=snap['source'],finished_at='2026-10-30T00:00:00Z')
        receipt=sandbox.ameesh_sandbox_complete(self.db,'turn-key','owner',7,result,settle=api.sandbox_settle)
        self.assertFalse(receipt['replayed'])
        replay=sandbox.ameesh_sandbox_complete(self.db,'turn-key','owner',7,result,settle=api.sandbox_settle)
        self.assertTrue(replay['replayed'])
        self.assertEqual(float(self.reservation()['usd']),0.6)
        self.assertEqual(self.db.query("select turns from agent_registry where name='agent'")[0]['turns'],1)

    def test_pending_relay_request_keeps_full_bound_and_record_retry_is_idempotent(self):
        from ameesh import relay
        self.admit(sandbox=self.snapshot())
        identity=relay.Identity(executor='exec',host='host',agent='agent',lease_owner='owner',
                                lease_epoch=7,turn='turn-key')
        marker=api.relay_begin(self.db,identity)
        self.spend('key',0.4,relay=True)
        with self.db.transaction() as tx:
            outcome=api.sandbox_settle(tx,tx.query('select * from sandbox_turns')[0],'succeeded')
        self.assertEqual(outcome['state'],'kept')
        self.assertEqual(float(self.reservation()['usd']),2)
        writer=object.__new__(relay.Relay); writer.db=self.db; writer._db_lock=threading.Lock()
        row=dict(agent='agent',harness='deepseek',turn='turn-key',model='model',
                 session=None,usd=0.7,input_tokens=1,cached_input_tokens=0,output_tokens=1,
                 executor='exec',lease_owner='owner',lease_epoch=7,turn_id='turn-key',relay_request=marker)
        self.assertTrue(writer._write(row)); self.assertTrue(writer._write(row))
        self.assertEqual(self.db.query('select count(*) as n from turn_costs')[0]['n'],2)
        self.db.execute("update sandbox_turns set state = 'succeeded'")
        self.refusal('revoked',api.relay_begin,self.db,identity)

    def test_existing_account_entry_points_cannot_settle_only_half(self):
        self.admit(); self.spend('key',0.8)
        budgets=storage.of(self.db).budgets
        budgets.account_settle('key',state='settled',actor='runner')
        self.assertEqual(self.reservation()['state'],'settled')
        self.assertEqual(float(self.reservation()['usd']),0.8)
        self.admit('uncertain'); budgets.account_settle('uncertain',state='kept',actor='runner')
        account=self.db.query("select id from account_reservations where spend_key='uncertain'")[0]
        budgets.account_release(account['id'],actor='human:t')
        self.assertEqual(self.reservation('uncertain')['state'],'released')
        self.admit('abort'); budgets.account_settle('abort',state='released',actor='runner')
        self.assertEqual(self.reservation('abort')['state'],'released')

    def test_local_selector_reads_audited_profile_and_never_environment(self):
        from ameesh import canon, turn_budget
        from .test_sandbox_canon import agent_card, git
        root=Path(self.tmp)/'canon'
        (root/'agents').mkdir(parents=True)
        card=root/'agents'/'worker.md'
        card.write_text(agent_card(tranche='migration'))
        git(str(root),'init','-q'); git(str(root),'add','-A'); git(str(root),'commit','-qm','admitted')
        pinned=git(str(root),'rev-parse','HEAD')
        card.write_text(agent_card(tranche='bypass'))
        git(str(root),'add','-A'); git(str(root),'commit','-qm','not synced yet')
        loaded=canon.load(str(root),ref='HEAD')
        state=dict(status='ok',last_good_commit=pinned,checked_ts=time.time())
        cfg=dataclasses.replace(self.cfg,canon=str(root))
        with mock.patch('ameesh.canon.load_configured',return_value=[loaded]), \
             mock.patch('ameesh.canon_sync.state',return_value=state), \
             mock.patch.dict(os.environ,{'AMEESH_TRANCHE':'bypass'}):
            self.assertEqual(turn_budget.local_tranche(cfg,self.db,'tpk-reviewer'),'migration')
        with mock.patch('ameesh.canon.load_configured',return_value=[loaded]), \
             mock.patch('ameesh.canon_sync.state',return_value=dict(state,checked_ts=1)):
            self.refusal('profile-refused',turn_budget.local_tranche,cfg,self.db,'tpk-reviewer')

    def test_runner_uses_tranche_before_launch_and_keeps_unscoped_behavior(self):
        from ameesh import accounts, registry, turn_budget
        from ameesh.runner import Runner, AgentWorker
        from .support import FAKEBIN
        cfg=dataclasses.replace(self.cfg,budget_check_interval=0.0,accounts={'claude':[dict(
            name='k',type='api_key_env',key_env='K',billing_account='provider',check_credentials=False)]})
        runner=Runner(cfg,self.db,once=True)
        cwd=os.path.join(self.tmp,'work');os.makedirs(cwd)
        self.register('worker','claude',cwd=cwd)
        lease=registry.claim(self.db,'worker',runner.runner_id,3600)
        worker=AgentWorker(runner,registry.get(self.db,'worker'),lease)
        profile=accounts.Profile(harness='claude',name='k',kind='api_key_env',env='K',key_env='K',
                                 billing_account='provider',reserve_usd=2)
        try:
            with mock.patch.dict(os.environ,{'AMEESH_BIN_DIR':FAKEBIN,'K':'sk-test'}), \
                 mock.patch('ameesh.turn_budget.local_tranche',return_value='missing'):
                worker._account=profile
                self.assertFalse(worker.run_turn(dict(kind='prompt',prompt='t',ids=[])))
                self.assertIn('tranche-unavailable',registry.get(self.db,'worker')['status_text'])
                self.assertFalse(self.db.query('select * from turn_costs'))
            with mock.patch.dict(os.environ,{'AMEESH_BIN_DIR':FAKEBIN,'K':'sk-test'}), \
                 mock.patch('ameesh.turn_budget.local_tranche',return_value='migration'):
                worker._account=profile
                self.assertTrue(worker.run_turn(dict(kind='prompt',prompt='t',ids=[])))
                row=self.db.query('select * from tranche_reservations')[0]
                self.assertEqual((row['path'],row['state']),('runner','settled'))
            # All currently unscoped agents still delegate to the existing API;
            # no tranche schema lookup or cap is inferred from account/host/env.
            with mock.patch('ameesh.turn_budget.ameesh_admit_turn') as admit, \
                 mock.patch('ameesh.account_budget.reserve',return_value='old result') as old:
                self.assertEqual(turn_budget.reserve_local(self.cfg,self.db,'ordinary',profile,'key','turn'),
                                 'old result')
                old.assert_called_once_with(self.db,'ordinary',profile,'key');admit.assert_not_called()
        finally:
            worker.watchdog_stop.set()

    def test_retry_family_is_atomic_across_agents_and_accounts_share_tranche(self):
        storage.of(self.db).budgets.account_put('second',100,actor='human:t')
        self.limits(4)
        self.admit('first','one')
        api.ameesh_admit_turn(self.db,'two','second',2,'second-key','migration','runner',turn_id='second-turn')
        self.refusal('tranche-exhausted',self.admit,'third','three')
        self.limits(50)
        snaps=[self.snapshot('sand%d'%i) for i in range(4)]
        conns=[self.connect() for _ in snaps];gate=Barrier(4)
        def go(i):
            gate.wait(timeout=15)
            try:
                return self.admit('sandbox%d'%i,'sand%d'%i,db=conns[i],sandbox=snaps[i])
            except sandbox.Refusal as e:
                return e.code
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                rows=list(pool.map(go,range(4)))
        finally:
            for db in conns:db.close()
        self.assertEqual(sorted(r['attempt'] for r in rows if isinstance(r,dict)),[1,2])
        self.assertEqual(rows.count('retry-exhausted'),2)
        self.assertEqual(self.db.query('select count(*) as n from sandbox_turns')[0]['n'],2)

    def test_tranche_window_is_checked_after_waiting_for_the_account(self):
        self.db.execute("update tranche_limits set ends_at = clock_timestamp() + interval '0.5 seconds'")
        blocker=self.connect(); waiter=self.connect(); result=[]
        def go():
            try:
                result.append(self.admit(db=waiter))
            except sandbox.Refusal as exc:
                result.append(exc.code)
        try:
            with blocker.transaction() as tx:
                api._lock_account(tx,'provider')
                thread=threading.Thread(target=go); thread.start()
                tx.query('select pg_sleep(0.7)')
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result,['tranche-unavailable'])
            self.assertFalse(self.db.query('select * from tranche_reservations'))
            self.assertFalse(self.db.query('select * from account_reservations'))
        finally:
            blocker.close();waiter.close()

    def test_new_placement_revision_does_not_hide_surviving_jobs(self):
        one=self.snapshot('one');one.update(placement_ref='member:host@old',max_concurrent_jobs=1)
        self.admit('one','one',sandbox=one)
        two=self.snapshot('two');two.update(placement_ref='member:host@new',max_concurrent_jobs=1)
        two['scope']=dict(two['scope'],pr=2)
        self.refusal('placement-full',self.admit,'two','two',sandbox=two)
        self.assertFalse(self.db.query("select * from account_reservations where spend_key = 'two'"))

    def test_psql_driver_admits_and_settles_in_one_transaction(self):
        from ameesh import db as db_mod
        snapshot=self.snapshot('psql');snapshot['request_ids']=[1,2]
        db=db_mod.connect(self.cfg,driver='psql')
        try:
            got=self.admit('psql','psql',db=db,sandbox=snapshot)
            self.assertEqual(got['attempt'],1)
            self.assertTrue(self.admit('psql','psql',db=db,sandbox=snapshot)['replayed'])
            self.assertEqual(db.query('select request_ids from sandbox_turns')[0]['request_ids'],[1,2])
            with db.transaction() as tx:
                result=api.sandbox_settle(tx,tx.query('select * from sandbox_turns')[0],'cancelled')
            self.assertEqual(result['state'],'kept')
            self.assertEqual(self.reservation('psql')['state'],'kept')
        finally:
            db.close()
