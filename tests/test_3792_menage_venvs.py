# SPDX-License-Identifier: AGPL-3.0-only
"""#3792 : retirer les anciens venv-* d'une installation d'hôte.

Incident du 26/10 : chaque mise à jour d'hôte créait un `venv-<sha7>` complet
(65 à 85 Mo) et rien ne retirait les anciens ; 30 venvs s'accumulaient sur
agents-1 (environ 2 Gio, disque à 95 %). Le ménage manuel au motif `venv-*` a
aussi retiré le lien `venv-courant`, et `ameesh` a répondu « aucun venv
vivant » pendant 1 à 2 minutes.

Le ménage de `bascule.py` doit donc :

* lire le courant par la CIBLE du lien `venv-courant`, jamais par un motif ;
* garder le précédent enregistré (`venv.precedent`) ;
* garder tout venv qu'un processus vivant nomme par un chemin absolu
  (`/proc/<pid>/cmdline`) ;
* ne jamais retirer le lien lui-même, ni un autre lien, ni le venv stable
  `venv` ;
* ne rien retirer quand l'état ne permet pas de conclure (lien disparu,
  processus illisibles) ;
* être à blanc par défaut et ne jamais faire échouer une bascule réussie.

Témoin du corps : cinq venv, dont le courant, le précédent et un en usage →
deux retirés, le lien `venv-courant` intact.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

from ameesh import platform

from tests import support  # noqa: F401 — environnement isolé du poste

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy/mise-a-jour"
SPEC = importlib.util.spec_from_file_location("bascule_3792", DEPLOY / "bascule.py")
bascule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bascule)


def faux_venv(base: Path, version: str, *, vivant: bool = False) -> Path:
    """Un venv versionné factice : `bin/python` et `bin/ameesh` nomment la version."""
    path = base / ("venv-%s" % version)
    (path / "bin").mkdir(parents=True)
    python = path / "bin" / "python"
    if vivant:
        python.write_text("#!/bin/sh\nsleep 120\n")
    else:
        python.write_text('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\necho %s\n' % version)
    python.chmod(0o755)
    ameesh = path / "bin" / "ameesh"
    ameesh.write_text("#!/bin/sh\necho %s\n" % version)
    ameesh.chmod(0o755)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (path / bascule.MARKER).write_text(json.dumps({"version": version}))
    return path


def cmdline(*arguments: str) -> bytes:
    """Une ligne de commande `/proc/<pid>/cmdline` : arguments séparés par NUL."""
    return b"".join(os.fsencode(argument) + b"\0" for argument in arguments)


class MenageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="menage-3792-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "venv").mkdir()
        self.proc = self.base / "proc"
        self.proc.mkdir()

    def lien(self, cible: str) -> None:
        (self.base / bascule.CURRENT).symlink_to(cible)

    def precedent(self, version: str) -> None:
        (self.base / bascule.REPORT).write_text(version + "\n")

    def cinq_venvs(self):
        """Le témoin du corps : courant, précédent, en usage, deux anciens."""
        courant = faux_venv(self.base, "1111111")
        precedent = faux_venv(self.base, "2222222")
        usage = faux_venv(self.base, "3333333", vivant=True)
        anciens = [faux_venv(self.base, "4444444"), faux_venv(self.base, "5555555")]
        self.lien(courant.name)
        self.precedent("2222222")
        (self.proc / "4242").mkdir()
        (self.proc / "4242" / "cmdline").write_bytes(
            cmdline(str(usage / "bin" / "python"), "-c", "import ameesh"))
        return courant, precedent, usage, anciens

    def menager(self, *, appliquer: bool):
        return bascule._menager(str(self.base / "venv"), appliquer=appliquer,
                                racine=str(self.proc))

    # -- le témoin du corps --------------------------------------------------

    def test_cinq_venvs_deux_retires_et_lien_intact(self):
        courant, precedent, usage, anciens = self.cinq_venvs()
        avant = os.readlink(self.base / bascule.CURRENT)

        rapport = self.menager(appliquer=True)

        self.assertIsNone(rapport["probleme"])
        self.assertEqual(["venv-4444444", "venv-5555555"], sorted(rapport["retires"]))
        self.assertFalse(anciens[0].exists(), "un ancien venv est resté")
        self.assertFalse(anciens[1].exists(), "un ancien venv est resté")
        self.assertTrue(courant.is_dir(), "le venv courant a été retiré")
        self.assertTrue(precedent.is_dir(), "le venv précédent a été retiré")
        self.assertTrue(usage.is_dir(), "le venv d'un processus vivant a été retiré")
        self.assertTrue((self.base / "venv").is_dir(), "le venv stable a été touché")
        # le lien n'est ni retiré ni repointé : les launchers le lisent tel quel
        self.assertTrue((self.base / bascule.CURRENT).is_symlink())
        self.assertEqual(avant, os.readlink(self.base / bascule.CURRENT))
        raisons = dict(rapport["gardes"])
        self.assertEqual("courant", raisons["venv-1111111"])
        self.assertEqual("précédent", raisons["venv-2222222"])
        self.assertEqual("utilisé par un processus vivant", raisons["venv-3333333"])

    def test_le_lien_est_lu_par_sa_cible_absolue(self):
        courant = faux_venv(self.base, "1111111")
        self.lien(str(courant))
        rapport = self.menager(appliquer=True)
        self.assertIsNone(rapport["probleme"])
        self.assertTrue(courant.is_dir())
        self.assertEqual([], rapport["retires"])

    # -- mode à blanc --------------------------------------------------------

    def test_a_blanc_ne_retire_rien(self):
        _courant, _precedent, _usage, anciens = self.cinq_venvs()
        rapport = self.menager(appliquer=False)
        self.assertEqual(["venv-4444444", "venv-5555555"], sorted(rapport["retirables"]))
        self.assertEqual([], rapport["retires"])
        for ancien in anciens:
            self.assertTrue(ancien.is_dir(), "le mode à blanc a retiré %s" % ancien)
        self.assertTrue((self.base / bascule.CURRENT).is_symlink())

    # -- processus vivants ---------------------------------------------------

    @unittest.skipUnless(platform.is_linux(), "lecture de /proc/<pid>/cmdline : Linux")
    def test_un_processus_reel_garde_son_venv(self):
        _courant, _precedent, usage, anciens = self.cinq_venvs()
        # Le faux venv « en usage » porte un vrai processus qui dort : son
        # argv[1] est le chemin absolu du script dans le venv.
        enfant = subprocess.Popen([str(usage / "bin" / "python")],
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        self.addCleanup(self._arreter, enfant)
        chemin = usage / "bin" / "python"
        for _ in range(100):
            try:
                if os.fsencode(str(chemin)) in Path("/proc/%d/cmdline" % enfant.pid).read_bytes():
                    break
            except OSError:
                pass
            time.sleep(0.01)

        rapport = bascule._menager(str(self.base / "venv"), appliquer=True)

        self.assertIsNone(rapport["probleme"])
        self.assertTrue(usage.is_dir(), "le venv du processus vivant a été retiré")
        self.assertEqual(["venv-4444444", "venv-5555555"], sorted(rapport["retires"]))
        self.assertTrue((self.base / bascule.CURRENT).is_symlink())
        self.assertTrue(all(not ancien.exists() for ancien in anciens))

    @staticmethod
    def _arreter(enfant):
        if enfant.poll() is None:
            enfant.kill()
        enfant.wait(timeout=10)

    def test_un_chemin_au_prefixe_proche_ne_garde_pas_le_venv(self):
        courant = faux_venv(self.base, "1111111")
        ancien = faux_venv(self.base, "3333333")
        self.lien(courant.name)
        (self.proc / "4242").mkdir()
        (self.proc / "4242" / "cmdline").write_bytes(
            cmdline(str(self.base / "venv-3333333x" / "bin" / "python")))
        rapport = self.menager(appliquer=True)
        self.assertEqual(["venv-3333333"], rapport["retires"])
        self.assertFalse(ancien.exists())

    # -- ne rien retirer dans le doute --------------------------------------

    def test_sans_lien_courant_rien_n_est_retire(self):
        ancien = faux_venv(self.base, "3333333")
        rapport = self.menager(appliquer=True)
        self.assertIn("venv-courant", rapport["probleme"] or "")
        self.assertEqual([], rapport["retires"])
        self.assertTrue(ancien.is_dir())

    def test_lien_brise_rien_n_est_retire(self):
        ancien = faux_venv(self.base, "3333333")
        self.lien("venv-absente")
        rapport = self.menager(appliquer=True)
        self.assertIn("venv-courant", rapport["probleme"] or "")
        self.assertEqual([], rapport["retires"])
        self.assertTrue(ancien.is_dir())
        self.assertTrue((self.base / bascule.CURRENT).is_symlink())

    def test_processus_illisibles_rien_n_est_retire(self):
        courant = faux_venv(self.base, "1111111")
        ancien = faux_venv(self.base, "3333333")
        self.lien(courant.name)
        (self.proc / "4242").mkdir()
        # un cmdline qui est un répertoire : la lecture échoue sans que le
        # processus ait disparu (EISDIR) -> relevé non concluant
        (self.proc / "4242" / "cmdline").mkdir()
        rapport = self.menager(appliquer=True)
        self.assertIn("processus", rapport["probleme"] or "")
        self.assertEqual([], rapport["retires"])
        self.assertTrue(ancien.is_dir())
        self.assertTrue(self.proc.joinpath("4242").is_dir())

    def test_un_pid_disparu_entre_la_liste_et_la_lecture_est_ignore(self):
        """Un processus terminé pendant le relevé ne rend pas le ménage muet.

        Le PID peut aussi être réutilisé entre `listdir` et `open` : la
        course ne doit pas faire échouer le ménage (défaut vu à la passe
        psycopg du banc, 26/10).
        """
        courant = faux_venv(self.base, "1111111")
        usage = faux_venv(self.base, "3333333")
        (self.proc / "4242").mkdir()
        (self.proc / "4242" / "cmdline").write_bytes(
            cmdline(str(usage / "bin" / "python")))
        self.lien(courant.name)
        vrai = bascule.os.listdir

        def lister(chemin):
            if chemin == str(self.proc):
                return ["4242", "9999"]  # 9999 n'existe pas (course)
            return vrai(chemin)

        with mock.patch.object(bascule.os, "listdir", side_effect=lister):
            chemins = bascule._chemins_proc(str(self.proc))
        self.assertIn(str(usage / "bin" / "python"), chemins)
        self.assertTrue(usage.is_dir())

    def test_un_retrait_en_echec_n_interrompt_pas_les_autres(self):
        """Un candidat qui disparaît pendant le ménage n'interrompt pas les autres."""
        courant = faux_venv(self.base, "1111111")
        premier = faux_venv(self.base, "3333333")
        second = faux_venv(self.base, "4444444")
        self.lien(courant.name)
        vrai = bascule._supprimer

        def supprimer(chemin):
            if Path(chemin) == premier:
                raise FileNotFoundError(str(chemin))
            vrai(chemin)

        with mock.patch.object(bascule, "_supprimer", side_effect=supprimer):
            rapport = self.menager(appliquer=True)
        self.assertEqual(["venv-4444444"], rapport["retires"])
        self.assertEqual(1, len(rapport["erreurs"]))
        self.assertFalse(second.exists())
        self.assertTrue((self.base / bascule.CURRENT).is_symlink())

    def test_un_changement_de_lien_interrompt_le_menage(self):
        """Un lien repointé pendant le ménage arrête les retraits suivants."""
        courant = faux_venv(self.base, "1111111")
        premier = faux_venv(self.base, "3333333")
        dernier = faux_venv(self.base, "4444444")
        self.lien(courant.name)
        vrai = bascule._supprimer
        lien = self.base / bascule.CURRENT

        def supprimer(chemin):
            vrai(chemin)
            if Path(chemin) == premier:
                lien.unlink()
                lien.symlink_to(dernier.name)

        with mock.patch.object(bascule, "_supprimer", side_effect=supprimer):
            rapport = self.menager(appliquer=True)
        self.assertIn("changé", rapport["probleme"] or "")
        self.assertEqual(["venv-3333333"], rapport["retires"])
        self.assertTrue(dernier.is_dir(), "le nouveau courant a été retiré")
        self.assertEqual(dernier.name, os.readlink(lien))

    def test_rapport_precedent_illisible_ne_leve_pas(self):
        courant = faux_venv(self.base, "1111111")
        ancien = faux_venv(self.base, "3333333")
        self.lien(courant.name)
        (self.base / bascule.REPORT).write_bytes(b"\xff\xfe\n")
        rapport = self.menager(appliquer=True)
        self.assertIsNone(rapport["probleme"])
        self.assertEqual(["venv-3333333"], rapport["retires"])
        self.assertFalse(ancien.exists())

    def test_repli_ps_sans_proc_extrait_les_chemins(self):
        """Sans `/proc` (macOS), `ps` fournit les chemins absolus des processus."""
        sortie = SimpleNamespace(stdout="/usr/bin/x /tmp/venv-1111111/bin/python\n")
        with mock.patch.object(bascule.subprocess, "run", return_value=sortie):
            chemins = bascule._chemins_ps()
        self.assertIn("/tmp/venv-1111111/bin/python", chemins)

    # -- liens et fichiers hors candidats -----------------------------------

    def test_ni_le_lien_ni_un_fichier_venv_ne_sont_candidats(self):
        courant = faux_venv(self.base, "1111111")
        garde = faux_venv(self.base, "3333333")
        self.lien(courant.name)
        self.precedent("3333333")
        (self.base / "venv-4444444").symlink_to(garde.name)
        fichier = self.base / "venv-5555555"
        fichier.write_text("pas un venv")
        rapport = self.menager(appliquer=True)
        self.assertEqual([], rapport["retires"])
        self.assertTrue(garde.is_dir())
        self.assertTrue((self.base / "venv-4444444").is_symlink())
        self.assertTrue(fichier.is_file())

    def test_un_repertoire_venv_courant_n_est_jamais_candidat(self):
        """Même un répertoire nommé `venv-courant` n'est jamais candidat.

        C'est le format d'avant le lien (ou un remplacement manuel) : le
        courant n'est pas lisible, donc le ménage ne retire rien du tout.
        """
        courant = faux_venv(self.base, "1111111")
        (self.base / bascule.CURRENT).mkdir()  # format ancien : plus un lien
        rapport = self.menager(appliquer=True)
        self.assertIn("venv-courant", rapport["probleme"] or "")
        self.assertTrue((self.base / bascule.CURRENT).is_dir())
        self.assertTrue(courant.is_dir())
        self.assertEqual([], rapport["retires"])

    # -- le précédent nommé « precedent » (première adoption) ---------------

    def test_le_precedent_precedent_est_garde(self):
        courant = faux_venv(self.base, "1111111")
        precedent = self.base / "venv-precedent"
        precedent.mkdir()
        self.lien(courant.name)
        self.precedent("precedent")
        rapport = self.menager(appliquer=True)
        self.assertIsNone(rapport["probleme"])
        self.assertTrue(precedent.is_dir())
        self.assertEqual([], rapport["retires"])


