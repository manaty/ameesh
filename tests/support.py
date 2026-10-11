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
# la surcharge AMEESH_CODEX_SESSIONS prime sur CODEX_HOME : jamais héritée
os.environ.pop("AMEESH_CODEX_SESSIONS", None)
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


#: dossier Codex vide, partagé par les sous-processus de test
EMPTY_CODEX_HOME = tempfile.mkdtemp(prefix="ameesh-test-codex-home-")
# aussi pour les tests qui lisent les jauges dans le processus même
os.environ["CODEX_HOME"] = EMPTY_CODEX_HOME

#: dossier de descripteurs de harnais vide (L16) : les tests ne voient jamais
#: ceux de l'hôte, et posent les leurs explicitement quand ils en ont besoin
EMPTY_HARNESS_DIR = tempfile.mkdtemp(prefix="ameesh-test-harnesses-")
os.environ["AMEESH_HARNESSES_DIR"] = EMPTY_HARNESS_DIR

#: barème de prix ABSENT : le fichier de prix de l'hôte
#: (~/.config/nexlink-agents/prices.json) ne fuit pas dans les tests, ni dans
#: le processus (l'exécuteur construit son `CostBook` en mémoire) ni dans les
#: sous-processus. Les tests de tarif posent leur propre `AMEESH_PRICES`.
EMPTY_PRICES = os.path.join(tempfile.mkdtemp(prefix="ameesh-test-prices-"), "absent.json")
os.environ["AMEESH_PRICES"] = EMPTY_PRICES
#: L73 : le /tmp du système vu par le ménage, vide et propre à la suite (le
#: vrai /tmp de la machine n'est ni mesuré ni balayé par les tests)
EMPTY_SYSTEM_TMP = tempfile.mkdtemp(prefix="ameesh-test-system-tmp-")
os.environ["AMEESH_SYSTEM_TMP"] = EMPTY_SYSTEM_TMP
#: L125 : pas de fenêtre de regroupement du courrier dans le banc — un message
#: réveille son destinataire tout de suite, comme avant L125 ; les tests du
#: regroupement (test_l125_courrier_regroupe) posent leur propre fenêtre
os.environ["AMEESH_MAIL_BATCH"] = "0"
#: les binaires réels du poste (variables de l'exécuteur) ne fuient pas dans les
#: tests : le banc pose ses faux harnais par `AMEESH_BIN_DIR` ou explicitement
#: L106 : ni les emplacements connus des harnais (mise, ~/.local/bin, npx), ni
#: le PATH du gestionnaire systemd du poste : un test « binaire introuvable »
#: ne doit pas trouver le vrai harnais du développeur. Et l'alimentation du
#: poste (portable sur batterie) ne bloque pas les tours des tests : un dossier
#: de sources d'alimentation vide (« inconnu »), les tests de L106 posent le leur.
os.environ["AMEESH_HARNESS_SEARCH"] = ""
os.environ["AMEESH_SYSTEMCTL"] = ""
EMPTY_POWER_SUPPLY = tempfile.mkdtemp(prefix="ameesh-test-power-")
os.environ["AMEESH_POWER_SUPPLY_DIR"] = EMPTY_POWER_SUPPLY
for _bin_var in ("AMEESH_CLAUDE_BIN", "AGENT_MESH_CLAUDE_BIN", "AMEESH_CODEX_BIN",
                 "AGENT_MESH_CODEX_BIN", "AMEESH_DSH_BIN", "AGENT_MESH_DSH_BIN"):
    os.environ.pop(_bin_var, None)
