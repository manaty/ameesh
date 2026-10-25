# SPDX-License-Identifier: AGPL-3.0-only
"""Nexlink cutoffs: real Git candidates, fake GitHub and database reads."""
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import nexlink_release as nx, release_cutoff as rc


class VersionTests(unittest.TestCase):
    def test_sequence_and_invalid_names(self):
        self.assertEqual(nx.next_version("11v"), "11w")
        self.assertEqual(nx.next_version("11z"), "12a")
        for name in ("0a", "11aa", "v11w", "11W", "11w;prod"):
            with self.assertRaises(rc.ReleaseError):
                nx.version_key(name)

    def test_configuration_keeps_semver_and_refuses_staging_override(self):
        cfg = types.SimpleNamespace(humans="", radar={})
        with mock.patch.object(rc.radar, "settings_of", return_value=types.SimpleNamespace(projects={})):
            self.assertEqual(rc.ProjectRelease.from_config(cfg, "ameesh", {"repo": "."}).model,
                             "semver")
            project = rc.ProjectRelease.from_config(cfg, "nexlink", {
                "repo": ".", "target": "develop", "model": "nexlink"})
            self.assertEqual(project.model, "nexlink")
            for entry in ({"target": "main"}, {"target": "develop", "staging": "prod.sh"}):
                with self.assertRaises(rc.ReleaseError):
                    rc.ProjectRelease.from_config(cfg, "nexlink", dict(
                        {"repo": ".", "model": "nexlink"}, **entry))


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@localhost")
        (self.repo / "base").write_text("base")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.project = rc.ProjectRelease("nexlink", str(self.repo), target="develop", model="nexlink")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True,
                                       stderr=subprocess.STDOUT)

    def commit(self, name, text):
        (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / name).write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", name)
        return self.git("rev-parse", "HEAD").strip()

    def test_merge_freezes_both_parents_without_moving_checkout(self):
        develop = self.commit("develop", "develop")
        self.git("checkout", "-q", self.base)
        main = self.commit("main", "main")
        before = self.git("rev-parse", "HEAD")
        sha = nx.merge_candidate(self.project, develop, main)
        self.assertEqual(self.git("show", sha + ":develop"), "develop")
        self.assertEqual(self.git("show", sha + ":main"), "main")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("rev-list", "--parents", "-1", sha).split()[1:], [develop, main])

    def test_conflict_refused_and_worktree_removed(self):
        develop = self.commit("base", "develop")
        self.git("checkout", "-q", self.base)
        main = self.commit("base", "main")
        with self.assertRaisesRegex(rc.ReleaseError, "conflicts"):
            nx.merge_candidate(self.project, develop, main)
        self.assertEqual(len(self.git("worktree", "list", "--porcelain").split("worktree ")) - 1, 1)

    def test_migration_checksum_and_ledger_gate(self):
        sha = self.commit("supabase/migrations/540_expiry.sql", "select 1;\n")
        required = nx.migration_requirements(self.project, sha, self.base)["hub"][0]
        self.assertEqual(required["name"], "540_expiry")
        pending = {"sha": sha, "main": self.base}
        row = dict(required, applied_by="operator", applied_at="2026-10-25T10:00:00Z")
        with mock.patch.object(nx, "migration_rows", return_value=[row]):
            self.assertIn("hub", nx.migration_gate(None, self.project, pending))
        for bad in ([], [dict(row, checksum="wrong")], [dict(row, applied_at=None)]):
            with mock.patch.object(nx, "migration_rows", return_value=bad):
                with self.assertRaisesRegex(rc.ReleaseError, "attestation"):
                    nx.migration_gate(None, self.project, pending)

    def test_content_marker_read_like_migrate_sh_and_renames_seen(self):
        old = self.commit("supabase/migrations/541_x.sql", "select 0;\n")
        sha = self.commit("supabase/migrations/541_x.sql", "--Migrate-Target:  content\nselect 1;\n")
        self.assertEqual(set(nx.migration_requirements(self.project, sha, old)), {"content"})
        self.git("mv", "supabase/migrations/541_x.sql", "supabase/migrations/542_x.sql")
        self.git("commit", "-qm", "rename")
        moved = self.git("rev-parse", "HEAD").strip()
        rows = nx.migration_requirements(self.project, moved, sha)["content"]
        self.assertEqual([row["version"] for row in rows], [542])

    def test_gate_checks_both_staging_targets_and_ignores_old_holes(self):
        old = self.commit("supabase/migrations/001_old.sql", "select 0;\n")
        sha = self.commit("supabase/migrations/540_new.sql", "select 1;\n")
        required = nx.migration_requirements(self.project, sha, old)
        self.assertEqual(set(required), {"hub", "content"})
        self.assertEqual([row["version"] for row in required["hub"]], [540])
        def rows(project, target, wanted):
            return [dict(wanted[0], applied_by="operator", applied_at="2026-10-25")] if target == "hub" else []
        with mock.patch.object(nx, "migration_rows", side_effect=rows):
            with self.assertRaisesRegex(rc.ReleaseError, "540_new on content"):
                nx.migration_gate(None, self.project, {"sha": sha, "main": old})

    def test_ci_planner_bridge_uses_frozen_script_without_dispatch(self):
        program = """export async function main(args, deps) {
          if (!args.includes('--dry-run') || !args.includes('--sql-anchor')) throw Error('args');
          console.log(JSON.stringify({schema:'nexlink-release-top-dispatch/1',
            head:args[args.indexOf('--head')+1],ref:args[args.indexOf('--ref')+1],
            due:['lot/sql-invariant'],anchor:[{context:'lot/sql-invariant',
            input:'landing_target',sha:args[args.indexOf('--sql-anchor')+1]}],blocked:[],maxInflight:2}));
        }
        """
        sha = self.commit("scripts/ci/release-top-dispatch.mjs", program)
        result = nx.ci_plan(self.project, {"sha": sha, "main": self.base,
                                         "integration": "integration/release-11w"})
        self.assertEqual(result, {"max_inflight": 2, "parts": [
            {"workflow": "sql-invariant.yml", "anchor_input": "landing_target"}]})

    def test_staging_environment_and_service_checked_in_frozen_workflow(self):
        path = ".github/workflows/deploy-api.yml"
        good = """env:
  SERVICE: nexlink-api-staging
jobs:
  deploy:
    environment: staging
    steps:
      - with:
          service_account: '${{ vars.GCP_STAGING_DEPLOYER_SA }}'
"""
        sha = self.commit(path, good)
        nx.validate_staging(self.project, sha, "deploy-api.yml")
        for before, after in (("environment: staging", "environment: production"),
                              ("SERVICE: nexlink-api-staging", "SERVICE: nexlink-api"),
                              ("GCP_STAGING_DEPLOYER_SA", "GCP_PROD_DEPLOYER_SA")):
            sha = self.commit(path, good.replace(before, after))
            with self.assertRaises(rc.ReleaseError):
                nx.validate_staging(self.project, sha, "deploy-api.yml")
        with self.assertRaises(rc.ReleaseError):
            nx.validate_staging(self.project, sha, "deploy-api-prod.yml")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink")
        self.pending = dict(sha="a" * 40, main="b" * 40, tag="nexlink-11w", version="11w",
                            integration="integration/release-11w",
                            ci_parts=[{"workflow": "sql-invariant.yml", "anchor_input": "landing_target"}])
        self.saved = []

    def save(self):
        self.saved.append(json.loads(json.dumps(self.pending)))

    def test_dispatch_records_intent_before_side_effect_and_reconciles(self):
        def run(project, args):
            self.assertIn("ci/sql-invariant.yml", self.saved[-1]["dispatches"])
            self.assertIn("expected_sha=" + "a" * 40, args)
            self.assertIn("landing_target=" + "b" * 40, args)
            return ""
        with mock.patch.object(nx, "gh", side_effect=run):
            self.assertFalse(nx.workflow_step(self.project, self.pending, "sql-invariant.yml",
                                              staging=False, save=self.save, now=100))
        row = dict(databaseId=1, headSha="a" * 40, headBranch=self.pending["integration"],
                   event="workflow_dispatch", createdAt="1970-01-01T00:01:40Z",
                   status="completed", conclusion="success", url="https://example/run/1")
        with mock.patch.object(nx, "runs", return_value=[row]), mock.patch.object(nx, "gh") as gh:
            self.assertTrue(nx.workflow_step(self.project, self.pending, "sql-invariant.yml",
                                             staging=False, save=self.save, now=200))
            gh.assert_not_called()

    def test_ambiguous_dispatch_is_never_resent(self):
        with mock.patch.object(nx, "gh", side_effect=rc.TransientReleaseError("timeout")):
            with self.assertRaises(rc.TransientReleaseError):
                nx.workflow_step(self.project, self.pending, "sql-invariant.yml",
                                 staging=False, save=self.save, now=100)
        with mock.patch.object(nx, "runs", return_value=[]), mock.patch.object(nx, "gh") as gh:
            self.assertFalse(nx.workflow_step(self.project, self.pending, "sql-invariant.yml",
                                              staging=False, save=self.save, now=200))
            gh.assert_not_called()
            self.assertIn("run not visible", self.pending["waiting"])

    def test_staging_dispatch_runs_on_release_branch_not_tag(self):
        # The staging environment's branch policy admits release/*, no nexlink-* tag.
        with mock.patch.object(nx, "validate_staging"), \
                mock.patch.object(nx, "gh", return_value="") as gh:
            self.assertFalse(nx.workflow_step(self.project, self.pending, "deploy-api.yml",
                                              staging=True, save=self.save, now=100))
        args = gh.call_args.args[1]
        self.assertEqual(args[args.index("--ref") + 1], "release/11w")
        self.assertNotIn("-f", args)
        row = dict(databaseId=2, headSha="a" * 40, headBranch="release/11w",
                   event="workflow_dispatch", createdAt="1970-01-01T00:01:40Z",
                   status="completed", conclusion="success", url="https://example/run/2")
        with mock.patch.object(nx, "validate_staging"), \
                mock.patch.object(nx, "runs", return_value=[row]) as runs:
            self.assertTrue(nx.workflow_step(self.project, self.pending, "deploy-api.yml",
                                             staging=True, save=self.save, now=200))
        self.assertEqual(runs.call_args.args[2], "release/11w")

    def test_production_dispatch_rejected_before_external_operation(self):
        with mock.patch.object(nx, "gh") as gh:
            with self.assertRaises(rc.ReleaseError):
                nx.workflow_step(self.project, self.pending, "deploy-api-prod.yml",
                                 staging=True, save=self.save, now=100)
            gh.assert_not_called()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.temp.name)
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink")
        self.pending = dict(sha="a" * 40, main="b" * 40, base="c" * 40,
                            tag="nexlink-11w", version="11w", cutoff_at=100, label="11:00",
                            integration="integration/release-11w", empty=False,
                            ci_max_inflight=3, ci_parts=[{"workflow": "backend-suite.yml"}])
        state = {"schema": rc.SCHEMA, "projects": {"nexlink": {"pending_nexlink": self.pending}}}
        rc.save_state(self.cfg, state)
        for name, value in (("compiled_rules", (object(), types.SimpleNamespace(commit="d" * 40))),
                            ("cutoff_slot", (1000, "23:00")), ("fetch", None)):
            patcher = mock.patch.object(rc, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(nx, "ensure_ref")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_resume_frozen_cutoff_outside_window_without_new_snapshot(self):
        with mock.patch.object(nx, "snapshot") as snapshot, \
                mock.patch.object(nx, "workflow_step", return_value=False):
            report = rc.execute(self.cfg, None, self.project, now=100000)
            self.assertEqual(report["result"], "waiting-ci")
            self.assertEqual(report["cutoff_at"], 100)
            snapshot.assert_not_called()

    def test_missing_migration_prevents_all_staging_dispatch(self):
        with mock.patch.object(nx, "workflow_step", return_value=True) as dispatch, \
                mock.patch.object(nx, "publish", return_value="https://example/pr/1"), \
                mock.patch.object(nx, "migration_gate", side_effect=rc.ReleaseError("missing migration")):
            with self.assertRaisesRegex(rc.ReleaseError, "missing migration"):
                rc.execute(self.cfg, None, self.project, now=100000)
            self.assertTrue(all(not call.kwargs["staging"] for call in dispatch.call_args_list))
            self.assertIn("pending_nexlink", rc.load_state(self.cfg)["projects"]["nexlink"])

    def test_empty_cut_creates_branch_without_staging_or_migration_read(self):
        state = rc.load_state(self.cfg)
        state["projects"]["nexlink"]["pending_nexlink"]["empty"] = True
        rc.save_state(self.cfg, state)
        with mock.patch.object(nx, "workflow_step", return_value=True) as dispatch, \
                mock.patch.object(nx, "publish", return_value="https://example/pr/1") as publish, \
                mock.patch.object(nx, "migration_gate") as migration:
            report = rc.execute(self.cfg, None, self.project, now=100000)
            self.assertEqual(report["result"], "empty")
            publish.assert_called_once()
            migration.assert_not_called()
            self.assertTrue(all(not call.kwargs["staging"] for call in dispatch.call_args_list))
            self.assertEqual(rc.load_state(self.cfg)["projects"]["nexlink"]["last_cutoff"], 100)

    def test_wave_starts_at_most_three_parts(self):
        state = rc.load_state(self.cfg)
        state["projects"]["nexlink"]["pending_nexlink"]["ci_parts"] = [
            {"workflow": "suite-%d.yml" % n} for n in range(8)]
        rc.save_state(self.cfg, state)
        with mock.patch.object(nx, "workflow_step", return_value=False) as dispatch:
            report = rc.execute(self.cfg, None, self.project, now=100000)
            self.assertEqual(report["result"], "waiting-ci")
            self.assertEqual(dispatch.call_count, 3)


if __name__ == "__main__":
    unittest.main()


class TickStatusTests(unittest.TestCase):
    def test_pending_and_empty_are_success_but_failure_is_not(self):
        from ameesh import release
        args = types.SimpleNamespace(project="nexlink", now=100, release_command="tick",
                                     json=True)
        project = rc.ProjectRelease("nexlink", "/unused", model="nexlink")
        for result in ("waiting-ci", "waiting-staging", "empty", "failed"):
            with self.subTest(result=result), \
                    mock.patch.object(release, "_project_names", return_value=["nexlink"]), \
                    mock.patch("ameesh.db.open_db"), \
                    mock.patch.object(rc, "execute", return_value={"result": result}), \
                    mock.patch("ameesh.staging_pending.tick", return_value={}), \
                    mock.patch("builtins.print"):
                self.assertEqual(release._release_tick(None, args, {"nexlink": project}),
                                 int(result == "failed"))
