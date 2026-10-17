# SPDX-License-Identifier: AGPL-3.0-only
"""#2157 — les commits de composition portent `ameesh-work: <lot>`.

Le banc monte un dépôt git jetable : une fusion propre, une fusion en conflit
dont le message est préparé avant résolution, et les refus d'une source déjà
composée ou introuvable. Aucune base, aucun réseau.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

from ameesh import plan_git

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "paquet-compose.py"


class PaquetComposeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-compose-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "paquet"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("base.txt", "base\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.rev("HEAD")

    # -- helpers -----------------------------------------------------------
    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True)
        if check and proc.returncode != 0:
            raise AssertionError(
                "git %s a échoué : %s" % (" ".join(args),
                                          proc.stderr.decode("utf-8", "replace")))
        return proc

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", ref).stdout.decode().strip()

    def write(self, name: str, content: str) -> None:
        (self.repo / name).write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.rev("HEAD")

    def lot_file(self, lot: int, name: str, content: str) -> str:
        self.git("switch", "-q", "-c", "lot/%d" % lot, "main")
        self.write(name, content)
        sha = self.commit("#%d : %s" % (lot, name))
        self.git("switch", "-q", "main")
        return sha

    def compose(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.repo), *args],
            capture_output=True, text=True,
        )

    def message(self, ref: str = "HEAD") -> str:
        return self.git("show", "-s", "--format=%B", ref).stdout.decode()

    # -- tests -------------------------------------------------------------
    def test_fusion_propre_ajoute_un_trailer_par_lot(self) -> None:
        source = self.lot_file(7, "sept.txt", "sept\n")
        short = source[:12]
        proc = self.compose("--lot", "7", "--lot", "8", "--source", source,
                            "--cut", "0400", "--note", "unité de banc")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("unité composée", proc.stdout)
        parents = self.git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
        self.assertEqual(len(parents), 3, "la composition doit être une fusion")
        self.assertEqual(parents[1].decode(), self.base)
        text = self.message()
        self.assertTrue(text.startswith(
            "vehicule 0400 : #7/#8 (%s) — unité de banc\n" % short), text)
        self.assertIn("\nameesh-work: 7\n", text)
        self.assertIn("\nameesh-work: 8\n", text)
        # le commit est déclarable par le code qui lit les trailers
        self.assertEqual(plan_git.cited_lots(text),
                         {7: "ameesh-work", 8: "ameesh-work"})
        self.assertEqual(self.git("status", "--porcelain").stdout, b"")

    def test_conflit_prepare_le_message_sans_commiter(self) -> None:
        source = self.lot_file(9, "base.txt", "lot\n")
        self.write("base.txt", "main\n")
        head = self.commit("main change base.txt")
        proc = self.compose("--lot", "9", "--source", source, "--cut", "0400")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.rev("HEAD"), head, "aucun commit sur conflit")
        self.assertIsNotNone(self.rev("MERGE_HEAD"))
        message_path = self.repo / ".git" / "MERGE_MSG"
        prepared = message_path.read_text(encoding="utf-8")
        self.assertIn("ameesh-work: 9", prepared)
        self.assertIn("vehicule 0400 : #9", prepared)
        # résolution : le commit reprend le message préparé, trailer compris
        self.write("base.txt", "résolu\n")
        self.git("add", "base.txt")
        self.git("commit", "--no-edit")
        text = self.message()
        self.assertIn("ameesh-work: 9", text)
        self.assertEqual(plan_git.cited_lots(text), {9: "ameesh-work"})
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def test_corps_deja_citant_ne_duplique_pas_le_trailer(self) -> None:
        source = self.lot_file(11, "onze.txt", "onze\n")
        proc = self.compose("--lot", "11", "--source", source,
                            "--body", "ameesh-work: 11")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.message().count("ameesh-work: 11"), 1)

    def test_source_deja_ancetre_refusee(self) -> None:
        self.write("apres.txt", "après\n")
        head = self.commit("commit suivant")
        proc = self.compose("--lot", "3", "--source", self.base)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("déjà ancêtre", proc.stderr)
        self.assertEqual(self.rev("HEAD"), head)

    def test_source_introuvable_refusee(self) -> None:
        proc = self.compose("--lot", "3", "--source", "deadbeefdeadbeef")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("introuvable", proc.stderr)


if __name__ == "__main__":
    unittest.main()
