# SPDX-License-Identifier: AGPL-3.0-only
"""An obsolete unreceived freeze must not poison every later freeze (#3301)."""
import subprocess
from unittest import mock

from ameesh import merge_queue, review_authors, review_provenance, work

from .test_2924_history_rewrite_provenance import _RewriteFixture


class PriorUnknownTest(_RewriteFixture):
    def _build_head_a(self):
        self.repo.write("src/ameesh/runner.py", "base_call()\n")
        self.base = self.repo.commit("base")
        self.repo.write("src/ameesh/runner.py",
                        "base_call()\noriginal_guard()\noriginal_action()\n")
        self.original = self.repo.commit("impl: original", agent="impl")
        self.receipt("impl", "deepseek", [self.original])
        # A cherry-pick preserves the author but has an unrecorded committer.
        tree = self.repo.git("rev-parse", self.original + "^{tree}")
        env = dict(self.env("impl"), GIT_COMMITTER_NAME="operator",
                   GIT_COMMITTER_EMAIL="operator@example.invalid")
        self.unreceived = self.repo.git("commit-tree", tree, "-p", self.base,
                                        "-m", "operator: cherry-pick", env=env)
        self.head_a = self.unreceived
        self.db.execute("UPDATE work_items SET branch_target = %s WHERE id = %s",
                        (self.base, self.lot))
        self.freeze(self.head_a)

    def replace(self, text=None, *, actor="impl", git_author=None, receipt=True):
        self.repo.git("reset", "--hard", self.base)
        self.repo.write("src/ameesh/runner.py", text or
                        "base_call()\noriginal_guard()\noriginal_action()\n")
        if git_author:
            self.repo.git("add", "-A")
            env = dict(self.env(actor),
                       GIT_AUTHOR_EMAIL=git_author + "@ameesh.invalid")
            self.repo.git("commit", "-qm", "received replacement", env=env)
            self.head_b = self.repo.git("rev-parse", "HEAD")
        else:
            self.head_b = self.repo.commit("received replacement", agent=actor)
        if receipt:
            self.receipt(actor, {"impl": "deepseek", "dev1": "claude"}[actor],
                         [self.head_b])
        self.freeze_b = self.freeze(self.head_b)

    def read(self, *, strict=False):
        commits = review_authors._commits_between(self.repo.root, self.base,
                                                  self.head_b)
        return review_authors.reading(
            self.db, self.lot_item(), self.freeze_b, commits, self.repo.root,
            ("claude", "codex", "deepseek"), base=self.base, head=self.head_b,
            strict=strict)

    def gate(self):
        klass = {"class": "sensitive", "doubt": False, "files": [],
                 "repo": self.repo.root, "target": self.base, "base_sha": self.base}
        for actor in ("cx3", "dev1"):
            self.verdict(actor, self.head_b)
        return merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), self.head_b,
            repo=self.repo.root, klass=klass, item=self.lot_item(), required=2)

    def test_same_author_received_recommit_clears_old_unknown(self):
        self.replace()
        authors, unknown, _ = self.read(strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual({r["name"]: r["family"] for r in authors},
                         {"impl": "deepseek"})

    def test_gate_and_designation_accept_the_received_replacement(self):
        from ameesh import review_designation_activation as act
        self.replace()
        authors, unknown, _ = act.attribution_of_head(
            self.db, self.lot_item(), self.freeze_b, self.repo.root,
            self.base, self.head_b, families=("claude", "codex", "deepseek"))
        self.assertEqual(unknown, [])
        self.assertEqual({r["name"] for r in authors}, {"impl"})
        result = self.gate()
        self.assertFalse(result.get("error"), result)
        self.assertTrue(result["ok"], result)

    def test_wholly_rewritten_content_does_not_carry_an_unknown(self):
        self.replace("base_call()\nnew_admission_model()\n")
        self.assertEqual(self.read(strict=True)[1], [])

    def test_another_author_cannot_launder_the_same_patch(self):
        self.replace(actor="dev1")
        self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_receipt_of_the_committer_does_not_prove_the_git_author(self):
        self.replace(actor="dev1", git_author="impl")
        self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_only_partial_patch_equivalence_keeps_the_unknown(self):
        self.replace("base_call()\noriginal_guard()\n    original_action()\n")
        self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_an_unreceived_replacement_does_not_excuse_the_unknown(self):
        self.replace(receipt=False)
        self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_an_unreadable_patch_does_not_excuse_the_unknown(self):
        self.replace()
        with mock.patch.object(review_authors, "_read_diff_patch_id",
                               return_value=None):
            self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_failed_local_diff_does_not_excuse_the_unknown(self):
        self.replace()
        original = review_authors._read_git
        failed_reads = []

        def unreadable(repo, *args, **kwargs):
            if (args[0] == "diff" and
                    self.unreceived.startswith(args[-1]) and
                    args[-2] == args[-1] + "^"):
                failed_reads.append(args)
                return subprocess.CompletedProcess(args, 1, b"", b"unreadable object")
            return original(repo, *args, **kwargs)

        with mock.patch.object(review_authors, "_read_git", side_effect=unreadable):
            self.assertIn(self.unreceived[:12], self.read(strict=True)[1])
        self.assertTrue(failed_reads, "the unreadable diff must actually be attempted")

    def test_current_unknown_is_never_excused_by_a_historical_receipt(self):
        self.head_b = self.unreceived
        self.freeze_b = self.freeze(self.head_b)
        self.assertIn(self.unreceived[:12], self.read(strict=True)[1])

    def test_known_historical_corrector_is_still_carried(self):
        self.repo.git("reset", "--hard", self.unreceived)
        self.repo.write("tests/test_original.py", "assert original_guard_is_safe()\n")
        correction = self.repo.commit("cx3: correction", agent="cx3")
        self.receipt("cx3", "codex", [correction])
        self.freeze(correction)
        self.replace()
        self.repo.write("tests/test_original.py", "assert original_guard_is_safe()\n")
        self.head_b = self.repo.commit("impl: consolidate correction", agent="impl")
        self.receipt("impl", "deepseek", [self.head_b])
        self.freeze_b = self.freeze(self.head_b)
        authors, unknown, correctors = self.read(strict=True)
        self.assertEqual(unknown, [])
        self.assertEqual({r["name"]: r["family"] for r in authors},
                         {"impl": "deepseek", "cx3": "codex"})
        self.assertIn("cx3", {r["name"] for r in correctors})

    def test_contradictory_historical_receipt_is_still_refused(self):
        self.replace()
        original = review_provenance.receipt_for_commit

        def contradictory(db, sha):
            if sha == self.unreceived:
                raise review_provenance.ProvenanceError("contradictory receipt")
            return original(db, sha)

        with mock.patch.object(review_provenance, "receipt_for_commit",
                               side_effect=contradictory):
            with self.assertRaises(review_provenance.ProvenanceError):
                self.read(strict=True)
            self.assertIn(self.unreceived[:12], self.read()[1])
