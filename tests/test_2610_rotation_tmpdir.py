# SPDX-License-Identifier: AGPL-3.0-only
"""#2610 : une rotation de session ne vide pas le dossier temporaire de l'agent.

Le 19/10 à 03:24, la rotation de l'auditeur a vidé `$TMPDIR` entre la rotation
et la suite du tour, alors que le résumé de reprise désignait des fichiers de
travail en cours (`analyse-20261019/angle1-4.md`, deux clones). Une suite de
rotation continue le MÊME travail : son dossier temporaire est gardé. Le
vidage reste pour une vraie session neuve (témoins L219).
"""
from __future__ import annotations

import dataclasses
import os
import unittest
from unittest import mock

from ameesh import menage, registry
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase


class RotationTmpdirTest(PgTestCase):
    def _worker(self, name: str = "rot2610"):
        cfg = dataclasses.replace(self.cfg, session_min_turns=1,
                                  session_max_turn_seconds=0.0,
                                  session_max_tokens=0.0)
        runner = Runner(cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        return AgentWorker(runner, registry.get(self.db, name), lease)

    def test_une_rotation_garde_le_dossier_temporaire(self):
        worker = self._worker()
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log}
        try:
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "premier tour", "ids": []}))
                # le dossier que le tour vient d'ouvrir (même calcul que runner)
                tmp = menage.turn_env(
                    worker.cfg, worker.runner.housekeeping_policy(), worker.name,
                    {})["TMPDIR"]
                temoin = os.path.join(tmp, "analyse-en-cours.md")
                with open(temoin, "w", encoding="utf-8") as fh:
                    fh.write("travail en cours, cité par le résumé")
                with mock.patch.dict(os.environ,
                                     {"AMEESH_TEST_RESULT": "résumé de rotation"}):
                    self.assertTrue(worker.maybe_rotate())
                self.assertEqual(worker.resume_summary, "résumé de rotation")
                # la suite part sans session : c'est une continuation, pas une
                # vraie session neuve
                self.assertTrue(worker.run_turn(
                    {"kind": "prompt", "prompt": "suite du travail", "ids": []}))
                self.assertTrue(
                    os.path.exists(temoin),
                    "la suite de rotation a vidé le dossier temporaire de l'agent")
        finally:
            worker.watchdog_stop.set()


if __name__ == "__main__":
    unittest.main()
