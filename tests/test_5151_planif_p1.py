# SPDX-License-Identifier: AGPL-3.0-only
"""#5151 — un P1 n'est pas daté au lendemain derrière des lots qui attendent
autrui, et la date du planificateur n'endort pas l'agent qui le porte.

Témoins du lot :

* la file de cx2 du 31/10 rejouée : #5111 (qa) et #4972 (validated)
  n'occupent pas la file, #5145 (P1) est daté du jour ;
* un P1 passe devant un lot de priorité inférieure, même en cours ;
* un début futur posé par le planificateur ne vaut pas attente pour un P1
  (`planned_waits`, `operations.listing`), une date posée à la main si ;
* le sommeil épargne l'agent d'un tel P1 et, quand il endort, nomme au
  journal le lot écarté et sa raison.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
import re
import time
import unittest
from unittest import mock

from ameesh import file_decoupage as fd
from ameesh import lot_schedule, mechanics, planned_waits, registry, storage, work
from ameesh.storage.postgres import work_dependencies as pg_deps

from .support import PgTestCase

# un midi local fixe : les jours ne dépendent pas du fuseau de la machine
NOW = dt.datetime(2026, 10, 31, 12, 0).timestamp()
H = 3600.0
BODY = {"schema": "ameesh-estimate-reference/1", "min_lots": 3, "groups": [
    {"level": "type", "type": "bug", "lots": 5, "median_minutes": 360.0,
     "p80_minutes": 600.0}]}
SCHEDULED = "reference p80 (table #16)"


def _day(ts):
    return dt.date.fromtimestamp(ts).isoformat()


def _tomorrow():
    return (dt.date.today() + dt.timedelta(days=1)).isoformat()


class QueueTest(unittest.TestCase):

    def test_cx2_queue_of_31_10_replayed(self):
        lots = [{"id": 5111, "type": "bug", "state": "qa", "started_ts": NOW - 5 * H},
                {"id": 4972, "type": "bug", "state": "validated",
                 "started_ts": NOW - 3 * H},
                {"id": 5145, "type": "bug", "state": "intake", "priority": 1}]
        out = lot_schedule.plan_queue(lots, {}, BODY, {}, now=NOW)
        by = {e["id"]: e for e in out}
        self.assertEqual(by[5145]["start_ts"], NOW)
        self.assertEqual(by[5145]["planned_start"], _day(NOW))
        # même en P2 : les deux lots attendent autrui, la file est libre
        lots[2] = dict(lots[2], priority=2)
        by = {e["id"]: e for e in lot_schedule.plan_queue(lots, {}, BODY, {}, now=NOW)}
        self.assertEqual(by[5145]["start_ts"], NOW)

    def test_lots_waiting_on_others_do_not_hold_the_queue(self):
        for state in ("qa", "validated", "waiting_human", "blocked"):
            lots = [{"id": 1, "type": "bug", "state": state, "started_ts": NOW - H},
                    {"id": 2, "type": "bug", "state": "intake"}]
            by = {e["id"]: e for e in lot_schedule.plan_queue(lots, {}, BODY, {}, now=NOW)}
            self.assertEqual(by[2]["start_ts"], NOW, state)
            # le lot en attente garde ses dates : début mesuré, fin au p80
            self.assertEqual(by[1]["end_ts"], NOW + 9 * H, state)
        # un lot en cours (build) occupe toujours la file
        lots = [{"id": 1, "type": "bug", "state": "build", "started_ts": NOW - H},
                {"id": 2, "type": "bug", "state": "intake"}]
        by = {e["id"]: e for e in lot_schedule.plan_queue(lots, {}, BODY, {}, now=NOW)}
        self.assertEqual(by[2]["start_ts"], NOW + 5 * H)

    def test_a_p1_goes_before_a_lower_priority_lot_in_progress(self):
        lots = [{"id": 1, "type": "bug", "state": "build", "started_ts": NOW - H},
                {"id": 2, "type": "bug", "state": "intake", "priority": 1},
                {"id": 3, "type": "bug", "state": "intake"}]
        out = lot_schedule.plan_queue(lots, {}, BODY, {}, now=NOW)
        self.assertEqual([e["id"] for e in out], [2, 1, 3])
        by = {e["id"]: e for e in out}
        self.assertEqual(by[2]["start_ts"], NOW)
        # le lot en cours garde son début ; son reste (9 h au p80, 5 h à la
        # médiane) passe après la médiane du P1 (6 h)
        self.assertEqual((by[1]["start_ts"], by[1]["end_ts"]), (NOW - H, NOW + 15 * H))
        self.assertEqual(by[3]["start_ts"], NOW + 11 * H)

    def test_a_p1_dated_by_hand_keeps_its_date(self):
        lots = [{"id": 1, "type": "bug", "state": "intake", "priority": 1},
                {"id": 2, "type": "bug", "state": "intake"}]
        plans = {1: {"planned_start": "2030-01-01", "planned_source": "set by orch"}}
        by = {e["id"]: e for e in lot_schedule.plan_queue(lots, plans, BODY, {}, now=NOW)}
        self.assertEqual(by[1]["planned_start"], "2030-01-01")
        self.assertEqual(by[2]["start_ts"], NOW)


class PlannedWaitTest(unittest.TestCase):

    def resolve(self, row, plan):
        return planned_waits.resolve([row], {int(row["id"]): plan}, (), {}, (), now=NOW)

    def test_a_scheduler_date_is_no_wait_for_a_p1(self):
        tomorrow = _day(NOW + 24 * H)
        row = {"id": 1, "state": "intake", "priority": 1}
        self.assertEqual(self.resolve(row, {"planned_start": tomorrow,
                                            "planned_source": SCHEDULED}), {})
        # posée à la main : une attente, même en P1
        wait = self.resolve(row, {"planned_start": tomorrow,
                                  "planned_source": "set by mesh-design"})[1]
        self.assertEqual((wait["what"], wait["start"]), ("scheduled", tomorrow))
        # un P2 daté par le planificateur attend sa date (règle inchangée)
        wait = self.resolve(dict(row, priority=2), {"planned_start": tomorrow,
                                                    "planned_source": SCHEDULED})[1]
        self.assertEqual(wait["what"], "scheduled")
        # sans priorité lue : la date vaut (sens prudent)
        self.assertEqual(self.resolve({"id": 1, "state": "intake"}, {
            "planned_start": tomorrow, "planned_source": SCHEDULED})[1]["what"],
            "scheduled")

    def test_the_storage_reads_the_same_source(self):
        self.assertEqual(pg_deps.SCHEDULE_SOURCE, lot_schedule.SOURCE)


SETTINGS = mechanics.QueueFill(repos_part=0.20, repos_s=1800, wake_mail_s=7200)


def agent(name):
    return {"name": name, "mode": "execute", "status": "idle", "lease_live": True,
            "lease_owner": "runner:h1", "unread": 0, "assigned_lot_id": None,
            "active_lots": 0, "actionable_lots": 0, "projects": ["ameesh"],
            "team": "ameesh", "harness": "deepseek", "responsible": "human:alice"}


class SleepTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1"):
            registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host,
                            cwd=self.tmp)
        self.db.execute("UPDATE agent_registry SET responsible = 'human:alice',"
                        " team = 'ameesh'")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}
        self.t0 = time.time()

    def run_pass(self, offset):
        return fd.run(None, self.db, now=self.t0 + offset, state=self.state,
                      listing=[agent("dev0"), agent("dev1")], canons=[],
                      settings=SETTINGS)

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def exhausted(self):
        self.run_pass(0)
        self.run_pass(1800)
        split = int(self.db.query("SELECT id FROM work_items WHERE source = %s",
                                  ("file-decoupage:ameesh",))[0]["id"])
        work.note(self.db, split, "roadmap: épuisée ameesh (test)", actor="pilote")
        return split

    def p1_dated_tomorrow(self, source):
        made = work.add(self.db, title="P1 daté demain", app="ameesh", actor="pilote")
        lot = int(made["id"])
        work.assign(self.db, lot, "dev1", actor="pilote")
        # #5198 : demain selon le jour local du processus, celui que lisent
        # `operations.listing` et le planificateur ; `current_date` suit le
        # fuseau de la session SQL et diffère d'un jour certains soirs.
        self.db.execute("UPDATE work_items SET priority = 1,"
                        " planned_start = %s, planned_source = %s"
                        " WHERE id = %s", (_tomorrow(), source, lot))
        return lot

    def listing_row(self, name):
        return next(r for r in storage.of(self.db).operations.listing()
                    if r["name"] == name)

    def test_a_p1_dated_by_the_scheduler_is_actionable_and_spares_its_agent(self):
        split = self.exhausted()
        lot = self.p1_dated_tomorrow(SCHEDULED)
        row = self.listing_row("dev1")
        self.assertEqual((row["actionable_lots"], row["actionable_lot_id"]), (1, lot))
        out = self.run_pass(1900)
        by_agent = {r["agent"]: r for g in out if g["kind"] == "sleep"
                    for r in g["agents"]}
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertIn("assigned actionable lot #%d" % lot, by_agent["dev1"]["why"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        self.assertFalse(any(n.startswith(fd.SLEEP_SET_ASIDE_PREFIX)
                             for n in self.notes(split)))

    def test_a_date_set_by_hand_still_waits_and_the_journal_names_it(self):
        split = self.exhausted()
        lot = self.p1_dated_tomorrow("set by mesh-design")
        self.assertEqual(self.listing_row("dev1")["actionable_lots"], 0)
        out = self.run_pass(1900)
        by_agent = {r["agent"]: r for g in out if g["kind"] == "sleep"
                    for r in g["agents"]}
        self.assertTrue(by_agent["dev1"]["slept"])
        tomorrow = _tomorrow()
        self.assertEqual(by_agent["dev1"]["set_aside"],
                         [{"lot": lot, "why": "planned start %s" % tomorrow}])
        self.assertIn("%s dev1: #%d (planned start %s)"
                      % (fd.SLEEP_SET_ASIDE_PREFIX, lot, tomorrow), self.notes(split))

    # -- B1 de ameesh-cx3 : la correction due par relecteur désigné ------
    def qa_lot(self, reviewers):
        lot = int(work.add(self.db, title="qa à deux relecteurs", app="ameesh",
                           actor="pilote")["id"])
        work.assign(self.db, lot, "dev1", actor="pilote")
        work.move(self.db, lot, "build")
        work.move(self.db, lot, "qa")
        work.milestone(self.db, lot, "frozen", sha="c" * 40, actor="dev1")
        self.db.execute("UPDATE work_items SET reviewers ="
                        " coalesce(string_to_array(nullif(%s, ''), ','), '{}')"
                        " WHERE id = %s", (",".join(reviewers), lot))
        return lot

    def verdict(self, lot, actor, kind):
        work.milestone(self.db, lot, "verdict", verdict=kind, sha="c" * 40, actor=actor)

    def test_a_block_then_another_reviewer_ok_keeps_the_author_actionable(self):
        from ameesh import review_designation_activation as rda
        split = self.exhausted()
        lot = self.qa_lot(["rev-a", "rev-b"])
        self.verdict(lot, "rev-a", "blocked")
        self.verdict(lot, "rev-b", "ok")
        self.assertEqual([r["lot"] for r in rda.due_fixes(self.db, "dev1")], [lot])
        row = self.listing_row("dev1")
        self.assertEqual((row["actionable_lots"], row["actionable_lot_id"]), (1, lot))
        out = self.run_pass(1900)
        by_agent = {r["agent"]: r for g in out if g["kind"] == "sleep"
                    for r in g["agents"]}
        self.assertFalse(by_agent["dev1"]["slept"])
        self.assertFalse(registry.get(self.db, "dev1")["operator_paused"])
        # le même relecteur lève son blocage : plus rien n'est dû
        self.verdict(lot, "rev-a", "ok")
        self.assertEqual(self.listing_row("dev1")["actionable_lots"], 0)
        self.assertIsNotNone(split)

    def test_without_seats_the_last_verdict_decides(self):
        lot = self.qa_lot([])
        self.verdict(lot, "rev-a", "blocked")
        self.assertEqual(self.listing_row("dev1")["actionable_lots"], 1)
        self.verdict(lot, "rev-b", "ok")
        self.assertEqual(self.listing_row("dev1")["actionable_lots"], 0)


class LocalDaySourceTest(unittest.TestCase):
    """#5198 : le jour local vient du processus (`date.today()`, celui du
    planificateur et de `operations.listing`), jamais du fuseau de la session
    SQL : en heure de Paris, `current_date` vaut déjà demain entre 23:00 et
    00:00 UTC et un lot daté d'aujourd'hui paraîtrait futur."""

    PATTERN = re.compile(r"\b(current_date|localtimestamp|now\(\)\s*::\s*date)\b",
                         re.IGNORECASE)

    def test_no_product_sql_reads_the_day_of_the_session(self):
        root = Path(__file__).resolve().parents[1] / "src" / "ameesh"
        found = [f"{path.relative_to(root)}:{n}"
                 for path in sorted(root.rglob("*")) if path.suffix in (".py", ".sql")
                 for n, line in enumerate(path.read_text().splitlines(), 1)
                 if self.PATTERN.search(line.split("--")[0].split("#")[0])]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
