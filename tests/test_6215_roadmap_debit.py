# SPDX-License-Identifier: AGPL-3.0-only
"""#6215 : la note « roadmap: épuisée » est lue par une seule fonction.

Constat de l'auditeur (07/11 17:15Z et 18:15Z) : l'orchestrateur a noté
« roadmap: épuisée nexlink » sur #5138 à 16:29Z. Le sommeil automatique la lit
et endort les agents nexlink, mais `ameesh debit` ne l'affiche pas et compte
encore le hold du lot de découpage comme attente lente (--check sort 2).

Cause réelle : la note était bien lue, mais `capacity` **annulait** le verdict
d'épuisement dès qu'un lot du projet était retenu par la garde d'empreinte
(#4561) — un lot que `split_startable` ne compte pourtant pas démarrable et qui
n'empêche donc pas le sommeil (#4717). Les deux lecteurs divergeaient sur cette
seule condition.

Témoins :
* la forme réelle de la note de #5138 est reconnue par `roadmap_state` (le
  sommeil) et par `_roadmap_verdicts` (le débit), verdict identique ;
* une `roadmap: rouverte` postérieure rouvre pour les deux ;
* une note sur un autre lot (fermé, ordinaire, autre projet) est ignorée,
  comme pour le sommeil qui ne lit que le lot de découpage ouvert ;
* un lot retenu par la garde d'empreinte ne cache plus l'épuisement, et
  l'alerte d'empreinte garde son geste (#4561) ;
* le hold du lot de découpage posé avant la note est couvert par elle
  (`roadmap_waits`, hors --check) ; reposé après, il reste une attente lente.
"""
from __future__ import annotations

import unittest

from ameesh import debit, file_decoupage

from tests.test_3256_debit_capacity import H, T, measure, split_lot, team
from tests.test_3292_debit import event, item, run
from tests.test_3292_debit import T as BUILD_T

SPLIT_NEXLINK = "file-decoupage:nexlink"

#: La note réelle de #5138 (événement 139151, 2026-11-07T16:29:55Z), texte
#: intégral : la reconnaissance ne doit pas dépendre d'une forme courte.
REAL_NOTE_5138 = (
    "roadmap: épuisée nexlink — 16:3xZ, après la fusion du paquet 464 "
    "(develop a5f8efbd4). Source : docs/roadmap.md (rafraîchi par #6177, "
    "paquet 462) et la file. En cours : #6167 (texte alternatif serveur, "
    "deepseek2), #6125 (3w, claude1), #6132 (avis claude1, paquet suivant), "
    "paquet 465 (#6200). S'ouvre ensuite : #6140 à la fusion de #6132. Le "
    "reste attend l'owner ou une porte : décisions #198674 (#659 C1 T1, "
    "signature) et #194929 (T6 pages hôtes, en retard), séance PC (#5772 "
    "P2b/P2c, #5003 B84), #4712 (17/11), #6126 (plancher de version 1m), "
    "MP-2/MP-14 prod (#5772, #5647), #5684 (première route réelle). Paniers "
    "sous le seuil de lot (#6199, #6201, #6059…) laissés au panier.")


def note(text, ident=5138, source=SPLIT_NEXLINK, at=T - H, eid=139151):
    return {"id": eid, "work_item_id": ident, "source": source,
            "note": text, "at_ts": at}


def nexlink(notes, **kwargs):
    kwargs.setdefault("open_lots", [split_lot(5138, "nexlink")])
    return measure(team(2, 8, "nexlink"), roadmap_notes=notes, **kwargs)


class RealNoteTest(unittest.TestCase):
    def test_the_real_note_of_5138_is_read_by_both_readers(self):
        notes = [note(REAL_NOTE_5138)]
        # le sommeil : `roadmap_state` sur les notes du lot de découpage ouvert
        self.assertEqual(file_decoupage.roadmap_state(notes, "nexlink"),
                         file_decoupage.EXHAUSTED_STATE)
        self.assertTrue(file_decoupage.exhausted(notes, "nexlink"))
        self.assertEqual(file_decoupage.exhausted_since(notes, "nexlink"), T - H)
        # le débit : le même verdict, sur le même lot
        self.assertEqual(
            debit._roadmap_verdicts(notes, [split_lot(5138, "nexlink")]),
            {"nexlink": {"lot": 5138, "at_ts": T - H}})
        cap = nexlink(notes)
        self.assertEqual(cap["projects"][0]["roadmap_exhausted"],
                         {"lot": 5138, "at_ts": T - H})
        self.assertEqual(cap["alerts"], [])
        self.assertIn("feuille de route épuisée", "\n".join(
            debit._capacity_text(cap)))

    def test_a_footprint_held_lot_no_longer_reopens_the_route(self):
        # le constat réel du 17:16Z : nexlink avait un lot en attente
        # d'empreinte, et le débit annulait l'épuisement alors que le sommeil
        # endormait les agents (le lot n'est pas démarrable, #4717).
        cap = nexlink([note(REAL_NOTE_5138)],
                      open_lots=[split_lot(5138, "nexlink"),
                                 {"id": 6140, "source": "github:issue",
                                  "app": "nexlink", "state": "intake"}],
                      startable=[{"id": 6140, "app": "nexlink"}],
                      footprint={6140: [{"id": 6132, "state": "qa"}]})
        project = cap["projects"][0]
        self.assertEqual(project["roadmap_exhausted"]["lot"], 5138)
        self.assertEqual([row["lot"] for row in project["footprint_lots"]], [6140])
        self.assertIn("en attente d'empreinte", "\n".join(
            debit._capacity_text(cap)))
        # #4561 : le projet ne se tait pas pour autant, l'alerte nomme le geste
        self.assertEqual(len(cap["alerts"]), 1)
        self.assertIn("en attente d'empreinte", cap["alerts"][0]["gesture"])


