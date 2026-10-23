# SPDX-License-Identifier: AGPL-3.0-only
"""#2873 — la porte du gel et du verdict vit dans `freeze_gate`, sans changement.

Le gel (`frozen`) et le verdict (`verdict`) sont les deux jalons qui prouvent
une tête relue. Leur écriture, leur lecture et la résolution des têtes
quittent `work.py` (touché par presque tous les lots) pour `freeze_gate.py`,
déclaré sensible à deux avis au canon (§8) ; `work.py` réexporte les noms,
donc les appelants et les tests existants ne changent pas.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import freeze_gate, work

HEAD = "a" * 40
OTHER = "b" * 40


class _Store:
    """Le seul morceau de stockage lu par la porte."""

    def __init__(self, rows):
        self.rows = rows
        self.work = mock.Mock()
        self.work.milestones.return_value = rows


class ReexportTest(unittest.TestCase):
    """`work` garde l'API publique des jalons : le déplacement est invisible."""

    def test_work_reexports_the_gate_entry_points(self):
        self.assertIs(work.milestone, freeze_gate.milestone)
        self.assertIs(work.milestones, freeze_gate.milestones)
        self.assertIs(work.freeze_head, freeze_gate.freeze_head)
        self.assertIs(work.reviewed_head, freeze_gate.reviewed_head)
        self.assertIs(work.frozen_head, freeze_gate.frozen_head)
        self.assertIs(work._frozen_head, freeze_gate.frozen_head)
        self.assertIs(work.FREEZE_ABSENT_MARK, freeze_gate.FREEZE_ABSENT_MARK)
        self.assertIs(work.FREEZE_ABSENT_LINE, freeze_gate.FREEZE_ABSENT_LINE)
        self.assertIs(work.FREEZE_UNPUBLISHED_MARK,
                      freeze_gate.FREEZE_UNPUBLISHED_MARK)
        self.assertIs(work.FREEZE_UNVERIFIED_MARK,
                      freeze_gate.FREEZE_UNVERIFIED_MARK)
        self.assertEqual(work.REVIEWED_HEAD_MIN, freeze_gate.REVIEWED_HEAD_MIN)
        self.assertEqual(work.FROZEN_HEAD_MIN, freeze_gate.FROZEN_HEAD_MIN)
        self.assertEqual(work.MILESTONE_KINDS, freeze_gate.MILESTONE_KINDS)
        self.assertEqual(work.MANUAL_KINDS, freeze_gate.MANUAL_KINDS)
        self.assertEqual(work.VERDICTS, freeze_gate.VERDICTS)

    def test_work_milestone_is_the_gate_write(self):
        with self.assertRaises(work.WorkError) as caught:
            work.milestone(object(), 1, "bogus", actor="auteur")
        self.assertIn("unknown milestone", str(caught.exception))


class HeadTest(unittest.TestCase):
    """Les têtes complètes passent ; une tête absente est refusée, nommée."""

    def test_a_full_frozen_head_is_accepted(self):
        with mock.patch.object(freeze_gate, "_head_repos", return_value=[]):
            self.assertEqual(
                freeze_gate.freeze_head(object(), {"id": 1}, HEAD), HEAD)

    def test_a_full_reviewed_head_is_accepted_when_it_is_the_frozen_head(self):
        # #2962 : une tête complète doit se résoudre, ou nommer la tête gelée
        # courante — c'est le cas d'un relecteur qui n'a pas le dépôt.
        with mock.patch.object(freeze_gate, "frozen_head", return_value=OTHER):
            self.assertEqual(
                freeze_gate.reviewed_head(object(), {"id": 1}, OTHER), OTHER)

    def test_a_verdict_without_head_is_refused_with_the_gate_error(self):
        with mock.patch.object(freeze_gate, "frozen_head", return_value=""):
            with self.assertRaises(work.WorkError) as caught:
                freeze_gate.reviewed_head(object(), {"id": 1}, "")
        self.assertIn("head-missing", str(caught.exception))
        self.assertIn("no frozen head", str(caught.exception))


class ReadTest(unittest.TestCase):
    """La lecture des jalons et de la dernière tête gelée passe par la porte."""

    def test_the_latest_freeze_is_read_through_the_gate(self):
        rows = [
            {"id": 1, "kind": "frozen", "sha": HEAD, "at_ts": 10},
            {"id": 2, "kind": "verdict", "sha": HEAD, "at_ts": 20},
            {"id": 3, "kind": "frozen", "sha": OTHER, "at_ts": 30},
        ]
        store = _Store(rows)
        with mock.patch.object(freeze_gate.storage, "of", return_value=store):
            self.assertEqual(freeze_gate.milestones(object(), 7), rows)
            self.assertEqual(freeze_gate.frozen_head(object(), 7), OTHER)
        store.work.milestones.assert_called_with(7, 100)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
