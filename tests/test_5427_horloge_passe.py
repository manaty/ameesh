# SPDX-License-Identifier: AGPL-3.0-only
"""#5427 : la passe horloge/fuseau (`scripts/test-horloge-passe.py`).

Sans libfaketime : la sélection, les scénarios, l'environnement simulé, la
vérification d'horloge (sous-processus simulé) et le compte rendu. Un vrai
scénario `fuseau` (sans libfaketime) prouve le lancement et la lecture des
échecs ; la simulation du jour elle-même est vérifiée à la main sous
libfaketime (le test du lot : réintroduire #5412 et voir la passe rougir).
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "horloge_passe", REPO / "scripts" / "test-horloge-passe.py")
passe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(passe)


class ModulesOfTest(unittest.TestCase):
    def test_comments_and_blank_lines_are_ignored(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "liste.txt"
        path.write_text("# une graine\ntests.test_b  # en fin de ligne\n\n"
                        "tests.test_a\n", encoding="utf-8")
        self.assertEqual(passe.modules_of(path), ["tests.test_b", "tests.test_a"])

    def test_missing_list_is_empty(self):
        self.assertEqual(passe.modules_of(pathlib.Path("/nonexistent/liste.txt")), [])


class SelectionTest(unittest.TestCase):
    """La graine passe avant les constats H3 du lint, sans doublon."""

    def repo(self, *, findings=None, broken=False) -> pathlib.Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "tests" / "parts").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "tests" / "parts" / "horloge.txt").write_text(
            "tests.test_seed\n# tests.test_comment\ntests.test_shared\n",
            encoding="utf-8")
        if broken:
            (root / "scripts" / "test-hermeticity-lint.py").write_text(
                "import sys\nsys.exit(3)\n", encoding="utf-8")
        else:
            (root / "scripts" / "test-hermeticity-lint.py").write_text(
                "import json, sys\n"
                "print(json.dumps({'findings': %s}))\n"
                % json.dumps(findings or []), encoding="utf-8")
        return root

    def test_seed_then_calendar_modules(self):
        root = self.repo(findings=[
            {"path": "tests/test_seed.py", "rule": "H3",
             "message": "fixture dépendante du jour : datetime.date.today()"},
            {"path": "tests/test_day.py", "rule": "H3",
             "message": "fixture dépendante du jour : datetime.date.today()"},
            {"path": "tests/test_other.py", "rule": "H1"},
        ])
        self.assertEqual(passe.selection(root),
                         ["tests.test_seed", "tests.test_shared", "tests.test_day"])

    def test_broken_lint_keeps_the_seed(self):
        self.assertEqual(passe.selection(self.repo(broken=True)),
                         ["tests.test_seed", "tests.test_shared"])

    def test_fake_clock_plays_only_the_calendar_modules(self):
        """La graine (temps réel, processus) n'entre pas sous libfaketime."""
        root = self.repo(findings=[
            {"path": "tests/test_day.py", "rule": "H3",
             "message": "fixture dépendante du jour : datetime.date.today()"},
            {"path": "tests/test_other.py", "rule": "H1"},
        ])
        fake = [sc for sc in passe.scenarios() if sc["name"] == "23h45"][0]
        self.assertEqual(passe.scenario_modules(fake, repo=root), ["tests.test_day"])

    def test_timezone_replays_the_seed_too(self):
        root = self.repo(findings=[{"path": "tests/test_day.py", "rule": "H3",
             "message": "fixture dépendante du jour : datetime.date.today()"}])
        fuseau = [sc for sc in passe.scenarios() if sc["name"] == "fuseau"][0]
        self.assertEqual(passe.scenario_modules(fuseau, repo=root),
                         ["tests.test_seed", "tests.test_shared", "tests.test_day"])

    def test_override_wins_over_the_selection(self):
        root = self.repo(findings=[{"path": "tests/test_day.py", "rule": "H3",
             "message": "fixture dépendante du jour : datetime.date.today()"}])
        fake = [sc for sc in passe.scenarios() if sc["name"] == "23h45"][0]
        self.assertEqual(
            passe.scenario_modules(fake, repo=root, override=["tests.test_x"]),
            ["tests.test_x"])


