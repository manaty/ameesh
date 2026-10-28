# SPDX-License-Identifier: AGPL-3.0-only
"""#4235 : une fenêtre d'observation à échéance armée n'est pas un goulot.

Constat de l'auditeur du 28/10 12:15Z : #4027 porte deux holds « fenêtre du
témoin 24 h / fenêtre d'observation voulue » avec un réveil armé au 28/10
17:35Z. `ameesh debit` les comptait dans `slow_waits` (niveau 2, geste
« relancer le détenteur ») et `--check` sortait 2, alors que rien n'était
actionnable avant l'échéance : l'attente est voulue et datée.

Témoin : un hold de pilote `hold: fenêtre : <motif>` avec `--wake-at` sort des
`slow_waits` et entre dans `window_waits` jusqu'à l'échéance ; `--check` ne
sort plus 2. À l'échéance, le hold redevient une attente ordinaire (le réveil
échu la signale). Une fenêtre déclarée couvre les autres holds du lot ; une
déclaration non pilote ou une échéance illisible ne soustrait rien.
"""
from __future__ import annotations

import types
import unittest

from ameesh import debit, planned_waits
from .test_3437_debit_dependance import H, T
from .test_3498_hold_externe import event, item, run
from .test_l119_backlog import _Base


class DeclarationTest(unittest.TestCase):
    def test_fenetre_avec_echeance(self):
        declared = planned_waits.observation_hold(
            "hold: fenêtre : relire le témoin 24 h (due 2026-10-28 17:35 UTC)")
        self.assertEqual(declared,
                         {"motif": "relire le témoin 24 h",
                          "due": "2026-10-28 17:35 UTC"})

    def test_formes_du_journal(self):
        for text in (
                "hold: fenêtre d'observation : relire le témoin "
                "[réveil programmé: 2026-10-28 17:35 UTC]",
                "hold: observation : relire le témoin "
                "[réveil programmé: 2026-10-28 17:35 UTC]",
                "hold: fenêtre du témoin 24 h encore en cours "
                "[réveil programmé: 2026-10-28 17:35 UTC]",
                "hold: (pilote mesh-design, 00:0xZ) Fenêtre d'observation "
                "voulue : relire le témoin "
                "[réveil programmé: 2026-10-28 17:35 UTC]"):
            with self.subTest(text=text):
                declared = planned_waits.observation_hold(text)
                self.assertIsNotNone(declared)
                self.assertEqual(declared["due"], "2026-10-28 17:35 UTC")
                self.assertIn("témoin", declared["motif"])

    def test_sans_echeance_ou_sans_motif_rien_n_est_declare(self):
        for text in ("hold: fenêtre opérateur",
                     "hold: fenêtre : relire le témoin",
                     "hold: fenêtre (due 2026-10-28 17:35 UTC)",
                     "hold: attend un paquet (due 2026-10-28 17:35 UTC)",
                     "hold : fenêtre : relire (due 2026-10-28 17:35 UTC)",
                     "unhold: fenêtre fermée"):
            with self.subTest(text=text):
                self.assertIsNone(planned_waits.observation_hold(text))


