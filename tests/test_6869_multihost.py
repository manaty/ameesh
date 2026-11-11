# SPDX-License-Identifier: AGPL-3.0-only
"""#6869 (EP8.4a): a multi-host client finds the primary, reconnects to the new
one, and sweeps mail, leases and budget after a LISTEN/NOTIFY cut.

The DSN is a list of hosts: libpq tries them in order and
`target_session_attrs=read-write` makes it skip a standby or a demoted
primary. `dsn.primary` adds that attribute to a multi-host DSN only, byte for
byte unchanged on a single host (the current deployment). The failover test
kills the first endpoint (a TCP proxy in front of the bench database, with a
real connection) and proves both drivers reach the second host; the listener
test kills the LISTEN connection and proves the runner re-subscribes and
sweeps immediately, without waiting for the poll; the migration test kills a
backend in the middle of a migration and proves the transaction left neither a
table nor a `schema_migrations` row behind.
"""
from __future__ import annotations

import dataclasses
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qsl, unquote, urlsplit

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import dsn as dsn_mod
from ameesh import mail, migrations, registry
from ameesh.runner import AgentWorker, Runner

from .support import EMPTY_CONFIG, PgTestCase, TEST_DSN, scaled


def _test_dsn_parts() -> dict:
    """The bench connection as a dict, for a DSN copied to another port."""
    dsn = TEST_DSN
    if dsn.startswith(("postgresql://", "postgres://")):
        parts = urlsplit(dsn)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        return {
            "host": parts.hostname or os.environ.get("PGHOST") or "127.0.0.1",
            "port": parts.port or int(os.environ.get("PGPORT") or 5432),
            "dbname": unquote(parts.path.lstrip("/")) or "postgres",
            "user": unquote(parts.username or "") or os.environ.get("PGUSER") or "",
            "password": unquote(parts.password or "") or os.environ.get("PGPASSWORD") or "",
            "sslmode": query.get("sslmode") or os.environ.get("PGSSLMODE") or "",
        }
    tokens = dict(dsn_mod._tokens(dsn))
    return {
        "host": (tokens.get("host") or os.environ.get("PGHOST") or "127.0.0.1").split(",")[0],
        "port": int(tokens.get("port") or os.environ.get("PGPORT") or 5432),
        "dbname": tokens.get("dbname") or "postgres",
        "user": tokens.get("user") or os.environ.get("PGUSER") or "",
        "password": tokens.get("password") or os.environ.get("PGPASSWORD") or "",
        "sslmode": tokens.get("sslmode") or os.environ.get("PGSSLMODE") or "",
    }


def _passfile_password(host: str, port: int, dbname: str, user: str) -> str:
    """The password libpq would find in ~/.pgpass for the bench host.

    The failover DSN keeps the bench host but moves the first endpoint to a
    proxy port; PGPASSWORD (set from this value) applies to every host, while
    a port-specific ~/.pgpass entry would not."""
    path = os.environ.get("PGPASSFILE") or os.path.expanduser("~/.pgpass")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ""
    for line in lines:
        if not line or line.startswith("#"):
            continue
        fields = []
        current = ""
        i = 0
        while i < len(line):
            if line[i] == "\\" and i + 1 < len(line):
                current += line[i + 1]
                i += 2
                continue
            if line[i] == ":":
                fields.append(current)
                current = ""
                i += 1
                continue
            current += line[i]
            i += 1
        fields.append(current)
        if len(fields) != 5:
            continue
        f_host, f_port, f_db, f_user, f_password = fields
        if f_host not in ("*", host) or f_port not in ("*", str(port)):
            continue
        if f_db not in ("*", dbname) or f_user not in ("*", user):
            continue
        return f_password
    return ""


