# SPDX-License-Identifier: AGPL-3.0-only
"""#518 — a DSN's password never reaches a command line, a trace or a display.

`ameesh.dsn.split_password` (also `db.split_password`, used by the psql
driver) splits a DSN as libpq reads it; `deploy/mise-a-jour/poste.sh` runs the
same file for pg_dump, psql and role-executor.sh, the password through
PGPASSWORD. One table of DSNs is checked against both, and against libpq
itself (psycopg's `conninfo_to_dict`) when psycopg is importable. Fake
`pg_dump`, `psql`, `ssh` and `systemctl` on PATH record their argv and their
PGPASSWORD; no database, no server.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from ameesh import db as db_mod
from ameesh import dsn as dsn_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "mise-a-jour", "poste.sh")

# (DSN, its password, the DSN without it); None: the DSN is kept as is
CASES = [
    ("postgresql://ameesh:Pw-url-1@mesh.invalid/ameesh", "Pw-url-1",
     "postgresql://ameesh@mesh.invalid/ameesh"),
    ("postgresql://u%40tenant:p%40ss%2Fw0rd@h.invalid:5442/mesh?sslmode=require", "p@ss/w0rd",
     "postgresql://u%40tenant@h.invalid:5442/mesh?sslmode=require"),
    ("postgresql://u@h.invalid/db?password=Pw-query-2&sslmode=require", "Pw-query-2",
     "postgresql://u@h.invalid/db?sslmode=require"),
    ("postgresql://u@h.invalid/db?password=Pw-first-3&application_name=x&pass%77ord=Pw-last-3",
     "Pw-last-3", "postgresql://u@h.invalid/db?application_name=x"),
    ("postgresql://u:Pw-ipv6-4@[::1]:5432,h2.invalid:5433/db?target_session_attrs=any",
     "Pw-ipv6-4", "postgresql://u@[::1]:5432,h2.invalid:5433/db?target_session_attrs=any"),
    ("postgresql://:Pw-nouser-5@h.invalid/db", "Pw-nouser-5", "postgresql://h.invalid/db"),
    ("postgresql://a?b:Pw-13@h.invalid/db?sslmode=require", "Pw-13",
     "postgresql://a?b@h.invalid/db?sslmode=require"),
    ("postgres://u@h.invalid/db?sslnegotiation=direct&sslmode=require", "", None),
    ("postgresql://u@h.invalid/db?passfile=/home/u/.pgpass&", "",
     "postgresql://u@h.invalid/db?passfile=/home/u/.pgpass"),
    ("dbname=db password='Pw-6 dbname=other' user=u", "Pw-6 dbname=other", "dbname=db user=u"),
    ("password='Pw two words' host=h.invalid", "Pw two words", "host=h.invalid"),
    ("host=h.invalid password = Pw-blanks-7 dbname=db", "Pw-blanks-7", "host=h.invalid dbname=db"),
    ("password=Pw-first-8 host=h.invalid password=Pw-last-8", "Pw-last-8", "host=h.invalid"),
    (r"password='Pw\'9\\x' host=h.invalid", "Pw'9\\x", "host=h.invalid"),
    (r"password=Pw\ 10 host=h.invalid", "Pw 10", "host=h.invalid"),
    ("host=h.invalid application_name='my app' sslmode=require sslnegotiation=direct "
     "password=Pw-11", "Pw-11",
     "host=h.invalid application_name='my app' sslmode=require sslnegotiation=direct"),
    ("host=h1.invalid,h2.invalid port=5432,5433 password=Pw-12 target_session_attrs=read-write",
     "Pw-12", "host=h1.invalid,h2.invalid port=5432,5433 target_session_attrs=read-write"),
    ("host=/run/postgresql dbname=ameesh user=ameesh", "", None),
    ("host=h.invalid passfile=/home/u/.pgpass", "", None),
    ("ameesh", "", None),
]
# refused, as libpq refuses them; the first one on purpose (fail closed):
# libpq would take the query's password
REFUSED = [
    "postgresql://u:Pw-a@h.invalid/db?password=Pw-b",
    "dbname=db password='Pw-unterminated",
    "host=h.invalid password",
    "postgresql://u@h.invalid/db?password",
    "postgresql://u:Pw%zz@h.invalid/db",
    "postgresql://u@h.invalid/db?a=1&&password=Pw-c",
]


def case(prefix):
    """The table's case whose DSN starts with `prefix`."""
    return next(c for c in CASES if c[0].startswith(prefix))


def expected(case):
    dsn, password, rest = case
    return password, dsn if rest is None else rest


class SplitPasswordTest(unittest.TestCase):
    def test_table(self):
        for case in CASES:
            with self.subTest(dsn=case[0]):
                self.assertEqual(db_mod.split_password(case[0]), expected(case))

    def test_refused_without_quoting_the_dsn(self):
        for dsn in REFUSED:
            with self.subTest(dsn=dsn):
                with self.assertRaises(dsn_mod.DsnError) as caught:
                    dsn_mod.split_password(dsn)
                self.assertNotIn("Pw", str(caught.exception))

    def test_libpq_reads_the_same_connection(self):
        try:
            from psycopg import errors as pg_errors
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            self.skipTest("psycopg not importable: libpq's own parser unavailable")
        for case in CASES:
            dsn = case[0]
            if not dsn.startswith(("postgresql://", "postgres://")) and "=" not in dsn:
                continue   # a bare database name is not a connection string
            with self.subTest(dsn=dsn):
                password, rest = expected(case)
                whole = conninfo_to_dict(dsn)
                self.assertEqual(whole.pop("password", ""), password)
                self.assertEqual(conninfo_to_dict(rest), whole)
        for dsn in REFUSED[1:]:
            with self.subTest(refused=dsn), self.assertRaises(pg_errors.ProgrammingError):
                conninfo_to_dict(dsn)


