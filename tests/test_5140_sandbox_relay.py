# SPDX-License-Identifier: AGPL-3.0-only
"""Real admission, DB clock, revocation and lease fencing for sandbox bearers."""
from unittest import mock

from ameesh import relay, runner_sandbox, sandbox_relay, storage
from ameesh.storage.postgres import sandbox as s1
from ameesh.storage.postgres import turn_admission
from .support import PgTestCase
from . import test_4872_turn_admission as admission_fixture


class SandboxRelayTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ('tranche_relay_requests', 'sandbox_results', 'sandbox_turns',
                      'tranche_events', 'tranche_reservations', 'tranche_limits',
                      'account_reservations', 'account_budgets', 'turn_costs'):
            self.db.execute('delete from ' + table)
        storage.of(self.db).budgets.account_put('provider', 100, actor='human:t')
        turn_admission.tranche_create(self.db, 'migration', 'project', 50,
                                     starts_at='2026-01-01', actor='human:t',
                                     admitted_ref='decision:1')
        snapshot = admission_fixture.TurnAdmissionTest.snapshot(self)
        if self._testMethodName == 'test_expiry_uses_database_clock':
            snapshot['limits']['turn_seconds'] = 1
        admission_fixture.TurnAdmissionTest.admit(self, sandbox=snapshot)
        self.host = self.db.query("select host from agent_registry where name = 'agent'")[0]['host']

    def issue(self, **changes):
        args = dict(agent='agent', owner='owner', epoch=7, turn_id='turn-key', host=self.host)
        args.update(changes)
        return sandbox_relay.issue(self.db, **args)

    def test_opaque_token_and_exact_deadline(self):
        token, identity = self.issue()
        self.assertEqual(sandbox_relay.Tokens(self.db).verify_token(token), identity)
        self.assertEqual(identity.turn, 'turn-key')
        expiry = self.db.query("select extract(epoch from deadline_ts)::float8 as expiry"
                               " from sandbox_turns where turn_id = 'turn-key'")[0]['expiry']
        self.assertEqual(identity.expires_ts, expiry)
        stored = self.db.query('select token_sha256 from sandbox_relay_tokens')[0]['token_sha256']
        self.assertNotIn(token, stored)
        with self.assertRaises(relay.TokenError):
            sandbox_relay.Tokens(self.db).verify_token('ams1.ordinary-lease-token')

    def test_expiry_uses_database_clock(self):
        token, _ = self.issue()
        # The deadline is immutable. Wait in the disposable DB, with the
        # runner's clock deliberately wrong to prove it cannot extend expiry.
        self.db.query('select pg_sleep(1.1)')
        with self.assertRaises(relay.TokenError):
            with mock.patch('time.time', return_value=0):
                sandbox_relay.Tokens(self.db).verify_token(token)
        with self.assertRaises(s1.Refusal):
            self.issue()

    def test_lost_lease_epoch_owner_host_and_cancellation_refuse(self):
        token, _ = self.issue()
        mutations = [
            ("update agent_registry set lease_epoch = 8 where name = 'agent'",
             "update agent_registry set lease_epoch = 7 where name = 'agent'"),
            ("update agent_registry set lease_owner = 'other' where name = 'agent'",
             "update agent_registry set lease_owner = 'owner' where name = 'agent'"),
            ("update agent_registry set lease_expires_at = now() - interval '1 second' where name = 'agent'",
             "update agent_registry set lease_expires_at = now() + interval '1 hour' where name = 'agent'"),
            ("update agent_registry set host = 'other-host' where name = 'agent'",
             "update agent_registry set host = %s where name = 'agent'"),
        ]
        for mutation, restore in mutations:
            with self.subTest(mutation=mutation):
                self.db.execute(mutation)
                with self.assertRaises(relay.TokenError):
                    sandbox_relay.Tokens(self.db).verify_token(token)
                self.db.execute(restore, (self.host,) if '%s' in restore else ())

    def test_cancellation_refuses_issued_token(self):
        token, _ = self.issue()
        self.db.execute('update sandbox_turns set cancel_requested_at = clock_timestamp()')
        with self.assertRaises(relay.TokenError):
            sandbox_relay.Tokens(self.db).verify_token(token)

    def test_terminal_turn_and_unknown_bearer_refuse(self):
        token, _ = self.issue()
        self.db.execute("update sandbox_turns set state = 'cancelled'")
        for candidate in (token, sandbox_relay.PREFIX + 'a' * 43):
            with self.assertRaises(relay.TokenError):
                sandbox_relay.Tokens(self.db).verify_token(candidate)

    def test_lease_token_cannot_replace_sandbox_token(self):
        token, identity = self.issue()
        legacy = relay.StaticTokens({'lease': identity})
        verifier = sandbox_relay.RelayTokens(self.db, legacy)
        self.assertEqual(verifier.verify_token(token), identity)
        with self.assertRaises(relay.TokenError):
            verifier.verify_token('lease')

    def test_unreadable_canon_after_admission_never_falls_back(self):
        from types import SimpleNamespace as NS
        worker = NS(db=self.db, name='agent', cfg=NS(canon=True), _canon_id=lambda: None)
        with mock.patch.object(runner_sandbox.canon, 'load_configured', return_value=[]), \
                self.assertRaises(s1.Refusal):
            runner_sandbox.select(worker)

    def test_sandbox_prefix_never_reaches_legacy_verifier(self):
        legacy = mock.Mock()
        verifier = sandbox_relay.RelayTokens(self.db, legacy)
        for token in (sandbox_relay.PREFIX + 'a' * 43, sandbox_relay.PREFIX + 'broken'):
            with self.assertRaises(relay.TokenError):
                verifier.verify_token(token)
        legacy.verify_token.assert_not_called()

    def test_real_runner_issuer_reaches_credentials_boundary(self):
        from types import SimpleNamespace as NS
        from ameesh.runner import Runner
        owner = object.__new__(Runner)
        owner.db, owner.host, owner.runner_id, owner.mediated = self.db, self.host, 'owner', False
        worker = NS(runner=owner, name='agent', epoch=7)
        row = self.db.query("select * from sandbox_turns where turn_id = 'turn-key'")[0]
        with mock.patch.object(relay, 'relay_url', return_value='https://relay.example.test'):
            material = runner_sandbox.credentials(worker, row)
        self.assertEqual(sandbox_relay.Tokens(self.db).verify_token(material.token), material.identity)
        count = self.db.query('select count(*) as n from sandbox_relay_tokens')[0]['n']
        with mock.patch.object(relay, 'relay_url', return_value=''), self.assertRaises(s1.Refusal):
            runner_sandbox.credentials(worker, row)
        self.assertEqual(self.db.query('select count(*) as n from sandbox_relay_tokens')[0]['n'], count)

    def test_loss_between_verification_and_forward_refuses_provider_intent(self):
        token, _ = self.issue()
        identity = sandbox_relay.Tokens(self.db).verify_token(token)
        self.db.execute("update agent_registry set lease_epoch = 8 where name = 'agent'")
        with self.assertRaises(s1.Refusal):
            turn_admission.relay_begin(self.db, identity)
        self.assertEqual(self.db.query('select count(*) as n from tranche_relay_requests')[0]['n'], 0)

    def test_relay_calls_count_once_and_leave_spend_key_null(self):
        import threading
        _, identity = self.issue()
        writer = object.__new__(relay.Relay)
        writer.db, writer._db_lock = self.db, threading.Lock()
        row = dict(agent='agent', harness='deepseek', turn='turn-key', model='model',
                   session=None, usd=0.3, input_tokens=1, cached_input_tokens=0,
                   output_tokens=1, executor=identity.executor, lease_owner='owner',
                   lease_epoch=7, turn_id='turn-key')
        for usd in (0.3, 0.7):
            marker = turn_admission.relay_begin(self.db, identity)
            call = dict(row, usd=usd, relay_request=marker)
            self.assertTrue(writer._write(call))
            self.assertTrue(writer._write(call))
            # Bypass the in-memory/marker shortcut: the ledger itself must
            # deduplicate a replay of the same call id.
            args = dict(agent='agent', harness='deepseek', turn='turn-key', model='model',
                        session=None, usd=usd, input_tokens=1, cached_input_tokens=0,
                        output_tokens=1, cum_usd=None, cum_input_tokens=None,
                        cum_cached_input_tokens=None, cum_output_tokens=None, source='relay',
                        lease_owner='owner', lease_epoch=7, turn_id='turn-key', relay_call_id=marker)
            self.assertFalse(storage.of(self.db).turn_costs.insert(**args))
        rows = self.db.query('select spend_key,billing_account,usd from turn_costs')
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['spend_key'] is None and r['billing_account'] == 'provider' for r in rows))
        self.assertAlmostEqual(sum(float(r['usd']) for r in rows), 1)
        with self.db.transaction() as tx:
            settled = turn_admission.sandbox_settle(tx, tx.query('select * from sandbox_turns')[0], 'succeeded')
        self.assertEqual(float(settled['usd']), 1)
