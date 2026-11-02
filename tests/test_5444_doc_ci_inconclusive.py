# SPDX-License-Identifier: AGPL-3.0-only
"""#5444: doc-only packets reject proved negatives, wrap unreadable Git."""
import types
import unittest
from unittest import mock

from ameesh import ci_parts, merge_queue, radar
from . import test_l160g_nexlink as fixture
from ameesh.canon import GitError


class DocOnlyEvidenceTest(unittest.TestCase):
    def packet(self, runs, *, git_error=None):
        reader = mock.Mock(return_value=())
        reader.proof = {"merge_base": "a" * 40, "due": ()}
        reader.holder = "repo"
        changes = [("100644", "100644", "docs/a.md")]
        raw = (mock.patch.object(ci_parts, "_git", side_effect=git_error) if git_error
               else mock.patch.object(ci_parts, "raw_changes", return_value=changes))
        with mock.patch.object(merge_queue, "_parts_reader", return_value=reader), \
                mock.patch.object(merge_queue, "_no_declared_sensitive_path", return_value=True), \
                raw:
            return merge_queue.doc_only_packet(
                types.SimpleNamespace(ci_parts=True),
                {"branch": "integration/paquet-5444"}, "repo", "main", "b" * 40,
                None, klass={}, commit_ci=lambda sha: runs)

    def test_inconclusive_checks_do_not_reject_a_proved_docs_packet(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure", "stale",
                           "skipped", "neutral", "pending"):
            with self.subTest(conclusion=conclusion):
                run = (fixture._run("docs", status="in_progress", conclusion=None)
                       if conclusion == "pending" else fixture._run("docs", conclusion=conclusion))
                runs = radar.commit_ci(radar.Project("p", "/r", github="o/r"), "a" * 40,
                                       gh=fixture._Gh([run]))
                self.assertEqual(self.packet(runs)["due"], ())

    def test_a_proved_negative_still_rejects(self):
        for field in ("negative", "required_negative"):
            with self.subTest(field=field), self.assertRaisesRegex(
                    merge_queue.QueueError, "is red.*docs"):
                self.packet({field: ["docs"]})

    def test_no_checks_does_not_require_ci(self):
        self.assertEqual(self.packet({})["due"], ())

    def test_raw_git_failure_is_wrapped_at_the_source(self):
        error = GitError("git diff timed out")
        with mock.patch.object(ci_parts, "_git", side_effect=error), \
                self.assertRaisesRegex(ci_parts.PartsError, "change set.*timed out") as caught:
            ci_parts.raw_changes("repo", "a" * 40, "b" * 40)
        self.assertIs(caught.exception.__cause__, error)

    def test_doc_only_packet_translates_the_git_failure_to_queue_error(self):
        with self.assertRaisesRegex(merge_queue.QueueError, "change set.*timed out") as caught:
            self.packet({}, git_error=GitError("git diff timed out"))
        self.assertIsInstance(caught.exception.__cause__, ci_parts.PartsError)
