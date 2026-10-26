# SPDX-License-Identifier: AGPL-3.0-only
"""#3802 : le verrou `host-pass` ne se tient plus des heures sans le dire.

Une passe est bornée (budget compilé), rend le verrou au plafond et le dit ;
une passe sautée derrière un détenteur trop vieux nomme son pid, son runner
et son heure de prise, et l'alerte `housekeeping_blocked` le reprend. Aucun
descripteur du verrou ne survit à la passe.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from ameesh import exploitation, housekeeping as hk, mesh_cli, storage
from ameesh.housekeeping import HOST_PASS_ALERT_AFTER_S, PassBudget, _PassExpired
from ameesh.platform import session_lock

from .support import PgTestCase


def _fd_for(path: str) -> list[int]:
    """The descriptors this very process has open on `path` (witness #3802:
    a descriptor of the host lock must not survive the pass)."""
    out = []
    try:
        # hermeticity: allow — les descripteurs du processus de test lui-même
        names = os.listdir("/proc/self/fd")
    except OSError:
        return out
    for name in names:
        try:
            # hermeticity: allow — compare le lien du descripteur au verrou
            if os.readlink(os.path.join("/proc/self/fd", name)) == path:
                out.append(int(name))
        except (OSError, ValueError):
            continue
    return out


class PassBudgetTest(unittest.TestCase):
    def test_phases_are_traced_and_the_checkpoint_raises_once_expired(self):
        now = [0.0]
        budget = PassBudget(1.0, clock=lambda: now[0])
        budget.check("tmp")
        now[0] = 0.5
        budget.check("caches")
        now[0] = 1.0
        with self.assertRaises(_PassExpired) as caught:
            budget.check("worktrees")
        self.assertEqual(caught.exception.phase, "caches")
        summary = budget.summary()
        self.assertTrue(summary["timed_out"])
        self.assertEqual(summary["phases"], [
            {"phase": "tmp", "seconds": 0.5},
            {"phase": "caches", "seconds": 0.5}])

    def test_an_external_call_never_outlives_the_budget(self):
        now = [0.0]
        budget = PassBudget(10.0, clock=lambda: now[0])
        self.assertEqual(budget.timeout(20.0), 10.0)
        now[0] = 9.5
        self.assertEqual(budget.timeout(20.0), 0.5)
        now[0] = 10.0
        self.assertEqual(budget.timeout(20.0), 0.0)


class HostPassLockTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-3802-")
        self.addCleanup(temporary.cleanup)
        self.dir = os.path.join(temporary.name, ".menage")
        self.cfg = types.SimpleNamespace(state_dir=temporary.name, host="pc",
                                         dsn="", schema="public")
        self.policy = hk.Policy(worktrees=False, deps=False, artifacts=(),
                                docker_volumes=False)
        self.lock_path = hk.host_pass_lock(self.cfg)

    def passage(self, **kwargs):
        return hk.run_pass(self.cfg, object(), self.policy, host="pc", actor="test",
                           artifact_scan=False, **kwargs)

    def test_the_lock_carries_the_holder_while_held_and_not_after(self):
        seen = {}

        def during(*args, **kwargs):
            seen["holder"] = session_lock.holder(self.dir, "host-pass")
            seen["state"] = session_lock.read_state(self.dir, "host-pass")
            return {}

        with mock.patch.object(hk, "_run_pass", side_effect=during):
            report = self.passage()
        self.assertEqual(seen["holder"]["pid"], os.getpid())
        self.assertEqual(seen["holder"]["runner"], "test")
        self.assertEqual(seen["state"][session_lock.HOLDER_KEY]["pid"], os.getpid())
        self.assertIn("since", seen["holder"])
        self.assertEqual(report["budget"]["timed_out"], False)
        # released and cleared: a later pass takes it, nobody is named
        state = session_lock.read_state(self.dir, "host-pass")
        self.assertNotIn(session_lock.HOLDER_KEY, state)
        lock = session_lock.try_lock(self.dir, "host-pass")
        self.assertIsNotNone(lock)
        lock.close()

    def test_no_descriptor_of_the_lock_survives_the_pass(self):
        seen = {}

        def during(*args, **kwargs):
            seen["fds"] = _fd_for(self.lock_path)
            seen["inheritable"] = {fd: os.get_inheritable(fd) for fd in seen["fds"]}
            return {}

        with mock.patch.object(hk, "_run_pass", side_effect=during):
            self.passage()
        self.assertTrue(seen["fds"], "the pass holds the lock while it runs")
        self.assertTrue(all(value is False for value in seen["inheritable"].values()),
                        seen["inheritable"])
        if os.path.isdir("/proc/self/fd"):
            self.assertEqual(_fd_for(self.lock_path), [])

    def test_a_failed_pass_releases_and_clears_the_lock(self):
        with mock.patch.object(hk, "_run_pass", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                self.passage()
        state = session_lock.read_state(self.dir, "host-pass")
        self.assertNotIn(session_lock.HOLDER_KEY, state)
        lock = session_lock.try_lock(self.dir, "host-pass")
        self.assertIsNotNone(lock)
        lock.close()

    def test_a_skipped_pass_still_prints_the_historical_reason(self):
        """#2173 is unchanged: a pass that did not run is not "executed"."""
        held = session_lock.try_lock(self.dir, "host-pass")
        self.addCleanup(held.close)
        session_lock.write_content(held, {session_lock.HOLDER_KEY: {
            "pid": os.getpid(), "runner": "test", "since": time.time(),
            "phase": "", "source": "lock"}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            report = self.passage()
            hk.print_report(report)
        self.assertEqual(report["skipped"],
                         ["another housekeeping pass is running on this host"])
        self.assertEqual(out.getvalue(),
                         "housekeeping of pc — skipped — another housekeeping pass is "
                         "running on this host\n")


class HostPassBudgetTest(unittest.TestCase):
    """A blocked phase (a real sleeping subprocess) cannot keep the lock."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-3802-budget-")
        self.addCleanup(temporary.cleanup)
        self.dir = os.path.join(temporary.name, ".menage")
        self.cfg = types.SimpleNamespace(state_dir=temporary.name, host="pc",
                                         dsn="", schema="public")
        self.policy = hk.Policy(worktrees=False, deps=False, artifacts=(),
                                docker_volumes=False)
        self.sleep = ("import time; time.sleep(30)")

    def test_a_blocked_phase_does_not_hold_the_lock_past_the_budget(self):
        def slow(runtime, db, host, *, dry_run=False, budget=None):
            try:
                subprocess.run([sys.executable, "-c", self.sleep],
                               timeout=budget.timeout(30))
            except subprocess.TimeoutExpired:
                pass
            return {"entries": [], "plan": [], "ameesh": [], "others": [],
                    "others_total": 0}

        started = time.monotonic()
        with mock.patch.object(hk, "reap_containers", side_effect=slow):
            report = hk.run_pass(self.cfg, object(), self.policy, host="pc",
                                 actor="test", artifact_scan=False, budget_s=0.4)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertEqual(report["status"], "timed_out")
        self.assertTrue(report["timed_out"])
        self.assertIn("budget", " ".join(report["skipped"]))
        phases = [row["phase"] for row in report["budget"]["phases"]]
        self.assertIn("containers", phases)
        lock = session_lock.try_lock(self.dir, "host-pass")
        self.assertIsNotNone(lock, "the lock is released after the budget")
        lock.close()
        state = session_lock.read_state(self.dir, "host-pass")
        self.assertNotIn(hk.mesh_key(self.cfg), state,
                         "a timed-out pass does not count as a completed pass")

    def test_an_exhausted_budget_does_not_even_start_a_phase(self):
        report = hk.run_pass(self.cfg, object(), self.policy, host="pc",
                             actor="test", artifact_scan=False, budget_s=0)
        self.assertEqual(report["status"], "timed_out")
        self.assertEqual(report["budget"]["phases"], [])
        lock = session_lock.try_lock(self.dir, "host-pass")
        self.assertIsNotNone(lock)
        lock.close()


class HostPassBlockedTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees"):
            self.db.execute("DELETE FROM " + table)
        sandbox = tempfile.TemporaryDirectory(dir=self.tmp)
        self.addCleanup(sandbox.cleanup)
        self.cfgl = types.SimpleNamespace(state_dir=sandbox.name, host="pc",
                                          dsn="", schema="public")
        self.dir = os.path.join(sandbox.name, hk.MANAGED_DIR)
        self.store = storage.of(self.db).housekeeping
        self.policy = hk.Policy(worktrees=False, deps=False, artifacts=(),
                                docker_volumes=False)

    def _held(self, *, since: float | None = None, runner="runner:mty-nxl-impl-03"):
        """Hold the lock in this process and write its holder record."""
        fh = session_lock.try_lock(self.dir, "host-pass")
        self.assertIsNotNone(fh)
        self.addCleanup(fh.close)
        holder = {"pid": os.getpid(), "runner": runner,
                  "since": time.time() - HOST_PASS_ALERT_AFTER_S - 60
                  if since is None else since,
                  "host": "pc", "phase": "worktrees", "source": "lock"}
        session_lock.write_content(fh, {session_lock.HOLDER_KEY: holder})
        return fh, holder

    def _pass(self, **kwargs):
        return hk.run_pass(self.cfgl, self.db, self.policy, host="pc", actor="test",
                           artifact_scan=False, **kwargs)

    def _alerts(self, kind="housekeeping_blocked"):
        return [alert for alert in exploitation._housekeeping_alerts([], self.db,
                                                                    readings=[])
                if alert["type"] == kind]

    def _blocked_rows(self):
        return self.db.query("SELECT * FROM housekeeping_log WHERE kind = 'host_pass'")

    def test_a_skipped_pass_past_the_budget_names_the_holder(self):
        _fh, holder = self._held()
        report = self._pass()
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["skipped"],
                         ["another housekeeping pass is running on this host"])
        self.assertEqual(report["holder"]["pid"], os.getpid())
        self.assertEqual(report["holder"]["runner"], holder["runner"])
        self.assertIn("blocked", report)
        rows = self._blocked_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["action"], "blocked")
        data = rows[0]["data"]
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["runner"], holder["runner"])
        self.assertEqual(data["phase"], "worktrees")
        self.assertAlmostEqual(float(data["since"]), holder["since"], delta=1)

    def test_one_line_per_episode_and_the_alert_names_the_holder(self):
        self._held()
        self._pass()
        self._pass()                    # within HOST_PASS_REFRESH_S: no second line
        rows = self._blocked_rows()
        self.assertEqual(len(rows), 1, rows)
        alerts = self._alerts()
        self.assertEqual(len(alerts), 1, alerts)
        alert = alerts[0]
        self.assertEqual(alert["host"], "pc")
        self.assertEqual(alert["pid"], os.getpid())
        self.assertEqual(alert["runner"], "runner:mty-nxl-impl-03")
        self.assertEqual(alert["phase"], "worktrees")
        self.assertIn("host-pass lock is held by runner:mty-nxl-impl-03", alert["detail"])
        self.assertIn("pid %d" % os.getpid(), alert["detail"])

    def test_a_short_hold_is_named_but_not_alerted(self):
        self._held(since=time.time() - 5)
        report = self._pass()
        self.assertEqual(report["holder"]["pid"], os.getpid())
        self.assertNotIn("blocked", report)
        self.assertEqual(self._blocked_rows(), [])
        self.assertEqual(self._alerts(), [])

    def test_a_later_successful_pass_closes_the_alert(self):
        self._held()
        self._pass()
        self.assertEqual(len(self._alerts()), 1)
        time.sleep(0.05)
        self.store.log("pc", [{"kind": "mesure", "action": "measured",
                               "path": os.path.join(self.cfgl.state_dir, "tmp"),
                               "bytes": 0, "detail": "housekeeping summary",
                               "data": {}}], actor="test")
        self.assertEqual(self._alerts(), [])

    def test_a_timed_out_pass_journals_its_phase_and_what_it_did(self):
        def slow(runtime, db, host, *, dry_run=False, budget=None):
            try:
                subprocess.run([sys.executable, "-c",
                                "import time; time.sleep(30)"],
                               timeout=budget.timeout(30))
            except subprocess.TimeoutExpired:
                pass
            return {"entries": [], "plan": [], "ameesh": [], "others": [],
                    "others_total": 0}

        with mock.patch.object(hk, "reap_containers", side_effect=slow):
            report = hk.run_pass(self.cfgl, self.db, self.policy, host="pc",
                                 actor="test", artifact_scan=False, budget_s=0.4)
        self.assertEqual(report["status"], "timed_out")
        rows = self._blocked_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["action"], "timed_out")
        self.assertTrue(rows[0]["data"]["timed_out"])
        self.assertIn("containers", [p["phase"] for p in rows[0]["data"]["phases"]])
        lock = session_lock.try_lock(self.dir, "host-pass")
        self.assertIsNotNone(lock)
        lock.close()

    def test_hosts_shows_the_holder_of_the_held_lock(self):
        self._held()
        self._pass()
        menage = mesh_cli._host_menage(self.store, "pc", self.policy, None, None)
        self.assertEqual(menage["host_pass"]["pid"], os.getpid())
        self.assertEqual(menage["host_pass"]["runner"], "runner:mty-nxl-impl-03")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mesh_cli._print_host_menage(menage)
        self.assertIn("host-pass lock held by runner:mty-nxl-impl-03", out.getvalue())
        self.assertIn("pid %d" % os.getpid(), out.getvalue())


@unittest.skipUnless(os.path.exists("/proc/locks"), "Linux /proc/locks")
class HolderFallbackTest(unittest.TestCase):
    def test_proc_locks_names_the_pid_when_the_record_is_missing(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-3802-proc-")
        self.addCleanup(temporary.cleanup)
        directory = os.path.join(temporary.name, ".menage")
        fh = session_lock.try_lock(directory, "host-pass")
        self.addCleanup(fh.close)
        session_lock.write_content(fh, {"some-mesh": 1})   # no holder record
        holder = session_lock.holder(directory, "host-pass")
        self.assertIsNotNone(holder)
        self.assertEqual(holder["pid"], os.getpid())
        self.assertEqual(holder["source"], "proc-locks")
        self.assertIsNotNone(holder["since"])


if __name__ == "__main__":
    unittest.main()
