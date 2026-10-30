# SPDX-License-Identifier: AGPL-3.0-only
"""#4756 : le lot de découpage est un geste du pilote.

Le 29/10, #4510 (découpage ameesh) est né sans assigné : le pilote avait déjà
un lot non démarré, `work.assign` l'a mis dans la file d'équipe (#540) sans
erreur, et la prise automatique (L165) l'a servi cinq minutes plus tard à un
exécutant au repos. Le refus restait dans le rendu du passage
(`assign_error`), que personne ne lisait.

Témoins : un pilote qui a déjà un lot non démarré reçoit quand même le lot de
découpage ; un refus d'attribution est dit au pilote (courrier et journal) ;
un lot de découpage libre n'est jamais prenable au repos, un lot ordinaire
l'est toujours.
"""
from __future__ import annotations

from unittest import mock

from ameesh import backlog, work
from ameesh import work_lifecycle as lifecycle
from ameesh import file_decoupage as fd

from .test_3256_file_decoupage import SETTINGS, team
from .test_l165_prise_lots import _Lots


class DecoupagePiloteTest(_Lots):
    def setUp(self):
        super().setUp()
        for name in ("pilote", "dev0", "dev1", "dev2", "dev3", "dev4"):
            self.agent(name, harness="deepseek", team="ameesh")
        pilots = mock.patch("ameesh.alert_briefs.project_pilots", return_value=["pilote"])
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

    def test_le_pilote_avec_un_lot_non_demarre_recoit_le_decoupage(self):
        self.lot("lot du pilote", app="ameesh", assignee="pilote")
        self.run_pass(0)
        out = self.run_pass(1800)
        self.assertEqual([g["kind"] for g in out], ["split"])
        self.assertNotIn("assign_error", out[0])
        lot = self.split()
        self.assertEqual(lot["assignee"], "pilote")
        self.assertFalse([n for n in self.notes(lot["id"]) if "team queue" in n],
                         self.notes(lot["id"]))
        self.assertFalse([c for c in self.courriers() if c["recipient"] == "pilote"])

    def test_un_refus_d_attribution_est_dit_au_pilote(self):
        with mock.patch.object(work, "assign", side_effect=work.WorkError("pilote arrêté")):
            self.run_pass(0)
            out = self.run_pass(1800)
        self.assertIn("pilote arrêté", out[0]["assign_error"])
        lot = self.split()
        self.assertIsNone(lot["assignee"])
        mails = [c for c in self.courriers() if c["recipient"] == "pilote"]
        self.assertEqual(len(mails), 1, mails)
        self.assertEqual(str(mails[0]["work_item_id"]), str(lot["id"]))
        self.assertIn("pilote arrêté", mails[0]["body"])
        self.assertTrue([n for n in self.notes(lot["id"]) if "non attribué" in n],
                        self.notes(lot["id"]))

    def test_un_lot_de_decoupage_libre_n_est_pas_pris_au_repos(self):
        split = self.lot("Découper la feuille de route de ameesh", app="ameesh",
                         source="file-decoupage:ameesh", priority=1)
        ordinaire = self.lot("lot ordinaire", app="ameesh", priority=2)
        prises = self.take()
        pris = [int(p["item"]["id"]) for p in prises if p.get("kind") == "lot"]
        self.assertNotIn(int(split["id"]), pris)
        self.assertIn(int(ordinaire["id"]), pris)
        self.assertIsNone(work.get(self.db, int(split["id"]))["assignee"])

    def test_lot_takeable_refuse_le_seul_lot_de_decoupage(self):
        base = {"state": lifecycle.REQUESTED, "assignee": None, "lot_team": "ameesh"}
        self.assertTrue(backlog.lot_takeable(dict(base, source="auditeur")))
        self.assertFalse(backlog.lot_takeable(dict(base, source="file-decoupage:ameesh")))
