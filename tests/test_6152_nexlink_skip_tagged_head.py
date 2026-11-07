# SPDX-License-Identifier: AGPL-3.0-only
"""#6152: a Nexlink cutoff skips a base a manual release already tagged.

07/11: rel-01 tagged develop's head `release-11au` by hand at 09:42Z; the
10:00Z automatic tick cut the same tree again as 11av. The tag on the chosen
base is the durable proof: the slot is `skipped` (not `missed`), with no tag,
branch or failure mail.
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import nexlink_release as nx, release, release_cutoff as rc


class TagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.remote = Path(self.temp.name) / "remote.git"
        subprocess.check_output(["git", "init", "-q", "--bare", str(self.remote)])
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "develop")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@localhost")
        (self.repo / "base").write_text("base")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "develop")
        self.project = rc.ProjectRelease("nexlink", str(self.repo), target="develop",
                                         model="nexlink")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True,
                                       stderr=subprocess.STDOUT).strip()

    def commit(self, name):
        (self.repo / name).write_text(name)
        self.git("add", ".")
        self.git("commit", "-qm", name)
        return self.git("rev-parse", "HEAD")

    def test_tag_on_base_wins_over_a_release_branch_moved_ahead(self):
        """The real 11au case: tag on develop's head, branch one merge ahead."""
        self.git("tag", "release-11at", self.base)
        self.git("tag", "release-11au", self.base)
        self.git("push", "-q", "origin", "refs/tags/release-11at",
                 "refs/tags/release-11au")
        moved = self.commit("release-main")
        self.git("branch", "release/11au", moved)
        self.git("push", "-q", "origin", "release/11au")
        self.assertEqual(nx.tagged_at(self.project, self.base), {"11at", "11au"})
        # `branches` sees release/11au at `moved`, so previous_release != base:
        # only the tag itself proves that this exact tree was already cut.
        self.assertNotEqual(nx.previous_release(self.project, moved), self.base)
        self.assertEqual(nx.already_cut(self.project, self.base, moved), "11au")

    def test_untagged_base_is_not_skipped(self):
        tagged = self.commit("tagged")
        self.git("tag", "release-11au", tagged)
        self.git("push", "-q", "origin", "refs/tags/release-11au")
        fresh = self.commit("fresh")
        self.assertEqual(nx.tagged_at(self.project, fresh), set())
        self.assertIsNone(nx.already_cut(self.project, fresh, tagged))

    def test_annotated_tag_is_read_on_its_commit(self):
        self.git("tag", "-a", "release-11av", "-m", "manual", self.base)
        self.git("push", "-q", "origin", "refs/tags/release-11av")
        self.assertEqual(nx.tagged_at(self.project, self.base), {"11av"})

    def test_pending_own_tag_on_the_published_merge_is_not_skipped(self):
        """B/P1: publish tags pending[sha], a merge distinct from the base."""
        self.git("tag", "release-11au", self.base)
        self.git("push", "-q", "origin", "refs/tags/release-11au")
        merged = self.commit("merged-main")
        self.git("tag", "release-11av", merged)
        self.git("push", "-q", "origin", "refs/tags/release-11av")
        pending = {"version": "11av", "base": self.base, "previous": self.base,
                   "sha": merged}
        # The base only proves 11au; reading the own tag on the base alone
        # would abandon the already-published 11av pending.
        self.assertEqual(nx.tagged_at(self.project, self.base), {"11au"})
        self.assertIsNone(nx.pending_already_cut(self.project, pending))

    def test_pending_whose_previous_release_is_its_base_is_skipped(self):
        self.git("branch", "release/11au", self.base)
        self.git("push", "-q", "origin", "release/11au")
        pending = {"version": "11av", "base": self.base, "previous": self.base}
        self.assertEqual(nx.pending_already_cut(self.project, pending), "11au")
        own = dict(pending, version="11au")
        self.assertIsNone(nx.pending_already_cut(self.project, own))


