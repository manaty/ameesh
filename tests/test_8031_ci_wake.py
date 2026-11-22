# SPDX-License-Identifier: AGPL-3.0-only
"""#8031 : réveil d'une VM CI sur un job en file sans runner.

La file est celle que `ci.Watcher` mesure déjà (#845, #7015, #7935) ; le lien
à l'hôte se lit sur ses runners enregistrés (nom `<hôte>-<slot>`, en ligne ou
hors ligne). Le doute (relevé périmé, étiquettes illisibles) ne réveille
jamais et interdit l'arrêt ; un relevé mesuré avant le dernier arrêt de l'hôte
ne provoque pas de réveil en boucle.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import time
import unittest
from unittest import mock

from tests.support import PgTestCase
from tests.test_5925_vm_adapter import TARGET, policy
from ameesh import ci, config, host_release, storage, vm_adapter as vm
from ameesh.storage.postgres.vm_adapter import Queue
from ameesh.vm_adapter import Refused


REPO = "manaty/ameesh"
HOST = "ameesh-ci-1"
NOW = 1_800_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def runner_row(name, labels, *, repo=REPO, seen=None):
    return {"repo": repo, "name": name, "labels": labels,
            "seen_ts": NOW - 60 if seen is None else seen}


def queue_row(*, repo=REPO, seen=None, wait_seen=None, scan_seen=None):
    seen_ts = NOW - 60 if seen is None else seen
    wait_ts = seen_ts if wait_seen is None else wait_seen
    return {"repo": repo, "queued": 1, "oldest_ts": NOW - 900,
            "seen_ts": seen_ts, "wait_seen_ts": wait_ts,
            "scan_seen_ts": wait_ts if scan_seen is None else scan_seen}


def wait_row(labels, *, repo=REPO, seen=None):
    return {"repo": repo, "labels": list(labels), "oldest_id": 11,
            "oldest_ts": NOW - 900, "seen_ts": NOW - 60 if seen is None else seen}


class CiWakeDecisionTest(unittest.TestCase):
    def test_no_registered_runner_is_inert(self):
        """Aucune VM CI inscrite : rien ne change, même avec une file pleine."""
        evidence = host_release.ci_wake(
            HOST, [], [queue_row()], [wait_row(["self-hosted", "linux", "ameesh-ci"])],
            now=NOW)
        self.assertEqual(evidence, {"state": "inert"})

    def test_a_job_the_host_serves_wakes_it(self):
        evidence = host_release.ci_wake(
            HOST,
            [runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])],
            [queue_row()], [wait_row(["self-hosted", "linux", "ameesh-ci"])],
            now=NOW)
        self.assertEqual(evidence["state"], "waiting")
        self.assertEqual(evidence["repo"], REPO)
        self.assertEqual(evidence["labels"], ["self-hosted", "linux", "ameesh-ci"])

    def test_a_job_another_host_serves_does_not_wake_this_one(self):
        evidence = host_release.ci_wake(
            HOST,
            [runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci-prio"])],
            [queue_row()], [wait_row(["self-hosted", "linux", "ameesh-ci"])],
            now=NOW)
        self.assertEqual(evidence["state"], "idle")

    def test_runner_names_are_bounded_to_the_host(self):
        """`ameesh-ci-10-1` n'est pas un runner de `ameesh-ci-1`."""
        evidence = host_release.ci_wake(
            HOST,
            [runner_row("ameesh-ci-10-1", ["self-hosted", "linux", "ameesh-ci"])],
            [queue_row()], [wait_row(["self-hosted", "linux", "ameesh-ci"])],
            now=NOW)
        self.assertEqual(evidence, {"state": "inert"})

    def test_empty_queue_is_idle(self):
        evidence = host_release.ci_wake(
            HOST,
            [runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])],
            [queue_row()], [], now=NOW)
        self.assertEqual(evidence, {"state": "idle"})

    def test_stale_or_missing_queue_is_unknown_never_a_wake(self):
        runners = [runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        waiting = [wait_row(["self-hosted", "linux", "ameesh-ci"])]
        missing = host_release.ci_wake(HOST, runners, [], waiting, now=NOW)
        self.assertEqual(missing["state"], "unknown")
        stale = host_release.ci_wake(HOST, runners, [queue_row(seen=NOW - 4000)],
                                     waiting, now=NOW)
        self.assertEqual(stale["state"], "unknown")
        stale_wait = host_release.ci_wake(
            HOST, runners, [queue_row()],
            [wait_row(["self-hosted", "linux", "ameesh-ci"], seen=NOW - 4000)],
            now=NOW)
        self.assertEqual(stale_wait["state"], "unknown")

    def test_a_queue_without_its_wake_publication_is_unknown(self):
        """N-1 : la file de capacité est fraîche mais le stamp de publication
        des groupes manque ou vient d'une autre passe → doute, jamais idle."""
        runners = [runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        waiting = [wait_row(["self-hosted", "linux", "ameesh-ci"])]
        unpublished = queue_row()
        unpublished["wait_seen_ts"] = None
        evidence = host_release.ci_wake(HOST, runners, [unpublished], waiting,
                                        now=NOW)
        self.assertEqual(evidence["state"], "unknown")
        self.assertIn("not published", evidence["reason"])
        older = host_release.ci_wake(
            HOST, runners, [queue_row(seen=NOW - 10, wait_seen=NOW - 60)],
            waiting, now=NOW)
        self.assertEqual(older["state"], "unknown")
        # groupes publiés mais file non estampillée : le scan est plus récent
        # que `wait_seen_at`, même quand la file de réveil est vide.
        unstamped = host_release.ci_wake(
            HOST, runners,
            [queue_row(seen=NOW - 60, wait_seen=NOW - 60, scan_seen=NOW - 10)],
            [], now=NOW)
        self.assertEqual(unstamped["state"], "unknown")
        # publication interrompue : les groupes sont plus récents que la ligne
        # de capacité qui les a estampillés.
        later = host_release.ci_wake(
            HOST, runners, [queue_row(seen=NOW - 60, wait_seen=NOW - 60)],
            [wait_row(["self-hosted", "linux", "ameesh-ci"], seen=NOW)], now=NOW)
        self.assertEqual(later["state"], "unknown")

    def test_evidence_reads_the_queue_and_groups_in_one_query(self):
        """Lecture déchirée : la capacité, le scan et les groupes doivent venir
        d'un seul instantané, jamais de deux requêtes séparées."""
        ci = mock.Mock()
        ci.all_runners.return_value = [
            runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        ci.wake_queue.return_value = [{
            "repo": REPO, "seen_ts": NOW - 60, "wait_seen_ts": NOW - 60,
            "scan_seen_ts": NOW - 60, "oldest_ts": NOW - 900,
            "labels": ["self-hosted", "linux", "ameesh-ci"],
            "wait_oldest_ts": NOW - 900, "wait_queued": 1,
            "group_seen_ts": NOW - 60}]
        store = mock.Mock()
        store.ci = ci
        with mock.patch.object(host_release.storage, "of", return_value=store):
            evidence = host_release.ci_wake_evidence(None, HOST, now=NOW)
        self.assertEqual(evidence["state"], "waiting")
        ci.wake_queue.assert_called_once_with()
        ci.queue_wait.assert_not_called()
        ci.queued_oldest.assert_not_called()

    def test_unreadable_labels_are_unknown_and_never_a_wake(self):
        evidence = host_release.ci_wake(
            HOST, [runner_row("ameesh-ci-1-1", None)], [queue_row()],
            [wait_row(["self-hosted", "linux", "ameesh-ci"])], now=NOW)
        self.assertEqual(evidence["state"], "unknown")

    def test_a_queue_measured_before_the_release_never_wakes(self):
        """Anti-boucle : le relevé qui précède l'arrêt ne rallume pas la VM."""
        evidence = host_release.ci_wake(
            HOST,
            [runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])],
            [queue_row(seen=NOW - 60)],
            [wait_row(["self-hosted", "linux", "ameesh-ci"], seen=NOW - 60)],
            now=NOW, released_ts=NOW - 30)
        self.assertEqual(evidence, {"state": "idle"})

    def test_the_first_complete_reading_after_the_release_wakes(self):
        evidence = host_release.ci_wake(
            HOST,
            [runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])],
            [queue_row(seen=NOW)],
            [wait_row(["self-hosted", "linux", "ameesh-ci"], seen=NOW)],
            now=NOW, released_ts=NOW - 30)
        self.assertEqual(evidence["state"], "waiting")

    def test_labels_compare_case_folded_and_a_served_set_is_a_subset(self):
        evidence = host_release.ci_wake(
            HOST,
            [runner_row("ameesh-ci-1-1", ["Self-Hosted", "Linux", "AMEESH-CI",
                                          "ameesh-ci-pool"])],
            [queue_row()], [wait_row(["self-hosted", "linux", "ameesh-ci"])],
            now=NOW)
        self.assertEqual(evidence["state"], "waiting")

    def test_one_repo_unreadable_does_not_hide_a_waiting_other(self):
        runners = [runner_row("ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"]),
                   runner_row("ameesh-ci-1-2", ["self-hosted", "linux", "ameesh-ci"],
                              repo="manaty/other")]
        evidence = host_release.ci_wake(
            HOST, runners, [queue_row(), queue_row(seen=NOW - 4000, repo="manaty/other")],
            [wait_row(["self-hosted", "linux", "ameesh-ci"])], now=NOW)
        self.assertEqual(evidence["state"], "waiting")
        self.assertEqual(evidence["repo"], REPO)


