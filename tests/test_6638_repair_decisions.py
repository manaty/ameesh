# SPDX-License-Identifier: AGPL-3.0-only
"""U13/A9: repair plans reach the existing decision queue, at most once.

The adapter files a request; it never admits, executes or approves a repair.
Only the exact recorded owner option (with the proof the plan requires) is
read as approval: silence at the deadline, a free text and a mail reaction
leave the plan unapproved. Deadline and reminder are the queue's existing
`decision_pending` / `decision_overdue` alerts, not a second queue.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
import unittest

from ameesh import decisions, exploitation, registry, storage, validation_deadlines as vd, work
from ameesh.repair_triage import (
    RepairDecisionError, RepairPlan, RepairPlanConflict, file_plan_decision, plan_decision,
)

from .support import PgTestCase

ALICE = "human:alice"
OWNER = "human:owner"


class RepairPlanShapeTest(unittest.TestCase):
    def plan(self, **changes):
        values = dict(key="plan-1", project="ameesh", environment="staging", lot=42,
                      question="Autoriser la réparation proposée ?",
                      options=(("approve", "Autoriser la réparation"),
                               ("refuse", "Ne pas réparer")),
                      approve="approve", recommend="refuse", why="périmètre à confirmer",
                      signals=("rs1_" + "a" * 32,))
        values.update(changes)
        return RepairPlan(**values)

    def test_only_bounded_structured_content_is_accepted(self):
        for changes in (
                {"key": "plan with spaces"}, {"key": ""}, {"key": "p" * 129},
                {"project": "ameesh/staging"}, {"environment": "Staging"},
                {"lot": 0}, {"lot": "42"}, {"question": "  "},
                {"options": []}, {"options": ("approve=one",)},
                {"options": (("approve", "a"), ("approve", "b"))},
                {"approve": "other"}, {"recommend": "other"},
                {"why": "without recommendation", "recommend": ""},
                {"signals": ("not-a-receipt",)}, {"signals": ("rs1_" + "a" * 32,) * 2},
                {"needs_signature": "yes"}, {"due_ts": float("nan")}, {"due_ts": 0},
                {"due_ts": 10 ** 1000}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(**changes)

    def test_digest_binds_the_authority_content(self):
        plan = self.plan()
        self.assertRegex(plan.digest(), r"^[0-9a-f]{64}$")
        self.assertEqual(plan.digest(), self.plan().digest())
        for changes in ({"question": "Autre question ?"},
                        {"options": (("approve", "Autoriser"), ("refuse", "Refuser"))},
                        {"recommend": "", "why": ""}, {"due_ts": 1_800_000_000},
                        {"needs_signature": True},
                        {"signals": ("rs1_" + "b" * 32,)}):
            with self.subTest(changes=changes):
                self.assertNotEqual(plan.digest(), self.plan(**changes).digest())
        payload = plan.payload()
        self.assertEqual(payload["schema"], "repair-plan/1")
        self.assertEqual(payload["digest"], plan.digest())
        self.assertNotIn("question", payload)       # the decision carries it
        self.assertEqual(plan.option_values(),
                         ["approve=Autoriser la réparation", "refuse=Ne pas réparer"])


class RepairDecisionTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "orch", harness="deepseek", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET responsible = %s WHERE name = 'orch'",
                        (ALICE,))
        self.lot = work.add(self.db, title="U13 plan de réparation", assignee="orch",
                            app="ameesh")
        work.move(self.db, self.lot["id"], "build")
        self.now = time.time()

    def plan(self, **changes):
        values = dict(key="plan-1", project="ameesh", environment="staging",
                      lot=self.lot["id"], question="Autoriser la réparation proposée ?",
                      options=(("approve", "Autoriser la réparation"),
                               ("refuse", "Ne pas réparer")),
                      approve="approve", recommend="refuse",
                      why="périmètre à confirmer", signals=("rs1_" + "a" * 32,))
        values.update(changes)
        return RepairPlan(**values)

    def file(self, plan=None, **changes):
        return file_plan_decision(self.cfg, self.db, plan or self.plan(),
                                  requester="orch", now=self.now, **changes)

    def count_requests(self):
        return self.db.query("SELECT count(*)::int AS n FROM agent_mailbox"
                             " WHERE kind = 'request'")[0]["n"]

    def test_one_request_per_plan_even_after_an_answer(self):
        first = self.file()
        self.assertTrue(first["created"])
        view = first["decision"]
        self.assertEqual((view["state"], view["requester"]), ("pending", "orch"))
        self.assertEqual(view["repair_plan"]["key"], "plan-1")
        self.assertEqual(view["repair_plan"]["digest"], self.plan().digest())
        body = storage.of(self.db).decisions.get(view["id"])["body"]
        self.assertIn("Repair plan plan-1 (ameesh/staging), content sha256:", body)
        self.assertTrue(any("repair plan plan-1" in line
                            for line in decisions.format_view(view)))
        self.assertEqual(self.count_requests(), 1)
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "waiting_human")

        again = self.file()
        self.assertFalse(again["created"])
        self.assertEqual(again["decision"]["id"], view["id"])
        self.assertEqual(self.count_requests(), 1)

        decisions.answer(self.cfg, self.db, view["id"], "approve", human=ALICE,
                         channel="cli", now=self.now + 1)
        replay = self.file()
        self.assertFalse(replay["created"])
        self.assertEqual(replay["decision"]["id"], view["id"])
        self.assertEqual(replay["decision"]["state"], "answered")
        self.assertEqual(self.count_requests(), 1)

    def test_same_key_with_changed_content_is_refused_without_touching_the_request(self):
        first = self.file()
        changed = self.plan(question="Autoriser une autre réparation ?")
        with self.assertRaises(RepairPlanConflict):
            self.file(changed)
        self.assertEqual(self.count_requests(), 1)
        current = decisions.get(self.db, first["decision"]["id"])
        self.assertEqual(current["question"], "Autoriser la réparation proposée ?")
        self.assertEqual(current["repair_plan"]["digest"], self.plan().digest())

    def test_deadline_and_reminder_are_the_existing_queue_alerts(self):
        plan = self.plan(due_ts=self.now + 3600)
        view = self.file(plan)["decision"]
        self.assertAlmostEqual(view["due_ts"], self.now + 3600, delta=0.001)
        pending = [a for a in exploitation.alerts(self.cfg, self.db, now=self.now + 60)
                   if a["type"].startswith("decision_")]
        self.assertEqual([(a["type"], a["decision"]) for a in pending],
                         [("decision_pending", view["id"])])
        outcome = plan_decision(self.db, plan, now=self.now + 60)
        self.assertEqual((outcome["state"], outcome["approved"]), ("pending", False))
        overdue = [a for a in exploitation.alerts(self.cfg, self.db, now=self.now + 3700)
                   if a["type"] == "decision_overdue"]
        self.assertEqual([(a["type"], a["decision"]) for a in overdue],
                         [("decision_overdue", view["id"])])
        self.assertTrue(overdue[0]["detail"].startswith("REMINDER: no answer"))
        outcome = plan_decision(self.db, plan, now=self.now + 3700)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"]),
                         ("not_approved", False, "no_answer_at_deadline"))

    def test_missing_substitute_goes_to_the_declared_owner_and_never_approves(self):
        cfg = dataclasses.replace(self.cfg, project_planning={"ameesh": {"validations": {
            "owner": OWNER, "owner_timezone": "UTC",
            "domains": {"repair": [{"human": ALICE, "timezone": "UTC"}]}}}})
        plan = self.plan(due_ts=self.now + 60)
        view = file_plan_decision(cfg, self.db, plan, requester="orch", now=self.now)["decision"]
        self.assertEqual(view["owner"], ALICE)
        self.assertEqual(vd.sweep(cfg, self.db, now=self.now + 61),
                         [{"id": view["id"], "status": "escalated"}])
        current = decisions.get(self.db, view["id"], self.now + 62)
        self.assertEqual(current["owner"], OWNER)
        self.assertEqual(current["validation"]["status"], "escalated")
        self.assertIn("name an authorized substitute",
                      storage.of(self.db).decisions.get(view["id"])["body"])
        outcome = plan_decision(self.db, plan, now=self.now + 62)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"]),
                         ("not_approved", False, "no_answer_at_deadline"))
        self.assertTrue(outcome["escalated"])

    def test_silence_and_mail_reaction_leave_the_plan_unapproved(self):
        plan = self.plan(due_ts=self.now + 60)
        view = self.file(plan)["decision"]
        self.db.execute(
            "INSERT INTO agent_mailbox(sender, recipient, body, kind, status)"
            " VALUES ('orch', %s, 'approved, go ahead', 'notify', 'pending')", (ALICE,))
        self.assertEqual(plan_decision(self.db, plan, now=self.now + 10)["state"], "pending")
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")
        outcome = plan_decision(self.db, plan, now=self.now + 61)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"]),
                         ("not_approved", False, "no_answer_at_deadline"))
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")

    def test_only_the_exact_option_approves_and_text_or_other_choices_do_not(self):
        approved = self.plan()
        approved_view = self.file(approved)["decision"]
        decisions.answer(self.cfg, self.db, approved_view["id"], "approve",
                         human=ALICE, channel="cli", now=self.now + 1)
        outcome = plan_decision(self.db, approved, now=self.now + 2)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"],
                          outcome["answer_option"]),
                         ("approved", True, "owner_option", "approve"))

        refused = self.plan(key="plan-2", question="Réparer ou non ?")
        refused_view = self.file(refused)["decision"]
        decisions.answer(self.cfg, self.db, refused_view["id"], "refuse",
                         human=ALICE, channel="cli", now=self.now + 1)
        outcome = plan_decision(self.db, refused, now=self.now + 2)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"]),
                         ("not_approved", False, "other_option"))

        free = self.plan(key="plan-3", question="Réparer librement ?")
        free_view = self.file(free)["decision"]
        decisions.answer(self.cfg, self.db, free_view["id"], "go ahead",
                         human=ALICE, channel="cli", now=self.now + 1)
        outcome = plan_decision(self.db, free, now=self.now + 2)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"],
                          outcome["answer_option"]),
                         ("not_approved", False, "free_text_without_approval", None))

        withdrawn = self.plan(key="plan-4", question="Réparer puis retirer ?")
        withdrawn_view = self.file(withdrawn)["decision"]
        decisions.withdraw(self.cfg, self.db, withdrawn_view["id"], requester="orch",
                           why="plus nécessaire", now=self.now + 1)
        outcome = plan_decision(self.db, withdrawn, now=self.now + 2)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"]),
                         ("not_approved", False, "withdrawn"))

    def test_a_signed_requirement_is_not_waived_by_an_unsigned_answer(self):
        plan = self.plan(needs_signature=True, question="Autoriser la réparation signée ?")
        view = self.file(plan)["decision"]
        self.assertTrue(view["needs_signature"])
        with self.assertRaises(decisions.DecisionError):
            decisions.answer(self.cfg, self.db, view["id"], "approve",
                             human=ALICE, channel="cli", now=self.now + 1)
        outcome = plan_decision(self.db, plan, now=self.now + 2)
        self.assertEqual((outcome["state"], outcome["approved"]), ("pending", False))
        self.assertEqual(decisions.get(self.db, view["id"])["state"], "pending")

    def test_replacing_the_signed_plan_metadata_breaks_the_signature(self):
        # B1 (#6638): the signed document binds the plan key and digest too, so
        # swapping the stored plan for another content cannot keep the proof.
        from .test_l124_decisions import SignatureTest
        key = SignatureTest.owner_key(self)
        plan = self.plan(needs_signature=True, question="Autoriser le plan signé ?")
        view = self.file(plan)["decision"]
        decisions.answer(self.cfg, self.db, view["id"], "approve",
                         human=ALICE, channel="cli", key_path=key, now=self.now + 1)
        approved = plan_decision(self.db, plan, now=self.now + 2)
        self.assertEqual((approved["state"], approved["approved"], approved["reason"]),
                         ("approved", True, "signed_owner_option"))
        changed = self.plan(needs_signature=True, question="Autoriser le plan signé ?",
                            why="autre périmètre")
        row = storage.of(self.db).decisions.get(view["id"])
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = dict(payload)
        payload["decision"]["repair_plan"] = changed.payload()
        self.db.execute("UPDATE agent_mailbox SET payload = %s::jsonb WHERE id = %s",
                        (json.dumps(payload, ensure_ascii=False), view["id"]))
        outcome = plan_decision(self.db, changed, now=self.now + 3)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"]),
                         ("not_approved", False, "invalid_signature"))

    def test_a_superseded_authority_does_not_approve(self):
        from .test_l124_decisions import SignatureTest
        key = SignatureTest.owner_key(self)
        plan = self.plan()
        view = self.file(plan)["decision"]
        decisions.answer(self.cfg, self.db, view["id"], "approve",
                         human=ALICE, channel="cli", now=self.now + 1)
        replacement = decisions.ask(
            self.cfg, self.db, requester="orch", question="Remplacer cette autorisation ?",
            options=["approve=Approuver", "refuse=Refuser"], lot=str(self.lot["id"]),
            supersedes=view["id"], supersedes_option="approve", now=self.now + 2)
        decisions.answer(self.cfg, self.db, replacement["id"], "approve", human=ALICE,
                         channel="cli", key_path=key, now=self.now + 3)
        self.assertEqual(decisions.get(self.db, view["id"])["superseded_by"],
                         replacement["id"])
        outcome = plan_decision(self.db, plan, now=self.now + 4)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"]),
                         ("not_approved", False, "authority_not_current"))

    def test_a_changed_plan_does_not_inherit_an_approval(self):
        plan = self.plan()
        view = self.file(plan)["decision"]
        decisions.answer(self.cfg, self.db, view["id"], "approve",
                         human=ALICE, channel="cli", now=self.now + 1)
        outcome = plan_decision(self.db, self.plan(question="Contenu changé ?"),
                                now=self.now + 2)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["reason"]),
                         ("not_approved", False, "plan_content_changed"))

    def test_absent_plan_has_no_implicit_approval(self):
        outcome = plan_decision(self.db, self.plan(), now=self.now)
        self.assertEqual((outcome["state"], outcome["approved"], outcome["decision"],
                          outcome["reason"]), ("absent", False, None, "no_request"))

    def test_an_unbounded_repair_receipt_is_refused_before_any_write(self):
        for receipt in ({"key": "plan-1"}, {"key": "plan-1", "digest": "not-a-digest"},
                        {"key": "plan\n1", "digest": "a" * 64}):
            with self.subTest(receipt=receipt), self.assertRaises(decisions.DecisionError):
                decisions.ask(self.cfg, self.db, requester="orch",
                              question="Une réparation ?", lot=str(self.lot["id"]),
                              repair_plan=receipt)
        self.assertEqual(self.count_requests(), 0)

    def test_a_plan_for_another_project_or_with_a_past_deadline_is_refused(self):
        other = work.add(self.db, title="Autre projet", assignee="orch", app="nexlink")
        with self.assertRaises(RepairDecisionError):
            self.file(self.plan(lot=other["id"]))
        with self.assertRaises(RepairDecisionError):
            self.file(self.plan(due_ts=self.now - 10))
        self.assertEqual(self.count_requests(), 0)

    def test_two_concurrent_calls_file_one_request(self):
        plan = self.plan()
        barrier = threading.Barrier(2)
        results, failures = [], []

        def run():
            from ameesh import db as db_mod
            connection = db_mod.connect(self.cfg)
            try:
                barrier.wait(timeout=10)
                results.append(file_plan_decision(self.cfg, connection, plan,
                                                  requester="orch", now=self.now))
            except BaseException as exc:  # noqa: BLE001 — reported by the thread
                failures.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse([t for t in threads if t.is_alive()])
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({r["decision"]["id"] for r in results}), 1)
        self.assertEqual(sorted(r["created"] for r in results), [False, True])
        self.assertEqual(self.count_requests(), 1)
