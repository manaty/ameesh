# SPDX-License-Identifier: AGPL-3.0-only
"""#3923 — la lecture du registre et les soldes ne repassent plus par le tas.

Constat (27/10, base commune) : `operations.listing` coûtait 1,2 à 1,4 s et
partait deux fois par passe et par exécuteur ; ses latérales de courrier et de
lots relisaient le tas, et `provider_balances` était relu pour chaque candidat
de la prise. Les témoins ci-dessous fixent les trois lectures d'index (0495),
les comptes de non-lus inchangés, l'absence de lecture du registre dans la
garde locale de solde, et le partage d'une lecture de soldes par passe.
"""
from __future__ import annotations

import json
import time
import unittest

from ameesh import cost, runner, storage
from ameesh import db as db_mod
from ameesh.storage.postgres.mailbox import PASSIVE_SQL

from .support import PgTestCase


class ListingIndexDbTest(PgTestCase):
    """0495 : les trois index partiels du listing sont ceux du plan."""

    ACTIF = "agent_mailbox_unread_active_idx"
    RECENT = "work_items_assignee_recent_idx"
    REVIEWER = "work_items_reviewer_state_idx"

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM agent_mailbox")
        self.db.execute(
            "INSERT INTO agent_registry (name, status) VALUES ('t3923', 'idle')"
            " ON CONFLICT DO NOTHING")

    def _plan(self, sql, params=()) -> str:
        # le pilote psql enveloppe chaque lecture dans un agrégat JSON, où
        # `EXPLAIN` ne peut pas entrer : le plan se lit sous psycopg.
        if not isinstance(self.db, db_mod.PsycopgDriver):
            self.skipTest("EXPLAIN is wrapped by the psql driver")
        with self.db.transaction() as tx:
            tx.execute("SET LOCAL enable_seqscan = off")
            tx.execute("SET LOCAL enable_bitmapscan = off")
            return json.dumps(tx.query("EXPLAIN (FORMAT JSON) " + sql, params))

    def test_the_active_index_predicate_is_not_passive_sql(self):
        # PostgreSQL décompile les deux prédicats de la même façon : un index
        # de contrôle bâti sur la négation de `PASSIVE_SQL` doit rendre le
        # même texte. Un changement du prédicat passif casse ce témoin.
        self.db.execute("DROP TABLE IF EXISTS mailbox_3923")
        self.db.execute("CREATE TABLE mailbox_3923 (LIKE agent_mailbox)")
        self.db.execute(
            "CREATE INDEX mailbox_3923_idx ON mailbox_3923 (recipient, id)"
            " INCLUDE (created_at) WHERE delivered_at IS NULL AND NOT "
            + PASSIVE_SQL.format(t="mailbox_3923"))
        rows = self.db.query(
            "SELECT c.relname, pg_get_expr(i.indpred, i.indrelid) AS pred"
            "  FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname IN (%s, 'mailbox_3923_idx')"
            "   AND c.relnamespace = current_schema()::regnamespace",
            (self.ACTIF,))
        preds = {r["relname"]: r["pred"] for r in rows}
        self.assertEqual(set(preds), {self.ACTIF, "mailbox_3923_idx"})
        self.assertEqual(preds[self.ACTIF], preds["mailbox_3923_idx"])

    def test_the_listing_reads_the_three_partial_indexes(self):
        # des lignes réelles pour que le planificateur ait des statistiques
        for payload in ({}, {"ack": True}, {"info": True}):
            self.db.execute(
                "INSERT INTO agent_mailbox (sender, recipient, body, payload)"
                " VALUES ('test', 't3923', 'x', %s::jsonb)",
                (json.dumps(payload),))
        self.db.execute(
            "INSERT INTO work_items (title, type, state, assignee)"
            " VALUES ('lot 3923', 'evolution', 'build', 't3923')")
        # #3946 : avec un seul lot, l'index d'état (tous les `qa`) et l'index
        # partiel de relecture (relecteur, état) coûtent exactement pareil : le
        # départage dépend de l'ordre des index, que le rejeu du gabarit
        # (`pg_dump`, le chemin de la CI) ne donne pas comme `migrate()` (le
        # chemin local). Assez de lots `qa` d'un autre relecteur pour que le
        # partiel soit nettement moins cher : le plan ne dépend plus d'un
        # départage.
        self.db.execute(
            "INSERT INTO work_items (title, type, state, reviewer)"
            " SELECT 'lot qa ' || g, 'evolution', 'qa', 'autre-relecteur'"
            "   FROM generate_series(1, 500) g")
        self.db.execute("ANALYZE agent_mailbox")
        self.db.execute("ANALYZE work_items")
        cap = {}
        original = self.db.query

        def capture(sql, params=()):
            cap["sql"], cap["params"] = sql, tuple(params)
            return original(sql, params)

        self.db.query = capture
        try:
            rows = storage.of(self.db).operations.listing()
        finally:
            self.db.query = original
        self.assertEqual([r["name"] for r in rows], ["t3923"])
        plan = self._plan(cap["sql"], cap["params"])
        for index in (self.ACTIF, "agent_mailbox_passive_unread_idx",
                      self.RECENT, self.REVIEWER):
            self.assertIn(index, plan)

    def test_the_counts_are_unchanged(self):
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, payload, created_at)"
            " VALUES ('test', 't3923', 'x', '{}'::jsonb, now() - interval '2 hours')")
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, payload, created_at)"
            " VALUES ('test', 't3923', 'x', '{\"urgent\": true}'::jsonb, now())")
        for payload in ({"ack": True}, {"info": True}, {"cc": "orch"},
                        {"radar": {"lot": 1}}):
            self.db.execute(
                "INSERT INTO agent_mailbox (sender, recipient, body, payload)"
                " VALUES ('test', 't3923', 'x', %s::jsonb)",
                (json.dumps(payload),))
        self.db.execute(
            "INSERT INTO agent_mailbox (sender, recipient, body, payload, delivered_at)"
            " VALUES ('test', 't3923', 'x', '{\"ack\": true}'::jsonb, now())")
        row = storage.of(self.db).operations.listing()[0]
        self.assertEqual((row["unread"], row["passive_unread"]), (2, 4))
        self.assertAlmostEqual(
            float(row["oldest_unread_ts"]),
            float(self.db.query("SELECT extract(epoch from now() - interval '2 hours')"
                                " AS ts")[0]["ts"]), delta=60.0)