class CablageActiverTest(unittest.TestCase):
    """`activer` déclenche le ménage, sans jamais faire échouer la bascule."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="menage-3792-activer-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def activer(self, version: str, precedent: str | None = None) -> int:
        arguments = ["activer", "--venv", str(self.base / "venv"), "--version", version]
        if precedent:
            arguments += ["--precedent", precedent]
        with redirect_stdout(io.StringIO()):
            return bascule.main(arguments)

    def test_activer_retire_les_anciens_apres_la_bascule(self):
        faux_venv(self.base, "1111111")
        os.rename(self.base / "venv-1111111", self.base / "venv")
        faux_venv(self.base, "2222222")
        self.assertEqual(0, self.activer("2222222", precedent="1111111"))

        faux_venv(self.base, "3333333")
        faux_venv(self.base, "4444444")
        self.assertEqual(0, self.activer("2222222"))  # déjà actif : ménage aussi

        self.assertEqual("venv-2222222", os.readlink(self.base / bascule.CURRENT))
        self.assertTrue((self.base / "venv-1111111").is_dir())
        self.assertTrue((self.base / "venv-2222222").is_dir())
        self.assertFalse((self.base / "venv-3333333").exists())
        self.assertFalse((self.base / "venv-4444444").exists())

    def test_retour_ne_fait_pas_de_menage(self):
        """Un retour laisse la version quittée et les anciennes pour un examen."""
        faux_venv(self.base, "1111111")
        os.rename(self.base / "venv-1111111", self.base / "venv")
        faux_venv(self.base, "2222222")
        self.assertEqual(0, self.activer("2222222", precedent="1111111"))
        faux_venv(self.base, "3333333")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, bascule.main(
                ["retour", "--venv", str(self.base / "venv")]))
        self.assertEqual("venv-1111111", os.readlink(self.base / bascule.CURRENT))
        self.assertTrue((self.base / "venv-2222222").is_dir())
        self.assertTrue((self.base / "venv-3333333").is_dir())

    def test_une_erreur_de_menage_ne_fait_pas_echouer_la_bascule(self):
        faux_venv(self.base, "1111111")
        os.rename(self.base / "venv-1111111", self.base / "venv")
        faux_venv(self.base, "2222222")
        self.assertEqual(0, self.activer("2222222", precedent="1111111"))
        faux_venv(self.base, "3333333")
        with mock.patch.object(bascule, "_supprimer",
                               side_effect=OSError("disque en lecture seule")), \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(0, self.activer("2222222"))
        self.assertIn("conservé", err.getvalue())
        self.assertTrue((self.base / "venv-3333333").is_dir())
        self.assertEqual("venv-2222222", os.readlink(self.base / bascule.CURRENT))

    def test_une_exception_du_menage_ne_fait_pas_echouer_la_bascule(self):
        faux_venv(self.base, "1111111")
        os.rename(self.base / "venv-1111111", self.base / "venv")
        faux_venv(self.base, "2222222")
        self.assertEqual(0, self.activer("2222222", precedent="1111111"))
        with mock.patch.object(bascule, "_menager",
                               side_effect=RuntimeError("inattendu")), \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(0, self.activer("2222222"))
        self.assertIn("impossible", err.getvalue())
        self.assertEqual("venv-2222222", os.readlink(self.base / bascule.CURRENT))


if __name__ == "__main__":
    unittest.main()
