# SPDX-License-Identifier: AGPL-3.0-only
"""#4549 : une suite verte plus récente remplace un run rouge **sans test**.

Constat (29/10, h-05 #4523, PR #3466, tête 915d3d230c) : le run 40857172347 a
échoué au `plan` (« the merge-ref base is a565151…, the requested base is
a02b97c9… : the composition moved ») sans exécuter un seul test ; son check
requis `tests` est FAILURE. Le run 40860357427, même workflow et même tête,
même base a02b97c9, a conclu vert à 11:19Z. GitHub tient le check requis pour
satisfait (`mergeStateStatus` UNSTABLE), mais la porte d'ameesh gardait le
rouge : `fire._latest_runs` et `radar.latest_rollup` ne départagent les runs
qu'à l'intérieur d'une même suite (#3008), or chaque déclenchement de workflow
ouvre une suite neuve. L'auto-merge et `work merge-pr` restaient bloqués.

La règle : pour un même workflow et une même tête, une suite **plus récente**
conclue verte, demandée alors que la cible portait déjà son commit courant,
remplace la même vérification d'une suite plus ancienne dont le `plan` a
échoué (ses parts sont sautées, aucun test n'a tourné). Le doute ne verdit
jamais : un rouge de test, une part qui a démarré, deux suites concurrentes,
des dates absentes ou ex aequo, un plan vert non prouvé ou une base de
composition plus ancienne gardent la porte fermée. `work merge-pr` accepte
`UNSTABLE` quand le seul non-vert est un tel run supplanté ou un check
facultatif ; le portier `paquet` et le détail de l'agrégat gardent leurs
portes.

Témoins sans base : les deux suites du constat, la première rouge sur un test,
la suite concurrente, la base périmée, les workflows `tests`/`paquet`, le
rollup avec suites connues et la garde `UNSTABLE` de `merge-pr`.
"""
from __future__ import annotations

import json
import types
import unittest

from ameesh import fire, merge_pr, radar

from tests.test_2271_fire_required_checks import FakeGh

SHA = "915d3d230c8c6303be295f6ec38a266e8f02481d"
APP = 15368
REQUIRED = (("tests", APP),)
BASE = "a02b97c9bc5503b353f8bbd4b6668caa9011e317"


def _run(name, conclusion, *, suite, started, status="completed", app_id=APP):
    """Un check run de la tête, dans la forme de `_head_check_runs` ; la
    forme brute de l'API (`app`, `check_suite`) est jointe pour les lecteurs
    qui la parcourent."""
    return {"name": name, "status": status, "conclusion": conclusion,
            "head_sha": SHA, "app": {"id": app_id},
            "check_suite": {"id": suite},
            "app_id": app_id, "check_suite_id": suite,
            "started_at": started,
            "details_url": "https://github.com/manaty/ameesh/actions/runs/%d/job/1" % suite}


def _plan_failed(suite, started):
    """La suite du run 40857172347 : plan rouge, parts sautées, aucun test."""
    return [
        _run("plan", "failure", suite=suite, started=started),
        _run("version number", "success", suite=suite, started=started),
        _run("psycopg 1/8", "skipped", suite=suite, started=started),
        _run("tests", "failure", suite=suite, started=started),
    ]


def _green(suite, started):
    """La suite du run 40860357427 : plan vert, parts vertes, agrégat vert."""
    return [
        _run("plan", "success", suite=suite, started=started),
        _run("version number", "success", suite=suite, started=started),
        _run("psycopg 1/8", "success", suite=suite, started=started),
        _run("tests", "success", suite=suite, started=started),
    ]


def _test_red(suite, started):
    """Un vrai rouge de test : la part a démarré (flux) et a échoué."""
    return [
        _run("plan", "success", suite=suite, started=started),
        _run("tests (flux) psycopg 1/8", "failure", suite=suite, started=started),
        _run("psycopg 1/8", "failure", suite=suite, started=started),
        _run("tests", "failure", suite=suite, started=started),
    ]


def _paquet_plan_failed(suite, started):
    """Une suite du portier `paquet` au plan rouge : autre workflow."""
    return _plan_failed(suite, started) + [
        _run(radar.PACKET_COVERAGE, "cancelled", suite=suite, started=started),
        _run(radar.PACKET_SUITE, "skipped", suite=suite, started=started),
    ]


