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
import os
import pathlib
import shutil
import socket
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

    def test_only_discards_a_group_lot_that_is_no_longer_ready(self) -> None:
        # B2 (auditeur #4275) : `resume`/`recompose` repassent le même groupe ;
        # un lot devenu non prêt est écarté avec son motif, pas une erreur qui
        # garde la cible réservée.
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[], head="c" * 40)
        worktree = pathlib.Path(self.tmp.name) / "wt-only-missing"
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text(
            "", encoding="utf-8")
        args = argparse.Namespace(
            repo=str(self.repo), name="banc", target="main", carrier=99,
            base=None, local=True, no_packet_record=True, keep_worktree=False,
            keep_failed_hours=24.0, force=False, tests_timeout=10,
            pilot="pilote", only=[1, 2], exclude=[], exclude_reason=[])
        seen: dict = {}

        def compose_side_effect(_worktree, candidates, *_args, **_kwargs):
            seen["lots"] = [candidate.lot for candidate in candidates]
            return composition

        payload = {
            "ready": [1], "waiting": [2],
            "candidates": [
                {"work_item": 1, "ready": True, "causes": [], "error": ""},
                {"work_item": 2, "ready": False, "error": "",
                 "causes": [{"code": "verdicts-missing"}]}]}
        with mock.patch.object(paquet_auto, "fresh_base",
                               return_value="a" * 40), \
                mock.patch.object(paquet_auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(paquet_auto, "make_worktree",
                                  return_value=worktree), \
                mock.patch.object(paquet_auto, "compute_candidates",
                                  return_value=(
                                      [paquet_auto.Candidate(1, "a" * 40)],
                                      payload)), \
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
        self.assertEqual(
            [(item.lot, item.reason) for item in composition.discarded],
            [(2, paquet_auto.PARALLEL_GROUP_NOT_READY)])
        self.assertIn("verdicts-missing", composition.discarded[0].detail)

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

    # -- #4581 : la recomposition garde les membres de la composition ---------
    def test_keep_composition_garde_les_membres_a_leur_source(self) -> None:
        # Témoin #4581 : composition A = {1, 2, 3}, lot 4 prêt, --exclude 2 :
        # on recompose {1, 3} ; 4 est nommé pour la coupure suivante.
        first = self.lot(1, "un.txt", "1\n")
        second = self.lot(2, "deux.txt", "2\n")
        third = self.lot(3, "trois.txt", "3\n")
        candidates = [paquet_auto.Candidate(1, first),
                      paquet_auto.Candidate(2, second),
                      paquet_auto.Candidate(3, third),
                      paquet_auto.Candidate(4, "d" * 40)]
        specs = [([1], first), ([2], second), ([3], third)]
        members, discarded = paquet_auto.keep_composition(
            candidates, specs, worktree=self.repo, already_discarded={2})
        self.assertEqual([member.lot for member in members], [1, 3])
        self.assertEqual([item.lot for item in discarded], [4])
        self.assertEqual(discarded[0].reason, paquet_auto.KEEP_READY_REASON)
        self.assertEqual(discarded[0].reason, "coupure suivante : recomposition")

    def test_keep_composition_source_abregee_est_reprise(self) -> None:
        # #4581 (auditeur) : le manifeste écrit la source en --short=12 et le
        # candidat porte le SHA complet ; la source composée est résolue dans
        # le worktree (second parent de la tête gelée) avant comparaison.
        first = self.lot(1, "un.txt", "1\n")
        members, discarded = paquet_auto.keep_composition(
            [paquet_auto.Candidate(1, first)], [([1], first[:12])],
            worktree=self.repo, already_discarded=set())
        self.assertEqual([member.lot for member in members], [1])
        self.assertEqual(discarded, [])

    def test_keep_composition_source_composee_absente_est_ecartee(self) -> None:
        first = self.lot(1, "un.txt", "1\n")
        members, discarded = paquet_auto.keep_composition(
            [paquet_auto.Candidate(1, first)], [([1], "0" * 12)],
            worktree=self.repo, already_discarded=set())
        self.assertEqual(members, [])
        self.assertEqual([item.lot for item in discarded], [1])
        self.assertEqual(discarded[0].reason, paquet_auto.KEEP_NOT_READY_REASON)
        self.assertIn("introuvable", discarded[0].detail)

    def test_keep_composition_ecarte_une_tete_regelee(self) -> None:
        # #4581 : un membre gelé sur une nouvelle tête n'est jamais réadmis
        # sans ci de paquet sur cette tête ; il va à la coupure suivante.
        first = self.lot(1, "un.txt", "1\n")
        second = self.lot(2, "deux.txt", "2\n")
        third = self.lot(3, "trois.txt", "3\n")
        refrozen = self.lot(4, "quatre.txt", "4\n")
        candidates = [paquet_auto.Candidate(1, first),
                      paquet_auto.Candidate(2, refrozen),
                      paquet_auto.Candidate(3, third)]
        specs = [([1], first[:12]), ([2], second[:12]), ([3], third[:12])]
        members, discarded = paquet_auto.keep_composition(
            candidates, specs, worktree=self.repo, already_discarded=set())
        self.assertEqual([member.lot for member in members], [1, 3])
        self.assertEqual([item.lot for item in discarded], [2])
        self.assertEqual(discarded[0].reason, paquet_auto.KEEP_REFROZEN_REASON)
        self.assertIn("nouvelle tête", discarded[0].detail)

    def test_keep_composition_nomme_le_membre_absent(self) -> None:
        first = self.lot(1, "un.txt", "1\n")
        second = self.lot(2, "deux.txt", "2\n")
        third = self.lot(3, "trois.txt", "3\n")
        candidates = [paquet_auto.Candidate(1, first),
                      paquet_auto.Candidate(3, third)]
        specs = [([1], first), ([2], second), ([3], third)]
        members, discarded = paquet_auto.keep_composition(
            candidates, specs, worktree=self.repo, already_discarded=set())
        self.assertEqual([member.lot for member in members], [1, 3])
        self.assertEqual([item.lot for item in discarded], [2])
        self.assertEqual(discarded[0].reason, paquet_auto.KEEP_NOT_READY_REASON)


    def test_keep_composition_ecarte_une_resolution(self) -> None:
        # Une unité à plusieurs lots ne se reprend pas sans la relire ; chaque
        # lot est nommé aux écartés, sinon les suivants ne seraient ni composés
        # ni expliqués au contrôle d'inclusion (#4593).
        candidates = [paquet_auto.Candidate(1, "a" * 40),
                      paquet_auto.Candidate(2, "b" * 40)]
        members, discarded = paquet_auto.keep_composition(
            candidates, [([1, 2], "a" * 40)], worktree=self.repo, already_discarded=set())
        self.assertEqual(members, [])
        self.assertEqual([item.lot for item in discarded], [1, 2])
        self.assertTrue(all("résolution" in item.detail for item in discarded))
        self.assertEqual({item.reason for item in discarded},
                         {paquet_auto.KEEP_NOT_READY_REASON})

    def test_keep_composition_lit_le_manifeste_gele(self) -> None:
        manifest = ("# Paquet\n\n## Composition (2 unités, 2 lots)\n\n"
                    "| unité | lot(s) | source | commit | note |\n"
                    "|---|---|---|---|---|\n"
                    "| 1 | #7 | `%s` | `1111111` | composition |\n"
                    "| 2 | #8 | `%s` | `2222222` | composition |\n"
                    % ("a" * 12, "b" * 12))
        self.write("reviews/integration-paquet-banc.md", manifest)
        self.git("add", "-A", cwd=self.repo)
        self.git("commit", "-qm", "manifeste", cwd=self.repo)
        specs = paquet_auto.composition_specs(self.repo, self.rev("HEAD"), "banc")
        self.assertEqual(specs, [([7], "a" * 12), ([8], "b" * 12)])

    def test_keep_composition_ref_inconnue_refusee(self) -> None:
        with self.assertRaises(paquet_auto.InputError):
            paquet_auto.composition_specs(self.repo, "0" * 40, "banc")

    def test_manifeste_nomme_les_prets_non_admis(self) -> None:
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, "a" * 40, "b" * 40)],
            discarded=[paquet_auto.Discarded(
                4, paquet_auto.KEEP_READY_REASON, "hors composition conservée")],
            head="c" * 40)
        manifest = paquet_auto.build_manifest(
            name="banc", target="main", base_sha=self.base, carrier=99,
            composition=composition, table="", tests=[], freeze_seconds=0.0)
        self.assertIn("| #4 | coupure suivante : recomposition — hors composition "
                      "conservée |", manifest)

    def test_recomposition_keep_composition_borne_les_candidats(self) -> None:
        # #4581 : au niveau de la composition, --exclude 2 + la composition
        # précédente {1, 2} ne composent que #1 ; #3, prêt depuis, est nommé aux
        # écartés pour la coupure suivante, jamais composé.
        first = self.lot(1, "un.txt", "1\n")
        second = self.lot(2, "deux.txt", "2\n")
        manifest = ("# Paquet\n\n## Composition (2 unités, 2 lots)\n\n"
                    "| unité | lot(s) | source | commit | note |\n"
                    "|---|---|---|---|---|\n"
                    "| 1 | #1 | `%s` | `1111111` | composition |\n"
                    "| 2 | #2 | `%s` | `2222222` | composition |\n"
                    % (first[:12], second[:12]))
        self.write("reviews/integration-paquet-banc.md", manifest)
        keep = self.commit("manifeste")
        worktree = pathlib.Path(self.tmp.name) / "wt-keep"
        self.git("worktree", "add", "--detach", str(worktree), self.base,
                 cwd=self.repo)
        self.addCleanup(paquet_auto.remove_worktree, self.repo, worktree)
        (worktree / "scripts").mkdir(parents=True)
        (worktree / "scripts" / "paquet-compose.py").write_text(
            "", encoding="utf-8")
        composition = paquet_auto.Composition(
            units=[paquet_auto.Unit(1, first, "d" * 40)],
            discarded=[], head="e" * 40)
        args = argparse.Namespace(
            repo=str(self.repo), name="banc", target="main", carrier=99,
            base=None, local=True, no_packet_record=True, keep_worktree=False,
            keep_failed_hours=24.0, force=True, tests_timeout=10,
            pilot="pilote", exclude=[2], exclude_reason=["rouge ci-full"],
            keep_composition=keep)
        seen: dict[str, list[int]] = {}
        manifests: list[str] = []

        def fake_compose(_worktree, candidates, *_args, **_kwargs):
            seen["candidates"] = [candidate.lot for candidate in candidates]
            return composition

        def capture(_worktree, _rel, text, **_kwargs):
            manifests.append(text)
            return "d" * 40

        with mock.patch.object(paquet_auto, "fresh_base",
                               return_value=self.base), \
                mock.patch.object(paquet_auto, "prune_stale_worktrees",
                                  return_value=[]), \
                mock.patch.object(paquet_auto, "make_worktree",
                                  return_value=worktree), \
                mock.patch.object(paquet_auto, "compute_candidates",
                                  return_value=(
                                      [paquet_auto.Candidate(1, first),
                                       paquet_auto.Candidate(2, second),
                                       paquet_auto.Candidate(3, "c" * 40)],
                                      {"ready": [1, 2, 3], "waiting": []})), \
                mock.patch.object(paquet_auto, "compose_candidates",
                                  side_effect=fake_compose), \
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
        self.assertEqual(seen["candidates"], [1])
        self.assertEqual(len(manifests), 1)
        self.assertIn("| #2 | rouge ci-full — --exclude, recomposition sans lui |",
                      manifests[0])
        self.assertIn("| #3 | coupure suivante : recomposition — hors de la "
                      "composition conservée ; non admis, coupure suivante |",
                      manifests[0])

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

    # -- #4329 : verrou de composition par nom de paquet -------------------
    def test_verrou_de_composition_par_nom(self) -> None:
        name = "h-verrou"
        self.assertIsNone(paquet_auto.composition_holder(self.repo, name))
        lock = paquet_auto.PackageLock(self.repo, name)
        lock.acquire()
        try:
            self.assertEqual(
                paquet_auto.composition_holder(self.repo, name),
                (socket.gethostname(), os.getpid()))
            with self.assertRaises(paquet_auto.CompositionBusy):
                paquet_auto.PackageLock(self.repo, name).acquire()
            self.assertIsNone(paquet_auto.composition_holder(self.repo, "h-autre"))
        finally:
            lock.release()
        self.assertIsNone(paquet_auto.composition_holder(self.repo, name))

    def test_clear_ignore_son_propre_verrou(self) -> None:
        # La composition qui recompose tient elle-même le verrou : la reprise
        # doit continuer à fermer le worktree de sa tentative morte.
        branch = "integration/paquet-h-propre"
        kept = paquet_auto.make_worktree(self.repo, self.base, branch)
        self.addCleanup(shutil.rmtree, kept, True)
        lock = paquet_auto.PackageLock(self.repo, "h-propre")
        lock.acquire()
        try:
            paquet_auto.clear_local_branch(self.repo, branch,
                                           log=lambda _l: None)
        finally:
            lock.release()
        self.assertFalse(kept.exists())
        self.assertIsNone(paquet_auto.rev(self.repo, "refs/heads/" + branch))

    def test_recomposition_ne_touche_pas_un_worktree_vivant(self) -> None:
        # #4329 : le lancement manuel et le tic ne se partagent pas le même
        # nom de paquet ; le worktree du vivant reste, puis se nettoie à sa
        # mort (le noyau rend le flock).
        branch = "integration/paquet-h-verrou-vivant"
        kept = paquet_auto.make_worktree(self.repo, self.base, branch)
        self.addCleanup(shutil.rmtree, kept, True)
        marker = pathlib.Path(self.tmp.name) / "verrou-pris"
        code = (
            "import importlib.util, pathlib, sys, time\n"
            "spec = importlib.util.spec_from_file_location("
            "'paquet_auto_lock', sys.argv[1])\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name] = module\n"
            "spec.loader.exec_module(module)\n"
            "module.PackageLock(pathlib.Path(sys.argv[2]), sys.argv[3]).acquire()\n"
            "pathlib.Path(sys.argv[4]).write_text('locked', encoding='ascii')\n"
            "time.sleep(120)\n")
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(SCRIPT), str(self.repo),
             "h-verrou-vivant", str(marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def stop() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            for pipe in (child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()

        self.addCleanup(stop)
        deadline = time.time() + 20
        while (not marker.exists() and child.poll() is None
               and time.time() < deadline):
            time.sleep(0.05)
        if not marker.exists():
            child.kill()
            _out, err = child.communicate(timeout=10)
            self.fail("verrou enfant non pris : %s" % err)
        with self.assertRaises(paquet_auto.CompositionBusy) as caught:
            paquet_auto.clear_local_branch(self.repo, branch,
                                           log=lambda _l: None)
        message = str(caught.exception)
        self.assertIn("composition en cours", message)
        self.assertIn("%s:%d" % (socket.gethostname(), child.pid), message)
        self.assertTrue(kept.exists())
        child.kill()
        child.wait(timeout=10)
        paquet_auto.clear_local_branch(self.repo, branch, log=lambda _l: None)
        self.assertFalse(kept.exists())
        self.assertIsNone(paquet_auto.rev(self.repo, "refs/heads/" + branch))

    def test_purge_ne_touche_pas_une_composition_vivante(self) -> None:
        path = pathlib.Path(self.tmp.name) / "paquet-auto-vivant"
        branch = "integration/paquet-h-purge-vivante"
        self.git("worktree", "add", "-b", branch, str(path), self.base,
                 cwd=self.repo)
        self.addCleanup(lambda: self.git(
            "worktree", "remove", "--force", str(path), cwd=self.repo,
            check=False))
        with mock.patch.object(paquet_auto, "composition_holder",
                               return_value=("hote", 12345)):
            removed = paquet_auto.prune_stale_worktrees(
                self.repo, 1, now=time.time() + 3600)
        self.assertNotIn(str(path), removed)
        self.assertTrue(path.exists())

    def test_main_rend_le_code_de_composition_en_cours(self) -> None:
        with mock.patch.object(
                paquet_auto, "compose_packet",
                side_effect=paquet_auto.CompositionBusy("banc", "hote", 7)):
            code = paquet_auto.main(["--name", "banc", "--carrier", "1"])
        self.assertEqual(code, paquet_auto.IN_PROGRESS_RC)

    def test_compose_packet_refuse_une_composition_vivante(self) -> None:
        # Deux compositions du même nom : la seconde s'arrête au verrou, avant
        # de lire l'état ou de toucher au worktree de la première.
        name = "h-compose-vivante"
        args = argparse.Namespace(
            repo=str(self.repo), name=name, target="main", carrier=99,
            force=True, no_packet_record=True, local=True, keep_worktree=False,
            keep_failed_hours=24.0, tests_timeout=10, pilot="pilote")
        lock = paquet_auto.PackageLock(self.repo, name)
        lock.acquire()
        try:
            with self.assertRaises(paquet_auto.CompositionBusy) as caught:
                paquet_auto.compose_packet(args)
        finally:
            lock.release()
        self.assertIn("composition en cours", str(caught.exception))
        branch = paquet_auto.package_branch(name)
        self.assertIsNone(paquet_auto.rev(self.repo, "refs/heads/" + branch))
        self.assertEqual(paquet_auto._worktrees_on(self.repo, branch), [])

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

    def test_grouped_composition_admits_every_lot(self):
        source = "a" * 40
        composition = paquet_auto.Composition(
            [paquet_auto.Unit(1, source, "b" * 40)], [], "b" * 40)
        table = ("## Composition\n\n| ordre | lots | source | commit | note |\n"
                 "|---|---|---|---|---|\n"
                 "| 1 | #1, #2 | `%s` | `%s` | résolution |\n" % (source, "b" * 40))
        manifest = paquet_auto.build_manifest(
            name="grouped", target="main", base_sha=self.base, carrier=99,
            composition=composition, table=table, tests=[], freeze_seconds=0.0)
        from ameesh import packet_parallel
        self.assertEqual(packet_parallel.manifest_units(manifest), [
            {"work_item": 1, "source": source},
            {"work_item": 2, "source": source}])
        self.assertIn("1 unités, 2 lots", manifest)

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
        # #4581 : un lot prêt depuis la composition (jamais composé) est nommé
        # aux écartés ; le contrôle d'inclusion l'accepte comme expliqué.
        composition.discarded.append(paquet_auto.Discarded(
            4, paquet_auto.KEEP_READY_REASON, "hors composition conservée"))
        manifest = paquet_auto.build_manifest(
            name="inclusion", target="main", base_sha=self.base, carrier=99,
            composition=composition, table=table, tests=[], freeze_seconds=0.0)
        rel = "reviews/integration-paquet-inclusion.md"
        self.write(rel, manifest, cwd=worktree)
        head = self.commit("manifeste", cwd=worktree)
        ready = pathlib.Path(self.tmp.name) / "prets.txt"
        ready.write_text("#1\n#2\n#3\n#4\n", encoding="utf-8")
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
        # au manifeste, pas attendu ; #4 prêt depuis la composition non plus.
        parse = self._parse_manifest(worktree / rel)
        self.assertEqual(parse.admissions, [1, 3])
        self.assertIn(2, parse.ecartes)
        self.assertIn("conflit", parse.ecartes[2])
        self.assertIn(4, parse.ecartes)
        self.assertIn(paquet_auto.KEEP_READY_REASON, parse.ecartes[4])

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


class KeepCompositionOptionTest(unittest.TestCase):
    """#4581 : l'option de recomposition est transmise, et refuse le rejeu."""

    def test_main_transmet_keep_composition(self) -> None:
        parsed: list[str] = []
        original = paquet_auto.compose_packet
        paquet_auto.compose_packet = (
            lambda args: parsed.append(args.keep_composition) or 0)
        try:
            rc = paquet_auto.main(["--name", "h-x", "--carrier", "9",
                                   "--keep-composition", "a" * 40])
        finally:
            paquet_auto.compose_packet = original
        self.assertEqual(rc, 0)
        self.assertEqual(parsed, ["a" * 40])

    def test_rejeu_refuse_keep_composition(self) -> None:
        self.assertEqual(
            paquet_auto.main(["--name", "h-x", "--replay", "HEAD",
                              "--base", "HEAD", "--keep-composition", "a" * 40]),
            2)

    def test_publication_refuse_keep_composition(self) -> None:
        self.assertEqual(
            paquet_auto.main(["--name", "h-x", "--carrier", "9",
                              "--publish", "refs/ameesh/paquet-auto/teste/h-x",
                              "--publish-sha", "a" * 40,
                              "--keep-composition", "b" * 40]), 2)


if __name__ == "__main__":
    unittest.main()
