# SPDX-License-Identifier: AGPL-3.0-only
"""#4965 : sans pilote, le refus d'attribution du lot de découpage est écrit.

Constat de relecture de #4756 (tête d6c2d8afe08e) : `_tell_refusal` n'est
appelé que si `assign_error`, or ce champ n'est posé que si un pilote existe.
Quand `project_pilots` est vide, le lot de découpage était créé libre sans
note ; la docstring de `_tell_refusal` annonce pourtant que le journal le dit.
Le repli de l'auditeur à 1 h restait le seul signal.

Témoin : sans pilote, le lot de découpage reste libre, mais le journal du lot
porte une note de refus explicite (et aucun courrier n'est envoyé).
"""
from __future__ import annotations

from unittest import mock

from ameesh import file_decoupage as fd

from .test_3256_file_decoupage import SETTINGS, team
from .test_l165_prise_lots import _Lots


class DecoupageSansPiloteTest(_Lots):
    def setUp(self):
        super().setUp()
        for name in ("dev0", "dev1", "dev2"):
            self.agent(name, harness="deepseek", team="ameesh")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=[])
        pilots.start()
        self.addCleanup(pilots.stop)
        self.state: dict = {}

    def run_pass(self, offset):
        return fd.run(None, self.db, now=self.now + offset, state=self.state,
                      listing=team(2, 3), canons=[], settings=SETTINGS)

    def split(self):
        rows = self.db.query("SELECT id, assignee FROM work_items WHERE source = %s",
                             ("file-decoupage:ameesh",))
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def notes(self, lot):
        return [r["note"] for r in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(lot),))]

    def test_sans_pilote_le_refus_est_note_dans_le_journal_du_lot(self):
        self.run_pass(0)
        out = self.run_pass(1800)
        self.assertEqual([g["kind"] for g in out], ["split"])
        self.assertIsNone(out[0]["pilot"])
        lot = self.split()
        self.assertIsNone(lot["assignee"])
        notes = [n for n in self.notes(lot["id"]) if "non attribué" in n]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("aucun pilote disponible", notes[0])
        self.assertFalse(self.courriers(), "aucun pilote à qui écrire")
        # une passe suivante voit le lot ouvert : pas de seconde note.
        self.assertEqual(self.run_pass(2400), [])
        self.assertEqual([n for n in self.notes(lot["id"]) if "non attribué" in n],
                         notes)
