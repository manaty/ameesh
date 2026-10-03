"""Socle des tests agent-mesh : schéma Postgres jetable, CLI en sous-processus.

Chaque classe de test reçoit son propre schéma (`t_<classe>_<hex>`) dans le
conteneur local `agent-mesh-pg` et son propre état sur disque : aucun test ne
touche les données de production ni les vrais harnais.
"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from agent_mesh import config as config_mod
from agent_mesh import db as db_mod
from agent_mesh import migrations

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
FAKEBIN = os.path.join(REPO, "tests", "fakebin")
TEST_DSN = os.environ.get(
    "AGENT_MESH_TEST_DSN",
    "postgresql://agent_mesh:agent_mesh@127.0.0.1:55432/agent_mesh",
)
REQUIRE_DB = os.environ.get("AGENT_MESH_REQUIRE_DB", "1") != "0"


def child_env(**extra: str) -> dict:
    """Environnement d'un sous-processus de test (CLI ou exécuteur)."""
    env = dict(os.environ)
    env["AGENT_MESH_DSN"] = TEST_DSN
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["AGENT_MESH_BIN_DIR"] = FAKEBIN
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


class PgTestCase(unittest.TestCase):
    """Base : un schéma migré, un bac à sable disque, des utilitaires CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = "t_%s_%s" % (
            cls.__name__.lower().replace("test", "")[:18] or "x",
            uuid.uuid4().hex[:8],
        )
        cls.tmp = tempfile.mkdtemp(prefix="agent-mesh-test-")
        cls.state = os.path.join(cls.tmp, "state")
        cls.v0state = os.path.join(cls.tmp, "v0state")
        cls.conf = os.path.join(cls.tmp, "conf")
        cls.turns_log = os.path.join(cls.tmp, "turns.log")
        for path in (cls.state, cls.v0state, cls.conf):
            os.makedirs(path, exist_ok=True)
        cls.cfg = config_mod.load(env=child_env(
            AGENT_MESH_SCHEMA=cls.schema,
            AGENT_MESH_STATE=cls.state,
            AGENT_MAIL_STATE=cls.v0state,
            AGENT_MAIL_CONFIG=cls.conf,
            AGENT_MESH_TEST_LOG=cls.turns_log,
        ))
        try:
            cls.db = db_mod.connect(cls.cfg)
        except db_mod.Unavailable as exc:
            message = (
                "Postgres injoignable (%s) : lancez scripts/pg-up.sh, ou "
                "AGENT_MESH_REQUIRE_DB=0 pour ignorer ces tests. Détail : %s"
                % (TEST_DSN, exc)
            )
            if REQUIRE_DB:
                raise AssertionError(message) from exc
            raise unittest.SkipTest(message) from exc
        migrations.migrate(cls.db, log=None)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % cls.schema)
            cls.db.close()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- utilitaires -------------------------------------------------------
    def setUp(self) -> None:
        """Chaque test part d'une base et d'un disque propres."""
        self.db.execute("TRUNCATE agent_registry, agent_mailbox RESTART IDENTITY CASCADE")
        for path in (self.state, self.v0state, self.conf):
            shutil.rmtree(path, ignore_errors=True)
            os.makedirs(path, exist_ok=True)
        if os.path.exists(self.turns_log):
            os.unlink(self.turns_log)

    @classmethod
    def env(cls, **extra: str) -> dict:
        base = dict(
            AGENT_MESH_SCHEMA=cls.schema,
            AGENT_MESH_STATE=cls.state,
            AGENT_MAIL_STATE=cls.v0state,
            AGENT_MAIL_CONFIG=cls.conf,
            AGENT_MESH_TEST_LOG=cls.turns_log,
        )
        base.update(extra)
        return child_env(**base)

    def connect(self, schema: str | None = None):
        return db_mod.connect(dataclasses.replace(self.cfg, schema=schema or self.schema))

    def cli(self, *args: str, env: dict | None = None, stdin: str | None = None,
            timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agent_mesh.cli", *args],
            input=stdin, capture_output=True, text=True, timeout=timeout,
            env=env or self.env(), cwd=self.tmp,
        )

    def runner(self, *args: str, env: dict | None = None, timeout: float = 60.0,
               ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agent_mesh.runner", *args],
            capture_output=True, text=True, timeout=timeout,
            env=env or self.env(), cwd=self.tmp,
        )

    def runner_popen(self, *args: str, env: dict | None = None) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "agent_mesh.runner", *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=env or self.env(), cwd=self.tmp,
        )

    # -- journal des faux harnais -----------------------------------------
    def turns(self) -> list[dict]:
        if not os.path.exists(self.turns_log):
            return []
        with open(self.turns_log, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def clear_turns(self) -> None:
        if os.path.exists(self.turns_log):
            os.unlink(self.turns_log)

    # -- attentes ----------------------------------------------------------
    def wait_for(self, predicate, timeout: float = 10.0, interval: float = 0.1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        self.fail("condition non remplie après %ss" % timeout)

    def register(self, name: str, harness: str, **kwargs) -> subprocess.CompletedProcess:
        args = ["register", name, harness]
        for key, value in kwargs.items():
            flag = "--" + key.replace("_", "-")
            if value is True:
                args.append(flag)
            elif value is not None:
                args += [flag, str(value)]
        return self.runner(*args)