class _Proxy:
    """A transparent TCP endpoint in front of the bench database.

    `kill()` closes the listening socket and every forwarded connection with
    a reset, which is what a lost primary looks like to libpq."""

    def __init__(self, upstream: tuple[str, int]):
        self.upstream = upstream
        self.accepted = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._conns: list[socket.socket] = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._accept, daemon=True,
                                        name="proxy-%d" % self.port)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                client, _peer = self._listener.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(self.upstream, timeout=5)
            except OSError:
                client.close()
                continue
            with self._lock:
                self.accepted += 1
                self._conns.extend((client, upstream))
            threading.Thread(target=self._pump, args=(client, upstream),
                             daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client),
                             daemon=True).start()

    @staticmethod
    def _pump(source: socket.socket, target: socket.socket) -> None:
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                target.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, target):
                try:
                    sock.close()
                except OSError:
                    pass

    def kill(self) -> None:
        self._stop.set()
        # `close()` alone does not wake an `accept()` blocked in another
        # thread on Linux; `shutdown()` does, and the listening port then
        # refuses new connections before the test dials the second host.
        try:
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._listener.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)
        with self._lock:
            conns, self._conns = self._conns, []
        for sock in conns:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def close(self) -> None:
        self.kill()


class _Subscription:
    """A listener whose `wait` can block until released, then report a cut."""

    def __init__(self, release: threading.Event | None = None):
        self.release = release
        self.closed = False

    def wait(self, timeout: float):
        if self.release is None:
            time.sleep(min(timeout, 0.05))
            return None
        if self.release.wait(timeout):
            return {"event": "down", "error": "connection lost"}
        return None

    def close(self) -> None:
        self.closed = True


