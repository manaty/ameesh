# SPDX-License-Identifier: AGPL-3.0-only
"""#4271 — les tests ciblés d'un paquet horaire tiennent dans le créneau.

h-07 du 28/10 (13 unités) : 20 à 30 min par unité, jouées l'une après l'autre.
La carte d'impact ne mesure que psycopg et les tests tournaient sous psql
(~6 fois plus lent) : 1134 s de carte pour 13 unités. Témoins :

* `paquet-attribution.py --select` pèse un module par `tests/parts/durees.json`
  pour le pilote demandé ; la durée de la carte ne sert qu'à défaut, pour
  psycopg et une carte à la base ;
* `_run_series` joue les séries en parallèle, rend les résultats dans l'ordre,
  et une garde refusée n'en démarre plus ;
* `check_local_tests(parallel=N)` demande un créneau `ameesh heavy` ;
  `run_tests` borne la série par `heavy --max-duration`, rc 124 = non concluant ;
* `packet_tests` du canon compilé (`mechanics`) et sa transmission par
  paquet-horaire.

Aucune base, aucun réseau.
"""
from __future__ import annotations

import datetime
import gzip
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from ameesh import mechanics

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "paquet-attribution.py"


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(ROOT / "scripts" / "paquet-auto.py", "test_paquet_auto_4271")
paquet_horaire = _load_script(ROOT / "scripts" / "paquet-horaire.py",
                              "test_paquet_horaire_4271")

SOURCE_V1 = "def chaude():\n    return 1\n"
SOURCE_V2 = "def chaude():\n    return 10\n"


class DurationsBySelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-4271-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("src/pkg/mod.py", SOURCE_V1)
        self.write("tests/__init__.py", "")
        for name in ("test_a", "test_b", "test_c"):
            self.write("tests/%s.py" % name, "X = 1\n")
        # sous psql, test_a et test_b coûtent 50 s ; test_c n'est pas mesuré
        self.write("tests/parts/durees.json", json.dumps({
            "psycopg": {"test_a": 2.0, "test_b": 2.0},
            "psql": {"test_a": 50.0, "test_b": 50.0}}))
        self.base = self.commit("base")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def card(self, commit: str) -> None:
        # la carte croit les trois modules quasi gratuits (mesure psycopg)
        card = {
            "schema": "ameesh-carte/1", "commit": commit,
            "genere": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "blocs": {"src/pkg/mod.py": {
                "chaude": {"modules": ["test_a", "test_b", "test_c"]}}},
            "modules": {name: {"durations": {"psycopg": 0.1}}
                        for name in ("test_a", "test_b", "test_c")},
        }
        path = self.repo / "carte" / "carte.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(card, stream)

    def select(self, unit: str, budget: float, driver: str | None) -> dict:
        request = {"schema": "ameesh-paquet-selection-request/1",
                   "base": self.base, "budget": budget,
                   "units": [{"lot": 7, "unit_sha": unit}]}
        if driver is not None:
            request["driver"] = driver
        proc = subprocess.run(
            [sys.executable, str(HELPER), "--select", "--repo", str(self.repo),
             "--slug", "banc/banc", "--state-dir", self.tmp.name],
            input=json.dumps(request), capture_output=True, text=True,
            timeout=60)
        return {"rc": proc.returncode, **json.loads(proc.stdout)}

    def unit(self) -> str:
        self.write("src/pkg/mod.py", SOURCE_V2)
        return self.commit("unité")

    @staticmethod
    def modules(report: dict) -> list[str]:
        return [row["module"] for row in report["units"][0]["added"]]

    def test_psql_est_pese_par_la_table_du_pilote(self) -> None:
        self.card(self.base)
        report = self.select(self.unit(), budget=60, driver="psql")
        self.assertEqual(report["rc"], 0)
        self.assertEqual(report["durations"]["driver"], "psql")
        self.assertEqual(report["durations"]["table"], "tests/parts/durees.json")
        # 50 s par module mesuré, test_c au forfait (60 s) : un seul tient
        self.assertEqual(self.modules(report), ["tests.test_a"])
        self.assertEqual(report["units"][0]["dropped"], 2)

    def test_psycopg_carte_a_la_base_complete_la_table(self) -> None:
        self.card(self.base)
        report = self.select(self.unit(), budget=60, driver=None)
        self.assertEqual(report["durations"]["driver"], "psycopg")
        self.assertTrue(report["durations"]["card"])
        # 2 + 2 de la table, 0.1 de la carte pour test_c
        self.assertEqual(self.modules(report),
                         ["tests.test_c", "tests.test_a", "tests.test_b"])

    def test_carte_anterieure_ne_donne_plus_de_duree(self) -> None:
        old = self.base
        self.write("README", "x\n")
        self.base = self.commit("base suivante")
        self.card(old)
        report = self.select(self.unit(), budget=30, driver="psycopg")
        self.assertEqual(report["card"]["base"], "antérieure")
        self.assertFalse(report["durations"]["card"])
        # test_c, sans mesure, est au forfait : hors budget
        self.assertEqual(self.modules(report), ["tests.test_a", "tests.test_b"])

    def test_pilote_inconnu_refuse(self) -> None:
        report = self.select(self.unit(), budget=60, driver="sqlite")
        self.assertEqual(report["rc"], 2)
        self.assertEqual(report["status"], "invalid")


