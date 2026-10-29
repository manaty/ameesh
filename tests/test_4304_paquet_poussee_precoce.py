# SPDX-License-Identifier: AGPL-3.0-only
"""#4304 — la branche de paquet est poussée dès la composition (canon §6).

Constat h-07 (28/10) : `paquet-auto.py` ne poussait `integration/paquet-*`
qu'après les tests par unité (20 min à plus d'une heure) ; la branche restait
invisible et la CI complète ne partait pas. Désormais, dès les freeze-checks
et l'inclusion verts : poussée, PR, gel du porteur et demande ci-full, puis les
tests par unité, en parallèle de la CI. Un rouge retire l'unité : la tête
recomposée remplace la poussée. Le manifeste poussé avant les tests dispense
toujours de la prévalidation (#4136), sinon le gel relancerait l'offload.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import prevalidation_calls as calls

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "paquet-auto.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("test_4304_paquet_auto", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_4304_paquet_auto"] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script()

NAME = "h-20261028-07"
BRANCH = "integration/paquet-" + NAME


def green(label="unité #1"):
    return paquet_auto.TestRun(label, ["tests.test_x"], 0, "Ran 1 test in 0.1s\n\nOK\n", 0.1)


class Flux:
    """Le flux de `compose_packet`, chaque geste extérieur consigné dans l'ordre."""

    def __init__(self, test, *, tests, compositions, states=("OPEN",), local=False,
                 record=False, **extra):
        self.test = test
        self.extra = extra
        self.events: list[tuple] = []
        self.tests = list(tests)
        self.compositions = list(compositions)
        self.states = list(states)
        self.local = local
        self.record = record
        self.heads = iter("%x" % n * 40 for n in range(1, 10))
        self.manifests: list = []

    def log(self, name, result=None):
        def side_effect(*args, **kwargs):
            self.events.append((name, args, kwargs))
            return result
        return side_effect

    def run(self) -> int:
        tmp = tempfile.mkdtemp(prefix="paquet-4304-")
        self.test.addCleanup(shutil.rmtree, tmp, True)
        worktree = pathlib.Path(tmp) / "wt"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text("")
        args = argparse.Namespace(
            repo=tmp, name=NAME, target="main", carrier=4304, base=None,
            local=self.local, no_packet_record=not self.record, keep_worktree=False,
            keep_failed_hours=24.0, force=False, tests_timeout=10,
            pilot="mesh-design", sender="mesh-design", **self.extra)
        first, *rest = self.compositions

        def recompose(*args, **kwargs):
            self.events.append(("recompose", args, kwargs))
            return rest.pop(0)

        def manifest(**kwargs):
            self.manifests.append(kwargs["tests"])
            return "manifeste"

        def write(*_args, **_kwargs):
            head = next(self.heads)
            self.events.append(("manifest", (head,), {}))
            return head

        def tests(*_args, **kwargs):
            self.events.append(("tests", (), {}))
            entry = self.tests.pop(0)
            # #4463 : une entrée appelable joue la garde des séries
            return entry(**kwargs) if callable(entry) else entry

        def state(*_args):
            self.events.append(("state", (), {}))
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]

        patches = [
            mock.patch.object(paquet_auto, "check_epoch"),
            mock.patch.object(paquet_auto, "fresh_base", return_value="a" * 40),
            mock.patch.object(paquet_auto, "prune_stale_worktrees", return_value=[]),
            mock.patch.object(paquet_auto, "make_worktree", return_value=worktree),
            mock.patch.object(paquet_auto, "compute_candidates", return_value=(
                [paquet_auto.Candidate(1, "a" * 40), paquet_auto.Candidate(2, "c" * 40)],
                {"ready": [1, 2], "waiting": []})),
            mock.patch.object(paquet_auto, "compose_candidates", return_value=first),
            mock.patch.object(paquet_auto, "recompose", side_effect=recompose),
            # #4501 : la liaison en base est mockée, mais ses arguments sont
            # consignés (tête poussée attendue).
            mock.patch.object(paquet_auto, "remote_branch", return_value=None),
            mock.patch.object(paquet_auto, "ensure_carrier_branch"),
            mock.patch.object(paquet_auto, "record_packet",
                              side_effect=self.log("record", "record")),
            mock.patch.object(paquet_auto, "notify_removals", return_value=[]),
            mock.patch.object(paquet_auto, "run_freeze_checks",
                              side_effect=self.log("freeze-checks", (0, "ok", 0.1))),
            mock.patch.object(paquet_auto, "check_local_tests", side_effect=tests),
            mock.patch.object(paquet_auto, "composition_table", return_value="## Composition\n"),
            mock.patch.object(paquet_auto, "build_manifest", side_effect=manifest),
            mock.patch.object(paquet_auto, "write_manifest", side_effect=write),
            mock.patch.object(paquet_auto, "run_inclusion",
                              side_effect=self.log("inclusion", (0, "ok"))),
            mock.patch.object(paquet_auto, "pull_request_state", side_effect=state),
            mock.patch.object(paquet_auto, "push_branch", side_effect=self.log("push", "poussée")),
            mock.patch.object(paquet_auto, "ensure_pull_request",
                              side_effect=self.log("pr", (3281, "http://pr"))),
            mock.patch.object(paquet_auto, "freeze_carrier",
                              side_effect=self.log("freeze", "gel")),
            mock.patch.object(paquet_auto, "request_full_ci",
                              side_effect=self.log("ci-full", "ci-full demandée")),
            mock.patch.object(paquet_auto, "withdraw_packet",
                              side_effect=self.log("withdraw", [])),
            mock.patch.object(paquet_auto, "note_carrier", side_effect=self.log("note", "note")),
            mock.patch.object(paquet_auto, "send_mail", side_effect=self.log("mail", "envoyé")),
            mock.patch.object(paquet_auto, "remove_worktree"),
            # #4329 : le verrou de composition exige un dépôt git réel ; ce
            # banc monte un dépôt factice et mocke déjà tous les gestes git.
            mock.patch.object(paquet_auto, "PackageLock"),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return paquet_auto.compose_packet(args)

    def names(self) -> list[str]:
        return [event[0] for event in self.events]

    def calls(self, name) -> list[tuple]:
        return [event for event in self.events if event[0] == name]


class PousseePrecoceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.two = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40),
                   paquet_auto.Unit(2, "c" * 40, "d" * 40)],
            discarded=[], head="e" * 40)
        self.one = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="f" * 40)
        self.none = paquet_auto.Composition(units=[], discarded=[], head="a" * 40)

    def test_poussee_gel_et_ci_full_avant_le_premier_test_d_unite(self) -> None:
        flux = Flux(self, tests=[([green()], [], [])], compositions=[self.two])
        self.assertEqual(flux.run(), 0)
        names = flux.names()
        first_test = names.index("tests")
        for gesture in ("freeze-checks", "manifest", "inclusion", "push", "pr",
                        "freeze", "ci-full"):
            self.assertLess(names.index(gesture), first_test, (gesture, names))
        self.assertEqual(names[:first_test],
                         ["freeze-checks", "manifest", "inclusion", "push", "pr",
                          "freeze", "ci-full"])
        head = flux.calls("manifest")[0][1][0]
        self.assertEqual(flux.calls("freeze")[0][1][2], head)
        self.assertEqual(flux.calls("ci-full")[0][1][2], head)
        self.assertIn("tests par unité en cours", flux.calls("freeze")[0][1][3])
        # le manifeste poussé ne prétend aucun test ; la PR finale les porte
        self.assertEqual(flux.manifests, [None])
        self.assertEqual(len(flux.calls("pr")), 2)
        self.assertIn("Tests par unité : en cours", flux.calls("pr")[0][1][4])
        self.assertIn("Tests locaux : 1 séries", flux.calls("pr")[1][1][4])
        self.assertEqual(names[-2:], ["note", "mail"])
        self.assertEqual(len(flux.calls("push")), 1)

    def test_rouge_d_unite_recompose_et_remplace_la_poussee(self) -> None:
        removal = paquet_auto.Removal(2, tests=["tests.test_x.T.test_y"], attributed=True)
        red = paquet_auto.TestRun("unité #2", ["tests.test_x"], 1,
                                  "FAIL: test_y (tests.test_x.T.test_y)\n", 0.1)
        flux = Flux(self, tests=[([red], [2], [removal]), ([green()], [], [])],
                    compositions=[self.two, self.one])
        self.assertEqual(flux.run(), 0)
        pushes = flux.calls("push")
        self.assertEqual(len(pushes), 2)
        first, second = [event[1][0] for event in flux.calls("manifest")]
        self.assertIsNone(pushes[0][2]["force_lease"])
        # la recomposition remplace la tête que ce lancement a poussée
        self.assertEqual(pushes[1][2]["force_lease"], first)
        self.assertEqual([event[1][2] for event in flux.calls("freeze")], [first, second])
        self.assertEqual([event[1][2] for event in flux.calls("ci-full")], [first, second])
        names = flux.names()
        # la PR est relue avant de pousser la recomposition
        second_push = [i for i, name in enumerate(names) if name == "push"][1]
        self.assertEqual(names[second_push - 1], "state")
        self.assertEqual(flux.manifests, [None, None])

    def test_recomposition_du_meme_composeur_dit_la_tete_poussee(self) -> None:
        """#4501 : la liaison d'une recomposition nomme la tête qu'elle remplace.

        Le composeur qui a poussé la tête détient son bail (`lease`) : la
        première liaison ne remplace rien (`expect_pushed` vide), la seconde
        dit la tête poussée du même lancement, que `packet record` reconnaît.
        """
        removal = paquet_auto.Removal(2, tests=["tests.test_x.T.test_y"], attributed=True)
        red = paquet_auto.TestRun("unité #2", ["tests.test_x"], 1,
                                  "FAIL: test_y (tests.test_x.T.test_y)\n", 0.1)
        flux = Flux(self, tests=[([red], [2], [removal]), ([green()], [], [])],
                    compositions=[self.two, self.one], record=True)
        self.assertEqual(flux.run(), 0)
        first, second = [event[1][0] for event in flux.calls("manifest")]
        self.assertEqual([event[2].get("expect_pushed") for event in flux.calls("record")],
                         ["", first])
        self.assertEqual(flux.calls("recompose")[0][2]["expect_pushed"], first)
        self.assertNotEqual(second, first)

    def test_pr_close_pendant_les_tests_rien_n_est_recompose(self) -> None:
        # #4322 : l'état de la PR est lu avant toute recomposition (la fusion
        # au vert pendant les tests est couverte par test_4322)
        removal = paquet_auto.Removal(2, tests=["t"], attributed=True)
        red = paquet_auto.TestRun("unité #2", ["tests.test_x"], 1, "FAIL: t\n", 0.1)
        flux = Flux(self, tests=[([red], [2], [removal])],
                    compositions=[self.two, self.one], states=("CLOSED",))
        with self.assertRaisesRegex(paquet_auto.PaquetAutoError, "closed"):
            flux.run()
        self.assertEqual(len(flux.calls("push")), 1)
        self.assertEqual(len(flux.calls("manifest")), 1)

    def test_plus_aucune_unite_apres_poussee_retire_le_paquet(self) -> None:
        removal = paquet_auto.Removal(1, tests=["t"], attributed=True)
        red = paquet_auto.TestRun("unité #1", ["tests.test_x"], 1, "FAIL: t\n", 0.1)
        flux = Flux(self, tests=[([red], [1], [removal])],
                    compositions=[self.one, self.none])
        out = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(
                " ".join(str(x) for x in a))):
            self.assertEqual(flux.run(), 0)
        withdraw = flux.calls("withdraw")
        self.assertEqual(len(withdraw), 1)
        self.assertEqual(withdraw[0][1][1:3], (4304, 3281))
        # paquet-horaire clôt le porteur sur cette ligne (after_compose)
        self.assertTrue(any(line.startswith("aucune unité après recomposition")
                            for line in out), out)
        self.assertEqual(flux.calls("mail"), [])

    def test_local_ne_pousse_rien_et_joue_les_tests(self) -> None:
        flux = Flux(self, tests=[([green()], [], [])], compositions=[self.two], local=True)
        self.assertEqual(flux.run(), 0)
        for gesture in ("push", "pr", "freeze", "ci-full", "note", "mail"):
            self.assertEqual(flux.calls(gesture), [], gesture)
        self.assertEqual(len(flux.calls("tests")), 1)


