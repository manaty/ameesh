# SPDX-License-Identifier: AGPL-3.0-only
"""#3557 — un plancher posé après le gel complète la désignation du gel.

Constat #3536 : le gel désignait une famille (barème 1) ; le pilote pose
`work reviewer 3536 --floor 2`, le plancher est écrit mais la désignation
enregistrée ne le relit pas (`work reviewers --apply` répond required=1). La
porte exige pourtant max(barème, plancher) : le lot attendait un second avis
que personne n'était chargé de donner.

Témoins : plancher 2 après un gel à 1 → une seconde famille est désignée et
notifiée, la marque porte required=2 ; plancher sans gel → rien ; plancher
déjà couvert → rien ; `reviewers --apply` complète un plancher écrit sans
complétion (version antérieure) ; un plancher non couvert ne retire pas les
sièges déjà désignés ; un siège retiré à la main **avant** la hausse n'est ni
réexaminé (stale) ni réinscrit par la complétion (B/P1 dev4, B/P2 cx1) ; un
retrait **pendant** la passe refuse toujours, sans écrire.

#3617 : une attente partielle pourvoit les familles disponibles (marque
`waiting`, demande aux seuls nouveaux sièges, sans avis de capacité) ; la
reprise complète le siège manquant sans réinscrire un siège retiré.
"""
from __future__ import annotations

import dataclasses
import os
import unittest
from unittest import mock

from ameesh import registry
from ameesh import review_designation as rd
from ameesh import review_designation_activation as act
from ameesh import review_designation_pg as pg
from ameesh import review_floor, work
from ameesh.work_reviewers import designated

from . import test_2447_review_designation_activation as base
from .support import PgTestCase


