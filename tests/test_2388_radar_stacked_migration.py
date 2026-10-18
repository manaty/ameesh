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

import subprocess
import tempfile
import unittest

from ameesh import radar

from .test_l160b_radar import _git, _write, git_env

MIG = "src/ameesh/migrations"
INHERITED = MIG + "/516_stacked.sql"
CONTENT = "-- stacked\nSELECT 1;\n"


def _git_at(repo, date, *args):
    """`_git` with fixed author/committer dates, so the survivor ranking has a
    deterministic order (#2388 review)."""
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=dict(git_env(), GIT_AUTHOR_DATE=date,
                                   GIT_COMMITTER_DATE=date))
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


PARENT = {"id": 2350, "branch": "lot/2350-parent"}
STACKED = {"id": 2351, "branch": "lot/2351-stacked"}
PACKAGE = {"id": 2363, "branch": "integration/paquet-63"}
SIBLING = {"id": 2352, "branch": "lot/2352-sibling"}
OTHER = {"id": 2400, "branch": "lot/2400-other"}

#: A < C < B, fixed so the survivor ranking is deterministic (#2388 review).
DATES = {"A": "2026-01-01T02:00:00+0000", "B": "2026-01-01T05:00:00+0000",
         "C": "2026-01-01T03:00:00+0000", "D": "2026-01-01T04:00:00+0000"}


class StackedMigrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ameesh-2388-")
        self.addCleanup(temporary.cleanup)
        self.repo = temporary.name
        _git(self.repo, "init", "-q", "-b", "main")
        self.commit("README.md", "Base\n")

    def commit(self, path, text, date=None):
        _write(self.repo, path, text)
        _git(self.repo, "add", "-A")
        if date is None:
            _git(self.repo, "commit", "-qm", path)
        else:
            _git_at(self.repo, date, "commit", "-qm", path)

    def branch(self, base, name, edits, date=None):
        _git(self.repo, "checkout", "-q", "-B", name, base)
        for path, text in edits.items():
            self.commit(path, text, date)
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

        # One representative keeps the attribution; the stacked copy is the
        # inherited one, so a third holder still collides with the parent.
        self.assertIn(INHERITED, seen[PARENT["id"]])
        self.assertNotIn(INHERITED, seen[STACKED["id"]])
        self.assertIn(own, seen[STACKED["id"]])
        self.assertEqual(self.scan([STACKED, PARENT])["migrations"], [])

    def test_inherited_copy_still_collides_with_a_third_party(self):
        """Review #2388: hiding both copies hid a real collision.

        parent adds 516_parent, stacked forks it, parent advances, and an
        independent lot adds a different 516. The inherited copy must keep a
        representative: 516 is a real duplicate with the independent lot.
        """
        self.branch("main", PARENT["branch"],
                    {MIG + "/516_parent.sql": CONTENT})
        self.branch(PARENT["branch"], STACKED["branch"], {"stacked.py": "b\n"})
        self.branch(PARENT["branch"], PARENT["branch"], {"parent.py": "a\n"})
        self.branch("main", OTHER["branch"],
                    {MIG + "/516_independent.sql": "-- third\n"})

        report = self.scan([PARENT, STACKED, OTHER])

        self.assertEqual([m["number"] for m in report["migrations"]], ["516"])
        self.assertEqual([h["work_item"] for h in report["migrations"][0]["lots"]],
                         [PARENT["id"], OTHER["id"]])
        # The same collision is seen when the stacked lot is not scanned.
        self.assertEqual([m["number"] for m in self.scan([PARENT, OTHER])["migrations"]],
                         ["516"])

    def test_two_siblings_without_their_parent_still_collide_with_a_third_party(self):
        """Two lots forked on the same base (the parent itself is not scanned)
        and a third independent lot reusing the number: one sibling copy is
        inherited, the other representative still collides."""
        anchor = "lot/2340-anchor"
        self.branch("main", anchor, {MIG + "/516_shared.sql": CONTENT})
        self.branch(anchor, STACKED["branch"], {"stacked.py": "b\n"})
        self.branch(anchor, SIBLING["branch"], {"sibling.py": "c\n"})
        self.branch("main", OTHER["branch"],
                    {MIG + "/516_independent.sql": "-- third\n"})

        self.assertEqual(self.scan([STACKED, SIBLING])["migrations"], [])
        report = self.scan([STACKED, SIBLING, OTHER])

        self.assertEqual([m["number"] for m in report["migrations"]], ["516"])
        holders = [h["work_item"] for h in report["migrations"][0]["lots"]]
        self.assertEqual(len(holders), 2)
        self.assertIn(OTHER["id"], holders)

    def test_mixed_ancestor_and_diverged_holders_keep_one_survivor(self):
        """Review #2388: M→A→B and M→C, with dates A<C<B, plus an independent
        holder D. Pair by pair, A/B drops B (strict ancestor), A/C drops A and
        B/C drops C (older tip first): no branch carries 516 any more. One
        survivor is chosen for the whole group, and it still collides with D."""
        anchor = "lot/2349-anchor"
        self.branch("main", anchor, {INHERITED: CONTENT})
        self.branch(anchor, PARENT["branch"], {"a.py": "a\n"}, date=DATES["A"])
        self.branch(PARENT["branch"], STACKED["branch"], {"b.py": "b\n"},
                    date=DATES["B"])
        self.branch(anchor, SIBLING["branch"], {"c.py": "c\n"}, date=DATES["C"])
        self.branch("main", OTHER["branch"],
                    {MIG + "/516_independent.sql": "-- third\n"}, date=DATES["D"])
        seen = {}

        def reservations(branches):
            seen.update({b["work_item"]: list(b.get("added") or ()) for b in branches})
            return []

        report = self.scan([PARENT, STACKED, SIBLING, OTHER],
                           reservations=reservations)

        self.assertEqual([m["number"] for m in report["migrations"]], ["516"])
        self.assertEqual([h["work_item"] for h in report["migrations"][0]["lots"]],
                         [SIBLING["id"], OTHER["id"]])
        # C (dated between A and B) is the single survivor of the inherited
        # group: A and B no longer carry 516, C still does.
        self.assertIn(INHERITED, seen[SIBLING["id"]])
        self.assertNotIn(INHERITED, seen[PARENT["id"]])
        self.assertNotIn(INHERITED, seen[STACKED["id"]])
        self.assertIn(MIG + "/516_independent.sql", seen[OTHER["id"]])
        # The same collision is seen without the intermediate stacked lot.
        self.assertEqual(
            [m["number"] for m in self.scan([PARENT, SIBLING, OTHER])["migrations"]],
            ["516"])


if __name__ == "__main__":
    unittest.main()
