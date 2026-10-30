# SPDX-License-Identifier: AGPL-3.0-only
"""#4256: deadlines, authority substitution, private facts and immutable winners."""
from __future__ import annotations

import datetime as dt
import dataclasses
import json
import unittest
import threading
from unittest import mock

from . import support
from .support import PgTestCase
from ameesh import authority, db as db_mod, decisions, registry, signing, storage, validation_deadlines as vd, work


def ts(value):
    return dt.datetime.fromisoformat(value).timestamp()


class CalendarTest(unittest.TestCase):
    def test_weekend_and_recipient_zone(self):
        start = ts("2026-10-30T15:00:00+08:00")
        self.assertEqual(vd.stamp(vd.business_due(start, "Asia/Manila"), "Asia/Manila"),
                         "2026-11-02T15:00:00+08:00")

    def test_dst_preserves_local_wall_time(self):
        start = ts("2026-10-23T15:00:00+02:00")
        self.assertEqual(vd.stamp(vd.business_due(start, "Europe/Paris"), "Europe/Paris"),
                         "2026-10-26T15:00:00+01:00")


class ValidationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "requester", harness="codex", host=self.cfg.host,
                        chantier="acme")
        self.db.execute("UPDATE agent_registry SET responsible = 'human:owner'"
                        " WHERE name = 'requester'")
        self.cfg = dataclasses.replace(self.cfg, project_planning={"acme": {"validations": {
            "owner": "human:owner", "owner_timezone": "UTC",
            "domains": {"ready": [{"human": "human:tpm", "timezone": "Asia/Manila"},
                                    {"human": "human:po", "timezone": "Europe/Paris"}]}}}})
        self.lot = work.add(self.db, title="Validation fixture", assignee="requester", app="acme")
        work.move(self.db, self.lot["id"], "build")
        self.now = ts("2026-10-29T09:00:00+00:00")

    def ask(self, **kwargs):
        return decisions.ask(self.cfg, self.db, requester="requester",
                             lot=str(self.lot["id"]), question="Accept the specification?",
                             options=["a=Accept", "b=Revise"], domain="ready", now=self.now,
                             issue_links=["https://example.org/issues/7"], **kwargs)

    def answer(self, view, text="a", human="human:tpm", now=None):
        return decisions.answer(self.cfg, self.db, view["id"], text, human=human,
                                channel="cli", now=now or self.now + 1)

    def test_default_deadline_is_announced_and_journalled(self):
        view = self.ask()
        self.assertEqual(view["owner"], "human:tpm")
        self.assertEqual(view["due_ts"], vd.business_due(self.now, "Asia/Manila"))
        row = storage.of(self.db).decisions.get(view["id"])
        self.assertIn("2026-10-30T17:00:00+08:00", row["body"])
        self.assertIn("https://example.org/issues/7", row["body"])
        audit = storage.of(self.db).decisions.validation_history(person="human:tpm")
        self.assertTrue(audit[0]["facts"]["defaulted"])

    def test_explicit_deadline_preserved(self):
        view = self.ask(due_ts=self.now + 7200)
        self.assertEqual(view["due_ts"], self.now + 7200)
        self.assertFalse(view["validation"]["defaulted"])

    def test_single_escalation_uses_only_next_authority(self):
        view = self.ask(due_ts=self.now + 60)
        self.assertEqual(vd.sweep(self.cfg, self.db, now=self.now + 59), [])
        self.assertEqual(vd.sweep(self.cfg, self.db, now=self.now + 60),
                         [{"id": view["id"], "status": "escalated"}])
        self.assertEqual(vd.sweep(self.cfg, self.db, now=self.now + 120), [])
        current = decisions.get(self.db, view["id"])
        self.assertEqual(current["owner"], "human:po")
        self.assertEqual(current["validation"]["status"], "escalated")
        with self.assertRaises(decisions.DecisionError):
            self.answer(current, human="human:stranger")
        self.answer(current, human="human:po", now=self.now + 61)
        self.assertEqual(vd.sweep(self.cfg, self.db, now=self.now + 130), [])

    def test_ordinary_decision_keeps_resolved_recipient_and_zone(self):
        settings = self.cfg.project_planning["acme"]["validations"]
        settings["timezone"] = "Asia/Manila"
        settings["owner_timezone"] = "Europe/Paris"
        self.db.execute("UPDATE agent_registry SET responsible = 'human:client'"
                        " WHERE name = 'requester'")
        view = decisions.ask(self.cfg, self.db, requester="requester",
                             lot=str(self.lot["id"]), question="Ordinary decision?", now=self.now)
        self.assertEqual(view["owner"], "human:client")
        self.assertEqual(view["validation"]["timezone"], "Asia/Manila")
        self.assertEqual(view["due_ts"], vd.business_due(self.now, "Asia/Manila"))

    def test_owner_recipient_uses_owner_timezone(self):
        self.cfg.project_planning["acme"]["validations"]["owner_timezone"] = "Europe/Paris"
        view = decisions.ask(self.cfg, self.db, requester="requester",
                             lot=str(self.lot["id"]), question="Owner decision?", now=self.now)
        self.assertEqual(view["owner"], "human:owner")
        self.assertEqual(view["validation"]["timezone"], "Europe/Paris")
        self.assertEqual(view["due_ts"], vd.business_due(self.now, "Europe/Paris"))
        self.assertIn(vd.stamp(view["due_ts"], "Europe/Paris"),
                      storage.of(self.db).decisions.get(view["id"])["body"])

    def test_escalation_records_recipient_request_once(self):
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        vd.sweep(self.cfg, self.db, now=self.now + 62)
        rows = storage.of(self.db).decisions.validation_history(person="human:po")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "requested")
        self.assertEqual(rows[0]["facts"]["requested_ts"], self.now + 61)
        self.assertEqual(rows[0]["facts"]["escalated_from"], "human:tpm")

    def test_escalated_request_says_so_to_the_substitute(self):
        """#4542 N1: the substitute sees why an overdue request reaches them."""
        view = self.ask(due_ts=self.now + 60)
        self.assertNotIn("ESCALATED", decisions.format_view(view, self.now)[0])
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        current = decisions.get(self.db, view["id"], self.now + 62)
        self.assertIn("ESCALATED", storage.of(self.db).decisions.get(view["id"])["body"])
        self.assertIn("[ESCALATED]", decisions.format_view(current, self.now + 62)[0])
        self.assertIn("(escalated)", decisions.describe(current))

    def test_withdrawal_closes_each_journalled_request(self):
        """#4542 N2: a withdrawal is the outcome of the initial and the
        substitute's requests, once even on a replay."""
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        decisions.withdraw(self.cfg, self.db, view["id"], requester="requester",
                           why="obsolete", now=self.now + 70)
        for person in ("human:tpm", "human:po"):
            kinds = [r["kind"] for r in
                     storage.of(self.db).decisions.validation_history(person=person)]
            self.assertEqual(kinds.count("withdrawn"), 1, (person, kinds))
            row = [r for r in storage.of(self.db).decisions.validation_history(person=person)
                   if r["kind"] == "withdrawn"][0]
            self.assertEqual(row["facts"]["withdrawn_by"], "requester")
            self.assertNotIn("obsolete", json.dumps(row["facts"]))

    def test_early_response_cancels_escalation(self):
        view = self.ask(due_ts=self.now + 60)
        self.answer(view)
        self.assertEqual(vd.sweep(self.cfg, self.db, now=self.now + 61), [])
        self.assertFalse(any(r["kind"] == "missed" for r in
                             storage.of(self.db).decisions.validation_history(person="human:tpm")))

    def test_timely_winner_stays_timely_after_replay_and_contradiction(self):
        view = self.ask(due_ts=self.now + 60)
        self.answer(view, now=self.now + 10)
        original = decisions.get(self.db, view["id"])["answer"]
        self.answer(view, now=self.now + 70)
        followup = self.answer(view, text="b", now=self.now + 80)
        self.assertEqual(followup["relation"], "contradicts")
        self.assertEqual(decisions.get(self.db, view["id"])["answer"], original)
        audit = storage.of(self.db).decisions.validation_history(person="human:tpm")
        self.assertNotIn("missed", [event["kind"] for event in audit])
        self.assertIn("late_response", [event["kind"] for event in audit])

    def test_no_substitute_goes_to_owner(self):
        self.cfg.project_planning["acme"]["validations"]["domains"]["ready"].pop()
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        current = storage.of(self.db).decisions.get(view["id"])
        self.assertEqual(current["recipient"], "human:owner")
        self.assertIn("name an authorized substitute", current["body"])

    def test_initial_late_response_can_win_after_escalation(self):
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        self.answer(view, now=self.now + 62)
        winner = decisions.get(self.db, view["id"])["answer"]
        self.assertEqual(winner["by"], "human:tpm")
        audit = storage.of(self.db).decisions.validation_history(person="human:tpm")
        self.assertIn("late_response", [r["kind"] for r in audit])

    def test_contradictory_late_response_opens_one_typed_followup(self):
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        self.answer(view, human="human:po", now=self.now + 62)
        first = self.answer(view, text="b", now=self.now + 63)
        again = self.answer(view, text="b", now=self.now + 64)
        self.assertEqual(first["relation"], "contradicts")
        self.assertEqual(first["followup"], again["followup"])
        self.assertEqual(decisions.get(self.db, view["id"])["answer"]["option"], "a")
        self.assertEqual(decisions.get(self.db, first["followup"])["state"], "pending")
        late = [r for r in storage.of(self.db).decisions.validation_history(person="human:tpm")
                if r["kind"] == "late_response"]
        self.assertEqual(len(late), 1)

    def test_late_contradiction_survives_merged_lot_and_retries(self):
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        self.answer(view, human="human:po", now=self.now + 62)
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (self.lot["id"],))
        for offset in (63, 64):
            result = self.answer(view, text="b", now=self.now + offset)
            self.assertTrue(result["followup_pending"])
        late = [r for r in storage.of(self.db).decisions.validation_history(person="human:tpm")
                if r["kind"] == "late_response"]
        self.assertEqual(len(late), 1)
        pending = [r for r in storage.of(self.db).decisions.validation_history(person="human:tpm")
                   if r["kind"] == "followup_pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["facts"]["relation"], "contradicts")
        self.assertEqual(decisions.get(self.db, view["id"])["answer"]["option"], "a")

    def test_long_late_reply_is_preserved_and_followup_fits(self):
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        self.answer(view, human="human:po", now=self.now + 62)
        text = "x" * decisions.MAX_ANSWER
        result = self.answer(view, text=text, now=self.now + 63)
        self.assertTrue(result["followup"])
        late = [r for r in storage.of(self.db).decisions.validation_history(person="human:tpm")
                if r["kind"] == "late_response"]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["facts"]["text"], text)

    def test_agent_cannot_read_private_history(self):
        self.ask()
        with mock.patch.object(decisions, "answering_human",
                               side_effect=decisions.DecisionError("agent session")):
            with self.assertRaises(decisions.DecisionError):
                vd.history(self.cfg, self.db, person="human:tpm", key_path="missing", signer="owner")
        with mock.patch.object(decisions, "answering_human"):
            with self.assertRaises(decisions.DecisionError):
                vd.history(self.cfg, self.db, person="human:tpm", key_path="missing", signer="stranger")

    def test_public_projection_has_no_audit_or_route(self):
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        public = json.dumps(decisions.get(self.db, view["id"]))
        self.assertNotIn("requested_ts", public)
        self.assertNotIn("missed", public)
        self.assertNotIn('"nodes"', public)

    def test_private_history_requires_the_live_owner_private_key(self):
        self.ask()
        registry.upsert(self.db, "owner", harness="other", host=self.cfg.host)
        private, public, _ = self.make_test_key("owner")
        authority.register_key(self.db, "owner", signing.read_public(public), role="owner")
        with mock.patch.object(decisions, "answering_human"):
            rows = vd.history(self.cfg, self.db, person="human:tpm", key_path=private,
                              signer="owner")
            self.assertEqual(rows[0]["kind"], "requested")
            wrong, _, _ = self.make_test_key("wrong")
            with self.assertRaises(decisions.DecisionError):
                vd.history(self.cfg, self.db, person="human:tpm", key_path=wrong, signer="owner")
            authority.revoke_key(self.db, "owner")
            with self.assertRaises(decisions.DecisionError):
                vd.history(self.cfg, self.db, person="human:tpm", key_path=private, signer="owner")

    def test_concurrent_hosts_escalate_only_once(self):
        view = self.ask(due_ts=self.now + 60)
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run():
            connection = db_mod.connect(self.cfg)
            try:
                barrier.wait(10)
                results.extend(vd.sweep(self.cfg, connection, now=self.now + 61))
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, [{"id": view["id"], "status": "escalated"}])
        missed = [r for r in storage.of(self.db).decisions.validation_history(person="human:tpm")
                  if r["kind"] == "missed"]
        self.assertEqual(len(missed), 1)

    def test_legacy_rows_can_still_be_answered(self):
        row = storage.of(self.db).decisions.create(
            sender="requester", recipient="human:tpm", host=self.cfg.host,
            work_item_id=str(self.lot["id"]), body="Legacy request",
            payload={"decision": {"state": "pending", "lot": self.lot["id"],
                                  "options": [], "due_ts": self.now + 60}})
        view = decisions.get(self.db, row["id"])
        self.answer(view, text="accepted")
        self.assertEqual(decisions.get(self.db, row["id"])["state"], "answered")

    def test_concurrent_authorized_answers_keep_one_winner(self):
        view = self.ask(due_ts=self.now + 60)
        vd.sweep(self.cfg, self.db, now=self.now + 61)
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run(human, text):
            connection = db_mod.connect(self.cfg)
            try:
                barrier.wait(10)
                results.append(decisions.answer(self.cfg, connection, view["id"], text,
                                                 human=human, channel="cli", now=self.now + 62))
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=run, args=("human:tpm", "a"), daemon=True),
                   threading.Thread(target=run, args=("human:po", "b"), daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(bool(r.get("followup")) for r in results), 1)
        winner = decisions.get(self.db, view["id"])["answer"]
        self.assertIn((winner["by"], winner["option"]),
                      (("human:tpm", "a"), ("human:po", "b")))

    def test_legacy_queue_prefix_cannot_hide_a_due_validation(self):
        self.db.execute(
            "INSERT INTO agent_mailbox(sender, recipient, body, kind, payload)"
            " SELECT 'requester', 'human:owner', 'legacy', 'request',"
            " jsonb_build_object('decision', jsonb_build_object('due_ts', %s))"
            " FROM generate_series(1, 501)", (self.now + 60,))
        view = self.ask(due_ts=self.now + 60)
        self.assertEqual(vd.sweep(self.cfg, self.db, now=self.now + 61),
                         [{"id": view["id"], "status": "escalated"}])

    def test_bad_decision_does_not_suppress_the_next_escalation(self):
        first = self.ask(due_ts=self.now + 60)
        second = self.ask(due_ts=self.now + 60)
        self.db.execute(
            "UPDATE decision_validations SET routing = jsonb_set("
            "routing, '{nodes,1,timezone}', %s::jsonb) WHERE decision_id = %s",
            (json.dumps("Invalid/PrivateTimezone"), first["id"]))
        with self.assertLogs(vd.LOGGER, level="WARNING") as logs:
            changed = vd.sweep(self.cfg, self.db, now=self.now + 61)
        self.assertEqual(changed, [{"id": second["id"], "status": "escalated"}])
        self.assertEqual(decisions.get(self.db, first["id"])["owner"], "human:tpm")
        self.assertEqual(decisions.get(self.db, second["id"])["owner"], "human:po")
        self.assertIn("#%s" % first["id"], logs.output[0])
        self.assertNotIn("Invalid/PrivateTimezone", " ".join(logs.output))
        self.assertIsNone(storage.of(self.db).decisions.validation_route(first["id"]).get("escalated_to"))

    @mock.patch("ameesh.migrations.require_compatible_writer", new=lambda db: None)
    def test_runner_notifier_safety_brief_survives_sweep_failure(self):
        from types import SimpleNamespace
        from ameesh import notify_gap, runner
        fake = SimpleNamespace(cfg=self.cfg, dry_run=False, mediated=False, db=None)
        report = {"sent": [], "failed": []}
        with mock.patch.object(vd, "sweep", side_effect=RuntimeError("private payload")), \
                mock.patch.object(notify_gap, "brief_once", return_value=report) as brief, \
                mock.patch.object(runner, "log_async") as log:
            result = runner.Runner.notify_health_once(fake, db=self.db, now=self.now)
        self.assertIs(result, report)
        brief.assert_called_once_with(self.cfg, self.db, now=self.now)
        log.assert_called_once_with("validation deadlines: sweep failed (RuntimeError)")

    def test_malformed_deadline_is_isolated_before_query_cast(self):
        first = self.ask(due_ts=self.now + 60)
        second = self.ask(due_ts=self.now + 60)
        self.db.execute(
            "UPDATE agent_mailbox SET payload = jsonb_set("
            "payload, '{decision,due_ts}', %s::jsonb) WHERE id = %s",
            (json.dumps("invalid private deadline"), first["id"]))
        with self.assertLogs(vd.LOGGER, level="WARNING") as logs:
            changed = vd.sweep(self.cfg, self.db, now=self.now + 61)
        self.assertEqual(changed, [{"id": second["id"], "status": "escalated"}])
        self.assertNotIn("invalid private deadline", " ".join(logs.output))
