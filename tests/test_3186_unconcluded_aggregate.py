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
from . import test_3244_release_optional_jobs as t3244
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
        # B2 impl-04 : `--failed` ne rejoue pas un job annulé ; le run entier
        self.assertEqual(gh.reruns(), [["run", "rerun", "200", "--repo", SLUG]])

    def test_every_never_concluded_run_is_rerun_in_full(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure", "stale", "failure"):
            with self.subTest(conclusion=conclusion):
                gh = RunsGh({200: _run_row(conclusion=conclusion)})
                out = radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200)], gh=gh)
                self.assertEqual(out[0]["result"], "rerun")
                [call] = gh.reruns()
                self.assertNotIn("--failed", call)

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
        # la garde est relue juste avant la mutation, après la lecture du run
        gh = RunsGh({200: _run_row()})
        out = fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA, self.report(),
                                        gh=gh, guard=lambda: False)
        self.assertEqual([row["result"] for row in out], ["busy"])
        self.assertEqual(gh.reruns(), [])
        self.assertEqual(self.notes, [])

    def test_the_guard_is_read_after_the_run_and_before_the_rerun(self):
        events = []
        gh = RunsGh({200: _run_row()})
        inner = gh.__call__

        def call(argv, **kw):
            events.append(argv[0] if argv[0] == "api" else " ".join(argv[:2]))
            return inner(argv, **kw)

        def guard():
            events.append("guard")
            return True
        radar.rerun_unconcluded(SLUG, f2271.SHA, [("tests", 200)], gh=call, guard=guard)
        self.assertEqual(events, ["api", "guard", "run rerun"])

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


class DirectGh(f2271.FakeGh):
    """Le faux `gh` du feu (#2271), avec l'URL du job du check requis, la
    lecture du run 200 et sa relance."""

    def __init__(self, events):
        run = dict(f2271._run("tests", conclusion="cancelled"), details_url=JOB_URL % 200)
        super().__init__(check_runs=[run])
        self.events = events

    def __call__(self, argv, **kwargs):
        if argv[:1] == ["api"] and argv[-1].endswith("/actions/runs/200"):
            self.calls.append(list(argv))
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                dict(_run_row(), id=200)))
        if argv[:2] == ["run", "rerun"]:
            self.calls.append(list(argv))
            self.events.append("rerun")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return super().__call__(argv, **kwargs)


class MergeDirectTest(unittest.TestCase):
    """B2 de la relecture cx2 : la relance du feu passe par la garde du
    pilote élu, relue juste avant la mutation, comme la fusion."""

    ROW = f2271.WiringTest.ROW
    MARKS = [{"kind": "frozen", "sha": f2271.SHA, "actor": "pilote", "at_ts": 0.0, "id": 1},
             {"kind": "fire", "sha": f2271.SHA, "actor": "pilote", "at_ts": 1.0, "id": 2}]

    def merge(self, gate):
        events: list = []
        gh = DirectGh(events)
        with mock.patch.object(fire.work, "note"), \
                mock.patch("ameesh.pilot_gesture.gate",
                           side_effect=lambda *a, **k: gate(events, *a, **k)):
            result = fire._merge_direct(None, None, self.ROW, self.MARKS,
                                        fire.active(self.MARKS),
                                        f2271._status([_aggregate("CANCELLED")]), out={},
                                        slug=SLUG, branch="lot/42-x", target="main",
                                        info={}, repo=None, project=None, gh=gh,
                                        dry_run=False, handle=None)
        return result, events

    def test_the_elected_guard_is_held_before_the_rerun(self):
        fence = mock.Mock()

        def gate(events, cfg, db, gesture, ref, **kw):
            events.append("gate %s" % gesture)
            return fence
        result, events = self.merge(gate)
        self.assertEqual(result["result"], "not-ready")
        self.assertIn("never concluded", result["detail"])
        self.assertEqual(events, ["gate merge", "rerun"])
        fence.hold.assert_called_once()

    def test_a_refused_elected_guard_reruns_nothing(self):
        from ameesh import pilot_gesture

        def gate(events, *a, **k):
            events.append("gate")
            raise pilot_gesture.GestureRefused("stale epoch")
        result, events = self.merge(gate)
        self.assertEqual(events, ["gate"])
        self.assertEqual(result["reruns"][0]["result"], "busy")


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
                self.assertEqual(gh.reruns(), [["run", "rerun", "200", "--repo", banc.SLUG]])
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


