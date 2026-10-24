# SPDX-License-Identifier: AGPL-3.0-only
"""#3186 — un agrégat requis jamais conclu est une preuve absente.

Décision owner #128512 du 23/10/2026 (option a, signée) : « Agrégat requis
jamais conclu = preuve absente, donc bloquant : on relance le seul run (coût :
une relance). Les non-concluants à l'intérieur d'un agrégat qui a conclu
restent non bloquants (§3). »

Un check requis (l'agrégat `tests`, #876) qui n'a pas conclu lui-même —
`CANCELLED`, `TIMED_OUT`, `STARTUP_FAILURE`, `STALE` — bloque la porte du
feu, du paquet et de la release, et son run est relancé une seule fois. Un
check requis sauté, un check facultatif ou une part non concluante restent
consignés, comme sous #3085.

Témoins sans réseau ni base : le faux `gh` de #2271 pour `ready_pr`, le banc
de paquet de #2813 pour `packet_automerge`, la lecture CI de #l160g pour
`commit_ci`, et les lectures injectées de #2448 pour la release.
"""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from ameesh import fire, packet_automerge, radar, release_cutoff

from . import test_2271_fire_required_checks as f2271
from . import test_2448_release_cutoff as r2448
from . import test_2813_packet_auto_merge as banc
from . import test_l160g_nexlink as l160g

SLUG = "manaty/ameesh"
JOB_URL = "https://github.com/manaty/ameesh/actions/runs/%d/job/123"


def _aggregate(conclusion, run=200, status="COMPLETED"):
    """L'agrégat `tests` tel que le rollup de la PR le porte."""
    return dict(f2271._check("tests", conclusion, status), detailsUrl=JOB_URL % run)


class RunsGh:
    """`gh` réduit à la lecture d'un run et à sa relance."""

    def __init__(self, runs=None, *, read_error="", rerun_error=""):
        self.runs = dict(runs or {})
        self.read_error, self.rerun_error = read_error, rerun_error
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        if argv[:1] == ["api"] and "/actions/runs/" in argv[1]:
            run_id = int(argv[1].rsplit("/", 1)[1])
            if self.read_error or run_id not in self.runs:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr=self.read_error or "HTTP 404")
            return types.SimpleNamespace(returncode=0, stderr="",
                                         stdout=json.dumps(dict(self.runs[run_id], id=run_id)))
        if argv[:2] == ["run", "rerun"]:
            if self.rerun_error:
                return types.SimpleNamespace(returncode=1, stdout="", stderr=self.rerun_error)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="unexpected gh %s" % " ".join(argv))

    def reruns(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["run", "rerun"]]


def _run_row(head=f2271.SHA, status="completed", conclusion="cancelled", attempt=1):
    return {"head_sha": head, "status": status, "conclusion": conclusion,
            "run_attempt": attempt}


class ClassificationTest(unittest.TestCase):
    """Le classifieur pur : jamais conclu ⊂ non concluant."""

    def test_cancelled_expired_unstarted_and_stale_never_concluded(self):
        for conclusion in ("CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "STALE",
                           "cancelled", " timed_out "):
            with self.subTest(conclusion=conclusion):
                self.assertTrue(radar.never_concluded(conclusion))

    def test_a_deliberate_conclusion_is_not_never_concluded(self):
        for conclusion in ("SUCCESS", "FAILURE", "SKIPPED", "NEUTRAL",
                           "ACTION_REQUIRED", "PENDING", "", None):
            with self.subTest(conclusion=conclusion):
                self.assertFalse(radar.never_concluded(conclusion))

    def test_the_run_of_a_check_is_read_from_its_job_url_or_id(self):
        self.assertEqual(radar.actions_run_id(SLUG, {"detailsUrl": JOB_URL % 42}), 42)
        self.assertEqual(radar.actions_run_id(SLUG, {"details_url": JOB_URL % 43}), 43)
        self.assertEqual(radar.actions_run_id(SLUG, {"databaseId": 44}), 44)
        # un autre dépôt, un statut de commit, une autre application : rien
        self.assertIsNone(radar.actions_run_id("manaty/nexlink",
                                               {"detailsUrl": JOB_URL % 42}))
        self.assertIsNone(radar.actions_run_id(SLUG, f2271._context("ci/tests", "ERROR")))
        self.assertIsNone(radar.actions_run_id(SLUG, {"detailsUrl": "https://ci.example/1"}))
        self.assertIsNone(radar.actions_run_id(SLUG, {"databaseId": True}))


