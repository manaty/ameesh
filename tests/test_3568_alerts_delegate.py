# SPDX-License-Identifier: AGPL-3.0-only
"""#3568: the `alerts` pass follows the route, not only the elected lease.

Witness: the lease is given to a candidate that cannot push (no `notify` route)
and the `alerts` timer of a route-capable candidate runs the pass anyway. The
route is published with the candidate's standing (`role_standings.notify_ok`,
resolved locally by `role_timers.notify_reach`); `election.alerts_delegate`
gives the pass to the holder when it can push, otherwise to the first fresh
candidate that can, in the election's own order so every reader agrees.

The missing route stays a *warning*, never a refusal (that is #3402): the host
can still hold the lease and run the other role timers, it simply does not keep
the alert delivery. The delegated run carries no lease epoch: it is not the
holder's fenced gesture, and the alerts pass writes no pilot gesture.

The selection alone does not reserve execution (review cx3): when the holder's
route returns while a delegate's child runs, both would select the holder. The
alerts pass therefore takes a shared execution lease in `merge_locks` for the
whole child, renews it, and stops the child if it is lost. The renewal is a
network call that can hang: a local monotonic deadline, fixed at acquisition
and extended only by a successful renewal, stops the child before the server
lease can be taken (review cx3, second round). The witnesses below cover the
overlap at route return, the renewal, the fail-closed loss, and a renewal
blocked past the local deadline.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from types import SimpleNamespace
from unittest import mock

from ameesh import (election, notify, registry, role_run, role_timers,
                    role_standings_pg as pg)

from .support import PgTestCase

NAME = "pilote-3568"
NTFY = {"routes": {"human:owner": ["ntfy"]},
        "channels": {"ntfy": {"url": "https://ntfy.example", "topic": "t"}}}
#: the secrets file is host state: these witnesses never read the real one
NO_ENV = {}
ALERTS_KEY = role_run.ALERTS_LEASE_KEY % NAME


class _Conn:
    """Test connection: the bench database, closed only when it owns it."""

    def __init__(self, db, *, own: bool = False):
        self.db = db
        self.own = own

    def query(self, sql, params=None):
        if params is None:
            return self.db.query(sql)
        return self.db.query(sql, params)

    def close(self):
        if self.own:
            self.db.close()


class _AlertsCase(PgTestCase):
    """Two candidate hosts (`banc`, `atelier`) and one elected role."""

    HOST = "banc"

    def setUp(self) -> None:
        super().setUp()
        self.cfg = dataclasses.replace(self.cfg, host=self.HOST)
        for table in ("role_standings", "role_handover", "agent_candidates",
                      "canon_state", "host_lifecycle", "host_holds",
                      "credential_health", "host_resources", "merge_locks"):
            self.db.execute("DELETE FROM " + table)
        # the role's harness need not exist on the bench (same fixture as the
        # election tests: the witness is about the route, not the harness)
        adapter = mock.patch("ameesh.adapters.adapter_for", return_value=object())
        adapter.start()
        self.addCleanup(adapter.stop)
        env = mock.patch.object(role_timers, "unit_env", return_value=dict(NO_ENV))
        env.start()
        self.addCleanup(env.stop)

    def role(self, *, releve="election", hosts=("banc", "atelier"),
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


class AlertsDelegateTest(_AlertsCase):
    def test_the_holder_can_push_and_keeps_the_pass(self):
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=True)
        self.standing("atelier", notify_ok=True)
        decision = election.alerts_delegate(
            self.db, registry.get(self.db, NAME), holder="banc")
        self.assertEqual(decision["delegate"], "banc")
        self.assertTrue(decision["holder_notify_ok"])
        self.assertIn("holder can push", decision["reason"])

    def test_a_holder_without_route_leaves_the_pass_to_a_routed_candidate(self):
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        decision = election.alerts_delegate(
            self.db, registry.get(self.db, NAME), holder="banc")
        self.assertEqual(decision["delegate"], "atelier")
        self.assertFalse(decision["holder_notify_ok"])
        self.assertIn("banc cannot push", decision["reason"])
        # the route is visible on every reader, not only through the delegate
        ranked = {item["host"]: item for item in election.candidacy(self.db, NAME)}
        self.assertTrue(ranked["atelier"]["notify_ok"])
        self.assertFalse(ranked["banc"]["notify_ok"])
        self.assertIn("no route", ranked["banc"]["notify_diagnostic"])

    def test_no_fresh_candidate_can_push_means_no_delegate(self):
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=False)
        decision = election.alerts_delegate(
            self.db, registry.get(self.db, NAME), holder="banc")
        self.assertIsNone(decision["delegate"])
        self.assertIn("no fresh candidate can push", decision["reason"])

    def test_a_stale_holder_standing_does_not_keep_the_pass(self):
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=True,
                      evaluated_ts=time.time() - 3600.0)
        self.standing("atelier", notify_ok=True)
        decision = election.alerts_delegate(
            self.db, registry.get(self.db, NAME), holder="banc")
        self.assertEqual(decision["delegate"], "atelier")

    def test_a_pinned_role_keeps_its_host(self):
        self.role(releve="hote", hosts=("banc",))
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        decision = election.alerts_delegate(
            self.db, registry.get(self.db, NAME), holder="banc")
        self.assertEqual(decision["delegate"], "banc")
        self.assertIn("pinned role", decision["reason"])

    def test_publish_self_records_the_local_route(self):
        self.role()
        routed = dataclasses.replace(self.cfg, notify=NTFY)
        self.assertEqual(election.publish_self(routed, self.db, "banc", force=True), 1)
        row = pg.latest(self.db, NAME)["banc"]
        self.assertTrue(row["notify_ok"], row["notify_diagnostic"])
        self.assertEqual(row["notify_diagnostic"], "")
        self.assertEqual(
            election.publish_self(dataclasses.replace(self.cfg, notify=None),
                                  self.db, "atelier", force=True), 1)
        row = pg.latest(self.db, NAME)["atelier"]
        self.assertFalse(row["notify_ok"])
        self.assertIn("human:owner has no route", row["notify_diagnostic"])


class AlertsGateTest(_AlertsCase):
    """`role run --pass alerts`: the route-capable candidate runs the pass."""

    def args(self, command, *, role: str = NAME, pass_name: str = "alerts",
             min_left: float = 0.0):
        return Namespace(role=role, pass_name=pass_name, min_left=min_left,
                         dry_run=False, json=False, command=list(command))

    def run_cli(self, cfg, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            with mock.patch.object(role_run.db_mod, "connect",
                                   return_value=_Conn(self.db)):
                code = role_run.run(cfg, args)
        return code, out.getvalue()

    def run_cli_isolated(self, cfg, args):
        """One real connection per call, as two timer processes would have."""
        original = role_run.db_mod.connect

        def connect(cfg_):
            return _Conn(original(cfg_), own=True)

        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(role_run.db_mod, "connect", side_effect=connect):
            code = role_run.run(cfg, args)
        return code, out.getvalue()

    def marker_command(self, marker: str) -> list[str]:
        return ["python3", "-c", "open(%r, 'w').write('x')" % marker]

    def test_the_lease_holder_without_route_stands_by_for_alerts(self):
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        with tempfile.TemporaryDirectory(prefix="3568-holder-") as folder:
            marker = os.path.join(folder, "ran")
            code, out = self.run_cli(self.cfg, self.args(self.marker_command(marker)))
            self.assertEqual(code, 0)
            self.assertIn("standby", out)
            self.assertIn("delegated to atelier", out)
            self.assertFalse(os.path.exists(marker))

    def test_the_route_capable_candidate_runs_the_pass_of_the_lease_holder(self):
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        with tempfile.TemporaryDirectory(prefix="3568-delegate-") as folder:
            marker = os.path.join(folder, "ran")
            code, _ = self.run_cli(
                dataclasses.replace(self.cfg, host="atelier"),
                self.args(self.marker_command(marker)))
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(marker))

    def test_the_delegate_does_not_carry_the_holders_epoch(self):
        self.role()
        self.hold("banc", epoch=7)
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        keys = ("AMEESH_ROLE", "AMEESH_ROLE_HOST", "AMEESH_ROLE_PASS",
                "AMEESH_LEASE_EPOCH", "AMEESH_LEASE_OWNER")
        with tempfile.TemporaryDirectory(prefix="3568-env-") as folder:
            target = os.path.join(folder, "env.json")
            command = ["python3", "-c",
                       "import json, os; open(%r, 'w').write(json.dumps("
                       "{key: os.environ.get(key, '') for key in %r}))"
                       % (target, keys)]
            code, _ = self.run_cli(
                dataclasses.replace(self.cfg, host="atelier"), self.args(command))
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as source:
                env = json.load(source)
        self.assertEqual(env["AMEESH_ROLE"], NAME)
        self.assertEqual(env["AMEESH_ROLE_HOST"], "atelier")
        self.assertEqual(env["AMEESH_ROLE_PASS"], "alerts")
        self.assertEqual(env["AMEESH_LEASE_EPOCH"], "")
        self.assertEqual(env["AMEESH_LEASE_OWNER"], "")

    def test_a_holder_that_can_push_keeps_its_epoch(self):
        self.role()
        self.hold("banc", epoch=11)
        self.standing("banc", notify_ok=True)
        self.standing("atelier", notify_ok=True)
        keys = ("AMEESH_ROLE", "AMEESH_ROLE_HOST", "AMEESH_ROLE_PASS",
                "AMEESH_LEASE_EPOCH")
        with tempfile.TemporaryDirectory(prefix="3568-epoch-") as folder:
            target = os.path.join(folder, "env.json")
            command = ["python3", "-c",
                       "import json, os; open(%r, 'w').write(json.dumps("
                       "{key: os.environ.get(key, '') for key in %r}))"
                       % (target, keys)]
            code, _ = self.run_cli(self.cfg, self.args(command))
            self.assertEqual(code, 0)
            with open(target, encoding="utf-8") as source:
                env = json.load(source)
        self.assertEqual(env["AMEESH_ROLE_HOST"], "banc")
        self.assertEqual(env["AMEESH_ROLE_PASS"], "alerts")
        self.assertEqual(env["AMEESH_LEASE_EPOCH"], "11")

    def test_without_a_live_lease_the_alerts_pass_stands_by(self):
        self.role()
        self.standing("banc", notify_ok=True)
        with tempfile.TemporaryDirectory(prefix="3568-free-") as folder:
            marker = os.path.join(folder, "ran")
            code, out = self.run_cli(self.cfg, self.args(self.marker_command(marker)))
            self.assertEqual(code, 0)
            self.assertIn("no live lease", out)
            self.assertFalse(os.path.exists(marker))

    def test_the_other_passes_still_require_the_lease(self):
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        with tempfile.TemporaryDirectory(prefix="3568-ci-") as folder:
            marker = os.path.join(folder, "ran")
            code, out = self.run_cli(
                dataclasses.replace(self.cfg, host="atelier"),
                self.args(self.marker_command(marker), pass_name=""))
            self.assertEqual(code, 0)
            self.assertIn("other-host", out)
            self.assertFalse(os.path.exists(marker))

    def test_an_unknown_pass_is_an_invocation_error(self):
        code, _ = self.run_cli(self.cfg, self.args(["true"], pass_name="bogus"))
        self.assertEqual(code, 2)

    def test_a_route_return_cannot_start_a_second_alerts_child(self):
        """#3568 cx3: selection is not a reservation; the child holds the lease."""
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        folder = tempfile.mkdtemp(prefix="3568-overlap-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        started = os.path.join(folder, "started")
        done = os.path.join(folder, "done")
        release = os.path.join(folder, "release")
        second = os.path.join(folder, "second")
        command = ["python3", "-c", WAIT_CHILD, started, release, done]
        first = {}

        def run_first():
            first["result"] = self.run_cli_isolated(
                dataclasses.replace(self.cfg, host="atelier"), self.args(command))

        thread = threading.Thread(target=run_first, name="alerts-first")
        thread.start()
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline and not os.path.exists(started):
                time.sleep(0.02)
            self.assertTrue(os.path.exists(started),
                            "the delegate child did not start")
            # The holder's route returns while the delegate child is running:
            # both hosts now select the holder, but only the child holds the
            # shared execution lease.
            self.standing("banc", notify_ok=True)
            code, out = self.run_cli_isolated(
                self.cfg, self.args(self.marker_command(second)))
            self.assertEqual(code, 0)
            self.assertIn("shared execution lease", out)
            self.assertFalse(os.path.exists(second))
        finally:
            # Let the first child finish only after the second attempt: a slow
            # query must not turn "the lease is held" into "the child is gone".
            open(release, "w").close()
            thread.join(timeout=20.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first["result"][0], 0)
        self.assertTrue(os.path.exists(done))
        # The lease is released with the child: the new delegate runs next.
        code, _ = self.run_cli_isolated(
            self.cfg, self.args(self.marker_command(second)))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(second))

    def test_the_shared_lease_is_exclusive_and_renewable(self):
        lease = role_run._AlertsLease(self.db, NAME, "atelier", ttl=2.0)
        other_db = role_run.db_mod.connect(self.__class__.cfg)
        other = role_run._AlertsLease(other_db, NAME, "banc", ttl=2.0)
        try:
            self.assertIsNotNone(lease.acquire())
            self.assertIsNone(other.acquire())
            # A renewal extends the expiry: an expired-at-first lease would
            # already be takeable here if the heartbeat did not run.
            self.assertTrue(lease.renew())
            self.assertIsNone(other.acquire())
            self.assertTrue(lease.release())
            self.assertIsNotNone(other.acquire())
        finally:
            lease.release()
            other.release()
            other_db.close()

    def test_a_stolen_lease_stops_the_alerts_child(self):
        """A lost lease kills the child: no second delivery can overlap."""
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        folder = tempfile.mkdtemp(prefix="3568-lost-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        started = os.path.join(folder, "started")
        done = os.path.join(folder, "done")
        release = os.path.join(folder, "release")
        command = ["python3", "-c", WAIT_CHILD, started, release, done]
        first = {}

        def run_first():
            # A short TTL relative to the bench, but long enough for the
            # selection re-reads and the child start to fit before the local
            # deadline. The watcher's next renewal is TTL/3 away.
            with mock.patch.object(role_run, "ALERTS_LEASE_TTL_S", 8.0):
                first["result"] = self.run_cli_isolated(
                    dataclasses.replace(self.cfg, host="atelier"), self.args(command))

        thread = threading.Thread(target=run_first, name="alerts-lost")
        thread.start()
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline and not os.path.exists(started):
                time.sleep(0.02)
            self.assertTrue(os.path.exists(started))
            # Steal the lease as an expiry + takeover would. The watcher must
            # notice at its next renewal, stop the child, and report an error.
            self.db.execute(
                "UPDATE merge_locks SET owner = 'other', epoch = epoch + 1"
                " WHERE target = %s", (ALERTS_KEY,))
        finally:
            thread.join(timeout=15.0)
            alive = thread.is_alive()
            if alive:  # let the child finish so the failure can be reported
                open(release, "w").close()
                thread.join(timeout=5.0)
        self.assertFalse(alive, "a stolen lease left the child running")
        self.assertEqual(first["result"][0], 1)
        self.assertFalse(os.path.exists(done))

    def test_a_blocked_renewal_cannot_overlap_the_next_host(self):
        """cx3 round 2: a suspended renewal must not leave a live child."""
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        folder = tempfile.mkdtemp(prefix="3568-suspended-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        lock = os.path.join(folder, "lock")
        first_started = os.path.join(folder, "first")
        second_started = os.path.join(folder, "second")
        overlap = os.path.join(folder, "overlap")
        first_command = ["python3", "-c", LOCK_CHILD, lock, first_started,
                         overlap, "20"]
        second_command = ["python3", "-c", LOCK_CHILD, lock, second_started,
                          overlap, "0.2"]
        original_renew = role_run._AlertsLease.renew

        def suspended_renew(lease):
            # The delegate's renewal is suspended before the server: it never
            # returns before the lease expires (the other host renews normally).
            if "atelier" in lease.owner:
                time.sleep(4.0)
                return False
            return original_renew(lease)

        first = {}

        def run_first():
            with mock.patch.object(role_run, "ALERTS_LEASE_TTL_S", 8.0), \
                    mock.patch.object(role_run._AlertsLease, "renew",
                                      suspended_renew):
                first["result"] = self.run_cli_isolated(
                    dataclasses.replace(self.cfg, host="atelier"),
                    self.args(first_command))

        thread = threading.Thread(target=run_first, name="alerts-suspended")
        thread.start()
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline and not os.path.exists(first_started):
                time.sleep(0.02)
            self.assertTrue(os.path.exists(first_started),
                            "the delegate child did not start")
            # The holder's route returns: it now selects itself, as soon as
            # the shared execution lease is free.
            self.standing("banc", notify_ok=True)
            # Wait for the database lease to expire. The first child must
            # already be dead by then, on its local deadline.
            deadline = time.time() + 15.0
            while time.time() < deadline:
                rows = self.db.query(
                    "SELECT (owner IS NULL OR expires_at < clock_timestamp())"
                    " AS free FROM merge_locks WHERE target = %s", (ALERTS_KEY,))
                if rows and rows[0]["free"]:
                    break
                time.sleep(0.05)
            else:
                self.fail("the shared execution lease never expired")
            code, _ = self.run_cli_isolated(self.cfg, self.args(second_command))
            self.assertEqual(code, 0)
            self.assertTrue(os.path.exists(second_started),
                            "the next host could not run the pass")
            self.assertFalse(os.path.exists(overlap),
                             "two alerts children ran at the same time")
        finally:
            thread.join(timeout=15.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first["result"][0], 1)

    def test_a_re_read_past_the_deadline_does_not_start_a_child(self):
        """cx3 round 2: the local deadline also covers the pre-Popen reads."""
        self.role()
        self.hold("banc")
        self.standing("banc", notify_ok=False)
        self.standing("atelier", notify_ok=True)
        folder = tempfile.mkdtemp(prefix="3568-reread-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        ran = os.path.join(folder, "ran")
        original = role_run._alerts_delegation
        calls = {"n": 0}

        def slow_reread(db, report, *, host):
            calls["n"] += 1
            # The first read chooses the delegate; the second is the re-read
            # under the execution lease and blocks past the local deadline.
            if calls["n"] >= 2:
                time.sleep(1.2)
            return original(db, report, host=host)

        with mock.patch.object(role_run, "ALERTS_LEASE_TTL_S", 1.0), \
                mock.patch.object(role_run, "_alerts_delegation", slow_reread):
            code, _ = self.run_cli_isolated(
                dataclasses.replace(self.cfg, host="atelier"),
                self.args(self.marker_command(ran)))
        self.assertEqual(code, 1)
        self.assertGreaterEqual(calls["n"], 2)
        self.assertFalse(os.path.exists(ran),
                         "a child started after the local deadline")

#: A child that stays alive until the test writes a release file: the overlap
#: witnesses must not depend on a fixed sleep finishing after a slow query.
WAIT_CHILD = (
    "import pathlib, sys, time\n"
    "started, release, done = sys.argv[1:4]\n"
    "pathlib.Path(started).write_text('x')\n"
    "while not pathlib.Path(release).exists():\n"
    "    time.sleep(0.02)\n"
    "pathlib.Path(done).write_text('x')\n"
)


#: A child that holds an exclusive `flock` until it dies. A second child that
#: can take the lock proves the first one is really gone; one that cannot
#: writes the overlap witness. Used to make "no simultaneous children" a
#: kernel fact rather than a timing guess.
LOCK_CHILD = (
    "import fcntl, os, pathlib, sys, time\n"
    "lock, started, overlap, seconds = sys.argv[1:5]\n"
    "handle = open(lock, 'w')\n"
    "try:\n"
    "    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "except BlockingIOError:\n"
    "    pathlib.Path(overlap).write_text(str(os.getpid()))\n"
    "    raise SystemExit(9)\n"
    "pathlib.Path(started).write_text(str(os.getpid()))\n"
    "time.sleep(float(seconds))\n"
)


class AlertsDeadlineTest(unittest.TestCase):
    """A suspended renewal cannot keep the child alive past the lease."""

    def test_a_blocked_renewal_stops_the_child_without_it(self):
        folder = tempfile.mkdtemp(prefix="3568-deadline-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        lock = os.path.join(folder, "lock")
        started = os.path.join(folder, "started")
        overlap = os.path.join(folder, "overlap")

        class BlockedLease:
            """No database: the renewal is suspended and never answers."""

            ttl = 1.2
            kill_grace = 0.2

            def __init__(self):
                self.acquired = time.monotonic()
                # conservative local deadline: two thirds of the TTL, so the
                # child is dead before the lease could be taken at 1.2 s
                self.deadline = self.acquired + 0.8
                self.renewing = threading.Event()
                self.renew_blocked = threading.Event()

            def remaining(self):
                return self.deadline - time.monotonic()

            def renew(self):
                self.renewing.set()
                self.renew_blocked.wait(5.0)
                return False

        lease = BlockedLease()
        execution = role_run._AlertsExecution(lease)
        execution.start()
        result = {}

        def run_child():
            result["value"] = role_run._run_under_lease(
                ["python3", "-c", LOCK_CHILD, lock, started, overlap, "5"],
                os.environ.copy(), lease, execution)

        thread = threading.Thread(target=run_child, name="blocked-renewal")
        thread.start()
        try:
            deadline = time.time() + 5.0
            while time.time() < deadline and not os.path.exists(started):
                time.sleep(0.01)
            self.assertTrue(os.path.exists(started), "the child did not start")
            proc = execution.proc
            deadline = time.time() + 3.0
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.01)
            self.assertIsNotNone(proc.poll(),
                                 "the child outlived the local deadline")
            self.assertLess(time.monotonic(), lease.acquired + 1.2,
                            "the child was still alive at the database expiry")
            # The stop did not wait for the renewal: it is still blocked.
            self.assertTrue(lease.renewing.is_set())
            self.assertFalse(lease.renew_blocked.is_set())
            self.assertTrue(thread.is_alive())
            # The kernel lock is free: a second child would start now.
            handle = open(lock, "w")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                handle.close()
        finally:
            lease.renew_blocked.set()
            thread.join(timeout=5.0)
            execution.close()
        self.assertFalse(thread.is_alive())
        # The child was stopped by the watchdog (a signal), and the caller
        # sees the failed execution lease; the wrapper maps it to exit 1.
        self.assertTrue(result["value"][1])
        self.assertLess(result["value"][0], 0)
        self.assertFalse(os.path.exists(overlap))


class AlertsUnitTest(unittest.TestCase):
    """The generated unit asks for the delegated pass; the others do not."""

    def render(self, name: str) -> str:
        spec = role_timers.TimerSpec(
            name, name, 30, "ameesh — %s under the elected role lease" % name)
        return role_timers.render_service(spec, role="mesh-design")

    def test_the_alerts_service_declares_its_delegated_pass(self):
        text = self.render("alerts")
        self.assertIn("role run mesh-design --pass alerts -- "
                      "%s notify --once --only alerts" % role_timers.DEFAULT_BIN,
                      text)

    def test_the_observation_services_keep_the_lease_only_gate(self):
        for name in ("issues", "github-watch", "ci"):
            with self.subTest(name=name):
                self.assertNotIn("--pass", self.render(name))

    def test_a_delegated_pass_does_not_open_the_github_host_gate(self):
        cfg = SimpleNamespace(host="atelier")
        env = {"AMEESH_ROLE": "mesh-design", "AMEESH_ROLE_HOST": "atelier",
               "AMEESH_ROLE_PASS": "alerts"}
        with mock.patch.dict(os.environ, env):
            self.assertFalse(role_run.role_host_authorized(cfg))
        with mock.patch.dict(os.environ, dict(env, AMEESH_ROLE_PASS="")):
            self.assertTrue(role_run.role_host_authorized(cfg))


class DelegatePushesTest(unittest.TestCase):
    """The delegate's notifier pushes a pending decision on its own route."""

    def test_the_alerts_pass_pushes_from_the_routed_candidate(self):
        sent = []

        class Sender:
            def send(self, channel, message):
                sent.append((channel.name, message.title))
                return None

        cfg = SimpleNamespace(host="atelier", state_dir=tempfile.gettempdir())
        notifier = notify.Notifier(cfg, notify.parse_config(NTFY), sender=Sender(),
                                   only={"alerts"}, clock=lambda: 5000.0)
        decision = {"schema": "ameesh-alert/1", "type": "decision_pending",
                    "agent": "mesh-design", "lot": 3568,
                    "responsible": "human:owner",
                    "detail": "decision #3568 pending", "since": 4900.0}
        with mock.patch.object(notifier, "_brief_agents"):
            notifier.run_pass(None, current=[decision])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "ntfy")


if __name__ == "__main__":
    unittest.main()
