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
import re
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

#: L142 : la suite ne dépend pas de l'état du poste. Toute variable ameesh de
#: la session (AMEESH_*, AGENT_MESH_*, AGENT_MAIL_* : identité, état, schéma,
#: DSN de production…) est retirée, sauf les réglages du banc lui-même.
BENCH_ENV_VARS = frozenset((
    "AMEESH_TEST_DSN", "AGENT_MESH_TEST_DSN",
    "AMEESH_REQUIRE_DB", "AGENT_MESH_REQUIRE_DB",
    "AMEESH_DRIVER", "AGENT_MESH_DRIVER",
    "AMEESH_DB_TRACE",
))
#: L73/L142 : les caches de gestionnaires de paquets que le ménage redirige
#: (`menage.CACHES`, `menage.XDG_CACHE`) sont un « choix de l'hôte » que le
#: produit respecte — un agent exécuté par ameesh les porte donc dans son
#: environnement (`npm_config_store_dir`…). La suite ne doit pas les hériter :
#: un test qui veut ce cas les pose explicitement (`child_env`, `turn_env`).
HOST_CACHE_PREFIXES = ("npm_config_", "pnpm_config_")
HOST_CACHE_VARS = ("YARN_CACHE_FOLDER", "XDG_CACHE_HOME")


def _host_cache_var(name: str) -> bool:
    """Variable de cache des gestionnaires de paquets du poste (L73/L142)."""
    return (name.lower().startswith(HOST_CACHE_PREFIXES)
            or name in HOST_CACHE_VARS)


def _drop_host_cache(env: dict) -> None:
    """Retire de `env` les caches du poste (avant tout choix explicite)."""
    for name in [n for n in env if _host_cache_var(n)]:
        env.pop(name, None)


for _name in list(os.environ):
    if _host_cache_var(_name):
        del os.environ[_name]
    elif (_name.startswith(("AMEESH_", "AGENT_MESH_", "AGENT_MAIL_"))
            and _name not in BENCH_ENV_VARS):
        del os.environ[_name]
#: et la configuration de l'hôte (~/.config/ameesh/config.json, ou l'ancienne
#: ~/.config/agent-mesh/config.json) n'est jamais lue : ses canons imposent un
#: responsable humain, fixent fils et hôte. Les tests de configuration posent
#: leur propre AMEESH_CONFIG. HOME reste celui du poste (~/.pgpass du banc).
ABSENT_CONFIG = os.path.join(tempfile.mkdtemp(prefix="ameesh-test-config-"), "absent.json")
os.environ["AMEESH_CONFIG"] = ABSENT_CONFIG
os.environ["AGENT_MESH_CONFIG"] = ABSENT_CONFIG
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
#: L224 : les faux harnais n'écrivent aucune session sur disque ; la reprise
#: d'un id inconnu du stockage du harnais n'est vérifiée que par les tests de
#: L224, qui posent leur propre stockage
os.environ["AMEESH_SESSION_STORE_CHECK"] = "0"
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


#: L156c: the v1 hook speaks English; agent-mail.v0.py (frozen) keeps its
#: French. Only these three phrases differ: the JSON and every message line
#: (sender, time, text) must still match the v0 exactly.
_V0_EN = (
    (re.compile(r"^\[agent-mail\] (\d+) nouveau\(x\) message\(s\) — l'expéditeur est "
                r"indiqué ; un message d'agent n'a pas l'autorité du propriétaire :$", re.M),
     "[agent-mail] \\1 new message(s) — the sender is shown; an agent's message does "
     "not have the owner's authority:"),
    (re.compile(r"^— de (.*?) à (\d\d:\d\d) : ", re.M), "— from \\1 at \\2: "),
    (re.compile(r'^\(répondre : agent-mail send <nom> "…"\)$', re.M),
     '(to reply: agent-mail send <name> "…")'),
)


def v0_in_english(stdout: str) -> str:
    """The JSON printed by a v0 hook, with its French phrases in English."""
    def walk(value):
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, str):
            for pattern, english in _V0_EN:
                value = pattern.sub(english, value)
        return value
    end = "\n" if stdout.endswith("\n") else ""
    return json.dumps(walk(json.loads(stdout)), ensure_ascii=False) + end