class RerunTest(unittest.TestCase):
    """`radar.rerun_unconcluded` : une relance, jamais deux."""

    def test_a_concluded_first_attempt_is_rerun_once(self):
        gh = RunsGh({200: _run_row()})
        out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200), ("tests", 200)],
                                      gh=gh)
        self.assertEqual([row["result"] for row in out], ["rerun"])
        self.assertEqual(gh.reruns(), [["run", "rerun", "200", "--failed", "--repo", SLUG]])

    def test_a_run_already_rerun_is_not_rerun_again(self):
        gh = RunsGh({200: _run_row(attempt=2)})
        out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200)], gh=gh)
        self.assertEqual(out[0]["result"], "spent")
        self.assertIn("already rerun once", out[0]["detail"])
        self.assertEqual(gh.reruns(), [])

    def test_a_running_run_waits(self):
        gh = RunsGh({200: _run_row(status="queued", conclusion=None, attempt=2)})
        out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200)], gh=gh)
        self.assertEqual(out[0]["result"], "running")
        self.assertEqual(gh.reruns(), [])

    def test_a_run_of_another_head_is_refused(self):
        gh = RunsGh({200: _run_row(head="f" * 40)})
        out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200)], gh=gh)
        self.assertEqual(out[0]["result"], "refused")
        self.assertEqual(gh.reruns(), [])

    def test_no_run_unreadable_run_and_refused_rerun_are_reported(self):
        gh = RunsGh({})
        out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("ci/x", None), ("tests", 200)],
                                      gh=gh)
        self.assertEqual([row["result"] for row in out], ["unknown", "error"])
        gh = RunsGh({200: _run_row()}, rerun_error="HTTP 403")
        out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200)], gh=gh)
        self.assertEqual(out[0]["result"], "error")
        self.assertIn("HTTP 403", out[0]["detail"])

    def test_a_dry_run_reads_but_never_reruns(self):
        gh = RunsGh({200: _run_row()})
        out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200)], gh=gh,
                                      dry_run=True)
        self.assertEqual(out[0]["result"], "would-rerun")
        self.assertEqual(gh.reruns(), [])

    def test_a_plan_github_runner_is_accepted(self):
        class Runner:
            calls = []

            def run(self, argv, stdin=None):
                self.calls.append(argv)
                if argv[0] == "api":
                    return 0, json.dumps(dict(_run_row(), id=200)), ""
                return 0, "", ""
        runner = Runner()
        out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200)], gh=runner)
        self.assertEqual(out[0]["result"], "rerun")
        self.assertEqual(runner.calls[-1][:3], ["run", "rerun", "200"])


