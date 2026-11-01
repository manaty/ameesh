# SPDX-License-Identifier: AGPL-3.0-only
"""#5244 : l'avancement compte les activations (canon §5, §14), pas la fusion."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import activation, plan, roadmap, staging_pending, work

from ameesh.storage.postgres import progress as postgres_progress

from .support import PgTestCase


def lot(ident, state="delivered", reason=None, app="ameesh", executor=None, package="e1"):
    return {"id": ident, "state": state, "close_reason": reason, "app": app,
            "external_executor": executor, "package_id": package}


PROOF = activation.Evidence(ancestors=sorted(["a" * 40, "c" * 40]), known={"b" * 12})


def step(item, sha=None, receipts=None, proof=PROOF):
    return activation.step_of(item, sha, receipts=receipts, proof=proof)["step"]


class Steps(unittest.TestCase):
    def test_merged_needs_the_attested_commit(self):
        self.assertEqual(step(lot(1), "a" * 12), "active")
        self.assertEqual(step(lot(1), "b" * 12), "merged")
        self.assertFalse(activation.step_of(lot(1), "b" * 12, receipts=None,
                                            proof=PROOF)["counted"])

    def test_absence_of_evidence_is_not_a_negative(self):
        for item, sha, proof in ((lot(1), "d" * 12, PROOF),  # commit absent du clone
                                 (lot(1), None, PROOF),
                                 (lot(1), "a" * 12, activation.Evidence()),
                                 (lot(1, app="autre"), "a" * 12, PROOF)):
            view = activation.step_of(item, sha, receipts=None, proof=proof)
            self.assertEqual((view["step"], view["counted"]), ("unmeasured", False))

    def test_operator_gesture_needs_an_executed_signed_receipt(self):
        gesture = lot(1, "closed", "done", executor="bureau")
        self.assertEqual(step(gesture), "gesture")
        view = activation.step_of(gesture, None, receipts={1: "act"}, proof=PROOF)
        self.assertEqual((view["step"], view["counted"], view["action"]),
                         ("receipt", True, "act"))

    def test_authorization_is_not_a_result(self):
        row = {"work_item": 1, "action_id": "act", "state": "confirmed", "auth_kind": "receipt"}
        self.assertEqual(activation.signed_receipts([row]), {1: "act"})
        for other in ({"state": "approved"}, {"state": "failed"}, {"auth_kind": "standing"},
                      {"auth_kind": "none"}):
            self.assertEqual(activation.signed_receipts([dict(row, **other)]), {})

    def test_losing_the_evidence_never_raises_progress(self):
        view = activation.step_of(lot(1), "a" * 12, receipts=None, proof=activation.Evidence())
        epic = plan.summarize(Tally.PACKAGES[:1], [lot(1)], {1: view})["epics"][0]
        self.assertEqual((epic["progress_active"], epic["lots_unmeasured"], epic["lots_active"]),
                         (0, 1, 0))
        self.assertEqual(epic["progress"], 1)

    def test_other_closures_keep_their_meaning(self):
        self.assertEqual(step(lot(1, "closed", "done")), "abandoned")
        self.assertEqual(step(lot(1, "closed", "superseded", executor="bureau")), "abandoned")
        self.assertEqual(step(lot(1, "closed", "delivered")), "delivered")
        self.assertEqual(step(lot(1, "closed", "delivered", executor="bureau")), "delivered")
        self.assertEqual(step(lot(1, "build")), "open")

    def test_annulled_merge_is_not_the_lot_commit(self):
        rows = [{"work_item_id": 1, "kind": "merged", "sha": "A" * 12},
                {"work_item_id": 1, "kind": "merged", "sha": "b" * 12, "annulled": True}]
        self.assertEqual(activation.merged_shas(rows), {1: "a" * 12})

    def test_short_sha_prefix(self):
        self.assertTrue(PROOF.contains("c" * 7))
        self.assertFalse(PROOF.contains("b" * 7))


class Tally(unittest.TestCase):
    PACKAGES = [{"id": "e1", "kind": "epic", "title": "E"},
                {"id": "f1", "kind": "lot", "parent": "e1", "title": "F"}]

    def summary(self, items, steps):
        return plan.summarize(self.PACKAGES, items, steps)["epics"][0]

    def test_without_activation_nothing_changes(self):
        epic = plan.summarize(self.PACKAGES, [lot(1), lot(2, package="f1")])["epics"][0]
        self.assertEqual((epic["lots_merged"], epic["progress"]), (2, 1))
        self.assertIsNone(epic["progress_active"])
        self.assertEqual(plan.done_share(epic), 1)

    def test_progress_counts_active_lots_only(self):
        steps = {1: {"step": "merged", "counted": False}, 2: {"step": "active", "counted": True},
                 3: {"step": "gesture", "counted": False}}
        epic = self.summary([lot(1), lot(2), lot(3, "closed", "done", executor="x")], steps)
        self.assertEqual(epic["lots_pending"], 1)  # la fiche f1, sans lot
        self.assertEqual((epic["progress_active"], epic["lots_activation_total"]), (0.25, 4))
        # la fusion garde son sens (contrat ameesh-progress/1) : le geste reste hors compte
        self.assertEqual((epic["lots_merged"], epic["progress"]), (2, round(2 / 3, 4)))
        self.assertEqual((epic["lots_active"], epic["lots_waiting_activation"]), (1, 2))

    def test_card_is_held_by_its_least_advanced_lot(self):
        steps = {1: {"step": "active", "counted": True}, 2: {"step": "merged", "counted": False}}
        epic = self.summary([lot(1, package="f1"), lot(2, package="f1")], steps)
        self.assertEqual((epic["lots_waiting_activation"], epic["progress_active"]), (1, 0))
        steps[2] = {"step": "active", "counted": True}
        epic = self.summary([lot(1, package="f1"), lot(2, package="f1")], steps)
        self.assertEqual((epic["lots_active"], epic["progress_active"]), (1, 1))
        self.assertEqual(epic["lots"][0]["step"], "active")
        steps[2] = {"step": "merged", "counted": False}
        epic = self.summary([lot(1, package="f1"), lot(2, package="f1")], steps)
        self.assertEqual(epic["lots"][0]["step"], "waiting")


class Overview(unittest.TestCase):
    def test_fully_merged_milestone_stays_until_done(self):
        from ameesh import progress
        ms = {"id": "M", "title": "m", "progress": 1, "lots_merged": 1, "lots_total": 1,
              "progress_active": 0, "lots_active": 0, "lots_waiting_activation": 0,
              "lots_unmeasured": 1}
        cards = progress.build_overview([{"name": "p"}], [], [ms], {"M": {"p"}}, [])
        self.assertEqual(cards[0]["milestone"]["id"], "M")
        ms["progress_active"] = 1
        cards = progress.build_overview([{"name": "p"}], [], [ms], {"M": {"p"}}, [])
        self.assertIsNone(cards[0]["milestone"])
        del ms["progress_active"]  # ancien instantané : la fusion décide
        cards = progress.build_overview([{"name": "p"}], [], [ms], {"M": {"p"}}, [])
        self.assertIsNone(cards[0]["milestone"])


class Clone(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.git("init", "-q")
        self.first = self.commit("one")
        self.second = self.commit("two")

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", "-C", self.dir.name, *args], env=env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def evidence(self, hosts, shas=()):
        with mock.patch.object(staging_pending, "active_hosts", return_value=hosts), \
                mock.patch.object(activation, "_clones", return_value=[self.dir.name]):
            return activation.evidence(None, shas)

    def test_every_active_host_must_carry_the_commit(self):
        proof = self.evidence({"vm1": {"sha": self.second}, "vm2": {"sha": self.first},
                               "poste": {"sha": None, "absent": True}},
                              [self.second, "d" * 12])
        self.assertTrue(proof.contains(self.first[:12]))
        self.assertFalse(proof.contains(self.second[:12]))
        self.assertEqual(proof.known, {self.second})

    def test_unknown_attested_commit_measures_nothing(self):
        self.assertIsNone(self.evidence({"vm1": {"sha": "d" * 40}}).ancestors)
        self.assertIsNone(self.evidence({}).ancestors)


class Registry(PgTestCase):
    def setUp(self):
        super().setUp()
        self.ident = int(work.add(db=self.db, title="Geste", assignee=None, app="ameesh")["id"])

    def test_receipt_columns_are_read(self):
        from ameesh import storage
        reads = storage.of(self.db).progress
        self.assertEqual(reads.lot_actions([self.ident]), [])
        self.assertFalse(reads.lot_milestones([self.ident])[0]["annulled"])
        self.assertIn("auth_kind", postgres_progress._ACTION_COLUMNS)

    def test_commitment_waits_for_activation(self):
        row = {"id": 1, "work_item_id": self.ident, "status": "open", "kind": "commitment",
               "what": "x", "due_on": "2026-01-01"}
        base = roadmap.today()
        item = lot(self.ident)
        view = roadmap._commitment_view(row, base, {self.ident: item},
                                        {self.ident: {"counted": False}})
        self.assertFalse(view["fulfilled"])
        self.assertTrue(view["late"])
        view = roadmap._commitment_view(row, base, {self.ident: item},
                                        {self.ident: {"counted": True}})
        self.assertTrue(view["fulfilled"])
        view = roadmap._commitment_view(row, base, {self.ident: item},
                                        {self.ident: {"counted": False, "step": "unmeasured"}})
        self.assertFalse(view["fulfilled"])
        self.assertIsNone(view["late"])