def _green(label, modules):
    return paquet_auto.TestRun(label, list(modules), 0, "Ran 1 test in 0.1s\n\nOK", 0.1)


class SeriesTest(unittest.TestCase):
    def test_les_series_partent_ensemble_et_rendent_leur_ordre(self) -> None:
        barrier = threading.Barrier(3, timeout=10)

        def job(name):
            def run():
                barrier.wait()  # bloquerait en série
                return name
            return run

        out = paquet_auto._run_series([job("a"), job("b"), job("c")], parallel=3)
        self.assertEqual(out, ["a", "b", "c"])

    def test_garde_refusee_arrete_les_series_suivantes(self) -> None:
        started = []
        guard = mock.Mock(side_effect=[None, paquet_auto.PaquetAutoError("epoch")]
                          + [None] * 5)

        def job(name):
            return lambda: started.append(name) or name

        with self.assertRaisesRegex(paquet_auto.PaquetAutoError, "epoch"):
            paquet_auto._run_series([job(n) for n in "abcd"], parallel=1, guard=guard)
        self.assertEqual(started, ["a"])
        started.clear()
        guard = mock.Mock(side_effect=paquet_auto.PaquetAutoError("epoch"))
        with self.assertRaisesRegex(paquet_auto.PaquetAutoError, "epoch"):
            paquet_auto._run_series([job(n) for n in "abcd"], parallel=2, guard=guard)
        self.assertEqual(started, [])

    def test_check_local_tests_parallele_demande_un_creneau(self) -> None:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a", "1" * 40),
                   paquet_auto.Unit(2, "b", "2" * 40)],
            discarded=[], head="3" * 40)
        calls = []

        def fake_run(_worktree, label, modules, *, timeout, heavy=False):
            calls.append((label, heavy, timeout))
            return _green(label, modules)

        own = {1: ["tests.test_un"], 2: ["tests.test_deux"]}
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               side_effect=lambda _w, unit: own[unit.lot]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto.shutil, "which", return_value="/bin/ameesh"), \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake_run):
            runs, faulty, _removals = paquet_auto.check_local_tests(
                pathlib.Path("."), composition, timeout=900, parallel=4,
                select=lambda *_: ({}, ""), log=lambda _line: None)
        self.assertEqual(faulty, [])
        self.assertEqual([run.label for run in runs],
                         ["unité #1", "unité #2", "contrats"])
        self.assertEqual(sorted(calls), [("contrats", True, 900),
                                         ("unité #1", True, 900),
                                         ("unité #2", True, 900)])

    def test_sans_ameesh_dans_le_path_repli_en_serie_dit(self) -> None:
        # #4298 : ni créneau ni schéma isolé : pas de séries simultanées
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a", "1" * 40),
                   paquet_auto.Unit(2, "b", "2" * 40)],
            discarded=[], head="3" * 40)
        calls, lines = [], []

        def fake_run(_worktree, label, modules, *, timeout, heavy=False):
            calls.append((label, heavy))
            return _green(label, modules)

        own = {1: ["tests.test_un"], 2: ["tests.test_deux"]}
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               side_effect=lambda _w, unit: own[unit.lot]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto.shutil, "which", return_value=None), \
                mock.patch.object(paquet_auto, "_run_series",
                                  wraps=paquet_auto._run_series) as series, \
                mock.patch.object(paquet_auto, "run_tests", side_effect=fake_run):
            runs, faulty, _removals = paquet_auto.check_local_tests(
                pathlib.Path("."), composition, timeout=900, parallel=4,
                select=lambda *_: ({}, ""), log=lines.append)
        self.assertEqual(faulty, [])
        self.assertEqual(series.call_args.kwargs["parallel"], 1)
        self.assertEqual(calls, [("unité #1", False), ("unité #2", False),
                                 ("contrats", False)])
        self.assertTrue(any("absent du PATH" in line for line in lines))


