# SPDX-License-Identifier: AGPL-3.0-only
"""L166 : une session fille (sous-agent, fourche) n'est jamais retenue comme
session de l'agent.

Constat du 2026-10-11 : deepseek5 arrêté après 5 échecs rapides, dsh refusant
de reprendre `bc9d3965…` (« is a subagent or forked session and cannot be
driven directly »). dsh déclenche les hooks de ses agents enfants avec LEUR
session et sans `agent_id` ; le hook, sous bail, l'inscrivait au registre, et
l'exécuteur la reprenait au tour suivant. Postgres réel ; faux harnais `dsh`.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ameesh import adapters, registry
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

FILLE = "bc9d3965-d6c7-4e90-a482-96296b7beaa6"


class LectureDuFluxTest(unittest.TestCase):
    def test_session_fille_du_flux_dsh_ignoree(self):
        lecteur = adapters.DshStream()
        for fille in ({"origin": "subagent"}, {"parentSession": "s-mere"},
                      {"parentSessionId": "s-mere"}):
            with self.subTest(fille=fille):
                lu = lecteur.parse(json.dumps(dict({"type": "session", "sessionId": FILLE},
                                                   **fille)))
                self.assertNotIn("session", lu)
        self.assertEqual(lecteur.parse(json.dumps(
            {"type": "session", "sessionId": "s-mere", "cwd": "/w"}))["session"], "s-mere")

    def test_session_d_un_sous_agent_claude_ignoree(self):
        lecteur = adapters.ClaudeStream()
        for kind in ({"type": "system", "subtype": "init"},
                     {"type": "result", "result": "fini"}):
            with self.subTest(kind=kind["type"]):
                sous_agent = dict(kind, session_id=FILLE, parent_tool_use_id="toolu_1")
                self.assertNotIn("session", lecteur.parse(json.dumps(sous_agent)))
                principal = dict(kind, session_id="s-mere", parent_tool_use_id=None)
                self.assertEqual(lecteur.parse(json.dumps(principal))["session"], "s-mere")

    def test_refus_reconnu(self):
        self.assertTrue(adapters.child_session_refused(
            'session "%s" is a subagent or forked session and cannot be driven directly'
            % FILLE))
        for autre in (None, "", "le harnais a rendu le code 1",
                      'session "x" does not exist; omit --session-id'):
            self.assertFalse(adapters.child_session_refused(autre))


class _Base(PgTestCase):
    def _worker(self, name: str):
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "deepseek", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return runner, worker

    def _env(self):
        return mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_TEST_DSH_CHILD_SESSIONS": FILLE}, clear=False)

    def _tour(self, worker) -> bool:
        return worker.run_turn({"kind": "prompt", "prompt": "travail", "ids": []})

    def _reprises(self) -> list[str | None]:
        out = []
        for tour in self.turns():
            argv = tour["argv"]
            out.append(argv[argv.index("--session-id") + 1]
                       if "--session-id" in argv else None)
        return out


class HookSousBailTest(_Base):
    def test_le_hook_d_un_enfant_n_inscrit_pas_sa_session(self):
        _runner, worker = self._worker("ds-hook")
        registry.set_session(self.db, "ds-hook", "s-mere")
        env = self.env(AGENT_MAIL_NAME="ds-hook", AMEESH_RUNNER_ID=worker.runner.runner_id,
                       AMEESH_LEASE_EPOCH=str(worker.epoch))
        for event in ("SessionStart", "PostToolUse", "Stop"):
            # hook d'un agent enfant de dsh : sa propre session, sans agent_id
            proc = self.cli("hook", "deepseek", env=env, stdin=json.dumps(
                {"hook_event_name": event, "session_id": FILLE, "cwd": self.tmp}))
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(registry.get(self.db, "ds-hook")["session_id"], "s-mere")


class RepriseApresRefusTest(_Base):
    def test_retour_a_la_derniere_session_principale(self):
        _runner, worker = self._worker("ds-mere")
        with self._env():
            self.assertTrue(self._tour(worker))  # session ouverte par le flux
            # la session fille a été inscrite (avant L166, par le hook)
            registry.set_session(self.db, "ds-mere", FILLE)
            worker.agent = registry.get(self.db, "ds-mere")
            self.assertFalse(self._tour(worker))
            self.assertFalse(worker.fast_failure)  # pas compté comme échec rapide
            self.assertEqual(registry.get(self.db, "ds-mere")["session_id"], "dsh-session-1")
            self.assertTrue(self._tour(worker))
        self.assertEqual(self._reprises(), [None, FILLE, "dsh-session-1"])

    def test_session_neuve_sans_session_principale_connue(self):
        _runner, worker = self._worker("ds-neuve")
        registry.set_session(self.db, "ds-neuve", FILLE)
        worker.agent = registry.get(self.db, "ds-neuve")
        with self._env():
            self.assertFalse(self._tour(worker))
            self.assertFalse(worker.fast_failure)
            self.assertIsNone(registry.get(self.db, "ds-neuve")["session_id"])
            self.assertEqual(worker.resume_origin, "ameesh")
            self.assertTrue(self._tour(worker))
        self.assertEqual(self._reprises(), [FILLE, None])
        self.assertIn("session fille %s écartée" % FILLE, " ".join(self.turns()[-1]["argv"]))
        self.assertEqual(registry.get(self.db, "ds-neuve")["session_id"], "dsh-session-1")


if __name__ == "__main__":
    unittest.main()