class ScenariosTest(unittest.TestCase):
    def test_nearest_occurrence(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 2, 23, 40, tzinfo=dt.timezone.utc)
        self.assertEqual(passe._nearest(now, 23, 45),
                         dt.datetime(2026, 11, 2, 23, 45, tzinfo=dt.timezone.utc))
        self.assertEqual(passe._nearest(now, 0, 5),
                         dt.datetime(2026, 11, 3, 0, 5, tzinfo=dt.timezone.utc))
        # 01:00 : la bascule de 23:45 est derrière, celle de 00:05 aussi
        early = dt.datetime(2026, 11, 2, 1, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(passe._nearest(early, 23, 45),
                         dt.datetime(2026, 11, 1, 23, 45, tzinfo=dt.timezone.utc))
        self.assertEqual(passe._nearest(early, 0, 5),
                         dt.datetime(2026, 11, 2, 0, 5, tzinfo=dt.timezone.utc))

    def test_boundary_days(self):
        import datetime as dt
        now = dt.datetime(2026, 11, 2, 23, 40, tzinfo=dt.timezone.utc)
        by_name = {sc["name"]: sc for sc in passe.scenarios(now)}
        self.assertEqual(by_name["23h45"]["fake_at"],
                         dt.datetime(2026, 11, 2, 23, 45, tzinfo=dt.timezone.utc))
        self.assertEqual(by_name["00h05"]["fake_at"],
                         dt.datetime(2026, 11, 3, 0, 5, tzinfo=dt.timezone.utc))
        self.assertIsNone(by_name["fuseau"]["fake_at"])
        self.assertEqual(by_name["fuseau"]["tz"], "Pacific/Kiritimati")


class ClockEnvTest(unittest.TestCase):
    def test_fake_clock_environment(self):
        with mock.patch.object(passe, "libfaketime", return_value="libfaketime.so.1"):
            env = passe._clock_env({"LD_PRELOAD": "ancien.so"},
                                   fake_at=passe.dt.datetime(
                                       2026, 11, 2, 23, 45,
                                       tzinfo=passe.dt.timezone.utc),
                                   tz="UTC")
        self.assertEqual(env["LD_PRELOAD"], "libfaketime.so.1:ancien.so")
        self.assertEqual(env["FAKETIME"], "@2026-11-02 23:45:00")
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(env["PGTZ"], "UTC")
        self.assertEqual(env["FAKETIME_DONT_FAKE_MONOTONIC"], "1")
        self.assertEqual(env["NO_FAKE_STAT"], "1")

    def test_missing_libfaketime_is_not_conclusive(self):
        with mock.patch.object(passe, "libfaketime", return_value=None):
            with self.assertRaises(passe.Unavailable):
                passe._clock_env({}, fake_at=passe.dt.datetime(
                    2026, 11, 2, 23, 45, tzinfo=passe.dt.timezone.utc), tz="UTC")

    def test_extreme_timezone_has_no_fake_clock(self):
        env = passe._clock_env({}, fake_at=None, tz="Pacific/Kiritimati")
        self.assertEqual(env["TZ"], "Pacific/Kiritimati")
        self.assertEqual(env["PGTZ"], "Pacific/Kiritimati")
        self.assertNotIn("FAKETIME", env)
        self.assertNotIn("LD_PRELOAD", env)

    def test_python_env_defaults_to_the_psycopg_driver(self):
        """Le `psql` d'Ubuntu (wrapper Perl) se bloque sous libfaketime."""
        env = passe._python_env({}, REPO)
        self.assertEqual(env["AMEESH_DRIVER"], "psycopg")
        self.assertIn(str(REPO / "src"), env["PYTHONPATH"])
        explicit = passe._python_env({"AMEESH_DRIVER": "psql"}, REPO)
        self.assertEqual(explicit["AMEESH_DRIVER"], "psql")

    def test_explicit_library_override(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lib = pathlib.Path(tmp.name) / "libfaketime.so.1"
        lib.write_bytes(b"")
        with mock.patch.dict(os.environ, {"AMEESH_FAKETIME_LIB": str(lib)}):
            self.assertEqual(passe.libfaketime(), str(lib))
        with mock.patch.object(passe.ctypes.util, "find_library", return_value=None), \
                mock.patch.dict(os.environ,
                                {"AMEESH_FAKETIME_LIB": str(lib) + ".absent"}):
            self.assertIsNone(passe.libfaketime())


class CheckClockTest(unittest.TestCase):
    def completed(self, stdout: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    def target(self):
        return passe.dt.datetime(2026, 11, 2, 23, 45, tzinfo=passe.dt.timezone.utc)

    def test_matching_clock_passes(self):
        with mock.patch.object(passe.subprocess, "run",
                               return_value=self.completed(
                                   "2026-11-02 23:45 1793663100.0\n")):
            passe.check_clock({}, self.target())

    def test_wrong_clock_is_not_conclusive(self):
        with mock.patch.object(passe.subprocess, "run",
                               return_value=self.completed(
                                   "2026-11-02 12:00 1793620800.0\n")):
            with self.assertRaises(passe.Unavailable):
                passe.check_clock({}, self.target())

    def test_unreadable_clock_is_not_conclusive(self):
        with mock.patch.object(passe.subprocess, "run",
                               return_value=self.completed("pas une heure\n")):
            with self.assertRaises(passe.Unavailable):
                passe.check_clock({}, self.target())


class RunScenarioTest(unittest.TestCase):
    """Le lancement réel d'un scénario sans libfaketime, et la lecture des
    échecs (`FAIL:`/`ERROR:`), qui nomment le test à corriger."""

    def test_green_module(self):
        scenario = [sc for sc in passe.scenarios() if sc["name"] == "fuseau"][0]
        run = passe.run_scenario(scenario, ["tests.test_hermeticity_lint"],
                                 timeout=120)
        self.assertEqual(run["returncode"], 0)
        self.assertEqual(run["failures"], [])

    def test_failing_module(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "tests").mkdir()
        (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (root / "tests" / "test_rouge.py").write_text(
            "import unittest\n"
            "class RougeTest(unittest.TestCase):\n"
            "    def test_rouge(self):\n"
            "        self.fail('horloge')\n", encoding="utf-8")
        scenario = [sc for sc in passe.scenarios() if sc["name"] == "fuseau"][0]
        run = passe.run_scenario(scenario, ["tests.test_rouge"], repo=root,
                                 timeout=120)
        self.assertEqual(run["returncode"], 1)
        self.assertTrue(run["failures"])
        self.assertIn("RougeTest", run["failures"][0])

    def test_a_scenario_far_from_its_target_is_not_due(self):
        """Hors fenêtre, le décalage avec la base fausserait les baux et les âges."""
        import datetime as dt
        scenario = [sc for sc in passe.scenarios() if sc["name"] == "23h45"][0]
        scenario = dict(scenario, fake_at=passe._utcnow() + dt.timedelta(days=1))
        with self.assertRaises(passe.NotDue):
            passe.run_scenario(scenario, ["tests.test_hermeticity_lint"], max_skew=60)

    def test_a_lingering_child_does_not_hold_the_pass(self):
        """Un descendant qui garde la sortie ne doit pas bloquer la passe.

        `test_runner` laisse des processus derrière lui : avec un tube, la
        passe attendait leur EOF au lieu de conclure (#5427)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "tests").mkdir()
        (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (root / "tests" / "test_fils.py").write_text(
            "import subprocess, sys, unittest\n"
            "class FilsTest(unittest.TestCase):\n"
            "    def test_laisse_un_fils(self):\n"
            "        subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(5)'])\n", encoding="utf-8")
        scenario = [sc for sc in passe.scenarios() if sc["name"] == "fuseau"][0]
        run = passe.run_scenario(scenario, ["tests.test_fils"], repo=root,
                                 timeout=20)
        self.assertEqual(run["returncode"], 0)
        self.assertFalse(run["timed_out"])
        self.assertLess(run["seconds"], 10)


class ReportTest(unittest.TestCase):
    def test_green(self):
        payload = passe.report([{"failures": [], "returncode": 0,
                                 "timed_out": False}], ["m"], [])
        self.assertEqual(payload["verdict"], "green")

    def test_red_wins_over_unavailability(self):
        payload = passe.report([{"failures": ["FAIL: x"], "returncode": 1,
                                 "timed_out": False}], ["m"], ["fuseau: absent"])
        self.assertEqual(payload["verdict"], "red")

    def test_a_not_due_scenario_does_not_make_the_pass_inconclusive(self):
        payload = passe.report([{"failures": [], "returncode": 0,
                                 "timed_out": False}], ["m"], [],
                               ["23h45: hors fenêtre"])
        self.assertEqual(payload["verdict"], "green")
        self.assertEqual(payload["skipped"], ["23h45: hors fenêtre"])

    def test_timeout_without_failure_is_inconclusive(self):
        payload = passe.report([{"failures": [], "returncode": None,
                                 "timed_out": True}], ["m"], [])
        self.assertEqual(payload["verdict"], "inconclusive")

    def test_nothing_run_is_inconclusive(self):
        payload = passe.report([], ["m"], ["23h45: libfaketime absent"])
        self.assertEqual(payload["verdict"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
