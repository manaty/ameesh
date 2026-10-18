# SPDX-License-Identifier: AGPL-3.0-only
"""#2388: a lot stacked on a parent keeps the parent's migrations, it does not
add them again.

A branch born on a package head (or on another lot) carries the migrations
already present in that base. The radar computes `added` against the target,
so those inherited files appear as additions of every stacked lot; when the
parent's tip has moved since the fork, #2279's strict-ancestor proof no longer
recognises them and a false duplicate is reported.
"""
from __future__ import annotations

import tempfile
import unittest

from ameesh import radar

from .test_l160b_radar import _git, _write

MIG = "src/ameesh/migrations"
INHERITED = MIG + "/516_stacked.sql"
CONTENT = "-- stacked\nSELECT 1;\n"

PARENT = {"id": 2350, "branch": "lot/2350-parent"}
STACKED = {"id": 2351, "branch": "lot/2351-stacked"}
PACKAGE = {"id": 2363, "branch": "integration/paquet-63"}
SIBLING = {"id": 2352, "branch": "lot/2352-sibling"}
OTHER = {"id": 2400, "branch": "lot/2400-other"}


class StackedMigrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-2388-")
        self.addCleanup(temporary.cleanup)
        self.repo = temporary.name
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("README.md", "Base\n")

    def commit(self, path, text):
        _write(self.repo, path, text)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", path)

    def branch(self, base, name, edits):
        _git(self.repo, "checkout", "-q", "-B", name, base)
        for path, text in edits.items():
            self.commit(path, text)
        return _git(self.repo, "rev-parse", "HEAD")

    def scan(self, lots, *, reservations=None):
        return radar.scan(self.repo, lots, default_target="main", migrations=MIG,
                          reservations=reservations)

    def test_stacked_lot_inherits_the_migration_of_its_parent(self):
        """#2388: the parent moved after the fork; the file is still inherited."""
        self.branch("main", PARENT["branch"], {INHERITED: CONTENT})
        self.branch(PARENT["branch"], STACKED["branch"], {"stacked.py": "b\n"})
        self.branch(PARENT["branch"], PARENT["branch"], {"parent.py": "a\n"})

        report = self.scan([STACKED, PARENT])

        self.assertEqual(report["migrations"], [])
        self.assertEqual(len(report["branches"]), 2)

    def test_sibling_lots_stacked_on_a_moving_package_do_not_duplicate(self):
        """The real shape: two lots forked on the same package head, then the
        package advanced. Neither lot added the package's migration."""
        self.branch("main", PACKAGE["branch"], {INHERITED: CONTENT})
        self.branch(PACKAGE["branch"], STACKED["branch"], {"stacked.py": "b\n"})
        self.branch(PACKAGE["branch"], SIBLING["branch"], {"sibling.py": "c\n"})
        self.branch(PACKAGE["branch"], PACKAGE["branch"], {"package.py": "p\n"})

        report = self.scan([PACKAGE, STACKED, SIBLING])

        self.assertEqual(report["migrations"], [])

    def test_independent_lots_reusing_a_number_are_a_duplicate(self):
        self.branch("main", PARENT["branch"],
                    {MIG + "/516_first.sql": "-- first\n"})
        self.branch("main", OTHER["branch"],
                    {MIG + "/516_second.sql": "-- second\n"})

        report = self.scan([PARENT, OTHER])

        self.assertEqual([m["number"] for m in report["migrations"]], ["516"])
        self.assertEqual([h["work_item"] for h in report["migrations"][0]["lots"]],
                         [PARENT["id"], OTHER["id"]])
        self.assertIsNone(report["migrations"][0]["in_target"])

    def test_independent_identical_additions_remain_a_collision(self):
        """No common base carries the file: two identical copies are still two
        additions, not an inheritance."""
        self.branch("main", PARENT["branch"], {INHERITED: CONTENT})
        self.branch("main", OTHER["branch"], {INHERITED: CONTENT})

        report = self.scan([PARENT, OTHER])

        self.assertEqual([m["number"] for m in report["migrations"]], ["516"])
        self.assertEqual([h["work_item"] for h in report["migrations"][0]["lots"]],
                         [PARENT["id"], OTHER["id"]])

    def test_stacked_lot_keeps_its_own_migration_and_drops_the_inherited_one(self):
        own = MIG + "/600_own.sql"
        self.branch("main", PARENT["branch"], {INHERITED: CONTENT})
        self.branch(PARENT["branch"], STACKED["branch"], {own: "-- own\n"})
        self.branch(PARENT["branch"], PARENT["branch"], {"parent.py": "a\n"})
        seen = {}

        def reservations(branches):
            seen.update({b["work_item"]: list(b.get("added") or ()) for b in branches})
            return []

        self.scan([STACKED, PARENT], reservations=reservations)

        self.assertNotIn(INHERITED, seen[STACKED["id"]])
        self.assertIn(own, seen[STACKED["id"]])
        self.assertEqual(self.scan([STACKED, PARENT])["migrations"], [])


if __name__ == "__main__":
    unittest.main()
