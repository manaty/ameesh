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
from tests import test_1155_staging as fixture


def row(ident, run, state="running", deadline=None):
    return dict(id=ident, data=dict(run=run, sha="a" * 40, state=state, started=1,
                                    deadline=deadline or time.time() + 600))


class InFlightTest(unittest.TestCase):
    def test_running_before_deadline_blocks_unless_exempt(self):
        now = time.time()
        rows = [row(1, "a"), row(2, "late", deadline=now - 1),
                row(3, "done"), row(4, "done", state="completed")]
        self.assertEqual(staging_watch.in_flight(rows, now=now), ["a"])
        self.assertEqual(staging_watch.in_flight(rows, now=now, exempt={"a"}), [])


class ExclusiveRecordTest(unittest.TestCase):
    def setUp(self):
        self.rows = []
        self.written = []
        self.tx = mock.Mock()
        self.db = types.SimpleNamespace(transaction=lambda: nullcontext(self.tx))
        store = types.SimpleNamespace(events=types.SimpleNamespace(
            latest_type=lambda *a: self.rows,
            record=lambda *a: self.written.append(a) or 7))
        patch = mock.patch.object(staging_watch.storage, "of", return_value=store)
        patch.start()
        self.addCleanup(patch.stop)
        self.snapshot = row(0, "mine")["data"]

    def test_start_refused_while_another_path_runs(self):
        self.rows = [row(1, "manual")]
        with self.assertRaises(staging_watch.Busy):
            staging_watch.record(self.db, self.snapshot, exclusive=True)
        self.assertEqual(self.written, [])
        self.assertIn("pg_advisory_xact_lock", self.tx.query.call_args.args[0])

    def test_own_run_and_proved_stopped_run_do_not_block(self):
        self.rows = [row(1, "mine"), row(2, "old")]
        staging_watch.record(self.db, self.snapshot, exclusive=True, supersede=["old"])
        self.assertEqual(len(self.written), 1)

    def test_cli_exits_busy_without_writing(self):
        self.rows = [row(1, "manual")]
        import json
        with mock.patch("ameesh.config.load"), \
             mock.patch("ameesh.db.connect", return_value=mock.Mock(
                 transaction=self.db.transaction)):
            code = staging_watch.main([json.dumps(self.snapshot), "--exclusive"])
        self.assertEqual(code, staging_watch.BUSY)
        self.assertEqual(self.written, [])


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
