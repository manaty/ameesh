# SPDX-License-Identifier: AGPL-3.0-only
"""Public fixture surface, contract v1.5 (tranche F2, #7701/#5902).

An installed external module qualifies against a **consumer-owned** disposable
database: this module creates a fresh database on the consumer's PostgreSQL
server, applies the released schema through the documented ``ameesh migrate``
command, keeps every file in a private temporary root, and drops the database
again on exit.  It never touches another database, never writes a core table
through this module, and imports neither ``tests.support`` nor a private path.

The written boundary is ``docs/api/module-contract-v1.md``; the witness that a
consumer can copy is ``examples/fixtures/public-fixture-witness``.  A fixture
hands back:

* ``dsn`` — the disposable database, for the consumer's own SQL client;
* ``env(**extra)`` — an isolated process environment for the installed
  executables (``AMEESH_DSN``, an empty config, private state directories);
* ``cli()`` / ``mail()`` / ``run()`` — the installed executables as
  subprocesses, never imports of their dispatchers;
* ``connect()`` — an opaque core-owned connection for the public Python
  surfaces (:mod:`ameesh.public.mail`); do not query core tables with it;
* ``read_view()`` — the named SQL read views admitted by the contract, with
  explicit columns only.

The database name is always ``ameesh_fixture_<random>``.  Only the instance
that created a database drops it; ``close()`` is idempotent and ``keep=True``
leaves it for inspection.  The admin DSN is a secret: ``dsn`` and the child
environment carry it (or its password), and ``repr()`` never shows it.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import Mapping, Optional, Sequence

from .. import config as config_mod
from .. import db as db_mod
from .. import dsn as dsn_mod

#: revision of the written contract served by this module
CONTRACT_VERSION = "1.5"

#: every database this surface creates starts with this prefix
DATABASE_PREFIX = "ameesh_fixture_"

#: the named SQL read views admitted by the contract (never a base table)
ADMITTED_VIEWS = ("agent_mesh_overview", "mesh_keys", "mesh_approvals_status")

#: environment keys the fixture owns in the child environment
ENV_KEYS = ("AMEESH_DSN", "AMEESH_CONFIG", "AMEESH_STATE", "AGENT_MAIL_STATE",
            "AGENT_MAIL_CONFIG", "AMEESH_SCHEMA")

#: prefixes removed from the inherited environment: no host mesh state leaks in
_STRIPPED_PREFIXES = ("AMEESH_", "AGENT_MESH_", "AGENT_MAIL_")

#: identifiers accepted for a view or a column (never a SQL fragment)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
#: database names the fixture is willing to create and drop
_DB_NAME = re.compile(r"^ameesh_fixture_[a-z0-9_]{4,64}$")

#: default timeout, in seconds, of one installed command
DEFAULT_TIMEOUT = 120


class FixtureError(RuntimeError):
    """The fixture could not create, migrate or query its disposable database."""

    code = "fixture"


class FixtureRefused(FixtureError):
    """The fixture refuses an unsafe target or an out-of-protocol call."""

    code = "refused"


class Fixture:
    """A consumer-owned disposable database and its isolated process state.

    ``Fixture(admin_dsn)`` does not connect until :meth:`create` (or the
    context-manager form).  ``admin_dsn`` must name an existing database whose
    role may ``CREATE DATABASE``; the fixture derives the disposable database
    name itself and refuses to drop any name it did not create.
    """

    def __init__(self, admin_dsn: str, *, executable: Optional[Sequence[str]] = None,
                 mail_executable: Optional[Sequence[str]] = None,
                 root: Optional[str] = None, prefix: str = DATABASE_PREFIX,
                 keep: bool = False) -> None:
        if not isinstance(admin_dsn, str) or not admin_dsn.strip():
            raise FixtureRefused("admin_dsn is required: a DSN allowed to CREATE DATABASE")
        if not _IDENT.match(prefix) or not prefix.startswith("ameesh_fixture_"):
            raise FixtureRefused(
                "prefix must start with ameesh_fixture_ and be a plain identifier: %r"
                % (prefix,))
        self.admin_dsn = admin_dsn
        self.prefix = prefix
        self.keep = bool(keep)
        self.executable = _command(executable) if executable else (sys.executable, "-m", "ameesh")
        self.mail_executable = (_command(mail_executable) if mail_executable
                                else (sys.executable, "-m", "ameesh.entry"))
        self._root = root
        self._owns_root = root is None
        self._database = ""
        self._dsn = ""
        self._created = False
        self._closed = False
        self._canon_root = ""
        self._canon_ref = ""
        self._connections = []
        self._db = None

    # -- lifecycle ---------------------------------------------------------
    def create(self) -> "Fixture":
        """Create the disposable database, apply the released schema, return self."""
        if self._created:
            raise FixtureRefused("fixture already created: %s" % self._database)
        if self._closed:
            raise FixtureRefused("fixture already closed")
        self._root = self._root or tempfile.mkdtemp(prefix="ameesh-fixture-")
        self._root = os.path.abspath(self._root)
        for relative in ("state", "v0state", "conf", "canon"):
            os.makedirs(os.path.join(self._root, relative), exist_ok=True)
        self._write_config()
        last = None
        for _attempt in range(3):
            name = self.prefix + secrets.token_hex(8)
            try:
                self._admin("CREATE DATABASE \"%s\"" % name)
            except FixtureError as exc:
                last = exc
                continue
            self._database = name
            self._dsn = dsn_mod.with_database(self.admin_dsn, name)
            self._created = True
            break
        if not self._created:
            raise FixtureError("CREATE DATABASE failed after three attempts: %s"
                               % (last,))
        try:
            self._migrate()
        except BaseException:
            self._drop()
            raise
        return self

    def close(self) -> None:
        """Close the connections and drop the disposable database (idempotent)."""
        if self._db is not None:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001 — teardown must not mask the reason
                pass
            self._db = None
        for connection in self._connections:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
        self._connections = []
        if self._created and not self.keep:
            self._drop()
        if self._owns_root and self._root:
            shutil.rmtree(self._root, ignore_errors=True)
        self._db = None
        self._closed = True

    def __enter__(self) -> "Fixture":
        return self.create()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def __repr__(self) -> str:
        return "<Fixture database=%r created=%s>" % (self._database or None,
                                                     self._created)

    # -- published accessors ----------------------------------------------
    @property
    def database(self) -> str:
        """The disposable database name (empty before :meth:`create`)."""
        return self._database

    @property
    def dsn(self) -> str:
        """The disposable database DSN; it carries the consumer's credentials."""
        if not self._created:
            raise FixtureRefused("fixture not created")
        return self._dsn

    @property
    def root(self) -> str:
        """The private temporary root (canon, config, state)."""
        if not self._root:
            raise FixtureRefused("fixture not created")
        return self._root

    @property
    def db(self):
        """An opaque core-owned connection for the public Python surfaces."""
        return self.connect()

    def connect(self):
        """Open another core-owned connection to the disposable database.

        Use it for concurrency witnesses (one connection per thread) and pass
        it to :mod:`ameesh.public.mail`.  It is not an SQL escape hatch: the
        core's tables are internal, and only the named views are admitted.
        """
        if not self._created:
            raise FixtureRefused("fixture not created")
        cfg = config_mod.load(env=self.env())
        try:
            connection = db_mod.connect(cfg)
        except db_mod.Unavailable as exc:
            raise FixtureError("cannot connect to %s: %s" % (self._database, exc)) from exc
        self._connections.append(connection)
        return connection

    def env(self, **extra: str) -> dict:
        """The isolated child environment, with this fixture's database and paths.

        Every inherited ``AMEESH_*``/``AGENT_MESH_*``/``AGENT_MAIL_*`` variable is
        removed first, so a host canon, DSN or runner identity cannot leak into
        the witness.  ``extra`` is applied last and must be strings.
        """
        if not self._created:
            raise FixtureRefused("fixture not created")
        base = {key: value for key, value in os.environ.items()
                if not key.startswith(_STRIPPED_PREFIXES)}
        base.update({
            "AMEESH_DSN": self._dsn,
            "AMEESH_CONFIG": os.path.join(self._root, "config.json"),
            "AMEESH_STATE": os.path.join(self._root, "state"),
            "AGENT_MAIL_STATE": os.path.join(self._root, "v0state"),
            "AGENT_MAIL_CONFIG": os.path.join(self._root, "conf"),
            "AMEESH_SCHEMA": "public",
        })
        if self._canon_root:
            base["AMEESH_CANON"] = self._canon_root
            base["AMEESH_CANON_REF"] = self._canon_ref or "main"
        if base.get("PYTHONPATH"):
            # The child runs in the fixture root: a relative source path of the
            # parent would otherwise resolve against another directory.
            base["PYTHONPATH"] = os.pathsep.join(
                os.path.abspath(part) for part in base["PYTHONPATH"].split(os.pathsep)
                if part)
        for key, value in extra.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise FixtureRefused("environment entries must be strings: %r" % ((key, value),))
            base[key] = value
        return base

    # -- installed executables --------------------------------------------
    def cli(self, *args: str, env: Optional[Mapping[str, str]] = None,
            stdin: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        """Run the installed ``ameesh`` executable with the fixture environment."""
        return self.run(self.executable, *args, env=env, stdin=stdin, timeout=timeout)

    def mail(self, *args: str, env: Optional[Mapping[str, str]] = None,
             stdin: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        """Run the installed ``agent-mail`` executable with the fixture environment."""
        return self.run(self.mail_executable, *args, env=env, stdin=stdin,
                        timeout=timeout)

    def run(self, command: Sequence[str], *args: str,
            env: Optional[Mapping[str, str]] = None, stdin: Optional[str] = None,
            timeout: float = DEFAULT_TIMEOUT):
        """Run one installed executable; ``command`` is a prefix, never an import."""
        if not self._created:
            raise FixtureRefused("fixture not created")
        merged = self.env()
        if env:
            merged.update(env)
        try:
            return subprocess.run(list(command) + list(args), input=stdin,
                                  capture_output=True, text=True, timeout=timeout,
                                  env=merged, cwd=self._root)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FixtureError("command %s failed to run: %s"
                               % (" ".join(command), exc)) from exc

    # -- private files and a git canon ------------------------------------
    def path(self, relative: str) -> str:
        """A path under the private root; absolute paths and ``..`` are refused."""
        return os.path.join(self.root, _relative(relative))

    def write(self, relative: str, text: str) -> str:
        """Write a UTF-8 file under the private root; returns its path."""
        if not isinstance(text, str):
            raise FixtureRefused("text must be a string")
        path = self.path(relative)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read(self, relative: str) -> str:
        """Read a UTF-8 file under the private root."""
        with open(self.path(relative), encoding="utf-8") as handle:
            return handle.read()

    def git(self, *args: str) -> str:
        """Run ``git`` in the canon repository; returns stripped stdout."""
        if not self._canon_root:
            raise FixtureRefused("no canon repository: call canon() first")
        return self._git(*args)

    def canon(self, files: Mapping[str, str], *, message: str = "fixture canon",
              ref: str = "main") -> str:
        """Write a fresh git canon and return its commit.

        The canon is a private repository under the fixture root.  ``ref`` is a
        local branch; a matching ``refs/remotes/origin/<ref>`` is set so the
        core's authenticator check sees the canonical branch without a network
        remote, exactly as a consumer's checkout would after a fetch.
        """
        if not isinstance(files, Mapping) or not files:
            raise FixtureRefused("canon files must be a non-empty mapping")
        if not _IDENT.match(ref):
            raise FixtureRefused("canon ref must be a plain branch name: %r" % (ref,))
        root = os.path.join(self.root, "canon")
        if os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(root)
        for relative, text in files.items():
            if not isinstance(relative, str) or not isinstance(text, str):
                raise FixtureRefused("canon files must map strings to strings")
            destination = os.path.join(root, _relative(relative))
            os.makedirs(os.path.dirname(destination) or root, exist_ok=True)
            with open(destination, "w", encoding="utf-8") as handle:
                handle.write(text)
        self._canon_root, self._canon_ref = root, ref
        self._git("init", "-q", "-b", ref)
        self._git("add", "-A")
        self._git("-c", "user.name=fixture", "-c", "user.email=fixture@ameesh.invalid",
                  "commit", "-q", "-m", message)
        commit = self._git("rev-parse", "HEAD")
        self._git("update-ref", "refs/remotes/origin/%s" % ref, commit)
        return commit

    # -- named read views --------------------------------------------------
    def read_view(self, view: str, columns: Sequence[str]) -> list:
        """Rows of one admitted named view, with explicit columns (never ``*``)."""
        if view not in ADMITTED_VIEWS:
            raise FixtureRefused("view %r is not an admitted named view" % (view,))
        names = tuple(str(column) for column in columns)
        if not names or any(not _IDENT.match(name) for name in names):
            raise FixtureRefused("explicit plain column names are required: %r"
                                 % (list(columns),))
        query = "SELECT %s FROM %s" % (", ".join(names), view)
        try:
            return self.db.query(query)
        except FixtureRefused:
            raise
        except Exception as exc:  # noqa: BLE001 — one stable public error
            raise FixtureError("read_view(%s): %s" % (view, exc)) from exc

    # -- internals ---------------------------------------------------------
    @property
    def _config_path(self) -> str:
        return os.path.join(self._root, "config.json")

    def _write_config(self) -> None:
        with open(self._config_path, "w", encoding="utf-8") as handle:
            handle.write("{}\n")

    def _migrate(self) -> None:
        done = self.run(self.executable, "migrate", timeout=600)
        if done.returncode != 0:
            raise FixtureError("ameesh migrate refused (exit %d): %s"
                               % (done.returncode, (done.stderr or "").strip()[-800:]))

    def _admin(self, statement: str) -> None:
        """Run one administrative statement on the consumer's server."""
        try:
            import psycopg  # type: ignore
        except ImportError:
            self._admin_psql(statement)
            return
        try:
            with psycopg.connect(self.admin_dsn, autocommit=True) as connection:
                connection.execute(statement)
        except Exception as exc:  # noqa: BLE001 — one stable public error
            raise FixtureError("admin statement failed: %s" % exc) from exc

    def _admin_psql(self, statement: str) -> None:
        try:
            done = subprocess.run(
                ["psql", "-w", "-v", "ON_ERROR_STOP=1", "-d", self.admin_dsn,
                 "-c", statement],
                capture_output=True, text=True, timeout=60, env=dict(os.environ))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FixtureError("psql admin statement failed to run: %s" % exc) from exc
        if done.returncode != 0:
            raise FixtureError("psql admin statement refused (exit %d): %s"
                               % (done.returncode, (done.stderr or "").strip()[-800:]))

    def _drop(self) -> None:
        name = (self._database or "").strip()
        if not _DB_NAME.match(name) or not name.startswith(self.prefix):
            raise FixtureRefused("refusing to drop a database this fixture did not create: %r"
                                 % (name,))
        try:
            self._admin("DROP DATABASE IF EXISTS \"%s\" WITH (FORCE)" % name)
        finally:
            self._created = False

    def _git(self, *args: str) -> str:
        env = dict(os.environ)
        env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@ameesh.invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@ameesh.invalid",
        })
        done = subprocess.run(["git", "-C", self._canon_root, *args],
                              capture_output=True, text=True, env=env, timeout=60)
        if done.returncode != 0:
            raise FixtureError("git %s failed: %s"
                               % (" ".join(args), (done.stderr or "").strip()[-500:]))
        return done.stdout.strip()


def _command(executable: Sequence[str]) -> tuple:
    """A string command is one executable; a sequence is kept as given."""
    if isinstance(executable, str):
        return (executable,)
    return tuple(executable)


def _relative(relative: str) -> str:
    """A safe relative path under the fixture root."""
    if not isinstance(relative, str) or not relative:
        raise FixtureRefused("a relative path is required")
    if os.path.isabs(relative):
        raise FixtureRefused("absolute path refused: %r" % (relative,))
    normal = os.path.normpath(relative)
    if normal == ".." or normal.startswith(".." + os.sep):
        raise FixtureRefused("path leaves the fixture root: %r" % (relative,))
    return normal
