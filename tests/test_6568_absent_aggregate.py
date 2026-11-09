# SPDX-License-Identifier: AGPL-3.0-only
"""#6568 — un run conclu sans l'agrégat requis est une preuve absente d'infra.

Constat du 09/11 18:19Z : run 42667953303 (#5021, tête `1e8464105aaf`), 33
jobs `success`/`skipped` et aucun job négatif, jobs `tests` (agrégat requis
par la protection de `main`) et `bilan des impactés` jamais créés, run conclu
`failure` une minute après la dernière part. Le run porte l'annotation
« Internal server error. Correlation ID: fdd4064c-… » ; GitHub a déclaré une
panne Actions/Checks (`tp8cghg7tlc4`, 18:12Z). `tests.yml` est identique à
`main` et l'expression `runs-on` n'est pas en cause.

Règle (canon §3) : toutes les parts sont vertes sur la tête gelée mais
l'agrégat requis est absent d'un run conclu sans job négatif → non concluant
d'infrastructure ; le run est relancé **une fois**, avec une trace, au lieu
d'attendre une fusion admin du pilote. La porte reste fermée : jamais un
`tests` vert sans les parts.

Témoins sans réseau ni base : le faux `gh` du feu (#2271) et le banc de paquet
de #2813.
"""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from ameesh import fire, packet_automerge, radar

from . import test_2271_fire_required_checks as f2271
from . import test_2813_packet_auto_merge as banc

SLUG = "manaty/ameesh"
JOB_URL = "https://github.com/manaty/ameesh/actions/runs/%d/job/123"


def _row(run_id=500, *, event="pull_request", conclusion="failure",
         status="completed", head=f2271.SHA, name="tests [PR #1 ci-full]"):
    """Une ligne de `gh run list --commit <head>`."""
    return {"databaseId": run_id, "name": name, "workflowName": "tests",
            "event": event, "status": status, "conclusion": conclusion,
            "headSha": head, "url": "https://github.com/%s/actions/runs/%d"
            % (SLUG, run_id)}


def _job(name, conclusion="success"):
    return {"name": name, "status": "completed", "conclusion": conclusion}


#: les jobs du run du constat : que des parts vertes, l'agrégat n'existe pas.
GREEN_JOBS = [_job("plan"), _job("psycopg 1/8"), _job("psql 3/16"),
              _job("autre système de fichiers", "skipped")]


class Gh:
    """Le peu de `gh` lu : la liste des runs de la tête, leurs jobs, la
    lecture du run à relancer et sa relance."""

    def __init__(self, runs=(), jobs=None, *, list_error="", view_error="",
                 rerun_error=""):
        self.runs = list(runs)
        self.jobs = dict(jobs or {})
        self.list_error, self.view_error = list_error, view_error
        self.rerun_error = rerun_error
        self.attempts: dict[int, int] = {}
        self.states: dict[int, tuple[str, str]] = {}
        self.calls: list[list[str]] = []

    def run(self, argv, stdin=None):
        """La forme `plan_github.Gh.run` (utilisée par `gh run list/view`)."""
        proc = self(argv)
        return proc.returncode, proc.stdout, proc.stderr

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        args = list(argv)
        if args[:2] == ["run", "list"]:
            if self.list_error:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr=self.list_error)
            return types.SimpleNamespace(returncode=0, stderr="",
                                         stdout=json.dumps(self.runs))
        if args[:2] == ["run", "view"]:
            run_id = int(args[2])
            if self.view_error:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr=self.view_error)
            if run_id not in self.jobs:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr="HTTP 404")
            return types.SimpleNamespace(returncode=0, stderr="",
                                         stdout=json.dumps({"jobs": self.jobs[run_id]}))
        if args[:1] == ["api"] and "/actions/runs/" in args[1]:
            run_id = int(args[1].rsplit("/", 1)[1])
            if run_id not in self.jobs:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr="HTTP 404")
            status, conclusion = self.states.get(
                run_id, ("completed", "failure"))
            return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "id": run_id, "head_sha": f2271.SHA, "status": status,
                "conclusion": conclusion,
                "run_attempt": self.attempts.get(run_id, 1)}))
        if args[:2] == ["run", "rerun"]:
            if self.rerun_error:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr=self.rerun_error)
            run_id = int(args[2])
            self.attempts[run_id] = self.attempts.get(run_id, 1) + 1
            self.states[run_id] = ("in_progress", None)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="unexpected gh %s" % " ".join(args))

    def reruns(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["run", "rerun"]]