class ReadyPrTest(unittest.TestCase):
    """`fire.ready_pr` : l'agrégat requis jamais conclu bloque."""

    def ready(self, rollup, *, required=(("tests", None),), gh=None):
        report: dict = {}
        ok, why = fire.ready_pr(f2271._status(rollup), target="main", project=None,
                                frozen=f2271.SHA, required=required, slug=SLUG,
                                gh=gh, report=report)
        return ok, why, report

    def test_a_required_aggregate_that_never_concluded_blocks(self):
        for conclusion in ("CANCELLED", "TIMED_OUT", "STARTUP_FAILURE", "STALE"):
            with self.subTest(conclusion=conclusion):
                entry = _aggregate(conclusion)
                ok, why, report = self.ready([entry])
                self.assertFalse(ok)
                self.assertIn("tests (%s: never concluded, no test proof)" % conclusion, why)
                self.assertEqual(report["consigned"], [])
                self.assertEqual(report["unconcluded"], [("tests", entry)])

    def test_an_app_scoped_aggregate_that_never_concluded_blocks(self):
        run = dict(f2271._run("tests", conclusion="cancelled"),
                   details_url=JOB_URL % 201)
        gh = f2271.FakeGh(check_runs=[run])
        ok, why, report = self.ready([_aggregate("CANCELLED", 201)],
                                     required=(("tests", 15368),), gh=gh)
        self.assertFalse(ok)
        self.assertIn("never concluded", why)
        [(name, entry)] = report["unconcluded"]
        self.assertEqual(name, "tests")
        self.assertEqual(radar.actions_run_id(SLUG, entry), 201)

    def test_a_second_source_that_never_concluded_still_blocks(self):
        # un même nom porté par deux sources n'est vert que si toutes le sont
        green = dict(_aggregate("SUCCESS"), workflowName="tests")
        cancelled = dict(_aggregate("CANCELLED", 202), workflowName="other")
        ok, why, report = self.ready([green, cancelled])
        self.assertFalse(ok)
        self.assertEqual(report["unconcluded"], [("tests", cancelled)])

    def test_a_proven_failure_or_a_wait_comes_first(self):
        failed = dict(_aggregate("FAILURE"), workflowName="tests")
        cancelled = dict(_aggregate("CANCELLED", 202), workflowName="other")
        ok, why, report = self.ready([failed, cancelled])
        self.assertFalse(ok)
        self.assertIn("tests (FAILURE)", why)
        self.assertEqual(report["unconcluded"], [])

    def test_no_rerun_when_another_required_check_or_the_packet_proof_is_red(self):
        # la relance ne lèverait pas la porte : elle ne coûterait qu'une suite
        ok, why, report = self.ready([_aggregate("CANCELLED"),
                                      f2271._check("lint", "FAILURE")],
                                     required=(("tests", None), ("lint", None)))
        self.assertFalse(ok)
        self.assertIn("never concluded", why)
        self.assertEqual(report["unconcluded"], [])
        sentinel = {"__typename": "CheckRun", "name": "paquet (run proof unavailable)",
                    "status": "COMPLETED", "conclusion": "FAILURE"}
        ok, why, report = self.ready([_aggregate("CANCELLED"), sentinel])
        self.assertFalse(ok)
        self.assertEqual(report["unconcluded"], [])

    def test_a_pending_second_required_check_does_not_hold_the_rerun(self):
        ok, why, report = self.ready([_aggregate("CANCELLED"),
                                      f2271._check("lint", None, "IN_PROGRESS")],
                                     required=(("tests", None), ("lint", None)))
        self.assertFalse(ok)
        self.assertEqual(len(report["unconcluded"]), 1)

    def test_a_skipped_required_check_is_still_consigned(self):
        ok, why, report = self.ready([_aggregate("SKIPPED")])
        self.assertTrue(ok, why)
        self.assertEqual(report["consigned"], ["tests (SKIPPED)"])
        self.assertEqual(report["unconcluded"], [])

    def test_a_non_conclusive_part_inside_a_concluded_aggregate_does_not_block(self):
        # « les non-concluants à l'intérieur d'un agrégat qui a conclu restent
        # non bloquants » : une part annulée, hors des requis, ne gate pas.
        ok, why, report = self.ready([_aggregate("SUCCESS"),
                                      f2271._check("psycopg 1/4", "CANCELLED"),
                                      f2271._check("platform-macos", "TIMED_OUT")])
        self.assertTrue(ok, why)
        self.assertEqual(report["unconcluded"], [])

    def test_the_strict_reader_is_unchanged(self):
        self.assertEqual(fire._required_gap((("tests", None),), [_aggregate("CANCELLED")]),
                         ["tests (CANCELLED)"])


class RelaunchTest(unittest.TestCase):
    """`fire.relaunch_unconcluded` : la relance sous le bail, notée une fois."""

    def setUp(self):
        self.notes: list[tuple] = []
        patcher = mock.patch.object(fire.work, "note",
                                    lambda db, lot, text, **kw: self.notes.append(
                                        (lot, text, kw.get("once"))))
        patcher.start()
        self.addCleanup(patcher.stop)

    def report(self, run=200):
        return {"unconcluded": [("tests", _aggregate("CANCELLED", run))]}

    def test_the_run_is_rerun_and_noted_once(self):
        gh = RunsGh({200: _run_row()})
        out = fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA, self.report(),
                                        gh=gh, guard=lambda: True)
        self.assertEqual([row["result"] for row in out], ["rerun"])
        self.assertEqual(len(gh.reruns()), 1)
        [(lot, text, once)] = self.notes
        self.assertEqual((lot, once), (7, True))
        self.assertIn("never concluded", text)
        self.assertIn("#128512", text)

    def test_a_lost_lease_reruns_nothing(self):
        gh = RunsGh({200: _run_row()})
        out = fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA, self.report(),
                                        gh=gh, guard=lambda: False)
        self.assertEqual([row["result"] for row in out], ["busy"])
        self.assertEqual(gh.calls, [])
        self.assertEqual(self.notes, [])

    def test_nothing_unconcluded_does_nothing(self):
        gh = RunsGh({})
        self.assertEqual(fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA,
                                                   {"unconcluded": []}, gh=gh), [])
        self.assertEqual(gh.calls, [])

    def test_a_rerun_in_progress_is_not_noted(self):
        gh = RunsGh({200: _run_row(status="in_progress", conclusion=None, attempt=2)})
        out = fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA, self.report(), gh=gh)
        self.assertEqual(out[0]["result"], "running")
        self.assertEqual(self.notes, [])


