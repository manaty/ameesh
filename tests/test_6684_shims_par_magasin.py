# SPDX-License-Identifier: AGPL-3.0-only
"""#6684 : un dossier de shims par magasin d'ameesh, sur un même state_dir.

Deux magasins (le magasin par défaut et un magasin séparé) écrivaient les
mêmes `<state_dir>/.menage/bin-ameesh/ameesh` et `agent-mail` : le dernier
runner qui démarrait un tour décidait de l'ameesh de tous les tours. Témoins :

* deux magasins sur le même `state_dir` → deux dossiers, chacun visant le
  venv de son magasin ;
* même magasin, deux versions (`venv-<sha7>`) → même dossier ;
* magasin par défaut → chemin inchangé (aucune migration des hôtes) ;
* échec d'écriture → comportement de #2772 inchangé (le venv de production
  n'est jamais rouvert) ;
* `ameesh doctor` signale un dossier de shims dont la cible n'est pas le venv
  du magasin courant.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ameesh import venv_guard  # noqa: E402


def _executable(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class Banc(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ameesh-6684-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.cfg = SimpleNamespace(state_dir=str(self.state))
        self.home_patch = mock.patch.dict(
            os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def venv(self, root: Path, nom: str) -> Path:
        venv = root / nom
        for entry in venv_guard.SHIMMED:
            _executable(venv / "bin" / entry)
        return venv

    def en_venv(self, prefix):
        return mock.patch.multiple(sys, prefix=str(prefix), base_prefix="/usr")

    def shims(self, directory) -> dict:
        return {nom: (Path(directory) / nom).read_text(encoding="utf-8")
                for nom in venv_guard.SHIMMED}


class DossierTest(Banc):
    def test_deux_magasins_sur_le_meme_etat_ont_deux_dossiers(self):
        """Le magasin par défaut garde le chemin ; l'autre en reçoit un."""
        defaut = self.venv(self.home / ".local/share/ameesh", "venv-aaa1111")
        manaty = self.venv(self.base / "ameesh-manaty", "venv-bbb2222")

        with self.en_venv(defaut):
            dossier_defaut = venv_guard.shim_dir(self.cfg)
        with self.en_venv(manaty):
            dossier_manaty = venv_guard.shim_dir(self.cfg)

        self.assertEqual(os.path.join(str(self.state), ".menage",
                                      venv_guard.SHIM_DIR), dossier_defaut)
        self.assertNotEqual(dossier_defaut, dossier_manaty)
        self.assertTrue(os.path.basename(dossier_manaty).startswith(
            venv_guard.SHIM_DIR + "-"), dossier_manaty)

        # chacun écrit ses shims, et le dernier écrivain ne vole plus l'autre
        for prefix, dossier in ((defaut, dossier_defaut),
                                (manaty, dossier_manaty)):
            with self.en_venv(prefix):
                venv_guard.scrub({"PATH": "/usr/bin"}, dossier)
        textes_defaut = self.shims(dossier_defaut)
        textes_manaty = self.shims(dossier_manaty)
        for nom in venv_guard.SHIMMED:
            self.assertIn(str(defaut / "bin" / nom), textes_defaut[nom])
            self.assertIn(str(manaty / "bin" / nom), textes_manaty[nom])
            self.assertNotIn(str(manaty), textes_defaut[nom])
            self.assertNotIn(str(defaut), textes_manaty[nom])

        # réécrire un magasin ne touche pas le dossier de l'autre
        with self.en_venv(manaty):
            venv_guard.scrub({"PATH": "/usr/bin"}, dossier_manaty)
        self.assertEqual(textes_defaut, self.shims(dossier_defaut))

    def test_meme_magasin_deux_versions_meme_dossier(self):
        """`venv-<sha7>` change à chaque déploiement, la racine non (#3311)."""
        share = self.base / "share"
        ancien = self.venv(share, "venv-aaa1111")
        courant = self.venv(share, "venv-bbb2222")
        with self.en_venv(ancien):
            premier = venv_guard.shim_dir(self.cfg)
        with self.en_venv(courant):
            second = venv_guard.shim_dir(self.cfg)
        self.assertEqual(premier, second)

    def test_lanceur_stable_le_meme_shim_pour_deux_versions(self):
        """#3311 : le shim vise le lanceur stable voisin, pas chaque version."""
        share = self.base / "share"
        ancien = self.venv(share, "venv-aaa1111")
        courant = self.venv(share, "venv-bbb2222")
        stable = self.venv(share, "venv")
        (stable / venv_guard.BASCULE_MARKER).write_text("{}", encoding="utf-8")
        with self.en_venv(ancien):
            dossier = venv_guard.shim_dir(self.cfg)
            venv_guard.scrub({"PATH": "/usr/bin"}, dossier)
        textes = self.shims(dossier)
        with self.en_venv(courant):
            venv_guard.scrub({"PATH": "/usr/bin"}, dossier)
        self.assertEqual(textes, self.shims(dossier))
        for nom in venv_guard.SHIMMED:
            self.assertIn(str(stable / "bin" / nom), textes[nom])

    def test_magasin_par_defaut_garde_le_chemin_historique(self):
        historique = os.path.join(str(self.state), ".menage",
                                  venv_guard.SHIM_DIR)
        for prefix in (self.home / ".local/share/ameesh/venv-aaa1111",
                       self.home / ".local/share/ameesh/venv-bbb2222",
                       Path("/opt/ameesh/venv-aaa1111")):
            with self.en_venv(prefix):
                self.assertEqual(historique, venv_guard.shim_dir(self.cfg))
                self.assertTrue(venv_guard.is_default_store())

    def test_magasin_d_outils_a_sa_propre_racine(self):
        """`uv tool install` n'a pas de `venv-<sha7>` : le venv est sa racine."""
        outils = self.venv(self.base / "uv/tools", "ameesh")
        with self.en_venv(outils):
            self.assertEqual(str(outils), venv_guard.store_root())
            self.assertFalse(venv_guard.is_default_store())
            dossier = venv_guard.shim_dir(self.cfg)
            self.assertNotEqual(
                os.path.join(str(self.state), ".menage",
                             venv_guard.SHIM_DIR), dossier)
            venv_guard.scrub({"PATH": "/usr/bin"}, dossier)
            self.assertEqual([], venv_guard.shim_problems(self.cfg))
        for nom in venv_guard.SHIMMED:
            self.assertIn(str(outils / "bin" / nom),
                          (Path(dossier) / nom).read_text(encoding="utf-8"))

    def test_echec_d_ecriture_ne_rouvre_pas_le_venv(self):
        """#2772 : un dossier de shims par magasin ne rouvre pas le PATH."""
        manaty = self.venv(self.base / "ameesh-manaty", "venv-bbb2222")
        autre = self.base / "outils"
        autre.mkdir()
        env = {"VIRTUAL_ENV": str(manaty),
               "PATH": os.pathsep.join([str(manaty / "bin"), str(autre),
                                        "/usr/bin"])}
        with self.en_venv(manaty):
            dossier = venv_guard.shim_dir(self.cfg)
            with mock.patch.object(venv_guard, "install_shims",
                                   side_effect=OSError("ro")):
                with self.assertLogs(venv_guard.LOG, level="WARNING"):
                    venv_guard.scrub(env, dossier)
        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertEqual([str(autre), "/usr/bin"],
                         env["PATH"].split(os.pathsep))


