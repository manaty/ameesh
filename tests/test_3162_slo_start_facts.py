# SPDX-License-Identifier: AGPL-3.0-only
"""#3162 : SLO « ready → started » — un lot déjà commencé et resté en intake.

Cas réels du 23/10 : #3151 (P1, début mesuré à 13:51Z, branche associée à
14:04Z, alerte niveau 1 à 14:08Z) et #3146 (gelé, auto-merge armé, alerte
niveau 2 à 13:54Z). Le début mesuré conclut l'étape comme le passage en
build ; une branche associée ou un gel prouvent le début sans le dater : ni
alerte, ni durée inventée (#1982). Un lot sans aucun de ces faits garde son
alerte.
"""
from __future__ import annotations

import unittest

from ameesh import estimates, progress, slo, work

T0 = 1_700_000_000.0


def _item(**extra):
    item = {"state": "intake", "assignee": "executant", "created_ts": T0,
            "updated_ts": T0}
    item.update(extra)
    return item


def _ev(state, at, note="", actor="chef"):
    return {"state": state, "created_ts": at, "note": note, "actor": actor}


def _intake(rows):
    return [r for r in rows if r["step"] == "intake_started"]


class StartFactsTest(unittest.TestCase):
    def _rows(self, item, events, *, now, jalons=None):
        if jalons is None:
            jalons = progress._work_item_milestones(item, events, [])
        return slo.lot_steps(item, jalons, events, now=now)

    def test_debut_mesure_en_intake_conclut_l_etape(self):
        # #3151 : P1, début mesuré 60 s après la création, toujours en intake
        # 18 min plus tard — l'étape est tenue, sans alerte.
        events = [_ev("intake", T0, "created"),
                  _ev("intake", T0 + 60,
                      estimates.START_NOTE + "first turn of executant on the lot",
                      actor="executant")]
        item = _item(priority=1)
        rows = _intake(self._rows(item, events, now=T0 + 18 * 60))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], 0)
        self.assertEqual(rows[0]["done_ts"], T0 + 60)
        self.assertEqual(rows[0]["elapsed_s"], 60)
        self.assertEqual(slo.breaches(rows), [])

    def test_debut_mesure_avant_le_build_date_le_debut(self):
        events = [_ev("intake", T0),
                  _ev("intake", T0 + 600, estimates.START_NOTE + "first turn of x on the lot"),
                  _ev("build", T0 + 5400)]
        item = _item(state="build", updated_ts=T0 + 5400)
        row = _intake(self._rows(item, events, now=T0 + 7200))[0]
        self.assertEqual(row["done_ts"], T0 + 600)
        self.assertEqual(row["level"], 0)

    def test_lot_gele_en_intake_sans_alerte(self):
        # #3146 : gelé (jalon `frozen`), resté en intake — pas d'alerte.
        events = [_ev("intake", T0, "created")]
        item = _item()
        jalons = progress._work_item_milestones(item, events, [])
        jalons["frozen"] = T0 + 1200
        rows = _intake(self._rows(item, events, now=T0 + 3 * 3600, jalons=jalons))
        self.assertEqual(rows, [])

    def test_branche_associee_en_intake_sans_alerte(self):
        for note in ("branch review/x-1-a to main — explicit association (before: none)",
                     "branch lot/1-a — merge sweep (before: none)"):
            with self.subTest(note=note):
                events = [_ev("intake", T0, "created"), _ev("intake", T0 + 600, note)]
                rows = _intake(self._rows(_item(), events, now=T0 + 3 * 3600))
                self.assertEqual(rows, [])

    def test_branche_detachee_n_est_pas_un_debut(self):
        events = [_ev("intake", T0, "created"),
                  _ev("intake", T0 + 600, work.DETACH_NOTE + "lot/1-a (PR closed) (before: x)")]
        rows = _intake(self._rows(_item(), events, now=T0 + 3660))
        self.assertEqual(rows[0]["level"], 1)

    def test_lot_sans_aucun_fait_garde_son_alerte(self):
        events = [_ev("intake", T0, "created"),
                  _ev("intake", T0 + 60, "assigned to executant (before: nobody)")]
        item = _item(priority=1)
        rows = _intake(self._rows(item, events, now=T0 + 3600))
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]["level"], 1)
        self.assertIn("bureau", rows[0]["targets"])


if __name__ == "__main__":
    unittest.main()
