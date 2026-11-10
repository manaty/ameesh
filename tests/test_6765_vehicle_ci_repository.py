# SPDX-License-Identifier: AGPL-3.0-only
"""#6765: CI uses the live vehicle proof, including locked member revalidation."""
import argparse
from unittest import mock

from ameesh import ci, packet_automerge, packet_reviews, review_target
from . import test_l161g_ci_gel as fixtures


class VehicleCiTest(fixtures.GelTestCase):
    def setUp(self):
        super().setUp()
        self.item["branch"] = "integration/paquet-6765"
        self.jalons.append(fixtures.jalon("frozen", sha=fixtures.GEL))
        self.work.enter_context(mock.patch.object(packet_automerge, "_gate_repo",
                                                  return_value="/checkout"))
        self.work.enter_context(mock.patch("ameesh.plan_git.local_commit",
                                           return_value=fixtures.GEL))
        self.work.enter_context(mock.patch.object(review_target, "resolve",
                                                  return_value=fixtures.BASE))
        self.proof = self.work.enter_context(mock.patch.object(
            packet_reviews, "need", return_value={"ok": True, "required": 0}))

    def plan(self):
        api = fixtures.Github(pulls=[fixtures.pull(head_ref=self.item["branch"])])
        def read(path):
            if path.endswith("/branches/integration%2Fpaquet-6765"):
                return {"commit": {"sha": fixtures.GEL}}
            return api(path)
        return self.gel(read, full=True, reason="package")

    def test_zero_own_delta_does_not_require_a_vehicle_verdict(self):
        decision = self.plan()
        self.assertEqual(decision["action"], "request")
        self.assertEqual(decision["avis"], 0)
        self.assertEqual(decision["vehicle_review"],
                         {"repo": "/checkout", "base": fixtures.BASE})
        ci._revalidate(self.db, self.item["id"], decision)
        self.assertEqual(self.proof.call_count, 2)

    def test_unreviewed_member_blocks_ci_and_locked_request(self):
        decision = self.plan()
        self.proof.return_value = {"ok": False, "required": None,
                                   "why": "constituent #12: missing review"}
        for act in (self.plan, lambda: ci._revalidate(self.db, self.item["id"], decision)):
            with self.subTest(action=act):
                with self.assertRaisesRegex(ci.GelError, "constituent #12"):
                    act()

    def test_own_delta_and_absent_members_do_not_get_a_waiver(self):
        for needed in (None, {"ok": None, "required": 1}):
            with self.subTest(needed=needed):
                self.proof.return_value = needed
                with self.assertRaisesRegex(ci.GelError, "independent verdict"):
                    self.plan()

    def test_missing_repository_is_named_and_does_not_read_vehicle(self):
        with mock.patch.object(packet_automerge, "_gate_repo", return_value=""), \
                mock.patch("ameesh.fire.review_checkout", return_value=None):
            with self.assertRaisesRegex(ci.GelError, "no repository"):
                self.plan()
        self.proof.assert_not_called()

    def test_changed_own_delta_proof_under_lock_refuses(self):
        decision = self.plan()
        self.proof.return_value = {"ok": None, "required": 1}
        with self.assertRaisesRegex(ci.GelError, "proof changed"):
            ci._revalidate(self.db, self.item["id"], decision)

    def test_missing_frozen_head_refuses_before_a_waiver(self):
        with mock.patch("ameesh.plan_git.local_commit", return_value=None), \
                mock.patch.object(packet_automerge, "_fetch_into", return_value="offline"):
            with self.assertRaisesRegex(ci.GelError, "frozen head.*not available"):
                self.plan()
        self.proof.assert_not_called()

    def test_auto_merge_receives_the_same_checkout_as_the_ci_proof(self):
        decision = self.plan()
        args = argparse.Namespace(id=self.item["id"], actor="pilot", prevalidation_repo="")
        with mock.patch.object(packet_automerge, "arm", return_value={"state": "armed"}) as arm:
            ci._arm_packet(None, self.db, decision, fixtures.Gh(), args)
        self.assertEqual(arm.call_args.kwargs["checkout"], "/checkout")
