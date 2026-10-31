# SPDX-License-Identifier: AGPL-3.0-only
"""#5084 : un écarté ne revient pas par une unité qui le contient ; la pause
posée pendant une tentative est relue avant de pousser.

h-09 : #5038 écarté, #5077 gardé alors que sa tête contenait celle de #5038 ;
le manifeste disait #5038 écarté et ses commits partaient au paquet. Et une
tentative partie avant une pause poussait quand même : `paquet-auto.py` relit
la pause (`--pause-project`) juste avant la poussée.
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "paquet-auto.py"
COMPOSE = REPO / "scripts" / "paquet-compose.py"

_spec = importlib.util.spec_from_file_location("test_5084_paquet_auto", SCRIPT)
paquet_auto = importlib.util.module_from_spec(_spec)
sys.modules["test_5084_paquet_auto"] = paquet_auto
_spec.loader.exec_module(paquet_auto)


class ContenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="paquet-5084-")
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.base = self.commit("base.txt", "base\n", "base")

    def git(self, *args: str, cwd: pathlib.Path | None = None) -> str:
        proc = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True)
        if proc.returncode:
            raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def commit(self, name: str, content: str, message: str) -> str:
        (self.repo / name).write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def lot(self, lot: int, start: str = "main") -> str:
        self.git("switch", "-q", "-c", "lot/%d" % lot, start)
        sha = self.commit("lot%d.txt" % lot, "%d\n" % lot, "#%d" % lot)
        self.git("switch", "-q", "main")
        return sha

    def worktree(self) -> pathlib.Path:
        path = self.tmp / "banc"
        self.git("worktree", "add", "-q", "-b", "integration/paquet-banc",
                 str(path), self.base)
        return path

    def compose(self, candidates, removed):
        return paquet_auto.compose_candidates(
            self.worktree(), candidates, 99, compose_script=COMPOSE,
            record_package=False, removed=removed, log=lambda _line: None)

    def test_unite_contenant_un_ecarte_est_ecartee(self) -> None:
        # h-09 : #2 est bâti sur #1 écarté ; #3 n'en dépend pas
        first = self.lot(1)
        second = self.lot(2, "lot/1")
        third = self.lot(3)
        composition = self.compose(
            [paquet_auto.Candidate(2, second), paquet_auto.Candidate(3, third)],
            {1: first})
        self.assertEqual([unit.lot for unit in composition.units], [3])
        [gone] = composition.discarded
        self.assertEqual(gone.lot, 2)
        self.assertEqual(gone.reason, paquet_auto.CONTAINS_DISCARDED_REASON)
        self.assertIn("#1", gone.detail)
        self.assertNotIn("#1", self.git("log", "--format=%s", "HEAD",
                                        cwd=self.tmp / "banc"))

    def test_contenance_transitive_dans_la_composition(self) -> None:
        # #4 contient #2, lui-même écarté ici parce qu'il contient #1
        first = self.lot(1)
        second = self.lot(2, "lot/1")
        fourth = self.lot(4, "lot/2")
        composition = self.compose(
            [paquet_auto.Candidate(2, second), paquet_auto.Candidate(4, fourth)],
            {1: first})
        self.assertEqual(composition.units, [])
        self.assertEqual([item.lot for item in composition.discarded], [2, 4])

    def test_ecarte_deja_sur_la_cible_ne_retire_rien(self) -> None:
        # la source écartée est déjà dans la base : rien n'est porté en plus
        second = self.lot(2)
        composition = self.compose([paquet_auto.Candidate(2, second)],
                                   {1: self.base})
        self.assertEqual([unit.lot for unit in composition.units], [2])

    def test_source_ecartee_absente_du_clone_ignoree(self) -> None:
        second = self.lot(2)
        composition = self.compose([paquet_auto.Candidate(2, second)],
                                   {1: "0123456789ab" * 3 + "0123"})
        self.assertEqual([unit.lot for unit in composition.units], [2])

    def test_sources_des_ecartes_lues_dans_la_liste(self) -> None:
        listed = [paquet_auto.Candidate(5, "e" * 40)]
        payload = {"candidates": [
            {"work_item": 6, "source": "f" * 40},
            {"work_item": 7, "published_head": "a" * 40},
            {"work_item": 8}]}
        self.assertEqual(
            paquet_auto.discarded_sources(payload, listed, [5, 6, 7, 8, 9]),
            {5: "e" * 40, 6: "f" * 40, 7: "a" * 40})


class PauseAvantPousseeTest(unittest.TestCase):
    def proc(self, rc: int, out: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], rc, out.encode(), b"")

    def test_sans_projet_aucune_lecture(self) -> None:
        with mock.patch.object(paquet_auto, "run_command") as run:
            paquet_auto.check_pause(REPO, "", "main")
        run.assert_not_called()

    def test_pause_active_arrete_la_poussee(self) -> None:
        with mock.patch.object(paquet_auto, "run_command",
                               return_value=self.proc(
                                   paquet_auto.PAUSE_ACTIVE_EXIT,
                                   "en pause jusqu'à 12:00 (staging, par bureau)")):
            with self.assertRaises(paquet_auto.PacketPaused) as raised:
                paquet_auto.check_pause(REPO, "ameesh", "main")
        self.assertIn("staging", str(raised.exception))

    def test_pause_illisible_arrete_aussi(self) -> None:
        with mock.patch.object(paquet_auto, "run_command",
                               return_value=self.proc(1, "base absente")):
            with self.assertRaises(paquet_auto.PacketPaused):
                paquet_auto.check_pause(REPO, "ameesh", "main")

    def test_sans_pause_on_pousse(self) -> None:
        with mock.patch.object(paquet_auto, "run_command",
                               return_value=self.proc(0)) as run:
            paquet_auto.check_pause(REPO, "ameesh", "main")
        self.assertEqual(run.call_args[0][0][-2:], ["ameesh", "main"])

    def test_main_rend_le_code_de_pause(self) -> None:
        with mock.patch.object(paquet_auto, "compose_packet",
                               side_effect=paquet_auto.PacketPaused("pause")):
            rc = paquet_auto.main(["--name", "h-20261031-01", "--carrier", "7",
                                   "--pause-project", "ameesh"])
        self.assertEqual(rc, paquet_auto.PAUSED_RC)


if __name__ == "__main__":
    unittest.main()