# one line per call: tool, PGPASSWORD, then argv, separated by \037 (a POSIX
# sh, cheap to start on a loaded host); `-At` queries answer 1 (the role
# exists); pg_dump writes its -f file; ssh drains its standard input
FAKE_TOOL = r'''#!/bin/sh
name=${0##*/}
{ printf '%s\037' "$name" "${PGPASSWORD-<unset>}" "$@"; printf '\n'; } >> "$FAKE_STATE/calls.log"
case $name in
  ssh) cat > /dev/null ;;
  pg_dump) prev=; for a; do [ "$prev" = -f ] && : > "$a"; prev=$a; done ;;
esac
case "$*" in *-At*) echo 1 ;; esac
exit 0
'''


class PosteDsnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-518-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        bin_dir = os.path.join(self.tmp, "bin")
        self.conf = os.path.join(home, ".config", "ameesh")
        self.state = os.path.join(self.tmp, "state")
        for path in (bin_dir, self.conf, self.state):
            os.makedirs(path)
        with open(os.path.join(self.conf, "bascule.env"), "w", encoding="utf-8") as fh:
            fh.write("VM=vm.invalid\n")
        for tool in ("pg_dump", "psql", "ssh", "systemctl"):
            path = os.path.join(bin_dir, tool)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(FAKE_TOOL)
            os.chmod(path, 0o755)
        self.env = dict(os.environ, HOME=home, FAKE_STATE=self.state,
                        PATH=bin_dir + os.pathsep + os.environ["PATH"])
        for name in ("PGPASSWORD", "BASCULE_ENV", "UNITES_RESTE"):
            self.env.pop(name, None)

    def configure(self, mesh, old=None):
        for name, dsn in (("config.json", mesh), ("config-ancienne-base.json", old)):
            path = os.path.join(self.conf, name)
            if dsn is None:
                if os.path.exists(path):
                    os.remove(path)
                continue
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"dsn": dsn}, fh)
        calls = os.path.join(self.state, "calls.log")
        if os.path.exists(calls):
            os.remove(calls)

    def run_step(self, step, *, trace=False):
        proc = subprocess.run(["bash"] + (["-x"] if trace else []) + [SCRIPT, step],
                              env=self.env, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = []
        path = os.path.join(self.state, "calls.log")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh.read().splitlines():
                    name, password, *argv = line.split("\037")[:-1]
                    calls.append({"tool": name, "argv": argv,
                                  "pgpassword": None if password == "<unset>" else password})
        return proc, calls

    def assert_secret_absent(self, text):
        # every password of the table, and nothing else, contains "Pw": a
        # partial leak (the end of a quoted value) is caught too
        self.assertNotIn("Pw", text)

    def test_backups_follow_the_shared_table(self):
        for case in CASES:
            if case[0] == "ameesh":
                continue
            with self.subTest(dsn=case[0]):
                password, rest = expected(case)
                self.configure(case[0])
                proc, calls = self.run_step("sauvegarder")
                dumps = [c for c in calls if c["tool"] == "pg_dump"]
                self.assertEqual(len(dumps), 1, calls)
                argv = dumps[0]["argv"]
                self.assertEqual(argv[argv.index("-d") + 1], rest)
                self.assertEqual(dumps[0]["pgpassword"], password or None)
                self.assert_secret_absent(json.dumps([c["argv"] for c in calls]))
                self.assert_secret_absent(proc.stdout + proc.stderr)

    def test_roles_of_the_old_base(self):
        mesh, old = case("postgresql://ameesh:"), case("dbname=db password=")
        self.configure(mesh[0], old[0])
        proc, calls = self.run_step("role")
        psql = [c for c in calls if c["tool"] == "psql"]
        self.assertEqual(len(psql), 4, calls)   # 2 by poste.sh, 2 by role-executor.sh
        for call in psql:
            self.assertIn("dbname=db user=u", call["argv"])
            self.assertEqual(call["pgpassword"], old[1])
        self.assertTrue(all(c["pgpassword"] is None for c in calls if c["tool"] == "ssh"))
        self.assert_secret_absent(json.dumps([c["argv"] for c in calls]))
        self.assert_secret_absent(proc.stdout + proc.stderr)

    def test_no_secret_in_a_trace_or_a_display(self):
        mesh, old = case("postgresql://u@h.invalid/db?password=Pw-first"), case(r"password='Pw\'")
        self.configure(mesh[0], old[0])
        for step in ("verifier", "sauvegarder", "role"):
            with self.subTest(step=step):
                proc, calls = self.run_step(step, trace=True)
                self.assertIn("+ ", proc.stderr)   # traced indeed
                self.assert_secret_absent(proc.stdout + proc.stderr)
                if step == "verifier":
                    self.assertIn("mesh     : %s" % expected(mesh)[1], proc.stdout)
                    self.assertIn("ancienne : %s" % expected(old)[1], proc.stdout)

    def test_invalid_dsn_stops_without_quoting_it(self):
        self.configure(REFUSED[0])
        proc = subprocess.run(["bash", SCRIPT, "sauvegarder"], env=self.env,
                              capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        self.assertNotEqual(proc.returncode, 0)
        self.assert_secret_absent(proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "calls.log")))


if __name__ == "__main__":
    unittest.main()
