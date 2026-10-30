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
import threading
import time
import traceback
import types
import unittest
from unittest import mock
import uuid
from unittest import mock

from ameesh import bench as bench_mod
from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import migrations
from ameesh import work as work_mod
from ameesh.platform import SYSTEM as PLATFORM_SYSTEM
from . import gabarit

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
    # #3752 : AMEESH_TEST_GABARIT=0 rejoue migrate() à chaque classe
    "AMEESH_TEST_GABARIT",
    # L98 T2b (#4408) : AMEESH_MODULES=bare, le job « profil nu » de la CI
    "AMEESH_MODULES",
))
#: L73/L142 : les caches de gestionnaires de paquets que le ménage redirige
#: (`menage.CACHES`, `menage.XDG_CACHE`) sont un « choix de l'hôte » que le
#: produit respecte — un agent exécuté par ameesh les porte donc dans son
#: environnement (`npm_config_store_dir`…). La suite ne doit pas les hériter :
#: un test qui veut ce cas les pose explicitement (`child_env`, `turn_env`).
HOST_CACHE_PREFIXES = ("npm_config_", "pnpm_config_")
HOST_CACHE_VARS = ("YARN_CACHE_FOLDER", "XDG_CACHE_HOME")


#: L98 T2b (#4408) : au profil nu (`AMEESH_MODULES=bare`), le cœur tourne sans
#: module intégré ; un test qui lit `dev` ou `it` est alors sauté, jamais faux.
BARE_PROFILE = os.environ.get("AMEESH_MODULES", "").strip() == "bare"
needs_builtin_modules = unittest.skipIf(
    BARE_PROFILE, "modules intégrés requis (profil nu, AMEESH_MODULES=bare)")


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
#: « Aucune config » se dit par un fichier VIDE mais EXISTANT : un chemin
#: explicite absent est refusé (#1127), et un chemin absent ne peut donc plus
#: servir de sentinelle.
EMPTY_CONFIG = os.path.join(tempfile.mkdtemp(prefix="ameesh-test-config-"), "empty.json")
with open(EMPTY_CONFIG, "w", encoding="utf-8") as _fh:
    _fh.write("{}\n")
os.environ["AMEESH_CONFIG"] = EMPTY_CONFIG
os.environ["AGENT_MESH_CONFIG"] = EMPTY_CONFIG
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
#: lot #3735 : une seule passe de ménage du banc par processus de suite (un
#: schéma t_* laissé par une exécution tuée). Un échec de lecture ne fait
#: jamais échouer un test ; `AMEESH_BENCH_UPKEEP=0` la désactive.
BENCH_UPKEEP_DONE = False
#: L142 : le banc ne dépend pas non plus de l'umask de la session appelante
#: (0002 sur certains hôtes, 0022 en CI). Les fixtures d'arbres de confiance
#: créent répertoires 0700 et fichiers 0644 et les vérifient ; un umask
#: permissif rendait leurs sous-répertoires groupables et la suite rouge.
os.umask(0o022)

#: #749 : préfixe des sorties négatives volontaires qu'un test surface dans le
#: journal. Le bilan de la suite complète (`scripts/test-parallele.sh`) cherche
#: un verdict unittest en début de ligne (`^(Ran |OK|FAILED)`) : une sortie
#: qu'un test attend exprès — « ROLE CONTRACT FAILED » (message de production
#: de deploy/sql/role-executor.sql), « FAILED: neither the cache nor a local
#: npm ci » — ne doit pas pouvoir passer pour un échec. On la préfixe au
#: moment où le test la surface (message d'assertion) ; on ne renomme jamais
#: le message de production à la source (décision mesh-design 13/10/2026,
#: #749 point 3). Convention figée par tests/test_expected_output.py.
ATTENDU = "attendu: "


def attendu(texte: str) -> str:
    """#749 : préfixe chaque ligne d'une sortie négative volontaire.

    À passer en message d'assertion quand le test vérifie un échec attendu :
    `self.assertIn("refusé", proc.stderr, attendu(proc.stderr))`. La
    vérification porte sur la sortie brute ; seule la trace écrite au journal
    est préfixée."""
    return "\n".join(ATTENDU + ligne if ligne else ligne
                     for ligne in texte.splitlines())


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