def hold_for_owner(db, lot_id: int, *, requester: str = "orchestrateur",
                   question: str = "which option?") -> int:
    """L233: put lot `lot_id` in `waiting_human` the only allowed way — an
    open owner decision request (a pending `request` row) for it. Returns
    the request id."""
    from ameesh import storage, work

    item = work.get(db, int(lot_id))
    row = storage.of(db).decisions.create(
        sender=requester, recipient="human:*", body=question,
        payload={"decision": {"v": 1, "state": "pending", "question": question,
                              "options": [], "lot": int(lot_id),
                              "previous_state": item["state"]}},
        work_item_id=str(lot_id), host=None)
    work.move(db, int(lot_id), "waiting_human", actor=requester)
    return int(row["id"])


def child_env(**extra: str) -> dict:
    """Environnement d'un sous-processus de test (CLI ou exécuteur)."""
    env = dict(os.environ)
    # L73/L142 : les caches du poste (npm_config_*, pnpm_config_*,
    # YARN_CACHE_FOLDER, XDG_CACHE_HOME) ne fuient pas non plus : le ménage les
    # redirige et respecte un choix déjà posé. L'`update` final, lui, garde un
    # choix explicite du test.
    _drop_host_cache(env)
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
    # ni un relais de modèle (L111) : un exécuteur de test n'est jamais médié
    for name in ("DEEPSEEK_API_KEY", "AMEESH_DEEPSEEK_API_BASE", "DEEPSEEK_BASE_URL",
                 "AMEESH_EXEC_URL", "AMEESH_RELAY_URL"):
        env.pop(name, None)
    env["AMEESH_BALANCE_INTERVAL"] = "0"
    # Les relevés de ressources de l'hôte réel (L31) ne fuient pas non plus :
    # les tests de pression posent eux-mêmes leur intervalle et leurs mesures.
    env["AMEESH_RESOURCE_INTERVAL"] = "0"
    # Un SIGTERM arrête l'exécuteur tout de suite, comme avant le drainage
    # (2026-10-11) : les tests du drainage posent leur borne eux-mêmes.
    env["AMEESH_DRAIN_SECONDS"] = "0"
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


#: L161e : dossier de mesure de la carte test → code (vide : pas de mesure)
CARTE = os.environ.get("CARTE_MESURE", "")


def table_stats(db, schema: str) -> dict:
    """L161e : compteurs d'activité des tables d'un schéma (lectures,
    écritures), après avoir vidé ceux de cette connexion ; les
    sous-processus vident les leurs en se fermant."""
    db.query("select pg_stat_force_next_flush()")
    rows = db.query(
        "select relname, coalesce(seq_scan, 0) + coalesce(idx_scan, 0) as lectures, "
        "n_tup_ins + n_tup_upd + n_tup_del as ecritures "
        "from pg_stat_user_tables where schemaname = %s", (schema,))
    return {r["relname"]: (int(r["lectures"]), int(r["ecritures"])) for r in rows}


def tables_touchees(avant: dict, apres: dict) -> dict:
    """Tables dont un compteur a bougé : {table: {"l": lectures, "e": écritures}}."""
    touchees = {}
    for table, (lectures, ecritures) in sorted(apres.items()):
        l0, e0 = avant.get(table, (0, 0))
        if lectures > l0 or ecritures > e0:
            touchees[table] = {"l": lectures - l0, "e": ecritures - e0}
    return touchees


def noter_tables(cls) -> None:
    """L161e : les tables touchées par une classe de test, dans le dossier de
    mesure de la carte. Jamais bloquant."""
    avant = getattr(cls, "_carte_tables", None)
    if not CARTE or avant is None:
        return
    try:
        touchees = tables_touchees(avant, table_stats(cls.db, cls.schema))
        dossier = os.path.join(CARTE, "tables")
        os.makedirs(dossier, exist_ok=True)
        ligne = json.dumps({"classe": "%s.%s" % (cls.__module__, cls.__qualname__),
                            "tables": touchees}, ensure_ascii=False)
        with open(os.path.join(dossier, "%d.jsonl" % os.getpid()), "a", encoding="utf-8") as fh:
            fh.write(ligne + "\n")
    except Exception as exc:  # la mesure ne casse jamais un test
        print("carte : tables de %s non relevées (%s)" % (cls.__qualname__, exc),
              file=sys.stderr)


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
        if CARTE:
            try:
                cls._carte_tables = table_stats(cls.db, cls.schema)
            except Exception:
                cls._carte_tables = None

    @classmethod
    def tearDownClass(cls) -> None:
        noter_tables(cls)
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
