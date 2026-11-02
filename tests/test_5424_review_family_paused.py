# SPDX-License-Identifier: AGPL-3.0-only
import unittest
from ameesh import review_designation as rd, review_family_paused as pause, exploitation
from tests.test_4779_reviewer_profile import agent_row
from tests.test_3292_debit import H, T, event, frozen, item, run, verdict
from tests.test_5430_debit_waiting_mark import waiting_mark, HEAD, FAMILIES


def registry():
    rows = [agent_row("author", harness="deepseek"), agent_row("reviewer", harness="claude")]
    for name in ("cx1", "cx2"):
        row = agent_row(name, harness="codex", status="blocked")
        row.update(session_account="primary", status_text=
                   "budget : pause (account primary, seven_day 80%%) until reset@%d; cap" % (T+H))
        rows.append(row)
    return rows


class FamilyPausedTest(unittest.TestCase):
    def inputs(self, ident=1):
        return (item(ident, assignee="author"), frozen(ident, ident+100, T-4*H, sha=HEAD),
                event(ident, ident+200, T-4*H, waiting_mark(ident, ident+100)))

    def alerts(self, rows=None):
        inputs = [self.inputs(1), self.inputs(2)]
        return pause.alerts([i[0] for i in inputs], [i[1] for i in inputs],
                            [i[2] for i in inputs], registry() if rows is None else rows,
                            families=FAMILIES, now=T)

    def test_one_alert_for_two_lots_and_no_paused_designation(self):
        alerts = self.alerts()
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["family"], "codex")
        self.assertEqual(alert["resume_ts"], T+H)
        self.assertEqual(alert["lots"], [1, 2])
        self.assertIn("primary", alert["detail"])
        candidates = rd.candidates_from_registry(registry(), project="ameesh", families=FAMILIES)
        result = rd.select(required=2, authors=[{"name":"author", "family":"deepseek"}],
                           candidates=candidates, families=FAMILIES)
        self.assertEqual([r["name"] for r in result["reviewers"]], ["reviewer"])
        self.assertEqual(result["state"], "waiting")

    def test_available_fallback_removes_family_alert(self):
        rows = registry() + [agent_row("fallback", harness="codex")]
        self.assertEqual(self.alerts(rows), [])

    def test_operator_pause_is_not_a_budget_episode(self):
        rows = registry()
        rows[-1]["operator_paused"] = True
        self.assertEqual(self.alerts(rows), [])

    def test_lot_changes_do_not_change_episode_key(self):
        alert = self.alerts()[0]
        changed = dict(alert, lots=[], detail="changed")
        self.assertEqual(exploitation.alert_key(alert), exploitation.alert_key(changed))

    def test_removing_a_projects_last_lot_keeps_the_family_episode(self):
        rows = registry()
        for row in registry():
            row = dict(row, name=row["name"] + "-nxl", team="nexlink")
            if row["harness"] == "codex":
                row["status_text"] = row["status_text"].replace("primary", "secondary")
            rows.append(row)
        first, second = self.inputs(1), self.inputs(2)
        second[0]["app"] = "nexlink"
        both = pause.alerts([first[0], second[0]], [first[1], second[1]],
                            [first[2], second[2]], rows, families=FAMILIES, now=T)
        one = pause.alerts([first[0]], [first[1]], [first[2]], rows,
                           families=FAMILIES, now=T)
        self.assertEqual(len(both), 1)
        self.assertEqual(exploitation.alert_key(both[0]), exploitation.alert_key(one[0]))

    def test_alert_carries_the_contract_schema(self):
        # #5424 N1 : le contrat `ameesh-alert/1` de tous les producteurs
        self.assertEqual(self.alerts()[0]["schema"], exploitation.ALERT_SCHEMA)

    def test_held_lot_names_its_issue(self):
        row, milestone, ev = self.inputs()
        row["issue_ref"] = "manaty/ameesh#4070"
        alert = pause.alerts([row], [milestone], [ev], registry(), families=FAMILIES, now=T)[0]
        self.assertEqual(alert["held_lots"][0]["issue_ref"], "manaty/ameesh#4070")
        self.assertIn("manaty/ameesh#4070", alert["detail"])

    def test_pilot_and_closed_host_do_not_mask_the_pause(self):
        # #5424 N6 : le pilote et un hôte fermé sont hors du vivier désigné
        rows = registry() + [agent_row("pilot", harness="codex"),
                             dict(agent_row("drained", harness="codex"), host="h-lost")]
        self.assertEqual(self.alerts(rows), [])
        row, milestone, ev = self.inputs()
        alerts = pause.alerts([row], [milestone], [ev], rows, families=FAMILIES, now=T,
                              pilots={"ameesh": "pilot"}, closed_hosts={"h-lost"})
        self.assertEqual([alert["family"] for alert in alerts], ["codex"])
        self.assertEqual({p["name"] for p in alerts[0]["pauses"]}, {"cx1", "cx2"})

    def test_debit_names_the_wait(self):
        row, milestone, ev = self.inputs()
        result = run(items=[row], milestones=[milestone], events=[ev], agents=registry(),
                     families=FAMILIES)
        self.assertEqual(result["qa"][0]["review_family_paused"][0]["family"], "codex")
        self.assertIn("review_family_paused", [w["kind"] for w in result["slow_waits"]])

    def test_expired_pause_does_not_claim_future_capacity_loss(self):
        rows = registry()
        for row in rows[-2:]:
            row["status_text"] = row["status_text"].replace(str(int(T+H)), str(int(T-1)))
        self.assertEqual(self.alerts(rows), [])

    def test_short_window_keeps_candidates_and_has_no_family_alert(self):
        rows = registry()
        for row in rows[-2:]:
            row["status_text"] = "budget : account primary: 2/2 concurrent turns (provider burst bucket)"
        self.assertEqual(self.alerts(rows), [])

    def test_vote_from_paused_family_still_counts(self):
        row, milestone, ev = self.inputs()
        marks = [milestone, verdict(1, 103, T-60, "cx1", "ok", sha=HEAD)]
        self.assertEqual(pause.alerts([row], marks, [ev], registry(),
                                      families=FAMILIES, now=T), [])

    def test_brief_goes_to_pilot_and_bureau(self):
        from ameesh import alert_briefs
        alert = dict(self.alerts()[0], orchestrators=["pilot"])
        listing = registry() + [dict(agent_row("pilot"), lease_live=True),
                                dict(agent_row("bureau"), lease_live=True)]
        briefs = alert_briefs.briefs(alert, listing)
        self.assertEqual(set(briefs), {"pilot", "bureau"})
        self.assertIn("primary", briefs["pilot"])
        self.assertIn("decision", briefs["bureau"])

    def test_no_canon_families_proves_no_pause(self):
        row, milestone, ev = self.inputs()
        self.assertEqual(pause.alerts([row], [milestone], [ev], registry(),
                                      families=(), now=T), [])


