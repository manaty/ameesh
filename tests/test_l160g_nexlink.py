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
import types

from ameesh import land, merge_queue, plan_github, radar, resolutions

from .test_l160b_radar import _git
from .test_l160e2_land import _Land, FakeGh
from .test_l160d_resolutions import _manual_resolution

import unittest
from unittest import mock

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

    def test_clone_alias_cannot_bypass_repository_conditions(self):
        cfg = types.SimpleNamespace(radar={"projects": {"n": NEXLINK}})
        with mock.patch("ameesh.migrations.origin_repo", return_value="acme/product"):
            cfg.radar["projects"]["n"] = dict(NEXLINK, github="acme/product")
            project = merge_queue.radar_project(cfg, "/another/worktree")
        self.assertTrue(project.ci_required)
        self.assertIn("main", project.protected)

    def test_exact_checkout_cannot_hide_another_project_for_the_same_repository(self):
        cfg = types.SimpleNamespace(radar={"projects": {
            "strict": dict(NEXLINK, repo="/strict", github="acme/product"),
            "permissive": {"repo": "/permissive", "github": "acme/product"}}})
        with mock.patch("ameesh.migrations.origin_repo", return_value="acme/product"):
            for checkout in ("/permissive", "/strict", "/another/worktree"):
                with self.subTest(checkout=checkout):
                    with self.assertRaisesRegex(merge_queue.QueueError, "ambiguous"):
                        merge_queue.radar_project(cfg, checkout)

    def test_exact_checkout_without_origin_still_checks_configured_repository_ambiguity(self):
        cfg = types.SimpleNamespace(radar={"projects": {
            "strict": dict(NEXLINK, repo="/strict", github="acme/product"),
            "permissive": {"repo": "/permissive", "github": "acme/product"}}})
        with mock.patch("ameesh.migrations.origin_repo", return_value=None):
            with self.assertRaisesRegex(merge_queue.QueueError, "ambiguous"):
                merge_queue.radar_project(cfg, "/permissive")

    def test_invalid_or_ambiguous_settings_refuse_queue_conditions(self):
        cfg = types.SimpleNamespace(radar={"projects": {"n": dict(NEXLINK, ci_required="yes")}})
        with self.assertRaises(merge_queue.QueueError):
            merge_queue.radar_project(cfg, "/another/worktree")
        cfg.radar = {"projects": {n: dict(NEXLINK, repo="/" + n, github="acme/product")
                                  for n in ("first", "second")}}
        with mock.patch("ameesh.migrations.origin_repo", return_value="acme/product"):
            with self.assertRaisesRegex(merge_queue.QueueError, "ambiguous"):
                merge_queue.radar_project(cfg, "/another/worktree")


