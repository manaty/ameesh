# SPDX-License-Identifier: AGPL-3.0-only
"""#2177 : le faux agent ACP sauve sa couverture avant de sortir sur SIGTERM.

Le pont ACP tue son agent à la fermeture (SIGTERM, puis SIGKILL après la
grâce). Un processus Python instrumenté par coverage, frappé pendant son
`atexit`, laisse un fichier `.coverage.*` de 0 octet : la carte entière devient
partielle et la prévalidation refuse (incident #1770, gardes #1707/#1257
intactes). Le faux agent est un processus de test : il écrit sa mesure avant
de sortir, sans toucher au pont ni aux agents réels.

Le module `coverage` est ici un doublon de test : ces témoins sont hermétiques
et ne dépendent pas de coverage installé. Le cas `FAKE_ACP_IGNORE_SIGTERM=1`
reste celui du test d'annulation : la mesure est sauvée, puis l'agent continue
d'ignorer SIGTERM, donc le pont exerce bien sa grâce.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from .support import SRC, child_env

FAKE_AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fake_acp_agent.py")


class CoverageSaveOnSigtermTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="l2177-couverture-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.marker = os.path.join(self.tmp, "saved")
        self.covpy = os.path.join(self.tmp, "covpy")
        os.makedirs(self.covpy)
        with open(os.path.join(self.covpy, "coverage.py"), "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent("""\
                import os

                class _Collector:
                    def save(self):
                        path = os.environ.get("FAKE_COVERAGE_SAVE")
                        if path:
                            with open(path, "w", encoding="utf-8") as out:
                                out.write("saved\\n")

                class Coverage:
                    @staticmethod
                    def current():
                        return _Collector()
                """))

    def start(self, *, ignore: bool = False):
        env = child_env(
            FAKE_ACP_MODE="mute",
            FAKE_ACP_READY="1",
            FAKE_COVERAGE_SAVE=self.marker,
            PYTHONPATH=self.covpy + os.pathsep + SRC
                      + os.pathsep + os.environ.get("PYTHONPATH", ""))
        if ignore:
            env["FAKE_ACP_IGNORE_SIGTERM"] = "1"
        return subprocess.Popen([sys.executable, FAKE_AGENT],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)

    def ready(self, proc) -> None:
        """Attend le gestionnaire installé : aucun délai en dur."""
        self.assertEqual(proc.stderr.readline().strip(), "ready")

    def test_sigterm_sauve_la_couverture_avant_de_sortir(self):
        proc = self.start()
        try:
            self.ready(proc)
            proc.send_signal(signal.SIGTERM)
            _out, err = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue(os.path.exists(self.marker))
        with open(self.marker, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "saved")

    def test_sigterm_ignore_sauve_quand_meme_la_couverture(self):
        proc = self.start(ignore=True)
        try:
            self.ready(proc)
            proc.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + 5
            while not os.path.exists(self.marker) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(os.path.exists(self.marker))
            # le mode « ignore » est conservé : le pont devra escalader.
            self.assertIsNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            else:
                proc.communicate()


if __name__ == "__main__":
    unittest.main()
