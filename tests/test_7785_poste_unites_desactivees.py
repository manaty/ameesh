# SPDX-License-Identifier: AGPL-3.0-only
"""#7785 : le poste liste les unités runner volontairement désactivées.

La liste vient de systemd (`list-unit-files`), le nom de la persona de
`ExecStart`, et le registre dit si elle est encore à mener : une unité
désactivée d'un agent arrêté, retiré ou non-exécutant ne doit jamais être
tolérée par la santé staging. Un registre illisible n'est pas deviné.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import support  # noqa: F401 — environnement isolé du poste


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/mise-a-jour/poste.sh"

SYSTEMCTL = '''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_LOG"
case "$*" in
  *list-unit-files*)
    cat "$TEST_UNITES" ;;
  *"show -p ExecStart"*)
    for a in "$@"; do unite="$a"; done
    nom="${unite#ameesh-runner-agent@}"
    printf 'argv[]=/usr/bin/python /x/runner --agents %s ;\\n' "${nom%.service}" ;;
esac
'''

AMEESH = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
registre = json.loads(Path(os.environ["TEST_REGISTRE"]).read_text())
if sys.argv[1] == "show":
    row = registre.get(sys.argv[2])
    if row is None:
        sys.exit(1)
    print(row if isinstance(row, str) else json.dumps(row))
    sys.exit(0)
sys.exit(2)
'''


class PosteUnitesDesactiveesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="poste-7785-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.venv = self.home / ".local/share/ameesh/venv"
        (self.venv / "bin").mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        for nom, source in (("systemctl", SYSTEMCTL), ("ameesh", AMEESH)):
            chemin = self.venv / "bin" / nom if nom == "ameesh" else self.bin / nom
            chemin.write_text(source)
            chemin.chmod(0o755)
        (self.root / "bascule.env").write_text("VM=simulation\n")
        self.log = self.root / "systemctl.log"
        self.unites = self.root / "unites"
        self.registre = self.root / "registre.json"
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        BASCULE_ENV=str(self.root / "bascule.env"),
                        TEST_LOG=str(self.log), TEST_UNITES=str(self.unites),
                        TEST_REGISTRE=str(self.registre))

    def run_poste(self):
        return subprocess.run(["bash", str(SCRIPT), "unites-desactivees"],
                              env=self.env, capture_output=True, text=True,
                              timeout=30)

    def test_liste_les_seules_personas_encore_a_mener(self):
        self.unites.write_text(
            "ameesh-runner-agent@dev8.service disabled\n"
            "ameesh-runner-agent@dev9.service masked\n"
            "ameesh-runner-agent@dev7.service enabled\n"
            "ameesh-runner-agent@retire.service disabled\n"
            "ameesh-runner-agent@observe.service disabled\n"
            "ameesh-runner-agent@illisible.service disabled\n")
        self.registre.write_text(
            '{"dev8": {"name": "dev8", "status": "idle", "mode": "execute"},'
            ' "dev9": {"name": "dev9", "status": "blocked", "mode": "execute"},'
            ' "dev7": {"name": "dev7", "status": "idle", "mode": "execute"},'
            ' "retire": {"name": "retire", "status": "idle", "mode": "execute",'
            '            "retired_ts": 900.0},'
            ' "observe": {"name": "observe", "status": "idle", "mode": "observe"},'
            ' "illisible": "pas du json"}')
        p = self.run_poste()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout.split(), ["dev8", "dev9"])

    def test_aucune_unite_desactivee_rend_une_sortie_vide(self):
        self.unites.write_text(
            "ameesh-runner-agent@dev8.service enabled\n"
            "ameesh-runner-agent@dev9.service enabled\n")
        self.registre.write_text(
            '{"dev8": {"name": "dev8", "status": "idle", "mode": "execute"}}')
        p = self.run_poste()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(p.stdout, "")


if __name__ == "__main__":
    unittest.main()
