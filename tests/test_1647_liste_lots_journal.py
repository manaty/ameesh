# SPDX-License-Identifier: AGPL-3.0-only
"""#1647 — la liste des lots ne relit plus le texte du journal.

`work list --json` (comme `progress` et `roadmap`) ne demande plus que les
transitions et le dernier instant du journal : les notes, qui pèsent des
mégaoctets sur une liste de lots, ne sont ni transférées ni recopiées par le
préchargement. Le témoin borne le TRAVAIL (lignes lues), jamais une durée
murale : un seuil en secondes dépendrait de la charge de l'hôte (canon §3).
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import cost
from ameesh import mesh_cli
from ameesh import progress
from ameesh import stagnation
from ameesh import storage
from ameesh import work
from ameesh.storage.postgres.progress import Progress

from tests.support import PgTestCase

#: des notes longues : c'est le texte du journal qui dominait le transfert
NOTES = 12
NOTE_BODY = "x" * 1024


class ListeLotsJournalTest(PgTestCase):
    """Le journal complet reste lisible pour un lot ; les vues groupées
    (liste, instantané) lisent des transitions, jamais les notes."""

    def setUp(self) -> None:
        super().setUp()
        self.item = work.add(self.db, title="lot journal", app="demo")
        self.lot = int(self.item["id"])
        for index in range(NOTES):
            work.note(self.db, self.lot, "note %02d %s" % (index, NOTE_BODY))
        self.st = storage.of(self.db)

    def _book(self):
        return cost.CostBook(state_dir=self.state,
                             codex_sessions=os.path.join(self.tmp, "codex"),
                             db=self.db)

    def _journal(self):
        return self.st.progress.lot_events([self.lot])

    def test_transitions_ecartent_les_notes(self):
        journal = self._journal()
        transitions = self.st.progress.lot_transitions([self.lot])
        # la création, son analyse d'impact (#4818 : une ligne par estimation)
        # et les notes
        self.assertEqual(len(journal), 2 + NOTES)
        # la vue groupée ne lit qu'une ligne : la création, pas les notes
        self.assertEqual(len(transitions), 1)
        attendues, precedent = [], None
        for ev in journal:
            if precedent is None or ev["state"] != precedent:
                attendues.append(ev)
            precedent = ev["state"]
        self.assertEqual([(e["id"], e["state"], e["actor"]) for e in transitions],
                         [(e["id"], e["state"], e["actor"]) for e in attendues])
        for row in transitions:
            self.assertNotIn("note", row)

    def test_dernier_evenement_agrege(self):
        journal = self._journal()
        dernier = self.st.progress.lot_event_last([self.lot])
        self.assertEqual([int(r["work_item_id"]) for r in dernier], [self.lot])
        self.assertEqual(float(dernier[0]["last_ts"]),
                         max(float(e["created_ts"]) for e in journal))

    def test_jalons_et_activite_identiques_au_journal(self):
        journal = self._journal()
        transitions = self.st.progress.lot_transitions([self.lot])
        dernier = float(self.st.progress.lot_event_last([self.lot])[0]["last_ts"])
        item = work.get(self.db, self.lot)
        self.assertEqual(progress._jalons_de_lot(item, journal, [], []),
                         progress._jalons_de_lot(item, transitions, [], []))
        self.assertEqual(stagnation.last_activity(item, journal),
                         stagnation.last_activity(item, transitions, events_ts=dernier))

    def test_une_note_tardive_reste_dans_l_activite(self):
        # un événement écrit sans toucher `updated_at` (comme le marquage d'un
        # parent) : l'activité doit le voir, et `events_ts` le porte.
        self.db.execute("UPDATE work_items SET updated_at = now() - interval '2 hours'"
                        " WHERE id = %s", (self.lot,))
        self.db.execute("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                        " VALUES (%s, 'intake', 'note tardive', 'témoin')", (self.lot,))
        transitions = self.st.progress.lot_transitions([self.lot])
        dernier = float(self.st.progress.lot_event_last([self.lot])[0]["last_ts"])
        item = work.get(self.db, self.lot)
        complet = stagnation.last_activity(item, self._journal())
        self.assertEqual(complet,
                         stagnation.last_activity(item, transitions, events_ts=dernier))
        self.assertLess(stagnation.last_activity(item, transitions), complet)

    def test_repli_de_l_interface_sans_notes(self):
        from ameesh.storage import interface as SI

        class Faux:
            def lot_events(self, _ids):
                return [
                    {"id": 1, "work_item_id": 7, "state": "intake", "note": "création",
                     "actor": "a", "created_ts": 10.0},
                    {"id": 2, "work_item_id": 7, "state": "intake", "note": "x" * 5000,
                     "actor": "a", "created_ts": 11.0},
                    {"id": 3, "work_item_id": 7, "state": "build", "note": "prise",
                     "actor": "b", "created_ts": 12.0},
                ]

        faux = Faux()
        self.assertEqual(SI.Progress.lot_transitions(faux, [7]),
                         [{"id": 1, "work_item_id": 7, "state": "intake", "actor": "a",
                           "created_ts": 10.0},
                          {"id": 3, "work_item_id": 7, "state": "build", "actor": "b",
                           "created_ts": 12.0}])
        self.assertEqual(SI.Progress.lot_event_last(faux, [7]),
                         [{"work_item_id": 7, "last_ts": 12.0}])

    def test_work_list_et_progress_ne_lisent_pas_le_journal(self):
        # #1660 : une note tardive écrite sans toucher les horodatages de la
        # ligne (comme le marquage d'un parent) isole la source de l'activité.
        # Ligne, journal et jalon `requested` reculent de 2 h : sinon l'un
        # d'eux, resté près de maintenant, masquerait le retrait d'`events_ts`
        # (marge de quelques secondes seulement).
        self.db.execute("UPDATE work_items SET updated_at = now() - interval '2 hours',"
                        " created_at = now() - interval '2 hours' WHERE id = %s",
                        (self.lot,))
        self.db.execute("UPDATE work_item_events SET created_at = now() - interval '2 hours'"
                        " WHERE work_item_id = %s", (self.lot,))
        self.db.execute("UPDATE work_item_milestones SET at = now() - interval '2 hours'"
                        " WHERE work_item_id = %s", (self.lot,))
        self.db.execute("INSERT INTO work_item_events (work_item_id, state, note, actor)"
                        " VALUES (%s, 'intake', 'note tardive', 'témoin')", (self.lot,))
        dernier = float(self.st.progress.lot_event_last([self.lot])[0]["last_ts"])

        def interdit(*_args, **_kwargs):
            raise AssertionError("le journal complet est relu par une vue groupée (#1647)")

        with mock.patch.object(Progress, "lot_events", interdit):
            rows = mesh_cli.work_rows(self.db, limit=50)
            snap = progress.snapshot(self.db, self.cfg, since="24h", book=self._book())
        ligne = next(r for r in rows if int(r["id"]) == self.lot)
        self.assertIsNotNone(ligne["last_activity_ts"])
        # l'activité est celle du journal (la note tardive), pas d'`updated_at`
        self.assertEqual(ligne["last_activity_ts"], round(dernier, 3))
        self.assertEqual(ligne["waiting_for"],
                         {"what": "start", "who": None,
                          "label": "assignment (lot unassigned)"})
        vue = next(l for l in snap["lots"] if int(l["id"]) == self.lot)
        self.assertEqual(vue["last_activity_ts"], ligne["last_activity_ts"])


if __name__ == "__main__":
    unittest.main()
