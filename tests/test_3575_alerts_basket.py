# SPDX-License-Identifier: AGPL-3.0-only
"""#3575: the non-blocking defects left by the #3568 alerts work, grouped.

Four witnesses, one per basket line:

* `election.alerts_delegate` has hysteresis: the **last executor** keeps the
  alerts pass while its shared `notify_liveness` beat is fresh and it can
  push, so two routed fallbacks no longer alternate at every capacity
  tie-break, each with its own local notify state;
* `election._notify_reach` does not publish `notify_ok` for a host that has
  the route but not the `ameesh-role-alerts.timer` (missing, stale or
  disabled): the delegate must be a host that will actually run the pass;
* `role_run._stop_child` escalates on the **process group**, not on the
  leader's exit status: a descendant that ignores TERM is SIGKILLed after
  the grace, even when the leader exited on TERM or was already reaped;
* `role_run.live` exposes the responsible human the hysteresis reads, so the
  timer and `ameesh role status` compute the same delegate.
"""
from __future__ import annotations

import dataclasses
import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ameesh import election, notify_gap, registry, role_run, role_timers, storage

from .support import PgTestCase

NAME = "pilote-3575"
NTFY = {"routes": {"human:owner": ["ntfy"]},
        "channels": {"ntfy": {"url": "https://ntfy.example", "topic": "t"}}}


class _ElectionCase(PgTestCase):
    """Three candidate hosts and one elected role with a route-less holder."""

    HOST = "banc"

    def setUp(self) -> None:
        super().setUp()
        self.cfg = dataclasses.replace(self.cfg, host=self.HOST)
        for table in ("role_standings", "role_handover", "agent_candidates",
                      "canon_state", "host_lifecycle", "host_holds",
                      "credential_health", "host_resources", "merge_locks",
                      "notify_liveness", "alerts_delegations"):
            self.db.execute("DELETE FROM " + table)
        # the role's harness need not exist on the bench (same fixture as the
        # election tests: the witness is about the route, not the harness)
        adapter = mock.patch("ameesh.adapters.adapter_for", return_value=object())
        adapter.start()
        self.addCleanup(adapter.stop)
        env = mock.patch.object(role_timers, "unit_env", return_value={})
        env.start()
        self.addCleanup(env.stop)

    def role(self, *, releve="election", hosts=("banc", "atelier", "forge"),
             responsible="human:owner"):
        registry.upsert(self.db, NAME, harness="claude", host=hosts[0], cwd="/work")
        self.db.execute(
            "UPDATE agent_registry SET releve = %s, candidats = %s::text[],"
            " responsible = %s WHERE name = %s",
            (releve, "{%s}" % ",".join(hosts), responsible, NAME))
        return registry.get(self.db, NAME)

    def standing(self, host: str, *, notify_ok: bool, score: float = 90.0,
                 ready: bool = True, evaluated_ts: float | None = None,
                 diagnostic: str = "") -> None:
        self.db.execute(
            "INSERT INTO role_standings"
            " (name, host, evaluated_at, availability, capacity, score, ready,"
            "  diagnostic, notify_ok, notify_diagnostic)"
            " VALUES (%s, %s, coalesce(to_timestamp(%s), clock_timestamp()),"
            "         %s, 0.5, %s, %s, %s, %s, %s)",
            (NAME, host, evaluated_ts, score, score + 0.5, ready, diagnostic,
             notify_ok,
             "" if notify_ok else "human:owner has no route on this host"))

    def hold(self, host: str = "banc", *, epoch: int = 7,
             expires_in: float = 300.0) -> dict:
        self.db.execute(
            "UPDATE agent_registry SET host = %s, lease_owner = 'runner-test',"
            " lease_epoch = %s, lease_expires_at = clock_timestamp()"
            " + make_interval(secs => %s) WHERE name = %s",
            (host, epoch, expires_in, NAME))
        return registry.get(self.db, NAME)

    def beat(self, host: str, *, age: float = 0.0,
             human: str = "human:owner") -> None:
        """The shared liveness beat the alerts pass writes on its host."""
        storage.of(self.db).notify_liveness.beat(
            host, [human], now=time.time() - float(age))

    def decide(self, *, holder="banc"):
        return election.alerts_delegate(
            self.db, registry.get(self.db, NAME), holder=holder)