#: #2580 : le registre des comptes partagés de l'hôte (XDG) ne doit jamais
#: écrire dans le `~/.local/state` réel d'un exécuteur de test.
EMPTY_XDG_STATE = tempfile.mkdtemp(prefix="ameesh-test-xdg-state-")
os.environ["XDG_STATE_HOME"] = EMPTY_XDG_STATE

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
#: #1580 (tranche 3) : jamais les jauges du widget omarchy du poste (pc-smichea
#: en a de fraîches, dans ~/.local/state/omarchy/agents/usage) ; un test qui en
#: veut pointe la variable sur son propre dossier
EMPTY_USAGE_WIDGET = tempfile.mkdtemp(prefix="ameesh-test-usage-")
os.environ["AMEESH_USAGE_WIDGET_DIR"] = EMPTY_USAGE_WIDGET
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
    # #4609 : la base du banc n'est pas celle du mesh déployé ; la vérification
    # de la lecture complète quotidienne y serait toujours rouge. Les témoins
    # de `mesh_backup_unseen` posent eux-mêmes leur part (`mesh_backup_min_share`).
    env["AMEESH_MESH_BACKUP_MIN_SHARE"] = "0"
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


#: L66/#3131 : la route de notification de bureau de CET OS. Un banc qui
#: remplace le backend pose les DEUX surcharges explicites (la couche
#: plateforme choisit selon `SYSTEM`) et ses assertions suivent cette route :
#: sous Linux `notify-send`, sous macOS `osascript`.
DESKTOP_MACOS = PLATFORM_SYSTEM == "macos"
DESKTOP_VAR = "AMEESH_OSASCRIPT_BIN" if DESKTOP_MACOS else "AMEESH_NOTIFY_SEND_BIN"
DESKTOP_BACKEND = "osascript" if DESKTOP_MACOS else "notify-send"
DESKTOP_MISSING = DESKTOP_BACKEND + " not found"


def desktop_backend_env(binary: str, **extra) -> dict:
    """Environnement d'un faux backend de bureau, pour CET OS.

    Les deux surcharges pointent le même faux binaire : quelle que soit la
    route choisie par `ameesh.platform`, le banc n'affiche jamais rien et
    journalise ce qu'il aurait affiché."""
    env = {"AMEESH_NOTIFY_SEND_BIN": binary, "AMEESH_OSASCRIPT_BIN": binary}
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


def desktop_received(argv: list) -> tuple:
    """(titre, corps, urgence) reçus par le faux backend de cet OS.

    Linux : `notify-send --urgency=… -- titre corps`. macOS : `osascript -e
    'display notification "corps" with title "titre" [sound name "Basso"]'`
    (pas de niveau d'urgence : le son signale le critique, L66)."""
    if DESKTOP_MACOS:
        script = argv[-1]
        match = re.search(
            r'display notification ("(?:[^"\\]|\\.)*") with title '
            r'("(?:[^"\\]|\\.)*")', script)
        if match is None:
            raise AssertionError("script AppleScript sans notification : %r" % script)
        return (_applescript_text(match.group(2)), _applescript_text(match.group(1)),
                "critical" if 'sound name "Basso"' in script else "normal")
    urgency = "normal"
    for argument in argv:
        if argument.startswith("--urgency="):
            urgency = argument.split("=", 1)[1]
    return argv[-2], argv[-1], urgency


def _applescript_text(quoted: str) -> str:
    """Le texte d'une chaîne AppleScript entre guillemets (cf.
    `platform.notify._applescript_string`) : `\\`, `"` et les sauts de ligne."""
    escaped = {"n": "\n", "\\": "\\", '"': '"'}
    return re.sub(r"\\(.)", lambda m: escaped.get(m.group(1), m.group(1)),
                  quoted[1:-1])


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


#: #2451 : plafond §4 de fixture pour les tests en processus. Le plafond réel
#: vient du canon compilé (`work.build_limit`) ; sa lecture est couverte par
#: test_2451_canon_consommateurs, et un test en sous-processus pointe
#: `AMEESH_CANON` vers `mechanics_canon()`.
FIXTURE_BUILD_CAP = 2


