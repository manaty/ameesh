# SPDX-License-Identifier: AGPL-3.0-only
"""#5328 : le débit prend ses corrections dues de la même lecture que
`ameesh work fixes-owed` (le calcul unique #3288).

Témoin #5307 : tête gelée 9d77ca83e7e9, avis `blocked` puis `ok` du même
relecteur deepseek5 sur cette tête, plus rev-02 ok. `work fixes-owed claude1`
ne devait rien, mais `debit --json` annonçait claude1 au repos avec
`fixes: [5307]` et un geste de relance : le débit lisait le dernier `blocked`
de la tête sans voir le `ok` postérieur du même relecteur. Le calcul unique
(#3288) tranche par le **dernier verdict de chaque acteur** ; le débit doit le
lire, pas le refaire.
"""
from __future__ import annotations

import json
import time
import unittest

from ameesh import debit
from ameesh import review_designation_activation as activation
from ameesh import work

from .support import PgTestCase

H = 3600.0
#: La tête gelée du témoin #5307 (préfixe réel, complétée à 40).
SHA = "9d77ca83e7e9" + "0" * 28


class DebitCorrectionDueTest(PgTestCase):
    """Une tête gelée, son relecteur, et le dernier mot qu'il y a dit."""

    def setUp(self) -> None:
        super().setUp()
        for name, harness in (("claude1", "claude"), ("deepseek5", "deepseek")):
            self.db.execute(
                "INSERT INTO agent_registry (name, status, host, harness) "
                "VALUES (%s, 'idle', 'ameesh-agents-2', %s)", (name, harness))

    def seed(self, lot: int, *, ok_after_block: bool, age: float = 6 * H,
             sha: str = SHA) -> int:
        """Le lot #5307 : claude1 assigné, deepseek5 relecteur désigné.

        `ok_after_block` dit si le relecteur se ravise sur la même tête —
        exactement ce que `work fixes-owed` interprète comme « plus rien dû ».
        Rend l'identifiant de base, que `work.add` attribue.
        """
        item = work.add(self.db, title="lot #%d" % lot, app="ameesh",
                        assignee="claude1", priority=3)
        ident = int(item["id"])
        work.move(self.db, ident, "build")
        work.move(self.db, ident, "qa")
        now = time.time()
        freeze_at = now - age
        self.db.execute(
            "INSERT INTO work_item_milestones "
            "(id, work_item_id, kind, at, sha, actor, note) "
            "VALUES (%s, %s, 'frozen', to_timestamp(%s), %s, 'author', '')",
            (ident * 10, ident, freeze_at, sha))
        self.db.execute(
            "INSERT INTO work_item_events "
            "(work_item_id, state, note, actor, created_at) "
            "VALUES (%s, 'qa', %s, 'ameesh', to_timestamp(%s))",
            (ident, "review-designation:%d:%d: %s" % (
                ident, ident * 10,
                json.dumps({"reviewers": [{"name": "deepseek5"}],
                            "authors": [{"name": "author", "family": ""}]})),
             freeze_at + 60))
        self.db.execute(
            "INSERT INTO work_item_milestones "
            "(id, work_item_id, kind, at, sha, actor, verdict, note) "
            "VALUES (%s, %s, 'verdict', to_timestamp(%s), %s, 'deepseek5', "
            "'blocked', '')",
            (ident * 10 + 1, ident, now - age + 300, sha))
        if ok_after_block:
            self.db.execute(
                "INSERT INTO work_item_milestones "
                "(id, work_item_id, kind, at, sha, actor, verdict, note) "
                "VALUES (%s, %s, 'verdict', to_timestamp(%s), %s, 'deepseek5', "
                "'ok', '')",
                (ident * 10 + 2, ident, now - age + 600, sha))
        return ident

    def report(self) -> dict:
        return debit.read(self.db, cfg=self.cfg, canons=[],
                          installed="1.21.0", jobs=False)

    def idle(self, report: dict) -> dict:
        return {row["agent"]: row for row in report["idle_agents_with_work"]}

    def test_a_later_ok_of_the_same_reviewer_owes_no_fix(self):
        # Le témoin #5307 : blocked puis ok de deepseek5 sur la même tête.
        ident = self.seed(5307, ok_after_block=True)
        self.assertEqual(activation.due_fixes(self.db), [])
        report = self.report()
        self.assertEqual(report["blocked_reviews"], [])
        self.assertEqual(report["idle_agents_with_work"], [])
        self.assertNotIn(
            (ident, "blocking_review"),
            [(row["lot"], row["kind"]) for row in report["slow_waits"]])
        qa = [row for row in report["qa"] if row["lot"] == ident][0]
        self.assertTrue(qa["ready"])

    def test_a_standing_block_is_still_a_fix(self):
        # Sans le ok, le même lot reste une correction due : la correction ne
        # vide pas la liste, elle retire seulement le faux positif.
        ident = self.seed(5310, ok_after_block=False)
        owed = activation.due_fixes(self.db)
        self.assertEqual([(row["lot"], row["name"]) for row in owed],
                         [(ident, "claude1")])
        report = self.report()
        self.assertEqual([row["lot"] for row in report["blocked_reviews"]], [ident])
        self.assertEqual(self.idle(report)["claude1"]["fixes"], [ident])
        self.assertIn(
            (ident, "blocking_review"),
            [(row["lot"], row["kind"]) for row in report["slow_waits"]])

    def test_debit_fixes_are_the_fixes_owed_rows(self):
        # Les deux moitiés dans la même passe : le faux positif (#5307) et le
        # vrai blocage. Le rapport ne nomme que ce que `fixes-owed` nomme.
        ok = self.seed(5307, ok_after_block=True)
        standing = self.seed(5310, ok_after_block=False)
        owed = activation.due_fixes(self.db)
        report = self.report()
        debit_fixes = {(row["agent"], lot)
                       for row in report["idle_agents_with_work"]
                       for lot in row["fixes"]}
        self.assertEqual(debit_fixes,
                         {(row["name"], row["lot"]) for row in owed})
        self.assertEqual({row["lot"] for row in report["blocked_reviews"]},
                         {row["lot"] for row in owed})
        self.assertNotIn(("claude1", ok), debit_fixes)
        self.assertIn(("claude1", standing), debit_fixes)


if __name__ == "__main__":
    unittest.main()
