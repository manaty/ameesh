# SPDX-License-Identifier: AGPL-3.0-only
"""#1277 : un sujet de commit « #<lot> … » ne survit pas à un rebasage qui
ouvre l'éditeur — git prend cette ligne pour un commentaire et la retire, le
corps devient le sujet. `scripts/rebase-lot.sh` passe core.commentChar=';' à
chaque invocation de git rebase (--continue compris) et contrôle les sujets
après coup. #1669 : le même lanceur contrôle l'historique rejoué — chaque
commit d'une branche `lot/<id>…` garde un sujet « #<id> … » (bisect rattachable
au lot) et aucun commit rejoué n'introduit de marqueur de conflit. Banc hors
ligne : dépôts git jetables, aucun réseau, aucune base."""
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

    def _lancer(self, *commande: str) -> subprocess.CompletedProcess:
        env = dict(GIT_ENV, GIT_EDITOR="true")
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
        """Le contrôle : un commit « #… » sauté est signalé (sortie 4)."""
        self._depot()
        self._conflit("bash", LANCEUR, "main")
        proc = self._lancer("bash", LANCEUR, "--skip")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("#900 sujet du lot", proc.stderr)


class HistoriqueBrancheTest(unittest.TestCase):
    """#1669 : après un rebasage réussi, le lanceur contrôle la plage rejouée —
    préfixe « #<lot> » sur chaque commit d'une branche `lot/<id>…`, et aucun
    marqueur de conflit committé (un bisect reste exploitable)."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-1669-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.depot = os.path.join(self.base, "depot")
        os.makedirs(self.depot)
        _git(self.depot, "init", "-q", "-b", "main")
        _git(self.depot, "config", "user.name", "t")
        _git(self.depot, "config", "user.email", "t@t")
        self._commit("base.txt", "base\n", "base")

    def _ecrire(self, nom: str, contenu: str) -> None:
        with open(os.path.join(self.depot, nom), "w") as fh:
            fh.write(contenu)

    def _commit(self, nom: str, contenu: str, sujet: str) -> None:
        self._ecrire(nom, contenu)
        _git(self.depot, "add", nom)
        _git(self.depot, "commit", "-qm", sujet)

    def _avance_main(self) -> None:
        _git(self.depot, "checkout", "-q", "main")
        self._commit("main.txt", "main\n", "main avance")

    def _lancer(self, *commande: str) -> subprocess.CompletedProcess:
        env = dict(GIT_ENV, GIT_EDITOR="true")
        return subprocess.run(list(commande), cwd=self.depot, capture_output=True,
                              text=True, env=env)

    def test_sujet_sans_prefixe_du_lot_signale(self) -> None:
        """Un commit rejoué de `lot/<id>…` sans sujet « #<id> … » : sortie 4."""
        _git(self.depot, "checkout", "-qb", "lot/1476-essai")
        self._commit("lot.txt", "lot\n", "sujet sans prefixe")
        self._avance_main()
        _git(self.depot, "checkout", "-q", "lot/1476-essai")
        proc = self._lancer("bash", LANCEUR, "main")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("sans le préfixe « #1476 »", proc.stderr)
        self.assertIn("sujet sans prefixe", proc.stderr)

    def test_prefixe_du_lot_conserve_passe(self) -> None:
        """Le même rebasage avec un sujet « #1476 … » : aucun constat."""
        _git(self.depot, "checkout", "-qb", "lot/1476-essai")
        self._commit("lot.txt", "lot\n", "#1476 : sujet du lot")
        self._avance_main()
        _git(self.depot, "checkout", "-q", "lot/1476-essai")
        proc = self._lancer("bash", LANCEUR, "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("rebase-lot :", proc.stderr)

    def test_marqueur_de_conflit_au_commit_intermediaire_signale(self) -> None:
        """Un marqueur committé dans un commit rejoué (pas le dernier) : 4."""
        _git(self.depot, "checkout", "-qb", "lot/1476-essai")
        self._commit("lot.txt", "a\n<<<<<<< HEAD\nb\n>>>>>>> autre\n",
                     "#1476 : resolution ratee")
        self._commit("suite.txt", "suite\n", "#1476 : suite du lot")
        self._avance_main()
        _git(self.depot, "checkout", "-q", "lot/1476-essai")
        proc = self._lancer("bash", LANCEUR, "main")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn("marqueurs de conflit committés", proc.stderr)
        self.assertIn("resolution ratee", _git(self.depot, "log", "--format=%s",
                                               "-1", "HEAD~1").stdout)

    def test_onto_change_la_borne_de_la_plage(self) -> None:
        """`--onto main base` : la plage part de la destination (main), pas de
        l'ancien upstream — le commit « main avance » n'est pas contrôlé."""
        base = _git(self.depot, "rev-parse", "HEAD").stdout.strip()
        _git(self.depot, "checkout", "-q", "main")
        self._commit("main.txt", "main\n", "main avance")
        _git(self.depot, "checkout", "-q", "-b", "lot/1476-essai", base)
        self._commit("lot.txt", "lot\n", "#1476 : sujet du lot")
        proc = self._lancer("bash", LANCEUR, "--onto", "main", base, "lot/1476-essai")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_base_et_branche_hors_lot_non_controlees(self) -> None:
        """Un marqueur committé sur la base, et une branche qui n'est pas
        `lot/<id>…` : la plage rejouée seule est contrôlée (aucun faux positif)."""
        _git(self.depot, "checkout", "-q", "main")
        self._commit("main.txt", "<<<<<<< HEAD\n", "main avance")
        _git(self.depot, "checkout", "-qb", "agent/truc")
        self._commit("a.txt", "a\n", "sujet libre sans prefixe")
        proc = self._lancer("bash", LANCEUR, "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