class ManifesteTest(unittest.TestCase):
    def manifest(self) -> str:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)], discarded=[], head="c" * 40)
        return paquet_auto.build_manifest(
            name=NAME, target="main", base_sha="a" * 40, carrier=4304,
            composition=composition, table="## Composition\n", tests=None,
            freeze_seconds=1.0)

    def test_le_manifeste_dit_les_tests_en_parallele(self) -> None:
        text = self.manifest()
        self.assertIn(paquet_auto.TESTS_PARALLEL_LINE, text)
        self.assertNotIn("tests exécutés", text)

    def test_la_prevalidation_dispense_le_manifeste_pousse_avant_les_tests(self) -> None:
        root = tempfile.mkdtemp(prefix="ameesh-4304-")
        self.addCleanup(shutil.rmtree, root, True)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def git(*args):
            return subprocess.run(["git", "-C", root, *args], check=True, env=env,
                                  capture_output=True, text=True).stdout.strip()

        git("init", "-q", "-b", "main")
        path = pathlib.Path(root) / paquet_auto.manifest_rel(NAME)
        path.parent.mkdir(parents=True)
        path.write_text(self.manifest(), encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "manifeste")
        head = git("rev-parse", "HEAD")
        got = calls.composer_tested(None, None, {"id": 4304, "branch": BRANCH},
                                    head, repo=root)
        self.assertEqual(got, {"manifest": paquet_auto.manifest_rel(NAME), "sha": head,
                               "tests": 0, "parallel": True})
        self.assertIn("in parallel with the full CI", calls.skipped_text(got))


if __name__ == "__main__":
    unittest.main()
