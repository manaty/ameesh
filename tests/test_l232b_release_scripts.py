# SPDX-License-Identifier: AGPL-3.0-only
"""Release pinning through the real dispatcher, with isolated fake migrations."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
# poste.sh « sauvegarder » exécute sauvegarder-prive.py, qui parle psycopg : la
# part psql de la CI n'en a pas, ces deux témoins y sont sautés et couverts par
# la part psycopg (même règle que test_653_db_connections).
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None
FAKE_MODULE = '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["RELEASE_CALLS"], "a") as out:
    out.write(json.dumps({"release": Path(__file__).with_name("release").read_text(),
                          "args": args}) + "\\n")
# Like cli.cmd_migrate, a dry-run reports nonadditivity without refusing.
if args[0] == "migrate" and "--dry-run" not in args and "--additive-only" in args and Path(__file__).with_name("nonadditive").exists():
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
        (bin_dir / "pg_dump").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "${PG_DUMP_CALLS:?}"\n'
            'while [ "$1" != --file ]; do shift; done\nprintf archive > "$2"\n')
        (bin_dir / "pg_dump").chmod(0o755)
        (bin_dir / "pg_restore").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "pg_restore").chmod(0o755)
        (bin_dir / "ssh").write_text(
            '#!/bin/sh\nfor arg do command=$arg; done\n'
            '/bin/sh -c "$command"\n')
        (bin_dir / "ssh").chmod(0o755)
        (bin_dir / "systemctl").write_text(
            '#!/bin/sh\ncase "$1" in\n'
            'start) [ "${BACKUP_START_STATUS:-0}" = 0 ] || exit "${BACKUP_START_STATUS}";\n'
            '  d=${SAUVEGARDES_VM:?}; mkdir -p "$d";\n'
            '  n=$(find "$d" -maxdepth 1 -type f -name "ameesh-*.dump" | wc -l);\n'
            '  printf "server dump %s\\n" "$n" > "$d/ameesh-simulation-20261017-1500$(printf "%02d" "$n").dump";\n'
            '  exit 0 ;;\n'
            'show) case "$4" in\n'
            'Result) echo "${BACKUP_RESULT:-success}" ;;\n'
            'ExecMainStatus) echo "${BACKUP_EXIT_STATUS:-0}" ;;\n'
            '*) exit 97 ;; esac ;;\n'
            '*) exit 98 ;; esac\n')
        (bin_dir / "systemctl").chmod(0o755)
        (bin_dir / "sha256sum").write_text(
            '#!/bin/sh\n'
            'if [ "${FAKE_SHA256_BAD:-0}" = 1 ]; then\n'
            '  printf "%064d  %s\\n" 0 "${2:-}"\n'
            '  exit 0\n'
            'fi\n'
            'exec /usr/bin/sha256sum "$@"\n')
        (bin_dir / "sha256sum").chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
        self.remote_dumps = self.root / "remote-dumps"
        self.remote_dumps.mkdir()
        self.env["SAUVEGARDES_VM"] = str(self.remote_dumps)
        self.env["PG_DUMP_CALLS"] = str(self.root / "pg_dump-calls")
        self.git("init", "-q")
        self.script = self.repo / "deploy/mise-a-jour/poste.sh"
        self.script.parent.mkdir(parents=True)
        shutil.copy(ROOT / "deploy/mise-a-jour/poste.sh", self.script)
        shutil.copy(ROOT / "deploy/mise-a-jour/sauvegarder-prive.py", self.script.with_name("sauvegarder-prive.py"))
        self.module = self.repo / "src/ameesh"
        self.module.mkdir(parents=True)
        (self.module / "__init__.py").write_text("")
        (self.module / "__main__.py").write_text(FAKE_MODULE)
        # poste.sh splits the DSN's password through the deployed commit's
        # src/ameesh/dsn.py (#518); a real snapshot carries it, so the fixture does.
        shutil.copy(ROOT / "src/ameesh/dsn.py", self.module / "dsn.py")
        self.a = self.commit("A")
        self.b = self.commit("B")
        (self.root / "bascule.env").write_text("VM=simulation\n")
        self.share = Path(self.env["HOME"]) / ".local/share/ameesh"
        venv = self.share / "venv/bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        (venv / "python").chmod(0o755)
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

    def test_missing_ref_takes_clone_head(self):
        # #1245 : sans REF, « migrer » migre le HEAD du dépôt de déploiement,
        # comme « installer » ; le contrôleur L232b migre, lui, son commit épinglé.
        done = self.run_script("migrer")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertEqual([call["release"] for call in calls], ["B", "B", "B"])
        self.assertEqual(calls[0]["args"], ["migrate", "--dry-run", "--additive-only"])
        self.assertEqual(calls[1]["args"], ["migrate", "--additive-only"])

    def test_installed_nonadditive_still_refused_with_override(self):
        c = self.commit("C", nonadditive=True)
        # Legacy installed state cannot be an implicit migration authorisation.
        (self.share / "src-current").symlink_to("src-" + c[:7])
        done = self.run_script("migrer", c, MIGRER_APRES="1")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertEqual(self.calls(), [
            {"release": "C", "args": ["migrate", "--dry-run", "--additive-only"]},
            {"release": "C", "args": ["migrate", "--additive-only"]},
        ])

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_failed_backup_start_is_propagated(self):
        done = self.run_script("sauvegarder", BACKUP_START_STATUS="42")
        self.assertEqual(done.returncode, 42, done.stdout + done.stderr)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_completed_backup_result_and_exit_status_are_required(self):
        for env in ({"BACKUP_RESULT": "exit-code"}, {"BACKUP_EXIT_STATUS": "1"}):
            with self.subTest(env=env):
                done = self.run_script("sauvegarder", **env)
                self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_successful_oneshot_does_not_require_active_state(self):
        done = self.run_script("sauvegarder")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_mesh_dump_is_pulled_from_the_server_not_dumped_from_the_poste(self):
        done = self.run_script("sauvegarder")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        calls = self.root / "pg_dump-calls"
        self.assertFalse(calls.exists() and calls.read_text().strip(),
                         "pg_dump ran on the poste for the mesh database")
        dumps = list((Path(self.env["HOME"]) / "backups/ameesh").glob(
            "mesh-*-avant-l60-l74.dump"))
        self.assertEqual(len(dumps), 1, dumps)
        self.assertEqual(dumps[0].read_bytes(), b"server dump 0\n")
        self.assertEqual(stat.S_IMODE(dumps[0].stat().st_mode), 0o600)

    @unittest.skipUnless(HAVE_PSYCOPG,
                         "psycopg absent (part psql) : sauvegarder-prive.py non exécutable")
    def test_corrupt_retrieval_never_publishes_and_logs_bytes(self):
        done = self.run_script("sauvegarder", FAKE_SHA256_BAD="1")
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("digest mismatch", done.stderr)
        self.assertIn("bytes written", done.stderr)
        self.assertIn("rapatriement en échec", done.stderr)
        self.assertFalse(list((Path(self.env["HOME"]) / "backups/ameesh").glob(
            "mesh-*-avant-l60-l74.dump")))