def mechanics_canon(root: str) -> str:
    """Un canon Git minimal portant `processus/mecanique.yaml` (#2451).

    Le document et la fédération sont ceux de test_l1616_mechanics (plafond 2,
    familles claude/codex/deepseek) ; `members: []` en fait une racine de
    canon chargeable par `canon.load_configured` (AMEESH_CANON + _REF=HEAD)."""
    from .test_l1616_mechanics import FEDERATION, _Repo
    os.makedirs(root, exist_ok=True)
    _Repo(root, federation_text=FEDERATION + "members: []\n")
    return root


def patch_families(testcase, families=("claude", "codex", "deepseek")):
    """Épingle les familles compilées d'un banc sans canon Git (#2664).

    Le lecteur `review_lot.compiled_families` est patché pour la durée du test ;
    les témoins du lecteur construisent leur propre canon et ne passent pas
    par ici. Rend le patcheur, déjà arrêté au `cleanup` du test.
    """
    patcher = mock.patch("ameesh.review_lot.compiled_families",
                         return_value=tuple(families))
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return patcher


#: #3601 : globaux sensibles du banc. Un test peut les remplacer (mock.patch,
#: redirect_stdout…) à condition de les restaurer avant la fin de sa classe ;
#: le `tearDownClass` commun le vérifie et nomme la classe et le global
#: fautifs. #3600 : des patchs entrés depuis un thread et défaits dans le
#: désordre laissaient `ameesh.db.connect` remplacé ; l'erreur n'apparaissait
#: que 35 classes plus loin, en `setUpClass`.
SENSITIVE_GLOBALS = (
    ("ameesh.db.connect", db_mod, "connect", db_mod.connect),
    ("sys.stdout", sys, "stdout", sys.stdout),
    ("sys.stderr", sys, "stderr", sys.stderr),
)

#: #3625/#3641 : sous `python -m unittest -b`, unittest remplace `sys.stdout`
#: et `sys.stderr` par ses tampons avant `setUpClass` et `tearDownClass`. Leur
#: référence n'est prise par classe que si c'est le tampon du résultat actif ;
#: sinon la référence du chargement reste la vérité, pour qu'une fuite d'une
#: classe précédente (hors du mixin) reste visible. `ameesh.db.connect` garde
#: toujours celle du chargement.
SENSITIVE_GLOBALS_PER_CLASS = frozenset(("sys.stdout", "sys.stderr"))


def _describe_global(value) -> str:
    """Forme courte d'un global, pour le message d'échec."""
    if isinstance(value, types.FunctionType):
        return "fonction %s" % value.__qualname__
    return type(value).__name__


def _active_result_streams() -> dict:
    """#3641 : les flux du résultat tamponné de la classe en cours, s'il les
    possède réellement.

    Sous `-b`, `unittest` appelle `setUpClass` entre `TestResult._setupStdout`
    et `_restoreStdout` ; la trame de `TestSuite._handleClassSetUp` tient
    encore le `result`, et ses deux tampons sont exactement les flux courants.
    Seuls ceux-là sont pris par classe : un autre `StringIO` est une fuite
    d'une classe hors du mixin, que la garde #3601 doit continuer de voir
    (#3625 la masquait en le prenant pour la référence de la classe).
    """
    frame = sys._getframe(1)
    try:
        while frame is not None:
            result = frame.f_locals.get("result")
            if (getattr(result, "buffer", False)
                    and sys.stdout is getattr(result, "_stdout_buffer", None)
                    and sys.stderr is getattr(result, "_stderr_buffer", None)):
                return {"sys.stdout": sys.stdout, "sys.stderr": sys.stderr}
            frame = frame.f_back
    finally:
        del frame
    return {}


