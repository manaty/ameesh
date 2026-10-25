# SPDX-License-Identifier: AGPL-3.0-only
"""#3555 — le temps parqué sous une retenue ne compte pas après le gel.

Constat : #3496, témoin du drill #3439, a le GO de cx2 depuis 06:27Z. Il est
parqué sous un hold du pilote de 07:36 à 12:06Z, puis re-gelé pour T0 12:55Z.
À 12:07Z, `slo.py` lève « review GO → merge late by 4 h 06 », niveau 3, vers le
bureau et l'auditeur : la suspension des retenues (#2916, `hold_release_ts` et
`pilot_hold_releases`) ne s'appliquait qu'au délai intake.

Attendu : le temps passé en `blocked` ou `waiting_human`, ou sous un `hold:`
de pilote, ne compte pas dans les étapes d'après le gel (`green_verdict`,
`verdict_merge` ; `blocked_freeze` l'était déjà, #3333) ; l'horloge repart de
la levée. Témoins : le journal de #3496 rejoué ne lève rien à 12:07Z ; un lot
sans retenue garde son alerte.
"""
from __future__ import annotations

import unittest

from ameesh import slo

#: 2026-10-25T06:24:00Z, le gel de #3496 (le journal réel, à la minute près).
T0 = 1_792_909_440.0
GO = 3 * 60                  # 06:27, verdict OK de cx2
HELD = 28 * 60               # 06:52, T0 avancé abandonné -> blocked
SLOT = 37 * 60               # 07:01, premier créneau de paquet après le GO
RELEASE = 5 * 3600 + 42 * 60  # 12:06, retour en qa (re-gel pour 12:55)
NOW = RELEASE + 60           # 12:07, l'instant de la fausse alerte


def _ev(ident: int, state: str, at: float, note: str = "") -> dict:
    return {"id": ident, "state": state, "created_ts": at, "note": note,
            "actor": "mty-amh-impl-06"}


def _item(state: str = "qa") -> dict:
    return {"id": 3496, "state": state, "priority": 2,
            "assignee": "mty-amh-impl-06",
            "created_ts": T0, "updated_ts": T0 + NOW}


def _jalons(*, verdict: float = T0 + GO, merged=None) -> dict:
    return {"requested": T0, "verdict": verdict, "verdict_kind": "ok",
            "verdict_current": True, "merged": merged}


def _rows(events, *, state="qa", jalons=None, now=T0 + NOW, **kw) -> list:
    return slo.lot_steps(_item(state), jalons or _jalons(), events,
                         now=now, **kw)


def _row(rows, step):
    got = [r for r in rows if r["step"] == step]
    assert len(got) == 1, "%s absent de %r" % (step, rows)
    return got[0]


def _journal_3496() -> list:
    """Le journal de #3496 : qa, blocked au gate manqué, hold du pilote, retour
    en qa à 12:06Z. Le hold de l'assigné (08:22) ne change pas l'état."""
    return [
        _ev(1, "qa", T0, "freeze aabe8d706516"),
        _ev(2, "blocked", T0 + HELD,
            "T0 avancé 07:00Z abandonné au gate 06:50Z ; témoin conservé"),
        _ev(3, "blocked", T0 + 72 * 60,
            "hold: (pilote 07:3xZ) drill #3439 reporté à 13:00Z"),
        _ev(4, "blocked", T0 + 118 * 60,
            "hold: témoin en attente du réveil 12:30Z"),
        _ev(5, "qa", T0 + RELEASE,
            "re-gel pour T0 12:55Z ; verdict cx2 OK conservé"),
    ]


class Temoin3496Test(unittest.TestCase):
    """Le journal réel rejoué : plus d'alerte à 12:07Z, alerte sans retenue."""

    def test_le_journal_rejoue_ne_leve_rien_a_1207(self):
        rows = _rows(_journal_3496(), packet_slot=T0 + SLOT)
        row = _row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + RELEASE)
        self.assertEqual(row["elapsed_s"], 60)
        self.assertEqual(row["level"], 0)
        self.assertEqual(row["hold_released_ts"], T0 + RELEASE)
        self.assertEqual(slo.breaches(rows), [])

    def test_un_lot_sans_retenue_garde_son_alerte(self):
        # Le même GO et le même créneau, sans le parcage : le retard est réel.
        rows = _rows([_ev(1, "qa", T0, "freeze")],
                     packet_slot=T0 + SLOT)
        row = _row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + SLOT)
        self.assertEqual(row["elapsed_s"], int(NOW - SLOT))
        self.assertEqual(row["level"], 3)
        self.assertEqual(row["targets"], ("bureau", "auditeur"))
        self.assertEqual([r["step"] for r in slo.breaches(rows)],
                         ["verdict_merge"])


