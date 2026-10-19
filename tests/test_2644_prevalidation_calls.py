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
from types import SimpleNamespace
from unittest import mock

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
    return SimpleNamespace(classes=("P0", "P1"), contracts=(), hosts=hosts,
                           timeout_seconds=60, parts_script="scripts/test-parts.py")


class Repo:
    """Un dépôt jetable avec un `scripts/test-plan.py` factice."""

    def __init__(self, script=None):
        self.root = tempfile.mkdtemp(prefix="ameesh-2644-")
        subprocess.run(["git", "init", "-q", "-b", "main", self.root],
                       check=True, env=ENV)
        self.write("README", "base\n")
        self.base = self.commit("base")
        self.write("scripts/test-plan.py", script if script is not None else PLAN_SCRIPT)
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
        'selection': {'mode': 'impacted', 'selected': 1}, 'sure': True,
        'phase2_safe': True, 'classes': {}, 'contract': [], 'parts': {}}
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

        def load(repo, commit, project):
            if repo == "/canon1":
                raise pv.PrevalidationError("block-missing", "absent")
            return None, block

        with mock.patch("ameesh.canon.load_configured", return_value=canons), \
                mock.patch.object(pv, "load_parameters", side_effect=load):
            got = calls.parameters(cfg, "ameesh")
        self.assertEqual(got.repo, "/canon2")
        self.assertEqual(got.commit, "2" * 40)


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

    def test_failing_script_is_named_and_cleaned(self):
        self.repo.write("scripts/test-plan.py", "import sys\nsys.exit(3)\n")
        head = self.repo.commit("red script")
        with self.assertRaises(pv.PrevalidationError) as caught:
            calls.prepare(self.repo.root, head, self.repo.base)
        self.assertEqual(caught.exception.cause, "selection-unsafe")
        self.assertIn("refusé le plan", caught.exception.why)


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

    def call(self, validate, *, hosts=("vm1", "vm2"), database=None):
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
                             project="ameesh", validate=validate)

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

    def test_persistence_failure_does_not_turn_a_green_run_into_a_refusal(self):
        fake_db = object()
        warnings = []
        with mock.patch.object(pv, "record", side_effect=RuntimeError("0450 absente")):
            got = self.call(lambda **kw: report(), database=fake_db)
        # `run` ne lève pas : le run réel reste la preuve.
        self.assertTrue(got["ok"])


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

    def test_red_result_never_blocks_the_freeze(self):
        outcome = self.freeze(returns=report(ok=False, causes=("run-red",),
                                             failed=("tests.test_x",)))
        self.assertEqual(outcome["state"], "red")
        self.assertTrue(any("run-red" in n and "tests.test_x" in n for n in self.notes))

    def test_unavailable_prevalidation_is_journaled(self):
        outcome = self.freeze(raises=pv.PrevalidationError("no-vm", "vm absente"))
        self.assertEqual(outcome["state"], "unavailable")
        self.assertTrue(any("unavailable" in n and "no-vm" in n for n in self.notes))

    def test_a_freeze_without_sha_is_journaled(self):
        outcome = calls.after_freeze(None, object(), 2644, "")
        self.assertEqual(outcome["state"], "unavailable")
        self.assertTrue(any("gel sans --sha" in n for n in self.notes))

    def test_an_unexpected_database_error_does_not_escape(self):
        with mock.patch.object(calls, "repository", side_effect=RuntimeError("registre")):
            outcome = calls.after_freeze(None, object(), 2644, "a" * 40)
        self.assertEqual(outcome["state"], "unavailable")


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
        prepared = SimpleNamespace(close=lambda: None)
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
        prepared = SimpleNamespace(close=lambda: None)
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