class ClassificationTest(unittest.TestCase):
    def test_fenetre_future_sort_des_attentes_d_agent(self):
        report = run(
            items=[item(4027, assignee="ameesh-cx1")],
            events=[event(
                4027, 1, T - 21 * H,
                "hold: fenêtre : relire le témoin 24 h "
                "[réveil programmé: 2026-10-24 05:20 UTC]",
                actor="mesh-design")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["totals"]["slow_waits"], 0)
        self.assertEqual(report["totals"]["window_waits"], 1)
        window = report["window_waits"][0]
        self.assertEqual(window["lot"], 4027)
        self.assertEqual(window["kind"], "hold_window")
        self.assertEqual(window["who"], "mesh-design")
        self.assertEqual(window["declared"], "relire le témoin 24 h")
        self.assertEqual(window["due_ts"], T + 2 * H)
        self.assertEqual(window["due_in_s"], 2 * H)
        self.assertIn("aucune relance avant", window["gesture"])
        self.assertIn("relire le témoin", window["gesture"])
        # Le hold reste compté comme hold, mais ce n'est pas une attente de
        # plus de 2 h hors humain.
        self.assertEqual(report["totals"]["holds"], 1)

    def test_echeance_echue_redevient_une_attente_ordinaire(self):
        report = run(
            items=[item(4027, assignee="ameesh-cx1")],
            events=[event(
                4027, 1, T - 5 * H,
                "hold: fenêtre : relire le témoin 24 h "
                "[réveil programmé: 2026-10-24 00:20 UTC]",
                actor="mesh-design")])
        self.assertEqual(report["window_waits"], [])
        self.assertEqual(report["totals"]["window_waits"], 0)
        self.assertIn("hold", [row["kind"] for row in report["slow_waits"]])
        self.assertIn("wake_overdue",
                      [row["kind"] for row in report["slow_waits"]])

    def test_fenetre_declaree_couvre_les_autres_holds_du_lot(self):
        # Témoin #4027 : le hold de l'assigné (non pilote) et celui du pilote
        # portent la même fenêtre ; le lot ne doit pas rester dans les attentes
        # d'agent à cause du premier.
        first = ("hold: fenêtre du témoin 24 h encore en cours "
                 "[réveil programmé: 2026-10-24 05:20 UTC]")
        second = ("hold: (pilote mesh-design, 00:0xZ) Fenêtre d'observation "
                  "voulue : le témoin court jusqu'au 24/10 05:20 UTC "
                  "[réveil programmé: 2026-10-24 05:20 UTC]")
        holds = {4027: {
            "ameesh-cx1": {"what": "hold", "by": "ameesh-cx1",
                           "since": T - 21 * H, "note": first,
                           "pilot": False},
            "mesh-design": {"what": "hold", "by": "mesh-design",
                            "since": T - 10 * H, "note": second,
                            "pilot": True}}}
        report = run(
            items=[item(4027, assignee="ameesh-cx1")],
            events=[event(4027, 1, T - 21 * H, first, actor="ameesh-cx1"),
                    event(4027, 2, T - 10 * H, second, actor="mesh-design")],
            holds=holds)
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["totals"]["holds"], 2)
        self.assertEqual([row["lot"] for row in report["window_waits"]], [4027])
        self.assertEqual(report["window_waits"][0]["who"], "mesh-design")
        self.assertTrue(all(row.get("window_lot") for row in report["holds"]))
        # La fenêtre du pilote couvre le lot : le hold non pilote ne produit
        # pas de constat « attente d'agent ».
        self.assertFalse(any("lot 4027" in text
                             for text in report["unreadable"]))

    def test_fenetre_non_pilote_ne_soustrait_pas_le_lot(self):
        note = ("hold: fenêtre : relire le témoin "
                "[réveil programmé: 2026-10-24 05:20 UTC]")
        holds = {4027: {"dev": {"what": "hold", "by": "dev",
                                "since": T - 3 * H, "note": note,
                                "pilot": False}}}
        report = run(items=[item(4027)], events=[event(4027, 1, T - 3 * H, note)],
                     holds=holds)
        self.assertEqual([row["kind"] for row in report["slow_waits"]], ["hold"])
        self.assertEqual(report["window_waits"], [])
        self.assertTrue(any("observation window declared by dev" in text
                            for text in report["unreadable"]))

    def test_echeance_illisible_ne_devine_rien(self):
        report = run(
            items=[item(4027)],
            events=[event(4027, 1, T - 3 * H,
                          "hold: fenêtre : relire le témoin (due demain)",
                          actor="mesh-design")])
        self.assertEqual(report["window_waits"], [])
        self.assertEqual([row["kind"] for row in report["slow_waits"]], ["hold"])
        self.assertTrue(any("observation window due date unreadable" in text
                            for text in report["unreadable"]))

    def test_une_attente_externe_garde_la_priorite(self):
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 5 * H,
                          "hold: fenêtre : relire le témoin "
                          "[réveil programmé: 2026-10-24 05:20 UTC]",
                          actor="pilote"),
                    event(3482, 2, T - 3 * H,
                          "hold: attente bureau : poser le staging",
                          actor="pilote")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual([row["kind"] for row in report["external_waits"]],
                         ["hold_external"])
        # L'attente externe, plus explicite, n'est pas doublée par une fenêtre.
        self.assertEqual(report["window_waits"], [])

    def test_le_texte_liste_la_fenetre_a_part(self):
        report = run(
            items=[item(4027, assignee="ameesh-cx1")],
            events=[event(
                4027, 1, T - 21 * H,
                "hold: fenêtre : relire le témoin 24 h "
                "[réveil programmé: 2026-10-24 05:20 UTC]",
                actor="mesh-design")])
        text = debit.format_text(report)
        self.assertIn("Fenêtres d'observation", text)
        self.assertIn("#4027", text)
        self.assertIn("2026-10-24T05:20:00Z", text)
        self.assertIn("fenêtres : 1", text)
        self.assertNotIn("#4027", debit.format_text(
            run(items=[item(1)], events=[])))


class ReadTest(_Base):
    def _item(self, ident, assignee):
        self.db.execute(
            "INSERT INTO work_items (id, type, source, title, app, state, "
            "assignee) VALUES (%s, 'evolution', 'test', %s, 'ameesh', "
            "'blocked', %s)",
            (ident, "lot %d" % ident, assignee))

    def _event(self, ident, eid, at, note, actor="human:proprio",
               state="blocked"):
        self.db.execute(
            "INSERT INTO work_item_events "
            "(id, work_item_id, state, note, actor, created_at) "
            "VALUES (%s, %s, %s, %s, %s, to_timestamp(%s))",
            (eid, ident, state, note, actor, at))

    def test_read_liste_la_fenetre_hors_des_attentes(self):
        canons = types.SimpleNamespace(hosts=[], processes=[])
        self._item(4027, "ameesh-cx1")
        self._event(4027, 1, T - 21 * H,
                    "hold: fenêtre : relire le témoin 24 h "
                    "[réveil programmé: 2026-10-24 05:20 UTC]",
                    actor="human:proprio")
        report = debit.read(self.db, cfg=self.cfg, now=T, canons=[canons],
                            installed="1.21.0", jobs=False)
        self.assertNotIn(4027, [row["lot"] for row in report["slow_waits"]])
        windows = [row for row in report["window_waits"] if row["lot"] == 4027]
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["kind"], "hold_window")
        self.assertEqual(windows[0]["due_ts"], T + 2 * H)
        self.assertEqual(report["totals"]["slow_waits"], 0)


if __name__ == "__main__":
    unittest.main()