class BaseGh(FakeGh):
    """`FakeGh` qui sert la cible courante et sa date de commit (#4549)."""

    def __init__(self, committed, *, runs=None, **kwargs):
        super().__init__(**kwargs)
        self.committed = committed
        #: preuve de run explicite : {id: {workflow_id, created_at, …}}
        self.runs = dict(runs or {})

    def __call__(self, argv, **kwargs):
        path = argv[1] if len(argv) > 1 and isinstance(argv[1], str) else ""
        if argv[:1] == ["api"] and "/git/ref/heads/" in path:
            self.calls.append(list(argv))
            return types.SimpleNamespace(
                returncode=0, stderr="",
                stdout=json.dumps({"object": {"sha": BASE}}))
        if argv[:1] == ["api"] and "/commits/" in path:
            self.calls.append(list(argv))
            return types.SimpleNamespace(
                returncode=0, stderr="",
                stdout=json.dumps({"commit": {"committer": {"date": self.committed}}}))
        if argv[:1] == ["api"] and "/actions/runs/" in path:
            self.calls.append(list(argv))
            try:
                run_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="bad run")
            row = dict(self.runs.get(run_id) or {})
            if not row:
                checks = [run for run in self.check_runs
                          if (run.get("check_suite") or {}).get("id") == run_id
                          and run.get("started_at")]
                if not checks:
                    return types.SimpleNamespace(returncode=1, stdout="",
                                                 stderr="run not found")
                row = {"workflow_id": 374908092,
                       "path": ".github/workflows/tests.yml",
                       "created_at": min(run["started_at"] for run in checks)}
            row.update({"id": run_id, "head_sha": row.get("head_sha", SHA)})
            return types.SimpleNamespace(returncode=0, stderr="",
                                         stdout=json.dumps(row))
        return super().__call__(argv, **kwargs)


def _gh(committed="2026-10-29T10:02:25Z", *, check_runs=(), protection=None,
        runs=None):
    kwargs = {"check_runs": check_runs, "runs": runs}
    if protection is not None:
        kwargs["protection"] = protection
    return BaseGh(committed, **kwargs)


