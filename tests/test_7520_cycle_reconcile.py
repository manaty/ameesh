# SPDX-License-Identifier: AGPL-3.0-only
"""#7520: operator recovery never erases known debt or delayed billing."""
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
import threading
from unittest import mock

from ameesh import budget, cycle_budget as gate, identity, storage
from .support import PgTestCase


class CycleReconcileTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('delete from credit_reconciliations')
        self.db.execute('delete from credit_requests')
        self.db.execute('delete from credit_cycles')
        self.api = storage.of(self.db).budgets
        self.account = 'reconcile-test'
        self.now = datetime.fromisoformat(str(self.db.query('select clock_timestamp() as at')[0]['at']))

    def attest(self, **extra):
        values = dict(credit_usd='1', cap_usd='1',
                      observed_at=(self.now - timedelta(days=1)).isoformat(),
                      expires_at=(self.now + timedelta(days=1)).isoformat(),
                      fresh_until=(self.now + timedelta(hours=1)).isoformat(),
                      source='statement:fixture', actor='human:operator')
        values.update(extra)
        return gate.attest(self.db, self.account, **values)

    def reserve(self, attempt, amount='0.4'):
        rate = Decimal(amount) / 2
        return gate.reserve(self.db, self.account, 'worker', attempt,
                            gate.RequestBound(1000000, 1000000, {'tier': (rate, rate)}))

    def reconcile(self, attempt, amount):
        return gate.reconcile(self.db, self.account, attempt, actual_usd=amount,
                              source='invoice:123', reason='final usage confirmed', actor='desk')

    def clear(self):
        return gate.clear_breach(self.db, self.account, source='incident:123',
                                 reason='transport fixed', actor='desk',
                                 adapter_evidence='reviewed-transport:abc')

    def test_retained_attempt_releases_only_on_explicit_evidence(self):
        self.attest()
        self.reserve('uncertain', '0.9')
        gate.settle(self.db, self.account, 'uncertain', None)
        with self.assertRaises(gate.CreditRefused):
            self.reserve('next')
        with self.assertRaises(ValueError):
            gate.reconcile(self.db, self.account, 'uncertain', actual_usd='0',
                           source='', reason='unknown', actor='desk')
        self.reconcile('uncertain', '0.1')
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 100000)
        self.reserve('next')
        with self.assertRaises(ValueError):
            self.reconcile('uncertain', '0')
        journal = self.db.query('select * from credit_reconciliations')
        self.assertEqual(len(journal), 1)
        self.assertEqual(journal[0]['actor'], 'desk')
        self.assertEqual(journal[0]['evidence']['before']['state'], 'kept')

    def test_crash_with_open_reservation_can_be_reconciled_but_not_resent(self):
        self.attest()
        self.reserve('crashed')
        self.reconcile('crashed', '0')
        with self.assertRaises(gate.CreditRefused):
            self.reserve('crashed')
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 0)

    def test_breach_needs_reconciled_debt_and_separate_adapter_evidence(self):
        self.attest()
        self.reserve('breach', '0.2')
        with self.assertRaises(ValueError):
            gate.settle(self.db, self.account, 'breach', '0.3')
        with self.assertRaises(ValueError):
            self.clear()
        with self.assertRaises(ValueError):
            self.reconcile('breach', '0.1')
        self.reconcile('breach', '0.3')
        with self.assertRaises(gate.CreditRefused):
            self.reserve('still-blocked')
        with self.assertRaises(ValueError):
            gate.clear_breach(self.db, self.account, source='x', reason='x',
                              actor='desk', adapter_evidence=' ')
        self.clear()
        self.assertFalse(self.api.cycle_status(self.account)['account_breached'])
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 300000)
        self.reserve('resumed')
        self.assertEqual([r['event'] for r in self.db.query(
            'select event from credit_reconciliations order by id')], ['settle', 'clear_breach'])

    def test_higher_operator_cost_also_blocks_the_account(self):
        self.attest()
        self.reserve('unknown', '0.2')
        self.reconcile('unknown', '0.3')
        self.assertTrue(self.api.cycle_status(self.account)['account_breached'])
        with self.assertRaises(gate.CreditRefused):
            self.reserve('blocked')

    def test_larger_late_result_after_reconciliation_is_not_lost(self):
        self.attest()
        self.reserve('late')
        self.reconcile('late', '0')
        with self.assertRaises(ValueError):
            gate.settle(self.db, self.account, 'late', '0.1')
        self.assertEqual(self.api.cycle_status(self.account)['used_micro'], 400000)
        self.assertTrue(self.api.cycle_status(self.account)['account_breached'])
        with self.assertRaises(ValueError):
            self.clear()
        with self.assertRaises(gate.CreditRefused):
            self.reserve('blocked')

    def test_statement_billing_lag_keeps_recent_settlements(self):
        self.attest()
        for key in ('old', 'recent', 'pending'):
            self.reserve(key, '0.2')
        for key in ('old', 'recent'):
            gate.settle(self.db, self.account, key, '0.1')
        self.db.execute("update credit_requests set closed_at = %s where attempt_id = 'old'",
                        (self.now - timedelta(hours=3),))
        self.db.execute("update credit_requests set closed_at = %s where attempt_id = 'recent'",
                        (self.now - timedelta(minutes=30),))
        self.attest(observed_at=self.now.isoformat(), billing_lag_seconds=3600)
        status = self.api.cycle_status(self.account)
        self.assertEqual(status['billing_lag_seconds'], 3600)
        self.assertEqual(status['used_micro'], 300000)  # recent + pending, old already billed
        with self.assertRaises(gate.CreditRefused):
            self.reserve('over', '0.8')
        for lag in (-1, 0.5, True):
            with self.subTest(lag=lag), self.assertRaises(ValueError):
                self.attest(billing_lag_seconds=lag)

    def test_concurrent_operator_reconciliation_records_once(self):
        self.attest()
        self.reserve('shared')
        gate.settle(self.db, self.account, 'shared', None)
        conns = [self.connect(), self.connect()]
        barrier = threading.Barrier(2)
        results = []

        def resolve(db):
            barrier.wait(30)
            try:
                gate.reconcile(db, self.account, 'shared', actual_usd='0.1',
                               source='invoice:final', reason='checked', actor='desk')
                results.append('settled')
            except ValueError:
                results.append('refused')

        threads = [threading.Thread(target=resolve, args=(db,)) for db in conns]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(40)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertCountEqual(results, ['settled', 'refused'])
            self.assertEqual(self.db.query('select count(*) as n from credit_reconciliations')[0]['n'], 1)
        finally:
            for db in conns:
                db.close()


