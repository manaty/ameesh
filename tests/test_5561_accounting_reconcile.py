# SPDX-License-Identifier: AGPL-3.0-only
"""Exact-marker human recovery and historical accounting separation (#4162)."""
from __future__ import annotations

import unittest
import threading
from unittest import mock

from .support import PgTestCase
from ameesh import registry, storage
from ameesh.runner import AgentWorker


class AccountingReconcileTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute('DELETE FROM spend_marker_events')
        self.db.execute('DELETE FROM account_reservations')
        self.db.execute('DELETE FROM turn_costs')
        self.db.execute('DELETE FROM spend_pending')
        self.api = storage.of(self.db).pending_spend

    def marker(self, name='reconcile'):
        proc = self.register(name, 'deepseek')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.db.execute("UPDATE agent_registry SET status = 'blocked',"
                        " status_text = 'budget : turn accounting unavailable' WHERE name = %s", (name,))
        self.db.execute("INSERT INTO spend_pending(agent,start_index,created_at)"
                        " VALUES (%s,2,'2026-10-20T23:51:40.160356Z')", (name,))
        return self.api.get(name)

    def reservation(self, key, *, before=False, state='kept'):
        self.db.execute("INSERT INTO account_reservations"
                        " (agent,billing_account,usd,period,state,spend_key,created_at)"
                        " VALUES ('reconcile','test-account',2,current_date,%s,%s,"
                        " '2026-10-20T23:51:40.160356Z'::timestamptz + %s::interval)",
                        (state, key, '-1 hour' if before else '1 second'))

    def cost(self, key, *, before=False):
        self.db.execute("INSERT INTO turn_costs(agent,harness,model,usd,spend_key,recorded_at)"
                        " VALUES ('reconcile','deepseek','m',0.75,%s,"
                        " '2026-10-20T23:51:40.160356Z'::timestamptz + %s::interval)",
                        (key, '-1 hour' if before else '2 seconds'))

    def resolve(self, *, key=None, abandon=False):
        return self.api.reconcile('reconcile', spend_key=key, abandon=abandon,
                                  actor='human:chef', reason='checked historical accounting')

    def test_prior_rows_do_not_contradict_unique_posterior_candidate(self):
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation('reconcile:2:1792530000', before=True, state='open')
        self.cost('reconcile:2:1792530000', before=True)
        self.reservation(key)
        marker = self.api.get('reconcile')
        self.assertFalse(marker['ledger_ambiguous'])
        self.assertEqual(marker['ledger_key'], key)
        # The prior reservation is foreign; its accounting is untouched.
        self.assertEqual(self.db.query("SELECT state FROM account_reservations"
                                       " WHERE spend_key='reconcile:2:1792530000'")[0]['state'], 'open')

    def test_posterior_foreign_row_still_blocks(self):
        self.marker()
        self.reservation('reconcile:2:1792540300.16036')
        self.cost('reconcile:2:1792530000')
        self.assertTrue(self.api.get('reconcile')['ledger_ambiguous'])

    def test_link_audited_and_does_not_clear_marker_or_settle_reservation(self):
        marker = self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation(key)
        self.cost('reconcile:2:1792530000')
        self.assertTrue(self.api.get('reconcile')['ledger_ambiguous'])
        result = self.resolve(key=key)
        resolved = self.api.get('reconcile')
        self.assertEqual(resolved['ledger_key'], key)
        self.assertFalse(resolved['ledger_ambiguous'])
        self.assertEqual(resolved['created_at'], marker['created_at'])
        audit = self.db.query('SELECT actor, reason, snapshot FROM spend_marker_events WHERE id=%s',
                              (result['id'],))[0]
        self.assertEqual(audit['actor'], 'human:chef')
        self.assertTrue(audit['reason'])
        self.assertEqual(audit['snapshot']['keys'], sorted([key, 'reconcile:2:1792530000']))
        self.assertEqual(storage.of(self.db).budgets.account_get(key)['state'], 'kept')

    def test_resolution_does_not_survive_marker_replacement(self):
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation(key)
        self.resolve(key=key)
        replacement = self.api.put_get('reconcile', 2, 'next', 'm')
        resolved = self.api.get('reconcile')
        self.assertEqual(resolved['ledger_key'], replacement['spend_key'])
        self.assertNotEqual(resolved['ledger_key'], key)

    def test_abandon_settles_real_cost_and_audits_in_same_transaction(self):
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation(key)
        self.cost(key)
        self.resolve(abandon=True)
        self.assertIsNone(self.api.get('reconcile'))
        self.assertEqual(storage.of(self.db).budgets.account_get(key)['state'], 'settled')
        ledger = self.db.query('SELECT usd,billing_account FROM turn_costs WHERE spend_key=%s', (key,))[0]
        self.assertEqual(ledger['usd'], .75)
        self.assertEqual(ledger['billing_account'], 'test-account')
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM spend_marker_events"
                                      " WHERE event='abandon'")[0]['n'], 1)

    def test_abandon_without_actual_cost_rolls_back_all_settlements(self):
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation(key)
        self.cost(key)
        self.reservation('reconcile:2:unknown')
        with self.assertRaisesRegex(ValueError, 'actual cost unavailable'):
            self.resolve(abandon=True)
        self.assertIsNotNone(self.api.get('reconcile'))
        self.assertEqual(storage.of(self.db).budgets.account_get(key)['state'], 'kept')
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM spend_marker_events')[0]['n'], 0)

    def test_link_rejects_prior_or_unknown_key_and_running_agent(self):
        self.marker()
        self.reservation('reconcile:2:old', before=True)
        for key in ('reconcile:2:old', 'other:2:unknown'):
            with self.assertRaisesRegex(ValueError, 'no posterior'):
                self.resolve(key=key)
        self.db.execute("UPDATE agent_registry SET status='running' WHERE name='reconcile'")
        with self.assertRaisesRegex(ValueError, 'must be blocked'):
            self.resolve(key='reconcile:2:old')

    def test_incident_is_unique_across_retries_and_replacements_are_distinct(self):
        self.marker()
        self.cost('reconcile:2:foreign')
        marker = registry.pending_spend_get(self.db, 'reconcile')
        self.assertTrue(marker['ledger_ambiguous'])
        self.assertTrue(self.api.report_ambiguous('reconcile', marker))
        self.assertFalse(storage.of(self.db).pending_spend.report_ambiguous('reconcile', marker))
        self.api.put_get('reconcile', 2, 'next', 'm')
        self.assertFalse(self.api.report_ambiguous('reconcile', marker))

    def test_worker_reports_only_once_but_always_refuses_ambiguous_accounting(self):
        self.marker()
        self.cost('reconcile:2:foreign')
        marker = registry.pending_spend_get(self.db, 'reconcile')
        worker = object.__new__(AgentWorker)
        threading.Thread.__init__(worker)
        worker.db, worker.name = self.db, 'reconcile'
        worker._fil_note = mock.Mock()
        with mock.patch('ameesh.runner.log_async') as log:
            for _ in range(3):
                self.assertFalse(worker._compta_ecrit(marker))
                self.assertTrue(worker._compta_en_echec)
        self.assertEqual(log.call_count, 1)
        self.assertEqual(worker._fil_note.call_count, 1)
        self.assertIsNotNone(self.api.get('reconcile'))

    def test_abandon_audit_failure_rolls_back_marker_and_reservation(self):
        from ameesh import db as db_mod
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation(key)
        self.cost(key)
        self.db.execute("CREATE FUNCTION reject_reconciliation() RETURNS trigger LANGUAGE plpgsql"
                        " AS $$ BEGIN RAISE EXCEPTION 'audit unavailable'; END $$")
        self.db.execute('CREATE TRIGGER reject_reconciliation BEFORE INSERT ON spend_marker_events'
                        ' FOR EACH ROW EXECUTE FUNCTION reject_reconciliation()')
        try:
            with self.assertRaises(db_mod.DbError):
                self.resolve(abandon=True)
            self.assertIsNotNone(self.api.get('reconcile'))
            self.assertEqual(storage.of(self.db).budgets.account_get(key)['state'], 'kept')
            self.assertIsNone(self.db.query('SELECT billing_account FROM turn_costs'
                                           ' WHERE spend_key=%s', (key,))[0]['billing_account'])
        finally:
            self.db.execute('DROP TRIGGER reject_reconciliation ON spend_marker_events')
            self.db.execute('DROP FUNCTION reject_reconciliation()')

    def test_running_resources_refuse_resolution(self):
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation(key)
        self.db.execute("INSERT INTO turn_resources(turn_id,agent,host,label)"
                        " VALUES ('active','reconcile','test','test')")
        try:
            with self.assertRaisesRegex(ValueError, 'resources still active'):
                self.resolve(key=key)
        finally:
            self.db.execute("DELETE FROM turn_resources WHERE turn_id='active'")

    def test_human_abandonment_lifts_existing_workers_accounting_block(self):
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation(key)
        self.cost(key)
        self.resolve(abandon=True)
        worker = object.__new__(AgentWorker)
        threading.Thread.__init__(worker)
        worker._compta_doute = False
        worker._compta_en_echec = True
        worker._compta_en_attente = lambda: registry.pending_spend_get(self.db, 'reconcile')
        worker._compta_repare()
        self.assertFalse(worker._compta_en_echec)

    def test_proven_resolution_cannot_be_rebound_to_another_key(self):
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        other = 'reconcile:2:other'
        self.reservation(key)
        self.reservation(other)
        self.resolve(key=key)
        with self.assertRaisesRegex(ValueError, 'different proven'):
            self.resolve(key=other)

    def test_cli_cannot_claim_another_human_or_bypass_agent_identity(self):
        self.marker()
        key = 'reconcile:2:1792540300.16036'
        self.reservation(key)
        args = ('budget', 'reconcile', 'reconcile', '--spend-key', key,
                '--reason', 'verified', '--human', 'human:chef')
        for env in (self.env(AGENT_MAIL_NAME='reconcile', AMEESH_HUMANS='chef', USER='chef'),
                    self.env(AMEESH_HUMANS='intrus', USER='intrus')):
            proc = self.mesh(*args, env=env)
            self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.db.query('SELECT count(*) AS n FROM spend_marker_events')[0]['n'], 0)
        proc = self.mesh(*args, env=self.env(AMEESH_HUMANS='chef', USER='chef'))
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == '__main__':
    unittest.main()