class RequiredGateSuiteSupersessionTest(unittest.TestCase):
    """La porte du feu et de `merge-pr` sur la provenance de la tête."""

    def _gate(self, *provenance, required=REQUIRED, **kwargs):
        return fire._required_gate(required, (), list(provenance), **kwargs)

    def test_a_newer_green_suite_replaces_a_plan_failed_suite(self):
        runs = (_plan_failed(110747286243, "2026-10-29T10:23:27Z")
                + _green(110756051303, "2026-10-29T10:47:45Z"))
        self.assertEqual(self._gate(*runs), ([], []))
        self.assertEqual(self._gate(*reversed(runs)), ([], []))

    def test_the_base_of_the_green_suite_must_be_the_current_target(self):
        runs = (_plan_failed(1, "2026-10-29T10:23:27Z")
                + _green(2, "2026-10-29T10:47:45Z"))
        # la verte est demandée après le commit courant de la cible : elle
        # remplace le plan rouge.
        self.assertEqual(
            self._gate(*runs, slug="manaty/ameesh", head=SHA,
                       gh=_gh("2026-10-29T10:02:25Z", check_runs=runs),
                       target="main"),
            ([], []))
        # le commit courant est arrivé après la demande : la verte a testé une
        # composition plus ancienne, le rouge reste.
        self.assertEqual(
            self._gate(*runs, slug="manaty/ameesh", head=SHA,
                       gh=_gh("2026-10-29T10:55:00Z", check_runs=runs),
                       target="main")[0],
            ["tests (FAILURE)"])

    def test_a_run_from_another_workflow_does_not_replace(self):
        runs = (_plan_failed(1, "2026-10-29T10:23:27Z")
                + _green(2, "2026-10-29T10:47:45Z"))
        gh = _gh(check_runs=runs, runs={
            2: {"workflow_id": 999, "path": ".github/workflows/other.yml",
                "created_at": "2026-10-29T10:47:45Z"}})
        self.assertEqual(
            self._gate(*runs, slug="manaty/ameesh", head=SHA, gh=gh,
                       target="main")[0],
            ["tests (FAILURE)"])

    def test_a_queued_run_requested_before_the_target_commit_does_not_open(self):
        runs = (_plan_failed(1, "2026-10-29T10:23:27Z")
                + _green(2, "2026-10-29T10:47:45Z"))
        # le job a démarré après le commit courant, mais le run a été demandé
        # avant : sa composition ne porte pas la cible courante.
        gh = _gh(check_runs=runs, runs={
            2: {"workflow_id": 374908092, "path": ".github/workflows/tests.yml",
                "created_at": "2026-10-29T09:50:00Z"}})
        self.assertEqual(
            self._gate(*runs, slug="manaty/ameesh", head=SHA, gh=gh,
                       target="main")[0],
            ["tests (FAILURE)"])

    def test_a_red_test_still_blocks(self):
        runs = (_test_red(1, "2026-10-29T10:00:00Z")
                + _green(2, "2026-10-29T10:30:00Z"))
        self.assertEqual(self._gate(*runs)[0], ["tests (FAILURE)"])

    def test_a_concurrent_suite_keeps_the_gate_closed(self):
        runs = _plan_failed(1, "2026-10-29T10:00:00Z") + [
            _run("plan", "success", suite=2, started="2026-10-29T10:30:00Z"),
            _run("tests", "", suite=2, started="2026-10-29T10:40:00Z",
                 status="in_progress"),
        ]
        # la suite concurrente n'est jamais verte : le rouge ancien reste.
        self.assertTrue(self._gate(*runs)[0])

    def test_a_newer_plan_failure_does_not_replace_an_older_green(self):
        runs = (_green(1, "2026-10-29T10:00:00Z")
                + _plan_failed(2, "2026-10-29T10:30:00Z"))
        self.assertEqual(self._gate(*runs)[0], ["tests (FAILURE)"])

    def test_missing_dates_keep_the_failure(self):
        broken = _green(2, "2026-10-29T10:30:00Z")
        broken[0]["started_at"] = None
        runs = _plan_failed(1, "2026-10-29T10:00:00Z") + broken
        self.assertEqual(self._gate(*runs)[0], ["tests (FAILURE)"])

    def test_a_tie_keeps_the_failure(self):
        runs = (_plan_failed(1, "2026-10-29T10:00:00Z")
                + _green(2, "2026-10-29T10:00:00Z"))
        self.assertEqual(self._gate(*runs)[0], ["tests (FAILURE)"])

    def test_a_green_suite_without_a_verified_plan_does_not_open(self):
        green = [run for run in _green(2, "2026-10-29T10:30:00Z")
                 if run["name"] != "plan"]
        runs = _plan_failed(1, "2026-10-29T10:00:00Z") + green
        self.assertEqual(self._gate(*runs)[0], ["tests (FAILURE)"])

    def test_another_workflow_does_not_replace_the_failure(self):
        runs = (_paquet_plan_failed(1, "2026-10-29T10:00:00Z")
                + _green(2, "2026-10-29T10:30:00Z"))
        self.assertEqual(self._gate(*runs)[0], ["tests (FAILURE)"])

    def test_a_run_without_a_suite_is_never_masked(self):
        old = _plan_failed(1, "2026-10-29T10:00:00Z")
        old[-1]["check_suite_id"] = None
        runs = old + _green(2, "2026-10-29T10:30:00Z")
        self.assertEqual(self._gate(*runs)[0], ["tests (FAILURE)"])

    def test_merge_pr_strict_gap_uses_the_same_rule(self):
        runs = (_plan_failed(1, "2026-10-29T10:00:00Z")
                + _green(2, "2026-10-29T10:30:00Z"))
        self.assertEqual(
            fire._required_gap(REQUIRED, (), runs, slug="manaty/ameesh",
                               gh=_gh(check_runs=runs), head=SHA, target="main"),
            [])


