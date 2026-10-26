# SPDX-License-Identifier: AGPL-3.0-only
"""#3672 : un lot fermé ne reste pas dans les attentes du débit.

Constat de l'auditeur, 26/10 06:2xZ : `ameesh debit --json` classait #3585
(fermé « superseded by #3540 », sans assigné) en `slow_waits`, kind `hold`,
détenteur ameesh-cx3, âge 13,7 h. Le lot n'avait aucun jalon terminal : sa
fermeture n'était écrite qu'au journal, et la note `hold:` posée ensuite
(même état `closed`) tenait comme une retenue active.

Témoin : un lot fermé au journal portant un hold n'apparaît plus dans
`slow_waits` et ne fait pas sortir `--check` 2 ; un lot `merged` retenu
après sa fusion reste lu (il peut attendre son déploiement).
"""
from __future__ import annotations

import unittest

from .test_3437_debit_dependance import H, T
from .test_3498_hold_externe import event, item, run


class LotFermeTest(unittest.TestCase):
    def test_hold_d_un_lot_ferme_sans_jalon_terminal(self):
        report = run(
            items=[item(3585, assignee="")],
            events=[event(3585, 1, T - 14 * H, None, state="intake"),
                    event(3585, 2, T - 13.8 * H,
                          "superseded by #3540", state="closed"),
                    event(3585, 3, T - 13.7 * H,
                          "hold: attente du bureau pour appliquer #3540",
                          actor="ameesh-cx3", state="closed")])
        self.assertEqual(report["slow_waits"], [])
        self.assertEqual(report["human_waits"], [])
        self.assertEqual(report["external_waits"], [])
        self.assertEqual(report["totals"]["slow_waits"], 0)

    def test_reveil_echu_d_un_lot_ferme(self):
        report = run(
            items=[item(3586)],
            events=[event(3586, 1, T - 5 * H,
                          "wake: relire (due 2026-10-01 00:00 UTC)",
                          actor="mty-amh-impl-05", state="build"),
                    event(3586, 2, T - 4 * H, "abandonné", state="closed")])
        self.assertEqual(report["slow_waits"], [])

    def test_lot_rouvert_apres_fermeture_reste_lu(self):
        report = run(
            items=[item(3587)],
            events=[event(3587, 1, T - 6 * H, "fermé", state="closed"),
                    event(3587, 2, T - 5 * H, "rouvert", state="build"),
                    event(3587, 3, T - 4 * H, "hold: attend un paquet",
                          actor="mty-amh-impl-05", state="build")])
        self.assertEqual([(row["lot"], row["kind"]) for row in report["slow_waits"]],
                         [(3587, "hold")])

    def test_hold_apres_fusion_reste_lu(self):
        report = run(
            items=[item(3588)],
            milestones=[{"work_item_id": 3588, "id": 1, "kind": "merged",
                         "at_ts": T - 5 * H}],
            events=[event(3588, 1, T - 5 * H, "merged", state="merged"),
                    event(3588, 2, T - 4 * H, "hold: attend un paquet",
                          actor="mty-amh-impl-05", state="merged")])
        self.assertEqual([(row["lot"], row["kind"]) for row in report["slow_waits"]],
                         [(3588, "hold")])


if __name__ == "__main__":
    unittest.main()