class FloorCompletionTest(base._ActivationFixture, PgTestCase):
    """Le gel réel, le canon réel, puis le plancher du pilote."""

    def setUp(self):
        super().setUp()
        self.canon = base._Canon(
            os.path.join(self.tmp, "canon-un-avis-floor"),
            federation=base.FEDERATION.replace("            reviews: 2",
                                               "            reviews: 1"))
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root,
                                       canon_ref="HEAD")

    def floor(self, value, *, complete=True):
        return review_floor.set_floor(
            self.cfg, self.db, self.lot, value,
            reason="clés et prod hors barème", actor="human:smichea",
            complete=complete)

    def test_a_raised_floor_completes_the_freeze_designation(self):
        _, out = self.freeze()
        self.assertEqual(out["state"], rd.DESIGNATED, out)
        self.assertEqual(out["required"], 1, out)
        first = [r["name"] for r in out["reviewers"]]
        self.assertEqual(len(first), 1)
        before = len(self.mailbox())

        result = self.floor(2)

        self.assertTrue(result["changed"])
        completion = result["designation"]
        self.assertEqual(completion["state"], rd.DESIGNATED, completion)
        self.assertEqual(completion["required"], 2)
        self.assertTrue(completion.get("completed"), completion)
        names = [r["name"] for r in completion["reviewers"]]
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0], first[0])
        self.assertEqual(len({r["family"] for r in completion["reviewers"]}), 2)
        added = [name for name in names if name != first[0]]
        self.assertEqual(completion.get("added"), added)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before:]], added)
        self.assertEqual([m["kind"] for m in self.mailbox()[before:]], ["request"])
        self.assertIn("review 2/2", self.mailbox()[before]["body"])
        self.assertEqual(designated(work.get(self.db, self.lot)), names)

        mark = pg.designation_of(self.marks()[-1])
        self.assertEqual(mark["state"], rd.DESIGNATED)
        self.assertEqual(mark["required"], 2)
        self.assertEqual([r["name"] for r in mark["reviewers"]], names)
        self.assertIn("review floor 2", mark["why"])
        floor_notes = self.notes(act.FLOOR_PREFIX)
        self.assertEqual(len(floor_notes), 1, floor_notes)
        self.assertIn("completed", floor_notes[0])
        self.assertIn(added[0], floor_notes[0])
        self.assertIn(first[0], floor_notes[0])

        # un second `--apply` ne renvoie ni courrier ni marque
        before_mail, before_marks = len(self.mailbox()), len(self.marks())
        again = act.reviewers(self.cfg, self.db, self.lot, apply=True)["applied"]
        self.assertEqual(again["state"], rd.DESIGNATED)
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.marks()), before_marks)

    def test_a_floor_without_a_freeze_writes_no_designation(self):
        result = self.floor(2)
        self.assertTrue(result["changed"])
        completion = result["designation"]
        self.assertEqual(completion["state"], "unfrozen")
        self.assertEqual(self.mailbox(), [])
        self.assertEqual(self.marks(), [])
        self.assertEqual(self.notes(act.FLOOR_PREFIX), [])
        self.assertEqual(self.notes(act.NONE_PREFIX), [])

    def test_a_floor_already_covered_writes_nothing(self):
        # canon du banc à deux avis : le gel désigne déjà deux familles
        self.canon = base._Canon(os.path.join(self.tmp, "canon-deux-avis"),
                                 federation=base.FEDERATION)
        self.cfg = dataclasses.replace(self.cfg, canon=self.canon.root,
                                       canon_ref="HEAD")
        _, out = self.freeze()
        self.assertEqual(out["required"], 2, out)
        before_mail, before_marks = len(self.mailbox()), len(self.marks())

        result = self.floor(2)

        completion = result["designation"]
        self.assertEqual(completion["state"], rd.DESIGNATED)
        self.assertFalse(completion.get("completed"), completion)
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.marks()), before_marks)
        self.assertEqual(self.notes(act.FLOOR_PREFIX), [])

    def test_reviewers_apply_completes_a_floor_written_without_it(self):
        # simule une version antérieure : la ligne de plancher est écrite,
        # la désignation ne la relit pas ; `--apply` doit la compléter.
        _, out = self.freeze()
        self.assertEqual(out["required"], 1, out)
        before = len(self.mailbox())
        written = self.floor(2, complete=False)
        self.assertNotIn("designation", written)
        self.assertEqual(len(self.mailbox()), before)

        applied = act.reviewers(self.cfg, self.db, self.lot,
                                apply=True)["applied"]

        self.assertEqual(applied["state"], rd.DESIGNATED, applied)
        self.assertEqual(applied["required"], 2)
        self.assertTrue(applied.get("completed"), applied)
        self.assertEqual(len(self.mailbox()), before + 1)
        self.assertEqual(len(designated(work.get(self.db, self.lot))), 2)

    def test_an_uncoverable_floor_keeps_the_existing_designation(self):
        _, out = self.freeze()
        first = [r["name"] for r in out["reviewers"]]
        self.assertEqual(len(first), 1)
        # plus qu'une famille indépendante : le plancher 2 ne peut pas être couvert
        self.db.execute("UPDATE agent_registry SET status = 'dead'"
                        " WHERE name = 'r2'")
        before_mail, before_marks = len(self.mailbox()), len(self.marks())

        result = self.floor(2)

        completion = result["designation"]
        self.assertEqual(completion["state"], rd.WAITING, completion)
        self.assertIn("missing", completion)
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.marks()), before_marks)
        self.assertEqual(designated(work.get(self.db, self.lot)), first)
        self.assertTrue(self.notes(act.FLOOR_PREFIX))

    def test_the_cli_floor_command_completes_the_designation(self):
        _, out = self.freeze()
        first = [r["name"] for r in out["reviewers"]]
        before = len(self.mailbox())
        proc = self.mesh("work", "reviewer", str(self.lot), "--floor", "2",
                         "--actor", "human:smichea",
                         env=self.env(AMEESH_CANON=self.canon.root,
                                      AMEESH_CANON_REF="HEAD"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("review floor 2", proc.stdout)
        self.assertIn("designation: completed", proc.stdout)
        self.assertEqual(len(self.mailbox()), before + 1)
        self.assertEqual(len(designated(work.get(self.db, self.lot))), 2)
        self.assertIn(first[0], designated(work.get(self.db, self.lot)))

    # -- sièges retirés à la main (B/P1 dev4, B/P2 cx1) --------------------

    def remove(self, name):
        work.designate_reviewer(self.cfg, self.db, self.lot, name,
                                actor="human:smichea", remove=True)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])

    def _third_family(self):
        """Un second agent claude disponible : après le retrait de r1, deux
        familles distinctes restent couvrables (r2 codex, r3 claude)."""
        registry.upsert(self.db, "r3", harness="claude", host=self.cfg.host,
                        cwd=self.repo.root, status="idle")
        self.db.execute("UPDATE agent_registry SET team = 'ameesh' WHERE name = %s",
                        ("r3",))

    def test_a_seat_removed_before_the_floor_is_not_rewritten(self):
        self._third_family()
        _, out = self.freeze()
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1"], out)
        self.remove("r1")
        before_mail, before_marks = len(self.mailbox()), len(self.marks())

        result = self.floor(2)

        completion = result["designation"]
        self.assertEqual(completion["state"], rd.DESIGNATED, completion)
        self.assertTrue(completion.get("completed"), completion)
        names = [r["name"] for r in completion["reviewers"]]
        self.assertEqual(names, ["r2", "r3"], completion)
        self.assertEqual(designated(work.get(self.db, self.lot)), names)
        self.assertEqual([m["recipient"] for m in self.mailbox()[before_mail:]],
                         ["r2", "r3"])
        self.assertGreater(len(self.marks()), before_marks)

        # rejeu `--apply` : même désignation, aucun courrier ni marque
        before_mail, before_marks = len(self.mailbox()), len(self.marks())
        again = act.reviewers(self.cfg, self.db, self.lot, apply=True)["applied"]
        self.assertEqual(again["state"], rd.DESIGNATED, again)
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.marks()), before_marks)

    def test_a_seat_removed_before_the_floor_provides_the_available_family(self):
        # #3617 : r1 retiré, une seule famille disponible (r2 codex) pour un
        # plancher à deux avis. L'attente est partielle : r2 est pourvu tout
        # de suite (demande, marque `waiting`, colonne), sans avis de capacité.
        _, out = self.freeze()
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1"], out)
        self.remove("r1")
        before_mail, before_marks = len(self.mailbox()), len(self.marks())

        result = self.floor(2)

        completion = result["designation"]
        self.assertEqual(completion["state"], rd.WAITING, completion)
        self.assertTrue(completion.get("completed"), completion)
        self.assertEqual(completion.get("added"), ["r2"], completion)
        self.assertEqual([r["name"] for r in completion["reviewers"]], ["r2"])
        self.assertEqual(completion["missing"], 1, completion)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r2"])
        sent = self.mailbox()[before_mail:]
        self.assertEqual([(m["recipient"], m["kind"]) for m in sent],
                         [("r2", "request")])
        self.assertEqual(len(self.marks()), before_marks + 1)
        mark = pg.designation_of(self.marks()[-1])
        self.assertEqual(mark["state"], rd.WAITING)
        self.assertEqual(mark["required"], 2)
        self.assertEqual([r["name"] for r in mark["reviewers"]], ["r2"])
        floor_notes = self.notes(act.FLOOR_PREFIX)
        self.assertEqual(len(floor_notes), 1, floor_notes)
        self.assertIn("partially completed", floor_notes[0])
        self.assertIn("r2", floor_notes[0])

        # le rejeu ne renvoie rien et ne réinscrit pas r1 (claude, disponible)
        before_mail, before_marks = len(self.mailbox()), len(self.marks())
        again = act.reviewers(self.cfg, self.db, self.lot, apply=True)["applied"]
        self.assertEqual(again["state"], rd.WAITING, again)
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.marks()), before_marks)
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r2"])
        self.assertEqual(len(self.notes(act.FLOOR_PREFIX)), 1)

        # la capacité revient (r3 claude) : la reprise pourvoit le siège
        # manquant, garde r2 sans second courrier et ne reprend jamais r1
        self._third_family()
        before_mail = len(self.mailbox())
        done = act.reviewers(self.cfg, self.db, self.lot, apply=True)["applied"]
        self.assertEqual(done["state"], rd.DESIGNATED, done)
        self.assertEqual([r["name"] for r in done["reviewers"]], ["r2", "r3"])
        self.assertEqual([m["recipient"] for m in self.mailbox()[before_mail:]],
                         ["r3"])
        self.assertEqual(designated(work.get(self.db, self.lot)), ["r2", "r3"])

    def test_a_floor_with_no_available_family_leaves_a_journaled_wait(self):
        # r1 retiré et r2 mort : aucune famille ne peut être pourvue, rien
        # n'est écrit ni envoyé (attente journalisée, une par gel et plancher)
        _, out = self.freeze()
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1"], out)
        self.remove("r1")
        self.db.execute("UPDATE agent_registry SET status = 'dead'"
                        " WHERE name = 'r2'")
        before_mail, before_marks = len(self.mailbox()), len(self.marks())

        result = self.floor(2)

        completion = result["designation"]
        self.assertEqual(completion["state"], rd.WAITING, completion)
        self.assertFalse(completion.get("completed"), completion)
        self.assertEqual(designated(work.get(self.db, self.lot)), [])
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.marks()), before_marks)
        self.assertTrue(self.notes(act.FLOOR_PREFIX))
        before_notes = len(self.notes(act.FLOOR_PREFIX))

        again = act.reviewers(self.cfg, self.db, self.lot, apply=True)["applied"]
        self.assertEqual(again["state"], rd.WAITING, again)
        self.assertEqual(len(self.mailbox()), before_mail)
        self.assertEqual(len(self.marks()), before_marks)
        self.assertEqual(len(self.notes(act.FLOOR_PREFIX)), before_notes)

    def test_a_seat_removed_during_the_pass_still_refuses_stale(self):
        _, out = self.freeze()
        self.assertEqual([r["name"] for r in out["reviewers"]], ["r1"], out)
        before_marks = len(self.marks())
        real = act._selection

        def remove_then_select(inputs, port, **kwargs):
            # retrait concurrent, sous la passe : la colonne vivante perd le
            # siège examiné ; la complétion doit refuser, jamais le réécrire.
            self.db.execute("UPDATE work_items SET reviewers = '{}' WHERE id = %s",
                            (self.lot,))
            return real(inputs, port, **kwargs)

        with mock.patch.object(act, "_selection", remove_then_select):
            result = self.floor(2)

        completion = result["designation"]
        self.assertEqual(completion["state"], "stale", completion)
        self.assertIn("column changed", completion["why"])
        self.assertEqual(len(self.marks()), before_marks)
        self.assertEqual(self.notes(act.FLOOR_PREFIX), [])


if __name__ == "__main__":
    unittest.main()
