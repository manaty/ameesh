# SPDX-License-Identifier: AGPL-3.0-only
"""#753 : à l'arrêt d'un agent, ses lots ouverts retournent à la file d'équipe
(assigné effacé, journal avec la raison) ; un agent ne les garde pas."""
from __future__ import annotations

from ameesh import registry, storage, work

from .support import PgTestCase


class _Base(PgTestCase):
    def agent(self, name, *, status="idle", team="nexlink"):
        registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, team = %s,"
            " responsible = 'human:proprio' WHERE name = %s",
            (status, team, name))

    def lot(self, title, *, assignee=None):
        item = work.add(self.db, title=title, app="nexlink")
        if assignee:
            work.assign(self.db, item["id"], assignee, cfg=self.cfg)
        return work.get(self.db, item["id"])


class StopReleasesLotsTest(_Base):
    def test_release_open_lots_with_journal_and_idempotence(self):
        self.agent("dev1")
        lots = [self.lot("A", assignee="dev1"),
                self.lot("B", assignee="dev1"),
                self.lot("C", assignee="dev1")]
        registry.set_status(self.db, "dev1", "stopped", status_text="stopped by hand",
                            stop_reason="manuel")
        done = work.release_lots(self.db, "dev1", reason="stopped (#753)", actor="stop")
        self.assertEqual({row["id"] for row in done}, {lot["id"] for lot in lots})
        for lot in lots:
            self.assertIsNone(work.get(self.db, lot["id"])["assignee"])
        ev = self.db.query(
            "SELECT note, actor FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (lots[0]["id"],))[0]
        self.assertIn("#753", ev["note"])
        self.assertEqual(ev["actor"], "stop")
        # idempotent : plus rien à rendre
        self.assertEqual(work.release_lots(self.db, "dev1", reason="again", actor="stop"), [])

    def test_release_leaves_other_agents_and_closed_lots(self):
        self.agent("dev1")
        self.agent("dev2")
        mine = self.lot("mine", assignee="dev1")
        theirs = self.lot("theirs", assignee="dev2")
        merged = self.lot("merged", assignee="dev1")
        work.merged(self.db, merged["id"], sha="deadbeef" * 5, note="", actor="test")
        done = work.release_lots(self.db, "dev1", reason="r", actor="a")
        self.assertEqual([row["id"] for row in done], [mine["id"]])
        self.assertEqual(work.get(self.db, theirs["id"])["assignee"], "dev2")
        self.assertEqual(work.get(self.db, merged["id"])["assignee"], "dev1")

    def test_unassigned_lot_is_untouched(self):
        self.agent("dev1")
        free = self.lot("free")
        self.assertEqual(work.release_lots(self.db, "dev1", reason="r", actor="a"), [])
        self.assertIsNone(work.get(self.db, free["id"])["assignee"])


class PromotedMeasurementTest(_Base):
    """#753 (2e incrément) : un lot promu dont la mesure reste à refaire ne
    reste pas avec l'agent arrêté — il va à l'acteur de la mesure (auditeur)."""

    def measure(self, item_id, *, offset_s=0.0):
        self.db.execute(
            "INSERT INTO work_item_measures (work_item_id, started_at, merged_at,"
            " total_minutes, agent_minutes, review_minutes, human_minutes)"
            " VALUES (%s, now(), now(), 1, 1, 0, 0)", (item_id,))
        if offset_s:
            self.db.execute(
                "UPDATE work_item_measures SET measured_at = now() + make_interval(secs => %s)"
                " WHERE work_item_id = %s", (float(offset_s), item_id))

    def promoted(self, assignee="dev1", *, measure_before=True):
        self.agent(assignee)
        lot = self.lot("deployed", assignee=assignee)
        work.merged(self.db, lot["id"], sha="a" * 40, actor=assignee)
        if measure_before:
            self.measure(lot["id"], offset_s=-3600)
        work.move(self.db, lot["id"], "promoted", note="déployé", actor="auditeur")
        if not measure_before:
            self.measure(lot["id"], offset_s=3600)
        return lot

    def test_measurement_actor_is_the_auditor(self):
        self.agent("auditeur")
        self.assertEqual(work.measurement_actor(self.db, self.cfg), "auditeur")

    def test_pending_measure_goes_to_the_measure_actor(self):
        lot = self.promoted()
        done = work.release_lots(self.db, "dev1", reason="stopped (#753)", actor="stop",
                                 measure_actor="auditeur",
                                 measure_reason="measurement to redo (#753)")
        self.assertEqual([row["id"] for row in done], [lot["id"]])
        item = work.get(self.db, lot["id"])
        self.assertEqual((item["state"], item["assignee"]), ("promoted", "auditeur"))
        ev = self.db.query(
            "SELECT note, actor FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (lot["id"],))[0]
        self.assertIn("measurement to redo", ev["note"])
        self.assertEqual(ev["actor"], "stop")
        # idempotent : le lot a changé d'assigné
        self.assertEqual(work.release_lots(self.db, "dev1", reason="again", actor="stop",
                                           measure_actor="auditeur",
                                           measure_reason="m"), [])

    def test_measure_taken_after_promotion_keeps_the_lot(self):
        lot = self.promoted(measure_before=False)
        self.assertEqual(work.release_lots(self.db, "dev1", reason="r", actor="a",
                                           measure_actor="auditeur", measure_reason="m"), [])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_no_measure_at_all_is_pending(self):
        self.agent("dev1")
        lot = self.lot("never measured", assignee="dev1")
        work.merged(self.db, lot["id"], sha="c" * 40, actor="dev1")
        work.move(self.db, lot["id"], "promoted", note="déployé", actor="auditeur")
        done = work.release_lots(self.db, "dev1", reason="r", actor="a",
                                 measure_actor="auditeur", measure_reason="m")
        self.assertEqual([row["id"] for row in done], [lot["id"]])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "auditeur")

    def test_measure_actor_stopped_agent_leaves_the_lot(self):
        lot = self.promoted(assignee="auditeur")
        done = work.release_lots(self.db, "auditeur", reason="r", actor="a",
                                 measure_actor="auditeur", measure_reason="m")
        self.assertEqual(done, [])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "auditeur")


class DryRunTest(_Base):
    def test_dry_run_returns_candidates_without_writing(self):
        self.agent("dev1")
        open_lot = self.lot("open", assignee="dev1")
        done = work.release_lots(self.db, "dev1", reason="r", actor="a", dry_run=True)
        self.assertTrue(done and all(row.get("dry_run") for row in done))
        self.assertEqual({row["id"] for row in done}, {open_lot["id"]})
        self.assertEqual(work.get(self.db, open_lot["id"])["assignee"], "dev1")
