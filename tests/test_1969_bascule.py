# SPDX-License-Identifier: AGPL-3.0-only
"""#1969 : bascule atomique du venv de mise à jour.

Banc isolé uniquement : des venvs factices dans un répertoire temporaire, une
boucle d'appels concurrents, aucune installation réelle, aucun hôte, aucune
base. On vérifie les témoins exigés par le corps du lot :

* pendant `activer` puis `retour`, un appel CLI concurrent voit toujours
  l'ancien ou le nouveau code ENTIER (jamais absent, jamais partiel) ;
* une préparation en échec laisse la version active intacte ;
* les shebangs/chemins restent valides après la bascule (le script console du
  venv versionné s'exécute via `<venv>/bin/…`) ;
* l'ancien venv reste sur disque (un processus déjà lancé garde son
  environnement) ;
* `retour` ramène à la version précédente complète, sans pip, et est
  idempotent.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/mise-a-jour'
SPEC = importlib.util.spec_from_file_location("bascule_1969", DEPLOY / "bascule.py")
bascule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bascule)

TEMOIN = """#!/bin/sh
# script console du venv versionné : nomme la version qui le porte
cat "$(dirname "$0")/../VERSION"
"""


def faux_venv(base: Path, version: str, *, marque: bool = True) -> Path:
    """Un venv factice : `bin/temoin` nomme la version, marqueur `.pret`."""
    path = base / ("venv-%s" % version)
    (path / "bin").mkdir(parents=True)
    (path / "VERSION").write_text(version + "\n")
    temoin = path / "bin" / "temoin"
    temoin.write_text(TEMOIN)
    temoin.chmod(0o755)
    if marque:
        (path / bascule.MARKER).write_text(json.dumps({"version": version}))
    return path


def lire(venv: Path) -> tuple[int, str]:
    proc = subprocess.run([str(venv / "bin" / "temoin")], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def activer(venv: Path, version: str) -> int:
    return bascule.main(["activer", "--venv", str(venv), "--version", version])


def retour(venv: Path) -> int:
    return bascule.main(["retour", "--venv", str(venv)])


class BasculeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bascule-1969-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.venv = self.base / "venv"

    # -- première conversion : venv réel -> lien, puis bascules ------------

    def test_conversion_atomique_retour_et_idempotence(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)      # état d'avant #1969
        faux_venv(self.base, "v2")
        self.assertEqual((0, "v1"), lire(self.venv))

        self.assertEqual(0, activer(self.venv, "v2"))
        self.assertTrue(self.venv.is_symlink())
        self.assertEqual((0, "v2"), lire(self.venv))
        # l'ancien venv entier reste sur disque (processus déjà lancé)
        self.assertEqual((0, "v1"), lire(self.base / "venv-precedent"))

        self.assertEqual(0, retour(self.venv))
        self.assertEqual((0, "v1"), lire(self.venv))
        self.assertEqual(0, retour(self.venv))            # idempotent
        self.assertEqual((0, "v1"), lire(self.venv))

        self.assertEqual(0, activer(self.venv, "v2"))     # lien -> lien
        self.assertEqual((0, "v2"), lire(self.venv))

    def test_bascule_concurrente_voit_ancien_ou_nouveau_entier(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)
        faux_venv(self.base, "v2")
        arret = threading.Event()
        vus: list[tuple[int, str]] = []

        def lecteur():
            while not arret.is_set():
                vus.append(lire(self.venv))

        fil = threading.Thread(target=lecteur, daemon=True)
        fil.start()
        try:
            for _ in range(25):
                self.assertEqual(0, activer(self.venv, "v2"))
                self.assertEqual(0, retour(self.venv))
        finally:
            arret.set()
            fil.join(timeout=10)
        self.assertTrue(vus, "aucun appel concurrent observé")
        codes = {code for code, _ in vus}
        sorties = {sortie for _, sortie in vus}
        self.assertEqual({0}, codes, "un appel concurrent a échoué : %r" % (vus[:5],))
        self.assertLessEqual(sorties, {"v1", "v2"}, "état partiel observé : %r" % sorties)

    # -- préparation isolée et échec --------------------------------------

    def test_preparation_echouee_laisse_la_version_active_intacte(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)
        (self.base / "src-v3").mkdir()
        args = SimpleNamespace(venv=str(self.venv), src=str(self.base / "src-v3"),
                               version="v3", python="python3")
        with mock.patch.object(bascule, "_creer_venv"), \
             mock.patch.object(bascule, "_pip_install",
                               side_effect=subprocess.CalledProcessError(1, "pip")):
            with redirect_stdout(io.StringIO()):
                code = bascule.preparer(args)
        self.assertEqual(1, code)
        self.assertFalse((self.base / "venv-v3").exists(), "répertoire partiel laissé")
        self.assertFalse(self.venv.is_symlink())
        self.assertEqual((0, "v1"), lire(self.venv))

    def test_preparation_complete_puis_activer(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)
        (self.base / "src-v2").mkdir()

        def creer(directory, python):
            faux_venv(self.base, "v2")
            os.rename(self.base / "venv-v2", directory)

        args = SimpleNamespace(venv=str(self.venv), src=str(self.base / "src-v2"),
                               version="v2", python="python3")
        with mock.patch.object(bascule, "_creer_venv", side_effect=creer), \
             mock.patch.object(bascule, "_pip_install"), \
             mock.patch.object(bascule, "_controles"):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, bascule.preparer(args))
        self.assertTrue((self.base / "venv-v2" / bascule.MARKER).is_file())
        self.assertEqual(0, activer(self.venv, "v2"))
        self.assertEqual((0, "v2"), lire(self.venv))

    def test_activer_refuse_un_venv_non_prepare(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)
        faux_venv(self.base, "v9", marque=False)
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                activer(self.venv, "v9")
        self.assertEqual((0, "v1"), lire(self.venv))

    def test_retour_sans_precedente_refuse(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                retour(self.venv)
        self.assertEqual((0, "v1"), lire(self.venv))


class CablageTest(unittest.TestCase):
    """Les scripts de mise à jour passent par la bascule, plus par pip vivant."""

    def test_poste_et_vm_utilisent_la_bascule(self):
        for nom in ("poste.sh", "vm.sh"):
            texte = (DEPLOY / nom).read_text()
            self.assertIn("bascule.py", texte, "%s n'appelle pas la bascule" % nom)
            self.assertNotIn('"$VENV/bin/pip" install', texte,
                             "%s installe encore dans le venv vivant" % nom)
            self.assertNotIn('$VENV/bin/pip install', texte,
                             "%s installe encore dans le venv vivant" % nom)

    def test_preparer_avant_activer_dans_poste(self):
        texte = (DEPLOY / "poste.sh").read_text()
        self.assertLess(texte.index('bascule.py" preparer'),
                        texte.index('bascule.py" activer'))


if __name__ == "__main__":
    unittest.main()
