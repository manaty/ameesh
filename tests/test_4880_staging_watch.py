# SPDX-License-Identifier: AGPL-3.0-only
"""#4880: independent detection of an interrupted staging sequence."""
from pathlib import Path
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


def beating(now, *, ident=1, progress=None, beat=None, deadline=None, runners=20):
    """A #5425 snapshot: 1800 s turn cap, 900 s margin, one active host."""
    budget = 900 + runners * 1800
    return row(ident, deadline=107300, beat=now if beat is None else beat,
               progress=now if progress is None else progress,
               silence_s=900, stall_s=2700, hosts={"vm-1": {
                   "stage": "redemarrer", "verdict": "en-cours", "since": 100,
                   "deadline": deadline or 100 + budget}})


class LivenessTest(unittest.TestCase):
    def test_three_hosts_of_twenty_runners_progressing_is_never_late(self):
        # Beyond the old fixed 7200 s and the snapshot's own deadline, each
        # runner taking a full turn: a real rolling restart is not an alert.
        hosts, runners, cap = 3, 20, 1800
        started = 100
        for tick in range(hosts * runners):
            now = started + (tick + 1) * cap
            snapshot = beating(now, ident=tick + 1, progress=now - cap + 1,
                               deadline=now + 900 + runners * cap)
            snapshot["data"]["deadline"] = started + 7200
            self.assertEqual(staging_watch.findings([snapshot], now=now), [], tick)

    def test_frozen_step_without_heartbeat_is_reported_before_the_fixed_deadline(self):
        frozen = beating(1000)
        self.assertEqual(staging_watch.findings([frozen], now=1899), [])
        finding, = staging_watch.findings([frozen], now=1900)
        self.assertLess(1900, frozen["data"]["deadline"])
        self.assertIn("ni battement ni progrès depuis 15 min", finding["detail"])
        self.assertIn("vm-1: redemarrer/en-cours depuis 00:01:40Z", finding["detail"])
        self.assertEqual((finding["value"], finding["threshold"]), (900, 900))

    def test_live_launcher_without_progress_is_seen(self):
        stuck = beating(5000, beat=7699, progress=5000)
        self.assertEqual(staging_watch.findings([stuck], now=7699), [])
        finding, = staging_watch.findings([beating(5000, beat=7700)], now=7700)
        self.assertIn("lanceur vivant, aucun progrès depuis 45 min", finding["detail"])

    def test_past_its_host_budget_a_live_launcher_gets_only_the_margin(self):
        past = beating(5000, beat=5900, progress=5000, deadline=5500)
        finding, = staging_watch.findings([past], now=5900)
        self.assertEqual(finding["threshold"], 900)
        # still progressing, past the budget: no alert
        self.assertEqual(staging_watch.findings(
            [beating(5900, deadline=5500)], now=5900), [])

    def test_a_launcher_older_than_the_heartbeat_keeps_its_fixed_deadline(self):
        self.assertEqual(staging_watch.findings([row()], now=199), [])
        finding, = staging_watch.findings([row()], now=201)
        self.assertIn("délai dépassé", finding["detail"])

    def test_record_keeps_liveness_fields_and_refuses_a_non_finite_one(self):
        written = []
        store = mock.Mock()
        store.events.record = lambda *args: written.append(args[-1]) or 1
        with mock.patch.object(staging_watch.storage, "of", return_value=store):
            staging_watch.record(object(), beating(150)["data"])
            data = written[0]
            self.assertEqual(data["beat"], 150)
            self.assertEqual(data["hosts"]["vm-1"]["since"], 100.0)
            self.assertEqual(data["hosts"]["vm-1"]["deadline"], 36900 + 100.0)
            with self.assertRaises(ValueError):
                staging_watch.record(object(), dict(beating(150)["data"],
                                                    beat=float("nan")))

    def test_running_request_past_its_deadline_follows_its_sequence(self):
        request = request_row(5, "r1", state="running", run="run-a", deadline=200)
        healthy = beating(1000)
        self.assertEqual(staging_watch.request_findings(
            [request], now=1000, sequences=[healthy]), [])
        self.assertEqual(len(staging_watch.request_findings(
            [request], now=1900, sequences=[healthy])), 1)
        self.assertEqual(len(staging_watch.request_findings(
            [request], now=1000, sequences=[row()])), 1)


