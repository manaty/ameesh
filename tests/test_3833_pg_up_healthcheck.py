# SPDX-License-Identifier: AGPL-3.0-only
"""#3833 : le banc naît avec docker-init et un healthcheck sans shell.

Sans `--init`, le postmaster est PID 1 du conteneur et recueille tout
orphelin : le healthcheck `CMD-SHELL pg_isready` est tué par Docker à son
délai de 3 s sous un verrou de catalogue (VACUUM FULL, DROP DATABASE) ; le
`pg_isready` orphelin sort 2 (PQPING_NO_RESPONSE) et le postmaster le prend
pour un backend mort — « server process (PID n) exited with exit code 2 » —
puis redémarre le banc. Le healthcheck `exec pg_isready` retire le shell (donc
l'orphelin) et `--init` fait recueillir les orphelins par docker-init.

Un faux `docker` (état JSON, journal des appels) remplace le vrai : aucun
conteneur n'est créé et le ménage du banc est désactivé.
"""

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


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def save():
    with open(state_path, "w") as f:
        json.dump(state, f)


if cmd == "inspect":
    name = args[-1]
    c = state.get(name)
    if c is None:
        sys.exit(1)
    fmt = opt("-f")
    if fmt is None:
        print("[{}]")
    elif ".State.Running" in fmt:
        print("true" if c.get("running") else "false")
    elif ".HostConfig.Init" in fmt:
        print("true" if c.get("init") else "false")
    elif ".State.Health.Status" in fmt:
        print("healthy")
    elif ".Mounts" in fmt:
        pass
elif cmd == "ps":
    if "-a" not in args and "--all" not in args:
        for name, c in state.items():
            if c.get("running"):
                print(name)
elif cmd == "volume":
    pass
elif cmd == "run":
    state[opt("--name")] = {
        "running": True,
        "init": "--init" in args,
        "health_cmd": opt("--health-cmd"),
    }
    save()
elif cmd == "start":
    state[args[1]]["running"] = True
    save()
elif cmd == "logs":
    pass
'''


class PgUpHealthcheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pg-up-3833-")
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
            # le faux `run` ne crée rien : ne jamais toucher le vrai banc
            "AMEESH_PG_UPKEEP": "0",
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, containers, **env):
        with open(self.state, "w") as f:
            json.dump(containers, f)
        open(self.log, "w").close()
        proc = subprocess.run(
            ["bash", SCRIPT], env=dict(self.env, **env),
            capture_output=True, text=True, timeout=60)
        with open(self.log) as f:
            calls = [json.loads(line) for line in f if line.strip()]
        return proc, calls

    def test_a_new_container_gets_an_init_and_a_shell_free_healthcheck(self):
        """Le banc neuf doit reposer sur docker-init et `exec pg_isready`."""
        proc, calls = self.run_script({}, AMEESH_PG_PASSWORD="x")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = [c for c in calls if c[0] == "run"]
        self.assertEqual(len(run), 1)
        self.assertIn("--init", run[0])
        health = run[0][run[0].index("--health-cmd") + 1]
        self.assertEqual(health, "exec pg_isready -U agent_mesh -d agent_mesh")
        self.assertTrue(health.startswith("exec "))
        self.assertNotIn("sans --init", proc.stderr)

    def test_an_existing_container_without_init_is_flagged(self):
        """Un conteneur créé avant le correctif reste exposé : on le dit, avec
        le geste de recréation, sans jamais le recréer soi-même."""
        proc, calls = self.run_script(
            {"ameesh-pg": {"running": True, "init": False}})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("sans --init", proc.stderr)
        self.assertIn("docker stop ameesh-pg", proc.stderr)
        self.assertIn("scripts/pg-up.sh", proc.stderr)
        self.assertFalse([c for c in calls if c[0] == "run"],
                         "un banc existant n'est jamais recréé par le script")

    def test_an_existing_container_with_init_is_quiet(self):
        """Le même banc recréé avec --init ne produit plus l'avertissement."""
        proc, calls = self.run_script(
            {"ameesh-pg": {"running": True, "init": True}})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("sans --init", proc.stderr)
        self.assertFalse([c for c in calls if c[0] == "run"])


if __name__ == "__main__":
    unittest.main()
