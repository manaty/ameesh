# SPDX-License-Identifier: AGPL-3.0-only
"""#1305 — les dumps de sauvegarde ne sont lisibles que par ameesh.

Le vrai `deploy/serveur/sauvegarder-base.sh` est exécuté, avec un faux
`pg_dump` qui se comporte comme le vrai sur le point qui compte : il tronque le
fichier existant sans changer son mode et note le mode qu'il voit à l'ouverture.
Le répertoire des dumps est redirigé par `AMEESH_SAUVEGARDES_DIR`, positionnée
ici seulement (le service systemd ne la connaît pas). Aucune base, aucun réseau.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "serveur", "sauvegarder-base.sh")

FAKE_PG_DUMP = '''#!/usr/bin/env python3
"""Faux pg_dump : note le mode vu à l'ouverture, puis écrit le dump."""
import os
import sys

args = sys.argv[1:]
chemin = args[args.index("-f") + 1]
try:
    mode = "%o" % (os.stat(chemin).st_mode & 0o7777)
except FileNotFoundError:
    mode = "absent"
with open(os.path.join(os.environ["FAKE_STATE"], "mode-ouverture"), "w") as fh:
    fh.write(mode)
with open(chemin, "w") as fh:  # tronque sans changer le mode, comme pg_dump
    fh.write("dump %s\\n" % chemin)
'''

FAKE_CURL = '''#!/bin/sh
exit 0
'''


class SauvegardesPriveesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1305-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dumps = os.path.join(self.tmp, "sauvegardes")
        self.state = os.path.join(self.tmp, "state")
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.state)
        os.makedirs(fakebin, mode=0o755)
        for nom, contenu in (("pg_dump", FAKE_PG_DUMP), ("curl", FAKE_CURL)):
            chemin = os.path.join(fakebin, nom)
            with open(chemin, "w", encoding="utf-8") as fh:
                fh.write(contenu)
            os.chmod(chemin, 0o755)
        self.host = self.sortie("hostname")
        self.jour = self.sortie("date", "+%F")
        self.env = dict(os.environ, FAKE_STATE=self.state,
                        AMEESH_SAUVEGARDES_DIR=self.dumps,
                        PATH=fakebin + os.pathsep + os.environ.get("PATH", ""))
        # umask permissif de l'appelant : le script doit imposer le sien
        ancien = os.umask(0o022)
        self.addCleanup(os.umask, ancien)

    def sortie(self, *commande):
        return subprocess.run(commande, capture_output=True, text=True,
                              check=True).stdout.strip()

    def run_script(self):
        return subprocess.run(["/bin/sh", SCRIPT], capture_output=True,
                              text=True, env=self.env, timeout=60)

    def mode(self, chemin):
        return stat.S_IMODE(os.stat(chemin).st_mode)

    def dump_du_jour(self):
        return os.path.join(self.dumps,
                            "ameesh-%s-%s.dump" % (self.host, self.jour))

    def dump_ancien(self, nom, contenu, age_jours=0):
        chemin = os.path.join(self.dumps, nom)
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write(contenu)
        os.chmod(chemin, 0o644)
        if age_jours:
            vieux = os.stat(chemin).st_mtime - age_jours * 86400
            os.utime(chemin, (vieux, vieux))
        return chemin

    def mode_vu_par_pg_dump(self):
        with open(os.path.join(self.state, "mode-ouverture"),
                  encoding="utf-8") as fh:
            return fh.read()

    def test_repertoire_cree_prive_et_dump_0600(self):
        self.assertFalse(os.path.exists(self.dumps))
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(self.dumps), 0o700)
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)
        # créé sous le umask du script (0700), pas restreint après coup
        self.assertEqual(self.mode_vu_par_pg_dump(), "absent")

    def test_repertoire_preexistant_0755_restreint(self):
        os.makedirs(self.dumps, mode=0o755)
        os.chmod(self.dumps, 0o755)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(self.dumps), 0o700)
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)

    def test_dump_du_jour_preexistant_0644_prive_avant_ecriture(self):
        os.makedirs(self.dumps)
        chemin = self.dump_du_jour()
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write("ancien dump du jour\n")
        os.chmod(chemin, 0o644)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        # pg_dump a bien vu 0600 à l'ouverture : le mode a été corrigé avant,
        # sinon il aurait réécrit le fichier en 0644.
        self.assertEqual(self.mode_vu_par_pg_dump(), "600")
        self.assertEqual(self.mode(chemin), 0o600)
        with open(chemin, encoding="utf-8") as fh:
            self.assertNotIn("ancien", fh.read())

    def test_dumps_anciens_normalises_sans_reecriture(self):
        os.makedirs(self.dumps)
        chemin = self.dump_ancien("ameesh-vieux-2020-01-01.dump", "intact\n")
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(chemin), 0o600)
        with open(chemin, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "intact\n")

    def test_dump_de_plus_de_14_jours_supprime(self):
        os.makedirs(self.dumps)
        chemin = self.dump_ancien("ameesh-vieux-2019-12-31.dump", "vieux\n",
                                  age_jours=20)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertFalse(os.path.exists(chemin))


if __name__ == "__main__":
    unittest.main()
