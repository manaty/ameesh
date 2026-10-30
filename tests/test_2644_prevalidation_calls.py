# SPDX-License-Identifier: AGPL-3.0-only
"""#2644 — les trois accroches automatiques de la prévalidation #1770.

Les témoins couvrent le calcul du plan dans l'arbre du commit candidat (vrai
Git, script factice), les paramètres compilés du canon, le repli d'hôte sur
une indisponibilité de transport (jamais après un rouge), le rapport persisté,
la porte du paquet (rouge = refus nommé avec les tests), le gel non bloquant
(journal, jamais d'exception) et la composition du train.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from ameesh import canon
from ameesh import mechanics
from ameesh import plan_git
from ameesh import prevalidation as pv
from ameesh import prevalidation_calls as calls

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def git(root, *args) -> str:
    return subprocess.run(["git", "-C", root, *args], check=True, env=ENV,
                          capture_output=True, text=True).stdout.strip()


def report(*, ok=True, causes=(), failed=(), missing=(), modules=("test_a",),
           detail=(), host="vm1", outcome=None):
    return {"schema": pv.REPORT_SCHEMA, "lot": 2644, "sha": "a" * 40,
            "base": "b" * 40, "card": "c" * 40, "modules": list(modules),
            "planned": list(modules), "missing": list(missing),
            "parts": ["psycopg:1"], "planned_parts": ["psycopg:1"], "tests": 3,
            "host": host,
            "outcome": outcome or ("green" if ok else "run-red"),
            "exit": 0, "seconds": 12.5, "causes": list(causes), "ok": ok,
            "detail": list(detail), "failed": list(failed)}


def params(*, hosts=("vm1", "vm2")):
    return mechanics.Prevalidation(classes=("P0", "P1"), contracts=(), hosts=hosts,
                                   timeout_seconds=60,
                                   parts_script="scripts/test-parts.py")


class Repo:
    """Un dépôt jetable avec un `scripts/test-plan.py` factice."""

    def __init__(self, script=None):
        self.root = tempfile.mkdtemp(prefix="ameesh-2644-")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root],
                       check=True, env=ENV)
        self.write("README", "base\n")
        self.base = self.commit("base")
        self.write("scripts/test-plan.py", script if script is not None else PLAN_SCRIPT)
        self.write("scripts/test-parts.py", "# lanceur factice (#2794)\n")
        self.write("data.txt", "head\n")
        self.head = self.commit("head")

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, message):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


PLAN_SCRIPT = """#!/usr/bin/env python3
import argparse, json, subprocess
p = argparse.ArgumentParser()
p.add_argument('command'); p.add_argument('--out'); p.add_argument('--event')
p.add_argument('--base')
p.add_argument('--mode'); p.add_argument('--map', default='')
p.add_argument('--ref', default='')
a = p.parse_args()
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
plan = {'schema': 'ameesh-test-plan/1', 'head': head, 'map': {'used': True},
        'base_arg': a.base,
        'selection': {'mode': 'impacted', 'selected': 1}, 'sure': True,
        'phase2_safe': True, 'classes': {}, 'contract': [], 'parts': {}}
open(a.out, 'w', encoding='utf-8').write(json.dumps(plan))
"""


FULL_PLAN_SCRIPT = """#!/usr/bin/env python3
import argparse, json, subprocess
p = argparse.ArgumentParser()
p.add_argument('command'); p.add_argument('--out'); p.add_argument('--event')
p.add_argument('--base')
p.add_argument('--mode'); p.add_argument('--map', default='')
p.add_argument('--ref', default='')
a = p.parse_args()
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
plan = {'schema': 'ameesh-test-plan/1', 'head': head,
        'map': {'commit': a.base, 'used': True, 'age_h': 1.0},
        'selection': {'mode': 'full', 'why': 'test full suite'}, 'sure': False,
        'phase2_safe': False, 'phase2_why': 'test',
        'unsure': ['selection unsure (test)'], 'classes': {}, 'contract': [],
        'parts': {}}
