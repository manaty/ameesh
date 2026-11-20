# SPDX-License-Identifier: AGPL-3.0-only
"""#7940 : une attente humaine explicite de plus de 24 h ne gèle plus
l'empreinte qu'elle touche pour tout le mesh.

Constat (mesh-design #228931, 20/11) : #7647, en hold pilote « attente go
owner » depuis le 17/11, gardait son empreinte « en cours » sans limite et
bloquait l'assignation de #7927, une ligne déclarative de
`hotes/ameesh-agents-3.md`, dans une autre partie de la fiche.

La règle : un lot dont le dernier hold est une formule d'attente humaine
(#3498 : owner, propriétaire, avatar, bureau) qui tient depuis plus de 24 h
ne retient plus le démarrage d'un lot qui chevauche son empreinte. Le
chevauchement devient un avertissement journalisé sur les deux lots ; le lot
en attente garde son empreinte pour l'information. L'âge se compte depuis la
note de hold elle-même, jamais depuis le dernier événement du lot (les notes
automatiques du runner ne remettent pas l'horloge à zéro). La règle ne
touche ni la réservation des numéros de migration, ni un lot gelé dans un
paquet ouvert (cible réservée, canon §12).
"""
from __future__ import annotations

import unittest

from ameesh import storage, work
from ameesh import work_footprints as fp

from .test_4173_empreinte import _Empreintes

HEURE = 3600.0
SEUIL = 24 * HEURE


class _AttenteHumaine(_Empreintes):
    def hold(self, item, text="hold: attente owner : signer le go owner",
             *, age_s=SEUIL + HEURE, actor="human:proprio"):
        """Pose un hold sur `item` et backdate sa note (l'horloge de #7940)."""
        work.note(self.db, int(item["id"]), text, actor=actor)
        event = self.db.query(
            "SELECT id FROM work_item_events WHERE work_item_id = %s"
            " ORDER BY id DESC LIMIT 1", (int(item["id"]),))[0]
        self.db.execute(
            "UPDATE work_item_events SET created_at = now() - make_interval(secs => %s)"
            " WHERE id = %s", (float(age_s), int(event["id"])))
        return int(event["id"])

    def notes(self, item):
        return [row["note"] for row in self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s ORDER BY id",
            (int(item["id"]),))]

    def overlapping(self, item):
        return storage.of(self.db).work_footprints.overlapping(int(item["id"]))

    def taken(self, limit=10):
        return [row["id"] for row in storage.of(self.db).work.unassigned_lots(limit)]

    def couple(self, *, path="hotes/ameesh-agents-3.md",
               occupant="hotes/ameesh-agents-3.md"):
        """Un occupant assigné et un voisin libre qui chevauche son empreinte."""
        premier = self.lot("occupant en attente humaine")
        second = self.lot("voisin prioritaire", priority=1)
        self.footprint(premier, occupant)
        self.footprint(second, path)
        self.give(premier, "human:proprio")
        return premier, second


