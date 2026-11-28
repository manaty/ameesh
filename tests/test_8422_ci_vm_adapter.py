# SPDX-License-Identifier: AGPL-3.0-only
"""#8422: CI VM targets of the VM adapter, stop at rest and return proof.

Tranche 5 of #8029. A CI VM (`ameesh-ci-1..3`) publishes no host reading, has
no ameesh runner and no Host-card hook: the operator worker is its controller.
It opens the drain itself when the measured CI wake queue is idle and its
registered runners have been at rest for the enrollment hysteresis. The stop
proof is that same measurement (no busy runner, last busy older than the quiet
bound); the return proof is every registered runner online in a reading made
after the power-on, instead of the host reading and the probe agent.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import PgTestCase
from tests.test_5925_vm_adapter import TARGET, policy
from ameesh import config, storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue


HOST = "ameesh-ci-1"
REPO = "manaty/ameesh"
LABELS = ["self-hosted", "linux", "ameesh-ci"]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def ci_target(**over):
    """An enrolled CI target: no probe, no health/reconcile (README)."""
    target = dict(zone="fr-par-1",
                  server_id="11111111-1111-4111-8111-111111111111",
                  project_id="22222222-2222-4222-8222-222222222222",
                  timezone="Europe/Paris", kind="ci",
                  quiesce_argv=["/quiesce"], halt_argv=["/halt"],
                  halted_argv=["/halted"])
    target.update(over)
    return target


class CiEnrollmentTest(unittest.TestCase):
    """`kind: ci` keeps the operator stop contract, not the agent probe one."""

    def test_ci_target_needs_no_probe_agent_nor_health_commands(self):
        value = policy(targets={"ci": ci_target()})
        kept = vm.enrollment(value, local_host="controller")
        self.assertEqual(kept["targets"]["ci"]["kind"], "ci")

    def test_unknown_kind_is_refused(self):
        # A missing `kind` is the backward-compatible `agent` default (the
        # historical enrollment has no such key); only an explicit bad value
        # is refused.
        for kind in ("", "vm", "CI", None, True):
            value = policy(targets={"ci": ci_target(kind=kind)})
            with self.assertRaisesRegex(vm.Refused, "kind"):
                vm.enrollment(value, local_host="controller")

    def test_ci_target_still_requires_the_operator_stop_commands(self):
        for key in ("quiesce_argv", "halt_argv", "halted_argv"):
            value = policy(targets={"ci": ci_target()})
            del value["targets"]["ci"][key]
            with self.assertRaisesRegex(vm.Refused, "operator commands"):
                vm.enrollment(value, local_host="controller")

    def test_ci_target_cannot_be_a_replica(self):
        # The standby bound is enrolled first (a replica needs it); the CI
        # refusal comes at the target itself.
        value = policy(targets={"ci": ci_target(replica=True)},
                       standby_timeout_s=10)
        with self.assertRaisesRegex(vm.Refused, "replica"):
            vm.enrollment(value, local_host="controller")

    def test_agent_target_keeps_the_probe_and_health_contract(self):
        for key in ("probe_agent", "health_argv", "reconcile_argv"):
            value = policy(targets={"burst": dict(TARGET)})
            del value["targets"]["burst"][key]
            with self.assertRaises(vm.Refused):
                vm.enrollment(value, local_host="controller")


class CiDomainTest(PgTestCase):
    """Shared fixtures: measured queue and runners of a CI host."""

    def setUp(self):
        super().setUp()
        for table in ("ci_runner_state", "ci_queue_state", "ci_queue_wait",
                      "ci_queue_wait_scan"):
            self.db.execute("DELETE FROM %s" % table)
        self.cfg = replace(self.cfg, idle_after_s=1800)
        self.st = storage.of(self.db)
        self.now = Queue(self.db).now()

    def scan_runner(self, *, name=None, online=True, busy=False):
        self.st.ci.observe_runners(REPO, [{
            "id": 1, "name": name or (HOST + "-1"), "busy": busy and online,
            "job": None, "stuck": False, "labels": list(LABELS),
            "online": online}])

    def seed_runner(self, *, name=None, online=True, busy=False,
                    seen=None, last_busy=None):
        name = name or (HOST + "-1")
        self.scan_runner(name=name, online=online, busy=busy)
        # A current watcher writes `state_seen_at` with the same instant as
        # `seen_at`; the fixture keeps that identity when it dates the row.
        seen = self.now if seen is None else seen
        self.db.execute(
            "UPDATE ci_runner_state SET seen_at=to_timestamp(%s), "
            "state_seen_at=to_timestamp(%s), last_busy_at=%s WHERE name=%s",
            (seen, seen,
             None if last_busy is None else datetime.fromtimestamp(
                 float(last_busy), timezone.utc), name))

    def n1_scan(self, *, seen=None, busy=False):
        """An N-1 watcher: it refreshes `seen_at` and the legacy busy state,
        never the 0616 facts (`online`, `last_busy_at`) nor their stamp."""
        self.db.execute(
            "UPDATE ci_runner_state SET seen_at=to_timestamp(%s), busy=%s, "
            "busy_since=CASE WHEN %s THEN now() END WHERE name=%s",
            (self.now if seen is None else seen, bool(busy), bool(busy),
             HOST + "-1"))

    def seed_idle_queue(self):
        self.st.ci.observe_queue_wait(REPO, [])
        self.st.ci.observe_queue(REPO, oldest_id=None, oldest_at=None, queued=0,
                                 wait_published=True)

    def seed_waiting_queue(self, labels=LABELS):
        self.st.ci.observe_queue_wait(REPO, [{
            "labels": list(labels), "oldest_id": 11,
            "oldest_at": iso(self.now - 900), "queued": 1}])
        self.st.ci.observe_queue(REPO, oldest_id=11,
                                 oldest_at=iso(self.now - 900), queued=1,
                                 wait_published=True)

    def control(self):
        return storage.of(self.db).host_lifecycle.drain(
            HOST, actor="tester", automatic=True, close_claims=True)

    def observe(self, state="running", *, on_since=None, off_since=None,
                poweron=None):
        Queue(self.db).observe(HOST, state, 90)
        self.db.execute(
            "UPDATE vm_adapter_observations SET on_since=%s, off_since=%s, "
            "poweron_at=%s WHERE host=%s",
            (None if on_since is None else datetime.fromtimestamp(
                float(on_since), timezone.utc),
             None if off_since is None else datetime.fromtimestamp(
                 float(off_since), timezone.utc),
             None if poweron is None else datetime.fromtimestamp(
                 float(poweron), timezone.utc), HOST))


class CiRunnerActivityTest(CiDomainTest):
    def test_no_measured_runner_is_inert(self):
        self.assertEqual(vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now), {"state": "inert"})

    def test_an_online_idle_fleet_reports_rest(self):
        self.seed_runner(online=True, last_busy=self.now - 3600)
        activity = vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now)
        self.assertEqual(activity["state"], "ok")
        self.assertTrue(activity["online"])
        self.assertEqual(activity["busy"], [])
        self.assertEqual(activity["last_busy_ts"], self.now - 3600)

    def test_offline_busy_and_stale_are_distinct_doubts(self):
        self.seed_runner(online=False)
        self.assertFalse(vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now)["online"])
        self.db.execute("DELETE FROM ci_runner_state")
        self.seed_runner(online=True, busy=True, last_busy=self.now - 10)
        activity = vm.host_release.ci_runner_activity(self.db, HOST, now=self.now)
        self.assertEqual(activity["busy"], [HOST + "-1"])
        self.db.execute("UPDATE ci_runner_state SET seen_at=to_timestamp(%s), "
                        "state_seen_at=to_timestamp(%s)",
                        (self.now - 4000, self.now - 4000))
        self.assertEqual(vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now)["state"], "unknown")

    def test_an_n1_scan_is_not_a_proof(self):
        # The N-1 watcher refreshed the scan age after the power-on while
        # `online=true` came from before the stop: it must not prove a return.
        self.seed_runner(online=True, seen=self.now - 120,
                         last_busy=self.now - 7200)
        self.n1_scan(seen=self.now - 10)
        activity = vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now)
        self.assertEqual(activity["state"], "unknown")
        self.assertIn("not dated", activity["reason"])
        self.assertEqual(vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now, after=self.now - 30)["state"],
            "unknown")

    def test_a_current_scan_restores_the_reading(self):
        self.seed_runner(online=True, seen=self.now - 4000,
                         last_busy=self.now - 7200)
        self.n1_scan(seen=self.now - 10)
        self.assertEqual(vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now)["state"], "unknown")
        self.seed_runner(online=True, last_busy=self.now - 7200)
        activity = vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now)
        self.assertEqual(activity["state"], "ok")
        self.assertTrue(activity["online"])

    def test_a_reading_before_the_poweron_is_stale(self):
        self.seed_runner(seen=self.now - 60)
        self.assertEqual(vm.host_release.ci_runner_activity(
            self.db, HOST, now=self.now, after=self.now - 30)["state"], "stale")


class CiStopEvidenceTest(CiDomainTest):
    def evidence(self, **kw):
        on_ts = kw.pop("on_ts", self.now - 7200)
        with mock.patch.object(vm.host_release, "_canon_read",
                               return_value=([object()], False)):
            return vm.ci_stop_evidence(
                self.cfg, self.db, HOST, {}, now=self.now, quiet_s=1800,
                on_ts=on_ts, **kw)

    def test_idle_queue_and_resting_runners_prove_the_stop(self):
        self.seed_idle_queue()
        self.seed_runner(online=True, last_busy=self.now - 7200)
        footprint = self.evidence()
        self.assertEqual(footprint["state"], "idle")
        self.assertEqual(footprint["capacity"], "burst")
        self.assertEqual(footprint["idle_since"], self.now - 7200)

    def test_an_offline_runner_does_not_block_the_stop(self):
        self.seed_idle_queue()
        self.seed_runner(online=False, last_busy=self.now - 7200)
        self.assertEqual(self.evidence()["state"], "idle")

    def test_a_waiting_or_unreadable_queue_refuses_the_stop(self):
        self.seed_runner(last_busy=self.now - 7200)
        with self.assertRaisesRegex(vm.Refused, "CI queue unreadable"):
            self.evidence()
        self.seed_waiting_queue()
        with self.assertRaisesRegex(vm.Refused, "CI job waiting"):
            self.evidence()

    def test_a_busy_or_recently_busy_runner_refuses_the_stop(self):
        self.seed_idle_queue()
        self.seed_runner(busy=True, last_busy=self.now - 10)
        with self.assertRaisesRegex(vm.Refused, "CI runner busy"):
            self.evidence()
        self.db.execute("DELETE FROM ci_runner_state")
        self.seed_runner(busy=False, last_busy=self.now - 60)
        with self.assertRaisesRegex(vm.Refused, "not at rest"):
            self.evidence()

    def test_an_n1_busy_scan_ending_between_readings_refuses_the_stop(self):
        # The last current scan saw the fleet idle; an N-1 watcher then saw a
        # job busy and its end without writing `last_busy_at`. The row looks
        # idle with an old last busy, but its state stamp no longer matches
        # the scan: the stop must not read it.
        self.seed_idle_queue()
        self.seed_runner(online=True, seen=self.now - 4000,
                         last_busy=self.now - 7200)
        self.n1_scan(seen=self.now - 60, busy=True)
        self.n1_scan(seen=self.now - 10, busy=False)
        with self.assertRaisesRegex(vm.Refused, "not dated"):
            self.evidence()

    def test_the_first_current_scan_after_an_n1_write_resets_the_rest(self):
        # An N-1 watcher saw a job busy and its end; the next current scan
        # cannot know when that job ended, so it dates the rest from its own
        # scan instead of the stale last busy.
        self.seed_idle_queue()
        self.seed_runner(online=True, seen=self.now - 4000,
                         last_busy=self.now - 7200)
        self.n1_scan(seen=self.now - 60, busy=True)
        self.n1_scan(seen=self.now - 10, busy=False)
        self.scan_runner(online=True)
        activity = vm.host_release.ci_runner_activity(self.db, HOST, now=self.now)
        self.assertEqual(activity["state"], "ok")
        self.assertGreaterEqual(activity["last_busy_ts"], self.now - 1)
        with self.assertRaisesRegex(vm.Refused, "not at rest"):
            self.evidence()

    def test_no_measured_runner_refuses_the_stop(self):
        self.seed_idle_queue()
        with self.assertRaisesRegex(vm.Refused, "not measured"):
            self.evidence()

    def test_no_rest_instant_refuses_the_stop(self):
        # A runner never seen busy and no `on_since`: the quiet bound cannot
        # be proved, so the controller must not open a drain.
        self.seed_idle_queue()
        self.seed_runner(online=True)
        with self.assertRaisesRegex(vm.Refused, "rest not measured"):
            self.evidence(on_ts=None)


class CiControllerTest(CiDomainTest):
    """The worker opens the drain; no hook runs on the CI VM."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM vm_adapter_requests")
        self.db.execute("DELETE FROM vm_adapter_observations")
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("DELETE FROM host_holds")
        self.provider = mock.Mock()
        self.provider.state.return_value = "running"
        self.provider.allowed.return_value = {"poweron", "poweroff"}

    def tick(self):
        with mock.patch.object(vm.host_release, "_canon_read",
                               return_value=([object()], False)):
            return vm.tick_target(
                self.cfg, self.db,
                policy(targets={HOST: ci_target()}), HOST, self.provider)

    def test_a_resting_fleet_opens_the_drain_and_its_requests(self):
        self.seed_idle_queue()
        self.seed_runner(last_busy=self.now - 7200)
        self.observe("running", on_since=self.now - 7200)
        self.assertEqual(self.tick(), "draining requested")
        control = Queue(self.db).control(HOST)
        self.assertEqual(control["mode"], "draining")
        self.assertFalse(control["claims_open"])
        self.assertTrue(control["automatic"])
        self.assertEqual(
            [row["state"] for row in Queue(self.db).pending_all(HOST)],
            ["draining", "idle-drained"])

    def test_a_waiting_job_never_opens_the_drain(self):
        self.seed_waiting_queue()
        self.seed_runner(last_busy=self.now - 7200)
        self.observe("running", on_since=self.now - 7200)
        self.assertIn("CI job waiting", self.tick())
        self.assertIsNone(Queue(self.db).control(HOST))
        self.provider.action.assert_not_called()

    def test_a_recently_busy_runner_waits_for_the_hysteresis(self):
        self.seed_idle_queue()
        self.seed_runner(last_busy=self.now - 60)
        self.observe("running", on_since=self.now - 7200)
        self.assertIn("not at rest", self.tick())
        self.assertIsNone(Queue(self.db).control(HOST))

    def test_a_hold_and_a_stopped_vm_open_nothing(self):
        self.seed_idle_queue()
        self.seed_runner(last_busy=self.now - 7200)
        self.observe("running", on_since=self.now - 7200)
        self.st.hosts.hold(HOST, reason="test", author="tester", seconds=600)
        self.assertEqual(self.tick(), "held")
        self.assertIsNone(Queue(self.db).control(HOST))
        self.db.execute("DELETE FROM host_holds")
        self.provider.state.return_value = "stopped"
        self.assertEqual(self.tick(), "provider stopped")


