# SPDX-License-Identifier: AGPL-3.0-only
"""#7436 : lire les têtes de PR et confirmer leur état avant l'alerte d'âge."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
import unittest
from unittest import mock

from ameesh import forge_packets
from . import test_6088_forge_packets as fixtures

git = fixtures.git


class PacketStateTest(unittest.TestCase):
    setUp = fixtures.ForgePacketTest.setUp
    gh = fixtures.ForgePacketTest.gh
    pr = fixtures.ForgePacketTest.pr
    frozen_ms = fixtures.ForgePacketTest.frozen_ms

    def old_pr(self):
        return dict(self.pr(), createdAt="2026-11-07T03:00:00Z",
                    statusCheckRollup=[{"status": "IN_PROGRESS"}])

    def lookup(self, gh, *, cache=None):
        return forge_packets.vehicle(
            None, {"id": 42}, self.frozen_ms(self.frozen), gh=gh,
            cache={} if cache is None else cache, now=1794030000, target_s=3600)

    def fresh_gh(self, **changes):
        calls = []
        fresh = dict(self.old_pr(), state="OPEN")
        fresh.update(changes)

        def gh(args):
            calls.append(args)
            value = [self.old_pr()] if args[1] == "list" else fresh
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")
        gh.calls = calls
        return gh

    def test_old_running_pr_is_refreshed_once_per_pass(self):
        gh, cache = self.fresh_gh(), {}
        for _ in range(2):
            found = self.lookup(gh, cache=cache)
            self.assertEqual(found["ci"], "running")
            self.assertNotIn("merged", found)
        self.assertEqual([args[1] for args in gh.calls], ["list", "view"])
        self.lookup(gh)  # passe suivante : aucune réutilisation de l'état frais
        self.assertEqual([args[1] for args in gh.calls], ["list", "view", "list", "view"])

    def test_cached_open_pr_now_merged_is_identified(self):
        self.assertTrue(self.lookup(self.fresh_gh(state="MERGED"))["merged"])

    def test_closed_without_merge_no_longer_carries(self):
        self.assertEqual(self.lookup(self.fresh_gh(state="CLOSED")), {})

    def test_merged_pr_with_changed_head_must_still_contain_frozen_lot(self):
        self.assertEqual(self.lookup(self.fresh_gh(
            state="MERGED", headRefOid=self.other)), {})

    def test_fresh_head_must_still_contain_frozen_lot(self):
        self.assertEqual(self.lookup(self.fresh_gh(headRefOid=self.other)), {})

    def test_unreadable_fresh_state_is_reported(self):
        def gh(args):
            if args[1] == "list":
                return self.gh([self.old_pr()])(args)
            return SimpleNamespace(returncode=1, stdout="", stderr="offline")
        self.assertIn("fresh state unreadable", self.lookup(gh)["unreadable"])

    def test_red_ci_does_not_need_an_age_refresh(self):
        gh = self.gh([dict(self.old_pr(), statusCheckRollup=[{"conclusion": "FAILURE"}])])
        self.assertEqual(self.lookup(gh)["ci"], "red")
        self.assertEqual(len(gh.calls), 1)

    def test_missing_head_is_fetched_from_pull_ref_before_reading_trailers(self):
        stale = os.path.join(self.repo, "stale")
        git(self.repo, "update-ref", "refs/pull/9/head", self.head)
        git(self.repo, "clone", "--single-branch", "--branch", "develop",
            "--no-local", self.repo, stale)
        absent = git(stale, "rev-list", "--all")
        self.assertNotIn(self.head, absent)
        with mock.patch("ameesh.lot_class.repository", return_value=stale), \
                mock.patch("ameesh.fire._same_repository", return_value=True), \
                mock.patch("ameesh.fire.review_checkout", return_value=None):
            found = forge_packets.vehicle(
                None, {"id": 42}, self.frozen_ms(self.frozen),
                gh=self.gh([self.pr()]), cache={})
        self.assertEqual(found["head"], self.head)
        self.assertEqual(git(stale, "cat-file", "-t", self.head), "commit")
        self.assertFalse(os.path.exists(os.path.join(stale, ".git", "FETCH_HEAD")))
        self.assertEqual(git(stale, "branch", "--show-current"), "develop")


if __name__ == "__main__":
    unittest.main()
