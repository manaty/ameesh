# SPDX-License-Identifier: AGPL-3.0-only
"""#2200 : un agent en pause opérateur ne se lit pas comme disponible.

Trois lectures le présentaient comme réveillable : `ameesh show` (texte)
omettait la pause, `ameesh projects` la perdait dans sa requête (état
« idle », raison nulle), et la lecture partagée des alertes la perdait aussi
si bien qu'`idle_capacity` le proposait. La pause est portée par la lecture
partagée (`operations.listing`, `projects.board`), projetée une seule fois
(`progress.operator_pause_of`) et jugée par la disponibilité partagée
(`registry.wake_status`), que l'attribution utilise déjà.

Les témoins portent sur des états simulés/réels de la base de test ; aucun
agent réel n'est pausé, repris ni reconfiguré.
"""
from __future__ import annotations

import time
import unittest

from ameesh import agent_pause, progress, projects, registry, storage, work
from ameesh import sous_utilisation

from .support import PgTestCase

REASON = "réduction charge PC"


class PausedAvailabilityTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "dev-paused", chantier="ameesh", harness="deepseek",
                        host=self.cfg.host, cwd=self.tmp)
        # le bail existe avant la pause (claim refuse déjà un agent pausé) ;
        # la pause ne le retire pas : l'exécuteur finit le tour, n'en ouvre
        # plus, et la ligne garde `lease_live`.
        self.assertIsNotNone(registry.claim(self.db, "dev-paused", "runner-test", 3600))
        self.pause(True)

    def pause(self, paused, reason=REASON):
        return agent_pause.change(self.db, "dev-paused", paused=paused, reason=reason,
                                  actor="human:smichea", principal="human:smichea")

    def listing_row(self):
        return next(r for r in storage.of(self.db).operations.listing()
                    if r["name"] == "dev-paused")

    def board_row(self):
        board = storage.of(self.db).projects.board(max_lots=10)
        return next(r for r in board["agents"] if r["name"] == "dev-paused")

    def test_shared_listing_carries_the_operator_pause(self):
        row = self.listing_row()
        self.assertTrue(row["operator_paused"])
        self.assertEqual(row["operator_pause_reason"], REASON)
        self.assertEqual(row["operator_pause_by"], "human:smichea")
        self.assertIsNotNone(row["operator_pause_ts"])
        # la lecture partagée (alertes, prise automatique, handoff) en dérive
        # l'état : plus jamais « idle ».
        self.assertEqual(progress.agent_state(row), "paused")
        self.assertEqual(progress.operator_pause_of(row)["reason"], REASON)

    def test_idle_capacity_does_not_suggest_a_paused_agent(self):
        now = time.time()
        self.assertEqual(
            sous_utilisation.idle_agents([self.listing_row()], now, idle_s=0), [])
        # contre-épreuve : sans la pause, le même agent est bien proposé —
        # le correctif n'est pas un veto définitif.
        self.pause(False)
        self.assertEqual(
            [r["name"] for r in sous_utilisation.idle_agents(
                [self.listing_row()], now, idle_s=0)],
            ["dev-paused"])

    def test_plan_underused_does_not_offer_paused_agents_as_capacity(self):
        # #2200 (auditeur 17/10 08:27) : `plan_underused` nommait les agents
        # « idle » d'un harnais sous-consommé — dont quatre en pause opérateur.
        # Les chiffres de sous-consommation restent dus ; l'agent pausé n'est
        # pas une capacité disponible et ne doit pas être proposé.
        now = time.time()
        storage.of(self.db).operations.record_gauges([{
            "harness": "deepseek", "key": "deepseek-10080min", "used": 0.20,
            "resets_at": now + 4 * 86400, "window_s": 7 * 86400,
            "account": "primaire"}])
        alertes = sous_utilisation.plan_underused(
            self.db, [self.listing_row()], now)
        self.assertEqual(len(alertes), 1)
        self.assertEqual(alertes[0]["idle_agents"], [])
        self.assertNotIn("idle deepseek agents", alertes[0]["detail"])
        # contre-épreuve : pause levée, l'agent redevient la capacité nommée.
        self.pause(False)
        alertes = sous_utilisation.plan_underused(
            self.db, [self.listing_row()], now)
        self.assertEqual(alertes[0]["idle_agents"], ["dev-paused"])
        self.assertIn("idle deepseek agents: dev-paused", alertes[0]["detail"])

    def test_projects_view_names_the_pause_instead_of_idle(self):
        row = self.board_row()
        self.assertTrue(row["operator_paused"])
        self.assertEqual(row["operator_pause_reason"], REASON)
        board = storage.of(self.db).projects.board(max_lots=10)
        view = projects.build(board, now=time.time(), paid_harnesses=())
        agent = next(a for p in view["projects"] for a in p["agents"]
                     if a["name"] == "dev-paused")
        self.assertEqual(agent["state"], "paused")
        self.assertIn("operator pause", agent["reason"])
        self.assertIn("human:smichea", agent["reason"])
        self.assertIn(REASON, agent["reason"])
        self.assertEqual(agent["operator_pause"]["reason"], REASON)
        self.assertTrue(agent["operator_pause"]["by"] == "human:smichea")

    def test_shared_availability_refuses_a_paused_agent(self):
        ok, why = registry.wakeable(self.db, self.cfg, "dev-paused")
        self.assertFalse(ok)
        self.assertIn("operator pause", why)
        self.assertIn("human:smichea", why)
        with self.assertRaisesRegex(work.WorkError, "operator pause"):
            work.check_assignee(self.db, "dev-paused", cfg=self.cfg)
        # la sortie est nommée : lever la pause, ou confier le lot ailleurs.
        with self.assertRaisesRegex(work.WorkError, "ameesh resume dev-paused --operator"):
            work.check_assignee(self.db, "dev-paused", cfg=self.cfg)
        # contre-épreuve : la pause levée, la disponibilité revient.
        self.pause(False)
        self.assertEqual(registry.wakeable(self.db, self.cfg, "dev-paused"), (True, ""))

    def test_show_text_says_the_pause_not_just_idle(self):
        out = self.mesh("show", "dev-paused")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("op-paused", out.stdout)
        self.assertIn("operator pause since", out.stdout)
        self.assertIn("human:smichea", out.stdout)
        self.assertIn(REASON, out.stdout)
        self.assertIn("no new turn starts", out.stdout)
        # le JSON portait déjà la pause : les deux lectures se répondent.
        data = self.mesh("show", "dev-paused", "--json")
        self.assertEqual(data.returncode, 0, data.stderr)
        import json
        row = json.loads(data.stdout)
        self.assertTrue(row["operator_paused"])
        self.assertEqual(row["operator_pause_reason"], REASON)


if __name__ == "__main__":
    unittest.main()
