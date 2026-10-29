# SPDX-License-Identifier: AGPL-3.0-only
"""#4148 (B of #3167): strict host release ordering, adapter protocol and runs."""
from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from ameesh import (config, db as db_mod, host_idle, host_lifecycle,
                    host_release, registry, storage, work)

from .support import PgTestCase


class RunsEvidenceTest(unittest.TestCase):
    def test_a_real_measure_is_usable(self):
        self.assertEqual(host_release.runs_evidence(
            {"runs": 0, "runs_source": "host measure"}), (True, 0, "host measure"))
        self.assertEqual(host_release.runs_evidence(
            {"runs": 2, "runs_source": "declared registry"}), (True, 2, "declared registry"))

    def test_placeholder_or_unreadable_is_unknown_never_zero(self):
        for footprint in ({"runs": 0, "runs_source": "aucune interface de déclaration"},
                          {"runs": 0},
                          {"runs": None, "runs_source": "host measure"},
                          {"runs": -1, "runs_source": "host measure"},
                          {"runs": True, "runs_source": "host measure"},
                          {}):
            with self.subTest(footprint=footprint):
                known, count, source = host_release.runs_evidence(footprint)
                self.assertFalse(known)
                self.assertIsNone(count)
                self.assertTrue(source)


class DecisionTest(unittest.TestCase):
    def footprint(self, **extra):
        return dict(dict(state="idle", capacity="burst", baseline_guard=False,
                         unknown=[], runs=0, runs_source="host measure"), **extra)

    def test_auto_drain_requires_a_hook_a_burst_host_and_declared_zero_runs(self):
        self.assertIsNone(host_release.auto_drain_blocker(
            self.footprint(), None, hook="/hook"))
        for fp, hook, held in (
                (self.footprint(), None, None),
                (self.footprint(capacity="baseline"), "/hook", None),
                (self.footprint(baseline_guard=True), "/hook", None),
                (self.footprint(state="busy"), "/hook", None),
                (self.footprint(unknown=["stale"]), "/hook", None),
                (self.footprint(runs_source="aucune interface de déclaration"),
                 "/hook", None),
                (self.footprint(runs=1), "/hook", None),
                (self.footprint(), "/hook", {"host": "vm"})):
            with self.subTest(fp=fp, hook=hook, held=held):
                self.assertIsNotNone(host_release.auto_drain_blocker(
                    fp, None, hook=hook, held=held))
        self.assertIsNotNone(host_release.auto_drain_blocker(
            self.footprint(), {"mode": "draining"}, hook="/hook"))

    def test_release_needs_a_post_withdrawal_quiet_reading_and_no_lease(self):
        cfg = config.Config(resource_interval=60, host_release_settle=1,
                            host_release_drain_bound=3600)
        control = {"host": "vm", "requested_ts": 1000}
        reading = {"sampled_ts": 1010, "turns_in_progress": 0}
        self.assertIsNone(host_release.release_blocker(
            control, [], reading, footprint=self.footprint(), cfg=cfg, now=1030))
        for reason, listing, sample, fp in (
                ("runs unknown", [], 1010,
                 self.footprint(runs_source="aucune interface de déclaration")),
                ("declared runs", [], 1010, self.footprint(runs=1)),
                ("turn", [], 1010, self.footprint()),
                ("lease", [{"host": "vm", "lease_live": True}], 1010,
                 self.footprint()),
                ("before withdrawal", [], 900, self.footprint()),
                ("stale", [], 1, self.footprint())):
            with self.subTest(reason=reason):
                row = dict(reading, turns_in_progress=1 if reason == "turn" else 0,
                           sampled_ts=sample)
                self.assertIsNotNone(host_release.release_blocker(
                    control, listing, row, footprint=fp, cfg=cfg, now=1030))
        self.assertIn("overdue", host_release.release_blocker(
            control, [], reading, footprint=self.footprint(), cfg=cfg, now=5000))

    def test_return_only_wakes_a_host_its_agent_cannot_leave(self):
        control = {"host": "vm", "mode": "draining", "claims_open": False}
        listing = [dict(name="pinned", host="vm", mode="execute", status="idle",
                        actionable_lots=1, unread=0, admitted_hosts=["vm"]),
                   dict(name="movable", host="vm", mode="execute", status="idle",
                        actionable_lots=1, unread=0, admitted_hosts=["vm", "other"]),
                   dict(name="elected", host="vm", mode="execute", status="idle",
                        releve="election", actionable_lots=1, unread=0,
                        admitted_hosts=["vm"]),
                   dict(name="nowhere", host="vm", mode="execute", status="idle",
                        actionable_lots=1, unread=0, admitted_hosts=[])]
        self.assertEqual(host_release.return_targets(
            control, listing, {"vm": control, "other": {"host": "other",
                                                        "mode": "available"}},
            known_hosts={"vm", "other"}, holds={}), ["pinned"])
        self.assertEqual(host_release.return_targets(
            control, listing, {"vm": control},
            known_hosts={"vm"}, holds={"vm": {}}), [])


class HostReleaseTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE host_lifecycle, host_release_locks, host_resources, host_holds "
                        "CASCADE")
        self.api = storage.of(self.db)
        self.tmpdir = tempfile.mkdtemp(dir=self.tmp)
        self.log = os.path.join(self.tmpdir, "hook.log")
        self.script = os.path.join(self.tmpdir, "hook.py")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent("""\
                import os, sys, time
                state, host = sys.argv[1], sys.argv[2]
                with open(os.environ["HOOK_LOG"], "a", encoding="utf-8") as fh:
                    fh.write("%s %s %s\\n" % (state, host,
                                              os.environ.get("AMEESH_HOST_GENERATION", "")))
                fail = os.environ.get("HOOK_FAIL", "")
                if state in fail.split(","):
                    sys.exit(7)
                barrier = os.environ.get("HOOK_BARRIER")
                if barrier and state == "idle-drained":
                    open(barrier + ".started", "w", encoding="utf-8").close()
                    while not os.path.exists(barrier + ".release"):
                        time.sleep(0.05)
                """))
        self._env = {"HOOK_LOG": self.log}
        self._old = {key: os.environ.get(key)
                     for key in ("HOOK_LOG", "HOOK_FAIL", "HOOK_RUNS",
                                 "HOOK_BARRIER")}
        for key in ("HOOK_FAIL", "HOOK_RUNS", "HOOK_BARRIER"):
            os.environ.pop(key, None)
        os.environ.update(self._env)
        self.addCleanup(self._restore_env)
        self._runs = mock.patch.object(
            host_release, "runs_evidence", return_value=(True, 0, "host measure"))
        self._runs.start()
        self.addCleanup(self._runs.stop)
        self._runs_stopped = False
        self.cfg = dataclasses.replace(
            self.cfg, host="vm", resource_interval=60, idle_after_s=60,
            host_release_settle=0, host_release_drain_bound=3600,
            host_release_retry=1, host_release_interval=0,
            lifecycle_hook_timeout=5,
            lifecycle_hooks={"vm": "%s %s" % (sys.executable, self.script)})

    def _restore_env(self):
        for key, old in self._old.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    def add_agent(self, name="worker", *, host="vm", admitted=("vm",), **extra):
        registry.upsert(self.db, name, harness="deepseek", host=host,
                        cwd="/work/" + name, mode="execute")
        self.db.execute(
            "UPDATE agent_registry SET status='idle', status_since=now()-interval '1 hour', "
            "admitted_hosts=string_to_array(%s, ',') WHERE name=%s",
            (",".join(admitted) or None, name))
        for key, value in extra.items():
            self.db.execute("UPDATE agent_registry SET %s=%%s WHERE name=%%s" % key,
                            (value, name))
        return name

    def reading(self, *, host="vm", ts=None, turns=0, **extra):
        row = self.api.hosts.record(dict({
            "host": host, "turns_in_progress": turns, "cpu_count": 8,
            "mem_total_bytes": 16 * 1024 ** 3, "mem_available_bytes": 12 * 1024 ** 3,
            "app_cpu": 0.1, "load1": 0.1,
            "cgroup": {"container": False, "limits_readable": True}}, **extra))
        if ts is not None:
            self.db.execute("UPDATE host_resources SET sampled_at=to_timestamp(%s) "
                            "WHERE id=%s", (float(ts), row["id"]))
            row["sampled_ts"] = float(ts)
        return row

    def quiet_history(self, *, host="vm", now=None, idle=120):
        now = time.time() if now is None else float(now)
        for offset in (idle, idle / 2.0, 0):
            self.reading(host=host, ts=now - offset)

    def calls(self):
        try:
            with open(self.log, encoding="utf-8") as fh:
                return [line.split() for line in fh if line.strip()]
        except FileNotFoundError:
            return []

    def states(self):
        """Adapter state calls, without the read-only `runs` declarations."""
        return [call[0] for call in self.calls() if call[0] != "runs"]

    def clear_calls(self):
        open(self.log, "w", encoding="utf-8").close()

    def unknown_runs(self):
        if not self._runs_stopped:
            self._runs.stop()
            self._runs_stopped = True

    def once(self, *, local_host=None, now=None, actor="runner-test"):
        return host_release.once(self.cfg, self.db, local_host=local_host,
                                 actor=actor, now=now)

    def control(self, host="vm"):
        return self.api.host_lifecycle.get(host)

    def test_idle_burst_is_drained_then_released_and_claims_stay_closed(self):
        self.add_agent()
        self.quiet_history()
        now = time.time()
        self.once(local_host="vm", now=now)
        control = self.control()
        self.assertEqual((control["mode"], control["release_state"],
                          control["automatic"]), ("draining", "draining", True))
        self.assertFalse(control["claims_open"])
        self.assertEqual(self.states(), ["draining"])
        self.assertIsNone(registry.claim(self.db, "worker", "other", ttl_seconds=60))
        # A post-withdrawal quiet reading proves the drain, then the adapter
        # is told to release the resource.
        self.reading()
        self.once(now=time.time())
        control = self.control()
        self.assertEqual((control["release_state"], control["hook_state"]),
                         ("idle-drained", "idle-drained"))
        self.assertFalse(control["claims_open"])
        self.assertEqual([call[0] for call in self.calls()],
                         ["draining", "idle-drained"])
        self.assertIsNone(registry.claim(self.db, "worker", "other", ttl_seconds=60))
        # Replay: the journal keeps one call per generation and state.
        self.once(now=time.time() + 1)
        self.assertEqual([call[0] for call in self.calls()],
                         ["draining", "idle-drained"])

    def test_baseline_guard_and_hold_are_never_drained(self):
        self.add_agent()
        self.quiet_history()
        now = time.time()
        with mock.patch.object(host_idle, "snapshot",
                               return_value={"vm": dict(
                                   state="idle", capacity="baseline",
                                   baseline_guard=False, unknown=[],
                                   runs=0, runs_source="host measure")}):
            self.once(local_host="vm", now=now)
            self.assertIsNone(self.control())
        with mock.patch.object(host_idle, "snapshot",
                               return_value={"vm": dict(
                                   state="idle", capacity="burst",
                                   baseline_guard=True, unknown=[],
                                   runs=0, runs_source="host measure")}):
            self.once(local_host="vm", now=now)
            self.assertIsNone(self.control())
        self.api.hosts.hold("vm", reason="maintenance", author="human:op",
                            seconds=600)
        self.once(local_host="vm", now=now)
        self.assertIsNone(self.control())

    def test_unknown_runs_never_drain_and_raise_the_147_alert(self):
        self.add_agent()
        self.quiet_history()
        self.unknown_runs()
        self.once(local_host="vm", now=time.time())
        self.assertIsNone(self.control())
        alerts = host_release.alerts(self.cfg, self.db, time.time())
        self.assertEqual([alert["type"] for alert in alerts],
                         ["host_release_no_runs"])
        self.assertIn("#4147", alerts[0]["detail"])

    def test_declared_zero_runs_and_no_hold_drain_the_idle_burst_host(self):
        self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        self.assertEqual(self.control()["mode"], "draining")
        self.assertEqual(self.states(), ["draining"])

    def test_released_host_waking_is_not_lost_before_a_post_ack_reading(self):
        worker = self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        self.reading()
        self.once(now=time.time())
        self.assertEqual(self.control()["release_state"], "idle-drained")
        work.add(self.db, title="wake", assignee=worker, app="ameesh")
        self.once(now=time.time() + 1)
        self.assertEqual(self.control()["release_state"], "busy")
        self.assertFalse(self.control()["claims_open"])
        # The VM is off: its readings go stale while `busy` boots it. This is
        # not a lost host until the boot grace expires.
        self.db.execute("UPDATE host_resources SET sampled_at=now()-interval '1 hour' "
                        "WHERE host='vm'")
        changed = self.api.host_lifecycle.detect_losses(
            actor="runner", older_than=180, released_grace=900)
        self.assertEqual(changed, [])
        self.assertEqual(self.control()["mode"], "draining")
        self.db.execute("UPDATE host_lifecycle SET updated_at=now()-interval '2 hours' "
                        "WHERE host='vm'")
        changed = self.api.host_lifecycle.detect_losses(
            actor="runner", older_than=180, released_grace=60)
        self.assertEqual([row["mode"] for row in changed], ["lost"])

    def test_release_decision_is_refused_after_a_return(self):
        self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.assertEqual((control["mode"], control["hook_state"]),
                         ("draining", "draining"))
        epoch, generation = control["release_epoch"], control["generation"]
        # A return slips between the scanner's release decision and its
        # begin_transition: the decision must not survive the epoch bump.
        self.api.host_lifecycle.undrain("vm", actor="human:op", claims_open=True)
        self.assertEqual(self.control()["mode"], "returning")
        self.assertTrue(self.control()["claims_open"])
        attempt = self.api.host_lifecycle.begin_transition(
            "vm", generation, "idle-drained", release_epoch=epoch,
            actor="runner", stale_after=30, retry_after=0)
        self.assertIsNone(attempt)
        states = [row["state"] for row in self.api.host_lifecycle.transition_journal("vm")]
        self.assertEqual(states, ["draining"])

    def test_return_waits_for_an_in_flight_idle_drained_hook(self):
        worker = self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.reading()
        barrier = os.path.join(self.tmpdir, "barrier")
        os.environ["HOOK_BARRIER"] = barrier
        self.addCleanup(lambda: os.environ.pop("HOOK_BARRIER", None))
        errors = []

        def scan():
            other = db_mod.connect(self.cfg)
            try:
                host_release.once(self.cfg, other, local_host=None,
                                  actor="runner-thread", now=time.time())
            except Exception as exc:  # pragma: no cover - surfaced in the test
                errors.append(exc)
            finally:
                other.close()

        thread = threading.Thread(target=scan, daemon=True)
        thread.start()
        marker = barrier + ".started"
        deadline = time.time() + 15
        while not os.path.exists(marker) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(marker),
                        "idle-drained hook did not start: %r" % (errors,))
        # Work arrives and an operator asks for the host back while the
        # release hook is held: no claim may reopen.
        work.add(self.db, title="wake", assignee=worker, app="ameesh")
        host_lifecycle.undrain(self.cfg, self.db, "vm", actor="human:op")
        control = self.control()
        self.assertEqual(control["mode"], "draining")
        self.assertFalse(control["claims_open"])
        self.assertNotIn("busy", self.states())
        open(barrier + ".release", "w").close()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.control()["release_state"], "idle-drained")
        self.assertFalse(self.control()["claims_open"])
        # The scanner now answers `busy`; only a post-ack reading reopens.
        self.once(now=time.time())
        self.assertEqual(self.control()["mode"], "draining")
        self.assertFalse(self.control()["claims_open"])
        self.reading()
        self.once(now=time.time())
        self.assertTrue(self.control()["claims_open"])
        self.assertEqual(self.control()["mode"], "returning")
        self.assertLess(self.states().index("idle-drained"),
                        self.states().index("busy"))

    def test_scheduled_return_without_an_adapter_reopens_claims(self):
        self.add_agent()
        self.cfg = dataclasses.replace(self.cfg, lifecycle_hooks={})
        self.api.host_lifecycle.drain("vm", actor="human:op", duration=3600)
        self.db.execute("UPDATE host_lifecycle SET return_at=now()-interval '1 second' "
                        "WHERE host='vm'")
        self.api.host_lifecycle.expire_returns(open_claims=False)
        self.assertEqual(self.control()["mode"], "returning")
        self.assertFalse(self.control()["claims_open"])
        self.once(now=time.time())
        self.assertTrue(self.control()["claims_open"])

    def test_draining_hook_failure_keeps_claims_closed_and_alerts(self):
        self.add_agent()
        self.quiet_history()
        os.environ["HOOK_FAIL"] = "draining"
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.assertEqual(control["mode"], "draining")
        self.assertEqual(control["release_state"], "draining")
        self.assertFalse(control["claims_open"])
        self.assertEqual(control["release_error_state"], "draining")
        self.assertIn("exit 7", control["release_error"])
        alerts = host_release.alerts(self.cfg, self.db, time.time())
        self.assertEqual([alert["type"] for alert in alerts], ["host_release_failed"])

    def test_no_idle_drained_while_a_turn_or_a_lease_remains(self):
        worker = self.add_agent()
        lease = registry.claim(self.db, worker, "runner", ttl_seconds=300)
        self.assertTrue(registry.begin_turn(self.db, worker, "runner",
                                            lease["lease_epoch"], "active"))
        self.api.host_lifecycle.drain("vm", actor="human:op")
        self.reading(turns=1)
        self.once(now=time.time())
        self.assertEqual(self.states(), ["draining"])
        self.assertEqual(self.control()["release_state"], "draining")
        self.assertFalse(self.control()["claims_open"])
        registry.end_turn(self.db, worker, "runner", lease["lease_epoch"],
                          status="idle")
        registry.release(self.db, worker, "runner", lease["lease_epoch"])
        self.reading(turns=0)
        self.once(now=time.time())
        self.assertEqual(self.states(), ["draining", "idle-drained"])
        self.assertEqual(self.control()["release_state"], "idle-drained")

    def test_idle_drained_failure_keeps_the_previous_state(self):
        self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        os.environ["HOOK_FAIL"] = "idle-drained"
        self.reading()
        self.once(now=time.time())
        control = self.control()
        self.assertEqual(control["release_state"], "draining")
        self.assertEqual(control["hook_state"], "draining")
        self.assertEqual(control["release_error_state"], "idle-drained")
        self.assertFalse(control["claims_open"])

    def test_return_calls_busy_before_any_claim_and_waits_for_a_fresh_reading(self):
        worker = self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.reading()
        self.once(now=time.time())
        self.assertEqual(self.control()["release_state"], "idle-drained")
        self.clear_calls()
        # Work arrives for an agent admitted only there. The busy hook is
        # called; claims stay closed until a *new* reading proves the boot.
        work.add(self.db, title="wake", assignee=worker, app="ameesh")
        self.once(now=time.time() + 1)
        self.assertFalse(self.control()["claims_open"])
        self.assertEqual(self.states(), ["busy"])
        self.assertIsNone(registry.claim(self.db, worker, "runner", ttl_seconds=60))
        self.reading()
        self.once(now=time.time() + 10)
        control = self.control()
        self.assertTrue(control["claims_open"])
        self.assertEqual(control["mode"], "returning")
        self.assertEqual(self.states()[0], "busy")
        self.assertIsNotNone(registry.claim(self.db, worker, "runner", ttl_seconds=60))

    def test_busy_failure_never_reopens_claims(self):
        worker = self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.reading()
        self.once(now=time.time())
        work.add(self.db, title="wake", assignee=worker, app="ameesh")
        os.environ["HOOK_FAIL"] = "busy"
        self.once(now=time.time() + 1)
        control = self.control()
        self.assertEqual(control["mode"], "draining")
        self.assertFalse(control["claims_open"])
        self.assertEqual(control["release_error_state"], "busy")
        self.assertIsNone(registry.claim(self.db, worker, "runner", ttl_seconds=60))

    def test_scheduled_return_opens_claims_only_after_busy_and_fresh_reading(self):
        self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.reading()
        self.once(now=time.time())
        # `expire_returns` is the existing L232e path for a planned drain.
        self.db.execute("UPDATE host_lifecycle SET return_at=now()-interval '1 second' "
                        "WHERE host='vm'")
        self.api.host_lifecycle.expire_returns(open_claims=False)
        control = self.control()
        self.assertEqual(control["mode"], "returning")
        self.assertFalse(control["claims_open"])
        self.once(now=time.time() + 1)
        self.assertEqual(self.states()[-1], "busy")
        self.assertFalse(self.control()["claims_open"])
        self.reading()
        self.once(now=time.time() + 10)
        self.assertTrue(self.control()["claims_open"])

    def test_manual_undrain_uses_busy_and_reopens_only_with_a_fresh_reading(self):
        self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.reading()
        self.once(now=time.time())
        host_lifecycle.undrain(self.cfg, self.db, "vm", actor="human:op")
        control = self.control()
        self.assertEqual(control["mode"], "draining")
        self.assertFalse(control["claims_open"])
        self.assertEqual(self.states()[-1], "busy")
        self.reading()
        host_lifecycle.undrain(self.cfg, self.db, "vm", actor="human:op")
        self.assertTrue(self.control()["claims_open"])
        self.assertEqual(self.control()["mode"], "returning")

    def test_intentional_release_is_not_a_lost_host(self):
        self.add_agent()
        self.quiet_history()
        self.once(local_host="vm", now=time.time())
        control = self.control()
        self.reading()
        self.once(now=time.time())
        self.db.execute("UPDATE host_resources SET sampled_at=now()-interval '1 hour' "
                        "WHERE host='vm'")
        changed = self.api.host_lifecycle.detect_losses(
            actor="runner", older_than=180)
        self.assertEqual(changed, [])
        self.assertEqual(self.control()["mode"], "draining")

    def test_overdue_withdrawal_alerts_and_stays_closed(self):
        self.add_agent()
        self.api.host_lifecycle.drain("vm", actor="human:op")
        self.db.execute("UPDATE host_lifecycle SET requested_at=now()-interval '2 hours' "
                        "WHERE host='vm'")
        alerts = host_release.alerts(self.cfg, self.db, time.time())
        self.assertEqual([alert["type"] for alert in alerts], ["host_release_overdue"])
        self.assertIn("undrain vm", alerts[0]["detail"])


class FootprintReleaseTest(unittest.TestCase):
    def test_release_overlays_state_claims_and_declared_runs(self):
        fp = dict(state="idle", capacity="burst", runs=0,
                  runs_source="aucune interface de déclaration", unknown=[])
        control = dict(host="vm", mode="draining", generation=3,
                       release_state="idle-drained", hook_state="draining",
                       claims_open=False, automatic=True, requested_ts=10,
                       released_ts=20, runs_declared=None,
                       runs_source="aucune interface de déclaration: exit 2",
                       runs_checked_ts=21, release_error=None,
                       release_error_state=None, release_error_ts=None)
        out = host_idle.apply_release(fp, control)
        self.assertEqual(out["state"], "idle-drained")
        # runs stay A's measurement, never the adapter's
        self.assertEqual((out["runs"], out["runs_source"]),
                         (0, "aucune interface de déclaration"))
        self.assertFalse(out["release"]["claims_open"])
        self.assertNotIn("runs_unknown", out["release"])
        self.assertIn("release idle-drained", host_idle.line(out))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
