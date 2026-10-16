# SPDX-License-Identifier: AGPL-3.0-only
"""#1277 : un sujet de commit « #<lot> … » ne survit pas à un rebasage qui
ouvre l'éditeur — git prend cette ligne pour un commentaire et la retire, le
corps devient le sujet. `scripts/rebase-lot.sh` passe core.commentChar=';' à
chaque invocation de git rebase (--continue compris) et contrôle les sujets
après coup. Banc hors ligne : dépôts git jetables, aucun réseau, aucune base."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANCEUR = os.path.join(ROOT, "scripts", "rebase-lot.sh")
#: config git globale et système neutralisées : le défaut constaté est celui de
#: git, pas celui du poste (un core.commentChar global masquerait le défaut).
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _git(depot: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", depot, *args], capture_output=True, text=True,
                          env=GIT_ENV)
    if check and proc.returncode:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr or proc.stdout))
    return proc


class RebaseSujetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-1277-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.depot = os.path.join(self.base, "depot")

    def _ecrire(self, contenu: str) -> None:
        with open(os.path.join(self.depot, "f.txt"), "w") as fh:
            fh.write(contenu)

    def _depot(self) -> None:
        """base, un commit de lot « #900 … » en conflit, et main qui avance."""
        os.makedirs(self.depot)
        _git(self.depot, "init", "-q", "-b", "main")
        _git(self.depot, "config", "user.name", "t")
        _git(self.depot, "config", "user.email", "t@t")
        self._ecrire("l1\nl2\nl3\n")
        _git(self.depot, "add", "f.txt")
        _git(self.depot, "commit", "-qm", "base")
        _git(self.depot, "checkout", "-qb", "lot")
        self._ecrire("l1\nlot\nl3\n")
        _git(self.depot, "commit", "-qam", "#900 sujet du lot", "-m", "corps du lot")
        _git(self.depot, "checkout", "-q", "main")
        self._ecrire("l1\nmain\nl3\n")
        _git(self.depot, "commit", "-qam", "main avance")
        _git(self.depot, "checkout", "-q", "lot")

    def _lancer(self, *commande: str, editeur: str = "true") -> subprocess.CompletedProcess:
        env = dict(GIT_ENV, GIT_EDITOR=editeur)
        return subprocess.run(list(commande), cwd=self.depot, capture_output=True,
                              text=True, env=env)

    def _conflit(self, *commande: str) -> None:
        """Démarre le rebasage : il s'arrête sur le conflit, puis est résolu."""
        proc = self._lancer(*commande)
        self.assertNotEqual(proc.returncode, 0, "le rebasage devait s'arrêter sur le conflit")
        self._ecrire("l1\nresolu\nl3\n")
        _git(self.depot, "add", "f.txt")

    def _sujet(self) -> str:
        return _git(self.depot, "log", "--format=%s", "-1").stdout.strip()

    def test_git_seul_perd_le_sujet(self) -> None:
        """Témoin du défaut : le nettoyage par défaut retire la ligne « #… »."""
        self._depot()
        self._conflit("git", "rebase", "main")
        proc = self._lancer("git", "rebase", "--continue")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "corps du lot")

    def test_le_lanceur_conserve_le_sujet(self) -> None:
        """La garde : core.commentChar=';' sur l'invocation qui ouvre l'éditeur."""
        self._depot()
        self._conflit("bash", LANCEUR, "main")
        proc = self._lancer("bash", LANCEUR, "--continue")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._sujet(), "#900 sujet du lot")

    def test_le_lanceur_signale_un_sujet_perdu(self) -> None:
        """Le contrôle : un sujet retiré par l'éditeur est signalé (sortie 4).

        #1516 : le témoin utilisait `--skip`, un abandon volontaire, que le
        lanceur ne signale plus comme une perte ; l'éditeur qui mange la ligne
        « #… » reproduit le défaut lui-même."""
        self._depot()
        self._conflit("bash", LANCEUR, "main")
        proc = self._lancer("bash", LANCEUR, "--continue", editeur="sed -i 1d")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("#900 sujet du lot", proc.stderr)
        self.assertEqual(self._sujet(), "corps du lot")


if __name__ == "__main__":
    unittest.main()