class OperatorAuthorityTest(unittest.TestCase):
    def authorize(self, *, source='runner', avatar=True, readable=True, owns=True):
        binding = identity.Binding(name='office-agent', source=source, ok=True)
        fiche = SimpleNamespace(data={'avatar': {'human': 'human:smichea'}} if avatar else {})
        card = SimpleNamespace(title='office-agent', roles=['bureau'], fiche=fiche,
                               responsible='human:smichea')
        canon = SimpleNamespace(agents=[card], readable=readable,
                                owns=lambda _: owns,
                                resolve_human=lambda human: object())
        with mock.patch('ameesh.identity.resolve_binding', return_value=binding), \
             mock.patch('ameesh.canon.load_configured', return_value=[canon]), \
             mock.patch('ameesh.canon.configured', return_value=[object()]), \
             mock.patch('ameesh.registry.get', return_value={'name': 'office-agent', 'canon': 'mesh'}):
            return gate.operator(object(), object())

    def test_bound_avatar_can_operate_without_a_fixed_agent_name(self):
        self.assertEqual(self.authorize(), 'office-agent')
        self.assertEqual(self.authorize(source='session'), 'office-agent')

    def test_self_declared_non_avatar_and_foreign_card_are_refused(self):
        for values in ({'source': 'explicit'}, {'avatar': False},
                       {'owns': False}, {'readable': False}):
            with self.subTest(values=values), self.assertRaises((ValueError, budget.BudgetError)):
                self.authorize(**values)
