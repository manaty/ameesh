# SPDX-License-Identifier: AGPL-3.0-only
"""Published ancestry, admitted member ancestry, and the unknown outsider."""
import os
import subprocess
import types
from unittest import TestCase

from ameesh import merge_queue, provenance_scope, radar, registry, storage, work
from . import test_3091_packet_member_base as fixture
from .support import PgTestCase
from .test_2447_review_designation_activation import _Repo


class ProductionGateTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name, harness in (("author", "deepseek"), ("r_codex", "codex"),
                              ("r_claude", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.tmp, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                            " WHERE name = %s", (name,))
        self.repo = _Repo(os.path.join(self.tmp, "packet"))
        self.repo.git("checkout", "-q", "-b", "main")
        self.repo.write("README.md", "base\n")
        self.repo.write(".github/workflows/tests.yml", "name: tests\n")
        self.base = self.repo.commit("integration base", agent="author")
        self.receipt(self.base, "author", "deepseek")

    receipt = fixture.PacketMemberBaseTest.receipt
    verdict = fixture.PacketMemberBaseTest.verdict
    member_row = fixture.PacketMemberBaseTest.member_row
    record = fixture.PacketMemberBaseTest.record
    stacked_vehicle = fixture.PacketMemberBaseTest.stacked_vehicle

    def prepare(self, *, outsider=False, no_merge_receipt=False):
        units = self.stacked_vehicle()
        # The first member can attest its own old commit by its reference
        # fallback, but that commit cannot be attributed to the composer.
        self.db.execute("DELETE FROM provenance_receipts WHERE subject = %s",
                        ("commit:" + units[0][1],))
        remote = os.path.join(self.tmp, "published.git")
        subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
        self.repo.git("remote", "add", "origin", remote)
        self.repo.git("checkout", "-q", "-b", "local-production", self.base)
        self.published = []
        for number in range(3):
            self.repo.write("src/production-%d.py" % number, "published\n")
            self.published.append(self.repo.commit("published without receipt", agent="old-agent"))
        # Conflict resolution in the return merge remains the composer's work.
        self.repo.write("src/shared.py", "production\n")
        self.published.append(self.repo.commit("published shared file", agent="old-agent"))
        self.repo.git("push", "-q", "origin", "HEAD:refs/heads/live")
        self.repo.git("update-ref", "refs/remotes/origin/live", self.base)
        self.repo.git("checkout", "-q", "integration/stack")
        if outsider:
            self.repo.write("src/unpublished.py", "unknown\n")
            self.unknown = self.repo.commit("not in production or any member", agent="unregistered")
        self.repo.write("src/shared.py", "vehicle\n")
        own = self.repo.commit("prepare resolution", agent="composer")
        self.receipt(own, "composer", "codex")
        result = subprocess.run(["git", "-C", self.repo.root, "merge", "--no-ff", "--no-commit",
                                 "local-production"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.repo.write("src/shared.py", "resolved\n")
        self.packet = self.repo.commit("return production with resolution", agent="composer")
        if not no_merge_receipt:
            self.receipt(self.packet, "composer", "codex")
        registry.upsert(self.db, "composer", harness="codex", host=self.cfg.host,
                        cwd=self.tmp, status="idle")
        self.db.execute("UPDATE work_items SET assignee = 'composer' WHERE id = %s",
                        (self.vehicle,))
        storage.of(self.db).work.add_milestone(self.vehicle, "frozen", sha=units[0][1],
                                               actor="composer", verdict=None, note="prior vehicle freeze")
        storage.of(self.db).work.add_milestone(self.vehicle, "frozen", sha=self.packet,
                                               actor="composer", verdict=None, note="")
        self.lot = self.vehicle
        self.verdict("r_claude", self.packet)
        self.verdict("author", self.packet)
        self.gate_cfg = types.SimpleNamespace(radar={"projects": {"ameesh": {
            "repo": self.repo.root, "target": "develop", "production_branch": "live"}}})
        return units

    def gate(self, cfg=True):
        row = work.get(self.db, self.vehicle)
        marks = work.milestones(self.db, self.vehicle)
        # Require two independent opinions on the vehicle resolution.
        canon = fixture._canon()
        canon.federation["review_policies"]["risk_classes"]["projects"]["ameesh"]["rules"].append(
            {"paths": ["src/**"], "class": "sensitive", "critical": True})
        # Supply the synthetic policy through the same vehicle reader as prod.
        vehicle = fixture.packet_reviews.need(
            self.db, self.vehicle, row, marks, self.repo.root, self.base, self.packet,
            cfg=self.gate_cfg if cfg else None, canons=[canon])
        return merge_queue.review_gate(
            self.db, marks, self.packet, repo=self.repo.root,
            klass={"class": "normal", "base_sha": self.base, "files": [{"path": "src/shared.py"}]},
            item=row, cfg=self.gate_cfg if cfg else None, target=self.base, vehicle=vehicle)

    def test_packet_174_production_and_admitted_member_are_traced(self):
        units = self.prepare()
        out = self.gate()
        self.assertTrue(out["ok"], out)
        exclusions = out["provenance_exclusions"]
        published = {row["commit"] for row in exclusions if row["reason"] == "published-production"}
        self.assertEqual(published, set(self.published))
        self.assertIn(units[0][1], {row["commit"] for row in exclusions
                                   if row["reason"] == "admitted-member"})
        self.assertNotIn(self.packet, {row["commit"] for row in exclusions})
        self.assertIn({"name": "composer", "family": "codex"}, out["authors"])
        for sha in self.published:
            self.assertIn(sha, out["provenance_manifest"])
        self.assertIn("refs/heads/live", out["provenance_manifest"])

    def test_unpublished_unknown_still_refuses(self):
        self.prepare(outsider=True)
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn(self.unknown[:12], out["why"])

    def test_merge_resolution_without_receipt_is_not_hidden(self):
        self.prepare(no_merge_receipt=True)
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn(self.packet[:12], out["why"])

    def test_no_production_configuration_does_not_exclude_published_commits(self):
        self.prepare()
        out = self.gate(cfg=False)
        self.assertFalse(out["ok"], out)
        self.assertIn(self.published[0][:12], out["why"])

    def test_local_tracking_ref_cannot_attest_unpublished_commit(self):
        self.prepare(outsider=True)
        self.repo.git("update-ref", "refs/remotes/origin/live", self.packet)
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn(self.unknown[:12], out["why"])

    def test_published_object_is_fetched_without_creating_tracking_ref(self):
        self.prepare()
        scratch = _Repo(os.path.join(self.tmp, "scratch"))
        scratch.git("remote", "add", "origin", self.repo.git("remote", "get-url", "origin"))
        scope = provenance_scope.production(self.gate_cfg, {"app": "ameesh"}, scratch.root)
        self.assertEqual(scope["head"], self.published[-1])
        self.assertEqual(scratch.git("rev-parse", "--verify", scope["head"] + "^{commit}"),
                         self.published[-1])
        self.assertEqual(scratch.git("for-each-ref", "--format=%(refname)"), "")

    def test_remote_failure_refuses_despite_local_production_ref(self):
        self.prepare()
        self.repo.git("update-ref", "refs/remotes/origin/live", self.packet)
        self.repo.git("remote", "set-url", "origin", os.path.join(self.tmp, "absent.git"))
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertIn("remote branch unreadable", out["why"])


class ConfigurationTest(TestCase):
    def test_production_branch_is_explicit_and_validated(self):
        self.assertEqual(radar.parse_settings({"projects": {"p": {"repo": "/fixture"}}})
                         .projects["p"].production_branch, "")
        with self.assertRaises(ValueError):
            radar.parse_settings({"projects": {"p": {"repo": "/fixture", "production_branch": "--bad"}}})