class ProtectedTest(unittest.TestCase):
    def test_globs(self):
        project = radar.parse_settings({"projects": {"n": NEXLINK}}).projects["n"]
        for target in ("main", "origin/main", "refs/heads/main", "desktop-v1.4", "nexlink-releases",
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

    def test_configured_default_is_checked_before_repository_default(self):
        self.ready(4)
        self.fake.lots[4]["branch_target"] = None
        self.project = dataclasses.replace(self.project, target="develop", protected=("main",))
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        row = merge_queue.enter(None, 4, repo=self.repo, ci=None, project=self.project, clone=clone)
        self.assertEqual(row["target"], "develop")

    def test_explicit_protected_target_overrides_configured_default(self):
        self.ready(4)
        self.fake.lots[4]["branch_target"] = "refs/heads/main"
        self.project = dataclasses.replace(self.project, target="develop", protected=("main",))
        with self.assertRaisesRegex(merge_queue.QueueError, "protected target"):
            merge_queue.enter(None, 4, repo=self.repo, ci=None, project=self.project)


class _Gh:
    """`gh run list` answering `rows` (or failing)."""

    def __init__(self, rows=None, code=0, checks=None, statuses=None):
        self.rows, self.code, self.calls = rows or [], code, []
        self.checks = checks or []
        self.statuses = statuses or []

    def run(self, argv, stdin=None):
        self.calls.append(argv)
        data = self.rows
        if argv[0] == "api":
            data = ({"total_count": len(self.checks), "check_runs": self.checks}
                    if "check-runs" in argv[1] else
                    {"sha": "a" * 40, "total_count": len(self.statuses), "statuses": self.statuses})
        return self.code, json.dumps(data), "boom" if self.code else ""


def _run(name, status="completed", conclusion="success", at="2026-10-12T20:00:00Z"):
    return {"workflowName": name, "name": name, "status": status, "conclusion": conclusion,
            "createdAt": at, "startedAt": at, "updatedAt": at, "headSha": "a" * 40,
            "workflowDatabaseId": sum(map(ord, name))}


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
        for field in ("createdAt", "startedAt", "updatedAt"):
            rows[0][field], rows[1][field] = rows[1][field], rows[0][field]
        self.assertFalse(radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))["green"])

    def test_distinct_workflows_with_the_same_name_do_not_hide_failure(self):
        rows = [dict(_run("CI", conclusion="failure"), workflowDatabaseId=1),
                dict(_run("CI", at="2026-10-12T21:00:00Z"), workflowDatabaseId=2)]
        out = radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))
        self.assertEqual(out["runs"], 2)
        self.assertFalse(out["green"])
        self.assertTrue(out["failed"])

    def test_missing_or_invalid_workflow_identity_or_time_is_refused(self):
        for key, value in (("workflowDatabaseId", None), ("workflowDatabaseId", ""),
                           ("workflowDatabaseId", True), ("createdAt", None),
                           ("createdAt", ""), ("createdAt", "yesterday"),
                           ("createdAt", "2026-10-12T21:00:00"),
                           ("startedAt", None), ("updatedAt", "yesterday")):
            with self.subTest(key=key, value=value):
                with self.assertRaises(plan_github.GithubError):
                    radar.commit_ci(self.project, "a" * 40,
                                    gh=_Gh([dict(_run("ci"), **{key: value})]))

    def test_workflow_order_uses_instants_and_refuses_ambiguous_latest_time(self):
        rows = [_run("ci", conclusion="failure", at="2026-10-12T20:00:00+02:00"),
                _run("ci", at="2026-10-12T19:00:00Z")]
        self.assertTrue(radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))["green"])
        for field in ("createdAt", "startedAt", "updatedAt"):
            rows[0][field] = "2026-10-12T21:00:00+02:00"
        with self.assertRaises(plan_github.GithubError):
            radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))

    def test_unreadable_raises(self):
        with self.assertRaises(plan_github.GithubError):
            radar.commit_ci(self.project, "a" * 40, gh=_Gh(code=1))
        import tempfile
        bare = tempfile.mkdtemp(prefix="ameesh-l160g-")
        self.addCleanup(os.rmdir, bare)
        with self.assertRaises(plan_github.GithubError):       # no GitHub repository
            radar.commit_ci(radar.Project("n", bare, ci_required=True), "a" * 40, gh=_Gh())

    def test_truncated_malformed_or_wrong_sha_cannot_certify_ci(self):
        for rows in ([_run("ci")] * 100, ["bad run"],
                     [dict(_run("ci"), headSha="b" * 40)]):
            with self.subTest(rows=rows[:1]):
                with self.assertRaises(plan_github.GithubError):
                    radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))


    def test_older_run_rerun_failure_or_pending_overrides_newer_created_success(self):
        for status, conclusion in (("completed", "failure"), ("in_progress", None)):
            with self.subTest(status=status):
                rows = [dict(_run("ci", status, conclusion, at="2026-10-12T10:00:00Z"),
                             updatedAt="2026-10-12T12:00:00Z", startedAt="2026-10-12T12:00:00Z"),
                        dict(_run("ci", at="2026-10-12T11:00:00Z"),
                             updatedAt="2026-10-12T11:01:00Z", startedAt="2026-10-12T11:00:00Z")]
                self.assertFalse(radar.commit_ci(self.project, "a" * 40, gh=_Gh(rows))["green"])

    def test_external_check_failure_or_pending_blocks_actions_success(self):
        for status, conclusion in (("completed", "failure"), ("in_progress", None)):
            with self.subTest(status=status):
                checks = [{"id": 42, "app": {"id": 99, "slug": "external-ci"},
                           "name": "quality", "head_sha": "a" * 40,
                           "status": status, "conclusion": conclusion,
                           "started_at": "2026-10-12T10:00:00Z",
                           "completed_at": "2026-10-12T11:00:00Z" if status == "completed" else None}]
                self.assertFalse(radar.commit_ci(self.project, "a" * 40,
                                                gh=_Gh([_run("ci")], checks=checks))["green"])

    def test_external_commit_status_blocks_actions_success(self):
        for state in ("failure", "error", "pending"):
            with self.subTest(state=state):
                self.assertFalse(radar.commit_ci(self.project, "a" * 40,
                    gh=_Gh([_run("ci")], statuses=[{"context": "legacy-ci", "state": state}]))["green"])


    def test_external_green_can_certify_and_apps_with_same_name_stay_distinct(self):
        check = {"id": 1, "app": {"id": 9, "slug": "ci-app"}, "name": "ci",
                 "head_sha": "a" * 40, "status": "completed", "conclusion": "success",
                 "started_at": "2026-10-12T10:00:00Z", "completed_at": "2026-10-12T11:00:00Z"}
        self.assertTrue(radar.commit_ci(self.project, "a" * 40, gh=_Gh(checks=[check]))["green"])
        other = dict(check, id=2, app={"id": 10, "slug": "other"}, conclusion="failure")
        self.assertFalse(radar.commit_ci(self.project, "a" * 40,
                                        gh=_Gh(checks=[check, other]))["green"])
        self.assertTrue(radar.commit_ci(self.project, "a" * 40,
            gh=_Gh(statuses=[{"context": "ci", "state": "success"}]))["green"])

    def test_external_read_errors_and_truncation_refuse_certification(self):
        for bad in ({}, {"total_count": 1, "check_runs": []},
                    {"total_count": 0, "check_runs": [], "sha": "b" * 40}):
            gh = _Gh([_run("ci")])
            real = gh.run
            def run(argv, stdin=None):
                if argv[0] == "api":
                    return 0, json.dumps(bad), ""
                return real(argv, stdin)
            gh.run = run
            with self.subTest(bad=bad), self.assertRaises(plan_github.GithubError):
                radar.commit_ci(self.project, "a" * 40, gh=gh)


    def test_older_external_check_updated_failure_or_pending_overrides_newer_id(self):
        for status, conclusion in (("completed", "failure"), ("in_progress", None)):
            with self.subTest(status=status):
                old = {"id": 41, "app": {"id": 99, "slug": "external-ci"},
                       "name": "quality", "head_sha": "a" * 40,
                       "status": status, "conclusion": conclusion,
                       "started_at": "2026-10-12T10:00:00Z",
                       "completed_at": "2026-10-12T12:00:00Z" if status == "completed" else None}
                newer = dict(old, id=42, status="completed", conclusion="success",
                             started_at="2026-10-12T11:00:00Z", completed_at="2026-10-12T11:30:00Z")
                out = radar.commit_ci(self.project, "a" * 40, gh=_Gh([_run("ci")], checks=[old, newer]))
                self.assertFalse(out["green"], out)
                with mock.patch.object(radar, "commit_ci", return_value=out):
                    with self.assertRaises(merge_queue.QueueError):
                        merge_queue._required_ci(self.project, "a" * 40)

    def test_missing_or_invalid_external_check_dates_refuse_certification(self):
        check = {"id": 42, "app": {"id": 99, "slug": "external-ci"}, "name": "quality",
                 "head_sha": "a" * 40, "status": "completed", "conclusion": "success",
                 "started_at": "2026-10-12T10:00:00Z", "completed_at": "2026-10-12T11:00:00Z"}
        for field in ("started_at", "completed_at"):
            for value in (None, "", "yesterday", "2026-10-12T11:00:00"):
                with self.subTest(field=field, value=value), self.assertRaises(plan_github.GithubError):
                    radar.commit_ci(self.project, "a" * 40,
                                    gh=_Gh([_run("ci")], checks=[dict(check, **{field: value})]))


    def test_external_activity_supersedes_old_failure_but_unfinished_check_blocks(self):
        old = {"id": 41, "app": {"id": 99, "slug": "external-ci"}, "name": "quality",
               "head_sha": "a" * 40, "status": "completed", "conclusion": "failure",
               "started_at": "2026-10-12T10:00:00Z", "completed_at": "2026-10-12T10:30:00Z"}
        newer = dict(old, id=42, conclusion="success", started_at="2026-10-12T11:00:00Z",
                     completed_at="2026-10-12T11:30:00Z")
        self.assertTrue(radar.commit_ci(self.project, "a" * 40, gh=_Gh(checks=[old, newer]))["green"])
        old.update(status="in_progress", conclusion=None, completed_at=None)
        self.assertFalse(radar.commit_ci(self.project, "a" * 40, gh=_Gh(checks=[old, newer]))["green"])

    def test_external_ambiguous_activity_is_refused(self):
        check = {"id": 41, "app": {"id": 99, "slug": "external-ci"}, "name": "quality",
                 "head_sha": "a" * 40, "status": "completed", "conclusion": "failure",
                 "started_at": "2026-10-12T10:00:00Z", "completed_at": "2026-10-12T11:00:00Z"}
        with self.assertRaisesRegex(plan_github.GithubError, "ambiguous"):
            radar.commit_ci(self.project, "a" * 40,
                            gh=_Gh(checks=[check, dict(check, id=42, conclusion="success")]))


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


