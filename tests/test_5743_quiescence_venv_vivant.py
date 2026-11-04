# SPDX-License-Identifier: AGPL-3.0-only
"""#5743 : la sonde de quiescence et son empreinte suivent la version vivante.

Le venv stable d'un compte basculé (bascule.py) ne porte que des lanceurs : son
`lib/` n'a aucun paquet ameesh. L'exécutant (`staging_executor._notifier`) et
la sonde (`quiescence-staging.sh`) résolvent donc le lien `venv-courant` : ils
prennent l'empreinte du python et du paquet de la version vivante, puis lancent
ce même python, y compris après un basculement de version. Banc isolé : un
stable NEUF écrit par `_installer_lanceurs` (comme #5737), aucun hôte, aucune
base.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import staging_executor as executor

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/mise-a-jour/quiescence-staging.sh"
SPEC = importlib.util.spec_from_file_location(
    "bascule_5743", ROOT / "deploy/mise-a-jour/bascule.py")
bascule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bascule)

#: python d'une version : note son chemin et le destinataire de l'alerte
PYTHON = '#!/usr/bin/env bash\n' \
    'if [ "$1" = - ]; then cat > /dev/null; printf "%s\\t%s\\n" "$0" "$2" >> "$FAUX/python"; fi\n'


class VenvVivantTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="5743-")
        self.addCleanup(tmp.cleanup)
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        self.home = Path(tmp.name)
        self.base = self.home / ".local/share/ameesh"
        self.stable = str(self.base / "venv")
        for version in ("v1", "v2"):
            self.version(version)
        # stable neuf : seuls les lanceurs, puis la bascule sur v1
        os.makedirs(os.path.join(self.stable, "bin"))
        bascule._installer_lanceurs(self.stable, "v1")
        bascule._pointer(self.stable, "v1")
        self.faux = self.home / "faux"
        self.faux.mkdir()
        self.bascule_env = self.home / "bascule.env"
        self.bascule_env.write_text("")
        self.options = {"quiescence_probe": [str(SCRIPT)],
                        "bascule_env": str(self.bascule_env)}
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"]}
        for patch in (mock.patch.object(executor, "path_problem", return_value=None),
                      mock.patch.object(executor, "tree_problem", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def version(self, version):
        racine = self.base / ("venv-" + version)
        python = racine / "bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(PYTHON)
        python.chmod(0o755)
        paquet = racine / "lib/python3.12/site-packages/ameesh"
        paquet.mkdir(parents=True)
        (paquet / "mail.py").write_text("version = %r\n" % version)

    def vivant(self, version):
        racine = os.path.realpath(self.base / ("venv-" + version))
        return (os.path.join(racine, "bin/python"),
                os.path.join(racine, "lib/python3.12/site-packages/ameesh"))

    def test_stable_neuf_sans_paquet(self):
        self.assertTrue(os.path.isfile(os.path.join(self.stable, executor.BASCULE_LAUNCHERS)))
        self.assertFalse(os.path.exists(os.path.join(self.stable, "lib")))

    def test_empreinte_de_la_version_vivante_puis_apres_bascule(self):
        empreintes, probleme = executor.fingerprints(self.options, self.env)
        self.assertIsNone(probleme)
        python, paquet = self.vivant("v1")
        self.assertIn(python, empreintes)
        self.assertIn(paquet, empreintes)
        bascule._pointer(self.stable, "v2")
        empreintes, probleme = executor.fingerprints(self.options, self.env)
        self.assertIsNone(probleme)
        python2, paquet2 = self.vivant("v2")
        self.assertIn(python2, empreintes)
        self.assertIn(paquet2, empreintes)
        self.assertNotIn(paquet, empreintes)

    def test_python_epingle_sur_une_version_reste_epingle(self):
        bascule._pointer(self.stable, "v2")
        python, paquet = self.vivant("v1")
        empreintes, probleme = executor.fingerprints(
            self.options, dict(self.env, QUIESCENCE_PYTHON=python))
        self.assertIsNone(probleme)
        self.assertIn(paquet, empreintes)

    def test_stable_sans_version_vivante_refuse(self):
        os.remove(self.base / "venv-courant")
        _empreintes, probleme = executor.fingerprints(self.options, self.env)
        self.assertIn("no live venv", probleme)

    def test_la_sonde_alerte_avec_le_python_dont_on_a_pris_lempreinte(self):
        bascule._pointer(self.stable, "v2")
        empreintes, probleme = executor.fingerprints(self.options, self.env)
        self.assertIsNone(probleme)
        prive = self.home / "prive"
        prive.mkdir(mode=0o700)
        (prive / "bascule.env").write_text(
            'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3"\nHOTE_LOCAL=ameesh-agents-2\n')
        ssh = self.home / "bin/ssh"
        ssh.parent.mkdir()
        ssh.write_text("#!/bin/sh\nexit 255\n")
        ssh.chmod(0o755)
        res = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60,
            env=dict(self.env, PATH="%s:/usr/bin:/bin" % ssh.parent, FAUX=str(self.faux),
                     BASCULE_ENV=str(prive / "bascule.env"),
                     STAGING_PREVIOUS_HOST="ameesh-agents-1", STAGING_PREVIOUS_RUN="ab12cd",
                     QUIESCENCE_TRAVAIL=str(self.home / "travail")))
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertNotIn("non remise", res.stderr)
        appels = [ligne.split("\t") for ligne in
                  (self.faux / "python").read_text().splitlines()]
        python, _paquet = self.vivant("v2")
        self.assertEqual(appels, [[python, "mesh-design"], [python, "bureau"]])
        self.assertIn(python, empreintes)


if __name__ == "__main__":
    unittest.main()