class CiStopTickTest(CiDomainTest):
    """Once the control drains, the historical two-phase stop carries it."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM vm_adapter_requests")
        self.db.execute("DELETE FROM vm_adapter_observations")
        self.db.execute("DELETE FROM host_lifecycle")
        self.control()
        self.db.execute(
            "UPDATE host_lifecycle SET hook_state='draining',"
            "release_state='draining' WHERE host=%s", (HOST,))
        row = Queue(self.db).control(HOST)
        Queue(self.db).request(HOST, row["generation"], row["release_epoch"],
                               "idle-drained")
        self.observe("running", on_since=self.now - 7200)
        self.seed_idle_queue()
        self.seed_runner(last_busy=self.now - 7200)
        self.provider = mock.Mock()
        self.provider.state.return_value = "running"
        self.provider.allowed.return_value = {"poweron", "poweroff"}

    def tick(self):
        with mock.patch.object(vm.host_release, "_canon_read",
                               return_value=([object()], False)):
            return vm.tick_host(
                self.cfg, self.db, policy(targets={HOST: ci_target()}),
                HOST, self.provider, probe=mock.Mock(return_value=True))

    def test_the_stop_phases_run_on_the_ci_evidence(self):
        self.assertEqual(self.tick(), "stop prepared")
        row = Queue(self.db).pending(HOST)[0]
        self.assertEqual(row["result"], vm.STOP_PREPARED)
        self.assertEqual(self.tick(), "halt prepared")
        self.assertEqual(self.tick(), "halt sent")
        row = Queue(self.db).pending(HOST)[0]
        self.assertEqual(row["result"], vm.HALT_SENT)


class CiReturnTickTest(CiDomainTest):
    """A stopped CI host wakes on a queued job and returns on its runners."""

    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM vm_adapter_requests")
        self.db.execute("DELETE FROM vm_adapter_observations")
        self.db.execute("DELETE FROM host_lifecycle")
        self.control()
        self.db.execute(
            "UPDATE host_lifecycle SET hook_state='idle-drained',"
            "release_state='idle-drained' WHERE host=%s", (HOST,))
        # GitHub keeps the runner registration while the VM is stopped: that
        # registration is how the wake recognises the CI host (#8031).
        self.seed_runner(online=False)
        self.seed_waiting_queue()
        self.provider = mock.Mock()
        self.provider.state.return_value = "stopped"
        self.provider.allowed.return_value = {"poweron", "poweroff"}

    def tick(self):
        with mock.patch.object(vm.host_release, "_canon_read",
                               return_value=([object()], False)):
            return vm.tick_host(
                self.cfg, self.db, policy(targets={HOST: ci_target()}),
                HOST, self.provider, probe=mock.Mock(return_value=True))

    def tick_target(self):
        with mock.patch.object(vm.host_release, "_canon_read",
                               return_value=([object()], False)):
            return vm.tick_target(
                self.cfg, self.db, policy(targets={HOST: ci_target()}),
                HOST, self.provider)

    def wake(self):
        self.observe("stopped", off_since=self.now - 7200)
        self.assertEqual(self.tick(), "starting")
        self.provider.action.assert_called_once_with(
            policy(targets={HOST: ci_target()})["targets"][HOST], "poweron")
        self.db.execute(
            "UPDATE vm_adapter_observations SET provider_state='running',"
            "on_since=to_timestamp(%s),poweron_at=to_timestamp(%s),"
            "off_since=NULL WHERE host=%s",
            (self.now - 60, self.now - 120, HOST))
        self.provider.state.return_value = "running"

    def test_runners_online_after_the_poweron_complete_the_return(self):
        self.wake()
        self.seed_runner(online=True, seen=self.now - 10,
                         last_busy=self.now - 3600)
        self.assertEqual(self.tick(), "ready")
        control = Queue(self.db).control(HOST)
        self.assertEqual(control["mode"], "available")
        self.assertTrue(control["claims_open"])
        done = [row for row in Queue(self.db).pending_all(HOST)
                if row["state"] == "busy"]
        self.assertEqual(done, [])
        row = self.db.query(
            "SELECT result FROM vm_adapter_requests WHERE host=%s AND state='busy'",
            (HOST,))[0]
        self.assertEqual(row["result"], "runners online")

    def test_a_returned_host_opens_a_new_drain_cycle(self):
        self.wake()
        self.seed_runner(online=True, seen=self.now - 10,
                         last_busy=self.now - 7200)
        self.assertEqual(self.tick(), "ready")
        returned = Queue(self.db).control(HOST)
        self.assertEqual(returned["mode"], "available")
        # `ci_ready` reopens the claims before `complete_return`: an available
        # CI control with claims open is the normal start of the next cycle,
        # not a concurrent wave.
        self.assertTrue(returned["claims_open"])
        self.db.execute(
            "UPDATE vm_adapter_observations SET on_since=to_timestamp(%s) "
            "WHERE host=%s", (self.now - 7200, HOST))
        self.seed_idle_queue()
        self.assertEqual(self.tick_target(), "draining requested")
        control = Queue(self.db).control(HOST)
        self.assertEqual(control["mode"], "draining")
        self.assertFalse(control["claims_open"])
        self.assertGreater(control["generation"], returned["generation"])
        self.assertEqual(
            [row["state"] for row in Queue(self.db).pending_all(HOST)],
            ["draining", "idle-drained"])

    def test_offline_or_stale_runners_keep_the_return_open(self):
        self.wake()
        self.seed_runner(online=False, seen=self.now - 10)
        self.assertEqual(self.tick(), "CI runners offline")
        self.db.execute("DELETE FROM ci_runner_state")
        self.seed_runner(online=True, seen=self.now - 300)
        self.assertEqual(self.tick(), "CI runners not measured since the wake")
        self.assertEqual(Queue(self.db).control(HOST)["mode"], "draining")


if __name__ == "__main__":
    unittest.main()
