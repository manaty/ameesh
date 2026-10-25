# SPDX-License-Identifier: AGPL-3.0-only
"""#3554 — une baisse d'exigence du canon (avis ou classe) demande deux avis.

Le constat : un diff du canon ne se classait que par ses chemins, donc une PR
qui retirait une règle de secrets passait en léger. Le module compare les
politiques **compilées** (base et tête) sur les fichiers suivis des dépôts des
projets déclarés ; une baisse rend deux avis, un durcissement ou un changement
neutre laisse le barème inchangé.

Témoins : home#176 (18 fichiers 1→2) → 1 avis ; retrait de
`deploy/**/*secret*` → 2 avis et la liste au journal ; un changement neutre →
1 avis.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import (canon_lowering, lot_class, merge_queue, registry,
                    review_designation_activation as act, review_floor, storage,
                    work)

from .support import PgTestCase

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

MECHANICS = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
reviews:
  families: [claude, codex, deepseek]
  exclude_author_family: true
  distinct_families: true
  light_reviews: 1
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
projects: {}
"""


def _federation(*, secret_rule=True, batch_reviews=1, tools_class=True,
                project=True):
    """La politique de base ; les témoins retirent une règle ou en ajoutent."""
    ameesh_rules = ["          - paths:\n              - \"deploy/**\"\n"
                    "            class: sensitive"]
    ameesh_counts = ["          - paths:\n              - \"deploy/**\"\n"
                     "            reviews: 1"]
    if secret_rule:
        ameesh_rules.append("          - paths:\n"
                            "              - \"deploy/**/*secret*\"\n"
                            "            class: sensitive")
        ameesh_counts.append("          - paths:\n"
                             "              - \"deploy/**/*secret*\"\n"
                             "            reviews: 2")
    if tools_class:
        ameesh_rules.append("          - paths:\n              - \"tools/**\"\n"
                            "            class: sensitive")
        ameesh_counts.append("          - paths:\n              - \"tools/**\"\n"
                             "            reviews: 1")
    if batch_reviews:
        ameesh_rules.append("          - paths:\n              - \"src/batch/**\"\n"
                            "            class: sensitive")
        ameesh_counts.append("          - paths:\n              - \"src/batch/**\"\n"
                             "            reviews: %d" % batch_reviews)
    ameesh = ("      ameesh:\n        default: light\n        rules:\n"
              + "\n".join(ameesh_rules) + "\n")
    if not project:
        ameesh = ""
    return ("federation: \"0.1\"\nid: manaty\nreview_policies:\n"
            "  risk_classes:\n    default: normal\n    projects:\n"
            + ameesh
            + "      nexlink:\n        default: sensitive\n        rules: []\n"
            "  train_reviews:\n    schema: ameesh-train-reviews-policy/1\n"
            "    projects:\n"
            + ("      ameesh:\n        rules:\n"
               + "\n".join(ameesh_counts) + "\n" if project else "")
            + "      nexlink:\n        rules:\n          - paths:\n"
              "              - \"**\"\n            reviews: 1\n")


class _Repo:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        proc = subprocess.run(["git", "-C", self.root, *args],
                              capture_output=True, text=True, env=ENV)
        if proc.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, path, text):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full) or self.root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")


def _cfg(canon, radar=None):
    return types.SimpleNamespace(
        canon=canon, canon_ref="", canon_untrusted=False, extra_canons=(),
        state_dir=tempfile.mkdtemp(prefix="ameesh-3554-state-"),
        radar={"projects": radar or {}})


class DetectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-3554-")
        self.addCleanup(self._cleanup)
        self.canon = _Repo(os.path.join(self.tmp, "canon"))
        self.canon.write("processus/mecanique.yaml", MECHANICS)
        self.canon.write("federation.yaml", _federation())
        self.canon.write("index.md", "# canon\n")
        self.canon.write("agents/pilot.md", "---\ntitle: pilot\n---\n")
        self.base = self.canon.commit("base")
        self.project = _Repo(os.path.join(self.tmp, "ameesh"))
        for path in ("deploy/good.py", "deploy/keep-secret.py",
                     "tools/run.py", "README.md"):
            self.project.write(path, "x\n")
        for index in range(18):
            self.project.write("src/batch/f%02d.py" % index, "x\n")
        self.project.commit("project")
        self.cfg = _cfg(self.canon.root, {
            "ameesh": {"repo": self.project.root, "target": "main"},
            "nexlink": {"repo": self.project.root, "target": "main"},
        })

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _head(self, text):
        self.canon.write("federation.yaml", text)
        return self.canon.commit("head")

    def _detect(self, head, paths=("federation.yaml",)):
        return canon_lowering.for_lot(self.cfg, self.canon.root, self.base,
                                      head, list(paths))

    def test_only_the_policy_paths_trigger_the_comparison(self):
        self.assertTrue(canon_lowering.touches(["federation.yaml"]))
        self.assertTrue(canon_lowering.touches(["agents/pilot.md"]))
        self.assertTrue(canon_lowering.touches(["processus/mecanique.yaml"]))
        self.assertFalse(canon_lowering.touches(["src/ameesh/canon.py"]))
        head = self._head(_federation(secret_rule=False))
        self.assertIsNone(canon_lowering.for_lot(
            self.cfg, self.canon.root, self.base, head,
            ["src/ameesh/canon.py"]))

    def test_home_176_is_a_hardening_and_keeps_one_review(self):
        # 18 fichiers passent de 1 à 2 avis : rien ne baisse.
        head = self._head(_federation(batch_reviews=2))
        found = self._detect(head)
        self.assertEqual(found.floor, 0)
        self.assertEqual(found.lowered, ())
        self.assertEqual(found.hardened, 18)
        self.assertEqual(found.journal_text(), "")

    def test_removing_a_secret_rule_lowers_the_file_and_lists_it(self):
        head = self._head(_federation(secret_rule=False))
        found = self._detect(head)
        self.assertEqual(found.floor, 2)
        self.assertEqual([item.path for item in found.lowered],
                         ["deploy/keep-secret.py"])
        item = found.lowered[0]
        self.assertEqual((item.before_class, item.before_reviews,
                          item.after_class, item.after_reviews),
                         ("sensitive", 2, "sensitive", 1))
        text = found.journal_text()
        self.assertTrue(text.startswith(canon_lowering.PREFIX), text)
        self.assertIn("deploy/keep-secret.py", text)
        self.assertIn("sensitive/2→sensitive/1", text)

    def test_losing_a_class_alone_is_a_lowering(self):
        # tools/run.py passe de sensible à léger sans perdre de compte.
        head = self._head(_federation(tools_class=False))
        found = self._detect(head)
        self.assertEqual(found.floor, 2)
        self.assertEqual([item.path for item in found.lowered], ["tools/run.py"])
        self.assertEqual(found.lowered[0].before_class, "sensitive")
        self.assertEqual(found.lowered[0].after_class, "light")

    def test_a_neutral_change_keeps_one_review(self):
        head = self._head(_federation() + "# commentaire\n")
        found = self._detect(head)
        self.assertEqual(found.floor, 0)
        self.assertEqual(found.lowered, ())
        self.assertEqual(found.hardened, 0)
        self.assertGreater(found.unchanged, 0)


    def test_a_git_timeout_in_the_comparison_is_a_doubt_not_a_crash(self):
        head = self._head(_federation(secret_rule=False))
        with mock.patch.object(
                canon_lowering.plan_git, "local_commit",
                side_effect=canon_lowering.canon_mod.GitError("timeout sonde")):
            found = canon_lowering.for_lot(
                self.cfg, self.canon.root, self.base, head, ["federation.yaml"])
        self.assertIsInstance(found, canon_lowering.Detection)
        self.assertEqual(found.floor, 2)
        self.assertIn("comparaison impossible", " ".join(found.errors))

    def test_a_project_revision_timeout_fails_closed_without_raising(self):
        head = self._head(_federation(secret_rule=False))
        with mock.patch.object(
                canon_lowering, "_revision",
                side_effect=canon_lowering.canon_mod.GitError("timeout projet")):
            found = self._detect(head)
        self.assertEqual(found.floor, 2)
        self.assertTrue(any("dépôt illisible" in error for error in found.errors),
                        found.errors)


    def test_removing_a_project_from_the_policy_is_a_doubt(self):
        head = self._head(_federation(project=False))
        found = self._detect(head)
        self.assertEqual(found.floor, 2)
        self.assertTrue(found.errors)
        self.assertIn("ameesh", " ".join(found.errors))
        self.assertIn("ameesh", found.journal_text())

    def test_an_unreadable_head_policy_is_a_doubt(self):
        head = self._head(_federation() + "review_policies: [\n")
        found = self._detect(head)
        self.assertEqual(found.floor, 2)
        self.assertTrue(found.errors)


    def test_a_changed_policy_without_its_repository_is_a_doubt(self):
        # Sans dépôt, on ne peut pas prouver que 1→0 avis est un durcissement.
        self.cfg.radar = {"projects": {}}
        self.canon.write("processus/mecanique.yaml",
                         MECHANICS.replace("light_reviews: 1", "light_reviews: 0"))
        head = self.canon.commit("moins d'avis")
        found = self._detect(head)
        self.assertEqual(found.floor, 2)
        self.assertTrue(any("dépôt illisible" in error for error in found.errors),
                        found.errors)

    def test_an_undeclared_project_repository_is_named_not_guessed(self):
        self.cfg.radar = {"projects": {
            "ameesh": {"repo": self.project.root, "target": "main"}}}
        head = self._head(_federation(secret_rule=False))
        found = self._detect(head)
        self.assertEqual(found.floor, 2)
        self.assertEqual([item.path for item in found.lowered],
                         ["deploy/keep-secret.py"])
        self.assertTrue(any(name == "nexlink" for name, _why in found.unmeasured))