class LastExecutorTest(_ElectionCase):
    """The fallback no longer alternates: the freshest executor keeps it."""

    def routed_pair(self):
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        # atelier sorts before forge and scores higher: without a beat it is
        # the election's first candidate that can push.
        self.standing("atelier", notify_ok=True, score=95.0)
        self.standing("forge", notify_ok=True, score=90.0)

    def test_the_last_executor_keeps_the_pass_over_the_capacity_tie_break(self):
        self.routed_pair()
        first = self.decide()
        self.assertEqual(first["delegate"], "atelier")
        self.assertIsNone(first["last_executor"])
        self.beat("forge", age=5.0)
        second = self.decide()
        self.assertEqual(second["delegate"], "forge")
        self.assertEqual(second["last_executor"], "forge")
        self.assertIn("last executor forge", second["reason"])
        # The other candidate ran last: it takes the pass back, as expected.
        self.beat("atelier", age=1.0)
        third = self.decide()
        self.assertEqual(third["delegate"], "atelier")
        self.assertEqual(third["last_executor"], "atelier")

    def test_a_stale_beat_is_not_evidence_of_a_running_pass(self):
        self.routed_pair()
        self.beat("forge", age=notify_gap.ALIVE_S + 60.0)
        self.assertEqual(self.decide()["delegate"], "atelier")
        # A fresh beat on the other candidate always wins over a stale one.
        self.beat("forge", age=30.0)
        self.beat("atelier", age=notify_gap.ALIVE_S + 5.0)
        self.assertEqual(self.decide()["delegate"], "forge")

    def test_the_holder_route_still_takes_precedence(self):
        self.routed_pair()
        self.beat("forge", age=1.0)
        self.standing("banc", notify_ok=True)
        decision = self.decide()
        self.assertEqual(decision["delegate"], "banc")
        self.assertTrue(decision["holder_notify_ok"])
        self.assertIn("holder can push", decision["reason"])

    def test_a_deleted_beat_keeps_the_election_order(self):
        self.routed_pair()
        self.beat("forge", age=5.0)
        self.db.execute("DELETE FROM notify_liveness")
        self.assertEqual(self.decide()["delegate"], "atelier")

    def test_the_wrapper_reads_the_same_last_executor(self):
        self.routed_pair()
        self.beat("forge", age=2.0)
        report = role_run.live(self.db, NAME)
        self.assertEqual(report["responsible"], "human:owner")
        decision = role_run._alerts_delegation(self.db, report, host="forge")
        self.assertEqual(decision["delegate"], "forge")
        self.assertEqual(decision["last_executor"], "forge")


class NotifyReachTest(unittest.TestCase):
    """A route alone is not a delegate: the host must have the timer."""

    def reach(self, *, humans=(["human:owner"], ""), gap="", timer_error=None):
        cfg = SimpleNamespace(notify=NTFY)
        role = {"name": "mesh-design", "responsible": "human:owner"}
        with mock.patch.object(role_timers, "notify_reach", return_value=humans), \
                mock.patch.object(role_timers, "alerts_timer_gap",
                                  return_value=gap, side_effect=timer_error) as timer:
            result = election._notify_reach(cfg, role)
        return result, timer

    def test_a_route_and_an_installed_timer_publish_notify_ok(self):
        result, timer = self.reach()
        self.assertEqual(result, (True, ""))
        timer.assert_called_once()

    def test_a_route_without_the_alerts_timer_is_not_a_delegate(self):
        result, _ = self.reach(gap="the alerts pass would not run here: "
                                    "ameesh-role-alerts.timer is missing")
        self.assertFalse(result[0])
        self.assertIn("would not run here", result[1])

    def test_the_route_is_checked_before_the_timer(self):
        result, timer = self.reach(humans=([], "human:owner has no route on this host"))
        self.assertEqual(result, (False, "human:owner has no route on this host"))
        timer.assert_not_called()

    def test_a_broken_timer_check_is_reported_not_raised(self):
        result, _ = self.reach(timer_error=OSError("boum"))
        self.assertFalse(result[0])
        self.assertIn("alerts timer unreadable: boum", result[1])


