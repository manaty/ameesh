# SPDX-License-Identifier: AGPL-3.0-only
"""#6228 : les dossiers temporaires du banc ne survivent pas au processus.

Constat AG2 du 07/11/2026 : le TMPDIR de `paquet-auto`
(`/home/ameesh/.cache/ameesh/paquet-auto`) gardait ~3 400 dossiers
`ameesh-test-*` par jour, huit par processus de test important
`tests.support`.

Le nettoyage est un `atexit` borné aux dossiers que CE processus a créés :
ils restent en place pendant toute la suite (leur rôle d'isolement ne
change pas) et partent à la sortie ; un dossier fourni par l'appelant
(`TMPDIR` et son contenu) n'est jamais touché.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: instantané des huit chemins que le banc crée à l'import, et de leur
#: présence PENDANT le processus (la preuve que le rôle d'isolement reste).
PROBE = r"""
import json, os
from tests import support
paths = {
    "config": support.EMPTY_CONFIG,
    "codex_home": support.EMPTY_CODEX_HOME,
    "xdg_state": support.EMPTY_XDG_STATE,
    "harnesses": support.EMPTY_HARNESS_DIR,
    "prices": support.EMPTY_PRICES,
    "system_tmp": support.EMPTY_SYSTEM_TMP,
    "power": support.EMPTY_POWER_SUPPLY,
    "usage": support.EMPTY_USAGE_WIDGET,
}


def created_dir(path):
    # EMPTY_CONFIG et EMPTY_PRICES sont des fichiers DANS un dossier créé
    return os.path.dirname(path) if path.endswith(".json") else path


print(json.dumps({name: {"dir": created_dir(path),
                         "present": os.path.isdir(created_dir(path))}
                  for name, path in paths.items()}))
"""


class SupportTempdirsTest(unittest.TestCase):
    """Un processus qui importe le banc ne laisse rien dans le TMPDIR."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-test-6228-")
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = os.path.realpath(self.tmp.name)
        self.sentinel = os.path.join(self.tmpdir, "dossier-de-l-appelant")
        os.makedirs(self.sentinel)
        with open(os.path.join(self.tmpdir, "fichier-de-l-appelant"),
                  "w", encoding="utf-8") as fh:
            fh.write("x\n")

    def probe(self) -> dict:
        env = {name: value for name, value in os.environ.items()
               if not name.startswith(("AMEESH_", "AGENT_MESH_", "AGENT_MAIL_"))}
        env.update(TMPDIR=self.tmpdir,
                   PYTHONPATH=os.pathsep.join((REPO, os.path.join(REPO, "src"))))
        proc = subprocess.run([sys.executable, "-c", PROBE], capture_output=True,
                              text=True, timeout=60, env=env, cwd=REPO)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_import_ne_laisse_aucun_dossier_du_banc(self):
        paths = self.probe()
        self.assertEqual(sorted(paths), [
            "codex_home", "config", "harnesses", "power", "prices",
            "system_tmp", "usage", "xdg_state"])
        for name, entry in paths.items():
            # créé et présent pendant le processus : l'isolement est intact
            self.assertTrue(entry["present"], name)
            self.assertTrue(entry["dir"].startswith(self.tmpdir + os.sep),
                            entry["dir"])
        # ... et aucun dossier du banc ne survit à sa sortie
        restants = sorted(name for name in os.listdir(self.tmpdir)
                          if name.startswith("ameesh-test-"))
        self.assertEqual(restants, [])
        # le TMPDIR fourni par l'appelant, lui, est intact
        self.assertTrue(os.path.isdir(self.sentinel))
        self.assertTrue(os.path.isfile(
            os.path.join(self.tmpdir, "fichier-de-l-appelant")))


if __name__ == "__main__":
    unittest.main()