class MonitorTest(unittest.TestCase):
    """#5450: the single stop verdict, on a simulated clock.

    No sleep: every check gets its instant, and the reader is the run's own
    latest snapshot. The launchers only run `check()` periodically.
    """

    def setUp(self):
        self.latest = None
        patch = mock.patch.object(
            staging_watch, "latest_snapshot",
            side_effect=lambda db, run: self.latest)
        patch.start()
        self.addCleanup(patch.stop)

    def monitor(self, *, fallback=None, after_s=900):
        return staging_watch.Monitor(object(), "run-a",
                                     fallback=fallback, after_s=after_s)

    def snapshot(self, *, now, progress=None, beat=None):
        budget = 900 + 20 * 1800
        return dict(run="run-a", sha="a" * 40, state="running", started=0,
                    deadline=107300, beat=now if beat is None else beat,
                    progress=now if progress is None else progress,
                    silence_s=900, stall_s=2700, hosts={"vm-1": {
                        "stage": "redemarrer", "verdict": "en-cours",
                        "since": 100, "deadline": 100 + budget}})

    def test_a_slow_progressing_sequence_is_never_stopped(self):
        monitor = self.monitor()
        started = 100
        # Three hosts x twenty runners, one full turn each: beyond the old
        # 7200 s bound, and beyond a single host budget.
        for tick in range(3 * 20):
            now = started + (tick + 1) * 1800
            self.latest = self.snapshot(now=now)
            self.assertIsNone(monitor.check(now=now), tick)

    def test_a_frozen_sequence_is_stopped_after_the_alert_delay(self):
        monitor = self.monitor()
        self.latest = self.snapshot(now=1000)
        self.assertIsNone(monitor.check(now=1900))   # first late verdict
        self.assertIsNone(monitor.check(now=2799))   # 14 min 59 s later
        self.assertIn("ni battement ni progrès", monitor.check(now=2800))

    def test_a_recovering_sequence_restarts_the_alert_delay(self):
        monitor = self.monitor()
        self.latest = self.snapshot(now=1000)
        self.assertIsNone(monitor.check(now=1900))
        self.latest = self.snapshot(now=2000, progress=2000, beat=2000)
        self.assertIsNone(monitor.check(now=2000))
        self.latest = self.snapshot(now=2000, progress=2000, beat=2000)
        self.assertIsNone(monitor.check(now=5000))   # new first late verdict
        self.assertIsNone(monitor.check(now=5899))
        self.assertIn("ni battement ni progrès", monitor.check(now=5900))

    def test_a_launcher_without_heartbeat_is_bounded_by_the_fallback(self):
        # An older launcher publishes no beat/progress: the snapshot's own
        # 11 h deadline is the fallback, then the same operator delay.
        monitor = self.monitor()
        self.latest = dict(run="run-a", sha="a" * 40, state="running",
                           started=0, deadline=int(staging_watch.STAGING_FALLBACK_S),
                           hosts={})
        self.assertIsNone(monitor.check(now=39599))
        self.assertIsNone(monitor.check(now=39600))  # first late verdict
        self.assertIsNone(monitor.check(now=40499))
        self.assertIn("délai dépassé", monitor.check(now=40500))

    def test_the_request_fallback_judges_before_the_first_snapshot(self):
        monitor = self.monitor(fallback=dict(
            run="run-a", sha="a" * 40, state="running", started=100,
            deadline=200, hosts={}))
        self.assertIsNone(monitor.check(now=199))
        self.assertIsNone(monitor.check(now=200))
        self.assertIsNone(monitor.check(now=1099))
        self.assertIn("délai dépassé", monitor.check(now=1100))

    def test_stop_records_the_reason_and_does_not_invent_a_success(self):
        written = []
        store = mock.Mock()
        store.events.record = lambda *args: written.append(args[-1]) or 1
        self.latest = self.snapshot(now=1000)
        with mock.patch.object(staging_watch.storage, "of", return_value=store):
            self.monitor().stop("délai dépassé : figé")
        self.assertEqual(written[0]["state"], "failed")
        self.assertEqual(written[0]["reason"], "délai dépassé : figé")


