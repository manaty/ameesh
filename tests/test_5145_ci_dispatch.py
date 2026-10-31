# SPDX-License-Identifier: AGPL-3.0-only
"""Multi-workflow frozen-head requests against an in-memory GitHub API."""
import argparse
import base64
import contextlib
import io
import json
import types
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from ameesh import ci, plan_github, work
from tests import test_l161g_ci_gel as fixtures

REPO = "example/product"
SUITES = tuple("suite-%02d.yml" % n for n in range(12))
WORKFLOW = "on:\n  workflow_dispatch:\n    inputs:\n      expected_sha:\n        required: true\n        type: string\n"


class Api:
    def __init__(self, suites=SUITES):
        self.suites = suites
        self.paths = []
        self.dispatches = []
        self.runs = []
        self.head = fixtures.GEL
        self.fail = None
        self.planner = False

    def __call__(self, path):
        self.paths.append(path)
        assert path.startswith("repos/" + REPO + "/"), path
        if "/branches/" in path:
            return {"commit": {"sha": self.head if path.endswith(ci._q(fixtures.BRANCHE))
                                else fixtures.BASE}}
        if "/commits/" in path:
            return {"commit": {"message": "frozen package"}}
        if "/pulls?" in path:
            return [fixtures.pull()]
        if path.endswith("actions/workflows?per_page=100"):
            return {"workflows": [{"path": ".github/workflows/" + w, "state": "active"}
                                  for w in self.suites]}
        if "/contents/" in path:
            content = WORKFLOW
            if "tests.yml" in path or "dispatch-due-parts.mjs" in path and not self.planner:
                raise plan_github.GithubError("HTTP 404 not found")
            return {"content": base64.b64encode(content.encode()).decode()}
        if "/runs?" in path:
            workflow = path.split("/workflows/")[1].split("/")[0]
            query = parse_qs(urlsplit(path).query)
            return {"workflow_runs": [r for r in self.runs
                                      if r["path"].endswith("/" + unquote(workflow))
                                      and all(r.get(k) == v[0] for k, v in query.items()
                                              if k in ("head_sha", "event"))
                                      and (not query.get("branch") or
                                           r["head_branch"] == query["branch"][0])]}
        raise AssertionError(path)

    def run(self, argv, stdin=None):
        if argv == ["api", "user", "--jq", ".login"]:
            return 0, "tester", ""
        if argv[-3].endswith("/dispatches"):
            workflow = argv[-3].split("/workflows/")[1].split("/")[0]
            self.dispatches.append((workflow, json.loads(stdin)))
            return (1, "", "timeout") if workflow == self.fail else (0, "", "")
        raise AssertionError(argv)

    def materialize(self, count=12, conclusion="success"):
        self.runs = [dict(id=100+i, path=".github/workflows/" + w,
                          head_sha=fixtures.GEL, head_branch=fixtures.BRANCHE,
                          event="workflow_dispatch", status="completed",
                          conclusion=conclusion) for i, w in enumerate(SUITES[:count])]