class BalancesTest(PgTestCase):
    """#3923 : la fenêtre + le dernier relevé antérieur, sans tri d'historique."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE provider_balances RESTART IDENTITY")
        self.db.execute(
            "INSERT INTO provider_balances (provider, currency, account, total, observed_at)"
            " VALUES ('deepseek', 'USD', NULL, 10, now() - interval '3 hours'),"
            "        ('deepseek', 'USD', NULL, 9,  now() - interval '2 hours'),"
            "        ('deepseek', 'USD', NULL, 8,  now() - interval '30 minutes')")

    def _rows(self, **kw):
        return storage.of(self.db).operations.balances(provider=None, **kw)

    def test_the_window_and_the_series_are_unchanged(self):
        rows = self._rows(since_s=3600)
        self.assertEqual([r["total"] for r in rows], [9.0, 8.0])
        self.assertEqual([r["total"] for r in self._rows(since_s=0.0)], [8.0])
        self.assertEqual([r["total"] for r in self._rows(since_s=86400)],
                         [10.0, 9.0, 8.0])

    def test_the_filters_still_apply_to_both_branches(self):
        self.assertEqual(storage.of(self.db).operations.balances(
            provider="deepseek", since_s=3600, account=None)[0]["total"], 9.0)
        self.assertEqual(storage.of(self.db).operations.balances(
            provider="autre", since_s=3600, account=None), [])


class LocalBalanceTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE provider_balances RESTART IDENTITY")

    def test_local_alerts_do_not_read_the_registry_listing(self):
        self.db.execute(
            "INSERT INTO provider_balances (provider, currency, total, observed_at)"
            " VALUES ('deepseek', 'USD', 5, now())")
        seen = []
        original = self.db.query

        def capture(sql, params=()):
            seen.append(sql)
            return original(sql, params)

        self.db.query = capture
        try:
            alerts = runner.local_balance_alerts(
                self.db, {"hours": 0, "min_usd": 10, "window_s": 3600}, time.time())
        finally:
            self.db.query = original
        self.assertEqual([a["type"] for a in alerts], ["balance_low"])
        self.assertFalse(any("r.chantier" in sql for sql in seen),
                         "la garde locale relit operations.listing")


class BudgetPassCacheTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE provider_balances RESTART IDENTITY")
        self.db.execute("DELETE FROM turn_costs")

    def _book(self) -> cost.CostBook:
        return cost.CostBook(
            state_dir=self.state, db=self.db,
            prices_path=self.tmp + "/prices.json",
            codex_sessions=self.tmp + "/codex", tools={"a1": "deepseek"},
            hourly_usd=1.0, daily_usd=0.0)

    def _count(self, fn) -> int:
        n = {"balances": 0}
        original = self.db.query

        def capture(sql, params=()):
            up = sql.upper()
            if "PROVIDER_BALANCES" in up and "UNION" in up:
                n["balances"] += 1
            return original(sql, params)

        self.db.query = capture
        try:
            fn()
        finally:
            self.db.query = original
        return n["balances"]

    def test_one_balance_read_per_pass(self):
        book = self._book()
        cache: dict = {}

        def pass_calls():
            for _ in range(3):
                self.assertEqual(book.over(
                    "a1", paid_harnesses=("deepseek",), pace=False,
                    account_checked=True, cache=cache), "")

        self.assertEqual(self._count(pass_calls), 1)
        # le même cache ne relit pas ; une passe suivante (cache vidé) relit.
        self.assertEqual(self._count(pass_calls), 0)
        cache.clear()
        self.assertEqual(self._count(pass_calls), 1)
        self.assertEqual(
            book.paid_spent(("deepseek",), 3600, "a1", cache=cache),
            book.paid_spent(("deepseek",), 3600, "a1", cache=cache))


if __name__ == "__main__":
    unittest.main()