open(a.out, 'w', encoding='utf-8').write(json.dumps(plan))
"""


class ParametersTest(unittest.TestCase):
    def test_explicit_canon_commit(self):
        block = params(hosts=("vm1",))
        with mock.patch.object(pv, "load_parameters", return_value=(None, block)):
            got = calls.parameters(None, "ameesh", canon_repo="/canon", canon_commit="d" * 40)
        self.assertEqual((got.repo, got.commit, got.project), ("/canon", "d" * 40, "ameesh"))
        self.assertEqual(got.hosts, ("vm1",))

    def test_no_canon_configured(self):
        with self.assertRaises(pv.PrevalidationError) as caught:
            calls.parameters(None, "ameesh")
        self.assertEqual(caught.exception.cause, "block-missing")

    def test_canon_without_block_is_named(self):
        cfg = SimpleNamespace(canon="/canon")
        source = SimpleNamespace(mode="git", repo="/canon", commit="e" * 40, prefix="")
        canon = SimpleNamespace(root="/canon", sources=(source,))
        with mock.patch("ameesh.canon.load_configured", return_value=[canon]), \
                mock.patch.object(pv, "load_parameters",
                                  side_effect=pv.PrevalidationError("block-missing", "no block")):
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.parameters(cfg, "ameesh")
        self.assertEqual(caught.exception.cause, "block-missing")
        self.assertIn("no block", caught.exception.why)

    def test_first_canon_with_a_block_wins(self):
        cfg = SimpleNamespace(canon="/canon")
        first = SimpleNamespace(mode="git", repo="/canon1", commit="1" * 40, prefix="")
        second = SimpleNamespace(mode="git", repo="/canon2", commit="2" * 40, prefix="")
        canons = [SimpleNamespace(root="/canon1", sources=(first,)),
                  SimpleNamespace(root="/canon2", sources=(second,))]
        block = params(hosts=("vm1",))

        def load(repo, commit, project, *, deadline=None):
            if repo == "/canon1":
                raise pv.PrevalidationError("block-missing", "absent")
            return None, block

        with mock.patch("ameesh.canon.load_configured", return_value=canons), \
                mock.patch.object(pv, "load_parameters", side_effect=load):
            got = calls.parameters(cfg, "ameesh")
        self.assertEqual(got.repo, "/canon2")
        self.assertEqual(got.commit, "2" * 40)

    def test_the_deadline_reaches_the_canon_reader(self):
        block = params(hosts=("vm1",))
        deadline = calls.Deadline(5.0)
        seen = {}

        def load(repo, commit, project, *, deadline=None):
            seen["deadline"] = deadline
            seen["source"] = (repo, commit, project)
            return None, block

        with mock.patch.object(pv, "load_parameters", side_effect=load):
            got = calls.parameters(None, "ameesh", canon_repo="/canon",
                                   canon_commit="d" * 40, deadline=deadline)
        self.assertIs(seen["deadline"], deadline)
        self.assertEqual(seen["source"], ("/canon", "d" * 40, "ameesh"))
        self.assertEqual(got.hosts, ("vm1",))

    def test_an_expired_budget_is_run_incomplete_not_block_missing(self):
        # #2665/B1 cx2 : l'échéance épuisée ne se lit pas « canon sans bloc ».
        cfg = SimpleNamespace(canon="/canon")
        source = SimpleNamespace(mode="git", repo="/canon", commit="e" * 40, prefix="")
        configured = SimpleNamespace(root="/canon", sources=(source,))
        deadline = calls.Deadline(0.0)
        with mock.patch("ameesh.canon.load_configured", return_value=[configured]), \
                mock.patch.object(pv, "load_parameters") as load:
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.parameters(cfg, "ameesh", deadline=deadline)
        self.assertEqual(caught.exception.cause, "run-incomplete")
        load.assert_not_called()

    def test_the_configured_discovery_is_bounded_by_the_deadline(self):
        # #2665/B1 cx1+cx2 : load_configured→from_config→load→git_toplevel
        # gardait 60 s ; le faux git dormant 0,15 s doit être coupé à 0,03 s.
        root = tempfile.mkdtemp(prefix="ameesh-2665-canon-")
        self.addCleanup(shutil.rmtree, root, True)
        cfg = SimpleNamespace(canon=root, canon_ref="", canon_untrusted=False)
        now = [100.0]
        deadline = calls.Deadline(0.03, clock=lambda: now[0])

        def slow(cmd, **kwargs):
            now[0] += 0.15
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        with mock.patch.object(canon.subprocess, "run", side_effect=slow):
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.parameters(cfg, "ameesh", deadline=deadline)
        self.assertEqual(caught.exception.cause, "run-incomplete")
        self.assertIn("découverte des canons", caught.exception.why)

    def test_an_expired_discovery_launches_no_git(self):
        root = tempfile.mkdtemp(prefix="ameesh-2665-canon-")
        self.addCleanup(shutil.rmtree, root, True)
        cfg = SimpleNamespace(canon=root, canon_ref="", canon_untrusted=False)
        with mock.patch.object(canon.subprocess, "run") as run:
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.parameters(cfg, "ameesh", deadline=calls.Deadline(0.0))
        self.assertEqual(caught.exception.cause, "run-incomplete")
        run.assert_not_called()

    def test_an_expired_canon_read_is_run_incomplete(self):
        now = [100.0]
        deadline = calls.Deadline(5.0, clock=lambda: now[0])

        def slow_read(*args, **kwargs):
            now[0] += 10.0
            raise mechanics.MechanicsError("git ls-tree: timed out")

        with mock.patch.object(mechanics, "load", side_effect=slow_read):
            with self.assertRaises(pv.PrevalidationError) as caught:
                pv.load_parameters("/repo", "a" * 40, "ameesh", deadline=deadline)
        self.assertEqual(caught.exception.cause, "run-incomplete")

    def test_a_canon_read_error_with_budget_left_is_not_masked(self):
        deadline = calls.Deadline(60.0)
        with mock.patch.object(mechanics, "load",
                               side_effect=mechanics.MechanicsError("bad canon")):
            with self.assertRaises(mechanics.MechanicsError):
                pv.load_parameters("/repo", "a" * 40, "ameesh", deadline=deadline)


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def test_explicit_tree_carrying_the_head(self):
        self.assertEqual(calls.repository(None, None, {}, self.repo.head,
                                          explicit=self.repo.root), self.repo.root)

    def test_unknown_head_is_not_guessed(self):
        self.assertEqual(calls.repository(None, None, {}, "f" * 40,
                                          explicit=self.repo.root), "")

    def test_registry_worktree(self):
        row = {"cwd": self.repo.root}
        with mock.patch("ameesh.registry.get", return_value=row), \
                mock.patch("os.getcwd", return_value="/"):
            self.assertEqual(calls.repository(None, object(), {"assignee": "dev"},
                                              self.repo.head), self.repo.root)


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def test_plan_is_built_in_the_candidate_tree(self):
        prepared = calls.prepare(self.repo.root, self.repo.head, self.repo.base)
        self.addCleanup(prepared.close)
        self.assertEqual(prepared.plan["head"], self.repo.head)
        self.assertTrue(os.path.isfile(prepared.path))
        self.assertTrue(prepared.path.startswith(prepared.tree + os.sep))
        self.assertNotEqual(prepared.tree, self.repo.root)

    def test_close_removes_the_worktree(self):
        prepared = calls.prepare(self.repo.root, self.repo.head, self.repo.base)
        tree = prepared.tree
        prepared.close()
        self.assertFalse(os.path.exists(tree))
        self.assertNotIn(tree, git(self.repo.root, "worktree", "list", "--porcelain"))

    def test_tracked_plan_name_is_not_replaced(self):
        self.repo.write(calls.PLAN_NAME, "already tracked\n")
        head = self.repo.commit("tracked plan name")
        prepared = calls.prepare(self.repo.root, head, self.repo.base)
        self.addCleanup(prepared.close)
        self.assertNotEqual(prepared.rel, calls.PLAN_NAME)
        self.assertTrue(prepared.rel.startswith("ameesh-prevalidation-plan-"))

    def test_unknown_base_is_refused(self):
        with self.assertRaises(pv.PrevalidationError) as caught:
            calls.prepare(self.repo.root, self.repo.head, "f" * 40)
        self.assertEqual(caught.exception.cause, "selection-unsafe")

    def test_plan_base_is_the_merge_base_not_the_target_tip(self):
        # #3800 : comparer le tip de la cible à la tête fait passer les
        # changements de la cible pour ceux du lot ; `--base` doit être le
        # merge-base (ici le point de branche).
        self.repo.write("target.txt", "t\n")
        target = self.repo.commit("target")
        git(self.repo.root, "checkout", "-q", "-b", "lot", self.repo.head)
        self.repo.write("lot.txt", "l\n")
        branch = self.repo.commit("lot")
        prepared = calls.prepare(self.repo.root, branch, target)
        self.addCleanup(prepared.close)
        self.assertEqual(prepared.plan["base_arg"], self.repo.head)
        self.assertNotEqual(prepared.plan["base_arg"], target)

    def test_failing_script_is_named_and_cleaned(self):
        self.repo.write("scripts/test-plan.py", "import sys\nsys.exit(3)\n")
        head = self.repo.commit("red script")
        with self.assertRaises(pv.PrevalidationError) as caught:
            calls.prepare(self.repo.root, head, self.repo.base)
        self.assertEqual(caught.exception.cause, "selection-unsafe")
        self.assertIn("refusé le plan", caught.exception.why)

    def test_both_commit_probes_read_the_shared_deadline(self):
        # #2665/B1 cx2 : les sondes head/base repartaient de GIT_TIMEOUT (60 s).
        # #3877 : le plan (vrai sous-processus) a le reste de l'échéance ;
        # 0,5 s ne suffisait pas sur un runner chargé. Le reste (20 s) reste
        # sous GIT_TIMEOUT : la sonde lit toujours l'échéance partagée.
        now = [100.0]
        deadline = calls.Deadline(40.0, clock=lambda: now[0])
        seen = []

        def probe(repo, rev, *, timeout=None):
            seen.append((rev, timeout))
            now[0] += 10.0
            return self.repo.head if len(seen) == 1 else self.repo.base

        with mock.patch.object(plan_git, "_commit", side_effect=probe):
            prepared = calls.prepare(self.repo.root, self.repo.head, self.repo.base,
                                     deadline=deadline)
        self.addCleanup(prepared.close)
        self.assertEqual(seen[0], (self.repo.head, 40.0))
        self.assertAlmostEqual(seen[1][1], 30.0)

    def test_an_overrunning_commit_probe_is_run_incomplete(self):
        # La preuve de cx2 : une sonde lente ne doit pas rendre selection-unsafe.
        now = [100.0]
        deadline = calls.Deadline(0.03, clock=lambda: now[0])
        seen = {}

        def probe(repo, rev, *, timeout=None):
            seen["timeout"] = timeout
            now[0] += timeout
            raise canon.GitError("git rev-parse: timed out")

        with mock.patch.object(plan_git, "_commit", side_effect=probe):
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.prepare(self.repo.root, self.repo.head, self.repo.base,
                              deadline=deadline)
        self.assertEqual(caught.exception.cause, "run-incomplete")
        self.assertAlmostEqual(seen["timeout"], 0.03)


class ContractFallbackTest(unittest.TestCase):
    """#3800 : sur sélection incertaine, les contrats tournent quand même."""

    def setUp(self):
        self.repo = Repo(script=FULL_PLAN_SCRIPT)
        self.addCleanup(self.repo.cleanup)
        self.repo.write("tests/parts/toujours.txt",
                        "test_cluster  # rôles du cluster\n"
                        "test_migrations  # migrations\n")
        self.repo.write("tests/test_cluster.py", "x = 1\n")
        self.repo.write("tests/test_l107_executor_contract.py", "x = 1\n")
        self.repo.write("tests/test_l161f_plan.py", "x = 1\n")
        self.head = self.repo.commit("contracts")

    def block(self):
        return replace(params(hosts=("vm1",)),
                       contracts=("tests/parts/toujours.txt",))

    def parameters(self):
        return calls.Parameters(params=self.block(), repo="/canon",
                                commit="c" * 40, project="ameesh")

    def test_uncertain_selection_produces_a_contract_plan(self):
        prepared = calls.prepare(self.repo.root, self.head, self.repo.base)
        self.addCleanup(prepared.close)
        fallback = calls.contract_fallback(prepared, self.parameters(),
                                           head=self.head, base=self.repo.base)
        self.assertIsNotNone(fallback)
        plan = prepared.plan
        self.assertEqual(plan["selection"]["mode"], "contract")
        self.assertEqual(plan["fallback"]["cause"], "selection-full")
        modules = plan["parts"]["psycopg"][0]["modules"]
        self.assertIn("test_cluster", modules)
        self.assertIn("test_migrations", modules)
        self.assertIn("test_l161f_plan", modules)
        self.assertEqual(plan["contract"], ["test_cluster", "test_migrations"])
        self.assertIn(plan["classes"]["test_l161f_plan"][0], ("P0", "P1"))
        # la preuve se relit comme une sélection de contrat, pas comme un plan
        # impacté ordinaire
        chosen = pv.selection(plan, head=self.head, base=self.repo.base)
        self.assertTrue(chosen.ok, chosen.why)
        self.assertEqual(chosen.fallback["cause"], "selection-full")

    def test_contract_read_is_bounded_by_the_deadline(self):
        # #3800/N cx2 B1 : sous le bail de la cible (gate_train), la lecture
        # des contrats doit recevoir un timeout borné, pas `subprocess.run`
        # sans borne.
        prepared = calls.prepare(self.repo.root, self.head, self.repo.base)
        self.addCleanup(prepared.close)
        deadline = calls.Deadline(5.0)
        seen = []

        def suspended(argv, **kw):
            seen.append(kw.get("timeout"))
            raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))

        with mock.patch.object(calls.subprocess, "run", side_effect=suspended):
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.contract_fallback(prepared, self.parameters(),
                                        head=self.head, base=self.repo.base,
                                        deadline=deadline)
        self.assertEqual(caught.exception.cause, "run-incomplete")
        self.assertTrue(seen and seen[0] is not None)
        self.assertGreater(seen[0], 0.0)
        self.assertLessEqual(seen[0], 5.0)

    def test_expired_budget_before_contract_read_is_run_incomplete(self):
        prepared = calls.prepare(self.repo.root, self.head, self.repo.base)
        self.addCleanup(prepared.close)
        with mock.patch.object(calls.subprocess, "run") as run:
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.contract_fallback(prepared, self.parameters(),
                                        head=self.head, base=self.repo.base,
                                        deadline=calls.Deadline(0.0))
        self.assertEqual(caught.exception.cause, "run-incomplete")
        run.assert_not_called()

    def test_run_plays_the_fallback_and_reports_it(self):
        seen = {}

        def validate(**kw):
            with open(kw["plan_path"], "r", encoding="utf-8") as fh:
                seen["plan"] = json.load(fh)
            return report()

        with mock.patch.object(calls, "parameters", return_value=self.parameters()), \
                mock.patch.object(calls, "map_for", return_value=("", "")):
            got = calls.run(None, None, 3800, repo=self.repo.root, head=self.head,
                            base=self.repo.base, project="ameesh", validate=validate)
        self.assertTrue(got["ok"])
        self.assertEqual(seen["plan"]["selection"]["mode"], "contract")
        self.assertEqual(seen["plan"]["fallback"]["cause"], "selection-full")
        self.assertEqual(got["fallback"]["cause"], "selection-full")
        self.assertEqual(got["selection"], "contract")

    def test_train_plays_the_fallback_before_publication(self):
        seen = {}

        def hosts(prepared, params_, **kw):
            with open(prepared.path, "r", encoding="utf-8") as fh:
                seen["plan"] = json.load(fh)
            seen["profile"] = kw.get("profile")
            return report()

        with mock.patch.object(calls, "parameters", return_value=self.parameters()), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "_run_hosts", side_effect=hosts):
            got = calls.gate_train(None, repo=self.repo.root, head=self.head,
                                   base=self.repo.base)
        self.assertEqual(got["state"], "green")
        self.assertEqual(seen["plan"]["selection"]["mode"], "contract")
        # #4008 : le train aussi joue le profil du projet, pas `default`.
        self.assertEqual(seen["profile"], "ameesh")
        self.assertEqual(got["report"]["fallback"]["cause"], "selection-full")

    def test_packet_gate_uses_the_fallback(self):
        # #3800 point (3) : le porteur `integration/**` suit la même règle que
        # le gel d'un lot.
        seen = {}

        def hosts(prepared, params_, **kw):
            seen["profile"] = kw.get("profile")
            return report()

        with mock.patch.object(calls, "parameters", return_value=self.parameters()), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "_run_hosts", side_effect=hosts):
            got = calls.gate_packet(
                None, None, {"id": 3800, "app": "ameesh"},
                {"sha": self.head, "base": self.repo.base,
                 "branch": "integration/paquet-3800"},
                repo=self.repo.root)
        self.assertEqual(got["state"], "green")
        # #4008 : la porte du paquet joue le profil du projet, pas `default`.
        self.assertEqual(seen["profile"], "ameesh")
        self.assertEqual(got["report"]["fallback"]["cause"], "selection-full")
        self.assertEqual(got["report"]["selection"], "contract")

    def test_fallback_adds_transverse_contracts_and_changed_tests(self):
        # #3954 : un lot qui change l'interface de stockage rend le contrat de
        # l'exécuteur (#3885, tête 68c30d91) et son test changé (P0, #3916,
        # tête 7ae1e095) reste joué par le repli « contrats ».
        script = FULL_PLAN_SCRIPT.replace(
            "'classes': {}",
            "'classes': {'test_3845_paquet_horaire': ['P0', 'test changé'],"
            " 'test_a': ['P3', '']}")
        self.repo.write("scripts/test-plan.py", script)
        self.repo.write("src/ameesh/storage/interface.py",
                        "class Storage:\n    def ops(self):\n        return 1\n")
        self.repo.write("tests/test_3845_paquet_horaire.py", "x = 1\n")
        self.head = self.repo.commit("transverse")
        prepared = calls.prepare(self.repo.root, self.head, self.repo.base)
        self.addCleanup(prepared.close)
        fallback = calls.contract_fallback(prepared, self.parameters(),
                                           head=self.head, base=self.repo.base)
        self.assertIsNotNone(fallback)
        plan = prepared.plan
        self.assertEqual(plan["selection"]["mode"], "contract")
        self.assertIn("contrats et tests du lot", plan["selection"]["why"])
        # #4574 : le décor écrit tests/parts/toujours.txt après la base, d'où
        # aussi le contrat de la liste figée (test_l161f_plan).
        self.assertEqual(fallback["transverse"],
                         ["test_l107_executor_contract", "test_l161f_plan"])
        self.assertEqual(fallback["changed_tests"], ["test_3845_paquet_horaire"])
        self.assertFalse(fallback["transverse_all"])
        self.assertEqual(fallback["diff"], "read")
        modules = plan["parts"]["psycopg"][0]["modules"]
        self.assertIn("test_l107_executor_contract", modules)
        self.assertIn("test_3845_paquet_horaire", modules)
        self.assertIn("test_cluster", modules)
        # la preuve se relit comme un repli déclaré, pas comme un vert de
        # sélection : les modules supplémentaires sont dans les classes du plan.
        chosen = pv.selection(plan, head=self.head, base=self.repo.base)
        self.assertTrue(chosen.ok, chosen.why)
        self.assertEqual(chosen.fallback["transverse"],
                         ["test_l107_executor_contract", "test_l161f_plan"])

    def test_unreadable_diff_plays_all_transverse_contracts(self):
        # #3954/B de cx3 : un diff illisible ne conclut pas « aucun contrat
        # transverse dû » ; le repli joue alors tous les contrats déclarés.
        prepared = calls.prepare(self.repo.root, self.head, self.repo.base)
        self.addCleanup(prepared.close)
        with mock.patch.object(calls, "_changed_paths", return_value=None):
            fallback = calls.contract_fallback(prepared, self.parameters(),
                                               head=self.head, base=self.repo.base)
        self.assertIsNotNone(fallback)
        self.assertTrue(fallback["transverse_all"])
        self.assertEqual(fallback["diff"], "unreadable")
        self.assertEqual(fallback["transverse"],
                         sorted(calls.transverse_contracts.CONTRACTS))
        modules = prepared.plan["parts"]["psycopg"][0]["modules"]
        self.assertIn("test_l107_executor_contract", modules)
        self.assertIn("test_cluster", modules)

    def test_missing_declared_module_refuses_the_fallback(self):
        # #3954/B de cx3 : une déclaration qui nomme un module disparu ne se
        # suppose pas ; le repli est refusé (le refus d'origine reste).
        prepared = calls.prepare(self.repo.root, self.head, self.repo.base)
        self.addCleanup(prepared.close)
        with mock.patch.dict(calls.transverse_contracts.CONTRACTS,
                             {"test_absent_3954": ("src/ameesh/x.py",)}):
            fallback = calls.contract_fallback(prepared, self.parameters(),
                                               head=self.head, base=self.repo.base)
        self.assertIsNone(fallback)


