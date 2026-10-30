# SPDX-License-Identifier: AGPL-3.0-only
"""TPK #2167 / lot #4874 (B2) — `ameesh turn observe` and the quiescent allowlist.

The states themselves are covered at the shared builder (`TurnObservationTest`
in `test_l84_serve.py`, lot #4831). These tests hold what B2 adds: the command
and its exit codes, the same document as the routes, the allowlist as a
contract, and the fake-harness path mail → turn → context → observation.
"""
from __future__ import annotations

import json
import os
import shlex
import unittest
import uuid
from unittest import mock

from ameesh import mail, registry, serve, storage, turn_observation, turn_observe
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase
from .test_l159a_capacity_guard import errors as schema_errors

SCHEMA = serve.SCHEMAS[turn_observation.SCHEMA]


class QuiescentAllowlistTest(unittest.TestCase):
    """The allowlist is the contract: only these states may ever count."""

    def test_the_allowlist_is_exact(self):
        self.assertEqual(turn_observation.QUIESCENT_ACCOUNTING,
                         {"settled", "released", "kept", "none"})
        self.assertLessEqual(turn_observation.QUIESCENT_ACCOUNTING,
                             set(turn_observation.ACCOUNTING_STATES))

    def test_only_an_idle_agent_without_a_turn_is_quiescent(self):
        for state in turn_observation.QUIESCENT_ACCOUNTING:
            self.assertTrue(turn_observation.is_quiescent("idle", state, False), state)
            self.assertFalse(turn_observation.is_quiescent("idle", state, True), state)
            for status in ("queued", "running", "blocked", "stopped", "dead", "unknown"):
                self.assertFalse(turn_observation.is_quiescent(status, state, False), status)

    def test_a_state_outside_the_allowlist_never_counts(self):
        for state in ("pending", "unknown", "future-state", "", "SETTLED", None):
            self.assertFalse(turn_observation.is_quiescent("idle", state, False), state)