class ForgeRuns(banc.FakeGh):
    """Le banc de #2813, avec l'URL du job sur le check run de la tête, la
    lecture du run 200 à un essai donné et sa relance."""

    def __init__(self, *, attempt=1, run_status="completed", **kwargs):
        super().__init__(**kwargs)
        self.attempt, self.run_status = attempt, run_status

    def answer(self, args):
        args = list(args)
        path = args[-1] if args else ""
        if args[:1] == ["api"] and path.endswith("/actions/runs/200"):
            self.calls.append(args)
            return 0, json.dumps({"id": 200, "head_sha": self.head, "run_attempt": self.attempt,
                                  "status": self.run_status, "conclusion": "cancelled",
                                  "head_branch": self.branch, "event": "pull_request",
                                  "path": ".github/workflows/tests.yml"}), ""
        if args[:2] == ["run", "rerun"]:
            self.calls.append(args)
            self.attempt += 1
            self.run_status = "queued"
            return 0, "", ""
        code, out, err = super().answer(args)
        if args[:1] == ["api"] and "/check-runs" in path and not code:
            pages = json.loads(out)
            for run in pages[0]["check_runs"]:
                run["details_url"] = JOB_URL % 200
            out = json.dumps(pages)
        return code, out, err

    def reruns(self):
        return [call for call in self.calls if call[:2] == ["run", "rerun"]]


class PacketTest(banc.Journal):
    """`packet_automerge` : le porteur à l'agrégat jamais conclu attend et
    relance son run une fois."""

    def setUp(self):
        super().setUp()
        for state, conclusion in (("cancelled", "CANCELLED"), ("timed_out", "TIMED_OUT"),
                                  ("startup_failure", "STARTUP_FAILURE")):
            banc.RUNS.setdefault(state, ("COMPLETED", conclusion))
        self.created: list[dict] = []
        patcher = mock.patch.object(packet_automerge.work, "add",
                                    lambda db, **kw: self.created.append(kw))
        patcher.start()
        self.addCleanup(patcher.stop)

    def check(self, gh, **kw):
        return packet_automerge.check(None, None, banc.LOT, gh=gh, **kw)

    def test_a_never_concluded_aggregate_waits_and_reruns_its_run_once(self):
        for state in ("cancelled", "timed_out", "startup_failure"):
            with self.subTest(state=state):
                self.events.clear()
                self.armed()
                gh = ForgeRuns(tests=state)
                out = self.check(gh)
                self.assertEqual(out["result"], "pending", out)
                self.assertIn("never concluded", out["why"])
                self.assertEqual([row["result"] for row in out["reruns"]], ["rerun"])
                self.assertEqual(gh.reruns(), [["run", "rerun", "200", "--failed",
                                                "--repo", banc.SLUG]])
                self.assertEqual(gh.merges(), [])
                self.assertEqual(self.created, [])
                self.assertTrue(any("#128512" in note for note in self.notes()))
                # le passage suivant voit la relance en cours : rien de plus
                out = self.check(gh)
                self.assertEqual(out["reruns"][0]["result"], "running")
                self.assertEqual(len(gh.reruns()), 1)
                self.assertIsNotNone(self.active())

    def test_a_run_already_rerun_is_not_rerun_again(self):
        self.armed()
        gh = ForgeRuns(tests="cancelled", attempt=2)
        out = self.check(gh)
        self.assertEqual(out["result"], "pending")
        self.assertEqual(out["reruns"][0]["result"], "spent")
        self.assertEqual(gh.reruns(), [])
        self.assertEqual(gh.merges(), [])

    def test_a_lost_lease_reruns_nothing(self):
        self.armed()
        self.renew = False
        gh = ForgeRuns(tests="cancelled")
        out = self.check(gh)
        self.assertEqual(out["result"], "pending")
        self.assertEqual(out["reruns"][0]["result"], "busy")
        self.assertEqual(gh.reruns(), [])

    def test_a_dry_run_reruns_nothing(self):
        self.armed()
        gh = ForgeRuns(tests="cancelled")
        out = self.check(gh, dry_run=True)
        self.assertEqual(out["reruns"][0]["result"], "would-rerun")
        self.assertEqual(gh.reruns(), [])

    def test_a_red_aggregate_never_triggers_a_rerun(self):
        self.armed()
        gh = ForgeRuns(tests="failure")
        out = self.check(gh)
        self.assertEqual(out["result"], "pending")
        self.assertNotIn("reruns", out)
        self.assertEqual(gh.reruns(), [])