class HeavyRunTest(unittest.TestCase):
    def run_with(self, returncode: int):
        seen = {}

        def fake(cmd, *, cwd=None, input_text=None, timeout=None, env=None):
            seen.update(cmd=cmd, timeout=timeout, env=env)
            return subprocess.CompletedProcess(cmd, returncode, b"", b"")

        with mock.patch.object(paquet_auto.shutil, "which", return_value="/bin/ameesh"), \
                mock.patch.object(paquet_auto, "project_python", return_value="py"), \
                mock.patch.object(paquet_auto, "save_test_log", side_effect=lambda run: run), \
                mock.patch.object(paquet_auto, "run_command", side_effect=fake):
            run = paquet_auto.run_tests(pathlib.Path("."), "unité #1", ["tests.test_x"],
                                        timeout=900, heavy=True)
        return run, seen

    def test_la_borne_est_tenue_par_heavy(self) -> None:
        run, seen = self.run_with(0)
        self.assertEqual(seen["cmd"][:5],
                         ["/bin/ameesh", "heavy", "--max-duration", "900s", "--"])
        self.assertEqual(seen["cmd"][5:], ["py", "-m", "unittest", "-q", "tests.test_x"])
        self.assertTrue(seen["env"]["AMEESH_SCHEMA"].startswith("t_paq_"))
        # l'attente d'un créneau a sa propre borne, au-delà de celle de la série
        self.assertGreater(seen["timeout"], 900)
        self.assertTrue(run.green)

    def test_borne_atteinte_non_concluante(self) -> None:
        run, _seen = self.run_with(124)
        self.assertTrue(run.timed_out)
        self.assertTrue(run.inconclusive)
        self.assertFalse(run.real_failure)

    def test_pilote_impose_par_l_environnement(self) -> None:
        with mock.patch.dict(paquet_auto.os.environ, {"AMEESH_DRIVER": "psql"}):
            self.assertEqual(paquet_auto.test_driver(pathlib.Path(".")), "psql")
        with mock.patch.dict(paquet_auto.os.environ,
                             {"AMEESH_DRIVER": "", "AGENT_MESH_DRIVER": ""}), \
                mock.patch.object(paquet_auto, "project_python", return_value="py"), \
                mock.patch.object(paquet_auto, "_supports_psycopg", return_value=False):
            self.assertEqual(paquet_auto.test_driver(pathlib.Path(".")), "psql")


DOCUMENT = """schema: ameesh-mechanics/1
timezone: Europe/Paris
cutoffs: ["11:00", "23:00"]
build:
  max_in_build: 2
ci:
  packet: full
  incident_label: ci-full
  environment_red: does_not_block
  faulty_lot: removed_without_new_review
merge:
  named_fire: false
  repeat_freeze: false
  on_green_packet: true
  post_merge_red: forward_or_revert
reviews:
  families: [claude, codex]
  exclude_author_family: true
  distinct_families: true
projects: {}
"""


class CanonTest(unittest.TestCase):
    def test_bloc_compile(self) -> None:
        doc = mechanics.compile_document(
            DOCUMENT + "packet_tests:\n  unit_timeout_seconds: 900\n  parallel: 4\n")
        self.assertEqual(doc.packet_tests,
                         mechanics.PacketTests(unit_timeout_seconds=900, parallel=4))
        self.assertEqual(doc.to_dict()["packet_tests"],
                         {"unit_timeout_seconds": 900, "parallel": 4})
        self.assertIsNone(mechanics.compile_document(DOCUMENT).packet_tests)

    def test_bloc_mal_forme_refuse(self) -> None:
        for block in ("packet_tests:\n  unit_timeout_seconds: 900\n",
                      "packet_tests:\n  unit_timeout_seconds: 0\n  parallel: 4\n",
                      "packet_tests:\n  unit_timeout_seconds: 900\n  parallel: 4\n"
                      "  autre: 1\n"):
            with self.subTest(block=block), self.assertRaises(mechanics.MechanicsError):
                mechanics.compile_document(DOCUMENT + block)

    def context(self, unit_tests):
        shell = mock.Mock()
        shell.run.return_value = paquet_horaire.Result(0, "", "")
        ctx = paquet_horaire.Context(
            repo=ROOT, target="main", project="ameesh", pilot="p", actor="p",
            shell=shell, python="python3", unit_tests=unit_tests)
        ctx.say = mock.Mock()
        return ctx, shell

    def test_paquet_horaire_transmet_le_canon(self) -> None:
        ctx, shell = self.context(mechanics.PacketTests(900, 4))
        paquet_horaire.paquet_auto(ctx, "h-20261028-08", 4271)
        cmd = shell.run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--tests-timeout") + 1], "900")
        self.assertEqual(cmd[cmd.index("--tests-parallel") + 1], "4")

    def test_bloc_absent_dit_et_garde_les_defauts(self) -> None:
        ctx, shell = self.context(None)
        paquet_horaire.paquet_auto(ctx, "h-20261028-08", 4271)
        cmd = shell.run.call_args.args[0]
        self.assertNotIn("--tests-timeout", cmd)
        self.assertNotIn("--tests-parallel", cmd)
        self.assertIn("packet_tests absent", ctx.say.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
