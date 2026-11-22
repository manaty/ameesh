# SPDX-License-Identifier: AGPL-3.0-only
"""#8098 : le profil d'offload `nexlink` du guest (prévalidation au gel).

L'invité d'ameesh-agents-3 ne connaissait que `ameesh`, `default` et
`postgres` : chaque gel Nexlink sortait en `unknown profile 'nexlink'` (75,
`no-vm`). Le profil livre ce que `scripts/test-parts.py` de manaty/nexlink
attend, sans élargir les droits de l'invité :

* `node` 22 épinglé (empreinte SHA-256 vérifiée, jamais un binaire non
  contrôlé) et un `docker` qui renvoie au Podman rootless déjà utilisé par
  `job-postgres` ; son `run` place le conteneur dans la tranche du job et ses
  volumes d'image en tmpfs ;
* le ménage `after` ne retire que les conteneurs de **ce** job (étiquette
  `nexlink.ci.owner` posée par `scripts/sql-tests.sh` depuis `RUNNER_NAME`),
  jamais ceux d'un voisin, y compris par le chemin des orphelins (sans l'env
  du profil).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from ameesh.platform import offload_shell

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES = os.path.join(REPO, "deploy", "guest", "profiles")


def _fake(bindir: str, name: str, body: str) -> None:
    path = os.path.join(bindir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class NexlinkProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t8098-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.profile = offload_shell.load_profile({"profiles": PROFILES}, "nexlink")
        self.cache = os.path.join(self.tmp, "cache")
        os.makedirs(self.cache)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "podman.log")
        # chaque appel à podman : une ligne, arguments séparés par `|`
        _fake(self.bin, "podman", 'IFS="|"; echo "$*" >> "%s"\n' % self.log)
        # un téléchargement qui n'est pas le binaire épinglé
        _fake(self.bin, "curl",
              'while [ $# -gt 1 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
              'echo not-node > "$out"\n')

    def env(self, **extra) -> dict:
        env = {"PATH": self.bin + ":/usr/bin:/bin", "TMPDIR": self.tmp,
               "OFFLOAD_CACHE": self.cache, "OFFLOAD_JOB": "j42"}
        env.update(extra)
        return env

    def sh(self, script: str, env: dict, cwd: str | None = None):
        return subprocess.run(["/bin/sh", "-ec", script], env=env, cwd=cwd or self.tmp,
                              capture_output=True, text=True)

    def calls(self) -> list[list[str]]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [line.rstrip("\n").split("|") for line in fh]

    def test_setup_refuses_a_node_that_is_not_the_pinned_one(self):
        res = self.sh(self.profile["setup"], self.env())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("FAILED", res.stdout + res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.cache, "node", "bin", "node")))

    def test_docker_run_goes_to_podman_in_the_job_slice(self):
        self.sh(self.profile["setup"], self.env())
        docker = os.path.join(self.cache, "bin", "docker")
        env = self.env(OFFLOAD_SLICE="ameesh-job-j42.slice")
        subprocess.run([docker, "run", "-d", "--rm", "--name", "n", "postgres:17"],
                       env=env, check=True)
        subprocess.run([docker, "rm", "-f", "-v", "n"], env=env, check=True)
        self.assertEqual(self.calls(), [
            ["run", "--image-volume=tmpfs", "--cgroup-parent", "ameesh-job-j42.slice",
             "-d", "--rm", "--name", "n", "postgres:17"],
            ["rm", "-f", "-v", "n"],
        ])

    def test_docker_run_without_a_slice_keeps_tmpfs_volumes(self):
        self.sh(self.profile["setup"], self.env())
        subprocess.run([os.path.join(self.cache, "bin", "docker"), "run", "img"],
                       env=self.env(), check=True)
        self.assertEqual(self.calls(), [["run", "--image-volume=tmpfs", "img"]])

    def test_env_puts_node_and_the_shim_first_and_labels_the_job(self):
        env = {"OFFLOAD_CACHE": "/c", "OFFLOAD_JOB": "j42", "PATH": "/usr/bin"}
        out = {key: offload_shell._expand(value, env)
               for key, value in self.profile["env"].items()}
        self.assertEqual(out["PATH"], "/c/node/bin:/c/bin:/usr/bin")
        self.assertEqual(out["RUNNER_NAME"], "ameesh-job-j42")
        self.assertEqual(out["PG_IMAGE"], "docker.io/library/postgres:17")

    def test_after_removes_only_this_jobs_containers(self):
        res = self.sh(self.profile["after"], self.env())
        self.assertEqual(res.returncode, 0)
        self.assertEqual(self.calls(),
                         [["ps", "-aq", "--filter", "label=nexlink.ci.owner=ameesh-job-j42"]])

    def test_before_installs_each_package_with_a_lockfile(self):
        work = os.path.join(self.tmp, "work")
        for rel in ("backend", "mobile", "shared/compute", "shared/docs", "assist"):
            os.makedirs(os.path.join(work, rel))
        for rel in ("backend", "mobile", "shared/compute", "assist"):
            open(os.path.join(work, rel, "package-lock.json"), "w").close()
        _fake(self.bin, "npm", 'echo "$(basename "$PWD") $*" >> "%s"\n' % self.log)
        res = self.sh(self.profile["before"], self.env(), cwd=work)
        self.assertEqual(res.returncode, 0, res.stderr)
        # le stdout du hook est lu en K=V : rien de npm n'y passe
        self.assertEqual(res.stdout, "")
        with open(self.log, encoding="utf-8") as fh:
            done = [line.split()[0] for line in fh]
        self.assertEqual(done, ["backend", "mobile", "compute"])


if __name__ == "__main__":
    unittest.main()
