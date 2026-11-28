# SPDX-License-Identifier: AGPL-3.0-only
"""#8441 : `scripts/rebase-lot.sh` refuse (sortie 4) un historique rejoué dont
un message garde le gabarit de l'éditeur — `commit.cleanup=whitespace` sans
éditeur ouvert le conserve en entier (deux commits de #8395 sur main). Banc
hors ligne : dépôts git jetables, aucun réseau, aucune base."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANCEUR = os.path.join(ROOT, "scripts", "rebase-lot.sh")
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_EDITOR="true")
GABARIT = """#8441 : sujet du lot

# Please enter the commit message for your changes. Lines starting
# with '#' will be kept; you may remove them yourself if you want to.
#
# interactive rebase in progress; onto 4d256af32
"""


class GabaritEditeurTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-8441-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.depot = os.path.join(self.base, "depot")
        os.makedirs(self.depot)
        self._git("init", "-q", "-b", "main")
        self._commit("f.txt", "base\n", "base")

    def _git(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", self.depot, *args], capture_output=True,
                              text=True, env=env or GIT_ENV)

    def _commit(self, nom: str, contenu: str, message: str) -> None:
        with open(os.path.join(self.depot, nom), "w") as fh:
            fh.write(contenu)
        self._git("add", nom)
        proc = subprocess.run(
            ["git", "-C", self.depot, "commit", "-q", "--cleanup=verbatim", "-F", "-"],
            input=message, capture_output=True, text=True, env=GIT_ENV)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _lancer(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", LANCEUR, *args], cwd=self.depot,
                              capture_output=True, text=True, env=env or GIT_ENV)

    def test_gabarit_committe_signale(self) -> None:
        self._git("checkout", "-qb", "lot/8441-essai")
        self._commit("lot.txt", "lot\n", GABARIT)
        self._git("checkout", "-q", "main")
        self._commit("main.txt", "main\n", "main avance\n")
        self._git("checkout", "-q", "lot/8441-essai")
        proc = self._lancer("main")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("gabarit de l'éditeur", proc.stderr)
        self.assertIn("#8441 : sujet du lot", proc.stderr)

    def test_continue_sous_cleanup_whitespace_signale(self) -> None:
        """La cause vue sur #8395 : conflit, puis --continue avec
        commit.cleanup=whitespace et un éditeur qui ne s'ouvre pas."""
        self._git("checkout", "-qb", "lot/8441-essai")
        self._commit("f.txt", "lot\n", "#8441 : sujet du lot\n")
        self._git("checkout", "-q", "main")
        self._commit("f.txt", "main\n", "main avance\n")
        self._git("checkout", "-q", "lot/8441-essai")
        self.assertNotEqual(self._lancer("main").returncode, 0)
        with open(os.path.join(self.depot, "f.txt"), "w") as fh:
            fh.write("resolu\n")
        self._git("add", "f.txt")
        env = dict(GIT_ENV, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="commit.cleanup",
                   GIT_CONFIG_VALUE_0="whitespace")
        proc = self._lancer("--continue", env=env)
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("gabarit de l'éditeur", proc.stderr)

    def test_message_propre_passe(self) -> None:
        self._git("checkout", "-qb", "lot/8441-essai")
        self._commit("lot.txt", "lot\n", "#8441 : sujet du lot\n\ncorps\n")
        self._git("checkout", "-q", "main")
        self._commit("main.txt", "main\n", "main avance\n")
        self._git("checkout", "-q", "lot/8441-essai")
        proc = self._lancer("main")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("rebase-lot :", proc.stderr)


if __name__ == "__main__":
    unittest.main()
