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