class AlertsTimerGapTest(unittest.TestCase):
    """The unit is checked as installed, current, enabled and active."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="3575-timer-")
        self.dir = Path(self.tmp.name)
        self.spec = role_timers.TimerSpec(
            "alerts", "alerts", role_timers.ALERTS_INTERVAL_S,
            role_timers.ALERTS_DESCRIPTION)
        (self.dir / self.spec.service).write_text(
            role_timers.render_service(self.spec, role="mesh-design"),
            encoding="utf-8")
        (self.dir / self.spec.timer).write_text(
            role_timers.render_timer(self.spec), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def gap(self, **kw):
        kw.setdefault("directory", self.dir)
        kw.setdefault("apply", False)
        kw.setdefault("role", "mesh-design")
        return role_timers.alerts_timer_gap(**kw)

    def test_the_installed_unit_is_ready(self):
        self.assertEqual(self.gap(), "")

    def test_another_elected_role_reads_the_installed_role(self):
        # #4114: one alerts unit per host, rendered for mesh-design. The
        # auditeur is a candidate on the same host: its pass is the same
        # global pass, so the unit is not "out of date" for it.
        self.assertEqual(self.gap(role="auditeur"), "")
        self.assertEqual(self.gap(role="orchestrateur"), "")

    def test_another_role_still_reads_a_stale_unit(self):
        (self.dir / self.spec.service).write_text(
            role_timers.render_service(self.spec, role="mesh-design").replace(
                " --pass alerts", ""),
            encoding="utf-8")
        self.assertIn("ameesh-role-alerts.service is out of date",
                      self.gap(role="auditeur"))

    def test_another_role_still_needs_the_timer_enabled(self):
        def systemctl(*args, **kwargs):
            return SimpleNamespace(
                returncode=1 if "is-enabled" in args else 0,
                stdout="", stderr="")

        with mock.patch.object(role_timers, "_systemctl", side_effect=systemctl):
            gap = self.gap(role="auditeur", apply=True)
        self.assertIn("ameesh-role-alerts.timer is not enabled", gap)

    def test_a_unit_that_names_no_role_is_out_of_date(self):
        (self.dir / self.spec.service).write_text("# hand-written\n",
                                                  encoding="utf-8")
        self.assertIn("ameesh-role-alerts.service is out of date",
                      self.gap(role="auditeur"))

    def test_the_installed_role_is_read_from_the_service(self):
        content = (self.dir / self.spec.service).read_text(encoding="utf-8")
        self.assertEqual(role_timers.alerts_service_role(content), "mesh-design")
        self.assertEqual(role_timers.alerts_service_role(""), "")

    def test_a_missing_timer_is_named(self):
        (self.dir / self.spec.timer).unlink()
        self.assertIn("ameesh-role-alerts.timer is missing", self.gap())

    def test_a_unit_from_before_the_delegated_pass_is_out_of_date(self):
        (self.dir / self.spec.service).write_text(
            role_timers.render_service(self.spec, role="mesh-design").replace(
                " --pass alerts", ""),
            encoding="utf-8")
        self.assertIn("ameesh-role-alerts.service is out of date", self.gap())

    def test_the_role_name_is_required(self):
        self.assertIn("role name is unknown",
                      role_timers.alerts_timer_gap(role="", directory=self.dir,
                                                   apply=False))

    def test_a_disabled_timer_is_named(self):
        def systemctl(*args, **kwargs):
            return SimpleNamespace(
                returncode=1 if "is-enabled" in args else 0,
                stdout="", stderr="")

        with mock.patch.object(role_timers, "_systemctl", side_effect=systemctl):
            gap = self.gap(apply=True)
        self.assertIn("ameesh-role-alerts.timer is not enabled", gap)

    def test_an_inactive_timer_is_named(self):
        def systemctl(*args, **kwargs):
            return SimpleNamespace(
                returncode=1 if "is-active" in args else 0,
                stdout="", stderr="")

        with mock.patch.object(role_timers, "_systemctl", side_effect=systemctl):
            gap = self.gap(apply=True)
        self.assertIn("ameesh-role-alerts.timer is not active", gap)

    def test_an_enabled_and_active_timer_is_ready(self):
        with mock.patch.object(role_timers, "_systemctl") as systemctl:
            systemctl.return_value = SimpleNamespace(
                returncode=0, stdout="", stderr="")
            self.assertEqual(self.gap(apply=True), "")
        checked = [call.args[0] for call in systemctl.call_args_list]
        self.assertEqual(checked, ["is-enabled", "is-active"])

    def test_a_missing_systemctl_is_reported_not_raised(self):
        with mock.patch.object(role_timers, "_systemctl",
                               side_effect=role_timers.TimerError(
                                   "systemctl is not available")):
            gap = self.gap(apply=True)
        self.assertIn("cannot be checked: systemctl is not available", gap)


#: A child that starts a descendant, both in the child's own session:
#: the descendant takes an exclusive flock, ignores SIGTERM and stays alive;
#: the leader either waits for SIGTERM (and exits on it) or exits at once.
GROUP_CHILD = """\
import fcntl, os, pathlib, signal, subprocess, sys, time

