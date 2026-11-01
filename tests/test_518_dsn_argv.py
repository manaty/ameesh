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
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import dsn as dsn_mod

try:  # the private backup channel (L232b) splits credentials through psycopg
    import psycopg
except ImportError:  # the psql-only parts: the private channel cannot run
    psycopg = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "mise-a-jour", "poste.sh")
DSN_PY = os.path.join(ROOT, "src", "ameesh", "dsn.py")

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
    ("postgresql://:Pw-nouser-5@h.invalid/db", "Pw-nouser-5", "postgresql://@h.invalid/db"),
    ("postgresql://a?b:Pw-13@h.invalid/db?sslmode=require", "Pw-13",
     "postgresql://a?b@h.invalid/db?sslmode=require"),
    ("postgresql://u@h.invalid/db?password =Pw-key-14&sslmode=require", "Pw-key-14",
     "postgresql://u@h.invalid/db?sslmode=require"),
    ("postgresql://u@h.invalid/db? password=Pw-key-15", "Pw-key-15",
     "postgresql://u@h.invalid/db"),
    ("postgresql://u: Pw-trim-16 @h.invalid/db", "Pw-trim-16", "postgresql://u@h.invalid/db"),
    ("postgresql://@u:not-a-password@h.invalid/db", "", None),
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
    # a final newline may be data: an encoded or quoted one in a password, an
    # escaped one in a value, a URI's database name
    ("postgresql://u:Pw-lf-18%0A@h.invalid/db", "Pw-lf-18\n", "postgresql://u@h.invalid/db"),
    ("password='Pw-lf-19\n' host=h.invalid", "Pw-lf-19\n", "host=h.invalid"),
    ("password=Pw-20 dbname=production\\\n", "Pw-20", "dbname='production\n'"),
    ("postgresql://u:Pw-21@h.invalid/production\n", "Pw-21",
     "postgresql://u@h.invalid/production\n"),
    # an explicit empty password still overrides PGPASSWORD and a service: it stays
    ("host=h.invalid password='' dbname=db", "", "host=h.invalid dbname=db password=''"),
    ("postgresql://u@h.invalid/db?password=&sslmode=require", "",
     "postgresql://u@h.invalid/db?sslmode=require&password="),
    ("service=prod password=''", "", None),
    ("service=prod dbname=db", "", None),
    ("ameesh", "", None),
]
# refused on purpose, though libpq's parser takes them (fail closed): libpq
# would take the query's password; next to a service, PGPASSWORD would lose
# to the service file's password; libpq stops reading at a NUL, the split
# would not
DELIBERATE = [
    "postgresql://u:Pw-a@h.invalid/db?password=Pw-b",
    "service=prod password=Pw-svc-22",
    "postgresql://u:Pw-svc-23@h.invalid/db?service=prod",
    "host=h.invalid dbname=production password=Pw-nul\x00 dbname=other password=Pw-nul-2",
]
# refused, as libpq refuses them
REFUSED = [
    "dbname=db password='Pw-unterminated",
    "host=h.invalid password",
    "postgresql://u@h.invalid/db?password",
    "postgresql://u:Pw%zz@h.invalid/db",
    "postgresql://u@h.invalid/db?a=1&&password=Pw-c",
    "postgresql://u:Pw inner@h.invalid/db",
    "postgresql://u@h.invalid/db?password=Pw inner",
]


def case(prefix):
    """The table's case whose DSN starts with `prefix`."""
    return next(c for c in CASES if c[0].startswith(prefix))


def blanks_in_uri(dsn):
    """libpq 16 to 18 drop the blanks around a URI token and refuse one
    inside it, as ameesh.dsn does; an older libpq may keep them."""
    return dsn.startswith(("postgresql://", "postgres://")) and " " in dsn


def without_empty_password(parameters):
    """libpq's parameters minus the empty password the private channel drops.

    An explicit empty password still overrides PGPASSWORD or a service: the
    private channel keeps that override in its own private pgpass file
    (L232b), so the DSN handed to the tool no longer carries the key.
    """
    return {k: v for k, v in parameters.items() if not (k == "password" and v == "")}


def expected(case):
    dsn, password, rest = case
    return password, dsn if rest is None else rest


