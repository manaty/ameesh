# SPDX-License-Identifier: AGPL-3.0-only
"""#3807 — composition scriptée du paquet, sans agent.

Le banc monte un dépôt git jetable : une unité propre, un candidat en conflit
(écarté sans commit), une unité propre après lui, puis le rejeu d'un manifeste
gelé et le contrôle d'inclusion sur le manifeste produit. Aucune base, aucun
réseau.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "paquet-auto.py"
COMPOSE = REPO / "scripts" / "paquet-compose.py"
INCLUSION = REPO / "scripts" / "paquet-inclusion.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("test_paquet_auto_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_paquet_auto_script"] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script()


def _has_ref(ref: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True)
    return proc.returncode == 0


class PaquetAutoBanc(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paquet-auto-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main", cwd=self.repo)
        self.git("config", "user.name", "banc", cwd=self.repo)
        self.git("config", "user.email", "banc@example.invalid", cwd=self.repo)
        self.git("config", "commit.gpgsign", "false", cwd=self.repo)
        self.write("base.txt", "base\n")
        self.write("shared.txt", "base\n")
        self.git("add", "-A", cwd=self.repo)
        self.git("commit", "-qm", "base", cwd=self.repo)
        self.base = self.rev("HEAD")

    # -- helpers -----------------------------------------------------------
    def git(self, *args: str, cwd: pathlib.Path | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise AssertionError(
                "git %s a échoué : %s" % (" ".join(args), proc.stderr))
        return proc

    def rev(self, ref: str, cwd: pathlib.Path | None = None) -> str:
        return self.git("rev-parse", ref, cwd=cwd).stdout.strip()

    def write(self, name: str, content: str, cwd: pathlib.Path | None = None) -> None:
        path = (cwd or self.repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str, cwd: pathlib.Path | None = None) -> str:
        where = cwd or self.repo
        self.git("add", "-A", cwd=where)
        self.git("commit", "-qm", message, cwd=where)
        return self.rev("HEAD", cwd=where)

    def lot(self, lot: int, name: str, content: str) -> str:
        self.git("switch", "-q", "-c", "lot/%d" % lot, "main", cwd=self.repo)
        self.write(name, content)
        sha = self.commit("#%d : %s" % (lot, name))
        self.git("switch", "-q", "main", cwd=self.repo)
        return sha

    def worktree(self, name: str, head: str = None) -> pathlib.Path:
        path = pathlib.Path(self.tmp.name) / name
        branch = "integration/paquet-%s" % name
        self.git("worktree", "add", "-b", branch, str(path),
                 head or self.base, cwd=self.repo)
        self.addCleanup(self.git, "worktree", "remove", "--force", str(path),
                        cwd=self.repo, check=False)
        return path

    def fixture(self) -> tuple[str, str, str]:
        """Un candidat propre, un conflictuel, un propre après lui."""
        first = self.lot(1, "shared.txt", "un\n")
        conflict = self.lot(2, "shared.txt", "deux\n")
        third = self.lot(3, "trois.txt", "trois\n")
        return first, conflict, third

    # -- témoins -----------------------------------------------------------
    def test_non_concluant_consigne_sans_retirer_d_unite(self) -> None:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "0" * 40, "1" * 40)],
            discarded=[], head="2" * 40)
        unit_run = paquet_auto.TestRun(
            "unité #1", ["tests.test_unit"], 1,
            "OperationalError: could not connect to server", 0.1)
        contract_run = paquet_auto.TestRun(
            "contrats", ["tests.test_contract"], None,
            "did not finish in 1 s", 1.0, timed_out=True)
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               return_value=["tests.test_unit"]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contract"]), \
                mock.patch.object(paquet_auto, "run_tests",
                                  side_effect=[unit_run, contract_run]):
            runs, faulty, _removals = paquet_auto.check_local_tests(
                pathlib.Path(self.tmp.name), composition, timeout=1,
                log=lambda _line: None)
        self.assertEqual(faulty, [])
        self.assertEqual([run.label for run in runs], ["unité #1", "contrats"])
        self.assertTrue(runs[0].inconclusive)
        self.assertTrue(runs[1].inconclusive)
        self.assertFalse(runs[0].real_failure)
        self.assertFalse(runs[1].real_failure)
        manifest = paquet_auto.build_manifest(
            name="banc", target="main", base_sha=self.base, carrier=99,
            composition=composition,
            table="## Composition (1 unité, 1 lot)\n", tests=runs,
            freeze_seconds=0.0)
        self.assertIn("Non concluants (canon §3", manifest)
        self.assertIn("tests.test_unit", manifest)
        self.assertIn("could not connect", manifest)
        self.assertIn("délai dépassé", manifest)

    def test_rouge_reel_retire_l_unite(self) -> None:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "0" * 40, "1" * 40)],
            discarded=[], head="2" * 40)
        red = paquet_auto.TestRun(
            "unité #1", ["tests.test_unit"], 1,
            "FAIL: test_x (tests.test_unit.TestX)\nAssertionError", 0.1)
        green = paquet_auto.TestRun(
            "contrats", ["tests.test_contract"], 0,
            "Ran 1 test in 0.1s\n\nOK", 0.1)
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               return_value=["tests.test_unit"]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contract"]), \
                mock.patch.object(paquet_auto, "run_tests",
                                  side_effect=[red, green]):
            runs, faulty, _removals = paquet_auto.check_local_tests(
                pathlib.Path(self.tmp.name), composition, timeout=1,
                log=lambda _line: None)
        self.assertEqual(faulty, [1])
        self.assertFalse(runs[0].inconclusive)

    def test_run_mixte_fail_et_operationalerror_est_rouge(self) -> None:
        mixed = paquet_auto.TestRun(
            "unité #1", ["tests.test_unit"], 1,
            "======================================================================\n"
            "FAIL: test_regression (tests.test_unit.TestX)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  ...\n"
            "AssertionError: wrong result\n"
            "\n"
            "======================================================================\n"
            "ERROR: test_db (tests.test_unit.TestX)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  ...\n"
            "psycopg.OperationalError: connection refused\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 2 tests in 0.1s\n"
            "\n"
            "FAILED (failures=1, errors=1)\n",
            0.1)
        self.assertTrue(mixed.real_failure)
        self.assertFalse(mixed.inconclusive)
        summary = paquet_auto._tests_summary([mixed])
        self.assertIn("1 échec(s)", summary)
        self.assertIn("0 non concluant(s)", summary)
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "0" * 40, "1" * 40)],
            discarded=[], head="2" * 40)
        green = paquet_auto.TestRun(
            "contrats", ["tests.test_contract"], 0,
            "Ran 1 test in 0.1s\n\nOK", 0.1)
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               return_value=["tests.test_unit"]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contract"]), \
                mock.patch.object(paquet_auto, "run_tests",
                                  side_effect=[mixed, green]):
            runs, faulty, _removals = paquet_auto.check_local_tests(
                pathlib.Path(self.tmp.name), composition, timeout=1,
                log=lambda _line: None)
        self.assertEqual(faulty, [1])
        self.assertFalse(runs[0].inconclusive)

    def test_error_assertion_avec_motif_environment_est_rouge(self) -> None:
        # Le nom du type prime : une AssertionError qui cite « OperationalError »
        # dans son message reste un rouge réel, pas une panne d'environnement.
        run = paquet_auto.TestRun(
            "unité #1", ["tests.test_unit"], 1,
            "======================================================================\n"
            "ERROR: test_regression (tests.test_unit.TestX)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  ...\n"
            "AssertionError: expected OperationalError: connection refused\n"
            "\n"
            "FAILED (errors=1)\n",
            0.1)
        self.assertTrue(run.real_failure)
        self.assertFalse(run.inconclusive)

    def test_contrat_mixte_est_rouge(self) -> None:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "0" * 40, "1" * 40)],
            discarded=[], head="2" * 40)
        green = paquet_auto.TestRun(
            "unité #1", ["tests.test_unit"], 0,
            "Ran 1 test in 0.1s\n\nOK", 0.1)
        mixed = paquet_auto.TestRun(
            "contrats", ["tests.test_contract"], 1,
            "======================================================================\n"
            "FAIL: test_regression (tests.test_contract.TestX)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  ...\n"
            "AssertionError: wrong result\n"
            "\n"
            "======================================================================\n"
            "ERROR: test_db (tests.test_contract.TestX)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  ...\n"
            "psycopg.OperationalError: connection refused\n"
            "\n"
            "FAILED (failures=1, errors=1)\n",
            0.1)
        with mock.patch.object(paquet_auto, "unit_test_modules",
                               return_value=["tests.test_unit"]), \
                mock.patch.object(paquet_auto, "contract_modules",
                                  return_value=["tests.test_contract"]), \
                mock.patch.object(paquet_auto, "run_tests",
                                  side_effect=[green, mixed]):
            with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
                paquet_auto.check_local_tests(
                    pathlib.Path(self.tmp.name), composition, timeout=1,
                    log=lambda _line: None)
        self.assertIn("contrat rouges", str(caught.exception))

    def test_non_concluant_poursuit_jusqu_au_gel(self) -> None:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="c" * 40)
        inconclusive = paquet_auto.TestRun(
            "unité #1", ["tests.test_x"], 1,
            "OperationalError: could not connect", 0.1)
        worktree = pathlib.Path(self.tmp.name) / "wt"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text(
            "", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo), name="banc", target="main", carrier=99,
            base=None, local=False, no_packet_record=True, keep_worktree=False,
            keep_failed_hours=24.0, force=False, tests_timeout=10,
            pilot="pilote")
        calls: dict[str, object] = {}

        def record(name, result):
            def side_effect(*_args, **_kwargs):
                calls[name] = True
                return result
            return side_effect

        with mock.patch.object(paquet_auto, "fresh_base",
                               return_value="a" * 40), \
                mock.patch.object(paquet_auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(paquet_auto, "make_worktree",
                                  return_value=worktree), \
                mock.patch.object(paquet_auto, "compute_candidates",
                                  return_value=([paquet_auto.Candidate(1, "a" * 40)],
                                                {"ready": [1], "waiting": []})), \
                mock.patch.object(paquet_auto, "compose_candidates",
                                  return_value=composition), \
                mock.patch.object(paquet_auto, "run_freeze_checks",
                                  return_value=(0, "ok", 0.1)), \
                mock.patch.object(paquet_auto, "check_local_tests",
                                  return_value=([inconclusive], [], [])), \
                mock.patch.object(paquet_auto, "composition_table",
                                  return_value="## Composition (1 unité, 1 lot)\n"), \
                mock.patch.object(paquet_auto, "write_manifest",
                                  return_value="d" * 40), \
                mock.patch.object(paquet_auto, "run_inclusion",
                                  return_value=(0, "inclusion ok")), \
                mock.patch.object(paquet_auto, "push_branch",
                                  side_effect=record("push", "pushed")), \
                mock.patch.object(paquet_auto, "ensure_pull_request",
                                  side_effect=record("pr", (1, "http://pr"))), \
                mock.patch.object(paquet_auto, "freeze_carrier",
                                  side_effect=record("freeze", "gel")), \
                mock.patch.multiple(paquet_auto,
                                    request_full_ci=mock.Mock(return_value="ci-full"),
                                    note_carrier=mock.Mock(return_value="note"),
                                    pull_request_state=mock.Mock(return_value="OPEN")), \
                mock.patch.object(paquet_auto, "send_mail",
                                  side_effect=record("mail", "sent")), \
                mock.patch.object(paquet_auto, "remove_worktree"):
            self.assertEqual(paquet_auto.compose_packet(args), 0)
        self.assertEqual(set(calls), {"push", "pr", "freeze", "mail"})

    def test_only_keeps_the_parallel_group_and_explains_the_other(self) -> None:
        # #3256 C : un paquet parallèle ne compose que son groupe ; l'autre
        # lot prêt est écrit aux écartés avec le motif du paquet parallèle.
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="c" * 40)
        worktree = pathlib.Path(self.tmp.name) / "wt-only"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text(
            "", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo), name="banc", target="main", carrier=99,
            base=None, local=True, no_packet_record=True, keep_worktree=False,
            keep_failed_hours=24.0, force=False, tests_timeout=10,
            pilot="pilote", only=[1], exclude=[], exclude_reason=[])
        seen: dict = {}

        def compose_side_effect(_worktree, candidates, *_args, **_kwargs):
            seen["lots"] = [candidate.lot for candidate in candidates]
            return composition

        with mock.patch.object(paquet_auto, "fresh_base",
                               return_value="a" * 40), \
                mock.patch.object(paquet_auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(paquet_auto, "make_worktree",
                                  return_value=worktree), \
                mock.patch.object(paquet_auto, "compute_candidates",
                                  return_value=(
                                      [paquet_auto.Candidate(1, "a" * 40),
                                       paquet_auto.Candidate(2, "b" * 40)],
                                      {"ready": [1, 2], "waiting": []})), \
                mock.patch.object(paquet_auto, "compose_candidates",
                                  side_effect=compose_side_effect), \
                mock.patch.object(paquet_auto, "run_freeze_checks",
                                  return_value=(0, "ok", 0.1)), \
                mock.patch.object(paquet_auto, "check_local_tests",
                                  return_value=([], [], [])), \
                mock.patch.object(paquet_auto, "composition_table",
                                  return_value="## Composition (1 unité, 1 lot)\n"), \
                mock.patch.object(paquet_auto, "write_manifest",
                                  return_value="d" * 40), \
                mock.patch.object(paquet_auto, "run_inclusion",
                                  return_value=(0, "inclusion ok")), \
                mock.patch.object(paquet_auto, "remove_worktree"):
            self.assertEqual(paquet_auto.compose_packet(args), 0)
        self.assertEqual(seen["lots"], [1])
        self.assertEqual([(item.lot, item.reason) for item in composition.discarded],
                         [(2, paquet_auto.PARALLEL_EXCLUDE_REASON)])

    def test_only_refuses_a_lot_outside_the_ready_list(self) -> None:
        worktree = pathlib.Path(self.tmp.name) / "wt-only-missing"
        (worktree / "scripts").mkdir(parents=True)
        args = argparse.Namespace(
            repo=str(self.repo), name="banc", target="main", carrier=99,
            base=None, local=True, no_packet_record=True, keep_worktree=False,
            keep_failed_hours=24.0, force=False, tests_timeout=10,
            pilot="pilote", only=[2], exclude=[], exclude_reason=[])
        with mock.patch.object(paquet_auto, "fresh_base",
                               return_value="a" * 40), \
                mock.patch.object(paquet_auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(paquet_auto, "make_worktree",
                                  return_value=worktree), \
                mock.patch.object(paquet_auto, "compute_candidates",
                                  return_value=(
                                      [paquet_auto.Candidate(1, "a" * 40)],
                                      {"ready": [1], "waiting": []})), \
                mock.patch.object(paquet_auto, "remove_worktree"):
            with self.assertRaises(paquet_auto.InputError):
                paquet_auto.compose_packet(args)

    def test_exclude_reason_s_apparie_par_ordre(self) -> None:
        # #4201 : la i-ème raison décrit le i-ème --exclude.
        discards = paquet_auto.exclude_discards(
            [7, 8], ["B de relecture", "rouge local test_l84_serve"], set())
        self.assertEqual([(item.lot, item.reason) for item in discards],
                         [(7, "B de relecture"),
                          (8, "rouge local test_l84_serve")])

    def test_exclude_sans_motif_ecrit_le_motif_neutre(self) -> None:
        # #4201 : sans --exclude-reason, plus de motif générique faux.
        discards = paquet_auto.exclude_discards([7, 8], [], set())
        self.assertEqual([(item.lot, item.reason) for item in discards],
                         [(7, paquet_auto.EXCLUDE_DEFAULT_REASON),
                          (8, paquet_auto.EXCLUDE_DEFAULT_REASON)])
        self.assertEqual(paquet_auto.EXCLUDE_DEFAULT_REASON, "exclu par le pilote")

    def test_exclude_reason_ne_glisse_pas_sur_un_lot_deja_en_erreur(self) -> None:
        # Le lot déjà écarté par la liste calculée n'est pas répété, mais sa
        # raison reste appariée : la raison de #8 ne décrit pas #7.
        discards = paquet_auto.exclude_discards([7, 8], ["sept", "huit"], {7})
        self.assertEqual([(item.lot, item.reason) for item in discards],
                         [(8, "huit")])

    def test_exclude_reason_sans_lot_refuse(self) -> None:
        with self.assertRaises(paquet_auto.InputError):
            paquet_auto.exclude_discards([], ["motif orphelin"], set())

    def test_manifeste_porte_le_motif_exclude_reason(self) -> None:
        # Témoin #4201 : une recomposition --exclude écrit au manifeste le vrai
        # motif du pilote, pas la phrase figée « ci-full rouge attribuée ».
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="c" * 40)
        worktree = pathlib.Path(self.tmp.name) / "wt-exclude"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text(
            "", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo), name="banc", target="main", carrier=99,
            base=None, local=True, no_packet_record=True, keep_worktree=False,
            keep_failed_hours=24.0, force=False, tests_timeout=10,
            pilot="pilote", exclude=[2],
            exclude_reason=["B de relecture posé après la composition"])
        manifests: list[str] = []

        def capture(_worktree, _rel, text, **_kwargs):
            manifests.append(text)
            return "d" * 40

        with mock.patch.object(paquet_auto, "fresh_base",
                               return_value="a" * 40), \
                mock.patch.object(paquet_auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(paquet_auto, "make_worktree",
                                  return_value=worktree), \
                mock.patch.object(paquet_auto, "compute_candidates",
                                  return_value=(
                                      [paquet_auto.Candidate(1, "a" * 40),
                                       paquet_auto.Candidate(2, "e" * 40)],
                                      {"ready": [1, 2], "waiting": []})), \
                mock.patch.object(paquet_auto, "compose_candidates",
                                  return_value=composition), \
                mock.patch.object(paquet_auto, "run_freeze_checks",
                                  return_value=(0, "ok", 0.1)), \
                mock.patch.object(paquet_auto, "check_local_tests",
                                  return_value=([], [], [])), \
                mock.patch.object(paquet_auto, "composition_table",
                                  return_value="## Composition (1 unité, 1 lot)\n"), \
                mock.patch.object(paquet_auto, "write_manifest",
                                  side_effect=capture), \
                mock.patch.object(paquet_auto, "run_inclusion",
                                  return_value=(0, "inclusion ok")), \
                mock.patch.object(paquet_auto, "remove_worktree"):
            self.assertEqual(paquet_auto.compose_packet(args), 0)
        self.assertEqual(len(manifests), 1)
        manifest = manifests[0]
        self.assertIn("| #2 | B de relecture posé après la composition — "
                      "--exclude, recomposition sans lui |", manifest)
        self.assertNotIn("ci-full rouge attribuée", manifest)

    def test_manifeste_echappe_le_motif_du_pilote(self) -> None:
        # Le motif est du texte libre : ni `|` ni saut de ligne dans la table.
        composition = paquet_auto.Composition(
            units=[], discarded=[paquet_auto.Discarded(
                7, "rouge local | test_l84_serve", "")], head="a" * 40)
        manifest = paquet_auto.build_manifest(
            name="banc", target="main", base_sha=self.base, carrier=99,
            composition=composition, table="", tests=[], freeze_seconds=0.0)
        self.assertIn("| #7 | rouge local / test_l84_serve |", manifest)

    def test_worktree_d_echec_ancien_est_purge(self) -> None:
        path = pathlib.Path(self.tmp.name) / "paquet-auto-vieux"
        self.git("worktree", "add", "-b", "tmp/vieux", str(path), self.base,
                 cwd=self.repo)
        removed = paquet_auto.prune_stale_worktrees(
            self.repo, 1, now=time.time() + 3600)
        self.assertIn(str(path), removed)
        self.assertFalse(path.exists())

    def test_reprise_ferme_le_worktree_garde_par_la_tentative_1(self) -> None:
        # #3916 : la tentative 1 a échoué et gardé son worktree sur la branche
        # du paquet ; la reprise (--force) le ferme puis recompose.
        branch = "integration/paquet-h-reprise"
        kept = paquet_auto.make_worktree(self.repo, self.base, branch)
        self.addCleanup(shutil.rmtree, kept, True)
        self.assertTrue(kept.name.startswith("paquet-auto-"))
        logged: list[str] = []
        paquet_auto.clear_local_branch(self.repo, branch, log=logged.append)
        self.assertFalse(kept.exists())
        self.assertIn(str(kept), "\n".join(logged))
        again = paquet_auto.make_worktree(self.repo, self.base, branch)
        self.addCleanup(paquet_auto.remove_worktree, self.repo, again)
        self.assertEqual(paquet_auto.rev(again, "HEAD"), self.base)

    def test_reprise_refuse_un_worktree_qui_n_est_pas_le_sien(self) -> None:
        branch = "integration/paquet-autre"
        path = pathlib.Path(self.tmp.name) / "a-la-main"
        self.git("worktree", "add", "-b", branch, str(path), self.base)
        with self.assertRaises(paquet_auto.InputError):
            paquet_auto.clear_local_branch(self.repo, branch, log=lambda _l: None)
        self.assertTrue(path.exists())

    def test_branche_de_candidat_invalide_refusee_avant_fetch(self) -> None:
        candidate = paquet_auto.Candidate(
            1, "deadbeef" * 5, branch="--upload-pack=evil")
        with self.assertRaises(paquet_auto.InputError):
            paquet_auto.ensure_source(self.repo, candidate)

    def test_source_remise_fetchee_par_sa_ref(self) -> None:
        # #3916 : la remise n'est que sous refs/ameesh/resolutions/ d'origin ;
        # fetcher la branche ne l'apporte pas, sa ref si.
        origin = pathlib.Path(self.tmp.name) / "origin"
        self.git("clone", "-q", "--bare", str(self.repo), str(origin))
        remise = self.lot(7, "remise.txt", "remise\n")
        ref = "refs/ameesh/resolutions/7/main-%s" % remise[:9]
        self.git("push", "-q", str(origin), "%s:%s" % (remise, ref))
        self.git("branch", "-D", "lot/7")
        clone = pathlib.Path(self.tmp.name) / "clone"
        self.git("clone", "-q", "--no-local", str(origin), str(clone))
        self.assertIsNone(paquet_auto.rev(clone, remise))
        candidate = paquet_auto.Candidate(
            7, remise, branch="lot/7", remise_ref=ref)
        paquet_auto.ensure_source(clone, candidate)
        self.assertEqual(candidate.source, remise)

    def test_remise_d_un_autre_lot_refusee_avant_fetch(self) -> None:
        for ref in ("refs/ameesh/resolutions/8/main-abc",
                    "refs/heads/main", "--upload-pack=evil",
                    "refs/ameesh/resolutions/7/"):
            candidate = paquet_auto.Candidate(7, "deadbeef" * 5, remise_ref=ref)
            with self.assertRaises(paquet_auto.InputError, msg=ref):
                paquet_auto.ensure_source(self.repo, candidate)

    def test_conflit_ecarte_sans_rien_commiter(self) -> None:
        first, conflict, third = self.fixture()
        worktree = self.worktree("banc")
        composition = paquet_auto.compose_candidates(
            worktree,
            [paquet_auto.Candidate(1, first),
             paquet_auto.Candidate(2, conflict),
             paquet_auto.Candidate(3, third)],
            99,
            compose_script=COMPOSE,
            record_package=False,
            log=lambda _line: None,
        )
        self.assertEqual([unit.lot for unit in composition.units], [1, 3])
        self.assertEqual(len(composition.discarded), 1)
        self.assertEqual(composition.discarded[0].lot, 2)
        self.assertEqual(composition.discarded[0].reason, "conflit, coupure suivante")
        self.assertIn("shared.txt", composition.discarded[0].detail)
        # Aucun commit du candidat conflictuel, aucune fusion en cours, arbre propre.
        log = self.git("log", "--first-parent", "--format=%s",
                       cwd=worktree).stdout
        self.assertNotIn("#2", log)
        self.assertEqual(
            self.git("status", "--porcelain", cwd=worktree).stdout, "")
        self.assertNotEqual(
            self.git("rev-parse", "--verify", "--quiet", "MERGE_HEAD",
                     cwd=worktree, check=False).returncode, 0)
        self.assertEqual(self.rev("HEAD", cwd=worktree), composition.head)

    def _fake_candidates(self, payload: dict, code: int) -> str:
        data = pathlib.Path(self.tmp.name) / "candidats.json"
        data.write_text(json.dumps(payload), encoding="utf-8")
        fake = (
            "import pathlib, sys\n"
            "print(pathlib.Path(%r).read_text(encoding='utf-8'))\n"
            "sys.exit(%d)\n" % (str(data), code)
        )
        self.write("scripts/paquet-candidats.py", fake)
        return self.commit("faux candidats")

    def test_lot_en_erreur_jamais_compose(self) -> None:
        payload = {
            "schema": "ameesh-packet-candidates/1",
            "base_source": "option",
            "ready": [7, 8],
            "candidates": [
                {"work_item": 7, "source": self.base},
                {"work_item": 8, "source": self.base},
            ],
            "errors": ["#7 : porte illisible"],
            "errors_by_lot": [{"work_item": 7, "error": "porte illisible"}],
        }
        head = self._fake_candidates(payload, 3)
        worktree = self.worktree("candidats", head)
        ready = pathlib.Path(self.tmp.name) / "prets.txt"
        candidates, got = paquet_auto.compute_candidates(
            worktree, "main", self.base, ready)
        self.assertEqual([candidate.lot for candidate in candidates], [8])
        errors = paquet_auto.error_discards(got)
        self.assertEqual([item.lot for item in errors], [7])
        self.assertIn("jamais composé", errors[0].reason)
        self.assertIn("porte illisible", errors[0].detail)

    def test_remise_portee_par_le_candidat(self) -> None:
        ref = "refs/ameesh/resolutions/7/main-fb029a526"
        payload = {
            "schema": "ameesh-packet-candidates/1",
            "base_source": "option",
            "ready": [7, 8],
            "candidates": [
                {"work_item": 7, "source": self.base,
                 "remise": {"ref": ref, "sha": self.base}},
                {"work_item": 8, "source": self.base, "remise": None},
            ],
            "errors": [],
        }
        head = self._fake_candidates(payload, 0)
        worktree = self.worktree("candidats-remise", head)
        ready = pathlib.Path(self.tmp.name) / "prets.txt"
        candidates, _got = paquet_auto.compute_candidates(
            worktree, "main", self.base, ready)
        self.assertEqual([c.remise_ref for c in candidates], [ref, ""])

    def test_erreur_globale_interrompt_la_composition(self) -> None:
        payload = {
            "schema": "ameesh-packet-candidates/1",
            "base_source": "option",
            "ready": [],
            "candidates": [],
            "errors": ["base illisible"],
            "errors_by_lot": [],
        }
        head = self._fake_candidates(payload, 1)
        worktree = self.worktree("candidats-erreur", head)
        ready = pathlib.Path(self.tmp.name) / "prets.txt"
        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            paquet_auto.compute_candidates(worktree, "main", self.base, ready)
        self.assertIn("base illisible", str(caught.exception))

    def test_manifeste_lu_par_le_controle_d_inclusion(self) -> None:
        first, conflict, third = self.fixture()
        worktree = self.worktree("inclusion")
        composition = paquet_auto.compose_candidates(
            worktree,
            [paquet_auto.Candidate(1, first),
             paquet_auto.Candidate(2, conflict),
             paquet_auto.Candidate(3, third)],
            99,
            compose_script=COMPOSE,
            record_package=False,
            log=lambda _line: None,
        )
        table = paquet_auto.composition_table(worktree, self.base, COMPOSE)
        manifest = paquet_auto.build_manifest(
            name="inclusion", target="main", base_sha=self.base, carrier=99,
            composition=composition, table=table, tests=[], freeze_seconds=0.0)
        rel = "reviews/integration-paquet-inclusion.md"
        self.write(rel, manifest, cwd=worktree)
        head = self.commit("manifeste", cwd=worktree)
        ready = pathlib.Path(self.tmp.name) / "prets.txt"
        ready.write_text("#1\n#2\n#3\n", encoding="utf-8")
        expected = pathlib.Path(self.tmp.name) / "attendus.txt"
        expected.write_text("#1\n#3\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(INCLUSION), "--repo", str(worktree),
             "--manifest", str(worktree / rel), "--head", head,
             "--ready-file", str(ready), "--expected-file", str(expected)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("VERDICT : aucun défaut", proc.stdout)
        # La table d'admissions ne cite que les lots composés : #2 est écarté
        # au manifeste, pas attendu.
        parse = self._parse_manifest(worktree / rel)
        self.assertEqual(parse.admissions, [1, 3])
        self.assertIn(2, parse.ecartes)
        self.assertIn("conflit", parse.ecartes[2])

    def _parse_manifest(self, path: pathlib.Path):
        spec = importlib.util.spec_from_file_location("test_paquet_inclusion_script",
                                                      INCLUSION)
        module = importlib.util.module_from_spec(spec)
        sys.modules["test_paquet_inclusion_script"] = module
        spec.loader.exec_module(module)
        return module.parse_manifest(path)

    def test_rejeu_rend_le_meme_arbre(self) -> None:
        first = self.lot(11, "onze.txt", "onze\n")
        second = self.lot(12, "douze.txt", "douze\n")
        reference = self.worktree("reference")
        composition = paquet_auto.compose_candidates(
            reference,
            [paquet_auto.Candidate(11, first),
             paquet_auto.Candidate(12, second)],
            99, compose_script=COMPOSE, record_package=False,
            log=lambda _line: None)
        table = paquet_auto.composition_table(reference, self.base, COMPOSE)
        manifest = paquet_auto.build_manifest(
            name="fixture", target="main", base_sha=self.base, carrier=99,
            composition=composition, table=table, tests=[], freeze_seconds=0.0)
        self.write("reviews/integration-paquet-fixture.md", manifest, cwd=reference)
        ref = self.commit("manifeste de référence", cwd=reference)
        expected_tree = self.rev("HEAD^{tree}", cwd=reference)
        self.git("worktree", "remove", "--force", str(reference), cwd=self.repo)
        code = paquet_auto.replay(
            self.repo, ref, "fixture", self.base, compose_script=COMPOSE,
            log=lambda _line: None)
        self.assertEqual(code, 0)
        # Le rejeu supprime son worktree et sa branche temporaire ; l'arbre
        # comparé est celui de la référence.
        branches = self.git("branch", "--list", "tmp/paquet-auto-rejeu-*",
                            cwd=self.repo).stdout
        self.assertEqual(branches.strip(), "")
        self.assertEqual(self.rev(ref + "^{tree}"), expected_tree)
        # Admission (#3813) : #12 en erreur dans la liste calculée est refusé ;
        # une liste sans erreur laisse passer le même manifeste.
        bad = pathlib.Path(self.tmp.name) / "candidats-legacy.json"
        bad.write_text(json.dumps({
            "base": self.base, "ready": [11],
            "errors": ["#12 : porte illisible"],
        }), encoding="utf-8")
        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            paquet_auto.replay(
                self.repo, ref, "fixture", self.base, compose_script=COMPOSE,
                candidates_json=bad, log=lambda _line: None)
        self.assertIn("#12", str(caught.exception))
        self.assertIn("erreur", str(caught.exception))
        good = pathlib.Path(self.tmp.name) / "candidats-prets.json"
        good.write_text(json.dumps({
            "base": self.base, "ready": [11, 12], "errors": [],
        }), encoding="utf-8")
        self.assertEqual(paquet_auto.replay(
            self.repo, ref, "fixture", self.base, compose_script=COMPOSE,
            candidates_json=good, log=lambda _line: None), 0)

    @unittest.skipUnless(_has_ref("b57c0380") and _has_ref("f444955b"),
                         "témoin h-31 v3 : références absentes de ce clone")
    def test_rejeu_h31_v3_refuse_le_lot_en_erreur(self) -> None:
        # Le snapshot minimal des entrées h-31 : #3506 est en erreur de porte,
        # les six autres unités de la v4 sont prêtes.
        snapshot = pathlib.Path(self.tmp.name) / "h31-candidats.json"
        snapshot.write_text(json.dumps({
            "base": "f444955b2eebcde5b096175e2c52e7a32bceda06",
            "ready": [3779, 3782, 3755, 3792, 3788, 3735],
            "errors": ["#3506 : provenance de gel illisible (cas #3781)"],
        }), encoding="utf-8")
        with self.assertRaises(paquet_auto.PaquetAutoError) as caught:
            paquet_auto.replay(
                REPO, "b57c0380", "h-20261026-31", "f444955b",
                candidates_json=snapshot, log=lambda _line: None)
        self.assertIn("#3506", str(caught.exception))
        self.assertIn("erreur", str(caught.exception))

    @unittest.skipUnless(_has_ref("fc59a14e") and _has_ref("f444955b"),
                         "témoin h-31 v4 : références absentes de ce clone")
    def test_rejeu_h31_v4_arbre_identique(self) -> None:
        self.assertEqual(
            paquet_auto.main(["--repo", str(REPO), "--replay", "fc59a14e",
                              "--name", "h-20261026-31", "--base", "f444955b"]),
            0)


if __name__ == "__main__":
    unittest.main()
