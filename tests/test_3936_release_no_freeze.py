# SPDX-License-Identifier: AGPL-3.0-only
"""#3936 — un lot release n'attend pas de nouveau gel (horloge SLO).

La porte des familles (#3742) refuse le gel d'un lot `release/**` dont la plage
porte les lots déjà relus des trois familles ; sa fusion est le geste du pilote
sur la tête verte (canon §2). Après un retour en build ou un avis bloquant,
`blocked_freeze` ne doit donc plus courir (fausses escalades P1, #3808, #3721).
"""
import time
import unittest

from ameesh import lot_class, slo

H = 3600.0


def _ev(state, ts, note=""):
    return {"state": state, "created_ts": ts, "note": note}


class ReleaseNoFreezeTest(unittest.TestCase):
    def rows(self, item, *, fixes=()):
        now = time.time()
        verdict = now - 6 * H
        events = [_ev("intake", now - 10 * H), _ev("build", now - 9 * H),
                  _ev("qa", now - 7 * H), _ev("build", now - 5 * H)]
        jalons = {"requested": now - 10 * H, "verdict": verdict,
                  "verdict_kind": "ok", "verdict_current": True}
        base = {"id": 1, "state": "build", "assignee": "rel", "priority": 1,
                "created_ts": now - 10 * H}
        base.update(item)
        return slo.lot_steps(base, jalons, events, now=now, fixes=list(fixes))

    def steps(self, rows):
        return [r["step"] for r in rows]

    def test_witness_an_ordinary_lot_still_owes_the_fix(self):
        rows = self.rows({"branch": "lot/x", "branch_target": "main"})
        got = [r for r in rows if r["step"] == "blocked_freeze"]
        self.assertEqual(len(got), 1, rows)
        self.assertTrue(got[0].get("returned"))

    def test_a_release_head_back_in_build_owes_no_freeze(self):
        rows = self.rows({"branch": "release/11y", "branch_target": "main"})
        self.assertNotIn("blocked_freeze", self.steps(rows))
        # son silence reste mesuré, sans escalade P1 propre au gel
        self.assertIn("build_quiet", self.steps(rows))

    def test_a_blocking_review_on_a_release_owes_no_freeze(self):
        fix = {"name": "rel", "blocked_ts": time.time() - 4 * H,
               "blocked_by": "r", "sha": "a" * 40}
        rows = self.rows({"branch": "release/11y", "branch_target": "main"},
                         fixes=[fix])
        self.assertNotIn("blocked_freeze", self.steps(rows))

    def test_release_target_and_type_are_release_too(self):
        self.assertTrue(lot_class.release_vehicle({"branch_target": "release/1.10"}))
        self.assertTrue(lot_class.release_vehicle({"type": "release"}))
        self.assertTrue(lot_class.release_vehicle({"branch": "origin/release/11y"}))
        self.assertEqual(lot_class.release_vehicle({"branch": "lot/release-notes"}), "")
        self.assertEqual(lot_class.release_vehicle({}), "")

    def test_the_gate_class_is_unchanged(self):
        # la classe de la porte ne lit toujours que la cible et le type
        self.assertEqual(lot_class.release_reason(
            {"branch": "release/11y", "branch_target": "main"}), "")


if __name__ == "__main__":
    unittest.main()