class CallableGh:
    """La forme de production du feu (`land._gh`) : un appelable, sans `.run`."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = inner.calls

    def __call__(self, argv, **_kwargs):
        proc = self.inner(argv)
        return types.SimpleNamespace(returncode=proc.returncode,
                                     stdout=proc.stdout, stderr=proc.stderr)


class DetectionTest(unittest.TestCase):
    """`radar.failed_run_without_negative` : la signature exacte du constat."""

    def read(self, gh):
        return radar.failed_run_without_negative(SLUG, f2271.SHA, gh=gh)

    def test_a_concluded_run_without_any_negative_job_is_found(self):
        run = self.read(Gh([_row(500)], {500: GREEN_JOBS}))
        self.assertEqual(run["id"], 500)
        self.assertEqual(run["workflow"], "tests")
        self.assertIn("/actions/runs/500", run["url"])

    def test_the_callable_gh_of_the_fire_is_supported(self):
        run = self.read(CallableGh(Gh([_row(500)], {500: GREEN_JOBS})))
        self.assertEqual(run["id"], 500)

    def test_a_run_with_a_negative_job_is_not_an_infra_failure(self):
        jobs = GREEN_JOBS + [_job("psql 4/16", "failure")]
        self.assertIsNone(self.read(Gh([_row(500)], {500: jobs})))

    def test_a_run_without_any_job_proves_nothing(self):
        # `startup_failure` de `macos` (0 check, même seconde) : pas un candidat.
        self.assertIsNone(self.read(Gh([_row(500, conclusion="startup_failure")],
                                       {500: []})))

    def test_a_green_run_another_event_or_another_head_is_ignored(self):
        for run in (_row(500, conclusion="success"),
                    _row(500, event="push"),
                    _row(500, event="pull_request_target"),
                    _row(500, head="f" * 40)):
            with self.subTest(run=run):
                self.assertIsNone(self.read(Gh([run], {500: GREEN_JOBS})))

    def test_an_unreadable_listing_or_jobs_relaunch_nothing(self):
        self.assertIsNone(self.read(Gh([_row(500)], {500: GREEN_JOBS},
                                       list_error="HTTP 502")))
        self.assertIsNone(self.read(Gh([_row(500)], {500: GREEN_JOBS},
                                       view_error="HTTP 502")))

    def test_two_candidates_are_ambiguous_and_relaunch_nothing(self):
        # on ne relance pas au hasard : la trappe reste fermée.
        gh = Gh([_row(500), _row(501)], {500: GREEN_JOBS, 501: GREEN_JOBS})
        self.assertIsNone(self.read(gh))


class ReadyPrTest(unittest.TestCase):
    """`fire.ready_pr` : l'agrégat absent d'un run d'infra entre dans
    `report["unconcluded"]`, la porte reste fermée."""

    def ready(self, gh, *, rollup=(), required=(("tests", None),), report=True):
        out = {} if report else None
        ok, why = fire.ready_pr(f2271._status(list(rollup)), target="main",
                                project=None, frozen=f2271.SHA,
                                required=required, slug=SLUG, gh=gh, report=out)
        return ok, why, out

    def test_the_incident_head_keeps_the_gate_closed_and_queues_one_rerun(self):
        gh = Gh([_row(500)], {500: GREEN_JOBS})
        ok, why, report = self.ready(gh)
        self.assertFalse(ok)
        self.assertIn("tests (absent)", why)
        [(name, entry)] = report["unconcluded"]
        self.assertEqual(name, "tests")
        self.assertEqual(entry["databaseId"], 500)
        self.assertTrue(entry.get("missing_aggregate"))

    def test_a_negative_part_leaves_no_rerun(self):
        gh = Gh([_row(500)], {500: GREEN_JOBS + [_job("psql 4/16", "failure")]})
        ok, why, report = self.ready(gh)
        self.assertFalse(ok)
        self.assertIn("tests (absent)", why)
        self.assertEqual(report["unconcluded"], [])

    def test_a_proven_red_elsewhere_withdraws_the_rerun(self):
        # la relance ne lèverait pas la porte : elle ne coûterait qu'une suite.
        gh = Gh([_row(500)], {500: GREEN_JOBS})
        ok, why, report = self.ready(
            gh, rollup=[f2271._check("lint", "FAILURE")],
            required=(("tests", None), ("lint", None)))
        self.assertFalse(ok)
        self.assertIn("lint (FAILURE)", why)
        self.assertEqual(report["unconcluded"], [])

    def test_a_read_only_gate_does_not_read_the_runs(self):
        # `report=None` : la porte de statut ne paie pas la lecture d'infra.
        gh = Gh([_row(500)], {500: GREEN_JOBS})
        ok, why, _ = self.ready(gh, report=False)
        self.assertFalse(ok)
        self.assertEqual(gh.calls, [])

    def test_no_run_on_the_head_is_not_an_infra_failure(self):
        gh = Gh([], {})
        ok, why, report = self.ready(gh)
        self.assertFalse(ok)
        self.assertEqual(report["unconcluded"], [])
        self.assertTrue(any(call[:2] == ["run", "list"] for call in gh.calls))


def _ready(gh, **kwargs):
    """Le même passage, hors d'une instance de test (pour les autres bancs)."""
    return ReadyPrTest().ready(gh, **kwargs)