class CommitProbeTest(unittest.TestCase):
    def test_the_probe_keeps_no_lazy_fetch_and_the_bound(self):
        # #1749 ne doit pas se perdre en bornant la sonde (#2665).
        seen = {}

        def fake_git(repo, *args, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(returncode=0, stdout=b"a" * 40 + b"\n")

        with mock.patch.object(plan_git, "_git", side_effect=fake_git):
            got = plan_git._commit("/repo", "abc", timeout=1.25)
        self.assertEqual(got, "a" * 40)
        self.assertEqual(seen.get("timeout"), 1.25)
        self.assertIs(seen.get("env_extra"), canon.NO_LAZY_FETCH)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def fake_prepared(self):
        tree = tempfile.mkdtemp(prefix="ameesh-2644-tree-")
        self.addCleanup(shutil.rmtree, tree, True)
        return calls.Prepared(repo=self.repo.root, root=tree, tree=tree,
                              path=os.path.join(tree, "plan.json"),
                              rel="plan.json", plan={})

    def call(self, validate, *, hosts=("vm1", "vm2"), database=None,
             profile=""):
        prepared = self.fake_prepared()
        block = params(hosts=hosts)
        with mock.patch.object(calls, "prepare", return_value=prepared), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "parameters",
                                  return_value=calls.Parameters(
                                      params=block, repo="/canon",
                                      commit="c" * 40, project="ameesh")):
            return calls.run(None, database, 2644, repo=self.repo.root,
                             head=self.repo.head, base=self.repo.base,
                             project="ameesh", profile=profile, validate=validate)

    def test_run_uses_the_project_profile_by_default(self):
        # #4008 : sans profil nommé, l'automatisme jouait `default` (sans
        # base) et les contrats tombaient en setUpClass. Le profil du projet
        # (`ameesh`) porte le venv et la base jetable.
        seen = {}

        def validate(**kw):
            seen["profile"] = kw.get("profile")
            return report()

        got = self.call(validate)
        self.assertTrue(got["ok"])
        self.assertEqual(seen["profile"], "ameesh")

    def test_an_explicit_profile_wins(self):
        seen = {}

        def validate(**kw):
            seen["profile"] = kw.get("profile")
            return report()

        got = self.call(validate, profile="postgres")
        self.assertTrue(got["ok"])
        self.assertEqual(seen["profile"], "postgres")

    def test_transport_unavailability_tries_the_next_host(self):
        seen = []

        def validate(**kw):
            seen.append(kw["host"])
            if kw["host"] == "vm1":
                raise pv.PrevalidationError("no-vm", "vm absente")
            return report()

        got = self.call(validate)
        self.assertTrue(got["ok"])
        self.assertEqual(seen, ["vm1", "vm2"])

    def test_a_red_run_is_not_retried_on_another_host(self):
        seen = []

        def validate(**kw):
            seen.append(kw["host"])
            return report(ok=False, causes=("run-red",), failed=("tests.test_x",))

        got = self.call(validate)
        self.assertFalse(got["ok"])
        self.assertEqual(seen, ["vm1"])

    def test_report_is_persisted_after_the_run(self):
        fake_db = object()
        with mock.patch.object(pv, "record") as record:
            got = self.call(lambda **kw: report(), database=fake_db)
        record.assert_called_once_with(fake_db, got)

    def test_a_transport_report_is_not_persisted(self):
        # Un no-vm n'est pas un run : la table des rapports ne le reçoit pas.
        with mock.patch.object(pv, "record") as record:
            got = self.call(lambda **kw: report(ok=False, causes=("no-vm",),
                                                outcome="no-vm"))
        self.assertEqual(got["outcome"], "no-vm")
        record.assert_not_called()

    def test_persistence_failure_does_not_turn_a_green_run_into_a_refusal(self):
        fake_db = object()
        warnings = []
        with mock.patch.object(pv, "record", side_effect=RuntimeError("0450 absente")):
            got = self.call(lambda **kw: report(), database=fake_db)
        # `run` ne lève pas : le run réel reste la preuve.
        self.assertTrue(got["ok"])


