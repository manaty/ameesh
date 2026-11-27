# SPDX-License-Identifier: AGPL-3.0-only
"""#8343 (manaty/ameesh#6583) : lecture publique des réservations ouvertes.

`ameesh reservations --json` (`ameesh-reservations/1`) et
`GET /api/v1/reservations` publient les réservations de compte encore
comptées (`open`, `kept`), de la plus ancienne à la plus récente, bornées.
La lecture ne réserve, ne règle et ne libère rien ; une ligne illisible est
signalée sans faire tomber les autres, et aucun `spend_key` ni secret n'est
rendu.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from ameesh import account_reservations, registry, serve, storage

from .support import PgTestCase

ACCOUNT_A = "acct-a"
ACCOUNT_B = "acct-b"


class AccountReservationsTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("account_reservations", "account_budgets", "budget_events",
                      "turn_costs", "spend_pending"):
            self.db.execute("DELETE FROM " + table)
        self.api = storage.of(self.db).budgets
        self.api.account_put(ACCOUNT_A, 100.0, actor="human:t")
        self.api.account_put(ACCOUNT_B, 50.0, actor="human:t")
        self._keys = 0

    # -- fixtures ----------------------------------------------------------
    def agent(self, name="a1", *, account="p1", **columns):
        registry.upsert(self.db, name, harness="deepseek", host="h1",
                        cwd=self.tmp, session_id="s-1", status="idle")
        values = {"session_account": account}
        values.update(columns)
        if values:
            self.db.execute(
                "UPDATE agent_registry SET %s WHERE name = %%s"
                % ", ".join("%s = %%s" % key for key in values),
                tuple(values.values()) + (name,))

    def today(self):
        # the database clock, like the period written by the reservation
        return self.db.query(
            "SELECT (now() at time zone 'Europe/Paris')::date::text AS day"
        )[0]["day"]

    def reservation(self, agent, account, usd, state="open", *, age="5 minutes",
                    period=None, created_ts=None):
        self._keys += 1
        key = "k-%d" % self._keys
        period = period or self.today()
        if created_ts is None:
            self.db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES (%s, %s, %s, %s::date, %s, %s, now() - %s::interval)",
                (account, agent, usd, period, state, key, age))
        else:
            self.db.execute(
                "INSERT INTO account_reservations (billing_account, agent, usd,"
                " period, state, spend_key, created_at)"
                " VALUES (%s, %s, %s, %s::date, %s, %s, to_timestamp(%s))",
                (account, agent, usd, period, state, key, float(created_ts)))
        return self.db.query(
            "SELECT id FROM account_reservations WHERE spend_key = %s",
            (key,))[0]["id"]

    def document(self, **kwargs):
        return account_reservations.open_reservations(self.db, **kwargs)

    # -- the document ------------------------------------------------------
    def test_empty_fleet_is_an_empty_document(self):
        document = self.document()
        self.assertEqual(document["schema"], "ameesh-reservations/1")
        self.assertEqual(document["count"], 0)
        self.assertEqual(document["reservations"], [])
        self.assertEqual(document["unreadable"], [])
        self.assertFalse(document["truncated"])
        self.assertEqual(document["limit"], account_reservations.DEFAULT_LIMIT)
        self.assertEqual(document["filter"],
                         {"account": None, "agent": None,
                          "states": ["open", "kept"]})
        self.assertGreater(document["observed_ts"], 0)

    def test_several_reservations_are_sorted_bounded_and_aged(self):
        self.agent("a1")
        self.agent("a2", account="p2")
        self.agent("a3")
        oldest = self.reservation("a2", ACCOUNT_B, 3.5, "kept", age="2 hours")
        middle = self.reservation("a1", ACCOUNT_A, 2.0, age="10 minutes")
        newest = self.reservation("a3", ACCOUNT_A, 4.25, age="1 minute")

        document = self.document(limit=2)
        self.assertTrue(document["truncated"])
        self.assertEqual([row["id"] for row in document["reservations"]],
                         [oldest, middle])
        self.assertEqual([row["state"] for row in document["reservations"]],
                         ["kept", "open"])

        document = self.document(limit=10)
        self.assertFalse(document["truncated"])
        self.assertEqual([row["id"] for row in document["reservations"]],
                         [oldest, middle, newest])
        rows = {row["id"]: row for row in document["reservations"]}
        self.assertEqual(rows[oldest]["agent"], "a2")
        self.assertEqual(rows[oldest]["billing_account"], ACCOUNT_B)
        self.assertEqual(rows[oldest]["usd"], 3.5)
        self.assertEqual(rows[oldest]["period"], self.today())
        self.assertTrue(rows[oldest]["account_envelope"])
        self.assertGreater(rows[oldest]["period_reset_ts"],
                           rows[oldest]["created_ts"])
        self.assertGreater(rows[oldest]["age_s"], 7000)
        self.assertLess(rows[oldest]["age_s"], 7400)
        self.assertGreater(rows[middle]["age_s"], 500)
        self.assertLess(rows[middle]["age_s"], 700)
        self.assertEqual(rows[middle]["account_profile"], "p1")
        self.assertAlmostEqual(rows[newest]["usd"], 4.25)

        self.assertEqual([row["id"] for row in
                          self.document(state="open", limit=10)["reservations"]],
                         [middle, newest])
        self.assertEqual([row["id"] for row in
                          self.document(state="kept", limit=10)["reservations"]],
                         [oldest])
        self.assertEqual([row["id"] for row in
                          self.document(account=ACCOUNT_B)["reservations"]],
                         [oldest])
        self.assertEqual([row["id"] for row in
                          self.document(agent="a1")["reservations"]], [middle])

    def test_expired_kept_is_not_counted_and_old_open_is(self):
        self.agent("a1")
        expired = self.reservation("a1", ACCOUNT_A, 3.0, "kept",
                                   period="2020-01-01", age="2 hours")
        opened = self.reservation("a1", ACCOUNT_A, 2.0, "open",
                                  period="2020-01-01", age="1 hour")
        current = self.reservation("a1", ACCOUNT_A, 1.0, "kept",
                                   age="5 minutes")
        document = self.document(limit=10)
        self.assertEqual([row["id"] for row in document["reservations"]],
                         [opened, current])
        self.assertNotIn(expired,
                         [row["id"] for row in document["reservations"]])
        self.assertEqual(document["unreadable"], [])
        self.assertEqual([row["id"] for row in
                          self.document(state="kept", limit=10)["reservations"]],
                         [current])
        # an old open is still in flight, whatever its period
        self.assertEqual([row["id"] for row in
                          self.document(state="open", limit=10)["reservations"]],
                         [opened])

    def test_kept_counts_only_in_the_envelope_timezone_day(self):
        account = "acct-tz"
        self.api.account_put(account, 10.0, actor="human:t", tz="Pacific/Auckland")
        # 2026-11-27 10:59Z = 23:59 in Auckland (NZDT); one minute later is
        # midnight there, so the calendar day of the envelope changes.
        before = datetime(2026, 11, 27, 10, 59, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 11, 27, 11, 1, tzinfo=timezone.utc).timestamp()
        old_kept = self.reservation("a1", account, 2.0, "kept",
                                    period="2026-11-27", created_ts=before - 120)
        old_open = self.reservation("a1", account, 1.0, "open",
                                    period="2026-11-27", created_ts=before - 60)
        new_kept = self.reservation("a1", account, 3.0, "kept",
                                    period="2026-11-28", created_ts=after + 60)

        def listed(at):
            return [row["id"] for row in self.api.account_open_reservations(
                limit=10, account=account, states=["open", "kept"], at=at)]

        self.assertEqual(listed(before), [old_kept, old_open])
        self.assertEqual(listed(after), [old_open, new_kept])
        rows = {row["id"]: row for row in self.api.account_open_reservations(
            limit=10, account=account, states=["open", "kept"], at=before)}
        # midnight in the envelope's zone is 11:00Z, one minute after `before`
        self.assertAlmostEqual(rows[old_kept]["period_reset_ts"], before + 60,
                               places=3)

    def test_unknown_agent_is_listed_not_fatal(self):
        self.agent("a1")
        known = self.reservation("a1", ACCOUNT_A, 2.0)
        ghost = self.reservation("ghost", ACCOUNT_B, 1.5, "kept")
        document = self.document()
        self.assertEqual(document["count"], 2)
        self.assertEqual(document["unreadable"], [])
        rows = {row["id"]: row for row in document["reservations"]}
        self.assertTrue(rows[known]["agent_known"])
        self.assertEqual(rows[known]["account_profile"], "p1")
        self.assertFalse(rows[ghost]["agent_known"])
        self.assertIsNone(rows[ghost]["account_profile"])
        self.assertEqual(rows[ghost]["agent"], "ghost")

    def test_unreadable_row_is_signalled_and_never_breaks_json(self):
        self.agent("a1")
        good = self.reservation("a1", ACCOUNT_A, 2.0)
        # `usd >= 0` accepts NaN in PostgreSQL: the row exists, but a finite
        # amount cannot be rendered. It must be reported, not crash the read.
        self.db.execute(
            "INSERT INTO account_reservations (billing_account, agent, usd,"
            " period, state, spend_key)"
            " VALUES (%s, 'a1', 'NaN'::float8, %s::date, 'kept', 'nan-key')",
            (ACCOUNT_A, self.today()))
        bad_id = self.db.query(
            "SELECT id FROM account_reservations WHERE spend_key = 'nan-key'"
        )[0]["id"]
        document = self.document()
        self.assertEqual([row["id"] for row in document["reservations"]], [good])
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["unreadable"], [{
            "id": bad_id, "agent": "a1", "billing_account": ACCOUNT_A,
            "state": "kept", "reason": "invalid_amount"}])
        # the emitted document is valid JSON: no NaN/Infinity token escapes
        encoded = json.dumps(document, ensure_ascii=False, allow_nan=False)
        self.assertEqual(json.loads(encoded)["unreadable"][0]["reason"],
                         "invalid_amount")

    def test_read_only_snapshot_and_closed_projection(self):
        self.agent(
            "a1", session_account="p1", public_key="CANARY-PUBLIC-KEY",
            pending_prompt="CANARY-PENDING", current_prompt="CANARY-CURRENT",
            last_error="CANARY-ERROR", cwd="/srv/CANARY-CWD",
            status="blocked", status_text="CANARY-STATUS")
        self.reservation("a1", ACCOUNT_A, 2.0)
        opened = self.document()
        before = self.db.query(
            "SELECT id, state, usd::float8 AS usd, closed_at"
            " FROM account_reservations ORDER BY id")
        events = self.db.query("SELECT count(*)::int AS n FROM budget_events")[0]["n"]

        # a read-only connection proves the builder cannot write; a write
        # would fail at the database, not merely be absent from the code.
        read_only = self.connect(read_only=True)
        try:
            again = account_reservations.open_reservations(read_only)
        finally:
            read_only.close()
        self.assertEqual([row["id"] for row in again["reservations"]],
                         [row["id"] for row in opened["reservations"]])
        after = self.db.query(
            "SELECT id, state, usd::float8 AS usd, closed_at"
            " FROM account_reservations ORDER BY id")
        self.assertEqual(after, before)
        self.assertEqual(
            self.db.query("SELECT count(*)::int AS n FROM budget_events")[0]["n"],
            events)

        raw = json.dumps(again, ensure_ascii=False, allow_nan=False)
        for forbidden in ("CANARY", "spend_key", "public_key", "key_env",
                          "password", "dsn"):
            self.assertNotIn(forbidden, raw)
        self.assertEqual(set(again), {
            "schema", "observed_ts", "limit", "truncated", "filter", "count",
            "reservations", "unreadable"})
        self.assertEqual(set(again["reservations"][0]), {
            "id", "agent", "agent_known", "account_profile", "billing_account",
            "usd", "state", "period", "created_ts", "age_s",
            "account_envelope", "period_reset_ts"})

    def test_cli_and_http_are_one_document(self):
        self.agent("a1")
        self.reservation("a1", ACCOUNT_A, 2.0)
        self.reservation("ghost", ACCOUNT_B, 1.5, "kept", age="1 hour")
        ctx = serve.Context(cfg=self.cfg, human="human:owner")
        served = serve._reservations(ctx, self.db, {}, {})
        out = self.mesh("reservations", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        cli = json.loads(out.stdout)
        self.assertEqual(cli["schema"], served["schema"])
        self.assertEqual(cli["filter"], served["filter"])
        self.assertEqual([row["id"] for row in cli["reservations"]],
                         [row["id"] for row in served["reservations"]])
        self.assertEqual(
            [(row["agent"], row["billing_account"], row["usd"], row["state"])
             for row in cli["reservations"]],
            [(row["agent"], row["billing_account"], row["usd"], row["state"])
             for row in served["reservations"]])

        kept = serve._reservations(ctx, self.db, {},
                                   {"state": ["kept"], "limit": ["1"]})
        self.assertEqual(kept["filter"]["states"], ["kept"])
        self.assertEqual(kept["limit"], 1)
        self.assertFalse(kept["truncated"])

        text = self.mesh("reservations")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("RESERVATIONS", text.stdout)
        self.assertIn("a1", text.stdout)

        for query in ({"limit": ["0"]}, {"limit": ["501"]},
                      {"state": ["weird"]}, {"account": ["one", "two"]}):
            with self.subTest(query=query), self.assertRaises(serve.ApiError) as caught:
                serve._reservations(ctx, self.db, {}, query)
            self.assertEqual((caught.exception.status, caught.exception.code),
                             (400, "bad_query"))
        for argv in (("reservations", "--limit", "0"),
                     ("reservations", "--limit", "501"),
                     ("reservations", "--state", "weird")):
            self.assertEqual(self.mesh(*argv).returncode, 2, argv)

    def test_route_is_documented_with_the_frozen_shape(self):
        document = serve.openapi()
        operation = document["paths"]["/api/v1/reservations"]["get"]
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]
            ["schema"]["$ref"].rsplit("/", 1)[1],
            "ameesh-reservations-1")
        params = {param["name"]: param for param in operation["parameters"]}
        self.assertEqual(set(params), {"limit", "account", "agent", "state"})
        self.assertEqual(params["state"]["schema"]["enum"], ["all", "open", "kept"])
        self.assertEqual(params["limit"]["schema"]["maximum"],
                         account_reservations.MAX_LIMIT)


class DocumentValidationTest(unittest.TestCase):
    """Closed reasons and bounds, without a database."""

    base = {
        "id": 1, "agent": "a1", "billing_account": ACCOUNT_A,
        "session_account": "p1", "agent_known": True, "usd": 2.0,
        "state": "open", "period": "2026-11-27", "created_ts": 100.0,
        "age_s": 5.0, "account_envelope": True, "period_reset_ts": 200.0,
    }

    def row(self, **changes):
        return account_reservations._row(dict(self.base, **changes))

    def test_every_unreadable_reason_is_closed_and_specific(self):
        for reason, changes in (
                ("invalid_id", {"id": 0}),
                ("invalid_identity", {"agent": ""}),
                ("invalid_identity", {"billing_account": None}),
                ("invalid_state", {"state": "settled"}),
                ("invalid_amount", {"usd": float("nan")}),
                ("invalid_amount", {"usd": -1.0}),
                ("invalid_timestamp", {"created_ts": None}),
                ("invalid_timestamp", {"age_s": float("inf")}),
                ("invalid_period", {"period": ""})):
            with self.subTest(reason=reason, changes=changes):
                row, bad = self.row(**changes)
                self.assertIsNone(row)
                self.assertEqual(bad["reason"], reason)
                self.assertIn(bad["reason"], account_reservations.UNREADABLE_REASONS)

    def test_bounds_and_truncation(self):
        with self.assertRaises(ValueError):
            account_reservations.open_reservations(
                None, limit=account_reservations.MAX_LIMIT + 1)
        with self.assertRaises(ValueError):
            account_reservations.open_reservations(None, state="settled")
        raw = [dict(self.base, id=index) for index in (1, 2)]
        document = account_reservations._document(
            raw, at=300.0, limit=1, account=None, agent=None,
            states=["open", "kept"])
        self.assertTrue(document["truncated"])
        self.assertEqual([row["id"] for row in document["reservations"]], [1])
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["observed_ts"], 300.0)