class TurnObserveTest(PgTestCase):
    """The command over the real database, in its own read-only session."""

    def setUp(self):
        super().setUp()
        for table in ("turn_resources", "turn_costs", "spend_pending",
                      "account_reservations"):
            self.db.execute("DELETE FROM " + table)

    def agent(self, name):
        registry.upsert(self.db, name, chantier="demo", harness="deepseek", host="h1")
        return name

    def done_turn(self, agent):
        turn_id = uuid.uuid4().hex
        resources = storage.of(self.db).turn_resources
        resources.open_turn(turn_id, agent, "h1", pgid=None, label="tour")
        resources.close_turn(turn_id, orphan=False)
        return turn_id

    def observed(self, *args):
        result = self.mesh("turn", "observe", *args, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(schema_errors(SCHEMA, document), [])
        return document

    def test_by_turn_is_the_document_of_the_route(self):
        agent = self.agent("obs-turn")
        turn_id = self.done_turn(agent)
        document = self.observed("--turn", turn_id)
        self.assertEqual(document["schema"], "ameesh-turn-observation/1")
        self.assertEqual(document["by"], {"turn": turn_id})
        self.assertIsNone(document["request"])
        self.assertEqual(document["turn"]["state"], "ended")
        self.assertTrue(document["quiescent"])
        route = serve._turn(None, self.db, {"turn": turn_id}, {})
        for stamped in (document, route):
            stamped.pop("observed_ts")
        self.assertEqual(document, route)

    def test_by_request_is_metadata_only(self):
        agent = self.agent("obs-request")
        message = mail.send(self.db, "human:owner", agent, "corps-jamais-observe",
                            kind="request", payload={"cle": "charge-jamais-observee"})
        result = self.mesh("turn", "observe", "--request", str(message), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("jamais-observe", result.stdout)
        document = json.loads(result.stdout)
        self.assertEqual(document["by"], {"request": message})
        self.assertEqual(document["request"]["state"], "pending")
        self.assertIsNone(document["turn"])
        route = serve._request(None, self.db, {"request": str(message)}, {})
        for stamped in (document, route):
            stamped.pop("observed_ts")
        self.assertEqual(document, route)

    def test_stale_marker_with_a_ledger_row_is_settled(self):
        agent = self.agent("obs-stale")
        marker = registry.pending_spend_begin(
            self.db, agent, 1, "tour", "deepseek-flash", turn_id=uuid.uuid4().hex)
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, turn, model, usd, turn_id,"
            " spend_key, started_at, recorded_at, source)"
            " VALUES (%s, 'deepseek', 'tour', 'deepseek-flash', 0.5, %s, %s,"
            " now() - interval '1 hour', now(), 'harness')",
            (agent, marker["turn_id"], marker["spend_key"]))
        document = self.observed("--turn", marker["turn_id"])
        self.assertEqual(document["accounting"]["state"], "settled")
        self.assertIsNotNone(document["accounting"]["marker"])
        self.assertTrue(document["quiescent"])

    def test_a_reservation_alone_is_unknown_and_not_quiescent(self):
        agent = self.agent("obs-reservation")
        turn_id = self.done_turn(agent)
        self.db.execute(
            "INSERT INTO account_reservations (billing_account, agent, usd,"
            " period, state, spend_key) VALUES ('acct', %s, 2.0, current_date,"
            " 'open', %s)", (agent, "%s:1:clef" % agent))
        document = self.observed("--turn", turn_id)
        self.assertEqual(document["accounting"]["state"], "unknown")
        self.assertFalse(document["quiescent"])

    def test_a_future_accounting_state_is_not_quiescent(self):
        agent = self.agent("obs-future")
        turn_id = self.done_turn(agent)
        future = {"state": "future-state", "spend_key": None, "marker": None,
                  "reservation": None, "ledger": None}
        with mock.patch.object(turn_observation, "_accounting", return_value=future):
            document = turn_observation.observe(self.db, turn=turn_id)
        self.assertEqual(document["accounting"]["state"], "future-state")
        self.assertEqual(document["agent"]["status"], "idle")
        self.assertFalse(document["quiescent"])

    def test_an_unknown_target_is_exit_3_and_prints_no_document(self):
        for args in (("--turn", uuid.uuid4().hex), ("--request", "999999999")):
            result = self.mesh("turn", "observe", *args, "--json")
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("unknown", result.stderr)

    def test_a_usage_error_is_exit_2(self):
        turn_id = uuid.uuid4().hex
        for args in ((), ("--turn", "pas-un-tour"), ("--request", "abc"),
                     ("--turn", turn_id, "--request", "1")):
            result = self.mesh("turn", "observe", *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual(result.stdout, "", args)
        self.assertEqual(self.mesh("turn").returncode, 2)

    def test_the_identifier_forms_are_those_of_the_routes(self):
        self.assertEqual(turn_observe._TURN_RE.pattern, serve._TURN_RE.pattern)
        self.assertEqual(turn_observe._MAIL_RE.pattern, serve._MAIL_RE.pattern)

    def test_the_session_is_read_only(self):
        seen = []

        def open_session(cfg):
            seen.append(cfg.read_only)
            return mock.Mock()

        args = mock.Mock(turn=uuid.uuid4().hex, request=None, json=True)
        with mock.patch("ameesh.mesh_cli._open", open_session), \
                mock.patch.object(turn_observation, "observe",
                                  side_effect=turn_observation.Unknown("unknown turn")):
            self.assertEqual(turn_observe.cmd_observe(self.cfg, args), 3)
        self.assertEqual(seen, [True])

    def test_the_text_output_names_the_states(self):
        agent = self.agent("obs-text")
        turn_id = self.done_turn(agent)
        result = self.mesh("turn", "observe", "--turn", turn_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("%s: ended" % turn_id, result.stdout)
        self.assertIn("accounting none", result.stdout)
        self.assertIn("quiescent yes", result.stdout)


class ObservationEndToEndTest(PgTestCase):
    """A real worker and the fake harness: mail → turn → context → observation."""

    def test_the_turn_handed_to_the_harness_is_the_one_observed(self):
        name = "obs-e2e"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        capture = os.path.join(self.tmp, "obs-e2e.json")
        runner = Runner(self.cfg, self.db, once=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        message = mail.send(self.db, "orch", name, "corps-jamais-observe", kind="request")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_RUN": 'cp "$AMEESH_TURN_CONTEXT" %s' % shlex.quote(capture)}
        with mock.patch.dict(os.environ, env, clear=False):
            spec = worker.pick()
            self.assertTrue(worker.run_turn(spec))
        with open(capture, encoding="utf-8") as handle:
            context = json.load(handle)
        self.assertEqual([row["id"] for row in context["requests"]], [message])

        result = self.mesh("turn", "observe", "--request", str(message), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("jamais-observe", result.stdout)
        document = json.loads(result.stdout)
        self.assertEqual(schema_errors(SCHEMA, document), [])
        self.assertEqual(document["request"]["state"], "delivered")
        self.assertEqual(document["request"]["delivered_turn_id"], context["turn"]["id"])
        self.assertEqual(document["turn"]["id"], context["turn"]["id"])
        self.assertEqual(document["turn"]["state"], "ended")
        self.assertEqual(document["agent"]["name"], name)
        self.assertEqual(document["accounting"]["state"], "settled")
        self.assertEqual(document["agent"]["status"], "idle")
        self.assertFalse(document["agent"]["turn_in_progress"])
        self.assertTrue(document["quiescent"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