class DispatchTest(fixtures.GelTestCase):
    def setUp(self):
        super().setUp()
        self.gele()
        self.item["issue_ref"] = REPO + "#1"
        self.cfg = types.SimpleNamespace(github={"projects": {"product": {
            "repo": REPO, "workflows": list(SUITES)}}})
        self.notes = []
        self.work.enter_context(mock.patch.object(work, "note", self.note))

    def note(self, db, ident, text, **kwargs):
        self.notes.append(text)
        self.events.append({"id": len(self.events)+1, "note": text, "created_ts": 1e12})

    def plan(self, api, **kwargs):
        return ci.gel_plan(self.db, self.cfg, 1343, api=api, full=True,
                           reason="package full suite", repo=REPO, **kwargs)

    def request(self, api, decision, force=False):
        return ci._request_once(self.db, 1343, decision, api=api, gh=api, force=force)

    def test_twelve_dispatches_frozen_sha_one_applied_request_and_green_only_when_complete(self):
        api = Api()
        decision = self.plan(api)
        self.assertEqual(decision["path"], "workflow-dispatch")
        result = self.request(api, decision)
        self.assertTrue(result["requested"])
        self.assertEqual(len(api.dispatches), 12)
        self.assertEqual({w for w, _ in api.dispatches}, set(SUITES))
        for _, payload in api.dispatches:
            self.assertEqual(payload, {"ref": fixtures.BRANCHE,
                                      "inputs": {"expected_sha": fixtures.GEL}})
        self.assertEqual(len([n for n in self.notes if n.startswith("ci-label applied")]), 1)
        self.assertEqual(self.plan(api)["action"], "running")
        api.materialize(count=11)
        with self.assertRaisesRegex(ci.GelError, "none green"):
            self.plan(api)
        api.materialize()
        self.assertEqual(self.plan(api)["action"], "green")
        self.assertFalse(self.request(api, decision)["requested"])
        self.assertEqual(len(api.dispatches), 12)
        self.assertFalse(any("tests.yml/runs" in p for p in api.paths))

    def test_full_cli_resolves_dot_and_dispatches_the_selected_workflows(self):
        api = Api()
        args = argparse.Namespace(id=1343, sha=fixtures.GEL, avis=1, exception="",
                                  reason="full packet suite", full=True, force=False,
                                  repo=".", dry_run=False, json=True)
        output = io.StringIO()
        with mock.patch("ameesh.db.open_db", return_value=self.db), \
             mock.patch.object(plan_github, "Gh", return_value=api), \
             mock.patch.object(plan_github, "_json", side_effect=lambda gh, argv: api(argv[-1])), \
             mock.patch.object(ci, "_local_repo", return_value=REPO), \
             contextlib.redirect_stdout(output):
            self.assertEqual(ci.cmd_ci_run(self.cfg, args), 0)
        self.assertTrue(json.loads(output.getvalue())["requested"])
        self.assertEqual(len(api.dispatches), 12)

    def test_foreign_success_is_not_a_green_request(self):
        api = Api()
        api.materialize()
        with self.assertRaisesRegex(ci.GelError, "cannot be checked"):
            self.plan(api)
        self.assertEqual(self.plan(api, force=True)["action"], "request")

    def test_base_change_cannot_reuse_dispatch_green(self):
        api = Api()
        decision = self.plan(api)
        self.request(api, decision)
        api.materialize()
        self.assertEqual(self.plan(api)["action"], "green")
        with mock.patch.object(ci, "_pr_of", return_value={"number": fixtures.PR,
                "head": fixtures.GEL, "base": fixtures.AUTRE, "head_ref": fixtures.BRANCHE,
                "base_ref": "main", "labels": []}):
            with self.assertRaisesRegex(ci.GelError, "cannot be checked"):
                self.plan(api)

    def test_head_changed_under_lock_prevents_all_dispatches(self):
        api = Api()
        decision = self.plan(api)
        api.head = fixtures.TETE
        result = self.request(api, decision)
        self.assertFalse(result["requested"])
        self.assertEqual(api.dispatches, [])

    def test_failed_dispatch_keeps_intent_and_stops(self):
        api = Api()
        decision = self.plan(api)
        api.fail = SUITES[1]
        result = self.request(api, decision)
        self.assertFalse(result["requested"])
        self.assertIn("intent retained", result["why"])
        self.assertEqual(len(api.dispatches), 2)
        self.assertEqual(self.plan(api)["action"], "running")

    def test_missing_workflows_and_missing_guard_refuse_before_any_dispatch(self):
        api = Api(suites=())
        self.cfg.github = {}
        with self.assertRaisesRegex(ci.GelError, "no suite workflow"):
            self.plan(api)
        api = Api()
        original = api.__call__
        def unguarded(path):
            if "/contents/.github/workflows/suite-" in path:
                return {"content": base64.b64encode(b'on:\n  workflow_dispatch: {}\n').decode()}
            return original(path)
        with self.assertRaisesRegex(ci.GelError, "expected_sha"):
            self.plan(unguarded)
        self.assertEqual(api.dispatches, [])

    def test_frozen_head_mismatch_refuses(self):
        api = Api()
        api.head = fixtures.TETE
        with self.assertRaisesRegex(ci.GelError, "ref moved"):
            self.plan(api, sha=fixtures.GEL)
        self.assertEqual(api.dispatches, [])

    def test_runs_query_and_cancel_read_declared_workflows(self):
        api = Api()
        api.materialize()
        api.runs[0].update(status="queued", head_sha=fixtures.TETE)
        result = ci.cancel_plan(self.db, self.cfg, 1343, api=api, workflows=SUITES, repo=REPO)
        self.assertEqual([r["id"] for r in result["runs"]], [100])
        self.assertFalse(any("tests.yml/runs" in p for p in api.paths))

    def test_repository_planner_argv_is_used_without_copying_part_list(self):
        api = Api()
        api.planner = True
        report = {"schema": "nexlink-dispatch-due-parts/1", "ref": fixtures.BRANCHE,
                  "head": fixtures.GEL, "dryRun": True, "failures": [],
                  "parts": [{"workflow": w} for w in SUITES],
                  "argv": [{"argv": ["workflow", "run", w, "--ref", fixtures.BRANCHE,
                                      "-f", "expected_sha=" + fixtures.GEL]} for w in SUITES]}
        with mock.patch("ameesh.prevalidation_calls.repository", return_value="/clone"), \
             mock.patch("tempfile.mkdtemp", return_value="/folder"), \
             mock.patch("ameesh.release_cutoff._git"), \
             mock.patch("ameesh.release_cutoff._drop_worktree") as drop, \
             mock.patch("subprocess.run", return_value=types.SimpleNamespace(
                 returncode=0, stdout=json.dumps(report), stderr="")) as run:
            self.assertEqual(set(self.plan(api)["dispatch"]), set(SUITES))
            self.assertEqual(run.call_args.args[0], ["node", "scripts/ci/dispatch-due-parts.mjs",
                             fixtures.BRANCHE, fixtures.GEL, "--dry-run"])
            self.assertEqual(run.call_args.kwargs["cwd"], "/folder/tree")
            drop.assert_called_once_with("/clone", "/folder", "/folder/tree")

    def test_registered_workflow_missing_refuses_before_dispatch(self):
        api = Api(suites=SUITES[:-1])
        with self.assertRaisesRegex(ci.GelError, "not registered"):
            self.plan(api)
        self.assertEqual(api.dispatches, [])

    def test_discovery_works_without_declared_list(self):
        self.cfg.github = {}
        api = Api()
        self.assertEqual(set(self.plan(api)["dispatch"]), set(SUITES))

    def test_planner_present_but_clone_missing_never_falls_back(self):
        api = Api()
        api.planner = True
        with mock.patch("ameesh.prevalidation_calls.repository", return_value=""):
            with self.assertRaisesRegex(ci.GelError, "needs a local clone"):
                self.plan(api)
        self.assertEqual(api.dispatches, [])

    def test_branch_moves_between_dispatches_stops_and_records_outcome(self):
        api = Api()
        decision = self.plan(api)
        original = api.run
        def run(argv, stdin=None):
            result = original(argv, stdin)
            if api.dispatches:
                api.head = fixtures.TETE
            return result
        api.run = run
        result = self.request(api, decision)
        self.assertFalse(result["requested"])
        self.assertIn("branch moved", result["why"])
        self.assertEqual(len(api.dispatches), 1)
        self.assertTrue(any("CI workflow dispatch outcome: branch moved" in n for n in self.notes))

    def test_repeat_before_runs_materialize_does_not_dispatch_twice(self):
        api = Api()
        decision = self.plan(api)
        self.assertTrue(self.request(api, decision)["requested"])
        self.assertFalse(self.request(api, decision)["requested"])
        self.assertEqual(len(api.dispatches), 12)


