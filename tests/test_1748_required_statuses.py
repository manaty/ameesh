"""#1748 (a): a Nexlink package head is green only with a `lot/*` status.

The package bootstrapper (`package-new-suites.yml`) runs on every
`integration/**` push. On a change set that makes no part due, it ends with
`success` and `skipped` checks only, and no `lot/*` status: the merge gate
must not read that rollup green (R1 Claude ameesh-dev7 of nexlink#1188 on
db1c0c66, option (a) of the orchestrator, 15/10/2026).
"""
from __future__ import annotations

import json
import types
import unittest

from ameesh import land, merge_pr, merge_queue, radar

from .test_l160g_nexlink import _Gh, _run

SHA = "a" * 40
PROJECT = radar.Project("nexlink", "/r", github="o/nexlink", ci_required=True,
                        required_statuses=(("integration/**", "lot/"),))


def _check(name, conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": "COMPLETED",
            "conclusion": conclusion}


def _status(context, state="SUCCESS"):
    return {"__typename": "StatusContext", "context": context, "state": state}


#: what the bootstrapper alone leaves on a documentation-only package
BOOTSTRAP_ONLY = [_check("lot-plan / Due parts of the change set"),
                  _check("lot-plan / Announce the due parts still missing a status"),
                  _check("civil-crypto", "SKIPPED"), _check("mobile-native-harness", "SKIPPED"),
                  _check("complete")]


class SettingsTest(unittest.TestCase):
    def test_declared_and_refused(self):
        settings = radar.parse_settings({"projects": {"nexlink": {
            "repo": "/r", "required_statuses": {"integration/**": "lot/"}}}})
        self.assertEqual(settings.projects["nexlink"].required_statuses,
                         (("integration/**", "lot/"),))
        for bad in (["lot/"], {"integration/**": ""}, {"": "lot/"}, {"integration/**": 1}):
            with self.subTest(bad=bad), self.assertRaises(radar.SettingsError):
                radar.parse_settings({"projects": {"nexlink": {
                    "repo": "/r", "required_statuses": bad}}})

    def test_branch_selection(self):
        self.assertEqual(radar.required_statuses(PROJECT, "integration/hotfix-1748"), ("lot/",))
        self.assertEqual(radar.required_statuses(PROJECT, "refs/heads/integration/paquet-9"),
                         ("lot/",))
        self.assertEqual(radar.required_statuses(PROJECT, "agent/x"), ())
        # an unknown branch never lowers the gate
        self.assertEqual(radar.required_statuses(PROJECT, None), ("lot/",))
        self.assertEqual(radar.required_statuses(None, None), ())
        self.assertEqual(radar.required_statuses(radar.Project("p", "/r"), None), ())


class RollupTest(unittest.TestCase):
    required = ("lot/",)

    def test_bootstrapper_alone_is_not_green(self):
        # the red witness: without (a), this rollup read green
        self.assertTrue(radar.ci_green(BOOTSTRAP_ONLY))
        self.assertFalse(radar.ci_green(BOOTSTRAP_ONLY, self.required))

    def test_a_concluded_lot_status_is_required(self):
        self.assertTrue(radar.ci_green(BOOTSTRAP_ONLY + [_status("lot/docs-gate")],
                                       self.required))
        self.assertFalse(radar.ci_green(BOOTSTRAP_ONLY + [_status("lot/docs-gate"),
                                                          _status("lot/backend-suite", "PENDING")],
                                        self.required))
        self.assertFalse(radar.ci_green([_status("lot/backend-suite", "FAILURE")], self.required))

    def test_only_a_commit_status_counts(self):
        # a job name is free text: a check run named lot/… vouches for nothing
        self.assertFalse(radar.ci_green(BOOTSTRAP_ONLY + [_check("lot/docs-gate")],
                                        self.required))
        self.assertFalse(radar.ci_green(BOOTSTRAP_ONLY + [_status("ci/docs-gate")],
                                        self.required))


class PrStatusTest(unittest.TestCase):
    def status(self, rollup, branch="integration/paquet-9", project=PROJECT):
        rows = [{"number": 7, "headRefOid": SHA, "baseRefName": "develop",
                 "statusCheckRollup": rollup}]
        gh = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout=json.dumps(rows),
                                                      stderr="")
        return land.pr_status("o/nexlink", branch, gh=gh, project=project)["ci"]

    def test_land_waits_for_the_lot_status(self):
        self.assertEqual(self.status(BOOTSTRAP_ONLY), "pending")
        self.assertEqual(self.status(BOOTSTRAP_ONLY + [_status("lot/docs-gate")]), "green")
        self.assertEqual(self.status(BOOTSTRAP_ONLY + [_check("x", "FAILURE")]), "red")
        self.assertEqual(self.status(BOOTSTRAP_ONLY, branch="lot/1-x"), "green")
        self.assertEqual(self.status(BOOTSTRAP_ONLY, project=None), "green")


class CommitCiTest(unittest.TestCase):
    def test_runs_alone_do_not_certify_a_package(self):
        gh = _Gh([_run("Package suites awaiting main"), _run("x", conclusion="skipped")])
        out = radar.commit_ci(PROJECT, SHA, gh=gh, branch="integration/paquet-9")
        self.assertFalse(out["green"])
        self.assertEqual(out["missing"], ["lot/"])
        self.assertTrue(radar.commit_ci(PROJECT, SHA, gh=gh, branch="lot/1-x")["green"])
        self.assertFalse(radar.commit_ci(PROJECT, SHA, gh=gh)["green"])
        gh.statuses = [{"context": "lot/docs-gate", "state": "success"}]
        self.assertTrue(radar.commit_ci(PROJECT, SHA, gh=gh, branch="integration/paquet-9")["green"])

    def test_queue_names_the_missing_status(self):
        gh = _Gh([_run("Package suites awaiting main")])
        reader = lambda sha: radar.commit_ci(PROJECT, sha, gh=gh, branch="integration/paquet-9")
        with self.assertRaisesRegex(merge_queue.QueueError, r"no successful lot/\* status"):
            merge_queue._required_ci(PROJECT, SHA, reader, branch="integration/paquet-9")


class MergePrTest(unittest.TestCase):
    def test_direct_merge_guard(self):
        view = {"state": "OPEN", "isDraft": False, "headRefOid": SHA,
                "headRefName": "integration/paquet-9", "baseRefName": "develop",
                "mergeStateStatus": "CLEAN", "statusCheckRollup": BOOTSTRAP_ONLY}
        with self.assertRaisesRegex(merge_pr.MergePrError, "not all concluded and green"):
            merge_pr._pr_guards(view, 7, SHA, "develop", PROJECT)
        merge_pr._pr_guards(dict(view, statusCheckRollup=BOOTSTRAP_ONLY + [_status("lot/x")]),
                            7, SHA, "develop", PROJECT)
        # an unreadable head branch gets every declared prefix
        with self.assertRaises(merge_pr.MergePrError):
            merge_pr._pr_guards(dict(view, headRefName=""), 7, SHA, "develop", PROJECT)


if __name__ == "__main__":
    unittest.main()
