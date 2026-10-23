# SPDX-License-Identifier: AGPL-3.0-only
"""#3035: a lost host reopens only on a verified reconciliation proof."""
import contextlib
import io
import json
from unittest import mock

from ameesh import host_lifecycle, registry, storage
from ameesh.runner import Runner
from .support import PgTestCase


class HostReconcileTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle,host_resources,credential_health CASCADE")
        # The event log is append-only for 30 days; keep this test's window
        # rather than deleting a previous test's trace.
        self.event_base = self.db.query(
            "SELECT coalesce(max(id),0) AS id FROM ameesh_events")[0]["id"]
        self.api = storage.of(self.db)
        registry.upsert(self.db, "dev", harness="codex", host="source", cwd="/work/source")
        self.lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        self.api.hosts.record({"host": "source", "gate_state": "available"})
        self.age_source()
        registry.release(self.db, "dev", "runner", self.lease["lease_epoch"])
        self.control = self.detect_loss()[0]

    def age_source(self):
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-interval '10 minutes' "
                        "WHERE host='source'")

    def detect_loss(self):
        return self.api.host_lifecycle.detect_losses(actor="remote", older_than=180,
                                                     critical=("dev",))

    def fresh(self):
        return self.api.hosts.record({"host": "source", "gate_state": "available"})

    def reconcile(self, *, actor="runner-peer@target", fresh_within=180):
        return self.api.host_lifecycle.reconcile_loss(
            "source", actor=actor, fresh_within=fresh_within)

    def add_handoff(self, *, state="planned", evidence=None, age="0 seconds"):
        self.db.execute(
            "INSERT INTO host_handoffs(id,agent,source,target,state,plan,evidence,memory_bytes,"
            "disk_bytes,reserved_until,updated_at) VALUES ('op-3035','dev','source','target',%s,"
            "%s::jsonb,%s::jsonb,1,1,clock_timestamp()+interval '5 minutes',"
            "clock_timestamp()-make_interval(secs=>%s))",
            (state, json.dumps({"reason": "host_loss"}), json.dumps(evidence or {}),
             {"0 seconds": 0, "1 day": 86400}[age]))

    def event(self):
        row = self.db.query(
            "SELECT type, object, data FROM ameesh_events WHERE id > %s "
            "AND type='host.reconciled' AND object='host:source' "
            "ORDER BY id DESC LIMIT 1", (self.event_base,))[0]
        data = row["data"]
        if isinstance(data, str):
            data = json.loads(data)
        return dict(row, data=data)

    def test_gate_state_names_lost_instead_of_draining(self):
        self.assertEqual(self.api.hosts.latest("source")["gate_state"], "lost")

    def test_verified_proof_reopens_claims_and_traces_actor_and_evidence(self):
        self.fresh()
        result = self.reconcile()
        self.assertTrue(result["reconciled"], result)
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["actor"]), ("available", "runner-peer@target"))
        self.assertEqual(result["generation"], self.control["generation"])
        self.assertEqual(self.api.hosts.latest("source")["gate_state"], "available")
        self.assertIsNotNone(registry.claim(self.db, "dev", "runner", ttl_seconds=300))
        event = self.event()
        self.assertEqual(event["object"], "host:source")
        self.assertEqual(event["data"]["generation"], self.control["generation"])
        self.assertEqual(event["data"]["agents"], ["dev"])
        self.assertEqual((event["data"]["from"], event["data"]["via"], event["data"]["to"]),
                         ("lost", "returning", "available"))
        self.assertEqual(event["data"]["actor"], "runner-peer@target")
        self.assertGreaterEqual(event["data"]["fresh_ts"], event["data"]["lost_ts"])
        self.assertLessEqual(event["data"]["first_fresh_ts"], event["data"]["fresh_ts"])
        self.assertGreater(event["data"]["first_fresh_ts"], event["data"]["lost_ts"])
        self.assertEqual(event["data"]["checks"],
                         ["fresh_reading", "agents_home", "no_live_lease",
                          "no_active_handoff", "no_recovery_attempt"])

    def test_fresh_reading_alone_does_not_reopen(self):
        self.fresh()
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")
        self.assertIsNone(registry.claim(self.db, "dev", "runner", ttl_seconds=300))

    def test_missing_fresh_reading_is_a_named_refusal(self):
        result = self.reconcile()
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["missing"], ["fresh_reading"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_stale_fresh_reading_beyond_the_threshold_is_refused(self):
        self.fresh()
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-interval '5 minutes' "
                        "WHERE host='source'")
        result = self.reconcile()
        self.assertEqual(result["missing"], ["fresh_reading"])

    def test_agent_moved_elsewhere_keeps_the_host_closed(self):
        self.fresh()
        self.db.execute("UPDATE agent_registry SET host='target' WHERE name='dev'")
        result = self.reconcile()
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["missing"], ["agent_moved:dev"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_live_lease_keeps_the_host_closed(self):
        self.fresh()
        self.db.execute("UPDATE agent_registry SET lease_owner='runner', "
                        "lease_expires_at=clock_timestamp()+interval '5 minutes' WHERE name='dev'")
        result = self.reconcile()
        self.assertIn("live_lease:dev", result["missing"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_fenced_agent_keeps_the_host_closed(self):
        self.fresh()
        self.db.execute("UPDATE agent_registry SET handoff_fenced=true WHERE name='dev'")
        result = self.reconcile()
        self.assertIn("handoff_fenced:dev", result["missing"])

    def test_active_handoff_keeps_the_host_closed(self):
        self.fresh()
        self.add_handoff(state="planned")
        result = self.reconcile()
        self.assertIn("active_handoff:dev", result["missing"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_recovery_attempt_since_the_loss_keeps_the_host_closed(self):
        self.fresh()
        self.add_handoff(state="verified", evidence={"source_recovered": True})
        result = self.reconcile()
        self.assertIn("recovery_attempted:dev", result["missing"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_recovery_attempt_before_this_loss_does_not_block_it(self):
        self.fresh()
        self.add_handoff(state="verified", evidence={"source_recovered": True}, age="1 day")
        result = self.reconcile()
        self.assertTrue(result["reconciled"], result)

    def test_reconciliation_is_idempotent(self):
        self.fresh()
        self.assertTrue(self.reconcile()["reconciled"])
        again = self.reconcile()
        self.assertFalse(again["reconciled"])
        self.assertEqual(again["missing"], ["not_lost"])
        self.assertEqual(again["mode"], "available")
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM ameesh_events WHERE id > %s AND type='host.reconciled' "
            "AND object='host:source'", (self.event_base,))[0]["n"], 1)

    def test_runner_lifecycle_pass_reconciles_a_returning_host(self):
        self.fresh()
        runner = object.__new__(Runner)
        runner.runner_id = "runner-mty-x@peer"
        runner.cfg = self.cfg
        runner.handoff_limits_for = lambda host: None
        Runner.host_lifecycle_once(runner, self.db)
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["actor"]), ("available", "runner-mty-x@peer"))
        self.assertIsNotNone(registry.claim(self.db, "dev", "runner", ttl_seconds=300))

    def test_cli_dispatch_routes_host_reconcile(self):
        from ameesh import main as main_mod
        with mock.patch("ameesh.host_lifecycle.main", return_value=0) as impl:
            self.assertEqual(main_mod.main(["host", "reconcile", "source"]), 0)
        impl.assert_called_once_with(["reconcile", "source"])

    def test_module_reconcile_uses_the_session_corrector(self):
        self.fresh()
        with mock.patch.object(host_lifecycle.work, "corrector",
                               return_value="human:smichea") as corrector:
            result = host_lifecycle.reconcile(self.cfg, self.db, "source")
        self.assertTrue(result["reconciled"], result)
        corrector.assert_called_once()
        self.assertEqual(self.api.host_lifecycle.get("source")["actor"], "human:smichea")

    def test_command_refuses_then_reopens_with_its_actor(self):
        from ameesh import db as db_mod
        real_connect = db_mod.connect

        def connect(_cfg):
            return real_connect(self.cfg)

        with mock.patch.object(host_lifecycle.config, "load", return_value=self.cfg), \
                mock.patch.object(host_lifecycle.db_mod, "connect", side_effect=connect), \
                mock.patch.object(host_lifecycle.work, "corrector", return_value="human:smichea"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                refused = host_lifecycle.main(["reconcile", "source"])
            self.assertEqual(refused, 1)
            self.assertIn("fresh_reading", out.getvalue())
            self.fresh()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                reopened = host_lifecycle.main(["reconcile", "source"])
            self.assertEqual(reopened, 0)
            self.assertIn('"reconciled": true', out.getvalue())
        self.assertEqual(self.api.host_lifecycle.get("source")["actor"], "human:smichea")