class DsnPrimaryTest(unittest.TestCase):
    """`dsn.primary`: multi-host targets the primary, mono-host is untouched."""

    def test_single_host_is_unchanged(self):
        for dsn in (
            "postgresql://u@h.invalid/db",
            "postgresql://u@h.invalid/db?sslmode=require",
            "host=h.invalid dbname=db",
            "dbname=db",
            "postgresql:///db",
        ):
            with self.subTest(dsn=dsn):
                self.assertEqual(dsn_mod.primary(dsn, {}), dsn)
                self.assertFalse(dsn_mod.multi_host(dsn))

    def test_uri_multi_host_gets_read_write(self):
        dsn = "postgresql://u:pw@h1:5432,h2:5433/db"
        got = dsn_mod.primary(dsn, {})
        self.assertEqual(got, dsn + "?target_session_attrs=read-write")
        password, rest = dsn_mod.split_password(got, {})
        self.assertEqual(password, "pw")
        self.assertEqual(rest, "postgresql://u@h1:5432,h2:5433/db"
                              "?target_session_attrs=read-write")

    def test_uri_query_is_preserved(self):
        for dsn in (
            "postgresql://u@h1:5432,h2:5433/db?sslmode=require",
            "postgresql://u@h1:5432,h2:5433/db?sslmode=require&",
            "postgresql://u@h1:5432,h2:5433/db?",
        ):
            with self.subTest(dsn=dsn):
                got = dsn_mod.primary(dsn, {})
                self.assertIn("target_session_attrs=read-write", got)
                self.assertNotIn("&&", got)
                self.assertEqual(got.count("?"), 1)

    def test_uri_host_parameter_follows_libpq(self):
        # libpq keeps the last `host=` of the query, and an empty host entry
        # is a real destination (the default host), not a hole to remove.
        last = "postgresql:///db?host=localhost&host=h1,h2"
        self.assertEqual(dsn_mod.hosts(last), ["h1", "h2"])
        self.assertIn("target_session_attrs=read-write", dsn_mod.primary(last, {}))
        empty = "postgresql:///db?host=,h2"
        self.assertEqual(dsn_mod.hosts(empty), ["", "h2"])
        self.assertIn("target_session_attrs=read-write", dsn_mod.primary(empty, {}))
        keyword = "host=,h2 dbname=db"
        self.assertEqual(dsn_mod.hosts(keyword), ["", "h2"])
        self.assertIn("target_session_attrs=read-write", dsn_mod.primary(keyword, {}))
        self.assertEqual(dsn_mod.hosts("postgresql:///db"), [])

    def test_existing_target_session_attrs_is_respected(self):
        for dsn in (
            "postgresql://u@h1,h2/db?target_session_attrs=any",
            "host=h1,h2 dbname=db target_session_attrs=prefer-standby",
            "postgresql://u@h1,h2/db?target%5Fsession%5Fattrs=any",
        ):
            with self.subTest(dsn=dsn):
                self.assertEqual(dsn_mod.primary(dsn, {}), dsn)

    def test_environment_and_service_decide_the_policy(self):
        for env in ({"PGTARGETSESSIONATTRS": "any"}, {"PGSERVICE": "prod"}):
            with self.subTest(env=env):
                self.assertEqual(dsn_mod.primary("host=h1,h2", env), "host=h1,h2")
        self.assertEqual(dsn_mod.primary("service=prod host=h1,h2", {}),
                         "service=prod host=h1,h2")

    def test_keyword_multi_host(self):
        for dsn, expected in (
            ("host=h1,h2 port=5432,5433 dbname=db",
             "host=h1,h2 port=5432,5433 dbname=db target_session_attrs=read-write"),
            ("host='h1,h2' dbname=db",
             "host='h1,h2' dbname=db target_session_attrs=read-write"),
            ("hostaddr=10.0.0.1,10.0.0.2 dbname=db",
             "hostaddr=10.0.0.1,10.0.0.2 dbname=db target_session_attrs=read-write"),
        ):
            with self.subTest(dsn=dsn):
                self.assertEqual(dsn_mod.primary(dsn, {}), expected)

    def test_hosts_follows_libpq(self):
        self.assertEqual(dsn_mod.hosts("postgresql://u@h1:5432,h2:5433/db"),
                         ["h1", "h2"])
        self.assertEqual(dsn_mod.hosts("postgresql://u@[::1]:5432,[::2]:5433/db"),
                         ["::1", "::2"])
        self.assertEqual(dsn_mod.hosts("postgresql:///db?host=h1,h2"), ["h1", "h2"])
        self.assertEqual(dsn_mod.hosts("host='h1,h2' port=1,2"), ["h1", "h2"])
        self.assertEqual(dsn_mod.hosts("hostaddr=10.0.0.1,10.0.0.2"), ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(dsn_mod.hosts("dbname=db"), [])

    def test_invalid_dsn_is_left_to_libpq(self):
        for dsn in ("postgresql://u@h1,h2/db?password", "host=h1,h2 password"):
            with self.subTest(dsn=dsn):
                self.assertEqual(dsn_mod.primary(dsn, {}), dsn)


class ConfigAndRemoteTest(unittest.TestCase):
    """The normalization reaches `Config.load`, and `is_remote` reads all hosts."""

    def test_config_load_normalizes_only_multi_host(self):
        multi = "postgresql://u@h1:5432,h2:5433/db"
        env = {"AMEESH_CONFIG": EMPTY_CONFIG, "AMEESH_DSN": multi}
        got = config_mod.load(env, register_credentials=False)
        self.assertEqual(got.dsn, multi + "?target_session_attrs=read-write")
        mono = "postgresql://u@h.invalid/db"
        got = config_mod.load({"AMEESH_CONFIG": EMPTY_CONFIG, "AMEESH_DSN": mono},
                              register_credentials=False)
        self.assertEqual(got.dsn, mono)

    def test_is_remote_uses_every_host(self):
        base = config_mod.Config(dsn="postgresql://u@127.0.0.1:5432,h.invalid:5432/db")
        self.assertTrue(db_mod.is_remote(base))
        local = config_mod.Config(dsn="postgresql://u@127.0.0.1:5432,localhost:5432/db")
        self.assertFalse(db_mod.is_remote(local))
        socket_dsn = config_mod.Config(
            dsn="postgresql:///db?host=/var/run/postgresql,/tmp")
        self.assertFalse(db_mod.is_remote(socket_dsn))


class FailoverTest(PgTestCase):
    """A killed first endpoint hands the client to the second host."""

    def _failover_cfg(self):
        parts = _test_dsn_parts()
        if not parts["host"] or parts["host"].startswith(("/", "@")):
            self.skipTest("the bench uses a Unix socket")
        password = parts["password"] or _passfile_password(
            parts["host"], parts["port"], parts["dbname"], parts["user"])
        if password and not parts["password"]:
            patcher = mock.patch.dict(os.environ, {"PGPASSWORD": password})
            patcher.start()
            self.addCleanup(patcher.stop)
        self.proxy = _Proxy((parts["host"], parts["port"]))
        self.addCleanup(self.proxy.close)
        dsn = "host=%s,%s port=%d,%d dbname=%s" % (
            "127.0.0.1", parts["host"], self.proxy.port, parts["port"], parts["dbname"])
        if parts["user"]:
            dsn += " user=%s" % dsn_mod._quote(parts["user"])
        if password:
            dsn += " password=%s" % dsn_mod._quote(password)
        if parts["sslmode"]:
            dsn += " sslmode=%s" % parts["sslmode"]
        dsn = dsn_mod.primary(dsn)
        self.assertIn("target_session_attrs=read-write", dsn)
        return dataclasses.replace(self.cfg, dsn=dsn, connect_timeout=2)

    def test_psycopg_reaches_the_second_host(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg absent")
        cfg = self._failover_cfg()
        driver = db_mod.PsycopgDriver(cfg)
        self.addCleanup(driver.close)
        first = driver.query("SELECT pg_backend_pid() AS pid")[0]["pid"]
        self.assertEqual(self.proxy.accepted, 1,
                         "the first connection must go through the killed host")
        self.proxy.kill()
        # libpq notices the reset either on the failed query (then the next
        # call reconnects through `_live`) or already at `_live`; both paths
        # must end on the second host.
        try:
            second = driver.query("SELECT pg_backend_pid() AS pid")[0]["pid"]
        except db_mod.Unavailable:
            second = driver.query("SELECT pg_backend_pid() AS pid")[0]["pid"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.proxy.accepted, 1,
                         "a dead first host must not be dialled again")

    def test_psql_reaches_the_second_host(self):
        cfg = self._failover_cfg()
        driver = db_mod.PsqlDriver(cfg)
        first = driver.query("SELECT pg_backend_pid() AS pid")[0]["pid"]
        with self.assertRaises(db_mod.DbError):
            with driver.transaction() as tx:
                tx.query("SELECT 1")
                before = self.proxy.accepted
                self.proxy.kill()
                tx.query("SELECT 1")
        # a new statement is a new psql: the dead host is skipped
        second = driver.query("SELECT pg_backend_pid() AS pid")[0]["pid"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.proxy.accepted, before,
                         "a dead first host must not be dialled again")


class ListenerSweepTest(PgTestCase):
    """The listener re-subscribes after a cut and sweeps without waiting."""

    def test_listener_resubscribes_and_sweeps(self):
        runner = Runner(dataclasses.replace(
            self.cfg, poll=0.1, db_retry_max=0.05, connect_timeout=1.0), self.db)
        worker = SimpleNamespace(wake=threading.Event())
        runner.workers["sweep"] = worker
        first = _Subscription(threading.Event())
        second = _Subscription()
        subs = [first, second]
        calls = []

        def listen(channels):
            calls.append(list(channels))
            return subs.pop(0) if subs else second

        thread = threading.Thread(target=runner._listen_loop, daemon=True)
        with mock.patch.object(self.db, "listen", side_effect=listen):
            thread.start()
            self.addCleanup(lambda: (runner.stop.set(), thread.join(
                scaled(5))))
            self.wait_for(lambda: len(calls) == 1 and worker.wake.is_set(),
                          timeout=5.0)
            worker.wake.clear()
            runner.budget_cache._at = 12.0   # stale: a cut may have moved it
            first.release.set()
            self.wait_for(lambda: len(calls) == 2 and worker.wake.is_set(),
                          timeout=5.0)
            self.assertIsNone(runner.budget_cache._at,
                              "budget caps must be reread after the cut")
            self.assertTrue(first.closed)
            self.assertFalse(second.closed)
            runner.stop.set()
            thread.join(scaled(5))
        self.assertTrue(second.closed)

    def test_transient_refusal_retries_instead_of_polling_forever(self):
        runner = Runner(dataclasses.replace(
            self.cfg, poll=0.1, db_retry_max=0.05, connect_timeout=1.0), self.db)
        calls = []
        listener = _Subscription()

        def listen(channels):
            calls.append(1)
            return None if len(calls) < 3 else listener

        thread = threading.Thread(target=runner._listen_loop, daemon=True)
        with mock.patch.object(self.db, "listen", side_effect=listen), \
                mock.patch.object(self.db, "ping", return_value=None):
            thread.start()
            self.addCleanup(lambda: (runner.stop.set(), thread.join(
                scaled(5))))
            self.wait_for(lambda: len(calls) >= 3, timeout=5.0)
            self.assertTrue(thread.is_alive(),
                            "a transient LISTEN refusal must not end the listener")
            runner.stop.set()
            thread.join(scaled(5))
        self.assertTrue(listener.closed)

    def test_driver_without_listen_falls_back_once(self):
        runner = Runner(dataclasses.replace(
            self.cfg, poll=0.1, db_retry_max=0.05, connect_timeout=1.0), self.db)
        calls = []
        with mock.patch.object(self.db, "listen", side_effect=lambda channels: calls.append(1)), \
                mock.patch.object(type(self.db), "supports_listen", False):
            runner._listen_loop()
        self.assertEqual(len(calls), 1)

    def test_a_mail_sent_during_the_cut_is_picked_after_the_sweep(self):
        name = "sweep-mail"
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.assertEqual(self.register(name, "claude", cwd=cwd).returncode, 0)
        runner = Runner(dataclasses.replace(
            self.cfg, budget_usd_per_hour=0.0, poll=3600.0), self.db)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertTrue(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        runner.workers[name] = worker
        worker.wake.clear()
        body = "sent during the cut: deliver without waiting for the poll"
        mail.send(self.db, "ameesh", name, body, kind="notify")
        self.assertFalse(worker.wake.is_set(), "no listener: no NOTIFY reaches it")
        runner.sweep_after_reconnect()
        self.assertTrue(worker.wake.is_set())
        spec = worker.pick()
        self.assertIsNotNone(spec, "the mail must be selected immediately")
        self.assertIn(body, spec.get("prompt") or "")


class MigrationInterruptionTest(PgTestCase):
    """A backend killed mid-migration leaves no partial state; a retry works."""

    def test_cut_in_a_migration_rolls_back_and_the_retry_applies(self):
        marker = "ameesh_6869_%s" % os.urandom(4).hex()
        table = "mig_partial_%s" % os.urandom(4).hex()
        directory = tempfile.mkdtemp(dir=self.tmp, prefix="migrations-")
        path = os.path.join(directory, "9999_bascule.sql")
        with open(path, "w", encoding="utf-8") as fh:
            # the marker is an SQL literal, not a comment: psql sends the
            # `pg_sleep` statement alone, and a comment would not appear in
            # `pg_stat_activity.query` (the failover test must find it).
            fh.write("CREATE TABLE %s (n integer);\n"
                     "SELECT pg_sleep(30), '%s';\n" % (table, marker))
        admin = db_mod.connect(self.cfg, writer=False)
        mig = db_mod.connect(self.cfg, writer=False)
        self.addCleanup(admin.close)
        outcome: dict = {}

        def run():
            try:
                migrations.migrate(mig, directory=directory)
            except BaseException as exc:  # noqa: BLE001 — the test asserts the type
                outcome["error"] = exc
            finally:
                mig.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        rows = self.wait_for(
            lambda: admin.query(
                "SELECT pid FROM pg_stat_activity WHERE state = 'active' "
                "AND pid <> pg_backend_pid() AND query LIKE %s",
                ("%" + marker + "%",)),
            timeout=15.0,
            msg=lambda: "migration query not seen: %s"
                        % admin.query("SELECT pid, state, query FROM pg_stat_activity "
                                      "WHERE datname = current_database()"))
        admin.execute("SELECT pg_terminate_backend(%s)", (rows[0]["pid"],))
        thread.join(scaled(15))
        self.assertFalse(thread.is_alive(), "the killed migration must return")
        self.assertIsInstance(outcome.get("error"), db_mod.DbError)
        self.assertIsNone(admin.query("SELECT to_regclass(%s)::text AS present", (table,))[0]["present"])
        self.assertEqual(admin.query(
            "SELECT count(*)::int AS n FROM schema_migrations WHERE version = 9999"
        )[0]["n"], 0)
        # the retry (the caller's, as the study says) applies the whole file
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("CREATE TABLE %s (n integer);\n" % table)
        done = migrations.migrate(admin, directory=directory)
        self.assertEqual([item.version for item in done], [9999])
        self.assertIsNotNone(admin.query("SELECT to_regclass(%s)::text AS present", (table,))[0]["present"])
        self.addCleanup(lambda: admin.execute("DROP TABLE IF EXISTS %s" % table))
        self.addCleanup(lambda: admin.execute(
            "DELETE FROM schema_migrations WHERE version = 9999"))


if __name__ == "__main__":
    unittest.main()