class PlanTests(unittest.TestCase):
    def test_plan_marks_a_tagged_base_as_skipped(self):
        cfg = types.SimpleNamespace()
        project = rc.ProjectRelease("nexlink", "/unused", target="develop", model="nexlink")
        main, head, base = "a" * 40, "b" * 40, "c" * 40
        with mock.patch.object(rc, "compiled_rules",
                               return_value=(object(), types.SimpleNamespace(commit="d" * 40))), \
                mock.patch.object(rc, "cutoff_slot", return_value=(1000, "11:00")), \
                mock.patch.object(nx, "snapshot",
                                  return_value=(head, base, main, {}, None)), \
                mock.patch.object(nx, "previous_release", return_value=main), \
                mock.patch.object(nx, "tagged_at", return_value={"11au"}), \
                mock.patch.object(nx, "choose_version") as choose:
            plan = nx.plan(cfg, project, now=1000)
        self.assertEqual(plan.skipped, "11au")
        self.assertEqual(plan.version, "11au")
        choose.assert_not_called()
        self.assertIn("Créneau sauté", plan.notes)


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.temp.name)
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink")
        for name, value in (("compiled_rules", (object(), types.SimpleNamespace(commit="d" * 40))),
                            ("cutoff_slot", (1000, "11:00")),
                            ("fetch", None),
                            ("send_report", {}),
                            ("write_report", "/tmp/report.json")):
            patcher = mock.patch.object(rc, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(nx, "snapshot",
                                    return_value=("b" * 40, "c" * 40, "a" * 40, {}, None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tagged_base_skips_without_tag_branch_or_mail(self):
        with mock.patch.object(nx, "previous_release", return_value="a" * 40), \
                mock.patch.object(nx, "tagged_at", return_value={"11au"}), \
                mock.patch.object(nx, "merge_candidate") as merge, \
                mock.patch.object(nx, "ensure_ref") as ensure, \
                mock.patch.object(nx, "publish") as publish, \
                mock.patch.object(nx, "ci_plan") as ci_plan, \
                mock.patch.object(nx, "workflow_step") as dispatch:
            report = nx.execute(self.cfg, object(), self.project, now=1000)
        self.assertEqual(report["result"], "skipped")
        self.assertEqual(report["version"], "11au")
        self.assertIn("créneau sauté : tête déjà coupée en release-11au",
                      report["message"])
        merge.assert_not_called()
        ensure.assert_not_called()
        publish.assert_not_called()
        ci_plan.assert_not_called()
        dispatch.assert_not_called()
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual(slot["last_result"], "skipped")
        self.assertEqual(slot["last_cutoff"], 1000)
        self.assertNotIn("pending_nexlink", slot)
        self.assertNotEqual(slot["last_result"], "missed")
        rc.send_report.assert_called_once()
        self.assertIn("créneau sauté : tête déjà coupée en release-11au",
                      rc.send_report.call_args.args[3])
        self.assertFalse(rc.send_report.call_args.kwargs["urgent"])

    def save_pending(self, version="11av", base="c" * 40):
        pending = dict(schema=rc.SCHEMA, project="nexlink", version=version,
                       tag="release-" + version, integration="integration/release-" + version,
                       head=base, base=base, main="a" * 40, sha="e" * 40,
                       cutoff_at=1000, label="11:00", previous="a" * 40, empty=True,
                       ci_max_inflight=3, ci_parts=[{"workflow": "backend-suite.yml"}])
        rc.save_state(self.cfg, {"schema": rc.SCHEMA,
                                 "projects": {"nexlink": {"pending_nexlink": pending}}})
        return pending

    def test_pending_from_older_code_is_skipped_before_any_tag(self):
        """The live 11av pending (10:00Z) must not resume on the 11au base."""
        self.save_pending()
        dropped = lambda project, slot, save: (slot.pop("abandoned_integration"), save())
        with mock.patch.object(nx, "release_tags", return_value={"11au": "c" * 40}), \
                mock.patch.object(nx, "drop_abandoned", side_effect=dropped) as drop, \
                mock.patch.object(nx, "ensure_ref") as ensure, \
                mock.patch.object(nx, "merge_candidate") as merge, \
                mock.patch.object(nx, "publish") as publish, \
                mock.patch.object(nx, "workflow_step") as dispatch:
            report = nx.execute(self.cfg, object(), self.project, now=1000)
        self.assertEqual(report["result"], "skipped")
        self.assertEqual(report["version"], "11au")
        drop.assert_called_once()
        ensure.assert_not_called()
        merge.assert_not_called()
        publish.assert_not_called()
        dispatch.assert_not_called()
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertNotIn("pending_nexlink", slot)
        self.assertEqual(slot["last_result"], "skipped")
        self.assertEqual(slot["last_cutoff"], 1000)

    def test_pending_with_its_own_tag_resumes_instead_of_skipping(self):
        """A crash after the pending's own tag push must reconcile, not skip."""
        self.save_pending()
        with mock.patch.object(nx, "release_tags", return_value={"11av": "e" * 40}), \
                mock.patch.object(nx, "ensure_ref") as ensure, \
                mock.patch.object(nx, "merge_candidate") as merge, \
                mock.patch.object(nx, "workflow_step", return_value=True), \
                mock.patch.object(nx, "publish", return_value="https://example/pr/1") as publish:
            report = nx.execute(self.cfg, object(), self.project, now=1000)
        self.assertEqual(report["result"], "empty")
        self.assertEqual(report["version"], "11av")
        ensure.assert_called_once()
        merge.assert_not_called()
        publish.assert_called_once()
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual(slot["last_result"], "empty")
        self.assertNotIn("pending_nexlink", slot)

    def test_untagged_base_still_cuts_normally(self):
        with mock.patch.object(nx, "previous_release", return_value="a" * 40), \
                mock.patch.object(nx, "tagged_at", return_value=set()), \
                mock.patch.object(nx, "choose_version", return_value="11av"), \
                mock.patch.object(nx, "merge_candidate", return_value="e" * 40) as merge, \
                mock.patch.object(nx, "ensure_ref") as ensure, \
                mock.patch.object(nx, "ci_plan",
                                  return_value={"parts": [{"workflow": "backend-suite.yml"}],
                                                "max_inflight": 2}), \
                mock.patch.object(nx, "workflow_step", return_value=True), \
                mock.patch.object(nx, "migration_gate", return_value={}), \
                mock.patch.object(nx, "publish", return_value="https://example/pr/1") as publish, \
                mock.patch.object(rc, "same_tree", return_value=False):
            report = nx.execute(self.cfg, object(), self.project, now=1000)
        self.assertEqual(report["result"], "released")
        self.assertEqual(report["version"], "11av")
        merge.assert_called_once()
        ensure.assert_called_once()
        publish.assert_called_once()
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual(slot["last_result"], "released")
        self.assertNotEqual(slot["last_result"], "skipped")


class DryRunTests(unittest.TestCase):
    def plan(self, skipped):
        return types.SimpleNamespace(
            canon_commit="d" * 40, cutoff_at=1000, cutoff_label="11:00",
            head="b" * 40, green=True, last_green="b" * 40, base="c" * 40,
            current_version="", previous={"commit": "a" * 40, "release_commit": "a" * 40},
            version=skipped or "11av", floor="patch",
            floor_reason="Nexlink release branch sequence", packets=[], direct=[],
            migrations=[], included=[], excluded=[], notes="", empty=False,
            tree_green=None, kept_active={}, skipped=skipped)

    def test_dry_run_prints_skipped(self):
        project = rc.ProjectRelease("nexlink", "/unused", target="develop", model="nexlink")
        with mock.patch.object(rc, "build_plan", return_value=self.plan("11au")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                release._dry_run(None, project, now=1000, version=None, as_json=False)
        self.assertIn("créneau sauté : tête déjà coupée en release-11au", out.getvalue())
        self.assertIn("skipped", out.getvalue())

    def test_dry_run_json_reports_skipped(self):
        project = rc.ProjectRelease("nexlink", "/unused", target="develop", model="nexlink")
        with mock.patch.object(rc, "build_plan", return_value=self.plan("11au")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                release._dry_run(None, project, now=1000, version=None, as_json=True)
        report = json.loads(out.getvalue())
        self.assertEqual(report["result"], "skipped")
        self.assertEqual(report["skipped"], "11au")

    def test_dry_run_keeps_a_fresh_base_as_planned(self):
        project = rc.ProjectRelease("nexlink", "/unused", target="develop", model="nexlink")
        with mock.patch.object(rc, "build_plan", return_value=self.plan(None)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                release._dry_run(None, project, now=1000, version=None, as_json=True)
        report = json.loads(out.getvalue())
        self.assertEqual(report["result"], "planned")
        self.assertIsNone(report["skipped"])


class TickStatusTests(unittest.TestCase):
    def test_skipped_is_a_normal_tick_result(self):
        args = types.SimpleNamespace(project="nexlink", now=100, release_command="tick",
                                     json=True)
        project = rc.ProjectRelease("nexlink", "/unused", model="nexlink")
        with mock.patch.object(release, "_project_names", return_value=["nexlink"]), \
                mock.patch("ameesh.db.open_db"), \
                mock.patch.object(rc, "execute", return_value={"result": "skipped"}), \
                mock.patch("ameesh.staging_pending.tick", return_value={}), \
                mock.patch("builtins.print"):
            self.assertEqual(release._release_tick(None, args, {"nexlink": project}), 0)


if __name__ == "__main__":
    unittest.main()
