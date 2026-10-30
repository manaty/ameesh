# SPDX-License-Identifier: AGPL-3.0-only
"""#4754 : une unité de veille ou de réveil ne nomme jamais un venv versionné.

Le constat (agents-2, 30/10) : `ameesh-watch-mty-nxl-rel-01.service` portait
`ExecStart=".../venv-52d1e28/bin/ameesh" watch send …`, chemin rendu par
`shutil.which` dans le tour qui l'avait armée ; le ménage des venv (#3792)
l'a retiré et la veille échouait en 203/EXEC à chaque tir (24 en 24 h).

Témoins : le chemin écrit suit le lien vivant `venv-courant` (sinon
`~/.local/bin`) ; les unités déjà écrites sont re-épinglées par le passage de
ménage, une seule fois, sans toucher aux autres unités ; à blanc, rien n'est
écrit ; le passage d'hôte ne lit les unités que si l'appelant les nomme.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from ameesh import housekeeping, watch, workspaces


def _exe(path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    os.chmod(path, 0o755)
    return path


class StableBinaryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="t4754-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.base = os.path.join(self.root, "share", "ameesh")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.versioned = os.path.join(self.base, "venv-52d1e28", "bin", "ameesh")

    def live(self):
        _exe(os.path.join(self.base, "venv-69e13a3", "bin", "ameesh"))
        os.symlink("venv-69e13a3", os.path.join(self.base, "venv-courant"))

    def test_un_venv_versionne_devient_le_lien_vivant(self):
        self.live()
        self.assertEqual(watch.stable_binary(self.versioned, home=self.home),
                         os.path.join(self.base, "venv-courant", "bin", "ameesh"))

    def test_sans_lien_vivant_le_lanceur_de_l_utilisateur(self):
        local = _exe(os.path.join(self.home, ".local", "bin", "ameesh"))
        self.assertEqual(watch.stable_binary(self.versioned, home=self.home), local)

    def test_un_chemin_stable_ou_sans_repli_reste_tel_quel(self):
        for path in (os.path.join(self.base, "venv", "bin", "ameesh"),
                     "/home/ameesh/.local/state/ameesh/.menage/bin-ameesh/ameesh",
                     "/usr/bin/env"):
            self.assertEqual(watch.stable_binary(path, home=self.home), path)
        # aucun chemin stable exécutable : rien d'inventé
        self.assertEqual(watch.stable_binary(self.versioned, home=self.home),
                         self.versioned)

    def test_la_veille_armee_depuis_un_venv_versionne(self):
        self.live()
        with mock.patch.object(watch.shutil, "which", return_value=self.versioned), \
                mock.patch.object(watch.os.path, "expanduser", return_value=self.home):
            ameesh = watch._ameesh_binary()
        self.assertNotIn("venv-52d1e28", ameesh)
        service = watch.units("a1", 1800, ameesh, "/usr/bin")[watch.unit_name("a1", "service")]
        self.assertIn('ExecStart="%s" watch send --agent a1' % ameesh, service)


class RepinTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="t4754-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.units = os.path.join(self.root, "units")
        os.makedirs(self.units)
        self.base = os.path.join(self.root, "share", "ameesh")
        _exe(os.path.join(self.base, "venv-69e13a3", "bin", "ameesh"))
        os.symlink("venv-69e13a3", os.path.join(self.base, "venv-courant"))
        self.dead = os.path.join(self.base, "venv-52d1e28", "bin", "ameesh")
        self.live = os.path.join(self.base, "venv-courant", "bin", "ameesh")
        self.calls = []

    def run_(self, cmd, **kw):
        self.calls.append(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    def write(self, name, text, mode=0o600):
        path = os.path.join(self.units, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, mode)
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_veille_et_reveil_re_epingles_une_fois(self):
        veille = self.write("ameesh-watch-mty-nxl-rel-01.service",
                            '[Service]\nEnvironment="PATH=%s"\nExecStart="%s" watch send '
                            '--agent mty-nxl-rel-01\n' % (os.path.dirname(self.dead), self.dead))
        reveil = self.write("ameesh-wake-2-a1-m6h.service",
                            '[Service]\nExecStart="/usr/bin/env" "AGENT_MAIL_NAME=a1" "%s" '
                            'watch fire --agent a1 --wake m6h\n' % self.dead)
        fixed = watch.repin_units(self.units, run=self.run_)
        self.assertEqual(sorted(f["path"] for f in fixed), sorted([veille, reveil]))
        self.assertIn('ExecStart="%s" watch send --agent mty-nxl-rel-01' % self.live,
                      self.read(veille))
        self.assertIn('"AGENT_MAIL_NAME=a1" "%s" watch fire' % self.live, self.read(reveil))
        # seule la ligne d'exécution change : le PATH épinglé reste
        self.assertIn('Environment="PATH=%s"' % os.path.dirname(self.dead), self.read(veille))
        self.assertEqual(stat.S_IMODE(os.stat(veille).st_mode), 0o600)
        self.assertEqual(self.calls, [["systemctl", "--user", "daemon-reload"]])
        # un second passage n'a plus rien à faire
        self.assertEqual(watch.repin_units(self.units, run=self.run_), [])
        self.assertEqual(len(self.calls), 1)

    def test_les_autres_unites_ne_sont_pas_touchees(self):
        texts = {
            "ameesh-watch-orchestrateur.service":
                "ExecStart=%h/.local/share/ameesh/venv/bin/ameesh role run orchestrateur "
                '-- "/x/.menage/bin-ameesh/ameesh" watch send --agent orchestrateur\n',
            "ameesh-runner-agent@.service": 'ExecStart="%s" runner\n' % self.dead,
            "ameesh-watch-a1.timer": "[Timer]\nOnUnitActiveSec=1800\n",
        }
        for name, text in texts.items():
            self.write(name, text)
        self.assertEqual(watch.repin_units(self.units, run=self.run_), [])
        for name, text in texts.items():
            self.assertEqual(self.read(os.path.join(self.units, name)), text)
        self.assertEqual(self.calls, [])

    def test_a_blanc_rien_n_est_ecrit(self):
        text = 'ExecStart="%s" watch send --agent a1\n' % self.dead
        path = self.write("ameesh-watch-a1.service", text)
        fixed = watch.repin_units(self.units, run=self.run_, dry_run=True)
        self.assertEqual(fixed, [{"path": path, "before": [self.dead], "after": [self.live]}])
        self.assertEqual(self.read(path), text)
        self.assertEqual(self.calls, [])

    def test_dossier_absent(self):
        self.assertEqual(watch.repin_units(os.path.join(self.root, "nope"), run=self.run_), [])


class HostPassTest(unittest.TestCase):
    def test_la_facade_ne_nomme_les_unites_que_si_on_les_lui_donne(self):
        cfg, db, pol = object(), object(), housekeeping.Policy()
        with mock.patch.object(workspaces, "dev_module", return_value=object()), \
                mock.patch.object(housekeeping, "run_pass", return_value={}) as run:
            workspaces.periodic_pass(cfg, db, pol, host="pc", actor="r", units_dir="/u")
            workspaces.periodic_pass(cfg, db, pol, host="pc", actor="r")
        self.assertEqual(run.call_args_list[0].kwargs.get("units_dir"), "/u")
        self.assertNotIn("units_dir", run.call_args_list[1].kwargs)

    def test_hors_systemd_aucun_dossier(self):
        with mock.patch.object(housekeeping.platform, "is_linux", return_value=False):
            self.assertIsNone(housekeeping.host_units_dir())
        with mock.patch.object(housekeeping.platform, "is_linux", return_value=True), \
                mock.patch.dict(os.environ, {"AMEESH_SYSTEMD_USER_DIR": "/u"}):
            self.assertEqual(housekeeping.host_units_dir(), "/u")


if __name__ == "__main__":
    unittest.main()