class SplitPasswordTest(unittest.TestCase):
    def test_table(self):
        for case in CASES:
            with self.subTest(dsn=case[0]):
                self.assertEqual(db_mod.split_password(case[0], {}), expected(case))

    def test_refused_without_quoting_the_dsn(self):
        for dsn in DELIBERATE + REFUSED:
            with self.subTest(dsn=dsn):
                with self.assertRaises(dsn_mod.DsnError) as caught:
                    dsn_mod.split_password(dsn, {})
                self.assertNotIn("Pw", str(caught.exception))

    def test_a_service_from_the_environment(self):
        for case in CASES:
            with self.subTest(dsn=case[0]):
                if case[1]:
                    with self.assertRaises(dsn_mod.DsnError) as caught:
                        dsn_mod.split_password(case[0], {"PGSERVICE": "prod"})
                    self.assertNotIn("Pw", str(caught.exception))
                else:
                    self.assertEqual(dsn_mod.split_password(case[0], {"PGSERVICE": "prod"}),
                                     expected(case))

    def test_libpq_reads_the_same_connection(self):
        try:
            from psycopg import errors as pg_errors, pq
            from psycopg.conninfo import conninfo_to_dict
        except ImportError:
            self.skipTest("psycopg not importable: libpq's own parser unavailable")
        for case in CASES:
            dsn = case[0]
            if not dsn.startswith(("postgresql://", "postgres://")) and "=" not in dsn:
                continue   # a bare database name is not a connection string
            if blanks_in_uri(dsn) and pq.version() < 160000:
                continue
            with self.subTest(dsn=dsn):
                password, rest = expected(case)
                whole = conninfo_to_dict(dsn)
                self.assertEqual(whole.get("password", ""), password)
                if password:
                    del whole["password"]
                self.assertEqual(conninfo_to_dict(rest), whole)
        for dsn in REFUSED:
            if blanks_in_uri(dsn) and pq.version() < 160000:
                continue
            with self.subTest(refused=dsn), self.assertRaises(pg_errors.ProgrammingError):
                conninfo_to_dict(dsn)


class MaskDsnTest(unittest.TestCase):
    """L537 : `mask_dsn` rend le DSN de `split_password`, jamais un mot de passe.

    Le marqueur dit qu'il y en avait un ; un DSN refusé s'affiche
    `<invalid DSN>`, jamais cité."""

    def test_table(self):
        for dsn, password, rest in CASES:
            with self.subTest(dsn=dsn):
                without = dsn if rest is None else rest
                shown = config_mod.mask_dsn(dsn)
                if not password:
                    self.assertEqual(shown, without)
                    continue
                self.assertNotIn(password, shown)
                self.assertIn(config_mod.MASKED_PASSWORD, shown)
                self.assertTrue(shown.startswith(without), shown)

    def test_refused_shows_invalid_without_quoting(self):
        for dsn in DELIBERATE + REFUSED:
            with self.subTest(dsn=dsn):
                shown = config_mod.mask_dsn(dsn)
                self.assertEqual(shown, "<invalid DSN>")
                self.assertNotIn("Pw", shown)

    def test_a_password_alone_is_the_marker(self):
        self.assertEqual(config_mod.mask_dsn("password='two words'"),
                         config_mod.MASKED_PASSWORD)
        self.assertEqual(config_mod.mask_dsn("postgresql://u@h.invalid/db"),
                         "postgresql://u@h.invalid/db")


class CommandLineTest(unittest.TestCase):
    """`python3 dsn.py password|dsn`, as poste.sh runs it: bytes in, bytes out."""

    def run_dsn(self, part, dsn):
        env = {k: v for k, v in os.environ.items() if k != "PGSERVICE"}
        return subprocess.run([sys.executable, DSN_PY, part], input=dsn.encode(),
                              capture_output=True, env=env, timeout=120)

    def test_table(self):
        for case in CASES:
            for part, want in zip(("password", "dsn"), expected(case)):
                with self.subTest(dsn=case[0], part=part):
                    proc = self.run_dsn(part, case[0])
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.decode(), want)

    def test_refused_without_quoting_the_dsn(self):
        for dsn in DELIBERATE + REFUSED[:1]:
            with self.subTest(dsn=dsn):
                proc = self.run_dsn("dsn", dsn)
                self.assertEqual((proc.returncode, proc.stdout), (1, b""))
                self.assertNotIn(b"Pw", proc.stderr)