class HoldApresLeGoTest(unittest.TestCase):
    """`verdict_merge` repart de la levée, jamais avant le GO."""

    def test_une_retenue_d_etat_repousse_le_depart(self):
        for state in slo.HOLDS:
            with self.subTest(state=state):
                events = [_ev(1, "qa", T0), _ev(2, state, T0 + 60),
                          _ev(3, "qa", T0 + 3600)]
                rows = _rows(events, now=T0 + 3660)
                row = _row(rows, "verdict_merge")
                self.assertEqual(row["started_ts"], T0 + 3600)
                self.assertEqual(row["elapsed_s"], 60)
                self.assertEqual(row["level"], 0)

    def test_un_hold_de_pilote_leve_repousse_le_depart(self):
        # Le hold explicite (#1664) ne change pas l'état : seule sa levée,
        # autorisée par l'appelant, date le redépart.
        rows = _rows([_ev(1, "qa", T0)], hold_releases=[T0 + 3600],
                     now=T0 + 3660)
        row = _row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + 3600)
        self.assertEqual(row["elapsed_s"], 60)
        self.assertEqual(row["hold_released_ts"], T0 + 3600)

    def test_une_levee_avant_le_go_ne_repousse_pas_le_depart(self):
        events = [_ev(1, "blocked", T0), _ev(2, "qa", T0 + 60)]
        rows = _rows(events, hold_releases=[T0 + 120], now=T0 + GO + 60)
        row = _row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + GO)
        self.assertNotIn("hold_released_ts", row)

    def test_l_horloge_s_arrete_a_l_entree_en_retenue(self):
        # #2231 : un lot déjà en retard au moment où il est parqué garde son
        # alerte (le pilote doit le débloquer) ; le temps du parcage, lui, ne
        # compte pas — l'horloge s'arrête à l'entrée en retenue.
        for state in slo.HOLDS:
            with self.subTest(state=state):
                events = [_ev(1, "qa", T0), _ev(2, state, T0 + 3600)]
                rows = _rows(events, state=state, jalons=_jalons(verdict=T0),
                             now=T0 + 10 * 3600)
                row = _row(rows, "verdict_merge")
                self.assertEqual(row["elapsed_s"], 3600)
                self.assertEqual(row["level"], 2)
                self.assertEqual(row["targets"], ("pilot",))

    def test_le_parcage_ne_compte_pas_dans_la_mesure(self):
        for state in slo.HOLDS:
            with self.subTest(state=state):
                events = [_ev(1, "qa", T0), _ev(2, state, T0 + 60)]
                rows = _rows(events, state=state, jalons=_jalons(verdict=T0),
                             now=T0 + 10 * 3600)
                row = _row(rows, "verdict_merge")
                self.assertEqual(row["elapsed_s"], 60)
                self.assertEqual(row["level"], 0)
                self.assertEqual(slo.breaches(rows), [])

    def test_la_levee_apres_le_creneau_de_paquet_gouverne(self):
        # #2335 : le créneau reste lisible, mais c'est la levée, postérieure,
        # qui fait repartir l'horloge.
        rows = _rows([_ev(1, "qa", T0)], packet_slot=T0 + 1200,
                     hold_releases=[T0 + 3600], now=T0 + 3660)
        row = _row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + 3600)
        self.assertEqual(row["packet_slot"], T0 + 1200)
        self.assertEqual(row["elapsed_s"], 60)
        self.assertEqual(row["level"], 0)

    def test_une_fusion_deja_faite_exclut_le_temps_parque(self):
        rows = _rows([_ev(1, "qa", T0)], jalons=_jalons(merged=T0 + 7200),
                     hold_releases=[T0 + 3600], now=T0 + 10000)
        row = _row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + 3600)
        self.assertEqual(row["elapsed_s"], 3600)
        self.assertEqual(row["done_ts"], T0 + 7200)
        self.assertEqual(row["hold_released_ts"], T0 + 3600)

    def test_une_levee_apres_la_fusion_ne_reecrit_pas_le_passe(self):
        rows = _rows([_ev(1, "qa", T0)], jalons=_jalons(merged=T0 + 3600),
                     hold_releases=[T0 + 7200], now=T0 + 10000)
        row = _row(rows, "verdict_merge")
        self.assertEqual(row["started_ts"], T0 + GO)
        self.assertEqual(row["elapsed_s"], int(3600 - GO))
        self.assertNotIn("hold_released_ts", row)


class AvisEnCoursTest(unittest.TestCase):
    """`green_verdict` est aussi une étape d'après le gel : même suspension."""

    REVIEW = {"name": "ameesh-cx2", "designated_ts": T0 + 60}

    def test_un_avis_du_repart_de_la_levee(self):
        rows = _rows([_ev(1, "qa", T0)], reviews=[dict(self.REVIEW)],
                     hold_releases=[T0 + 3600], now=T0 + 3660)
        row = _row(rows, "green_verdict")
        self.assertEqual(row["reviewer"], "ameesh-cx2")
        self.assertEqual(row["started_ts"], T0 + 3600)
        self.assertEqual(row["elapsed_s"], 60)
        self.assertEqual(row["level"], 0)
        self.assertEqual(row["hold_released_ts"], T0 + 3600)

    def test_le_temps_du_relecteur_s_arrete_a_l_entree_en_retenue(self):
        for state in slo.HOLDS:
            with self.subTest(state=state):
                events = [_ev(1, "qa", T0), _ev(2, state, T0 + 7300)]
                rows = _rows(events, state=state, reviews=[dict(self.REVIEW)],
                             now=T0 + 20 * 3600)
                row = _row(rows, "green_verdict")
                # 7300 s de retard acquis avant l'entrée, pas 20 h de parcage.
                self.assertEqual(row["elapsed_s"], 7300 - 60)
                self.assertEqual(row["level"], 1)

    def test_une_retenue_avant_la_designation_ne_fait_pas_courir(self):
        rows = _rows([_ev(1, "qa", T0), _ev(2, "blocked", T0 + 10)],
                     state="blocked", reviews=[dict(self.REVIEW)],
                     now=T0 + 20 * 3600)
        row = _row(rows, "green_verdict")
        self.assertEqual(row["elapsed_s"], 0)
        self.assertEqual(slo.breaches(rows), [])


if __name__ == "__main__":
    unittest.main()