class ToolingTest(unittest.TestCase):
    """#2794 : un dépôt sans le plan ni le lanceur d'ameesh (nexlink) est
    refusé nommément avant tout arbre, carte ou hôte."""

    def setUp(self):
        self.repo = Repo()
        self.addCleanup(self.repo.cleanup)

    def parameters(self):
        return calls.Parameters(params=params(), repo="/canon", commit="c" * 40,
                                project="nexlink")

    def test_an_ameesh_tree_is_tooled(self):
        calls.tooling(self.repo.root, self.repo.head, self.parameters())

    def test_a_tree_without_the_plan_script_is_tooling_missing(self):
        git(self.repo.root, "rm", "-q", "scripts/test-plan.py", "scripts/test-parts.py")
        head = self.repo.commit("nexlink-like")
        with self.assertRaises(pv.PrevalidationError) as caught:
            calls.tooling(self.repo.root, head, self.parameters())
        self.assertEqual(caught.exception.cause, "tooling-missing")
        self.assertIn("tooling-missing", pv.CAUSES)
        self.assertIn("scripts/test-plan.py", caught.exception.why)
        self.assertIn("scripts/test-parts.py", caught.exception.why)
        self.assertIn("nexlink", caught.exception.why)

    def test_the_declared_launcher_is_the_one_checked(self):
        block = replace(params(), parts_script="tools/parts.py")
        with self.assertRaises(pv.PrevalidationError) as caught:
            calls.tooling(self.repo.root, self.repo.head, calls.Parameters(
                params=block, repo="/canon", commit="c" * 40, project="x"))
        self.assertIn("tools/parts.py", caught.exception.why)
        self.assertNotIn("test-plan.py", caught.exception.why)

    def test_an_unknown_head_is_left_to_prepare(self):
        calls.tooling(self.repo.root, "f" * 40, self.parameters())
        calls.tooling("/nonexistent-2794", "f" * 40, self.parameters())

    def test_run_refuses_before_any_map_or_tree(self):
        git(self.repo.root, "rm", "-q", "scripts/test-plan.py")
        head = self.repo.commit("sans plan")
        with mock.patch.object(calls, "parameters", return_value=self.parameters()), \
                mock.patch.object(calls, "map_for") as map_for, \
                mock.patch.object(calls, "prepare") as prepare:
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.run(None, None, 2794, repo=self.repo.root, head=head,
                          base=self.repo.base, project="nexlink")
        self.assertEqual(caught.exception.cause, "tooling-missing")
        map_for.assert_not_called()
        prepare.assert_not_called()

    def test_the_train_says_tooling_missing_unavailable(self):
        git(self.repo.root, "rm", "-q", "scripts/test-plan.py")
        head = self.repo.commit("sans plan")
        with mock.patch.object(calls, "parameters", return_value=self.parameters()), \
                mock.patch.object(calls, "map_for") as map_for:
            got = calls.gate_train(None, repo=self.repo.root, head=head,
                                   base=self.repo.base, project="nexlink")
        self.assertEqual(got["state"], "unavailable")
        self.assertEqual(got["cause"], "tooling-missing")
        map_for.assert_not_called()