class ReleaseBlockerCiTest(unittest.TestCase):
    CFG = replace(config.Config(), host_release_settle=1,
                  host_release_drain_bound=3600)

    def test_a_waiting_ci_job_blocks_the_stop(self):
        blocker = host_release.release_blocker(
            {"host": HOST, "requested_ts": 1000}, [], {"sampled_ts": 1010,
                                                       "turns_in_progress": 0},
            footprint={"runs": 0, "runs_source": "host measure"}, cfg=self.CFG,
            now=1030, ci={"state": "waiting", "repo": REPO,
                          "labels": ["self-hosted", "ameesh-ci"]})
        self.assertIn("CI job waiting", blocker)
        self.assertIn(REPO, blocker)

    def test_an_unreadable_ci_queue_blocks_the_stop(self):
        blocker = host_release.release_blocker(
            {"host": HOST, "requested_ts": 1000}, [], {"sampled_ts": 1010,
                                                       "turns_in_progress": 0},
            footprint={"runs": 0, "runs_source": "host measure"}, cfg=self.CFG,
            now=1030, ci={"state": "unknown", "reason": "queue reading stale"})
        self.assertIn("CI queue unreadable", blocker)

    def test_an_idle_or_inert_ci_host_still_stops(self):
        for evidence in ({"state": "idle"}, {"state": "inert"}, None):
            self.assertIsNone(host_release.release_blocker(
                {"host": HOST, "requested_ts": 1000}, [],
                {"sampled_ts": 1010, "turns_in_progress": 0},
                footprint={"runs": 0, "runs_source": "host measure"},
                cfg=self.CFG, now=1030, ci=evidence))

    def test_wake_due_takes_the_measured_ci_queue(self):
        control = {"host": HOST, "mode": "draining"}
        target = dict(TARGET, release_windows=[])
        args = dict(now=NOW, horizon=900, target=target)
        self.assertTrue(vm.wake_due(
            HOST, control, [], [], {HOST: control}, {}, ci={"state": "waiting"},
            **args))
        self.assertFalse(vm.wake_due(
            HOST, control, [], [], {HOST: control}, {}, ci={"state": "unknown"},
            **args))
        self.assertFalse(vm.wake_due(
            HOST, control, [], [], {HOST: control}, {}, ci={"state": "idle"},
            **args))