lock, started, mode = sys.argv[1:4]

if mode == "grandchild":
    handle = open(lock, "a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path(started).write_text(str(os.getpid()))
    time.sleep(60)
    raise SystemExit(0)

def on_term(*_args):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, on_term)
subprocess.Popen([sys.executable, __file__, lock, started, "grandchild"])
deadline = time.time() + 10.0
while not pathlib.Path(started).exists() and time.time() < deadline:
    time.sleep(0.01)
if not pathlib.Path(started).exists():
    raise SystemExit(2)
if mode == "exit":
    raise SystemExit(0)
while True:
    time.sleep(0.1)
"""


class ChildGroupStopTest(unittest.TestCase):
    """#3575: the group is stopped even when the leader is already gone."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="3575-group-")
        self.dir = Path(self.tmp.name)
        self.script = self.dir / "child.py"
        self.script.write_text(GROUP_CHILD, encoding="utf-8")
        self.lock = str(self.dir / "lock")
        self.started = self.dir / "started"
        self.procs = []

    def tearDown(self) -> None:
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=5.0)
            except Exception:  # noqa: BLE001 - cleanup never fails the witness
                pass
        self.tmp.cleanup()

    def spawn(self, mode: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, str(self.script), self.lock, str(self.started), mode],
            start_new_session=True)
        self.procs.append(proc)
        deadline = time.time() + 10.0
        while time.time() < deadline and not self.started.exists():
            time.sleep(0.01)
        self.assertTrue(self.started.exists(), "the descendant did not start")
        self.assertTrue(role_run._group_alive(proc.pid))
        return proc

    def lock_is_free(self) -> bool:
        handle = open(self.lock, "a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            handle.close()

    def test_the_descendant_ignores_term_before_it_announces_itself(self):
        # #5030: `spawn` returns on the announcement; a TERM sent in the gap
        # killed the descendant at once, and the grace looked unhonoured.
        grandchild = GROUP_CHILD.split('if mode == "grandchild":')[1]
        self.assertLess(
            grandchild.index("signal.SIG_IGN"),
            grandchild.index("write_text"),
            "the descendant announces itself before ignoring TERM")

    def test_a_descendant_that_ignores_term_is_killed_after_the_leader(self):
        proc = self.spawn("leader")
        began = time.monotonic()
        role_run._stop_child(proc, 0.25)
        self.assertGreaterEqual(time.monotonic() - began, 0.2,
                                "the group stop did not honour its grace")
        self.assertIsNotNone(proc.poll(), "the leader is still alive")
        self.assertTrue(self.lock_is_free(),
                        "a descendant that ignores TERM survived the group stop")

    def test_a_reaped_leader_does_not_hide_its_descendants(self):
        proc = self.spawn("exit")
        self.assertIsNotNone(proc.wait(timeout=10.0),
                             "the leader did not exit")
        # killpg(pid, …) is used directly: os.getpgid(pid) would fail here.
        self.assertTrue(role_run._group_alive(proc.pid))
        role_run._stop_child(proc, 0.25, pgid=proc.pid)
        self.assertTrue(self.lock_is_free(),
                        "a descendant survived after its leader was reaped")

    def test_the_execution_guard_keeps_the_group_it_started(self):
        class Lease:
            kill_grace = 0.25

            def remaining(self):
                return 30.0

        execution = role_run._AlertsExecution(Lease())
        proc = execution.start_child(
            [sys.executable, str(self.script), self.lock, str(self.started),
             "leader"], os.environ.copy())
        self.procs.append(proc)
        deadline = time.time() + 10.0
        while time.time() < deadline and not self.started.exists():
            time.sleep(0.01)
        self.assertTrue(self.started.exists(), "the descendant did not start")
        self.assertEqual(execution.pgid, proc.pid)
        execution.kill()
        self.assertTrue(self.lock_is_free(),
                        "the execution guard lost the descendant's group")


if __name__ == "__main__":
    unittest.main()
