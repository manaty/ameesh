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
fail = os.environ.get("FAKE_DOCKER_FAIL", "")
if (cmd == "ps" and "ps" in fail) or (
        cmd == "inspect" and "inspect-mounts" in fail and ".Mounts" in " ".join(args)) or (
        cmd == "volume" and "volume-" + args[1] in fail):
    sys.exit(1)

def source(vol):
    # .Source d'un montage : le chemin d'un bind mount, le _data d'un volume
    return vol if vol.startswith("/") else "/var/lib/docker/volumes/" + vol + "/_data"

def named():
    # les volumes montés, plus ceux que plus aucun conteneur ne monte
    extra = [v for v in os.environ.get("FAKE_DOCKER_VOLUMES", "").split(",") if v]
    return sorted({v for c in state.values() for v in c["volumes"]
                   if not v.startswith("/")} | set(extra))

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
        for vol in c["volumes"]:
            print(source(vol))
    elif ".State.Health.Status" in fmt:
        print("healthy")
elif cmd == "ps":
    # comme le démon : `volume=` compare le nom d'un volume nommé (ou la
    # destination), jamais la source d'un bind mount (un chemin absolu ici)
    vol = opt("--filter").split("=", 1)[1] if opt("--filter") else None
    for name, c in state.items():
        if not (c["running"] or "-a" in args):
            continue
        if vol is None or (vol in c["volumes"] and not vol.startswith("/")):
            print(name)
elif cmd == "volume" and args[1] == "ls":
    for vol in named():
        print(vol)
elif cmd == "volume" and args[1] == "inspect":
    if args[-1] not in named():
        sys.exit(1)
    print(source(args[-1]))
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

    def test_starting_on_a_bind_mount_already_running_is_refused(self):
        # AMEESH_PG_VOLUME=/srv/pg-data pour A et B : un bind mount, que le
        # filtre `volume=` de docker ne voit pas ; comparé par sa source
        shared = {"A": {"running": True, "volumes": ["/srv/pg-data"]},
                  "B": {"running": False, "volumes": ["/srv/pg-data"]}}
        proc, after, calls = self.run_script(shared, AMEESH_PG_NAME="B")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("/srv/pg-data", proc.stderr)
        self.assertFalse(after["B"]["running"])
        self.assertFalse([c for c in calls if c[0] == "start"])

    def test_a_failed_docker_inspect_or_ps_refuses_the_start(self):
        for fail in ("inspect-mounts", "ps"):
            with self.subTest(fail=fail):
                alone = {"B": {"running": False, "volumes": ["b-data"]}}
                proc, after, calls = self.run_script(
                    alone, AMEESH_PG_NAME="B", FAKE_DOCKER_FAIL=fail)
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("échec", proc.stderr)
                self.assertFalse(after["B"]["running"])
                self.assertFalse([c for c in calls if c[0] == "start"])


    # L159f3 (#307) : à la création, la source que prendra le conteneur est
    # comparée à celles des autres conteneurs, que le filtre `volume=` de
    # docker ne voit pas pour un bind mount

    def assert_creation_refused(self, containers, volume, needle, **env):
        proc, after, calls = self.run_script(
            containers, AMEESH_PG_NAME="B", AMEESH_PG_VOLUME=volume,
            AMEESH_PG_PASSWORD="x", **env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(needle, proc.stderr)
        self.assertNotIn("B", after)
        self.assertFalse([c for c in calls if c[0] == "run"])

    def test_creation_on_a_bind_mount_already_running_is_refused(self):
        running = {"A": {"running": True, "volumes": ["/srv/pg-data"]}}
        self.assert_creation_refused(running, "/srv/pg-data", "A")

    def test_creation_on_a_bind_mount_of_a_stopped_container_is_refused(self):
        # même arrêté, A redémarrerait sur le même répertoire ; le chemin
        # demandé n'est pas écrit comme docker l'a enregistré
        stopped = {"A": {"running": False, "volumes": ["/srv/pg-data"]}}
        self.assert_creation_refused(stopped, "/srv//pg-data/", "A")

    def test_creation_on_the_data_dir_of_a_named_volume_is_refused(self):
        running = {"A": {"running": True, "volumes": ["a-data"]}}
        self.assert_creation_refused(
            running, "/var/lib/docker/volumes/a-data/_data", "A")

    def test_creation_on_a_free_bind_mount_proceeds(self):
        others = {"A": {"running": True, "volumes": ["/srv/a"]},
                  "C": {"running": False, "volumes": ["c-data"]}}
        proc, after, _ = self.run_script(
            others, AMEESH_PG_NAME="B", AMEESH_PG_VOLUME="/srv/b",
            AMEESH_PG_PASSWORD="x")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after["B"]["volumes"], ["/srv/b"])

    def test_creation_on_an_existing_free_named_volume_proceeds(self):
        # B-data existe, aucun conteneur ne le monte plus
        others = {"A": {"running": True, "volumes": ["/srv/a"]}}
        proc, after, _ = self.run_script(
            others, AMEESH_PG_NAME="B", AMEESH_PG_PASSWORD="x",
            FAKE_DOCKER_VOLUMES="B-data")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(after["B"]["volumes"], ["B-data"])

    def test_a_failed_inspection_refuses_the_creation(self):
        # une inspection en échec (docker ps, montages d'un autre conteneur,
        # liste ou inspection des volumes) refuse sans rien créer
        others = {"A": {"running": True, "volumes": ["a-data"]}}
        for fail, volume in (("ps", "/srv/b"), ("ps", "b-data"),
                             ("inspect-mounts", "/srv/b"),
                             ("inspect-mounts", "b-data"),
                             ("volume-ls", "b-data"),
                             ("volume-inspect", "b-data")):
            with self.subTest(fail=fail, volume=volume):
                self.assert_creation_refused(
                    others, volume, "échec", FAKE_DOCKER_FAIL=fail,
                    FAKE_DOCKER_VOLUMES="b-data")


if __name__ == "__main__":
    unittest.main()