class LatestRollupSuiteSupersessionTest(unittest.TestCase):
    """`radar.latest_rollup` quand le rollup porte les suites (GraphQL)."""

    @staticmethod
    def _rollup(runs, workflow="tests"):
        return [{"__typename": "CheckRun", "workflowName": workflow,
                 "name": run["name"], "status": run["status"].upper(),
                 "conclusion": run["conclusion"].upper(),
                 "startedAt": run["started_at"],
                 "checkSuiteId": run["check_suite"]["id"]} for run in runs]

    def test_the_whole_superseded_suite_is_dropped(self):
        rollup = self._rollup(_plan_failed(1, "2026-10-29T10:00:00Z")
                              + _green(2, "2026-10-29T10:30:00Z"))
        rows = radar.latest_rollup(rollup)
        self.assertNotIn(1, [row["checkSuiteId"] for row in rows])
        self.assertTrue(radar.ci_green(rollup))
        self.assertEqual(radar.ci_state(rollup), "green")

    def test_a_red_test_keeps_the_old_suite(self):
        rollup = self._rollup(_test_red(1, "2026-10-29T10:00:00Z")
                              + _green(2, "2026-10-29T10:30:00Z"))
        rows = radar.latest_rollup(rollup)
        self.assertIn("FAILURE", [row["conclusion"] for row in rows])
        self.assertFalse(radar.ci_green(rollup))
        self.assertEqual(radar.ci_state(rollup), "red")

    def test_two_workflows_do_not_replace_each_other(self):
        rollup = (self._rollup(_plan_failed(1, "2026-10-29T10:00:00Z"))
                  + self._rollup(_green(2, "2026-10-29T10:30:00Z"),
                                 workflow="paquet"))
        rows = radar.latest_rollup(rollup)
        self.assertIn(1, [row["checkSuiteId"] for row in rows])
        self.assertFalse(radar.ci_green(rollup))


class MergePrUnstableTest(unittest.TestCase):
    """`work merge-pr` : UNSTABLE toléré pour un facultatif ou un supplanté."""

    @staticmethod
    def _view(rollup, state="UNSTABLE"):
        return {"number": 3466, "state": "OPEN", "isDraft": False,
                "baseRefName": "main", "headRefOid": SHA,
                "headRefName": "integration/paquet-h-20261029-05",
                "mergeStateStatus": state, "statusCheckRollup": rollup}

    def test_unstable_with_an_optional_non_green_is_accepted(self):
        rollup = [
            {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED",
             "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "couverture (PR ouverte sur cette tête)",
             "workflowName": "paquet", "status": "COMPLETED",
             "conclusion": "CANCELLED"},
        ]
        merge_pr._pr_guards(self._view(rollup), 3466, SHA, "main", project=None)
        self.assertTrue(merge_pr._unstable_ci_ok(rollup))

    def test_blocked_is_still_refused(self):
        with self.assertRaises(merge_pr.MergePrError):
            merge_pr._pr_guards(self._view([], state="BLOCKED"), 3466, SHA,
                                "main", project=None)

    def test_a_packet_failure_keeps_the_unstable_gate_closed(self):
        rollup = [
            {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED",
             "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "suite complète du paquet",
             "workflowName": "paquet", "status": "COMPLETED",
             "conclusion": "FAILURE"},
        ]
        self.assertFalse(merge_pr._unstable_ci_ok(rollup))


class MergePrRequiredRefusalTest(unittest.TestCase):
    """La lecture complète de `merge-pr` sur les données du constat."""

    PROTECTION = {"required_status_checks": {
        "strict": False, "contexts": ["tests"],
        "checks": [{"context": "tests", "app_id": APP}]}}

    def _refusal(self, *provenance, committed="2026-10-29T10:02:25Z"):
        gh = _gh(committed, check_runs=list(provenance),
                 protection=self.PROTECTION)
        return merge_pr._required_refusal("manaty/ameesh", "main", None, (),
                                          head=SHA, gh=gh)

    def test_the_incident_passes_the_required_gate(self):
        self.assertEqual(
            self._refusal(*_plan_failed(1, "2026-10-29T10:23:27Z"),
                          *_green(2, "2026-10-29T10:47:45Z")), "")

    def test_a_red_test_is_still_refused(self):
        refusal = self._refusal(*_test_red(1, "2026-10-29T10:00:00Z"),
                                *_green(2, "2026-10-29T10:30:00Z"))
        self.assertIn("tests (FAILURE)", refusal)


if __name__ == "__main__":
    unittest.main()
