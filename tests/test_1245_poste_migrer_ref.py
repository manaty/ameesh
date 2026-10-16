# SPDX-License-Identifier: AGPL-3.0-only
"""#1245 — `poste.sh migrer <REF>` migre REF, pas le HEAD du clone.

Un dépôt local à deux révisions (A puis B) tient lieu de dépôt de déploiement ;
un faux `$VENV/bin/python` note le `PYTHONPATH` (l'instantané `src-<court>`) et
les arguments de chaque appel. Aucun réseau, aucune base, aucun venv réel.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "mise-a-jour", "poste.sh")

FAKE_PYTHON = r'''#!/bin/sh
printf '%s\t%s\n' "$PYTHONPATH" "$*" >> "$FAKE_STATE/argv.log"
exit 0
'''


def _git(cwd, *args) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class MigrerRefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1245-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "depot")
        self.state = os.path.join(self.tmp, "state")
        venv_bin = os.path.join(self.home, ".local", "share", "ameesh", "venv", "bin")
        conf = os.path.join(self.home, ".config", "ameesh")
        for path in (venv_bin, conf, self.state):
            os.makedirs(path)
        # dépôt de déploiement : le script, dsn.py, puis les révisions A et B
        os.makedirs(os.path.join(self.repo, "deploy", "mise-a-jour"))
        os.makedirs(os.path.join(self.repo, "src", "ameesh"))
        shutil.copy(SCRIPT, os.path.join(self.repo, "deploy", "mise-a-jour", "poste.sh"))
        shutil.copy(os.path.join(ROOT, "src", "ameesh", "dsn.py"),
                    os.path.join(self.repo, "src", "ameesh", "dsn.py"))
        _git(self.repo, "init", "-q", "-b", "main")
        self.sha_a = self.revision("0001_a.sql")
        self.sha_b = self.revision("0002_b.sql")
        # HOME du poste : bascule.env (VM requise), config.json, python enregistreur
        with open(os.path.join(conf, "bascule.env"), "w", encoding="utf-8") as fh:
            fh.write("VM=vm.invalid\n")
        with open(os.path.join(conf, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"dsn": "postgresql://ameesh@mesh.invalid:5432/ameesh"}, fh)
        python = os.path.join(venv_bin, "python")
        with open(python, "w", encoding="utf-8") as fh:
            fh.write(FAKE_PYTHON)
        os.chmod(python, 0o755)
        self.env = dict(os.environ, HOME=self.home, FAKE_STATE=self.state,
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        self.env.pop("AMEESH_DSN", None)

    def revision(self, migration) -> str:
        path = os.path.join(self.repo, "src", "ameesh", "migrations", migration)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("-- %s\n" % migration)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", migration)
        return _git(self.repo, "rev-parse", "HEAD")

    def run_script(self, *args):
        return subprocess.run(
            ["bash", os.path.join(self.repo, "deploy", "mise-a-jour", "poste.sh"), *args],
            capture_output=True, text=True, env=self.env, timeout=120)

    def calls(self):
        path = os.path.join(self.state, "argv.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            lignes = fh.read().splitlines()
        return [line.split("\t", 1) for line in lignes if line]

    def snapshot(self, sha) -> str:
        return os.path.join(self.home, ".local", "share", "ameesh", "src-%s" % sha[:7])

    def test_migrer_ref_prend_ref_pas_head(self):
        done = self.run_script("migrer", self.sha_a)
        self.assertEqual(done.returncode, 0, done.stderr)
        pythonpaths = {p for p, _ in self.calls()}
        self.assertIn(self.snapshot(self.sha_a) + "/src", pythonpaths)
        self.assertTrue(os.path.isfile(os.path.join(
            self.snapshot(self.sha_a), "src", "ameesh", "migrations", "0001_a.sql")))
        self.assertFalse(os.path.exists(self.snapshot(self.sha_b)),
                         "la révision HEAD a été prise alors que REF était demandé")

    def test_migrer_sans_ref_prend_head(self):
        done = self.run_script("migrer")
        self.assertEqual(done.returncode, 0, done.stderr)
        pythonpaths = {p for p, _ in self.calls()}
        self.assertIn(self.snapshot(self.sha_b) + "/src", pythonpaths)
