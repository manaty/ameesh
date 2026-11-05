# SPDX-License-Identifier: AGPL-3.0-only
"""#5932: asynchronous adapter wait vs failure, stale stop intent."""
from __future__ import annotations

import dataclasses
import time
import unittest

from tests.support import PgTestCase
from ameesh import host_idle, host_release, storage


class WaitStateTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM vm_adapter_requests")
        self.db.execute("DELETE FROM host_lifecycle")
        self.api = storage.of(self.db).host_lifecycle
        self.cfg = dataclasses.replace(
            self.cfg, host_release_adapter_stall=900,
            host_release_drain_bound=2100, host_release_retry=300)
        self.api.drain("vm", actor="human:op", close_claims=True)
        control = self.api.get("vm")
        self.generation = control["generation"]
        self.epoch = control["release_epoch"]

    def ack_draining(self):
        self.db.execute("UPDATE host_lifecycle SET hook_state='draining' "
                        "WHERE host='vm'")

    def begin(self, state="idle-drained"):
        control = self.api.get("vm")
        return self.api.begin_transition(
            "vm", control["generation"], state,
            release_epoch=control["release_epoch"], actor="scanner",
            stale_after=420, retry_after=float(self.cfg.host_release_retry))

    def wait(self, state="idle-drained"):
        control = self.api.get("vm")
        return self.api.wait_transition(
            "vm", control["generation"], state,
            release_epoch=control["release_epoch"], actor="scanner",
            detail={"command": "adapter"})

    def queue(self, state="idle-drained", result="poweroff intent", age_s=0.0,
              generation=None, epoch=None):
        self.db.execute(
            "INSERT INTO vm_adapter_requests(host,generation,release_epoch,state,"
            "status,result,created_at,updated_at) VALUES "
            "('vm',%s,%s,%s,'pending',%s,clock_timestamp(),"
            "clock_timestamp()-make_interval(secs=>%s))",
            (self.generation if generation is None else generation,
             self.epoch if epoch is None else epoch, state, result, float(age_s)))

    def alerts(self):
        return host_release.alerts(self.cfg, self.db, time.time())

    def types(self):
        return [alert["type"] for alert in self.alerts()]

    def test_wait_does_not_acknowledge_or_fail(self):
        self.ack_draining()
        before = len(self.db.query(
            "SELECT 1 FROM ameesh_events WHERE type='host.release_waiting'"))
        self.assertIsNotNone(self.begin())
        row = self.wait()
        self.assertEqual(row["release_state"], "draining")
        self.assertEqual(row["hook_state"], "draining")
        self.assertEqual(row["hook_wait_state"], "idle-drained")
        self.assertIsNotNone(row["hook_wait_ts"])
        self.assertIsNone(row["hook_attempt_state"])
        self.assertIsNone(row["release_error"])
        self.assertFalse(row["claims_open"])
        events = self.db.query(
            "SELECT data FROM ameesh_events WHERE type='host.release_waiting' "
            "ORDER BY created_at DESC, id DESC LIMIT 1")
        after = len(self.db.query(
            "SELECT 1 FROM ameesh_events WHERE type='host.release_waiting'"))
        self.assertEqual(after, before + 1)
        self.assertEqual(events[0]["data"]["state"], "idle-drained")

    def test_same_state_retry_keeps_the_first_wait_instant(self):
        self.ack_draining()
        self.begin()
        self.wait()
        self.db.execute("UPDATE host_lifecycle SET hook_wait_at=now()-"
                        "interval '400 seconds' WHERE host='vm'")
        stale = self.api.get("vm")["hook_wait_ts"]
        self.assertIsNotNone(self.begin())
        row = self.wait()
        self.assertEqual(row["hook_wait_ts"], stale)

    def test_a_new_state_starts_a_new_wait(self):
        self.ack_draining()
        self.begin()
        self.wait()
        self.db.execute("UPDATE host_lifecycle SET hook_wait_at=now()-"
                        "interval '400 seconds' WHERE host='vm'")
        stale = self.api.get("vm")["hook_wait_ts"]
        self.assertIsNotNone(self.begin("busy"))
        row = self.wait("busy")
        self.assertEqual(row["hook_wait_state"], "busy")
        self.assertGreater(row["hook_wait_ts"], stale + 300)

    def test_wait_is_fenced_by_generation_and_epoch(self):
        self.ack_draining()
        self.begin()
        self.db.execute("UPDATE host_lifecycle SET generation=generation+1 "
                        "WHERE host='vm'")
        self.assertIsNone(self.wait())
        self.assertIsNone(self.api.get("vm")["hook_wait_state"])

        self.db.execute("DELETE FROM host_lifecycle")
        self.api.drain("vm", actor="human:op", close_claims=True)
        self.ack_draining()
        self.assertIsNotNone(self.begin())
        self.db.execute("UPDATE host_lifecycle SET release_epoch=release_epoch+1 "
                        "WHERE host='vm'")
        self.assertIsNone(self.wait())
        self.assertIsNone(self.api.get("vm")["hook_wait_state"])

    def test_finish_acknowledges_and_clears_the_wait(self):
        self.ack_draining()
        self.begin()
        self.wait()
        control = self.api.get("vm")
        self.api.finish_transition(
            "vm", control["generation"], "idle-drained",
            release_epoch=control["release_epoch"], actor="worker",
            detail={"adapter": "test"})
        row = self.api.get("vm")
        self.assertEqual(row["release_state"], "idle-drained")
        self.assertEqual(row["hook_state"], "idle-drained")
        self.assertIsNone(row["hook_wait_state"])
        self.assertIsNotNone(row["released_ts"])
        self.assertFalse(row["claims_open"])

    def test_a_young_wait_paces_the_same_state_retry(self):
        self.ack_draining()
        self.begin()
        self.wait()
        self.assertIsNone(self.begin())
        self.db.execute("UPDATE host_lifecycle SET hook_wait_at=now()-"
                        "interval '400 seconds' WHERE host='vm'")
        self.assertIsNotNone(self.begin())

    def test_poweroff_intent_blocks_retry_and_raises_the_dedicated_alert(self):
        self.ack_draining()
        self.queue(result='poweroff intent', age_s=1000)
        self.db.execute("UPDATE host_lifecycle SET hook_attempt_at=now()-"
                        "interval '2 hours', hook_wait_at=now()-interval '2 hours', "
                        "hook_wait_state='idle-drained' WHERE host='vm'")
        # Archive protection: never clear the durable stop intent by retrying.
        self.assertIsNone(self.begin())
        alerts = self.alerts()
        self.assertEqual([alert["type"] for alert in alerts],
                         ["host_release_adapter_stalled"])
        self.assertEqual(alerts[0]["reason"], "poweroff")
        self.assertEqual(alerts[0]["state"], "idle-drained")
        self.assertEqual(alerts[0]["generation"], self.generation)
        self.assertFalse(self.api.get("vm")["claims_open"])

    def test_a_young_poweroff_intent_is_a_normal_trailing_wait(self):
        self.queue(result='poweroff intent', age_s=60)
        self.db.execute("UPDATE host_lifecycle SET requested_at=now()-"
                        "interval '3000 seconds' WHERE host='vm'")
        self.assertEqual(self.types(), [])
        self.db.execute("UPDATE vm_adapter_requests SET updated_at=now()-"
                        "interval '1000 seconds' WHERE host='vm'")
        self.assertEqual(self.types(), ["host_release_adapter_stalled"])

    def test_a_ticking_worker_suppresses_overdue_and_its_loss_is_signalled(self):
        from ameesh.storage.postgres.vm_adapter import Queue
        self.queue(state='draining', result=None, age_s=1000)
        self.db.execute("UPDATE host_lifecycle SET requested_at=now()-"
                        "interval '3000 seconds' WHERE host='vm'")
        Queue(self.db).observe('vm', 'running', 90)
        self.assertEqual(self.types(), [])
        self.db.execute("UPDATE vm_adapter_observations SET updated_at=now()-"
                        "interval '1000 seconds' WHERE host='vm'")
        alerts = self.alerts()
        self.assertEqual([alert["type"] for alert in alerts],
                         ["host_release_adapter_stalled"])
        self.assertEqual(alerts[0]["reason"], "worker")
        self.assertIn("not been seen", alerts[0]["detail"])

    def test_an_obsolete_intent_is_signalled(self):
        self.ack_draining()
        self.queue(generation=self.generation - 1, age_s=1000)
        self.assertIsNotNone(self.begin())
        alerts = self.alerts()
        self.assertEqual([alert["type"] for alert in alerts],
                         ["host_release_adapter_stalled"])
        self.assertEqual(alerts[0]["reason"], "obsolete")
        self.assertIn("obsolete", alerts[0]["detail"])

    def test_a_queued_stop_is_not_a_lost_host(self):
        # #5932: the worker may have cleared hook_attempt_state by recording
        # the wait; the wait itself keeps the intentional stop out of the loss
        # detector while the VM archives.
        from ameesh import registry
        registry.upsert(self.db, "worker", harness="deepseek", host="vm",
                        cwd="/work/worker", mode="execute")
        self.db.execute(
            "UPDATE host_lifecycle SET release_state='draining', "
            "hook_state='draining', hook_wait_state='idle-drained', "
            "hook_wait_at=now() WHERE host='vm'")
        storage.of(self.db).hosts.record({
            "host": "vm", "turns_in_progress": 0, "cpu_count": 8,
            "mem_total_bytes": 16 * 1024 ** 3,
            "mem_available_bytes": 12 * 1024 ** 3,
            "app_cpu": 0.1, "load1": 0.1,
            "cgroup": {"container": False, "limits_readable": True}})
        self.db.execute("UPDATE host_resources SET sampled_at=now()-"
                        "interval '1 hour' WHERE host='vm'")
        self.assertEqual(self.api.detect_losses(actor="runner",
                                                older_than=180), [])
        self.assertEqual(self.api.get("vm")["mode"], "draining")


class FootprintWaitTest(unittest.TestCase):
    def test_the_release_overlay_carries_the_explicit_wait(self):
        footprint = host_idle.apply_release(
            {"state": "draining"},
            {"mode": "draining", "release_state": "draining",
             "hook_state": "draining", "hook_wait_state": "idle-drained",
             "hook_wait_ts": 1000.0, "claims_open": False})
        self.assertEqual(footprint["release"]["wait_state"], "idle-drained")
        self.assertEqual(footprint["release"]["wait_ts"], 1000.0)
