# SPDX-License-Identifier: AGPL-3.0-only
"""#6834 : le gel ne peut emprunter la tête ou la branche d'un lot ouvert."""
from unittest import mock

from ameesh import backlog, plan_git, work
from . import test_6161_freeze_branch as fixtures
from .test_2962_freeze_unresolved import git


class FreezeOwnershipTest(fixtures.FreezeBranchTest):
    # Reprendre les témoins existants vérifie aussi les gels à branche propre.
    def other(self):
        return backlog.add(self.db, title="détenteur", value_score=40,
                           expected_value="garde du gel")["id"]

    def assert_no_freeze(self):
        self.assertFalse(any(m["kind"] == "frozen"
                             for m in work.milestones(self.db, self.lot)))
        self.assertIsNone(work.get(self.db, self.lot)["branch"])

    def test_exact_open_frozen_head_is_refused(self):
        self.publish()
        owner = self.other()
        work.milestone(self.db, owner, "frozen", sha=self.sha, repo=self.repo,
                       actor="human:test", on_frozen=lambda *args: None)
        with self.assertRaisesRegex(work.WorkError, "owned-by-open-lot.*#%s" % owner):
            self.freeze()
        self.assert_no_freeze()

    def test_inferred_branch_owned_by_other_open_lot_is_refused(self):
        self.publish("shared")
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/shared")
        owner = self.other()
        work.set_branch(self.db, owner, "shared", actor="human:test")
        resolve = plan_git.local_commit
        def without_owner_ref(repo, ref, **kwargs):
            # Le clone peut lire le commit sans disposer de la ref du détenteur.
            return None if ref.endswith("/shared") else resolve(repo, ref, **kwargs)
        with mock.patch.object(plan_git, "local_commit", side_effect=without_owner_ref):
            with self.assertRaisesRegex(work.WorkError, "owned-by-open-lot.*branch.*#%s" % owner):
                self.freeze()
        self.assert_no_freeze()

    def test_delivered_ancestor_does_not_own_new_head(self):
        self.publish()
        owner = self.other()
        row = dict(work.get(self.db, owner), state="delivered", branch="agent/basket",
                   branch_head=git(self.repo, "rev-parse", "HEAD^"))
        from ameesh import storage
        st = storage.of(self.db)
        with mock.patch.object(type(st.work), "items", return_value=[row]):
            self.freeze()
        self.assertEqual(work.get(self.db, self.lot)["branch"], "agent/basket")

    def test_notes_without_branch_or_delta_point_to_done(self):
        git(self.repo, "rm", "-q", "change")
        git(self.repo, "commit", "-qm", "no remaining delta")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(work.WorkError, "notes-only.*done"):
            self.freeze()
        self.assert_no_freeze()


    def test_unfrozen_associated_branch_head_is_owned(self):
        owner = self.other()
        work.set_branch(self.db, owner, "agent/basket", actor="human:test")
        work.set_branch(self.db, self.lot, "own-branch", actor="human:test")
        with self.assertRaisesRegex(work.WorkError, "owned-by-open-lot.*head.*#%s" % owner):
            self.freeze()
        self.assertFalse(any(m["kind"] == "frozen"
                             for m in work.milestones(self.db, self.lot)))

    def test_own_refreeze_survives_child_branch_pointing_at_head(self):
        self.publish()
        self.freeze()
        owner = self.other()
        git(self.repo, "branch", "child", self.sha)
        work.set_branch(self.db, owner, "child", actor="human:test")
        self.freeze()
        self.assertEqual(work.get(self.db, self.lot)["branch"], "agent/basket")
