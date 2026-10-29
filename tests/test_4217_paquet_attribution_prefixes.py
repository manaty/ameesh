# SPDX-License-Identifier: AGPL-3.0-only
"""#4217 — un rouge de voisin est situé sur les préfixes de l'empilement.

Témoins : un empilement synthétique A, B, C où le test casse dès A et ne
sort que dans l'unité C (module voisin) ; le retrait attendu est A, pas B,
même quand l'attribution par fichiers désigne B. Un rejeu non concluant ne
retire personne : `AttributionUnreadable` et courrier au pilote. Une erreur
de fixture DB en `QueryCanceled` (statement timeout) est non concluante et
jamais attribuée, dans un module propre comme dans un voisin. Aucune base,
aucun réseau.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO = ROOT / "scripts" / "paquet-auto.py"


def _load_script(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script(AUTO, "test_paquet_auto_4217")

TARGET = "tests.test_target.T.test_value"


def _red(label: str, modules, test: str = TARGET) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(
        label, list(modules), 1,
        "FAIL: test_value (%s)\nAssertionError: 2 != 1\n" % test, 0.1)


def _green(label: str, modules) -> "paquet_auto.TestRun":
    return paquet_auto.TestRun(label, list(modules), 0,
                               "Ran 1 test in 0.1s\n\nOK", 0.1)


class PrefixReplayTest(unittest.TestCase):
    """`prefix_failures` sur un vrai empilement A, B, C, rejeu simulé."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-4217-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("src/pkg/__init__.py", "")
        self.write("src/pkg/value.py", "VALUE = 1\n")
        self.write("tests/__init__.py", "")
        self.write("tests/test_target.py",
                   "import unittest\n"
                   "\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_value(self):\n"
                   "        self.assertEqual(1, 1)\n")
        self.base = self.commit("base")
        self.unit_a = self.commit_unit("a", "src/pkg/value.py", "VALUE = 2\n")
        self.unit_b = self.commit_unit("b", "docs/only-b.md", "b\n")
        self.unit_c = self.commit_unit("c", "docs/only-c.md", "c\n")
        self.composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, self.unit_a),
                   paquet_auto.Unit(2, "b" * 40, self.unit_b),
                   paquet_auto.Unit(3, "c" * 40, self.unit_c)],
            discarded=[], head=self.unit_c)
        self.failure = {"test": TARGET, "module": "test_target"}

    def git(self, *args: str, cwd=None) -> str:
        proc = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def commit_unit(self, name: str, path: str, content: str) -> str:
        self.write(path, content)
        self.write("tests/test_own_%s.py" % name,
                   "import unittest\n"
                   "\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        self.assertTrue(True)\n")
        return self.commit("unité %s" % name)

    def replay_at(self, red_labels):
        calls = []

        def replay(_worktree, label, modules, *, timeout):
            calls.append((label, list(modules)))
            if label in red_labels:
                return _red(label, modules)
            return _green(label, modules)

        return replay, calls

    def test_le_premier_prefixe_rouge_est_le_fautif(self) -> None:
        replay, calls = self.replay_at({"préfixe 1 (#1)"})
        found, why, flaky = paquet_auto.prefix_failures(
            self.repo, self.composition, [self.failure], timeout=1,
            replay=replay, log=lambda _line: None)
        self.assertEqual((why, flaky), ("", []))
        self.assertEqual(list(found), [1])
        self.assertEqual(found[1][0]["test"], TARGET)
        # Le premier préfixe suffit : les suivants ne sont pas rejoués.
        self.assertEqual(calls, [("préfixe 1 (#1)", [TARGET])])

    def test_une_unite_sans_rouge_est_sautee(self) -> None:
        replay, calls = self.replay_at({"préfixe 2 (#2)"})
        found, why, flaky = paquet_auto.prefix_failures(
            self.repo, self.composition, [self.failure], timeout=1,
            replay=replay, log=lambda _line: None)
        self.assertEqual((why, flaky), ("", []))
        self.assertEqual(list(found), [2])
        self.assertEqual([label for label, _modules in calls],
                         ["préfixe 1 (#1)", "préfixe 2 (#2)"])

    def test_rouge_seulement_a_la_tete(self) -> None:
        replay, calls = self.replay_at({"préfixe 3 (#3)"})
        found, why, flaky = paquet_auto.prefix_failures(
            self.repo, self.composition, [self.failure], timeout=1,
            replay=replay, log=lambda _line: None)
        self.assertEqual((why, flaky), ("", []))
        self.assertEqual(list(found), [3])
        self.assertEqual(len(calls), 3)

    def test_rejeu_non_concluant_ne_situe_personne(self) -> None:
        def replay(_worktree, label, modules, *, timeout):
            return paquet_auto.TestRun(label, list(modules), None,
                                       "did not finish in 1 s", 1.0,
                                       timed_out=True)

        found, why, flaky = paquet_auto.prefix_failures(
            self.repo, self.composition, [self.failure], timeout=1,
            replay=replay, log=lambda _line: None)
        self.assertEqual((found, flaky), ({}, []))
        self.assertIn("non concluant", why)

    def test_rouge_de_la_tete_mais_vert_sur_les_prefixes_est_instable(self) -> None:
        # #4221 : vert sur tous les préfixes = instable, consigné non
        # concluant, le paquet continue ; ce n'est plus une lecture illisible.
        replay, _calls = self.replay_at(set())
        found, why, flaky = paquet_auto.prefix_failures(
            self.repo, self.composition, [self.failure], timeout=1,
            replay=replay, log=lambda _line: None)
        self.assertEqual((found, why), ({}, ""))
        self.assertEqual([failure["test"] for failure in flaky], [TARGET])


