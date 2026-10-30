# SPDX-License-Identifier: AGPL-3.0-only
"""#3033 — the reviews an agent still owes survive rotations.

Constat (mesh-design, 23/10) : #3004 gelé le 21/10, dev1 et cx2 désignés,
aucun verdict pendant 1 j 14 h. La demande de désignation était un courrier
lu ; ni le brief de reprise ni une commande ne listait les avis dus, et cx2 se
déclarait disponible. Attendu : une lecture des avis dus (le calcul de la
charge de #3004, seul calcul), portée par le brief de reprise et par la
relance d'inactivité ; une désignation sans verdict depuis 2 h relance le
relecteur, puis le pilote (§9 `green_verdict`).

Témoins : (a) un agent désigné sur un gel sans verdict voit le lot dans son
brief après rotation ; (b) un verdict sur la tête gelée le retire ; (c) un
regel sur une nouvelle tête le remet.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from ameesh import adapters, alert_briefs, reprise, runner, slo, storage, work
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg

from . import test_2447_review_designation_activation as t2447
from .support import PgTestCase


def _brief(test, name: str) -> str:
    row = {"name": name, "harness": "claude", "host": test.cfg.host, "team": "ameesh"}
    return reprise.deterministic_brief(test.cfg, test.db, row)


def _reviews_section(text: str) -> str:
    start = text.index("## Reviews you owe")
    end = text.index("\n## ", start + 1)
    return text[start:end]


class PendingReviewsTest(t2447._ActivationFixture, PgTestCase):
    """The pending reviews of an agent, read from the designation journal."""

    pending_lot = t2447.PendingLoadTest.pending_lot

    def designate(self, lot: int, sha: str, freeze_id: int, reviewer: str) -> None:
        note = pg._mark_note(lot, freeze_id, state=rd.DESIGNATED, required=1,
                             reviewers=[{"name": reviewer, "family": "claude"}],
                             missing=0, why="test", authors=[], policy_ref="", sha=sha)
        storage.of(self.db).work.note(lot, "qa", note, "test")
        self.db.execute("UPDATE work_items SET reviewers = ARRAY[%s] WHERE id = %s",
                        (reviewer, lot))

    def test_witnesses_brief_verdict_and_refreeze(self):
        lot, sha = self.pending_lot("r1")
        # témoin (a) : after a rotation, the brief of r1 lists the lot, its
        # frozen head and the verdict command; r2 owes nothing
        section = _reviews_section(_brief(self, "r1"))
        self.assertIn("#%d [qa]" % lot, section)
        self.assertIn("verdict ok|blocked --sha %s" % sha, section)
        self.assertIn(reprise.NO_PENDING_REVIEW, _reviews_section(_brief(self, "r2")))
        rows = act.pending_reviews(self.db, "R1")
        self.assertEqual([(r["lot"], r["name"], r["sha"]) for r in rows],
                         [(lot, "r1", sha)])
        self.assertIsNotNone(rows[0]["designated_ts"])
        # témoin (b) : a verdict on the frozen head removes it
        work.milestone(self.db, lot, "verdict", sha=sha[:12], actor="r1", verdict="ok",
                       repo=self.repo.root, after_commit=[])
        self.assertEqual(act.pending_reviews(self.db, "r1"), [])
        self.assertIn(reprise.NO_PENDING_REVIEW, _reviews_section(_brief(self, "r1")))
        # témoin (c) : a new freeze designating r1 on a new head puts it back
        self.repo.write("docs/refreeze.md", "new head\n")
        head = self.repo.commit("new head")
        row = work.milestone(self.db, lot, "frozen", sha=head, actor="author",
                             repo=self.repo.root,
                             on_frozen=lambda tx, milestone, threads: None,
                             after_commit=[])
        self.designate(lot, head, int(row["id"]), "r1")
        self.assertEqual([(r["lot"], r["sha"]) for r in act.pending_reviews(self.db, "r1")],
                         [(lot, head)])
        self.assertIn(head, _reviews_section(_brief(self, "r1")))

    def test_a_manual_swap_moves_the_review(self):
        # #3004 itself : the pilot removed dev1 and designated dev2 by hand.
        # The removed reviewer owes nothing; the new one owes the frozen head,
        # and both readings (load and queue) agree.
        lot, sha = self.pending_lot("r1")
        store = storage.of(self.db).work
        store.designate_reviewer(lot, "r2", actor="pilot", event="reviewer r2 designated")
        store.designate_reviewer(lot, "r1", actor="pilot", event="reviewer r1 removed",
                                 remove=True)
        self.assertEqual(act.pending_reviews(self.db, "r1"), [])
        self.assertEqual([(r["lot"], r["sha"]) for r in act.pending_reviews(self.db, "r2")],
                         [(lot, sha)])
        loads = act._loads(self.db)
        self.assertNotIn("r1", loads)
        self.assertEqual(loads.get("r2"), 1)

    def test_a_validated_quorum_discharges_the_surplus_seat(self):
        # #4851 / #4825: three seats remain recorded after two sufficient
        # opinions. The third reviewer owes nothing once the lot is validated.
        lot, sha = self.pending_lot("r1")
        self.db.execute(
            "UPDATE work_items SET reviewers = ARRAY['r1', 'r2', 'r3'] WHERE id = %s",
            (lot,))
        for reviewer in ("r1", "r2"):
            self.db.execute(
                "INSERT INTO work_item_milestones"
                " (work_item_id, kind, sha, actor, verdict)"
                " VALUES (%s, 'verdict', %s, %s, 'ok')", (lot, sha, reviewer))
        # The same snapshot in qa still owes the remaining designated review.
        self.assertEqual([r["name"] for r in act.pending_reviews(self.db)], ["r3"])
        self.assertEqual(act._loads(self.db).get("r3"), 1)
        author_load = act._loads(self.db).get("author")
        self.db.execute("UPDATE work_items SET state = 'validated' WHERE id = %s", (lot,))
        self.assertEqual(act.pending_reviews(self.db), [])
        self.assertNotIn("r3", act._loads(self.db))
        self.assertEqual(act._loads(self.db).get("author"), author_load)
        reviews, _fixes = act.owed_work(self.db)
        alerts = slo.lot_steps(
            {"id": lot, "state": "validated", "priority": 2, "created_ts": 0.0,
             "assignee": "author"}, {}, [], now=100000.0, reviews=reviews)
        self.assertEqual([r for r in alerts if r["step"] == "green_verdict"], [])
        self.assertIn(reprise.NO_PENDING_REVIEW, _reviews_section(_brief(self, "r3")))
        # Returning to qa restores the due review without rewriting the seats.
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (lot,))
        self.assertEqual([r["name"] for r in act.pending_reviews(self.db)], ["r3"])

    def test_an_old_automatic_name_is_not_owed_on_the_new_head(self):
        # During the selection of a new freeze, the column still carries the
        # previous head's automatic names: they owe nothing on the new head.
        lot, _sha = self.pending_lot("r1")
        self.repo.write("docs/next.md", "next\n")
        head = self.repo.commit("next")
        work.milestone(self.db, lot, "frozen", sha=head, actor="author",
                       repo=self.repo.root,
                       on_frozen=lambda tx, milestone, threads: None,
                       after_commit=[])
        self.assertEqual(act.pending_reviews(self.db, "r1"), [])

    def test_the_command_defaults_to_the_bound_identity(self):
        # B de la relecture dev3 : sans AGENT, la commande lit l'identité liée
        # (elle levait UnboundLocalError) ; AGENT et --all restent possibles.
        lot, sha = self.pending_lot("r1")
        out = self.mesh("work", "pending-reviews", env=self.env(AGENT_MAIL_NAME="r1"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("#%d [qa]" % lot, out.stdout)
        self.assertIn(sha[:12], out.stdout)
        out = self.mesh("work", "pending-reviews", "r2")
        self.assertEqual((out.returncode, out.stdout.strip()),
                         (0, "no pending review for r2"), out.stderr)
        out = self.mesh("work", "pending-reviews", "--all", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('"name": "r1"', out.stdout)

    def test_an_unreadable_queue_is_said_never_empty(self):
        with mock.patch.object(self.db, "query", side_effect=RuntimeError("boom")):
            lines = reprise.pending_review_lines(self.db, "r1")
        self.assertEqual(len(lines), 1)
        self.assertIn("unreadable", lines[0])

    def test_the_idle_turn_carries_the_owed_reviews(self):
        # The « available » answer comes from the idle relaunch : it now names
        # the reviews still owed, and stays the plain prompt without any.
        # #3273 (rebase) : the relaunch names the team's pilot; this fake
        # worker has none, so the base prompt is `IDLE_PROMPT_NO_PILOT`.
        lot, sha = self.pending_lot("r1")
        worker = types.SimpleNamespace(db=self.db, name="r1",
                                       _idle_pilot=lambda: "")
        prompt = runner.AgentWorker._idle_prompt(worker)
        self.assertTrue(prompt.endswith(adapters.IDLE_PROMPT_NO_PILOT))
        self.assertIn(adapters.IDLE_REVIEWS_HEADER, prompt)
        self.assertIn("#%d" % lot, prompt)
        self.assertIn(sha, prompt)
        free = types.SimpleNamespace(db=self.db, name="r2",
                                     _idle_pilot=lambda: "")
        self.assertEqual(runner.AgentWorker._idle_prompt(free),
                         adapters.IDLE_PROMPT_NO_PILOT)


class GreenVerdictStepTest(unittest.TestCase):
    """§9 `green_verdict` : a designation without a verdict for 2 h relaunches
    the reviewer, then the pilot (hypothesis (3) of the lot: the step was
    declared but not measured, so nothing escalated on #3004)."""

    item = {"id": 7, "state": "qa", "priority": 2, "created_ts": 0.0,
            "assignee": "author"}

    def lines(self, elapsed: float, *, state="qa", priority=2):
        item = dict(self.item, state=state, priority=priority)
        return [r for r in slo.lot_steps(
            item, {}, [], now=1000.0 + elapsed,
            reviews=[{"name": "r1", "designated_ts": 1000.0, "sha": "abc1234"}])
            if r["step"] == "green_verdict"]

    def test_the_step_is_measured(self):
        step = slo.step_of("green_verdict")
        self.assertTrue(step.measured)
        self.assertEqual(step.source, slo.LOT_EVENTS)
        self.assertEqual((step.target_s, step.critical_s), (7200, 1800))

    def test_reviewer_then_pilot(self):
        (line,) = self.lines(3600)
        self.assertEqual((line["level"], line["reviewer"]), (0, "r1"))
        (line,) = self.lines(7200)
        self.assertEqual((line["level"], tuple(line["targets"])), (1, ("assignee",)))
        (line,) = self.lines(4 * 3600)
        self.assertEqual((line["level"], tuple(line["targets"])), (2, ("pilot",)))
        (line,) = self.lines(1800, priority=1)
        self.assertEqual(line["level"], 1)

    def test_a_closed_lot_owes_no_review(self):
        self.assertEqual(self.lines(9000, state="merged"), [])

    def test_the_handover_grace_does_not_silence_a_reviewer(self):
        events = [{"note": "assigned to author (before: other)", "created_ts": 1000.0 + 7000}]
        rows = [r for r in slo.lot_steps(
            dict(self.item), {}, events, now=1000.0 + 7300,
            reviews=[{"name": "r1", "designated_ts": 1000.0}])
            if r["step"] == "green_verdict"]
        self.assertEqual(rows[0]["level"], 1)

    def test_the_brief_names_the_reviewer_and_the_gesture(self):
        alert = {"lot": 7, "title": "t", "lot_state": "qa", "level": 1,
                 "step": "green_verdict", "step_label": "x", "agent": "r1",
                 "sha": "abc1234", "value": 8000, "threshold": 7200}
        text = alert_briefs._slo_breach(alert)
        self.assertIn("owed by reviewer r1", text)
        self.assertIn("ameesh work milestone 7 verdict ok|blocked --sha abc1234", text)
        text = alert_briefs._slo_breach(dict(alert, level=2))
        self.assertIn("pilot: reviewer r1", text)
        self.assertIn("ameesh work reviewer 7 <agent>", text)
        self.assertNotIn("ameesh work assign", text)
