# SPDX-License-Identifier: AGPL-3.0-only
"""#4885: one staging sequence at a time across paths and hosts.

The elected executor (#4880) holds a shared execution lease; a manual or
synchronous `staging.sh` does not. Both now start through an exclusive write
in the shared `staging.sequence` log. Simulated hosts only.
"""
import time
import types
import unittest
from contextlib import nullcontext
from unittest import mock

from ameesh import staging_watch
from ameesh import storage
from tests import test_1155_staging as fixture
from tests.support import PgTestCase


def row(ident, run, state="running", deadline=None):
    return dict(id=ident, data=dict(run=run, sha="a" * 40, state=state, started=1,
                                    deadline=deadline or time.time() + 600))


class InFlightTest(unittest.TestCase):
    def test_late_run_still_blocks_until_terminal_or_exempt(self):
        now = time.time()
        rows = [row(1, "a"), row(2, "late", deadline=now - 1),
                row(3, "done"), row(4, "done", state="completed")]
        self.assertEqual(list(staging_watch.in_flight(rows)), ["a", "late"])
        self.assertEqual(list(staging_watch.in_flight(rows, exempt={"a", "late"})), [])


class SharedLog:
    """One shared `staging.sequence` log, seen by two launcher hosts.

    The exclusive start reads it with plain SQL in the transaction: the
    storage of a package installed before #4880 has only `events.record`.
    """
    def __init__(self):
        self.rows = []
        self.locks = []

    def transaction(self):
        return nullcontext(self)

    def query(self, sql, params):
        if "pg_advisory_xact_lock" in sql:
            self.locks.append(params)
            return [{}]
        self.assertion(sql, params)
        return self.latest_type()

    @staticmethod
    def assertion(sql, params):
        assert "distinct on (object)" in sql and params[0] == staging_watch.EVENT

    def latest_type(self):
        latest = {}
        for r in self.rows:
            latest[r["object"]] = r
        return list(latest.values())

    def record(self, kind, obj, project, data):
        self.rows.append(dict(id=len(self.rows) + 1, object=obj, data=data))
        return len(self.rows)


class ExclusiveRecordTest(unittest.TestCase):
    def setUp(self):
        self.log = SharedLog()
        # pre-#4880 installed storage: no `latest_type` on events
        store = types.SimpleNamespace(events=types.SimpleNamespace(record=self.log.record))
        patch = mock.patch.object(staging_watch.storage, "of", return_value=store)
        patch.start()
        self.addCleanup(patch.stop)

    def start(self, run, deadline=None, **kw):
        snapshot = row(0, run, deadline=deadline)["data"]
        return staging_watch.record(self.log, snapshot, exclusive=True, **kw)

    def test_live_launcher_past_deadline_refuses_second_host(self):
        # Host A: a manual launcher still alive after its deadline.
        self.start("host-a-run", deadline=time.time() + 1)
        self.log.rows[-1]["data"]["deadline"] = time.time() - 60
        # Host B (manual or elected) is refused before any mutation.
        with self.assertRaises(staging_watch.Busy):
            self.start("host-b-run")
        self.assertEqual(len(self.log.rows), 1)
        self.assertEqual(self.log.locks, [(staging_watch.EVENT,)] * 2)
        # The deadline still alerts the pilot.
        self.assertEqual(len(staging_watch.findings(self.log.rows, now=time.time())), 1)

    def test_proved_stop_supersedes_and_closes_old_run(self):
        self.start("host-a-run")
        self.start("host-b-run", supersede=["host-a-run"])
        states = {r["data"]["run"]: r["data"]["state"] for r in self.log.latest_type()}
        self.assertEqual(states, {"host-a-run": "failed", "host-b-run": "running"})
        # Once B ends, A no longer blocks a third start.
        staging_watch.record(self.log, dict(row(0, "host-b-run", state="completed")["data"]))
        self.start("host-c-run")
        self.assertEqual(staging_watch.findings(self.log.rows, now=time.time()), [])

    def test_truncated_history_is_refused_as_busy(self):
        self.log.rows = [dict(id=i, object="staging-run:%d" % i, data=row(0, str(i), state="completed")["data"])
                         for i in range(staging_watch.LIMIT)]
        with self.assertRaises(staging_watch.Busy):
            self.start("mine")

    def test_cli_exits_busy_without_writing(self):
        self.start("manual")
        import json
        with mock.patch("ameesh.config.load"), \
             mock.patch("ameesh.db.connect", return_value=mock.Mock(
                 transaction=self.log.transaction)):
            code = staging_watch.main([json.dumps(row(0, "mine")["data"]), "--exclusive"])
        self.assertEqual(code, staging_watch.BUSY)
        self.assertEqual(len(self.log.rows), 1)


class ExclusiveStartPgTest(PgTestCase):
    """The plain-SQL exclusive start against a migrated schema."""
    def test_late_run_blocks_then_proved_stop_supersedes(self):
        def snap(run):
            # deadline long past: a late run must still block
            return dict(run=run, sha="a" * 40, state="running", started=1,
                        deadline=2, notify="pilot", hosts={})
        staging_watch.record(self.db, snap("host-a"), exclusive=True)
        with self.assertRaises(staging_watch.Busy):
            staging_watch.record(self.db, snap("host-b"), exclusive=True)
        staging_watch.record(self.db, snap("host-b"), exclusive=True, supersede=["host-a"])
        latest = {r["data"]["run"]: r["data"]["state"] for r in
                  storage.of(self.db).events.latest_type(staging_watch.EVENT, 1, 10)}
        self.assertEqual(latest, {"host-a": "failed", "host-b": "running"})


class InheritedRunIdTest(unittest.TestCase):
    def test_synchronous_release_never_reuses_an_inherited_run_id(self):
        from ameesh import release_cutoff
        with mock.patch.dict("os.environ", STAGING_RUN_ID="fixed", STAGING_PREVIOUS_RUN="old"):
            env = release_cutoff._staging_env(60)
        self.assertNotIn("STAGING_RUN_ID", env)
        self.assertEqual(env["STAGING_PREVIOUS_RUN"], "old")
        self.assertEqual(env["STAGING_TIMEOUT_S"], "60")


class StagingScriptExclusiveTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.StagingTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        (self.fixture.state / 'scenario').write_text('suivi-occupe\n')

    def test_busy_shared_start_exits_75_before_any_host_operation(self):
        proc = self.fixture.run_staging()
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertIn('staging deja en cours', proc.stderr)
        calls = (self.fixture.state / 'calls.log').read_text()
        self.assertNotIn('action=installer', calls)
        self.assertNotIn('action=migrer', calls)

    def test_proved_previous_run_is_superseded(self):
        env = dict(self.fixture.env, STAGING_PREVIOUS_RUN='old-run')
        proc = self.fixture.run_staging(env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = (self.fixture.state / 'suivi-args.log').read_text().splitlines()
        self.assertEqual(args[0], '--exclusive --supersede old-run')


if __name__ == "__main__":
    unittest.main()
