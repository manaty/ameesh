# SPDX-License-Identifier: AGPL-3.0-only
"""#3444 — les clés SSH de l'owner sortent des exécuteurs d'agent (poste).

Décision owner #133454 a : la clé SSH root de l'owner vers les VM de CI n'est
plus atteignable depuis une session d'agent. Deux pièces, plus un prérequis
hôte :

* le gabarit d'unité retire l'agent SSH de l'environnement de l'exécuteur
  (`UnsetEnvironment=SSH_AUTH_SOCK SSH_AGENT_PID`) ;
* le drop-in du poste cache `~/.ssh` (`InaccessiblePaths=-%h/.ssh`) et
  refuse le démarrage si le mount namespace n'a pas été posé (témoin) ;
* le profil AppArmor rend à `systemd-executor` le droit de créer un espace
  de noms utilisateur, sans quoi systemd ignore SILENCIEUSEMENT
  `InaccessiblePaths=` sur Ubuntu 24.04 (constat sur agents-2, 25/10/2026).

Sans base : lecture des fichiers du dépôt et exécution du témoin.
"""
from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import tempfile
import unittest

from .support import REPO

SYSTEMD = os.path.join(REPO, "deploy", "systemd")
POSTE = os.path.join(REPO, "deploy", "poste")
UNITE = os.path.join(SYSTEMD, "ameesh-runner-agent@.service")
DROPIN = os.path.join(SYSTEMD, "ameesh-runner-agent@.service.d", "cles-owner.conf")
TEMOIN = os.path.join(POSTE, "verifie-cles-owner")
PROFIL = os.path.join(POSTE, "ameesh-systemd-executor.apparmor")
PREUVE = os.path.join(POSTE, "preuve-isolation-cles-owner.sh")
DOCS = os.path.join(REPO, "docs", "EXPLOITATION.md")
HELPER_INSTALLE = "/usr/local/libexec/ameesh/verifie-cles-owner"


def _service(chemin: str) -> configparser.SectionProxy:
    unite = configparser.ConfigParser(strict=False, interpolation=None)
    with open(chemin, encoding="utf-8") as fh:
        unite.read_string(fh.read())
    return unite["Service"]


def _mots(valeur: str | None) -> list[str]:
    return (valeur or "").split()


class GabaritTest(unittest.TestCase):
    """Le gabarit, commun à tous les hôtes, ne dépend pas du poste."""

    def test_l_agent_ssh_n_entre_pas_dans_l_executeur(self):
        self.assertEqual(set(_mots(_service(UNITE).get("UnsetEnvironment"))),
                         {"SSH_AUTH_SOCK", "SSH_AGENT_PID"})

    def test_applique_apres_environment_file(self):
        # UnsetEnvironment est appliqué en dernier par systemd : le placer
        # après EnvironmentFile rend la garantie lisible dans le fichier.
        with open(UNITE, encoding="utf-8") as fh:
            texte = fh.read()
        self.assertLess(texte.index("EnvironmentFile="),
                        texte.index("UnsetEnvironment="))

    def test_le_gabarit_n_exige_ni_temoin_ni_inaccessible_paths(self):
        # agents-1/2 n'ont pas le binaire du poste : le gabarit doit rester
        # installable partout (le drop-in est ce qui est propre au poste).
        service = _service(UNITE)
        self.assertIsNone(service.get("ExecStartPre"))
        self.assertIsNone(service.get("InaccessiblePaths"))


class DropInTest(unittest.TestCase):
    def test_cache_ssh_seulement(self):
        service = _service(DROPIN)
        chemins = _mots(service.get("InaccessiblePaths"))
        self.assertEqual(chemins, ["-%h/.ssh"])
        # ~/.gnupg n'est pas caché : les sauvegardes de session en dépendent.
        self.assertNotIn("%h/.gnupg", chemins)

    def test_retire_aussi_l_agent_ssh(self):
        self.assertEqual(set(_mots(_service(DROPIN).get("UnsetEnvironment"))),
                         {"SSH_AUTH_SOCK", "SSH_AGENT_PID"})

    def test_le_temoin_echoue_ferme(self):
        lignes = [l for l in (_service(DROPIN).get("ExecStartPre") or "").splitlines() if l]
        self.assertEqual(len(lignes), 1)
        self.assertTrue(lignes[0].startswith(HELPER_INSTALLE + " "), lignes[0])
        self.assertIn("%h/.ssh", lignes[0])
        # Sans préfixe `-` : un échec du témoin empêche le démarrage.
        self.assertFalse(lignes[0].lstrip().startswith("-"))


