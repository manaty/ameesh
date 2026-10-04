# SPDX-License-Identifier: AGPL-3.0-only
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

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import migrations

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO, "src")
FAKEBIN = os.path.join(REPO, "tests", "fakebin")
TEST_DSN = os.environ.get(
    "AMEESH_TEST_DSN", os.environ.get(
        "AGENT_MESH_TEST_DSN",
        #: aucun secret dans le dépôt : PGPASSWORD/~/.pgpass pour le banc local.
        "postgresql://agent_mesh@127.0.0.1:55432/agent_mesh",
    ),
)
REQUIRE_DB = os.environ.get(
    "AMEESH_REQUIRE_DB", os.environ.get("AGENT_MESH_REQUIRE_DB", "1")) != "0"


def apply_authenticators(db, members, **kwargs) -> dict:
    """CONTEXTE DE TEST EXPLICITE (tests de L6, sans canon git) : écrit le
    registre des authentificateurs depuis une liste de membres.

    Comme `canon_sync._sync_locked` : une transaction, le verrou du registre
    (`receipts.lock_registry`, qui rend son jeton), puis l'écriture interne
    `receipts._apply_authenticators` — mais SANS les contrôles du canon
    (branche de confiance, monotonie, retard). Réservé aux tests : en
    production, le seul point d'entrée est `canon_sync.sync_authenticators`.
    """
    from ameesh import receipts
    with db.transaction() as tx:
        lock = receipts.lock_registry(tx)
        return receipts._apply_authenticators(lock, members, **kwargs)


def child_env(**extra: str) -> dict:
    """Environnement d'un sous-processus de test (CLI ou exécuteur)."""
    env = dict(os.environ)
    # le canon et la règle R14 de la machine ne fuient pas dans les tests :
    # chaque test qui en a besoin les pose explicitement
    for name in ("AMEESH_CANON", "AMEESH_CANON_REF", "AMEESH_CANON_UNTRUSTED",
                 "AMEESH_REQUIRE_RESPONSIBLE", "AGENT_MESH_CANON", "AGENT_MESH_CANON_REF",
                 "AGENT_MESH_CANON_UNTRUSTED", "AGENT_MESH_REQUIRE_RESPONSIBLE"):
        env.pop(name, None)
    # Les fils vont sous l'état du test (AMEESH_STATE/fils), jamais dans un
    # dossier réel hérité de l'environnement du développeur.
    for name in ("AMEESH_THREADS", "AMEESH_PROJECT", "AMEESH_HUMANS"):
        env.pop(name, None)
    env["AMEESH_DSN"] = TEST_DSN
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["AMEESH_BIN_DIR"] = FAKEBIN
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
            AMEESH_SCHEMA=cls.schema,
            AMEESH_STATE=cls.state,
            AGENT_MAIL_STATE=cls.v0state,
            AGENT_MAIL_CONFIG=cls.conf,
            AMEESH_TEST_LOG=cls.turns_log,
        ))
        try:
            cls.db = db_mod.connect(cls.cfg)
        except db_mod.Unavailable as exc:
            message = (
                "Postgres injoignable (%s) : lancez scripts/pg-up.sh, ou "
                "AMEESH_REQUIRE_DB=0 pour ignorer ces tests. Détail : %s"
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
        self.db.execute(
            "TRUNCATE agent_registry, agent_mailbox, mesh_approvals, "
            "work_items, work_item_events, thread_index RESTART IDENTITY CASCADE")
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        for path in (self.state, self.v0state, self.conf):
            os.makedirs(path, exist_ok=True)

    @classmethod
    def env(cls, **extra: str) -> dict:
        base = dict(
            AMEESH_SCHEMA=cls.schema,
            AMEESH_STATE=cls.state,
            AGENT_MAIL_STATE=cls.v0state,
            AGENT_MAIL_CONFIG=cls.conf,
            AMEESH_TEST_LOG=cls.turns_log,
        )
        base.update(extra)
        return child_env(**base)

    def connect(self, schema: str | None = None):
        return db_mod.connect(dataclasses.replace(self.cfg, schema=schema or self.schema))

    def cli(self, *args: str, env: dict | None = None, stdin: str | None = None,
            timeout: float = 60.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.cli", *args],
            input=stdin, capture_output=True, text=True, timeout=timeout,
            env=env or self.env(), cwd=self.tmp,
        )

    def runner(self, *args: str, env: dict | None = None, timeout: float = 60.0,
               ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.runner", *args],
            capture_output=True, text=True, timeout=timeout,
            env=env or self.env(), cwd=self.tmp,
        )

    def mesh(self, *args: str, env: dict | None = None, timeout: float = 60.0,
             ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "ameesh.main", *args],
            capture_output=True, text=True, timeout=timeout,
            env=env or self.env(), cwd=self.tmp,
        )

    def runner_popen(self, *args: str, env: dict | None = None) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "ameesh.runner", *args],
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

    def make_test_key(self, name: str = "test-owner") -> tuple[str, str, str]:
        """Paire de clés de TEST dans le bac à sable — jamais une clé réelle."""
        from ameesh import signing
        directory = os.path.join(self.tmp, "keys")
        return signing.write_keypair(directory, name)

    def register(self, name: str, harness: str, **kwargs) -> subprocess.CompletedProcess:
        args = ["register", name, harness]
        for key, value in kwargs.items():
            flag = "--" + key.replace("_", "-")
            if value is True:
                args.append(flag)
            elif value is not None:
                args += [flag, str(value)]
        return self.runner(*args)
