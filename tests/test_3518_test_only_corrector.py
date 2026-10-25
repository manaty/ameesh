# SPDX-License-Identifier: AGPL-3.0-only
"""#3518: test-only corrections exclude the person, not their family."""
from __future__ import annotations

import types
from unittest import mock

import os

from ameesh import registry, review_designation as rd, work
from .support import PgTestCase
from . import test_2739_review_correction as fixture
from .test_2447_review_designation_activation import _Repo


def canon():
    return types.SimpleNamespace(root="fixture", sources=[], federation={
        "review_policies": {"risk_classes": {"projects": {"ameesh": {
            "default": "light", "rules": [
                {"paths": ["**/*.sql", "tests/parts/**", "src/**"],
                 "class": "sensitive"}]}}}}})


class TestOnlyCorrectorTest(PgTestCase):
    receipt = fixture.GateCorrectorTest.receipt
    verdict_receipt = fixture.GateCorrectorTest.verdict_receipt
    gate = fixture.GateCorrectorTest.gate

    def setUp(self):
        super().setUp()
        for table in ("turn_costs", "provenance_receipts", "canon_state",
                      "quota_gauge_readings"):
            self.db.execute("DELETE FROM %s" % table)
        self.repo = _Repo(os.path.join(self.tmp, "depot"))
        self.repo.write("src/ameesh/work.py", "base\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/work.py", "lot\n")
        self.author_commit = self.repo.commit("author: lot")
        for name, harness in (("author", "deepseek"), ("cx2", "codex"),
                              ("cx3", "codex"), ("dev1", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.repo.root, status="idle")
            self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                            (name,))
        self.item = work.add(self.db, title="Désignation au gel", app="ameesh",
                             assignee="author")
        self.lot = int(self.item["id"])
        work.move(self.db, self.lot, "build")
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        # the previous freeze designated cx2; the column is the trace of it
        self.db.execute("UPDATE work_items SET reviewers = ARRAY['cx2'] WHERE id = %s",
                        (self.lot,))
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, session, recorded_at)"
            " VALUES ('author', 'deepseek', 's-deepseek', now() - interval '1 minute')")
        self.receipt("author", "deepseek", [self.author_commit])
        patch = mock.patch.object(work, "configured_canons", return_value=[canon()])
        patch.start()
        self.addCleanup(patch.stop)

    def freeze(self, paths, *, actor="cx2"):
        for path in paths:
            self.repo.write(path, "correction\n")
        self.frozen = self.repo.commit("reviewer correction")
        self.receipt(actor, "codex", [self.frozen])
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="author")
        for name, family in (("cx2", "codex"), ("cx3", "codex"), ("dev1", "claude")):
            mark = work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                                  actor=name, verdict="ok")
            self.verdict_receipt(mark, family)

    def test_test_only_fix_keeps_family_but_never_its_own_verdict(self):
        self.freeze(["tests/test_product.py"])
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["corrections"], ["cx2"])
        self.assertEqual({row["actor"] for row in out["accepted"]}, {"cx3", "dev1"})
        self.assertNotIn("cx2", {row["name"] for row in out["authors"]})

    def test_mixed_correction_excludes_family(self):
        self.freeze(["tests/test_product.py", "src/ameesh/work.py"])
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertEqual([row["actor"] for row in out["accepted"]], ["dev1"])

    def test_ci_plan_correction_excludes_family(self):
        self.freeze(["tests/parts/always.txt"])
        self.assertFalse(self.gate()["ok"])

    def test_sensitive_test_sql_excludes_family(self):
        self.freeze(["tests/test_access.sql"])
        self.assertFalse(self.gate()["ok"])

    def test_sensitive_non_sql_test_keeps_family_excluded(self):
        policy = canon()
        policy.federation["review_policies"]["risk_classes"]["projects"]["ameesh"][
            "rules"].append({"paths": ["tests/test_product.py"], "class": "sensitive"})
        self.freeze(["tests/test_product.py"])
        with mock.patch.object(work, "configured_canons", return_value=[policy]):
            self.assertFalse(self.gate()["ok"])

    def test_unknown_policy_does_not_grant_an_exception(self):
        self.freeze(["tests/test_product.py"])
        with mock.patch.object(work, "configured_canons", return_value=[]):
            self.assertFalse(self.gate()["ok"])

    def test_a_move_from_product_to_tests_is_not_test_only(self):
        self.repo.git("mv", "src/ameesh/work.py", "test_product.py")
        self.freeze(["tests/test_product.py"])
        self.assertFalse(self.gate()["ok"])

    def test_large_test_correction_still_excludes_the_person(self):
        self.repo.write("tests/test_large.py", "assert True\n" * 60)
        self.freeze(["tests/test_product.py"])
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["corrections"], ["cx2"])
        self.assertNotIn("cx2", [row["actor"] for row in out["accepted"]])

    def test_test_only_corrector_survives_a_squash_without_excluding_family(self):
        self.freeze(["tests/test_product.py"])
        tree = self.repo.git("rev-parse", "HEAD^{tree}")
        self.frozen = self.repo.git("commit-tree", tree, "-p", self.base,
                                    "-m", "squashed product and tests")
        self.repo.git("reset", "--hard", self.frozen)
        self.receipt("author", "deepseek", [self.frozen])
        work.milestone(self.db, self.lot, "frozen", sha=self.frozen, actor="author")
        for actor, harness in (("cx2", "codex"), ("cx3", "codex"), ("dev1", "claude")):
            mark = work.milestone(self.db, self.lot, "verdict", sha=self.frozen,
                                  actor=actor, verdict="ok")
            self.verdict_receipt(mark, harness)
        out = self.gate()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["corrections"], ["cx2"])
        self.assertEqual({row["actor"] for row in out["accepted"]}, {"cx3", "dev1"})

    def test_prior_product_correction_still_excludes_a_current_test_corrector_family(self):
        self.freeze(["src/ameesh/work.py"])
        tree = self.repo.git("rev-parse", "HEAD^{tree}")
        squashed = self.repo.git("commit-tree", tree, "-p", self.base,
                                "-m", "consolidated product")
        self.repo.git("reset", "--hard", squashed)
        self.receipt("author", "deepseek", [squashed])
        self.freeze(["tests/test_product.py"])
        out = self.gate()
        self.assertFalse(out["ok"], out)
        self.assertEqual([row["actor"] for row in out["accepted"]], ["dev1"])

    def test_designation_keeps_the_family_and_excludes_manual_self_verdict(self):
        fixer = {"name": "cx2", "family": "codex", "test_only": True}
        out = rd.select(required=2,
                        authors=[{"name": "author", "family": "deepseek"}],
                        candidates=[rd.Candidate("cx2", "codex", True, 0),
                                    rd.Candidate("cx3", "codex", True, 0),
                                    rd.Candidate("dev1", "claude", True, 0)],
                        families=("codex", "claude", "deepseek"),
                        already=[{"name": "cx2", "family": "codex"}],
                        fixers=[fixer])
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual({row["name"] for row in out["reviewers"]}, {"cx3", "dev1"})
        self.assertEqual(out["manual"], [])
