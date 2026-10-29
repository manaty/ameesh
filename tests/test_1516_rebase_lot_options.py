# SPDX-License-Identifier: AGPL-3.0-only
"""#1516 : `scripts/rebase-lot.sh` refuse des options valides de git rebase
(-k, --keep-empty, --no-keep-empty, --allow-empty-message) et signale comme
perdu un commit abandonné volontairement (--empty=drop, drop dans -i,
--skip, commit devenu vide). Le contrôle ne voit une perte que si le corps du
commit réapparaît en sujet — la trace du défaut de #1277. Banc hors ligne :
dépôts git jetables, aucun réseau, aucune base."""
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
#: message du contrôle de perte, et celui de l'abandon volontaire
PERDU = "un sujet a pu passer en corps"
ABANDON = "abandon volontaire"


def _git(depot: str, *args: str, check: bool = True,
         input: str | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", depot, *args], capture_output=True, text=True,
                          env=GIT_ENV, input=input)
    if check and proc.returncode:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr or proc.stdout))
    return proc


class RebaseLotOptionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-1516-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self._n = 0
        self.depot = ""

    # -- fixtures ---------------------------------------------------------
    def _initialiser(self) -> None:
        """Un dépôt neuf par fixture : les cas d'un test ne se marchent pas dessus."""
        self._n += 1
        self.depot = os.path.join(self.base, "depot-%d" % self._n)
        os.makedirs(self.depot)
        _git(self.depot, "init", "-q", "-b", "main")
        _git(self.depot, "config", "user.name", "t")
        _git(self.depot, "config", "user.email", "t@t")

    def _ecrire(self, nom: str, contenu: str) -> None:
        with open(os.path.join(self.depot, nom), "w") as fh:
            fh.write(contenu)

    def _commit(self, nom: str, contenu: str, sujet: str, corps: str | None = None) -> None:
        self._ecrire(nom, contenu)
        _git(self.depot, "add", nom)
        args = ["commit", "-qm", sujet]
        if corps is not None:
            args += ["-m", corps]
        _git(self.depot, *args)

    def _main_avance(self) -> None:
        """main avance sur un fichier que le lot ne touche pas : aucun conflit."""
        _git(self.depot, "checkout", "-q", "main")
        self._commit("m.txt", "m\n", "main avance")
        _git(self.depot, "checkout", "-q", "lot")

    def _main_longue(self, taille: int = 2 * 1024 * 1024) -> None:
        """main avance d'un sujet plus long que la capacité d'un tube : la
        sortie de `git log --format=%s` dépasse alors ce qu'un tube peut
        contenir, donc `printf` ne peut pas finir avant que `grep -q` ne sorte
        au premier appariement — la course #4724 devient déterministe."""
        _git(self.depot, "checkout", "-q", "main")
        _git(self.depot, "commit", "-q", "--allow-empty", "-F", "-",
             input="m" * taille)
        _git(self.depot, "checkout", "-q", "lot")

    def _lot_simple(self) -> None:
        """#1 à garder, #2 à abandonner, main avance ailleurs."""
        self._initialiser()
        self._commit("f.txt", "a\n", "base")
        _git(self.depot, "checkout", "-qb", "lot")
        self._commit("g.txt", "x\n", "#1 a garder", "corps 1")
        self._commit("h.txt", "y\n", "#2 a abandonner", "corps 2")
        self._main_avance()

    def _lot_vide(self) -> None:
        """Un commit vide « #1 vide » puis un commit réel."""
        self._initialiser()
        self._commit("f.txt", "a\n", "base")
        _git(self.depot, "checkout", "-qb", "lot")
        _git(self.depot, "commit", "-q", "--allow-empty", "-m", "#1 vide")
        self._commit("g.txt", "x\n", "#2 reel", "corps 2")
        self._main_avance()

    def _lot_en_conflit(self) -> None:
        """base, un commit de lot « #900 … » en conflit, et main qui avance."""
        self._initialiser()
        self._commit("f.txt", "l1\nl2\nl3\n", "base")
        _git(self.depot, "checkout", "-qb", "lot")
        self._commit("f.txt", "l1\nlot\nl3\n", "#900 sujet du lot", "corps du lot")
        _git(self.depot, "checkout", "-q", "main")
        self._commit("f.txt", "l1\nmain\nl3\n", "main avance")
        _git(self.depot, "checkout", "-q", "lot")

    # -- outils -----------------------------------------------------------
    def _lancer(self, *commande: str, env: dict | None = None,
                editeur: str = "true") -> subprocess.CompletedProcess:
        e = dict(GIT_ENV, GIT_EDITOR=editeur)
        if env:
            e.update(env)
        return subprocess.run(list(commande), cwd=self.depot, capture_output=True,
                              text=True, env=e)

    def _conflit(self, *commande: str, contenu: str = "l1\nresolu\nl3\n") -> None:
        """Démarre le rebasage : il s'arrête sur le conflit, puis est résolu."""
        proc = self._lancer(*commande)
        self.assertNotEqual(proc.returncode, 0, "le rebasage devait s'arrêter sur le conflit")
        self._ecrire("f.txt", contenu)
        _git(self.depot, "add", "f.txt")

    def _sujets(self) -> list[str]:
        return _git(self.depot, "log", "--format=%s").stdout.splitlines()

    # -- les options valides ne sont plus refusées -------------------------
    def test_options_valides_acceptees(self) -> None:
        """-k, --keep-empty, --no-keep-empty, --allow-empty-message passent."""
        for option in ("-k", "--keep-empty", "--no-keep-empty", "--allow-empty-message"):
            with self.subTest(option=option):
                self._lot_simple()
                proc = self._lancer("bash", LANCEUR, option, "main")
                self.assertNotIn("non reconnue", proc.stderr)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("#1 a garder", self._sujets())

    def test_keep_empty_garde_le_commit_vide(self) -> None:
        """-k / --keep-empty gardent le commit vide : rien n'est abandonné."""
        for option in ("-k", "--keep-empty"):
            with self.subTest(option=option):
                self._lot_vide()
                proc = self._lancer("bash", LANCEUR, option, "main")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("#1 vide", self._sujets())
                self.assertNotIn(ABANDON, proc.stderr)

    def test_sujet_present_en_tete_n_est_pas_absent_sur_un_long_historique(self) -> None:
        """#4724 : `printf | grep -q` sous pipefail — `grep -q` sort au premier
        appariement, `printf` prend SIGPIPE (141) et pipefail fait passer le
        pipeline pour faux : un sujet présent est classé absent. Avec un
        `finaux` plus long que la capacité d'un tube, « #2 reel » est en tête
        et la course devient déterministe."""
        self._lot_vide()
        self._main_longue()
        proc = self._lancer("bash", LANCEUR, "--keep-empty", "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("#2 reel", self._sujets())
        self.assertNotIn(ABANDON, proc.stderr)

    def test_no_keep_empty_abandonne_le_commit_vide_sans_perte(self) -> None:
        """--no-keep-empty abandonne le commit vide : pas une perte (sortie 0)."""
        self._lot_vide()
        proc = self._lancer("bash", LANCEUR, "--no-keep-empty", "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("#1 vide", self._sujets())
        self.assertIn(ABANDON, proc.stderr)
        self.assertNotIn(PERDU, proc.stderr)

    # -- les abandons volontaires ne sont pas des pertes -------------------
    def test_drop_interactif_n_est_pas_une_perte(self) -> None:
        """Un commit retiré de la liste interactive est un abandon, pas une perte."""
        self._lot_simple()
        proc = self._lancer("bash", LANCEUR, "-i", "main",
                            env={"GIT_SEQUENCE_EDITOR": "sed -i 2d"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(ABANDON, proc.stderr)
        self.assertIn("#2 a abandonner", proc.stderr)
        self.assertNotIn(PERDU, proc.stderr)
        self.assertIn("#1 a garder", self._sujets())
        self.assertNotIn("#2 a abandonner", self._sujets())

    def test_empty_drop_n_est_pas_une_perte(self) -> None:
        """Un commit devenu vide, abandonné par --empty=drop, n'est pas une perte."""
        self._lot_en_conflit()
        # la résolution prend le contenu de l'amont : le commit devient vide
        self._conflit("bash", LANCEUR, "--empty=drop", "main", contenu="l1\nmain\nl3\n")
        proc = self._lancer("bash", LANCEUR, "--continue")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(ABANDON, proc.stderr)
        self.assertIn("#900 sujet du lot", proc.stderr)
        self.assertNotIn(PERDU, proc.stderr)

    def test_skip_n_est_pas_une_perte(self) -> None:
        """Un commit sauté au conflit (--skip) est un abandon, pas une perte."""
        self._lot_en_conflit()
        self._conflit("bash", LANCEUR, "main")
        proc = self._lancer("bash", LANCEUR, "--skip")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(ABANDON, proc.stderr)
        self.assertIn("#900 sujet du lot", proc.stderr)
        self.assertNotIn(PERDU, proc.stderr)

    def test_commit_devenu_vide_n_est_pas_une_perte(self) -> None:
        """Même sans option : le commit vide abandonné (défaut git) n'est pas une perte."""
        self._lot_en_conflit()
        self._conflit("bash", LANCEUR, "main", contenu="l1\nmain\nl3\n")
        proc = self._lancer("bash", LANCEUR, "--continue")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(ABANDON, proc.stderr)
        self.assertNotIn(PERDU, proc.stderr)

    # -- le défaut de #1277 reste signalé ---------------------------------
    def test_sujet_nettoye_reste_signale_en_interactif(self) -> None:
        """Un sujet mangé par l'éditeur reste signalé, même en rebasage interactif."""
        self._lot_en_conflit()
        self._conflit("bash", LANCEUR, "-i", "main")
        proc = self._lancer("bash", LANCEUR, "--continue", editeur="sed -i 1d")
        self.assertEqual(proc.returncode, 4, proc.stderr)
        self.assertIn(PERDU, proc.stderr)
        self.assertIn("#900 sujet du lot", proc.stderr)


if __name__ == "__main__":
    unittest.main()
