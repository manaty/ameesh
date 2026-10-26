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
from contextlib import redirect_stderr, redirect_stdout
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
        script = self.venv / "bin" / "agent-mail"
        script.write_text("#!%s\nimport pkg1969\nprint(pkg1969.VALUE)\n" % alias)
        script.chmod(0o755)
        # #3135 : un script hérité valide peut commencer par une docstring et un
        # `from __future__` ; la conversion ne doit pas invalider l'ordre exigé
        # par Python (relecture ameesh-cx2, B1).
        script = self.venv / "bin" / "agent-runner"
        script.write_text("#!%s\n\"\"\"Legacy command.\"\"\"\n"
                          "from __future__ import annotations\n"
                          "import pkg1969\nprint(pkg1969.VALUE)\n" % alias)
        script.chmod(0o755)
        vrai_venv(self.base, "v2", "v2", "new_only")
        (self.base / "venv-v2" / bascule.MARKER).write_text('{}')
        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        # #3135 : python3 est devenu un lanceur /bin/sh ; un script hérité qui
        # garderait `#!…/bin/python3` serait un shebang imbriqué, que macOS
        # refuse (Errno 8). Il passe par /usr/bin/env (binaire), qui exécute le
        # lanceur ; seul le shebang change.
        for nom in ("agent-mail", "agent-runner"):
            lignes = (self.venv / "bin" / nom).read_text().splitlines()
            self.assertEqual(lignes[0], "#!/usr/bin/env %s" % alias, nom)
        lignes = (self.venv / "bin" / "agent-runner").read_text().splitlines()
        self.assertEqual(lignes[1], '"""Legacy command."""')
        self.assertEqual(lignes[2], "from __future__ import annotations")
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

    def test_script_herite_garde_ses_instructions_sur_une_ligne(self):
        """#3135 B1 (ameesh-cx2) : la conversion ne touche qu'au shebang.

        Les cas « docstring ; from __future__ » et deux imports __future__ sur
        une même ligne restent valides : le corps du script est intact, sans
        déplacement ni duplication d'instructions.
        """
        vrai_venv(self.base, "v1", "v1", "old_only")
        os.rename(self.base / "venv-v1", self.venv)
        alias = self.venv / "bin" / "python3"
        alias.unlink()
        alias.symlink_to(os.path.realpath(sys.executable))
        corps = ('#!%s\n'
                 '"""Legacy command."""; from __future__ import annotations\n'
                 'from __future__ import annotations; from __future__ import division; '
                 'print("once")\n'
                 'print("OK")\n' % alias)
        script = self.venv / "bin" / "herite"
        script.write_text(corps)
        script.chmod(0o755)
        vrai_venv(self.base, "v2", "v2", "new_only")
        (self.base / "venv-v2" / bascule.MARKER).write_text('{}')
        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        lignes = script.read_text().splitlines()
        self.assertEqual(lignes[0], "#!/usr/bin/env %s" % alias)
        self.assertEqual(lignes[1:], corps.splitlines()[1:],
                         "le corps du script a été déplacé ou dupliqué")
        resultat = subprocess.run([str(script)], capture_output=True, text=True)
        self.assertEqual(0, resultat.returncode, resultat.stderr)
        self.assertEqual(resultat.stdout.splitlines(), ["once", "OK"])

    def test_script_console_ajoute_par_une_version_ulterieure_est_expose(self):
        """#2155 : un point d'entrée pip de la v2 apparaît dans le venv stable."""
        vrai_venv(self.base, "v1", "v1", "old_only")
        os.rename(self.base / "venv-v1", self.venv)
        vrai_venv(self.base, "v2", "v2", "new_only")
        (self.base / "venv-v2" / bascule.MARKER).write_text(json.dumps({"version": "v2"}))
        # pip n'écrit ce script que dans venv-v2/bin : le venv stable ne l'a
        # jamais vu à l'adoption.
        outil = self.base / "venv-v2" / "bin" / "futur-outil"
        outil.write_text("#!%s/bin/python\nimport pkg1969\nprint(pkg1969.VALUE)\n"
                         % (self.base / "venv-v2"))
        outil.chmod(0o755)

        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))

        lanceur = self.venv / "bin" / "futur-outil"
        self.assertTrue(lanceur.is_file(),
                        "le script console de la version active n'est pas exposé")
        resultat = subprocess.run([str(lanceur)], capture_output=True, text=True)
        self.assertEqual(0, resultat.returncode, resultat.stderr)
        self.assertEqual("v2", resultat.stdout.strip())
        # Le python et l'ameesh stables restent des lanceurs de la version vive.
        self.assertEqual((0, "v2"), lire(self.venv))

        self.assertEqual(0, retour(self.venv))
        # Le lanceur créé pour la v2 disparaît quand la version de retour ne
        # porte pas ce script ; les scripts adoptés de la v1 restent.
        self.assertFalse(lanceur.exists(), "lanceur d'une version quittée laissé")
        self.assertEqual((0, "v1"), lire(self.venv))

    def test_activation_echouee_apres_lanceurs_ne_laisse_pas_d_orphelin(self):
        """#2230 N1 : le ménage se recalcule depuis bin/, pas depuis le marqueur."""
        vrai_venv(self.base, "v1", "v1", "v1")
        os.rename(self.base / "venv-v1", self.venv)
        vrai_venv(self.base, "v2", "v2", "v2")
        (self.base / "venv-v2" / bascule.MARKER).write_text(json.dumps({"version": "v2"}))
        outil = self.base / "venv-v2" / "bin" / "outil-v2"
        outil.write_text("#!%s/bin/python\nimport pkg1969\nprint(pkg1969.VALUE)\n"
                         % (self.base / "venv-v2"))
        outil.chmod(0o755)

        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        lanceur = self.venv / "bin" / "outil-v2"
        self.assertTrue(lanceur.is_file())

        # #3792 : v3 est préparé APRÈS l'activation de v2. Un venv préparé
        # d'avance, ni courant ni précédent, serait retiré par le ménage de
        # l'activation précédente — le préparer après garde le scénario testé.
        vrai_venv(self.base, "v3", "v3", "v3")
        (self.base / "venv-v3" / bascule.MARKER).write_text(json.dumps({"version": "v3"}))

        # L'activation de v3 écrit ses lanceurs et son marqueur, puis échoue
        # avant la bascule (écriture de la précédente impossible) : le
        # marqueur nomme déjà v3, donc les noms de v2 en sortent.
        with mock.patch.object(bascule, "_write_report", side_effect=OSError("disque plein")):
            with self.assertRaises(OSError):
                activer(self.venv, "v3")
        self.assertEqual((0, "v2"), lire(self.venv))
        self.assertTrue(lanceur.is_file(), "l'échec ne doit rien retirer")

        # La reprise aboutit et retire le lanceur que la version vivante ne
        # porte plus : un orphelin ne survit pas à une activation interrompue.
        self.assertEqual(0, activer(self.venv, "v3"))
        self.assertEqual((0, "v3"), lire(self.venv))
        self.assertFalse(lanceur.exists(), "lanceur orphelin laissé hors du suivi")

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

    def test_retour_bascule_meme_si_les_lanceurs_ne_peuvent_pas_etre_ecrits(self):
        """#2230 N3 : le retour utile est le rename ; les lanceurs sont best-effort."""
        vrai_venv(self.base, "v1", "v1", "v1")
        os.rename(self.base / "venv-v1", self.venv)
        vrai_venv(self.base, "v2", "v2", "v2")
        (self.base / "venv-v2" / bascule.MARKER).write_text(json.dumps({"version": "v2"}))
        self.assertEqual(0, activer(self.venv, "v2", precedent="v1"))
        with mock.patch.object(bascule, "_installer_lanceurs",
                               side_effect=OSError("disque plein")):
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(0, retour(self.venv))
        self.assertEqual((0, "v1"), lire(self.venv))
        self.assertIn("lanceurs non réécrits", err.getvalue())

    def test_ecriture_echouee_retire_le_temporaire_et_preserve_la_cible(self):
        cible = self.base / "bin" / "ameesh"
        cible.parent.mkdir()
        cible.write_text("ancien")
        vrai_open = open
        erreur = OSError("disque plein")

        class FichierPlein:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, data):
                self.handle.write(data[:2])
                self.handle.flush()
                raise erreur

        for etape in ("write", "chmod", "replace"):
            with self.subTest(etape=etape):
                if etape == "write":
                    patch = mock.patch.object(
                        bascule, "open", create=True,
                        side_effect=lambda path, mode: FichierPlein(vrai_open(path, mode)))
                else:
                    patch = mock.patch.object(bascule.os, etape, side_effect=erreur)
                with patch, self.assertRaises(OSError) as levee:
                    bascule._write(str(cible), "nouveau")
                self.assertIs(erreur, levee.exception)
                self.assertEqual("ancien", cible.read_text())
                self.assertEqual([], list(cible.parent.glob("*.tmp.*")))

    def test_echec_du_nettoyage_ne_masque_pas_lechec_decriture(self):
        cible = self.base / "ameesh"
        erreur = OSError("rename impossible")
        with mock.patch.object(bascule.os, "replace", side_effect=erreur), \
             mock.patch.object(bascule.os, "remove", side_effect=PermissionError("nettoyage")), \
             self.assertRaises(OSError) as levee:
            bascule._write(str(cible), "nouveau")
        self.assertIs(erreur, levee.exception)

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

    def test_controles_acceptent_le_shebang_python3_de_pip(self):
        cible = self.base / "venv-v2"
        (cible / "bin").mkdir(parents=True)
        ameesh = cible / "bin" / "ameesh"

        def controler(shebang):
            ameesh.write_text(shebang + "\nimport sys\n")
            with mock.patch.object(bascule, "_run"):
                bascule._controles(str(cible))

        for nom in ("python", "python3", "python3.14"):
            controler("#!%s" % (cible / "bin" / nom))
        for faux in ("#!%s" % (self.venv / "bin" / "python3"),
                     "#!/usr/bin/python3",
                     "#!%s" % (cible / "bin" / "pythonx")):
            with self.assertRaises(RuntimeError):
                controler(faux)

    def test_controles_acceptent_le_wrapper_bin_sh_de_pip_chemin_long(self):
        """#2156 : au-delà de 127 octets, pip préfixe le script d'un /bin/sh."""
        cible = self.base / "venv-v2"
        (cible / "bin").mkdir(parents=True)
        ameesh = cible / "bin" / "ameesh"

        def controler(executable, fermeture="' '''"):
            # Forme écrite par pip 24 (distlib) quand le shebang dépasse 127
            # octets, reproduite avec un interpréteur de 148 caractères.
            preambule = ("#!/bin/sh\n"
                         "'''exec' %s \"$0\" \"$@\"\n"
                         "%s\n" % (executable, fermeture))
            ameesh.write_text(preambule + "import sys\n")
            with mock.patch.object(bascule, "_run"):
                bascule._controles(str(cible))

        for nom in ("python", "python3", "python3.14"):
            controler(cible / "bin" / nom)
        # chemin contenant une espace : distlib met l'exécutable entre guillemets
        controler('"%s"' % (cible / "bin" / "python3"))
        for faux in (self.venv / "bin" / "python3",
                     "/usr/bin/python3",
                     cible / "bin" / "pythonx"):
            with self.assertRaises(RuntimeError):
                controler(faux)
        # la troisième ligne ferme la chaîne qui rend le script valide en Python
        with self.assertRaises(RuntimeError):
            controler(cible / "bin" / "python3", fermeture="'''")

    def test_controles_diagnostiquent_lexec_fautif_avant_de_lexecuter(self):
        cible = self.base / "venv-v2"
        (cible / "bin").mkdir(parents=True)
        (cible / "bin" / "ameesh").write_text(
            '#!/bin/sh\n\'\'\'exec\' /autre/venv/bin/python3 "$0" "$@"\n\' \'\'\'\n')
        with mock.patch.object(bascule, "_run") as run, \
             self.assertRaises(RuntimeError) as levee:
            bascule._controles(str(cible))
        message = str(levee.exception)
        self.assertIn("ligne 2", message)
        self.assertIn("/autre/venv/bin/python3", message)
        self.assertIn("interpréteur attendu", message)
        self.assertIn(str(cible / "bin" / "python"), message)
        run.assert_not_called()

    def test_controles_bornent_chacune_des_trois_lignes_du_preambule(self):
        cible = self.base / "venv-v2"
        python = cible / "bin" / "python3"
        prefixe = [b"#!/bin/sh\n", ("'''exec' %s \"$0\" \"$@\"\n" % python).encode()]

        class LectureBornee(io.BytesIO):
            def readline(handle, size=-1):
                self.assertGreater(size, 0, "lecture sans borne")
                self.assertLessEqual(size, 8193)
                return super().readline(size)

        for ligne in (1, 2, 3):
            with self.subTest(ligne=ligne):
                source = LectureBornee(b"".join(prefixe[:ligne - 1]) + b"x" * 65536 + b"\n")
                with mock.patch.object(bascule, "open", create=True, return_value=source), \
                     mock.patch.object(bascule, "_run") as run, \
                     self.assertRaisesRegex(RuntimeError, "ligne %d.*8192" % ligne):
                    bascule._controles(str(cible))
                run.assert_not_called()

    def test_shebang_direct_ne_lit_pas_le_corps_python(self):
        cible = self.base / "venv-v2"
        (cible / "bin").mkdir(parents=True)
        (cible / "bin" / "ameesh").write_text(
            "#!%s\n#%s\n" % (cible / "bin" / "python3", "x" * 65536))
        with mock.patch.object(bascule, "_run"):
            bascule._controles(str(cible))

    def test_apostrophe_acceptee_en_shebang_direct_ou_wrapper_quote(self):
        court = vrai_venv(self.base / "l'owner", "v2", "v2", "new_only")
        long = vrai_venv(self.base / ("l'owner avec espace " + "x" * 120),
                         "v2", "v2", "new_only")
        for cible in (court, long):
            with self.subTest(cible=cible):
                python = cible / "bin" / "python3"
                preambule = ("#!%s\n" % python if cible == court else
                             '#!/bin/sh\n\'\'\'exec\' "%s" "$0" "$@"\n\' \'\'\'\n' % python)
                (cible / "bin" / "ameesh").write_text(
                    preambule + "import pkg1969\nprint(pkg1969.VALUE)\n")
                with mock.patch.object(bascule, "_run"):
                    bascule._controles(str(cible))
                self.assertEqual((0, "v2"), lire(cible))

    def test_wrapper_avec_apostrophe_non_quotee_est_refuse_explicitement(self):
        cible = self.base / "l'owner" / "venv-v2"
        (cible / "bin").mkdir(parents=True)
        (cible / "bin" / "ameesh").write_text(
            '#!/bin/sh\n\'\'\'exec\' %s "$0" "$@"\n\' \'\'\'\n' %
            (cible / "bin" / "python3"))
        with mock.patch.object(bascule, "_run") as run, \
             self.assertRaisesRegex(RuntimeError, "ligne 2.*quotage shell invalide"):
            bascule._controles(str(cible))
        run.assert_not_called()

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