class FloorIntegrationTest(PgTestCase):
    """Le plancher automatique suit la classe jusqu'à la porte et au journal."""

    def setUp(self):
        super().setUp()
        self.tmp_repo = tempfile.mkdtemp(prefix="ameesh-3554-db-")
        self.addCleanup(self._cleanup)
        self.canon = _Repo(os.path.join(self.tmp_repo, "canon"))
        self.canon.write("processus/mecanique.yaml", MECHANICS)
        self.canon.write("federation.yaml", _federation())
        self.canon.write("agents/pilot.md", "---\ntitle: pilot\n---\n")
        self.base = self.canon.commit("base")
        self.canon.git("checkout", "-q", "-b", "canon/3554-policy")
        self.canon.write("federation.yaml", _federation(secret_rule=False))
        self.head = self.canon.commit("head")
        self.project = _Repo(os.path.join(self.tmp_repo, "ameesh"))
        self.project.write("deploy/keep-secret.py", "x\n")
        self.project.write("README.md", "x\n")
        self.project.commit("project")
        self.cfg = _cfg(self.canon.root, {
            "ameesh": {"repo": self.project.root, "target": "main"}})
        registry.upsert(self.db, "author", harness="deepseek",
                        host="test", status="idle")
        self.item = work.add(self.db, title="canon : retrait d'une règle",
                             app="ameesh", branch="canon/3554-policy",
                             branch_target="main", assignee="author")
        self.lot = int(self.item["id"])
        self.mark = storage.of(self.db).work.add_milestone(
            self.lot, "frozen", sha=self.head, actor="author", verdict=None,
            note="")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp_repo, ignore_errors=True)


    def test_designation_raises_the_required_count_from_one_to_two(self):
        policy = act.Policy(rules=(), families=("claude", "codex", "deepseek"),
                            risk="light", required=1, ref="canon@base",
                            commit=self.base, source=self.canon.root)
        target = act.Target(repo=self.canon.root, declared="main",
                            sha=self.base, head=self.head)
        with mock.patch.object(act, "_policy", return_value=policy):
            inputs = act.prepare(
                self.cfg, self.db, self.lot,
                freeze={"id": int(self.mark["id"]), "sha": self.head},
                fetch=False, target=target)
        self.assertNotIsInstance(inputs, act.Unavailable, inputs)
        self.assertEqual(inputs.policy.required, 2)
        self.assertEqual(inputs.policy.canon_lowering["floor"], 2)
        self.assertEqual([item["path"] for item in
                          inputs.policy.canon_lowering["lowered"]],
                         ["deploy/keep-secret.py"])

    def test_the_class_carries_two_verdicts_to_the_gate(self):
        klass = lot_class.of_lot(self.db, self.lot, cfg=self.cfg)
        self.assertEqual(klass["reviews_floor"], 2)
        self.assertEqual(klass["reviews_floor_by"], "canon-policy-lowering")
        self.assertEqual(merge_queue.required_verdicts(klass), 2)
        self.assertEqual([item["path"] for item in
                          klass["canon_lowering"]["lowered"]],
                         ["deploy/keep-secret.py"])


class JournalTest(PgTestCase):
    """La liste des fichiers abaissés est écrite au journal, une fois par tête."""


    def test_a_lower_pilot_floor_never_overrides_the_canon_floor(self):
        with mock.patch.object(review_floor, "of_lot",
                               return_value={"floor": 1, "actor": "pilot"}):
            klass = review_floor.apply(
                {"reviews_floor": 2,
                 "reviews_floor_by": "canon-policy-lowering"}, mock.Mock(), 1)
        self.assertEqual(klass["reviews_floor"], 2)
        self.assertEqual(klass["reviews_floor_by"], "canon-policy-lowering")

    def test_the_lowered_files_are_journaled_once(self):
        registry.upsert(self.db, "author", harness="deepseek", host="test",
                        status="idle")
        item = work.add(self.db, title="canon", app="ameesh",
                        branch="canon/3554", branch_target="main",
                        assignee="author")
        lot = int(item["id"])
        head = "b" * 40
        found = canon_lowering.Detection(
            base="a" * 40, head=head,
            lowered=(canon_lowering.Lowered(
                "ameesh", "deploy/keep-secret.py", "sensitive", "sensitive",
                2, 1),))
        policy = act.Policy(rules=(), families=(), risk="light", required=2,
                            ref="", commit="", source="",
                            canon_lowering=found.to_dict())
        inputs = act.Inputs(item=item, freeze={"id": 7, "sha": head}, repo="",
                            base="a" * 40, paths=["federation.yaml"], commits=[],
                            policy=policy, authors=[], unknown_authors=[],
                            correctors=[], candidates=[], notify="", manual=[],
                            waiver=False)
        act._note_canon_lowering(self.db, item, {"id": 7, "sha": head}, inputs)
        act._note_canon_lowering(self.db, item, {"id": 7, "sha": head}, inputs)
        rows = storage.of(self.db).work.events_noted(
            canon_lowering.PREFIX, 0.0, ids=[lot])
        self.assertEqual(len(rows), 1, rows)
        note = rows[0]["note"]
        self.assertIn("deploy/keep-secret.py", note)
        self.assertIn("sensitive/2→sensitive/1", note)
        self.assertIn(head[:12], note)
        self.assertEqual(rows[0]["actor"], "ameesh")


if __name__ == "__main__":
    unittest.main()
