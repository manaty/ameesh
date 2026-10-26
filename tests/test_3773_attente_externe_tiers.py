# SPDX-License-Identifier: AGPL-3.0-only
"""#3773 : un hold « attente externe : <tiers> » sort des attentes d'agent.

Constat de l'auditeur du 26/10 15:21Z : #666 porte « hold: attente externe :
revue Apple (soumise vers 07:18Z) [réveil programmé: …] ». #3498 ne lisait que
le bureau et l'owner ; le tiers extérieur (revue Apple, store) restait donc une
attente d'agent et `ameesh debit` le sortait en slow_wait de niveau 2 à chaque
passage. Le témoin : la déclaration « attente externe : revue Apple » entre
dans `external_waits`, avec son tiers et sa relance, et ne fait pas sortir
`--check` 2.
"""
from __future__ import annotations

import unittest

from ameesh import debit, planned_waits
from .test_3437_debit_dependance import H, T
from .test_3498_hold_externe import event, item, run


class DeclarationTest(unittest.TestCase):
    def test_tiers_externe_sans_executant(self):
        declared = planned_waits.external_hold(
            "hold: attente externe : revue Apple (soumise vers 07:18Z)")
        self.assertEqual(declared,
                         {"executor": "externe",
                          "gesture": "revue Apple (soumise vers 07:18Z)",
                          "due": None})

    def test_tiers_externe_garde_son_reveil(self):
        declared = planned_waits.external_hold(
            "hold: attente externe : examen du store "
            "[réveil programmé: 2026-10-24 05:20 UTC]")
        self.assertEqual(declared["executor"], "externe")
        self.assertEqual(declared["gesture"], "examen du store")
        self.assertEqual(declared["due"], "2026-10-24 05:20 UTC")

    def test_executant_nomme_prime_sur_le_tiers(self):
        # #3498 ne change pas : « externe (bureau) » et « attente owner »
        # nomment leur exécutant, ils ne deviennent pas un tiers générique.
        for text, who in (
                ("hold: attente externe (bureau) : geste root", "bureau"),
                ("hold: attente externe bureau : geste root", "bureau"),
                ("hold: attente owner : signer le go", "owner"),
                ("hold: attente du propriétaire : signer le go", "owner")):
            with self.subTest(text=text):
                declared = planned_waits.external_hold(text)
                self.assertEqual(declared["executor"], who)

    def test_prose_et_declaration_sans_geste_ne_declarent_rien(self):
        for text in ("hold: attente externe de l'APK",
                     "hold: attente externe",
                     "hold: attente externe :",
                     "hold: attente externe depuis 3 h"):
            with self.subTest(text=text):
                self.assertIsNone(planned_waits.external_hold(text))


class ClassificationTest(unittest.TestCase):
    def test_hold_tiers_sort_des_attentes_d_agent(self):
        report = run(
            items=[item(666, assignee="codex1", app="nexlink")],
            events=[event(
                666, 1, T - 3 * H,
                "hold: attente externe : revue Apple (soumise vers 07:18Z) "
                "[réveil programmé: 2026-10-24 01:20 UTC]")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["human_waits"], [])
        self.assertEqual(report["totals"]["slow_waits"], 0)
        self.assertEqual(report["totals"]["external_waits"], 1)
        self.assertEqual(report["totals"]["external_reminders"], 1)
        self.assertEqual(report["totals"]["holds"], 1)
        external = report["external_waits"][0]
        self.assertEqual(external["lot"], 666)
        self.assertEqual(external["kind"], "hold_external")
        self.assertEqual(external["who"], "externe")
        self.assertEqual(external["declared"],
                         "revue Apple (soumise vers 07:18Z)")
        self.assertEqual(external["due_ts"], T - 2 * H)
        self.assertTrue(external["reminder_due"])
        self.assertIn("revue Apple", external["gesture"])
        self.assertIn("tiers extérieur", external["gesture"])

    def test_hold_tiers_a_echeance_future_ne_relance_pas(self):
        report = run(
            items=[item(666, assignee="codex1", app="nexlink")],
            events=[event(
                666, 1, T - 3 * H,
                "hold: attente externe : revue Apple "
                "[réveil programmé: 2026-10-24 05:20 UTC]")])
        self.assertEqual(report["slow_waits"], [])
        external = report["external_waits"][0]
        self.assertFalse(external["reminder_due"])
        self.assertEqual(report["totals"]["external_reminders"], 0)

    def test_le_texte_range_le_tiers_et_porte_la_relance_au_bureau(self):
        report = run(
            items=[item(666, assignee="codex1", app="nexlink")],
            events=[event(
                666, 1, T - 3 * H,
                "hold: attente externe : revue Apple "
                "[réveil programmé: 2026-10-24 01:20 UTC]")])
        text = debit.format_text(report)
        self.assertIn("Attentes externes", text)
        self.assertIn("#666", text)
        self.assertIn("revue Apple", text)
        self.assertIn("porter au bureau", text)


if __name__ == "__main__":
    unittest.main()
