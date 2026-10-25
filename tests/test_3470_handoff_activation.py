# SPDX-License-Identifier: AGPL-3.0-only
"""H3: activation, health and reconciliation of the enrolled handoff.

The pinned companion is loaded as source. Host effects run on disposable
private files, a local Git repository and a simulated systemd; no SSH, root
operation, credential value or production host is touched. A database-backed
class covers the companion's durable operation, turn-ledger proof and the H4
account raccord.
"""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from ameesh import handoff_contract, handoffs, registry, resources, storage
from .support import PgTestCase
from .test_3469_handoff_effects import (
    COMMIT, Fixture, RequestMixin, FakeSystem, FakeBridge, companion, git)


class StartSystem(FakeSystem):
    """H2 fake systemd plus the exact H3 start/stop observation."""

    def __init__(self):
        super().__init__()
        self.starts = 0
        self.start_state = 'active'
        self.start_calls = []

    def start(self, scope, account, unit):
        self.starts += 1
        self.start_calls.append(unit)
        if self.start_state:
            self.fields['ActiveState'] = self.start_state

    def stop(self, scope, account, unit):
        super().stop(scope, account, unit)
        self.fields['ActiveState'] = 'inactive'


class EffectTest(unittest.TestCase):
    """HostEffects: durable activation, health and reconciliation journal."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.system = StartSystem()
        self.fx.system = self.system
        self.fx.effects.system = self.system
        self.fx.effects.enable_seconds = 0.5
        self.fx.effects.restore_seconds = 0.5

    def request(self, step, **changes):
        extra = {}
        if step == 'admit':
            extra = {'harness': 'codex', 'account': self.fx.user,
                     'tools': ['git'], 'secrets': ['codex/api']}
        if step == 'verify':
            extra = {'commit': self.fx.commit}
        if step == 'reconcile_effect':
            extra = {'pending_step': 'enable', 'pending_epoch': 3}
        extra.update(changes)
        return RequestMixin().request(self.fx, step, **extra)

    def call(self, step, **changes):
        return self.fx.effects.perform(self.request(step, **changes))

    def prepared(self):
        """prepare + transfer + session: the state before switch."""
        for step in ('prepare', 'transfer', 'session'):
            receipt = self.call(step)
            self.assertIs(receipt['ok'], True, receipt)

    def activated(self):
        self.prepared()
        admitted = self.call('admit')
        self.assertIs(admitted['ok'], True, admitted)
        enabled = self.call('enable')
        self.assertIs(enabled['ok'], True, enabled)

    # -- admit -------------------------------------------------------------

    def test_admit_rechecks_the_prepared_destination_without_a_start(self):
        self.assertIs(self.call('prepare')['ok'], True)
        receipt = self.call('admit')
        self.assertIs(receipt['ok'], True)
        self.assertEqual(receipt['release_revision'], COMMIT)
        self.assertEqual(receipt['executor_enabled'], False)
        self.assertIs(receipt['caps_ready'], True)
        self.assertEqual(self.system.starts, 0)

    def test_admit_refuses_an_active_target_or_a_changed_release(self):
        self.assertIs(self.call('prepare')['ok'], True)
        self.system.fields['ActiveState'] = 'active'
        refused = self.call('admit')
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'target_active')
        self.system.fields['ActiveState'] = 'inactive'
        # A fresh operation has no prepare receipt: admit is ordered after it.
        self.assertEqual(self.call('admit', operation='b' * 32)['reason'], 'order')

    # -- enable ------------------------------------------------------------

    def test_enable_starts_the_exact_unit_and_replays_idempotently(self):
        self.activated()
        self.assertEqual(self.system.starts, 1)
        self.assertEqual(self.system.start_calls, ['ameesh-runner@dev.service'])
        replay = self.call('enable', controller_epoch=4)
        self.assertIs(replay['ok'], True)
        self.assertEqual(self.system.starts, 1)  # re-observed, not restarted
        self.assertEqual(replay['executor_enabled'], True)

    def test_enable_requires_the_prepared_session_and_checkout(self):
        for step in ('prepare', 'transfer', 'session', 'admit'):
            self.assertIs(self.call(step)['ok'], True)
        # The exact checkout was replaced after transfer: enable refuses.
        target = self.fx.work / 'dev'
        (target / 'a.txt').write_text('dirty\n')
        refused = self.call('enable')
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'checkout')
        self.assertEqual(self.system.starts, 0)

    def test_enable_timeout_is_uncertain_then_reconcile_quiesces_and_retry_starts(self):
        self.prepared()
        self.assertIs(self.call('admit')['ok'], True)
        self.system.start_state = None
        with self.assertRaises(companion.Uncertain):
            self.call('enable')
        # The start may have completed after the timeout: reconciliation must
        # revoke the epoch, stop the unit and prove quiescence.
        self.system.fields['ActiveState'] = 'active'
        proof = self.call('reconcile_effect', pending_step='enable',
                          pending_epoch=3, controller_epoch=4)
        self.assertIs(proof['ok'], True)
        self.assertIs(proof['quiesced'], True)
        self.assertEqual(proof['epoch'], 3)
        self.assertGreaterEqual(self.system.stops, 1)
        # A delayed call from the revoked epoch can no longer act.
        stale = self.call('enable', controller_epoch=3)
        self.assertEqual(stale['reason'], 'revoked_epoch')
        # The retry under the new epoch resumes the idempotent effect.
        self.system.start_state = 'active'
        retry = self.call('enable', controller_epoch=5)
        self.assertIs(retry['ok'], True)
        self.assertEqual(self.system.starts, 2)

    # -- verify ------------------------------------------------------------

    def test_verify_observes_the_exact_checkout_live_unit_and_caps(self):
        self.activated()
        receipt = self.call('verify')
        self.assertIs(receipt['ok'], True)
        self.assertEqual(receipt['commit'], self.fx.commit)
        self.assertIs(receipt['executor_enabled'], True)
        self.assertIs(receipt['caps_ready'], True)
        self.system.fields['ActiveState'] = 'inactive'
        refused = self.call('verify', controller_epoch=5)
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'target_inactive')

    def test_verify_accepts_the_agent_work_after_the_turn(self):
        self.activated()
        (self.fx.work / 'dev' / 'a.txt').write_text('agent work\n')
        receipt = self.call('verify')
        self.assertIs(receipt['ok'], True)

    def test_verify_refuses_a_reset_away_from_the_exact_commit(self):
        self.activated()
        target = self.fx.work / 'dev'
        git('checkout', '-q', '--orphan', 'other', cwd=target,
            home=self.fx.root)
        git('add', '-A', cwd=target, home=self.fx.root)
        git('commit', '-q', '-m', 'unrelated', cwd=target, home=self.fx.root)
        refused = self.call('verify')
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'checkout')

    # -- restore_source ----------------------------------------------------

    def test_restore_source_starts_the_enrolled_unit_and_is_idempotent(self):
        self.system.fields['ActiveState'] = 'inactive'
        receipt = self.call('restore_source')
        self.assertIs(receipt['ok'], True)
        self.assertIs(receipt['active'], True)
        self.assertEqual(self.system.starts, 1)
        replay = self.call('restore_source', controller_epoch=4)
        self.assertIs(replay['ok'], True)
        self.assertEqual(self.system.starts, 1)

    def test_restore_source_refuses_a_failed_unit(self):
        self.system.fields['ActiveState'] = 'failed'
        self.system.start_state = None
        refused = self.call('restore_source')
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'restore')

    def test_restore_source_is_allowed_after_a_refused_pre_switch_effect(self):
        refused = self.call('prepare', tools=['missing'], secrets=[])
        self.assertEqual(refused['reason'], 'tooling')
        receipt = self.call('restore_source')
        self.assertIs(receipt['ok'], True)

    def test_restore_source_is_refused_after_admission(self):
        self.prepared()
        self.assertIs(self.call('admit')['ok'], True)
        refused = self.call('restore_source', controller_epoch=4)
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'order')

    # -- reconcile_effect --------------------------------------------------

    def test_reconcile_refuses_a_foreign_pending_intent(self):
        self.assertIs(self.call('prepare')['ok'], True)
        # The journal carries the prepare receipt for controller epoch 3.
        refused = self.call('reconcile_effect', pending_step='prepare',
                            pending_epoch=2, controller_epoch=4)
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'intent_changed')

    def test_reconcile_without_a_host_intent_is_quiesced(self):
        proof = self.call('reconcile_effect', pending_step='enable',
                          pending_epoch=3, controller_epoch=4,
                          operation='b' * 32)
        self.assertIs(proof['ok'], True)
        self.assertIs(proof['quiesced'], True)
        self.assertEqual(proof['epoch'], 3)
        state = json.loads((self.fx.effects.operations / ('b' * 32)
                            / 'state.json').read_text())
        self.assertEqual(state['epoch'], 4)
        # The durable proof is replayable under a newer controller epoch.
        again = self.call('reconcile_effect', pending_step='enable',
                          pending_epoch=3, controller_epoch=5,
                          operation='b' * 32)
        self.assertIs(again['ok'], True)
        self.assertIs(again['quiesced'], True)
        self.assertEqual(again['epoch'], 3)

    def test_reconcile_keeps_the_pending_step_retryable(self):
        self.prepared()
        self.assertIs(self.call('admit')['ok'], True)
        self.system.start_state = None
        with self.assertRaises(companion.Uncertain):
            self.call('enable')
        self.system.fields['ActiveState'] = 'inactive'
        proof = self.call('reconcile_effect', pending_step='enable',
                          pending_epoch=3, controller_epoch=4)
        self.assertIs(proof['quiesced'], True)
        self.system.start_state = 'active'
        retry = self.call('enable', controller_epoch=5)
        self.assertIs(retry['ok'], True)
        # The retry revalidates the held state after reconciliation.
        held = self.call('revalidate', state='held', controller_epoch=5)
        self.assertIs(held['ok'], True)

    def test_reconcile_accepts_a_pending_step_that_never_reached_the_host(self):
        # The controller recorded its pending intent, then crashed before the
        # host saw the call. The pending step is ahead of this host's journal:
        # reconciliation quiesces nothing and must not dead-end the retry.
        for step in ('prepare', 'transfer', 'session', 'admit'):
            self.assertIs(self.call(step)['ok'], True)
        proof = self.call('reconcile_effect', pending_step='verify',
                          pending_epoch=3, controller_epoch=4)
        self.assertIs(proof['ok'], True, proof)
        self.assertIs(proof['quiesced'], True)
        self.assertEqual(proof['epoch'], 3)

    def test_two_successive_uncertainties_each_get_their_own_reconciliation(self):
        # B1: one operation can be interrupted again *after* a reconciliation.
        # The second (step, epoch) pair is a new uncertainty, not a replay of
        # the first: refusing it as ``intent_changed`` would leave the
        # operation held with no way forward.
        self.prepared()
        self.assertIs(self.call('admit')['ok'], True)
        self.system.start_state = None
        with self.assertRaises(companion.Uncertain):
            self.call('enable')                      # pending (enable, 3)
        self.system.fields['ActiveState'] = 'active'
        first = self.call('reconcile_effect', pending_step='enable',
                          pending_epoch=3, controller_epoch=4)
        self.assertIs(first['ok'], True, first)
        self.assertEqual(first['epoch'], 3)
        # The retry expires again under a new controller epoch.
        with self.assertRaises(companion.Uncertain):
            self.call('enable', controller_epoch=5)  # pending (enable, 5)
        self.system.fields['ActiveState'] = 'active'
        second = self.call('reconcile_effect', pending_step='enable',
                           pending_epoch=5, controller_epoch=6)
        self.assertIs(second['ok'], True, second)
        self.assertIs(second['quiesced'], True)
        self.assertEqual(second['epoch'], 5)
        self.assertGreaterEqual(self.system.stops, 2)
        directory = self.fx.effects.operations / ('a' * 32)
        self.assertEqual(
            sorted(path.name for path in directory.glob('reconcile-*.json')),
            ['reconcile-enable-3.json', 'reconcile-enable-5.json'])
        self.assertFalse((directory / 'reconcile.json').exists())
        # Each proof is still replayable and bound to its own epoch.
        replay = self.call('reconcile_effect', pending_step='enable',
                           pending_epoch=3, controller_epoch=7)
        self.assertIs(replay['ok'], True, replay)
        self.assertEqual(replay['epoch'], 3)


class WireContractTest(unittest.TestCase):
    """The H3 wire shape: bounded fields, exact receipts, no command/path."""

    def base(self, step, **fields):
        value = {'step': step, 'host': 'target', 'operation': 'a' * 32,
                 'action_id': 'a' * 32 + ':' + step, 'controller_epoch': 3,
                 'agent': 'dev', 'owner': 'runner', 'lease_epoch': 7}
        value.update(fields)
        return value

    def test_h3_requests_are_narrow_and_normalised(self):
        admit = handoff_contract.validate_effect_request(self.base(
            'admit', harness='deepseek', account='main',
            tools=['gh', 'git'], secrets=['z/api', 'a/api']))
        self.assertEqual(admit['tools'], ('gh', 'git'))
        self.assertEqual(admit['secrets'], ('a/api', 'z/api'))
        for step, fields in (('enable', {}),
                             ('verify', {'commit': COMMIT}),
                             ('restore_source', {}),
                             ('reconcile_effect', {'pending_step': 'enable',
                                                   'pending_epoch': 3})):
            with self.subTest(step=step):
                self.assertEqual(
                    handoff_contract.validate_effect_request(
                        self.base(step, **fields))['step'], step)
        for changes in ({'step': 'verify'},
                        {'step': 'reconcile_effect', 'pending_step': 'revalidate',
                         'pending_epoch': 3},
                        {'step': 'reconcile_effect', 'pending_step': 'enable',
                         'pending_epoch': 0},
                        {'step': 'enable', 'unit': 'other.service'}):
            broken = self.base('unknown')
            broken.update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(handoff_contract.ContractError):
                    handoff_contract.validate_effect_request(broken)

    def test_verify_request_requires_an_exact_commit(self):
        for commit in ('', 'a' * 12, 'A' * 40, '../a'):
            with self.subTest(commit=commit), self.assertRaises(
                    handoff_contract.ContractError):
                handoff_contract.validate_effect_request(
                    self.base('verify', commit=commit))

    def test_h3_receipts_are_exact_and_refuse_a_simulated_success(self):
        receipts = {
            'admit': {'release_revision': COMMIT, 'credentials_ready': True,
                      'caps_ready': True, 'tooling_ready': True,
                      'dsh_profile_ready': True, 'executor_enabled': False},
            'enable': {'unit': 'ameesh-runner@dev.service', 'active': True,
                       'executor_enabled': True},
            'verify': {'unit': 'ameesh-runner@dev.service', 'commit': COMMIT,
                       'executor_enabled': True, 'caps_ready': True},
            'restore_source': {'unit': 'ameesh-runner@dev.service',
                               'active': True, 'executor_enabled': True},
            'reconcile_effect': {'quiesced': True, 'epoch': 3},
        }
        requests = {
            'admit': self.base('admit', harness='deepseek', account='',
                               tools=['git'], secrets=[]),
            'enable': self.base('enable'),
            'verify': self.base('verify', commit=COMMIT),
            'restore_source': self.base('restore_source'),
            'reconcile_effect': self.base('reconcile_effect',
                                          pending_step='enable',
                                          pending_epoch=3),
        }
        for step, receipt in receipts.items():
            with self.subTest(step=step):
                value = dict(receipt, host='target',
                             action_id='a' * 32 + ':' + step,
                             controller_epoch=3, ok=True)
                self.assertTrue(
                    handoff_contract.validate_effect_receipt(
                        value, requests[step])['ok'])
        bad = [
            ('admit', {'executor_enabled': True}),
            ('enable', {'active': False}),
            ('enable', {'unit': 'arbitrary.service'}),
            ('verify', {'caps_ready': False}),
            ('verify', {'commit': 'b' * 40}),
            ('restore_source', {'active': False}),
            ('reconcile_effect', {'quiesced': False}),
            ('reconcile_effect', {'epoch': 0}),
        ]
        for step, changes in bad:
            with self.subTest(step=step, changes=changes), self.assertRaises(
                    handoff_contract.ContractError):
                value = dict({'host': 'target',
                              'action_id': 'a' * 32 + ':' + step,
                              'controller_epoch': 3, 'ok': True}, **receipts[step])
                value.update(changes)
                handoff_contract.validate_effect_receipt(value, requests[step])
        for step in receipts:
            with self.subTest(step=step), self.assertRaises(
                    handoff_contract.ContractError):
                value = dict({'host': 'target',
                              'action_id': 'a' * 32 + ':' + step,
                              'controller_epoch': 3, 'ok': True}, **receipts[step])
                value['diagnostic'] = 'private'
                handoff_contract.validate_effect_receipt(value, requests[step])


class H3FakeBridge(FakeBridge):
    """H2 bridge double plus the H3 typed receipts."""

    def perform(self, host, request):
        step = request['step']
        if step in self.fail:
            self.requests.append(request)
            return {'host': host, 'action_id': request['action_id'],
                    'controller_epoch': request['controller_epoch'],
                    'ok': False, 'reason': 'refused'}
        if step == 'admit':
            self.requests.append(request)
            base = {'host': host, 'action_id': request['action_id'],
                    'controller_epoch': request['controller_epoch']}
            return dict(base, ok=True, release_revision=COMMIT,
                        credentials_ready=True, caps_ready=True,
                        tooling_ready=True, dsh_profile_ready=True,
                        executor_enabled=False)
        if step == 'enable':
            self.requests.append(request)
            return {'host': host, 'action_id': request['action_id'],
                    'controller_epoch': request['controller_epoch'], 'ok': True,
                    'unit': 'ameesh-runner@dev.service', 'active': True,
                    'executor_enabled': True}
        if step == 'verify':
            self.requests.append(request)
            return {'host': host, 'action_id': request['action_id'],
                    'controller_epoch': request['controller_epoch'], 'ok': True,
                    'unit': 'ameesh-runner@dev.service',
                    'commit': request['commit'], 'executor_enabled': True,
                    'caps_ready': True}
        if step == 'restore_source':
            self.requests.append(request)
            return {'host': host, 'action_id': request['action_id'],
                    'controller_epoch': request['controller_epoch'], 'ok': True,
                    'unit': 'ameesh-runner@dev.service', 'active': True,
                    'executor_enabled': True}
        if step == 'reconcile_effect':
            self.requests.append(request)
            return {'host': host, 'action_id': request['action_id'],
                    'controller_epoch': request['controller_epoch'], 'ok': True,
                    'quiesced': True, 'epoch': request['pending_epoch']}
        return super().perform(host, request)


class AdapterTest(PgTestCase):
    """The companion against a durable operation and the turn ledger."""

    def setUp(self):
        super().setUp()
        self.fake = H3FakeBridge()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.host = 'controller'
        self.adapter = companion.HostAdapter(
            db=self.db,
            enrollment={'version': 1, 'host': self.host},
            hosts={'source': {'deepseek': {'accounts': ('main',),
                                           'tools': ('gh',), 'secrets': ()}},
                   'target': {'deepseek': {'accounts': ('main',),
                                           'tools': ('gh',), 'secrets': ()}}},
            references={}, transports={},
            # H2 final freeze: the plan's repository is bound to the enrolled
            # project before any effect, never widened by the caller.
            repositories={'ameesh': 'git@forge:manaty/app.git'},
            bridge=self.fake)
        registry.upsert(self.db, 'dev', chantier='ameesh', harness='deepseek',
                        host='source', cwd='/work/source')
        self.db.execute("UPDATE agent_registry SET session_account='main',"
                        " admitted_hosts=ARRAY['source','target'] WHERE name='dev'")
        registry.claim(self.db, 'dev', 'runner', ttl_seconds=300)
        self.api = storage.of(self.db)
        for host in ('source', 'target'):
            self.api.canon.record_state(
                host, 'ok', root='/canon', source='local', commit=COMMIT,
                good=True, diagnostic='')
            for _ in range(3):
                self.reading(host, memory=3 * 1024 ** 3 if host == 'source'
                             else 24 * 1024 ** 3)
        self.limits = resources.thresholds(
            {'resources': {'min_mem_available': '4GiB', 'min_disk_free': '1GiB'}})
        self.limits['max_agents'] = 4
        for harness, account in (('deepseek', 'main'), ('gh', '')):
            self.api.credentials.report('target', harness, account, 'healthy',
                                        source='manual', expires_at=time.time() + 3600)
        self.row = self.reserve()
        # H4 raccord: the destination account is frozen in the plan, never
        # re-read from the registry after the switch (which clears it).
        self.db.execute(
            "UPDATE host_handoffs SET plan = plan || %s::jsonb WHERE id=%s",
            (json.dumps({'account': 'main', 'session_account': 'main'}),
             self.row['id']))
        self.row = self.api.handoffs.get(self.row['id'])

    def reading(self, host, memory, **fields):
        return self.api.hosts.record({
            'host': host, 'mem_available_bytes': memory, 'swap_used_bytes': 0,
            'cpu_count': 8, 'load1': 0.0, 'disk_free_bytes': 100 * 1024 ** 3,
            'turns_in_progress': 0, **fields})

    def reserve(self):
        candidate = handoffs.plan(self.db, registry.get(self.db, 'dev'),
                                  limits_for=lambda host: self.limits)
        self.assertIsNotNone(candidate)
        return handoffs.request(self.db, candidate,
                                repository='git@forge:manaty/app.git')

    def set_state(self, state, **evidence):
        self.db.execute(
            "UPDATE host_handoffs SET state=%s, evidence=evidence || %s::jsonb"
            " WHERE id=%s",
            (state, json.dumps(evidence), self.row['id']))
        return self.api.handoffs.get(self.row['id'])

    def intent(self, step):
        row = self.api.handoffs.get(self.row['id'])
        with self.api.handoffs.controller(row['id']) as tx:
            self.assertIsNotNone(tx)
            value = storage.of(tx).handoffs.begin_effect(row['id'], row['state'], step)
        self.assertIsNotNone(value)
        value = dict(value)
        value['controller_epoch'] = value['evidence']['controller_epoch']
        return value

    def finish(self, operation):
        try:
            self.api.handoffs.finish_effect(operation['id'],
                                            operation['controller_epoch'])
        except Exception:
            pass

    # -- admit / enable ----------------------------------------------------

    def test_admit_uses_the_frozen_plan_account_and_no_registry_reread(self):
        row = self.set_state('switched', release_revision=COMMIT, branch='main',
                         commit=COMMIT, cwd='/w/dev')
        row['plan']['account'] = 'main'
        self.fake.requests.clear()
        operation = self.intent('admit')
        try:
            self.db.execute("UPDATE agent_registry SET session_account='other'"
                            " WHERE name='dev'")
            result = self.adapter.admit(operation)
        finally:
            self.finish(operation)
        self.assertIs(result['ok'], True, result)
        self.assertEqual(result['release_revision'], COMMIT)
        self.assertEqual(self.fake.requests[-1]['step'], 'admit')
        self.assertEqual(self.fake.requests[-1]['account'], 'main')
        self.assertEqual(self.fake.requests[-1]['tools'], ('gh',))

    def test_enable_returns_true_and_a_refusal_stays_false(self):
        self.set_state('activated', release_revision=COMMIT, branch='main',
                   commit=COMMIT, cwd='/w/dev')
        operation = self.intent('enable')
        try:
            self.assertIs(self.adapter.enable(operation), True)
        finally:
            self.finish(operation)
        self.assertEqual(self.fake.requests[-1]['step'], 'enable')
        self.set_state('activated', release_revision=COMMIT, branch='main',
                   commit=COMMIT, cwd='/w/dev')
        operation = self.intent('enable')
        self.fake.fail.add('enable')
        try:
            refused = self.adapter.enable(operation)
        finally:
            self.finish(operation)
        self.assertIs(refused['ok'], False)
        self.assertEqual(refused['reason'], 'refused')

    # -- verify ------------------------------------------------------------

    def turn(self, *, tokens=10, host='target', before_switch=False):
        self.db.execute(
            "INSERT INTO host_handoff_events (operation, state, detail)"
            " VALUES (%s, 'switched', 'test')", (self.row['id'],))
        turn = 'turn-%d-%d' % (int(time.time() * 1000), len(self.fake.requests))
        self.api.turn_resources.open_turn(turn, 'dev', host, pgid=None,
                                          label='first')
        if before_switch:
            self.db.execute(
                "UPDATE turn_resources SET started_at=now()-interval '10 minutes'"
                " WHERE turn_id=%s", (turn,))
        self.api.turn_resources.close_turn(turn, orphan=False)
        self.api.turn_costs.insert(
            agent='dev', harness='deepseek', turn=None, model=None,
            session=None, usd=0, input_tokens=tokens, cached_input_tokens=0,
            output_tokens=0, cum_usd=0, cum_input_tokens=tokens,
            cum_cached_input_tokens=0, cum_output_tokens=0, turn_id=turn)

    def test_verify_is_pending_until_the_first_authenticated_turn(self):
        self.set_state('activated', release_revision=COMMIT, branch='main',
                       commit=COMMIT, cwd='/w/dev')
        # Neither a source turn nor a target turn from before the switch is
        # proof of the destination's first authenticated turn.
        self.turn(tokens=10, host='source')
        self.turn(tokens=10, before_switch=True)
        operation = self.intent('verify')
        try:
            pending = self.adapter.verify(operation)
        finally:
            self.finish(operation)
        self.assertEqual(pending, {'ok': True, 'pending': True})
        self.turn(tokens=10)
        operation = self.intent('verify')
        try:
            proof = self.adapter.verify(operation)
        finally:
            self.finish(operation)
        self.assertIs(proof['ok'], True)
        self.assertEqual(proof['commit'], COMMIT)
        self.assertIs(proof['turn_completed'], True)
        self.assertIs(proof['authenticated'], True)
        self.assertIs(proof['caps_ready'], True)

    def test_verify_marks_a_zero_usage_turn_unauthenticated(self):
        self.set_state('activated', release_revision=COMMIT, branch='main',
                   commit=COMMIT, cwd='/w/dev')
        self.turn(tokens=0)
        operation = self.intent('verify')
        try:
            proof = self.adapter.verify(operation)
        finally:
            self.finish(operation)
        self.assertIs(proof['ok'], True)
        self.assertIs(proof['authenticated'], False)

    def switch(self, *, seconds=0):
        # PgTestCase truncates turn_costs (through its work_items FK) but not
        # turn_resources: drop the earlier tests' rows on this agent/host so
        # the proof really starts at this operation's switch event.
        self.db.execute("DELETE FROM turn_resources WHERE agent='dev'"
                        " AND host='target'")
        self.db.execute(
            "INSERT INTO host_handoff_events (operation, state, detail, created_at)"
            " VALUES (%s, 'switched', 'test', now() - make_interval(secs=>%s))",
            (self.row['id'], seconds))

    def terminal_turn(self, *, tokens=None, orphan=False, settled_seconds=None):
        """A terminal target turn; its ledger line only when ``tokens`` is set."""
        turn = 'turn-%d-%d' % (int(time.time() * 1000), len(self.fake.requests))
        self.api.turn_resources.open_turn(turn, 'dev', 'target', pgid=None,
                                          label='first')
        self.api.turn_resources.close_turn(turn, orphan=orphan)
        if tokens is not None:
            self.api.turn_costs.insert(
                agent='dev', harness='deepseek', turn=None, model=None,
                session=None, usd=0, input_tokens=tokens,
                cached_input_tokens=0, output_tokens=0, cum_usd=0,
                cum_input_tokens=tokens, cum_cached_input_tokens=0,
                cum_output_tokens=0, turn_id=turn)
        if settled_seconds is not None:
            self.db.execute(
                "UPDATE turn_resources SET"
                " started_at=now()-make_interval(secs=>%s),"
                " ended_at=now()-make_interval(secs=>%s) WHERE turn_id=%s",
                (settled_seconds + 10, settled_seconds, turn))
        return turn

    def test_verify_holds_a_settled_turn_without_a_cost_line(self):
        # B2: a terminal turn with no ledger line is the missing-login or
        # died-before-usage case. It must hold (authenticated false), never
        # leave verify pending forever.
        self.set_state('activated', release_revision=COMMIT, branch='main',
                       commit=COMMIT, cwd='/w/dev')
        self.switch(seconds=300)
        self.terminal_turn(tokens=None, settled_seconds=60)
        operation = self.intent('verify')
        try:
            proof = self.adapter.verify(operation)
        finally:
            self.finish(operation)
        self.assertIs(proof['ok'], True, proof)
        self.assertIs(proof.get('turn_completed'), True, proof)
        self.assertIs(proof.get('authenticated'), False, proof)

    def test_verify_waits_for_a_just_closed_turn_ledger_line(self):
        # The runner closes the resource before writing its ledger line: a
        # just-ended terminal turn stays pending, never a spurious hold.
        self.set_state('activated', release_revision=COMMIT, branch='main',
                       commit=COMMIT, cwd='/w/dev')
        self.switch()
        self.terminal_turn(tokens=None)
        operation = self.intent('verify')
        try:
            proof = self.adapter.verify(operation)
        finally:
            self.finish(operation)
        self.assertEqual(proof, {'ok': True, 'pending': True})

    def running_turn(self):
        turn = 'turn-%d-%d' % (int(time.time() * 1000), len(self.fake.requests))
        self.api.turn_resources.open_turn(turn, 'dev', 'target', pgid=None,
                                          label='first')
        self.addCleanup(self.db.execute,
                        "DELETE FROM turn_resources WHERE turn_id=%s", (turn,))
        return turn

    def test_verify_waits_while_the_latest_turn_still_runs(self):
        # A repaired login starts a new turn after an earlier failed one: the
        # in-flight attempt is the evidence, so verify stays pending instead
        # of holding on the older terminal turn.
        self.set_state('activated', release_revision=COMMIT, branch='main',
                       commit=COMMIT, cwd='/w/dev')
        self.switch(seconds=300)
        self.terminal_turn(tokens=None, settled_seconds=120)
        self.running_turn()
        operation = self.intent('verify')
        try:
            proof = self.adapter.verify(operation)
        finally:
            self.finish(operation)
        self.assertEqual(proof, {'ok': True, 'pending': True})

    def test_verify_holds_a_settled_orphan_turn_without_a_cost_line(self):
        self.set_state('activated', release_revision=COMMIT, branch='main',
                       commit=COMMIT, cwd='/w/dev')
        self.switch(seconds=300)
        self.terminal_turn(tokens=None, orphan=True, settled_seconds=60)
        operation = self.intent('verify')
        try:
            proof = self.adapter.verify(operation)
        finally:
            self.finish(operation)
        self.assertIs(proof['ok'], True, proof)
        self.assertIs(proof.get('authenticated'), False, proof)

    def test_verify_accepts_a_later_authenticated_turn_after_a_failure(self):
        # Once the login is repaired and the destination runs again, the new
        # authenticated turn is the proof; an older failed one does not pin
        # the operation to a hold.
        self.set_state('activated', release_revision=COMMIT, branch='main',
                       commit=COMMIT, cwd='/w/dev')
        self.switch(seconds=300)
        self.terminal_turn(tokens=None, settled_seconds=120)
        self.terminal_turn(tokens=10)
        operation = self.intent('verify')
        try:
            proof = self.adapter.verify(operation)
        finally:
            self.finish(operation)
        self.assertIs(proof['ok'], True, proof)
        self.assertIs(proof['turn_completed'], True)
        self.assertIs(proof['authenticated'], True)

    # -- restore / reconcile ----------------------------------------------

    def test_restore_source_returns_true(self):
        self.set_state('drained', release_revision=COMMIT, branch='main',
                   commit=COMMIT, cwd='/w/dev')
        operation = self.intent('restore_source')
        try:
            self.assertIs(self.adapter.restore_source(operation), True)
        finally:
            self.finish(operation)
        self.assertEqual(self.fake.requests[-1]['step'], 'restore_source')

    def test_reconcile_effect_returns_the_pending_epoch_proof(self):
        row = self.set_state('held', release_revision=COMMIT, branch='main',
                         commit=COMMIT, cwd='/w/dev')
        operation = self.intent('enable')
        try:
            pending = operation['evidence']['pending_effect']['epoch']
            operation['evidence']['pending_effect'] = {
                'name': 'enable', 'epoch': pending}
            proof = self.adapter.reconcile_effect(operation)
        finally:
            self.finish(operation)
        self.assertIs(proof['ok'], True)
        self.assertIs(proof['quiesced'], True)
        self.assertEqual(proof['epoch'], pending)
        self.assertEqual(self.fake.requests[-1]['pending_step'], 'enable')

    def test_account_divergence_refuses(self):
        operation = {'plan': {'account': 'main', 'session_account': 'other'}}
        with self.assertRaises(companion.Refused):
            companion._account(operation['plan'])


if __name__ == '__main__':
    unittest.main()