class FreezeTest(unittest.TestCase):
    def setUp(self):
        self.item = {"id": 2644, "app": "ameesh", "assignee": "dev",
                     "branch": "lot/2644-x", "branch_target": "main"}
        self.notes = []
        self.patches = [
            mock.patch("ameesh.work.get", return_value=self.item),
            mock.patch("ameesh.work.note",
                       side_effect=lambda db, lot, text, **kw: self.notes.append(text)),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def freeze(self, *, returns=None, raises=None, sha="a" * 40):
        target = (mock.patch.object(calls, "run", return_value=returns)
                  if raises is None else
                  mock.patch.object(calls, "run", side_effect=raises))
        with mock.patch.object(calls, "repository", return_value="/repo"), \
                mock.patch.object(calls, "target_base", return_value="b" * 40), \
                target:
            return calls.after_freeze(None, object(), 2644, sha)

    def test_green_result_is_attached_to_the_freeze(self):
        outcome = self.freeze(returns=report())
        self.assertEqual(outcome["state"], "green")
        self.assertTrue(any(n.startswith("prevalidation green") for n in self.notes))

    def test_a_contract_fallback_is_named_in_the_journal(self):
        # #3800 : le gel dit que la sélection était incertaine et que seuls
        # les contrats ont tourné.
        outcome = self.freeze(returns=report(
            detail=("sélection incertaine (selection-full) : contrats seuls "
                    "(12 module(s))",)))
        self.assertEqual(outcome["state"], "green")
        self.assertTrue(any("contrats seuls" in n for n in self.notes))

    def test_red_result_never_blocks_the_freeze(self):
        outcome = self.freeze(returns=report(ok=False, causes=("run-red",),
                                             failed=("tests.test_x",)))
        self.assertEqual(outcome["state"], "red")
        self.assertTrue(any("run-red" in n and "tests.test_x" in n for n in self.notes))

    def test_unavailable_prevalidation_is_journaled(self):
        outcome = self.freeze(raises=pv.PrevalidationError("no-vm", "vm absente"))
        self.assertEqual(outcome["state"], "unavailable")
        self.assertTrue(any("unavailable" in n and "no-vm" in n for n in self.notes))

    def test_a_no_vm_report_is_unavailable_not_red(self):
        # Le run d'offload peut rendre no-vm sans lever : ce n'est pas un rouge.
        outcome = self.freeze(returns=report(ok=False, causes=("no-vm",),
                                             outcome="no-vm"))
        self.assertEqual(outcome["state"], "unavailable")
        self.assertTrue(any("prevalidation unavailable" in n for n in self.notes))
        self.assertFalse(any(n.startswith("prevalidation red") for n in self.notes))

    def test_a_freeze_without_sha_is_journaled(self):
        outcome = calls.after_freeze(None, object(), 2644, "")
        self.assertEqual(outcome["state"], "unavailable")
        self.assertTrue(any("gel sans --sha" in n for n in self.notes))

    def test_an_unexpected_database_error_does_not_escape(self):
        with mock.patch.object(calls, "repository", side_effect=RuntimeError("registre")):
            outcome = calls.after_freeze(None, object(), 2644, "a" * 40)
        self.assertEqual(outcome["state"], "unavailable")


class FreezeJournalTest(unittest.TestCase):
    """#4758 : une note d'indisponibilité par lot et par cause, pas par tête.

    ``once=True`` ne déduplique que le texte exact ; le SHA ou le chemin du
    dépôt change à chaque gel. La clé stable testée ici est le couple
    (lot, cause), lue dans le journal du lot comme le fait ``_journal``.
    """

    def setUp(self):
        self.item = {"id": 4758, "app": "nexlink", "assignee": "dev",
                     "branch": "lot/4758-x", "branch_target": "develop"}
        self.notes = []
        self.patches = [
            mock.patch("ameesh.work.get", return_value=self.item),
            mock.patch("ameesh.work.note",
                       side_effect=lambda db, lot, text, **kw: self.notes.append(text)),
            mock.patch("ameesh.work.events_noted", side_effect=self.events_noted),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def events_noted(self, db, lot, prefix, since_ts=0.0):
        return [{"note": text} for text in self.notes if text.startswith(prefix)]

    def freeze(self, *, returns=None, raises=None, sha="a" * 40):
        target = (mock.patch.object(calls, "run", return_value=returns)
                  if raises is None else
                  mock.patch.object(calls, "run", side_effect=raises))
        with mock.patch.object(calls, "repository", return_value="/repo"), \
                mock.patch.object(calls, "target_base", return_value="b" * 40), \
                target:
            return calls.after_freeze(None, object(), 4758, sha)

    def test_two_tooling_missing_freezes_at_two_heads_write_one_note(self):
        # Témoin du lot : deux gels nexlink (deux SHA) → une seule note.
        first = pv.PrevalidationError(
            "tooling-missing",
            "scripts/test-plan.py, scripts/test-parts.py absent de /repo au SHA "
            "aaaaaaaaaaaa : le projet nexlink n'outille pas la prévalidation")
        second = pv.PrevalidationError(
            "tooling-missing",
            "scripts/test-plan.py, scripts/test-parts.py absent de /repo au SHA "
            "bbbbbbbbbbbb : le projet nexlink n'outille pas la prévalidation")
        self.freeze(raises=first, sha="a" * 40)
        self.freeze(raises=second, sha="b" * 40)
        self.assertEqual(len(self.notes), 1, self.notes)
        self.assertIn("aaaaaaaaaaaa", self.notes[0])

    def test_a_new_cause_keeps_its_own_note(self):
        self.freeze(raises=pv.PrevalidationError("tooling-missing", "sans plan"))
        self.freeze(raises=pv.PrevalidationError("no-vm", "vm absente"), sha="b" * 40)
        self.assertEqual(len(self.notes), 2, self.notes)
        self.assertTrue(self.notes[0].startswith(
            "prevalidation unavailable: tooling-missing"))
        self.assertTrue(self.notes[1].startswith("prevalidation unavailable: no-vm"))

    def test_unavailable_reports_are_deduplicated_by_cause(self):
        self.freeze(returns=report(ok=False, causes=("no-vm",), outcome="no-vm"),
                    sha="a" * 40)
        self.freeze(returns=report(ok=False, causes=("no-vm",), outcome="no-vm"),
                    sha="b" * 40)
        self.assertEqual(len(self.notes), 1, self.notes)
        self.assertIn("tête aaaaaaaaaaaa", self.notes[0])
        self.freeze(returns=report(ok=False, causes=("run-incomplete",),
                                   outcome="run-incomplete"), sha="c" * 40)
        self.assertEqual(len(self.notes), 2, self.notes)

    def test_a_new_run_incomplete_list_at_a_later_head_is_visible(self):
        # #4769 : la cause reste dédupliquée, mais une liste de modules
        # différente écrit sa ligne à la tête qui la porte.
        self.freeze(returns=report(ok=False, causes=("run-incomplete",),
                                   outcome="run-incomplete",
                                   missing=("test_a", "test_b")), sha="a" * 40)
        self.assertEqual(len(self.notes), 1, self.notes)
        # Même liste dans un autre ordre, autre tête : rien de neuf.
        self.freeze(returns=report(ok=False, causes=("run-incomplete",),
                                   outcome="run-incomplete",
                                   missing=("test_b", "test_a")), sha="b" * 40)
        self.assertEqual(len(self.notes), 1, self.notes)
        self.freeze(returns=report(ok=False, causes=("run-incomplete",),
                                   outcome="run-incomplete",
                                   missing=("test_a", "test_c")), sha="c" * 40)
        self.assertEqual(len(self.notes), 2, self.notes)
        self.assertIn("tête cccccccccccc", self.notes[1])
        self.assertIn("test_c", self.notes[1])

    def test_a_run_incomplete_list_that_returns_is_visible_again(self):
        ab = report(ok=False, causes=("run-incomplete",),
                    outcome="run-incomplete", missing=("test_a", "test_b"))
        ac = report(ok=False, causes=("run-incomplete",),
                    outcome="run-incomplete", missing=("test_a", "test_c"))
        self.freeze(returns=ab, sha="a" * 40)
        self.freeze(returns=ac, sha="b" * 40)
        self.freeze(returns=ab, sha="d" * 40)
        self.assertEqual(len(self.notes), 3, self.notes)
        self.assertIn("tête dddddddddddd", self.notes[2])

    def test_run_incomplete_without_a_module_list_stays_deduplicated(self):
        self.freeze(returns=report(ok=False, causes=("run-incomplete",),
                                   outcome="run-incomplete"), sha="a" * 40)
        self.freeze(returns=report(ok=False, causes=("run-incomplete",),
                                   outcome="run-incomplete"), sha="b" * 40)
        self.assertEqual(len(self.notes), 1, self.notes)

    def test_green_and_red_notes_stay_one_per_head(self):
        self.freeze(returns=report(), sha="a" * 40)
        self.freeze(returns=report(), sha="b" * 40)
        self.freeze(returns=report(ok=False, causes=("run-red",),
                                   failed=("tests.test_x",)), sha="c" * 40)
        self.assertEqual(len(self.notes), 3, self.notes)

    def test_an_unreadable_journal_key_still_writes_the_note(self):
        with mock.patch("ameesh.work.events_noted",
                        side_effect=RuntimeError("journal illisible")):
            self.freeze(raises=pv.PrevalidationError("tooling-missing", "sans plan"))
        self.assertEqual(len(self.notes), 1, self.notes)


class GatePacketTest(unittest.TestCase):
    def packet(self, **run_kw):
        with mock.patch.object(calls, "repository", return_value="/repo"), \
                mock.patch.object(calls, "run", **run_kw):
            return calls.gate_packet(None, None, {"id": 2644, "app": "ameesh"},
                                     {"sha": "a" * 40, "base": "b" * 40,
                                      "branch": "integration/paquet"})

    def test_green_packet_passes(self):
        got = self.packet(return_value=report())
        self.assertEqual(got["state"], "green")
        self.assertTrue(got["report"]["ok"])

    def test_red_packet_names_the_failing_tests(self):
        got = self.packet(return_value=report(ok=False, causes=("run-red",),
                                              failed=("tests.test_x",)))
        self.assertEqual(got["state"], "red")
        self.assertIn("tests.test_x", got["why"])

    def test_unavailable_packet_does_not_block(self):
        # Décision pilote 19/10 : une indisponibilité ne bloque jamais.
        got = self.packet(side_effect=pv.PrevalidationError("no-vm", "vm absente"))
        self.assertEqual(got["state"], "unavailable")
        self.assertEqual(got["cause"], "no-vm")

    def test_incomplete_run_is_unavailable_not_red(self):
        got = self.packet(return_value=report(ok=False, causes=("run-incomplete",),
                                              missing=("test_b",),
                                              outcome="run-incomplete"))
        self.assertEqual(got["state"], "unavailable")
        self.assertIn("test_b", got["why"])

    def test_no_tree_carrying_the_head_is_unavailable(self):
        with mock.patch.object(calls, "repository", return_value=""):
            got = calls.gate_packet(None, None, {"id": 2644, "app": "ameesh"},
                                    {"sha": "a" * 40, "base": "b" * 40,
                                     "branch": "integration/paquet"})
        self.assertEqual(got["state"], "unavailable")
        self.assertEqual(got["cause"], "selection-unsafe")
        self.assertIn("aucun arbre", got["why"])


class GateTrainTest(unittest.TestCase):
    def test_red_composition_is_refused(self):
        prepared = SimpleNamespace(close=lambda *a, **kw: None)
        with mock.patch.object(calls, "parameters",
                               return_value=calls.Parameters(
                                   params=params(), repo="/canon",
                                   commit="c" * 40, project="ameesh")), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "prepare", return_value=prepared), \
                mock.patch.object(calls, "_run_hosts",
                                  return_value=report(ok=False, causes=("run-red",),
                                                      failed=("tests.test_x",))):
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40)
        self.assertEqual(caught.exception.cause, "run-red")
        self.assertIn("tests.test_x", caught.exception.why)

    def test_unavailable_composition_does_not_block(self):
        prepared = SimpleNamespace(close=lambda *a, **kw: None)
        with mock.patch.object(calls, "parameters",
                               return_value=calls.Parameters(
                                   params=params(), repo="/canon",
                                   commit="c" * 40, project="ameesh")), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "prepare", return_value=prepared), \
                mock.patch.object(calls, "_run_hosts",
                                  side_effect=pv.PrevalidationError("no-vm", "absente")):
            got = calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40)
        self.assertEqual(got["state"], "unavailable")
        self.assertEqual(got["cause"], "no-vm")

    def test_an_internal_error_is_unavailable_not_a_crash(self):
        # B2 dev1 : OSError/TimeoutExpired/MechanicsError ne doivent pas
        # sortir de gate_train vers l'Engine.
        with mock.patch.object(calls, "parameters",
                               side_effect=OSError("disque plein")):
            got = calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40)
        self.assertEqual(got["state"], "unavailable")
        self.assertEqual(got["cause"], "internal")
        self.assertIn("OSError", got["why"])

    def test_the_train_timeout_stays_below_the_lock_ttl(self):
        # N dev1 : le run de prévalidation ne doit pas dépasser le bail de la
        # cible (land.LOCK_TTL_S 1800 s) ; plan 300 + run 1200.
        prepared = SimpleNamespace(close=lambda *a, **kw: None)
        seen = {}
        block = replace(params(), timeout_seconds=3600)

        def run_hosts(prepared_, params_, **kw):
            seen["timeout"] = params_.params.timeout_seconds
            return report()

        with mock.patch.object(calls, "parameters",
                               return_value=calls.Parameters(
                                   params=block, repo="/canon",
                                   commit="c" * 40, project="ameesh")), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "prepare", return_value=prepared), \
                mock.patch.object(calls, "_run_hosts", side_effect=run_hosts):
            got = calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40)
        self.assertEqual(got["state"], "green")
        self.assertEqual(seen["timeout"], calls.TRAIN_RUN_TIMEOUT)
        self.assertLess(calls.TRAIN_PLAN_TIMEOUT + seen["timeout"], 1800)

    def test_a_no_vm_report_does_not_block_publication(self):
        prepared = SimpleNamespace(close=lambda *a, **kw: None)
        with mock.patch.object(calls, "parameters",
                               return_value=calls.Parameters(
                                   params=params(), repo="/canon",
                                   commit="c" * 40, project="ameesh")), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "prepare", return_value=prepared), \
                mock.patch.object(calls, "_run_hosts",
                                  return_value=report(ok=False, causes=("no-vm",),
                                                      outcome="no-vm")):
            got = calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40)
        self.assertEqual(got["state"], "unavailable")
        self.assertEqual(got["cause"], "no-vm")

    def test_one_deadline_covers_map_tree_run_and_cleanup(self):
        # #2665/N cx1 : carte, préparation, run et nettoyage lisent la même
        # échéance ; le plan et le run ne repartent pas chacun de leur plafond.
        now = [1000.0]
        budget = calls.train_deadline(clock=lambda: 0.0).at
        seen = {}
        prepared = SimpleNamespace(
            close=lambda **kw: seen.__setitem__("close", kw.get("timeout")))

        def map_for(*args, **kw):
            seen["map"] = kw.get("deadline")
            now[0] += 100.0
            return "", ""

        def prepare(*args, **kw):
            seen["prepare"] = kw.get("deadline")
            seen["plan_timeout"] = kw.get("timeout")
            return prepared

        def run_hosts(*args, **kw):
            seen["run"] = kw.get("deadline")
            seen["run_remaining"] = kw["deadline"].remaining()
            now[0] += seen["run_remaining"] - 50.0
            return report()

        with mock.patch.object(calls, "parameters",
                               return_value=calls.Parameters(
                                   params=params(), repo="/canon",
                                   commit="c" * 40, project="ameesh")), \
                mock.patch.object(calls, "map_for", side_effect=map_for), \
                mock.patch.object(calls, "prepare", side_effect=prepare), \
                mock.patch.object(calls, "_run_hosts", side_effect=run_hosts):
            got = calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40,
                                   clock=lambda: now[0])
        self.assertEqual(got["state"], "green")
        deadline = seen["run"]
        self.assertIs(seen["map"], deadline)
        self.assertIs(seen["prepare"], deadline)
        self.assertEqual(seen["plan_timeout"],
                         min(calls.TRAIN_PLAN_TIMEOUT, budget - 100.0))
        self.assertAlmostEqual(seen["run_remaining"], budget - 100.0, places=3)
        self.assertEqual(seen["close"], 50.0)

    def test_an_expired_budget_stops_before_the_tree_and_the_hosts(self):
        now = [1000.0]

        def map_for(*args, **kw):
            # la carte a consommé tout le bail de la cible
            now[0] += calls.land.LOCK_TTL_S
            return "", ""

        with mock.patch.object(calls, "parameters",
                               return_value=calls.Parameters(
                                   params=params(), repo="/canon",
                                   commit="c" * 40, project="ameesh")), \
                mock.patch.object(calls, "map_for", side_effect=map_for), \
                mock.patch.object(calls, "prepare") as prepare, \
                mock.patch.object(calls, "_run_hosts") as run_hosts:
            got = calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40,
                                   clock=lambda: now[0])
        self.assertEqual(got["state"], "unavailable")
        self.assertEqual(got["cause"], "run-incomplete")
        prepare.assert_not_called()
        run_hosts.assert_not_called()

    def test_a_red_result_is_not_masked_by_an_expired_budget(self):
        # Un rouge exécuté reste un résultat : il arrête le train même si le
        # nettoyage a consommé la marge de publication.
        now = [1000.0]
        prepared = SimpleNamespace(close=lambda *a, **kw: None)

        def run_hosts(*args, **kw):
            now[0] += 10 * calls.land.LOCK_TTL_S
            return report(ok=False, causes=("run-red",), failed=("tests.test_x",))

        with mock.patch.object(calls, "parameters",
                               return_value=calls.Parameters(
                                   params=params(), repo="/canon",
                                   commit="c" * 40, project="ameesh")), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "prepare", return_value=prepared), \
                mock.patch.object(calls, "_run_hosts", side_effect=run_hosts):
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40,
                                 clock=lambda: now[0])
        self.assertEqual(caught.exception.cause, "run-red")
        self.assertIn("tests.test_x", caught.exception.why)

    def test_a_run_red_raised_inside_the_try_is_not_downgraded(self):
        # #4073 : la garde #2644 doit rester — un run-red levé par une étape
        # (hôte, contrat, arbre) n'est pas une indisponibilité ; le train ne
        # publie pas. Sans la garde, `gate_train` rendrait `unavailable`.
        prepared = SimpleNamespace(close=lambda *a, **kw: None)
        with mock.patch.object(calls, "parameters",
                               return_value=calls.Parameters(
                                   params=params(), repo="/canon",
                                   commit="c" * 40, project="ameesh")), \
                mock.patch.object(calls, "map_for", return_value=("", "")), \
                mock.patch.object(calls, "prepare", return_value=prepared), \
                mock.patch.object(calls, "_run_hosts",
                                  side_effect=pv.PrevalidationError(
                                      "run-red", "tests.test_x")):
            with self.assertRaises(pv.PrevalidationError) as caught:
                calls.gate_train(None, repo="/repo", head="a" * 40, base="b" * 40)
        self.assertEqual(caught.exception.cause, "run-red")
        self.assertIn("tests.test_x", caught.exception.why)