#: les comptes réels du lanceur (dossiers de configuration et clés des
#: harnais, `config_env`/`key_env` des descripteurs) ne fuient pas dans les
#: tests : sans compte déclaré, un tour doit voir l'environnement « nu » du
#: harnais. CODEX_HOME n'est pas retiré mais pointé sur un dossier vide
#: (ci-dessus) : absent, Codex retomberait sur le ~/.codex réel.
ACCOUNT_ENV_VARS = ("CLAUDE_CONFIG_DIR", "DSH_HOME",
                    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
for _compte_var in ACCOUNT_ENV_VARS:
    os.environ.pop(_compte_var, None)


def child_env(**extra: str) -> dict:
    """Environnement d'un sous-processus de test (CLI ou exécuteur)."""
    env = dict(os.environ)
    # le canon et la règle R14 de la machine ne fuient pas dans les tests :
    # chaque test qui en a besoin les pose explicitement
    for name in ("AMEESH_CANON", "AMEESH_CANONS", "AMEESH_CANON_REF", "AMEESH_CANON_UNTRUSTED",
                 "AMEESH_REQUIRE_RESPONSIBLE", "AGENT_MESH_CANON", "AGENT_MESH_CANON_REF",
                 "AGENT_MESH_CANON_UNTRUSTED", "AGENT_MESH_REQUIRE_RESPONSIBLE"):
        env.pop(name, None)
    # l'identité de session du lanceur (l'agent qui exécute la suite) ne fuit
    # jamais dans un sous-processus : chaque test pose la sienne. Sans cela,
    # un bail d'agent réel de la machine ferait échouer les tests d'autorité.
    for name in ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                 "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH"):
        env.pop(name, None)
    # Les fils vont sous l'état du test (AMEESH_STATE/fils), jamais dans un
    # dossier réel hérité de l'environnement du développeur.
    for name in ("AMEESH_THREADS", "AMEESH_PROJECT", "AMEESH_HUMANS"):
        env.pop(name, None)
    # Les jauges de forfait réelles du poste (journaux Codex de ~/.codex) ne
    # fuient pas dans les tests : un forfait très consommé mettrait en pause les
    # agents de test. Les tests de jauges posent leurs propres journaux.
    env["CODEX_HOME"] = EMPTY_CODEX_HOME
    # ni les comptes du lanceur (CLAUDE_CONFIG_DIR hérité, clés…)
    for name in ACCOUNT_ENV_VARS:
        env.pop(name, None)
    # Aucun réseau dans les tests (L26) : pas de clé de fournisseur, pas de
    # relevé de solde par l'exécuteur.
    for name in ("DEEPSEEK_API_KEY", "AMEESH_DEEPSEEK_API_BASE"):
        env.pop(name, None)
    env["AMEESH_BALANCE_INTERVAL"] = "0"
    # Les relevés de ressources de l'hôte réel (L31) ne fuient pas non plus :
    # les tests de pression posent eux-mêmes leur intervalle et leurs mesures.
    env["AMEESH_RESOURCE_INTERVAL"] = "0"
    # Ni passage périodique du ménage (L73) ni /tmp réel : chaque test de
    # ménage pose son intervalle et son dossier temporaire du système.
    env["AMEESH_HOUSEKEEPING_INTERVAL"] = "0"
    # ni relevé périodique des branches (L118) : les tests le lancent eux-mêmes
    env["AMEESH_BRANCH_SWEEP_INTERVAL"] = "0"
    env["AMEESH_SYSTEM_TMP"] = EMPTY_SYSTEM_TMP
    # Aucun moteur de conteneurs réel pendant les tests (L31) : les tests de
    # rattachement injectent leur propre façade.
    env["AMEESH_CONTAINER_RUNTIME"] = "none"
    env["AMEESH_DSN"] = TEST_DSN
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["AMEESH_BIN_DIR"] = FAKEBIN
    # les descripteurs de harnais de l'hôte ne fuient pas non plus (L16)
    env["AMEESH_HARNESSES_DIR"] = EMPTY_HARNESS_DIR
    # le barème de prix de l'hôte ne fuit pas : les tests de tarif posent le leur
    env["AMEESH_PRICES"] = EMPTY_PRICES
    env.pop("AGENT_MESH_HARNESSES_DIR", None)
    env.pop("AMEESH_CODEX_SESSIONS", None)   # isolement par CODEX_HOME (ci-dessus)
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
            "work_items, work_item_events, work_item_milestones, work_packages, "
            "thread_index "
            "RESTART IDENTITY CASCADE")
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
