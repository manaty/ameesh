# SPDX-License-Identifier: AGPL-3.0-only
"""#7441: dwell, durable multi-host waves and turn-boundary application."""
from __future__ import annotations

import os
import types
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from ameesh import account_rotation, account_turn, accounts, cost, storage, db as db_mod
from .test_l30_accounts import _Base, _dossier

NOW = 1_800_000_000.0


class RotationTest(_Base):
    def setUp(self):
        super().setUp()
        self.items = accounts.parse({'claude': [
            {'name': name, 'type': 'config_dir',
             'path': _dossier(os.path.join(self.tmp, name), creds='.credentials.json')}
            for name in ('a', 'b', 'c')]})['claude']
        self.store = storage.of(self.db).accounts
        self.book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)
        self.used = {'a': .1, 'b': .6, 'c': .2}
        self.hard = set()
        self.problems = set()
        self.patch = mock.patch.object(accounts, 'evaluate', side_effect=self.evaluate)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        claim = mock.patch('ameesh.account_claims.mark_refused', side_effect=lambda cfg, items, **kw: items)
        claim.start()
        self.addCleanup(claim.stop)

    def evaluate(self, book, profile, items, now, *args, **kwargs):
        used = self.used[profile.name]
        return accounts.Evaluation(
            profile, reason='pace threshold' if used >= .5 else '',
            gauges=[cost.Gauge('claude', 'seven_day', used, now + 86400, 604800,
                               read_at=now, produced_at=now)],
            hard=profile.name in self.hard,
            problems=['invalid profile'] if profile.name in self.problems else [],
            limits={'seven_day': (.5, 'pace cap')})

    def switched(self, name='pilot', age=1800):
        self.store.record_agent_switch('host-a', 'claude', from_account='a',
                                       to_account='b', reason='previous switch', agent=name)
        row = self.store.last_agent_switch('claude', name)
        self.db.execute('UPDATE account_switches SET at = to_timestamp(%s) WHERE id = %s',
                        (NOW - age, row['id']))
        return row['id']

    def choose(self, **kwargs):
        return accounts.choose(self.db, self.cfg.host, 'claude', self.items, self.book,
                               now=NOW, agent='pilot', critical=True,
                               session_account='b', **kwargs)

    def journal(self, kind):
        return self.db.query('SELECT * FROM account_switches WHERE kind = %s ORDER BY id', (kind,))

    def test_pace_crossed_after_30_minutes_is_held_and_journaled_once(self):
        self.switched()
        for _ in range(2):
            choice = self.choose()
            self.assertEqual(choice.profile.name, 'b')
            self.assertTrue(choice.retained)
            self.assertIsNone(choice.switched)
        self.assertEqual(len(self.journal('hysteresis')), 1)

    def test_clock_expires_two_hours_after_effective_switch(self):
        self.switched(age=7200)
        self.assertNotEqual(self.choose().profile.name, 'b')
        self.assertFalse(self.journal('hysteresis'))

    def test_real_saturation_after_30_minutes_bypasses_dwell(self):
        self.switched()
        self.used['b'] = 1.0
        self.assertNotEqual(self.choose().profile.name, 'b')
        self.assertFalse(self.journal('hysteresis'))

    def test_provider_refusal_after_30_minutes_bypasses_dwell(self):
        self.switched()
        with mock.patch('ameesh.auth_failure.failing', return_value={'b': {'reason': 'refused'}}):
            self.assertNotEqual(self.choose().profile.name, 'b')
        self.assertFalse(self.journal('hysteresis'))

    def test_hard_cap_and_invalid_profile_never_bypassed(self):
        self.switched()
        self.hard.add('b')
        self.assertNotEqual(self.choose().profile.name, 'b')
        self.hard.clear()
        self.problems.add('b')
        self.assertNotEqual(self.choose().profile.name, 'b')

    def test_simulation_writes_no_withheld_switch(self):
        self.switched()
        self.assertTrue(self.choose(simulate=True).retained)
        self.assertFalse(self.journal('hysteresis'))

    def proposals(self, target='a', expected=None):
        return [dict(agent=name, host='host-' + str(index), **{'from': 'b', 'to': target},
                     reason='threshold', expected_plan=(expected or {}).get(name, 0))
                for index, name in enumerate(('pilot', 'auditor', 'designer'))]

    def test_three_personas_on_different_hosts_share_one_wave(self):
        self.store.plan_agent_wave('claude', self.proposals(), wave='first')
        rows = self.journal('planned')
        self.assertEqual(len(rows), 3)
        self.assertEqual({row['reason'] for row in rows}, {'wave first: threshold'})
        self.assertEqual(len({row['host'] for row in rows}), 3)
        self.assertEqual(len({row['wave_epoch'] for row in rows}), 1)
        self.assertFalse(self.journal('agent'))  # planning never applies a switch

    def test_stale_planner_cannot_publish_a_second_wave(self):
        self.store.plan_agent_wave('claude', self.proposals(), wave='first')
        self.assertEqual(self.store.plan_agent_wave('claude', self.proposals('c'), wave='stale'), [])
        self.assertEqual(len(self.journal('planned')), 3)

    def test_newest_wave_supersedes_unstarted_plan_and_applies_once(self):
        self.store.plan_agent_wave('claude', self.proposals(), wave='first')
        old = self.store.pending_agent_plan('claude', 'pilot')
        epochs = {name: self.store.pending_agent_plan('claude', name)['id']
                  for name in ('pilot', 'auditor', 'designer')}
        self.store.plan_agent_wave('claude', self.proposals('c', epochs), wave='second')
        newest = self.store.pending_agent_plan('claude', 'pilot')
        args = dict(agent='pilot', from_account='b', to_account='c', reason=newest['reason'])
        self.assertFalse(self.store.apply_agent_plan('host-0', 'claude', epoch=old['wave_epoch'], **args))
        self.assertTrue(self.store.apply_agent_plan('host-0', 'claude', epoch=newest['wave_epoch'], **args))
        self.assertFalse(self.store.apply_agent_plan('host-0', 'claude', epoch=newest['wave_epoch'], **args))
        self.assertEqual(len(self.journal('agent')), 1)

    def worker(self):
        return types.SimpleNamespace(
            cfg=self.cfg, db=self.db, name='pilot', agent={'harness': 'claude'},
            runner=types.SimpleNamespace(is_critical=lambda name, row=None: True),
            current_session=lambda: 'session', _compte_session='b',
            _fil_note=mock.Mock())

    def test_coordinator_publishes_whole_wave_without_touching_running_personas(self):
        worker = self.worker()
        peers = [dict(name=name, harness='claude', host='remote', session_account='b',
                      status='running') for name in ('auditor', 'designer')]
        with mock.patch.object(account_rotation.registry, 'overview', return_value=peers):
            choice = account_rotation.coordinate(worker, self.items, self.book,
                                                   self.choose(), now=NOW)
        self.assertEqual(len(self.journal('planned')), 3)
        self.assertTrue(choice.switched['epoch'])
        self.assertEqual(worker._compte_session, 'b')
        self.assertFalse(self.journal('agent'))
        worker._pending_claude_switch = choice.switched
        with mock.patch.object(account_turn, '_log'):
            self.assertTrue(account_turn.begin_switch(worker, choice.profile))
            account_turn.mark(worker, os.path.join(self.tmp, 'events'), choice.profile)
        self.assertEqual(worker._compte_session, choice.profile.name)
        self.assertEqual(len(self.journal('agent')), 1)

    def test_cleared_session_publishes_and_consumes_wave(self):
        self.switched(age=7200)
        self.used.update(a=.4, b=.6, c=.1)
        worker = self.worker()
        worker.current_session = lambda: None
        choice = accounts.choose(
            self.db, self.cfg.host, 'claude', self.items, self.book,
            now=NOW, agent='pilot', critical=True, session_account=None)
        self.assertEqual(choice.switched['from'], 'b')
        self.assertEqual(choice.profile.name, 'c')
        with mock.patch.object(account_rotation.registry, 'overview', return_value=[]):
            choice = account_rotation.coordinate(worker, self.items, self.book, choice, now=NOW)
        self.assertIsNotNone(choice.profile)
        self.assertEqual(len(self.journal('planned')), 1)
        self.assertEqual(choice.switched['epoch'], self.journal('planned')[0]['wave_epoch'])
        worker._pending_claude_switch = choice.switched
        with mock.patch.object(account_turn, '_log'):
            self.assertTrue(account_turn.begin_switch(worker, choice.profile))
            account_turn.mark(worker, os.path.join(self.tmp, 'events'), choice.profile)
        self.assertEqual(self.store.last_agent_switch('claude', 'pilot')['to_account'], 'c')
        self.assertIsNone(self.store.pending_agent_plan('claude', 'pilot'))

    def test_cleared_session_keeps_individual_hysteresis(self):
        self.switched(age=1800)
        choice = accounts.choose(
            self.db, self.cfg.host, 'claude', self.items, self.book,
            now=NOW, agent='pilot', critical=True, session_account=None)
        self.assertEqual(choice.profile.name, 'b')
        self.assertIsNone(choice.switched)
        self.assertTrue(choice.retained)

    def test_planned_target_still_obeys_current_local_guards(self):
        self.hard.add('a')
        self.used['a'] = .9
        self.assertEqual(self.choose(planned_account='a').profile.name, 'c')

    def test_concurrent_hosts_publish_exactly_one_wave(self):
        barrier = threading.Barrier(2)

        def publish(wave):
            connection = db_mod.connect(self.cfg)
            try:
                barrier.wait(timeout=10)
                return storage.of(connection).accounts.plan_agent_wave(
                    'claude', self.proposals(), wave=wave)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, ('host-one', 'host-two')))
        self.assertEqual(sorted(map(len, results)), [0, 3])
        rows = self.journal('planned')
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['reason'] for row in rows}), 1)

    def test_recent_persona_is_held_independently_of_the_other_wave_members(self):
        self.switched(name='auditor')
        worker = self.worker()
        peers = [dict(name=name, harness='claude', host='remote', session_account='b')
                 for name in ('auditor', 'designer')]
        with mock.patch.object(account_rotation.registry, 'overview', return_value=peers):
            account_rotation.coordinate(worker, self.items, self.book, self.choose(), now=NOW)
        self.assertEqual({row['agent'] for row in self.journal('planned')},
                         {'pilot', 'designer'})
        self.assertEqual([row['agent'] for row in self.journal('hysteresis')], ['auditor'])

    def test_superseded_wave_never_reaches_the_turn_marker(self):
        self.store.plan_agent_wave('claude', self.proposals(), wave='first')
        old = self.store.pending_agent_plan('claude', 'pilot')
        epochs = {name: self.store.pending_agent_plan('claude', name)['id']
                  for name in ('pilot', 'auditor', 'designer')}
        self.store.plan_agent_wave('claude', self.proposals('c', epochs), wave='second')
        worker = self.worker()
        worker._pending_claude_switch = dict(
            **{'from': 'b', 'to': 'a'}, kind='agent', reason=old['reason'], epoch=old['wave_epoch'])
        path = os.path.join(self.tmp, 'superseded-events')
        with mock.patch.object(account_turn, '_log'):
            self.assertFalse(account_turn.begin_switch(worker, self.items[0]))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(worker._compte_session, 'b')
        self.assertFalse(self.journal('agent'))

    def test_provider_credit_exhaustion_bypasses_dwell(self):
        self.switched()
        with mock.patch('ameesh.provider_credits.exhausted_names', return_value={'b'}):
            self.assertNotEqual(self.choose().profile.name, 'b')
        self.assertFalse(self.journal('hysteresis'))

    def test_budget_poll_defers_effective_switch_until_turn_start(self):
        worker = self.worker()
        with mock.patch.object(accounts, 'profiles', return_value=self.items), \
                mock.patch.object(account_rotation.registry, 'overview', return_value=[]), \
                mock.patch.object(account_turn.account_window, 'reason_and_lift', return_value=('', None)), \
                mock.patch.object(account_turn.time, 'time', return_value=NOW), \
                mock.patch.object(account_turn, '_log'):
            self.assertEqual(account_turn.choose(worker, self.book), '')
            self.assertEqual(worker._compte_session, 'b')
            self.assertFalse(self.journal('agent'))
            self.assertEqual(len(self.journal('planned')), 1)
            self.assertTrue(account_turn.begin_switch(worker, worker._account))
            account_turn.mark(worker, os.path.join(self.tmp, 'start-events'), worker._account)
        self.assertEqual(len(self.journal('agent')), 1)
        self.assertEqual(worker._compte_session, worker._account.name)

    def test_rejected_planning_snapshot_defers_instead_of_switching_alone(self):
        worker = self.worker()
        with mock.patch.object(account_rotation.registry, 'overview', return_value=[]), \
                mock.patch.object(type(self.store), 'plan_agent_wave', return_value=[]):
            choice = account_rotation.coordinate(worker, self.items, self.book,
                                                   self.choose(), now=NOW)
        self.assertIsNone(choice.profile)
        self.assertIn('retry next turn', choice.reason)
        self.assertFalse(self.journal('agent'))

    def test_failed_wave_insert_rolls_back_every_persona(self):
        proposals = self.proposals()
        proposals[1]['to'] = None  # destination is NOT NULL; abort after the first insert
        with self.assertRaises(db_mod.DbError):
            self.store.plan_agent_wave('claude', proposals, wave='failed')
        self.assertFalse(self.journal('planned'))


class TurnBoundaryTest(_Base):
    def test_superseded_marker_restores_instruction_without_starting_harness(self):
        profiles = self._claude_comptes(partage=True)
        _runner, worker = self._worker('boundary', 'claude', {'claude': profiles})
        try:
            with mock.patch.object(account_turn, 'begin_switch', return_value=False):
                self.assertIsNotNone(self._tour(worker, 'keep this instruction'))
            self.assertFalse(worker._turn_launched)
            self.assertFalse(self.turns())
            row = account_rotation.registry.get(self.db, worker.name)
            self.assertIn('keep this instruction', row['pending_prompt'])
        finally:
            worker.watchdog_stop.set()
