# SPDX-License-Identifier: AGPL-3.0-only
"""#7518: expiring credit, per-attempt liabilities, shared admission."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import threading
import json
import unittest
from unittest import mock

from ameesh import cycle_budget as gate, storage
from ameesh.storage.postgres.cycle_budgets import CycleBudgets
from .support import PgTestCase


class QuoteTest(unittest.TestCase):
    def test_highest_tier_uncached_and_all_output_including_reasoning(self):
        bound = gate.RequestBound(1000, 2000, {'slow': ('1', '4'), 'fast': ('3', '2')}, '0.01')
        units, evidence = bound.quote()
        self.assertEqual(units, 21000)
        self.assertEqual(evidence['output_usd_per_million'], '4')

    def test_round_liability_up_credit_down(self):
        self.assertEqual(gate.micro('0.00000001'), 1)
        self.assertEqual(gate.micro('0.00000199', credit=True), 1)
        self.assertEqual(gate.RequestBound(1, 1, {'x': ('0.01', '0.01')}).quote()[0], 1)

    def test_invalid_or_unbounded_request(self):
        for value in ('NaN', 'Infinity', '-0.01', '1e50'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate.micro(value)
        for bound in (gate.RequestBound(1, 0, {'a': (1, 1)}),
                      gate.RequestBound(1, 1, {}), gate.RequestBound(True, 1, {'a': (1, 1)}),
                      gate.RequestBound(1, 1, {'a': ('NaN', 1)}),
                      gate.RequestBound(1, 1, {'a': (0, 1)})):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                bound.quote()
        with self.assertRaises(ValueError):
            gate.instant('2026-11-16T00:00:00')


class CycleBudgetTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('delete from credit_requests')
        self.db.execute('delete from credit_cycles')
        self.api = storage.of(self.db).budgets
        self.now = datetime.now(timezone.utc)
        self.account = 'test-cycle'

    def attest(self, cap='1', **kwargs):
        values = dict(credit_usd=cap, cap_usd=cap,
                      observed_at=(self.now - timedelta(minutes=1)).isoformat(),
                      expires_at=(self.now + timedelta(days=30)).isoformat(),
                      fresh_until=(self.now + timedelta(days=1)).isoformat(),
                      source='fixture: dated statement', actor='human:test')
        values.update(kwargs)
        return gate.attest(self.db, self.account, **values)

    def reserve(self, key, usd='0.4', db=None):
        # 1M input + 1M max output at half the chosen bound each.
        rate = Decimal(usd) / 2
        return gate.reserve(db or self.db, self.account, 'test-agent', key,
                            gate.RequestBound(1000000, 1000000, {'tier': (rate, rate)}))

    def test_missing_stale_expired_and_read_error_refuse_before_dispatch(self):
        with self.assertRaises(gate.CreditRefused):
            self.reserve('missing')
        self.attest()
        for column in ('fresh_until', 'expires_at'):
            with self.subTest(column=column):
                # Keep chronological constraints valid, but render the statement stale.
                self.db.execute("update credit_cycles set observed_at = now() - interval '3 days',"
                                " fresh_until = now() - interval '1 day',"
                                " expires_at = now() - interval '1 hour'")
                with self.assertRaises(gate.CreditRefused):
                    self.reserve(column)
        with mock.patch.object(CycleBudgets, 'cycle_reserve', side_effect=RuntimeError('unreadable')) as failed:
            with self.assertRaises(gate.CreditRefused):
                self.reserve('read-error')
            failed.assert_called_once()

    def test_explicit_metadata_cap_and_no_reset_at_midnight_or_reupload(self):
        cycle = self.attest(observed_at=(self.now - timedelta(days=2)).isoformat())
        self.reserve('one', '0.8')
        self.assertEqual(self.api.cycle_status(self.account)['id'], cycle)
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 800000)
        with self.assertRaises(gate.CreditRefused):
            self.reserve('two', '0.3')
        with self.assertRaises(ValueError):
            self.attest(observed_at=(self.now - timedelta(days=2)).isoformat())
        for change in (dict(credit_usd='1', cap_usd='2'), dict(source=''),
                       dict(observed_at=(self.now + timedelta(hours=1)).isoformat()),
                       dict(fresh_until=(self.now + timedelta(days=31)).isoformat())):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.attest(**change)

    def test_concurrent_hosts_share_one_credit(self):
        self.attest()
        conns = [self.connect() for _ in range(8)]
        barrier = threading.Barrier(len(conns))
        admitted, errors = [], []

        def run(i, db):
            try:
                barrier.wait(30)
                self.reserve('host-%d' % i, '0.4', db)
                admitted.append(i)
            except gate.CreditRefused:
                pass
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i, db)) for i, db in enumerate(conns)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(40)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(admitted), 2)
            self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 800000)
        finally:
            for db in conns:
                db.close()

    def test_settlement_once_and_uncertainty_keeps_liability(self):
        self.attest()
        self.reserve('complete')
        gate.settle(self.db, self.account, 'complete', '0.1')
        gate.settle(self.db, self.account, 'complete', '0.1')
        with self.assertRaises(gate.CreditRefused):
            self.reserve('complete')
        self.reserve('uncertain')
        gate.settle(self.db, self.account, 'uncertain', None)
        with self.assertRaises(ValueError):
            gate.settle(self.db, self.account, 'uncertain', '0.01')
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 500000)
        self.reserve('crashed')  # open liabilities count even without settlement
        with self.assertRaises(gate.CreditRefused):
            self.reserve('over')
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 900000)

    def test_new_dated_cycle_carries_pending_and_late_settlement(self):
        self.attest()
        self.reserve('old', '0.8')
        self.db.execute("update credit_cycles set observed_at = now() - interval '3 days',"
                        " fresh_until = now() - interval '2 days',"
                        " expires_at = now() - interval '1 day'")
        self.attest(observed_at=(self.now - timedelta(seconds=30)).isoformat())
        with self.assertRaises(gate.CreditRefused):
            self.reserve('new', '0.3')
        # A late result after the new statement must not disappear from accounting.
        gate.settle(self.db, self.account, 'old', '0.7')
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 700000)
        self.reserve('new', '0.3')

    def test_cli_attestation_is_human_only_and_inspectable(self):
        args = ('budget', 'cycle', 'attest', '--account', self.account,
                '--credit-usd', '1', '--cap-usd', '0.9',
                '--observed-at', (self.now - timedelta(minutes=1)).isoformat(),
                '--expires-at', (self.now + timedelta(days=30)).isoformat(),
                '--fresh-until', (self.now + timedelta(days=1)).isoformat(),
                '--source', 'fixture: owner reading', '--billing-lag-seconds', '0')
        refused = self.mesh(*args, env=self.env(AGENT_MAIL_NAME='test-agent'))
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('bound session identity', refused.stderr)
        self.assertIsNone(self.api.cycle_status(self.account))
        proc = self.mesh(*args, env=self.env(AMEESH_HUMANS='chef', USER='chef'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.mesh('budget', 'cycle', 'show', '--account', self.account)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(proc.stdout)
        self.assertEqual(row['actor'], 'human:chef')
        self.assertEqual(row['cap_micro'], 900000)
        self.assertEqual(row['source'], 'fixture: owner reading')

    def test_fresh_statement_uses_net_balance_without_erasing_pending(self):
        self.attest()
        self.reserve('spent', '0.4')
        gate.settle(self.db, self.account, 'spent', '0.1')
        self.reserve('in-flight', '0.4')
        observed = self.db.query('select clock_timestamp()::text as at')[0]['at']
        self.attest(cap='0.9', observed_at=observed)
        # Already-settled 0.1 is reflected in NET 0.9; in-flight is not forgiven.
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 400000)
        self.reserve('new-statement', '0.5')
        with self.assertRaises(gate.CreditRefused):
            self.reserve('no-room', '0.01')
        with self.assertRaises(ValueError):
            self.attest(cap='0.9', observed_at=observed)

    def test_failure_to_settle_after_transport_never_releases_committed_bound(self):
        self.attest()
        send = mock.Mock(return_value='response')
        with mock.patch.object(CycleBudgets, 'cycle_settle', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                gate.execute(self.db, self.account, 'a', 'settlement-failure',
                             gate.RequestBound(1000000, 1000000, {'tier': ('0.2', '0.2')}),
                             daily_admit=lambda: True, send=send, actual_cost=lambda r: '0.1')
        send.assert_called_once()
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 400000)

    def test_breached_bound_stops_account_and_keeps_actual_liability(self):
        self.attest()
        self.reserve('bad-adapter', '0.2')
        with self.assertRaisesRegex(ValueError, 'exceeded'):
            gate.settle(self.db, self.account, 'bad-adapter', '0.3')
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 300000)
        with self.assertRaises(gate.CreditRefused):
            self.reserve('after-breach', '0.1')

    def test_transport_cannot_run_without_reservation_and_daily_guard(self):
        calls = []
        bound = gate.RequestBound(1000000, 1000000, {'tier': ('0.2', '0.2')})
        def send():
            self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 400000)
            calls.append('send')
            return 'complete'
        def execute(key, **overrides):
            opts = dict(daily_admit=lambda: True, send=send, actual_cost=lambda r: '0.1')
            opts.update(overrides)
            return gate.execute(self.db, self.account, 'a', key, bound, **opts)
        with self.assertRaises(gate.CreditRefused):
            execute('absent')
        self.attest()
        with self.assertRaises(gate.CreditRefused):
            execute('daily-refused', daily_admit=lambda: False)
        self.assertEqual(calls, [])
        self.assertEqual(execute('success'), 'complete')
        self.assertEqual(calls, ['send'])
        with self.assertRaises(gate.CreditRefused):
            execute('success')  # no second transport on the same attempt id
        with self.assertRaises(TimeoutError):
            execute('timeout', send=mock.Mock(side_effect=TimeoutError))
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 500000)
        execute('missing-usage', send=lambda: 'truncated', actual_cost=lambda r: None)
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 900000)
