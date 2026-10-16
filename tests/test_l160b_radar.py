# SPDX-License-Identifier: AGPL-3.0-only
"""L160b : radar de conflits entre lots, en observation — fusions d'essai par
`git merge-tree`, sonde des migrations, journal lu par `ameesh conflicts`,
aucun courrier."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import radar, work

from .support import EMPTY_CONFIG, SRC, PgTestCase, child_env

MIG = "src/ameesh/migrations"


def git_env():
    """Environnement des git du banc (#1943) : hermétique à la configuration de
    l'agent (`~/.gitconfig`, `/etc/gitconfig` : rerere, autocrlf, hooks…), qui
    ne doit pas changer le verdict d'un test (canon §3 : même verdict sur tous
    les postes). Même isolation que les autres bancs du dépôt (`test_canon`,
    `test_1277`, `scripts/l160g-mergiraf.py`)."""
    return dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
                GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          env=git_env())
    if proc.returncode != 0:
        raise AssertionError("git %s : %s" % (" ".join(args), proc.stderr))
    return proc.stdout.strip()


def _write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


def make_repo(root) -> str:
    """main, et quatre branches de lots :
    a et b modifient la même ligne de `shared.py` (conflit) ; c touche
    `shared.py` ailleurs (recouvrement sans conflit) ; d ne touche que
    `other.py`. a et c ajoutent la migration 0002, d ajoute 0001 que main a
    déjà sous un autre nom."""
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _write(repo, "shared.py", "un\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n")
    _write(repo, "other.py", "x\n")
    _write(repo, MIG + "/0001_init.sql", "-- init\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    def branch(name, edits):
        _git(repo, "checkout", "-q", "-b", name, "main")
        for path, text in edits.items():
            _write(repo, path, text)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", name)
        _git(repo, "checkout", "-q", "main")

    branch("lot/1-a", {"shared.py": "UN\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n",
                       MIG + "/0002_a.sql": "-- a\n"})
    branch("lot/2-b", {"shared.py": "One\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n"})
    branch("lot/3-c", {"shared.py": "un\ndeux\ntrois\nquatre\ncinq\nsix\nSEPT\n",
                       MIG + "/0002_c.sql": "-- c\n"})
    branch("lot/4-d", {"other.py": "y\n", MIG + "/0001_d.sql": "-- d\n"})
    return repo


LOTS = [{"id": 1, "branch": "lot/1-a", "title": "a"},
        {"id": 2, "branch": "lot/2-b", "title": "b"},
        {"id": 3, "branch": "lot/3-c", "title": "c"},
        {"id": 4, "branch": "lot/4-d", "title": "d"},
        {"id": 5, "branch": "lot/5-ailleurs", "title": "branche d'un autre dépôt"}]


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-radar-test-")
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def objects(self):
        return _git(self.repo, "count-objects", "-v")

    def test_pairs_target_and_migrations(self):
        before = self.objects()
        report = radar.scan(self.repo, LOTS, migrations=MIG)
        self.assertEqual(before, self.objects(), "aucun objet écrit dans le dépôt")
        self.assertEqual([b["work_item"] for b in report["branches"]], [1, 2, 3, 4])
        self.assertEqual(report["pairs_checked"], 6)
        states = {tuple(p["lots"]): (p["state"], p["files"]) for p in report["pairs"]}
        self.assertEqual(states, {(1, 2): ("conflict", ["shared.py"]),
                                  (1, 3): ("overlap", []),
                                  (2, 3): ("overlap", [])})
        self.assertEqual({a["work_item"]: a["state"] for a in report["against_target"]},
                         {1: "clean", 2: "clean", 3: "clean", 4: "clean"})
        dups = {d["number"]: d for d in report["migrations"]}
        self.assertEqual(sorted(dups), ["0001", "0002"])
        self.assertEqual([h["work_item"] for h in dups["0002"]["lots"]], [1, 3])
        self.assertIsNone(dups["0002"]["in_target"])
        self.assertEqual(dups["0001"]["in_target"], MIG + "/0001_init.sql")
        self.assertEqual(radar.summary(report)["conflict"], 1)

    def test_conflict_with_target_and_origin_branch(self):
        _write(self.repo, "shared.py", "Uno\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n")
        _git(self.repo, "commit", "-qam", "main bouge")
        # une branche connue seulement par origin/ (agent d'un autre hôte)
        _git(self.repo, "update-ref", "refs/remotes/origin/lot/5-ailleurs", "lot/4-d")
        report = radar.scan(self.repo, LOTS, default_target="main")
        against = {a["work_item"]: (a["state"], a["files"]) for a in report["against_target"]}
        self.assertEqual(against[1], ("conflict", ["shared.py"]))
        self.assertEqual(against[4], ("clean", []))
        self.assertIn(5, against)
        self.assertEqual(report["migrations"], [])     # sonde non déclarée

    def test_cache_skips_unchanged_pairs(self):
        cache = radar.Cache()
        radar.scan(self.repo, LOTS, cache=cache)
        computed = dict(cache.merges)
        with mock.patch.object(radar, "trial_merge",
                                        side_effect=AssertionError("recalcul")):
            again = radar.scan(self.repo, LOTS, cache=cache)
        self.assertEqual(computed, cache.merges)
        self.assertEqual(len(again["pairs"]), 3)

    def test_changes_between_passes(self):
        first = radar.scan(self.repo, LOTS)
        _git(self.repo, "checkout", "-q", "lot/2-b")
        _write(self.repo, "shared.py", "UN\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n")
        _git(self.repo, "commit", "-qam", "b rejoint a")
        _git(self.repo, "checkout", "-q", "main")
        second = radar.scan(self.repo, LOTS)
        self.assertEqual(radar.changes(first, second),
                         [{"key": "pair:1-2", "from": "conflict", "to": "overlap"}])
        self.assertEqual(radar.changes(None, first)[0]["from"], "clean")


class SettingsTest(unittest.TestCase):
    def test_parse(self):
        got = radar.parse_settings({"projects": {"ameesh": {"repo": "~/w", "migrations":
                                                            MIG + "/"}}})
        project = got.projects["ameesh"]
        self.assertEqual(project.repo, os.path.expanduser("~/w"))
        self.assertEqual(project.migrations, MIG)
        self.assertEqual(radar.parse_settings(None).projects, {})
        self.assertEqual(radar.parse_settings({"projects": {"x": "/r"}}).projects["x"].repo,
                         "/r")

    def test_errors(self):
        for raw in ([], {"autre": 1}, {"projects": {"x": {}}},
                    {"projects": {"x": {"repo": "/r", "cle": 1}}},
                    {"projects": {"../x": "/r"}},
                    {"projects": {"x": {"repo": "/r", "target": "a..b"}}}):
            with self.assertRaises(ValueError, msg=raw):
                radar.parse_settings(raw)

    def test_full_suite_is_declared_by_workflow_file(self):
        """L756: la suite complète est une identité (le fichier de workflow),
        jamais un nom d'affichage libre."""
        raw = {"projects": {"x": {"repo": "/r", "full_suite":
                                  [".github/workflows/backend-suite.yml",
                                   ".github/workflows/desktop-suite.yaml"]}}}
        project = radar.parse_settings(raw).projects["x"]
        self.assertEqual(project.full_suite, (".github/workflows/backend-suite.yml",
                                              ".github/workflows/desktop-suite.yaml"))
        single = radar.parse_settings({"projects": {
            "x": {"repo": "/r", "full_suite": ".github/workflows/a.yml"}}}).projects["x"]
        self.assertEqual(single.full_suite, (".github/workflows/a.yml",))
        self.assertEqual(single.full_suite_required, ())
        self.assertEqual(single.full_suite_tolerated, ())
        named = radar.parse_settings({"projects": {"x": {"repo": "/r", "full_suite": [
            {"path": ".github/workflows/tests.yml",
             "jobs": ["plan", "psycopg *"], "tolerate": ["bilan des impactés"]},
            ".github/workflows/backend-suite.yml"]}}}).projects["x"]
        self.assertEqual(named.full_suite, (".github/workflows/tests.yml",
                                            ".github/workflows/backend-suite.yml"))
        self.assertEqual(named.full_suite_required,
                         ((".github/workflows/tests.yml", ("plan", "psycopg *")),))
        self.assertEqual(named.full_suite_tolerated,
                         ((".github/workflows/tests.yml", ("bilan des impactés",)),))
        for bad in (["Backend suite"],                                # un nom libre
                    ["backend-suite.yml"],                            # hors .github/workflows
                    [".github/workflows/a.txt"],                      # pas un workflow
                    ["/etc/.github/workflows/a.yml"],                 # absolu
                    [".github/workflows/../a.yml"],                   # remonte
                    [".github/workflows/a.yml", ".github/workflows/a.yml"],  # doublon
                    [{"path": ".github/workflows/a.yml"},             # jobs manquants
                     {"path": ".github/workflows/a.yml", "jobs": []},
                     {"path": ".github/workflows/a.yml", "jobs": [1]},
                     {"path": ".github/workflows/a.yml", "jobs": ["a", "a"]},
                     {"path": ".github/workflows/a.yml", "jobs": ["a"], "extra": 1},
                     {"path": ".github/workflows/a.yml", "jobs": ["*"]},      # * seul
                     {"path": ".github/workflows/a.yml", "jobs": ["a*b"]},    # * non final
                     {"path": ".github/workflows/a.yml", "jobs": ["a"],
                      "tolerate": ["a"]},                            # requis et toléré
                     {"path": ".github/workflows/a.yml", "jobs": ["a"],
                      "tolerate": [1]},
                     {"path": ".github/workflows/a.yml",
                      "tolerate": ["a"]},                              # toléré sans requis
                     {"jobs": ["a"]},
                     {"path": 1, "jobs": ["a"]},
                     [{"path": ".github/workflows/a.yml", "jobs": ["a"]},
                      ".github/workflows/a.yml"]],                    # doublon entre formes
                    [1],
                    "Backend suite"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    radar.parse_settings({"projects": {"x": {"repo": "/r",
                                                             "full_suite": bad}}})


class ConflictsCommandTest(unittest.TestCase):
    """`ameesh conflicts` lit le journal de l'hôte, sans base."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-radar-test-")
        self.state = os.path.join(self.tmp, "state")
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conflicts(self, *args):
        env = child_env(AMEESH_STATE=self.state, AMEESH_CONFIG=EMPTY_CONFIG,
                        AMEESH_DSN="postgresql://injoignable.invalid/x")
        env["PYTHONPATH"] = SRC
        return subprocess.run([sys.executable, "-m", "ameesh.main", "conflicts", *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_empty_then_journal(self):
        done = self.conflicts()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no radar pass", done.stdout)
        cfg = dataclasses.make_dataclass("Cfg", ["state_dir"])(self.state)
        report = radar.scan(self.repo, LOTS, migrations=MIG)
        report.update(project="ameesh", ts=0.0, duration_s=0.1, error=None)
        line = radar.write_journal(cfg, "ameesh", report)
        self.assertEqual(line["summary"]["conflict"], 1)
        self.assertTrue(line["changes"])
        done = self.conflicts()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("1 in conflict", done.stdout)
        self.assertIn("conflict #1 #2  shared.py", done.stdout)
        self.assertNotIn("overlap  #", done.stdout)
        self.assertIn("migration 0002", done.stdout)
        self.assertIn("overlap  #1 #3", self.conflicts("--all").stdout)
        data = json.loads(self.conflicts("--json").stdout)
        self.assertEqual(data["schema"], radar.SCHEMA)
        self.assertEqual(data["reports"][0]["project"], "ameesh")
        # second passage identique : aucun changement au journal
        self.assertEqual(radar.write_journal(cfg, "ameesh", report)["changes"], [])
        with open(os.path.join(self.state, "radar", "ameesh.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 2)


class RunPassTest(PgTestCase):
    """Le passage de l'exécuteur : lots ouverts de la base, aucun courrier."""

    def test_run_pass(self):
        repo = make_repo(self.tmp)
        for row in LOTS[:3]:
            work.add(self.db, title=row["title"], branch=row["branch"])
        closed = work.add(self.db, title="d", branch="lot/4-d")
        work.close(self.db, closed["id"], abandoned=True, note="test", actor="t")
        cfg = dataclasses.replace(self.cfg, radar={"projects": {"ameesh": {
            "repo": repo, "migrations": MIG}}})
        lines = radar.run_pass(cfg, self.db)
        self.assertEqual(len(lines), 1)
        self.assertIsNone(lines[0]["error"])
        self.assertEqual(lines[0]["summary"]["branches"], 3)
        self.assertEqual(lines[0]["summary"]["conflict"], 1)
        snapshot = radar.read_snapshot(cfg, "ameesh")
        self.assertEqual(snapshot["schema"], radar.SCHEMA)
        self.assertEqual(self.db.query("SELECT count(*) AS n FROM agent_mailbox")[0]["n"], 0)
        # sans `radar.projects` : rien
        self.assertEqual(radar.run_pass(self.cfg, self.db), [])


class BancGitHermetiqueTest(unittest.TestCase):
    """#1943 : le banc git des tests ne dépend pas de la config globale de
    l'agent — `test_l160e` voyait un `.git/rr-cache` créé par le
    `rerere.enabled=true` de son poste et rougissait (canon §3)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l160b-hermetique-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)

    def test_une_config_globale_hostile_n_atteint_pas_le_banc(self):
        hostile = os.path.join(self.tmp, "gitconfig")
        with open(hostile, "w", encoding="utf-8") as fh:
            fh.write("[rerere]\n\tenabled = true\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": hostile,
                                          "GIT_CONFIG_NOSYSTEM": ""}):
            # le conflit non résolu est le geste qui, sous rerere global,
            # laissait le rr-cache que l'assertion « dépôt intact » voyait
            _git(self.repo, "checkout", "-q", "-b", "conflit", "lot/2-b")
            with self.assertRaises(AssertionError):
                _git(self.repo, "merge", "-q", "--no-ff", "--no-commit", "lot/1-a")
            self.assertFalse(os.path.exists(
                os.path.join(self.repo, ".git", "rr-cache")))


if __name__ == "__main__":
    unittest.main()
