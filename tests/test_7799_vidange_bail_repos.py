# SPDX-License-Identifier: AGPL-3.0-only
"""#7799 : un runner au repos rend son bail sur un hôte en vidange.

Sans ce retour, `host_release.release_blocker` refuse `idle-drained` tant
qu'un agent placé tient un bail : la séquence R1 d'un hôte où des agents sont
placés ne converge jamais. Voie (a) : entre deux tours, un runner dont l'hôte
a les prises fermées cesse de renouveler et rend son bail ; il le reprend au
retour (`returning`/`available`). La garde d'entrée nomme les agents dont le
bail vit encore.
"""
from __future__ import annotations

import dataclasses
import time
import unittest
from unittest import mock

from ameesh import host_release, registry, storage, vm_adapter
from ameesh import runner as runner_mod
from ameesh.config import Config
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


class BlockerNamesHoldersTest(unittest.TestCase):
    def test_a_live_lease_is_named_never_an_anonymous_refusal(self):
        cfg = dataclasses.replace(
            Config(), resource_interval=60, host_release_settle=1,
            host_release_drain_bound=3600)
        control = {"host": "vm", "requested_ts": 1000}
        reading = {"sampled_ts": 1010, "turns_in_progress": 0}
        listing = [
            {"host": "vm", "lease_live": True, "name": "claude1"},
            {"host": "vm", "lease_live": True, "name": "impl-11"},
            {"host": "vm", "lease_live": False, "name": "impl-12"},
            {"host": "other", "lease_live": True, "name": "elsewhere"},
        ]
        with mock.patch.object(host_release, "runs_evidence",
                               return_value=(True, 0, "host measure")):
            blocker = host_release.release_blocker(
                control, listing, reading, footprint={}, cfg=cfg, now=1030)
        self.assertEqual(blocker, "a lease is still held: claude1, impl-11")


class AdapterEntryGuardTest(unittest.TestCase):
    def test_the_halt_guard_names_the_live_lease_holders(self):
        store = mock.Mock()
        store.hosts.latest.return_value = {
            "sampled_ts": 1010, "turns_in_progress": 0}
        store.operations.listing.return_value = [
            {"host": "vm", "lease_live": True, "name": "claude1"},
            {"host": "vm", "lease_live": True, "name": "impl-15"},
        ]
        cfg = dataclasses.replace(
            Config(), resource_interval=60, host_release_settle=1,
            host_release_drain_bound=3600)
        control = {"host": "vm", "requested_ts": 1000, "mode": "draining"}
        footprint = {"capacity": "burst", "unknown": [], "state": "idle",
                     "runs": 0, "runs_source": "host measure"}
        with mock.patch.object(vm_adapter.storage, "of", return_value=store), \
                mock.patch.object(vm_adapter.host_idle, "snapshot",
                                  return_value={"vm": footprint}), \
                mock.patch.object(vm_adapter.host_lifecycle, "critical_names",
                                  return_value=[]), \
                mock.patch.object(vm_adapter.host_release, "_canon_read",
                                  return_value=([object()], False)):
            with self.assertRaises(vm_adapter.Refused) as raised:
                vm_adapter.stop_evidence(cfg, mock.Mock(), "vm", control,
                                         now=1030)
        self.assertIn("claude1", str(raised.exception))
        self.assertIn("impl-15", str(raised.exception))


class IdleRunnerReleasesLeaseTest(PgTestCase):
    """L'hôte vidangé atteint `idle-drained` avec des runners au repos."""

    def setUp(self):
        super().setUp()
        self.cfg = dataclasses.replace(
            self.cfg, host="vm", resource_interval=60, idle_after_s=60,
            host_release_settle=0, host_release_drain_bound=3600,
            host_release_retry=1, host_release_interval=0,
            lifecycle_hooks={"vm": "true"})
        self.db.execute(
            "TRUNCATE host_lifecycle, host_release_locks, host_resources, "
            "host_holds CASCADE")
        self.api = storage.of(self.db)
        self._runs = mock.patch.object(
            host_release, "runs_evidence",
            return_value=(True, 0, "host measure"))
        self._runs.start()
        self.addCleanup(self._runs.stop)

    def add_agent(self, name="worker"):
        registry.upsert(self.db, name, harness="deepseek", host="vm",
                        cwd="/work/" + name, mode="execute")
        self.db.execute(
            "UPDATE agent_registry SET status='idle', "
            "status_since=now()-interval '1 hour', "
            "admitted_hosts=string_to_array('vm', ',') WHERE name=%s",
            (name,))
        return name

    def reading(self, *, ts=None, turns=0, host="vm"):
        row = self.api.hosts.record(dict({
            "host": host, "turns_in_progress": turns, "cpu_count": 8,
            "mem_total_bytes": 16 * 1024 ** 3,
            "mem_available_bytes": 12 * 1024 ** 3,
            "app_cpu": 0.1, "load1": 0.1,
            "cgroup": {"container": False, "limits_readable": True}}))
        if ts is not None:
            self.db.execute(
                "UPDATE host_resources SET sampled_at=to_timestamp(%s) "
                "WHERE id=%s", (float(ts), row["id"]))
            row["sampled_ts"] = float(ts)
        return row

    def quiet_history(self, *, now=None, idle=120):
        now = time.time() if now is None else float(now)
        for offset in (idle, idle / 2.0, 0):
            self.reading(ts=now - offset)

    def once(self, *, local_host=None, now=None):
        return host_release.once(self.cfg, self.db, local_host=local_host,
                                 actor="runner-test", now=now)

    def control(self):
        return self.api.host_lifecycle.get("vm")

    def test_an_idle_runner_returns_its_lease_and_the_host_reaches_idle_drained(self):
        self.add_agent()
        runner = Runner(self.cfg, self.db, agents=["worker"], once=True)
        lease = registry.claim(self.db, "worker", runner.runner_id,
                               ttl_seconds=600)
        self.assertIsNotNone(lease)
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.assertEqual((control["mode"], control["release_state"]),
                         ("draining", "draining"))
        self.assertFalse(control["claims_open"])

        # The demand still has a live lease: the guard names its holder and
        # no adapter is asked for idle-drained.
        blocker = host_release.release_blocker(
            control, self.api.operations.listing(), self.reading(),
            footprint={}, cfg=self.cfg, now=time.time())
        self.assertEqual(blocker, "a lease is still held: worker")
        self.once(now=time.time())
        self.assertEqual(self.control()["release_state"], "draining")

        # Between two turns the idle runner sees the closed claims and returns
        # its lease (voie a) instead of renewing it forever.
        worker = AgentWorker(runner, registry.get(self.db, "worker"), lease)
        self.addCleanup(worker.watchdog_stop.set)
        with mock.patch.object(runner_mod, "log"), \
                mock.patch.object(runner_mod, "log_async"):
            worker.run()
        self.assertIsNone(registry.get(self.db, "worker").get("lease_owner"))

        # Post-withdrawal quiet reading: the release can now be proven.
        self.reading()
        self.once(now=time.time())
        control = self.control()
        self.assertEqual((control["release_state"], control["hook_state"]),
                         ("idle-drained", "idle-drained"))
        self.assertFalse(control["claims_open"])


if __name__ == "__main__":
    unittest.main()