class RequiredCiLandTest(_Land):
    GREEN = {"runs": 1, "green": True, "pending": [], "failed": []}
    RED = {"runs": 1, "green": False, "pending": [], "failed": ["ci"]}

    def test_failed_rerun_after_green_admission_does_not_land(self):
        self.ready(4)
        self.project = dataclasses.replace(self.project, ci_required=True)
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        merge_queue.enter(None, 4, repo=self.repo, ci=None, project=self.project,
                          clone=clone, commit_ci=lambda sha: self.GREEN)
        self.fake.queue_rows[4]["rank"] = 1
        before = _git(self.origin, "rev-parse", "main")
        with mock.patch.object(radar, "commit_ci", return_value=self.RED) as ci:
            out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        ci.assert_called_once_with(self.project, self.tip(4))
        self.assertIn("is red", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_ci_enabled_after_queue_admission_requires_frozen_ci(self):
        self.queued(4)
        self.project = dataclasses.replace(self.project, ci_required=True)
        before = _git(self.origin, "rev-parse", "main")
        with mock.patch.object(radar, "commit_ci", return_value={
                "runs": 0, "green": False, "pending": [], "failed": []}) as ci:
            out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        ci.assert_called_once_with(self.project, self.tip(4))
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_ci_is_checked_again_after_merged_tree_gates(self):
        self.queued(4)
        self.project = dataclasses.replace(self.project, ci_required=True)
        before = _git(self.origin, "rev-parse", "main")
        with mock.patch.object(radar, "commit_ci", side_effect=[self.GREEN, self.RED]) as ci:
            out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertEqual(ci.call_args_list, [mock.call(self.project, self.tip(4))] * 2)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_already_pushed_commit_remains_a_fact_after_ci_turns_red(self):
        self.queued(4)
        _git(self.origin, "update-ref", "refs/heads/main", self.tip(4))
        self.project = dataclasses.replace(self.project, ci_required=True)
        with mock.patch.object(radar, "commit_ci", return_value=self.RED):
            out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_green_frozen_ci_is_rechecked_before_successful_push(self):
        self.queued(4)
        self.project = dataclasses.replace(self.project, ci_required=True)
        with mock.patch.object(radar, "commit_ci", return_value=self.GREEN) as ci:
            out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(ci.call_args_list, [mock.call(self.project, self.tip(4))] * 2)

    def test_unreadable_frozen_ci_does_not_land(self):
        self.queued(4)
        self.project = dataclasses.replace(self.project, ci_required=True)
        before = _git(self.origin, "rev-parse", "main")
        with mock.patch.object(radar, "commit_ci", side_effect=plan_github.GithubError("HTTP502")):
            out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("unreadable", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)


    def test_new_ambiguous_repository_configuration_prevents_landing(self):
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        self.cfg.radar = {"projects": {
            "p": {"repo": self.repo, "github": "o/r", "land": "local", "gates": ["true"]},
            "strict": {"repo": self.repo + "-other", "github": "o/r", "land": "local",
                       "gates": ["true"], "ci_required": True, "protected": ["main"]}}}
        out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("ambiguous", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)


    def test_manual_landing_under_permissive_fork_refuses_strict_queue_source(self):
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        self.fake.queue_rows[4]["source"] = "github.com/acme/strict"
        self.project = dataclasses.replace(self.project, ci_required=False, github="acme/fork")
        with mock.patch.object(land, "_land", wraps=land._land) as merging:
            out = self.land(4)
        self.assertEqual(out["result"], "error", out)
        self.assertIn("source", out["error"])
        merging.assert_not_called()
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")


    def test_landing_accepts_matching_normalized_source_but_refuses_missing_source(self):
        self.queued(4)
        self.fake.queue_rows[4]["source"] = "github.com/ACME/Product"
        with mock.patch.object(merge_queue, "source_of", return_value="github.com/acme/product"), \
                mock.patch.object(land, "_land", return_value={"result": "landing"}) as merging:
            self.assertEqual(self.land(4)["result"], "landing")
            merging.assert_called_once()
        self.fake.queue_rows[4]["source"] = ""
        with mock.patch.object(land, "_land") as merging:
            self.assertEqual(self.land(4)["result"], "error")
            merging.assert_not_called()


class PrFrozenCiR3Test(_Land):
    LAND = "pr"
    GREEN = {"runs": 1, "green": True, "pending": [], "failed": []}
    RED = {"runs": 1, "green": False, "pending": [], "failed": ["ci"]}

    def test_frozen_ci_is_rechecked_before_pr_merge(self):
        self.check_follow(True)

    def test_ci_enabled_while_waiting_for_pr_merge_checks_frozen_sha(self):
        self.check_follow(False)

    def check_follow(self, enabled_before_push):
        self.queued(4)
        gh = FakeGh()
        gh.head = self.tip(4)
        self.project = dataclasses.replace(self.project, ci_required=enabled_before_push)
        with mock.patch.object(radar, "commit_ci", return_value=self.GREEN):
            out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landing", out)
        gh.head, gh.ci = out["sha"], "green"
        self.project = dataclasses.replace(self.project, ci_required=True)
        with mock.patch.object(radar, "commit_ci", return_value=self.RED) as ci:
            out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "ejected", out)
        ci.assert_called_once_with(self.project, self.tip(4))
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))


