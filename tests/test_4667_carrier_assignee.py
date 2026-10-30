# SPDX-License-Identifier: AGPL-3.0-only
"""#4667 : le porteur de paquet naît assigné au pilote.

Un porteur (`integration/paquet-*`) est un véhicule sans delta propre : il
n'entre pas dans l'en-cours d'un agent (#540) et ne porte aucune empreinte
d'intake (#3185). Son corps cite pourtant le script du composeur, ce qui lui
donnait une empreinte `scripts/paquet-horaire.py` ; tant qu'un lot tenait ce
fichier — presque toujours — l'attribution était refusée et la CLI créait le
porteur libre, que le pilote réassignait à la main (ou oubliait : alerte SLO).

Témoins : un pilote qui a déjà un lot non démarré reçoit quand même le
porteur ; une empreinte explicite ne le désassigne pas ; un lot ordinaire
garde la file d'équipe et son empreinte.
"""
from __future__ import annotations

from unittest import mock

from ameesh import work, work_footprints as fp

from .test_l165_prise_lots import _Lots

BRANCH = "integration/paquet-h-20261030-01"


class CarrierAssigneeTest(_Lots):
    def setUp(self):
        super().setUp()
        self.agent("mesh-design")
        self.agent("dev1")

    def carrier(self, **kw):
        kw.setdefault("title", "Paquet horaire h-20261030-01 — composition")
        kw.setdefault("app", "ameesh")
        kw.setdefault("assignee", "mesh-design")
        kw.setdefault("branch", BRANCH)
        kw.setdefault("branch_target", "main")
        return work.add(self.db, **kw)

    def notes(self, item):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(item["id"]),))]

    def test_le_pilote_avec_un_lot_non_demarre_recoit_le_porteur(self):
        # #540 : le pilote a déjà un lot non démarré ; le porteur n'est pas un
        # lot de travail, il ne va pas dans la file d'équipe.
        self.lot("lot du pilote", app="ameesh", assignee="mesh-design")
        carrier = self.carrier(footprint=["scripts/paquet-horaire.py"])
        self.assertEqual(work.get(self.db, int(carrier["id"]))["assignee"],
                         "mesh-design")
        self.assertNotIn("queued_behind", carrier)
        self.assertNotIn("queue_warning", carrier)

    def test_un_porteur_ne_porte_pas_l_empreinte_inferee_de_son_corps(self):
        # Le corps du porteur cite le script du composeur : ce n'est pas un
        # delta du porteur. L'estimation d'intake n'est pas même demandée.
        with mock.patch.object(
                fp, "estimate_for_creation",
                return_value=["scripts/paquet-horaire.py"]) as estimate:
            carrier = self.carrier(
                body="Paquet composé par scripts/paquet-horaire.py (#3845).")
        estimate.assert_not_called()
        self.assertEqual(fp.paths_of(self.db, carrier["id"]), [])
        self.assertFalse([note for note in self.notes(carrier)
                          if note.startswith("Footprint")], self.notes(carrier))

    def test_le_chevauchement_ne_desassigne_pas_un_porteur(self):
        # Un lot occupe le fichier cité par le corps du porteur : l'empreinte
        # inférée le faisait naître libre (repli FootprintOverlap de la CLI).
        occupant = work.add(self.db, title="composeur", app="ameesh",
                            assignee="dev1",
                            footprint=["scripts/paquet-horaire.py"])
        self.assertEqual(occupant["assignee"], "dev1")
        carrier = self.carrier(footprint=["scripts/paquet-horaire.py"])
        self.assertEqual(work.get(self.db, int(carrier["id"]))["assignee"],
                         "mesh-design")
        self.assertEqual(fp.paths_of(self.db, carrier["id"]), [])

    def test_le_geste_explicite_attribue_le_porteur(self):
        # Attendu du lot : si le porteur naît libre, le composeur l'attribue
        # d'un geste explicite — sans le mettre dans la file d'équipe.
        self.lot("lot du pilote", app="ameesh", assignee="mesh-design")
        carrier = work.add(self.db, title="Paquet horaire h-20261030-03",
                           app="ameesh", branch=BRANCH, branch_target="main")
        self.assertIsNone(carrier["assignee"])
        out = work.assign(self.db, int(carrier["id"]), "mesh-design",
                          actor="mesh-design")
        self.assertEqual(out["item"]["assignee"], "mesh-design")
        self.assertNotIn("queue_warning", out)

    def test_cli_porteur_assigne_au_pilote(self):
        self.lot("lot du pilote", app="ameesh", assignee="mesh-design")
        proc = self.mesh("work", "add", "--title", "Paquet horaire h-20261030-02",
                         "--app", "ameesh", "--assignee", "mesh-design",
                         "--branch", BRANCH, "--target", "main",
                         "--estimate", "1h")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("team queue", proc.stderr)
        rows = self.db.query(
            "SELECT assignee FROM work_items WHERE title = %s",
            ("Paquet horaire h-20261030-02",))
        self.assertEqual(rows, [{"assignee": "mesh-design"}])

    def test_un_lot_ordinaire_garde_la_file_et_l_empreinte(self):
        self.lot("premier", app="ameesh", assignee="mesh-design")
        second = self.lot("second", app="ameesh", assignee="mesh-design",
                          footprint=["scripts/paquet-horaire.py"])
        self.assertIsNone(second["assignee"])
        self.assertIn("team queue", second["queue_warning"])
        self.assertEqual(fp.paths_of(self.db, second["id"]),
                         ["scripts/paquet-horaire.py"])