def capture_sensitive_globals(cls) -> None:
    """#3625/#3641 : fige la référence des globaux sensibles pour cette classe.

    Appelé en tête de `setUpClass`. Les flux ne sont pris par classe que
    lorsqu'ils sont le tampon du résultat tamponné actif (`-b`) : leur
    référence de classe distingue alors une fuite du tampon du lanceur, et la
    restauration remet ce tampon, pas le flux réel que
    `TestResult._restoreStdout` utiliserait ensuite (`AttributeError:
    getvalue`). Hors `-b`, ou quand la référence courante n'est pas ce
    tampon, la référence du chargement reste la vérité : une fuite d'une
    classe simple, hors du mixin, n'est plus masquée (#3641).
    """
    streams = _active_result_streams()
    cls._sensitive_globals_baseline = {
        nom: streams[nom]
        for nom, _module, _attribut, _expected in SENSITIVE_GLOBALS
        if nom in SENSITIVE_GLOBALS_PER_CLASS and nom in streams
    }


def _expected_sensitive_globals(cls):
    """Globaux attendus de `cls` : flux pris à sa tête, le reste au chargement."""
    baseline = cls.__dict__.get("_sensitive_globals_baseline") or {}
    return tuple(
        (nom, module, attribut, baseline.get(nom, attendu))
        for nom, module, attribut, attendu in SENSITIVE_GLOBALS
    )


def verify_sensitive_globals(cls) -> None:
    """#3601 : échoue dans `cls` si un global sensible n'est pas restauré.

    Le message nomme la classe et chaque global fautif. Les globaux sont
    restaurés avant l'échec : seule la classe qui a fui paie, les suivantes
    restent saines (#3600). Les flux sont restaurés vers la référence de la
    classe (#3625), qui est le tampon du lanceur sous `-b`.
    """
    fuites = []
    for nom, module, attribut, attendu in _expected_sensitive_globals(cls):
        actuel = getattr(module, attribut)
        if actuel is attendu:
            continue
        setattr(module, attribut, attendu)
        fuites.append((nom, attendu, actuel))
    if not fuites:
        return
    details = ", ".join(
        "%s (trouvé %s, attendu %s)" % (nom, _describe_global(actuel),
                                        _describe_global(attendu))
        for nom, attendu, actuel in fuites)
    raise AssertionError(
        "#3601 : %s.%s a laissé non restauré(s) : %s. Restauré(s) pour la "
        "classe suivante ; un patch a probablement été défait dans le désordre "
        "(entré depuis un thread), ou n'a pas été arrêté."
        % (cls.__module__, cls.__qualname__, details))


class SensitiveGlobalsMixin:
    """#3601 : vérifie à la fin de la classe que les globaux sensibles sont
    restaurés (`ameesh.db.connect`, `sys.stdout`, `sys.stderr`).

    Un test qui les remplace sans les restaurer fait échouer la classe
    fautive, en nommant le global ; ils sont restaurés avant l'échec pour que
    la classe suivante reste saine. Les classes de base voisines peuvent
    l'hériter ; une sous-classe qui redéfinit `setUpClass` ou `tearDownClass`
    doit appeler `super()` (les flux sont référencés à la tête de la classe,
    #3625).
    """

    @classmethod
    def setUpClass(cls) -> None:
        capture_sensitive_globals(cls)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            super().tearDownClass()
        finally:
            verify_sensitive_globals(cls)


#: #4740 : temps laissé à un fil créé par un test pour rendre la main après
#: ses cleanups. Un fil encore vivant au-delà a survécu à son test : son
#: écriture peut atterrir dans la capture `redirect_stdout` d'un test voisin.
THREAD_LEAK_GRACE_S = 1.0


def _live_threads_since(before: frozenset) -> list:
    """Les fils vivants créés depuis `before` (hors fil courant).

    `before` garde les objets `Thread`, pas leurs identifiants : un fil mort
    peut libérer son objet et un nouveau fil réutiliser son adresse ; la
    comparaison par objet ne confond pas les deux."""
    current = threading.current_thread()
    return [thread for thread in threading.enumerate()
            if thread is not current and thread.is_alive()
            and thread not in before]


def _thread_stack(thread) -> str:
    """Pile du fil, pour nommer l'endroit qui l'a lancé dans l'échec."""
    frame = sys._current_frames().get(thread.ident)
    if frame is None:
        return ""
    return "".join(traceback.format_stack(frame)[-6:])