class CallerDeadlineTest(unittest.TestCase):
    """#4067 : le gel et ``ci run`` rendent la main sous le délai du compositeur.

    Le canon déclare ``timeout_seconds: 3600`` ; sans échéance, l'offload
    attendait la file d'agents-3 et la commande était tuée à 600 s.
    """

    def setUp(self):
        self.now = [1000.0]
        self.clock = lambda: self.now[0]
        self.item = {"id": 4052, "app": "ameesh", "branch": "integration/h-44",
                     "branch_target": "main"}
        self.notes = []
        tree = tempfile.mkdtemp(prefix="ameesh-4067-tree-")
        self.addCleanup(shutil.rmtree, tree, True)
        prepared = calls.Prepared(repo=tree, root=tree, tree=tree,
                                  path=os.path.join(tree, "plan.json"),
                                  rel="plan.json", plan={})
        block = replace(params(hosts=("ameesh-agents-3",)), timeout_seconds=3600)
        for patch in (
                mock.patch.object(calls, "repository", return_value="/repo"),
                mock.patch.object(calls, "target_base", return_value="b" * 40),
                mock.patch.object(calls, "tooling"),
                mock.patch.object(calls, "map_for", return_value=("", "")),
                mock.patch.object(calls, "prepare", return_value=prepared),
                mock.patch.object(calls, "contract_fallback", return_value=None),
                mock.patch.object(calls, "parameters", return_value=calls.Parameters(
                    params=block, repo="/canon", commit="c" * 40, project="ameesh")),
                mock.patch.object(pv, "host_allowed", return_value=(True, "")),
                mock.patch.object(pv, "record"),
                mock.patch("ameesh.work.get", return_value=self.item),
                mock.patch("ameesh.work.note", side_effect=lambda db, lot, text, **kw:
                           self.notes.append(text))):
            patch.start()
            self.addCleanup(patch.stop)

    def offload(self, outcome):
        """Un ``validate`` qui lit l'échéance reçue, puis consomme du temps."""
        seen = {}

        def validate(**kw):
            deadline = kw["deadline"]
            seen["budget"] = deadline.remaining()
            seen["timeout_seconds"] = kw["params"].timeout_seconds
            if outcome == "slow":
                # la file de l'hôte retient le job : le client annule à l'échéance
                self.now[0] += deadline.remaining()
                return report(ok=False, causes=("run-incomplete",),
                              outcome="run-incomplete")
            self.now[0] += 30.0
            if outcome == "red":
                return report(ok=False, causes=("run-red",), failed=("tests.test_x",))
            return report()
        return validate, seen

    def packet(self, validate):
        with mock.patch.object(pv, "validate", side_effect=validate):
            return calls.gate_packet(
                None, None, self.item, {"sha": "a" * 40, "base": "b" * 40},
                deadline=calls.caller_deadline(clock=self.clock))

    def freeze(self, validate):
        with mock.patch.object(pv, "validate", side_effect=validate):
            return calls.after_freeze(
                None, object(), 4052, "a" * 40,
                deadline=calls.caller_deadline(clock=self.clock))

    def test_the_caller_budget_stays_under_the_composer_timeout(self):
        self.assertLess(calls.CALLER_BUDGET_S + pv.OFFLOAD_LOCAL_GRACE_S, 600)

    def test_a_slow_packet_offload_returns_unavailable_timeout_in_time(self):
        validate, seen = self.offload("slow")
        got = self.packet(validate)
        self.assertEqual(got["state"], "unavailable")
        self.assertEqual(got["cause"], "run-incomplete")
        self.assertTrue(got["why"].startswith("timeout"), got["why"])
        self.assertLessEqual(seen["timeout_seconds"], calls.CALLER_BUDGET_S)
        self.assertLessEqual(seen["budget"], calls.CALLER_BUDGET_S)
        self.assertLessEqual(self.now[0] - 1000.0, calls.CALLER_BUDGET_S)

    def test_a_slow_freeze_offload_is_journaled_timeout_in_time(self):
        validate, seen = self.offload("slow")
        got = self.freeze(validate)
        self.assertEqual(got["state"], "unavailable")
        self.assertTrue(any(n.startswith("prevalidation unavailable") and "timeout" in n
                            for n in self.notes), self.notes)
        self.assertLessEqual(seen["timeout_seconds"], calls.CALLER_BUDGET_S)
        self.assertLessEqual(self.now[0] - 1000.0, calls.CALLER_BUDGET_S)

    def test_a_fast_green_offload_is_unchanged(self):
        validate, _ = self.offload("green")
        self.assertEqual(self.packet(validate)["state"], "green")
        validate, _ = self.offload("green")
        self.assertEqual(self.freeze(validate)["state"], "green")
        self.assertFalse(any("timeout" in n for n in self.notes))

    def test_a_real_red_still_blocks_the_packet(self):
        validate, _ = self.offload("red")
        got = self.packet(validate)
        self.assertEqual(got["state"], "red")
        self.assertIn("tests.test_x", got["why"])

    def test_an_expired_budget_launches_no_offload(self):
        with mock.patch.object(pv, "validate") as validate:
            got = calls.gate_packet(None, None, self.item,
                                    {"sha": "a" * 40, "base": "b" * 40},
                                    deadline=calls.caller_deadline(0, clock=self.clock))
        validate.assert_not_called()
        self.assertEqual(got["state"], "unavailable")
        self.assertTrue(got["why"].startswith("timeout"), got["why"])

    def test_a_git_timeout_after_the_budget_is_timeout_not_unsafe(self):
        def slow(*a, **kw):
            self.now[0] += calls.CALLER_BUDGET_S
            raise subprocess.TimeoutExpired(["git"], 1)

        with mock.patch.object(calls, "prepare", side_effect=slow):
            got = calls.gate_packet(None, None, self.item,
                                    {"sha": "a" * 40, "base": "b" * 40},
                                    deadline=calls.caller_deadline(clock=self.clock))
        self.assertEqual(got["cause"], "run-incomplete")
        self.assertTrue(got["why"].startswith("timeout"), got["why"])

    def test_the_offload_client_cancels_its_job_within_the_budget(self):
        # canon 3600 s, budget 420 s : --timeout et --wait-timeout sous le budget,
        # le garde-fou local à budget + grâce.
        seen = {}

        def runner(argv, **kw):
            seen["argv"], seen["timeout"] = argv, kw.get("timeout")
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

        got = pv.run_offload("ameesh-agents-3", "true", timeout=3600, runner=runner,
                             deadline=calls.caller_deadline(clock=self.clock))
        argv = seen["argv"]
        self.assertLessEqual(int(argv[argv.index("--timeout") + 1]), calls.CALLER_BUDGET_S)
        self.assertLess(int(argv[argv.index("--wait-timeout") + 1]), calls.CALLER_BUDGET_S)
        self.assertLessEqual(seen["timeout"],
                             calls.CALLER_BUDGET_S + pv.OFFLOAD_LOCAL_GRACE_S)
        self.assertEqual(got["outcome"], "run-incomplete")

    def test_the_freeze_budget_stays_under_the_composer_timeout(self):
        # #4073 : le gel entier (contrôles statiques + prévalidation) doit
        # rendre la main sous les 600 s du compositeur, et ne pas raccourcir
        # la prévalidation quand les contrôles sont rapides.
        self.assertLess(calls.FREEZE_BUDGET_S, 600)
        self.assertGreaterEqual(calls.FREEZE_BUDGET_S, calls.CALLER_BUDGET_S)

    def test_the_freeze_budget_caps_the_caller_deadline(self):
        # 300 s de contrôles statiques : il reste 270 s de prévalidation, pas
        # le budget d'appelant de 420 s.
        self.now[0] = 1000.0
        limit = calls.freeze_deadline(clock=self.clock)
        self.now[0] += 300.0
        deadline = calls.caller_deadline(clock=self.clock, limit=limit)
        self.assertLessEqual(deadline.remaining(), calls.FREEZE_BUDGET_S - 300.0)
        self.assertLessEqual(deadline.remaining(), calls.CALLER_BUDGET_S)
        # un budget d'appelant plus court reste prioritaire
        smaller = calls.caller_deadline(90, clock=self.clock, limit=limit)
        self.assertLessEqual(smaller.remaining(), 90.0)

    def test_an_expired_freeze_budget_launches_no_offload(self):
        # L'échéance du gel entier épuisée : aucune prévalidation ne part.
        self.now[0] = 1000.0
        limit = calls.freeze_deadline(clock=self.clock)
        self.now[0] += calls.FREEZE_BUDGET_S
        deadline = calls.caller_deadline(clock=self.clock, limit=limit)
        self.assertTrue(deadline.expired())
        with mock.patch.object(pv, "validate") as validate:
            got = calls.after_freeze(None, object(), 4052, "a" * 40,
                                     deadline=deadline)
        validate.assert_not_called()
        self.assertEqual(got["state"], "unavailable")
        self.assertTrue(got["why"].startswith("timeout"), got["why"])


