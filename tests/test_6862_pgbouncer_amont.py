# SPDX-License-Identifier: AGPL-3.0-only
"""#6862 (EP8.5) : l'amont du PgBouncer local suit le primaire.

PgBouncer n'a pas `target_session_attrs` : une liste `host=a,b,c` est
parcourue en tourniquet, secours compris. `deploy/pgbouncer-host.py amont`
résout le primaire par le DSN multi-hôtes (libpq, `read-write`), réécrit la
ligne `[databases]` seulement si elle diffère, puis `RELOAD`. Les témoins
unitaires utilisent des faux ; le dernier fait la bascule réelle : une paire
PostgreSQL 17 locale, le vrai binaire PgBouncer, primaire tué, secours promu,
et une écriture à travers le pool qui atterrit sur le nouveau primaire.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
PG_BIN = pathlib.Path(os.environ.get("AMEESH_TEST_PG_BIN", "/usr/lib/postgresql/17/bin"))
PGBOUNCER = os.environ.get("AMEESH_TEST_PGBOUNCER") or shutil.which("pgbouncer") or \
    os.path.expanduser("~/.local/bin/pgbouncer")


def _recipe():
    spec = importlib.util.spec_from_file_location(
        "pgbouncer_host_6862", REPO / "deploy/pgbouncer-host.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INI = """[databases]
ameesh = host=10.78.0.10 port=5432 dbname=ameesh

