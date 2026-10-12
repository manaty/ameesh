# SPDX-License-Identifier: AGPL-3.0-only
"""L160g (#473): the conditions of the Nexlink orchestrator on L160f.

Generated files regenerated on the merged tree, never resolved; gates chosen
by the paths the merge touches, GitHub CI green on the frozen commit before
entering the queue; SQL migrations reserved through ameesh; protected targets
(main, release branches, tags) never merged by the queue."""
from __future__ import annotations

import dataclasses
import json
import os

from ameesh import land, merge_queue, plan_github, radar, resolutions

from .test_l160b_radar import _git
from .test_l160e2_land import _Land

import unittest

NEXLINK = {"repo": "/srv/nexlink", "target": "develop", "land": "local",
           "gates": ["backend/scripts/check-rollbacks.sh",
                     {"run": "npm run domains-export -- --check",
                      "paths": ["backend/domains/**", "backend/domains.json"]}],
           "generated": [{"path": "backend/domains.json", "regen": "npm run domains-export",
                          "check": "npm run domains-export -- --check"}],
           "ci_required": True,
           "protected": ["main", "desktop-v*", "nexlink-releases*", "refs/tags/*"]}


class SettingsTest(unittest.TestCase):
    def test_nexlink(self):
        project = radar.parse_settings({"projects": {"nexlink": NEXLINK}}).projects["nexlink"]
        self.assertEqual(project.gates, ("backend/scripts/check-rollbacks.sh",))
        self.assertEqual(project.path_gates, (radar.Gate(
            "npm run domains-export -- --check",
            ("backend/domains/**", "backend/domains.json")),))
        self.assertEqual(project.generated, (radar.Generated(
            "backend/domains.json", "npm run domains-export",
            "npm run domains-export -- --check"),))
        self.assertTrue(project.ci_required)
        self.assertEqual(project.protected[0], "main")

    def test_invalid(self):
        for key, value in (("generated", [{"path": "x"}]), ("generated", ["x"]),
                           ("gates", [{"run": "x", "paths": [1]}]),
                           ("gates", [{"run": "x", "when": ["a"]}]),
                           ("ci_required", "yes"), ("protected", [""])):
            with self.subTest(key=key, value=value):
                with self.assertRaises(radar.SettingsError):
                    radar.parse_settings({"projects": {"p": dict(NEXLINK, **{key: value})}})

    def test_a_landed_project_has_a_gate_of_some_kind(self):
        only_paths = dict(NEXLINK, gates=[{"run": "x", "paths": ["a/**"]}])
        radar.parse_settings({"projects": {"p": only_paths}})
        with self.assertRaises(radar.SettingsError):
            radar.parse_settings({"projects": {"p": dict(NEXLINK, gates=[])}})


class ProtectedTest(unittest.TestCase):
    def test_globs(self):
        project = radar.parse_settings({"projects": {"n": NEXLINK}}).projects["n"]
        for target in ("main", "origin/main", "desktop-v1.4", "nexlink-releases",
                       "nexlink-releases/2026-10", "refs/tags/v0.2.11"):
            self.assertIsNotNone(radar.protected_target(project, target), target)
        for target in ("develop", "origin/develop", "feature/main"):
            self.assertIsNone(radar.protected_target(project, target), target)
        self.assertIsNone(radar.protected_target(radar.Project("p", "/r"), "main"))


class ProtectedQueueTest(_Land):
    def protect(self):
        self.project = dataclasses.replace(self.project, protected=("main",))

    def test_queue_add_refused(self):
        self.ready(4)
        self.protect()
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        with self.assertRaisesRegex(merge_queue.QueueError, "protected target"):
            merge_queue.enter(None, 4, repo=self.repo, actor="orch", ci=None, clone=clone,
                              project=self.project)
        self.assertNotIn(4, self.fake.queue_rows)

    def test_land_ejects_a_lot_queued_before(self):
        self.queued(4)
        self.protect()
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("protected target", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)


class _Gh:
    """`gh run list` answering `rows` (or failing)."""

    def __init__(self, rows=None, code=0):
        self.rows, self.code, self.calls = rows or [], code, []

    def run(self, argv, stdin=None):
        self.calls.append(argv)
        return self.code, json.dumps(self.rows), "boom" if self.code else ""


def _run(name, status="completed", conclusion="success", at="2026-10-12T20:00:00Z"):
    return {"workflowName": name, "name": name, "status": status, "conclusion": conclusion,
            "createdAt": at}