class DeadlineTest(unittest.TestCase):
    def test_timeout_never_exceeds_the_shared_deadline(self):
        now = [100.0]
        deadline = calls.Deadline(50.0, clock=lambda: now[0])
        self.assertEqual(deadline.timeout(120), 50.0)
        now[0] += 40.0
        self.assertEqual(deadline.timeout(120), 10.0)
        now[0] += 10.0
        self.assertTrue(deadline.expired())
        self.assertEqual(deadline.timeout(120), 0.0)

    def test_the_train_budget_keeps_a_margin_under_the_lock(self):
        now = [100.0]
        budget = calls.train_deadline(clock=lambda: now[0]).at - now[0]
        ttl = calls.land.LOCK_TTL_S
        self.assertLess(budget, ttl)
        self.assertAlmostEqual(budget, ttl - min(calls.TRAIN_LOCK_MARGIN, ttl / 6.0))


class HostFallbackTest(unittest.TestCase):
    def test_host_fallbacks_never_run_past_the_deadline(self):
        # #2665/N cx1 : chaque repli ne reçoit que le temps restant ; l'hôte
        # suivant n'est pas essayé après l'échéance.
        now = [100.0]
        deadline = calls.Deadline(100.0, clock=lambda: now[0])
        seen = []

        def check(**kwargs):
            # l'échéance atteint le geste complet (validate → run_offload)
            self.assertIs(kwargs.get("deadline"), deadline)
            seen.append(kwargs["params"].timeout_seconds)
            now[0] += 80.0
            return report(ok=False, causes=("no-vm",), outcome="no-vm")

        prepared = SimpleNamespace(path="/plan", tree="/tree")
        block = replace(params(hosts=("vm1", "vm2", "vm3")), timeout_seconds=1000)
        with self.assertRaises(pv.PrevalidationError) as caught:
            calls._run_hosts(
                prepared,
                calls.Parameters(params=block, repo="", commit="", project="x"),
                lot=0, head="a" * 40, base="b" * 40, branch="",
                profile="", say=None, executable="ameesh", validate=check,
                deadline=deadline)
        self.assertEqual(caught.exception.cause, "run-incomplete")
        self.assertEqual(seen, [100, 20])


