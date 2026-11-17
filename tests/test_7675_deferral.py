# SPDX-License-Identifier: AGPL-3.0-only
"""E3b (#7675): the bounded structured deferral block, public surfaces.

The pure cases pin the closed reason mapping (the runner's persisted status
texts are the only source); the database cases hold the same block in
`ameesh show --json`, `ameesh list --json` and `ameesh-turn-observation/1`.
No case reads a prompt, a mail body or a payload.
"""
from __future__ import annotations

import datetime
import json
import unittest

from ameesh import (accounts, auth_failure, budget, deferral_observation, mail,
                    mesh_cli, provider_credits, registry, serve, turn_observation)
from ameesh.runner import AgentWorker

from .support import PgTestCase
from .test_l159a_capacity_guard import errors as schema_errors


def row(status: str, text: str = "", **extra) -> dict:
    return dict(status=status, status_text=text, **extra)


class MappingTest(unittest.TestCase):
    def reason(self, status: str, text: str = "", **extra):
        block = deferral_observation.classify(row(status, text, **extra))
        self.assertIsNotNone(block, (status, text))
        self.assertEqual(block["schema"], "ameesh-deferral/1")
        self.assertEqual(block["state"], "deferred")
        self.assertIn(block["reason"], deferral_observation.REASONS)
        return block["reason"]

    def test_every_blocked_reason_has_its_code(self):
        cases = (
            (auth_failure.STATUS_PREFIX + " codex/p1", "authentication"),
            (provider_credits.STATUS_PREFIX + " codex/p1", "provider_credits"),
            (budget.PAUSE_PREFIX + "hourly budget reached", "budget"),
            (accounts.CONFIG_BLOCK_PREFIX + "no valid account", "account"),
            ("account window: short-window cooldown until 12:00", "account_window"),
            (AgentWorker.DEFERRED_STATUS + " on h1", "session"),
            (AgentWorker.DB_STATUS + " : connection refused", "database"),
            (AgentWorker.HOST_STATUS + " : harness missing", "host"),
            (AgentWorker.WORKDIR_STATUS + " : /missing", "workdir"),
            ("turn context unavailable", "other"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.reason("blocked", text), expected)

    def test_the_mapping_uses_the_runner_constants(self):
        # duplicated strings (importing `runner` here would drag the executor
        # into every observation): the contract test keeps them honest.
        self.assertEqual(deferral_observation.SESSION_STATUS,
                         AgentWorker.DEFERRED_STATUS)
        self.assertEqual(deferral_observation.DATABASE_STATUS,
                         AgentWorker.DB_STATUS)
        self.assertEqual(deferral_observation.HOST_STATUS,
                         AgentWorker.HOST_STATUS)
        self.assertEqual(deferral_observation.WORKDIR_STATUS,
                         AgentWorker.WORKDIR_STATUS)

    def test_pressure_reuses_the_public_queue_object(self):
        text = ("waiting: pressure on host h1 (own cgroup) — memory pressure "
                "(PSI some 60 s, own cgroup); queued 42 s; capacity requested; "
                "retry in 10 s")
        block = deferral_observation.classify(
            row("queued", text), now=1000.0,
            since_ts=940.0)
        self.assertEqual(block["reason"], "pressure")
        self.assertEqual(block["age_s"], 60)
        self.assertEqual(block["since_ts"], 940.0)
        self.assertEqual(block["queue"]["state"], "queued")
        self.assertEqual(block["queue"]["reason"], "pressure")
        self.assertEqual(block["queue"]["host"], "h1")
        self.assertEqual(block["queue"]["scope"], "own_cgroup")
        self.assertEqual(block["queue"]["queued_s"], 42)
        self.assertEqual(block["queue"]["retry_s"], 10)
        self.assertEqual(block["queue"]["capacity"], "requested")
        self.assertFalse(block["queue"]["critical"])

    def test_critical_pressure_is_the_same_reason(self):
        text = ("paused: critical pressure on host h1 (host) — 1-min load; "
                "retry in 10 s")
        self.assertEqual(self.reason("blocked", text), "pressure")

    def test_queued_shutdown_and_restart_are_named(self):
        self.assertEqual(self.reason(
            "queued", "runner shutting down: instruction put back in the queue"),
            "shutdown")
        self.assertEqual(self.reason(
            "queued", "outdated runner: instruction put back in the queue"),
            "restart")

    def test_a_normal_state_is_not_a_deferral(self):
        for status in ("idle", "running", "stopped", "dead", "queued"):
            with self.subTest(status=status):
                self.assertIsNone(deferral_observation.classify(
                    row(status, "available: nothing due")))
        self.assertIsNone(deferral_observation.classify(None))
        self.assertIsNone(deferral_observation.classify({}))

    def test_operator_pause_takes_precedence(self):
        block = deferral_observation.classify(
            row("blocked", auth_failure.STATUS_PREFIX + " codex/p1",
                operator_paused=True, operator_pause_ts=900.0), now=1000.0)
        self.assertEqual(block["reason"], "operator")
        self.assertEqual(block["age_s"], 100)
        # an `after_turn` pause on a running agent is not a deferral yet
        self.assertIsNone(deferral_observation.classify(
            row("running", "turn in progress", operator_paused=True,
                operator_pause_ts=900.0), now=1000.0))

    def test_the_reason_list_is_closed(self):
        self.assertEqual(deferral_observation.STATES, ("deferred",))
        self.assertEqual(len(set(deferral_observation.REASONS)),
                         len(deferral_observation.REASONS))
        self.assertIn("other", deferral_observation.REASONS)
        self.assertEqual(deferral_observation.REASONS[-1], "other")

    def test_since_prefers_the_reason_start_and_accepts_a_datetime(self):
        block = deferral_observation.classify(
            row("blocked", AgentWorker.DEFERRED_STATUS,
                status_since_ts=100.0, status_reason_since_ts=120.0),
            now=200.0)
        self.assertEqual(block["since_ts"], 120.0)
        self.assertEqual(block["age_s"], 80)
        when = datetime.datetime.fromtimestamp(150.0, tz=datetime.timezone.utc)
        block = deferral_observation.classify(
            row("blocked", AgentWorker.DEFERRED_STATUS, status_since=when),
            now=200.0)
        self.assertEqual(block["since_ts"], 150.0)
        self.assertEqual(block["age_s"], 50)

    def test_no_free_text_is_copied(self):
        text = (auth_failure.STATUS_PREFIX + " codex/p1 secret-token-42 "
                "corps-interdit")
        block = deferral_observation.classify(row("blocked", text))
        self.assertNotIn("secret-token-42", json.dumps(block))
        self.assertNotIn("corps-interdit", json.dumps(block))
        self.assertNotIn("detail", block)

    def test_the_block_validates_against_the_public_schema(self):
        schema = serve.SCHEMAS["ameesh-turn-observation/1"]
        base = {
            "schema": "ameesh-turn-observation/1", "observed_ts": 1.0,
            "by": {"turn": "0" * 32, "request": None},
            "request": None,
            "turn": None,
            "agent": {"name": "a", "status": "blocked",
                      "status_since_ts": None, "status_text": "",
                      "harness": None, "model": None, "session_id": None,
                      "turns": 0, "budget_usd": None, "spent_usd": None,
                      "turn_in_progress": False, "last_turn_ts": None},
            "lease": {"owner": None, "epoch": None, "live": False,
                      "expires_ts": None},
            "accounting": {"state": "none", "spend_key": None, "marker": None,
                           "reservation": None, "ledger": None},
            "deferral": deferral_observation.classify(
                row("blocked", AgentWorker.WORKDIR_STATUS)),
            "quiescent": False,
        }
        self.assertEqual(schema_errors(schema, base), [])
        for reason in deferral_observation.REASONS:
            with self.subTest(reason=reason):
                base["deferral"]["reason"] = reason
                self.assertEqual(schema_errors(schema, base), [])


class PublicViewsTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "defer-a", chantier="demo", harness="deepseek",
                        host="h1")

    def block(self, text: str, status: str = "blocked") -> dict:
        registry.set_status(self.db, "defer-a", status, status_text=text)
        document = mesh_cli.agent_detail_json(self.db, "defer-a")
        return document["deferral"]

    def test_show_json_carries_the_block(self):
        block = self.block(AgentWorker.HOST_STATUS + " : harness missing")
        self.assertEqual(block["schema"], "ameesh-deferral/1")
        self.assertEqual(block["reason"], "host")
        result = self.mesh("show", "defer-a", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)["deferral"]
        # `age_s` est l'âge au moment de la lecture : il peut avoir pris une
        # seconde entre le bloc calculé en direct et la commande.
        self.assertIsInstance(document["age_s"], int)
        self.assertGreaterEqual(document["age_s"], block["age_s"])
        self.assertEqual(
            {k: v for k, v in document.items() if k != "age_s"},
            {k: v for k, v in block.items() if k != "age_s"})

    def test_list_json_carries_the_same_block(self):
        block = self.block(auth_failure.STATUS_PREFIX + " codex/p1")
        rows = mesh_cli.agent_rows(self.cfg, self.db)
        row_ = next(r for r in rows if r["name"] == "defer-a")
        self.assertEqual(row_["deferral"]["reason"], "authentication")
        self.assertEqual(row_["deferral"]["schema"], block["schema"])
        self.assertEqual(row_["deferral"]["since_ts"], block["since_ts"])

    def test_turn_observation_carries_it_and_keeps_accounting_authoritative(self):
        self.db.execute(
            "INSERT INTO account_reservations (billing_account, agent, usd,"
            " period, state, spend_key) VALUES ('acct', %s, 2.0, current_date,"
            " 'open', %s)", ("defer-a", "defer-a:1:clef"))
        registry.set_status(self.db, "defer-a", "blocked",
                            status_text="turn context unavailable")
        message = mail.send(self.db, "human:owner", "defer-a", "corps-interdit",
                            kind="request", payload={"secret": "charge-interdite"})
        document = turn_observation.observe(self.db, request=message)
        self.assertEqual(document["deferral"]["reason"], "other")
        self.assertEqual(document["accounting"]["state"], "unknown")
        self.assertFalse(document["quiescent"])
        text = json.dumps(document)
        self.assertNotIn("corps-interdit", text)
        self.assertNotIn("charge-interdite", text)
        self.assertNotIn("payload", text)
        self.assertEqual(
            schema_errors(serve.SCHEMAS[turn_observation.SCHEMA], document), [])

    def test_a_running_agent_has_no_block(self):
        registry.set_status(self.db, "defer-a", "running",
                            status_text="turn in progress")
        self.assertIsNone(mesh_cli.agent_detail_json(self.db, "defer-a")["deferral"])