class AttenteHumaineTest(_AttenteHumaine):
    def test_hold_owner_de_plus_de_24h_ne_bloque_plus_et_avertit_les_deux_lots(self):
        premier, second = self.couple()
        self.hold(premier)

        # L'empreinte reste lisible pour l'information, et le hold est nommé.
        self.assertIn(second["id"], self.taken())
        rows = self.overlapping(second)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["human_wait"])
        self.assertIn("hold: attente owner", rows[0]["human_hold_note"])
        self.assertIsNotNone(rows[0]["human_hold_ts"])

        work.assign(self.db, second["id"], "human:autre", actor="test")
        self.assertEqual(work.get(self.db, second["id"])["assignee"], "human:autre")

        # L'avertissement nomme les deux lots et le chemin partagé, des deux
        # côtés ; le lot en attente reçoit le geste de son go (rebase + delta).
        occupant_notes = self.notes(premier)
        started_notes = self.notes(second)
        self.assertTrue(any(
            "#%d" % second["id"] in note
            and "hotes/ameesh-agents-3.md" in note
            for note in occupant_notes), occupant_notes)
        self.assertTrue(any(
            "#%d" % premier["id"] in note
            and "hotes/ameesh-agents-3.md" in note
            for note in started_notes), started_notes)
        self.assertTrue(any(
            "rebase" in note and "delta" in note for note in occupant_notes),
            occupant_notes)

        # Le lot en attente garde son empreinte : la règle ne retire rien.
        self.assertEqual(fp.paths_of(self.db, premier["id"]),
                         ["hotes/ameesh-agents-3.md"])

    def test_un_hold_recent_refuse_comme_avant(self):
        premier, second = self.couple()
        self.hold(premier, age_s=23 * HEURE)

        self.assertNotIn(second["id"], self.taken())
        with self.assertRaises(work.WorkError) as ctx:
            work.assign(self.db, second["id"], "human:autre", actor="test")
        self.assertIn("footprint overlaps", str(ctx.exception))
        self.assertIsNone(work.get(self.db, second["id"])["assignee"])
        self.assertFalse(any(
            "not blocking" in note for note in self.notes(second)))

    def test_un_hold_ordinaire_ou_d_agent_ne_leve_rien(self):
        for text in ("hold: attend la CI du paquet",
                     "hold: attente CI, run 1234",
                     "hold: attente relecture : avis 2/2",
                     "hold: attente mesh-design : décision du feu"):
            with self.subTest(text=text):
                premier, second = self.couple()
                self.hold(premier, text)
                self.assertFalse(self.overlapping(second)[0]["human_wait"])
                self.assertNotIn(second["id"], self.taken())
                with self.assertRaises(work.WorkError):
                    work.assign(self.db, second["id"], "human:autre", actor="test")

    def test_les_formules_humaines_de_3498_sont_reconnues(self):
        for formula in ("hold: attente owner : signer le go",
                        "hold: attente du propriétaire : signer le go",
                        "hold: attente de l'avatar : poser le backup",
                        "hold: attente du bureau : signer le go",
                        "hold: attente externe (bureau) : signer le go"):
            with self.subTest(formula=formula):
                premier, second = self.couple()
                self.hold(premier, formula)
                rows = self.overlapping(second)
                self.assertTrue(rows and rows[0]["human_wait"], rows)

    def test_une_levee_ou_une_transition_d_agent_rebloque(self):
        # Une levée explicite après le hold : l'attente n'est plus ouverte.
        premier, second = self.couple()
        self.hold(premier)
        work.note(self.db, premier["id"], "unhold: go owner reçu", actor="human:proprio")
        self.assertFalse(self.overlapping(second)[0]["human_wait"])
        with self.assertRaises(work.WorkError):
            work.assign(self.db, second["id"], "human:autre", actor="test")

        # Un geste d'agent (transition d'état) lève aussi la retenue.
        premier2, second2 = self.couple()
        self.hold(premier2)
        work.move(self.db, premier2["id"], "build", actor="human:autre")
        self.assertFalse(self.overlapping(second2)[0]["human_wait"])
        with self.assertRaises(work.WorkError):
            work.assign(self.db, second2["id"], "human:autre", actor="test")

    def test_les_notes_et_transitions_automatiques_ne_remettent_pas_l_horloge(self):
        premier, second = self.couple()
        self.hold(premier)
        # Les notes automatiques du runner (relances, empreinte) sont
        # postérieures au hold : elles ne datent pas l'attente à nouveau.
        self.db.execute(
            "INSERT INTO work_item_events(work_item_id, state, note, actor,"
            " created_at) VALUES (%s, 'intake', %s, 'ameesh', clock_timestamp())",
            (premier["id"], "en attente d'empreinte : #7647 (2026-11-20T15:06Z)"))
        # Une transition automatique de l'outillage ne lève pas le hold (#5758).
        self.db.execute(
            "INSERT INTO work_item_events(work_item_id, state, note, actor,"
            " created_at) VALUES (%s, 'build', '', 'ameesh:freeze',"
            " clock_timestamp())", (premier["id"],))
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                        (premier["id"],))

        self.assertTrue(self.overlapping(second)[0]["human_wait"])
        self.assertIn(second["id"], self.taken())
        work.assign(self.db, second["id"], "human:autre", actor="test")
        self.assertEqual(work.get(self.db, second["id"])["assignee"], "human:autre")

    def test_un_autre_occupant_non_humain_rebloque(self):
        humain, second = self.couple()
        autre = self.lot("occupant ordinaire")
        self.footprint(autre, "hotes/ameesh-agents-3.md")
        self.give(autre, "human:proprio")
        self.hold(humain)

        self.assertNotIn(second["id"], self.taken())
        with self.assertRaises(work.WorkError) as ctx:
            work.assign(self.db, second["id"], "human:autre", actor="test")
        self.assertIn("#%d" % autre["id"], str(ctx.exception))

    def test_un_lot_dans_un_paquet_ouvert_reste_bloquant(self):
        premier, second = self.couple()
        paquet = self.lot("porteur du paquet", branch="integration/paquet-7940")
        self.db.execute(
            "INSERT INTO work_packet_members(package_id, work_item_id,"
            " package_branch, base_sha, head_sha, unit_sha, source_sha,"
            " frozen_sha, recorded_by)"
            " VALUES (%s, %s, 'integration/paquet-7940', %s, %s, %s, '', %s, 'test')",
            (paquet["id"], premier["id"], "a" * 40, "b" * 40, "c" * 40,
             "c" * 40))
        self.hold(premier)

        # La cible du paquet est réservée (§12) : le hold, même ancien, retient.
        self.assertFalse(self.overlapping(second)[0]["human_wait"])
        self.assertNotIn(second["id"], self.taken())
        with self.assertRaises(work.WorkError):
            work.assign(self.db, second["id"], "human:autre", actor="test")

        # Paquet clos : la règle reprend (le hold tient toujours).
        work.close(self.db, paquet["id"], abandoned=True, actor="test")
        self.assertTrue(self.overlapping(second)[0]["human_wait"])
        self.assertIn(second["id"], self.taken())

    def test_la_regle_ne_touche_pas_la_reservation_de_migration(self):
        premier, second = self.couple()
        self.db.execute("UPDATE work_items SET migration = 42,"
                        " migration_repo = 'ameesh' WHERE id = %s",
                        (premier["id"],))
        self.hold(premier)
        work.assign(self.db, second["id"], "human:autre", actor="test")
        self.assertEqual(work.get(self.db, premier["id"])["migration"], 42)

    def test_creation_attribuee_avertit_aussi(self):
        premier, _ = self.couple()
        self.hold(premier)
        item = work.add(self.db, title="créé déjà attribué", app="nexlink",
                        assignee="human:autre", footprint=["hotes"],
                        cfg=self.cfg)
        notes = self.notes(item)
        self.assertTrue(any("#%d" % premier["id"] in note for note in notes), notes)
        self.assertTrue(any(
            "#%d" % item["id"] in note for note in self.notes(premier)),
            self.notes(premier))

    def test_work_footprint_montre_l_attente_non_bloquante(self):
        premier, second = self.couple()
        self.hold(premier)
        proc = self.mesh("work", "footprint", str(second["id"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("human wait #%d" % premier["id"], proc.stdout)
        self.assertIn("not blocking (#7940)", proc.stdout)


if __name__ == "__main__":
    unittest.main()
