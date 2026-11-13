# SPDX-License-Identifier: AGPL-3.0-only
"""#7090 : une correction due sous une fenêtre d'observation n'est pas un goulot.

Constat de l'auditeur du 12/11 12:15Z : #7034 porte un hold de pilote
« hold: fenêtre : … » daté du 13/11 09:00Z, et le B de son gel 42f1c4474aad
restait compté en `slow_waits` (`blocking_review`, 5 h 18) ; mesh-design,
débiteur de la correction, figurait dans `idle_agents_with_work`, et
`--check` sortait 2 sur une attente voulue et datée (#4235).

Témoin : la correction due d'un lot sous fenêtre non échue sort des
`slow_waits` et des agents au repos avec du travail dû ; le lot est dans
`window_waits`. À l'échéance, elle redevient une attente `blocking_review`.
"""
from __future__ import annotations

import unittest

from .test_3437_debit_dependance import H, T, frozen
from .test_3498_hold_externe import event, item, run

WINDOW = ("hold: fenêtre : relire le témoin après le déploiement "
          "[réveil programmé: %s]")
#: la correction due de `due_fixes` (forme de `_owed_read`)
FIX = {"lot": 7034, "name": "mesh-design", "sha": "42f1c4474aad" + "0" * 28,
       "blocked_ts": T - 5 * H, "blocked_by": "relecteur", "title": "lot 7034",
       "state": "qa", "app": "ameesh"}
AGENTS = [{"name": "mesh-design", "status": "idle", "harness": "claude"}]


def report(due):
    return run(items=[item(7034, assignee="mesh-design")],
               milestones=[frozen(7034, 1, T - 6 * H, sha=FIX["sha"])],
               events=[event(7034, 1, T - 4 * H, WINDOW % due,
                             actor="mesh-design")],
               agents=AGENTS, fixes=[FIX])


class CorrectionSousFenetreTest(unittest.TestCase):
    def test_fenetre_non_echue_range_la_correction_a_part(self):
        out = report("2026-10-24 05:20 UTC")              # T + 2 h
        self.assertNotIn("blocking_review",
                         [row["kind"] for row in out["slow_waits"]])
        self.assertEqual(out["idle_agents_with_work"], [])
        self.assertEqual([row["lot"] for row in out["window_waits"]], [7034])
        # La correction reste listée comme avis bloquant (la dette est réelle),
        # marquée sous fenêtre : pas de retour au backlog.
        blocked = out["blocked_reviews"]
        self.assertEqual([row["lot"] for row in blocked], [7034])
        self.assertTrue(blocked[0]["window"])

    def test_echeance_echue_la_correction_redevient_due(self):
        out = report("2026-10-24 02:20 UTC")              # T - 1 h
        self.assertEqual(out["window_waits"], [])
        rows = [row for row in out["slow_waits"]
                if row["kind"] == "blocking_review"]
        self.assertEqual([(row["lot"], row["who"]) for row in rows],
                         [(7034, "mesh-design")])
        self.assertEqual([row["agent"] for row in out["idle_agents_with_work"]],
                         ["mesh-design"])
        self.assertEqual(out["idle_agents_with_work"][0]["fixes"], [7034])

    def test_sans_fenetre_rien_ne_change(self):
        out = run(items=[item(7034, assignee="mesh-design")],
                  milestones=[frozen(7034, 1, T - 6 * H, sha=FIX["sha"])],
                  agents=AGENTS, fixes=[FIX])
        self.assertIn("blocking_review",
                      [row["kind"] for row in out["slow_waits"]])
        self.assertEqual([row["agent"] for row in out["idle_agents_with_work"]],
                         ["mesh-design"])


if __name__ == "__main__":
    unittest.main()