class ThreadLeakMixin:
    """#4740 : échoue le test qui laisse un fil vivant après ses cleanups.

    Le témoin de #4736 (`test_l848_github_quota.JsonReportTest`) écrit « {} »
    depuis un fil : mal joint, ce fil survit à son test et son écriture tombe
    dans le `redirect_stdout` global d'un test voisin (« Extra data »). La
    classe vérifie, après les cleanups du test, qu'aucun fil créé pendant le
    test ne vit encore ; elle laisse `thread_leak_grace_s` aux fils qui
    s'arrêtent sur un tick (veilleurs) avant de nommer les survivants.

    Un test qui crée un fil doit le joindre avant sa fin, ou lui faire écrire
    un flux dédié. Une sous-classe qui redéfinit `setUp` doit appeler
    `super().setUp()` ; placer ce mixin avant la classe de base.
    """

    thread_leak_grace_s = THREAD_LEAK_GRACE_S

    def setUp(self) -> None:
        self._threads_before = frozenset(threading.enumerate())
        super().setUp()
        self.addCleanup(self._assert_no_surviving_thread)

    def _assert_no_surviving_thread(self) -> None:
        before = getattr(self, "_threads_before", None)
        if before is None:
            return
        deadline = time.monotonic() + max(0.0, float(self.thread_leak_grace_s))
        while True:
            survivors = _live_threads_since(before)
            if not survivors:
                return
            left = deadline - time.monotonic()
            if left <= 0:
                break
            for thread in survivors:
                thread.join(min(0.05, max(0.0, left)))
        details = ", ".join(
            "%s (daemon=%s, ident=%s)" % (thread.name, thread.daemon, thread.ident)
            for thread in survivors)
        stacks = "\n".join(_thread_stack(thread) for thread in survivors)
        raise AssertionError(
            "#4740 : %s.%s a laissé %d fil(s) vivant(s) après ses cleanups : %s. "
            "Joindre le fil avant la fin du test, ou lui faire écrire un flux "
            "dédié : un fil survivant écrit dans la capture stdout d'un test "
            "voisin.\n%s"
            % (type(self).__module__, type(self).__qualname__,
               len(survivors), details, stacks))


def _bench_upkeep(db) -> None:
    """Purge les schémas t_* orphelins, une fois par processus de suite.

    Le banc est partagé par tous les agents de l'hôte (`scripts/pg-up.sh`) :
    une exécution tuée laisse son schéma. La passe est bornée (âge, sessions,
    verrou court) et ne fait jamais échouer un test. #3735 B/P1 : la passe
    **ordinaire** ne purge que les schémas dont le marqueur prouve un
    propriétaire mort ; les schémas **sans marqueur** (suite legacy) restent
    gardés — leur processus Python survit au redémarrage du postmaster et le
    code ne peut pas prouver son arrêt ; leur purge est un geste d'opérateur
    explicite (`--admit-unmarked`), après arrêt de toute suite.
    """
    global BENCH_UPKEEP_DONE
    if BENCH_UPKEEP_DONE:
        return
    if os.environ.get("AMEESH_BENCH_UPKEEP", "1").lower() in ("0", "false", "no", "off"):
        BENCH_UPKEEP_DONE = True
        return
    try:
        result = bench_mod.purge_orphans(db)
    except Exception:  # noqa: BLE001 — un ménage de banc ne juge jamais un test
        return
    BENCH_UPKEEP_DONE = True
    if result.dropped:
        print("banc : %d schéma(s) t_* orphelin(s) purgé(s)" % len(result.dropped),
              file=sys.stderr)


