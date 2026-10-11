# SPDX-License-Identifier: AGPL-3.0-only
"""L142 : la suite donne le même verdict sur le poste et en CI.

Un poste « pollué » (configuration ~/.config/ameesh/config.json avec canon et
humains, ancienne config agent-mesh, variables AMEESH_* d'une session d'agent)
ne doit rien changer à ce que voient les tests une fois `tests.support`
importé : ni dans le processus, ni dans les sous-processus (`child_env`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = r"""
import json, os
from tests import support
from ameesh import config as config_mod
cfg = config_mod.load()
child = config_mod.load(env=support.child_env())
print(json.dumps({
    "env": sorted(n for n in os.environ
                  if n.startswith(("AMEESH_", "AGENT_MESH_", "AGENT_MAIL_"))),
    "child_env": sorted(n for n in support.child_env()
                        if n.startswith(("AMEESH_", "AGENT_MESH_", "AGENT_MAIL_"))),
    "humans": [cfg.humans, child.humans],
    "host": [cfg.host, child.host],
    "runner_id": [cfg.runner_id, child.runner_id],
    "schema": [cfg.schema, child.schema],
    "state_dir": [cfg.state_dir, child.state_dir],
    "dsn": cfg.dsn,
}))
"""


class PosteIsoleTest(unittest.TestCase):
    """Sans base : la preuve porte sur l'environnement, pas sur Postgres."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="ameesh-test-l142-home-")
        for sub, name in (("ameesh", "config.json"), ("agent-mesh", "config.json")):
            directory = os.path.join(self.home, ".config", sub)
            os.makedirs(directory)
            with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
                json.dump({"humans": ["proprietaire-du-poste"], "host": "poste-reel",
                           "schema": "schema_reel"}, fh)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)

    def probe(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("AMEESH_", "AGENT_MESH_", "AGENT_MAIL_"))}
        env.update(HOME=self.home, PYTHONPATH=os.pathsep.join(
            (REPO, os.path.join(REPO, "src"))))
        env.update(extra)
        proc = subprocess.run([sys.executable, "-c", PROBE], capture_output=True,
                              text=True, timeout=60, env=env, cwd=REPO)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_poste_pollue_et_poste_propre_meme_vue(self) -> None:
        pollue = self.probe(
            AMEESH_HUMANS="proprietaire-du-poste", AMEESH_HOST="poste-reel",
            AMEESH_RUNNER_ID="runner-reel", AGENT_MESH_RUNNER_ID="runner-reel",
            AMEESH_SCHEMA="schema_reel", AMEESH_STATE="/etat/reel",
            AMEESH_DSN="postgresql://prod@db/prod", AGENT_MAIL_NAME="agent-reel",
            AMEESH_CANON="/canon/reel", AMEESH_REQUIRE_RESPONSIBLE="1",
            AMEESH_CONFIG=os.path.join(self.home, ".config", "ameesh", "config.json"),
            AMEESH_TEST_DSN="postgresql://banc@127.0.0.1:1/banc",
        )
        propre = self.probe(AMEESH_TEST_DSN="postgresql://banc@127.0.0.1:1/banc")
        self.assertEqual(pollue, propre)
        # ni la config du poste ni la session de l'agent ne transparaissent
        self.assertEqual(pollue["humans"], ["", ""])
        self.assertNotIn("poste-reel", pollue["host"])
        self.assertEqual(pollue["runner_id"][0], propre["runner_id"][0])
        self.assertNotIn("schema_reel", pollue["schema"])
        self.assertNotEqual(pollue["dsn"], "postgresql://prod@db/prod")
        for name in ("AMEESH_RUNNER_ID", "AGENT_MAIL_NAME", "AMEESH_CANON",
                     "AMEESH_REQUIRE_RESPONSIBLE", "AMEESH_HUMANS", "AMEESH_STATE"):
            self.assertNotIn(name, pollue["env"])
            self.assertNotIn(name, pollue["child_env"])
        # les réglages du banc, eux, sont conservés
        self.assertIn("AMEESH_TEST_DSN", pollue["env"])


if __name__ == "__main__":
    unittest.main()
