# SPDX-License-Identifier: AGPL-3.0-only
"""#8341 (manaty/ameesh#6581): the fixture's agent qualification states, 1.7.

IMA's module tests needed agents in precise states (expired or foreign lease,
stale canon, refused placement, ambiguous spend marker, open or settled
reservation, cost row) and wrote the core's tables to get them.
``Fixture.agent``/``Fixture.agent_state`` reach those states through the
core's own code on the disposable database and read them back with the agent
observation.  The boundary tests run without a database; the database test
reaches every state, its reverse, a composed refusal, and proves that the
consumer's own database is never written.
"""
from __future__ import annotations

import unittest
from unittest import mock

from tests import support  # isolate host config before product imports
from tests.test_7701_public_fixture import ADMIN, bench_can_create_database, psql
from ameesh.public import _fixture_states as states_mod
from ameesh.public import fixture as fixture_mod

SECRET = "s3cr3t-pw"
SECRET_DSN = "postgresql://witness:%s@127.0.0.1:1/consumer" % SECRET


class StateBoundaryTest(unittest.TestCase):
    """Refusals and redaction of the state helpers, before any database."""

    def setUp(self):
        self.fixture = fixture_mod.Fixture(SECRET_DSN)

    def test_the_state_error_is_a_fixture_error_with_its_code(self):
        self.assertTrue(issubclass(fixture_mod.FixtureStateError, fixture_mod.FixtureError))
        self.assertEqual(fixture_mod.FixtureStateError.code, "state")
        self.assertEqual(fixture_mod.AGENT_STATES, states_mod.AGENT_STATES)

    def test_an_uncreated_fixture_refuses_the_state_helpers(self):
        with self.assertRaises(fixture_mod.FixtureRefused):
            self.fixture.agent("ima-agent")
        with self.assertRaises(fixture_mod.FixtureRefused):
            self.fixture.agent_state("ima-agent", "lease_live")

    def test_an_unknown_state_a_bad_name_or_option_is_refused(self):
        self.fixture._created = True
        with self.assertRaises(fixture_mod.FixtureRefused) as caught:
            self.fixture.agent_state("ima-agent", "lease_stolen")
        self.assertIn("lease_expired", str(caught.exception))
        for name in ("", "a b", "x;drop", "../agent", None):
            with self.subTest(name=name), self.assertRaises(fixture_mod.FixtureRefused):
                self.fixture.agent_state(name, "lease_live")
        for options in ({"usd": {"sql": "1"}}, {"usd": None}, {"usd": True},
                        {"spendkey": "typo"}, {"owner": "x"}):
            with self.subTest(options=options), \
                    self.assertRaises(fixture_mod.FixtureRefused):
                self.fixture.agent_state("ima-agent", "cost", **options)
        with self.assertRaises(fixture_mod.FixtureRefused):
            self.fixture.agent("ima-agent", host="bad host")

    def test_a_core_failure_is_a_state_error_without_the_dsn(self):
        self.fixture._created = True
        self.fixture._dsn = SECRET_DSN.replace("/consumer", "/ameesh_fixture_x")
        self.fixture._db = object()

        def boom(_db):
            raise RuntimeError("cannot reach %s (password %s)"
                               % (self.fixture._dsn, SECRET))
        with self.assertRaises(fixture_mod.FixtureStateError) as caught:
            self.fixture._state_call(boom)
        message = str(caught.exception)
        self.assertNotIn(SECRET, message)
        self.assertNotIn("postgresql://", message)
        self.assertIn("<redacted>", message)

    def test_a_state_not_reached_keeps_its_own_message(self):
        self.fixture._created = True
        self.fixture._db = object()
        with mock.patch.object(states_mod, "reach",
                               side_effect=states_mod.StateNotReached("not here")):
            with self.assertRaises(fixture_mod.FixtureStateError) as caught:
                self.fixture.agent_state("ima-agent", "lease_live")
        self.assertEqual(str(caught.exception), "not here")

    def test_every_state_has_a_handler_a_check_and_its_options(self):
        self.assertEqual(set(states_mod._HANDLERS), set(states_mod.AGENT_STATES))
        self.assertEqual(set(states_mod._CHECKS), set(states_mod.AGENT_STATES))
        self.assertEqual(set(states_mod.STATE_OPTIONS), set(states_mod.AGENT_STATES))