class RelaunchTest(unittest.TestCase):
    """`fire.relaunch_unconcluded` : relance unique et trace d'infra."""

    def setUp(self):
        self.notes: list[tuple] = []
        patcher = mock.patch.object(
            fire.work, "note",
            lambda db, lot, text, **kw: self.notes.append((lot, text, kw.get("once"))))
        patcher.start()
        self.addCleanup(patcher.stop)

    def report(self):
        gh = Gh([_row(500)], {500: GREEN_JOBS})
        _ok, _why, out = _ready(gh)
        return out, gh

    def test_the_run_is_rerun_once_and_noted_as_infrastructure(self):
        report, gh = self.report()
        out = fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA, report,
                                        gh=gh, guard=lambda: True)
        self.assertEqual([row["result"] for row in out], ["rerun"])
        self.assertEqual(gh.reruns(), [["run", "rerun", "500", "--repo", SLUG]])
        [(lot, text, once)] = self.notes
        self.assertEqual((lot, once), (7, True))
        self.assertIn("infrastructure non-conclusive", text)
        self.assertIn("#6568", text)
        self.assertIn("absent", text)
        # la tête n'est pas fusionnée et le second passage ne relance plus
        out = fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA, report,
                                        gh=gh, guard=lambda: True)
        self.assertEqual(out[0]["result"], "running")
        self.assertEqual(len(gh.reruns()), 1)

    def test_a_run_already_rerun_is_not_rerun_again(self):
        report, gh = self.report()
        gh.attempts[500] = 2
        gh.states[500] = ("completed", "failure")
        out = fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA, report,
                                        gh=gh, guard=lambda: True)
        self.assertEqual(out[0]["result"], "spent")
        self.assertEqual(gh.reruns(), [])

    def test_a_lost_lease_reruns_nothing(self):
        report, gh = self.report()
        out = fire.relaunch_unconcluded(object(), 7, SLUG, f2271.SHA, report,
                                        gh=gh, guard=lambda: False)
        self.assertEqual(out[0]["result"], "busy")
        self.assertEqual(gh.reruns(), [])


class MissingRunForge(banc.FakeGh):
    """Le banc de #2813 : un run `pull_request` conclu sans agrégat, puis sa
    relance (qui passe en cours) — et aucune fusion."""

    RUN = 500

    def __init__(self, **kwargs):
        kwargs.setdefault("tests", None)  # la tête ne porte aucun check `tests`
        super().__init__(**kwargs)
        self.attempt = 1
        self.run_status = "completed"

    def run(self, argv, stdin=None):
        """La forme `plan_github.Gh.run` (utilisée par `gh run list/view`)."""
        code, out, err = self.answer(list(argv))
        return code, out, err

    def answer(self, args):
        args = list(args)
        path = args[-1] if args else ""
        if args[:2] == ["run", "list"]:
            self.calls.append(args)
            return 0, json.dumps([{
                "databaseId": self.RUN, "name": "tests [PR #%d ci-full]" % banc.NUM,
                "workflowName": "tests", "event": "pull_request",
                "status": "completed", "conclusion": "failure",
                "headSha": self.head,
                "url": "https://github.com/%s/actions/runs/%d" % (SLUG, self.RUN)}]), ""
        if args[:2] == ["run", "view"]:
            self.calls.append(args)
            return 0, json.dumps({"jobs": [
                {"name": "plan", "status": "completed", "conclusion": "success"},
                {"name": "psycopg 1/8", "status": "completed", "conclusion": "success"},
                {"name": "psql 1/16", "status": "completed", "conclusion": "skipped"}]}), ""
        if args[:1] == ["api"] and path.endswith("/actions/runs/%d" % self.RUN):
            self.calls.append(args)
            return 0, json.dumps({"id": self.RUN, "head_sha": self.head,
                                  "status": self.run_status,
                                  "conclusion": "failure" if self.run_status == "completed"
                                  else None,
                                  "run_attempt": self.attempt}), ""
        if args[:2] == ["run", "rerun"]:
            self.calls.append(args)
            self.attempt += 1
            self.run_status = "in_progress"
            return 0, "", ""
        return super().answer(args)

    def reruns(self):
        return [call for call in self.calls if call[:2] == ["run", "rerun"]]



class PacketTest(banc.Journal):
    """L'auto-fusion du paquet : elle attend, relance une fois, ne fusionne
    jamais sur un agrégat absent."""

    def setUp(self):
        super().setUp()
        self.created: list[dict] = []
        patcher = mock.patch.object(packet_automerge.work, "add",
                                    lambda db, **kw: self.created.append(kw))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_absent_aggregate_waits_reruns_once_and_never_merges(self):
        self.armed()
        gh = MissingRunForge()
        out = packet_automerge.check(None, None, banc.LOT, gh=gh)
        self.assertEqual(out["result"], "pending", out)
        self.assertIn("tests (absent)", out["why"])
        self.assertEqual([row["result"] for row in out["reruns"]], ["rerun"])
        self.assertEqual(gh.reruns(), [["run", "rerun", "500", "--repo", banc.SLUG]])
        self.assertEqual(gh.merges(), [])
        self.assertEqual(self.created, [])
        self.assertTrue(any("infrastructure non-conclusive" in note
                            for note in self.notes()))
        # le passage suivant voit la relance en cours : une seule relance
        out = packet_automerge.check(None, None, banc.LOT, gh=gh)
        self.assertEqual(out["reruns"][0]["result"], "running")
        self.assertEqual(len(gh.reruns()), 1)
        self.assertEqual(gh.merges(), [])


if __name__ == "__main__":
    unittest.main()
