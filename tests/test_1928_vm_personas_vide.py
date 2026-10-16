# SPDX-License-Identifier: AGPL-3.0-only
"""#1928 — `vm.sh` (mode serveur) : une liste `PERSONAS_VM` explicitement vide
est admise (hôte sans exécutant, la configuration que `staging.sh` écrit),
alors qu'une variable absente ou une `VM` vide restent refusées avant tout
geste.

Le script réel est lancé avec un `BASCULE_ENV` temporaire et un faux `ssh` qui
journalise chaque commande distante et rend le minimum que lisent les gardes
(migrations présentes) : la liste vide ne tente aucune unité `ameesh-runner@`,
et aucune machine n'est touchée.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/mise-a-jour/vm.sh"

FAKE_SSH = r'''#!/usr/bin/env python3
"""Faux ssh : journalise la commande distante, rend des migrations présentes,
ne touche aucune machine."""
import os
import sys

cmd = sys.argv[-1]
with open(os.environ["FAKE_SSH_LOG"], "a") as journal:
    journal.write(cmd.replace("\n", " ") + "\n")
if "ls-tree --name-only" in cmd:
    print("src/ameesh/migrations/0001_init.sql")
elif "schema_migrations" in cmd:
    print("1")
elif "rev-parse" in cmd:
    print("0" * 40)
sys.exit(0)
'''


class PersonasVmVideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        ssh = self.bin / "ssh"
        ssh.write_text(FAKE_SSH)
        ssh.chmod(ssh.stat().st_mode | stat.S_IEXEC)
        self.log = self.dir / "ssh.log"

    def _env(self, contenu: str) -> dict:
        chemin = self.dir / "bascule.env"
        chemin.write_text(contenu)
        return dict(os.environ, BASCULE_ENV=str(chemin), HOME=str(self.home),
                    FAKE_SSH_LOG=str(self.log),
                    PATH="%s:%s" % (self.bin, os.environ["PATH"]))

    def _vm(self, *args: str, env: dict):
        return subprocess.run(["bash", str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def _appels(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def test_liste_vide_definie_acceptee_sans_service(self):
        """Vide défini : verifier, controler et redemarrer passent la garde et
        ne tentent aucun service de persona."""
        for action in ("verifier", "controler", "redemarrer"):
            with self.subTest(action=action):
                self.log.unlink(missing_ok=True)
                proc = self._vm(action, env=self._env("VM=10.77.0.2\nPERSONAS_VM=\n"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("PERSONAS_VM manquant", proc.stderr)
                self.assertNotIn("ameesh-runner@", self._appels())

    def test_variable_absente_refusee(self):
        """Variable absente : refus avant tout geste, message explicite."""
        proc = self._vm("controler", env=self._env("VM=10.77.0.2\n"))
        # Le refus vient de l'expansion `${PERSONAS_VM?…}` : bash sort en 1
        # (non interactif) ; seul le fait de refuser et le message comptent.
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PERSONAS_VM manquant", proc.stderr)
        self.assertNotIn("ameesh-runner@", self._appels())

    def test_vm_vide_refusee(self):
        """VM vide : toujours refusée, même avec PERSONAS_VM vide défini."""
        proc = self._vm("controler", env=self._env("VM=\nPERSONAS_VM=\n"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("VM manquant", proc.stderr)
        self.assertNotIn("ameesh-runner@", self._appels())


if __name__ == "__main__":
    unittest.main()
