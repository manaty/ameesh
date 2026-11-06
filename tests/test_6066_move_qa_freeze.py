# SPDX-License-Identifier: AGPL-3.0-only
"""#6066 — « move it to qa » ne gèle pas.

La consigne de réveil disait « move it to qa » ; `work move <id> qa` laissait
un lot à branche jamais gelé en qa, « waiting: freeze », sans relecteur, et
sa CI était refusée (« not frozen »). La consigne nomme le gel, et le move
d'un tel lot l'avertit (sortie et journal) avec la commande de gel.
"""
from __future__ import annotations

from ameesh import adapters, storage, work

from .support import PgTestCase

HEAD = "c" * 40


class WakePromptTest(PgTestCase):
    def test_wake_prompt_names_the_freeze(self):
        text = adapters.LOT_WAKE_PROMPT % dict(id=42, title="t", state="build")
        self.assertIn("ameesh work milestone 42 frozen --sha", text)
        self.assertNotIn("move it to qa.", text)


class MoveQaWithoutFreezeTest(PgTestCase):
    def lot(self, **kw):
        ident = work.add(self.db, title="lot #6066", app="ameesh", **kw)["id"]
        work.move(self.db, ident, "build")
        return ident

    def test_branch_without_freeze_warns_with_the_freeze_command(self):
        ident = self.lot(branch="lot/6066")
        row = work.move(self.db, ident, "qa")
        self.assertEqual(row["state"], "qa")
        self.assertIn("no freeze", row["freeze_warning"])
        self.assertIn("ameesh work milestone %d frozen --sha" % ident, row["freeze_warning"])
        notes = [e["note"] for e in work.events(self.db, ident)]
        self.assertTrue(any("ameesh work milestone %d frozen" % ident in n for n in notes))

    def test_frozen_lot_returning_to_qa_still_moves(self):
        ident = self.lot(branch="lot/6066")
        storage.of(self.db).work.add_milestone(ident, "frozen", sha=HEAD, actor="author",
                                               verdict=None, note="")
        self.assertEqual(work.get(self.db, ident)["state"], "qa")
        work.move(self.db, ident, "build")
        self.assertNotIn("freeze_warning", work.move(self.db, ident, "qa"))

    def test_lot_without_branch_has_no_warning(self):
        ident = self.lot()
        self.assertNotIn("freeze_warning", work.move(self.db, ident, "qa"))
