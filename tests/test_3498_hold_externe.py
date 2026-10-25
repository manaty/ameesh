# SPDX-License-Identifier: AGPL-3.0-only
"""#3498 : un hold nomme son exécutant externe et le débit le range à part.

Constat du 25/10 06:35Z : 39 des 49 attentes de plus de 2 h du débit étaient
des holds, dont beaucoup attendaient un geste du bureau au go déjà signé. Ni
`waiting_human` (aucune demande de décision owner ouverte), ni
`external_executor` (#3447, réservé aux lots sans assigné) ne les décrivaient :
un agent suit le lot, et le bureau doit agir. Le débit les comptait comme
attentes d'agent et `--check` sortait 2 sans geste possible.

Témoin : un lot assigné, en hold « attente bureau : <geste> », sort des
`slow_waits` et entre dans `external_waits` avec son exécutant. La relance va
au bureau (ou à l'owner), à l'échéance déclarée ou après le seuil du débit ;
une attente externe ne fait pas sortir `--check` 2, même échue.
"""
from __future__ import annotations

import types
import unittest

from ameesh import debit, planned_waits
from .test_3437_debit_dependance import H, T
from .test_l119_backlog import _Base


def item(ident, *, assignee="mty-amh-impl-05", app="ameesh"):
    return {"id": ident, "title": "lot %d" % ident, "app": app,
            "assignee": assignee, "priority": 2}


def event(ident, eid, at, note, *, actor="human:proprio", state="blocked"):
    return {"work_item_id": ident, "id": eid, "state": state,
            "at_ts": at, "actor": actor, "note": note}


def run(**extra):
    values = dict(items=[], milestones=[], events=[], agents=[], turns=[],
                  merges=[], ci_runs=[], jobs=[], now=T, installed="1.21.0",
                  devices={"names": [], "phrases": [], "sources": [],
                           "unreadable": []},
                  hours=6, wait_s=2 * H, ci_hours=24, ci_slow_s=30 * 60)
    values.update(extra)
    return debit.build(**values)


class DeclarationTest(unittest.TestCase):
    def test_attente_bureau(self):
        declared = planned_waits.external_hold(
            "hold: attente bureau : poser le backup 011 et l'apply")
        self.assertEqual(declared["executor"], "bureau")
        self.assertEqual(declared["gesture"], "poser le backup 011 et l'apply")
        self.assertIsNone(declared["due"])

    def test_attente_owner_et_proprietaire(self):
        for text in ("hold: attente owner : signer le go",
                     "hold: attente propriétaire : signer le go",
                     "hold: attente du proprietaire : signer le go"):
            with self.subTest(text=text):
                declared = planned_waits.external_hold(text)
                self.assertEqual(declared["executor"], "owner")
                self.assertEqual(declared["gesture"], "signer le go")

    def test_attente_externe_avec_parentheses(self):
        for text in ("hold: attente externe (bureau) : geste root",
                     "hold: attente externe bureau : geste root",
                     "hold: attente du bureau : geste root"):
            with self.subTest(text=text):
                self.assertEqual(planned_waits.external_hold(text),
                                 {"executor": "bureau", "gesture": "geste root",
                                  "due": None})

    def test_echeance_avant_ou_apres_le_geste(self):
        for text in (
                "hold: attente bureau (due 2026-10-25 12:00 UTC) : poser",
                "hold: attente bureau : poser (due 2026-10-25 12:00 UTC)",
                "hold: attente bureau (échéance 2026-10-25 12:00 UTC) : poser"):
            with self.subTest(text=text):
                declared = planned_waits.external_hold(text)
                self.assertEqual(declared["gesture"], "poser")
                self.assertEqual(declared["due"], "2026-10-25 12:00 UTC")

    def test_jeton_de_reveil_hors_du_geste(self):
        declared = planned_waits.external_hold(
            "hold: attente bureau : poser wake_at=2026-10-25T12:00:00+00:00 "
            "[réveil programmé: 2026-10-25 12:00 UTC]")
        self.assertEqual(declared["gesture"], "poser")
        self.assertEqual(declared["due"], "2026-10-25 12:00 UTC")

    def test_prose_et_declaration_sans_geste_ne_declarent_rien(self):
        for text in ("hold: attend un paquet Nexlink",
                     "hold: attente du bureau depuis 3 h",
                     "hold: attente bureau :",
                     "hold: attente bureau",
                     "hold: attente de la réponse du bureau : x"):
            with self.subTest(text=text):
                self.assertIsNone(planned_waits.external_hold(text))