@unittest.skipUnless(support.REQUIRE_DB, "banc Postgres requis (AMEESH_REQUIRE_DB=0)")
class FixtureStatesDatabaseTest(unittest.TestCase):
    """Every state on a disposable database, through the public fixture only."""

    AGENT = "ima-state-8341"

    @classmethod
    def setUpClass(cls):
        if not bench_can_create_database():
            raise unittest.SkipTest("bench role has no CREATEDB")
        cls.fixture = fixture_mod.Fixture(ADMIN).create()
        cls.commit = cls.fixture.canon({"index.md": "# states\n"})

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def setUp(self):
        # Each test starts from a freshly registered agent: the reverse states
        # undo what the previous test left (the first one finds no agent).
        self.agent = self.AGENT
        try:
            self.fixture.agent_state(self.agent, "lease_free")
            self.fixture.agent_state(self.agent, "spend_clear")
        except fixture_mod.FixtureStateError:
            pass
        self.registered = self.fixture.agent(self.agent)

    def state(self, state, **options):
        report = self.fixture.agent_state(self.agent, state, **options)
        self.assertEqual(report["state"], state)
        self.assertEqual(report["agent"], self.agent)
        return report

    def test_a_registered_agent_is_governed_fresh_admitted_and_claimable(self):
        report = self.registered
        self.assertIsNone(report["lease"]["owner"])
        self.assertTrue(report["canon"]["fresh"])
        self.assertEqual(report["canon"]["last_good_commit"], self.commit)
        self.assertTrue(report["canon"]["ref_matches"])
        self.assertTrue(report["placement"]["ok"])
        self.assertFalse({"canon_stale", "placement_refused"} & set(report["reasons"]))

    def test_the_lease_states_and_their_reverse(self):
        live = self.state("lease_live")
        self.assertEqual(live["lease"]["owner"], states_mod.FIXTURE_OWNER)
        self.assertTrue(live["lease"]["live"])
        foreign = self.state("lease_foreign")
        self.assertEqual(foreign["lease"]["owner"], states_mod.FOREIGN_OWNER)
        self.assertTrue(foreign["lease"]["live"])
        self.assertGreater(foreign["lease"]["epoch"], live["lease"]["epoch"])
        named = self.state("lease_foreign", owner="exec:other:host:1")
        self.assertEqual(named["lease"]["owner"], "exec:other:host:1")
        expired = self.state("lease_expired")
        self.assertEqual(expired["lease"]["owner"], states_mod.FIXTURE_OWNER)
        self.assertFalse(expired["lease"]["live"])
        self.assertIsNone(self.state("lease_free")["lease"]["owner"])
        with self.assertRaises(fixture_mod.FixtureStateError):
            self.state("lease_expired", ttl=30)

    def test_the_canon_states_and_their_reverse(self):
        stale = self.state("canon_stale")
        self.assertFalse(stale["canon"]["fresh"])
        self.assertIn("canon_stale", stale["reasons"])
        self.assertGreater(stale["canon"]["age_s"], stale["canon"]["max_age_s"])
        fresh = self.state("canon_fresh")
        self.assertTrue(fresh["canon"]["fresh"])
        self.assertNotIn("canon_stale", fresh["reasons"])

    def test_a_stale_canon_does_not_refuse_the_claim(self):
        # The core's claim checks the canon status, not its age (the age
        # gates a new turn): the lease is taken, canon_stale stays a reason.
        self.state("canon_stale")
        live = self.state("lease_live")
        self.assertTrue(live["lease"]["live"])
        self.assertIn("canon_stale", live["reasons"])
        self.state("lease_free")
        self.state("canon_fresh")

    def test_a_canon_state_stays_on_its_agents_own_host(self):
        other = "ima-state-8341-b"
        self.fixture.agent(other)
        self.state("canon_stale")
        self.assertTrue(self.fixture.agent_state(other, "canon_fresh")["canon"]["fresh"])
        self.assertTrue(self.fixture.agent(other)["canon"]["fresh"])
        self.assertEqual(self.registered["placement"]["ref"],
                         "hosts/fixture-host-%s.md" % self.agent)
        # Agents registered on one explicit host share its canon state.
        first = self.fixture.agent("ima-shared-a", host="ima-shared-host")
        self.fixture.agent("ima-shared-b", host="ima-shared-host")
        self.assertTrue(first["canon"]["fresh"])
        self.fixture.agent_state("ima-shared-a", "canon_stale")
        self.assertIn("canon_stale",
                      self.fixture.agent_state("ima-shared-b", "spend_clear")["reasons"])
        self.state("canon_fresh")

    def test_registering_again_releases_an_expired_lease_only(self):
        self.state("lease_expired")
        again = self.fixture.agent(self.agent)
        self.assertIsNone(again["lease"]["owner"])
        self.state("lease_live")
        with self.assertRaises(fixture_mod.FixtureStateError):
            self.fixture.agent(self.agent)
        self.state("lease_free")

    def test_the_placement_states_compose_with_the_claim(self):
        refused = self.state("placement_refused", diagnostic="host refuses deepseek")
        self.assertFalse(refused["placement"]["ok"])
        self.assertEqual(refused["placement"]["diagnostic"], "host refuses deepseek")
        self.assertIn("placement_refused", refused["reasons"])
        # A runner's claim refuses a refused placement: so does the fixture.
        with self.assertRaises(fixture_mod.FixtureStateError) as caught:
            self.state("lease_live")
        self.assertEqual(caught.exception.code, "state")
        admitted = self.state("placement_admitted")
        self.assertTrue(admitted["placement"]["ok"])
        self.assertTrue(self.state("lease_live")["lease"]["live"])
        self.state("lease_free")

    def test_the_spend_marker_states_and_their_reverse(self):
        ambiguous = self.state("spend_ambiguous")
        self.assertTrue(ambiguous["spend"]["marker"])
        self.assertTrue(ambiguous["spend"]["ledger_ambiguous"])
        clear = self.state("spend_clear")
        self.assertFalse(clear["spend"]["marker"])
        self.assertFalse(clear["spend"]["ledger_ambiguous"])

    def test_an_open_reservation_is_settled_with_its_cost(self):
        account = "ima-account-8341"
        opened = self.state("reservation_open", account=account, usd=2, limit_usd=50)
        self.assertEqual(opened["reservation"], {"state": "open", "usd": 2})
        self.assertEqual(opened["budget"]["usd"], 50)
        self.assertEqual(opened["budget"]["reserved"], 2)
        settled = self.state("reservation_settled", account=account, cost_usd=1.5)
        self.assertEqual(settled["spend_key"], opened["spend_key"])
        self.assertEqual(settled["reservation"]["state"], "settled")
        self.assertEqual(settled["budget"]["reserved"], 0)
        self.assertEqual(settled["budget"]["kept"], 0)
        self.assertAlmostEqual(settled["budget"]["settled"], 1.5)
        self.state("spend_clear")

    def test_a_cost_row_is_recorded(self):
        cost = self.state("cost", usd=0.4, input_tokens=10, output_tokens=2)
        self.assertEqual(cost["cost"]["rows"], 1)
        self.assertAlmostEqual(cost["cost"]["recorded_usd"], 0.4)
        # One row per turn key, as in the ledger: a replay is refused.
        with self.assertRaises(fixture_mod.FixtureStateError):
            self.state("cost", spend_key=cost["cost"]["spend_key"], usd=0.1)

    def _account_snapshot(self):
        # Core regression tests may inspect the disposable fixture database.
        # Compare complete rows to catch a cost, settlement or envelope write.
        return {table: self.fixture._db.query("SELECT * FROM " + table + " ORDER BY 1")
                for table in ("account_reservations", "turn_costs", "account_budgets")}

    def test_settlement_refuses_another_agents_key_without_writes(self):
        other = "other-owner-8352"
        self.fixture.agent(other)
        opened = self.state("reservation_open", account="owner-account-8352")
        before = self._account_snapshot()
        for options in ({}, {"account": opened["account"]},
                        {"account": "another-account-8352", "limit_usd": 12}):
            with self.subTest(options=options):
                with self.assertRaises(fixture_mod.FixtureStateError):
                    self.fixture.agent_state(other, "reservation_settled",
                                             spend_key=opened["spend_key"], **options)
                self.assertEqual(self._account_snapshot(), before)
        # The owner can still settle the key without specifying an account.
        settled = self.state("reservation_settled", spend_key=opened["spend_key"])
        self.assertEqual(settled["account"], opened["account"])
        self.assertEqual(settled["reservation"]["state"], "settled")

    def test_settlement_refuses_the_wrong_account_without_writes(self):
        opened = self.state("reservation_open", account="right-account-8352")
        before = self._account_snapshot()
        with self.assertRaises(fixture_mod.FixtureStateError):
            self.state("reservation_settled", spend_key=opened["spend_key"],
                       account="wrong-account-8352", limit_usd=12)
        self.assertEqual(self._account_snapshot(), before)
        settled = self.state("reservation_settled", spend_key=opened["spend_key"],
                             account=opened["account"])
        self.assertEqual(settled["reservation"]["state"], "settled")

    def test_automatic_settlement_selects_only_the_requested_agent_and_account(self):
        other = "other-auto-8352"
        self.fixture.agent(other)
        first = self.state("reservation_open", account="first-auto-8352")
        foreign = self.fixture.agent_state(other, "reservation_open",
                                          account="second-auto-8352")
        foreign_before = self.fixture._db.query(
            "SELECT * FROM account_reservations WHERE spend_key = %s",
            (foreign["spend_key"],))
        settled = self.state("reservation_settled", account="second-auto-8352")
        self.assertEqual(settled["account"], "second-auto-8352")
        self.assertNotIn(settled["spend_key"], (first["spend_key"], foreign["spend_key"]))
        self.assertEqual(settled["reservation"]["state"], "settled")
        self.assertEqual(self.fixture._db.query(
            "SELECT * FROM account_reservations WHERE spend_key = %s",
            (foreign["spend_key"],)), foreign_before)
        self.assertEqual(self.fixture._db.query(
            "SELECT count(*) AS n FROM turn_costs WHERE spend_key = ANY(%s)",
            ([first["spend_key"], foreign["spend_key"]],))[0]["n"], 0)

    def test_an_unregistered_agent_is_a_state_error_without_secret(self):
        with self.assertRaises(fixture_mod.FixtureStateError) as caught:
            self.fixture.agent_state("ima-never-registered", "lease_live")
        self.assertIn("agent()", str(caught.exception))
        self.assertNotIn(self.fixture.dsn, str(caught.exception))

    def test_the_consumer_database_is_never_written(self):
        self.state("lease_live")
        self.state("cost")
        has_registry = psql("SELECT to_regclass('public.agent_registry') IS NOT NULL")
        if has_registry == "t":
            self.assertEqual(psql("SELECT count(*) FROM agent_registry WHERE name = '%s'"
                                  % self.agent), "0")
            self.assertEqual(psql("SELECT count(*) FROM turn_costs WHERE agent = '%s'"
                                  % self.agent), "0")
        self.state("lease_free")


if __name__ == "__main__":
    unittest.main()
