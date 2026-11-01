# SPDX-License-Identifier: AGPL-3.0-only
"""#5216 — le contrôle de `canon check` (#5191) tourne au gel des lots du canon.

Témoins : un lot `canon/*` qui ajoute un chemin sensible sans nombre d'avis est
refusé et le chemin est nommé ; un lot conforme passe. Un manque déjà à la base
ne retient pas un lot étranger, une mesure impossible ne refuse pas.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import canon_lowering, canon_review_coverage as coverage, freeze_gate, work
from ameesh.deadline import Deadline
from ameesh.work import WorkError

from .support import PgTestCase
from .test_3554_canon_lowering import ENV, MECHANICS

SENSITIVE = "src/ameesh/runner_sandbox.py"
OTHER = "deploy/hote.sh"


def _federation(*classes, counted=()):
    rules = "".join('          - paths:\n              - "%s"\n'
                    "            class: sensitive\n" % path for path in classes)
    counts = "".join('          - paths:\n              - "%s"\n'
                     "            reviews: 2\n" % path for path in counted)
    return ('federation: "0.1"\nid: manaty\nreview_policies:\n'
            "  risk_classes:\n    default: normal\n    projects:\n"
            "      ameesh:\n        default: light\n        rules:\n" + rules
            + "  train_reviews:\n    schema: ameesh-train-reviews-policy/1\n"
            "    projects:\n      ameesh:\n        rules:\n" + counts)


class ForFreezeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.canon = os.path.join(tmp.name, "canon")
        self.project = os.path.join(tmp.name, "project")
        self._commit(self.project, {SENSITIVE: "x\n", OTHER: "x\n", "README.md": "x\n"})
        self.base = self._commit(self.canon, {
            "federation.yaml": _federation(SENSITIVE, counted=[SENSITIVE]),
            "processus/mecanique.yaml": MECHANICS})
        self._git(self.canon, "checkout", "-qb", "canon/5216-lot")
        patcher = mock.patch.object(
            canon_lowering, "_project_repository",
            side_effect=lambda cfg, project, deadline=None: (
                (self.project, "main"), ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _git(self, root, *args):
        return subprocess.run(["git", "-C", root, *args], capture_output=True,
                              text=True, env=ENV, check=True).stdout.strip()

    def _commit(self, root, files):
        os.makedirs(root, exist_ok=True)
        if not os.path.isdir(os.path.join(root, ".git")):
            self._git(root, "init", "-q", "-b", "main")
        for path, text in files.items():
            full = os.path.join(root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(text)
        self._git(root, "add", "-A")
        self._git(root, "commit", "-qm", "c")
        return self._git(root, "rev-parse", "HEAD")

    def _lot(self, text):
        return self._commit(self.canon, {"federation.yaml": text})

    def _check(self, head, **options):
        return coverage.for_freeze(None, self.canon, "main", head, **options)

    def test_lot_adding_sensitive_path_without_count_is_refused_naming_it(self):
        refused, _ = self._check(self._lot(
            _federation(SENSITIVE, OTHER, counted=[SENSITIVE])))
        self.assertEqual(len(refused), 1)
        self.assertIn(OTHER, refused[0])
        self.assertNotIn(SENSITIVE, refused[0])

    def test_compliant_lot_passes(self):
        head = self._lot(_federation(SENSITIVE, OTHER, counted=[SENSITIVE, OTHER]))
        self.assertEqual(self._check(head), ([], []))

    def test_policy_unreadable_at_head_is_refused(self):
        refused, _ = self._check(self._lot("review_policies: [\n"))
        self.assertEqual(len(refused), 1)
        self.assertIn("unreadable", refused[0])

    def test_policy_not_compiling_at_head_is_refused(self):
        refused, _ = self._check(self._commit(
            self.canon, {"processus/mecanique.yaml": "schema: inconnu\n"}))
        self.assertIn("does not compile", refused[0])

    def test_gap_already_at_base_does_not_hold_a_foreign_lot(self):
        self._git(self.canon, "checkout", "-q", "main")
        self._commit(self.canon, {"federation.yaml": _federation(
            SENSITIVE, OTHER, counted=[SENSITIVE])})
        self._git(self.canon, "checkout", "-qB", "canon/5216-lot")
        head = self._lot(_federation(SENSITIVE, OTHER, counted=[SENSITIVE]) + "# virgule\n")
        self.assertEqual(self._check(head), ([], []))

    def test_diff_outside_the_policy_reads_nothing(self):
        head = self._commit(self.canon, {"docs/note.md": "x\n"})
        with mock.patch.object(coverage, "_measure_project") as measure:
            self.assertEqual(self._check(head), ([], []))
        measure.assert_not_called()

    def test_missing_project_repository_is_not_measured_not_refused(self):
        head = self._lot(_federation(SENSITIVE, OTHER, counted=[SENSITIVE]))
        with mock.patch.object(canon_lowering, "_project_repository",
                               return_value=(None, "no repository")):
            refused, unmeasured = self._check(head)
        self.assertEqual(refused, [])
        self.assertIn("no repository", unmeasured[0])

    def test_expired_deadline_is_not_a_refusal(self):
        head = self._lot(_federation(SENSITIVE, OTHER, counted=[SENSITIVE]))
        refused, unmeasured = self._check(head, deadline=Deadline(0.0))
        self.assertEqual(refused, [])
        self.assertTrue(unmeasured)


class GateTest(unittest.TestCase):
    ITEM = {"branch": "canon/5216-lot", "branch_target": "main"}

    def _gate(self, item, result, repo="/canon"):
        db = types.SimpleNamespace(cfg=None)
        with mock.patch.object(coverage, "for_freeze", return_value=result) as called:
            return freeze_gate._canon_review_coverage(db, item, "a" * 40, repo), called

    def test_refusal_blocks_the_freeze_and_names_the_path(self):
        with self.assertRaisesRegex(WorkError, "canon-review-count.*deploy/hote.sh"):
            self._gate(self.ITEM, (["project ameesh: ... deploy/hote.sh"], []))

    def test_unmeasured_goes_to_the_freeze_note(self):
        note, _ = self._gate(self.ITEM, ([], ["ameesh (no repository)"]))
        self.assertIn("ameesh (no repository)", note)

    def test_ordinary_lot_is_not_measured(self):
        note, called = self._gate({"branch": "lot/1-x", "branch_target": "main"}, ([], []))
        self.assertEqual(note, "")
        called.assert_not_called()

    def test_head_not_read_on_this_host_is_not_measured(self):
        note, called = self._gate(self.ITEM, ([], []), repo="")
        self.assertEqual(note, "")
        called.assert_not_called()


class MilestoneTest(PgTestCase):
    """Le vrai gel : un refus n'écrit aucun jalon, le lot conforme gèle."""

    def setUp(self):
        super().setUp()
        self.helper = ForFreezeTest()
        self.helper.canon = os.path.join(self.tmp, "canon")
        self.helper.project = os.path.join(self.tmp, "project")
        self.helper._commit(self.helper.project, {SENSITIVE: "x\n", OTHER: "x\n"})
        base = self.helper._commit(self.helper.canon, {
            "federation.yaml": _federation(SENSITIVE, counted=[SENSITIVE]),
            "processus/mecanique.yaml": MECHANICS})
        self.helper._git(self.helper.canon, "update-ref", "refs/remotes/origin/main", base)
        self.helper._git(self.helper.canon, "checkout", "-qb", "canon/5216-lot")
        self.register("auteur", "codex", cwd=self.helper.canon)
        self.lot = work.add(self.db, title="lot du canon", app="ameesh",
                            assignee="auteur", actor="auteur",
                            branch="canon/5216-lot", branch_target="main")["id"]
        work.move(self.db, self.lot, "build", actor="auteur")
        patcher = mock.patch.object(canon_lowering, "_project_repository",
                                    return_value=((self.helper.project, "main"), ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _frozen(self):
        return [row for row in work.milestones(self.db, self.lot, None)
                if row.get("kind") == "frozen"]

    def test_refused_freeze_writes_no_milestone_and_names_the_path(self):
        head = self.helper._commit(self.helper.canon, {"federation.yaml": _federation(
            SENSITIVE, OTHER, counted=[SENSITIVE])})
        with self.assertRaisesRegex(WorkError, "canon-review-count.*" + OTHER):
            work.milestone(self.db, self.lot, "frozen", sha=head, actor="auteur",
                           repo=self.helper.canon)
        self.assertEqual(self._frozen(), [])

    def test_compliant_lot_freezes(self):
        head = self.helper._commit(self.helper.canon, {"federation.yaml": _federation(
            SENSITIVE, OTHER, counted=[SENSITIVE, OTHER])})
        work.milestone(self.db, self.lot, "frozen", sha=head, actor="auteur",
                       repo=self.helper.canon)
        self.assertEqual(len(self._frozen()), 1)


if __name__ == "__main__":
    unittest.main()
