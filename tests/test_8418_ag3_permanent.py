# SPDX-License-Identifier: AGPL-3.0-only
"""#8418 (tranche 1 de #8029) : AG3 en permanent.

La carte Host déclare le hook de l'adaptateur et `runs_interface: guest`
(#7687), `lend` conservé. Le témoin suit la chaîne réelle : le scanner
automatique (`_auto_drain`, hôte burst au repos) ouvre un drainage
`automatic`, l'adaptateur acquitte `idle-drained`, le worker arrête la VM,
puis une cause de réveil existante (courrier, lot assigné, file CI,
`return_ts`) redemande le retour. Aucun cloud réel : le fournisseur et les
commandes de la VM sont des doubles, comme dans les témoins #6252/#6276.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import (canon, config, host_idle, host_lifecycle, host_release,
                    registry, storage, vm_adapter as vm)
from ameesh.storage.postgres.vm_adapter import Queue
from tests.support import PgTestCase, scaled
from tests.test_5925_vm_adapter import TARGET, policy
from tests.test_l43_perimetre_hote import _Canons, host as host_yaml

HOST = "ameesh-agents-3"
HOOK = "@ameesh-python -m ameesh.vm_adapter request"


def card():
    """The AG3 policy the home card declares (the shape under test)."""
    return SimpleNamespace(
        title=HOST,
        policy=SimpleNamespace(lifecycle_hook=HOOK, runs_interface="guest",
                               lend=object()),
        fiche=SimpleNamespace(data={}),
    )


class CardTest(_Canons, unittest.TestCase):
    """The card shape is the one `canon.validate` and `host_idle` read."""

    def test_guest_and_hook_are_accepted_on_the_lending_card(self):
        text = host_yaml("bob", resources_yaml=(
            "  runs_interface: guest\n"
            "  lifecycle_hook: \"%s\"\n"
            "  lend:\n    cpu_max: 2\n    memory_max: 9GiB\n"
            "    disk_max: 40GiB\n" % HOOK))
        loaded = canon.load(self.acme(host_text=text), default=False)
        found = canon.validate(loaded)
        self.assertEqual(canon.errors(found), [], [f.to_dict() for f in found])
        card = loaded.host("pc")
        self.assertEqual(card.policy.lifecycle_hook, HOOK)
        self.assertEqual(card.policy.runs_interface, "guest")
        self.assertIsNotNone(card.policy.lend)
        self.assertEqual(host_idle.runs_interface(config.Config(), [loaded], "pc"),
                         ("guest", "Host card"))

    def test_the_card_without_guest_stays_lending_and_blocks_the_drain(self):
        # witness: `lend` alone leaves runs unknown, so the automatic drain is
        # refused by `declared runs unknown` (the pre-#7687 state).
        text = host_yaml("bob", resources_yaml=(
            "  lifecycle_hook: \"%s\"\n"
            "  lend:\n    cpu_max: 2\n    memory_max: 9GiB\n"
            "    disk_max: 40GiB\n" % HOOK))
        loaded = canon.load(self.acme(host_text=text), default=False)
        self.assertEqual(host_idle.runs_interface(config.Config(), [loaded], "pc"),
                         (host_idle.RUNS_LENDING, "Host card"))
        fp = dict(state="idle", capacity="burst", baseline_guard=False, unknown=[],
                  runs=None, runs_source="lending")
        self.assertIsNotNone(host_release.auto_drain_blocker(fp, None, hook=HOOK))


class PermanentCycleTest(PgTestCase):
    """Scanner → adaptateur → worker → réveil, sur la base de test réelle."""

    def setUp(self):
        super().setUp()
        for table in ("vm_adapter_requests", "vm_adapter_observations",
                      "host_lifecycle", "host_resources", "host_holds",
                      "host_release_locks"):
            self.db.execute("DELETE FROM " + table)
        self.api = storage.of(self.db)
        self.q = Queue(self.db)
        self.cfg = dataclasses.replace(
            self.cfg, host=HOST, idle_after_s=1800,
            host_release_settle=0, host_release_drain_bound=3600,
            host_release_retry=1, host_release_interval=0,
            lifecycle_hook_timeout=scaled(5),
            baseline_hosts=0,
        )
        self.canon = SimpleNamespace(hosts=[card()])
        self.policy = policy(targets={HOST: dict(TARGET, probe_agent="probe-ag3")})
        self.policy.update(min_on_s=0, min_off_s=0, wake_delay_s=900)
        self.provider = mock.Mock()
        self.provider.state.return_value = "running"
        self.provider.allowed.return_value = {"poweron", "poweroff"}
        self.probe = mock.Mock(return_value=True)
        self.hook_calls = []
        self._patch = mock.patch.object(host_release, "run_hook", self.run_hook)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._canon = mock.patch.object(
            host_release, "_canon_read",
            return_value=([self.canon], False))
        self._canon.start()
        self.addCleanup(self._canon.stop)

    # -- the real hook, in-process (the subprocess is plumbing, #5925) -----
    def run_hook(self, cfg, command, state, host, *, generation=None,
                 runner=None, timeout=None, clock=time.time):
        self.hook_calls.append((state, host, generation))
        with mock.patch("ameesh.vm_adapter.config.load", return_value=self.cfg), \
                mock.patch("ameesh.vm_adapter.db_mod.connect",
                           return_value=self.db), \
                mock.patch.object(self.db, "close"), \
                mock.patch.dict(os.environ,
                                {"AMEESH_HOST_GENERATION": str(generation)}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = vm.main(["request", state, host])
        return {"state": state, "host": host, "generation": generation,
                "ok": code == 0, "code": code, "command": command,
                "stdout": "", "stderr": "", "duration_s": 0.0}

    # -- fixtures ----------------------------------------------------------
    def agent(self, name="worker"):
        registry.upsert(self.db, name, harness="deepseek", host=HOST,
                        cwd="/work/" + name, mode="execute")
        self.db.execute(
            "UPDATE agent_registry SET status='idle',"
            " status_since=now()-interval '2 hours',"
            " admitted_hosts=string_to_array(%s, ',') WHERE name=%s",
            (HOST, name))
        return name

    def reading(self, *, ts=None, guest_jobs=0, turns=0, host=HOST):
        row = self.api.hosts.record(dict({
            "host": host, "turns_in_progress": turns, "cpu_count": 8,
            "mem_total_bytes": 16 * 1024 ** 3,
            "mem_available_bytes": 12 * 1024 ** 3,
            "app_cpu": 0.1, "load1": 0.1, "guest_jobs": guest_jobs,
            "cgroup": {"container": False, "limits_readable": True}}))
        if ts is not None:
            self.db.execute(
                "UPDATE host_resources SET sampled_at=to_timestamp(%s)"
                " WHERE id=%s", (float(ts), row["id"]))
            row["sampled_ts"] = float(ts)
        return row

    def quiet_history(self, *, idle=2000, host=HOST):
        # `host_idle` only walks an uninterrupted suffix: keep every sample
        # inside `staleness_s` (3 x resource_interval, >= 180 s here).
        now = time.time()
        for offset in range(0, idle + 1, 120):
            self.reading(ts=now - offset, host=host)

    def once(self, *, local_host=None, now=None):
        return host_release.once(self.cfg, self.db, local_host=local_host,
                                 actor="runner-test", now=now)

    def control(self):
        return self.q.control(HOST)

    def tick(self):
        return vm.tick_host(self.cfg, self.db, self.policy, HOST,
                            self.provider, probe=self.probe,
                            evidence=vm.stop_evidence)

    def request(self, state):
        rows = self.db.query(
            "SELECT * FROM vm_adapter_requests WHERE host=%s AND state=%s"
            " ORDER BY created_at DESC LIMIT 1", (HOST, state))
        return rows[0] if rows else None

    def age_observation(self):
        self.db.execute(
            "UPDATE vm_adapter_observations SET"
            " on_since=clock_timestamp()-interval '2 hours',"
            " off_since=clock_timestamp()-interval '2 hours' WHERE host=%s",
            (HOST,))

    def wake_mail(self, name="worker"):
        self.db.execute(
            "INSERT INTO agent_mailbox(sender, recipient, body, kind, payload)"
            " VALUES ('test', %s, 'reveil', 'request', '{}'::jsonb)", (name,))

    # -- the witness -------------------------------------------------------
    def test_auto_drain_stop_then_mail_wakes_and_reopens_claims(self):
        worker = self.agent()
        self.quiet_history()
        self.q.observe(HOST, "running", 90)
        self.age_observation()

        # 1. the scanner starts the withdrawal by itself: automatic, closed.
        self.once(local_host=HOST, now=time.time())
        control = self.control()
        self.assertIsNotNone(control)
        self.assertEqual((control["mode"], control["automatic"],
                          control["claims_open"]), ("draining", True, False))
        # the real adapter received the draining request (queued, #5932)
        self.assertEqual(self.hook_calls[0][0], "draining")
        self.assertEqual(self.request("draining")["status"], "pending")

        # 2. the worker acknowledges the claim closure; a post-withdrawal
        #    quiet reading then allows the idle-drained request.
        self.assertEqual(self.tick(), "draining")
        # the fresh reading is stamped by the database clock; the pass runs
        # one second later so it is strictly after the withdrawal and not
        # read as a future sample (test_4148's pattern, clock-skew safe).
        self.reading()
        self.once(now=time.time() + 1)
        self.assertEqual(self.request("draining")["status"], "done")
        self.assertEqual(self.request("idle-drained")["status"], "pending")
        self.assertEqual(self.control()["mode"], "draining")
        self.assertFalse(self.control()["claims_open"])

        # 3. the worker stops the VM (halt, proof, archive) and settles.
        self.assertEqual(self.tick(), "stop prepared")
        self.assertEqual(self.tick(), "halt prepared")
        self.assertEqual(self.tick(), "halt sent")
        self.provider.state.return_value = "stopped in place"
        self.assertEqual(self.tick(), "halt proven")
        self.assertEqual(self.tick(), "archiving")
        self.provider.action.assert_called_once_with(
            dict(TARGET, probe_agent="probe-ag3"), "poweroff")
        self.provider.state.return_value = "stopped"
        self.assertEqual(self.tick(), "stopped")
        self.assertEqual(self.request("idle-drained")["status"], "done")
        self.assertEqual(self.control()["release_state"], "idle-drained")
        self.assertFalse(self.control()["claims_open"])

        # 4. an existing wake cause (unread mail for a placed agent) asks the
        #    host back through the same hook.
        self.wake_mail(worker)
        self.once(now=time.time())
        self.assertEqual(self.request("busy")["status"], "pending")
        self.assertEqual(self.hook_calls[-1][0], "busy")

        # 5. the worker powers on, admits its probe, then reopens claims on a
        #    fresh post-busy reading.
        self.assertEqual(self.tick(), "starting")
        self.provider.action.assert_called_with(
            dict(TARGET, probe_agent="probe-ag3"), "poweron")
        self.provider.state.return_value = "running"
        # the first running tick dates the boot; the host reading that proves
        # it comes after, as in the real 60 s sampler.
        self.assertEqual(self.tick(), "fresh reading pending")
        self.reading()
        self.assertEqual(self.tick(), "probe admitted")
        self.reading()  # after the probe admission
        self.assertEqual(self.tick(), "ready")
        self.assertEqual(self.request("busy")["status"], "done")
        self.reading()  # after the busy journal
        self.once(now=time.time())
        control = self.control()
        self.assertTrue(control["claims_open"])
        self.assertIsNone(self.q.return_probe(HOST))
        # the runner's return completion (the scanner only reopens claims)
        self.assertEqual(host_lifecycle.complete_returns(self.db), [HOST])
        self.assertEqual(self.control()["mode"], "available")


class WakeCausesTest(unittest.TestCase):
    """The four existing causes, read by the same worker decision."""

    def test_wake_causes_are_the_existing_ones(self):
        control = dict(host=HOST, mode="draining", automatic=True,
                       claims_open=False, return_ts=None)
        row = dict(name="worker", host=HOST, admitted_hosts=[HOST],
                   releve="hote", mode="execute", status="idle",
                   actionable_lots=0, unread=0, urgent_unread=0,
                   has_pending_prompt=False, restart_requested_ts=None)
        target = dict(TARGET, probe_agent="probe-ag3")
        now = 1_800_000_000.0

        def due(row=row, control=control, ci=None):
            return vm.wake_due(HOST, control, [row], [], {HOST: control},
                               {}, now=now, horizon=900, target=target, ci=ci)

        self.assertFalse(due())
        self.assertTrue(due(dict(row, unread=1)))                 # courrier
        self.assertTrue(due(dict(row, actionable_lots=1)))        # lot assigné
        self.assertTrue(due(ci={"state": "waiting"}))             # file CI
        self.assertTrue(due(control=dict(control, return_ts=now + 60)))  # return_ts


if __name__ == "__main__":
    unittest.main()
