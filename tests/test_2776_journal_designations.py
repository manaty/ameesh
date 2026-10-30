# SPDX-License-Identifier: AGPL-3.0-only
"""#2776 — the manual reviewer designation is read from the whole journal.

#2739 reads a manual reviewer's retired seat from the journal. Two
non-blocking defects remained: a designation written by `work move qa
--reviewer` was ignored when its actor had only the recent assigner history
the write path admits, and the read was bounded to the last 500 events, so a
talkative lot could push an old designation out of the window. Both defects
went the strict way (a reviewer was treated as an author), never the unsafe
one.
"""
from __future__ import annotations

from ameesh import registry, work
from ameesh import review_designation_activation as act

from .support import PgTestCase


class JournalDesignationsTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name, harness in (("auteur", "deepseek"), ("relecteur", "codex"),
                              ("tiers", "claude")):
            registry.upsert(self.db, name, harness=harness, host=self.cfg.host,
                            cwd=self.tmp, status="idle")
        # #4779 : désigner à la main exige une fiche qui prouve `propose` et un
        # rôle ; le refus d'une fiche absente/illisible est couvert par
        # test_4779, ici la fiche de test porte la capacité.
        self.db.execute(
            "UPDATE agent_registry SET capabilities = ARRAY['read', 'propose'],"
            " roles = ARRAY['implementation'] WHERE name = 'relecteur'")

    def _lot(self, assignee="auteur"):
        item = work.add(self.db, title="journal", app="ameesh", assignee=assignee)
        return int(item["id"])

    def _assigner_history(self, actor="auteur"):
        # `corrector` admits an agent whose organisation declares no role when
        # it recently handed a lot over (`history=True`): reproduce that.
        other = self._lot(assignee="tiers")
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'build', 'assigned to tiers', %s)", (other, actor))

    def test_a_designation_written_at_qa_entry_is_read_after_retirement(self):
        # #2776: `work move qa --reviewer` is a designation path, not only
        # `work reviewer`; its actor is admitted by the write path (recent
        # assigner history), so the journal read admits it too.
        self._assigner_history()
        lot = self._lot()
        work.move(self.db, lot, "build")
        out = work.move(self.db, lot, "qa", reviewer="relecteur", actor="auteur")
        self.assertEqual(out["reviewer"], "relecteur")
        # the pilot retires the seat after the first-round correction (#2076)
        self.db.execute("UPDATE work_items SET reviewers = '{}'::text[],"
                        " reviewer = '' WHERE id = %s", (lot,))
        item = work.get(self.db, lot)
        self.assertEqual(act._journal_reviewers(self.db, lot), ["relecteur"])
        self.assertEqual(act._designated_reviewers(self.db, item, None, "auteur"),
                         {"relecteur"})

    def test_a_designation_older_than_the_last_500_events_is_read(self):
        lot = self._lot()
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'qa', 'reviewer relecteur designated', 'ameesh')", (lot,))
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " SELECT %s, 'qa', 'bruit ' || g, 'ameesh'"
            " FROM generate_series(1, 620) g", (lot,))
        # the former `events(lot, 500)` window no longer carries the line...
        self.assertNotIn("reviewer relecteur designated",
                         [e["note"] for e in work.events(self.db, lot, 500)])
        # ...but the prefix read does, whatever the journal length.
        self.assertEqual(act._journal_reviewers(self.db, lot), ["relecteur"])