class SourceCaseR3Test(unittest.TestCase):
    def test_github_source_case_has_one_queue_identity(self):
        with mock.patch.object(merge_queue.plan_git, "_out", side_effect=[
                "git@github.com:ACME/Product.git", "https://github.com/acme/product"]):
            self.assertEqual(merge_queue.source_of("/alias"), merge_queue.source_of("/configured"))


class OldSourceCaseR3Test(_Land):
    def test_old_mixed_case_source_is_ranked_by_normalized_project(self):
        self.queued(4)
        self.fake.queue_rows[4]["source"] = "github.com/ACME/Product"
        report = {"branches": [{"work_item": 4}], "against_target": []}
        with mock.patch.object(merge_queue, "source_of", return_value="github.com/acme/product"):
            out = merge_queue.run_pass(self.cfg, None, self.project, report)
        self.assertIn("main", out["targets"], out)
        self.assertEqual(out["targets"]["main"][0]["lot"], 4)


class GeneratedQueueTest(_Land):
    def prepare(self, check="grep -qx gen other.py"):
        self.queued(4)
        _git(self.repo, "checkout", "-q", "main")
        with open(os.path.join(self.repo, "other.py"), "w") as fh:
            fh.write("target version\n")
        _git(self.repo, "commit", "-qam", "target changed")
        _git(self.repo, "push", "-q", "origin", "main")
        self.project = dataclasses.replace(self.project, generated=(radar.Generated(
            "other.py", "printf 'gen\\n' > other.py", check),))

    def run_queue(self, cache=None):
        report = {"against_target": [{"work_item": 4, "state": "conflict", "target": "main",
                  "tip": self.tip(4), "target_head": _git(self.origin, "rev-parse", "main"),
                  "files": ["other.py"]}]}
        return merge_queue.run_pass(self.cfg, None, self.project, report, cache=cache)

    def test_generated_only_conflict_is_regenerated_in_the_stack(self):
        self.prepare()
        before = _git(self.origin, "rev-parse", "main")
        out = self.run_queue()
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued", out)
        self.assertEqual(out["targets"]["main"][0]["stack"]["state"], "clean")
        path = radar.clone_dir(self.cfg, "p")
        self.assertEqual(_git(path, "show", "HEAD:other.py"), "gen")
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_generated_conflicts_are_excluded_before_stack_rerere(self):
        self.prepare()
        original = resolutions._run
        seen = []

        def watch(path, *args, **kwargs):
            proc = original(path, *args, **kwargs)
            if args[-1] == "rerere" or ("merge" in args and "rerere.enabled=true" in args):
                seen.append(resolutions._text(original(
                    path, "diff", "--name-only", "--diff-filter=U")))
            return proc

        # No early target-conflict report: the stack must detect it itself.
        with mock.patch.object(resolutions, "_run", side_effect=watch):
            out = merge_queue.run_pass(self.cfg, None, self.project, {})
        self.assertTrue(seen, out)
        self.assertTrue(all("other.py" not in paths for paths in seen), seen)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued", out)

    def test_prepared_generated_resolution_never_supplies_stack_output(self):
        sha = _manual_resolution(self.repo, "lot/2-b", "lot/1-a")
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        clone.ensure()
        original = resolutions._run
        seen = []

        def watch(path, *args, **kwargs):
            if args[-1] == "rerere":
                seen.append(resolutions._text(original(
                    path, "diff", "--name-only", "--diff-filter=U")))
            return original(path, *args, **kwargs)

        with mock.patch.object(resolutions, "_run", side_effect=watch):
            out = merge_queue.stack(clone, _git(self.repo, "rev-parse", "main"),
                [{"work_item": n, "tip": self.tip(n)} for n in (1, 2)],
                [{"work_item_id": 2, "other_item_id": 1, "commit_sha": sha}],
                generated=(radar.Generated("shared.py", "printf 'gen\\n' > shared.py",
                                           "grep -qx gen shared.py"),))
        self.assertEqual(out[2], {"state": "clean", "files": []})
        self.assertEqual(_git(clone.path, "show", "HEAD:shared.py"), "gen")
        # Training may itself run rerere. Only the stack's final call matters.
        self.assertTrue(seen)
        self.assertNotIn("shared.py", seen[-1])

    def test_generator_failure_ejects_during_stacking(self):
        self.prepare(check="echo bad generated output; exit 3")
        out = self.run_queue()
        self.assertEqual(self.fake.queue_rows[4]["state"], "ejected", out)
        self.assertIn("generated", self.fake.queue_rows[4]["reason"])
        self.assertIn("bad generated output", self.sent[-1][1])

    def test_changed_generated_configuration_invalidates_stack_cache(self):
        self.prepare()
        cache = radar.Cache()
        self.run_queue(cache)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
        self.project = dataclasses.replace(self.project, generated=(radar.Generated(
            "other.py", "printf 'new\\n' > other.py", "grep -qx new other.py"),))
        self.run_queue(cache)
        self.assertEqual(_git(radar.clone_dir(self.cfg, "p"), "show", "HEAD:other.py"), "new")


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


