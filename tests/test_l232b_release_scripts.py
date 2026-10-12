# SPDX-License-Identifier: AGPL-3.0-only
"""Release pinning through the real dispatcher, with isolated fake migrations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_MODULE = '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["RELEASE_CALLS"], "a") as out:
    out.write(json.dumps({"release": Path(__file__).with_name("release").read_text(),
                          "args": args}) + "\\n")
if args[0] == "migrate" and "--additive-only" in args and Path(__file__).with_name("nonadditive").exists():
    sys.exit(3)
'''


class MigrationPinningTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ameesh-l232b-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = dict(os.environ, HOME=str(self.root / "home"),
                        GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.invalid",
                        RELEASE_CALLS=str(self.root / "calls"),
                        BASCULE_ENV=str(self.root / "bascule.env"))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python3").symlink_to(sys.executable)
        self.env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
        self.git("init", "-q")
        self.script = self.repo / "deploy/mise-a-jour/poste.sh"
        self.script.parent.mkdir(parents=True)
        shutil.copy(ROOT / "deploy/mise-a-jour/poste.sh", self.script)
        self.module = self.repo / "src/ameesh"
        self.module.mkdir(parents=True)
        (self.module / "__init__.py").write_text("")
        (self.module / "__main__.py").write_text(FAKE_MODULE)
        self.a = self.commit("A")
        self.b = self.commit("B")
        (self.root / "bascule.env").write_text("VM=simulation\n")
        self.share = Path(self.env["HOME"]) / ".local/share/ameesh"
        venv = self.share / "venv/bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(sys.executable)
        config = Path(self.env["HOME"]) / ".config/ameesh/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"dsn": "postgresql://simulation"}))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              check=True, text=True, capture_output=True).stdout.strip()

    def commit(self, release, nonadditive=False):
        (self.module / "release").write_text(release)
        if nonadditive:
            (self.module / "nonadditive").touch()
        self.git("add", ".")
        self.git("commit", "-qm", release)
        return self.git("rev-parse", "HEAD")

    def run_script(self, *args, **env):
        return subprocess.run(["bash", str(self.script), *args],
                              env=dict(self.env, **env), capture_output=True,
                              text=True, timeout=20)

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_approved_a_with_head_b_only_runs_a(self):
        done = self.run_script("migrer", self.a)
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertEqual([call["release"] for call in calls], ["A", "A", "A"])
        self.assertEqual(calls[0]["args"], ["migrate", "--dry-run", "--additive-only"])
        self.assertEqual(calls[1]["args"], ["migrate", "--additive-only"])
        self.assertEqual(calls[2]["args"], ["doctor", "--probe"])

    def test_missing_ref_never_runs_migrations(self):
        done = self.run_script("migrer")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_installed_nonadditive_still_refused_with_override(self):
        c = self.commit("C", nonadditive=True)
        # Legacy installed state cannot be an implicit migration authorisation.
        (self.share / "src-current").symlink_to("src-" + c[:7])
        done = self.run_script("migrer", c, MIGRER_APRES="1")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertEqual(self.calls(), [{"release": "C", "args": ["migrate", "--dry-run", "--additive-only"]}])
