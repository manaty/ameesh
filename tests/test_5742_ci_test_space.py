# SPDX-License-Identifier: AGPL-3.0-only
"""#5742 : refus avant les tests et durée de vie des temporaires d'une part."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_test_space_5742", ROOT / "scripts/ci-test-space.py")
espace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(espace)


def statvfs(gib=6, inodes=20000, total_inodes=100000):
    return types.SimpleNamespace(f_bavail=int(gib * 1024), f_frsize=1024 ** 2,
                                 f_favail=inodes, f_files=total_inodes)


@unittest.skipUnless(hasattr(os, "statvfs"), "parts Linux : statvfs requis")
class MargeTest(unittest.TestCase):
    def test_octets_et_inodes_sont_deux_conditions(self):
        for etat in (statvfs(gib=4), statvfs(inodes=9999)):
            with self.subTest(etat=etat), mock.patch.object(espace.os, "statvfs", return_value=etat):
                with self.assertRaises(OSError):
                    espace.verifier(Path("/faux"), 5)
        for etat in (statvfs(gib=5, inodes=10000), statvfs(inodes=0, total_inodes=0)):
            with mock.patch.object(espace.os, "statvfs", return_value=etat):
                espace.verifier(Path("/faux"), 5)

    def test_refus_sans_lancer_ni_creer_de_temporaire(self):
        for erreur in (OSError("marge insuffisante"), PermissionError("illisible")):
            with mock.patch.object(espace, "verifier", side_effect=erreur), \
                    mock.patch.object(espace.subprocess, "Popen") as popen, \
                    mock.patch.object(espace.tempfile, "TemporaryDirectory") as temporaire, \
                    contextlib.redirect_stdout(io.StringIO()) as sortie:
                self.assertEqual(espace.executer(["jamais-execute"]), 75)
                popen.assert_not_called()
                temporaire.assert_not_called()
                self.assertIn("non concluante", sortie.getvalue())

    def test_volume_tmp_distinct_du_worker_est_verifie(self):
        vus = []
        def verifier(chemin, mini):
            vus.append((chemin, mini))
            if chemin == Path("/tmp-faux-5742"):
                raise OSError("tmpfs plein")
        with mock.patch.dict(os.environ, {"TMPDIR": "/tmp-faux-5742", "RUNNER_TEMP": "/worker-5742"}), \
                mock.patch.object(espace, "verifier", side_effect=verifier), \
                mock.patch.object(espace.subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(espace.executer(["jamais-execute"]), 75)
        self.assertIn((Path("/"), 5), vus)
        self.assertIn((Path("/worker-5742"), 5), vus)
        self.assertIn((Path("/tmp-faux-5742"), 1), vus)
        popen.assert_not_called()


@unittest.skipUnless(hasattr(os, "killpg"), "parts Linux : groupes POSIX requis")
class TemporairesTest(unittest.TestCase):
    def test_fichiers_abandonnes_recuperes_succes_et_echec(self):
        for code in (0, 7):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp:
                parent = Path(tmp)
                temoin = parent / "autre-job"
                temoin.write_text("conserver")
                preuve = parent / "preuve.json"
                commande = [sys.executable, "-c", '''
import json, os, pathlib, sys, tempfile
racine = pathlib.Path(tempfile.mkdtemp())
(racine / 'abandonne').write_text('reste du test')
pathlib.Path(sys.argv[1]).write_text(json.dumps([str(racine), os.environ['TMPDIR'], os.environ['TMP'], os.environ['TEMP']]))
raise SystemExit(int(sys.argv[2]))
''', str(preuve), str(code)]
                with mock.patch.dict(os.environ, {"TMPDIR": str(parent)}), \
                        mock.patch.object(espace, "verifier"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(espace.executer(commande), code)
                fichier, racine, tmp_env, temp_env = json.loads(preuve.read_text())
                self.assertEqual(racine, tmp_env)
                self.assertEqual(racine, temp_env)
                self.assertEqual(Path(racine).parent, parent)
                self.assertFalse(Path(fichier).exists())
                self.assertFalse(Path(racine).exists())
                self.assertEqual(temoin.read_text(), "conserver")

    def test_annulation_nettoie_et_restaure_les_signaux(self):
        with tempfile.TemporaryDirectory() as tmp:
            avant = signal.getsignal(signal.SIGTERM)
            proc = mock.Mock(pid=57420000)
            proc.wait.side_effect = [SystemExit(143), 0]
            with mock.patch.dict(os.environ, {"TMPDIR": tmp}), \
                    mock.patch.object(espace, "verifier"), \
                    mock.patch.object(espace.subprocess, "Popen", return_value=proc), \
                    mock.patch.object(espace.os, "killpg") as tuer, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as resultat:
                    espace.executer(["faux"])
            self.assertEqual(resultat.exception.code, 143)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertEqual(signal.getsignal(signal.SIGTERM), avant)
            tuer.assert_any_call(proc.pid, signal.SIGTERM)
            tuer.assert_any_call(proc.pid, signal.SIGKILL)

    def test_term_reel_recupere_la_racine(self):
        # Le fils annonce sa disponibilité sur stdout ; le pilote du test
        # envoie TERM à son parent, sans minuterie ni mesure du disque réel.
        with tempfile.TemporaryDirectory() as tmp:
            code = """
import importlib.util, pathlib, signal, sys
spec = importlib.util.spec_from_file_location('space', sys.argv[1])
space = importlib.util.module_from_spec(spec)
spec.loader.exec_module(space)
space.verifier = lambda *args: None
command = [sys.executable, '-c', "import signal; print('pret', flush=True); signal.pause()"]
raise SystemExit(space.executer(command))
"""
            with subprocess.Popen([sys.executable, "-c", code, str(ROOT / "scripts/ci-test-space.py")],
                                  env=dict(os.environ, TMPDIR=tmp), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as proc:
                try:
                    sortie = b""
                    for _ in range(2):
                        lisibles, _, _ = select.select([proc.stdout], [], [], 5)
                        self.assertTrue(lisibles, "le témoin n'annonce pas son démarrage")
                        sortie += os.read(proc.stdout.fileno(), 4096)
                        if b"pret\n" in sortie:
                            break
                    proc.terminate()
                    _, erreur = proc.communicate(timeout=5)
                    self.assertIn(b"pret\n", sortie)
                    self.assertEqual(proc.returncode, 143, erreur)
                    self.assertEqual(list(Path(tmp).iterdir()), [])
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()

    def test_commande_absente_ne_laisse_pas_de_racine(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"TMPDIR": tmp}), \
                mock.patch.object(espace, "verifier"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(espace.executer([str(Path(tmp) / "absent")]), 75)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class WorkflowTest(unittest.TestCase):
    def test_toutes_les_parts_linux_passent_par_la_garde(self):
        for nom in ("tests.yml", "carte.yml"):
            lignes = (ROOT / ".github/workflows" / nom).read_text().splitlines()
            appels = [l for l in lignes if " scripts/test-parts.py run " in l and not l.lstrip().startswith("#")]
            self.assertEqual(len(appels), 3 if nom == "tests.yml" else 1)
            for ligne in appels:
                self.assertEqual(ligne.count("python3 scripts/ci-test-space.py -- "), 1)
