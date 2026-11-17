# SPDX-License-Identifier: AGPL-3.0-only
"""#7670 (E1b): `ameesh agent observe` and the read-only observation contract.

The document `ameesh-agent-observation/1` is a closed projection of the
admitted profile, the canon/placement references, the lease, the execution
bounds and the redacted provider budget. It is built inside one repeatable
read, read-only transaction: no claim, reservation, settlement, dispatch or
canon synchronization, and no prompt, path or credential.

`test_l84_serve` covers the HTTP route, its schema in the committed OpenAPI
document and the CLI sharing one serialization; here the states are exercised
at the builder both surfaces use.
"""
from __future__ import annotations

import dataclasses
import json
import unittest
from unittest import mock

from ameesh import agent_observation, db as db_mod, serve, storage

from .support import PgTestCase
from .test_l159a_capacity_guard import errors as schema_errors

HOST = "h1"
SHA = "a" * 40
OTHER_SHA = "b" * 40
CANON_REF = "membre:agents/observateur@" + SHA


class TransactionModesTest(unittest.TestCase):
    """`db.transaction(isolation=…, read_only=…)`: closed modes, no SQL text."""

    def test_modes_are_a_closed_list(self):
        self.assertEqual(db_mod.transaction_modes(), "")
        self.assertEqual(
            db_mod.transaction_modes(isolation="repeatable read", read_only=True),
            "ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        self.assertEqual(db_mod.transaction_modes(read_only=True), "READ ONLY")
        for bad in ("REPEATABLE READ; DROP TABLE agent_registry", "SNAPSHOT", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                db_mod.transaction_modes(isolation=bad)


class AgentObservationTest(PgTestCase):

    def setUp(self):
        super().setUp()
        for table in ("host_release_fences", "host_release_operations",
                      "host_lifecycle", "canon_state", "account_reservations",
                      "account_budgets", "turn_costs", "spend_pending",
                      "budget_events", "agent_name_resolutions",
                      "sandbox_turns", "turn_resources"):
            self.db.execute("DELETE FROM " + table)

    # -- fixtures ---------------------------------------------------------

    def agent(self, name="obs", *, alias=None, **columns):
        values = {"name": name, "chantier": "t", "host": HOST,
                  "harness": "deepseek", "model": "deepseek-v4",
                  "provider": "deepseek", "credential_mode": "env",
                  "responsible": "human:owner", "status": "idle"}
        values.update(columns)
        if alias is not None:
            values["aliases"] = [alias]
        keys = list(values)
        self.db.execute(
            "INSERT INTO agent_registry (%s) VALUES (%s)"
            % (", ".join(keys), ", ".join(["%s"] * len(keys))),
            tuple(values[key] for key in keys))
        return name

    def governed(self, name="obs", *, sha=SHA, state_sha=None, status="ok",
                 age_s=0, **columns):
        """An agent from the canon, with an admitted placement by default."""
        self.agent(name, canon_ref=CANON_REF.rsplit("@", 1)[0] + "@" + sha,
                   placement_ok=True, **columns)
        self.place(name)
        self.canon_state(state_sha or sha, status=status, age_s=age_s)
        return name

    def place(self, name="obs"):
        self.db.execute(
            "UPDATE agent_registry SET placement_profile = "
            "ameesh_placement_profile(host, harness, provider, model, "
            "credential_mode) WHERE name = %s", (name,))

    def canon_state(self, sha=SHA, *, status="ok", age_s=0):
        self.db.execute(
            "INSERT INTO canon_state (host, canon, status, last_good_commit,"
            " last_good_at, checked_at)"
            " VALUES (%s, '', %s, %s, now(), now() - make_interval(secs => %s))"
            " ON CONFLICT (host, canon) DO UPDATE SET status = excluded.status,"
            " last_good_commit = excluded.last_good_commit,"
            " checked_at = excluded.checked_at", (HOST, status, sha, age_s))

    def observe(self, name="obs", *, profile=None, **cfg):
        base = {"host": HOST}
        base.update(cfg)
        return agent_observation.observe(
            self.db, name, cfg=dataclasses.replace(self.cfg, **base),
            account_profile=profile)

    def profiles(self, *items, harness="deepseek"):
        return {"accounts": {harness: list(items)}}

    # -- the document -----------------------------------------------------

    def test_schema_of_a_complete_document(self):
        self.agent("obs", capabilities=["read", "report-drift"],
                   session_id="s-1", session_origin="s-1",
                   session_harness="deepseek", session_account="p1",
                   context_max_tokens=100000)
        self.db.execute(
            "INSERT INTO spend_pending (agent, start_index, turn, model,"
            " turn_id, work_item_id) VALUES ('obs', 3, 'tour', 'deepseek-v4',"
            " 'turn-1', 42)")
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner', lease_epoch = 7,"
            " lease_expires_at = now() + interval '1 hour', lease_since = now()")
        storage.of(self.db).budgets.account_put("acct-a", 20.0, actor="human:t")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, billing_account)"
            " VALUES ('obs', 'deepseek', 'deepseek-v4', 1.25, 'acct-a')")
        document = self.observe(
            **self.profiles({"name": "p1", "billing_account": "acct-a",
                             "reserve_usd": 2.0}))
        # HTTP serialises; validate what a client actually receives.
        document = json.loads(json.dumps(document))
        self.assertEqual(schema_errors(
            serve.SCHEMAS["ameesh-agent-observation/1"], document), [])
        self.assertEqual(document["schema"], "ameesh-agent-observation/1")
        self.assertEqual(document["agent"]["name"], "obs")
        self.assertEqual(document["profile"]["capabilities"],
                         ["read", "report-drift"])
        self.assertEqual(document["profile"]["session"],
                         {"id": "s-1", "harness": "deepseek", "account": "p1"})
        self.assertTrue(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"], [])
        self.assertEqual(document["limits"]["declared"]["context_max_tokens"],
                         100000)
        self.assertTrue(document["limits"]["available"])
        self.assertTrue(document["budget"]["available"])
        self.assertAlmostEqual(document["budget"]["settled_usd"], 1.25)
        self.assertAlmostEqual(document["budget"]["remaining_usd"], 18.75)
        self.assertEqual(document["native"]["turn"]["turn_id"], "turn-1")
        self.assertEqual(document["native"]["turn"]["work_item_id"], "42")
        self.assertEqual(document["missing"], [])

    def test_unknown_agent_is_exit_three_and_http_404(self):
        with self.assertRaises(agent_observation.Unknown):
            self.observe("nobody")

        out = self.mesh("agent", "observe", "nobody", "--json")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(out.stdout, "")
        self.assertIn("unknown agent", out.stderr)

        ctx = serve.Context(cfg=dataclasses.replace(self.cfg, host=HOST),
                            human="human:owner")
        with self.assertRaises(serve.ApiError) as caught:
            serve._agent_observation(ctx, self.db, {"agent": "nobody"}, {})
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(caught.exception.code, "not_found")

    def test_cli_and_http_use_the_builder(self):
        self.agent("obs")
        route = next(route for route in serve.ROUTES
                     if route.path == "/api/v1/agents/{agent}/observation")
        self.assertEqual(route.schema, "ameesh-agent-observation/1")
        self.assertEqual([query[0] for query in route.query], ["account_profile"])

        ctx = serve.Context(cfg=dataclasses.replace(self.cfg, host=HOST),
                            human="human:owner")
        served = serve._agent_observation(ctx, self.db, {"agent": "obs"}, {})
        out = self.mesh("agent", "observe", "obs", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        cli = json.loads(out.stdout)
        self.assertEqual(cli["schema"], served["schema"])
        self.assertEqual(cli["agent"], served["agent"])
        self.assertEqual(cli["admission"]["admitted"],
                         served["admission"]["admitted"])
        text = self.mesh("agent", "observe", "obs")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("admission", text.stdout)

        for argv in (("agent", "observe", "bad name"),
                     ("agent", "observe", "obs", "--account-profile", "bad name")):
            out = self.mesh(*argv)
            self.assertEqual(out.returncode, 2, (argv, out.stderr))

        with self.assertRaises(serve.ApiError) as caught:
            serve._agent_observation(ctx, self.db, {"agent": "obs"},
                                     {"account_profile": ["one", "two"]})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (400, "bad_query"))

    def test_closed_projection_never_exposes_prompts_paths_or_keys(self):
        self.agent(
            "obs", cwd="/srv/CANARY-CWD", session_cwd="/srv/CANARY-SESSION-CWD",
            pending_prompt="CANARY-PENDING", current_prompt="CANARY-CURRENT",
            last_error="CANARY-ERROR",
            operator_pause_reason="CANARY-PAUSE", election_brief="CANARY-BRIEF",
            public_key="CANARY-PUBLIC-KEY")
        for profile in (
                {"type": "api_key_env", "name": "p1", "env": "CANARY-ENV",
                 "key_env": "CANARY-KEY-ENV", "billing_account": "acct-a"},
                {"type": "api_key_env", "name": "p1", "env": "CANARY-ENV",
                 "key_file": "/keys/CANARY-KEY-FILE",
                 "billing_account": "acct-a"}):
            with self.subTest(profile=profile):
                document = self.observe("obs", **self.profiles(profile))
                raw = json.dumps(document)
                for canary in ("CANARY-CWD", "CANARY-SESSION-CWD",
                               "CANARY-PENDING", "CANARY-CURRENT",
                               "CANARY-ERROR", "CANARY-PAUSE",
                               "CANARY-BRIEF", "CANARY-PUBLIC-KEY",
                               "CANARY-KEY-ENV", "CANARY-KEY-FILE",
                               "CANARY"):
                    self.assertNotIn(canary, raw)
        self.assertEqual(set(document), {
            "schema", "observed_ts", "agent", "profile", "admission", "canon",
            "placement", "lease", "limits", "budget", "native", "missing"})
        # the fields that could carry a prompt or a path are not selected
        for key in ("pending_prompt", "current_prompt", "cwd", "session_cwd",
                    "last_error", "operator_pause_reason", "election_brief",
                    "public_key", "key_env", "key_file"):
            self.assertNotIn(key, document["agent"])
            self.assertNotIn(key, document["profile"])

    def test_no_write_no_alias_resolution_no_reservation(self):
        self.agent("obs", alias="obs-alias")
        storage.of(self.db).budgets.account_put("acct-a", 50.0, actor="human:t")
        self.db.execute(
            "INSERT INTO account_reservations (billing_account, agent, usd,"
            " period, spend_key) VALUES ('acct-a', 'obs', 1.5,"
            " (now() at time zone 'Europe/Paris')::date, 'k-open')")
        self.db.execute(
            "INSERT INTO spend_pending (agent, start_index, turn)"
            " VALUES ('obs', 1, 'tour')")

        def counts():
            return {table: self.db.query("SELECT count(*) AS n FROM %s" % table)[0]["n"]
                    for table in ("agent_name_resolutions", "account_reservations",
                                  "turn_costs", "spend_pending")}

        before = counts()
        row_before = self.db.query(
            "SELECT to_jsonb(r) AS row FROM agent_registry r WHERE name = 'obs'")[0]["row"]
        document = self.observe("obs-alias", **self.profiles(
            {"name": "p1", "billing_account": "acct-a"}))
        self.assertEqual(document["agent"]["name"], "obs")
        self.assertEqual(counts(), before)
        row_after = self.db.query(
            "SELECT to_jsonb(r) AS row FROM agent_registry r WHERE name = 'obs'")[0]["row"]
        self.assertEqual(row_before, row_after)
        reservation = self.db.query(
            "SELECT state FROM account_reservations WHERE spend_key = 'k-open'")[0]
        self.assertEqual(reservation["state"], "open")

    def test_one_snapshot_for_the_whole_document(self):
        """A concurrent commit after the first read is not half-observed."""
        self.agent("obs")
        self.db.execute(
            "UPDATE agent_registry SET lease_owner = 'runner', lease_epoch = 7,"
            " lease_expires_at = now() + interval '1 hour' WHERE name = 'obs'")
        storage.of(self.db).budgets.account_put("acct-a", 50.0, actor="human:t")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, billing_account)"
            " VALUES ('obs', 'deepseek', 'deepseek-v4', 1.0, 'acct-a')")
        original = agent_observation._budget_block

        def hook(db, cfg, row, requested):
            other = self.connect()
            try:
                # committed while the observation transaction is open
                other.execute(
                    "INSERT INTO turn_costs (agent, harness, model, usd,"
                    " billing_account) VALUES ('obs', 'deepseek',"
                    " 'deepseek-v4', 5.0, 'acct-a')")
                other.execute(
                    "UPDATE agent_registry SET status = 'running',"
                    " lease_owner = 'thief', lease_epoch = 99,"
                    " lease_expires_at = now() + interval '1 hour'"
                    " WHERE name = 'obs'")
            finally:
                other.close()
            return original(db, cfg, row, requested)

        with mock.patch.object(agent_observation, "_budget_block", hook):
            document = self.observe(**self.profiles(
                {"name": "p1", "billing_account": "acct-a"}))
        self.assertEqual(document["agent"]["status"], "idle")
        self.assertEqual(document["lease"]["owner"], "runner")
        self.assertEqual(document["lease"]["epoch"], 7)
        self.assertAlmostEqual(document["budget"]["settled_usd"], 1.0)
        # the concurrent commit is visible outside the snapshot
        row = self.db.query(
            "SELECT status, lease_owner FROM agent_registry WHERE name = 'obs'")[0]
        self.assertEqual((row["status"], row["lease_owner"]), ("running", "thief"))
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM turn_costs WHERE usd = 5.0")[0]["n"], 1)

    # -- admission states -------------------------------------------------

    def test_manual_agent_is_admitted_without_canon(self):
        self.agent("obs")
        document = self.observe("obs", host="another-host")
        admission = document["admission"]
        self.assertFalse(admission["governed"])
        self.assertIsNone(document["canon"])
        self.assertTrue(admission["claim_predicate"])
        self.assertTrue(admission["admitted"])
        self.assertEqual(admission["reasons"], [])
        # no declared account, and another observer host: both unavailable
        self.assertEqual(document["missing"],
                         ["account_envelope", "limits"])

    def test_governed_agent_is_admitted_only_when_fresh_and_matching(self):
        self.governed("obs")
        document = self.observe("obs", require_responsible=True)
        self.assertEqual(document["canon"]["state"], "ok")
        self.assertTrue(document["canon"]["fresh"])
        self.assertEqual(document["canon"]["ref_commit"], SHA)
        self.assertTrue(document["canon"]["ref_matches"])
        self.assertTrue(document["admission"]["governed"])
        self.assertTrue(document["admission"]["claim_predicate"])
        self.assertTrue(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"], [])
        self.assertTrue(document["placement"]["profile_ok"])

    def test_stale_canon_keeps_the_claim_predicate_but_refuses_admission(self):
        self.governed("obs", age_s=agent_observation.CANON_MAX_AGE_S + 5)
        document = self.observe("obs")
        self.assertFalse(document["canon"]["fresh"])
        self.assertGreater(document["canon"]["age_s"],
                           agent_observation.CANON_MAX_AGE_S)
        self.assertTrue(document["admission"]["claim_predicate"])
        self.assertFalse(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"], ["canon_stale"])
        self.assertNotIn("missing", document["missing"])

    def test_invalid_canon_refuses_the_claim(self):
        self.governed("obs", status="invalid")
        document = self.observe("obs")
        self.assertFalse(document["canon"]["fresh"])
        self.assertFalse(document["admission"]["claim_predicate"])
        self.assertFalse(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"],
                         ["canon_state_not_ok"])

    def test_missing_canon_state_is_reported_unavailable(self):
        self.agent("obs", canon_ref=CANON_REF, placement_ok=True)
        self.place("obs")
        document = self.observe("obs")
        self.assertIsNone(document["canon"]["state"])
        self.assertFalse(document["admission"]["claim_predicate"])
        self.assertFalse(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"],
                         ["canon_state_missing"])
        self.assertIn("canon_state", document["missing"])

    def test_ref_mismatch_refuses_admission(self):
        self.governed("obs", state_sha=OTHER_SHA)
        document = self.observe("obs")
        self.assertEqual(document["canon"]["ref_commit"], SHA)
        self.assertEqual(document["canon"]["last_good_commit"], OTHER_SHA)
        self.assertFalse(document["canon"]["ref_matches"])
        self.assertTrue(document["admission"]["claim_predicate"])
        self.assertFalse(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"],
                         ["canon_ref_mismatch"])

    def test_refused_placement(self):
        self.governed("obs")
        self.db.execute(
            "UPDATE agent_registry SET placement_ok = false WHERE name = 'obs'")
        document = self.observe("obs")
        self.assertFalse(document["admission"]["claim_predicate"])
        self.assertEqual(document["admission"]["reasons"],
                         ["placement_refused"])

    def test_diverged_placement_profile(self):
        self.governed("obs")
        self.db.execute(
            "UPDATE agent_registry SET placement_profile = 'host=old'"
            " WHERE name = 'obs'")
        document = self.observe("obs")
        self.assertFalse(document["admission"]["claim_predicate"])
        self.assertFalse(document["placement"]["profile_ok"])
        self.assertEqual(document["admission"]["reasons"],
                         ["placement_profile_diverged"])

    def test_missing_placement_is_reported(self):
        self.agent("obs", canon_ref=CANON_REF)
        self.canon_state()
        document = self.observe("obs")
        self.assertIsNone(document["placement"]["ok"])
        self.assertFalse(document["admission"]["claim_predicate"])
        self.assertEqual(document["admission"]["reasons"],
                         ["placement_missing"])

    def test_retired_external_and_expired_ephemeral(self):
        self.agent("obs")
        self.db.execute(
            "UPDATE agent_registry SET retired_at = now(), retired_by ="
            " 'human:owner', retired_reason = 'test' WHERE name = 'obs'")
        document = self.observe("obs")
        self.assertTrue(document["agent"]["retired"])
        self.assertFalse(document["admission"]["claim_predicate"])
        self.assertEqual(document["admission"]["reasons"], ["retired"])

        self.agent("ext", mode="externe")
        document = self.observe("ext")
        self.assertTrue(document["admission"]["claim_predicate"])
        self.assertFalse(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"], ["mode_externe"])

        self.agent("maker")
        self.agent("eph", created_by="maker")
        self.db.execute(
            "UPDATE agent_registry SET ephemeral = true,"
            " ephemeral_expires_at = now() - interval '1 hour'"
            " WHERE name = 'eph'")
        document = self.observe("eph")
        self.assertTrue(document["agent"]["ephemeral"])
        self.assertTrue(document["admission"]["claim_predicate"])
        self.assertFalse(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"],
                         ["ephemeral_expired"])

    def test_responsible_is_required_only_when_the_host_declares_it(self):
        self.agent("obs", responsible=None)
        self.assertTrue(self.observe("obs")["admission"]["admitted"])
        document = self.observe("obs", require_responsible=True)
        self.assertFalse(document["admission"]["admitted"])
        self.assertEqual(document["admission"]["reasons"],
                         ["responsible_missing"])

    def test_host_claims_and_handoff_fence(self):
        self.agent("obs")
        self.db.execute(
            "INSERT INTO host_lifecycle (host, mode, actor)"
            " VALUES (%s, 'draining', 'human:owner')", (HOST,))
        document = self.observe("obs")
        self.assertFalse(document["admission"]["claim_predicate"])
        self.assertEqual(document["admission"]["reasons"],
                         ["host_claims_closed"])
        self.db.execute("DELETE FROM host_lifecycle")

        self.db.execute(
            "INSERT INTO host_handoffs (id, agent, source, target, plan,"
            " memory_bytes, disk_bytes, reserved_until, fenced)"
            " VALUES ('h1', 'obs', 'a', 'b', '{}', 1, 1,"
            " now() + interval '1 hour', true)")
        document = self.observe("obs")
        self.assertFalse(document["admission"]["claim_predicate"])
        self.assertEqual(document["admission"]["reasons"], ["handoff_fenced"])
        self.db.execute("DELETE FROM host_handoffs")

    # -- lease, limits, native facts -------------------------------------

    def test_lease_live_and_expired(self):
        self.agent("obs")
        self.db.execute(
            "UPDATE agent_registry SET status = 'running',"
            " lease_owner = 'runner', lease_epoch = 7,"
            " lease_expires_at = now() + interval '1 hour',"
            " lease_since = now() - interval '10 minutes' WHERE name = 'obs'")
        document = self.observe("obs")
        self.assertEqual(document["lease"]["owner"], "runner")
        self.assertEqual(document["lease"]["epoch"], 7)
        self.assertTrue(document["lease"]["live"])
        self.assertTrue(document["lease"]["turn_in_progress"])
        self.assertIsNotNone(document["lease"]["expires_ts"])
        self.assertIsNotNone(document["lease"]["since_ts"])

        self.db.execute(
            "UPDATE agent_registry SET lease_expires_at ="
            " now() - interval '1 minute' WHERE name = 'obs'")
        document = self.observe("obs")
        self.assertFalse(document["lease"]["live"])
        self.assertFalse(document["lease"]["turn_in_progress"])

    def test_limits_are_effective_only_on_the_observed_agent_host(self):
        self.agent("obs", context_max_tokens=1234)
        document = self.observe("obs", host="another-host")
        self.assertFalse(document["limits"]["available"])
        self.assertIsNone(document["limits"]["effective"])
        self.assertEqual(document["limits"]["declared"]["context_max_tokens"],
                         1234)
        self.assertIn("limits", document["missing"])

        document = self.observe("obs", host=HOST)
        self.assertTrue(document["limits"]["available"])
        effective = document["limits"]["effective"]
        self.assertEqual(effective["context_max_tokens"], 1234)
        self.assertIsInstance(effective["turn_max_seconds"], int)
        self.assertIsInstance(effective["session_policy"], str)
        self.assertNotIn("limits", document["missing"])

    def test_native_pending_turn_and_sandbox(self):
        self.agent("obs")
        self.db.execute(
            "UPDATE agent_registry SET status = 'running',"
            " lease_owner = 'runner', lease_epoch = 1,"
            " lease_expires_at = now() + interval '1 hour' WHERE name = 'obs'")
        turn = "c" * 32
        self.db.execute(
            "INSERT INTO spend_pending (agent, start_index, turn, model,"
            " turn_id, work_item_id) VALUES ('obs', 2, 'tour', 'deepseek-v4',"
            " 'pending-1', 7)")
        storage.of(self.db).turn_resources.open_turn(
            turn, "obs", HOST, pgid=None, label="tour observé")
        self.db.execute(
            "INSERT INTO sandbox_turns (turn_id, spend_key, agent, owner,"
            " lease_epoch, scope, scope_digest, retry_family, scope_generation,"
            " attempt, max_attempts, profile_ref, profile_digest, placement_ref,"
            " placement_digest, canon_ref, limits, image_digest, provider,"
            " namespace, job_name, deadline_ts, source) VALUES (%s,"
            " 'sandbox-key', 'obs', 'runner', 1, '{}', 'digest', 'family', 1,"
            " 1, 2, 'profile', 'pd', 'place', 'pld', 'canon', '{}',"
            " 'sha256:image', 'kubernetes', 'default', 'sandbox-job',"
            " now() + interval '1 hour', '{}')", (turn,))
        document = self.observe("obs")
        self.assertEqual(document["native"]["turn"]["turn_id"], "pending-1")
        self.assertEqual(document["native"]["turn"]["model"], "deepseek-v4")
        self.assertEqual(document["native"]["turn"]["work_item_id"], "7")
        self.assertEqual(document["native"]["sandbox"]["turn_id"], turn)
        self.assertEqual(document["native"]["sandbox"]["agent"], "obs")
        self.assertEqual(document["native"]["sandbox"]["state"], "intent")

    # -- budget -----------------------------------------------------------

    def test_budget_envelope_is_redacted_and_bounded(self):
        self.agent("obs")
        storage.of(self.db).budgets.account_put("acct-a", 50.0, actor="human:t")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, billing_account)"
            " VALUES ('obs', 'deepseek', 'deepseek-v4', 1.0, 'acct-a')")
        self.db.execute(
            "INSERT INTO account_reservations (billing_account, agent, usd,"
            " period, spend_key) VALUES ('acct-a', 'obs', 1.5,"
            " (now() at time zone 'Europe/Paris')::date, 'k-open')")
        self.db.execute(
            "INSERT INTO account_reservations (billing_account, agent, usd,"
            " period, spend_key, state) VALUES ('acct-a', 'obs', 2.5,"
            " (now() at time zone 'Europe/Paris')::date, 'k-kept', 'kept')")
        document = self.observe("obs", **self.profiles(
            {"name": "p1", "billing_account": "acct-a", "reserve_usd": 2.0}))
        budget = document["budget"]
        self.assertTrue(budget["available"])
        self.assertIsNone(budget["reason"])
        self.assertEqual(budget["account_profile"], "p1")
        self.assertEqual(budget["billing_account"], "acct-a")
        self.assertAlmostEqual(budget["limit_usd"], 50.0)
        self.assertAlmostEqual(budget["settled_usd"], 1.0)
        self.assertAlmostEqual(budget["reserved_usd"], 1.5)
        self.assertAlmostEqual(budget["kept_usd"], 2.5)
        self.assertAlmostEqual(budget["used_usd"], 5.0)
        self.assertAlmostEqual(budget["remaining_usd"], 45.0)
        self.assertTrue(budget["admitted"])
        self.assertIsNotNone(budget["period"])
        self.assertEqual(budget["timezone"], "Europe/Paris")
        self.assertIsNotNone(budget["reset_ts"])
        self.assertNotIn("account_envelope", document["missing"])

    def test_budget_selection_is_explicit_then_session_then_single(self):
        self.agent("obs")
        storage.of(self.db).budgets.account_put("acct-a", 10.0, actor="human:t")
        storage.of(self.db).budgets.account_put("acct-b", 20.0, actor="human:t")
        two = self.profiles(
            {"name": "p1", "billing_account": "acct-a"},
            {"name": "p2", "billing_account": "acct-b"})
        self.assertEqual(
            self.observe("obs", profile="p2", **two)["budget"]["limit_usd"], 20.0)
        self.assertEqual(
            self.observe("obs", **two)["budget"]["reason"],
            "ambiguous_account_profile")

        self.db.execute("UPDATE agent_registry SET session_id = 'sess-1',"
                        " session_account = 'p2' WHERE name = 'obs'")
        document = self.observe("obs", **two)
        self.assertEqual(document["budget"]["account_profile"], "p2")
        self.assertEqual(document["budget"]["billing_account"], "acct-b")

        self.db.execute("UPDATE agent_registry SET session_account = 'gone'"
                        " WHERE name = 'obs'")
        document = self.observe("obs", **two)
        self.assertEqual(document["budget"]["reason"], "unknown_account_profile")

        self.db.execute("UPDATE agent_registry SET session_account = NULL"
                        " WHERE name = 'obs'")
        single = self.observe("obs", **self.profiles(
            {"name": "p1", "billing_account": "acct-a"}))
        self.assertEqual(single["budget"]["account_profile"], "p1")

    def test_budget_absent_or_unreadable_is_never_zero_cost(self):
        self.agent("obs")
        for cfg, reason in (
                ({}, "no_account_profile"),
                (self.profiles({"name": "p1"}), "no_billing_account"),
                (self.profiles({"name": "p1", "billing_account": "acct-a"}),
                 "no_account_envelope"),
                ({"accounts": {"deepseek": [{"name": "bad name"}]}},
                 "account_config_unreadable")):
            with self.subTest(reason=reason):
                document = self.observe("obs", **cfg)
                budget = document["budget"]
                self.assertFalse(budget["available"])
                self.assertEqual(budget["reason"], reason)
                self.assertIsNone(budget["limit_usd"])
                self.assertIsNone(budget["used_usd"])
                self.assertIsNone(budget["admitted"])
                self.assertIn("account_envelope", document["missing"])

        unknown = self.observe("obs", profile="nobody", **self.profiles(
            {"name": "p1", "billing_account": "acct-a"}))
        self.assertEqual(unknown["budget"]["reason"], "unknown_account_profile")
        self.assertEqual(unknown["budget"]["account_profile"], "nobody")

    def test_exhausted_envelope_is_visible_and_never_zero(self):
        self.agent("obs")
        storage.of(self.db).budgets.account_put("acct-a", 10.0, actor="human:t")
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, billing_account)"
            " VALUES ('obs', 'deepseek', 'deepseek-v4', 9.0, 'acct-a')")
        document = self.observe("obs", **self.profiles(
            {"name": "p1", "billing_account": "acct-a", "reserve_usd": 2.0}))
        budget = document["budget"]
        self.assertTrue(budget["available"])
        self.assertEqual(budget["reason"], "exhausted")
        self.assertFalse(budget["admitted"])
        self.assertAlmostEqual(budget["used_usd"], 9.0)
        self.assertAlmostEqual(budget["remaining_usd"], 1.0)
        self.assertIn("budget_headroom", document["missing"])

    # -- read-only transaction -------------------------------------------

    def test_observation_transaction_is_read_only(self):
        self.agent("obs")
        with self.db.transaction(isolation="REPEATABLE READ",
                                 read_only=True) as tx:
            self.assertEqual(
                tx.query("SHOW transaction_isolation")[0]["transaction_isolation"],
                "repeatable read")
            self.assertEqual(
                tx.query("SHOW transaction_read_only")[0]["transaction_read_only"],
                "on")
        with self.assertRaises(db_mod.DbError):
            with self.db.transaction(read_only=True) as tx:
                tx.execute("UPDATE agent_registry SET status = 'x'"
                           " WHERE name = 'obs'")

    def test_transaction_modes_require_the_outer_transaction(self):
        with self.assertRaises(db_mod.DbError):
            with self.db.transaction() as tx:
                with tx.transaction(isolation="SERIALIZABLE"):
                    pass