class PgTestCase(SensitiveGlobalsMixin, unittest.TestCase):
    """Base : un schéma migré, un bac à sable disque, des utilitaires CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.schema = "t_%s_%s" % (
            cls.__name__.lower().replace("test", "")[:18] or "x",
            uuid.uuid4().hex[:8],
        )
        cls.tmp = tempfile.mkdtemp(prefix="agent-mesh-test-")
        # #3735 : `tearDownClass` n'est pas appelé quand `setUpClass` échoue
        # (migration en erreur, kill) — le schéma resterait à jamais. Le
        # cleanup de classe est enregistré AVANT la migration ; le drop de
        # `tearDownClass` reste le chemin normal. `_schema_dropped` est remis à
        # zéro sur CETTE classe (jamais hérité) : le cleanup passé après
        # `tearDownClass` ne rouvrira pas la base fermée (B/P1 pilote #3735).
        cls._schema_dropped = False
        cls.addClassCleanup(cls._drop_schema)
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
        _bench_upkeep(cls.db)
        # #3752 : clone du gabarit migré une fois par base, sinon migrate()
        gabarit.preparer(cls.db, cls.schema)
        # #3735 : preuve de propriété durable du schéma — le processus de la
        # suite vit, même au repos entre deux transactions sans verrou ni
        # requête nommante ; sa mort libère la purge. Best-effort : sans
        # commentaire, l'âge de 6 h protège encore le schéma.
        try:
            bench_mod.claim_owner(cls.db, cls.schema)
        except Exception:  # noqa: BLE001 — jamais un échec de test pour la preuve
            pass
        if CARTE:
            try:
                cls._carte_tables = table_stats(cls.db, cls.schema)
            except Exception:
                cls._carte_tables = None

    @classmethod
    def _drop_schema(cls) -> None:
        """Supprime le schéma une fois, puis ferme la connexion de la classe.

        Le cleanup de classe de `unittest` passe APRÈS `tearDownClass` : sans
        drapeau, il exécuterait un DROP sur une base déjà fermée — le pilote
        psycopg rouvrirait une connexion (`_live`) jamais refermée, une fuite
        par classe (B/P1 pilote #3735, CI h-30). Le drapeau et la connexion
        sont lus dans ``cls.__dict__`` pour ne pas hériter de la classe
        parente.
        """
        if cls.__dict__.get("_schema_dropped", False):
            return
        db = cls.__dict__.get("db")
        if db is None:
            return
        try:
            db.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % cls.schema)
        except Exception:  # noqa: BLE001 — cleanup : ne masque pas l'erreur d'origine
            pass
        cls._schema_dropped = True
        try:
            db.close()
        except Exception:  # noqa: BLE001 — une fermeture ne juge pas la classe
            pass

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            noter_tables(cls)
            try:
                cls._drop_schema()
            finally:
                shutil.rmtree(cls.tmp, ignore_errors=True)
        finally:
            # #3601 : en dernier, même si le ménage de la base a échoué.
            super().tearDownClass()

    # -- utilitaires -------------------------------------------------------
    def setUp(self) -> None:
        """Chaque test part d'une base et d'un disque propres."""
        cap = mock.patch.object(work_mod, "build_limit", return_value=FIXTURE_BUILD_CAP)
        cap.start()
        self.addCleanup(cap.stop)
        self.db.execute(
            "TRUNCATE agent_registry, key_holders, key_events, agent_mailbox, mesh_approvals, "
            "work_items, work_item_events, work_item_milestones, work_packages, "
            "thread_index, notify_liveness, notify_briefs, estimate_references, "
            "harness_adoptions, harness_versions, repository_footprint_policies "
            "RESTART IDENTITY CASCADE")
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        for path in (self.state, self.v0state, self.conf):
            os.makedirs(path, exist_ok=True)
        # #2664 : ces bancs n'ont pas de canon Git configuré ; les familles
        # compilées y sont épinglées aux trois familles déclarées, comme sur un
        # hôte équipé. Le lecteur du canon a ses propres témoins, et un test
        # d'échec fermé défait ce patch localement.
        patch_families(self)


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

    def connect(self, schema: str | None = None, **overrides):
        """Connexion au schéma du test ; `overrides` remplace des champs de la
        configuration sur cette connexion (ex. `statement_timeout_ms`)."""
        return db_mod.connect(dataclasses.replace(
            self.cfg, schema=schema or self.schema, **overrides))

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
    def wait_for(self, predicate, timeout: float = 10.0, interval: float = 0.1,
                 msg=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        # #2497 : `msg` (appelable) joint l'état observé à l'échec, sans quoi
        # un rouge de CI sous charge reste muet sur sa cause
        self.fail("condition non remplie après %ss%s"
                  % (timeout, "\n" + msg() if msg else ""))

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