class CommitCiTest(unittest.TestCase):
    project = radar.Project("n", "/r", github="o/nexlink", ci_required=True)

    def test_green_pending_red(self):
        gh = _Gh([_run("ci"), _run("lint", conclusion="skipped")])
        out = radar.commit_ci(self.project, "a" * 40, gh=gh)
        self.assertEqual((out["runs"], out["green"]), (2, True))
        self.assertIn("--commit", gh.calls[0])
        self.assertIn("a" * 40, gh.calls[0])
        out = radar.commit_ci(self.project, "a" * 40, gh=_Gh([_run("ci", "in_progress", None)]))
        self.assertEqual((out["green"], out["pending"]), (False, ["ci"]))
        out = radar.commit_ci(self.project, "a" * 40, gh=_Gh([_run("ci", conclusion="failure")]))
        self.assertEqual((out["green"], out["failed"]), (False, ["ci"]))
        self.assertFalse(radar.commit_ci(self.project, "a" * 40, gh=_Gh([]))["green"])

    def test_the_latest_run_of_each_workflow_counts(self):
        rows = [_run("ci", conclusion="failure", at="2026-10-12T19:00:00Z"),
                _run("ci", at="2026-10-12T20:00:00Z")]          # re-run, green
        self.assertTrue(radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))["green"])
        rows.reverse()
        rows[0]["createdAt"], rows[1]["createdAt"] = rows[1]["createdAt"], rows[0]["createdAt"]
        self.assertFalse(radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))["green"])

    def test_unreadable_raises(self):
        with self.assertRaises(plan_github.GithubError):
            radar.commit_ci(self.project, "a" * 40, gh=_Gh(code=1))
        import tempfile
        bare = tempfile.mkdtemp(prefix="ameesh-l160g-")
        self.addCleanup(os.rmdir, bare)
        with self.assertRaises(plan_github.GithubError):       # no GitHub repository
            radar.commit_ci(radar.Project("n", bare, ci_required=True), "a" * 40, gh=_Gh())


class GatesForTest(unittest.TestCase):
    def test_paths(self):
        project = radar.parse_settings({"projects": {"n": NEXLINK}}).projects["n"]
        always = ("backend/scripts/check-rollbacks.sh",)
        both = always + ("npm run domains-export -- --check",)
        self.assertEqual(radar.gates_for(project, ["frontend/app.ts"]), always)
        self.assertEqual(radar.gates_for(project, ["backend/domains/billing/x.json"]), both)
        self.assertEqual(radar.gates_for(project, ["backend/domains.json"]), both)
        self.assertEqual(radar.gates_for(project, None), both)        # unknown: all of them


class RequiredCiQueueTest(_Land):
    def enter_with(self, runs=None, error=None):
        self.ready(4)
        self.project = dataclasses.replace(self.project, ci_required=True)
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        seen = []

        def commit_ci(sha):
            seen.append(sha)
            if error:
                raise plan_github.GithubError(error)
            return runs
        try:
            return merge_queue.enter(None, 4, repo=self.repo, actor="orch", ci=None,
                                     clone=clone, project=self.project, commit_ci=commit_ci)
        finally:
            self.seen = seen

    def test_green_on_the_frozen_commit_enters(self):
        row = self.enter_with({"runs": 1, "green": True, "pending": [], "failed": []})
        self.assertEqual(row["state"], "queued")
        self.assertEqual(self.seen, [self.tip(4)])                 # the frozen commit itself

    def test_refused_without_green_ci(self):
        for runs, error, why in (
                ({"runs": 0, "green": False, "pending": [], "failed": []}, None, "no run"),
                ({"runs": 1, "green": False, "pending": ["ci"], "failed": []}, None,
                 "still running"),
                ({"runs": 1, "green": False, "pending": [], "failed": ["ci"]}, None, "is red"),
                (None, "HTTP 502", "unreadable")):
            with self.subTest(why=why):
                with self.assertRaisesRegex(merge_queue.QueueError, why):
                    self.enter_with(runs, error)
                self.assertNotIn(4, self.fake.queue_rows)


class PathGatesLandTest(_Land):
    def test_only_the_gates_of_the_touched_paths_run(self):
        hit, miss = os.path.join(self.tmp, "hit"), os.path.join(self.tmp, "miss")
        self.project = dataclasses.replace(self.project, path_gates=(
            radar.Gate("touch %s" % hit, ("*",)),
            radar.Gate("touch %s" % miss, ("nowhere/**",))))
        self.queued(4)
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertTrue(os.path.exists(hit))
        self.assertFalse(os.path.exists(miss))

    def test_a_failed_path_gate_ejects(self):
        self.project = dataclasses.replace(self.project, path_gates=(
            radar.Gate("echo domains stale; exit 3", ("*",)),))
        self.queued(4)
        out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("domains stale", out["output"])


if __name__ == "__main__":
    unittest.main()
