# SPDX-License-Identifier: AGPL-3.0-only
"""#1969 : bascule atomique du venv de mise à jour.

Banc isolé uniquement : des venvs factices ou de VRAIS venvs (`--without-pip`)
dans un répertoire temporaire, une boucle d'appels concurrents, aucune
installation réelle, aucun hôte, aucune base. On vérifie les témoins exigés
par le corps du lot et par la revue BLOQUÉE du 16/10 :

* B1 — un processus garde un chemin IMMUTABLE : un enfant lancé avant la
  conversion (venv réel) puis un enfant lancé après (par le lanceur) peuvent
  importer un sous-module TARDIF après la bascule sans jamais voir le mélange
  ancien/nouveau ; de vrais interpréteurs sont utilisés pour ce témoin ;
* B2 — `retour` réutilise le venv réel conservé (lib liée, aucun pip), y
  compris à la première conversion ;
* pendant `activer` puis `retour`, un appel CLI concurrent voit toujours
  l'ancien ou le nouveau code ENTIER (jamais absent, jamais partiel) ;
* une préparation en échec laisse la version active intacte ;
* `retour` est idempotent et sans pip.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
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

#: enfant réel : importe le paquet, attend, puis importe un sous-module TARDIF
ENFANT = """import sys
import pkg1969
print("prefix=" + sys.prefix, flush=True)
print("valeur=" + pkg1969.VALUE, flush=True)
sys.stdin.readline()
import importlib
tardif = importlib.import_module("pkg1969." + sys.argv[1])
print("tardif=" + tardif.VALUE, flush=True)
"""


def faux_venv(base: Path, version: str, *, marque: bool = True) -> Path:
    """Un venv factice : `bin/python` et `bin/ameesh` nomment la version."""
    path = base / ("venv-%s" % version)
    (path / "bin").mkdir(parents=True)
    python = path / "bin" / "python"
    python.write_text('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\necho %s\n' % version)
    python.chmod(0o755)
    ameesh = path / "bin" / "ameesh"
    ameesh.write_text("#!/bin/sh\necho %s\n" % version)
    ameesh.chmod(0o755)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
    if marque:
        (path / bascule.MARKER).write_text(json.dumps({"version": version}))
    return path


def vrai_venv(base: Path, version: str, valeur: str, sous_module: str) -> Path:
    """Un VRAI venv (`--without-pip`) portant un paquet distinguable."""
    path = base / ("venv-%s" % version)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    site = next(path.glob("lib/python3*/site-packages"))
    paquet = site / "pkg1969"
    paquet.mkdir()
    (paquet / "__init__.py").write_text("VALUE = %r\n" % valeur)
    (paquet / (sous_module + ".py")).write_text("VALUE = %r\n" % valeur)
    ameesh = path / "bin" / "ameesh"
    ameesh.write_text("#!%s/bin/python\nimport pkg1969\nprint(pkg1969.VALUE)\n" % path)
    ameesh.chmod(0o755)
    return path


def lire(venv: Path) -> tuple[int, str]:
    proc = subprocess.run([str(venv / "bin" / "ameesh")], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def activer(venv: Path, version: str, precedent: str | None = None) -> int:
    args = ["activer", "--venv", str(venv), "--version", version]
    if precedent:
        args += ["--precedent", precedent]
    return bascule.main(args)


def retour(venv: Path) -> int:
    return bascule.main(["retour", "--venv", str(venv)])


class BasculeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bascule-1969-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.venv = self.base / "venv"
        self.enfants: list[subprocess.Popen] = []
        self.addCleanup(self._fermer_enfants)

    def _fermer_enfants(self):
        for enfant in self.enfants:
            if enfant.poll() is None:
                enfant.kill()
            enfant.wait(timeout=10)

    def enfant(self, python, sous_module):
        proc = subprocess.Popen([str(python), "-c", ENFANT, sous_module],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.enfants.append(proc)
        prefix = proc.stdout.readline().strip()
        valeur = proc.stdout.readline().strip()
        return proc, prefix, valeur

    @staticmethod
    def reveiller(proc):
        proc.stdin.write("\n")
        proc.stdin.flush()
        return proc.stdout.readline().strip()

    # -- B1 : le chemin d'un processus ne traverse jamais une bascule -------

    def test_import_tardif_ne_traverse_jamais_une_bascule(self):
        vrai_venv(self.base, "v1", "v1", "old_only")
        os.rename(self.base / "venv-v1", self.venv)      # venv réel d'avant #1969
        vrai_venv(self.base, "v2", "v2", "new_only")
        (self.base / "venv-v2" / bascule.MARKER).write_text(json.dumps({"version": "v2"}))
        # enfant hérité : lancé par le venv réel AVANT la conversion
        herite, prefix, valeur = self.enfant(self.venv / "bin" / "python", "old_only")
        self.assertEqual(os.path.realpath(self.venv), os.path.realpath(prefix.split("=", 1)[1]))
        self.assertEqual("valeur=v1", valeur)
        # bascule : adoption du venv réel (lib réutilisée) puis lien vers v2
        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        self.assertFalse(self.venv.is_symlink())
        self.assertTrue((self.venv / bascule.LAUNCHERS).is_file())
        self.assertEqual((0, "v2"), lire(self.venv))
        self.assertTrue((self.base / "venv-v1" / "lib").is_symlink())
        # l'enfant hérité importe encore SON sous-module d'origine
        self.assertEqual("tardif=v1", self.reveiller(herite))
        # un nouvel enfant part sur le venv versionné concret
        neuf, prefix, valeur = self.enfant(self.venv / "bin" / "python", "new_only")
        self.assertEqual(os.path.realpath(self.base / "venv-v2"),
                         os.path.realpath(prefix.split("=", 1)[1]))
        self.assertEqual("valeur=v2", valeur)
        # après retour, l'enfant neuf garde son chemin : toujours v2 entier
        self.assertEqual(0, retour(self.venv))
        self.assertEqual((0, "v1"), lire(self.venv))
        self.assertEqual("tardif=v2", self.reveiller(neuf))

    def test_alias_python3_et_scripts_console_suivent_la_version_vivante(self):
        vrai_venv(self.base, "v1", "v1", "old_only")
        os.rename(self.base / "venv-v1", self.venv)
        # Même forme que le venv installé : python3 vise directement le binaire
        # de base ; les points d'entrée pip ont ce python3 dans leur shebang.
        alias = self.venv / "bin" / "python3"
        alias.unlink()
        alias.symlink_to(os.path.realpath(sys.executable))
        for nom in ("agent-mail", "agent-runner"):
            script = self.venv / "bin" / nom
            script.write_text("#!%s\nimport pkg1969\nprint(pkg1969.VALUE)\n" % alias)
            script.chmod(0o755)
        vrai_venv(self.base, "v2", "v2", "new_only")
        (self.base / "venv-v2" / bascule.MARKER).write_text('{}')
        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        for valeur in ("v2", "v1"):
            for nom in ("python", "python3", "agent-mail", "agent-runner"):
                with self.subTest(version=valeur, commande=nom):
                    commande = [str(self.venv / "bin" / nom)]
                    if nom.startswith("python"):
                        commande += ["-c", "import pkg1969; print(pkg1969.VALUE)"]
                    resultat = subprocess.run(commande, capture_output=True, text=True)
                    self.assertEqual(0, resultat.returncode, resultat.stderr)
                    self.assertEqual(valeur, resultat.stdout.strip())
            if valeur == "v2":
                self.assertEqual(0, retour(self.venv))

    # -- B2 : le retour réutilise le venv réel, sans pip --------------------

    def test_retour_reutilise_le_venv_reel_sans_pip(self):
        vrai_venv(self.base, "v1", "v1", "old_only")
        os.rename(self.base / "venv-v1", self.venv)
        vrai_venv(self.base, "v2", "v2", "new_only")
        (self.base / "venv-v2" / bascule.MARKER).write_text(json.dumps({"version": "v2"}))
        with mock.patch.object(bascule, "_pip_install",
                               side_effect=AssertionError("pip interdit sur ce chemin")):
            self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
            self.assertEqual(0, retour(self.venv))
            self.assertEqual(0, retour(self.venv))       # idempotent, toujours sans pip
        self.assertEqual((0, "v1"), lire(self.venv))
        # la version adoptée réutilise le venv réel : lib liée, rien de déplacé
        self.assertTrue((self.base / "venv-v1" / "lib").is_symlink())
        self.assertTrue(os.path.samefile(self.base / "venv-v1" / "lib", self.venv / "lib"))
        self.assertEqual("v1", bascule.etat_de(str(self.venv))["precedente"])

    # -- conversions et idempotence (venvs factices, rapides) ---------------

    def test_conversion_atomique_retour_et_idempotence(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)      # état d'avant #1969
        faux_venv(self.base, "v2")
        self.assertEqual((0, "v1"), lire(self.venv))

        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        self.assertEqual((0, "v2"), lire(self.venv))
        self.assertEqual((0, "v1"), lire(self.base / "venv-v1"))   # conservé, adopté

        self.assertEqual(0, retour(self.venv))
        self.assertEqual((0, "v1"), lire(self.venv))
        self.assertEqual(0, retour(self.venv))            # idempotent
        self.assertEqual((0, "v1"), lire(self.venv))

        self.assertEqual(0, activer(self.venv, "v2"))     # lien -> lien
        self.assertEqual((0, "v2"), lire(self.venv))
        # relance sur le même SHA : aucun pip, la cible de retour ne bouge pas
        self.assertEqual(0, activer(self.venv, "v2", precedent="v2"))
        self.assertEqual((0, "v2"), lire(self.venv))
        self.assertEqual("v1", bascule.etat_de(str(self.venv))["precedente"])

    def test_echec_enregistrement_precedent_ne_bascule_pas(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)
        faux_venv(self.base, "v2")
        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        faux_venv(self.base, "v3")
        with mock.patch.object(bascule, "_write_report", side_effect=OSError("disque plein")):
            with self.assertRaises(OSError):
                activer(self.venv, "v3")
        self.assertEqual((0, "v2"), lire(self.venv))
        self.assertEqual(0, retour(self.venv))
        self.assertEqual((0, "v1"), lire(self.venv))

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
                self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
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

    def test_retour_sans_bascule_enregistree_est_un_noop(self):
        faux_venv(self.base, "v1")
        os.rename(self.base / "venv-v1", self.venv)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, retour(self.venv))
        self.assertEqual((0, "v1"), lire(self.venv))
        faux_venv(self.base, "v2")
        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        self.assertEqual((0, "v2"), lire(self.venv))


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

    def test_poste_adopte_la_version_quittee(self):
        """La première conversion nomme la version quittée : retour sans pip."""
        texte = (DEPLOY / "poste.sh").read_text()
        self.assertIn("--precedent", texte)

    def test_vm_adopte_la_version_quittee_et_retourne_sans_preparer(self):
        texte = (DEPLOY / "vm.sh").read_text()
        self.assertIn('reinstaller_agents "$sha" "$complet"', texte)
        self.assertIn("--precedent", texte)
        self.assertIn("$BASCULE_AGENTS retour", texte)
        self.assertNotIn('reinstaller_agents "$complet"', texte,
                         "le retour agents repasse par preparer/pip")


if __name__ == "__main__":
    unittest.main()