class CommitCiTest(unittest.TestCase):
    """`radar.commit_ci` : un run jamais conclu n'est pas `acceptable`."""

    project = radar.Project("n", "/r", github="o/nexlink", ci_required=True)

    def test_a_run_that_never_concluded_is_absent_proof(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                row = dict(l160g._run("tests", conclusion=conclusion), databaseId=300)
                out = radar.commit_ci(self.project, "a" * 40, gh=l160g._Gh([row]))
                self.assertFalse(out["acceptable"])
                self.assertEqual(out["unconcluded"], [{"check": "tests", "databaseId": 300}])
                self.assertEqual(out["inconclusive"], [])
                self.assertFalse(release_cutoff.reading_is_green(out))

    def test_a_skipped_or_waiting_for_action_run_stays_acceptable(self):
        rows = [l160g._run("tests"), l160g._run("pages", conclusion="action_required")]
        out = radar.commit_ci(self.project, "a" * 40, gh=l160g._Gh(rows))
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["unconcluded"], [])
        self.assertEqual(out["inconclusive"], ["pages"])

    def test_the_run_id_is_requested(self):
        gh = l160g._Gh([l160g._run("tests")])
        radar.commit_ci(self.project, "a" * 40, gh=gh)
        self.assertIn("databaseId", gh.calls[0][-1])


class ReleaseTest(r2448.ReleaseFixtureMixin, unittest.TestCase):
    """La porte de release : la tête jamais conclue n'est pas verte, son run
    est relancé une fois, l'écart est rapporté."""

    READING = {"green": False, "acceptable": False, "negative": [], "inconclusive": [],
               "unconcluded": [{"check": "tests", "databaseId": 300}],
               "pending": [], "failed": ["tests"], "missing": [], "runs": 1}

    def test_a_head_whose_run_never_concluded_is_not_released(self):
        with mock.patch.object(release_cutoff, "ci_reading", return_value=self.READING), \
                mock.patch.object(release_cutoff, "last_green", return_value=None):
            plan = release_cutoff.build_plan(self.cfg, self.project,
                                             now=r2448._paris(11, 5))
        self.assertFalse(plan.green)
        self.assertNotIn("Checks non concluants", plan.notes)

    def test_the_head_run_is_rerun_once_and_reported(self):
        plan = types.SimpleNamespace(ci=self.READING, head="a" * 40)
        calls = []

        def rerun(slug, head, runs, *, gh):
            calls.append((slug, head, runs))
            return [{"check": "tests", "run": 300, "result": "rerun",
                     "detail": "run 300 rerun (once)"}]
        with mock.patch.object(release_cutoff.radar, "rerun_unconcluded", rerun), \
                mock.patch.object(release_cutoff, "github_slug", return_value=SLUG):
            out = release_cutoff.rerun_head(self.project, plan)
        self.assertEqual(calls, [(SLUG, "a" * 40, [("tests", 300)])])
        self.assertEqual(out[0]["result"], "rerun")
        [line] = release_cutoff._unconcluded_deviation(plan, out)
        self.assertIn("jamais conclu", line)
        self.assertIn("#128512", line)
        self.assertIn("run 300 rerun (once)", line)

    def test_nothing_to_rerun_on_a_concluded_head(self):
        plan = types.SimpleNamespace(ci={"green": True}, head="a" * 40)
        with mock.patch.object(release_cutoff.radar, "rerun_unconcluded") as rerun:
            self.assertEqual(release_cutoff.rerun_head(self.project, plan), [])
        rerun.assert_not_called()
        self.assertEqual(release_cutoff._unconcluded_deviation(plan, []), [])

    def test_an_unreadable_slug_is_reported_not_raised(self):
        plan = types.SimpleNamespace(ci=self.READING, head="a" * 40)
        with mock.patch.object(release_cutoff, "github_slug",
                               side_effect=release_cutoff.ReleaseError("no remote")):
            out = release_cutoff.rerun_head(self.project, plan)
        self.assertEqual(out[0]["result"], "error")
        self.assertIn("no remote", out[0]["detail"])


if __name__ == "__main__":
    unittest.main()
