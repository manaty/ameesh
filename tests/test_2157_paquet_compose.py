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

from ameesh import plan_git, planned_waits

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
            "vehicule 0400 : #7 %s, #8 %s — unité de banc\n" % (short, short)),
            text)
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
        self.assertIn("vehicule 0400 : #9 %s" % source[:12], prepared)
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

    def test_un_marqueur_de_saut_dans_le_message_est_refuse(self) -> None:
        # #2546 : une tête composée avec `[skip ci]` ne lancerait aucun
        # workflow, même l'événement `pull_request: labeled` de sa CI. Le
        # refus tombe avant la fusion : HEAD et MERGE_HEAD restent intacts.
        source = self.lot_file(12, "douze.txt", "douze\n")
        for marker in ("[skip ci]", "[CI SKIP]", "[no ci]", "[skip actions]",
                       "[actions skip]"):
            with self.subTest(marker=marker):
                proc = self.compose("--lot", "12", "--source", source,
                                    "--note", "composition %s" % marker)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn(marker, proc.stderr)
                self.assertIn("instruction de saut GitHub", proc.stderr)
                self.assertEqual(self.rev("HEAD"), self.base)
                self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def test_le_marqueur_est_refuse_dans_chaque_champ(self) -> None:
        source = self.lot_file(13, "treize.txt", "treize\n")
        for field, value in (("--subject", "sujet [skip ci]"),
                             ("--note", "note [ci skip]"),
                             ("--body", "corps [no ci]"),
                             ("--cut", "0400 [skip actions]")):
            with self.subTest(field=field):
                proc = self.compose("--lot", "13", "--source", source,
                                    field, value)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn(field, proc.stderr)
                self.assertEqual(self.rev("HEAD"), self.base)
                self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

    def test_un_sujet_propre_reste_accepte(self) -> None:
        source = self.lot_file(15, "quinze.txt", "quinze\n")
        proc = self.compose("--lot", "15", "--source", source,
                            "--subject", "composition propre")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.message().splitlines()[0], "composition propre")

    def test_une_note_avec_marqueur_est_refusee_meme_avec_un_sujet_propre(self) -> None:
        # #2546 : le champ est contrôlé pour lui-même, même si `--subject`
        # rend `--note` inutilisé ; aucun marqueur ne doit entrer par un champ.
        source = self.lot_file(16, "seize.txt", "seize\n")
        proc = self.compose("--lot", "16", "--source", source,
                            "--subject", "sujet propre", "--note", "note [skip ci]")
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("--note", proc.stderr)
        self.assertEqual(self.rev("HEAD"), self.base)
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())

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

    # -- #2360/#2387 : unités de fusion et lots « #<lot> <tête> » -------------
    def test_units_liste_chaque_unite_avec_sa_tete(self) -> None:
        self.git("switch", "-q", "-c", "integration/paquet-banc")
        sept = self.lot_file(7, "sept.txt", "sept\n")
        huit = self.lot_file(8, "huit.txt", "huit\n")
        self.git("switch", "-q", "integration/paquet-banc")
        for lot, source in ((7, sept), (8, huit)):
            proc = self.compose("--lot", str(lot), "--source", source)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        # une mise à jour depuis la cible, sans trailer, n'est pas une unité
        self.git("switch", "-q", "main")
        self.write("cible.txt", "cible\n")
        self.commit("#99 cité en prose sur la cible")
        self.git("switch", "-q", "integration/paquet-banc")
        self.git("merge", "-q", "--no-ff", "-m", "mise à jour : #99 deadbeef0", "main")
        # remise de #7 recomposée : même place, tête la plus récente
        self.git("switch", "-q", "lot/7")
        self.write("sept.txt", "sept bis\n")
        remise = self.commit("#7 remise")
        self.git("switch", "-q", "integration/paquet-banc")
        proc = self.compose("--lot", "7", "--source", remise)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self.compose("--units", "--base", self.base)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = proc.stdout.strip()
        # #7 recomposé : une seule unité (la plus récente) et un seul lot
        self.assertEqual(line, "2 unités de fusion, 2 lots portés : #7 %s, #8 %s"
                         % (remise[:12], huit[:12]))
        unit = getattr(planned_waits, "_UNIT", None)
        if unit is not None:  # forme reconnue par les porteurs (#2335)
            self.assertEqual({int(m.group(1)) for m in unit.finditer(line)}, {7, 8})

    def test_units_compte_les_lots_d_une_fusion_multi_lot(self) -> None:
        self.git("switch", "-q", "-c", "integration/paquet-banc")
        sept = self.lot_file(7, "sept.txt", "sept\n")
        huit = self.lot_file(8, "huit.txt", "huit\n")
        neuf = self.lot_file(9, "neuf.txt", "neuf\n")
        self.git("switch", "-q", "integration/paquet-banc")
        # une fusion porte deux lots : une unité, deux lots listés
        proc = self.compose("--lot", "7", "--lot", "8", "--source", sept)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.compose("--lot", "9", "--source", neuf)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.compose("--units", "--base", self.base)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            "2 unités de fusion, 3 lots portés : #7 %s, #8 %s, #9 %s"
            % (sept[:12], sept[:12], neuf[:12]))

        # #8 recomposé seul : l'unité multi-lot reste comptée pour #7
        self.git("switch", "-q", "lot/8")
        self.write("huit.txt", "huit bis\n")
        remise = self.commit("#8 remise")
        self.git("switch", "-q", "integration/paquet-banc")
        proc = self.compose("--lot", "8", "--source", remise)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.compose("--units", "--base", self.base)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            "3 unités de fusion, 3 lots portés : #7 %s, #8 %s, #9 %s"
            % (sept[:12], remise[:12], neuf[:12]))

    def test_units_sans_unite_ou_mal_appele(self) -> None:
        proc = self.compose("--units", "--base", self.base)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("aucune unité", proc.stderr)
        proc = self.compose("--units")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--base", proc.stderr)
        proc = self.compose("--units", "--base", self.base, "--lot", "3")
        self.assertEqual(proc.returncode, 2)
        proc = self.compose("--units", "--base", "introuvable")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("introuvable", proc.stderr)


if __name__ == "__main__":
    unittest.main()
