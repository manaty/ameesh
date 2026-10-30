# SPDX-License-Identifier: AGPL-3.0-only
"""Real signed release fences, racing turn writers and receipt reconciliation."""
import importlib.util
import json
from pathlib import Path
import threading
import time
from unittest import mock

from ameesh import authority, db as db_mod, decisions, registry, signing, storage, work
from tests.support import PgTestCase
from tests.test_l232b_release_authority import release

SPEC = importlib.util.spec_from_file_location('release_fence',
        Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/fence-release.py')
fence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fence)


class ReleaseFenceTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('TRUNCATE host_release_operations CASCADE')
        self.db.execute('TRUNCATE host_lifecycle CASCADE')
        self.db.execute('TRUNCATE turn_resources')
        self.now = time.time()
        self.manifest = dict(host='release-host', commit='a'*40,
                             release='a'*40+'-'+'b'*64, database='mesh-test',
                             units=['ameesh@test.service'], policy='test-policy',
                             rollback_until=int(self.now)+100)
        self.operation = 'test-release'
        self.seed = b'\x42'*32  # Public synthetic fixture, never an enrolled key.
        registry.upsert(self.db, 'fixture-owner', harness='other', host='control')
        authority.register_key(self.db, 'fixture-owner', signing.public_from_seed(self.seed), role='owner')
        registry.upsert(self.db, 'fixture-controller', harness='other', host='control')
        lot = work.add(self.db, title='Synthetic host release fence', assignee='fixture-controller')
        self.view = decisions.ask(self.cfg, self.db, requester='fixture-controller', lot=str(lot['id']),
                question=release.question(self.operation, self.manifest, expires_at=int(self.now)+600),
                options=['%s=%s' % (o['key'],o['label']) for o in release.OPTIONS], needs_signature=True)
        self.auth = release.SignedDecisionAuthority(self.db, self.view['id'], self.operation,
                                                    self.manifest, clock=lambda: self.now)
        digest = decisions.answer_digest(self.view, 'apply', 'apply', 'human:fixture-owner')
        authority.create_approval(self.db, self.seed, approver='fixture-owner', action=decisions.APPROVAL_ACTION,
                artifact_kind=decisions.APPROVAL_KIND, artifact_hash=digest, ttl=900)
        decisions.answer(self.cfg, self.db, self.view['id'], 'apply', human='human:fixture-owner',
                         channel='cli', signed=True)
        registry.upsert(self.db, 'dev', harness='codex', host='release-host')
        self.probe = 'release-probe'
        registry.upsert(self.db, self.probe, harness='codex', host='release-host')
        self.lease = registry.claim(self.db, 'dev', 'runner', ttl_seconds=300)
        self.adapter = fence.SignedReleaseFence(self.db, self.auth)

    def request(self, step='fence'):
        return dict(operation=self.operation, manifest_digest=release.digest(self.manifest),
                    action_id=self.operation+':'+step, manifest=self.manifest, step=step)

    def perform(self, step='fence'):
        return self.adapter.perform(step, self.request(step), probe=self.probe)

    def test_close_preserves_running_turn_then_drains_without_next_prompt(self):
        registry.set_pending_prompt(self.db, 'dev', 'next work remains pending')
        registry.begin_turn(self.db, 'dev', 'runner', self.lease['lease_epoch'], 'current')
        before = registry.get(self.db, 'dev')
        with self.assertRaisesRegex(fence.FenceError, 'draining'):
            self.perform()
        after = registry.get(self.db, 'dev')
        for key in ('status','lease_owner','lease_epoch','session_id','host','pending_prompt'):
            self.assertEqual(after[key], before[key])
        self.assertIsNone(registry.take_pending_prompt(self.db, 'dev', 'runner', self.lease['lease_epoch']))
        self.assertFalse(self.adapter.fenced(self.request('migrate')))
        self.db.execute("UPDATE agent_registry SET status='idle' WHERE name='dev'")
        self.assertEqual(self.perform(), dict(self.request(), ok=True))
        self.assertTrue(self.adapter.fenced(self.request('migrate')))

    def test_claim_attach_and_turn_starts_refused_even_for_new_agent(self):
        self.perform()
        registry.release(self.db, 'dev', 'runner', self.lease['lease_epoch'])
        self.assertIsNone(registry.claim(self.db, 'dev', 'new-runner', ttl_seconds=300))
        self.assertEqual(registry.claimable(self.db, 'release-host'), [])
        self.assertIsNone(storage.of(self.db).leases.attach_claim('dev','human',300,require_responsible=False))
        registry.upsert(self.db, 'new', harness='codex', host='release-host')
        self.assertIsNone(registry.claim(self.db, 'new', 'new-runner', ttl_seconds=300))
        self.assertFalse(registry.begin_turn(self.db, 'dev', 'runner', self.lease['lease_epoch'], 'new'))

    def test_live_fence_admits_only_the_enrolled_probe_and_its_turn(self):
        self.perform()
        self.assertFalse(self.db.query(
            "SELECT ameesh_host_claims_open('release-host') AS open")[0]['open'])
        self.assertTrue(self.db.query(
            "SELECT ameesh_agent_claims_open('release-host',%s) AS open", (self.probe,))[0]['open'])
        # Scheduling decisions stay strict: the probe is never offered or placed.
        self.assertEqual(registry.claimable(self.db, 'release-host'), [])
        # Any other identity of the fenced host is still refused.
        registry.upsert(self.db, 'other', harness='codex', host='release-host')
        self.assertIsNone(registry.claim(self.db, 'other', 'other-runner', ttl_seconds=300))
        # The enrolled probe claims and starts its bounded health turn.
        lease = registry.claim(self.db, self.probe, 'probe-runner', ttl_seconds=300)
        self.assertIsNotNone(lease)
        self.assertTrue(registry.begin_turn(self.db, self.probe, 'probe-runner',
                                            lease['lease_epoch'], 'health'))
        registry.release(self.db, self.probe, 'probe-runner', lease['lease_epoch'])
        # Unfencing restores the strict predicate for everyone.
        self.perform('unfence')
        self.assertTrue(self.db.query(
            "SELECT ameesh_host_claims_open('release-host') AS open")[0]['open'])

    def test_fence_requires_a_well_formed_probe_and_keeps_the_first_one(self):
        with self.assertRaisesRegex(fence.FenceError, 'enrolled probe'):
            self.adapter.perform('fence', self.request(), probe=None)
        with self.assertRaisesRegex(fence.FenceError, 'enrolled probe'):
            self.adapter.perform('fence', self.request(), probe='not a probe')
        self.assertEqual(self.db.query('SELECT * FROM host_release_fences'), [])
        expected = self.perform()
        # A same-action replay never rewrites the enrolled probe.
        self.assertEqual(self.adapter.perform('fence', self.request(), probe='other-probe'), expected)
        self.assertEqual(self.db.query('SELECT probe FROM host_release_fences')[0]['probe'], self.probe)

    def test_expired_running_and_orphan_resources_are_not_drained(self):
        registry.begin_turn(self.db, 'dev', 'runner', self.lease['lease_epoch'], 'current')
        self.db.execute("UPDATE agent_registry SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE name='dev'")
        with self.assertRaisesRegex(fence.FenceError, 'draining'):
            self.perform()
        self.db.execute("UPDATE agent_registry SET status='dead' WHERE name='dev'")
        storage.of(self.db).turn_resources.open_turn('orphan','dev','release-host',pgid=1,label='synthetic')
        self.db.execute("UPDATE turn_resources SET status='orphan',ended_at=clock_timestamp()")
        with self.assertRaisesRegex(fence.FenceError, 'draining'):
            self.perform()
        self.db.execute("UPDATE turn_resources SET status='done'")
        self.assertEqual(self.perform(), dict(self.request(), ok=True))

    def test_unfence_replays_lost_reply_without_clearing_another_fence(self):
        self.perform()
        expected = self.perform('unfence')
        self.assertFalse(self.adapter.fenced(self.request('migrate')))
        self.db.execute('INSERT INTO host_release_operations(operation,host,manifest_digest) VALUES (%s,%s,%s)',
                        ('another','release-host','c'*64))
        self.db.execute('INSERT INTO host_release_fences(host,operation,probe) VALUES (%s,%s,%s)',
                        ('release-host','another','another-probe'))
        self.assertEqual(self.perform('unfence'), expected)
        self.assertEqual(self.db.query('SELECT operation FROM host_release_fences')[0]['operation'], 'another')

    def test_same_operation_cannot_reclose_the_host_after_its_unfence(self):
        self.perform()
        self.perform('unfence')
        self.assertEqual(self.db.query('SELECT * FROM host_release_fences'), [])
        # A second closure of the same operation, under another fence action,
        # must not resurrect the host closure after its terminal unfence.
        with self.assertRaisesRegex(fence.FenceError, 'reconcile'):
            self.perform('recovery_fence')
        self.assertEqual(self.db.query('SELECT * FROM host_release_fences'), [])
        # A same-action replay is refused too: it would attest a live closure.
        with self.assertRaisesRegex(fence.FenceError, 'reconcile'):
            self.perform('fence')
        self.assertEqual(self.db.query('SELECT * FROM host_release_fences'), [])
        self.assertFalse(self.adapter.fenced(self.request('migrate')))

    def test_foreign_fence_and_changed_receipt_hold_release(self):
        self.db.execute('INSERT INTO host_release_operations(operation,host,manifest_digest) VALUES (%s,%s,%s)',
                        ('another','release-host','c'*64))
        self.db.execute('INSERT INTO host_release_fences(host,operation,probe) VALUES (%s,%s,%s)',
                        ('release-host','another','another-probe'))
        with self.assertRaisesRegex(fence.FenceError, 'reconcile'):
            self.perform()
        self.db.execute('DELETE FROM host_release_fences')
        self.perform()
        self.db.execute("UPDATE host_release_fence_receipts SET receipt=jsonb_set(receipt,'{ok}','false')")
        with self.assertRaisesRegex(fence.FenceError, 'reconcile'):
            self.perform()
        self.assertEqual(self.db.query('SELECT operation FROM host_release_fences')[0]['operation'], self.operation)

    def test_unfence_requires_owned_fence_on_first_call(self):
        with self.assertRaisesRegex(fence.FenceError, 'reconcile'):
            self.perform('unfence')
        self.assertEqual(self.db.query('SELECT * FROM host_release_fence_receipts'), [])

    def test_release_unfence_preserves_withdrawal_and_handoff_holds(self):
        storage.of(self.db).host_lifecycle.drain('release-host', actor='orch')
        self.db.execute("UPDATE agent_registry SET handoff_fenced=true WHERE name='dev'")
        self.perform()
        self.perform('unfence')
        self.assertFalse(self.db.query("SELECT ameesh_host_claims_open('release-host') AS open")[0]['open'])
        self.assertTrue(registry.get(self.db, 'dev')['handoff_fenced'])
        self.assertEqual(storage.of(self.db).host_lifecycle.get('release-host')['mode'], 'draining')

    def test_pending_authority_blocks_all_effects(self):
        with mock.patch.object(decisions, 'get', return_value=self.view):
            with self.assertRaisesRegex(fence.FenceError, 'denied'):
                self.perform()
        self.assertEqual(self.db.query('SELECT * FROM host_release_operations'), [])

    def test_expiry_blocks_unfence_and_replay_but_preserves_fence(self):
        self.perform()
        self.now += 601
        for step in ('fence','unfence'):
            with self.subTest(step=step), self.assertRaisesRegex(fence.FenceError, 'denied'):
                self.perform(step)
        self.assertEqual(self.db.query('SELECT operation FROM host_release_fences')[0]['operation'], self.operation)

    def test_residual_prompt_and_accounting_markers_require_reconciliation(self):
        self.db.execute("UPDATE agent_registry SET current_prompt='unfinished',status='idle' WHERE name='dev'")
        with self.assertRaisesRegex(fence.FenceError, 'draining'):
            self.perform()
        self.db.execute("UPDATE agent_registry SET current_prompt=NULL WHERE name='dev'")
        registry.pending_spend_put(self.db, 'dev', 1, 'test-turn', 'test-model')
        with self.assertRaisesRegex(fence.FenceError, 'draining'):
            self.perform()
        registry.pending_spend_clear(self.db, 'dev')
        self.assertEqual(self.perform(), dict(self.request(), ok=True))

    def test_revocation_after_lock_wait_prevents_fence_publication(self):
        started, result = threading.Event(), []
        other = db_mod.connect(self.cfg)
        other_auth = release.SignedDecisionAuthority(other, self.view['id'], self.operation, self.manifest)
        adapter = fence.SignedReleaseFence(other, other_auth)
        original = adapter.store._lock

        def wait_lock(tx, host):
            started.set()
            original(tx, host)

        def close():
            try:
                adapter.perform('fence', self.request(), probe=self.probe)
            except fence.FenceError as exc:
                result.append(str(exc))

        thread = threading.Thread(target=close)
        try:
            with self.db.transaction() as tx:
                tx.query('SELECT name FROM agent_registry WHERE name=%s FOR UPDATE', ('dev',))
                with mock.patch.object(adapter.store, '_lock', wait_lock):
                    thread.start()
                    self.assertTrue(started.wait(10))
                    authority.revoke_key(tx, 'fixture-owner')
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(result), 1)
            self.assertEqual(self.db.query('SELECT * FROM host_release_fences'), [])
        finally:
            thread.join(10)
            other.close()

    def test_turn_waiting_on_row_reads_committed_fence(self):
        started, result = threading.Event(), []
        other = db_mod.connect(self.cfg)

        def begin():
            started.set()
            result.append(registry.begin_turn(other, 'dev', 'runner', self.lease['lease_epoch'], 'racing'))

        thread = threading.Thread(target=begin)
        try:
            with self.db.transaction() as tx:
                tx.query("SELECT name FROM agent_registry WHERE name='dev' FOR UPDATE")
                thread.start()
                self.assertTrue(started.wait(10))
                # Existing row lock is held through publication and commit.
                fence.SignedReleaseFence(tx, self.auth).perform('fence', self.request(), probe=self.probe)
            thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [False])
        finally:
            thread.join(10)
            other.close()

    def test_no_placement_withdrawal_or_auto_return_timer_is_created(self):
        self.perform()
        self.assertEqual(storage.of(self.db).host_lifecycle.listing(), [])
        self.assertEqual(storage.of(self.db).handoffs.active('dev'), None)
        self.assertTrue(self.db.query("SELECT NOT ameesh_host_claims_open('release-host') AS closed")[0]['closed'])

    def test_host_submit_guard_serializes_concurrent_unfence(self):
        self.perform()
        other = db_mod.connect(self.cfg)
        adapter = fence.SignedReleaseFence(other, release.SignedDecisionAuthority(
            other,self.view['id'],self.operation,self.manifest))
        started, result, errors = threading.Event(), [], []
        original = adapter.store._lock
        def observed_lock(tx,host):
            started.set()
            original(tx,host)
        def open_gate():
            try:
                result.append(adapter.perform('unfence',self.request('unfence')))
            except Exception as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=open_gate)
        try:
            with mock.patch.object(adapter.store,'_lock',observed_lock):
                with self.adapter.guard(self.request('restart')):
                    thread.start()
                    self.assertTrue(started.wait(10))
                    thread.join(0.1)
                    self.assertTrue(thread.is_alive())
                    self.assertEqual(self.db.query('SELECT operation FROM host_release_fences')[0]['operation'],self.operation)
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors,[])
            self.assertEqual(result,[dict(self.request('unfence'),ok=True)])
        finally:
            thread.join(10)
            other.close()

    def test_restart_helper_uses_real_signed_authority_and_database_guard(self):
        from tests.test_l232b_restart_helper import helper, System
        policy = dict(host=self.manifest['host'],units=['ameesh-runner@test.service'],
                      controller_unit='ameesh-release-controller.service',manager={'kind':'system'},restart_seconds=5)
        self.manifest = dict(self.manifest,units=policy['units'],policy=helper.policy_id(policy))
        self.operation = 'guarded-release'
        lot = work.add(self.db,title='Synthetic guarded restart',assignee='fixture-controller')
        view = decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(lot['id']),
            question=release.question(self.operation,self.manifest,expires_at=int(self.now)+600),
            options=['%s=%s' % (o['key'],o['label']) for o in release.OPTIONS],needs_signature=True)
        digest = decisions.answer_digest(view,'apply','apply','human:fixture-owner')
        authority.create_approval(self.db,self.seed,approver='fixture-owner',action=decisions.APPROVAL_ACTION,
            artifact_kind=decisions.APPROVAL_KIND,artifact_hash=digest,ttl=900)
        decisions.answer(self.cfg,self.db,view['id'],'apply',human='human:fixture-owner',channel='cli',signed=True)
        signed = release.SignedDecisionAuthority(self.db,view['id'],self.operation,self.manifest)
        gate = fence.SignedReleaseFence(self.db,signed)
        gate.perform('fence',self.request(),probe=self.probe)
        system = System()
        engine = helper.RestartHelper(Path(self.tmp)/'signed-helper',policy,authority=signed,fence=gate,system=system)
        expected = dict(self.request('restart'),ok=True)
        self.assertEqual(engine.perform('restart',self.request('restart')),expected)
        self.assertEqual(engine.perform('restart',self.request('restart')),expected)
        self.assertEqual(len(system.submissions),1)