class GeneratedLandTest(_Land):
    """(a): a generated file is regenerated on the merged tree, never merged
    nor resolved by rerere; its check gates the commit."""

    def generate(self, check="grep -qx gen other.py"):
        self.project = dataclasses.replace(self.project, generated=(radar.Generated(
            "other.py", "printf 'gen\\n' > other.py", check),))

    def test_changed_by_the_lot_regenerated(self):
        self.generate()
        self.queued(4)
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(_git(self.origin, "show", "main:other.py"), "gen")

    def test_input_only_change_regenerates_an_untouched_output(self):
        self.project = dataclasses.replace(self.project, generated=(radar.Generated(
            "output.txt", "cp other.py output.txt", "cmp other.py output.txt"),))
        self.queued(4)
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(_git(self.origin, "show", "main:output.txt"),
                         _git(self.origin, "show", "main:other.py"))

    def test_input_only_change_with_failed_generator_does_not_move_target(self):
        self.project = dataclasses.replace(self.project, generated=(radar.Generated(
            "output.txt", "echo generator failed; exit 7", "test -f output.txt"),))
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("generator failed", out["output"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_check_cannot_change_the_committed_output(self):
        self.generate(check="printf 'different\\n' > other.py")
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("changed the staged output", out["output"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_generated_conflict_is_absent_when_rerere_runs(self):
        self.queued(4)
        _git(self.repo, "checkout", "-q", "main")
        with open(os.path.join(self.repo, "other.py"), "w") as fh:
            fh.write("target version\n")
        _git(self.repo, "commit", "-qam", "target changed")
        _git(self.repo, "push", "-q", "origin", "main")
        self.generate()
        original = resolutions._run
        seen = []

        def watch(path, *args, **kwargs):
            if args[-1] == "rerere":
                conflicts = original(path, "diff", "--name-only", "--diff-filter=U")
                seen.append(resolutions._text(conflicts))
            return original(path, *args, **kwargs)

        with mock.patch.object(resolutions, "_run", side_effect=watch):
            out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertTrue(seen)
        self.assertTrue(all("other.py" not in paths for paths in seen), seen)

    def test_conflict_on_it_is_regenerated_not_ejected(self):
        self.queued(4)
        _git(self.repo, "checkout", "-q", "main")
        with open(os.path.join(self.repo, "other.py"), "w") as fh:
            fh.write("z\n")
        _git(self.repo, "commit", "-qam", "main moves other.py")
        _git(self.repo, "push", "-q", "origin", "main")
        self.generate()
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(_git(self.origin, "show", "main:other.py"), "gen")

    def test_failed_check_ejects_target_unmoved(self):
        self.generate(check="echo domains.json stale; exit 1")
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        self.assertIn("generated file other.py", self.fake.queue_rows[4]["reason"])
        self.assertIn("domains.json stale", out["output"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)

    def test_replay_never_counts_it_remaining(self):
        replayer = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        replayer.ensure()
        a, b = (_git(self.repo, "rev-parse", r) for r in ("lot/1-a", "lot/2-b"))
        plain = replayer.replay(a, b, "0" * 40)
        self.assertEqual(plain["remaining"], ["shared.py"])
        out = replayer.replay(a, b, "0" * 40, generated=frozenset({"shared.py"}))
        self.assertEqual((out["state"], out["remaining"]), ("prepared", []))


if __name__ == "__main__":
    unittest.main()
