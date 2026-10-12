# SPDX-License-Identifier: AGPL-3.0-only
"""#518 — `poste.sh sauvegarder` and `poste.sh role` keep a DSN's password off
every command line, which any process of the host reads (ps,
/proc/<pid>/cmdline): pg_dump, psql and role-executor.sh get the DSN without
it, and the password through PGPASSWORD, which libpq reads.

Fake `pg_dump`, `psql` and `ssh` on PATH log their argv and their PGPASSWORD;
no database, no server.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "mise-a-jour", "poste.sh")

MESH_PASSWORD = "mesh-s3cret"
OLD_PASSWORD = "old:p@ss/w0rd"   # percent-encoded in the URL

# argv to argv.log, PGPASSWORD to env.log. `-At` queries answer 1 (the role
# exists); pg_dump writes its -f file; ssh drains its standard input.
FAKE_TOOL = r'''#!/bin/sh
name=$(basename "$0")
echo "$name $*" >> "$FAKE_STATE/argv.log"
echo "$name ${PGPASSWORD-<unset>}" >> "$FAKE_STATE/env.log"
case $name in
  ssh) cat > /dev/null ;;
  pg_dump) prev=; for a; do [ "$prev" = -f ] && : > "$a"; prev=$a; done ;;
esac
case "$*" in *-At*) echo 1 ;; esac
exit 0
'''


class DsnArgvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-518-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        bin_dir = os.path.join(self.tmp, "bin")
        conf = os.path.join(home, ".config", "ameesh")
        self.state = os.path.join(self.tmp, "state")
        for path in (bin_dir, conf, self.state):
            os.makedirs(path)
        with open(os.path.join(conf, "bascule.env"), "w", encoding="utf-8") as fh:
            fh.write("VM=vm.invalid\n")
        with open(os.path.join(conf, "config.json"), "w", encoding="utf-8") as fh:
            fh.write('{"dsn": "postgresql://ameesh:%s@mesh.invalid/ameesh"}\n' % MESH_PASSWORD)
        with open(os.path.join(conf, "config-ancienne-base.json"), "w", encoding="utf-8") as fh:
            fh.write('{"dsn": "postgresql://owner:%s@old.invalid:5442/mesh?sslmode=disable"}\n'
                     % quote(OLD_PASSWORD, safe=""))
        for tool in ("pg_dump", "psql", "ssh"):
            path = os.path.join(bin_dir, tool)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(FAKE_TOOL)
            os.chmod(path, 0o755)
        self.env = dict(os.environ, HOME=home, FAKE_STATE=self.state,
                        PATH=bin_dir + os.pathsep + os.environ["PATH"])
        for name in ("PGPASSWORD", "BASCULE_ENV", "UNITES_RESTE"):
            self.env.pop(name, None)

    def run_step(self, step):
        proc = subprocess.run(["bash", SCRIPT, step], env=self.env, capture_output=True,
                              text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(os.path.join(self.state, "argv.log"), encoding="utf-8") as fh:
            argv = fh.read()
        with open(os.path.join(self.state, "env.log"), encoding="utf-8") as fh:
            env = fh.read().splitlines()
        return argv, env

    def assert_no_password(self, argv):
        for secret in (MESH_PASSWORD, OLD_PASSWORD, quote(OLD_PASSWORD, safe="")):
            self.assertNotIn(secret, argv)

    def test_backups_pass_the_password_through_the_environment(self):
        argv, env = self.run_step("sauvegarder")
        self.assert_no_password(argv)
        self.assertIn("pg_dump -Fc -d postgresql://ameesh@mesh.invalid/ameesh -f", argv)
        self.assertIn("pg_dump -Fc -d postgresql://owner@old.invalid:5442/mesh"
                      "?sslmode=disable -f", argv)
        dumps = [line for line in env if line.startswith("pg_dump ")]
        self.assertEqual(dumps, ["pg_dump %s" % MESH_PASSWORD, "pg_dump %s" % OLD_PASSWORD])

    def test_roles_of_the_old_base_pass_the_password_through_the_environment(self):
        argv, env = self.run_step("role")
        self.assert_no_password(argv)
        # role-executor.sh's psql calls (2) and poste.sh's own (2), all on the old base
        old = [line for line in argv.splitlines() if line.startswith("psql ")]
        self.assertEqual(len(old), 4, argv)
        for line in old:
            self.assertIn("postgresql://owner@old.invalid:5442/mesh?sslmode=disable", line)
        self.assertEqual([line for line in env if line.startswith("psql ")],
                         ["psql %s" % OLD_PASSWORD] * 4)
        # the mesh base is reached as postgres on the server: no DSN at all
        self.assertTrue(all(line == "ssh <unset>" for line in env if line.startswith("ssh ")))

    def test_a_dsn_without_password_sets_no_pgpassword(self):
        conf = os.path.join(self.env["HOME"], ".config", "ameesh")
        with open(os.path.join(conf, "config.json"), "w", encoding="utf-8") as fh:
            fh.write('{"dsn": "host=/run/postgresql dbname=ameesh user=ameesh"}\n')
        os.remove(os.path.join(conf, "config-ancienne-base.json"))
        argv, env = self.run_step("sauvegarder")
        self.assertIn("pg_dump -Fc -d host=/run/postgresql dbname=ameesh user=ameesh -f", argv)
        self.assertEqual([line for line in env if line.startswith("pg_dump ")],
                         ["pg_dump <unset>"])

    def test_keyword_dsn_password_is_split_out(self):
        conf = os.path.join(self.env["HOME"], ".config", "ameesh")
        with open(os.path.join(conf, "config.json"), "w", encoding="utf-8") as fh:
            fh.write('{"dsn": "host=mesh.invalid password=%s dbname=ameesh"}\n' % MESH_PASSWORD)
        os.remove(os.path.join(conf, "config-ancienne-base.json"))
        argv, env = self.run_step("sauvegarder")
        self.assert_no_password(argv)
        # split as db.split_password does: a double space where the password was
        self.assertIn("pg_dump -Fc -d host=mesh.invalid dbname=ameesh -f", " ".join(argv.split()))
        self.assertEqual([line for line in env if line.startswith("pg_dump ")],
                         ["pg_dump %s" % MESH_PASSWORD])


if __name__ == "__main__":
    unittest.main()
