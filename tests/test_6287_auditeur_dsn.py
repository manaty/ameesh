# SPDX-License-Identifier: AGPL-3.0-only
"""#6287: AUDITEUR_DSN reaches the auditor harness, and only it.

The executor's unit may carry the auditor role's own read-only connection
string. `child_environment` treats every `*_DSN` as a secret, so the harness
only receives it through the explicit grant made after account selection; an
unrelated agent's harness and an unapproved third-party child never do.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import registry
from ameesh.platform import child_environment as E
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

SRC = str(Path(__file__).resolve().parents[1] / "src")
DSN = ("host=10.77.0.1 port=5432 dbname=ameesh user=auditeur_lecture "
       "sslmode=require passfile=/run/private/auditeur.pgpass")


class AuditeurDsnGrantTest(unittest.TestCase):
    def test_the_auditor_harness_receives_its_read_only_dsn_and_no_other_agent(self):
        source = dict(AUDITEUR_DSN=DSN, AMEESH_DSN="postgresql://mesh@db/mesh",
                      GH_TOKEN="github", DEEPSEEK_API_KEY="provider")
        profile = SimpleNamespace(env="DEEPSEEK_API_KEY")
        for family in ("deepseek", "claude"):
            with self.subTest(family=family):
                auditor = E.harness(source, family, profile, auditor=True)
                self.assertEqual(E.for_child(["harness"], auditor)["AUDITEUR_DSN"], DSN)
                # The grant belongs to this one spawn, not to a mutable global list.
                self.assertEqual(E.for_child(["harness"], auditor.copy())["AUDITEUR_DSN"], DSN)
                other = E.harness(source, family, profile)
                self.assertNotIn("AUDITEUR_DSN", E.for_child(["harness"], other))
        # An ordinary child, and a tool that asks for database credentials by
        # name, never inherit the auditor's DSN.
        for argv in (["python3", "helper.py"], ["psql", "-c", "SELECT 1"],
                     ["ameesh", "work", "list"], ["agent-mail", "inbox"]):
            with self.subTest(argv=argv):
                self.assertNotIn("AUDITEUR_DSN", E.for_child(argv, source))

    def test_the_spawn_boundary_grants_it_only_to_the_auditor_harness(self):
        script = r'''
import json, os, subprocess, sys
from ameesh.platform import child_environment as E
E.install()
probe = "import json,os; print(json.dumps([k for k in os.environ if k == 'AUDITEUR_DSN']))"
auditor = json.loads(subprocess.check_output(
    [sys.executable, "-c", probe],
    env=E.harness(dict(os.environ), "deepseek", None, auditor=True), text=True))
other = json.loads(subprocess.check_output(
    [sys.executable, "-c", probe],
    env=E.harness(dict(os.environ), "deepseek", None), text=True))
# No explicit environment, no grant: a third-party child of the boundary.
third_party = json.loads(subprocess.check_output([sys.executable, "-c", probe], text=True))
print(json.dumps([auditor, other, third_party]))
'''
        env = E.for_child([sys.executable], os.environ)
        env.update(PYTHONPATH=SRC, AUDITEUR_DSN=DSN,
                   AMEESH_DSN="postgresql://mesh@db/mesh")
        output = subprocess.check_output(
            [sys.executable, "-c", script], env=E.grant(env, env), text=True)
        self.assertEqual(json.loads(output),
                         [["AUDITEUR_DSN"], [], []])


class AuditeurDsnTurnTest(PgTestCase):
    """The runner's real turn grants it to the auditor's harness, not others."""

    def _worker(self, name: str) -> AgentWorker:
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def test_a_turn_carries_the_dsn_only_for_the_auditor(self):
        auditor = self._worker("auditeur")
        other = self._worker("impl-dsn")
        # The production executor installs the spawn boundary at startup; the
        # test process does not, so install it for these turns and restore it
        # before any later test can inherit the patch.
        original = subprocess.Popen.__init__
        E.install()
        self.addCleanup(setattr, subprocess.Popen, "__init__", original)
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AUDITEUR_DSN": DSN}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(auditor.run_turn(
                {"kind": "prompt", "prompt": "audit", "ids": []}))
            self.assertTrue(other.run_turn(
                {"kind": "prompt", "prompt": "work", "ids": []}))
        turns = {turn["env"]["AGENT_MAIL_NAME"]: turn for turn in self.turns()}
        self.assertEqual(turns["auditeur"]["env"]["AUDITEUR_DSN"], DSN)
        self.assertIsNone(turns["impl-dsn"]["env"]["AUDITEUR_DSN"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