def _actions_check(name, conclusion, *, run=300, status="completed", check_id=1, suite=None):
    """Un check run Actions du commit, tel que `commits/<sha>/check-runs` le rend."""
    return {"id": check_id, "name": name, "head_sha": "a" * 40, "status": status,
            "conclusion": conclusion, "app": {"id": 15368, "slug": "github-actions"},
            "check_suite": {"id": run if suite is None else suite},
            "details_url": "https://github.com/o/nexlink/actions/runs/%d/job/9" % run,
            "started_at": "2026-10-12T20:00:00Z", "completed_at": "2026-10-12T20:05:00Z"}


class CommitCiTest(unittest.TestCase):
    """`radar.commit_ci` : seul un agrégat **requis** jamais conclu est une
    preuve absente (B1 de la relecture cx2) ; un workflow facultatif annulé
    reste non concluant."""

    project = radar.Project("n", "/r", github="o/nexlink", ci_required=True)

    def read(self, runs, checks, required=("tests",)):
        return radar.commit_ci(self.project, "a" * 40, required=required,
                               gh=l160g._Gh(runs, checks=checks))

    def test_a_required_aggregate_that_never_concluded_is_absent_proof(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure", "stale"):
            with self.subTest(conclusion=conclusion):
                out = self.read([l160g._run("tests", conclusion="cancelled")],
                                [_actions_check("tests", conclusion)])
                self.assertFalse(out["acceptable"])
                self.assertEqual(out["unconcluded"], [{"check": "tests", "databaseId": 300}])
                self.assertEqual(out["inconclusive"], [])
                self.assertFalse(release_cutoff.reading_is_green(out))

    def test_an_optional_cancelled_workflow_stays_non_conclusive(self):
        # B1 : tests requis vert, workflow facultatif annulé → releasable
        out = self.read([l160g._run("tests"), l160g._run("optional-pages", conclusion="cancelled")],
                        [_actions_check("tests", "success"),
                         _actions_check("optional-pages", "cancelled", run=301, check_id=2)])
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["unconcluded"], [])
        self.assertEqual(out["inconclusive"], ["optional-pages"])

    def test_without_required_checks_a_cancelled_run_stays_non_conclusive(self):
        out = self.read([l160g._run("tests", conclusion="cancelled")],
                        [_actions_check("tests", "cancelled")], required=())
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["unconcluded"], [])
        self.assertEqual(out["inconclusive"], ["tests"])

    def test_a_red_required_aggregate_in_a_cancelled_run_is_negative(self):
        # l'agrégat `if: always()` rougit d'une part annulée ; le run, lui,
        # se dit `cancelled` : la release lit le requis, pas seulement le run.
        out = self.read([l160g._run("tests", conclusion="cancelled")],
                        [_actions_check("tests", "failure")])
        self.assertFalse(out["acceptable"])
        self.assertEqual(out["negative"], ["tests"])
        self.assertEqual(out["unconcluded"], [])

    def test_the_latest_attempt_of_the_required_aggregate_counts(self):
        out = self.read([l160g._run("tests")],
                        [_actions_check("tests", "cancelled", check_id=1),
                         _actions_check("tests", "success", check_id=2)])
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["unconcluded"], [])
        out = self.read([l160g._run("tests", status="in_progress", conclusion=None)],
                        [_actions_check("tests", "cancelled", check_id=1),
                         _actions_check("tests", None, status="in_progress", check_id=2)])
        self.assertFalse(out["acceptable"])
        self.assertEqual(out["unconcluded"], [])

    def test_a_required_check_red_in_one_suite_is_failed_even_if_every_run_is_green(self):
        # B1 cx2 : les runs se disent verts, mais le requis `tests` est rouge
        # dans l'une des deux suites ; l'ordre de lecture n'y change rien.
        for checks in ([_actions_check("tests", "success", run=410, check_id=7),
                        _actions_check("tests", "failure", run=411, check_id=8)],
                       [_actions_check("tests", "failure", run=411, check_id=8),
                        _actions_check("tests", "success", run=410, check_id=7)]):
            with self.subTest(first=checks[0]["conclusion"]):
                out = self.read([l160g._run("A"), l160g._run("B")], checks,
                                required=(("tests", 15368),))
                self.assertIn("tests", out["failed"])
                self.assertEqual(out["negative"], ["tests"])
                self.assertEqual(out["required_negative"], ["tests"])
                self.assertFalse(out["green"])
                self.assertFalse(release_cutoff.reading_is_green(out))

    def test_a_green_workflow_does_not_hide_another_workflow_that_never_concluded(self):
        # R3 cx2 : deux workflows portent un check `tests` (suites distinctes) ;
        # le vert de l'un ne rachète pas l'agrégat annulé de l'autre, quel que
        # soit l'ordre reçu — la porte du paquet refuse le même cas (#3008).
        cancelled = _actions_check("tests", "cancelled", run=300, check_id=1)
        green = _actions_check("tests", "success", run=301, check_id=2)
        for a_run in ("cancelled", "success"):  # le repro de cx2, puis un run qui se dit vert
            runs = [l160g._run("A", conclusion=a_run), l160g._run("B")]
            for checks in ([cancelled, green], [green, cancelled]):
                with self.subTest(run_a=a_run, order=[c["conclusion"] for c in checks]):
                    out = self.read(runs, checks, required=(("tests", 15368),))
                    self.assertFalse(out["acceptable"])
                    self.assertFalse(out["green"])
                    self.assertEqual(out["unconcluded"],
                                     [{"check": "tests", "databaseId": 300}])
                    self.assertFalse(release_cutoff.reading_is_green(out))
        # deux essais de la même suite : le dernier l'emporte toujours
        rerun = _actions_check("tests", "success", run=300, check_id=2)
        out = self.read([l160g._run("A")], [cancelled, rerun], required=(("tests", 15368),))
        self.assertTrue(out["acceptable"])
        self.assertEqual(out["unconcluded"], [])

    def test_an_app_scoped_requirement_reads_only_its_application(self):
        other = dict(_actions_check("tests", "cancelled"), app={"id": 99, "slug": "other"},
                     details_url="https://ci.example/build/1")
        out = self.read([l160g._run("tests")], [_actions_check("tests", "success"), other],
                        required=(("tests", 15368),))
        self.assertEqual(out["unconcluded"], [])
        out = self.read([l160g._run("tests")], [_actions_check("tests", "success"), other],
                        required=(("tests", 99),))
        self.assertEqual(out["unconcluded"], [{"check": "tests", "databaseId": None}])