class CiWakeScannerTest(unittest.TestCase):
    def _advance(self, evidence, *, hook_state="draining", release_state="draining",
                 released_ts=None):
        row = {"host": HOST, "mode": "draining", "generation": 1,
               "release_epoch": 1, "claims_open": False,
               "hook_state": hook_state, "release_state": release_state,
               "requested_ts": NOW - 7200, "released_ts": released_ts}
        api = mock.Mock()
        api.transition_journal.return_value = [{"state": "draining",
                                                "generation": 1}]
        cfg = replace(config.Config(), host_release_settle=1.0,
                      host_release_drain_bound=86400.0)
        with mock.patch.object(host_release, "hook_for", return_value="true"), \
                mock.patch.object(host_release, "ci_wake_evidence",
                                  return_value=evidence), \
                mock.patch.object(host_release, "_fresh_reading",
                                  return_value={"sampled_ts": NOW - 10,
                                                "turns_in_progress": 0}), \
                mock.patch.object(host_release, "_host_footprint",
                                  return_value={"runs": 0, "runs_source": "mesure"}), \
                mock.patch.object(host_release, "_run_transition") as transition:
            host_release._advance(
                cfg, api, mock.Mock(), row, controls={}, listing=[],
                canons=[object()], canon_unknown=False, holds={}, known=set(),
                actor="tester", runner=None, now=NOW, clock=time.time)
        return transition

    def test_a_waiting_queue_asks_for_the_return(self):
        transition = self._advance({"state": "waiting", "repo": REPO,
                                    "labels": ["self-hosted", "ameesh-ci"]})
        transition.assert_called_once()
        self.assertEqual(transition.call_args.args[4], "busy")

    def test_a_released_host_is_woken_by_a_fresh_queue(self):
        transition = self._advance({"state": "waiting", "repo": REPO,
                                    "labels": ["self-hosted", "ameesh-ci"]},
                                   release_state="idle-drained", released_ts=NOW - 60)
        transition.assert_called_once()
        self.assertEqual(transition.call_args.args[4], "busy")

    def test_an_unreadable_queue_does_not_wake_and_blocks_the_stop(self):
        transition = self._advance({"state": "unknown", "reason": "stale"})
        transition.assert_not_called()

    def test_an_idle_queue_lets_the_stop_go_on(self):
        transition = self._advance({"state": "idle"})
        transition.assert_called_once()
        self.assertEqual(transition.call_args.args[4], "idle-drained")