class TemoinTest(unittest.TestCase):
    def _run(self, *chemins: str) -> subprocess.CompletedProcess:
        return subprocess.run([TEMOIN, *chemins], capture_output=True, text=True)

    def test_absent_ou_vide_conforme(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(os.path.join(tmp, "absent")).returncode, 0)
            vide = os.path.join(tmp, "vide")
            os.makedirs(vide)
            self.assertEqual(self._run(vide).returncode, 0)

    def test_repertoire_non_vide_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "id_test"), "w", encoding="utf-8") as fh:
                fh.write("canari\n")
            sortie = self._run(tmp)
            self.assertEqual(sortie.returncode, 1)
            self.assertIn("isolation absente", sortie.stderr)

    def test_fichier_lisible_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            chemin = os.path.join(tmp, "id_test")
            with open(chemin, "w", encoding="utf-8") as fh:
                fh.write("canari\n")
            sortie = self._run(chemin)
            self.assertEqual(sortie.returncode, 1)
            self.assertIn("encore lisible", sortie.stderr)

    def test_illisible_conforme(self):
        # Le mount ns de systemd peut rendre le chemin illisible (tmpfs 000)
        # au lieu de le remplacer par un répertoire vide : conforme aussi.
        if os.geteuid() == 0:
            self.skipTest("root lit un répertoire 000")
        with tempfile.TemporaryDirectory() as tmp:
            chemin = os.path.join(tmp, "cache")
            os.makedirs(chemin)
            try:
                os.chmod(chemin, 0)
                self.assertEqual(self._run(chemin).returncode, 0)
            finally:
                os.chmod(chemin, 0o700)


class ProfilAppArmorTest(unittest.TestCase):
    def test_profil_unconfined_avec_userns(self):
        with open(PROFIL, encoding="utf-8") as fh:
            lignes = [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]
        texte = "\n".join(lignes)
        self.assertIn("profile ameesh-systemd-executor "
                      "/usr/lib/systemd/systemd-executor flags=(unconfined)", texte)
        self.assertIn("userns,", texte)
        # Sans `allow all,`, un empilement avec unprivileged_userns rend le
        # label (mixed) et refuse tout le réseau (Ubuntu #2168612).
        self.assertIn("allow all,", texte)
        # Même forme que le profil bwrap posé par le train (#2381).
        self.assertIn("abi <abi/4.0>", texte)

    def test_le_profil_compile(self):
        parser = shutil.which("apparmor_parser") or "/sbin/apparmor_parser"
        if not os.access(parser, os.X_OK):
            self.skipTest("apparmor_parser absent")
        with tempfile.TemporaryDirectory() as cache:
            compile_ = subprocess.run(
                [parser, "-Q", "-T", "-W", "--cache-loc", cache,
                 "-o", os.devnull, PROFIL], capture_output=True, text=True)
        if compile_.returncode and "Permission denied" in compile_.stderr:
            self.skipTest("apparmor_parser sans droits sur ce banc")
        self.assertEqual(compile_.returncode, 0, compile_.stderr)


class PreuveTest(unittest.TestCase):
    def test_script_valide_et_memes_proprietes(self):
        subprocess.run(["sh", "-n", PREUVE], check=True)
        with open(PREUVE, encoding="utf-8") as fh:
            texte = fh.read()
        self.assertIn('--property="InaccessiblePaths=-$CIBLE"', texte)
        self.assertIn("UnsetEnvironment=SSH_AUTH_SOCK", texte)
        self.assertIn(HELPER_INSTALLE, texte)


class DocumentationTest(unittest.TestCase):
    def test_la_procedure_est_documentee(self):
        with open(DOCS, encoding="utf-8") as fh:
            texte = fh.read()
        for attendu in ("## Clés SSH de l'owner et exécuteurs du poste (#3444)",
                        HELPER_INSTALLE,
                        "deploy/poste/preuve-isolation-cles-owner.sh",
                        "deploy/poste/ameesh-systemd-executor.apparmor",
                        "~/.config/ameesh/cles-ci/",
                        "sudo apparmor_parser -r /etc/apparmor.d/ameesh-systemd-executor"):
            self.assertIn(attendu, texte)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
