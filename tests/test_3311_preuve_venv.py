# SPDX-License-Identifier: AGPL-3.0-only
"""#3311 — `poste.sh controler` prouve le venv de chaque exécuteur vivant.

Incident du 24/10/2026 (agents-2) : le lanceur `ExecStart` est stable (#1969),
donc `venv-courant` avait basculé sur le nouveau code alors que quatorze
exécuteurs déjà lancés tournaient encore l'ancien venv. `controler` ne lisait
que le venv du CLI : l'hôte passait pour sain. Le témoin lit l'`argv[0]`
concret de chaque MainPID (comme le helper `venv-processus.py`) : un processus
sur l'ancien venv fait échouer le contrôle. Même preuve côté serveur
(`vm.sh controler`, unités `ameesh-runner@<p>`).
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ameesh import platform

from tests import support  # noqa: F401 — environnement isolé du poste

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/mise-a-jour/poste.sh"

SYSTEMCTL = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
state = Path(os.environ['TEST_STATE'])
args = sys.argv[1:]
with (state / 'systemctl.log').open('a') as f:
    f.write(' '.join(args) + '\n')
if 'list-units' in args:
    # #3311 : la liste réelle des unités actives du poste.
    if any('ameesh-runner-agent@' in a for a in args):
        path = state / 'units'
        if path.exists():
            print(path.read_text().strip())
    sys.exit(0)
if 'show' in args and 'MainPID' in args:
    unit = args[-1]
    if unit == 'ameesh-notify.service':
        print((state / 'notify.pid').read_text().strip())
    else:
        name = unit.split('@')[1].removesuffix('.service')
        print((state / ('pid-' + name)).read_text().strip())
    sys.exit(0)
sys.exit(0)
'''

AMEESH = '#!/bin/sh\nexit 0\n'


@unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
class ControlerVenvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-3311-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.share = self.home / ".local/share/ameesh"
        self.venv = self.share / "venv"          # venv stable (ExecStart)
        self.live = self.share / "venv-aaaa1111"
        self.old = self.share / "venv-bbbb2222"
        for venv in (self.venv, self.live, self.old):
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").symlink_to(sys.executable)
        (self.share / "venv-courant").symlink_to(self.live.name)
        (self.venv / "bin/ameesh").write_text(AMEESH, encoding="utf-8")
        (self.venv / "bin/ameesh").chmod(0o755)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "units").write_text(
            "ameesh-runner-agent@dev9.service loaded active running runner\n"
            "ameesh-runner-agent@dev8.service loaded active running runner\n",
            encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        for nom, texte in (("systemctl", SYSTEMCTL),):
            chemin = self.bin / nom
            chemin.write_text(texte, encoding="utf-8")
            chemin.chmod(0o755)
        self.config = self.root / "bascule.env"
        self.config.write_text("VM=10.77.0.2\n", encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home),
                        BASCULE_ENV=str(self.config), TEST_STATE=str(self.state),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"])

    def processus(self, venv):
        """Un MainPID vivant dont l'argv[0] est le python concret du venv."""
        enfant = subprocess.Popen([str(venv / "bin/python"), "-c",
                                    "import time; time.sleep(60)"])
        self.addCleanup(self.arreter, enfant)
        return enfant.pid

    @staticmethod
    def arreter(enfant):
        if enfant.poll() is None:
            enfant.terminate()
        enfant.communicate(timeout=5)

    def lancer(self, pids):
        """pids : {agent: pid} ; notify tourne sur le venv courant."""
        (self.state / "notify.pid").write_text(str(self.processus(self.live)) + "\n")
        for agent, pid in pids.items():
            (self.state / ("pid-" + agent)).write_text(str(pid) + "\n")
        return subprocess.run(["bash", str(SCRIPT), "controler"], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_controler_vert_si_chaque_executeur_vit_sur_le_venv_courant(self):
        fait = self.lancer({"dev9": self.processus(self.live),
                            "dev8": self.processus(self.live)})
        self.assertEqual(fait.returncode, 0, fait.stdout + fait.stderr)
        self.assertIn("contrôles verts", fait.stdout)
        self.assertEqual(fait.stdout.count("MainPID vivant sur le venv courant"), 3)

    def test_controler_rouge_si_un_executeur_reste_sur_l_ancien_venv(self):
        fait = self.lancer({"dev9": self.processus(self.old),
                            "dev8": self.processus(self.live)})
        self.assertNotEqual(fait.returncode, 0)
        self.assertIn("ameesh-runner-agent@dev9.service : venv courant non prouvé",
                      fait.stderr)
        self.assertIn("dev8", fait.stdout)  # l'autre exécuteur reste sain
        self.assertNotIn("contrôles verts", fait.stdout)

    def test_non_dumpable_demande_preuve_root_sans_accepter_cmdline_seule(self):
        enfant = subprocess.Popen(
            [str(self.live / "bin/python"), "-c",
             "import ctypes,time; "
             "assert ctypes.CDLL(None).prctl(4,0,0,0,0)==0; "
             "print('ready',flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.arreter, enfant)
        self.assertEqual(enfant.stdout.readline().strip(), "ready")
        preuve = subprocess.run(
            [sys.executable, str(ROOT / "deploy/mise-a-jour/venv-processus.py"),
             str(enfant.pid), str(self.venv), "non-dumpable"],
            capture_output=True, text=True, timeout=10)
        # Le noyau refuse exe malgré la cmdline du bon venv.
        self.assertEqual(preuve.returncode, 3, preuve.stdout + preuve.stderr)
        sudo = self.bin / "sudo"
        sudo.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$TEST_STATE/sudo.log\"\nexit 1\n")
        sudo.chmod(0o755)
        fait = self.lancer({"dev9": enfant.pid})
        self.assertNotEqual(fait.returncode, 0)
        self.assertIn("-n python3", (self.state / "sudo.log").read_text())
        self.assertNotIn("contrôles verts", fait.stdout)
        # Un ancien venv est toujours refusé avant tout accès privilégié.
        courant = self.share / "venv-courant"
        courant.unlink()
        courant.symlink_to(self.old.name)
        mauvais = subprocess.run(
            [sys.executable, str(ROOT / "deploy/mise-a-jour/venv-processus.py"),
             str(enfant.pid), str(self.old), "ancien"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(mauvais.returncode, 1)

    def test_controler_rouge_si_un_mainpid_est_absent(self):
        for pid in ("0", "  "):
            with self.subTest(pid=pid):
                fait = self.lancer({"dev9": pid, "dev8": self.processus(self.live)})
                self.assertNotEqual(fait.returncode, 0)
                self.assertIn("venv courant non prouvé", fait.stderr)
                self.assertNotIn("contrôles verts", fait.stdout)


SSH = r'''#!/usr/bin/env python3
import os, subprocess, sys
from pathlib import Path
state = Path(os.environ['TEST_STATE'])
command = sys.argv[-1]
with (state / 'ssh.log').open('a') as f:
    f.write(command + '\n')
if 'MainPID' in command:
    print((state / 'pid').read_text().strip())
    sys.exit(0)
if 'python3 -' in command:
    # #3311 : le helper arrive sur stdin ; le venv stable de la VM est simulé.
    command = command.replace('/opt/ameesh/venv', os.environ['TEST_VENV'])
    sys.exit(subprocess.run(['bash', '-c', command]).returncode)
if 'ameesh doctor' in command:
    print('doctor ok')
    sys.exit(0)
sys.exit(0)
'''


@unittest.skipUnless(platform.is_linux(), "preuve du venv vivant via /proc : Linux")
class ControlerServeurTest(unittest.TestCase):
    """`vm.sh controler` (mode serveur) prouve le venv de `ameesh-runner@<p>`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-3311-srv-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".local/share/ameesh/venv/bin").mkdir(parents=True)
        ameesh = self.home / ".local/share/ameesh/venv/bin/ameesh"
        ameesh.write_text(AMEESH, encoding="utf-8")
        ameesh.chmod(0o755)
        self.vmroot = self.root / "vm"
        self.venv = self.vmroot / "venv"
        self.live = self.vmroot / "venv-aaa1111"
        self.old = self.vmroot / "venv-bbbb2222"
        (self.venv / "bin").mkdir(parents=True)
        for venv in (self.live, self.old):
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").symlink_to(sys.executable)
        (self.vmroot / "venv-courant").symlink_to(self.live.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        ssh = self.bin / "ssh"
        ssh.write_text(SSH, encoding="utf-8")
        ssh.chmod(0o755)
        self.config = self.root / "bascule.env"
        self.config.write_text("VM=10.77.0.2\nPERSONAS_VM=dev9\n",
                               encoding="utf-8")
        self.env = dict(os.environ, HOME=str(self.home),
                        BASCULE_ENV=str(self.config), TEST_STATE=str(self.state),
                        TEST_VENV=str(self.venv),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"])

    def processus(self, venv):
        enfant = subprocess.Popen([str(venv / "bin/python"), "-c",
                                    "import time; time.sleep(60)"],
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.addCleanup(self.arreter, enfant)
        return enfant.pid

    @staticmethod
    def arreter(enfant):
        if enfant.poll() is None:
            enfant.terminate()
        enfant.communicate(timeout=5)

    def controler(self, pid):
        (self.state / "pid").write_text(str(pid) + "\n")
        return subprocess.run(["bash", str(ROOT / "deploy/mise-a-jour/vm.sh"),
                               "controler"], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def test_controler_vert_si_le_runner_serveur_vit_sur_le_venv_courant(self):
        fait = self.controler(self.processus(self.live))
        self.assertEqual(fait.returncode, 0, fait.stdout + fait.stderr)
        self.assertIn("MainPID vivant sur le venv courant", fait.stdout)
        self.assertIn("contrôles verts", fait.stdout)

    def test_controler_rouge_si_le_runner_serveur_reste_sur_l_ancien_venv(self):
        fait = self.controler(self.processus(self.old))
        self.assertNotEqual(fait.returncode, 0)
        self.assertIn("venv courant non prouvé", fait.stderr)
        self.assertNotIn("contrôles verts", fait.stdout)

    def test_controler_rouge_si_le_mainpid_serveur_est_invalide(self):
        fait = self.controler("0")
        self.assertNotEqual(fait.returncode, 0)
        self.assertIn("venv courant non prouvé", fait.stderr)
        self.assertNotIn("contrôles verts", fait.stdout)


if __name__ == "__main__":
    unittest.main()
