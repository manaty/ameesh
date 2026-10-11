# SPDX-License-Identifier: AGPL-3.0-only
"""L217 : le plafond de contexte d'un tour ne compte que le fil principal.

Constat du 2026-10-11 : un tour de l'auditeur a été clos à 15,0 M jetons
relus, alors que son fil principal était à ~4 M. Ses quatre sous-agents en
avaient relu 11,7 M, et le tour a été clos sur un point sûr de sous-agent :
analyses perdues, rotation inutile. Le flux `stream-json` de Claude Code
publie les événements des sous-agents avec `parent_tool_use_id`. Leurs
jetons sont comptés à part, pour information. Postgres réel ; faux harnais
`claude` du banc.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ameesh import adapters, registry, storage
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class LectureDuFluxTest(unittest.TestCase):
    def _assistant(self, parent):
        return json.dumps({"type": "assistant", "parent_tool_use_id": parent, "message": {
            "id": "m1", "usage": {"input_tokens": 3, "cache_read_input_tokens": 7},
            "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]}})

    def _resultat(self, parent):
        return json.dumps({"type": "user", "parent_tool_use_id": parent, "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}})

    def test_sous_agent_ni_plafond_ni_point_sur(self):
        lecteur = adapters.ClaudeStream()
        appel = lecteur.parse(self._assistant("toolu_task"))
        self.assertNotIn("call_usage", appel)
        self.assertEqual(adapters.reread_tokens("claude", appel["subagent_call_usage"]), 10)
        self.assertNotIn("safe_point", lecteur.parse(self._resultat("toolu_task")))

    def test_fil_principal_inchange(self):
        lecteur = adapters.ClaudeStream()
        appel = lecteur.parse(self._assistant(None))
        self.assertEqual(adapters.reread_tokens("claude", appel["call_usage"]), 10)
        self.assertNotIn("subagent_call_usage", appel)
        self.assertTrue(lecteur.parse(self._resultat(None)).get("safe_point"))


class PlafondDuFilPrincipalTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")

    def _worker(self, name: str):
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        storage.of(self.db).operations.set_settings(name, {"context_max_tokens": "1000"})
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _tour(self, worker, appels) -> bool:
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_TEST_CLAUDE_CALLS": json.dumps(appels)}
        with mock.patch.dict(os.environ, env, clear=False):
            return worker.run_turn({"kind": "prompt", "prompt": "travail", "ids": []})

    def _flux(self, worker) -> str:
        with open(worker._path("events.jsonl"), encoding="utf-8") as fh:
            return fh.read()

    def test_sous_agents_au_dela_du_plafond_le_tour_va_au_bout(self):
        worker = self._worker("audit-sa")
        # le fil principal lance un sous-agent (outil-0) qui relit 2 400 jetons
        # en deux appels, plafond 1 000 ; le fil principal n'en relit que 150
        appels = [{"reread": 100, "tool": True},
                  {"reread": 1200, "tool": True, "parent": "outil-0"},
                  {"reread": 1200, "tool": True, "parent": "outil-0"},
                  {"reread": 50, "tool": True}]
        self.assertTrue(self._tour(worker, appels))
        self.assertIsNone(worker.last_turn_closed)
        self.assertIsNone(worker.rotation_forcee)
        self.assertEqual((worker.last_turn_main_reread, worker.last_turn_subagent_reread),
                         (150, 2400))
        self.assertIn('"tool_use_id": "outil-3"', self._flux(worker))

    def test_clos_au_point_sur_du_fil_principal_pas_d_un_sous_agent(self):
        worker = self._worker("audit-ps")
        appels = [{"reread": 1200},                            # plafond atteint
                  {"reread": 10, "tool": True, "parent": "outil-x"},  # sous-agent
                  {"reread": 10, "tool": True},                # point sûr principal
                  {"reread": 10, "tool": True, "sleep": 3}]    # jamais fini
        self.assertTrue(self._tour(worker, appels))
        self.assertTrue(worker.last_turn_closed.startswith("plafond de contexte"))
        flux = self._flux(worker)
        self.assertIn('"tool_use_id": "outil-1"', flux)  # le sous-agent n'a pas clos
        self.assertIn('"tool_use_id": "outil-2"', flux)  # clos ici
        self.assertNotIn('"tool_use_id": "outil-3"', flux)


if __name__ == "__main__":
    unittest.main()
