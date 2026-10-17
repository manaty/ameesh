# SPDX-License-Identifier: AGPL-3.0-only
"""#2225 : un processus mesuré frappé par SIGTERM pendant sa sortie sauve sa couverture.

Le pont ACP ferme l'entrée de son agent puis le termine aussitôt : l'agent,
qui a vu la fin de son entrée, est déjà dans sa sauvegarde atexit quand
SIGTERM arrive. Son gestionnaire (`SystemExit` du faux agent, #2177) ou la
terminaison par défaut coupait la sauvegarde : fichier `.coverage.*` de
0 octet, carte partielle (incident #1770, reproduit hors CI). La mesure
(`scripts/map_measure.py`) ignore désormais SIGTERM dès la sortie, juste avant
la sauvegarde coverage.

Le module coverage est ici un doublon, dans le seul processus enfant : sa
sauvegarde annonce son début, attend que le parent ait envoyé SIGTERM, puis
écrit un témoin. Aucun délai ne décide du verdict : la poignée de main est
explicite, les délais ne sont que des bornes.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest

from .support import REPO, child_env

SCRIPTS = os.path.join(REPO, "scripts")


class CoverageSaveDuringExitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="l2225-sortie-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.go = os.path.join(self.tmp, "sigterm-envoye")
        self.saved = os.path.join(self.tmp, "sauvee")
        covpy = os.path.join(self.tmp, "covpy")
        os.makedirs(covpy)
        with open(os.path.join(covpy, "coverage.py"), "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(f"""\
                import atexit, os, time

                def _save():
                    os.write(1, b"saving\\n")
                    fin = time.monotonic() + 10
                    while not os.path.exists({self.go!r}) and time.monotonic() < fin:
                        time.sleep(0.01)
                    # le gestionnaire Python s'exécute entre deux instructions :
                    # on lui en laisse plusieurs avant d'écrire le témoin
                    for _ in range(20):
                        time.sleep(0.01)
                    with open({self.saved!r}, "w", encoding="utf-8") as out:
                        out.write("saved\\n")

                def process_startup():
                    atexit.register(_save)
                """))
        self.child = os.path.join(self.tmp, "enfant.py")
        with open(self.child, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(f"""\
                import os, signal, sys
                sys.path.insert(0, {SCRIPTS!r})
                os.environ.setdefault("CARTE_MESURE", {os.path.join(self.tmp, "mesure")!r})
                os.environ.setdefault("COVERAGE_PROCESS_START", "inutilise")
                import map_measure
                map_measure.start()

                def _sortir(signum, frame):
                    raise SystemExit(0)  # comme le faux agent ACP (#2177)

                if os.environ.get("ENFANT_GESTIONNAIRE") == "1":
                    signal.signal(signal.SIGTERM, _sortir)
                sys.stdout.write("ready\\n")
                sys.stdout.flush()
                sys.stdin.read()
                """))
        self.env = child_env(PYTHONPATH=covpy + os.pathsep + os.environ.get("PYTHONPATH", ""))

    def terminate_during_exit(self, *, handler: bool) -> subprocess.Popen:
        env = dict(self.env, ENFANT_GESTIONNAIRE="1" if handler else "0")
        proc = subprocess.Popen([sys.executable, self.child], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            proc.stdin.close()  # l'enfant sort : sa sauvegarde atexit commence
            self.assertEqual(proc.stdout.readline().strip(), "saving")
            proc.send_signal(signal.SIGTERM)
            with open(self.go, "w", encoding="utf-8"):
                pass
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        return proc

    def test_gestionnaire_sigterm_ne_coupe_pas_la_sauvegarde(self):
        proc = self.terminate_during_exit(handler=True)
        self.assertTrue(os.path.exists(self.saved), "sauvegarde coupée par SIGTERM")
        self.assertEqual(proc.returncode, 0)

    def test_terminaison_par_defaut_ne_coupe_pas_la_sauvegarde(self):
        proc = self.terminate_during_exit(handler=False)
        self.assertTrue(os.path.exists(self.saved), "sauvegarde coupée par SIGTERM")
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
