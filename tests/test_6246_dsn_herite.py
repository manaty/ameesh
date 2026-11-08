# SPDX-License-Identifier: AGPL-3.0-only
"""#6246 : un sous-processus né d'un test n'atteint que la base du banc.

`tests.support` pose `AMEESH_DSN` au niveau du processus ; un enfant lancé
sans `env=` explicite (le défaut de `subprocess.run`, la forme qu'avait prise
test_4217 à travers `paquet-auto.compose_packet`) hérite de cette base de test,
jamais du DSN de la session ni du ~/.pgpass réels. Le témoin lance le vrai
binaire `bin/ameesh work note` sans `env=` : la note n'atterrit que dans le
schéma du test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from ameesh import config as config_mod
from ameesh import work

from . import support
from .support import PgTestCase

REPO = support.REPO


class DsnHeriteTest(PgTestCase):
    """Le DSN du banc est dans le processus, pas seulement dans `child_env`."""

    def test_dsn_de_test_pose_au_chargement(self):
        self.assertEqual(os.environ.get("AMEESH_DSN"), support.TEST_DSN)

    def test_un_banc_qui_pose_son_dsn_le_garde(self):
        # `child_env` part de l'environnement du processus : le banc qui pose
        # son DSN garde le sien, la valeur héritée ne l'écrase pas.
        self.assertEqual(
            support.child_env(AMEESH_DSN="postgresql://banc@127.0.0.1:1/banc")
            ["AMEESH_DSN"],
            "postgresql://banc@127.0.0.1:1/banc")

    def test_enfant_sans_env_explicite_herite_du_dsn_de_test(self):
        """Sans `env=`, l'enfant lit `AMEESH_DSN` du processus de la suite."""
        code = (
            "import json, os\n"
            "from ameesh import config\n"
            "print(json.dumps({'env': os.environ.get('AMEESH_DSN'),\n"
            "                  'cfg': config.load().dsn}))\n")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        seen = json.loads(proc.stdout)
        self.assertEqual(seen["env"], support.TEST_DSN)
        self.assertEqual(seen["cfg"], support.TEST_DSN)

    def _bench_environ(self) -> dict:
        """L'environnement du banc sauf le DSN : il doit venir de l'héritage."""
        env = self.env()
        for name in ("AMEESH_DSN", "AGENT_MESH_DSN"):
            env.pop(name, None)
        # bin/ameesh choisit son interpréteur : celui du banc, pas /usr/bin/python3
        env["AMEESH_PYTHON"] = sys.executable
        return env

    def test_ameesh_work_note_sans_env_ecrit_dans_le_schema_de_test(self):
        """Le vrai binaire, sans `env=` : la note reste dans la base du banc."""
        lot = work.add(self.db, title="témoin #6246", app="ameesh")
        note = "témoin #6246 : note d'un enfant qui n'a pas reçu d'environnement"
        command = [os.path.join(REPO, "bin", "ameesh"),
                   "work", "note", str(lot["id"]), note]
        with mock.patch.dict(os.environ, self._bench_environ()):
            # hermeticity: allow — témoin : l'enfant DOIT hériter du DSN du banc
            proc = subprocess.run(command, capture_output=True, text=True,
                                  timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = self.db.query(
            "SELECT note FROM work_item_events WHERE work_item_id = %s",
            (lot["id"],))
        self.assertIn(note, [row["note"] for row in rows])


if __name__ == "__main__":
    unittest.main()
