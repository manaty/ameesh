# SPDX-License-Identifier: AGPL-3.0-only
"""#5066 — un relecteur dont un lot est empilé sur le gel : exclu, mais pour un doute.

Cas réel #5029 (gel #28737, b43e3a7fe227) : ameesh-cx3 travaillait #5038 sur une
branche empilée sur le premier gel de #5029 (fe75128d3). Son commit 60438e212,
rattaché à #5038 par son tour, descend de ce gel et n'est pas dans la tête :
`_rewritten_after_freeze` (#4457) le lit comme un correctif réécrit.

Aucune preuve ne sépare ce commit d'un correctif repris par l'auteur : une
réécriture peut ne garder aucune ligne commune (B cx2/cx3 sur les têtes
034d681af, 622f066bb et e08f3450a). L'exclusion reste donc (canon §2) ; la porte
dit le doute, nomme le lot empilé et laisse la famille éligible.
"""
from __future__ import annotations

from ameesh import merge_queue, registry, work

from .support import PgTestCase
from .test_2447_review_designation_activation import _ActivationFixture

FAMILIES = ("claude", "codex", "deepseek")


class StackedLotNotCorrectorTest(_ActivationFixture, PgTestCase):
    """Un lot gelé, un autre lot de cx3 empilé sur le gel, puis un re-gel."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "cx3", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh'"
                        " WHERE name = %s", ("cx3",))
        self.other = int(work.add(self.db, title="Lot empilé de cx3",
                                  app="ameesh", assignee="cx3")["id"])

    def commit_receipt(self, actor, harness, sha, *, work_item_id=None):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id, at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, now() - interval '10 minutes')"
            " ON CONFLICT (subject) DO NOTHING",
            ("commit:" + sha, sha, actor, "s-" + harness, "t", harness,
             work_item_id))

    def verdict_receipt(self, mark, harness):
        self.db.execute(
            "INSERT INTO provenance_receipts"
            " (subject, sha, actor, session, turn, harness, work_item_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (subject) DO NOTHING",
            ("milestone:%d" % int(mark["id"]), self.frozen, mark["actor"],
             "s-" + harness, "t", harness, self.lot))

    def _stack(self, *, receipt_lot, other_head=True):
        """F1, le commit de cx3 empilé sur F1, puis F2 de l'auteur sans lui."""
        head_a = self.frozen
        self.freeze()                       # F1
        work.milestone(self.db, self.lot, "verdict", sha=head_a, actor="cx3",
                       verdict="ok")
        # cx3 travaille son propre lot sur une branche partie de F1.
        self.repo.write("src/ameesh/autre.py", "lot empilé de cx3\n")
        stacked = self.repo.commit("cx3: travail du lot empilé", agent="cx3")
        self.commit_receipt("cx3", "codex", stacked,
                            work_item_id=self.lot if receipt_lot == "this"
                            else self.other if receipt_lot == "other" else None)
        if other_head:
            self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                            (stacked, self.other))
        # L'auteur continue depuis F1 : le commit de cx3 n'est pas dans F2.
        self.repo.git("reset", "--hard", head_a)
        self.repo.write("src/ameesh/mail.py", "lot\nauteur : suite\n")
        head_b = self.repo.commit("author: suite du lot", agent="author")
        self.receipts(commits=[head_b])
        self.frozen = head_b
        self.freeze()                       # F2
        return head_b

    def gate(self, head):
        for actor, harness in (("cx3", "codex"), ("r1", "claude")):
            mark = work.milestone(self.db, self.lot, "verdict", sha=head,
                                  actor=actor, verdict="ok")
            self.verdict_receipt(mark, harness)
        klass = {"class": "sensitive", "doubt": False,
                 "files": [{"path": "src/ameesh/mail.py",
                            "class": "sensitive",
                            "rule": ["src/ameesh/*"]}],
                 "repo": self.repo.root, "target": self.base,
                 "base_sha": self.base}
        out = merge_queue.review_gate(
            self.db, work.milestones(self.db, self.lot), head,
            repo=self.repo.root, klass=klass, item=work.get(self.db, self.lot),
            required=2, families=FAMILIES)
        self.assertFalse(out.get("error"), out)
        return out

    def rejected_row(self, out):
        return [row for row in out["rejected"] if row["actor"] == "cx3"][0]

    def test_a_stacked_commit_excludes_and_says_the_doubt(self):
        out = self.gate(self._stack(receipt_lot="other"))
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)
        row = self.rejected_row(out)
        self.assertEqual(row["reason"], "stacked-corrector")
        self.assertEqual(row["stacked_lot"], self.other)
        self.assertIn("doubt", out["why"])
        self.assertNotIn("reviewer who corrected the head", out["why"])

    def test_the_stacked_lot_is_also_proven_by_its_freeze(self):
        head = self._stack(receipt_lot="other", other_head=False)
        stacked = self.db.query(
            "SELECT sha FROM provenance_receipts WHERE actor = 'cx3'"
            " AND subject LIKE 'commit:%%'")[0]["sha"]
        work.milestone(self.db, self.other, "frozen", sha=stacked, actor="cx3")
        self.assertEqual(self.rejected_row(self.gate(head))["stacked_lot"],
                         self.other)

    def test_the_family_stays_eligible_for_another_reviewer(self):
        # L'exclusion est nominative : un autre codex relit et la porte s'ouvre.
        head = self._stack(receipt_lot="other")
        registry.upsert(self.db, "cx4", harness="codex", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        mark = work.milestone(self.db, self.lot, "verdict", sha=head,
                              actor="cx4", verdict="ok")
        self.verdict_receipt(mark, "codex")
        out = self.gate(head)
        self.assertIn("cx3", out["corrections"])
        self.assertTrue(out["ok"], out)

    def test_a_receipt_without_a_lot_reads_it_from_its_turn(self):
        # #5055 : le chemin de production (#5029) : hook post-commit, lot NULL
        # sur le reçu, lot lu dans la ligne `turn_costs` du tour.
        self.db.execute(
            "INSERT INTO turn_costs (agent, harness, turn_id, work_item_id)"
            " VALUES ('cx3', 'codex', 't', %s)", (self.other,))
        row = self.rejected_row(self.gate(self._stack(receipt_lot=None)))
        self.assertEqual(row["reason"], "stacked-corrector")
        self.assertEqual(row["stacked_lot"], self.other)

    def test_an_unreadable_ancestry_keeps_the_gate_closed_and_says_so(self):
        # #5055 : clone partiel, tête de l'autre lot absente : la trace n'est
        # pas muette, et l'exclusion demeure.
        head = self._stack(receipt_lot="other")
        self.db.execute("UPDATE work_items SET branch_head = %s WHERE id = %s",
                        ("f" * 40, self.other))
        out = self.gate(head)
        self.assertFalse(out["ok"], out)
        row = self.rejected_row(out)
        self.assertEqual(row["reason"], "corrector")
        self.assertEqual(row["stacked_unread"], self.other)

    def _assert_plain_corrector(self, out):
        self.assertIn("cx3", out["corrections"])
        self.assertFalse(out["ok"], out)
        self.assertEqual(self.rejected_row(out)["reason"], "corrector")
        self.assertNotIn("stacked_lot", self.rejected_row(out))

    def test_another_lot_without_a_head_holding_the_commit_is_a_correction(self):
        self._assert_plain_corrector(
            self.gate(self._stack(receipt_lot="other", other_head=False)))

    def test_a_receipt_without_a_lot_is_a_correction(self):
        self._assert_plain_corrector(self.gate(self._stack(receipt_lot=None)))

    def test_a_receipt_of_this_lot_is_a_correction(self):
        # Le correctif réécrit de #4457, même si une tête voisine le contient.
        self._assert_plain_corrector(self.gate(self._stack(receipt_lot="this")))