class ClassificationTest(unittest.TestCase):
    def test_hold_bureau_sort_des_attentes_d_agent(self):
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 3 * H,
                          "hold: attente bureau : déplacer pc-smichea et "
                          "vérifier les comptes")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["human_waits"], [])
        self.assertEqual(report["totals"]["slow_waits"], 0)
        self.assertEqual(report["totals"]["external_waits"], 1)
        external = report["external_waits"][0]
        self.assertEqual(external["lot"], 3482)
        self.assertEqual(external["kind"], "hold_external")
        self.assertEqual(external["who"], "bureau")
        self.assertIn("déplacer pc-smichea", external["gesture"])
        self.assertIn("bureau", external["gesture"])
        # Le hold reste compté comme hold, mais ce n'est pas une attente
        # d'agent.
        self.assertEqual(report["totals"]["holds"], 1)

    def test_hold_owner_sort_aussi_des_attentes_d_agent(self):
        report = run(
            items=[item(3421)],
            events=[event(3421, 1, T - 3 * H,
                          "hold: attente owner : fournir le secret ntfy")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual([row["who"] for row in report["external_waits"]],
                         ["owner"])
        self.assertIn("l'owner", report["external_waits"][0]["gesture"])

    def test_une_declaration_couvre_les_autres_holds_du_lot(self):
        # Le pilote a posé une retenue, l'auditeur déclare l'attente externe
        # sur le même lot : le lot ne doit pas rester dans les attentes
        # d'agent à cause de l'ancienne retenue.
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 5 * H,
                          "hold: pilote — attente du paquet", actor="mesh-design"),
                    event(3482, 2, T - 3 * H,
                          "hold: attente bureau : geste root", actor="auditeur")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(len(report["external_waits"]), 1)
        self.assertEqual(report["external_waits"][0]["who"], "bureau")
        self.assertEqual(report["totals"]["holds"], 2)
        self.assertTrue(all(row.get("external_lot") for row in report["holds"]))

    def test_un_hold_ordinaire_reste_une_attente_d_agent(self):
        report = run(
            items=[item(2381)],
            events=[event(2381, 1, T - 3 * H,
                          "hold: attend un paquet Nexlink réel")])
        self.assertEqual([row["kind"] for row in report["slow_waits"]], ["hold"])
        self.assertEqual(report["external_waits"], [])

    def test_relance_periodique_apres_le_seuil(self):
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 3 * H,
                          "hold: attente bureau : geste root")])
        external = report["external_waits"][0]
        self.assertTrue(external["reminder_due"])
        self.assertIsNone(external["due_ts"])
        self.assertEqual(external["reminder_ts"], T - H)  # since + 2 h
        self.assertEqual(report["totals"]["external_reminders"], 1)

    def test_relance_future_ne_sonne_pas(self):
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 3 * H,
                          "hold: attente bureau : geste root "
                          "(due 2026-10-24 10:00 UTC)")])
        external = report["external_waits"][0]
        self.assertFalse(external["reminder_due"])
        self.assertEqual(external["due_ts"], T + 6 * H + 40 * 60)
        self.assertEqual(report["totals"]["external_reminders"], 0)

    def test_reveil_programme_devient_l_echeance_externe(self):
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 5 * H,
                          "hold: attente bureau : geste root "
                          "[réveil programmé: 2026-10-24 02:00 UTC]")])
        external = report["external_waits"][0]
        self.assertEqual(external["due_ts"], T - H - 20 * 60)
        self.assertTrue(external["reminder_due"])
        # Le réveil reste armé pour l'assigné, mais il ne devient pas une
        # attente d'agent.
        self.assertEqual(len(report["wakes"]), 1)
        self.assertNotIn("wake_overdue",
                         [row["kind"] for row in report["slow_waits"]])

    def test_echeance_passee_relance(self):
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 3 * H,
                          "hold: attente owner : signer le go "
                          "(due 2026-10-24 02:00 UTC)")])
        external = report["external_waits"][0]
        self.assertTrue(external["reminder_due"])
        self.assertEqual(external["due_ts"], T - H - 20 * 60)

    def test_un_hold_non_pilote_ne_soustrait_pas_le_lot(self):
        note = "hold: attente bureau : geste root"
        holds = {3482: {"dev": {"what": "hold", "by": "dev", "since": T - 3 * H,
                                "note": note, "pilot": False}}}
        report = run(items=[item(3482)], events=[event(3482, 1, T - 3 * H, note)],
                     holds=holds)
        self.assertEqual([row["kind"] for row in report["slow_waits"]], ["hold"])
        self.assertEqual(report["external_waits"], [])
        self.assertTrue(any("not a pilot hold" in text
                            for text in report["unreadable"]))

    def test_echeance_illisible_est_dite(self):
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 3 * H,
                          "hold: attente bureau : geste root (due demain)")])
        self.assertTrue(any("due date unreadable" in text
                            for text in report["unreadable"]))
        # Le repli périodique reste borné : la relance ne dort pas.
        self.assertTrue(report["external_waits"][0]["reminder_due"])

    def test_texte_nomme_l_executant_et_la_relance(self):
        report = run(
            items=[item(3482)],
            events=[event(3482, 1, T - 3 * H,
                          "hold: attente bureau : geste root")])
        text = debit.format_text(report)
        self.assertIn("Attentes externes", text)
        self.assertIn("bureau", text)
        self.assertIn("relance due", text)
        self.assertIn("canon §10", text)


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

    def test_read_classe_le_hold_bureau_en_attente_externe(self):
        canons = types.SimpleNamespace(hosts=[], processes=[])
        self._item(3482, "mty-amh-impl-05")
        self._event(3482, 1, T - 3 * H,
                    "hold: attente bureau : geste root (due 2026-10-24 10:00 UTC)")
        report = debit.read(self.db, cfg=self.cfg, now=T, canons=[canons],
                            installed="1.21.0", jobs=False)
        self.assertNotIn(3482, [row["lot"] for row in report["slow_waits"]])
        external = [row for row in report["external_waits"]
                    if row["lot"] == 3482]
        self.assertEqual(len(external), 1)
        self.assertEqual(external[0]["who"], "bureau")
        self.assertEqual(external[0]["kind"], "hold_external")
        self.assertFalse(external[0]["reminder_due"])
        self.assertEqual(report["totals"]["external_reminders"], 0)


if __name__ == "__main__":
    unittest.main()