class CheckLocalTestsTest(unittest.TestCase):
    """Le vrai `check_local_tests` situe le voisin rouge sur les préfixes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-4217-check-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "banc")
        self.git("config", "user.email", "banc@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("src/pkg/__init__.py", "")
        self.write("src/pkg/value.py", "VALUE = 1\n")
        self.write("tests/__init__.py", "")
        self.write("tests/parts/toujours.txt", "test_green\n")
        for name in ("test_green", "test_l161f_plan", "test_cluster"):
            self.write("tests/%s.py" % name, "# vert\n")
        self.write("tests/test_target.py",
                   "import unittest\n"
                   "\n"
                   "from pkg import value\n"
                   "\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_value(self):\n"
                   "        self.assertEqual(value.VALUE, 1)\n")
        self.base = self.commit("base")
        self.unit_a = self.commit_unit("a", "src/pkg/value.py", "VALUE = 2\n")
        self.unit_b = self.commit_unit("b", "docs/only-b.md", "b\n")
        self.unit_c = self.commit_unit("c", "docs/only-c.md", "c\n")
        self.composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, self.unit_a),
                   paquet_auto.Unit(2, "b" * 40, self.unit_b),
                   paquet_auto.Unit(3, "c" * 40, self.unit_c)],
            discarded=[], head=self.unit_c)

    def git(self, *args: str, cwd=None) -> str:
        proc = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
        return proc.stdout.strip()

    def write(self, name: str, content: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def commit_unit(self, name: str, path: str, content: str) -> str:
        self.write(path, content)
        self.write("tests/test_own_%s.py" % name,
                   "import unittest\n"
                   "\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        self.assertTrue(True)\n")
        return self.commit("unité %s" % name)

    def check(self, *, prefix=None):
        # L'unité C sélectionne le module voisin `test_target` ; les unités A
        # et B n'ont que leur module propre (vert). Le test casse pourtant
        # dès A : l'attribution par fichiers désignerait B (migration), le
        # rejeu des préfixes doit rendre A.
        neighbors = {3: [{"module": "tests.test_target", "reason": "carte",
                          "files": ["src/pkg/value.py"]}]}

        def attribute(_worktree, _composition, _failures):
            self.fail("un rouge de voisin ne doit plus passer par #3801")

        with mock.patch.object(paquet_auto, "project_python",
                               return_value=sys.executable):
            return paquet_auto.check_local_tests(
                self.repo, self.composition, timeout=30,
                base_sha=self.base, attribute=attribute,
                select=lambda *_a: (neighbors, "carte simulée"),
                prefix=prefix, log=lambda _line: None)

    def test_le_voisin_rouge_retire_a_et_pas_b(self) -> None:
        runs, faulty, removals = self.check()
        self.assertEqual(faulty, [1])
        self.assertEqual([removal.lot for removal in removals], [1])
        self.assertEqual(removals[0].proof, "prefixes")
        self.assertTrue(removals[0].attributed)
        self.assertIn("préfixe base+u1", removals[0].evidence[0])
        self.assertNotIn(2, faulty)
        self.assertEqual([run.label for run in runs],
                         ["unité #1", "unité #2", "unité #3", "contrats"])

    def test_un_rejeu_illisible_ne_retire_rien(self) -> None:
        def unreadable(*_args, **_kwargs):
            return {}, "worktree de préfixe impossible", []

        with self.assertRaises(paquet_auto.AttributionUnreadable) as caught:
            self.check(prefix=unreadable)
        self.assertIn("attribution des préfixes illisible",
                      str(caught.exception))
        self.assertEqual(caught.exception.tests, [TARGET])


class NotificationTest(unittest.TestCase):
    def test_un_rejeu_illisible_ecrit_au_pilote(self) -> None:
        error = paquet_auto.AttributionUnreadable(
            "worktree de préfixe impossible", tests=[TARGET])
        with mock.patch.object(paquet_auto, "send_mail") as send:
            why = paquet_auto.notify_unattributable(
                "h-20261028-42", 99, error, pilot="pilote",
                sender="mesh-design", log=lambda _line: None)
        self.assertEqual(why, "")
        recipient, lot, body, sender = send.call_args.args
        self.assertEqual((recipient, lot, sender), ("pilote", 99, "mesh-design"))
        self.assertIn("Aucun retrait automatique", body)
        self.assertIn(TARGET, body)

    def test_le_retrait_par_prefixe_le_dit_a_l_auteur(self) -> None:
        removal = paquet_auto.Removal(
            1, tests=[TARGET],
            evidence=["préfixe base+u1…u1 (premier rouge à #1)"],
            attributed=True, proof="prefixes")
        with mock.patch.object(paquet_auto, "lot_author",
                               return_value="mty-amh-impl-08"), \
                mock.patch.object(paquet_auto, "send_mail") as send:
            failed = paquet_auto.notify_removals(
                pathlib.Path("."), "h-20261028-42", [removal],
                sender="mesh-design", log=lambda _line: None)
        self.assertEqual(failed, [])
        _recipient, _lot, body, _sender = send.call_args.args
        self.assertIn("apparaît pour la première fois au préfixe", body)
        self.assertNotIn("la lecture #3801 désigne", body)


class ComposePacketPilotMailTest(unittest.TestCase):
    """Le flux complet prévient le pilote avant de s'arrêter."""

    def test_le_pilote_recoit_le_rouge_non_situe(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="paquet-4217-flux-")
        self.addCleanup(tmp.cleanup)
        repo = pathlib.Path(tmp.name) / "repo"
        repo.mkdir()
        worktree = pathlib.Path(tmp.name) / "wt"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text(
            "", encoding="utf-8")
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="b" * 40)
        error = paquet_auto.AttributionUnreadable(
            "worktree de préfixe impossible", tests=[TARGET])
        args = argparse.Namespace(
            repo=str(repo), name="h-20261028-42", target="main",
            carrier=99, base=None, local=False, no_packet_record=True,
            keep_worktree=False, keep_failed_hours=24.0, force=False,
            tests_timeout=10, pilot="pilote", sender="mesh-design")
        with mock.patch.object(paquet_auto, "PackageLock"), \
                mock.patch.object(paquet_auto, "fresh_base",
                               return_value="a" * 40), \
                mock.patch.object(paquet_auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(paquet_auto, "make_worktree",
                                  return_value=worktree), \
                mock.patch.object(
                    paquet_auto, "compute_candidates",
                    return_value=([paquet_auto.Candidate(1, "a" * 40)],
                                  {"ready": [1], "waiting": []})), \
                mock.patch.object(paquet_auto, "compose_candidates",
                                  return_value=composition), \
                mock.patch.object(paquet_auto, "run_freeze_checks",
                                  return_value=(0, "ok", 0.1)), \
                mock.patch.object(paquet_auto, "composition_table",
                                  return_value="# table\n"), \
                mock.patch.object(paquet_auto, "build_manifest",
                                  return_value="manifeste\n"), \
                mock.patch.object(paquet_auto, "write_manifest",
                                  return_value="b" * 40), \
                mock.patch.object(paquet_auto, "run_inclusion",
                                  return_value=(0, "inclusion : vert\n")), \
                mock.patch.object(paquet_auto, "push_branch"), \
                mock.patch.object(paquet_auto, "ensure_pull_request",
                                  return_value=(1, "url")), \
                mock.patch.object(paquet_auto, "freeze_carrier",
                                  return_value="gelé"), \
                mock.patch.object(paquet_auto, "request_full_ci",
                                  return_value="ci"), \
                mock.patch.object(paquet_auto, "check_local_tests",
                                  side_effect=error), \
                mock.patch.object(paquet_auto, "send_mail") as send:
            with self.assertRaises(paquet_auto.AttributionUnreadable):
                paquet_auto.compose_packet(args)
        self.assertEqual(send.call_count, 1)
        recipient, lot, body, sender = send.call_args.args
        self.assertEqual((recipient, lot, sender), ("pilote", 99, "mesh-design"))
        self.assertIn("Aucun retrait automatique", body)


class FixtureErrorTest(unittest.TestCase):
    """#4217 (2e cas) : une erreur de fixture DB n'est jamais attribuée.

    Un `tearDownClass`/`setUpClass` en `psycopg.errors.QueryCanceled`
    (statement timeout, base de test chargée) est une panne d'infrastructure,
    qu'il sorte dans un module propre ou voisin : la série est non concluante,
    consignée, et aucun retrait n'est décidé.
    """

    TEARDOWN = (
        "======================================================================\n"
        "ERROR: tearDownClass (tests.test_l56_role_executor.RoleExecutorTest)\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
        "\n"
        "----------------------------------------------------------------------\n"
        "Ran 33 tests in 42.0s\n"
        "FAILED (errors=1)\n")

    def run_of(self) -> "paquet_auto.TestRun":
        return paquet_auto.TestRun(
            "unité #1", ["tests.test_l56_role_executor"], 1,
            self.TEARDOWN, 42.0)

    def test_l_erreur_de_fixture_est_non_concluante(self) -> None:
        run = self.run_of()
        self.assertTrue(run.inconclusive)
        self.assertFalse(run.real_failure)
        self.assertEqual(paquet_auto.unittest_failures(self.TEARDOWN), [])
        self.assertIn("QueryCanceled", run.motive())

    def test_une_assertion_qui_cite_querycanceled_reste_reelle(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_x (tests.test_x.T.test_x)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "AssertionError: expected QueryCanceled: statement timeout\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = paquet_auto.TestRun("unité #1", ["tests.test_x"], 1, output, 0.1)
        self.assertFalse(run.inconclusive)
        self.assertTrue(run.real_failure)
        self.assertEqual(
            [failure["test"] for failure in
             paquet_auto.unittest_failures(output)],
            ["tests.test_x.T.test_x"])

    def test_un_vrai_rouge_reste_reel_a_cote_de_la_fixture(self) -> None:
        output = (
            "======================================================================\n"
            "FAIL: test_real (tests.test_l56_role_executor.RoleExecutorTest.test_real)\n"
            "----------------------------------------------------------------------\n"
            "AssertionError: vrai rouge\n"
            "\n"
            "======================================================================\n"
            "ERROR: tearDownClass (tests.test_l56_role_executor.RoleExecutorTest)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 33 tests in 42.0s\n"
            "FAILED (failures=1, errors=1)\n")
        run = paquet_auto.TestRun(
            "unité #1", ["tests.test_l56_role_executor"], 1, output, 42.0)
        self.assertFalse(run.inconclusive)
        self.assertTrue(run.real_failure)
        self.assertEqual(
            [failure["test"] for failure in
             paquet_auto.unittest_failures(output)],
            ["tests.test_l56_role_executor.RoleExecutorTest.test_real"])

    def test_querycanceled_chainee_vers_valueerror_reste_reelle(self) -> None:
        # B de la revue #4217 : la dernière exception effective est
        # ValueError ; l'QueryCanceled antérieur ne doit pas la classer
        # environnement.
        output = (
            "======================================================================\n"
            "ERROR: test_x (tests.test_x.T.test_x)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
            "\n"
            "During handling of the above exception, another exception occurred:\n"
            "\n"
            "Traceback (most recent call last):\n"
            "ValueError: invalid application state\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = paquet_auto.TestRun("unité #1", ["tests.test_x"], 1, output, 0.1)
        self.assertFalse(run.inconclusive)
        self.assertTrue(run.real_failure)
        self.assertEqual(
            [failure["test"] for failure in
             paquet_auto.unittest_failures(output)],
            ["tests.test_x.T.test_x"])

    def test_un_querycanceled_final_reste_non_concluant(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_x (tests.test_x.T.test_x)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "RuntimeError: emballage\n"
            "\n"
            "During handling of the above exception, another exception occurred:\n"
            "\n"
            "Traceback (most recent call last):\n"
            "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = paquet_auto.TestRun("unité #1", ["tests.test_x"], 1, output, 0.1)
        self.assertTrue(run.inconclusive)
        self.assertFalse(run.real_failure)
        self.assertEqual(paquet_auto.unittest_failures(output), [])

    def test_une_ligne_source_n_est_pas_une_exception(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_x (tests.test_x.T.test_x)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_x.py\", line 10, in test_x\n"
            "    raise ValueError('invalid')  # QueryCanceled: statement timeout\n"
            "ValueError: invalid application state\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = paquet_auto.TestRun("unité #1", ["tests.test_x"], 1, output, 0.1)
        self.assertFalse(run.inconclusive)
        self.assertTrue(run.real_failure)
        self.assertEqual(
            [failure["test"] for failure in
             paquet_auto.unittest_failures(output)],
            ["tests.test_x.T.test_x"])

    def test_une_ligne_context_apres_un_vrai_rouge_ne_change_rien(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_x (tests.test_x.T.test_x)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"tests/test_x.py\", line 10, in test_x\n"
            "ValueError: invalid application state\n"
            "CONTEXT:  SQL statement \"SELECT 1\"\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = paquet_auto.TestRun("unité #1", ["tests.test_x"], 1, output, 0.1)
        self.assertFalse(run.inconclusive)
        self.assertTrue(run.real_failure)
        self.assertEqual(
            [failure["test"] for failure in
             paquet_auto.unittest_failures(output)],
            ["tests.test_x.T.test_x"])

    def test_context_psycopg_ne_devient_pas_l_exception(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: tearDownClass (tests.test_l56_role_executor.RoleExecutorTest)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
            "CONTEXT:  SQL statement \"SELECT pg_sleep(1)\"\n"
            "PL/pgSQL function inline_code_block line 1 at PERFORM\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 33 tests in 42.0s\n"
            "FAILED (errors=1)\n")
        run = paquet_auto.TestRun(
            "unité #1", ["tests.test_l56_role_executor"], 1, output, 42.0)
        self.assertTrue(run.inconclusive)
        self.assertFalse(run.real_failure)
        self.assertEqual(paquet_auto.unittest_failures(output), [])

    def test_un_valueerror_nu_chainee_reste_reel(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_x (tests.test_x.T.test_x)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "psycopg.errors.QueryCanceled: canceling statement due to statement timeout\n"
            "CONTEXT:  SQL statement \"SELECT pg_sleep(1)\"\n"
            "PL/pgSQL function inline_code_block line 1 at PERFORM\n"
            "\n"
            "During handling of the above exception, another exception occurred:\n"
            "\n"
            "Traceback (most recent call last):\n"
            "ValueError\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = paquet_auto.TestRun("unité #1", ["tests.test_x"], 1, output, 0.1)
        self.assertFalse(run.inconclusive)
        self.assertTrue(run.real_failure)
        self.assertEqual(
            [failure["test"] for failure in
             paquet_auto.unittest_failures(output)],
            ["tests.test_x.T.test_x"])

    def test_un_valueerror_qui_cite_querycanceled_reste_reel(self) -> None:
        output = (
            "======================================================================\n"
            "ERROR: test_x (tests.test_x.T.test_x)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "ValueError: expected QueryCanceled from the driver\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.1s\n"
            "FAILED (errors=1)\n")
        run = paquet_auto.TestRun("unité #1", ["tests.test_x"], 1, output, 0.1)
        self.assertFalse(run.inconclusive)
        self.assertTrue(run.real_failure)
        self.assertEqual(
            [failure["test"] for failure in
             paquet_auto.unittest_failures(output)],
            ["tests.test_x.T.test_x"])

    def check(self, *, own, neighbors):
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="b" * 40)
        green = paquet_auto.TestRun(
            "contrats", ["tests.test_contrat"], 0,
            "Ran 1 test in 0.1s\n\nOK", 0.1)
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               return_value=list(own)), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contrat"]), \
                mock.patch.object(paquet_auto, "run_tests",
                                  side_effect=[self.run_of(), green]):
            return paquet_auto.check_local_tests(
                pathlib.Path("."), composition, timeout=1,
                log=lambda _line: None,
                select=lambda *_a: (neighbors, ""),
                attribute=lambda *_a: self.fail(
                    "une erreur de fixture ne doit jamais être attribuée"))

    def test_module_propre_en_fixture_ne_retire_rien(self) -> None:
        runs, faulty, removals = self.check(
            own=["tests.test_l56_role_executor"], neighbors={})
        self.assertEqual((faulty, removals), ([], []))
        self.assertTrue(runs[0].inconclusive)
        self.assertIn("1 non concluant(s)",
                      paquet_auto._tests_summary(runs))

    def test_module_voisin_en_fixture_ne_retire_rien(self) -> None:
        neighbor = {"module": "tests.test_l56_role_executor",
                    "reason": "carte",
                    "files": ["src/ameesh/cli.py"]}
        runs, faulty, removals = self.check(
            own=["tests.test_own"], neighbors={1: [neighbor]})
        self.assertEqual((faulty, removals), ([], []))
        self.assertTrue(runs[0].inconclusive)


if __name__ == "__main__":
    unittest.main()