# one record per call, ended by \036: tool, PGPASSWORD, PGPASSFILE, then argv,
# separated by \037, so that newlines survive (a POSIX sh, cheap to start on a
# loaded host); `-At` queries answer 1 (the role exists); pg_dump writes a
# non-empty -f/--file target; pg_restore lists nothing; ssh drains its input;
# `--version` (#5189 probe of the clients the same PATH runs) answers a major
# 17 and is not recorded: the calls measured are the ones that carry a DSN
FAKE_TOOL = r'''#!/bin/sh
name=${0##*/}
[ "$*" = --version ] && { echo "$name (PostgreSQL) 17.0"; exit 0; }
{ printf '%s\037' "$name" "${PGPASSWORD-<unset>}" "${PGPASSFILE-<unset>}" "$@"; printf '\036'; } >> "$FAKE_STATE/calls.log"
case $name in
  ssh)
    case "$*" in
      *"systemctl start ameesh-sauvegarde.service"*)
        cat > /dev/null
        d=$FAKE_STATE/server.dump
        [ -f "$d" ] || printf 'server dump\n' > "$d"
        printf '%s\n%s\n%s\n' \
          "/home/ameesh/sauvegardes/ameesh-test-20261017-150000.dump" \
          "$(wc -c < "$d")" \
          "$(sha256sum "$d" | cut -d' ' -f1)"
        ;;
      *"gzip -1 -c"*)
        cat > /dev/null
        gzip -1 -c "$FAKE_STATE/server.dump"
        ;;
      *) cat > /dev/null ;;
    esac ;;
  pg_restore) : ;;
  pg_dump) prev=; for a; do
      if [ "$prev" = -f ] || [ "$prev" = --file ]; then printf 'dump' > "$a"; fi
      prev=$a
    done ;;
esac
case "$*" in *-At*) echo 1 ;; esac
exit 0
'''

LAUNCHER = """import importlib.util
import sys

script = sys.argv[1]
spec = importlib.util.spec_from_file_location("sauvegarder_prive", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.source_server_major = lambda parameters: 17
sys.argv = sys.argv[1:]
raise SystemExit(module.main())
"""


class PosteDsnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-518-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        self.home = home
        bin_dir = os.path.join(self.tmp, "bin")
        self.conf = os.path.join(home, ".config", "ameesh")
        self.state = os.path.join(self.tmp, "state")
        for path in (bin_dir, self.conf, self.state):
            os.makedirs(path)
        with open(os.path.join(self.conf, "bascule.env"), "w", encoding="utf-8") as fh:
            fh.write("VM=vm.invalid\n")
        for tool in ("pg_dump", "pg_restore", "psql", "ssh", "systemctl"):
            path = os.path.join(bin_dir, tool)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(FAKE_TOOL)
            os.chmod(path, 0o755)
        # `sauvegarder` runs the private backup channel through the venv's
        # python, as a real poste does (~/.local/share/ameesh/venv): a shim to
        # this interpreter, so the channel really runs its pg_dump/pg_restore.
        # #5189 : the channel reads the source server's major before pg_dump;
        # no DSN of the table reaches a server, so the launcher fixes that one
        # major (17) and runs the repository helper itself.
        venv_bin = os.path.join(home, ".local", "share", "ameesh", "venv", "bin")
        os.makedirs(venv_bin)
        launcher = os.path.join(self.tmp, "launcher.py")
        with open(launcher, "w", encoding="utf-8") as fh:
            fh.write(LAUNCHER)
        python = os.path.join(venv_bin, "python")
        with open(python, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\ncase $1 in */sauvegarder-prive.py) exec %s %s \"$@\" ;; esac\n"
                     "exec %s \"$@\"\n" % (shlex.quote(sys.executable), shlex.quote(launcher),
                                           shlex.quote(sys.executable)))
        os.chmod(python, 0o755)
        self.env = dict(os.environ, HOME=home, FAKE_STATE=self.state,
                        PATH=bin_dir + os.pathsep + os.environ["PATH"])
        # the host's credential channels are not what this test measures: a
        # bench's PGPASSFILE (scripts/test.sh, #2170) would otherwise reach
        # the fake tools even when the DSN itself carries no password
        for name in ("PGPASSWORD", "PGPASSFILE", "PGSERVICE", "BASCULE_ENV",
                     "UNITES_RESTE", "SAUVEGARDES_VM", "RECUPERATION_TIMEOUT"):
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
        # the private channel refuses an existing destination: a fresh run
        # starts without this date's backups.
        shutil.rmtree(os.path.join(self.home, "backups"), ignore_errors=True)

    def run_step(self, step, *, trace=False):
        proc = subprocess.run(["bash"] + (["-x"] if trace else []) + [SCRIPT, step],
                              env=self.env, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc, self.calls()

    def calls(self):
        calls = []
        path = os.path.join(self.state, "calls.log")
        if os.path.exists(path):
            with open(path, encoding="utf-8", newline="") as fh:
                for record in fh.read().split("\036")[:-1]:
                    name, password, passfile, *argv = record.split("\037")[:-1]
                    calls.append({"tool": name, "argv": argv,
                                  "pgpassword": None if password == "<unset>" else password,
                                  "pgpassfile": None if passfile == "<unset>" else passfile})
        return calls

    def assert_secret_absent(self, text):
        # every password of the table, and nothing else, contains "Pw": a
        # partial leak (the end of a quoted value) is caught too
        self.assertNotIn("Pw", text)

    def public_dsn(self, calls):
        """The private channel's pg_dump call and the DSN it was handed."""
        dumps = [c for c in calls if c["tool"] == "pg_dump"]
        self.assertEqual(len(dumps), 1, calls)
        argv = dumps[0]["argv"]
        return dumps[0], argv[argv.index("--dbname") + 1]

    @unittest.skipUnless(psycopg, "the private backup channel needs psycopg")
    def test_mesh_dump_is_server_side_and_hands_no_dsn_to_a_tool(self):
        # #2261: the mesh dump is produced by ameesh-sauvegarde on the VM; the
        # poste must not run pg_dump with the mesh DSN at all.
        mesh = case("postgresql://ameesh:")
        self.configure(mesh[0])
        proc, calls = self.run_step("sauvegarder")
        self.assertFalse([c for c in calls if c["tool"] == "pg_dump"], calls)
        self.assert_secret_absent(json.dumps([c["argv"] for c in calls]))
        self.assert_secret_absent(proc.stdout + proc.stderr)

    @unittest.skipUnless(psycopg, "the private backup channel needs psycopg")
    def test_backups_follow_the_shared_table(self):
        from psycopg import pq
        from psycopg.conninfo import conninfo_to_dict
        mesh = case("postgresql://ameesh:")
        for entry in CASES:
            if entry[0] == "ameesh":
                continue
            if blanks_in_uri(entry[0]) and pq.version() < 160000:
                continue
            with self.subTest(dsn=entry[0]):
                password, rest = expected(entry)
                # the case is the old database: the mesh one is now dumped on
                # the VM, so the private channel's pg_dump is the old base's.
                self.configure(mesh[0], entry[0])
                if any(c in password for c in "\r\n\x00"):
                    # a private pgpass file cannot carry such a password: the
                    # backup stops instead of falling back to PGPASSWORD (#518)
                    self.assert_stops(self.env, database_tools_only=True)
                    continue
                carries = "password" in conninfo_to_dict(entry[0])
                proc, calls = self.run_step("sauvegarder")
                dump, public = self.public_dsn(calls)
                # the channel hands pg_dump the same connection without its
                # password; the credential rides a private pgpass file instead
                self.assertEqual(without_empty_password(conninfo_to_dict(public)),
                                 without_empty_password(conninfo_to_dict(rest)))
                self.assertIsNone(dump["pgpassword"])
                self.assertEqual(dump["pgpassfile"] is not None, carries)
                self.assert_secret_absent(json.dumps([c["argv"] for c in calls]))
                self.assert_secret_absent(proc.stdout + proc.stderr)

    @unittest.skipUnless(psycopg, "the private backup channel needs psycopg")
    def test_trailing_newlines_reach_the_old_base_tools(self):
        from psycopg import pq
        from psycopg.conninfo import conninfo_to_dict
        mesh = case("postgresql://ameesh:")
        for entry in (c for c in CASES if c[0].endswith("\n") or c[1].endswith("\n")):
            if blanks_in_uri(entry[0]) and pq.version() < 160000:
                continue
            password, rest = expected(entry)
            with self.subTest(dsn=entry[0]):
                self.configure(mesh[0], entry[0])
                if any(c in password for c in "\r\n\x00"):
                    # a private pgpass file cannot carry it: the backup stops
                    # instead of falling back to PGPASSWORD (#518, fail closed)
                    self.assert_stops(self.env, database_tools_only=True)
                    continue
                proc, calls = self.run_step("sauvegarder")
                dump, public = self.public_dsn(calls)
                self.assertEqual(without_empty_password(conninfo_to_dict(public)),
                                 without_empty_password(conninfo_to_dict(rest)))
                self.assertIsNone(dump["pgpassword"])

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
            if step == "sauvegarder" and not psycopg:
                continue   # the private backup channel cannot run without psycopg
            with self.subTest(step=step):
                proc, calls = self.run_step(step, trace=True)
                self.assertIn("+ ", proc.stderr)   # traced indeed
                self.assert_secret_absent(proc.stdout + proc.stderr)
                if step == "verifier":
                    self.assertIn("mesh     : %s" % expected(mesh)[1], proc.stdout)
                    self.assertIn("ancienne : %s" % expected(old)[1], proc.stdout)

    def assert_stops(self, env, *, database_tools_only=False):
        proc = subprocess.run(["bash", SCRIPT, "sauvegarder"], env=env,
                              capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        self.assertNotEqual(proc.returncode, 0)
        self.assert_secret_absent(proc.stdout + proc.stderr)
        calls = self.calls()
        if database_tools_only:
            # the server-side mesh dump needs no database tool; the refused
            # old-base channel must not start pg_dump or psql at all.
            self.assertFalse([c for c in calls if c["tool"] in ("pg_dump", "psql")], calls)
        else:
            self.assertFalse(calls)

    def test_invalid_dsn_stops_without_quoting_it(self):
        for dsn in DELIBERATE[:2] + DELIBERATE[3:]:
            with self.subTest(dsn=dsn):
                self.configure(dsn)
                self.assert_stops(self.env)

    def fake_venv(self, home):
        """A venv python that records its calls: migrer's ameesh_de runs it."""
        bin_dir = os.path.join(home, ".local", "share", "ameesh", "venv", "bin")
        os.makedirs(bin_dir, exist_ok=True)   # setUp already shims this python
        path = os.path.join(bin_dir, "python")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FAKE_TOOL)
        os.chmod(path, 0o755)

    def test_migrer_stops_on_a_refused_dsn(self):
        home = self.env["HOME"]
        self.fake_venv(home)
        # a refused DSN, then a valid one with dsn.py absent (a copy of the
        # repository that holds poste.sh only)
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "deploy", "mise-a-jour"))
        shutil.copy(SCRIPT, os.path.join(repo, "deploy", "mise-a-jour", "poste.sh"))
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t.invalid"]
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "poste.sh"]):
            subprocess.run(git + args, check=True, capture_output=True, env=self.env)
        for dsn, script in ((DELIBERATE[1], SCRIPT),
                            ("postgresql://u:Pw-mig-24@h.invalid/production",
                             os.path.join(repo, "deploy", "mise-a-jour", "poste.sh"))):
            with self.subTest(dsn=dsn, script=script):
                self.configure(dsn)
                proc = subprocess.run(["bash", script, "migrer"], env=self.env,
                                      capture_output=True, text=True, timeout=300,
                                      stdin=subprocess.DEVNULL)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assert_secret_absent(proc.stdout + proc.stderr)
                self.assertFalse(os.path.exists(os.path.join(self.state, "calls.log")),
                                 "a migration ran")

    def test_a_service_in_the_environment_stops_a_password(self):
        self.configure(case("postgresql://ameesh:")[0])
        self.assert_stops(dict(self.env, PGSERVICE="prod"))


if __name__ == "__main__":
    unittest.main()