class GuardApi:
    """Le sous-ensemble de l'API GitHub lu par `ci.Watcher._guard_state`."""

    def __init__(self):
        self.runs, self.jobs, self.runners = [], {}, []
        self.truncated = False

    def __call__(self, path):
        if "/actions/runs?" in path:
            extra = 1 if self.truncated else 0
            return {"total_count": len(self.runs) + extra,
                    "workflow_runs": list(self.runs)}
        if "/actions/runs/" in path and "/jobs" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            jobs = self.jobs.get(run_id, [])
            return {"total_count": len(jobs), "jobs": list(jobs)}
        if "/actions/runners" in path:
            return {"total_count": len(self.runners), "runners": list(self.runners)}
        raise AssertionError(path)


def guard_run(run_id, *, status="queued", created=None):
    return {"id": run_id, "status": status, "run_attempt": 1,
            "path": ".github/workflows/tests.yml", "head_sha": "a" * 40,
            "head_branch": "main", "event": "push",
            "created_at": iso(created or NOW)}


def guard_job(job_id, labels, *, created=None):
    return {"id": job_id, "name": "tests", "status": "queued",
            "created_at": iso(created or (NOW - 900)),
            "labels": [{"name": label} for label in labels]}


def guard_runner(name, labels, *, status="offline", busy=False):
    return {"id": 1, "name": name, "status": status, "busy": busy,
            "labels": [{"name": label} for label in labels]}


class CiQueueMeasurementTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for table in ("ci_runner_state", "ci_queue_state", "ci_queue_wait",
                      "ci_queue_wait_scan", "ci_guard_scan", "ci_guard_run"):
            self.db.execute("DELETE FROM %s" % table)
        self.horloge = time.time()
        self.api = GuardApi()
        self.logs = []

    def scan(self):
        self.horloge += ci.GUARD_SCAN_S
        watcher = ci.Watcher(config.Config(), api=self.api,
                             clock=lambda: self.horloge,
                             log=self.logs.append)
        watcher._guard(self.db, REPO)
        return storage.of(self.db).ci

    def test_offline_fleet_publishes_the_waiting_labels(self):
        self.api.runs = [guard_run(90)]
        self.api.jobs = {90: [guard_job(901, ["self-hosted", "linux", "ameesh-ci"])]}
        self.api.runners = [guard_runner(
            "ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        st = self.scan()
        (row,) = st.queue_wait()
        self.assertEqual((row["repo"], row["labels"], int(row["queued"])),
                         (REPO, ["ameesh-ci", "linux", "self-hosted"], 1))
        (queue,) = st.queued_oldest()
        self.assertEqual(int(queue["queued"]), 1)
        # #8031 : la file de capacité est estampillée par la même passe que
        # les groupes (scan écrit avec eux, avant la ligne de file).
        self.assertEqual(queue["wait_seen_ts"], queue["seen_ts"])
        self.assertLessEqual(queue["scan_seen_ts"], queue["wait_seen_ts"])
        # la correspondance hôte → étiquettes se lit sur les runners enregistrés
        runners = st.all_runners()
        self.assertEqual([(row["name"], row["labels"]) for row in runners],
                         [("ameesh-ci-1-1", ["ameesh-ci", "linux", "self-hosted"])])
        evidence = host_release.ci_wake_evidence(self.db, HOST, now=self.horloge)
        self.assertEqual(evidence["state"], "waiting")

    def test_an_unstamped_or_interrupted_publication_is_unknown(self):
        self.api.runs = [guard_run(97)]
        self.api.jobs = {97: [guard_job(971, ["self-hosted", "linux", "ameesh-ci"])]}
        self.api.runners = [guard_runner(
            "ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        st = self.scan()
        # groupes publiés (ici vides) puis arrêt avant la ligne de file : le
        # scan est plus récent que `wait_seen_at` → doute, même sans groupe.
        st.observe_queue_wait(REPO, [])
        evidence = host_release.ci_wake_evidence(self.db, HOST, now=self.horloge)
        self.assertEqual(evidence["state"], "unknown")
        self.assertIn("after the queue reading", evidence["reason"])
        # veilleur N-1 : la file de capacité est rafraîchie, pas le stamp.
        self.scan()
        st.observe_queue(REPO, oldest_id=971, oldest_at=iso(self.horloge - 900),
                         queued=1)
        evidence = host_release.ci_wake_evidence(self.db, HOST, now=self.horloge)
        self.assertEqual(evidence["state"], "unknown")
        self.assertIn("not published", evidence["reason"])

    def test_a_free_online_runner_is_not_a_wake(self):
        """Un runner en ligne, libre, peut prendre le job : la file de capacité
        le compte encore, mais aucune VM arrêtée ne manque."""
        self.api.runs = [guard_run(91)]
        self.api.jobs = {91: [guard_job(911, ["self-hosted", "linux", "ameesh-ci"])]}
        self.api.runners = [guard_runner(
            "ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"],
            status="online")]
        st = self.scan()
        self.assertEqual(st.queue_wait(), [])
        (queue,) = st.queued_oldest()
        self.assertEqual(int(queue["queued"]), 1)
        # la file de réveil vide est prouvée par une ligne de scan, pas par
        # l'absence de lignes (compatibilité N-1).
        (scan,) = st.wake_queue()
        self.assertIsNone(scan["labels"])
        self.assertIsNotNone(scan["scan_seen_ts"])
        evidence = host_release.ci_wake_evidence(self.db, HOST, now=self.horloge)
        self.assertEqual(evidence["state"], "idle")

    def test_a_busy_online_runner_is_not_a_wake(self):
        """En ligne mais occupé : le job attendra le prochain créneau de ce
        runner ; aucune VM n'est réveillée. La file de capacité, elle, reste."""
        self.api.runs = [guard_run(92, status="in_progress")]
        self.api.jobs = {92: [guard_job(921, ["self-hosted", "linux", "ameesh-ci"])]}
        self.api.runners = [guard_runner(
            "ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"],
            status="online", busy=True)]
        st = self.scan()
        self.assertEqual(st.queue_wait(), [])
        (queue,) = st.queued_oldest()
        self.assertEqual(int(queue["queued"]), 1)
        evidence = host_release.ci_wake_evidence(self.db, HOST, now=self.horloge)
        self.assertEqual(evidence["state"], "idle")

    def test_a_second_scan_replaces_the_previous_groups(self):
        """Un groupe qui demeure est mis à jour, un groupe disparu est retiré :
        deux relevés complets ne laissent jamais deux générations mêlées."""
        self.api.runs = [guard_run(96)]
        self.api.jobs = {96: [guard_job(961, ["self-hosted", "linux", "ameesh-ci"])]}
        self.api.runners = [guard_runner(
            "ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        st = self.scan()
        self.assertEqual([row["labels"] for row in st.queue_wait()],
                         [["ameesh-ci", "linux", "self-hosted"]])
        self.api.jobs = {96: [guard_job(
            961, ["self-hosted", "linux", "ameesh-ci", "gpu"])]}
        st = self.scan()
        (row,) = st.queue_wait()
        self.assertEqual(row["labels"],
                         ["ameesh-ci", "gpu", "linux", "self-hosted"])

    def test_a_github_hosted_job_is_not_our_capacity(self):
        self.api.runs = [guard_run(93)]
        self.api.jobs = {93: [guard_job(931, ["ubuntu-latest"])]}
        self.api.runners = [guard_runner(
            "ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        st = self.scan()
        self.assertEqual(st.queue_wait(), [])

    def test_an_incomplete_scan_keeps_the_previous_measurement(self):
        self.api.runs = [guard_run(94)]
        self.api.jobs = {94: [guard_job(941, ["self-hosted", "linux", "ameesh-ci"])]}
        self.api.runners = [guard_runner(
            "ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        st = self.scan()
        (kept,) = st.queue_wait()
        self.api.truncated = True
        self.scan()
        (row,) = st.queue_wait()
        self.assertEqual(row["labels"], kept["labels"])
        self.assertTrue(any("guard scan incomplete" in text for text in self.logs))
        self.assertFalse([text for text in self.logs if "guard failed" in text])

    def test_pruning_a_repo_removes_its_waiting_groups(self):
        self.api.runs = [guard_run(95)]
        self.api.jobs = {95: [guard_job(951, ["self-hosted", "linux", "ameesh-ci"])]}
        self.api.runners = [guard_runner(
            "ameesh-ci-1-1", ["self-hosted", "linux", "ameesh-ci"])]
        st = self.scan()
        self.assertEqual(len(st.queue_wait()), 1)
        st.prune_queue(["manaty/nexlink"])
        self.assertEqual(st.queue_wait(), [])


class CiWakeAdapterTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("DELETE FROM vm_adapter_requests")
        self.db.execute("DELETE FROM vm_adapter_observations")
        self.db.execute("DELETE FROM host_lifecycle")
        self.db.execute("INSERT INTO host_lifecycle"
                        "(host,mode,actor,claims_open,release_state,hook_state) "
                        "VALUES (%s,'draining','tester',false,'draining','draining')",
                        (HOST,))
        self.cfg = replace(self.cfg, idle_after_s=1800)
        self.q = Queue(self.db)
        self.st = storage.of(self.db)
        self.provider = mock.Mock()
        self.provider.state.return_value = "running"
        self.provider.allowed.return_value = {"poweron", "poweroff"}
        self.now = self.q.now()

    def seed_queue(self, *, labels=("self-hosted", "linux", "ameesh-ci")):
        self.st.ci.observe_runners(REPO, [{
            "id": 1, "name": "ameesh-ci-1-1", "busy": False, "job": None,
            "stuck": False, "labels": list(labels)}])
        self.st.ci.observe_queue_wait(REPO, [{
            "labels": list(labels), "oldest_id": 11,
            "oldest_at": iso(self.now - 900), "queued": 1}])
        self.st.ci.observe_queue(REPO, oldest_id=11,
                                 oldest_at=iso(self.now - 900), queued=1,
                                 wait_published=True)

    def request_stop(self):
        control = self.q.control(HOST)
        self.q.request(HOST, control["generation"], control["release_epoch"],
                       "idle-drained")

    def tick(self, target=None):
        return vm.tick_host(
            self.cfg, self.db,
            policy(targets={HOST: dict(target or TARGET)}), HOST, self.provider,
            probe=mock.Mock(return_value=True))

    def test_a_waiting_job_asks_for_the_return_instead_of_a_stop(self):
        self.seed_queue()
        self.request_stop()
        result = self.tick()
        self.assertEqual(result, "fresh reading pending")
        control = self.q.control(HOST)
        self.assertIsNotNone(control["return_requested_ts"])
        self.assertTrue(any(row["state"] == "busy"
                            for row in self.q.pending_all(HOST)))
        self.provider.action.assert_not_called()

    def test_an_unreadable_queue_refuses_the_stop(self):
        control = {"host": HOST, "requested_ts": self.now - 7200,
                   "mode": "draining", "released_ts": None}
        store = mock.Mock()
        store.hosts.latest.return_value = {"sampled_ts": self.now - 10,
                                           "turns_in_progress": 0}
        store.operations.listing.return_value = []
        with mock.patch.object(vm.storage, "of", return_value=store), \
                mock.patch.object(vm.host_release, "ci_wake_evidence",
                                  return_value={"state": "unknown",
                                                "reason": "queue reading stale"}), \
                mock.patch.object(vm.host_release, "_canon_read",
                                  return_value=([object()], False)), \
                mock.patch.object(vm.host_idle, "snapshot",
                                  return_value={HOST: {
                                      "capacity": "burst", "unknown": [],
                                      "state": "idle", "runs": 0,
                                      "runs_source": "host measure"}}), \
                mock.patch.object(vm.host_lifecycle, "critical_names",
                                  return_value=[]):
            with self.assertRaisesRegex(Refused, "CI queue unreadable"):
                vm.stop_evidence(self.cfg, mock.Mock(), HOST, control,
                                 now=self.now)

    def test_a_waiting_job_refuses_the_stop(self):
        control = {"host": HOST, "requested_ts": self.now - 7200,
                   "mode": "draining", "released_ts": None}
        store = mock.Mock()
        store.hosts.latest.return_value = {"sampled_ts": self.now - 10,
                                           "turns_in_progress": 0}
        store.operations.listing.return_value = []
        with mock.patch.object(vm.storage, "of", return_value=store), \
                mock.patch.object(vm.host_release, "ci_wake_evidence",
                                  return_value={"state": "waiting", "repo": REPO,
                                                "labels": ["self-hosted", "ameesh-ci"]}), \
                mock.patch.object(vm.host_release, "_canon_read",
                                  return_value=([object()], False)), \
                mock.patch.object(vm.host_idle, "snapshot",
                                  return_value={HOST: {
                                      "capacity": "burst", "unknown": [],
                                      "state": "idle", "runs": 0,
                                      "runs_source": "host measure"}}), \
                mock.patch.object(vm.host_lifecycle, "critical_names",
                                  return_value=[]):
            with self.assertRaisesRegex(Refused, "CI job waiting"):
                vm.stop_evidence(self.cfg, mock.Mock(), HOST, control,
                                 now=self.now)

    def test_a_non_ci_host_still_stops(self):
        """Aucun runner enregistré : la preuve d'arrêt est inchangée."""
        control = {"host": HOST, "requested_ts": self.now - 7200,
                   "mode": "draining", "released_ts": None}
        store = mock.Mock()
        store.hosts.latest.return_value = {"sampled_ts": self.now - 10,
                                           "turns_in_progress": 0}
        store.operations.listing.return_value = []
        footprint = {"capacity": "burst", "unknown": [], "state": "idle",
                     "runs": 0, "runs_source": "host measure"}
        with mock.patch.object(vm.storage, "of", return_value=store), \
                mock.patch.object(vm.host_release, "ci_wake_evidence",
                                  return_value={"state": "inert"}), \
                mock.patch.object(vm.host_release, "_canon_read",
                                  return_value=([object()], False)), \
                mock.patch.object(vm.host_idle, "snapshot",
                                  return_value={HOST: footprint}), \
                mock.patch.object(vm.host_lifecycle, "critical_names",
                                  return_value=[]):
            self.assertEqual(vm.stop_evidence(self.cfg, mock.Mock(), HOST,
                                              control, now=self.now), footprint)


if __name__ == "__main__":
    unittest.main()
