# SPDX-License-Identifier: AGPL-3.0-only
"""#4804 : la pré-lecture fire ne peut épuiser un budget puis autoriser."""
import unittest
from unittest import mock

from ameesh import fire, merge_queue, review_authors as authors
from ameesh.deadline import Deadline


class FireAuthorsBudgetTest(unittest.TestCase):
    def ready(self):
        return fire.ready_verdicts(None, None, {"id": 4804},
                                   [{"kind": "frozen", "sha": "a" * 40}],
                                   repo=None, project=None, target="main")

    def scope(self):
        return mock.patch.object(fire.lot_class, "of_lot",
                                 return_value={"class": "sensitive"})

    def test_pre_read_deadline_is_a_named_refusal(self):
        def read(*args, **kwargs):
            sweep = authors.active_sweep()
            self.assertIsNotNone(sweep)
            self.assertEqual(sweep.budget, merge_queue.AUTHORS_SWEEP_BUDGET_S)
            sweep.deadline = Deadline(0)
            sweep.check("pre-read")

        with self.scope(), mock.patch.object(merge_queue, "_complete_authors", side_effect=read), \
                mock.patch.object(merge_queue, "review_gate") as gate:
            ok, why, klass = self.ready()
        self.assertFalse(ok)
        self.assertIn("lot #4804", why)
        self.assertIn("lecture d'auteurs non concluante", why)
        self.assertIn("pre-read", why)
        self.assertEqual(klass["class"], "sensitive")
        gate.assert_not_called()
        self.assertIsNone(authors.active_sweep())

    def test_swallowed_deadline_never_turns_into_permission(self):
        def read(*args, **kwargs):
            try:
                authors.active_sweep().trip("swallowed")
            except authors.SweepDeadline:
                pass
            return [], []

        with self.scope(), mock.patch.object(merge_queue, "_complete_authors", side_effect=read), \
                mock.patch.object(merge_queue, "review_gate", return_value={"ok": True}):
            ok, why, _ = self.ready()
        self.assertFalse(ok)
        self.assertIn("swallowed", why)
        self.assertIn("lecture d'auteurs non concluante", why)

    def test_pre_read_and_gate_reuse_outer_budget(self):
        with authors.sweep_deadline(10, "outer") as outer:
            def read(*args, **kwargs):
                self.assertIs(authors.active_sweep(), outer)
                return [], []

            def gate(*args, **kwargs):
                self.assertIs(authors.active_sweep(), outer)
                return {"ok": True}

            with self.scope(), mock.patch.object(merge_queue, "_complete_authors", side_effect=read), \
                    mock.patch.object(merge_queue, "review_gate", side_effect=gate):
                self.assertTrue(self.ready()[0])
            self.assertIs(authors.active_sweep(), outer)

    def test_elapsed_budget_without_exception_still_refuses(self):
        def read(*args, **kwargs):
            authors.active_sweep().deadline = Deadline(0)
            return [], []

        with self.scope(), mock.patch.object(merge_queue, "_complete_authors", side_effect=read), \
                mock.patch.object(merge_queue, "review_gate", return_value={"ok": True}):
            ok, why, _ = self.ready()
        self.assertFalse(ok)
        self.assertIn("lecture d'auteurs non concluante", why)


class FireSymbolicBaseTest(unittest.TestCase):
    def test_freeze_preservation_resolves_origin_main_before_isolated_merge(self):
        from .test_2442_verdict_carry import Repo
        repo = Repo()
        self.addCleanup(repo.cleanup)
        head = repo.merge_target()
        import subprocess
        subprocess.run(['git', 'update-ref', 'refs/remotes/origin/main', repo.t1],
                       cwd=repo.root, check=True)
        self.assertTrue(authors._freeze_content_preserved(
            repo.root, repo.reviewed, head, base='origin/main'))
