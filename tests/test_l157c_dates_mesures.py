# SPDX-License-Identifier: AGPL-3.0-only
"""L157c: planned dates at the p80 of the reference table, queues ordered at
the median (`work schedule`, `work add --assignee`); actual time by phase
and merged diff size measured after the merge (`work measure`, runner).

Tests without a database (phases, size classes, gh and git parsing, queue
schedule), then on real Postgres (schedule written, dates set by a person
kept, measure stored and shown by `work show`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import lot_measures, lot_schedule, registry, roadmap, storage, work

from .support import PgTestCase

M = 60.0


def _ev(state, minute, i=0):
    return {"id": i, "state": state, "created_ts": minute * M}


def _ms(kind, minute):
    return {"kind": kind, "at_ts": minute * M}


class SplitTest(unittest.TestCase):

    def setUp(self) -> None:
        # build 0–30, waiting_human 30–50, build again; frozen at 100, back to
        # build at 130 (changes asked), frozen at 150, merged at 180
        self.events = [_ev("build", 0, 1), _ev("waiting_human", 30, 2), _ev("build", 50, 3),
                       _ev("build", 130, 4)]
        self.milestones = [_ms("frozen", 100), _ms("verdict", 125), _ms("frozen", 150)]

    def test_phases_by_priority(self):
        runs = [{"start_ts": 90 * M, "end_ts": 105 * M},
                {"start_ts": 140 * M, "end_ts": 155 * M}]
        out = lot_measures.split(0, 180 * M, events=self.events, milestones=self.milestones,
                                 ci_runs=runs)
        # human 30–50; review 100–130 and 150–180; CI 90–100 and 140–150
        # (the rest of the runs is in review); agent the rest
        self.assertEqual(out, {"total_minutes": 180.0, "agent_minutes": 80.0,
                               "ci_minutes": 20.0, "review_minutes": 60.0,
                               "human_minutes": 20.0, "ci_runs": 2,
                               "agent_source": "states"})

    def test_turns_are_the_agent_time_and_the_rest_is_unattended(self):
        """#796 : quand des tours sont attribués au lot, le temps d'agent est
        leur union — pas le reste de la chronologie d'états."""
        runs = [{"start_ts": 90 * M, "end_ts": 105 * M}]
        # tours : 10–25 (agent), 40–45 (dans l'attente humaine : priorité
        # humaine), 95–100 (dans le CI : priorité CI), 170–185 (déborde la
        # fenêtre, dont 150–180 en review)
        turns = [(10 * M, 25 * M), (40 * M, 45 * M), (95 * M, 100 * M),
                 (170 * M, 185 * M)]
        out = lot_measures.split(0, 180 * M, events=self.events, milestones=self.milestones,
                                 ci_runs=runs, turns=turns)
        self.assertEqual(out["agent_source"], "turns")
        # 15 min du premier tour ; le reste est humain, review, CI ou sans tour
        self.assertEqual(out["agent_minutes"], 15.0)
        self.assertEqual((out["human_minutes"], out["review_minutes"], out["ci_minutes"]),
                         (20.0, 60.0, 10.0))
        # total = agent + CI + review + human (bureau, #796) : le temps qu'aucun
        # tour ne couvre (25–30, 50–90, 100–150) n'est compté nulle part
        self.assertEqual(out["total_minutes"], 105.0)

    def test_turns_absent_keep_the_states_rule(self):
        out = lot_measures.split(0, 180 * M, events=self.events, milestones=self.milestones,
                                 ci_runs=[], turns=None)
        self.assertEqual(out["agent_source"], "states")
        self.assertEqual(out["agent_minutes"], 100.0)

    def test_ci_unknown_stays_agent_time(self):
        out = lot_measures.split(0, 180 * M, events=self.events, milestones=self.milestones,
                                 ci_runs=None)
        self.assertIsNone(out["ci_minutes"])
        self.assertIsNone(out["ci_runs"])
        self.assertEqual(out["agent_minutes"], 100.0)

    def test_state_before_start_and_qa(self):
        # the lot was already waiting for a human when the work started; qa
        # counts as review
        events = [_ev("waiting_human", -10, 1), _ev("build", 20, 2), _ev("qa", 40, 3)]
        out = lot_measures.split(0, 60 * M, events=events, milestones=[], ci_runs=[])
        self.assertEqual((out["human_minutes"], out["agent_minutes"], out["review_minutes"],
                          out["ci_minutes"]), (20.0, 20.0, 20.0, 0.0))

    def test_size_and_parsing(self):
        self.assertEqual([lot_measures.size_class(n) for n in (None, 0, 100, 101, 500, 501)],
                         [None, "s", "s", "m", "m", "l"])
        self.assertEqual(lot_measures.parse_shortstat(
            " 3 files changed, 120 insertions(+), 4 deletions(-)\n"),
            {"files": 3, "insertions": 120, "deletions": 4})
        self.assertEqual(lot_measures.parse_shortstat(" 1 file changed, 1 deletion(-)"),
                         {"files": 1, "insertions": 0, "deletions": 1})
        runs = lot_measures.parse_runs([
            {"createdAt": "2026-10-11T04:00:00Z", "updatedAt": "2026-10-11T04:08:00Z",
             "status": "completed"},
            {"createdAt": "2026-10-11T05:00:00Z", "updatedAt": "2026-10-11T05:01:00Z",
             "status": "in_progress"}])
        start = dt.datetime(2026, 10, 11, 4, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(runs, [{"start_ts": start, "end_ts": start + 8 * M}])

    def test_view_and_labels(self):
        v = lot_measures.view({"agent_minutes": 70, "ci_minutes": None, "review_minutes": 35,
                               "human_minutes": 0, "diff_files": 9, "diff_insertions": 600,
                               "diff_deletions": 12, "measured_size": "l",
                               "declared_size": "m"})
        self.assertFalse(v["size_match"])
        self.assertEqual(lot_measures.describe_phases(v),
                         "agent 1 h 10 · CI unknown · review 35 min · human 0 min")
        # #796 : le temps d'agent dit sa source
        v["agent_source"] = "turns"
        self.assertEqual(lot_measures.describe_phases(v),
                         "agent 1 h 10 (turns) · CI unknown · review 35 min · human 0 min")
        self.assertEqual(lot_measures.describe_size(v),
                         "declared m · measured l (612 lines, 9 files) — differs")
        self.assertIsNone(lot_measures.view(None))


def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def make_repo(root: str) -> tuple[str, str]:
    """A repository with a merge commit bringing 2 files, 150 lines: (repo, sha)."""
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.org")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "base.txt"), "w") as f:
        f.write("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "lot")
    for name, n in (("a.txt", 100), ("b.txt", 50)):
        with open(os.path.join(repo, name), "w") as f:
            f.write("".join("line %d\n" % i for i in range(n)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge lot", "lot")
    return repo, _git(repo, "rev-parse", "HEAD")


def make_branch_repo(root: str) -> tuple[str, str, str]:
    """A branch of 2 commits (300 lines in a.txt, then 5 in b.txt) merged in
    --no-ff, then another commit on main: (repo, branch tip, merge commit)."""
    repo = os.path.join(root, "repo2")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.org")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "base.txt"), "w") as f:
        f.write("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "lot")
    for name, n in (("a.txt", 300), ("b.txt", 5)):
        with open(os.path.join(repo, name), "w") as f:
            f.write("".join("line %d\n" % i for i in range(n)))
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", name)
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    with open(os.path.join(repo, "main.txt"), "w") as f:
        f.write("on main\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main moves")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge lot", "lot")
    merge = _git(repo, "rev-parse", "HEAD")
    with open(os.path.join(repo, "after.txt"), "w") as f:
        f.write("after\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "after")
    return repo, tip, merge


class DiffStatTest(unittest.TestCase):

    def test_branch_tip_merged_no_ff(self):
        # the review case: the milestone records the TIP of a 2-commit branch
        with tempfile.TemporaryDirectory() as root:
            repo, tip, merge = make_branch_repo(root)
            want = {"diff_files": 2, "diff_insertions": 305, "diff_deletions": 0,
                    "measured_size": "m"}
            self.assertEqual(lot_measures.diff_stat(repo, tip, "main"), want)
            self.assertEqual(lot_measures.diff_stat(repo, merge, "main"), want)
            self.assertEqual(lot_measures.diff_range(repo, tip, "main"),
                             (_git(repo, "rev-parse", merge + "^1"), merge))
            # a squash-like commit on the first-parent chain: itself
            after = _git(repo, "rev-parse", "main")
            self.assertEqual(lot_measures.diff_stat(repo, after, "main")["diff_insertions"],
                             1)

    def test_not_in_target_rebase(self):
        with tempfile.TemporaryDirectory() as root:
            repo, tip, _merge = make_branch_repo(root)
            _git(repo, "checkout", "-q", "-b", "other", tip + "~2")
            with open(os.path.join(repo, "c.txt"), "w") as f:
                f.write("".join("c %d\n" % i for i in range(40)))
            _git(repo, "add", ".")
            _git(repo, "commit", "-q", "-m", "c")
            other = _git(repo, "rev-parse", "HEAD")
            out = lot_measures.diff_stat(repo, other, "main")
            self.assertEqual((out["diff_files"], out["diff_insertions"]), (1, 40))

    def test_merged_diff(self):
        with tempfile.TemporaryDirectory() as root:
            repo, sha = make_repo(root)
            self.assertEqual(lot_measures.diff_stat(repo, sha),
                             {"diff_files": 2, "diff_insertions": 150, "diff_deletions": 0,
                              "measured_size": "m"})
            self.assertIsNone(lot_measures.diff_stat(repo, "0" * 40))
            self.assertIsNone(lot_measures.diff_stat(repo, "not-a-sha"))


# a fixed local noon: days do not depend on the machine's time zone
NOW = dt.datetime(2026, 10, 12, 12, 0).timestamp()
H = 3600.0
BODY = {"schema": "ameesh-estimate-reference/1", "min_lots": 3, "groups": [
    {"level": "type", "type": "evolution", "lots": 5, "median_minutes": 600.0,
     "p80_minutes": 1440.0}]}


def _day(ts):
    return dt.date.fromtimestamp(ts).isoformat()


class QueueTest(unittest.TestCase):

    def test_order_at_median_dates_at_p80(self):
        lots = [{"id": 1, "type": "evolution", "started_ts": NOW - 2 * H, "state": "build"},
                {"id": 2, "type": "evolution", "priority": 1, "state": "intake"},
                {"id": 3, "type": "evolution", "priority": 3, "state": "intake"},
                {"id": 4, "type": "bug", "state": "intake"},
                {"id": 5, "type": "chore", "estimate_minutes": 120, "state": "intake"}]
        out = lot_schedule.plan_queue(lots, {}, BODY, {}, now=NOW)
        # #5151: the P1 goes first, even before the lot in progress
        self.assertEqual([e["id"] for e in out], [2, 1, 4, 5, 3])
        by = {e["id"]: e for e in out}
        self.assertEqual((by[2]["start_ts"], by[2]["end_ts"]), (NOW, NOW + 24 * H))
        # in progress: started 2 h ago, its rest (22 h at the p80, 8 h at the
        # median) moves after the P1's median (now + 10 h)
        self.assertEqual((by[1]["start_ts"], by[1]["end_ts"]), (NOW - 2 * H, NOW + 32 * H))
        # no group, no estimate: unplanned, the queue does not move
        self.assertIsNone(by[4]["planned_start"])
        # no group, its own estimate: median = p80 = 2 h
        self.assertEqual((by[5]["basis"], by[5]["start_ts"], by[5]["end_ts"]),
                         ("estimate", NOW + 18 * H, NOW + 20 * H))
        self.assertEqual((by[3]["start_ts"], by[3]["planned_start"], by[3]["planned_end"]),
                         (NOW + 20 * H, _day(NOW + 20 * H), _day(NOW + 44 * H)))
        self.assertEqual(by[2]["basis"], "reference")

    def test_overrun_lot_never_ends_in_the_past(self):
        lots = [{"id": 1, "type": "evolution", "started_ts": NOW - 48 * H, "state": "build"},
                {"id": 2, "type": "evolution", "state": "intake"}]
        out = lot_schedule.plan_queue(lots, {}, BODY, {}, now=NOW)
        self.assertEqual(out[0]["end_ts"], NOW)
        self.assertEqual(out[1]["start_ts"], NOW)

    def test_lot_dated_by_hand_enters_at_its_date(self):
        lots = [{"id": 1, "type": "evolution", "priority": 1, "state": "intake"},
                {"id": 2, "type": "evolution", "state": "intake"},
                {"id": 3, "type": "evolution", "state": "intake"}]
        plans = {1: {"planned_start": "2030-01-01", "planned_source": "orchestrateur"}}
        out = lot_schedule.plan_queue(lots, plans, BODY, {}, now=NOW)
        by = {e["id"]: e for e in out}
        # the P1 lot set for 2030 does not push the others back
        self.assertEqual([e["id"] for e in out], [2, 3, 1])
        self.assertEqual((by[2]["start_ts"], by[3]["start_ts"]), (NOW, NOW + 10 * H))
        self.assertTrue(by[1]["pinned"])
        self.assertEqual(by[1]["planned_start"], "2030-01-01")
        # dated today: its date is reached, it goes first (priority 1)
        plans = {1: {"planned_start": _day(NOW), "planned_source": "orch"}}
        out = lot_schedule.plan_queue(lots, plans, BODY, {}, now=NOW)
        self.assertEqual([e["id"] for e in out], [1, 2, 3])
        # dated tomorrow (midnight = now + 12 h): lot 2 first (until now +
        # 10 h), then the queue has not reached it yet: lot 3, then lot 1
        plans = {1: {"planned_start": _day(NOW + 24 * H), "planned_source": "orch"}}
        out = lot_schedule.plan_queue(lots, plans, BODY, {}, now=NOW)
        self.assertEqual([(e["id"], e["start_ts"]) for e in out],
                         [(2, NOW), (3, NOW + 10 * H), (1, NOW + 20 * H)])
        # --force: chained like the others, in priority order
        out = lot_schedule.plan_queue(lots, plans, BODY, {}, now=NOW, force=True)
        self.assertEqual([e["id"] for e in out], [1, 2, 3])
        self.assertEqual(out[0]["start_ts"], NOW)

    def test_writable(self):
        entry = {"planned_start": "2026-10-12", "planned_end": "2026-10-13",
                 "current": {"planned_start": "2026-10-20", "planned_end": None,
                             "planned_source": "orchestrateur"}}
        self.assertFalse(lot_schedule._writable(entry, False))      # a person's date
        self.assertTrue(lot_schedule._writable(entry, True))
        entry["current"]["planned_source"] = "reference p80 (table #3)"
        self.assertTrue(lot_schedule._writable(entry, False))
        entry["current"] = {}
        self.assertTrue(lot_schedule._writable(entry, False))


class SchedulePgTest(PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host)

    def test_schedule_and_work_add(self):
        a = work.add(self.db, title="first", assignee="dev1", estimate=60, priority=1)
        # #540 : la file d'un agent au-delà d'un lot non démarré, forcée
        b = work.add(self.db, title="second", assignee="dev1", estimate=60,
                     force="test: a queue of lots (#540)")
        c = work.add(self.db, title="dated by hand", assignee="dev1", estimate=60,
                     priority=3, force="test: a queue of lots (#540)")
        storage.of(self.db).roadmap.plan_item(c["id"], {"start": "2030-01-01"},
                                              source="orchestrateur", actor="orch")
        # no table published: the lots' own estimates
        proc = self.mesh("work", "schedule", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sched = json.loads(proc.stdout)
        self.assertIsNone(sched["reference"])
        self.assertEqual([e["id"] for e in sched["queues"][0]["lots"]],
                         [a["id"], b["id"], c["id"]])
        self.assertEqual(sched["written"], [])      # shown only

        storage.of(self.db).work.publish_reference(BODY, actor="auditeur")
        proc = self.mesh("work", "schedule", "--apply", "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("reference table #", proc.stdout)
        self.assertIn("(kept: 2030-01-01 → —)", proc.stdout)
        plans = {p["id"]: p for p in storage.of(self.db).roadmap.item_plans(
            [a["id"], b["id"], c["id"]])}
        self.assertTrue(plans[a["id"]]["planned_source"].startswith("reference p80 (table #"))
        self.assertIsNotNone(plans[b["id"]]["planned_end"])
        self.assertEqual((str(plans[c["id"]]["planned_start"])[:10],
                          plans[c["id"]]["planned_source"]), ("2030-01-01", "orchestrateur"))

        # a new assigned lot gets its dates at creation
        proc = self.mesh("work", "add", "--title", "new", "--assignee", "dev1",
                         "--estimate", "1h", "--actor", "human:test",
                         "--force", "test: a queue of lots (#540)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("planned  : ", proc.stdout)
        self.assertIn("(reference p80 (table #", proc.stdout)


class ScheduleByHandPgTest(PgTestCase):
    """#704: a date set by hand after the schedule is kept (own class: it
    publishes a reference table, which SchedulePgTest expects absent)."""

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host)

    def test_date_set_by_hand_after_the_schedule_is_kept(self):
        # #704: `work plan` without --source on a lot the schedule had dated
        a = work.add(self.db, title="dated by the schedule", assignee="dev1", estimate=60)
        storage.of(self.db).work.publish_reference(BODY, actor="auditeur")
        proc = self.mesh("work", "schedule", "--apply", "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # a delivery date alone is never written by the schedule: its source stays
        proc = self.mesh("work", "plan", str(a["id"]), "--delivery", "2030-02-01",
                         "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(storage.of(self.db).roadmap.item_plans([a["id"]])[0]
                        ["planned_source"].startswith("reference p80 (table #"))
        # #740: only a human, an orchestrator or a design agent pins a date
        proc = self.mesh("work", "plan", str(a["id"]), "--start", "2030-01-01",
                         "--end", "2030-01-02", "--actor", "human:test")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        def plan():
            return storage.of(self.db).roadmap.item_plans([a["id"]])[0]

        self.assertEqual((str(plan()["planned_start"])[:10], plan()["planned_source"]),
                         ("2030-01-01", "set by human:test"))
        proc = self.mesh("work", "schedule", "--apply", "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(str(plan()["planned_start"])[:10], "2030-01-01")
        # a source given by the person stays as given (#740 r1: replacing a
        # "set by" marker takes the same authority as pinning)
        proc = self.mesh("work", "plan", str(a["id"]), "--start", "2030-01-01",
                         "--source", "decision #1", "--actor", "human:test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(plan()["planned_source"], "decision #1")
        # --force still rewrites a date set by hand
        proc = self.mesh("work", "schedule", "--apply", "--force", "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotEqual(str(plan()["planned_start"])[:10], "2030-01-01")
        self.assertTrue(plan()["planned_source"].startswith("reference p80 (table #"))


class ScheduleRacePgTest(PgTestCase):
    """#740 (P1a): the schedule writes a lot only while its dates are still
    those the schedule read (own class: it publishes a reference table)."""

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host)

    def _plan(self, lot):
        return storage.of(self.db).roadmap.item_plans([lot])[0]

    def test_a_date_set_after_the_build_is_kept(self):
        a = work.add(self.db, title="race", assignee="dev1", estimate=60)
        storage.of(self.db).work.publish_reference(BODY, actor="auditeur")
        sched = lot_schedule.build(self.db)
        roadmap.plan(self.db, a["id"], start="2030-01-01", end="2030-01-02",
                     actor="human:test")
        self.assertEqual(lot_schedule.apply(self.db, sched, actor="orch"), [])
        plan = self._plan(a["id"])
        self.assertEqual((str(plan["planned_start"])[:10], plan["planned_by"]),
                         ("2030-01-01", "human:test"))

    def test_a_forced_apply_keeps_a_date_changed_after_the_build(self):
        a = work.add(self.db, title="race", assignee="dev1", estimate=60)
        storage.of(self.db).work.publish_reference(BODY, actor="auditeur")
        roadmap.plan(self.db, a["id"], start="2030-01-01", end="2030-01-02",
                     source="decision #1", actor="human:test")
        sched = lot_schedule.build(self.db, force=True)
        roadmap.plan(self.db, a["id"], start="2030-02-01", end="2030-02-02",
                     source="decision #2", actor="human:test")
        self.assertEqual(lot_schedule.apply(self.db, sched, force=True, actor="orch"), [])
        self.assertEqual((str(self._plan(a["id"])["planned_start"])[:10],
                          self._plan(a["id"])["planned_source"]), ("2030-02-01", "decision #2"))
        # nothing changed since the build: --force still writes
        sched = lot_schedule.build(self.db, force=True)
        self.assertEqual([e["id"] for e in lot_schedule.apply(self.db, sched, force=True,
                                                              actor="orch")], [a["id"]])
        self.assertTrue(self._plan(a["id"])["planned_source"].startswith("reference p80"))


class PinAuthorityPgTest(PgTestCase):
    """#740 (P2): the "set by" marker that protects a date from the schedule
    needs a real change of the start or the end, by a human, an orchestrator
    or a design agent (`work.corrector`)."""

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host)
        self.lot = work.add(self.db, title="dated by the schedule", assignee="dev1",
                            estimate=60)["id"]
        storage.of(self.db).work.publish_reference(BODY, actor="auditeur")
        lot_schedule.apply(self.db, lot_schedule.build(self.db), actor="orch")

    def _plan(self):
        return storage.of(self.db).roadmap.item_plans([self.lot])[0]

    def test_an_agent_without_authority_does_not_pin(self):
        out = roadmap.plan(self.db, self.lot, start="2030-01-01", end="2030-01-02",
                           actor="dev1")
        self.assertEqual(str(self._plan()["planned_start"])[:10], "2030-01-01")
        self.assertTrue(self._plan()["planned_source"].startswith("reference p80"))
        self.assertIn("not protected", out.get("warning") or "")
        proc = self.mesh("work", "plan", str(self.lot), "--start", "2030-01-03",
                         "--end", "2030-01-04", "--actor", "dev1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("not protected", proc.stderr)

    def test_resubmitting_the_current_dates_does_not_pin(self):
        cur = self._plan()
        roadmap.plan(self.db, self.lot, start=str(cur["planned_start"])[:10],
                     end=str(cur["planned_end"])[:10], actor="human:test")
        self.assertTrue(self._plan()["planned_source"].startswith("reference p80"))

    def test_a_human_changing_the_start_pins(self):
        out = roadmap.plan(self.db, self.lot, start="2030-01-01", end="2030-01-02",
                           actor="human:test")
        self.assertEqual(self._plan()["planned_source"], "set by human:test")
        self.assertIsNone(out.get("warning"))

    def test_assigning_a_lot_gives_no_pin_authority(self):
        # r1 (1): having assigned a lot is not a declared role
        registry.upsert(self.db, "dev2", harness="claude", host=self.cfg.host)
        other = work.add(self.db, title="handed over", estimate=60)["id"]
        work.assign(self.db, other, "dev2", actor="dev1")
        out = roadmap.plan(self.db, self.lot, start="2030-01-01", end="2030-01-02",
                           actor="dev1")
        self.assertTrue(self._plan()["planned_source"].startswith("reference p80"))
        self.assertIn("not protected", out.get("warning") or "")

    def test_a_source_that_claims_the_dates_needs_authority(self):
        # r1 (2): a caller-supplied marker goes through the same check
        for claim in ("set by human:owner", "decision #9"):
            out = roadmap.plan(self.db, self.lot, delivery="2030-02-01", source=claim,
                               actor="dev1")
            self.assertTrue(self._plan()["planned_source"].startswith("reference p80"),
                            claim)
            self.assertIn("not recorded", out.get("warning") or "")
        roadmap.plan(self.db, self.lot, delivery="2030-02-01", source="decision #9",
                     actor="human:test")
        self.assertEqual(self._plan()["planned_source"], "decision #9")
        out = roadmap.plan(self.db, self.lot, delivery="2030-02-02",
                           source="set by human:owner", actor="dev1")
        self.assertEqual(self._plan()["planned_source"], "decision #9")
        self.assertIn("not recorded", out.get("warning") or "")

    def test_a_schedule_write_between_read_and_write_is_not_kept(self):
        # internal review r1 (P1): `work plan` writes only the row it read
        storage.of(self.db).roadmap.plan_item(self.lot, {"start": None, "end": None},
                                              source="decision #1", actor="orchestrateur")
        real, raced = roadmap._hand_source, []

        def racing(db, current, dates, source, actor):
            out = real(db, current, dates, source, actor)
            if not raced:     # the schedule dates the lot after `work plan` read it
                raced.append(True)
                lot_schedule.apply(db, lot_schedule.build(db), actor="orch")
            return out

        with mock.patch.object(roadmap, "_hand_source", racing):
            roadmap.plan(self.db, self.lot, start="2030-01-01", end="2030-01-02",
                         actor="human:test")
        self.assertEqual(self._plan()["planned_source"], "set by human:test")
        lot_schedule.apply(self.db, lot_schedule.build(self.db), actor="orch")
        self.assertEqual(str(self._plan()["planned_start"])[:10], "2030-01-01")

    def test_clearing_both_dates_by_hand_is_kept(self):
        # r2: the "set by" marker protects an intentional clearing too
        roadmap.plan(self.db, self.lot, start="-", end="-", actor="human:test")
        self.assertEqual(self._plan()["planned_source"], "set by human:test")
        lot_schedule.apply(self.db, lot_schedule.build(self.db), actor="orch")
        plan = self._plan()
        self.assertEqual((plan["planned_start"], plan["planned_end"], plan["planned_source"]),
                         (None, None, "set by human:test"))

    def test_clearing_a_date_is_a_change(self):
        # r1 (3): `--end -` is a change of the end
        out = roadmap.plan(self.db, self.lot, end="-", actor="dev1")
        self.assertIsNone(self._plan()["planned_end"])
        self.assertTrue(self._plan()["planned_source"].startswith("reference p80"))
        self.assertIn("not protected", out.get("warning") or "")
        lot_schedule.apply(self.db, lot_schedule.build(self.db), actor="orch")
        self.assertIsNotNone(self._plan()["planned_end"])
        roadmap.plan(self.db, self.lot, end="-", actor="human:test")
        self.assertIsNone(self._plan()["planned_end"])
        self.assertEqual(self._plan()["planned_source"], "set by human:test")


class PinRepairPgTest(PgTestCase):
    """#740 (P1b): a date set by hand before #704 still carries the
    schedule's source. `work schedule --pin` lists such lots (dry run) and
    protects those a corrector names; it never guesses."""

    def setUp(self) -> None:
        super().setUp()
        registry.upsert(self.db, "dev1", harness="claude", host=self.cfg.host)
        self.legacy = work.add(self.db, title="hand date before #704", assignee="dev1",
                               estimate=60)["id"]
        self.other = work.add(self.db, title="person's source", assignee="dev1",
                              estimate=60)["id"]
        storage.of(self.db).work.publish_reference(BODY, actor="auditeur")
        lot_schedule.apply(self.db, lot_schedule.build(self.db), actor="auditeur")
        # before #704: `work plan` without --source kept the schedule's source
        storage.of(self.db).roadmap.plan_item(self.legacy, {"start": "2030-01-01",
                                                            "end": "2030-01-02"},
                                              source=None, actor="orchestrateur")
        storage.of(self.db).roadmap.plan_item(self.other, {"start": "2030-03-01",
                                                           "end": "2030-03-02"},
                                              source="decision #1", actor="orchestrateur")

    def _plan(self, lot):
        return storage.of(self.db).roadmap.item_plans([lot])[0]

    def test_dry_run_lists_without_writing(self):
        before = self._plan(self.legacy)
        proc = self.mesh("work", "schedule", "--pin", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = {r["id"]: r for r in json.loads(proc.stdout)["candidates"]}
        self.assertEqual(set(rows), {self.legacy})
        self.assertEqual(rows[self.legacy]["planned_by"], "orchestrateur")
        self.assertTrue(rows[self.legacy]["moves"])
        self.assertEqual(self._plan(self.legacy), before)

    def test_pin_keeps_the_dates_with_who_set_them(self):
        proc = self.mesh("work", "schedule", "--pin", str(self.legacy), str(self.other),
                         "--actor", "human:test", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = {r["id"]: r["status"] for r in json.loads(proc.stdout)["pinned"]}
        self.assertEqual(status, {self.legacy: "pinned", self.other: "skipped"})
        plan = self._plan(self.legacy)
        self.assertEqual((str(plan["planned_start"])[:10], plan["planned_source"]),
                         ("2030-01-01", "set by orchestrateur"))
        self.assertEqual(self._plan(self.other)["planned_source"], "decision #1")
        proc = self.mesh("work", "schedule", "--apply", "--actor", "orch")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(str(self._plan(self.legacy)["planned_start"])[:10], "2030-01-01")

    def test_pin_needs_a_corrector(self):
        proc = self.mesh("work", "schedule", "--pin", str(self.legacy), "--actor", "dev1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refused", proc.stderr)
        self.assertTrue(self._plan(self.legacy)["planned_source"].startswith("reference p80"))

    def test_assigning_lots_gives_no_pin_right(self):
        # r1 (1): `work assign` once does not make a corrector for --pin
        registry.upsert(self.db, "dev2", harness="claude", host=self.cfg.host)
        work.assign(self.db, self.other, "dev2", actor="dev1")
        proc = self.mesh("work", "schedule", "--pin", str(self.legacy), "--actor", "dev1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refused", proc.stderr)
        self.assertTrue(self._plan(self.legacy)["planned_source"].startswith("reference p80"))


class MeasurePgTest(PgTestCase):

    def _at(self, sql, minutes, *params):
        self.db.execute(sql, (float(self.t0 + minutes * M),) + params)

    def test_measure_stored_and_shown(self):
        lot = work.add(self.db, title="measured", size="s", branch="lot/x")
        lid = lot["id"]
        work.move(self.db, lid, "build")
        work.milestone(self.db, lid, "frozen", sha="a" * 40)
        with tempfile.TemporaryDirectory() as root:
            repo, sha = make_repo(root)
            work.close_merged(self.db, lid, sha=sha)
            self.t0 = float(self.db.query(
                "SELECT extract(epoch from now())::float8 AS at_epoch")[0]["at_epoch"]) - 100 * M
            # build at 0, frozen at 60, merged at 100
            self._at("UPDATE work_items SET started_at = to_timestamp(%s) WHERE id = %s",
                     0, lid)
            self._at("UPDATE work_item_events SET created_at = to_timestamp(%s)"
                     " WHERE work_item_id = %s AND state = 'build'", 0, lid)
            self._at("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                     " WHERE work_item_id = %s AND kind = 'frozen'", 60, lid)
            self._at("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                     " WHERE work_item_id = %s AND kind = 'merged'", 100, lid)
            calls = []

            def ci(repo_dir, branch):
                calls.append((repo_dir, branch))
                return [{"start_ts": self.t0 + 50 * M, "end_ts": self.t0 + 70 * M}]

            rows = lot_measures.measure(self.db, None, repos=[repo], ci=ci)
            self.assertEqual(calls, [(repo, "lot/x")])
            self.assertEqual(len(rows), 1)
            m = lot_measures.view(rows[0])
            self.assertEqual((m["agent_minutes"], m["ci_minutes"], m["review_minutes"],
                              m["human_minutes"], m["total_minutes"]),
                             (50.0, 10.0, 40.0, 0.0, 100.0))
            self.assertEqual((m["diff_files"], m["measured_size"], m["declared_size"],
                              m["size_match"], m["merge_sha"]), (2, "m", "s", False, sha))
            # aucun tour attribué (lot d'avant 0423) : règle des états
            self.assertEqual(m["agent_source"], "states")
            # measured once: the next pass has nothing to do
            self.assertEqual(lot_measures.measure(self.db, None, repos=[repo], ci=ci), [])
            # measured again on demand, without a repository: the diff is kept
            again = lot_measures.measure(self.db, [lid], repos=[], ci=None)
            # ... and the phases measured with a known CI too
            self.assertEqual((again[0]["diff_files"], again[0]["ci_minutes"],
                              again[0]["agent_minutes"]), (2, 10.0, 50.0))

            proc = self.mesh("work", "show", str(lid))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("phases   : agent 50 min (states) · CI 10 min · review 40 min"
                          " · human 0 min", proc.stdout)
            self.assertIn("size     : declared s · measured m (150 lines, 2 files) — differs",
                          proc.stdout)
            proc = self.mesh("work", "show", str(lid), "--json")
            self.assertEqual(json.loads(proc.stdout)["measure"]["diff_insertions"], 150)
            proc = self.mesh("work", "measure", str(lid), "--repo", repo, "--no-ci", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)[0]["id"], lid)

    def _delivered(self, title, *, branch=None, sha="abcdef1"):
        lot = work.add(self.db, title=title, branch=branch)
        work.move(self.db, lot["id"], "build")
        work.close_merged(self.db, lot["id"], sha=sha)
        return int(lot["id"])

    def test_ci_unknown_retried_and_one_failure_does_not_block(self):
        with tempfile.TemporaryDirectory() as root:
            repo, sha = make_repo(root)
            a = self._delivered("a", branch="lot/a", sha=sha)
            b = self._delivered("b", branch="lot/b", sha=sha)
            # gh fails: CI unknown (None), not 0
            rows = lot_measures.measure(self.db, None, repos=[repo], ci=lambda r, br: None)
            self.assertEqual({r["work_item_id"]: r["ci_minutes"] for r in rows},
                             {a: None, b: None})
            # next pass: gh answers for b, raises for a — a is noted, b measured
            errors = []

            def ci(_repo, branch):
                if branch == "lot/a":
                    raise RuntimeError("gh broke")
                return []

            rows = lot_measures.measure(self.db, None, repos=[repo], ci=ci, errors=errors)
            self.assertEqual([(r["work_item_id"], r["ci_minutes"]) for r in rows], [(b, 0.0)])
            self.assertEqual([(lot, str(exc)) for lot, exc in errors], [(a, "gh broke")])
            # b is complete now; a still retried
            self.assertEqual([r["work_item_id"] for r in lot_measures.measure(
                self.db, None, repos=[repo], ci=lambda r, br: [])], [a])
            # an exhausted budget measures nothing
            c = self._delivered("c", branch="lot/c", sha=sha)
            self.assertEqual(lot_measures.measure(self.db, None, repos=[repo], ci=None,
                                                  budget_s=0), [])
            self.assertIsNone(storage.of(self.db).work.measure(c))

    def test_runner_pass_only_recent_lots(self):
        lot = work.add(self.db, title="old")
        work.move(self.db, lot["id"], "build")
        work.close_merged(self.db, lot["id"], sha="abcdef1")
        self.db.execute("UPDATE work_item_milestones SET at = now() - interval '3 days'"
                        " WHERE work_item_id = %s", (int(lot["id"]),))
        self.db.execute("UPDATE work_items SET started_at = now() - interval '4 days'"
                        " WHERE id = %s", (int(lot["id"]),))
        self.assertEqual(lot_measures.measure_pending(self.db, ci=None), [])
        # on demand, of any age
        rows = lot_measures.measure(self.db, None, repos=[], ci=None)
        self.assertEqual([r["work_item_id"] for r in rows], [lot["id"]])
        self.assertEqual(rows[0]["total_minutes"], 1440.0)

    def test_runner_pass_without_repository_measures_nothing(self):
        # #3972 : sans dépôt (aucun agent admis, dossier de travail illisible),
        # la passe ne mesure pas — le diff resterait inconnu et la mesure
        # prétendrait un dépôt que l'hôte n'a pas.
        with tempfile.TemporaryDirectory() as root:
            repo, sha = make_repo(root)
            lot = self._delivered("recent", branch="lot/recent", sha=sha)
            # aucun agent de l'hôte : aucun dossier de travail
            self.assertEqual(lot_measures.measure_pending(
                self.db, host=self.cfg.host, ci=None), [])
            self.assertIsNone(storage.of(self.db).work.measure(lot))
            # un agent existe, mais le filtre d'admission ne sélectionne personne
            registry.upsert(self.db, "solo", host=self.cfg.host, cwd=repo)
            for agents in (set(), {""}):
                self.assertEqual(lot_measures.measure_pending(
                    self.db, host=self.cfg.host, agents=agents, ci=None), [])
                self.assertIsNone(storage.of(self.db).work.measure(lot))
            # le même agent sélectionné : le lot est mesuré avec son diff
            rows = lot_measures.measure_pending(
                self.db, host=self.cfg.host, agents={"solo"}, ci=None)
            self.assertEqual([r["work_item_id"] for r in rows], [lot])
            self.assertEqual(rows[0]["diff_files"], 2)

    def test_lot_without_start_is_not_measured(self):
        lot = work.add(self.db, title="no start")
        work.close_merged(self.db, lot["id"], sha="abcdef1")
        self.db.execute("UPDATE work_items SET started_at = NULL WHERE id = %s",
                        (int(lot["id"]),))
        self.assertEqual(lot_measures.measure(self.db, None, repos=[], ci=None), [])

    def test_attributed_turns_give_agent_minutes(self):
        """#796 : quand des tours portent le lot, le temps d'agent est l'union
        de leurs intervalles (jamais comptée deux fois), et les tours d'un
        autre lot ou sans lot ne comptent pas."""
        lot = work.add(self.db, title="turns", branch="lot/t")
        lid = int(lot["id"])
        work.move(self.db, lid, "build")
        work.close_merged(self.db, lid, sha="abcdef1")
        self.t0 = float(self.db.query(
            "SELECT extract(epoch from now())::float8 AS at_epoch")[0]["at_epoch"]) - 100 * M
        self._at("UPDATE work_items SET started_at = to_timestamp(%s) WHERE id = %s", 0, lid)
        self._at("UPDATE work_item_events SET created_at = to_timestamp(%s)"
                 " WHERE work_item_id = %s AND state = 'build'", 0, lid)
        self._at("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                 " WHERE work_item_id = %s AND kind = 'merged'", 100, lid)
        # deux tours du lot (10–25 et 40–80) dont un recouvrement (50–60), et
        # deux tours étrangers : un autre lot, et un sans lot (relais)
        for start, end in ((10, 25), (40, 80), (50, 60)):
            self.db.execute(
                "INSERT INTO turn_costs (agent, harness, model, usd, started_at,"
                " recorded_at, work_item_id) VALUES ('dev1','claude','claude-opus-4',"
                " 0.1, to_timestamp(%s), to_timestamp(%s), %s)",
                (self.t0 + start * M, self.t0 + end * M, lid))
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, started_at, recorded_at,"
            " work_item_id) VALUES ('dev2','claude','claude-opus-4', 0.1,"
            " to_timestamp(%s), to_timestamp(%s), %s)",
            (self.t0 + 20 * M, self.t0 + 30 * M, self._other_lot()))
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, started_at, recorded_at)"
            " VALUES ('dev1','claude','claude-opus-4', 0.1, to_timestamp(%s),"
            " to_timestamp(%s))", (self.t0 + 20 * M, self.t0 + 90 * M))
        rows = lot_measures.measure(self.db, [lid], repos=[], ci=lambda r, b: [])
        m = lot_measures.view(rows[0])
        # union des tours du lot : 10–25 (15 min) + 40–80 (40 min) = 55 min
        self.assertEqual((m["agent_minutes"], m["agent_source"]), (55.0, "turns"))
        # aucun `turn_resources` : le début du tour est celui du marqueur
        self.assertEqual(storage.of(self.db).work.turn_spans(lid), [
            {"start_ts": self.t0 + 10 * M, "end_ts": self.t0 + 25 * M, "turn_id": None},
            {"start_ts": self.t0 + 40 * M, "end_ts": self.t0 + 80 * M, "turn_id": None},
            {"start_ts": self.t0 + 50 * M, "end_ts": self.t0 + 60 * M, "turn_id": None},
        ])

    def test_lot_start_is_the_first_attributed_turn(self):
        """#796 : le début mesuré du lot est celui de son premier tour attribué,
        pris sur `turn_resources.started_at` quand le tour y est relié — la
        date déclarée, posée à la prise en main, arrive souvent après le début
        du travail. Le temps d'agent reste l'union des tours."""
        lot = work.add(self.db, title="late start", branch="lot/late")
        lid = int(lot["id"])
        work.move(self.db, lid, "build")
        work.close_merged(self.db, lid, sha="abcdef1")
        self.t0 = float(self.db.query(
            "SELECT extract(epoch from now())::float8 AS at_epoch")[0]["at_epoch"]) - 100 * M
        self._at("UPDATE work_item_events SET created_at = to_timestamp(%s)"
                 " WHERE work_item_id = %s AND state = 'build'", 0, lid)
        self._at("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                 " WHERE work_item_id = %s AND kind = 'merged'", 100, lid)
        # déclaré à 30 (prise en main tardive) ; le tour a pris la main à 20
        self._at("UPDATE work_items SET started_at = to_timestamp(%s) WHERE id = %s", 30, lid)
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, started_at, recorded_at,"
            " turn_id, work_item_id) VALUES ('dev1','claude','claude-opus-4', 0.1,"
            " to_timestamp(%s), to_timestamp(%s), 'tour-1', %s)",
            (self.t0 + 25 * M, self.t0 + 50 * M, lid))
        self.db.execute(
            "INSERT INTO turn_resources (turn_id, agent, host, started_at)"
            " VALUES ('tour-1','dev1','h1', to_timestamp(%s))", (self.t0 + 20 * M,))
        rows = lot_measures.measure(self.db, [lid], repos=[], ci=lambda r, b: [])
        m = lot_measures.view(rows[0])
        # le tour 20–50 : 30 min d'agent, et rien d'autre (pas de review, pas de CI)
        self.assertEqual((m["agent_minutes"], m["agent_source"]), (30.0, "turns"))
        # le début stocké est celui de la ressource du tour, pas la date déclarée
        self.assertAlmostEqual(rows[0]["started_ts"], self.t0 + 20 * M, places=3)
        self.assertAlmostEqual(m["total_minutes"], 30.0, places=3)
        self.assertEqual(storage.of(self.db).work.turn_spans(lid), [
            {"start_ts": self.t0 + 20 * M, "end_ts": self.t0 + 50 * M, "turn_id": "tour-1"}])

    def test_turn_end_is_the_resource_end_not_the_late_repair(self):
        """#796 : la fin d'un tour attribué est celle de sa ressource
        (`turn_resources.ended_at`) quand elle est connue ; `recorded_at`,
        écrit plus tard par la réparation d'une panne, ne rallonge plus la
        durée d'un tour déjà terminé."""
        lot = work.add(self.db, title="late repair", branch="lot/lr")
        lid = int(lot["id"])
        work.move(self.db, lid, "build")
        work.close_merged(self.db, lid, sha="abcdef1")
        self.t0 = float(self.db.query(
            "SELECT extract(epoch from now())::float8 AS at_epoch")[0]["at_epoch"]) - 100 * M
        self._at("UPDATE work_items SET started_at = to_timestamp(%s) WHERE id = %s", 0, lid)
        self._at("UPDATE work_item_events SET created_at = to_timestamp(%s)"
                 " WHERE work_item_id = %s AND state = 'build'", 0, lid)
        self._at("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                 " WHERE work_item_id = %s AND kind = 'merged'", 100, lid)
        # la ressource dit la fin réelle (30) ; la ligne du grand livre n'a été
        # écrite qu'à 50, par la réparation d'une panne
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, model, usd, started_at, recorded_at,"
            " turn_id, work_item_id) VALUES ('dev1','claude','claude-opus-4', 0.1,"
            " to_timestamp(%s), to_timestamp(%s), 'tour-lr', %s)",
            (self.t0 + 20 * M, self.t0 + 50 * M, lid))
        self.db.execute(
            "INSERT INTO turn_resources (turn_id, agent, host, started_at, ended_at, status)"
            " VALUES ('tour-lr','dev1','h1', to_timestamp(%s), to_timestamp(%s), 'done')",
            (self.t0 + 20 * M, self.t0 + 30 * M))
        self.assertEqual(storage.of(self.db).work.turn_spans(lid), [
            {"start_ts": self.t0 + 20 * M, "end_ts": self.t0 + 30 * M, "turn_id": "tour-lr"}])
        rows = lot_measures.measure(self.db, [lid], repos=[], ci=lambda r, b: [])
        m = lot_measures.view(rows[0])
        # 20–30 = 10 min d'agent, pas les 30 min de la réparation tardive
        self.assertEqual((m["agent_minutes"], m["agent_source"]), (10.0, "turns"))

    def test_kept_measure_keeps_its_source(self):
        """#796 : quand les phases d'une mesure connue (CI lue) sont gardées
        faute de CI relisible, leur provenance de minutes d'agent est gardée
        avec elles : `states` ne devient pas `turns` sans que `agent_minutes`
        soit recalculé par la même règle."""
        lot = work.add(self.db, title="kept source")
        lid = int(lot["id"])
        st = storage.of(self.db).work
        base = dict(started_ts=1000.0, merged_ts=2000.0, total_minutes=60.0,
                    agent_minutes=42.0, ci_minutes=10.0, review_minutes=5.0,
                    human_minutes=3.0, agent_source="states")
        st.save_measure(lid, base)
        # mesure suivante sans CI : ses phases (et sa source) ne remplacent pas
        # celles déjà mesurées avec une CI connue
        st.save_measure(lid, dict(base, agent_minutes=7.0, ci_minutes=None,
                                  total_minutes=7.0, agent_source="turns"))
        m = st.measure(lid)
        self.assertEqual((m["agent_minutes"], m["agent_source"], m["ci_minutes"]),
                         (42.0, "states", 10.0))
        # CI relue : la nouvelle mesure remplace phases et source ensemble
        st.save_measure(lid, dict(base, agent_minutes=7.0, agent_source="turns"))
        m = st.measure(lid)
        self.assertEqual((m["agent_minutes"], m["agent_source"]), (7.0, "turns"))

    def _other_lot(self):
        return int(work.add(self.db, title="other")["id"])


if __name__ == "__main__":
    unittest.main()
