# SPDX-License-Identifier: AGPL-3.0-only
"""#3886 : la purge du banc trouve ameesh hors d'un clone.

Une copie déployée de pg-up.sh (staging, #3833) n'a pas de src/ à côté : le
python3 du système y levait ModuleNotFoundError. La purge prend AMEESH_PYTHON,
sinon l'interpréteur du venv d'ameesh, sinon python3 ; src/ n'est ajouté au
chemin que dans un clone.

Le faux `docker` de #3833 crée un banc sain ; un faux interpréteur consigne
ses arguments, son PYTHONPATH et son répertoire : aucune base n'est touchée.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.test_3833_pg_up_healthcheck import FAKE_DOCKER, SCRIPT

FAKE_PYTHON = '''#!/bin/sh
# la sonde `-c 'import ameesh.bench'` réussit sans être consignée
[ "$1" = -c ] && exit 0
printf '%s|%s|%s|%s\\n' "$*" "${PYTHONPATH:-}" "$PWD" "$0" >> "$FAKE_PYTHON_LOG"
'''


class PgUpPurgePythonTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pg-up-3886-")
        bin_dir = os.path.join(self.dir, "bin")
        os.mkdir(bin_dir)
        docker = os.path.join(bin_dir, "docker")
        with open(docker, "w") as f:
            f.write(FAKE_DOCKER % {"python": sys.executable})
        os.chmod(docker, 0o755)
        self.home = os.path.join(self.dir, "home")
        os.mkdir(self.home)
        self.state = os.path.join(self.dir, "state.json")
        self.pylog = os.path.join(self.dir, "python.log")
        self.env = {
            "PATH": bin_dir + os.pathsep + "/usr/bin:/bin",
            "HOME": self.home,
            "FAKE_DOCKER_STATE": self.state,
            "FAKE_DOCKER_LOG": os.path.join(self.dir, "calls.log"),
            "FAKE_PYTHON_LOG": self.pylog,
            "AMEESH_PG_PASSWORD": "x",
        }

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_python(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(FAKE_PYTHON)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def deployed_copy(self):
        """pg-up.sh copié seul, sans src/ à côté, comme sur un hôte."""
        root = os.path.join(self.dir, "deploy")
        os.makedirs(os.path.join(root, "scripts"))
        script = os.path.join(root, "scripts", "pg-up.sh")
        shutil.copy(SCRIPT, script)
        return root, script

    def run_script(self, script, **env):
        with open(self.state, "w") as f:
            json.dump({}, f)
        proc = subprocess.run(
            ["bash", script], env=dict(self.env, **env),
            capture_output=True, text=True, timeout=60)
        calls = []
        if os.path.exists(self.pylog):
            with open(self.pylog) as f:
                calls = [line.rstrip("\n").split("|") for line in f]
        return proc, calls

    def test_deployed_copy_uses_the_ameesh_venv(self):
        root, script = self.deployed_copy()
        venv = self.fake_python(os.path.join(
            self.home, ".local/share/ameesh/venv/bin/python"))
        self.assertTrue(os.access(venv, os.X_OK))
        proc, calls = self.run_script(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(calls), 1, proc.stdout)
        argv, pythonpath, cwd, interpreter = calls[0]
        self.assertTrue(argv.startswith("-m ameesh.bench purge --dsn "), argv)
        self.assertEqual(pythonpath, "")
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(root))
        self.assertEqual(interpreter, venv)

    def test_ameesh_python_wins_over_the_venv(self):
        _, script = self.deployed_copy()
        self.fake_python(os.path.join(
            self.home, ".local/share/ameesh/venv/bin/python"))
        other = self.fake_python(os.path.join(self.dir, "autre", "python"))
        proc, calls = self.run_script(script, AMEESH_PYTHON=other)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([c[3] for c in calls], [other])

    def test_deployed_copy_without_venv_skips_cleanly(self):
        _, script = self.deployed_copy()
        # python3 du système sans ameesh, quel que soit l'hôte de la suite
        system = os.path.join(self.dir, "bin", "python3")
        with open(system, "w") as f:
            f.write("#!/bin/sh\n[ \"$1\" = -c ] && exit 1\n"
                    "echo \"$*\" >> \"$FAKE_PYTHON_LOG\"\n")
        os.chmod(system, 0o755)
        proc, calls = self.run_script(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, [])
        self.assertIn("banc : purge sautée", proc.stdout)
        self.assertNotIn("ModuleNotFoundError", proc.stdout + proc.stderr)

    def test_venv_without_bench_skips_cleanly(self):
        """Un venv antérieur à #3735 n'a pas ameesh.bench : une ligne, pas
        de trace d'erreur."""
        _, script = self.deployed_copy()
        old = os.path.join(self.home, ".local/share/ameesh/venv/bin/python")
        os.makedirs(os.path.dirname(old))
        with open(old, "w") as f:
            # la sonde échoue comme `import ameesh.bench` sur ce venv ; tout
            # autre appel serait consigné
            f.write("#!/bin/sh\n[ \"$1\" = -c ] && exit 1\n"
                    "echo \"$*\" >> \"$FAKE_PYTHON_LOG\"\n")
        os.chmod(old, 0o755)
        proc, calls = self.run_script(script)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(calls, [])
        self.assertIn("banc : purge sautée : ameesh.bench introuvable pour "
                      + old, proc.stdout)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_clone_puts_src_first_on_the_path(self):
        venv = self.fake_python(os.path.join(self.dir, "venv", "python"))
        proc, calls = self.run_script(
            SCRIPT, AMEESH_PYTHON=venv, PYTHONPATH="/ailleurs")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "src:/ailleurs")


if __name__ == "__main__":
    unittest.main()
