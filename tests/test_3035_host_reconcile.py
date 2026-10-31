# SPDX-License-Identifier: AGPL-3.0-only
"""#3035: a lost host reopens only on a verified reconciliation proof."""

from .entrypoint import run as isolated_entrypoint
import contextlib
import io
import json
import threading
import time
from unittest import mock

from ameesh import db as db_mod, host_lifecycle, registry, storage
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
        # The loss happened one detection window ago: readings of the return
        # must fall after it, as they do on a real host that stayed lost.
        self.db.execute("UPDATE host_lifecycle "
                        "SET updated_at=clock_timestamp()-interval '900 seconds' "
                        "WHERE host='source'")

    def age_source(self):
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-interval '1000 seconds' "
                        "WHERE host='source'")

    def detect_loss(self):
        return self.api.host_lifecycle.detect_losses(actor="remote", older_than=180,
                                                     critical=("dev",))

    def fresh(self):
        return self.api.hosts.record({"host": "source", "gate_state": "available"})

    def returned(self, *, span=200, middle=100):
        """A sustained return: two readings in the detection window, then a fresh one."""
        self.db.execute(
            "INSERT INTO host_resources(host, sampled_at, gate_state) VALUES "
            "('source', clock_timestamp()-make_interval(secs=>%s), 'available'), "
            "('source', clock_timestamp()-make_interval(secs=>%s), 'available')",
            (span, middle))
        return self.fresh()

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

    def test_lost_stays_closed_for_the_placement_reader(self):
        # The only functional reader of the field, in this tree and in 1.16.0,
        # opens on `available` alone: an unknown value stays closed.
        from ameesh import relocation
        latest = self.api.hosts.latest("source")
        self.assertEqual(latest["gate_state"], "lost")
        self.assertFalse(relocation.gate_open(dict(latest, sampled_ts=time.time()), None))

    def test_verified_proof_reopens_claims_and_traces_actor_and_evidence(self):
        self.returned()
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
        self.assertEqual(event["data"]["readings"], 3)
        self.assertEqual(event["data"]["readings_since_loss"], 3)
        self.assertEqual(event["data"]["suffix_start_ts"], event["data"]["first_fresh_ts"])
        self.assertEqual(event["data"]["live_lease_agents"], [])
        self.assertEqual(event["data"]["checks"],
                         ["fresh_reading", "sustained_readings", "no_reading_gap",
                          "agents_home", "no_active_handoff", "no_recovery_attempt"])

    def test_a_single_fresh_reading_does_not_reopen(self):
        self.fresh()
        result = self.reconcile()
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["missing"], ["sustained_readings"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")
        self.assertIsNone(registry.claim(self.db, "dev", "runner", ttl_seconds=300))

    def test_a_short_history_does_not_reopen(self):
        self.db.execute("INSERT INTO host_resources(host, sampled_at, gate_state) "
                        "VALUES ('source', clock_timestamp()-interval '100 seconds', 'available')")
        self.fresh()
        self.assertEqual(self.reconcile()["missing"], ["sustained_readings"])

    def test_a_hole_as_large_as_the_window_does_not_reopen(self):
        self.db.execute("INSERT INTO host_resources(host, sampled_at, gate_state) "
                        "VALUES ('source', clock_timestamp()-interval '200 seconds', 'available')")
        self.fresh()
        self.assertEqual(self.reconcile()["missing"], ["sustained_readings"])

    def test_an_older_hole_before_the_recent_window_does_not_block(self):
        # B1 de la relecture cx2 : perte t-900, relevés t-600/t-200/t-100/t ;
        # le trou ancien (400 s) précède un suffixe continu de 200 s.
        self.db.execute(
            "INSERT INTO host_resources(host, sampled_at, gate_state) VALUES "
            "('source', clock_timestamp()-interval '600 seconds', 'available'), "
            "('source', clock_timestamp()-interval '200 seconds', 'available'), "
            "('source', clock_timestamp()-interval '100 seconds', 'available')")
        self.fresh()
        result = self.reconcile()
        self.assertTrue(result["reconciled"], result)
        self.assertEqual(result["evidence"]["readings"], 3)
        self.assertEqual(result["evidence"]["readings_since_loss"], 4)
        self.assertGreater(result["evidence"]["suffix_start_ts"],
                           result["evidence"]["first_fresh_ts"])
        self.assertGreaterEqual(result["evidence"]["coverage_s"], 180)

    def test_coverage_is_measured_between_the_suffix_readings(self):
        # B2 de la relecture cx2 : deux relevés espacés de 100 s, le dernier
        # encore frais ; la couverture réelle est 100 s, pas le silence final.
        self.db.execute(
            "INSERT INTO host_resources(host, sampled_at, gate_state) VALUES "
            "('source', clock_timestamp()-interval '200 seconds', 'available'), "
            "('source', clock_timestamp()-interval '100 seconds', 'available')")
        result = self.reconcile()
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["missing"], ["sustained_readings"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_a_future_reading_is_refused(self):
        self.returned()
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()+interval '1 hour' "
                        "WHERE host='source'")
        self.assertEqual(self.reconcile()["missing"], ["future_reading"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_missing_fresh_reading_is_a_named_refusal(self):
        result = self.reconcile()
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["missing"], ["fresh_reading"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_stale_fresh_reading_beyond_the_threshold_is_refused(self):
        self.returned()
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()-interval '5 minutes' "
                        "WHERE host='source'")
        result = self.reconcile()
        self.assertEqual(result["missing"], ["fresh_reading"])

    def test_agent_moved_elsewhere_keeps_the_host_closed(self):
        self.returned()
        self.db.execute("UPDATE agent_registry SET host='target' WHERE name='dev'")
        result = self.reconcile()
        self.assertFalse(result["reconciled"])
        self.assertEqual(result["missing"], ["agent_moved:dev"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_live_lease_of_a_returning_agent_does_not_block(self):
        # #3180: the host's own runner takes its lease back as soon as the
        # machine reboots, before the sustained-reading window closes. That
        # lease is the returning executor, not a second one; only a move away
        # (next test) must keep the host closed.
        self.returned()
        self.db.execute("UPDATE agent_registry SET lease_owner='runner-returning@source', "
                        "lease_expires_at=clock_timestamp()+interval '5 minutes' WHERE name='dev'")
        result = self.reconcile()
        self.assertTrue(result["reconciled"], result)
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "available")
        self.assertEqual(self.event()["data"]["live_lease_agents"], ["dev"])
        self.assertEqual(self.event()["data"]["checks"], [
            "fresh_reading", "sustained_readings", "no_reading_gap",
            "agents_home", "no_active_handoff", "no_recovery_attempt"])

    def test_fenced_agent_keeps_the_host_closed(self):
        self.returned()
        self.db.execute("UPDATE agent_registry SET handoff_fenced=true WHERE name='dev'")
        result = self.reconcile()
        self.assertIn("handoff_fenced:dev", result["missing"])

    def test_active_handoff_keeps_the_host_closed(self):
        self.returned()
        self.add_handoff(state="planned")
        result = self.reconcile()
        self.assertIn("active_handoff:dev", result["missing"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_recovery_attempt_since_the_loss_keeps_the_host_closed(self):
        self.returned()
        self.add_handoff(state="verified", evidence={"source_recovered": True})
        result = self.reconcile()
        self.assertIn("recovery_attempted:dev", result["missing"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_recovery_committed_while_reconcile_waits_is_seen_under_the_gate(self):
        self.returned()
        started, out = threading.Event(), []

        def waiter():
            other = db_mod.connect(self.cfg)
            try:
                started.set()
                out.append(storage.of(other).host_lifecycle.reconcile_loss(
                    "source", actor="runner-peer@target", fresh_within=180))
            finally:
                other.close()

        thread = threading.Thread(target=waiter, daemon=True)
        with self.db.transaction() as tx:
            tx.query("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                     ("host-gate:source",))
            thread.start()
            self.assertTrue(started.wait(5))
            thread.join(0.1)
            self.assertTrue(thread.is_alive(), "reconciliation must wait on the host gate")
            # The recovery commits while reconciliation waits on the same gate;
            # the proof is read again under the lock and sees it.
            tx.execute(
                "INSERT INTO host_handoffs(id,agent,source,target,state,plan,evidence,"
                "memory_bytes,disk_bytes,reserved_until) VALUES ('op-3035','dev','source',"
                "'target','prepared',%s::jsonb,%s::jsonb,1,1,"
                "clock_timestamp()+interval '5 minutes')",
                (json.dumps({"reason": "host_loss"}),
                 json.dumps({"source_recovered": True})))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(out[0]["reconciled"])
        self.assertIn("recovery_attempted:dev", out[0]["missing"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def test_recovery_attempt_before_this_loss_does_not_block_it(self):
        self.returned()
        self.add_handoff(state="verified", evidence={"source_recovered": True}, age="1 day")
        result = self.reconcile()
        self.assertTrue(result["reconciled"], result)

    def test_reconciliation_is_idempotent(self):
        self.returned()
        self.assertTrue(self.reconcile()["reconciled"])
        again = self.reconcile()
        self.assertFalse(again["reconciled"])
        self.assertEqual(again["missing"], ["not_lost"])
        self.assertEqual(again["mode"], "available")
        self.assertEqual(self.db.query(
            "SELECT count(*) AS n FROM ameesh_events WHERE id > %s AND type='host.reconciled' "
            "AND object='host:source'", (self.event_base,))[0]["n"], 1)

    def test_runner_lifecycle_pass_reconciles_a_returning_host(self):
        self.returned()
        runner = object.__new__(Runner)
        runner.runner_id = "runner-mty-x@peer"
        runner.cfg = self.cfg
        runner.handoff_limits_for = lambda host: None
        Runner.host_lifecycle_once(runner, self.db)
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["actor"]), ("available", "runner-mty-x@peer"))
        self.assertIsNotNone(registry.claim(self.db, "dev", "runner", ttl_seconds=300))

    def test_runner_lifecycle_pass_reopens_a_rebooting_host_that_holds_its_leases(self):
        # #3180 (pc-smichea, 23/10): the runners of the returning machine take
        # their leases back at boot, so the first lifecycle pass sees home
        # agents with live leases while the sustained readings are still
        # incomplete; once they cover the window the host must reopen alone.
        self.returned()
        self.db.execute("UPDATE agent_registry SET lease_owner='runner-returning@source', "
                        "lease_expires_at=clock_timestamp()+interval '5 minutes' WHERE name='dev'")
        runner = object.__new__(Runner)
        runner.runner_id = "runner-returning@source"
        runner.cfg = self.cfg
        runner.handoff_limits_for = lambda host: None
        Runner.host_lifecycle_once(runner, self.db)
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["actor"]), ("available", "runner-returning@source"))

    def test_cli_dispatch_routes_host_reconcile(self):
        from ameesh import main as main_mod
        with mock.patch("ameesh.host_lifecycle.main", return_value=0) as impl:
            self.assertEqual(isolated_entrypoint(main_mod.main, ["host", "reconcile", "source"]), 0)
        impl.assert_called_once_with(["reconcile", "source"])

    def test_module_reconcile_uses_the_session_corrector(self):
        self.returned()
        with mock.patch.object(host_lifecycle.work, "corrector",
                               return_value="human:smichea") as corrector:
            result = host_lifecycle.reconcile(self.cfg, self.db, "source")
        self.assertTrue(result["reconciled"], result)
        corrector.assert_called_once()
        self.assertEqual(self.api.host_lifecycle.get("source")["actor"], "human:smichea")

    def test_command_refuses_then_reopens_with_its_actor(self):
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
            self.returned()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                reopened = host_lifecycle.main(["reconcile", "source"])
            self.assertEqual(reopened, 0)
            self.assertIn('"reconciled": true', out.getvalue())
        self.assertEqual(self.api.host_lifecycle.get("source")["actor"], "human:smichea")
