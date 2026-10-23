# SPDX-License-Identifier: AGPL-3.0-only
"""#3250: a new freeze reads the current published target, including narrow clones."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from ameesh import canon, review_target, work
from ameesh import review_designation_activation as act

from . import support
from . import test_2447_review_designation_activation as fixtures


class TargetRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-3250-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = fixtures._Repo(os.path.join(self.tmp.name, "writer"))
        self.repo.write("base.txt", "base\n")
        self.base = self.repo.commit("base")
        self.repo.git("branch", "-M", "develop")
        self.origin = os.path.join(self.tmp.name, "origin.git")
        self.repo.git("init", "--bare", "-q", self.origin)
        self.repo.git("remote", "add", "origin", self.origin)
        self.repo.git("push", "-q", "origin", "develop:main", "develop")
        self.reader = fixtures._Repo(os.path.join(self.tmp.name, "reader"))
        self.reader.git("remote", "add", "origin", self.origin)
        # A clone restricted to main never updates develop on an ordinary fetch.
        self.reader.git("config", "remote.origin.fetch",
                        "+refs/heads/main:refs/remotes/origin/main")
        self.reader.git("fetch", "-q", "origin")
        self.reader.git("update-ref", "refs/remotes/origin/develop", self.base)

    def advance(self):
        self.repo.write("shared.txt", "already delivered\n")
        target = self.repo.commit("delivered by another lot")
        self.repo.git("push", "-q", "origin", "develop")
        self.repo.write("lot.txt", "own change\n")
        head = self.repo.commit("lot")
        self.repo.git("push", "-q", "origin", "HEAD:refs/heads/lot/test")
        self.reader.git("fetch", "-q", "origin", head)
        return target, head

    def test_target_advances_between_two_freezes_even_with_a_narrow_refspec(self):
        first = review_target.resolve(self.reader.root, "develop")
        self.assertEqual(first, self.base)
        target, head = self.advance()
        self.assertEqual(act._range(self.reader.root, "develop", head)[2],
                         [head, target])  # reproduction: head present, target stale
        second = review_target.resolve(self.reader.root, "develop")
        self.assertEqual(second, target)
        self.assertEqual(act._range(self.reader.root, second, head)[2], [head])
        self.assertEqual(self.reader.git("config", "remote.origin.fetch"),
                         "+refs/heads/main:refs/remotes/origin/main")

    def test_target_aliases_use_the_fully_qualified_tracking_ref(self):
        target, _head = self.advance()
        # Neither a local branch nor a same-named tag may mask the target.
        self.reader.git("update-ref", "refs/heads/origin/develop", self.base)
        self.reader.git("update-ref", "refs/tags/origin/develop", self.base)
        self.reader.git("update-ref", "refs/heads/develop", self.base)
        for ref in ("develop", "origin/develop", "refs/remotes/origin/develop",
                    "refs/heads/develop"):
            with self.subTest(ref=ref):
                self.assertEqual(review_target.resolve(self.reader.root, ref), target)

    def test_explicit_other_remote_and_branch_with_slash(self):
        target, _head = self.advance()
        self.repo.git("push", "-q", "origin", target + ":refs/heads/release/test")
        self.reader.git("remote", "add", "upstream", self.origin)
        self.assertEqual(review_target.resolve(self.reader.root, "upstream/release/test"),
                         target)

    def test_failed_fetch_does_not_read_a_stale_target(self):
        self.advance()
        self.reader.git("remote", "set-url", "origin", self.origin + "-missing")
        with self.assertRaisesRegex(review_target.TargetError, "cannot refresh target"):
            review_target.resolve(self.reader.root, "develop")

    def test_deleted_remote_branch_does_not_read_its_old_tracking_ref(self):
        self.repo.git("push", "-q", "origin", ":develop")
        with self.assertRaisesRegex(review_target.TargetError, "cannot refresh target"):
            review_target.resolve(self.reader.root, "develop")

    def test_transaction_never_fetches_or_accepts_an_uncertified_branch(self):
        with mock.patch.object(canon, "_git", wraps=canon._git) as git:
            with self.assertRaisesRegex(review_target.TargetError, "outside the freeze"):
                review_target.resolve(self.reader.root, "origin/develop", fetch=False)
        self.assertFalse(any("fetch" in call.args for call in git.call_args_list))
        self.assertEqual(review_target.resolve(self.reader.root, self.base, fetch=False),
                         self.base)


class FreezeTargetTest(fixtures._ActivationFixture, support.PgTestCase):
    def setUp(self):
        super().setUp()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo.git("init", "--bare", "-q", self.origin)
        self.repo.git("remote", "add", "origin", self.origin)
        # The local target stays behind, although the frozen head is present.
        self.repo.git("push", "-q", "origin", self.base + ":refs/heads/develop")
        self.repo.git("update-ref", "refs/remotes/origin/develop", self.base)
        self.db.execute("UPDATE work_items SET branch_target = 'develop' WHERE id = %s",
                        (self.lot,))

    def test_prepare_and_freeze_share_the_pinned_target_without_network_in_transaction(self):
        # One foreign commit advances the target; only the next commit is ours.
        delivered = self.frozen
        self.repo.git("push", "-q", "origin", delivered + ":refs/heads/develop")
        self.repo.write("src/ameesh/mail.py", "own delta\n")
        self.frozen = self.repo.commit("own delta", agent="author")
        self.receipts()
        item = work.get(self.db, self.lot)
        target = act.prepare_target(self.cfg, self.db, item, self.frozen)
        self.assertIsInstance(target, act.Target)
        self.assertEqual(target.sha, delivered)
        # A concurrent ref update after certification cannot change this reading.
        self.repo.git("update-ref", "refs/remotes/origin/develop", self.base)
        outcome = {}
        with mock.patch.object(review_target, "resolve", side_effect=AssertionError("refetch")):
            row, _ = self.freeze(hook=act.hook(self.cfg, outcome, target=target))
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(outcome["state"], "designated", outcome)
        self.assertEqual(outcome["unknown_authors"], [], outcome)
        # Recovery refreshes the same target through the same path.
        inputs = act.prepare(self.cfg, self.db, self.lot)
        self.assertIsInstance(inputs, act.Inputs)
        self.assertEqual(inputs.base, delivered)
        self.assertEqual(inputs.commits, [self.frozen])

    def test_automatic_milestone_hook_certifies_outside_the_transaction(self):
        self.receipts()
        db = self.connect(canon=self.canon.root, canon_ref="HEAD")
        self.addCleanup(db.close)
        original = review_target.resolve
        def resolve(repo, base, **kwargs):
            self.assertFalse(act.db_mod.in_transaction(db))
            return original(repo, base, **kwargs)
        outcome = {}
        with mock.patch.object(review_target, "resolve", side_effect=resolve) as read:
            row = work.milestone(db, self.lot, "frozen", sha=self.frozen,
                                 actor="author", outcome=outcome)
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(outcome["state"], "designated", outcome)
        read.assert_called_once()

    def test_failed_refresh_is_unavailable_and_does_not_lose_the_freeze(self):
        self.receipts()
        self.repo.git("remote", "set-url", "origin", self.origin + "-missing")
        db = self.connect(canon=self.canon.root, canon_ref="HEAD")
        self.addCleanup(db.close)
        outcome = {}
        row = work.milestone(db, self.lot, "frozen", sha=self.frozen,
                             actor="author", outcome=outcome)
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(outcome["state"], "unavailable", outcome)
        self.assertIn("cannot refresh target", outcome["why"])
        self.assertEqual(self.marks(), [])

    def test_branch_without_preflight_defers_instead_of_reading_a_stale_range(self):
        self.receipts()
        row, outcome = self.freeze()
        self.assertEqual(row["kind"], "frozen")
        self.assertEqual(outcome["state"], "unavailable", outcome)
        self.assertIn("requires refresh outside", outcome["why"])
