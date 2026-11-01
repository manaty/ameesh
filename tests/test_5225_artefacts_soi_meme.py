# SPDX-License-Identifier: AGPL-3.0-only
"""#5225 : la phase artefacts ne se voit plus détentrice par ses propres
descripteurs (le dossier parent ouvert par `remove_artifact`) ; tout autre
processus, un enfant compris, garde l'artefact. La table `/proc` est réduite aux
vrais processus du test : le reste de l'hôte ne crée aucun doute."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from ameesh import build_artifacts as ba
from ameesh import platform
from ameesh.platform import process

DAY = 24 * 3600.0


def _write(path: str, text: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@unittest.skipUnless(platform.is_linux(), "the process table is read from /proc (Linux)")
class SoiMeme(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="ameesh-5225-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.proc = os.path.join(self.base, "proc")
        os.makedirs(os.path.join(self.proc, "self"))
        patcher = mock.patch.object(process, "PROC", self.proc)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.show(os.getpid())
        self.wt = os.path.join(self.base, "wt")
        _write(os.path.join(self.wt, "web", "app.ts"))
        subprocess.run(["git", "init", "-q", self.wt], check=True, capture_output=True)
        self.artifact = os.path.join(self.wt, "web", "node_modules")
        _write(os.path.join(self.artifact, "left-pad", "index.js"))

    def show(self, pid: int) -> None:
        os.symlink("/proc/%d" % pid, os.path.join(self.proc, str(pid)))

    def child(self, path: str) -> None:
        """Un vrai processus qui tient `path` ouvert, déclaré dans la table."""
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        script = "import sys; sys.stdin.read()"
        proc = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, pass_fds=(fd,))
        self.addCleanup(proc.wait)
        self.addCleanup(proc.stdin.close)
        self.addCleanup(proc.stdout.close)
        self.show(proc.pid)

    def go(self) -> dict:
        return ba.run([{"path": self.wt, "locked": False}], ba.rules_of(None), idle_s=DAY,
                      protected=[], dry_run=False, now=time.time() + 2 * DAY,
                      journal_path=os.path.join(self.base, "journal"))

    def test_parent_fd_of_the_pass_does_not_hold_the_artifact(self):
        # le chemin réel de la panne : `remove_artifact` relit la table, pfd ouvert
        out = self.go()
        self.assertEqual([e["action"] for e in out["entries"]], ["deleted"], out["skipped"])
        self.assertFalse(os.path.exists(self.artifact))

    def test_own_fd_inside_the_artifact_does_not_hold_it(self):
        fd = os.open(os.path.join(self.artifact, "left-pad", "index.js"), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        out = self.go()
        self.assertEqual([e["action"] for e in out["entries"]], ["deleted"], out["skipped"])
        self.assertFalse(os.path.exists(self.artifact))

    def test_child_with_a_fd_inside_keeps_it(self):
        self.child(os.path.join(self.artifact, "left-pad", "index.js"))
        out = self.go()
        self.assertEqual(out["entries"], [])
        self.assertIn("process inside", " ".join(out["skipped"]))
        self.assertTrue(os.path.isdir(self.artifact))

    def test_child_on_the_directory_the_pass_holds_keeps_it(self):
        # `paths` garde un seul processus par chemin : la passe, listée la
        # première sur le même dossier, ne doit pas masquer l'autre détenteur
        self.child(os.path.dirname(self.artifact))
        pfd = os.open(os.path.dirname(self.artifact), os.O_RDONLY)
        self.addCleanup(os.close, pfd)
        out = self.go()
        self.assertEqual(out["entries"], [])
        self.assertIn("process inside", " ".join(out["skipped"]))
        self.assertTrue(os.path.isdir(self.artifact))

    def test_own_cwd_still_holds(self):
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.wt)
        out = self.go()
        self.assertEqual(out["entries"], [])
        self.assertIn("process inside", " ".join(out["skipped"]))
        self.assertTrue(os.path.isdir(self.artifact))


if __name__ == "__main__":
    unittest.main()
