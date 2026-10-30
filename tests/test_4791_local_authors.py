# SPDX-License-Identifier: AGPL-3.0-only
"""#4791: absent objects never start per-commit reads or remove authors."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ameesh import canon, merge_queue, review_authors as authors


class LocalAuthorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.git("init", "--quiet")
        self.git("config", "user.name", "fixture")
        self.git("config", "user.email", "fixture@example.test")
        with open(os.path.join(self.repo, "content"), "w") as stream:
            stream.write("original content\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "base")
        self.head = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo,
                                       stderr=subprocess.DEVNULL).decode().strip()

    def test_many_absent_commits_use_one_remote_free_batch(self):
        missing = ["%040x" % n for n in range(1, 101)]
        # A real promisor configuration: batch reads must occur outside it,
        # including on Git versions that ignore GIT_NO_LAZY_FETCH.
        self.git("config", "remote.origin.url", "https://invalid.example/repo")
        self.git("config", "remote.origin.promisor", "true")
        cache = {}
        original = authors.plan_git._git
        with mock.patch.object(authors.plan_git, "_git", wraps=original) as calls:
            self.assertFalse(authors._local_reads(self.repo, [self.head, *missing], cache))
            count = calls.call_count
            for sha in missing:
                self.assertTrue(authors._survives_rewrite(
                    self.repo, self.head, sha, cache, target=self.head))
            self.assertEqual(calls.call_count, count)
        batches = [call for call in calls.call_args_list if "cat-file" in call.args]
        self.assertEqual(len(batches), 1)
        self.assertNotEqual(batches[0].args[0], self.repo)
        self.assertIn("GIT_OBJECT_DIRECTORY", batches[0].kwargs["env_extra"])
        self.assertFalse(any("merge-base" in call.args for call in calls.call_args_list))

    def test_prior_unknown_authors_are_kept_without_per_commit_git(self):
        missing = ["%040x" % n for n in range(1, 51)]
        original = authors.plan_git._git
        with mock.patch.object(authors, "_freeze_reference", return_value={}), \
                mock.patch.object(authors, "designated_reviewers", return_value=set()), \
                mock.patch.object(authors, "_commit_receipt", return_value=None), \
                mock.patch.object(authors, "_unreceived_authors") as fallback, \
                mock.patch.object(authors.plan_git, "_git", wraps=original) as git:
            _names, _written, _corrections, unknown = authors._prior_reading(
                None, {}, {}, missing, self.repo, (), head=self.head,
                base=self.head, target=self.head)
        self.assertEqual(unknown, [sha[:12] for sha in missing])
        fallback.assert_not_called()
        self.assertEqual(sum("cat-file" in call.args for call in git.call_args_list), 1)
        self.assertFalse(any("merge-base" in call.args or "show" in call.args
                             for call in git.call_args_list))

    def test_presence_failure_is_cached_and_keeps_author(self):
        cache = {}
        with mock.patch.object(authors, "_present_commits", return_value=None) as probe:
            for _ in range(3):
                self.assertTrue(authors._survives_rewrite(
                    self.repo, self.head, "a" * 40, cache, target=self.head))
            probe.assert_called_once()

    def test_missing_candidate_stays_outside_target_without_history_reads(self):
        missing = ["%040x" % n for n in range(1, 51)]
        with mock.patch.object(authors, "_read_patch_ids") as patch_ids:
            self.assertEqual(authors._not_in_target(
                self.repo, missing, self.head, head=self.head), missing)
            patch_ids.assert_not_called()

    def test_local_ancestor_semantics_and_cache(self):
        cache = {}
        self.assertTrue(authors._is_ancestor(self.repo, self.head, self.head, cache))
        with mock.patch.object(authors.plan_git, "_git") as git:
            self.assertTrue(authors._is_ancestor(self.repo, self.head, self.head, cache))
            git.assert_not_called()
        self.assertIsNone(authors._is_ancestor(self.repo, "f" * 40, self.head))

    def test_parent_expression_is_checked_without_remote(self):
        present = authors._present_commits(self.repo, [self.head, self.head + "^"])
        self.assertEqual(present, {self.head: self.head})

    def test_git_read_has_short_bound_and_remaining_global_budget(self):
        proc = subprocess.CompletedProcess([], 0, b"", b"")
        with authors.sweep_deadline(0.5):
            with mock.patch.object(authors.plan_git, "_git", return_value=proc) as git:
                authors._read_git(self.repo, "rev-parse", self.head)
                self.assertLessEqual(git.call_args.kwargs["timeout"],
                                     authors.plan_git.PROBE_TIMEOUT_S)
                self.assertLessEqual(git.call_args.kwargs["deadline"].remaining(), 0.5)

    def test_heavy_patch_history_over_two_seconds_uses_sweep_budget(self):
        with open(os.path.join(self.repo, "content"), "a") as stream:
            stream.write("new distinctive line\n")
        self.git("commit", "-am", "change", "--quiet")
        changed = self.git("rev-parse", "HEAD")
        tree = self.git("rev-parse", changed + "^{tree}")
        target = self.git("commit-tree", tree, "-p", self.head, "-m", "copy")
        original = authors.plan_git._git
        def slow_log(repo, *args, **kwargs):
            if args[0] == "log":
                self.assertGreater(kwargs["timeout"], 2)
                self.assertNotEqual(repo, self.repo)
                self.assertIn("GIT_OBJECT_DIRECTORY", kwargs["env_extra"])
                time.sleep(2.1)
            return original(repo, *args, **kwargs)
        with authors.sweep_deadline(15), mock.patch.object(
                authors.plan_git, "_git", side_effect=slow_log):
            self.assertTrue(authors._patch_in_target(
                self.repo, target, changed, {}))

    def test_missing_blob_fails_in_reader_without_promisor(self):
        blob = self.git("rev-parse", self.head + ":content")
        self.git("config", "remote.origin.url", "https://invalid.example/repo")
        self.git("config", "remote.origin.promisor", "true")
        os.unlink(os.path.join(self.repo, ".git", "objects", blob[:2], blob[2:]))
        original = authors.plan_git._git
        with authors.sweep_deadline(15), mock.patch.object(
                authors.plan_git, "_git", wraps=original) as calls:
            proc = authors._read_git(self.repo, "show", self.head + ":content")
        self.assertNotEqual(proc.returncode, 0)
        show = next(call for call in calls.call_args_list if "show" in call.args)
        self.assertNotEqual(show.args[0], self.repo)
        self.assertIn("GIT_OBJECT_DIRECTORY", show.kwargs["env_extra"])
        self.assertNotIn(b"https://invalid.example", proc.stderr)

    def test_unreadable_target_index_marks_sweep_nonconclusive(self):
        calls = [{"patch": "commit"}, canon.GitError("missing blob")]
        with authors.sweep_deadline(15) as sweep, mock.patch.object(
                authors, "_read_patch_ids", side_effect=calls), mock.patch.object(
                authors, "_local_reads", return_value=True):
            self.assertFalse(authors._patch_in_target(
                self.repo, self.head, self.head, {}))
            self.assertIn("target patch index unreadable", sweep.tripped)

    def test_ancestor_timeout_is_doubt_and_cached(self):
        cache = {}
        with mock.patch.object(authors, "_local_reads", return_value=True), \
                mock.patch.object(authors, "_read_git", side_effect=canon.GitError("timeout")) as git:
            self.assertIsNone(authors._is_ancestor(self.repo, self.head, self.head, cache))
            self.assertIsNone(authors._is_ancestor(self.repo, self.head, self.head, cache))
            git.assert_called_once()

    def test_gate_rejects_even_when_inner_layer_swallows_deadline(self):
        def attribution(*args, **kwargs):
            sweep = authors.active_sweep()
            try:
                sweep.trip("simulated missing object")
            except authors.SweepDeadline:
                return [], [], []
        with mock.patch("ameesh.review_designation_activation.attribution_of_head",
                        side_effect=attribution), \
                mock.patch.object(merge_queue, "_freeze_milestone", return_value={}):
            with self.assertRaisesRegex(ValueError, "lecture d'auteurs non concluante"):
                merge_queue._attribution_on_freeze(
                    object(), [], self.head, repo=self.repo,
                    klass={"base_sha": self.head}, item={"id": 4791})

    def test_short_probe_failures_are_doubt_with_budget_remaining(self):
        for command in ("merge-base", "rev-parse", "rev-list", "ls-tree"):
            for error in (canon.GitDeadline, authors.plan_git.MergeProbeError):
                with self.subTest(command=command, error=error.__name__), \
                        authors.sweep_deadline(15) as sweep, mock.patch.object(
                            authors.plan_git, "_out", side_effect=error("probe failed")):
                    self.assertIsNone(authors._read_out(self.repo, command, "HEAD"))
                    self.assertFalse(sweep.tripped)

    def test_real_probe_deadline_keeps_freeze_authors(self):
        with authors.sweep_deadline(15) as sweep, mock.patch.object(
                authors.plan_git, "PROBE_TIMEOUT_S", 1e-9):
            self.assertIsNone(authors._read_out(self.repo, "merge-base", self.head, self.head))
            self.assertTrue(authors._freeze_content_preserved(
                self.repo, self.head, self.head, base=self.head))
            self.assertFalse(sweep.tripped)

    def test_read_out_preserves_exhausted_sweep(self):
        with authors.sweep_deadline(0) as sweep:
            with self.assertRaises(authors.SweepDeadline):
                authors._read_out(self.repo, "rev-parse", "HEAD")
            self.assertTrue(sweep.tripped)

    def test_merge_tree_probe_failure_keeps_freeze_authors(self):
        for error in (canon.GitDeadline, authors.plan_git.MergeProbeError):
            with self.subTest(error=error.__name__), authors.sweep_deadline(15), \
                    mock.patch.object(authors, "_tree_of", side_effect=["head", "freeze"]), \
                    mock.patch.object(authors, "_merged_tree", side_effect=error("probe failed")):
                self.assertTrue(authors._freeze_content_preserved(
                    self.repo, self.head, self.head, base=self.head))

    def test_merge_tree_reads_without_remote(self):
        original = authors.plan_git._git
        with mock.patch.object(authors.plan_git, "_git", wraps=original) as calls:
            self.assertEqual(authors._merged_tree(self.repo, self.head, self.head),
                             self.git("rev-parse", "HEAD^{tree}"))
        call = next(call for call in calls.call_args_list if "merge-tree" in call.args)
        self.assertNotEqual(call.args[0], self.repo)
        self.assertIn("GIT_OBJECT_DIRECTORY", call.kwargs["env_extra"])

    def test_all_refs_are_visible_in_remote_free_history_reader(self):
        self.git('branch', 'retained-original', self.head)
        self.git('checkout', '--quiet', '--detach', self.head)
        with open(os.path.join(self.repo, 'content'), 'a') as stream:
            stream.write('detached content\n')
        self.git('commit', '--quiet', '-am', 'detached')
        detached = self.git('rev-parse', 'HEAD')
        expected = self.git('log', '--all', '--format=%H')
        self.git('config', 'remote.origin.url', 'https://invalid.example/repo')
        self.git('config', 'remote.origin.promisor', 'true')
        observed = authors._read_out(self.repo, 'log', '--all', '--format=%H')
        self.assertEqual(set(observed.splitlines()), set(expected.splitlines()))
        self.assertIn(detached, observed)
        self.assertIn(self.head, observed)