class RepoTest(unittest.TestCase):
    def test_dot_and_absent_repo_resolve_origin(self):
        for remote in ("git@github.com:example/product.git", "https://github.com/example/product.git"):
            with self.subTest(remote=remote), mock.patch("subprocess.run", return_value=
                    types.SimpleNamespace(returncode=0, stdout=remote)):
                self.assertEqual(ci.repo_of(None, {}, repo="."), REPO)
                self.assertEqual(ci.repo_of(None, {}), REPO)

    def test_issue_ref_outside_git_does_not_read_origin(self):
        with tempfile.TemporaryDirectory(prefix="ameesh-5145-") as directory, \
             contextlib.chdir(directory), \
             mock.patch.object(ci, "_local_repo", side_effect=AssertionError("origin not needed")):
            self.assertEqual(ci.repo_of(None, {"issue_ref": "manaty/nexlink#12"}),
                             "manaty/nexlink")

    def test_nexlink_lot_from_ameesh_clone_uses_issue_ref(self):
        with mock.patch.object(ci, "_local_repo", return_value="manaty/ameesh") as origin:
            self.assertEqual(ci.repo_of(None, {"issue_ref": "manaty/nexlink#12"}),
                             "manaty/nexlink")
            origin.assert_not_called()

    def test_configured_project_outside_git_and_issue_ref_precedence(self):
        cfg = types.SimpleNamespace(github={"projects": {"nexlink": "manaty/nexlink"}})
        with tempfile.TemporaryDirectory(prefix="ameesh-5145-") as directory, \
             contextlib.chdir(directory), \
             mock.patch.object(ci, "_local_repo", side_effect=AssertionError("origin not needed")):
            self.assertEqual(ci.repo_of(cfg, {"app": "nexlink"}), "manaty/nexlink")
            self.assertEqual(ci.repo_of(cfg, {"app": "nexlink", "issue_ref": REPO + "#12"}), REPO)

    def test_dot_refuses_an_origin_diverging_from_lot(self):
        with mock.patch.object(ci, "_local_repo", return_value="manaty/ameesh"):
            with self.assertRaisesRegex(ci.GelError, "differs from lot"):
                ci.repo_of(None, {"id": 5141, "issue_ref": "manaty/nexlink#12"}, repo=".")
            self.assertEqual(ci.repo_of(None, {"issue_ref": "manaty/nexlink#12"},
                                        repo="example/product"), REPO)

    def test_unreadable_or_invalid_origin_refuses(self):
        with mock.patch("subprocess.run", return_value=types.SimpleNamespace(returncode=1, stdout="")):
            with self.assertRaisesRegex(ci.GelError, "cannot resolve origin"):
                ci.repo_of(None, {}, repo=".")
        with self.assertRaises(plan_github.GithubError):
            ci.repo_of(None, {}, repo="../unknown")