class HeartbeatScriptTest(unittest.TestCase):
    """The bash side: the snapshot, and a beat that dies with its launcher."""
    ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]

    def script(self):
        return (self.ROOT / "deploy/mise-a-jour/staging.sh").read_text()

    def bloc(self, debut, fin):
        source = self.script()
        return source[source.index(debut):source.index(fin, source.index(debut))]

    def harness(self, folder, tail, **env):
        import os
        import subprocess
        source = "\n".join([
            "set -euo pipefail",
            'JOURNAL=$D/j; SUIVI_RUN=r; SHA=aaa; PRECEDENT=',
            'SUIVI_DEBUT=$(date +%s); SUIVI_DELAI=7200',
            'NOTIF_STAGING=p; DEPUIS=2000-01-01T00:00:00Z; SUIVI_COURANTE=$D/c; SUIVI_PROGRES=$D/p',
            'SUIVI_SILENCE_S=900; SUIVI_PLAFOND_TOUR_S=1800; SUIVI_BASE_HOTE_S=900',
            'SUIVI_RUNNERS_DEFAUT=20; SUIVI_BATTEMENT_S=${BEAT:-120}; SUIVI_SUIVI=$D/out',
            'SUIVI_PY=x; touch "$JOURNAL"',
            'py_sequence() { shift; printf "%s\\n" "$1" >> "$SUIVI_SUIVI"; }',
            self.bloc("valider_battement() {", "if [ \"$POSTE_ABSENT\" = 1 ]; then\n  printf"),
            self.bloc("trace() {", "\n# --- gestes par hote"),
            tail])
        return subprocess.run(["bash", "-c", source], capture_output=True, text=True,
                              timeout=30, env=dict(os.environ, D=folder, **env))

    def snapshots(self, folder):
        import json
        from pathlib import Path
        out = Path(folder) / "out"
        return [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []

    def test_step_published_with_its_start_and_host_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = self.harness(folder, """
debut_etape vm-1 redemarrer
printf '3\\n' > "$SUIVI_PROGRES"
publier_suivi running
printf '{"ts":"%s","hote":"vm-1","etape":"redemarrer","verdict":"ko","sha":"aaa"}\\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$JOURNAL"
publier_suivi failed
""")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            first, signalled, closed = self.snapshots(folder)
        step = first["hosts"]["vm-1"]
        self.assertEqual((step["stage"], step["verdict"]), ("redemarrer", "en-cours"))
        self.assertEqual(step["deadline"] - step["since"], 900 + 20 * 1800)
        # before any runner signal, the unsignalled restart is judged by its budget
        self.assertEqual(first["stall_s"], 900 + 20 * 1800)
        self.assertEqual(signalled["stall_s"], 2700)
        self.assertEqual(signalled["hosts"]["vm-1"]["deadline"] - step["since"], 900 + 3 * 1800)
        self.assertEqual(closed["hosts"]["vm-1"]["verdict"], "ko")
        self.assertNotIn("deadline", closed["hosts"]["vm-1"])
        self.assertGreater(first["beat"], 0)

    def test_backup_budget_covers_server_and_retrieval_with_one_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = self.harness(folder, """
debut_etape poste sauvegarde
printf '1\\n' > "$SUIVI_PROGRES"
publier_suivi running
""")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            snapshot = self.snapshots(folder)[-1]
        step = snapshot["hosts"]["poste"]
        self.assertGreaterEqual(step["deadline"] - step["since"], 9060 + 30 + 3600)
        self.assertGreaterEqual(snapshot["stall_s"], 9060 + 30 + 3600)

    def test_closed_step_keeps_its_start_in_the_journal(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            proc = self.harness(folder, """
debut_etape vm-1 redemarrer
printf 'vm-1\\tredemarrer\\t1000\\t0\\n' > "$SUIVI_COURANTE"
trace vm-1 redemarrer ok "fini"
publier_suivi completed
""")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            journal = [json.loads(line) for line in
                       (Path(folder) / "j").read_text().splitlines()]
            self.assertEqual(journal[0]["started"], 1000)
            closed = self.snapshots(folder)[-1]["hosts"]["vm-1"]
        self.assertEqual((closed["stage"], closed["verdict"]), ("redemarrer", "ok"))
        self.assertEqual(closed["since"], 1000)

    def test_invalid_beat_interval_is_refused_before_any_publication(self):
        from pathlib import Path
        # #5450: the floor is 1 s; a tiny positive value is still a tight loop.
        for value in ("0", "-1", "0.5", "0.999", "1e-9", "abc", "nan", "inf"):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as folder:
                    proc = self.harness(folder, "echo tail", BEAT=value)
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertIn("STAGING_BATTEMENT_S invalide", proc.stderr)
                    self.assertFalse((Path(folder) / "out").exists())
                    self.assertNotIn("tail", proc.stdout)

    def test_beat_interval_of_one_second_and_above_is_accepted(self):
        for value in ("1", "1.5"):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as folder:
                    proc = self.harness(folder, "echo valid", BEAT=value)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "valid")

    @unittest.skipUnless(__import__("pathlib").Path("/proc/self/stat").exists(),
                         "requires /proc (Linux)")
    def test_beat_refuses_a_zombie_launcher(self):
        import os
        import time as clock
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            limit = clock.monotonic() + 5
            while clock.monotonic() < limit:
                try:
                    stat = (__import__("pathlib").Path("/proc/%d/stat" % pid)).read_text()
                except OSError:
                    self.fail("zombie disappeared before the check")
                if stat.rsplit(")", 1)[1].split()[0] == "Z":
                    break
                clock.sleep(0.05)
            else:
                self.fail("child did not become a zombie")
            with tempfile.TemporaryDirectory() as folder:
                proc = self.harness(folder, """
if lanceur_vivant "$ZPID"; then echo alive; else echo dead; fi
if lanceur_vivant $$; then echo self-alive; fi
""", ZPID=str(pid))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.split(), ["dead", "self-alive"])
        finally:
            os.waitpid(pid, 0)

    def test_beat_runs_then_stops_before_the_final_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = self.harness(folder, """
lancer_battement
sleep 2.5
arreter_battement
publier_suivi completed
""", BEAT="1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            states = [snap["state"] for snap in self.snapshots(folder)]
        self.assertGreaterEqual(states.count("running"), 1)
        self.assertEqual(states[-1], "completed")
        self.assertEqual(states.count("completed"), 1)

    def test_beat_stops_with_a_killed_launcher(self):
        import os
        import signal
        import time as clock
        with tempfile.TemporaryDirectory() as folder:
            proc = self.harness(folder, """
lancer_battement
printf '%s\\n' "$SUIVI_BATTEMENT_PID" > "$D/pid"
sleep 1.5
kill -KILL $$
""", BEAT="1")
            self.assertEqual(proc.returncode, -signal.SIGKILL)
            beat = int((__import__("pathlib").Path(folder) / "pid").read_text())
            limit = clock.monotonic() + 10
            while clock.monotonic() < limit:
                # an orphan may stay a zombie under this harness's reaper
                try:
                    stat = (__import__("pathlib").Path("/proc/%d/stat" % beat)).read_text()
                except OSError:
                    break
                if stat.rsplit(")", 1)[1].split()[0] == "Z":
                    break
                clock.sleep(0.2)
            else:
                self.fail("beat outlived its launcher")
            before = len(self.snapshots(folder))
            clock.sleep(2.2)
            self.assertEqual(len(self.snapshots(folder)), before,
                             "beat kept publishing after its launcher died")

    def test_every_staged_host_step_begins_published(self):
        source = self.script()
        for stage in ("installer", "role", "redemarrer", "timers", "controler", "sante"):
            self.assertIn('debut_etape "$nom" ' + stage, source)
        self.assertIn('STAGING_PROGRES="$SUIVI_PROGRES" "$VM_SH" agents', source)
        vm = (self.ROOT / "deploy/mise-a-jour/vm.sh").read_text()
        self.assertEqual(vm.count('signaler_progres "${#restantes[@]}"'), 2)


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

    def test_a_monitored_stop_keeps_its_overdue_reason_in_the_alert(self):
        failed = row(2, state="failed", reason="délai dépassé : figé")
        finding, = staging_watch.findings([failed], now=150)
        self.assertIn("délai dépassé : figé", finding["detail"])

    def test_a_failed_request_keeps_its_overdue_reason(self):
        alert, = self.alerts(requests=[
            request_row(1, "old", reason="délai dépassé : figé")])
        self.assertIn("délai dépassé : figé", alert["detail"])

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

    def test_a_request_line_is_judged_by_its_newest_snapshot(self):
        pending = request_row(1, "same-request", state="pending", deadline=300)
        running = request_row(2, "same-request", state="running", deadline=7200)
        # The initial pending line expires at 300 s, the running one at the
        # deployment timeout: the same request must not alert at 1000 s.
        self.assertEqual(self.alerts(requests=[running, pending], now=1000), [])
        late = request_row(2, "same-request", state="running", deadline=900)
        alert, = self.alerts(requests=[late, pending], now=1000)
        self.assertIn("deadline exceeded", alert["detail"])
        # A partial terminal snapshot supersedes the late running one.
        partial = request_row(3, "same-request", state="partial", deadline=900)
        self.assertEqual(self.alerts(requests=[partial, late, pending], now=1000), [])

    def test_busy_key_escalates_history_instead_of_blinding_alerts(self):
        completed = request_row(204, "new", sha="b" * 40, state="completed")
        old = [request_row(i, "old-%d" % i, sha="c" * 40, state="partial")
               for i in range(1, 204)]
        rows = [completed] + old
        calls = []

        def recent(kind, days, limit, object=None):
            calls.append(limit)
            return rows[:limit]

        with mock.patch.object(staging_watch.storage, "of") as factory:
            factory.return_value.events.latest_type.side_effect = [
                [], [], [completed]]
            factory.return_value.events.recent_type.side_effect = recent
            self.assertEqual(staging_watch.alerts(None, now=250), [])
        self.assertEqual(calls, [staging_watch.HISTORY_LIMIT,
                                 staging_watch.HISTORY_LIMIT * 2])


class HostRepairTest(unittest.TestCase):
    """#5558: a failure is settled host by host by later descendant runs."""

    OK = {"stage": "sante", "verdict": "ok"}
    DEFERRED = {"stage": "absent", "verdict": "reporte"}
    VMS = ("ameesh-agents-1", "ameesh-agents-2", "ameesh-agents-3",
           "manaty-mesh-1")

    @staticmethod
    def descends(ancestor, descendant):
        return (ancestor, descendant) in {("a" * 40, "b" * 40),
                                          ("a" * 40, "a" * 40)}

    def failed(self, hosts):
        return row(1, run="failed-run", state="failed", hosts=hosts)

    def later(self, ident, hosts, *, sha="b" * 40, state="partial"):
        return row(ident, run="later-%d" % ident, state=state, sha=sha,
                   notify="executor", hosts=hosts)

    def test_partial_run_with_workstation_deferred_clears_the_failure(self):
        failed = self.failed({"ameesh-agents-1": self.OK,
                              "ameesh-agents-2": {"stage": "controler",
                                                  "verdict": "ko"}})
        later = self.later(2, dict({vm: self.OK for vm in self.VMS},
                                   **{"pc-smichea": self.DEFERRED}))
        self.assertEqual(staging_watch.findings(
            [failed, later], now=250, descends=self.descends), [])
        request = request_row(3, "r1", run="failed-run")
        self.assertEqual(staging_watch.request_findings(
            [request], now=250, sequences=[failed, later],
            descends=self.descends), [])

    def test_failure_on_the_workstation_is_cleared_once_it_is_deferred(self):
        failed = self.failed({"pc-smichea": {"stage": "redemarrer",
                                             "verdict": "ko"}})
        later = self.later(2, {"ameesh-agents-1": self.OK,
                               "pc-smichea": self.DEFERRED})
        self.assertEqual(staging_watch.findings(
            [failed, later], now=250, descends=self.descends), [])

    def test_unrepaired_host_keeps_the_alert(self):
        failed = self.failed({"vm-2": {"stage": "installer", "verdict": "ko"}})
        later = self.later(2, {"vm-1": self.OK,
                               "vm-2": {"stage": "sante", "verdict": "ko"}})
        self.assertEqual(len(staging_watch.findings(
            [failed, later], now=250, descends=self.descends)), 1)
        request = request_row(3, "r1", run="failed-run")
        self.assertEqual(len(staging_watch.request_findings(
            [request], now=250, sequences=[failed, later],
            descends=self.descends)), 1)
        # the host repaired by a second later run settles it
        again = self.later(4, {"vm-2": self.OK})
        self.assertEqual(staging_watch.findings(
            [failed, later, again], now=250, descends=self.descends), [])

    def test_non_descendant_or_earlier_run_does_not_clear(self):
        failed = self.failed({"vm-1": {"stage": "installer", "verdict": "ko"}})
        other = self.later(2, {"vm-1": self.OK}, sha="c" * 40)
        self.assertEqual(len(staging_watch.findings(
            [failed, other], now=250, descends=self.descends)), 1)
        earlier = dict(self.later(0, {"vm-1": self.OK}))
        self.assertEqual(len(staging_watch.findings(
            [failed, earlier], now=250, descends=self.descends)), 1)

    def test_failure_before_any_host_needs_a_healthy_later_host(self):
        failed = self.failed({})
        deferred_only = self.later(2, {"pc-smichea": self.DEFERRED})
        self.assertEqual(len(staging_watch.findings(
            [failed, deferred_only], now=250, descends=self.descends)), 1)
        healthy = self.later(3, {"vm-1": self.OK})
        self.assertEqual(staging_watch.findings(
            [failed, deferred_only, healthy], now=250,
            descends=self.descends), [])

    @staticmethod
    def git(repo, *args):
        import subprocess
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c",
             "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
             *args], check=True, capture_output=True, text=True).stdout.strip()

    def test_installed_layout_proves_ancestry_in_the_deployment_clone(self):
        """The sealed venv's site-packages is not a repository: the clone
        beside the venv (`~/.local/share/ameesh/src`) proves the ancestry."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clone = root / "src"
            clone.mkdir()
            self.git(clone, "init", "-q")
            self.git(clone, "commit", "-q", "--allow-empty", "-m", "a")
            old = self.git(clone, "rev-parse", "HEAD")
            self.git(clone, "commit", "-q", "--allow-empty", "-m", "b")
            new = self.git(clone, "rev-parse", "HEAD")
            (root / "venv-1234567").mkdir()
            with mock.patch.object(staging_watch.sys, "prefix",
                                   str(root / "venv-1234567")):
                repos = staging_watch.ancestry_repos()
            self.assertEqual(repos[0], str(clone.resolve()))
            staging_watch._DESCENDS.clear()
            descends = staging_watch.git_descends(repos)
            self.assertTrue(descends(old, new))
            self.assertFalse(descends(new, old))
            failed = row(1, run="failed-run", state="failed", sha=old,
                         hosts={"vm-1": {"stage": "installer", "verdict": "ko"}})
            later = self.later(2, {"vm-1": self.OK,
                                   "pc-smichea": self.DEFERRED}, sha=new)
            self.assertEqual(staging_watch.findings(
                [failed, later], now=250, descends=descends), [])
            # without the deployment clone, nothing proves it: alert kept
            staging_watch._DESCENDS.clear()
            self.assertEqual(len(staging_watch.findings(
                [failed, later], now=250,
                descends=staging_watch.git_descends([str(root / "venv-1234567")]))), 1)
            # a proof is cached, a refusal is not
            self.assertTrue(descends(old, new))
            with mock.patch("ameesh.canon.git_contains") as contains:
                self.assertTrue(descends(old, new))
            contains.assert_not_called()
        staging_watch._DESCENDS.clear()

    def test_declared_clone_comes_first_and_alerts_use_the_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch("ameesh.migrations.checkout_for",
                            return_value=folder) as checkout:
                repos = staging_watch.ancestry_repos(cfg="cfg")
            checkout.assert_called_once_with("manaty/ameesh", "cfg")
            self.assertEqual(repos[0], folder)
        with mock.patch.object(staging_watch.storage, "of") as factory, \
                mock.patch.object(staging_watch, "ancestry_repos",
                                  return_value=[]) as found:
            events = factory.return_value.events
            events.latest_type.return_value = []
            staging_watch.alerts(None, now=250, cfg="cfg")
        found.assert_called_once_with("cfg")


class BoundsTest(unittest.TestCase):
    """#5450: the global bound, the operator delay and the unit bound agree."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_global_bound_and_units_do_not_overlap_the_next_cutoff(self):
        from ameesh import release_cutoff
        self.assertEqual(staging_watch.STAGING_FALLBACK_S, 11 * 3600)
        self.assertEqual(release_cutoff.STAGING_TIMEOUT_S,
                         staging_watch.STAGING_FALLBACK_S)
        self.assertLess(staging_watch.STAGING_FALLBACK_S
                        + staging_watch.STOP_AFTER_ALERT_S, 12 * 3600)
        for name in ("ameesh-release.service", "ameesh-release-nexlink.service",
                     "ameesh-staging.service"):
            text = (self.ROOT / "deploy/systemd" / name).read_text()
            self.assertIn("TimeoutStartSec=11h30min", text, name)
            self.assertIn("TimeoutStopSec=5min", text, name)
        script = (self.ROOT / "deploy/mise-a-jour/staging.sh").read_text()
        self.assertIn("SUIVI_DELAI=${STAGING_TIMEOUT_S:-39600}", script)

    def test_executor_default_is_the_global_fallback(self):
        import json
        from ameesh import staging_executor
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "staging.json"
            path.write_text(json.dumps({
                "key": "mesh", "project": "ameesh", "role": "pilot",
                "candidates": ["a", "b"], "repo": folder, "github": "o/r",
                "bascule_env": str(Path(folder) / "bascule.env")}))
            self.assertEqual(staging_executor.settings(str(path))["timeout_s"],
                             staging_watch.STAGING_FALLBACK_S)


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
        first = rows[-1]['data']['hosts']['ameesh-agents-1']
        self.assertEqual((first['stage'], first['verdict']), ('sante', 'ok'))
        self.assertGreater(first['since'], 0)
        # #5425: the killed step is published, with its start
        killed = rows[-1]['data']['hosts']['ameesh-agents-2']
        self.assertEqual((killed['stage'], killed['verdict']), ('installer', 'en-cours'))
        alerts = staging_watch.findings(rows, now=rows[-1]['data']['deadline'] + 1)
        self.assertEqual(len(alerts), 1)


if __name__ == '__main__':
    unittest.main()
