# SPDX-License-Identifier: AGPL-3.0-only
"""#3602 — une re-désignation manuelle survit au gel qui la suit.

`work_items.reviewers` porte les noms automatiques des marques de gel et les
noms désignés à la main. `_manual()` et `_sync_reviewers` triaient les deux
par le seul nom : un relecteur déjà nommé par une marque automatique
antérieure, puis re-désigné à la main (`ameesh work reviewer`), était compté
comme automatique et retiré en silence au gel suivant — ou à la reprise du
même gel. Témoin réel : #3576, auditeur re-désigné à 18:07Z, effacé à 18:30Z
par la reprise du gel #19367.

Le geste manuel est la ligne de journal datée `reviewer X designated`
(écrite par `work reviewer`, `work move qa --reviewer`) : un nom n'est retiré
que si aucune désignation manuelle de ce nom ne la suit. La même règle
compte les sièges manuels de la couverture et les avis dus (le relecteur
reste relancé).
"""
from __future__ import annotations

from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import registry, work
from ameesh.work_reviewers import designated

from . import test_2447_review_designation_activation as t2447
from .support import PgTestCase

HUMAN = "human:smichea"


class ManualRedesignationTest(t2447._ActivationFixture, PgTestCase):
    def _new_head(self, path: str = "src/ameesh/work.py") -> None:
        # A distinct content each call: a second head must be a real commit.
        self.repo.write(path, "another lot %s\n" % (self.frozen or "base"))
        self.frozen = self.repo.commit("another lot")
        self.receipts()

    def _families(self, harness: str) -> None:
        for name in ("r1", "r2"):
            registry.upsert(self.db, name, harness=harness)

    def test_a_replayed_waiting_freeze_keeps_the_manual_redesignation(self):
        # Témoin #3576 : F1 nomme r1, r2 ; F2 (nouvelle tête, aucune famille)
        # vide la colonne ; le pilote re-désigne r1 à la main ; la reprise de
        # F2 ne doit pas l'effacer en silence.
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        self._new_head()
        self._families("deepseek")
        _, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1", actor=HUMAN)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1"])
        before = len(self.notes())
        replay = act.designate(self.cfg, self.db, self.lot)
        self.assertEqual(replay["state"], rd.WAITING, replay)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1"])
        # rien n'est retiré : aucune note ne dit avoir effacé r1
        cleared = [note for note in self.notes()[before:]
                   if "cleared" in note and "r1" in note]
        self.assertEqual(cleared, [], self.notes()[before:])
        # le relecteur re-désigné reste relancé sur la tête gelée
        self.assertEqual(
            [(row["lot"], row["sha"]) for row in act.pending_reviews(self.db, "r1")],
            [(self.lot, self.frozen)])

    def test_the_next_freeze_keeps_the_manual_redesignation_and_it_stays_owed(self):
        # Le gel suivant garde le siège manuel ; il compte comme manuel pour
        # la couverture et l'avis reste dû (le relecteur est relancé).
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1", actor=HUMAN)
        self._new_head()
        self._families("deepseek")
        _, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1"])
        self.assertEqual(act._manual(self.db, work.get(self.db, self.lot)), ["r1"])
        cleared = [note for note in self.notes() if "cleared" in note]
        self.assertTrue(cleared, self.notes())
        self.assertIn("r2", cleared[-1])
        self.assertNotIn("r1", cleared[-1])
        self.assertEqual(
            [(row["lot"], row["sha"]) for row in act.pending_reviews(self.db, "r1")],
            [(self.lot, self.frozen)])

    def test_a_later_automatic_mark_rearms_the_retirement(self):
        # Le pilote retire son siège manuel, puis une marque plus récente
        # nomme de nouveau r1 : c'est redevenu un nom automatique, le gel
        # suivant doit pouvoir le retirer.
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1", actor=HUMAN)
        work.designate_reviewer(self.cfg, self.db, self.lot, "r1", actor=HUMAN,
                                remove=True)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r2"])
        self._new_head()
        _, again = self.freeze()
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r1", "r2"])
        self._new_head()
        self._families("deepseek")
        _, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        self.assertEqual(act._manual(self.db, work.get(self.db, self.lot)), [])
        self.assertTrue([note for note in self.notes()
                         if "cleared" in note and "r1" in note])

    def test_a_forged_journal_line_does_not_protect_a_seat(self):
        # La ligne de journal est un texte : seule une autorité de désignation
        # la rend manuelle. Une note libre d'un agent ordinaire ne peut pas
        # conserver un siège automatique.
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'qa', 'reviewer r1 designated', 'intruder')",
            (self.lot,))
        self._new_head()
        self._families("deepseek")
        _, waiting = self.freeze()
        self.assertEqual(waiting["state"], rd.WAITING, waiting)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        self.assertEqual(act._manual(self.db, work.get(self.db, self.lot)), [])