class MapTest(unittest.TestCase):
    def test_explicit_map_wins(self):
        path, cleanup = calls.map_for("manaty/ameesh", "/repo", explicit="/tmp/map.gz")
        self.assertEqual((path, cleanup), ("/tmp/map.gz", ""))

    def test_local_carte_in_the_tree(self):
        root = tempfile.mkdtemp(prefix="ameesh-2644-map-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "carte"))
        path = os.path.join(root, "carte", "carte.json.gz")
        with open(path, "wb") as fh:
            fh.write(b"x")
        got, cleanup = calls.map_for("manaty/ameesh", root)
        self.assertEqual((got, cleanup), (path, ""))

    def test_latest_carte_artifact_is_downloaded(self):
        root = tempfile.mkdtemp(prefix="ameesh-2644-map-")
        self.addCleanup(shutil.rmtree, root, True)

        def runner(argv, **kw):
            if argv[:3] == ["gh", "run", "list"]:
                self.assertIn("--branch", argv)
                self.assertEqual(argv[argv.index("--branch") + 1], "main")
                return SimpleNamespace(returncode=0, stdout='[{"databaseId": 42}]')
            if argv[:3] == ["gh", "run", "download"]:
                target = argv[argv.index("-D") + 1]
                os.makedirs(target, exist_ok=True)
                with open(os.path.join(target, "carte.json.gz"), "wb") as fh:
                    fh.write(b"x")
                return SimpleNamespace(returncode=0, stdout="")
            raise AssertionError(argv)

        path, cleanup = calls.map_for("manaty/ameesh", root, runner=runner)
        self.assertTrue(path.endswith("carte.json.gz"))
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(cleanup)
        shutil.rmtree(cleanup, ignore_errors=True)

    def test_carte_artifact_is_only_read_from_main(self):
        """#3352 : un run carte de PR ne fournit jamais la carte de la cible."""
        root = tempfile.mkdtemp(prefix="ameesh-2644-map-")
        self.addCleanup(shutil.rmtree, root, True)
        seen = []

        def runner(argv, **kw):
            seen.append(list(argv))
            if argv[:3] == ["gh", "run", "list"]:
                return SimpleNamespace(returncode=0, stdout="[]")
            raise AssertionError(argv)

        self.assertEqual(calls.map_for("manaty/ameesh", root, runner=runner),
                         ("", ""))
        self.assertEqual(seen, [[
            "gh", "run", "list", "--repo", "manaty/ameesh", "--workflow",
            "carte.yml", "--branch", "main", "--status", "success",
            "--limit", "1", "--json", "databaseId"]])

    def test_no_map_is_not_an_error(self):
        self.assertEqual(calls.map_for("", ""), ("", ""))


class DescribeTest(unittest.TestCase):
    def test_cause_failed_tests_and_missing_modules(self):
        text = calls.describe(report(ok=False, causes=("run-red",),
                                     failed=("tests.a", "tests.b"),
                                     missing=("test_c",)))
        self.assertIn("run-red", text)
        self.assertIn("tests.a", text)
        self.assertIn("test_c", text)


if __name__ == "__main__":
    unittest.main()
