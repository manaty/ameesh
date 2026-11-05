# SPDX-License-Identifier: AGPL-3.0-only
"""#5869: a manual drain keeps its intent through a loss and its reconciliation."""

import json
from unittest import mock

from ameesh import registry, storage
from ameesh.runner import Runner
from .support import PgTestCase


class DrainSurvivesLossTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle,host_resources,credential_health CASCADE")
        self.api = storage.of(self.db)
        registry.upsert(self.db, "dev", harness="codex", host="source", cwd="/work/source")
        self.api.hosts.record({"host": "source", "gate_state": "available"})

    def drain(self, **kwargs):
        return self.api.host_lifecycle.drain("source", actor="mesh-design", **kwargs)

    def lose(self):
        self.db.execute("UPDATE host_resources SET sampled_at=clock_timestamp()"
                        "-interval '1000 seconds' WHERE host='source'")
        lost = self.api.host_lifecycle.detect_losses(actor="remote", older_than=180)
        self.db.execute("UPDATE host_lifecycle SET updated_at=clock_timestamp()"
                        "-interval '900 seconds' WHERE host='source'")
        return lost

    def returned(self):
        self.db.execute(
            "INSERT INTO host_resources(host, sampled_at, gate_state) VALUES "
            "('source', clock_timestamp()-interval '200 seconds', 'available'), "
            "('source', clock_timestamp()-interval '100 seconds', 'available')")
        self.api.hosts.record({"host": "source", "gate_state": "available"})

    def reconcile(self):
        return self.api.host_lifecycle.reconcile_loss(
            "source", actor="runner-peer@target", fresh_within=180)

    def test_loss_of_a_draining_host_keeps_generation_and_return_at(self):
        drained = self.drain(duration=12 * 3600)
        lost = self.lose()[0]
        self.assertEqual(lost["mode"], "lost")
        self.assertEqual(lost["generation"], drained["generation"])
        self.assertAlmostEqual(lost["return_ts"], drained["return_ts"], places=3)

    def test_reconcile_restores_an_unexpired_manual_drain(self):
        drained = self.drain(duration=12 * 3600)
        self.lose()
        self.returned()
        result = self.reconcile()
        self.assertTrue(result["reconciled"], result)
        self.assertEqual(result["mode"], "draining")
        row = self.api.host_lifecycle.get("source")
        self.assertEqual((row["mode"], row["generation"], row["release_state"]),
                         ("draining", drained["generation"], "draining"))
        self.assertAlmostEqual(row["return_ts"], drained["return_ts"], places=3)
        self.assertFalse(row["automatic"])
        event = self.db.query("SELECT data FROM ameesh_events WHERE type='host.reconciled' "
                              "AND object='host:source' ORDER BY id DESC LIMIT 1")[0]["data"]
        if isinstance(event, str):
            event = json.loads(event)
        self.assertEqual((event["from"], event["to"]), ("lost", "draining"))
        # The operator's undrain works again, as on any drain.
        self.assertEqual(self.api.host_lifecycle.undrain(
            "source", actor="mesh-design")["mode"], "returning")

    def test_an_open_ended_drain_is_restored_too(self):
        self.drain()
        self.lose()
        self.returned()
        self.assertEqual(self.reconcile()["mode"], "draining")
        self.assertIsNone(self.api.host_lifecycle.get("source")["return_ts"])

    def test_an_expired_drain_returns_available_as_before(self):
        self.drain(duration=12 * 3600)
        self.lose()
        self.db.execute("UPDATE host_lifecycle SET return_at=clock_timestamp()"
                        "-interval '1 second' WHERE host='source'")
        self.returned()
        self.assertEqual(self.reconcile()["mode"], "available")

    def test_an_automatic_drain_returns_available_as_before(self):
        self.drain(duration=12 * 3600, automatic=True)
        self.lose()
        self.returned()
        self.assertEqual(self.reconcile()["mode"], "available")

    def test_a_loss_outside_a_drain_returns_available(self):
        self.drain(duration=60)
        self.api.host_lifecycle.undrain("source", actor="mesh-design")
        self.lose()
        self.returned()
        self.assertEqual(self.reconcile()["mode"], "available")

    def test_restore_keeps_the_proofs(self):
        self.drain(duration=12 * 3600)
        self.lose()
        self.api.hosts.record({"host": "source", "gate_state": "available"})
        result = self.reconcile()
        self.assertEqual(result["missing"], ["sustained_readings"])
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "lost")

    def _move(self, *, reason, state="verified"):
        registry.upsert(self.db, "dev", harness="codex", host="target", cwd="/work/target")
        self.db.execute(
            "INSERT INTO host_handoffs(id,agent,source,target,state,plan,memory_bytes,"
            "disk_bytes,reserved_until) VALUES ('op-5869','dev','source','target',%s,"
            "%s::jsonb,1,1,clock_timestamp()+interval '5 minutes')",
            (state, json.dumps({"reason": reason})))

    def test_an_agent_moved_by_the_drain_does_not_block_the_restore(self):
        self.drain(duration=12 * 3600)
        self.lose()
        self._move(reason="host_drain")
        self.returned()
        result = self.reconcile()
        self.assertEqual(result["mode"], "draining", result)
        self.assertEqual(result["evidence"]["moved_by_drain"], ["dev"])

    def test_an_agent_moved_otherwise_still_blocks(self):
        self.drain(duration=12 * 3600)
        self.lose()
        self._move(reason="host_loss")
        self.returned()
        self.assertEqual(self.reconcile()["missing"], ["agent_moved:dev"])

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_runner_pass_restores_the_drain_instead_of_reopening(self):
        self.drain(duration=12 * 3600)
        self.lose()
        self.returned()
        runner = object.__new__(Runner)
        runner.db = self.db
        runner.runner_id = "runner-orchestrateur@peer"
        runner.cfg = self.cfg
        runner.handoff_limits_for = lambda host: None
        Runner.host_lifecycle_once(runner, self.db)
        self.assertEqual(self.api.host_lifecycle.get("source")["mode"], "draining")
