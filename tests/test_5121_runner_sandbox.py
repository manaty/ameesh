# SPDX-License-Identifier: AGPL-3.0-only
"""Dormant runner selection and real S1/S3 + supervisor subprocess path."""
import os
import signal
import threading
import time
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import canon, registry, runner_sandbox as runtime
from ameesh.sandbox_turn import SandboxTurn
from ameesh.storage.postgres import sandbox as s1, turn_admission as s3
from . import test_sandbox_canon as card_fixture
from . import test_4872_turn_admission as admission_fixture
from .support import PgTestCase


class SelectionTest(card_fixture.SandboxCase):
    def worker(self):
        return NS(cfg=NS(canon=True), name='tpk-reviewer', db=object(),
                  runner=NS(host='tpk-stg-reviewers'), _canon_id=lambda: None)

    def dispatch(self):
        self.commit()
        loaded, _ = self.load(trusted=True)
        worker = self.worker()
        worker.fail_turn = mock.Mock()
        state = dict(last_good_commit=loaded.sources[0].commit, checked_ts=time.time(), status='ok')
        with mock.patch.object(canon, 'load_configured', return_value=[loaded]), \
             mock.patch.object(runtime.canon_sync, 'state', return_value=state), \
             mock.patch.object(runtime.account_turn, 'for_turn', return_value=NS(billing_account='provider', reserve_usd=2)), \
             mock.patch.object(runtime, 'run', return_value=True) as run:
            result = runtime.dispatch(worker, {})
        return worker, result, run

    def test_no_block_keeps_legacy_path_without_opening_sandbox_turn(self):
        self.write('agents/tpk-reviewer.md', card_fixture.agent_card().split('sandbox:')[0] + '---\n')
        worker, result, run = self.dispatch()
        self.assertIsNone(result)
        run.assert_not_called()
        worker.fail_turn.assert_not_called()

    def test_admitted_profile_selects_sandbox(self):
        worker, result, run = self.dispatch()
        self.assertTrue(result)
        self.assertTrue(run.call_args.args[2][0].ok)
        worker.fail_turn.assert_not_called()

    def test_unreadable_block_refuses_instead_of_falling_back(self):
        self.write('agents/tpk-reviewer.md', card_fixture.agent_card().split('sandbox:')[0] + 'sandbox: broken\n---\n')
        worker, result, run = self.dispatch()
        self.assertFalse(result)
        run.assert_not_called()
        self.assertIn('profile-refused', worker.fail_turn.call_args.args[0])

    def test_revoked_profile_refuses_instead_of_falling_back(self):
        self.write('agents/tpk-reviewer.md', card_fixture.agent_card(status='revoked'))
        worker, result, run = self.dispatch()
        self.assertFalse(result)
        run.assert_not_called()

    def test_missing_host_sandbox_is_a_named_refusal(self):
        self.write('hotes/tpk-stg-reviewers.md', card_fixture.host_card().split('  sandbox:')[0] + '---\n')
        worker, result, run = self.dispatch()
        self.assertFalse(result)
        run.assert_not_called()
        self.assertIn('profile-refused', worker.fail_turn.call_args.args[0])

    def test_resynchronization_revocation_invalidates_admitted_grant(self):
        worker, _, run = self.dispatch()
        admitted = run.call_args.args[2][0]
        self.write('agents/tpk-reviewer.md', card_fixture.agent_card(status='revoked'))
        pinned = self.commit()
        loaded, _ = self.load(trusted=True)
        state = dict(status='ok', checked_ts=time.time(), last_good_commit=pinned)
        with mock.patch.object(runtime, '_pinned_canon', return_value=(loaded, state, pinned)):
            self.assertFalse(runtime.grant_valid(worker, admitted))


class RunnerSandboxTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ('tranche_relay_requests', 'sandbox_results', 'sandbox_turns', 'tranche_events',
                      'tranche_reservations', 'tranche_limits', 'account_reservations',
                      'account_budgets', 'turn_costs', 'spend_pending'):
            self.db.execute('delete from ' + table)
        from ameesh import storage
        storage.of(self.db).budgets.account_put('provider', 100, actor='human:t')
        s3.tranche_create(self.db, 'migration', 'project', 50, starts_at='2026-01-01',
                          actor='human:t', admitted_ref='decision:1')
        self.envelope = admission_fixture.TurnAdmissionTest.snapshot(self)
        self.worker = NS(db=self.db, name='agent', epoch=7, cfg=NS(host='host'),
                         runner=NS(host='host', runner_id='owner', dry_run=False, lock=threading.Lock(), turns=0),
                         agent=dict(harness='deepseek', model='model', spent_usd=0), lease_deadline=time.time()+3600,
                         state_dir=self.state, lock=threading.Lock(), proc=None, pgid=None,
                         lease_lost=threading.Event(), stopping=threading.Event(), stop_requested=threading.Event(),
                         preempt_gen=0, _preempt_lock=threading.Lock(), preempting=threading.Event(),
                         _marqueurs_durables=lambda value: value, _compta_travail=None,
                         _spend_key=lambda start, marker: 'opaque-key', fail_turn=mock.Mock(),
                         _consume_scheduled_wake=mock.Mock(), _livre_courrier=mock.Mock(),
                         _open_turn_resource=mock.Mock(), _close_turn_resource=mock.Mock(),
                         _preempt_monitor=lambda done: done.wait())
        def marker(start, kind, model, **kwargs):
            self.worker._compta_travail = registry.pending_spend_begin(self.db, 'agent', start, kind, model, **kwargs)
            return True
        self.worker._compta_marque = marker
        self.worker._annule_tour_avant_lancement = mock.Mock()
        def stop():
            if self.worker.lease_lost.is_set() and self.worker.pgid:
                os.killpg(self.worker.pgid, signal.SIGTERM)
                return True
            return False
        self.worker.kill_if_stop_requested = stop
        self.admitted = NS(**{k: v for k, v in self.envelope.items() if k in (
            'profile_ref', 'profile_digest', 'placement_ref', 'placement_digest', 'canon_ref',
            'image_digest', 'provider', 'namespace', 'limits')})
        self.admitted.image = 'registry/reviewer@sha256:' + 'a'*64
        self.admitted.image_digest = self.admitted.image.split('@')[1]
        self.admitted.profile = 'profile'
        self.admitted.tranche = 'migration'
        self.admitted.active_deadline_seconds = 120
        self.admitted.ttl_seconds_after_finished = 3600
        self.admitted.limits.update(turns=1, tool_calls=10, max_attempts=2, job_seconds=120)
        self.host = NS(title='host', policy=NS(sandbox=canon.SandboxPlacement(
            'kubernetes', '/private/unused', 'default', self.admitted.image, ['profile'], 10,
            'decision:1', canon.SandboxJob(120, 0, 3600, False))))
        self.account = NS(name='test', billing_account='provider', reserve_usd=2)
        self.spec = dict(turn_id='turn-key', kind='mail', prompt='review', rows=[dict(
            id=21, sender='requester', created_ts=time.time(), payload=dict(
                schema='ameesh-sandbox-request/1', scope=self.envelope['scope']))])

    def run_sandbox(self, lose_lease=False, grant_valid=True):
        original = SandboxTurn.start
        children = []
        def start(adapter, spec, **kwargs):
            kwargs['provider_factory'] = 'tests.sandbox_runner_provider:CompletingProvider'
            child = original(adapter, spec, **kwargs)
            children.append(child.process)
            if lose_lease:
                self.db.execute("update agent_registry set lease_owner='replacement', lease_epoch=8 where name='agent'")
                self.worker.lease_lost.set()
            return child
        def token(env, *args, **kwargs):
            env['DEEPSEEK_API_KEY'] = 'private-session-token'
            return True
        with mock.patch.object(SandboxTurn, 'start', start), \
             mock.patch.object(runtime, 'grant_valid', return_value=grant_valid), \
             mock.patch.object(runtime.relay, 'apply_turn_env', side_effect=token), \
             mock.patch.object(runtime, 'source', return_value=dict(
                 release='test', commit='a'*40, contract='ameesh-sandbox/1')):
            result = runtime.run(self.worker, self.spec, (self.admitted, self.host, self.account))
        return result, children

    def test_one_end_to_end_turn_uses_s1_receipt_and_never_generic_end_turn(self):
        with mock.patch.object(registry, 'end_turn', side_effect=AssertionError('generic close forbidden')):
            result, children = self.run_sandbox()
        self.assertTrue(result, self.worker.fail_turn.call_args)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual(self.db.query('select state from sandbox_turns')[0]['state'], 'succeeded')
        self.assertEqual(len(self.db.query('select * from sandbox_results')), 1)
        self.assertEqual(self.db.query("select turns from agent_registry where name='agent'")[0]['turns'], 1)
        self.assertFalse(self.db.query('select * from spend_pending'))
        self.worker._livre_courrier.assert_called_once_with(self.spec)
        self.assertEqual(self.worker.runner.turns, 1)

    def test_lease_loss_signals_supervisor_and_never_closes_replacement(self):
        result, children = self.run_sandbox(lose_lease=True)
        self.assertFalse(result)
        self.assertEqual(children[0].returncode, -signal.SIGTERM)
        row = self.db.query("select lease_owner, turns from agent_registry where name='agent'")[0]
        self.assertEqual((row['lease_owner'], row['turns']), ('replacement', 0))
        self.assertFalse(self.db.query('select * from sandbox_results'))
        self.worker._livre_courrier.assert_not_called()

    def test_revocation_fences_and_cleans_before_accepting_a_result(self):
        result, children = self.run_sandbox(grant_valid=False)
        self.assertFalse(result)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual(self.db.query('select state from sandbox_turns')[0]['state'], 'cancelled')
        self.assertFalse(self.db.query('select * from sandbox_results'))
        self.assertFalse(self.db.query('select * from spend_pending'))
