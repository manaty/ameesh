# SPDX-License-Identifier: AGPL-3.0-only
"""#4880: independent detection of an interrupted staging sequence."""
import tempfile
import unittest
from unittest import mock

from ameesh import alert_briefs, staging_watch
from tests import test_1155_staging as fixture


def row(ident=1, run="run-a", state="running", **extra):
    data = dict(run=run, sha="a" * 40, started=100, deadline=200,
                state=state, notify="pilot", hosts={
                    "vm-1": {"stage": "sante", "verdict": "ok"}})
    data.update(extra)
    return dict(id=ident, data=data)


def request_row(ident, request, *, sha="a" * 40, state="failed", **extra):
    data = dict(request=request, sha=sha, state=state, notify="pilot")
    data.update(extra)
    return dict(id=ident, object="staging-request:mesh", data=data)


class WatchTest(unittest.TestCase):
    def test_fixed_deadline_survives_launcher_loss_and_reports_each_host(self):
        rows = [row(), row(2, hosts={
            "vm-1": {"stage": "sante", "verdict": "ok"},
            "vm-2": {"stage": "installer", "verdict": "ok"}})]
        self.assertEqual(staging_watch.findings(rows, now=199), [])
        finding, = staging_watch.findings(rows, now=201)
        self.assertTrue(finding["urgent"])
        self.assertEqual(finding["orchestrators"], ["pilot"])
        self.assertIn("vm-1: sante/ok", finding["detail"])
        self.assertIn("vm-2: installer/ok", finding["detail"])
        routed = alert_briefs.briefs(finding, [dict(name="pilot", status="idle", lease_live=True)],
                                     orchestrator_names=["pilot"])
        self.assertEqual(list(routed), ["pilot"])
        self.assertIn("vm-1: sante/ok", routed["pilot"])

    def test_failure_immediate_and_only_later_completed_retry_resolves(self):
        failed = row(2, state="failed")
        self.assertEqual(len(staging_watch.findings([failed], now=150)), 1)
        old_success = row(1, run="old", state="completed")
        self.assertEqual(len(staging_watch.findings([old_success, failed], now=150)), 1)
        new_success = row(3, run="new", state="completed", sha="b" * 40)
        self.assertEqual(staging_watch.findings([failed, new_success], now=250), [])

    def test_partial_subset_keeps_other_host_failure_until_completed(self):
        failed = row(1, state="failed", hosts={
            "vm-2": {"stage": "installer", "verdict": "ko"}})
        partial = row(2, run="subset", state="partial", sha="b" * 40,
                      hosts={"vm-1": {"stage": "sante", "verdict": "ok"}})
        self.assertEqual(len(staging_watch.findings([failed, partial], now=250)), 1)
        completed = row(3, run="full", state="completed", sha="c" * 40)
        self.assertEqual(staging_watch.findings([failed, partial, completed], now=250), [])
        completed["data"]["notify"] = "other-pilot"
        self.assertEqual(len(staging_watch.findings([failed, partial, completed], now=250)), 1)

    def test_unattested_success_is_not_a_completed_run(self):
        self.assertEqual(len(staging_watch.findings(
            [row(state="unattested")], now=150)), 1)
        for state in ("completed", "partial"):
            self.assertEqual(staging_watch.findings([row(state=state)], now=250), [])

    def test_local_writer_imports_without_platform_shadowing(self):
        import os
        import subprocess
        import sys
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour/staging.sh').read_text()
        function = source.split('py_sequence() {', 1)[1].split('\n# #3981', 1)[0]
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            venv = Path(folder)
            (venv / 'bin').mkdir()
            (venv / 'bin/python').symlink_to(sys.executable)
            command = 'py_sequence() {' + function + '\npy_sequence "$SUIVI_PY" --help'
            proc = subprocess.run(['bash', '-c', command], capture_output=True, text=True,
                                  env=dict(os.environ, OUTILS_DISTANTS='0', VENV=str(venv),
                                           DEPOT=str(root), SUIVI_PY=str(root / 'src/ameesh/staging_watch.py')))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('public staging snapshot', proc.stdout)

    def test_record_drops_private_fields_and_validates_deadline(self):
        with mock.patch.object(staging_watch.storage, "of") as factory:
            data = row()["data"]
            data["secret"] = "must never persist"
            data["hosts"]["vm-1"]["output"] = "private output"
            staging_watch.record(None, data)
            saved = factory.return_value.events.record.call_args.args[3]
            self.assertNotIn("secret", saved)
            self.assertNotIn("output", saved["hosts"]["vm-1"])
            for deadline in (99, float("inf"), float("nan")):
                with self.assertRaises(ValueError):
                    staging_watch.record(None, dict(data, deadline=deadline))

    def test_truncated_history_cannot_resolve_existing_alerts(self):
        with mock.patch.object(staging_watch.storage, "of") as factory:
            factory.return_value.events.latest_type.return_value = [row()] * staging_watch.LIMIT
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                staging_watch.alerts(None, now=250)

    def alerts(self, *, sequence=(), attested=(), requests=(), now=250):
        newest = {}
        for item in requests:
            key = item["object"]
            if key not in newest or item["id"] > newest[key]["id"]:
                newest[key] = item
        with mock.patch.object(staging_watch.storage, "of") as factory:
            factory.return_value.events.latest_type.side_effect = [
                list(sequence), list(attested), list(newest.values())]
            factory.return_value.events.recent_type.return_value = list(requests)
            return staging_watch.alerts(None, now=now)

    def test_later_successful_request_on_new_commit_settles_old_failure(self):
        self.assertEqual(self.alerts(requests=[
            request_row(2, "new", sha="b" * 40, state="completed"),
            request_row(1, "old")]), [])

    def test_later_partial_does_not_mask_an_earlier_failure(self):
        alert, = self.alerts(requests=[
            request_row(2, "subset", sha="b" * 40, state="partial"),
            request_row(1, "old")])
        self.assertTrue(alert["urgent"])
        self.assertIn("request old", alert["detail"])
        self.assertIn("ultérieure", alert["detail"])
        self.assertEqual(alert["reason"], "staging-request:mesh")

    def test_blocked_request_is_kept_behind_a_later_partial(self):
        alert, = self.alerts(requests=[
            request_row(2, "subset", sha="b" * 40, state="partial"),
            request_row(1, "blocked", state="pending",
                        blocked_reason="candidate deployment access missing")])
        self.assertIn("candidate deployment access missing", alert["detail"])

    def test_attestation_settles_a_request_whose_publication_failed(self):
        self.assertEqual(self.alerts(
            attested=[dict(id=2, data=dict(sha="a" * 40))],
            requests=[request_row(1, "old")]), [])

    def test_running_snapshot_is_not_interrupted_when_the_commit_is_attested(self):
        running = row(2, run="run-a", state="running", sha="a" * 40)
        attested = [dict(id=3, data=dict(sha="a" * 40))]
        self.assertEqual(staging_watch.findings([running], now=201,
                                                attested=attested), [])
        # An attestation older than the snapshot proves nothing about this run.
        self.assertEqual(len(staging_watch.findings(
            [running], now=201, attested=[dict(id=1, data=dict(sha="a" * 40))])), 1)

    def test_request_is_observable_even_if_launcher_dies_before_first_step(self):
        alert, = self.alerts(
            requests=[request_row(1, "request-a", state="pending", deadline=200)],
            now=201)
        self.assertTrue(alert["urgent"])
        self.assertIn("deadline exceeded", alert["detail"])


class KilledSequenceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.StagingTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
    def test_progress_outage_after_start_does_not_interrupt_hosts(self):
        (self.fixture.state / 'scenario').write_text('suivi-off-after-start\n')
        proc = self.fixture.run_staging()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('suivi staging indisponible', proc.stderr)
        calls = (self.fixture.state / 'calls.log').read_text()
        self.assertIn('host=ameesh-agents-3 action=redemarrer', calls)

    def test_kill_after_first_host_is_seen_without_launcher(self):
        (self.fixture.state / 'scenario').write_text('kill=ameesh-agents-2:installer\n')
        proc = self.fixture.run_staging()
        self.assertEqual(proc.returncode, -9, proc.stderr)
        import json
        rows = [dict(id=i+1, data=json.loads(line)) for i, line in enumerate(
            (self.fixture.state / 'staging-snapshots.jsonl').read_text().splitlines())]
        self.assertEqual(rows[-1]['data']['state'], 'running')
        self.assertEqual(rows[-1]['data']['hosts']['ameesh-agents-1'],
                         dict(stage='sante', verdict='ok'))
        alerts = staging_watch.findings(rows, now=rows[-1]['data']['deadline'] + 1)
        self.assertEqual(len(alerts), 1)


if __name__ == '__main__':
    unittest.main()
