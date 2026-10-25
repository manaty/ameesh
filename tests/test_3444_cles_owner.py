# SPDX-License-Identifier: AGPL-3.0-only
"""#3444 — les clés SSH de l'owner sortent des exécuteurs d'agent (poste).

Décision owner #133454 a : la clé SSH root de l'owner vers les VM de CI n'est
plus atteignable depuis une session d'agent. Ce livrable est un **garde-fou**
(le même uid peut le contourner), pas la frontière : la frontière reste un
lot séparé sur décision owner.

* le gabarit d'unité retire l'agent SSH de l'environnement de l'exécuteur
  (`UnsetEnvironment=SSH_AUTH_SOCK SSH_AGENT_PID`) ;
* le drop-in OPT-IN `deploy/poste/cles-owner.conf` cache `~/.ssh`
  (`InaccessiblePaths=%h/.ssh`) et refuse le démarrage si le mount namespace
  n'a pas été posé (témoin) ;
* le témoin exige un MONTAGE sur le chemin (`/proc/self/mountinfo`), pas un
  simple répertoire vide (relecture cx1 B1) ;
* le profil AppArmor rend à `systemd-executor` le droit de créer un espace de
  noms utilisateur, sans quoi systemd ignore SILENCIEUSEMENT
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
DROPIN = os.path.join(POSTE, "cles-owner.conf")
DROPIN_GABARIT = os.path.join(SYSTEMD, "ameesh-runner-agent@.service.d", "cles-owner.conf")
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
        # installable partout. Le drop-in est OPT-IN, sous deploy/poste/.
        service = _service(UNITE)
        self.assertIsNone(service.get("ExecStartPre"))
        self.assertIsNone(service.get("InaccessiblePaths"))

    def test_le_dropin_n_est_pas_dans_le_gabarit(self):
        self.assertFalse(os.path.exists(DROPIN_GABARIT))


class DropInTest(unittest.TestCase):
    def test_dropin_opt_in(self):
        with open(DROPIN, encoding="utf-8") as fh:
            texte = fh.read()
        self.assertIn("GARDE-FOU", texte)
        self.assertIn("OPT-IN", texte)
        self.assertIn("ne satisfait PAS seul la décision", texte)
        self.assertIn("NE PAS POSER CE FICHIER AVANT", texte)

    def test_cache_ssh_seulement(self):
        service = _service(DROPIN)
        # Sans `-` : un chemin absent ne serait pas masqué (le témoin refuse).
        self.assertEqual(_mots(service.get("InaccessiblePaths")), ["%h/.ssh"])

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

    def test_repertoire_vide_sans_montage_refuse(self):
        # Relecture cx1 B1 : un répertoire vide accessible n'est PAS une
        # preuve d'isolation — une clé déposée plus tard serait lisible.
        with tempfile.TemporaryDirectory() as tmp:
            sortie = self._run(tmp)
            self.assertEqual(sortie.returncode, 1)
            self.assertIn("aucun montage", sortie.stderr)

    def test_repertoire_non_vide_sans_montage_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "id_test"), "w", encoding="utf-8") as fh:
                fh.write("canari\n")
            self.assertEqual(self._run(tmp).returncode, 1)

    def test_chemin_absent_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(os.path.join(tmp, "absent")).returncode, 1)

    def test_fichier_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            chemin = os.path.join(tmp, "id_test")
            with open(chemin, "w", encoding="utf-8") as fh:
                fh.write("canari\n")
            self.assertEqual(self._run(chemin).returncode, 1)

    def test_montage_reel_conforme(self):
        # Le positif : un vrai mount namespace (bwrap a le profil userns du
        # train) pose un tmpfs sur le chemin ; le témoin doit alors passer.
        bwrap = shutil.which("bwrap")
        if not bwrap:
            self.skipTest("bwrap absent")
        with tempfile.TemporaryDirectory() as tmp:
            cible = os.path.join(tmp, "ssh")
            os.makedirs(cible)
            with open(os.path.join(cible, "id_test"), "w", encoding="utf-8") as fh:
                fh.write("canari\n")
            proc = subprocess.run(
                [bwrap, "--dev-bind", "/", "/", "--tmpfs", cible, TEMOIN, cible],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_le_temoin_regarde_les_montages(self):
        with open(TEMOIN, encoding="utf-8") as fh:
            texte = fh.read()
        self.assertIn("/proc/self/mountinfo", texte)


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
        # Sans `-` : la cible doit exister, sinon la preuve ne prouve rien.
        self.assertIn('--property="InaccessiblePaths=$CIBLE"', texte)
        self.assertIn("UnsetEnvironment=SSH_AUTH_SOCK", texte)
        self.assertIn(HELPER_INSTALLE, texte)
        self.assertIn("cible absente", texte)


class DocumentationTest(unittest.TestCase):
    def test_la_procedure_est_documentee(self):
        with open(DOCS, encoding="utf-8") as fh:
            texte = fh.read()
        for attendu in ("## Clés SSH de l'owner et exécuteurs du poste (#3444) — garde-fou",
                        "garde-fou, pas la frontière demandée",
                        "pas** déclaré\nfait sur cette seule base",
                        HELPER_INSTALLE,
                        "deploy/poste/cles-owner.conf",
                        "deploy/poste/preuve-isolation-cles-owner.sh",
                        "deploy/poste/ameesh-systemd-executor.apparmor",
                        "~/.config/ameesh/cles-ci/",
                        "ne jamais l'installer avant le prérequis",
                        "/proc/self/mountinfo",
                        "panier (§12)",
                        "sudo apparmor_parser -r /etc/apparmor.d/ameesh-systemd-executor"):
            self.assertIn(attendu, texte)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