class AmeeshReleaseReadingTest(unittest.TestCase):
    """B3 d'impl-04 : à la release ameesh, le requis rouge d'une suite que la
    lecture job par job ne choisit pas reste négatif — le filtre des
    workflows reclassés (`tests`, `paquet`, `macos`) ne l'efface pas."""

    project = release_cutoff.ProjectRelease("ameesh", "/unused", github="acme/project")

    def read(self, checks):
        real = radar.commit_ci
        runs = [l160g._run("A"), l160g._run("B")]
        green = t3244.suite(mac="success")
        with mock.patch.object(release_cutoff, "required_checks",
                               return_value=(("tests", 15368),)), \
                mock.patch.object(release_cutoff.radar, "commit_ci",
                                  side_effect=lambda target, sha, **kw: real(
                                      target, sha, gh=l160g._Gh(runs, checks=checks), **kw)), \
                mock.patch.object(release_cutoff, "suite_reading", return_value=green):
            return release_cutoff.ci_reading(self.project, "a" * 40)

    def test_a_red_required_check_of_another_suite_holds_the_release(self):
        red = _actions_check("tests", "failure", run=430, check_id=5)
        green = _actions_check("tests", "success", run=431, check_id=6)
        for checks in ([red, green], [green, red]):
            with self.subTest(first=checks[0]["conclusion"]):
                reading = self.read(checks)
                self.assertIn("tests", reading["negative"])
                self.assertFalse(reading["acceptable"])
                self.assertFalse(reading["green"])
                self.assertFalse(release_cutoff.reading_is_green(reading))

    def test_green_required_checks_still_release(self):
        reading = self.read([_actions_check("tests", "success", run=430, check_id=5),
                             _actions_check("tests", "success", run=431, check_id=6)])
        self.assertEqual(reading["negative"], [])
        self.assertTrue(release_cutoff.reading_is_green(reading), reading)


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
        plan = types.SimpleNamespace(ci=self.READING, head="a" * 40, project=self.project)
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

    def test_the_reading_carries_the_required_checks_of_the_target(self):
        release_cutoff._REQUIRED.clear()
        self.addCleanup(release_cutoff._REQUIRED.clear)
        seen = []
        with mock.patch.object(release_cutoff, "github_slug", return_value=SLUG), \
                mock.patch.object(release_cutoff.plan_github, "Gh"), \
                mock.patch.object(fire, "required_checks",
                                  return_value=(("tests", 15368),)) as read, \
                mock.patch.object(release_cutoff.radar, "commit_ci",
                                  side_effect=lambda target, sha, **kw: seen.append(kw) or {}), \
                mock.patch.object(release_cutoff, "suite_reading", return_value={"runs": []}):
            release_cutoff.ci_reading(self.project, "a" * 40)
            release_cutoff.ci_reading(self.project, "b" * 40)
        self.assertEqual(seen, [{"required": (("tests", 15368),)}] * 2)
        read.assert_called_once()           # relue au plus toutes les 5 min

    def test_unreadable_required_checks_are_transient(self):
        release_cutoff._REQUIRED.clear()
        self.addCleanup(release_cutoff._REQUIRED.clear)
        with mock.patch.object(release_cutoff, "github_slug", return_value=SLUG), \
                mock.patch.object(release_cutoff.plan_github, "Gh"), \
                mock.patch.object(fire, "required_checks",
                                  side_effect=fire.land.TransientLandError("HTTP 502")):
            with self.assertRaises(release_cutoff.TransientReleaseError):
                release_cutoff.ci_reading(self.project, "a" * 40)

    def test_a_forbidden_protection_read_does_not_hold_the_release(self):
        # §3 : un identifiant sans droits est un non concluant ; la tête est
        # lue sans requis connus et le rapport le dit.
        release_cutoff._REQUIRED.clear()
        self.addCleanup(release_cutoff._REQUIRED.clear)
        with mock.patch.object(release_cutoff, "github_slug", return_value=SLUG), \
                mock.patch.object(release_cutoff.plan_github, "Gh"), \
                mock.patch.object(fire, "required_checks",
                                  side_effect=fire.land.ForbiddenLandError("HTTP 403")), \
                mock.patch.object(release_cutoff.radar, "commit_ci",
                                  side_effect=lambda target, sha, **kw: {"required": kw}), \
                mock.patch.object(release_cutoff, "suite_reading", return_value={"runs": []}):
            reading = release_cutoff.ci_reading(self.project, "a" * 40)
        self.assertEqual(reading["required"], {"required": ()})
        self.assertTrue(reading["required_unread"])
        plan = types.SimpleNamespace(ci=reading, head="a" * 40, project=self.project)
        [line] = release_cutoff._unconcluded_deviation(plan, [])
        self.assertIn("illisibles", line)

    def test_nothing_to_rerun_on_a_concluded_head(self):
        plan = types.SimpleNamespace(ci={"green": True}, head="a" * 40, project=self.project)
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
