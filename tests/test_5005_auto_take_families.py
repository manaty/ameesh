# SPDX-License-Identifier: AGPL-3.0-only
"""#5005 : routage temporaire du travail neuf, compilé depuis le canon."""
from datetime import datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from ameesh import backlog, handoff, mechanics, notify, work
from . import test_l165_prise_lots as lots
from .test_l233b_budget_handoff import _Handoff, GIT_OK
from .test_l1616_mechanics import MECHANICS


def document(expire="2099-01-01", families="[deepseek]"):
    return mechanics.compile_document(MECHANICS + (
        "auto_take:\n  excluded_families: %s\n  expire: %s\n" % (families, expire)))


class CompilationTest(unittest.TestCase):
    def test_absent_block_preserves_serialization(self):
        policy = mechanics.compile_document(MECHANICS)
        self.assertIsNone(policy.auto_take)
        self.assertNotIn("auto_take", policy.to_dict())
        self.assertEqual(policy.auto_take_excluded_families(0), ())

    def test_valid_block_serializes_and_empty_list_is_allowed(self):
        self.assertEqual(document().to_dict()["auto_take"],
                         {"excluded_families": ["deepseek"], "expire": "2099-01-01"})
        self.assertEqual(document(families="[]").auto_take_excluded_families(0), ())

    def test_invalid_or_missing_expiry_and_unknown_family_refuse(self):
        for block in (
            "excluded_families: [deepseek]",
            "excluded_families: [unknown]\n  expire: 2026-11-01",
            "excluded_families: [deepseek, deepseek]\n  expire: 2026-11-01",
            "excluded_families: deepseek\n  expire: 2026-11-01",
            "excluded_families: [deepseek]\n  expire: 2026-02-30",
            "excluded_families: [deepseek]\n  expire: 20261101",
            "excluded_families: [deepseek]\n  expire: true",
            "expire: 2026-11-01",
            "excluded_families: [deepseek]\n  expire: 2026-11-01\n  typo: true",
        ):
            with self.subTest(block=block), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(MECHANICS + "auto_take:\n  " + block + "\n")

    def test_expiry_is_inclusive_in_canon_timezone(self):
        policy = document(expire="2026-10-30")
        for hour, expected in ((22, ("deepseek",)), (23, ())):
            # Paris UTC+1 : 23:00 UTC est déjà le jour suivant.
            now = datetime(2026, 10, 30, hour, 59, tzinfo=ZoneInfo("UTC")).timestamp()
            self.assertEqual(policy.auto_take_excluded_families(now), expected)


class BudgetHandoffTest(_Handoff):
    def hand_with_policy(self, policy, **kwargs):
        with patch.object(mechanics, "configured", return_value=policy), patch.object(
                handoff, "lot_git", return_value=GIT_OK):
            return self.hand(**kwargs)

    def test_excluded_family_build_goes_to_other_family(self):
        self.team()
        self.agent("codex1", "codex", team="nexlink")
        lot = self.lot("build", state="build", branch="lot/build")
        out = self.hand_with_policy(document())
        self.assertEqual([row["agent"] for row in out], ["codex1"])
        self.assertEqual(self.assignee(lot), "codex1")

    def test_without_eligible_target_build_stays_but_intake_returns(self):
        self.team()
        build = self.lot("build", state="build", branch="lot/build")
        intake = self.lot("intake")
        out = self.hand_with_policy(document())
        self.assertEqual([(row["item"]["id"], row["agent"]) for row in out],
                         [(intake["id"], None)])
        self.assertEqual(self.assignee(build), "claude1")
        self.assertIsNone(self.assignee(intake))

    def test_absent_or_expired_exclusion_preserves_handoff(self):
        self.team()
        lot = self.lot("build", state="build", branch="lot/build")
        for policy in (mechanics.compile_document(MECHANICS), document(expire="2000-01-01")):
            with self.subTest(policy=policy.auto_take):
                self.assertEqual([row["agent"] for row in self.hand_with_policy(policy, dry_run=True)],
                                 ["deepseek1"])
                self.assertEqual(self.assignee(lot), "claude1")

    def test_intake_return_does_not_read_take_policy(self):
        self.team()
        lot = self.lot("intake")
        with patch.object(mechanics, "configured", side_effect=AssertionError("unneeded read")) as read, patch.object(
                handoff, "lot_git", return_value=GIT_OK):
            out = self.hand()
        read.assert_not_called()
        self.assertEqual([(r["item"]["id"], r["agent"]) for r in out], [(lot["id"], None)])

    def test_build_without_target_does_not_read_take_policy(self):
        self.team()
        lot = self.lot("build", state="build", branch="lot/build")
        with patch.object(mechanics, "configured", side_effect=AssertionError("unneeded read")) as read:
            self.assertEqual(self.hand(budget_check=lambda name: "budget exhausted"), [])
        read.assert_not_called()
        self.assertEqual(self.assignee(lot), "claude1")

    def test_unreadable_canon_prevents_handoff(self):
        self.team()
        lot = self.lot("build", state="build", branch="lot/build")
        with patch.object(mechanics, "configured", side_effect=mechanics.MechanicsError("unreadable")):
            with self.assertRaises(mechanics.MechanicsError):
                self.hand()
        self.assertEqual(self.assignee(lot), "claude1")
        self.assertEqual(self.mails(), [])


