# SPDX-License-Identifier: AGPL-3.0-only
"""#6643 — the auditor's hourly short pass is skipped when nothing moved.

Constat (#206650, décision owner #207135 a) : la passe horaire courte tourne
même quand rien n'a bougé, chaque passe ouvrant un tour complet. Attendu : le
déclencheur saute la passe quand `ameesh debit --check` sort 0 et qu'aucun
courrier n'est arrivé depuis la précédente ; le saut est journalisé, sans
courrier. Contre-examen (#207148) : jamais de saut si une échéance tombe dans
l'heure ni si une tête/porte (gel, fusion, CI, staging) a changé ; une lecture
en erreur fait jouer la passe.

Témoins :
* calme complet -> `skipped`, événement `auditor.pass`, aucun envoi ;
* un courrier -> `played`, un envoi ;
* `debit --check` 2 -> `played` ;
* échéance dans l'heure -> `played` ;
* changement de tête ou de porte -> `played` ;
* erreur de lecture du débit -> `played`.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import auditor_pass, events, role_timers

STATE = {"milestones": 41, "merged": 39, "ci_seen": 100.5,
         "ci_concluded": 90.25, "ci_running": 1, "staging": 7}
PREVIOUS = {"ts": 900.0,
            "data": {"agent": "auditeur", "started_ts": 900.0, "state": STATE}}
NOW = 1000.0
QUIET = {"totals": {"slow_waits": 0, "capacity_alerts": 0},
         "wakes": [], "external_waits": [], "window_waits": []}


def arrivals(count=0, undelivered=0):
    return {"arrivals": count, "undelivered": undelivered}


class FakeDb:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def query(self, sql, params=()):
        self.queries.append((sql, params))
        return [dict(self.row)]


class GateStateTest(unittest.TestCase):
    def test_the_state_is_read_in_one_indexed_statement(self):
        db = FakeDb({"milestones": 5, "merged": 3, "ci_seen": 1.5,
                     "ci_concluded": 2.5, "ci_running": 1, "staging": 9})
        self.assertEqual(auditor_pass.gate_state(db), {
            "milestones": 5, "merged": 3, "ci_seen": 1.5,
            "ci_concluded": 2.5, "ci_running": 1, "staging": 9})
        sql, params = db.queries[0]
        self.assertEqual(params, ())
        self.assertIn("work_item_milestones", sql)
        self.assertIn("staging.attested", sql)
        self.assertIn("ci_runs", sql)
        # a Python list would not bind through the psql fallback (#286)
        self.assertNotIn("ANY(", sql)


class MailArrivalsTest(unittest.TestCase):
    def test_the_pass_mail_is_excluded_and_the_cutoff_is_bound(self):
        db = FakeDb({"arrivals": 2, "undelivered": 1})
        self.assertEqual(auditor_pass.mail_arrivals(db, "auditeur", 900.0),
                         {"arrivals": 2, "undelivered": 1})
        sql, params = db.queries[0]
        self.assertEqual(params, (900.0, auditor_pass.PASS_TAG,
                                  auditor_pass.PASS_TAG, "auditeur"))
        # both filters ignore the pass mail: counting it would make every
        # played pass open the next one, and the skip would never happen
        self.assertEqual(sql.count("payload->>'pass'"), 2)


class QuickDecisionTest(unittest.TestCase):
    def test_the_first_pass_is_played(self):
        decision = auditor_pass.quick_decision(None, STATE, arrivals())
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, auditor_pass.FIRST_PASS)

    def test_a_read_error_is_played(self):
        for where in ("previous", "gate", "mail"):
            decision = auditor_pass.quick_decision(None, STATE, arrivals(),
                                                   read_error=where)
            self.assertFalse(decision.skip)
            self.assertEqual(decision.reason, auditor_pass.READ_ERROR)
            self.assertEqual(decision.detail["where"], where)

    def test_a_mail_is_played(self):
        decision = auditor_pass.quick_decision(PREVIOUS, STATE, arrivals(1))
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, auditor_pass.MAIL)
        self.assertEqual(decision.detail["arrivals"], 1)

    def test_an_undelivered_mail_is_played(self):
        decision = auditor_pass.quick_decision(PREVIOUS, STATE, arrivals(0, 1))
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, auditor_pass.MAIL)
        self.assertEqual(decision.detail["undelivered"], 1)

    def test_quiet_and_unchanged_asks_for_the_debit(self):
        self.assertIsNone(auditor_pass.quick_decision(PREVIOUS, STATE, arrivals()))

    def test_a_gate_change_is_played(self):
        changed = dict(STATE, milestones=STATE["milestones"] + 1)
        decision = auditor_pass.quick_decision(PREVIOUS, changed, arrivals())
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, auditor_pass.GATE)

    def test_an_unreadable_gate_is_played(self):
        decision = auditor_pass.quick_decision(PREVIOUS, None, arrivals())
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, auditor_pass.READ_ERROR)
        self.assertEqual(decision.detail["where"], "gate")


class DebitCheckTest(unittest.TestCase):
    def test_the_check_is_the_cli_one(self):
        self.assertEqual(auditor_pass.debit_check(QUIET), 0)
        self.assertEqual(auditor_pass.debit_check(
            {"totals": {"slow_waits": 3, "capacity_alerts": 0}}), 2)
        self.assertEqual(auditor_pass.debit_check(
            {"totals": {"slow_waits": 0, "capacity_alerts": 1}}), 2)
        self.assertIsNone(auditor_pass.debit_check(None))
        self.assertIsNone(auditor_pass.debit_check({}))
        self.assertIsNone(auditor_pass.debit_check({"totals": {"slow_waits": 0}}))


class DeadlineTest(unittest.TestCase):
    def test_a_wake_within_the_hour_is_a_deadline(self):
        report = dict(QUIET, wakes=[{"lot": 12, "due_ts": NOW + 600}])
        hit = auditor_pass.deadline_within(report, [], NOW, since=NOW - 3600)
        self.assertEqual(hit["section"], "wakes")
        self.assertEqual(hit["lot"], 12)

    def test_a_deadline_that_fell_since_the_previous_pass_counts(self):
        report = dict(QUIET, wakes=[{"lot": 12, "due_ts": NOW - 600}])
        hit = auditor_pass.deadline_within(report, [], NOW, since=NOW - 3600)
        self.assertIsNotNone(hit)

    def test_a_long_overdue_deadline_does_not_keep_the_pass_awake(self):
        report = dict(QUIET, external_waits=[{"lot": 12, "due_ts": NOW - 7200}])
        self.assertIsNone(
            auditor_pass.deadline_within(report, [], NOW, since=NOW - 3600))

    def test_a_deadline_beyond_the_hour_does_not_count(self):
        report = dict(QUIET, wakes=[{"lot": 12, "due_ts": NOW + 7200}])
        self.assertIsNone(
            auditor_pass.deadline_within(report, [], NOW, since=NOW - 3600))

    def test_the_reminder_and_observation_dates_count(self):
        report = dict(QUIET, external_waits=[{"lot": 1, "reminder_ts": NOW + 60}],
                      window_waits=[{"lot": 2, "due_ts": NOW + 120}])
        self.assertEqual(
            auditor_pass.deadline_within(report, [], NOW, since=NOW - 3600)["lot"], 1)
        report = dict(QUIET, window_waits=[{"lot": 2, "due_ts": NOW + 120}])
        self.assertEqual(
            auditor_pass.deadline_within(report, [], NOW, since=NOW - 3600)["lot"], 2)

    def test_an_owner_decision_due_in_the_hour_counts(self):
        hit = auditor_pass.deadline_within(
            QUIET, [{"lot": 3, "due_ts": NOW + 300}], NOW, since=NOW - 3600)
        self.assertEqual(hit["section"], "decisions")
        self.assertEqual(hit["lot"], 3)


class FinalDecisionTest(unittest.TestCase):
    def test_a_quiet_debit_skips(self):
        decision = auditor_pass.final_decision(
            QUIET, [], NOW, since=NOW - 3600)
        self.assertTrue(decision.skip)
        self.assertEqual(decision.reason, auditor_pass.QUIET)

    def test_an_unreadable_debit_is_played(self):
        decision = auditor_pass.final_decision(
            None, [], NOW, since=NOW - 3600)
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, auditor_pass.READ_ERROR)

    def test_a_deadline_is_played_before_the_debit_check(self):
        report = dict(QUIET, totals={"slow_waits": 5, "capacity_alerts": 0},
                      wakes=[{"lot": 1, "due_ts": NOW + 60}])
        decision = auditor_pass.final_decision(report, [], NOW, since=NOW - 3600)
        self.assertEqual(decision.reason, auditor_pass.DEADLINE)

    def test_debit_check_two_is_played(self):
        report = dict(QUIET, totals={"slow_waits": 5, "capacity_alerts": 0})
        decision = auditor_pass.final_decision(report, [], NOW, since=NOW - 3600)
        self.assertFalse(decision.skip)
        self.assertEqual(decision.reason, auditor_pass.DEBIT)
        self.assertEqual(decision.detail["check"], 2)

    def test_a_read_error_is_played(self):
        decision = auditor_pass.final_decision(
            QUIET, [], NOW, since=NOW - 3600, read_error="debit")
        self.assertEqual(decision.reason, auditor_pass.READ_ERROR)


class JournalTest(unittest.TestCase):
    def test_a_skip_is_an_event_without_any_mail(self):
        with mock.patch.object(events, "record", return_value=77) as record:
            self.assertEqual(
                auditor_pass.record(
                    None, "auditeur", auditor_pass._skipped(auditor_pass.QUIET),
                    started_ts=900.0, state=STATE, arrivals=arrivals()),
                77)
        args, kwargs = record.call_args
        self.assertEqual(args[1], auditor_pass.EVENT)
        self.assertEqual(args[2], "auditeur")
        self.assertEqual(kwargs["decision"], "skipped")
        self.assertEqual(kwargs["reason"], auditor_pass.QUIET)
        self.assertEqual(kwargs["state"], STATE)
        self.assertEqual(kwargs["mail"], 0)

    def test_the_played_mail_is_the_hourly_one(self):
        with mock.patch.object(auditor_pass.mail, "send", return_value=12) as send:
            self.assertEqual(
                auditor_pass.send_pass_mail(None, "auditeur", started_ts=1000.0), 12)
        args, kwargs = send.call_args
        self.assertEqual(args[1], "ameesh")
        self.assertEqual(args[2], "auditeur")
        self.assertEqual(args[3], role_timers.AUDITOR_MAIL)
        self.assertEqual(kwargs["kind"], "notify")
        self.assertEqual(kwargs["payload"], {"pass": auditor_pass.PASS_TAG})
        self.assertEqual(kwargs["idempotency_key"], "auditeur-court:0")


class MainTest(unittest.TestCase):
    def run_main(self, *, previous=PREVIOUS, state=STATE, mail=arrivals(),
                 report=QUIET, decisions=(), debit_error=None, argv=None):
        db = mock.Mock()
        db.query.return_value = [{"ts": NOW}]
        db.close = mock.Mock()
        debit_read = mock.Mock(return_value=report)
        if debit_error is not None:
            debit_read.side_effect = debit_error
        with mock.patch.object(auditor_pass.config_mod, "load"), \
                mock.patch.object(auditor_pass.db_mod, "connect", return_value=db), \
                mock.patch.object(auditor_pass.db_mod, "require_schema"), \
                mock.patch.object(auditor_pass, "previous_pass",
                                  return_value=previous), \
                mock.patch.object(auditor_pass, "gate_state", return_value=state), \
                mock.patch.object(auditor_pass, "mail_arrivals", return_value=mail), \
                mock.patch.object(auditor_pass.mail, "send", return_value=12) as send, \
                mock.patch.object(auditor_pass.events, "record", return_value=77) as record, \
                mock.patch("ameesh.debit.read", debit_read), \
                mock.patch("ameesh.decisions.pending", return_value=list(decisions)):
            status = auditor_pass.main(argv or ["--agent", "auditeur"])
        return status, send, record, debit_read

    def test_nothing_new_skips_and_journals_without_mail(self):
        status, send, record, debit = self.run_main()
        self.assertEqual(status, 0)
        send.assert_not_called()
        self.assertTrue(debit.called)
        args, kwargs = record.call_args
        self.assertEqual(kwargs["decision"], "skipped")
        self.assertEqual(kwargs["reason"], auditor_pass.QUIET)

    def test_a_first_pass_is_played(self):
        status, send, record, debit = self.run_main(previous=None)
        self.assertEqual(status, 0)
        send.assert_called_once()
        self.assertFalse(debit.called)
        self.assertEqual(record.call_args.kwargs["reason"], auditor_pass.FIRST_PASS)

    def test_a_mail_is_played_without_reading_the_debit(self):
        status, send, record, debit = self.run_main(mail=arrivals(1))
        self.assertEqual(status, 0)
        send.assert_called_once()
        self.assertFalse(debit.called)
        self.assertEqual(record.call_args.kwargs["reason"], auditor_pass.MAIL)

    def test_a_gate_change_is_played(self):
        status, send, record, debit = self.run_main(
            state=dict(STATE, staging=STATE["staging"] + 1))
        self.assertEqual(status, 0)
        send.assert_called_once()
        self.assertFalse(debit.called)
        self.assertEqual(record.call_args.kwargs["reason"], auditor_pass.GATE)

    def test_debit_two_is_played(self):
        report = dict(QUIET, totals={"slow_waits": 2, "capacity_alerts": 0})
        status, send, record, _ = self.run_main(report=report)
        self.assertEqual(status, 0)
        send.assert_called_once()
        self.assertEqual(record.call_args.kwargs["reason"], auditor_pass.DEBIT)

    def test_an_unreadable_debit_is_played(self):
        status, send, record, _ = self.run_main(debit_error=RuntimeError("boom"))
        self.assertEqual(status, 0)
        send.assert_called_once()
        self.assertEqual(record.call_args.kwargs["reason"], auditor_pass.READ_ERROR)
        self.assertEqual(record.call_args.kwargs["detail"]["where"], "debit")

    def test_a_dry_run_writes_nothing(self):
        status, send, record, _ = self.run_main(argv=["--agent", "auditeur",
                                                     "--dry-run", "--json"])
        self.assertEqual(status, 0)
        send.assert_not_called()
        record.assert_not_called()


class TimerTest(unittest.TestCase):
    def test_the_short_pass_timer_is_hourly(self):
        self.assertEqual(role_timers.AUDITOR_INTERVAL_S, 3600)

    def test_the_event_type_is_declared(self):
        self.assertIn(auditor_pass.EVENT, events.TYPES)


if __name__ == "__main__":
    unittest.main()