[pgbouncer]
listen_port = 6432
"""


class FakeConn:
    def __init__(self, host, port="5432", dbname="ameesh", recovery=False):
        self.info = types.SimpleNamespace(host=host, port=int(port), dbname=dbname)
        self.recovery = recovery
        self.dsn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _sql):
        return types.SimpleNamespace(fetchone=lambda: (self.recovery,))


class AmontUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("psycopg absent")
        cls.recipe = _recipe()

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6862-amont-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.settings = self.home / ".config/pgbouncer"
        self.settings.mkdir(parents=True)
        self.ini = self.settings / "pgbouncer.ini"
        self.ini.write_text(INI)
        self.ini.chmod(0o600)
        self.dsn = self.settings / "amont.dsn"
        self.dsn.write_text("host=10.78.0.11,10.78.0.12,10.78.0.10 dbname=ameesh "
                            "user=ameesh sslmode=require\n")
        self.dsn.chmod(0o600)
        self.state = self.home / ".local/state/pgbouncer"
        self.state.mkdir(parents=True)
        self.reloads = []

    def connect_to(self, conn):
        def connect(dsn, **_kw):
            conn.dsn = dsn
            return conn
        return connect

    def run_amont(self, conn, **kw):
        return self.recipe.amont(self.home, self.state, connect=self.connect_to(conn),
                                 reload=self.reloads.append, **kw)

    def test_a_failover_rewrites_only_the_upstream_and_reloads(self):
        conn = FakeConn("10.78.0.11")
        self.assertEqual(self.run_amont(conn), "changed")
        self.assertIn("target_session_attrs=read-write", conn.dsn)
        text = self.ini.read_text()
        self.assertIn("ameesh = host=10.78.0.11 port=5432 dbname=ameesh", text)
        self.assertEqual(text.replace("10.78.0.11", "10.78.0.10"), INI)
        self.assertEqual(self.reloads, [self.state])
        backup = self.settings / "pgbouncer.ini.avant-amont"
        self.assertEqual(backup.read_text(), INI)
        for path in (self.ini, backup):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_same_primary_writes_nothing(self):
        before = self.ini.stat().st_mtime_ns
        self.assertEqual(self.run_amont(FakeConn("10.78.0.10")), "unchanged")
        self.assertEqual(self.ini.stat().st_mtime_ns, before)
        self.assertEqual(self.reloads, [])

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_amont(FakeConn("10.78.0.12"), dry_run=True), "changed")
        self.assertEqual(self.ini.read_text(), INI)
        self.assertEqual(self.reloads, [])

    def test_refusals_leave_the_pool_untouched(self):
        cases = {
            "standby": (FakeConn("10.78.0.12", recovery=True), None),
            "foreign host": (FakeConn("10.99.0.1"), None),
            "odd identifier": (FakeConn("10.78.0.11", dbname="a b"), None),
            "single host": (FakeConn("10.78.0.11"),
                            "host=10.78.0.11 dbname=ameesh user=ameesh\n"),
            "password": (FakeConn("10.78.0.11"),
                         "host=10.78.0.11,10.78.0.12 dbname=ameesh password=s3cret\n"),
        }
        for name, (conn, dsn) in cases.items():
            with self.subTest(name=name):
                if dsn:
                    self.dsn.write_text(dsn)
                with self.assertRaises(ValueError):
                    self.run_amont(conn)
                self.assertEqual(self.ini.read_text(), INI)
                self.assertEqual(self.reloads, [])
                self.setUp()

    def test_a_readable_dsn_file_is_refused(self):
        self.dsn.chmod(0o644)
        with self.assertRaises(ValueError):
            self.run_amont(FakeConn("10.78.0.11"))

    def test_pool_config_closes_servers_fast(self):
        text = self.recipe.pool_ini("ameesh", "10.78.0.10", "5432", socket_dir="/s",
                                    auth_file="/a", admin_user="ameesh", tls="require",
                                    host="ameesh-agents-1")
        self.assertIn("server_fast_close = 1", text)
        self.assertEqual(self.recipe.database_line(text, "ameesh")[1:],
                         ("10.78.0.10", "5432"))

    def test_actions_are_behind_the_host_user_guard(self):
        for action in ("amont", "install-amont"):
            with self.subTest(action=action), \
                    mock.patch("sys.argv", ["recipe", action]), \
                    mock.patch.object(self.recipe.socket, "gethostname",
                                      return_value="wrong-host"), \
                    mock.patch.object(self.recipe, "amont") as follow, \
                    mock.patch.object(self.recipe, "install_amont") as install:
                with self.assertRaises(ValueError):
                    self.recipe.main()
                follow.assert_not_called()
                install.assert_not_called()

    def test_a_failed_reload_puts_the_old_upstream_back(self):
        # Relecture impl-08 : sinon le passage suivant trouve la nouvelle
        # ligne, ne recharge plus, et le pool reste sur l'ancien amont.
        def refuse(_state):
            raise OSError("console down")
        with self.assertRaises(OSError):
            self.recipe.amont(self.home, self.state, connect=self.connect_to(
                FakeConn("10.78.0.11")), reload=refuse)
        self.assertEqual(self.ini.read_text(), INI)
        self.assertEqual(stat.S_IMODE(self.ini.stat().st_mode), 0o600)
        self.assertEqual(self.run_amont(FakeConn("10.78.0.11")), "changed")
        self.assertEqual(self.reloads, [self.state])

    def test_timer_command_keeps_the_venv_and_a_stable_script(self):
        # Relecture impl-08 : realpath(sys.executable) menait au python
        # système, sans psycopg ; le venv versionné et l'instantané de source
        # disparaissent aux mises à jour suivantes.
        system = self.tmp / "usr/bin/python3.12"
        system.parent.mkdir(parents=True)
        system.write_text("")
        versioned = self.home / ".local/share/ameesh/venv-abc1234/bin/python"
        versioned.parent.mkdir(parents=True)
        versioned.symlink_to(system)
        snapshot = self.home / ".local/share/ameesh/src-abc1234/deploy/pgbouncer-host.py"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text("")
        python, script = self.recipe.amont_command(self.home, str(versioned), str(snapshot))
        self.assertEqual((python, script), (str(versioned), str(snapshot)))
        launcher = self.home / ".local/share/ameesh/venv/bin/python"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        stable = self.home / ".local/share/ameesh/src/deploy/pgbouncer-host.py"
        stable.parent.mkdir(parents=True)
        stable.write_text("")
        self.assertEqual(self.recipe.amont_command(self.home, str(versioned), str(snapshot)),
                         (str(launcher), str(stable)))

    def test_install_through_main_checks_the_unit_command_first(self):
        # `main` refuses a home outside [A-Za-z0-9_./-] ; TMPDIR may hold `~`.
        parent = next((d for d in (tempfile.gettempdir(), os.path.expanduser("~/.cache"))
                       if re.fullmatch(r"/[A-Za-z0-9_./-]+", d) and os.path.isdir(d)), None)
        if parent is None:
            self.skipTest("no temporary directory with a plain path")
        root = pathlib.Path(tempfile.mkdtemp(prefix="t6862-main-", dir=parent))
        self.addCleanup(shutil.rmtree, root, True)
        self.home = root / "home"
        shutil.copytree(self.tmp / "home", self.home)
        (self.home / ".local/state/pgbouncer").chmod(0o700)
        versioned = self.home / ".local/share/ameesh/venv-abc1234/bin/python"
        versioned.parent.mkdir(parents=True)
        versioned.symlink_to(self.tmp / "system-python")
        with mock.patch("sys.argv", ["recipe", "install-amont"]), \
                mock.patch("sys.executable", str(versioned)), \
                mock.patch.object(self.recipe, "approved_home", return_value=self.home), \
                mock.patch.object(self.recipe, "run") as run:
            self.recipe.main()
        service = (self.home / ".config/systemd/user/ameesh-pgbouncer-amont.service").read_text()
        script = os.path.abspath(self.recipe.__file__)
        self.assertIn("ExecStart=%s %s amont\n" % (versioned, script), service)
        self.assertNotIn(os.path.realpath(versioned), service)
        self.assertEqual(run.call_args_list[0].args[0],
                         [str(versioned), script, "amont", "--dry-run"])

    def test_a_failing_unit_command_installs_nothing(self):
        with mock.patch.object(self.recipe, "run",
                               side_effect=subprocess.CalledProcessError(1, "x")):
            with self.assertRaises(subprocess.CalledProcessError):
                self.recipe.install_amont(self.home, "/usr/bin/python3", "/src/x.py")
        self.assertFalse((self.home / ".config/systemd/user").exists())

    def test_install_writes_the_timer_once(self):
        with mock.patch.object(self.recipe, "run") as run:
            self.recipe.install_amont(self.home, "/venv/bin/python3", "/src/pgbouncer-host.py")
            units = self.home / ".config/systemd/user"
            service = (units / "ameesh-pgbouncer-amont.service").read_text()
            self.assertIn("ExecStart=/venv/bin/python3 /src/pgbouncer-host.py amont", service)
            self.assertIn("OnUnitActiveSec=15s",
                          (units / "ameesh-pgbouncer-amont.timer").read_text())
            self.assertIn(["systemctl", "--user", "enable", "--now",
                           "ameesh-pgbouncer-amont.timer"], [c.args[0] for c in run.call_args_list])
            with self.assertRaises(ValueError):
                self.recipe.install_amont(self.home, "/venv/bin/python3", "/x")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env():
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("PG")]:
        env.pop(key)
    return env


@unittest.skipUnless(all((PG_BIN / b).exists() for b in ("initdb", "pg_ctl", "pg_basebackup")),
                     "binaires serveur PostgreSQL 17 absents")
@unittest.skipUnless(os.path.exists(PGBOUNCER), "binaire pgbouncer absent")
@unittest.skipIf(os.geteuid() == 0, "initdb refuse root")
class AmontFailoverTest(unittest.TestCase):
    """Primaire tué, secours promu : `amont` repointe le vrai PgBouncer."""

    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg absent")
        self.recipe = _recipe()
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6862-pg-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Chemin de socket Unix borné à 107 octets : un dossier court.
        self.sock = pathlib.Path(tempfile.mkdtemp(prefix="t6862s-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.sock, True)
        self.a, self.b = self.tmp / "a", self.tmp / "b"
        self.pa, self.pb = _free_port(), _free_port()
        try:
            self._pair()
            self._pool()
        except subprocess.CalledProcessError as exc:
            self.skipTest("banc local impossible : %s %s" % (
                exc.cmd[0], (exc.stderr or b"").decode(errors="replace")[-300:]))

    def _pg_ctl(self, data, port, action, *extra, check=True):
        return subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-o",
                               "-p %d -k %s" % (port, self.sock), "-l", str(data) + ".log",
                               "-w", action, *extra], check=check, capture_output=True,
                              env=_env())

    def _pair(self):
        subprocess.run([str(PG_BIN / "initdb"), "-D", str(self.a), "-U", "postgres",
                        "--auth=trust", "-E", "UTF8", "--locale=C"],
                       check=True, capture_output=True, env=_env())
        with open(self.a / "postgresql.conf", "a", encoding="utf-8") as fh:
            fh.write("listen_addresses = '127.0.0.1'\nwal_level = replica\n"
                     "max_wal_senders = 5\n")
        with open(self.a / "pg_hba.conf", "a", encoding="utf-8") as fh:
            fh.write("host replication postgres 127.0.0.1/32 trust\n")
        self._pg_ctl(self.a, self.pa, "start")
        self.addCleanup(self._pg_ctl, self.a, self.pa, "stop", "-m", "immediate", check=False)
        subprocess.run([str(PG_BIN / "pg_basebackup"), "-D", str(self.b), "-h", "127.0.0.1",
                        "-p", str(self.pa), "-U", "postgres", "-X", "stream", "-R"],
                       check=True, capture_output=True, env=_env())
        self._pg_ctl(self.b, self.pb, "start")
        self.addCleanup(self._pg_ctl, self.b, self.pb, "stop", "-m", "immediate", check=False)
        import psycopg
        with psycopg.connect("host=127.0.0.1 port=%d user=postgres dbname=postgres" % self.pa,
                             autocommit=True) as conn:
            conn.execute("CREATE TABLE t (n int)")

    def _pool(self):
        self.home = self.tmp / "home"
        settings = self.home / ".config/pgbouncer"
        settings.mkdir(parents=True)
        self.state = self.home / ".local/state/pgbouncer"
        self.state.mkdir(parents=True)
        (settings / "userlist.txt").write_text('"postgres" ""\n')
        ini = settings / "pgbouncer.ini"
        text = self.recipe.pool_ini("postgres", "127.0.0.1", str(self.pa),
                                    socket_dir=self.sock, auth_file=settings / "userlist.txt",
                                    admin_user="postgres", tls="disable", host="banc")
        ini.write_text(text)
        ini.chmod(0o600)
        (settings / "amont.dsn").write_text(
            "host=127.0.0.1,127.0.0.1 port=%d,%d user=postgres dbname=postgres "
            "connect_timeout=2\n" % (self.pa, self.pb))
        (settings / "amont.dsn").chmod(0o600)
        (self.state / "socket.dsn").write_text(
            "host=%s port=6432 user=postgres dbname=postgres" % self.sock)
        log = open(self.tmp / "pgbouncer.log", "wb")
        self.addCleanup(log.close)
        self.bouncer = subprocess.Popen([PGBOUNCER, str(ini)], stdout=log, stderr=log)
        self.addCleanup(self._stop_bouncer)
        for _ in range(100):
            if (self.sock / ".s.PGSQL.6432").exists():
                return
            time.sleep(0.05)
        self.skipTest("pgbouncer n'a pas ouvert sa socket")

    def _stop_bouncer(self):
        self.bouncer.terminate()
        try:
            self.bouncer.wait(10)
        except subprocess.TimeoutExpired:
            self.bouncer.kill()

    def _write_through_pool(self, n):
        import psycopg
        with psycopg.connect("host=%s port=6432 user=postgres dbname=postgres" % self.sock,
                             autocommit=True, connect_timeout=5) as conn:
            conn.execute("INSERT INTO t VALUES (%s)", (n,))
            return conn.execute("SELECT inet_server_port()").fetchone()[0]

    def test_failover_repoints_the_pool_to_the_new_primary(self):
        self.assertEqual(self.recipe.amont(self.home, self.state), "unchanged")
        self.assertEqual(self._write_through_pool(1), self.pa)
        self._pg_ctl(self.a, self.pa, "stop", "-m", "immediate")
        self._pg_ctl(self.b, self.pb, "promote")
        self.assertEqual(self.recipe.amont(self.home, self.state), "changed")
        port = None
        for _ in range(50):     # server_login_retry après l'échec sur A
            try:
                port = self._write_through_pool(2)
                break
            except Exception:
                time.sleep(0.2)
        self.assertEqual(port, self.pb)
        import psycopg
        with psycopg.connect("host=127.0.0.1 port=%d user=postgres dbname=postgres" % self.pb) as conn:
            self.assertEqual(sorted(r[0] for r in conn.execute("SELECT n FROM t")), [1, 2])
        self.assertEqual(self.recipe.amont(self.home, self.state), "unchanged")


if __name__ == "__main__":
    unittest.main()