class DoctorTest(Banc):
    def test_signale_une_cible_d_un_autre_magasin(self):
        defaut = self.venv(self.home / ".local/share/ameesh", "venv-aaa1111")
        manaty = self.venv(self.base / "ameesh-manaty", "venv-bbb2222")

        # l'incident : le magasin séparé a écrit ses shims dans le dossier
        # historique du magasin par défaut (dossier partagé d'avant #6684)
        with self.en_venv(manaty):
            historique = os.path.join(str(self.state), ".menage",
                                      venv_guard.SHIM_DIR)
            venv_guard.scrub({"PATH": "/usr/bin"}, historique)
        with self.en_venv(defaut):
            problemes = venv_guard.shim_problems(self.cfg)
        self.assertEqual(len(venv_guard.SHIMMED), len(problemes), problemes)
        self.assertTrue(all(str(manaty) in ligne for ligne in problemes),
                        problemes)

        # un tour du magasin par défaut réécrit son dossier : plus d'écart
        with self.en_venv(defaut):
            venv_guard.scrub({"PATH": "/usr/bin"},
                             venv_guard.shim_dir(self.cfg))
            self.assertEqual([], venv_guard.shim_problems(self.cfg))

    def test_shim_absent_ou_illisible_est_signale_sans_lever(self):
        manaty = self.venv(self.base / "ameesh-manaty", "venv-bbb2222")
        with self.en_venv(manaty):
            dossier = venv_guard.shim_dir(self.cfg)
            os.makedirs(dossier, mode=0o700)
            # absent : pas un écart (un tour le réécrit)
            self.assertEqual([], venv_guard.shim_problems(self.cfg))
            # présent mais étranger : signalé
            for nom in venv_guard.SHIMMED:
                with open(os.path.join(dossier, nom), "w",
                          encoding="utf-8") as fh:
                    fh.write("#!/bin/sh\n")
            problemes = venv_guard.shim_problems(self.cfg)
            self.assertEqual(len(venv_guard.SHIMMED), len(problemes))
            self.assertTrue(all("no target" in ligne
                                for ligne in problemes), problemes)

    def test_doctor_cable_le_controle_des_shims(self):
        texte = (ROOT / "src/ameesh/cli.py").read_text(encoding="utf-8")
        debut = texte.index("def cmd_doctor")
        corps = texte[debut:texte.index("\ndef ", debut + 1)]
        self.assertIn("venv_guard.shim_problems(cfg)", corps)
        self.assertIn('print("shims      : KO', corps)


if __name__ == "__main__":
    unittest.main()
