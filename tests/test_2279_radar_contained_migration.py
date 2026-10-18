# SPDX-License-Identifier: AGPL-3.0-only
"""#2279: a package inherits its constituent's identical migration."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import migration_reservations, radar, storage

from .test_l160b_radar import _git, _write

MIG = "supabase/migrations"
PATH = MIG + "/522_guard.sql"
CONTENT = "-- guard\nSELECT 1;\n"
LOT = {"id": 2106, "branch": "lot/2106"}
PACKAGE = {"id": 2277, "branch": "integration/paquet-57"}


class ContainedMigrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-2279-")
        self.addCleanup(temporary.cleanup)
        self.repo = temporary.name
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("README.md", "Base\n")
        _git(self.repo, "checkout", "-qb", LOT["branch"])
        self.commit(PATH, CONTENT)
        _git(self.repo, "checkout", "-qb", PACKAGE["branch"])
        self.commit("docs/package.md", "Package\n")
        self.project = radar.Project("product", self.repo, migrations=MIG,
                                     github="acme/product")
        self.registry = mock.Mock()
        self.registry.migration_holder.side_effect = lambda number, slug: (
            {"id": LOT["id"]} if number == 522 else None)
        self.registry.migration_landed_holder.return_value = None
        patcher = mock.patch.object(storage, "of", return_value=mock.Mock(work=self.registry))
        patcher.start()
        self.addCleanup(patcher.stop)

    def commit(self, path, text):
        _write(self.repo, path, text)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", path)

    def scan(self, lots=None):
        return radar.scan(self.repo, lots if lots is not None else [LOT, PACKAGE],
                          default_target="main", migrations=MIG,
                          reservations=lambda branches: migration_reservations.probe(
                              None, self.project, branches))

    def test_package_and_constituent_do_not_duplicate_or_lose_reservation(self):
        before = _git(self.repo, "count-objects", "-v")
        refs = _git(self.repo, "show-ref")
        for lots in ([LOT, PACKAGE], [PACKAGE, LOT]):
            with self.subTest(order=[r["id"] for r in lots]):
                report = self.scan(lots)
                self.assertEqual(report["migrations"], [])
                self.assertEqual(report["unreserved_migrations"], [])
                self.assertEqual(len(report["branches"]), 2)
                self.assertTrue(all("added" not in b for b in report["branches"]))
        self.assertEqual(_git(self.repo, "count-objects", "-v"), before)
        self.assertEqual(_git(self.repo, "show-ref"), refs)
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")

    def test_missing_constituent_reservation_is_still_reported_once(self):
        self.registry.migration_holder.side_effect = None
        self.registry.migration_holder.return_value = None
        report = self.scan()
        self.assertEqual(report["migrations"], [])
        self.assertEqual([(m["work_item"], m["path"])
                          for m in report["unreserved_migrations"]], [(2106, PATH)])

    def test_package_own_migration_is_still_checked(self):
        own = MIG + "/523_package.sql"
        self.commit(own, "-- package-owned\n")
        report = self.scan()
        self.assertEqual(report["migrations"], [])
        self.assertEqual([(m["work_item"], m["path"])
                          for m in report["unreserved_migrations"]], [(2277, own)])

    def test_modified_migration_keeps_both_holders_visible(self):
        self.commit(PATH, "-- changed by package\nSELECT 2;\n")
        report = self.scan()
        self.assertEqual([m["number"] for m in report["migrations"]], ["522"])
        self.assertEqual([h["work_item"] for h in report["migrations"][0]["lots"]],
                         [2106, 2277])
        self.assertEqual([m["work_item"] for m in report["unreserved_migrations"]], [2277])

    def test_independent_identical_additions_remain_a_collision(self):
        other = {"id": 2200, "branch": "lot/independent"}
        _git(self.repo, "checkout", "-qb", other["branch"], "main")
        self.commit(PATH, CONTENT)
        report = self.scan([LOT, other])
        self.assertEqual([m["number"] for m in report["migrations"]], ["522"])
        self.assertEqual([h["work_item"] for h in report["migrations"][0]["lots"]],
                         [2106, 2200])

    def test_reused_number_under_another_path_is_still_a_collision(self):
        own = MIG + "/522_other.sql"
        self.commit(own, CONTENT)
        report = self.scan()
        self.assertEqual([m["number"] for m in report["migrations"]], ["522"])
        self.assertEqual([h["path"] for h in report["migrations"][0]["lots"]], [PATH, own])
        self.assertEqual([m["path"] for m in report["unreserved_migrations"]], [own])

    def test_nested_packages_keep_the_original_holder(self):
        outer = {"id": 2300, "branch": "integration/next-package"}
        _git(self.repo, "checkout", "-qb", outer["branch"])
        self.commit("docs/outer.md", "Outer package\n")
        report = self.scan([outer, PACKAGE, LOT])
        self.assertEqual(report["migrations"], [])
        self.assertEqual(report["unreserved_migrations"], [])

    def test_different_targets_do_not_share_ownership(self):
        _git(self.repo, "branch", "other", "main")
        report = self.scan([LOT, dict(PACKAGE, branch_target="other")])
        self.assertEqual([m["work_item"] for m in report["unreserved_migrations"]], [2277])

    def test_equal_tips_do_not_drop_both_holders(self):
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-f", PACKAGE["branch"], LOT["branch"])
        report = self.scan()
        self.assertEqual([m["number"] for m in report["migrations"]], ["522"])
        self.assertEqual([m["work_item"] for m in report["unreserved_migrations"]], [2277])

    def test_unreadable_ancestry_keeps_the_warning(self):
        git = radar._git

        def unreadable(repo, *args, **kwargs):
            if args[:2] == ("merge-base", "--is-ancestor"):
                return subprocess.CompletedProcess(args, 128, b"", b"unreadable history")
            return git(repo, *args, **kwargs)

        with mock.patch.object(radar, "_git", side_effect=unreadable):
            report = self.scan()
        self.assertEqual([m["number"] for m in report["migrations"]], ["522"])
        self.assertEqual([m["work_item"] for m in report["unreserved_migrations"]], [2277])

    def test_unreadable_blob_keeps_the_warning(self):
        git = radar._git

        def unreadable(repo, *args, **kwargs):
            if args[:2] == ("rev-parse", "--verify") and args[-1].endswith(":" + PATH):
                return subprocess.CompletedProcess(args, 128, b"", b"unreadable tree")
            return git(repo, *args, **kwargs)

        with mock.patch.object(radar, "_git", side_effect=unreadable):
            report = self.scan()
        self.assertEqual([m["number"] for m in report["migrations"]], ["522"])
        self.assertEqual([m["work_item"] for m in report["unreserved_migrations"]], [2277])


if __name__ == "__main__":
    unittest.main()