class AutoTakeTest(lots._Lots):
    def policy(self, value=None):
        patched = patch.object(mechanics, "configured", return_value=value or document())
        patched.start()
        self.addCleanup(patched.stop)

    def test_excluded_family_leaves_lot_to_other_family_in_same_pass(self):
        self.policy()
        self.agent("excluded", harness="deepseek-flash", team="nexlink", since_s=5 * 3600)
        self.agent("eligible", harness="codex", team="nexlink")
        lot = self.lot("travail neuf")
        out = self.take(paid=(), review_max_per_hour=0)
        self.assertEqual([(r["agent"], r["kind"]) for r in out], [("eligible", "lot")])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "eligible")

    def test_excluded_family_does_not_take_backlog(self):
        self.policy()
        self.agent("excluded", harness="deepseek", team="nexlink")
        self.element("amelioration", 50, team="nexlink")
        self.assertEqual(self.take(review_max_per_hour=0), [])
        self.agent("eligible", harness="claude", team="nexlink")
        out = self.take(review_max_per_hour=0)
        self.assertEqual([(r["agent"], r["kind"]) for r in out], [("eligible", "backlog")])

    def test_absent_and_expired_policy_allow_normal_take(self):
        self.agent("excluded", harness="deepseek", team="nexlink")
        self.lot("travail neuf")
        for policy in (mechanics.compile_document(MECHANICS), document(expire="2000-01-01")):
            with self.subTest(policy=policy.auto_take), patch.object(
                    mechanics, "configured", return_value=policy):
                self.assertEqual([r["agent"] for r in self.take(dry_run=True)], ["excluded"])

    def test_unreadable_canon_aborts_before_assignment(self):
        self.agent("eligible", team="nexlink")
        lot = self.lot("travail neuf")
        with patch.object(mechanics, "configured", side_effect=mechanics.MechanicsError("unreadable")):
            with self.assertRaises(mechanics.MechanicsError):
                self.take()
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_no_take_attempt_does_not_read_policy(self):
        self.agent("eligible", team="nexlink")
        lot = self.lot("travail neuf")
        for options in ({"listing": []}, {"budget_check": lambda name: "exhausted"},
                        {"agents": set()}, {"lot_max_per_hour": 0}):
            with self.subTest(options=options), patch.object(
                    mechanics, "configured", side_effect=AssertionError("unneeded read")) as read:
                self.assertEqual(self.take(review_max_per_hour=0, **options), [])
                read.assert_not_called()
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_empty_pass_does_not_read_policy(self):
        self.agent("eligible", team="nexlink")
        with patch.object(mechanics, "configured", side_effect=AssertionError("unneeded read")) as read:
            self.assertEqual(self.take(review_max_per_hour=0), [])
        read.assert_not_called()

    def test_policy_is_read_once_for_multiple_candidate_pools(self):
        self.agent("excluded", harness="deepseek", team="nexlink")
        self.lot("travail neuf")
        with patch.object(mechanics, "configured", return_value=document()) as read:
            self.assertEqual(self.take(review_max_per_hour=0, agents=None), [])
        read.assert_called_once_with(self.cfg)

    def test_proposal_pool_keeps_excluded_family(self):
        self.policy()
        self.agent("excluded", harness="deepseek", team="nexlink")
        # Un backlog permet le passage jusqu'à la proposition, sans simuler
        # les règles de provenance/relecture qui ne changent pas dans #5005.
        self.element("amelioration", 50, team="nexlink")
        for kind in ("review", "correction"):
            def propose(*args, **kwargs):
                self.assertEqual([r["name"] for r in kwargs["pool"]], ["excluded"])
                return [{"agent": "excluded", "kind": kind}]
            with self.subTest(kind=kind), patch.object(backlog, "_propose_reviews", side_effect=propose):
                self.assertEqual([r["kind"] for r in self.take()], [kind])

    def test_notify_records_take_without_swallowing_a_canon_error(self):
        self.policy()
        self.agent("excluded", harness="deepseek", team="nexlink")
        self.agent("eligible", harness="codex", team="nexlink")
        lot = self.lot("travail neuf")
        records, logs = [], []
        notifier = notify.Notifier(
            self.cfg, notify.parse_config({}), log=logs.append,
            emit=records.append, clock=lambda: self.now,
            take={"idle_s": 0, "max_per_hour": 0,
                  "lot_idle_s": 900, "lot_max_per_hour": 6,
                  "review_max_per_hour": 0, "orchestras": []})
        notifier.run_pass(self.db, thresholds=lots._seuils())
        self.assertFalse(any("auto-take failed" in entry for entry in logs), logs)
        taken = [r for r in records if r["event"] == "auto_take"]
        self.assertEqual([(r["agent"], r["lot"]) for r in taken],
                         [("eligible", lot["id"])])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "eligible")

    def test_local_orchestrator_fallback_cannot_bypass_exclusion(self):
        self.policy()
        self.agent("orchestrateur", harness="deepseek", team="nexlink")
        self.lot("travail neuf")
        self.assertEqual(self.take(review_max_per_hour=0), [])