from tests import test_3236_brief_fenetre as delivery_fixture


from tests.support import PgTestCase


class FamilyEpisodeDeliveryTest(PgTestCase):
    def setUp(self):
        super().setUp()
        import time
        from ameesh import registry
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET team = 'ameesh', lease_owner = 'runner',"
                        " lease_expires_at = now() + interval '1 hour' WHERE name = 'orch'")
        self.now = time.time()
    mails = delivery_fixture.PlanUnderusedBriefWindowTest.mails
    notifier = delivery_fixture.PlanUnderusedBriefWindowTest.notifier
    def test_one_delivery_per_episode_even_after_six_hours_and_a_gap(self):
        alert = FamilyPausedTest().alerts()[0]
        alert.update(orchestrators=["orch"], resume_ts=self.now+48*H)
        alert["episode"] = [("primary", self.now+48*H)]
        notifier, clock = self.notifier()
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        clock[0] += 7*H
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        notifier.run_pass(self.db, current=[])
        notifier.run_pass(self.db, current=[dict(alert, lots=[1, 2, 3])])
        self.assertEqual(len(self.mails("orch")), 1)
        changed = dict(alert, episode=[("primary", self.now+72*H)])
        notifier.run_pass(self.db, current=[changed])
        self.assertEqual(len(self.mails("orch")), 2)

    def test_episode_without_deadline_is_briefed_again_after_six_hours(self):
        # #5424 N4 : sans échéance, l'entrée ne rend pas muette une pause ultérieure
        alert = dict(FamilyPausedTest().alerts()[0], orchestrators=["orch"], resume_ts=None)
        alert["episode"] = [("primary", None)]
        notifier, clock = self.notifier()
        notifier.run_pass(self.db, current=[alert])
        notifier.run_pass(self.db, current=[])
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 1)
        clock[0] += 7*H
        notifier.run_pass(self.db, current=[])
        notifier.run_pass(self.db, current=[alert])
        self.assertEqual(len(self.mails("orch")), 2)

    def test_open_lots_read_carries_issue_ref(self):
        from ameesh import storage, work
        lot = work.add(self.db, title="held", app="ameesh", issue_ref="manaty/ameesh#1")
        rows = storage.of(self.db).operations.open_lots_activity(None)
        row = next(row for row in rows if int(row["id"]) == int(lot["id"]))
        self.assertEqual(row["issue_ref"], "manaty/ameesh#1")

    def test_registry_readers_supply_pause_and_profile_evidence(self):
        from ameesh import debit, storage
        self.db.execute("UPDATE agent_registry SET status = 'blocked',"
                        " status_text = %s WHERE name = 'orch'",
                        ("budget : pause (account primary, seven_day 80 %%) until reset@%d; cap"
                         % int(self.now + H),))
        for rows in (debit._rows(self.db, debit._AGENTS_SQL),
                     storage.of(self.db).operations.listing()):
            row = next(row for row in rows if row["name"] == "orch")
            self.assertTrue(row["status_text"].startswith("budget : "))
            for field in ("capabilities", "roles", "relit", "session_harness", "session_account"):
                self.assertIn(field, row)