class RuleTest(unittest.TestCase):
    def test_a_later_reopening_reopens_for_both_readers(self):
        notes = [note(REAL_NOTE_5138, at=T - 2 * H, eid=139151),
                 note("roadmap: rouverte nexlink — lots démarrables sans "
                      "assigné (10:05Z) : #6122", at=T - H, eid=139200)]
        self.assertEqual(file_decoupage.roadmap_state(notes, "nexlink"),
                         file_decoupage.REOPENED_STATE)
        self.assertFalse(file_decoupage.exhausted(notes, "nexlink"))
        self.assertEqual(
            debit._roadmap_verdicts(notes, [split_lot(5138, "nexlink")]), {})
        cap = nexlink(notes)
        self.assertIsNone(cap["projects"][0]["roadmap_exhausted"])
        self.assertEqual(len(cap["alerts"]), 1)

    def test_a_later_exhaustion_after_a_reopening_holds(self):
        notes = [note("roadmap: rouverte nexlink", at=T - 2 * H, eid=1),
                 note("roadmap: épuisée nexlink", at=T - H, eid=2)]
        self.assertEqual(
            debit._roadmap_verdicts(notes, [split_lot(5138, "nexlink")]),
            {"nexlink": {"lot": 5138, "at_ts": T - H}})

    def test_a_note_on_another_lot_is_ignored(self):
        # même projet mais autre lot (le lot de découpage fermé #4952), lot
        # ordinaire, autre projet : la note ne vaut que sur le lot de
        # découpage OUVERT du projet, comme pour le sommeil.
        open_split = [split_lot(5138, "nexlink")]
        closed = split_lot(4952, "nexlink", state="closed")
        cases = [
            [note("roadmap: épuisée nexlink", ident=4952)],
            [note("roadmap: épuisée nexlink", ident=999, source="")],
            [note("roadmap: épuisée nexlink", ident=999,
                  source="file-decoupage:ameesh")],
        ]
        for notes in cases:
            with self.subTest(notes=notes):
                self.assertEqual(
                    debit._roadmap_verdicts(notes, open_split + [closed]), {})
                # le sommeil, lui, ne lit que le lot ouvert #5138 : aucune note
                self.assertIsNone(
                    file_decoupage.roadmap_state([], "nexlink"))

    def test_a_note_after_the_open_split_was_replaced_is_ignored(self):
        # un lot de découpage fermé puis un nouveau ouvert : seule la note du
        # nouveau lot compte, même si une note de l'ancien lot est plus
        # récente — la règle est celle de `_open_split`.
        notes = [note("roadmap: épuisée nexlink", ident=4952, at=T - 2 * H,
                      eid=1),
                 note("roadmap: rouverte nexlink", ident=5138, at=T - 3 * H,
                      eid=2)]
        self.assertEqual(
            debit._roadmap_verdicts(notes, [split_lot(5138, "nexlink")]), {})


class SplitHoldTest(unittest.TestCase):
    def split_item(self):
        return dict(item(5138, app="nexlink"), source=SPLIT_NEXLINK,
                    state="intake")

    def test_a_hold_before_the_note_is_covered_by_it(self):
        report = run(
            items=[self.split_item()],
            events=[event(5138, 1, BUILD_T - 3 * H,
                          "hold: découpage suivant à la fusion du paquet 461",
                          actor="orchestrateur", state="intake")],
            roadmap_exhausted={"nexlink": {"lot": 5138, "at_ts": BUILD_T - H}})
        self.assertEqual(report["totals"]["slow_waits"], 0)
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["totals"]["roadmap_waits"], 1)
        wait = report["roadmap_waits"][0]
        self.assertEqual(wait["kind"], "hold_roadmap")
        self.assertEqual(wait["lot"], 5138)
        self.assertEqual(wait["project"], "nexlink")
        self.assertIn("roadmap: rouverte nexlink", wait["gesture"])
        self.assertIn("Feuilles de route épuisées, hors --check",
                      debit.format_text(report))

    def test_a_hold_after_the_note_stays_a_slow_wait(self):
        # un hold reposé après la note est un geste nouveau : il n'est pas
        # couvert par l'épuisement et --check le voit (canon §9).
        report = run(
            items=[self.split_item()],
            events=[event(5138, 1, BUILD_T - 3 * H,
                          "hold: relire le découpage de la vague suivante",
                          actor="orchestrateur", state="intake")],
            roadmap_exhausted={"nexlink": {"lot": 5138, "at_ts": BUILD_T - 4 * H}})
        self.assertEqual(report["totals"]["slow_waits"], 1)
        self.assertEqual(report["totals"]["roadmap_waits"], 0)
        self.assertEqual(report["slow_waits"][0]["lot"], 5138)


if __name__ == "__main__":
    unittest.main()
