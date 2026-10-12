# SPDX-License-Identifier: AGPL-3.0-only
"""L159f2 (#278) : scripts/pg-up.sh ne fait jamais tourner deux postgres sur un
même volume — volume par défaut `${NAME}-data`, refus à la création comme au
démarrage d'un conteneur existant. Un faux `docker` (état JSON, journal des
appels) remplace le vrai : aucun conteneur n'est créé."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "pg-up.sh")

FAKE_DOCKER = r'''#!%(python)s
import json, os, sys
state_path = os.environ["FAKE_DOCKER_STATE"]
with open(state_path) as f:
    state = json.load(f)
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
cmd = args[0]

def save():
    with open(state_path, "w") as f:
        json.dump(state, f)

def opt(name):
    return args[args.index(name) + 1] if name in args else None

if cmd == "inspect":
    name, fmt = args[-1], opt("-f")
    c = state.get(name)
    if c is None:
        sys.exit(1)
    if fmt is None:
        print("[{}]")
    elif ".State.Running" in fmt:
        print("true" if c["running"] else "false")
    elif ".Mounts" in fmt:
        print(" ".join(c["volumes"]))
    elif ".State.Health.Status" in fmt:
        print("healthy")
elif cmd == "ps":
    vol = (opt("--filter") or "volume=").split("=", 1)[1]
    for name, c in state.items():
        if vol in c["volumes"] and (c["running"] or "-a" in args):
            print(name)
elif cmd == "start":
    state[args[1]]["running"] = True
    save()
elif cmd == "run":
    vol = opt("-v").split(":", 1)[0]
    state[opt("--name")] = {"running": True, "volumes": [vol]}
    save()
'''


class PgUpVolumeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pg-up-")
        bin_dir = os.path.join(self.dir, "bin")
        os.mkdir(bin_dir)
        docker = os.path.join(bin_dir, "docker")
        with open(docker, "w") as f:
            f.write(FAKE_DOCKER % {"python": sys.executable})
        os.chmod(docker, 0o755)
        self.state = os.path.join(self.dir, "state.json")
        self.log = os.path.join(self.dir, "calls.log")
        self.env = {
            "PATH": bin_dir + os.pathsep + "/usr/bin:/bin",
            "HOME": self.dir,
            "FAKE_DOCKER_STATE": self.state,
            "FAKE_DOCKER_LOG": self.log,
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, containers, **env):
        with open(self.state, "w") as f:
            json.dump(containers, f)
        open(self.log, "w").close()
        proc = subprocess.run(["bash", SCRIPT], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)
        with open(self.state) as f:
            after = json.load(f)
        with open(self.log) as f:
            calls = [json.loads(line) for line in f if line.strip()]
        return proc, after, calls

    def test_the_volume_follows_the_container_name(self):
        proc, after, _ = self.run_script(
            {}, AMEESH_PG_NAME="ameesh-dev7-pgtest", AMEESH_PG_PASSWORD="x")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after["ameesh-dev7-pgtest"]["volumes"], ["ameesh-dev7-pgtest-data"])

    def test_the_default_container_keeps_its_volume(self):
        proc, after, _ = self.run_script({}, AMEESH_PG_PASSWORD="x")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after["ameesh-pg"]["volumes"], ["ameesh-pg-data"])

    def test_creation_on_a_mounted_volume_is_refused(self):
        other = {"autre": {"running": False, "volumes": ["partage"]}}
        proc, after, calls = self.run_script(
            other, AMEESH_PG_NAME="nouveau", AMEESH_PG_VOLUME="partage",
            AMEESH_PG_PASSWORD="x")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("autre", proc.stderr)
        self.assertNotIn("nouveau", after)
        self.assertFalse([c for c in calls if c[0] == "run"])

    def test_starting_a_container_that_shares_a_running_volume_is_refused(self):
        # A et B partagent un volume depuis avant le garde : A tourne, B est
        # arrêté ; pg-up.sh avec le nom de B ne doit pas le démarrer
        shared = {"A": {"running": True, "volumes": ["partage"]},
                  "B": {"running": False, "volumes": ["partage"]}}
        proc, after, calls = self.run_script(shared, AMEESH_PG_NAME="B")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("A", proc.stderr)
        self.assertFalse(after["B"]["running"])
        self.assertFalse([c for c in calls if c[0] == "start"])

    def test_a_stopped_container_alone_on_its_volume_starts(self):
        alone = {"B": {"running": False, "volumes": ["b-data"]},
                 "C": {"running": False, "volumes": ["b-data"]}}
        # C est arrêté : il ne monte rien en ce moment, B peut démarrer
        proc, after, _ = self.run_script(alone, AMEESH_PG_NAME="B")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(after["B"]["running"])


if __name__ == "__main__":
    unittest.main()
