# SPDX-License-Identifier: AGPL-3.0-only
"""Échecs de tests remontés en temps réel au courrier de l'agent du lot (L161c).

    ameesh ci status <lot> [--limit N] [--json]
    ameesh ci run <lot> [--avis N] [--exception incident|unblock --reason …]
                        [--full --reason …] [--sha S] [--force] [--dry-run] [--json]
    ameesh ci cancel <lot> [--all] [--dry-run] [--json]

`ci status` lit les exécutions suivies ; `ci run` est le déclencheur du gel
(#1343, canon §3 : la relecture passe avant la CI) — il vérifie le gel, les
verdicts requis et la composition de la PR, puis pose sur elle l'étiquette
`ci-lot-<40hex>` / `ci-full-<40hex>` qui déclenche `tests.yml`
(`pull_request` `types: [labeled]`) sur la tête gelée ; `ci cancel` annule les
runs périmés d'un lot. For repositories without the label trigger, `--full`
uses the declared/discovered suites and the repository's own dispatch planner
(#5145), with `expected_sha` pinned to the requested head.

#2333 (canon §11 bis, décision owner 18/10/2026 « tout passe par paquet ») :
`ci run` en mode lot (`ci-lot-<sha>`) est refusé entre deux paquets, sauf
`--full` (la suite du paquet) ou exception `incident|unblock` motivée et
journalisée dans la note du lot (`packet_guard`).

Étude L161 (docs/design/etudes/l161-tests-par-impact.md, section 3), voie A :
pendant les tests, chaque part de la CI publie un check run « tests (flux) »
(`scripts/ci-flow.py publish`) dont la sortie porte, en JSON lignes, chaque
test en échec au schéma `ameesh-test-event/1` (L161b). `ameesh notify`, sur
l'hôte que désigne `github.host` (comme les issues des lots, L126), fait à
chaque passage un relevé (`Watcher.tick`) :

1. les exécutions en cours ou en attente des workflows suivis de chaque dépôt
   de `github.projects` (une requête par dépôt) ; les workflows suivis d'un
   dépôt sont ceux que déclare `github.projects.<projet>.workflows`, sinon —
   quand le dépôt ne publie pas `tests.yml` — les workflows actifs découverts
   (`actions/workflows`) qui ont la forme d'une suite (#1483 : les workflows
   d'exploitation — déploiement, sonde — ne rendent pas de verdict de bruit ;
   s'ils sont écartés, ils sont journalisés) : un dépôt dont les suites portent
   d'autres noms (nexlink : `backend-suite.yml`, `sql-invariant.yml`…) n'est
   plus ignoré en silence (#1110). Chaque exécution porte son fichier de
   workflow, pour que le verdict nomme la suite concernée ; chacune est
   rattachée à un lot : par
   sa branche (`work_items.branch`, L118), sinon par la ligne
   `ameesh-work: <id>` de son commit de tête (une fusion dans main) ; une
   exécution courte peut naître et finir entre deux passages sans jamais être
   vue « en cours » : un rattrapage borné des exécutions récemment terminées
   les découvre, rend leur verdict et réveille l'agent — une seule fois, et
   jamais pour une tête déjà remplacée (sauf sur une branche de base, où
   chaque tête porte un lot différent : #1110, #1483) ; une ligne restée sans
   lot (branche du lot posée après sa première vue, #1321) est rattrapée par
   ce même relevé, verdict compris, tant qu'elle est encore récente (#2021) ;
   tous les états non conclus sont lus, `pending` compris (#1161) : une
   exécution qui attend un groupe de concurrence derrière une exécution
   supplantée n'a sinon ni ligne, ni verdict, ni réveil ;
2. une nouvelle tête sur la branche d'un lot remplace l'exécution précédente
   (`superseded`), en cours ou déjà annulée : les messages de CI de l'ancienne tête que l'agent n'a
   pas encore reçus sont retirés sans être montrés ; un message déjà remis
   reste, car l'agent a pu agir ; les exécutions non démarrées (`queued`,
   `pending`) de la même PR, du même dépôt source et du même workflow
   (périmètre prudent du veilleur), dont l'essai est prouvé antérieur à celui
   de la tête courante sont annulées : jamais sur `main`, sur `release/…`, sur la tête
   courante, ni sur un essai plus récent (un rerun garde son `id` et augmente
   `run_attempt` ; sans date d'essai, rien n'est annulé) (#1161) ; une
   exécution de PR restée `pending` plus de cinq minutes est signalée à
   l'assigné ;
3. pour chaque exécution suivie, les check runs « tests (flux) » de sa tête :
   chaque échec nouveau est enregistré (`ci_test_failures`, 0055) ; un dépôt
   qui ne publie pas ce flux (Nexlink, qui lance `node --test`) livre ses
   échecs en TAP : quand une exécution conclut rouge sans aucun événement, les
   logs des jobs en échec sont lus et leurs lignes `not ok` deviennent les
   tests en échec (#3783). Un job rouge sans `not ok` reste sans nom de test —
   une panne d'installation n'est pas un test négatif (canon §3) ;
4. une exécution qui n'est plus en cours est conclue : verdict ;
5. une exécution relancée (`gh run rerun`, geste courant sous saturation)
   garde son `run_id` et change d'`run_attempt` (#901) : si GitHub la dit de
   nouveau en cours, sa ligne est rouverte et suivie jusqu'à son verdict ; un
   échec de l'attempt précédent qui n'est pas re-vu à la relance n'est plus
   imputé à l'exécution, et un échec qui revient compte comme nouveau. Chaque
   ligne publiée par `scripts/ci-flow.py` porte son attempt : tant que la
   relance n'a pas republié son check run, celui de l'attempt précédent reste
   le dernier de la tête et n'est pas relu ; une ligne sans marque vaut
   attempt 1 et n'est jamais imputée à une relance.

Courrier, de `ameesh` à l'assigné du lot, rangé dans le fil du lot, selon
L125 :

* premier échec de la tête : **urgent**, un seul par exécution (la CI seule ne
  peut pas déclencher `turn_churn`) ; passif (`ci_passif`) tant qu'aucune
  exécution conclue de la base n'a été relevée, car l'échec est peut-être
  hérité ;
* échecs suivants : un événement ordinaire par relevé (regroupé par la
  fenêtre de 90 s du courrier), passif (`info`, #5044) une fois la première
  alerte partie : le verdict les reprend tous ;
* verdict (vert, ou rouge avec la liste) : un événement ordinaire (#514 : la
  fin de CI réveille l'agent, elle ne perce pas le tour en cours ; le rouge
  perce déjà par la première alerte d'échec) ; passif (#5044) tant qu'un autre
  workflow de la même tête tourne, si c'est un vert ou un rouge déjà signalé :
  seul le dernier réveille, et si ce dernier est annulé (donc sans verdict), un
  avis réveille à sa place ; le courrier et la référence qui
  le rend vérifiable (`ci_runs.verdict_message_id`) sont publiés dans la même
  transaction (#795 N2) ; rien pour une exécution annulée ou remplacée ; le
  verdict nomme le workflow et, quand d'autres workflows de la même tête
  tournent encore, le dit — un vert de `backend-suite.yml` n'est pas « la CI
  est verte » ;
* **rouge hérité** : un test qui échouait déjà à la dernière exécution conclue
  de la base (la cible de la PR, sinon la base du dépôt —
  `github.projects.<projet>.base`, `main` par défaut, `develop` pour Nexlink :
  #1483 ; pour une base, l'exécution précédente de la même suite) n'est jamais
  imputé à l'auteur : pas d'urgent, une ligne dans le verdict ;
* **échec d'une branche de base sans lot** (une fusion de paquet ne porte
  souvent pas la ligne `ameesh-work:`) : au pilote du projet, l'orchestrateur
  déclaré au canon (`alert_briefs.project_pilots`) — plus jamais « (no
  recipient) » (#2935). La tête de chaque base est par ailleurs relue à chaque
  passage, même au-delà de la fenêtre de rattrapage : le dernier résultat
  **utile** (`success`/`failure`) par workflow suivi, relances terminées
  comprises et annulations ignorées, ferme `ci_main_red` au premier vert
  enregistré de la même base, sans attendre la prochaine exécution de cette
  base (#2935).

Une exécution rouge sur une branche de base lève aussi l'alerte `ci_main_red`
(`exploitation`, poussée par `ameesh notify` au responsable humain) : #846,
dès qu'un échec est relevé sur une exécution de main encore en cours, sans
attendre ses jobs agrégateurs — un job rouge sans événement de test publié
(part `job`) compte aussi.

Le réessai d'un échec (étude : « premier échec confirmé ») n'est pas encore
émis par le flux : un échec compte dès sa première occurrence.
"""
from __future__ import annotations

import contextlib
import json
import re
import secrets
import sys
import time
from datetime import datetime
from urllib.parse import quote

from . import packet_guard, role_run, storage

#: workflows suivis par défaut (nom du fichier), quand le dépôt publie le
#: workflow des tests L161 : comportement historique, compatible ameesh.
#: Un dépôt qui ne le publie pas fait l'objet d'une découverte (#1110).
WORKFLOWS = ("tests.yml",)
#: découverte des workflows d'un dépôt : durée de validité du cache, et délai
#: avant de réessayer après un échec de l'API
DISCOVERY_TTL = 3600.0
DISCOVERY_RETRY = 300.0
#: #1483 : un workflow *découvert* n'est suivi que s'il a la forme d'une suite
#: de tests. Sans liste déclarée, la découverte suivait tous les workflows
#: actifs : sur un dépôt comme Nexlink (déploiements, sondes, synchronisation),
#: chaque exécution terminée rendait un verdict de bruit à l'auteur du lot. La
#: liste déclarée (`github.projects.<projet>.workflows`) reste le moyen
#: explicite de suivre un workflow au nom inhabituel, et les workflows écartés
#: sont journalisés une fois — aucun n'est ignoré en silence.
SUITE_RE = re.compile(r"(?:^|[-_.])(?:suites?|tests?|invariants?|e2e)(?:[-_.]|$)",
                      re.IGNORECASE)
#: états GitHub d'une exécution pas encore conclue que le relevé lit : une
#: exécution en attente d'un runner est vue dès la poussée, avant que
#: l'exécution qu'elle remplace soit annulée. `pending` — l'exécution attend
#: son groupe de concurrence (#1161) — est un état non conclu comme les
#: autres : sans lui, un run bloqué derrière une exécution supplantée reste
#: invisible, et n'a ni ligne, ni supersession, ni verdict, ni réveil. Les
#: filtres `requested` et `waiting` sont acceptés par l'API (relevé du
#: 14/10/2026 : aucun run dans ces états) ; ils sont lus comme les autres.
LIVE_STATUSES = ("in_progress", "queued", "requested", "waiting", "pending")
#: états GitHub d'une exécution qui n'a pas démarré : les seuls que le relevé
#: annule quand une tête plus récente l'a supplantée (#1161)
CANCELLABLE_STATUSES = ("queued", "pending")
#: délai au-delà duquel une exécution de PR restée `pending` est signalée à
#: l'assigné du lot (#1161)
PENDING_ALERT = 300.0
#: rattrapage borné des exécutions terminées entre deux passages (#1110) : on
#: ne remonte pas plus loin que cette fenêtre, pour ne pas rendre de verdicts
#: anciens après une longue interruption du veilleur
CATCHUP_WINDOW = 3600.0
#: préfixe du nom des check runs publiés par `scripts/ci-flow.py publish`
CHECK_PREFIX = "tests (flux)"
SCHEMA = "ameesh-test-event/1"
#: branches de base par défaut : une exécution n'y est jamais remplacée, et ses
#: échecs servent de référence au rouge hérité. Un dépôt dont la base n'est pas
#: `main` — Nexlink, dont la base est `develop` — la déclare dans
#: `github.projects.<projet>.base` (#1483).
BASE_BRANCHES = ("main",)
#: #3344 : le workflow qui publie la carte de mesure (L161e), et la période
#: de relecture de sa dernière exécution réussie — une requête par heure et par
#: dépôt suffit à une alerte dont le seuil est de 48 h
MAP_WORKFLOW = "carte.yml"
MAP_READ_S = 3600.0
#: #1483 : taille des pages de listes d'exécutions du veilleur (l'API GitHub
#: plafonne à 100). À 50, une file de plus de 50 exécutions — tous workflows
#: confondus — pouvait cacher une exécution suivie, que seul le rattrapage
#: rendait, une heure plus tard.
LIST_PER_PAGE = 100
#: #2938 : pages lues au plus pour la tête d'une base. La première suffit
#: quand le dernier `success`/`failure` de chaque workflow suivi y est ; des
#: annulations plus récentes peuvent le cacher au-delà (N1/N2 de la relecture
#: #2935). Le plafond borne le coût d'une branche qui n'a jamais ce résultat.
HEAD_LIST_MAX_PAGES = 5
ECHECS = ("echec", "erreur")
#: conclusion GitHub → état de `ci_runs`
_CONCLUSIONS = {"success": "success", "neutral": "success", "skipped": "success",
                "cancelled": "cancelled", "stale": "cancelled"}


def conclusion_status(run: dict | None) -> str:
    """L'état `ci_runs` d'une exécution GitHub, ou "" si elle n'est pas conclue.

    Un relevé ne doit lire les logs TAP d'un job qu'une fois l'exécution
    conclue rouge (#3783) : pendant qu'elle tourne, la liste des jobs coûte
    un appel par passage sans rien apporter au verdict."""
    if not run or (run.get("status") or "") != "completed":
        return ""
    return _CONCLUSIONS.get(run.get("conclusion") or "", "failure")


#: #846 : conclusions d'un check run de job qui disent un main rouge, même
#: sans événement de test publié (panne d'installation, délai dépassé)
FAILED_JOB_CONCLUSIONS = ("failure", "timed_out")
#: #846 : part des échecs de job relevés ainsi (`ci_test_failures.part`)
JOB_PART = "job"
#: #3783 : un échec de test lu dans le log d'un job (`node --test`, TAP) —
#: le flux JSON de `scripts/ci-flow.py` n'existe que pour ameesh ; un dépôt
#: qui n'a pas ce publisher (Nexlink) ne laissait aucun nom de test, et un
#: rouge se lisait « no failed test seen » (résultat non concluant, canon §3).
#: Une ligne TAP est préfixée par l'horodatage du log GitHub (`…Z `), et un
#: sous-test est indenté : la reconnaissance est ancrée au début de la ligne
#: (après ce préfixe), sinon « error: expected not ok 1 - exemple » deviendrait
#: un faux échec. Le numéro est obligatoire (node numérote chaque test), ce qui
#: écarte les lignes d'extrait sans numéro.
TAP_RESULT_RE = re.compile(r"^\s*(not ok|ok)\s+\d+\s*(?:-\s*)?(.*\S)\s*$")
#: une ligne `not ok … # TODO` / `# SKIP` est une attente, pas un échec
TAP_DIRECTIVE_RE = re.compile(r"\s+#\s*(?:TODO|SKIP)\b", re.IGNORECASE)
#: échappements TAP (`\#`, `\\`) : le nom affiché est celui du test
TAP_ESCAPE_RE = re.compile(r"\\([#\\])")
#: séquences ANSI du log brut (`gh api` refuse de les imprimer sans option)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
#: horodatage du log GitHub Actions, préfixe de chaque ligne
LOG_TS_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z")
#: lignes de diagnostic gardées dans l'extrait d'un échec TAP
TAP_EXCERPT_LINES = 20
#: lignes d'extrait au plus dans un message
EXCERPT_LINES = 40
#: échecs détaillés au plus dans un message (les autres : leur nom)
DETAILED = 3
STATUS_SCHEMA = "ameesh-ci-status/1"
#: #845 (arbitrage A du pilote, 14/10) : le relevé « runner busy sans job »
#: coûte un appel par exécution non conclue (en plus des listes par état et de
#: celle des runners) : il est mené au plus toutes les `GUARD_SCAN_S` secondes,
#: jamais à chaque passage. La fenêtre est écrite dans la base avant le premier
#: appel HTTP (`ci_guard_scan`) : deux `notify --once` concurrents, ou deux
#: hôtes, ne la mènent pas deux fois. À 600 s et 66 appels au plus par relevé,
#: cette garde seule consomme au plus 396 appels/h et par dépôt (hors appels du
#: suivi des échecs) ; la durée d'un relevé n'est pas bornée globalement, donc
#: aucune détection en « <= 10 min » n'est promise.
GUARD_SCAN_S = 600.0
#: #845 (arbitrage A) : au-delà de ce nombre d'exécutions non conclues, le
#: relevé est trop large : rien n'est publié et l'incomplétude est journalisée
#: (`too_many_runs`), plutôt que de risquer une fausse panne (leçon de la mesure
#: du 13/10, #828) ou de dépasser le budget d'appels.
GUARD_SCAN_MAX_RUNS = 60
#: #845 : taille de page des listes du relevé (runners et jobs) ; les
#: exécutions sont listées par le relevé lui-même, sans le filtre de workflow
#: ni la page de 50 du suivi des échecs (un runner peut tenir un job de
#: `carte.yml`, et 43 exécutions non conclues ont été mesurées le 13/10)
GUARD_PER_PAGE = 100
#: #845 (revue N/P2 2328 ; arbitrage A) : budget d'appels GitHub d'un relevé de
#: la garde — une liste d'exécutions par état non conclu (`LIVE_STATUSES`,
#: #1161 en lit cinq), un appel par exécution non conclue (chacune est relue :
#: un run resté `queued` peut porter un job déjà `in_progress`, R1 cx3 B1), et
#: la liste des runners. Le plafond couvre exactement `GUARD_SCAN_MAX_RUNS`
#: exécutions : len(LIVE_STATUSES) + 60 + 1. Au-delà, le relevé est déclaré
#: incomplet et RIEN n'est publié : un relevé partiel ne vaut pas un relevé
#: complet, et les relevés précédents vieillissent jusqu'à ce que la fraîcheur
#: (`exploitation.CI_GUARD_FRESH_S`, 1800 s) les clôt. Le nombre d'appels
#: réellement consommés est journalisé dans `ci_guard_scan`.
GUARD_SCAN_MAX_CALLS = len(LIVE_STATUSES) + GUARD_SCAN_MAX_RUNS + 1


class _GuardBudget(RuntimeError):
    """#845 : le budget d'appels du relevé de la garde est épuisé."""


class CiError(RuntimeError):
    """Lot inconnu, réponse de GitHub illisible : message actionnable."""


# --------------------------------------------------------------------------
# lecture des check runs
# --------------------------------------------------------------------------

def workflow_of(path: str | None) -> str:
    """Le fichier de workflow d'une exécution (`…/backend-suite.yml`), ou ""."""
    return str(path or "").rsplit("/", 1)[-1].strip()


def _iso_ts(text: str | None) -> float | None:
    """Un horodatage ISO 8601 GitHub en secondes epoch, ou `None`."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def event_attempt(ev: dict) -> int:
    """L'attempt qui a publié une ligne d'échec (#901) ; 1 si la ligne n'a pas
    de marque (publiée avant le marquage) ou si elle est illisible : elle n'est
    jamais imputée à une relance."""
    try:
        return max(1, int(ev.get("attempt") or 1))
    except (TypeError, ValueError):
        return 1


def updated_ts(run: dict) -> float | None:
    """L'horodatage de dernière mise à jour d'une exécution (ISO 8601), ou
    `None` s'il est absent ou illisible."""
    return _iso_ts(run.get("updated_at") or run.get("created_at") or "")


def attempt_ts(run: dict) -> float | None:
    """L'horodatage du dernier essai d'une exécution (ISO 8601), ou `None` :
    l'âge d'une exécution qui attend, et la seule preuve d'antériorité entre
    deux exécutions (#1161). `run_started_at` date l'essai courant — un rerun
    garde son `run_id`, augmente `run_attempt` et repart à cette date ;
    `created_at` ne date que le premier essai, donc pour un rerun sans
    `run_started_at` la date est incertaine et rend `None`."""
    if int(run.get("run_attempt") or 1) > 1:
        return _iso_ts(run.get("run_started_at") or "")
    return _iso_ts(run.get("run_started_at") or run.get("created_at") or "")


def protected_branch(branch: str) -> bool:
    """Une branche que le relevé ne supplante ni n'annule jamais : branche de
    base (`main`) ou branche de release (`release/…`, canon §2) — #1161."""
    branch = (branch or "").strip()
    return branch in BASE_BRANCHES or branch.startswith("release/")


def pull_request_run(run: dict) -> bool:
    """Une exécution de PR : les seules que le relevé annule ou signale
    (#1161) — une poussée sur la branche de base d'un autre dépôt (`develop`)
    ou sur une étiquette n'est jamais une PR."""
    return bool(run.get("pull_requests")) \
        or (run.get("event") or "").startswith("pull_request")


def pr_identity(run: dict) -> tuple[str, tuple[int, ...]] | None:
    """(dépôt source, numéro(s) de PR) d'une exécution de PR, ou `None` sans
    preuve. Le groupe de concurrence des workflows d'ameesh est
    `format('pr-{0}', pull_request.number)` : deux PR de même nom de branche,
    dans deux dépôts source distincts, ne se supplantent pas (#1161)."""
    prs = [pr for pr in (run.get("pull_requests") or ()) if isinstance(pr, dict)]
    numbers = tuple(sorted({int(pr["number"]) for pr in prs
                            if pr.get("number") is not None}))
    if not numbers:
        return None
    source = str((run.get("head_repository") or {}).get("full_name") or "").strip()
    if not source:
        for pr in prs:
            source = str(((pr.get("head") or {}).get("repo") or {}).get("full_name")
                         or "").strip()
            if source:
                break
    if not source:
        return None
    return (source, numbers)


def run_group(run: dict) -> tuple | None:
    """Le périmètre prudent d'annulation : (dépôt source, PR, workflow).

    Les groupes GitHub peuvent être partagés entre workflows d'un dépôt.
    Exiger le même workflow est une garde volontaire du veilleur, qui ne
    reconstitue pas l'expression `concurrency.group`. `None` sans preuve :
    le relevé n'annule alors rien (#1161).
    """
    identity = pr_identity(run)
    if identity is None:
        return None
    path = str(run.get("path") or "").strip()
    return identity + (path or workflow_of(run.get("path")),)


def proven_older(candidate: dict, successor: dict) -> bool:
    """Le candidat est-il **prouvé** antérieur au successeur (#1161) ? L'ordre
    des `id` ne le prouve pas : un rerun garde son `id` et augmente
    `run_attempt`, si bien qu'une exécution d'`id` plus petit peut être le
    dernier essai lancé. Seules deux dates d'essai strictement ordonnées le
    prouvent ; une date incertaine conserve l'exécution."""
    before, after = attempt_ts(candidate), attempt_ts(successor)
    return before is not None and after is not None and before < after


def proof_of(run: dict, successor: dict) -> dict:
    """La preuve d'annulation d'une exécution, calculée sur la liste du relevé
    (#1161, R1) : l'essai qualifié, sa tête, son groupe de concurrence et la
    date d'essai du successeur. `cancel_run` la confronte à la relecture
    fraîche avant tout POST."""
    return {"run_attempt": int(run.get("run_attempt") or 1),
            "head_sha": run.get("head_sha") or "",
            "group": run_group(run),
            "successor_ts": attempt_ts(successor)}


def proof_holds(run: dict, proof: dict) -> bool:
    """La relecture fraîche confirme-t-elle la preuve calculée sur la liste
    (#1161, R1) ? Un essai qui a changé (`run_attempt`), une autre tête, un
    autre groupe de concurrence ou une date d'essai qui n'est plus antérieure
    au successeur rendent faux : l'exécution est conservée."""
    if int(run.get("run_attempt") or 1) != int(proof.get("run_attempt") or 0):
        return False
    if (run.get("head_sha") or "") != (proof.get("head_sha") or ""):
        return False
    if run_group(run) != proof.get("group"):
        return False
    ts, successor_ts = attempt_ts(run), proof.get("successor_ts")
    return ts is not None and successor_ts is not None and ts < successor_ts


def events_of(check_runs, run_id: int, attempt: int | None = None) -> list[dict]:
    """Les événements `test` en échec, pour l'exécution `run_id`, portés par
    la sortie des check runs « tests (flux) » d'une tête.

    #901 : `ci-flow.py` marque chaque ligne publiée de son attempt GitHub.
    Une relance garde le `run_id` : pendant qu'elle est en file, le dernier
    check run publié est encore celui de l'attempt précédent. `attempt=None`
    (le relevé) rend les lignes de tous les attempts, chacune avec sa
    provenance (`event_attempt`) : le relevé les enregistre sous leur propre
    attempt et n'impute à la relance que les lignes de l'attempt courant. Un
    entier ne rend que les lignes de cet attempt."""
    out, seen = [], set()
    for check in check_runs or ():
        if not str(check.get("name") or "").startswith(CHECK_PREFIX):
            continue
        text = ((check.get("output") or {}).get("text")) or ""
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or ev.get("v") != 1 or ev.get("type") != "test":
                continue
            if ev.get("statut") not in ECHECS or not ev.get("test"):
                continue
            if ev.get("execution") is not None and str(ev["execution"]) != str(run_id):
                continue
            if attempt is not None and event_attempt(ev) != int(attempt):
                continue
            # la clé porte l'attempt : un check de l'attempt précédent et celui
            # de la relance peuvent coexister dans la réponse, aucun ne masque
            # l'autre (#901)
            key = (event_attempt(ev), str(ev.get("part") or ""), ev["test"])
            if key not in seen:
                seen.add(key)
                out.append(ev)
    return out


def _log_line(line: str) -> str:
    """Une ligne de log GitHub : sans séquence ANSI ni préfixe d'horodatage.

    Le log brut de l'API (`actions/jobs/<id>/logs`) commence chaque ligne par
    `2026-…Z ` ; `gh run view --log-failed` y ajoute le job et l'étape, avant
    ce même horodatage. On ne retire l'horodatage que s'il est dans le préfixe
    (jamais un horodatage qui ferait partie du nom d'un test)."""
    line = ANSI_RE.sub("", str(line or "")).replace("\ufeff", "").rstrip("\r")
    match = LOG_TS_RE.search(line)
    if match is not None and match.start() <= 160 \
            and "not ok" not in line[:match.start()]:
        line = line[match.end():]
        if line.startswith(" "):
            line = line[1:]
    return line


def _tap_failed_at(line: str) -> str | None:
    """L'horodatage GitHub d'une ligne de log, ou None (ligne sans préfixe)."""
    match = LOG_TS_RE.search(str(line or ""))
    if match is None:
        return None
    # Postgres garde la microseconde ; le log GitHub en porte sept.
    return re.sub(r"(\.\d{6})\d+Z$", r"\1Z", match.group(0))


def tap_failures(text: str) -> list[dict]:
    """Les tests en échec lus dans un log TAP de node (#3783).

    Rend `[{test, excerpt, failed_at, module}]`, dans l'ordre du log. Un
    dépôt qui ne publie pas le flux JSON d'`scripts/ci-flow.py` (Nexlink)
    n'avait aucun nom de test : son rouge se lisait « no failed test seen »
    et passait pour non concluant (canon §3). Les lignes `not ok` de
    `node --test` sont la preuve négative ; `# TODO` et `# SKIP` restent des
    attentes, et le numéro de test est obligatoire.

    La reconnaissance est ancrée au début de la ligne TAP (après le préfixe
    GitHub) : une ligne de console ou d'assertion qui contient « not ok 1 - … »
    n'est pas un test. Le bloc de diagnostic YAML (`---` … `...`) qui suit un
    résultat est traversé sans y lire de test : un extrait qui cite une ligne
    TAP numérotée ne doit pas devenir une preuve négative (relecture #3783).
    """
    out: list[dict] = []
    seen: dict[str, dict] = {}
    current: dict | None = None
    in_yaml = False
    after_result = False
    for raw in str(text or "").splitlines():
        line = _log_line(raw)
        stripped = line.strip()
        if in_yaml:
            if stripped == "...":
                in_yaml = False
                current = None
                continue
            if current is not None:
                location = re.match(r"\s*location:\s*'?(.*?)'?\s*$", line)
                if location:
                    current["module"] = location.group(1).strip()
                if len(current["excerpt"]) < TAP_EXCERPT_LINES:
                    current["excerpt"].append(line.rstrip())
            continue
        match = TAP_RESULT_RE.match(line)
        if match is not None:
            after_result = True
            current = None
            if match.group(1) != "not ok":
                continue
            name = TAP_ESCAPE_RE.sub(r"\1", match.group(2).strip())
            if not name or TAP_DIRECTIVE_RE.search(match.group(2)) or name in seen:
                continue
            current = {"test": name, "excerpt": [], "failed_at": _tap_failed_at(raw),
                       "module": ""}
            seen[name] = current
            out.append(current)
            continue
        if stripped == "---" and after_result:
            after_result = False
            in_yaml = True
            if current is not None:
                current["excerpt"].append(line.rstrip())
            continue
        # Toute autre ligne (préambule, `# Subtest:`, sortie de console) ne
        # peut pas être un résultat TAP ; elle ferme l'attente du bloc.
        after_result = False
        if current is not None and stripped.startswith(("# Subtest:", "ok ", "Bail out!")):
            current = None
    for entry in out:
        entry["excerpt"] = "\n".join(entry["excerpt"]).strip()
    return out


def failed_jobs_of(jobs) -> list[str]:
    """#846 : les jobs déjà en échec d'une **tentative** d'exécution, d'après
    la liste des jobs de cette tentative (`actions/runs/<id>/attempts/<n>/jobs`)
    — les seuls publiés même quand aucun événement de test ne l'est (panne
    d'installation, délai dépassé). La tentative est explicite (#901 B1) : les
    check runs d'une tête ne portent que le `run_id`, celui d'une tentative
    antérieure encore publié serait imputé à la relance."""
    out = set()
    for job in jobs or ():
        name = str(job.get("name") or "").strip()
        if not name:
            continue
        if (job.get("conclusion") or "") not in FAILED_JOB_CONCLUSIONS:
            continue
        out.add(name)
    return sorted(out)


def _base_set(branches) -> frozenset:
    """Les branches de base, en minuscules : la comparaison d'une branche
    GitHub ne dépend pas de la casse (#1483)."""
    return frozenset(str(b).casefold() for b in (branches or BASE_BRANCHES))


def bases_for_repo(settings, repo: str) -> tuple:
    """Les branches de base d'un dépôt (#1483) : celles que déclare
    `github.projects.<projet>.base`, en tête (la première est la référence d'un
    push hors PR), puis `BASE_BRANCHES` — `main` reste une base même quand le
    dépôt déclare `develop` : traitée en branche de lot, chaque tête de main y
    remplacerait la précédente et retirerait son verdict, comme `ci_main_red`
    la lit déjà.

    #1683 : source unique du veilleur (`Watcher.bases`) et de l'alerte
    (`main_red`) — une branche d'un autre dépôt qui porterait le même nom
    n'est pas lue comme une base."""
    declared = settings.bases_for(repo) if settings is not None else None
    return tuple(dict.fromkeys(tuple(declared or ()) + BASE_BRANCHES))


def lot_of_run(run: dict, lots: list[dict], bases=BASE_BRANCHES) -> int | None:
    """Le lot d'une exécution : par sa branche (lots ouverts), sinon par la
    ligne `ameesh-work:` de son commit de tête. `bases` : les branches de base
    du dépôt (#1483), sur lesquelles aucune branche de lot n'est cherchée."""
    from .plan_git import WORK_TRAILER_RE
    branch = (run.get("head_branch") or "").casefold()
    if branch and branch not in _base_set(bases):
        for lot in lots:
            if (lot.get("branch") or "").casefold() == branch:
                return int(lot["id"])
    message = ((run.get("head_commit") or {}).get("message")) or ""
    match = WORK_TRAILER_RE.search(message)
    return int(match.group(1)) if match else None


def base_of(run: dict, default: str = BASE_BRANCHES[0]) -> str:
    """La branche de référence du rouge hérité : la cible de la PR, sinon la
    base du dépôt (`default`, #1483 : `develop` pour Nexlink)."""
    for pr in run.get("pull_requests") or ():
        ref = ((pr.get("base") or {}).get("ref")) or ""
        if ref:
            return ref
    return default


def _obsolete_head(run: dict, lot: dict | None, bases=BASE_BRANCHES) -> bool:
    """#1161 : la branche du lot connaît une tête plus récente (`branch_head`,
    posé par le relevé des branches) que celle de l'exécution — son verdict
    porterait sur une tête remplacée, et l'exécution de remplacement peut être
    `pending`, invisible au veilleur. Le silence vaut mieux qu'un faux vert.
    Jamais sur une branche de base (#1483) : chaque tête y a son lot, et le
    relevé normal n'y remplace jamais rien."""
    branch = run.get("head_branch") or ""
    if not branch or branch.casefold() in _base_set(bases):
        return False
    known = (lot or {}).get("branch_head") or ""
    return bool(known) and (run.get("head_sha") or "") != known


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    from .fil import clean_text
    return clean_text(text or "").strip()


def _excerpt(text: str) -> str:
    lines = _clean(text).splitlines()
    if len(lines) > EXCERPT_LINES:
        lines = lines[:EXCERPT_LINES] + ["… (%d more line(s))" % (len(lines) - EXCERPT_LINES)]
    return "\n".join(lines)


def _short(sha: str) -> str:
    return (sha or "")[:10]


def _head(run: dict, lot: dict | None) -> str:
    where = "lot #%d \"%s\"" % (lot["id"], lot.get("title") or "") if lot else "no lot"
    workflow = workflow_of(run.get("workflow") or run.get("path"))
    attempt = int(run.get("run_attempt") or 1)
    return "CI of %s%s, head %s on %s%s (%s)" % (
        where, " (%s)" % workflow if workflow else "",
        _short(run["head_sha"]), run["branch"],
        ", attempt %d" % attempt if attempt > 1 else "",
        run.get("url") or "run %s" % run["run_id"])


def _failure_lines(failures: list[dict], *, detail: bool,
                   attribution: dict | None = None) -> list[str]:
    """Les lignes d'échec ; `attribution` (#3801) nomme l'unité probable."""
    from . import packet_red
    by_test = packet_red.tests_by_name(attribution)
    out = []
    for i, f in enumerate(failures):
        out.append("• %s%s (%s, %s)" % (f["test"], " — inherited" if f.get("inherited") else "",
                                        f.get("part") or "?", f.get("status") or "echec"))
        entry = by_test.get(f["test"])
        if entry:
            out.append("  unités probables : %s" % packet_red.evidence_line(entry))
        if f.get("replay"):
            out.append("  replay: %s" % f["replay"])
        if detail and i < DETAILED and f.get("excerpt"):
            out.append("")
            out.extend("    " + line for line in _excerpt(f["excerpt"]).splitlines())
            out.append("")
    if detail and len(failures) > DETAILED:
        out.append("(excerpts of the first %d only)" % DETAILED)
    return out


def failure_text(run: dict, lot: dict | None, failures: list[dict], *, first: bool,
                 detail: bool = True, unknown_base: bool = False,
                 attribution: dict | None = None) -> str:
    """Le message d'un ou plusieurs échecs nouveaux d'une exécution.

    `attribution` (#3801) : le relevé `packet_red` du paquet, qui nomme pour
    chaque test l'unité probable. Absent hors porteur de paquet."""
    from . import packet_red
    head = _head(run, lot)
    what = ("first failure" if len(failures) == 1 else "first %d failures" % len(failures)) \
        if first else ("next failure" if len(failures) == 1
                       else "next %d failures" % len(failures))
    lines = ["%s: %s." % (head, what), ""]
    if unknown_base:
        lines += ["No finished run of %s has been seen yet: these failures may be "
                  "inherited, hence this message without a wake-up." % (run.get("base") or "main"),
                  ""]
    lines += _failure_lines(failures, detail=detail, attribution=attribution)
    note = packet_red.note_of(attribution)
    if note:
        lines += ["", note]
    lines += ["", "The suite goes on, verdict at the end. A new push replaces this "
                  "run: the messages you have not read yet will be withdrawn. No need "
                  "to wait for the CI in your turn (gh pr checks --watch): ameesh tells you."]
    return "\n".join(lines)


def verdict_text(run: dict, lot: dict | None, status: str, failures: list[dict],
                 pending: list[dict] | None = None,
                 attribution: dict | None = None) -> str:
    """Le message de fin d'une exécution. `pending` : les autres workflows de
    la même tête encore en cours — le verdict les nomme, pour qu'un vert d'une
    suite ne soit pas pris pour un vert de toute la CI (#1110).

    `attribution` (#3801) : le relevé `packet_red` du paquet, s'il est rouge.
    """
    from . import packet_red
    head = _head(run, lot)
    own = [f for f in failures if not f.get("inherited")]
    inherited = [f for f in failures if f.get("inherited")]
    others = sorted({workflow_of(r.get("workflow") or r.get("path")) or "?"
                     for r in pending or ()
                     if int(r.get("run_id") or 0) != int(run.get("run_id") or 0)})
    if status == "success" and not failures:
        first = "%s: green." % head
    else:
        lines = []
        if status == "success":
            lines.append("%s: green, despite failures seen along the way "
                         "(retried or tolerated)." % head)
        elif not own and inherited:
            lines.append("%s: red, but only failures inherited from %s (already red "
                         "before your push): nothing is charged to you."
                         % (head, run.get("base") or "main"))
        elif not failures:
            lines.append("%s: red, with no failed test seen in the stream (part interrupted, "
                         "timeout or another job): see the run." % head)
        else:
            inherited_note = (", and %d inherited from the base" % len(inherited)
                              if inherited else "")
            lines.append("%s: red, %d failure(s)%s." % (head, len(own), inherited_note))
        lines.append("")
        lines += _failure_lines(own + inherited, detail=False, attribution=attribution)
        first = "\n".join(lines).rstrip()
    note = packet_red.note_of(attribution)
    if note and (failures or status != "success"):
        first += "\n\n" + note
    if others:
        first += ("\n\nOther workflow(s) of this head are still running: %s — their "
                  "verdicts will follow." % ", ".join(others))
    return first


def pending_text(run: dict, lot: dict | None, waited: float, cancelled: list[int]) -> str:
    """Le message d'une exécution de PR restée `pending` : elle attend son
    groupe de concurrence. `cancelled` : les exécutions supplantées non
    démarrées que le relevé vient d'annuler pour le libérer (#1161)."""
    head = _head(run, lot)
    lines = ["%s: pending for %d min — the run is waiting for its concurrency "
             "group." % (head, max(1, int(waited // 60)))]
    if cancelled:
        lines.append("Superseded run(s) of an earlier head were holding it: %s "
                     "cancelled." % ", ".join(str(i) for i in cancelled))
    else:
        lines.append("No superseded run of this pull request can be cancelled: the "
                     "group is held elsewhere; the run starts when it frees.")
    lines += ["", "No need to wait for the CI in your turn (gh pr checks --watch): "
                  "ameesh tells you."]
    return "\n".join(lines)


def _send(db, to: str, text: str, short: str, *, lot: int | None, payload: dict,
          urgent: bool, after_commit: list | None = None) -> int:
    """Envoie ; un texte refusé comme illisible (extrait binaire ou encodé)
    part sans ses extraits. `after_commit` (une liste) : l'écriture du fil y
    est ajoutée, à exécuter par l'appelant après le commit de sa transaction
    (#795 N2)."""
    from . import mail, work
    from .fil import UnreadableBody
    body = dict(payload)
    if urgent:
        body["urgent"] = True
    try:
        return mail.send(db, work.SYSTEM_SENDER, to, text, kind="event",
                         work_item_id=str(lot) if lot else None, payload=body,
                         after_commit=after_commit)
    except UnreadableBody:
        return mail.send(db, work.SYSTEM_SENDER, to, short, kind="event",
                         work_item_id=str(lot) if lot else None, payload=body,
                         after_commit=after_commit)


@contextlib.contextmanager
def _atomic(db):
    """#795 N2 : la transaction qui porte le courrier et sa preuve.

    Le trigger `agent_mailbox` n'émet son `pg_notify` qu'au commit : un
    lecteur ne voit donc le courrier qu'une fois `ci_runs.urgent_message_id`
    ou `verdict_message_id` écrit, et `_system_event_interrupt` l'accepte.
    Un exécuteur médié (L109) n'a pas de transaction : ses opérations restent
    séquentielles (pas de NOTIFY local à intercaler)."""
    if storage.is_remote(db):
        yield db
        return
    with db.transaction() as tx:
        yield tx


def _send_posed(db, to: str, text: str, short: str, *, lot: int | None, payload: dict,
                urgent: bool, pose) -> int:
    """Dépose le courrier de l'événement et pose sa preuve dans la même
    transaction : `pose(stockage, message_id)` écrit la référence relue par
    l'exécuteur. Le fil externe n'est écrit qu'après le commit."""
    threads: list = []
    with _atomic(db) as tx:
        message_id = _send(tx, to, text, short, lot=lot, payload=payload,
                           urgent=urgent, after_commit=threads)
        pose(storage.of(tx), message_id)
    for record in threads:
        record(db.cfg, db)
    return message_id


def _same_lot(row: dict, pending: list[dict] | None) -> list[dict]:
    """Les exécutions encore en cours dont le verdict ira au même lot : seul
    celui-là peut prendre le relais du réveil (#5044)."""
    return [r for r in pending or ()
            if r.get("work_item_id") == row.get("work_item_id")]


def recipient_of(lot: dict | None) -> str | None:
    from . import work
    to = (lot or {}).get("assignee") or ""
    if not to or to.startswith(work.HUMAN_PREFIX):
        return None
    return to


# --------------------------------------------------------------------------
# relevé
# --------------------------------------------------------------------------

def gh_job_log(gh=None, *, timeout: float = 120.0):
    """La fonction `(dépôt, job_id) -> texte du log` (#3783).

    `gh api` suit la redirection signée de GitHub et ne transmet le jeton
    qu'à l'API ; `--allow-escape-sequences` est requis par `gh` dès qu'un log
    porte une séquence ANSI (npm en écrit). Une erreur est levée : l'appelant
    la journalise et laisse le relevé continuer."""
    from . import plan_github

    client = plan_github.Gh(gh=gh, timeout=timeout)

    def read(repo: str, job_id: int) -> str:
        path = "repos/%s/actions/jobs/%s/logs" % (repo, int(job_id))
        code, out, err = client.run(["api", "--allow-escape-sequences", path])
        if code != 0:
            raise plan_github.GithubError("gh api %s: exit code %d: %s" % (
                path, code, " ".join((err or out or "—").split())[:300]))
        return out
    return read


def gh_api(gh=None, *, cache_dir=None, ttl=None, clock=None):
    """La fonction `chemin -> JSON` de l'API GitHub : le jeton partagé, avec
    le cache et le quota partagés (#848) quand ils sont donnés, sinon le repli
    local `gh api`."""
    from . import plan_github

    client = plan_github.client(
        gh=gh, cache_dir=cache_dir, clock=clock or time.time,
        ttl=plan_github.DEFAULT_CI_TTL if ttl is None else ttl)
    if client is None:
        raise plan_github.GithubError("no GitHub token (%s) and no local gh"
                                      % ", ".join(plan_github.TOKEN_VARS))
    return client.get


#: les marqueurs canon d'un message de fusion (`plan_git.WORK_TRAILER_RE`,
#: `ameesh-work: <id>`). Le commit composé du train #1467 en porte **un par
#: membre** : le message entier ne désigne donc pas un lot unique.
def _message_lots(message: str) -> list:
    from . import plan_git
    out = []
    for match in plan_git.WORK_TRAILER_RE.finditer(message or ""):
        try:
            out.append(int(match.group(1)))
        except (TypeError, ValueError):
            continue
    return out


#: #2546 : les instructions de saut reconnues par GitHub. Un message de tête
#: qui en porte une — même en citation d'un commit antérieur — ne démarre
#: aucun workflow `push` ou `pull_request`, l'événement `labeled` de la
#: demande de CI compris. La forme est volontairement tolérante aux espaces et
#: à la casse : un marqueur approchant est refusé plutôt que laissé passer.
SKIP_CI_RE = re.compile(
    r"\[[ \t]*(?:skip[ \t]+ci|ci[ \t]+skip|no[ \t]+ci|skip[ \t]+actions|"
    r"actions[ \t]+skip)[ \t]*\]", re.IGNORECASE)


def skip_ci_marker(message: str) -> str:
    """Le marqueur de saut GitHub présent dans `message`, ou `''`."""
    match = SKIP_CI_RE.search(message or "")
    return match.group(0) if match else ""


def _commit_message(api, repo: str, sha: str) -> str:
    """Le message complet du commit `sha` (`API commits/{sha}`)."""
    entry = api("repos/%s/commits/%s" % (repo, sha)) or {}
    return str(((entry.get("commit") or {}).get("message")) or "")


def _files_of(entry: dict) -> list:
    return [str(f.get("filename")) for f in (entry.get("files") or ()) if f.get("filename")]


def _author_of(entry: dict) -> str:
    author = entry.get("author") or {}
    if author.get("login"):
        return str(author["login"])
    return str(((entry.get("commit") or {}).get("author") or {}).get("name") or "")


def _summary_of(entry: dict) -> str:
    message = ((entry.get("commit") or {}).get("message") or "").splitlines()
    return " ".join(message[0].split())[:160] if message else ""


def landed_commits(repo: str, head_sha: str, api=None, *, max_lots: int = 12) -> list:
    """#1681 : les commits de l'atterrissage qui a produit `head_sha`.

    Rend `[{sha, lot, author, summary, files}]`, la forme attendue par
    `incident.attribute` : le premier parent de la tête ouvre la comparaison
    (`repos/<repo>/compare/<parent>...<head>`, le même client `gh api` que le
    veilleur), et chaque commit porteur du marqueur canon `ameesh-work: <id>`
    nomme un lot.

    Trois cas, du plus précis au plus prudent :

    * un seul lot — un candidat, avec les fichiers de la comparaison entière ;
    * un paquet dont chaque lot porte **son** commit de fusion (un marqueur par
      commit) — un candidat par lot, fichiers de son commit (lus au détail) ;
    * un paquet composé (**plusieurs marqueurs dans un même message**, le
      commit final multiparent du train #1467) — les lots sont nommés, mais
      aucun fichier ne leur est attribué : l'agrégat ne se découpe pas sans la
      preuve du manifeste (`plan.proof.members[]`), et l'agrégat donnerait à
      chaque lot les changements des autres. Un candidat sans fichier ne fonde
      aucune accusation (`incident.attribute`).

    Sans marqueur, un candidat sans lot est rendu : il porte les fichiers, mais
    ne peut pas fonder une accusation.
    """
    if not repo or not head_sha:
        return []
    api = api or gh_api()
    head = api("repos/%s/commits/%s" % (repo, head_sha)) or {}
    parents = [str(p.get("sha") or "") for p in (head.get("parents") or ()) if p.get("sha")]
    if not parents:
        return []
    compare = api("repos/%s/compare/%s...%s" % (repo, parents[0], head_sha)) or {}
    commits = list(compare.get("commits") or ())
    by_lot: dict = {}
    composed: list = []
    for entry in commits:
        lots = _message_lots((entry.get("commit") or {}).get("message") or "")
        if len(lots) > 1:
            composed.append((entry, lots))
        elif lots:
            by_lot.setdefault(lots[0], []).append(entry)
    if composed:
        out = []
        for entry, lots in composed:
            for lot in lots[:max_lots]:
                out.append(dict(sha=str(entry.get("sha") or head_sha), lot=lot,
                                author=_author_of(entry), summary=_summary_of(entry), files=[]))
                max_lots -= 1
                if max_lots <= 0:
                    return out
        return out
    if not by_lot:
        if not commits:
            return []
        last = commits[-1]
        return [dict(sha=str(last.get("sha") or head_sha), lot=None, author=_author_of(last),
                     summary=_summary_of(last), files=_files_of(compare))]
    if len(by_lot) == 1:
        lot, entries = next(iter(by_lot.items()))
        last = entries[-1]
        return [dict(sha=str(last.get("sha") or head_sha), lot=lot, author=_author_of(last),
                     summary=_summary_of(last), files=_files_of(compare))]
    out = []
    for lot, entries in list(by_lot.items())[:max_lots]:
        last = entries[-1]
        detail = api("repos/%s/commits/%s" % (repo, last.get("sha"))) or {}
        out.append(dict(sha=str(last.get("sha") or ""), lot=lot, author=_author_of(last),
                        summary=_summary_of(last), files=_files_of(detail)))
    return out


def _guard_snapshot(run_id, attempt, status, jobs) -> dict:
    """#845 : le relevé d'une exécution — jobs `queued` (compte et plus
    vieux) et runners qui tiennent un job `in_progress` (`{nom: job}`).

    Rien n'est déduit d'un job `cancelled` : l'orphelin d'une exécution
    annulée compte comme un job en file (#876 le traite à la source)."""
    queued: list[tuple[str, int]] = []
    holding: dict[str, str] = {}
    for job in jobs:
        job_status = str(job.get("status") or "")
        if job_status == "queued":
            created = str(job.get("created_at") or job.get("started_at") or "")
            queued.append((created, int(job.get("id") or 0)))
        elif job_status == "in_progress" and job.get("runner_name"):
            holding.setdefault(str(job["runner_name"]), str(job.get("name") or ""))
    oldest = min(queued, key=lambda q: (q[0], q[1])) if queued else None
    return {"run_id": int(run_id), "attempt": int(attempt), "status": status,
            "queued": len(queued), "oldest_id": oldest[1] if oldest else None,
            "oldest_at": oldest[0] if oldest else None, "runners": holding}


class Watcher:
    """Le relevé de la CI, mené par `ameesh notify` à chaque passage, sur
    l'hôte désigné (`github.host`). Une erreur (GitHub, base) d'un dépôt est
    journalisée une fois par passage et n'arrête ni les autres dépôts, ni
    les alertes. Les lectures passent par le client à jeton et son cache
    partagé (#848) : une lecture identique n'est pas repayée à chaque
    passage, et un quota épuisé arrête le passage sans boucle."""

    def __init__(self, cfg, *, api=None, cancel=None, log=None, clock=None,
                 dry_run: bool = False, ttl=None, logs=None):
        self.cfg = cfg
        self.api = api
        #: #3783 : lecture du log d'un job (`(dépôt, job_id) -> texte`). None :
        #: pas de lecture de log (tests, ou `gh` absent) ; le relevé normal et
        #: le flux JSON gardent leur comportement.
        self.logs = logs
        self._parsed_logs: set = set()
        self._warned_logs: set = set()
        #: l'écriture d'annulation GitHub (`repos/<repo>/actions/runs/<id>/cancel`) ;
        #: None : `gh api -X POST` en production, jamais en test sans le dire
        self.cancel = cancel
        self.log = log or (lambda _text: None)
        self.clock = clock or time.time
        self.dry_run = bool(dry_run)
        #: découverte des workflows par dépôt : (horodatage, ttl, noms)
        self._workflows_cache: dict[str, tuple] = {}
        #: avertissements déjà journalisés par ce passage de service
        self._warned_workflows: set = set()
        self.ttl = None if ttl is None else float(ttl)
        #: #1483 : workflows découverts écartés (pas une suite), listes
        #: d'exécutions tronquées — journalisés une fois par passage
        self._warned_skipped: set = set()
        self._warned_truncated: set = set()
        #: têtes de branche déjà en échec de lecture (une fois par service)
        self._warned_branches: set = set()
        #: exécutions `pending` déjà signalées (une fois par service : la
        #: mémoire de `ci_runs` ne porte pas ce message, additif sans migration)
        self._pending_alerted: set = set()
        #: #3801 : par dépôt, le clone et la carte d'impact déjà lus (ou
        #: l'échec de leur lecture) pour les courriers d'un paquet rouge. Un
        #: téléchargement de carte raté n'est pas retenté à chaque échec du
        #: même dépôt (N2 du gel #20746).
        self._packet_shared: dict = {}

    def _cache_ttl(self) -> float:
        """La garde des lectures CI : le réglage `github.ci_ttl`, sinon le défaut."""
        if self.ttl is not None:
            return self.ttl
        from . import plan_github
        try:
            return plan_github.settings_of(self.cfg).ci_ttl
        except plan_github.SettingsError:
            return plan_github.DEFAULT_CI_TTL

    def settings(self):
        """Les réglages `github` validés, ou None (déjà journalisé)."""
        from . import plan_github
        try:
            return plan_github.settings_of(self.cfg)
        except plan_github.SettingsError:
            return None

    def repos(self) -> list[str]:
        settings = self.settings()
        if settings is None:
            return []  # déjà journalisé par la projection des issues
        if not settings.projects or not role_run.host_authorized(self.cfg, settings.host):
            return []
        return sorted(set(settings.projects.values()))

    def bases(self, repo: str) -> tuple:
        """Les branches de base d'un dépôt (#1483, #1683) : `bases_for_repo`,
        la même lecture que `ci.main_red` — le veilleur et l'alerte ne
        divergent pas."""
        return bases_for_repo(self.settings(), repo)

    def discovered_workflows(self, repo: str) -> tuple[str, ...] | None:
        """Les fichiers de workflow actifs du dépôt (API GitHub), ou None si
        l'API ne répond pas. Le résultat est gardé : une découverte par heure,
        une nouvelle tentative après un échec."""
        from . import plan_github
        now = float(self.clock())
        cached = self._workflows_cache.get(repo)
        if cached is not None and now - cached[0] < cached[1]:
            return cached[2]
        ttl, found = DISCOVERY_TTL, None
        try:
            data = self.api("repos/%s/actions/workflows?per_page=100" % repo) or {}
            files = []
            for item in data.get("workflows") or ():
                if str(item.get("state") or "active") != "active":
                    continue
                name = workflow_of(item.get("path"))
                if name and name not in files:
                    files.append(name)
            found = tuple(sorted(files))
        except plan_github.GithubRateLimited:
            # N5 #848 : le quota épuisé n'est pas une panne de découverte ; il
            # remonte au passage, qui s'arrête et le journalise comme tel.
            raise
        except Exception as exc:  # noqa: BLE001 - jamais fatal au relevé
            ttl = DISCOVERY_RETRY
            self.log("CI %s: workflow discovery failed (%s)"
                     % (repo, " ".join(str(exc).split())[:200]))
        self._workflows_cache[repo] = (now, ttl, found)
        return found

    def followed_workflows(self, repo: str) -> tuple[str, ...]:
        """Les workflows suivis d'un dépôt : la liste déclarée
        (`github.projects.<projet>.workflows`) si elle existe, sinon le défaut
        L161 tant que le dépôt publie `tests.yml`, sinon les workflows actifs
        découverts qui ont la forme d'une suite de tests (#1110, #1483 : un
        workflow d'exploitation — déploiement, sonde, synchronisation — ne rend
        pas de verdict de bruit à l'auteur du lot). Les workflows écartés sont
        journalisés, et la liste déclarée les suit explicitement ; si aucun
        workflow découvert ne ressemble à une suite, le comportement de #1110
        est conservé (tous suivis) pour qu'aucune suite au nom inhabituel ne
        soit perdue.

        #1683 : un `ci.yml` générique ne porte aucun marqueur de suite
        (`SUITE_RE`) — il est donc écarté **dès qu'une autre suite est
        reconnue**, comportement assumé, journalisé une fois, et la liste
        déclarée reste le moyen de le suivre. Aucun n'est ignoré en silence."""
        settings = self.settings()
        declared = settings.workflows_for(repo) if settings else None
        found = self.discovered_workflows(repo)
        if declared is not None:
            missing = tuple(w for w in declared if found is not None and w not in found)
            if missing and (repo, missing) not in self._warned_workflows:
                self._warned_workflows.add((repo, missing))
                self.log("CI %s: workflow(s) declared in github.projects but absent "
                         "from the repository: %s" % (repo, ", ".join(missing)))
            return declared
        if not found:
            return WORKFLOWS
        if WORKFLOWS[0] in found:
            return WORKFLOWS
        suites = tuple(w for w in found if SUITE_RE.search(w))
        if not suites:
            if (repo, "") not in self._warned_skipped:
                self._warned_skipped.add((repo, ""))
                self.log("CI %s: no discovered workflow looks like a test suite, "
                         "following all of them (#1110)" % repo)
            return found
        skipped = tuple(w for w in found if w not in suites)
        if skipped and (repo, skipped) not in self._warned_skipped:
            self._warned_skipped.add((repo, skipped))
            self.log("CI %s: discovered workflow(s) not followed (not a test suite): %s; "
                     "declare them in github.projects to follow them"
                     % (repo, ", ".join(skipped)))
        return suites

    def _listed(self, repo: str, status: str) -> list[dict]:
        """La liste des exécutions d'un état (#1483), page `LIST_PER_PAGE`.
        Une liste tronquée est journalisée une fois : au-delà, une exécution
        suivie peut rester cachée derrière une file d'autres workflows."""
        data = self.api("repos/%s/actions/runs?status=%s&per_page=%d"
                        % (repo, status, LIST_PER_PAGE)) or {}
        runs = list(data.get("workflow_runs") or ())
        total = int(data.get("total_count") or 0)
        if total > len(runs) and (repo, status) not in self._warned_truncated:
            self._warned_truncated.add((repo, status))
            self.log("CI %s: %s run list truncated (%d of %d), a followed run may "
                     "be seen late" % (repo, status, len(runs), total))
        return runs

    def _listed_on(self, repo: str, branch: str, status: str, *,
                   page: int = 1) -> list[dict]:
        """#2935 : une page (`page`, `LIST_PER_PAGE` exécutions) de la liste
        des exécutions d'une branche (filtre `branch` de l'API).

        #2938 (N1/N2 de la relecture #2935) : la première page ne suffit pas
        à dire l'état d'une base — plus de cent annulations plus récentes
        peuvent cacher le dernier `success`/`failure` utile. L'appelant page
        donc tant qu'il n'a pas le résultat utile de chaque workflow suivi
        (`_refresh_bases`), sous un plafond (`HEAD_LIST_MAX_PAGES`)."""
        data = self.api("repos/%s/actions/runs?branch=%s&status=%s&per_page=%d&page=%d"
                        % (repo, quote(branch, safe=""), status, LIST_PER_PAGE, page)) or {}
        return list(data.get("workflow_runs") or ())

    def branch_head(self, repo: str, branch: str) -> str | None:
        """La tête de `branch` selon GitHub, ou None (API muette, branche
        disparue) : elle prouve quelle exécution de la branche n'est pas
        supplantée, avant toute annulation (#1161)."""
        try:
            data = self.api("repos/%s/branches/%s" % (repo, branch)) or {}
        except Exception as exc:  # noqa: BLE001 - jamais fatal au relevé
            if (repo, branch) not in self._warned_branches:
                self._warned_branches.add((repo, branch))
                self.log("CI %s: head of branch %s unreadable (%s)"
                         % (repo, branch, " ".join(str(exc).split())[:200]))
            return None
        sha = str(((data.get("commit") or {}).get("sha")) or "").strip()
        return sha or None

    def cancel_run(self, repo: str, run_id: int, *, proof: dict | None = None) -> bool:
        """Annule une exécution GitHub **non démarrée** (`queued`/`pending`)
        dont l'essai est prouvé antérieur à celui de la tête courante du même
        groupe de concurrence : elle retient la tête courante (#1161).
        Idempotent : une exécution déjà conclue n'est pas annulée ; une erreur
        de l'API est journalisée et rend False, sans toucher à la base ni
        rendre de verdict.

        `proof` est la preuve calculée sur la liste du relevé (essai, tête,
        groupe, date du successeur) : la relecture fraîche doit la confirmer
        avant le POST. Un essai plus récent (`run_attempt`), une autre tête, un
        autre groupe ou une date d'essai qui n'est plus antérieure au
        successeur conservent l'exécution — le POST ne vise jamais un essai que
        la liste n'avait pas qualifié (#1161, R1)."""
        try:
            run = self.api("repos/%s/actions/runs/%s" % (repo, run_id)) or {}
            if (run.get("status") or "") not in CANCELLABLE_STATUSES:
                return False
            if proof is not None and not proof_holds(run, proof):
                return False
            if self.cancel is None:
                from . import plan_github
                plan_github._json(plan_github.Gh(), [
                    "api", "-X", "POST", "repos/%s/actions/runs/%s/cancel" % (repo, run_id)])
            else:
                self.cancel(repo, int(run_id))
            return True
        except Exception as exc:  # noqa: BLE001 - jamais fatal au relevé
            self.log("CI %s: cancel of run %s failed (%s)"
                     % (repo, run_id, " ".join(str(exc).split())[:200]))
            return False

    def tick(self, db) -> list[dict]:
        """Un relevé de chaque dépôt ; rend les envois et retraits.

        #1813 : les compteurs du cache de lectures partent à la fin du
        passage (`flush_reads`), même si le quota a coupé la boucle — le
        client du runner vit toute la vie du processus, une suite de hits
        restait sinon invisible sur disque jusqu'à la requête suivante."""
        from . import plan_github

        try:
            repos = self.repos()
            if not repos or self.dry_run:
                return []
            if self.api is None:
                self.api = gh_api(cache_dir=getattr(self.cfg, "state_dir", None),
                                  ttl=self._cache_ttl(), clock=self.clock)
                if self.logs is None:
                    # #3783 : les logs de jobs passent par `gh` (redirection
                    # signée, jeton jamais envoyé au stockage) ; sans `gh`, le
                    # relevé garde le flux JSON et le verdict sans nom de test.
                    try:
                        self.logs = gh_job_log()
                    except Exception as exc:  # noqa: BLE001 - jamais fatal
                        self.log("CI: job logs unavailable (%s)"
                                 % " ".join(str(exc).split())[:200])
            out: list[dict] = []
            for repo in repos:
                try:
                    out += self.watch(db, repo)
                except plan_github.GithubRateLimited as exc:
                    # quota épuisé : aucun autre appel ne part avant le reset
                    when = time.strftime("%H:%M:%SZ", time.gmtime(exc.reset)) \
                        if exc.reset else "unknown"
                    self.log("CI %s: GitHub quota exhausted (reset at %s): pass stopped"
                             % (repo, when))
                    break
                except Exception as exc:  # noqa: BLE001 - jamais fatal au passage
                    self.log("CI %s: check failed (%s)" % (repo, " ".join(str(exc).split())[:200]))
            # #2957 : le périmètre actif est la configuration de l'hôte
            # désigné. Un dépôt qui en sort ne doit pas laisser sa ligne de
            # file : le repli de `github_observation` sur un hôte sans
            # configuration la surveillerait et alerterait à jamais de son
            # silence. La purge ne dépend pas des appels GitHub du passage
            # (un quota épuisé ou une erreur d'un dépôt ne la saute pas).
            try:
                storage.of(db).ci.prune_queue(repos)
            except Exception as exc:  # noqa: BLE001 - cache seulement, jamais fatal
                self.log("CI: queue rows outside the active perimeter not pruned (%s)"
                         % " ".join(str(exc).split())[:200])
            try:
                storage.of(db).ci.prune_maps(repos)
            except Exception as exc:  # noqa: BLE001 - cache seulement, jamais fatal
                self.log("CI: map rows outside the active perimeter not pruned (%s)"
                         % " ".join(str(exc).split())[:200])
            return out
        finally:
            plan_github.flush_reads(getattr(self.cfg, "state_dir", None))

    # -- un dépôt --------------------------------------------------------------
    def watch(self, db, repo: str) -> list[dict]:
        st = storage.of(db)
        followed = set(self.followed_workflows(repo))
        bases = self.bases(repo)
        live = {}
        for status in LIVE_STATUSES:
            for r in self._listed(repo, status):
                if workflow_of(r.get("path")) in followed:
                    live.setdefault(int(r["id"]), r)
        lots = [r for r in st.work.open_for_sweep(500) if r.get("branch")]
        out: list[dict] = []
        for run_id in sorted(live):
            out += self._follow(db, repo, live[run_id], lots, bases)
        # les exécutions de PR bloquées en `pending` : les exécutions non
        # démarrées d'une tête supplantée retiennent le groupe de concurrence,
        # elles sont annulées ; la tête courante qui attend est signalée (#1161)
        out += self._unblock(db, repo, live, lots)
        # #747 : fin de l'attente d'un runner, distincte de la durée des tests
        st.ci.mark_started(repo, [i for i, r in live.items()
                                  if r.get("status") == "in_progress"])
        # les exécutions courtes, nées et finies entre deux passages, n'ont
        # jamais été vues « en cours » : rattrapage borné (#1110)
        out += self._catch_up(db, repo, lots, followed, bases)
        # #2935 : la tête de chaque base est relue même au-delà de la fenêtre
        # de rattrapage — sans quoi un veilleur arrêté plus d'une heure perdait
        # le vert qui ferme `ci_main_red`, et l'alerte restait fausse
        self._refresh_bases(db, repo, followed, bases)
        # les lignes que ce passage conclut sont figées AVANT les envois : le
        # verdict d'un workflow ne cite pas comme « encore en cours » un
        # workflow conclu dans le même passage (#1110)
        running = st.ci.running(repo)
        finishing: dict[int, dict] = {}
        for row in running:
            if int(row["run_id"]) in live:
                continue
            run = self.api("repos/%s/actions/runs/%s" % (repo, row["run_id"])) or {}
            if (run.get("status") or "") == "completed":
                finishing[int(row["run_id"])] = run
        for row in running:
            run = finishing.get(int(row["run_id"]))
            out += self._failures(db, repo, row, bases,
                                  terminal=conclusion_status(run))
            if run is None:
                continue
            others = [r for r in running if int(r["run_id"]) not in finishing
                      and int(r["run_id"]) != int(row["run_id"])
                      and r.get("head_sha") == row.get("head_sha")
                      and r.get("branch") == row.get("branch")]
            out += self._conclude(db, repo, row, run=run, pending=others)
        # #845 : la garde passe après les échecs et les verdicts — un relevé de
        # runners coûte une requête par exécution non conclue (quelques
        # dizaines de secondes en saturation) et ne doit pas les retarder
        self._guard(db, repo)
        self._map(db, repo, bases)
        return out

    def _map(self, db, repo: str, bases=BASE_BRANCHES) -> None:
        """#3344 : la dernière carte de mesure publiée sur la base du dépôt.

        Le plan de tests (`scripts/test-plan.py`) la juge périmée au-delà de
        48 h (#3450 : un lot garde les impactés de son propre diff, la voie
        documentation et la prévalidation retombent sur la suite complète) et
        le dit en annotation CI ; seule cette lecture permet à `ameesh alerts`
        de le voir (`map_stale`). Un dépôt sans `carte.yml`
        n'est pas lu ; la dernière exécution réussie de la base est relue au
        plus toutes les `MAP_READ_S` (date du relevé précédent, dans la base :
        plusieurs processus de relevé ne la repaient pas). Une erreur GitHub
        est journalisée et laisse le relevé précédent vieillir."""
        from . import plan_github
        found = self.discovered_workflows(repo)
        if not found or MAP_WORKFLOW not in found:
            if found is not None:
                # découverte réussie (même sans aucun workflow actif) : le
                # dépôt ne publie plus de carte, son dernier relevé ne doit pas
                # vieillir en une alerte perpétuelle (relecture #3344 cx1) ;
                # None, échec de découverte, garde le relevé
                storage.of(db).ci.forget_map(repo)
            return
        st = storage.of(db)
        branch = (tuple(bases) or BASE_BRANCHES)[0]
        now = float(self.clock())
        known = next((r for r in st.ci.maps()
                      if str(r.get("repo") or "").casefold() == repo.casefold()), None)
        if known is not None and known.get("branch") == branch \
                and now - float(known.get("seen_ts") or 0) < MAP_READ_S:
            return
        try:
            data = self.api("repos/%s/actions/workflows/%s/runs?branch=%s&status=success"
                            "&per_page=1" % (repo, MAP_WORKFLOW, quote(branch, safe=""))) or {}
        except plan_github.GithubRateLimited:
            raise
        except Exception as exc:  # noqa: BLE001 - jamais fatal au relevé
            self.log("CI %s: last %s run unreadable (%s)"
                     % (repo, MAP_WORKFLOW, " ".join(str(exc).split())[:200]))
            return
        runs = list(data.get("workflow_runs") or ())
        run = runs[0] if runs else {}
        st.ci.observe_map(repo, branch=branch, run_id=run.get("id"),
                          head_sha=run.get("head_sha"),
                          published_at=run.get("updated_at") or None)

    def _catch_up(self, db, repo: str, lots: list[dict], followed: set[str],
                  bases=BASE_BRANCHES) -> list[dict]:
        """Les exécutions terminées entre deux passages (#1110) : une exécution
        courte n'est jamais vue « en cours », donc jamais conclue sans ce
        rattrapage. Borné aux exécutions récentes ; une ligne déjà connue au
        même attempt est laissée au relevé normal (pas de doublon) — sauf si
        elle est restée sans lot : le rattachement tardif (#1321) est retenté
        ici, car une ligne terminale n'est plus revue vivante et son verdict se
        perdrait (#2021).
        Une ligne connue dont l'attempt a augmenté est une **relance** finie
        entre deux passages (#1573 N1) : rouverte comme au relevé, puis suivie
        par la queue existante (échecs + verdict).
        Une tête déjà remplacée sur la même branche est ignorée (pas de verdict
        périmé) — **sauf sur une branche de base** (#1483) : chaque tête de
        `main` porte un lot différent, le relevé normal n'y remplace jamais
        rien, et écarter la tête M1 quand M2 est déjà relevée perdait le verdict
        et le réveil du lot fusionné avec M1."""
        st = storage.of(db)
        runs = self._listed(repo, "completed")
        # les exécutions que ce passage conclut (relevé normal compris) sont
        # figées AVANT les envois : un verdict du rattrapage ne cite pas comme
        # « encore en cours » un workflow conclu dans le même passage (#1110)
        settled = {int(r["id"]) for r in runs}
        now = float(self.clock())
        out: list[dict] = []
        for run in runs:
            if workflow_of(run.get("path")) not in followed:
                continue
            run_id = int(run["id"])
            updated = updated_ts(run)
            if updated is None or not 0 <= now - updated <= CATCHUP_WINDOW:
                continue
            head = run.get("head_sha") or ""
            branch = run.get("head_branch") or ""
            known = st.ci.run(repo, run_id)
            if known is not None:
                attempt = int(run.get("run_attempt") or 1)
                if known["status"] != "superseded" \
                        and attempt > int(known.get("run_attempt") or 1):
                    # #1573 N1 : la relance est déjà finie ; `_follow` rouvre la
                    # ligne (et rattache le lot si la branche a été posée après),
                    # la queue ci-dessous rend ses échecs et son verdict
                    out += self._follow(db, repo, run, lots, bases)
                    row = st.ci.run(repo, run_id)
                    if row is None or row.get("status") != "running" \
                            or int(row.get("run_attempt") or 1) != attempt:
                        continue  # tête remplacée, ou ligne non rouverte
                else:
                    # même attempt : la liste en retard (#828) redit terminée
                    # une exécution déjà conclue — la rouvrir renverrait son
                    # verdict à chaque passe. #2021 : une ligne restée sans lot
                    # est rattachée tardivement et son verdict délivré une fois.
                    out += self._late_link(db, repo, run, known, lots, settled, bases)
                    continue
            else:
                if branch.casefold() not in _base_set(bases) \
                        and st.ci.head_replaced(repo, branch, head, run_id):
                    continue
                # #1161/#1483 : une tête du lot déjà remplacée (`branch_head`) ne
                # rend pas de verdict — la même garde que `_follow` sur une ligne
                # connue ; l'exécution de remplacement peut être `pending`.
                lot = lot_of_run(run, lots, bases)
                row_lot = next((l for l in lots if int(l["id"]) == lot), None) if lot else None
                if _obsolete_head(run, row_lot, bases):
                    continue
                out += self._follow(db, repo, run, lots, bases)
                row = st.ci.run(repo, run_id)
            if row is None or (run.get("status") or "") != "completed":
                continue
            out += self._failures(db, repo, row, bases,
                                  terminal=conclusion_status(run))
            pending = [r for r in st.ci.running(repo) if r.get("head_sha") == head
                       and r.get("branch") == branch
                       and int(r["run_id"]) not in settled]
            out += self._conclude(db, repo, row, run=run, pending=pending)
        return out

    def _refresh_bases(self, db, repo: str, followed: set, bases=BASE_BRANCHES) -> None:
        """#2935 : relève l'état terminal de la tête de chaque base, même plus
        ancien que `CATCHUP_WINDOW`.

        Le rattrapage est borné pour ne pas rendre de verdicts anciens après
        une interruption du veilleur ; mais `ci_main_red` lit `ci_runs`, et la
        base dont le vert (ou le rouge) a conclu pendant l'interruption n'y
        entrait jamais : l'alerte restait rouge alors que la base était verte,
        jusqu'à la prochaine exécution de cette base. Ici, seule la plus
        récente exécution **utile** (`success`/`failure`) par workflow suivi
        est retenue, sans courrier ni réveil : l'état de la base suffit à
        fermer l'alerte, un verdict ancien ne rouvre pas un tour pour rien.

        #2935 (relecture cx3) : une annulation derrière le dernier résultat
        utile ne le cache plus (`latest_concluded` ignore les annulations) ;
        une relance terminée pendant l'arrêt (`run_attempt` plus récent sur
        une ligne déjà connue) est reprise.

        #2938 (N1/N2 cx3) : la liste est paginée jusqu'au dernier résultat
        utile de chaque workflow suivi — plus de cent annulations plus
        récentes ne le cachent plus. #2938 (N auditeur) : seules les
        exécutions **antérieures** à `CATCHUP_WINDOW` sont enregistrées ici ;
        une exécution récente que le rattrapage n'aurait pas vue doit passer
        par `_catch_up`, qui courrielle, jamais par ce relevé silencieux.
        """
        from . import plan_github
        st = storage.of(db)
        now = float(self.clock())
        for branch in bases:
            newest: dict[str, dict] = {}
            seen: set[int] = set()
            page = 1
            while page <= HEAD_LIST_MAX_PAGES:
                try:
                    runs = self._listed_on(repo, branch, "completed", page=page)
                except plan_github.GithubRateLimited:
                    raise
                except Exception as exc:  # noqa: BLE001 - jamais fatal au relevé
                    self.log("CI %s: head of base %s unreadable (%s)"
                             % (repo, branch, " ".join(str(exc).split())[:200]))
                    runs = []
                    break
                fresh = 0
                for run in runs:
                    run_id = int(run.get("id") or 0)
                    if not run_id or run_id in seen:
                        continue
                    seen.add(run_id)
                    fresh += 1
                    if (run.get("head_branch") or "").casefold() != branch.casefold():
                        continue
                    workflow = workflow_of(run.get("path"))
                    if workflow not in followed:
                        continue
                    if _CONCLUSIONS.get(run.get("conclusion") or "", "failure") \
                            not in ("success", "failure"):
                        continue  # annulée : elle ne dit pas l'état de la base
                    if workflow not in newest or run_id > int(newest[workflow]["id"]):
                        newest[workflow] = run
                # fin de liste, plus rien de neuf (API qui ignore `page`), ou
                # dernier résultat utile de chaque workflow trouvé
                if len(runs) < LIST_PER_PAGE \
                        or all(w in newest for w in followed) or not fresh:
                    break
                page += 1
            if page > HEAD_LIST_MAX_PAGES and not all(w in newest for w in followed) \
                    and (repo, "head:%s" % branch) not in self._warned_truncated:
                self._warned_truncated.add((repo, "head:%s" % branch))
                self.log("CI %s: head list of base %s truncated at %d pages, a "
                         "useful result may be hidden deeper"
                         % (repo, branch, HEAD_LIST_MAX_PAGES))
            for run in newest.values():
                updated = updated_ts(run)
                if updated is None or now - updated <= CATCHUP_WINDOW:
                    continue  # récent ou indatable : laissé au rattrapage courriellé
                row = st.ci.run(repo, int(run["id"]))
                if row is None:
                    self._record_terminal(st, repo, run, bases)
                elif int(run.get("run_attempt") or 1) > int(row.get("run_attempt") or 1):
                    self._record_attempt(st, repo, run, bases)

    def _record_terminal(self, st, repo: str, run: dict,
                         bases=BASE_BRANCHES) -> None:
        """#2935 : enregistre une exécution terminale de base sans courrier.

        Son état (et ses échecs) sert à `ci_main_red` ; son verdict n'est pas
        rendu : il porte sur une exécution hors de la fenêtre de rattrapage, et
        rouvrir un tour pour un vert ancien n'a aucun sens. `open_run` puis
        `conclude` gardent la ligne dans la forme du relevé normal."""
        run_id = int(run["id"])
        status = _CONCLUSIONS.get(run.get("conclusion") or "", "failure")
        if status not in ("success", "failure"):
            return
        st.ci.open_run(repo, run_id, head_sha=run.get("head_sha") or "",
                       branch=run.get("head_branch") or "",
                       event=run.get("event") or "",
                       base=base_of(run, default=bases[0]),
                       work_item_id=lot_of_run(run, [], bases),
                       url=run.get("html_url"), workflow=workflow_of(run.get("path")),
                       created_at=run.get("created_at"),
                       run_attempt=int(run.get("run_attempt") or 1))
        row = st.ci.run(repo, run_id)
        if row is None:
            return
        st.ci.conclude(repo, run_id, status)
        self._record_base_failures(st, repo, row, run_id, status, bases)

    def _record_attempt(self, st, repo: str, run: dict,
                        bases=BASE_BRANCHES) -> None:
        """#2935 (relecture cx3 B1) : une relance terminée pendant l'arrêt du
        veilleur (`run_attempt` plus récent) remplace le résultat de l'attempt
        suivi — sinon un rouge d'attempt 1 reste alors que la relance a rendu
        vert. Une annulation n'est pas rejouée : elle ne dit ni vert ni rouge,
        l'ancien résultat utile reste (`main_red` ignore les annulations).

        `reopen_run` garde ses refus (ligne remplacée, tête plus récente) :
        une relance d'une tête qui n'est plus la tête courante de la base ne
        change pas son état courant."""
        run_id = int(run["id"])
        status = _CONCLUSIONS.get(run.get("conclusion") or "", "failure")
        if status not in ("success", "failure"):
            return
        attempt = int(run.get("run_attempt") or 1)
        reopened = st.ci.reopen_run(repo, run_id, run_attempt=attempt)
        if reopened is None:
            return
        st.ci.conclude(repo, run_id, status)
        row = st.ci.run(repo, run_id) or reopened
        self._record_base_failures(st, repo, row, run_id, status, bases)

    def _record_base_failures(self, st, repo: str, row: dict, run_id: int,
                              status: str, bases=BASE_BRANCHES) -> None:
        """#2935 : les échecs d'un résultat de base, pour le détail de
        `ci_main_red` ; aucun message : une exécution hors fenêtre ne réveille
        pas (seul son état est relu)."""
        if status != "failure":
            return
        try:
            self._record_failed_jobs(st, repo, dict(row, status="failure"), bases)
            checks = self.api("repos/%s/commits/%s/check-runs?per_page=100"
                              % (repo, row["head_sha"])) or {}
        except Exception as exc:  # noqa: BLE001 - le statut suffit à l'alerte
            self.log("CI %s run %s: failures unreadable (%s)"
                     % (repo, run_id, " ".join(str(exc).split())[:200]))
            return
        events = events_of(checks.get("check_runs"), run_id)
        if not events:
            # #3783 : même lecture TAP pour la base — la référence du rouge
            # hérité porte alors les noms de tests de nexlink, pas seulement
            # ses jobs.
            events = self._log_failures(repo, row)
        for ev in events:
            st.ci.add_failure(repo, run_id, run_attempt=event_attempt(ev),
                              part=str(ev.get("part") or ""), test=ev["test"],
                              module=ev.get("module") or "", status=ev.get("statut"),
                              inherited=False, excerpt=ev.get("extrait") or "",
                              replay=ev.get("rejouer") or "",
                              duration_s=ev.get("duree_s"), failed_at=ev.get("ts"))

    def _late_link(self, db, repo: str, run: dict, row: dict, lots: list[dict],
                   settled: set, bases=BASE_BRANCHES) -> list[dict]:
        """#2021 : rattache une ligne restée sans lot et, si elle est déjà
        terminale, délivre son verdict une seule fois.

        Les runs d'un paquet (`integration/**`) sont vus avant que la
        composition ne déclare sa branche : ceux qui se concluent entre-temps
        ne sont plus revus vivants par `_follow` (#1321), et leur verdict
        partait à personne. Le rattrapage les ramène ici, sous les mêmes
        gardes qu'un rattachement vivant : une tête plus récente déjà vue sur
        la branche (`head_replaced`, `branch_head` du lot) laisse la ligne sans
        lot, car son verdict porterait sur une tête remplacée."""
        st = storage.of(db)
        run_id = int(run["id"])
        status = row.get("status") or "running"
        if status not in ("running", "success", "failure"):
            return []
        if row.get("work_item_id") is not None or row.get("verdict_message_id"):
            return []
        head = run.get("head_sha") or ""
        branch = run.get("head_branch") or ""
        if not branch or branch.casefold() in _base_set(bases):
            return []
        if (run.get("status") or "") != "completed":
            return []
        if st.ci.head_replaced(repo, branch, head, run_id):
            return []
        lot_id = lot_of_run(run, lots, bases)
        lot = next((l for l in lots if lot_id is not None and int(l["id"]) == int(lot_id)), None)
        if lot is None or not recipient_of(lot):
            return []
        known_head = lot.get("branch_head") or ""
        if known_head and head != known_head:
            return []
        st.ci.open_run(repo, run_id, head_sha=head, branch=branch,
                       event=run.get("event") or "", base=base_of(run),
                       work_item_id=int(lot["id"]), url=run.get("html_url"),
                       workflow=workflow_of(run.get("path")),
                       created_at=run.get("created_at"))
        self.log("CI %s run %s: lot #%s linked late (branch set after the run)"
                 % (repo, run_id, lot["id"]))
        row = st.ci.run(repo, run_id) or row
        if status == "running" or row.get("verdict_message_id"):
            return []  # le relevé normal la conclura et enverra le verdict
        pending = [r for r in st.ci.running(repo) if r.get("head_sha") == head
                   and r.get("branch") == branch
                   and int(r["run_id"]) not in settled]
        return self._verdict(db, repo, row, run, status, pending=pending)

    # -- #845 : la garde de la CI ---------------------------------------------
    def _guard(self, db, repo: str) -> None:
        """#845 : l'état des runners et de la file, relevé par l'API (sans SSH).

        La file est l'âge du plus vieux **job** encore `queued`, toutes
        exécutions non conclues : un run reste `queued` tant qu'un de ses jobs
        attend, et son `created_at` ne date pas ses jobs (une re-exécution
        garde la date du run, un run `in_progress` peut encore porter un job en
        file).

        Le relevé est borné (revue N/P2 2328 ; arbitrage A du pilote, 14/10) :
        la fenêtre de 600 s est écrite dans la base avant le premier appel
        (`ci_guard_scan`, atomique par dépôt), chaque exécution non conclue est
        relue, et le relevé s'arrête au-delà de 60 exécutions ou de 66 appels
        (5 listes + 60 jobs + 1 runners). Chaque exécution non conclue est
        relue : un run resté `queued` peut porter un job déjà `in_progress`
        quand un autre attend (mesure fondatrice #828), donc aucun instantané de
        jobs ne peut être réutilisé sur le seul statut `queued` (R1 finale cx3,
        B1). Un relevé incomplet ne publie **rien** : il est journalisé et daté
        dans `ci_guard_scan`, l'état complet précédent reste en place et fonde
        encore ses alertes jusqu'à la fraîcheur (1800 s) — jamais une mesure
        partielle publiée comme complète.

        Rien ici n'est fatal au relevé : une erreur (jeton, GitHub) est
        journalisée et n'empêche ni les échecs, ni les verdicts.
        """
        from . import plan_github
        try:
            self._guard_state(db, repo)
        except plan_github.GithubRateLimited:
            # N5 #848 : un quota épuisé arrête le passage (tick le rattrape),
            # il n'est pas journalisé comme un échec de la garde.
            raise
        except Exception as exc:  # noqa: BLE001 - jamais fatal au passage
            self.log("CI %s: guard failed (%s)" % (repo, " ".join(str(exc).split())[:200]))

    def _guard_state(self, db, repo) -> None:
        st = storage.of(db)
        now = float(self.clock())
        # #845 (revue N/P2 2328) : la fenêtre est revendiquée AVANT le premier
        # appel HTTP, atomiquement dans la base : deux `notify --once`
        # concurrents (ou deux hôtes) ne doublent pas le coût, et un relevé tué
        # en vol laisse la fenêtre expirer. Le battement en mémoire ne suffisait
        # pas.
        if not st.ci.claim_guard_scan(repo, now=now, window_s=GUARD_SCAN_S):
            self.log("CI %s: guard scan skipped (window held)" % repo)
            return
        calls = 0
        runs_seen = 0
        covered = 0
        complete = False
        reason = None

        def api(path: str):
            """Un appel compté : le budget arrête le relevé, il ne le tronque pas."""
            nonlocal calls
            if calls >= GUARD_SCAN_MAX_CALLS:
                raise _GuardBudget(path)
            calls += 1
            return self.api(path)

        try:
            # #845 : le relevé liste lui-même les exécutions non conclues, tous
            # workflows et jusqu'à `GUARD_PER_PAGE` par état — la liste du suivi
            # des échecs (50, `tests.yml` seul) ne suffit pas à la garde
            not_done: dict[int, dict] = {}
            truncated = False
            for status in LIVE_STATUSES:
                data = api("repos/%s/actions/runs?status=%s&per_page=%d"
                           % (repo, status, GUARD_PER_PAGE)) or {}
                page = data.get("workflow_runs") or ()
                if int(data.get("total_count") or 0) > len(page):
                    truncated = True
                for r in page:
                    not_done.setdefault(int(r["id"]), r)
            runs_seen = len(not_done)
            if truncated:
                reason = "truncated"
            elif runs_seen > GUARD_SCAN_MAX_RUNS:
                reason = "too_many_runs"
            else:
                # #845 : la file se compte en jobs, pas en runs, et la date qui
                # compte est celle du job (le run peut être ancien et sa reprise
                # récente, et un run `in_progress` peut encore porter un job
                # `queued`). Rien n'est déduit des jobs `cancelled` du run : une
                # matrice `fail-fast` annule ses parts sans annuler l'exécution,
                # et le job né après cette annulation (agrégat `tests`, job
                # indépendant) attend réellement un runner. Un orphelin
                # d'exécution annulée (`if: always()` créé après l'annulation,
                # que GitHub ne sert jamais) a la même forme qu'un job légitime
                # né après une annulation de part : aucune inférence ne les
                # sépare sans masquer une file réelle. Les orphelins se traitent
                # à la source (`always() && !cancelled()` sur l'agrégat, #876).
                queued: list[tuple[str, int]] = []
                queued_total = 0
                holding: dict[str, str] = {}
                for run_id in sorted(not_done):
                    run = not_done[run_id]
                    attempt = int(run.get("run_attempt") or 1)
                    status = str(run.get("status") or "")
                    jobs_data = api("repos/%s/actions/runs/%d/jobs?per_page=%d"
                                    % (repo, run_id, GUARD_PER_PAGE)) or {}
                    jobs = jobs_data.get("jobs") or ()
                    if int(jobs_data.get("total_count") or 0) > len(jobs):
                        reason = "jobs_truncated"
                        break
                    snap = _guard_snapshot(run_id, attempt, status, jobs)
                    st.ci.observe_guard_run(
                        repo, run_id=run_id, attempt=attempt, status=status,
                        queued=int(snap["queued"]), oldest_id=snap["oldest_id"],
                        oldest_at=snap["oldest_at"], runners=snap["runners"])
                    covered += 1
                    queued_total += int(snap.get("queued") or 0)
                    if int(snap.get("queued") or 0):
                        queued.append((str(snap.get("oldest_at") or ""),
                                       int(snap.get("oldest_id") or 0)))
                    holding.update(snap.get("runners") or {})
                if reason is None:
                    data = api("repos/%s/actions/runners?per_page=%d"
                               % (repo, GUARD_PER_PAGE)) or {}
                    runners = data.get("runners") or ()
                    if int(data.get("total_count") or 0) > len(runners):
                        reason = "runner_truncated"
                    else:
                        if queued:
                            created, job_id = min(queued, key=lambda q: (q[0], q[1]))
                            st.ci.observe_queue(repo, oldest_id=job_id,
                                                oldest_at=created or None,
                                                queued=queued_total)
                        else:
                            st.ci.observe_queue(repo, oldest_id=None, oldest_at=None,
                                                queued=0)
                        state = []
                        for r in runners:
                            name = str(r.get("name") or "")
                            online = (r.get("status") or "online") == "online"
                            busy = bool(r.get("busy")) and online
                            state.append({"id": r.get("id"), "name": name, "busy": busy,
                                          "job": holding.get(name) if busy else None,
                                          "stuck": busy and name not in holding})
                        st.ci.observe_runners(repo, state)
                        complete = True
                        try:
                            st.ci.prune_guard_runs(
                                repo, [(int(rid), int(r.get("run_attempt") or 1))
                                       for rid, r in not_done.items()])
                        except Exception as exc:  # noqa: BLE001 - cache seulement
                            self.log("CI %s: guard cache not pruned (%s)"
                                     % (repo, " ".join(str(exc).split())[:120]))
        except _GuardBudget:
            reason = "budget"
        finally:
            if reason is None and not complete:
                reason = "failed"
            try:
                st.ci.finish_guard_scan(repo, now=now, calls=calls, runs=runs_seen,
                                        covered=covered, complete=complete,
                                        reason=reason)
            except Exception as exc:  # noqa: BLE001 - le relevé continue
                self.log("CI %s: guard scan state not recorded (%s)"
                         % (repo, " ".join(str(exc).split())[:120]))
            if not complete:
                self.log("CI %s: guard scan incomplete (%s): %d/%d runs, %d calls, "
                         "nothing published" % (repo, reason, covered, runs_seen, calls))

    def _follow(self, db, repo: str, run: dict, lots: list[dict],
                bases=BASE_BRANCHES) -> list[dict]:
        """Une exécution en cours : enregistrée si nouvelle ; une exécution
        connue sans lot rattaché l'est dès que la branche du lot est posée
        (#1321), tant que sa tête est encore celle de la branche ; une
        exécution connue que GitHub dit de nouveau en cours est une **relance**
        (#901 : `run_id` inchangé, `run_attempt` suivant) : sa ligne est
        rouverte et suivie, sauf si une tête plus récente de la branche l'a
        remplacée ou si l'attempt n'a pas augmenté (liste en retard, #828).
        Elle remplace les exécutions d'une autre tête sur la même
        branche (hors base)."""
        st = storage.of(db)
        run_id = int(run["id"])
        attempt = int(run.get("run_attempt") or 1)
        known = st.ci.run(repo, run_id)
        if known is not None:
            if known["status"] == "superseded":
                return []  # la tête de la branche a bougé : la relance est sans objet
            if known.get("work_item_id") is None:
                # #1321 : la branche du lot peut être posée après la première
                # vue du run ; sans ce rattachement, `_conclude` lit
                # `recipient_of(None)` et le verdict part à personne.
                # `open_run` est idempotent (coalesce côté stockage) : il ne
                # remplit que le lien encore vide.
                lot = lot_of_run(run, lots, bases)
                row = None
                if lot is not None:
                    row = next((l for l in lots if int(l["id"]) == lot), None)
                head = run.get("head_sha") or ""
                # une tête plus récente connue de la branche (`branch_head`,
                # posé par le relevé des branches) rend ce run périmé : le
                # rattacher enverrait un verdict d'une tête remplacée — et
                # l'exécution de remplacement peut être `pending`, invisible
                # au veilleur (#1161). Le silence vaut mieux qu'un faux vert.
                known_head = (row or {}).get("branch_head") or ""
                if lot is not None and not _obsolete_head(run, row, bases):
                    st.ci.open_run(repo, run_id, head_sha=head,
                                   branch=run.get("head_branch") or "",
                                   event=run.get("event") or "",
                                   base=base_of(run, default=bases[0]),
                                   work_item_id=lot, url=run.get("html_url"))
                    self.log("CI %s run %s: lot #%s linked (branch set later)"
                             % (repo, run_id, lot))
                elif lot is not None:
                    self.log("CI %s run %s: obsolete head %s (branch head %s), not linked"
                             % (repo, run_id, _short(head), _short(known_head)))
            known_attempt = int(known.get("run_attempt") or 1)
            if attempt <= known_attempt:
                # même attempt : soit la ligne est déjà suivie, soit une liste
                # d'exécutions en retard (#828) redit en cours une exécution
                # conclue sans qu'elle ait été relancée — la rouvrir renverrait
                # son verdict à chaque passe.
                return []
            reopened = st.ci.reopen_run(repo, run_id, run_attempt=attempt)
            if reopened is None:
                return []
            return [{"action": "rerun", "repo": repo, "run_id": run_id, "attempt": attempt,
                     "branch": reopened.get("branch"), "to": None}]
        st.ci.open_run(repo, run_id, head_sha=run.get("head_sha") or "",
                       branch=run.get("head_branch") or "", event=run.get("event") or "",
                       base=base_of(run, default=bases[0]),
                       work_item_id=lot_of_run(run, lots, bases),
                       url=run.get("html_url"), workflow=workflow_of(run.get("path")),
                       created_at=run.get("created_at"), run_attempt=attempt)
        branch = run.get("head_branch") or ""
        if not branch or branch.casefold() in _base_set(bases):
            return []
        out = []
        old = st.ci.supersede(repo, branch, run.get("head_sha") or "", run_id)
        if old:
            ids = st.ci.message_ids(repo, [r["run_id"] for r in old])
            withdrawn = st.mailbox.withdraw(ids, {
                "raison": "obsolete", "remplace_par": run.get("head_sha"),
                "execution": run_id, "ts": float(self.clock())}) if ids else []
            for r in old:
                out.append({"action": "superseded", "repo": repo, "run_id": r["run_id"],
                            "by": run.get("head_sha"), "withdrawn": withdrawn})
        return out

    def _unblock(self, db, repo: str, live: dict[int, dict], lots: list[dict]) -> list[dict]:
        """Les exécutions de PR qui attendent leur groupe de concurrence
        (#1161). Dans son périmètre prudent (même PR, même dépôt source,
        même workflow), le relevé annule les exécutions non démarrées d'un
        essai antérieur, jamais `main`, `release/…`, jamais la tête courante,
        jamais un essai dont l'antériorité n'est pas prouvée (un rerun reste,
        #901). La tête courante restée `pending` au-delà de PENDING_ALERT est
        signalée à l'assigné du lot."""
        groups: dict[tuple, list[dict]] = {}
        for run in live.values():
            branch = run.get("head_branch") or ""
            if not branch or protected_branch(branch) or not pull_request_run(run):
                continue
            key = run_group(run)
            if key is None:
                continue  # PR, dépôt source ou workflow non prouvés : jamais annulé
            groups.setdefault(key, []).append(run)
        now = float(self.clock())
        out: list[dict] = []
        heads: dict[str, str | None] = {}
        for key in sorted(groups):
            runs = groups[key]
            branch = runs[0].get("head_branch") or ""
            if len({r.get("head_sha") or "" for r in runs}) < 2 \
                    and not any((r.get("status") or "") == "pending" for r in runs):
                continue  # une seule tête visible : rien à libérer ni à signaler
            if branch not in heads:
                heads[branch] = self.branch_head(repo, branch)
            head = heads[branch]
            if not head:
                continue  # tête courante non prouvée : ni annulation, ni signal
            successors = [r for r in runs if (r.get("head_sha") or "") == head]
            if not successors:
                continue  # aucun essai de la tête courante dans ce groupe
            successor = max(successors,
                            key=lambda r: (attempt_ts(r) or 0.0, int(r["id"])))
            cancelled: list[int] = []
            for run in sorted(runs, key=lambda r: int(r["id"])):
                if (run.get("head_sha") or "") == head:
                    continue
                if (run.get("status") or "") not in CANCELLABLE_STATUSES:
                    continue
                if not proven_older(run, successor):
                    continue  # antériorité non prouvée : l'exécution est conservée
                run_id = int(run["id"])
                # la preuve est calculée sur la liste ; `cancel_run` la
                # revérifie sur la relecture fraîche avant le POST (#1161, R1)
                if self.cancel_run(repo, run_id, proof=proof_of(run, successor)):
                    cancelled.append(run_id)
                    out.append({"action": "cancelled", "repo": repo, "run_id": run_id,
                                "head_sha": run.get("head_sha"), "branch": branch,
                                "for": int(successor["id"]), "by": head})
            for run in sorted(runs, key=lambda r: int(r["id"])):
                if (run.get("status") or "") != "pending" \
                        or (run.get("head_sha") or "") != head:
                    continue  # une tête supplantée qui attend : annulée ci-dessus
                out += self._alert_pending(db, repo, run, lots, cancelled, now)
        return out

    def _alert_pending(self, db, repo: str, run: dict, lots: list[dict],
                       cancelled: list[int], now: float) -> list[dict]:
        """Signale à l'assigné une exécution de la tête courante restée
        `pending` plus de PENDING_ALERT (groupe de concurrence, #1161) : une
        fois par exécution et par vie du service — `ci_runs` ne porte pas ce
        message, l'additif #1161 reste sans migration."""
        from . import work
        key = (repo, int(run["id"]))
        if key in self._pending_alerted:
            return []
        started = attempt_ts(run)
        if started is None or now - started < PENDING_ALERT:
            return []
        self._pending_alerted.add(key)
        waited = now - started
        item_id = lot_of_run(run, lots)
        lot = work.get(db, int(item_id)) if item_id else None
        to = recipient_of(lot)
        if not to:
            return [{"action": "pending", "repo": repo, "run_id": int(run["id"]),
                     "to": None, "waited_s": int(waited), "cancelled": list(cancelled)}]
        # la forme des lignes `ci_runs` : `branch`, `run_id`, `url` — les textes
        # lisent l'exécution comme le reste du relevé
        view = dict(run, run_id=int(run["id"]), branch=run.get("head_branch") or "",
                    url=run.get("html_url") or run.get("url"),
                    workflow=workflow_of(run.get("path")))
        # #5044 : l'attente n'appelle aucun geste (passif), et un redémarrage du
        # service, qui oublie `_pending_alerted`, ne la redit pas.
        once = "ci-pending:%s:%s:%s" % (repo.replace("/", "."), int(run["id"]),
                                        int(run.get("run_attempt") or 1))
        if storage.of(db).mailbox.by_idempotency_key(work.SYSTEM_SENDER, to, once):
            return []
        text = pending_text(view, lot, waited, cancelled)
        payload = {"info": True, "idempotency_key": once,
                   "ci": {"repo": repo, "run_id": int(run["id"]),
                          "head_sha": run.get("head_sha"), "branch": run.get("head_branch"),
                          "kind": "pending", "workflow": workflow_of(run.get("path")),
                          "waited_s": int(waited), "cancelled": list(cancelled)}}
        message_id = _send(db, to, text, text, lot=lot["id"], payload=payload, urgent=False)
        return [{"action": "pending", "repo": repo, "run_id": int(run["id"]), "to": to,
                 "waited_s": int(waited), "cancelled": list(cancelled),
                 "message_id": message_id}]

    def _project_pilot(self, db, repo: str) -> str | None:
        """#2935 : le pilote du projet d'un dépôt, pour un échec de base sans
        lot.

        Le projet est celui que `github.projects` associe au dépôt ; le pilote
        est l'orchestrateur déclaré au canon (`alert_briefs.project_pilots`).
        Aucun rôle lisible : None, l'échec reste sans destinataire comme avant
        (le journal le dit)."""
        from . import alert_briefs, canon as canon_mod
        settings = self.settings()
        if settings is None:
            return None
        project = next((name for name, url in settings.projects.items()
                        if (url or "").casefold() == repo.casefold()), None)
        if not project:
            return None
        try:
            pilots = alert_briefs.project_pilots(
                project, storage.of(db).operations.listing(), cfg=self.cfg, db=db,
                canons=canon_mod.load_configured(self.cfg))
        except Exception as exc:  # noqa: BLE001 - jamais fatal au relevé
            self.log("CI %s: pilot of project %s unreadable (%s)"
                     % (repo, project, " ".join(str(exc).split())[:200]))
            return None
        if not pilots:
            self.log("CI %s: no pilot declared for project %s: base failure has no "
                     "recipient" % (repo, project))
            return None
        return pilots[0]

    def _attribution(self, db, lot, row: dict, failures: list[dict]):
        """#3801 : les unités probables d'un run rouge de paquet, jamais fatal.

        Le relevé est borné au porteur `integration/**` ; un défaut de lecture
        ou de téléchargement de carte rend un statut, pas une exception qui
        retiendrait le courrier.
        """
        if not lot or not failures:
            return None
        from . import packet_red
        shared = self._packet_shared.setdefault(str(row.get("repo") or "").strip(), {})
        try:
            return packet_red.attribute_run(db, lot, row, failures,
                                            cfg=self.cfg, api=self.api,
                                            shared=shared)
        except Exception as exc:  # noqa: BLE001 - le courrier reste dû
            self.log("CI %s run %s: packet attribution unreadable (%s)"
                     % (row.get("repo") or "?", row.get("run_id"), type(exc).__name__))
            return None

    def _failures(self, db, repo: str, row: dict, bases=BASE_BRANCHES,
                  terminal: str = "") -> list[dict]:
        """Les échecs nouveaux d'une exécution suivie, et leur message.

        `terminal` : le statut GitHub de l'exécution quand elle vient de
        conclure. Sur un rouge sans aucun événement de flux (dépôt sans
        `scripts/ci-flow.py`, Nexlink), les logs TAP des jobs en échec sont
        lus (#3783) ; le relevé ne les paie donc pas à chaque passage d'une
        exécution encore en cours.

        Un événement de l'attempt courant enregistré pendant que la ligne était
        encore à l'attempt précédent (liste en retard, #828) n'est pas
        réinséré par `add_failure` quand la ligne est ensuite rouverte : il est
        signalé ici tant qu'aucun message ne l'a porté (#1573 N2)."""
        from . import work
        st = storage.of(db)
        run_id, attempt = int(row["run_id"]), int(row.get("run_attempt") or 1)
        checks = self.api("repos/%s/commits/%s/check-runs?per_page=100"
                          % (repo, row["head_sha"])) or {}
        events = events_of(checks.get("check_runs"), int(row["run_id"]))
        self._record_failed_jobs(st, repo, row, bases)
        if not events and terminal == "failure":
            events = self._log_failures(repo, row)
        if not events:
            return []
        reference = st.ci.base_failures(repo, row.get("base") or bases[0],
                                        workflow=row.get("workflow")
                                        or workflow_of(row.get("path")))
        if reference and int(reference["run_id"]) == int(row["run_id"]):
            reference = {}
        # aucune exécution conclue de la base relevée (juste après le
        # déploiement, base peu active) : un échec peut être hérité sans
        # qu'on le sache, il n'est pas imputé par un urgent
        unknown_base = not reference
        before = set(reference.get("tests") or ())
        # les lignes déjà enregistrées pour l'attempt courant, avec leur
        # message : `add_failure` ne les réinsère pas (même attempt), mais
        # celles qu'aucun message n'a portées restent à signaler (#1573 N2)
        recorded = {(f["part"], f["test"]): f
                    for f in st.ci.failures(repo, int(run_id))}
        new, fresh = [], set()
        for ev in events:
            source = event_attempt(ev)
            failure = {"part": str(ev.get("part") or ""), "test": ev["test"],
                       "module": ev.get("module") or "", "status": ev.get("statut"),
                       "inherited": ev["test"] in before,
                       "excerpt": ev.get("extrait") or "", "replay": ev.get("rejouer") or "",
                       "duration_s": ev.get("duree_s"), "failed_at": ev.get("ts")}
            added = st.ci.add_failure(repo, run_id, run_attempt=source, **failure)
            if source == attempt:
                # une ligne de l'attempt précédent (check run pas encore
                # republié par la relance) est enregistrée sous le sien, jamais
                # imputée à l'attempt courant (#901)
                # #1786 N1 : la référence de la base peut changer entre deux
                # passes ; une ligne déjà inscrite sans message n'est
                # resignalée que si sa classe `inherited` n'a pas bougé — sinon
                # le message (calculé sur l'événement) et la ligne en base
                # (`add_failure` ne met pas à jour le même attempt)
                # divergeraient.
                seen = recorded.get((failure["part"], failure["test"]))
                if added or (seen is not None and not seen.get("message_id")
                             and bool(seen.get("inherited"))
                             == bool(failure["inherited"])):
                    new.append(failure)
                if added:
                    fresh.add((failure["part"], failure["test"]))
        own = [f for f in new if not f["inherited"]]
        if not own:
            return []
        lot = work.get(db, int(row["work_item_id"])) if row.get("work_item_id") else None
        to = recipient_of(lot)
        if not to and (row.get("branch") or "").casefold() in _base_set(bases):
            # #2935 : une poussée sur une base n'appartient souvent à aucun lot
            # (une fusion de paquet porte rarement la ligne `ameesh-work:`) ;
            # l'échec partait « (no recipient) ». Il va au pilote du projet.
            to = self._project_pilot(db, repo)
        if not to:
            # sans destinataire, aucun message ne portera jamais ces lignes :
            # seules les nouvelles sont journalisées, pas toutes à chaque passe.
            # #1612 : une ligne préinscrite sous l'attempt suivant n'est donc
            # pas rejournalisée à sa réouverture sans destinataire. Elle reste
            # en base pour ci.main_red ; aucun marqueur de courrier n'est créé.
            own = [f for f in own if (f["part"], f["test"]) in fresh]
            return [{"action": "failures", "repo": repo, "run_id": row["run_id"],
                     "tests": [f["test"] for f in own], "to": None}] if own else []
        # premier message de l'exécution (un passif sans base compte aussi)
        first = not row.get("urgent_message_id") and not any(
            f.get("message_id") for f in recorded.values())
        urgent = first and not unknown_base
        # #3801 : d'où vient probablement chaque échec dans le paquet
        attribution = self._attribution(db, lot, row, own)
        text = failure_text(row, lot, own, first=first, unknown_base=unknown_base,
                            attribution=attribution)
        short = failure_text(row, lot, own, first=first, unknown_base=unknown_base,
                             detail=False, attribution=attribution)
        payload = {"ci": {"repo": repo, "run_id": row["run_id"], "head_sha": row["head_sha"],
                          "branch": row["branch"], "kind": "failure",
                          "workflow": row.get("workflow") or workflow_of(row.get("path")),
                          "tests": [f["test"] for f in own]}}
        if unknown_base:
            payload["ci_passif"] = True
        elif row.get("urgent_message_id"):
            # #5044 : la première alerte a réveillé, le verdict reprendra la liste
            payload["info"] = True
        def pose(st, message_id: int) -> None:
            if urgent:
                st.ci.set_message(repo, int(row["run_id"]), "urgent", message_id)
                row["urgent_message_id"] = message_id
            st.ci.set_failure_message(repo, run_id, attempt,
                                      [(f["part"], f["test"]) for f in own], message_id)

        # #795 N2 : le courrier et sa preuve (`urgent_message_id`, `message_id`
        # des échecs) dans la même transaction : le NOTIFY du courrier n'est
        # délivré qu'au commit, quand l'exécuteur peut déjà le vérifier.
        message_id = _send_posed(db, to, text, short,
                                 lot=(lot or {}).get("id"), payload=payload,
                                 urgent=urgent, pose=pose)
        return [{"action": "failures", "repo": repo, "run_id": row["run_id"],
                 "tests": [f["test"] for f in own], "to": to, "urgent": urgent,
                 "message_id": message_id}]

    def _log_failures(self, repo: str, row: dict) -> list[dict]:
        """#3783 : les tests en échec lus dans les logs TAP des jobs rouges.

        Uniquement quand l'exécution est conclue rouge et qu'aucun événement
        de flux n'a été publié : Nexlink lance `node --test` sans le publisher
        d'`scripts/ci-flow.py`, donc `ci_test_failures` restait vide et le
        verdict disait « no failed test seen ». Le job lui-même n'est pas un
        test : un log sans ligne `not ok` ne rend aucun échec (une panne
        d'installation reste non concluante, canon §3), il n'est pas inventé.
        Les jobs déjà lus sont retenus par `(dépôt, exécution, tentative,
        job)` : un passage suivant ne retélécharge pas leur log."""
        if self.logs is None:
            return []
        run_id = int(row["run_id"])
        attempt = int(row.get("run_attempt") or 1)
        try:
            jobs = self.api("repos/%s/actions/runs/%s/attempts/%s/jobs?per_page=100"
                            % (repo, run_id, attempt)) or {}
        except Exception as exc:  # noqa: BLE001 - jamais fatal au relevé
            key = (repo, run_id, attempt, "jobs")
            if key not in self._warned_logs:
                self._warned_logs.add(key)
                self.log("CI %s run %s: job list unreadable (%s)"
                         % (repo, run_id, " ".join(str(exc).split())[:200]))
            return []
        out: list[dict] = []
        for job in jobs.get("jobs") or ():
            if (job.get("conclusion") or "") not in FAILED_JOB_CONCLUSIONS:
                continue
            try:
                job_id = int(job.get("id"))
            except (TypeError, ValueError):
                continue
            key = (repo, run_id, attempt, job_id)
            if key in self._parsed_logs:
                continue
            try:
                text = self.logs(repo, job_id)
            except Exception as exc:  # noqa: BLE001 - un log absent n'est pas un verdict
                if key not in self._warned_logs:
                    self._warned_logs.add(key)
                    self.log("CI %s run %s job %s: log unreadable (%s)"
                             % (repo, run_id, job_id, " ".join(str(exc).split())[:200]))
                continue
            self._parsed_logs.add(key)
            part = str(job.get("name") or JOB_PART)
            for tap in tap_failures(text):
                out.append({"v": 1, "schema": SCHEMA, "type": "test",
                            "execution": run_id, "attempt": attempt,
                            "part": part, "module": tap.get("module") or "",
                            "test": tap["test"], "statut": "echec",
                            "extrait": tap.get("excerpt") or "",
                            "rejouer": "", "duree_s": None,
                            "ts": tap.get("failed_at")})
        return out

    def _record_failed_jobs(self, st, repo: str, row: dict,
                            bases=BASE_BRANCHES) -> None:
        """#846 : sur une branche de base, un job déjà en échec est un rouge de
        la base, même sans événement de test publié (panne d'installation, délai
        dépassé). Relevé ici pour que `ci.main_red` alerte sans attendre la
        conclusion de l'exécution. Aucun courrier n'en part : `_failures` ne
        signale que les événements de test (part `job` mise à part). `bases` :
        les branches de base du dépôt (#1483).
        #901 B1 : les jobs sont ceux de l'attempt suivi, pas ceux de la tête —
        un échec de l'attempt précédent encore publié n'est jamais inscrit sous
        la relance, un vrai échec de l'attempt courant l'est."""
        if (row.get("branch") or "").casefold() not in _base_set(bases):
            return
        run_id = int(row["run_id"])
        attempt = int(row.get("run_attempt") or 1)
        jobs = self.api("repos/%s/actions/runs/%s/attempts/%s/jobs?per_page=100"
                        % (repo, run_id, attempt)) or {}
        for job in failed_jobs_of(jobs.get("jobs")):
            st.ci.add_failure(repo, run_id, run_attempt=attempt,
                              part=JOB_PART, test=job,
                              module="", status="echec", inherited=False,
                              excerpt="", replay="", duration_s=None, failed_at=None)

    def _conclude(self, db, repo: str, row: dict, *, run: dict | None = None,
                  pending: list[dict] | None = None) -> list[dict]:
        """Une exécution qui n'est plus en cours : son verdict. `run` : son
        dernier état GitHub, déjà relevé ; `pending` : les autres workflows de
        la même tête encore en cours, que le verdict nomme."""
        st = storage.of(db)
        if run is None:
            run = self.api("repos/%s/actions/runs/%s" % (repo, row["run_id"])) or {}
        if (run.get("status") or "") != "completed":
            return []  # en attente d'un runner, ou relancée : au prochain relevé
        # Le détail GitHub porte le dernier attempt, même si la réouverture
        # a été refusée (tête plus récente sur la branche, #1612). Son verdict
        # ne doit jamais conclure une autre tentative encore suivie en base :
        # la ligne est conclue par l'état de son propre attempt, sans quoi
        # elle resterait `running` sans fin et son verdict serait perdu.
        attempt = int(row.get("run_attempt") or 1)
        if int(run.get("run_attempt") or 1) != attempt:
            run = self.api("repos/%s/actions/runs/%s/attempts/%s"
                           % (repo, row["run_id"], attempt)) or {}
            if (run.get("status") or "") != "completed" \
                    or int(run.get("run_attempt") or 0) != attempt:
                return []
        status = _CONCLUSIONS.get(run.get("conclusion") or "", "failure")
        if not st.ci.conclude(repo, int(row["run_id"]), status):
            return []
        if status == "cancelled":
            return self._cancelled_last(db, repo, row, pending) or [
                {"action": "verdict", "repo": repo, "run_id": row["run_id"],
                 "status": status, "to": None}]
        return self._verdict(db, repo, row, run, status, pending=pending)

    def _cancelled_last(self, db, repo: str, row: dict,
                        pending: list[dict] | None) -> list[dict]:
        """#5044 : le dernier workflow d'une tête est annulé, il ne rend aucun
        verdict ; ceux des autres, passifs, attendaient ce dernier pour
        réveiller. Un avis prend sa place, sinon l'agent au repos ne les lit
        jamais. Une tête remplacée n'en a pas besoin : la nouvelle réveillera."""
        from . import mail, work
        if not row.get("work_item_id") or _same_lot(row, pending) \
                or storage.of(db).ci.head_replaced(repo, row["branch"], row["head_sha"],
                                                   int(row["run_id"])):
            return []
        st = storage.of(db)
        held = [r for r in st.ci.for_lot(row["work_item_id"], 50)
                if r.get("head_sha") == row.get("head_sha") and r.get("verdict_message_id")
                and mail.passive_reason(st.mailbox.get(r["verdict_message_id"]) or {})]
        lot = work.get(db, int(row["work_item_id"]))
        to = recipient_of(lot)
        if not held or not to:
            return []
        text = ("%s: cancelled, and it was the last workflow of this head. Verdicts of "
                "the others, in your mail: %s."
                % (_head(row, lot), ", ".join("%s %s" % (r.get("workflow") or r["run_id"],
                                                         r.get("status")) for r in held)))
        payload = {"ci": {"repo": repo, "run_id": row["run_id"], "head_sha": row["head_sha"],
                          "branch": row["branch"], "kind": "cancelled_last",
                          "workflow": row.get("workflow") or ""}}
        message_id = _send(db, to, text, text, lot=lot["id"], payload=payload, urgent=False)
        return [{"action": "verdict", "repo": repo, "run_id": row["run_id"],
                 "status": "cancelled", "to": to, "message_id": message_id}]

    def _verdict(self, db, repo: str, row: dict, run: dict, status: str, *,
                 pending: list[dict] | None = None) -> list[dict]:
        """Le verdict d'une exécution conclue, à l'assigné du lot rattaché
        (#2021 : délivré aussi pour une ligne terminale rattachée tardivement)."""
        from . import work
        st = storage.of(db)
        failures = st.ci.failures(repo, int(row["run_id"]))
        lot = work.get(db, int(row["work_item_id"])) if row.get("work_item_id") else None
        to = recipient_of(lot)
        if not to:
            return [{"action": "verdict", "repo": repo, "run_id": row["run_id"],
                     "status": status, "to": None}]
        # le verdict situe la suite : fichier du workflow et attempt du dernier
        # état GitHub, quand la ligne ne les porte pas encore (#901, #1110)
        view = dict(row, run_attempt=run.get("run_attempt") or row.get("run_attempt") or 1,
                    path=run.get("path") or row.get("path") or "")
        own = [f for f in failures if not f.get("inherited")]
        attribution = self._attribution(db, lot, row, own)
        text = verdict_text(view, lot, status, failures, pending=pending,
                            attribution=attribution)
        payload = {"ci": {"repo": repo, "run_id": row["run_id"], "head_sha": row["head_sha"],
                          "branch": row["branch"], "kind": "verdict", "status": status,
                          "workflow": view.get("workflow") or workflow_of(view.get("path")),
                          "attempt": int(view.get("run_attempt") or 1),
                          "failures": len([f for f in failures if not f.get("inherited")]),
                          "inherited": len([f for f in failures if f.get("inherited")])}}
        # #514 : la fin de CI réveille l'agent (événement ordinaire), elle ne
        # perce pas le tour en cours ; le rouge perce déjà par la première
        # alerte d'échec, urgente et vérifiable en base (`interrupt_allowed`).
        # #5044 : un autre workflow de la tête tourne encore, et ce verdict
        # n'apporte aucun signal neuf (vert, ou rouge déjà annoncé par l'alerte
        # urgente) : passif, le dernier verdict réveille.
        if _same_lot(row, pending) and (status == "success" or row.get("urgent_message_id")):
            payload["info"] = True
        # #795 N2 : le courrier du verdict et `verdict_message_id` restent
        # publiés dans la même transaction (un relevé intercalé ne voit jamais
        # l'un sans l'autre).
        message_id = _send_posed(
            db, to, text, text, lot=lot["id"], payload=payload, urgent=False,
            pose=lambda st, mid: st.ci.set_message(repo, int(row["run_id"]),
                                                   "verdict", mid))
        return [{"action": "verdict", "repo": repo, "run_id": row["run_id"], "status": status,
                 "to": to, "message_id": message_id}]


def describe(record: dict) -> str:
    """Une ligne de journal de `ameesh notify` pour un envoi ou un retrait."""
    where = "CI %s run %s" % (record.get("repo"), record.get("run_id"))
    if record["action"] == "superseded":
        return "%s superseded by %s; %d unread message(s) withdrawn" % (
            where, _short(record.get("by") or ""), len(record.get("withdrawn") or ()))
    if record["action"] == "rerun":
        return "%s re-run attempt %s: tracking reopened" % (where, record.get("attempt"))
    if record["action"] == "cancelled":
        return "%s: superseded run cancelled to free the concurrency group (for run %s)" % (
            where, record.get("for"))
    to = "→ %s%s" % (record["to"], " (urgent)" if record.get("urgent") else "") \
        if record.get("to") else "(no recipient)"
    if record["action"] == "failures":
        return "%s: %d failure(s) %s" % (where, len(record.get("tests") or ()), to)
    if record["action"] == "pending":
        return "%s: pending %d min %s" % (where, int(record.get("waited_s") or 0) // 60, to)
    return "%s: verdict %s %s" % (where, record.get("status"), to)


# --------------------------------------------------------------------------
# alerte ci_main_red
# --------------------------------------------------------------------------

def main_red(db, bases=BASE_BRANCHES, *, bases_for=None) -> list[dict]:
    """Les exécutions des branches de base qui sont rouges : la dernière
    exécution conclue de chaque workflow (un succès d'une suite ne masque pas
    l'échec d'une autre, #1110), et une exécution encore en cours dont un échec
    est déjà relevé (#846 : un main rouge ne reste pas silencieux derrière ses
    jobs agrégateurs, qui peuvent attendre un runner des heures).
    Une exécution en cours plus ancienne que la dernière conclue de son
    workflow est écartée : une exécution supprimée ou introuvable (l'API rend
    `{}`) n'est jamais conclue, et son job rouge garderait l'alerte, qui bloque
    les déploiements, sans fin (#1453).
    `bases` : toutes les branches de base configurables des dépôts (#1483 ;
    `Settings.all_bases`, le défaut `BASE_BRANCHES` restant à la charge de
    l'appelant).
    `bases_for(repo)` : les branches de base propres à un dépôt (#1683,
    `bases_for_repo`) ; sans lui, `bases` s'applique à tous les dépôts (appel
    historique) — une branche d'un dépôt qui porte le nom de la base d'un autre
    serait alors lue comme une base.
    `{repo, branch, run_id, head_sha, url, work_item_id, concluded_ts, tests}`."""
    st = storage.of(db)

    def on_base(row) -> bool:
        if bases_for is None:
            return True
        return (row.get("branch") or "").casefold() in _base_set(bases_for(row["repo"]))

    out, newest = [], {}
    for row in st.ci.latest_concluded(bases):
        if not on_base(row):
            continue
        key = (row["repo"], row["branch"], row.get("workflow"))
        newest[key] = max(newest.get(key, 0), int(row["run_id"]))
        if row.get("status") != "failure":
            continue
        tests = [f["test"] for f in st.ci.failures(row["repo"], int(row["run_id"]))]
        out.append(dict(row, tests=tests))
    for row in st.ci.running_on(bases):
        if not on_base(row):
            continue
        if int(row["run_id"]) < newest.get(
                (row["repo"], row["branch"], row.get("workflow")), 0):
            continue
        # hérités exclus : un échec déjà rouge à la dernière exécution conclue
        # de la base est déjà porté par l'alerte de celle-ci, de même clé
        own = [f["test"] for f in st.ci.failures(row["repo"], int(row["run_id"]))
               if not f.get("inherited")]
        if own:
            out.append(dict(row, tests=own))
    return out


def wait_text(run: dict, now: float) -> str:
    """#747 : attente d'un runner et durée de l'exécution, séparées."""
    created = run.get("created_ts") or run.get("first_seen_ts")
    started, end = run.get("started_ts"), run.get("concluded_ts")
    if created is None:
        return ""
    if started is None:
        if run.get("status") == "running":
            return "queued %d min" % ((now - float(created)) // 60)
        return ""
    return "queued %d min, ran %d min" % ((float(started) - float(created)) // 60,
                                         ((end or now) - float(started)) // 60)


# --------------------------------------------------------------------------
# ameesh ci status
# --------------------------------------------------------------------------

def status_of(db, item_id: int, limit: int = 5, *, cfg=None) -> dict:
    from . import packet_red, work
    item = work.get(db, int(item_id))
    if item is None:
        raise CiError("lot #%s unknown" % item_id)
    cfg = cfg if cfg is not None else getattr(db, "cfg", None)
    st = storage.of(db)
    rows = []
    for row in st.ci.for_lot(int(item_id), limit):
        failures = st.ci.failures(row["repo"], int(row["run_id"]))
        rows.append((row, failures))
    # #3801 : la carte et le clone sont les mêmes pour tous les runs du paquet
    shared: dict = {}
    runs = []
    for row, failures in rows:
        view = dict(row, failures=[{k: f.get(k) for k in (
            "part", "test", "module", "status", "inherited", "replay",
            "failed_ts")} for f in failures])
        own = [failure for failure in failures if not failure.get("inherited")]
        if packet_red.is_carrier(item) and own:
            view["attribution"] = packet_red.attribute_run(
                db, item, row, own, cfg=cfg, shared=shared)
        runs.append(view)
    report = {"schema": STATUS_SCHEMA, "work_item": int(item_id), "title": item.get("title"),
              "branch": item.get("branch"), "runs": runs}
    carrier = _carrier_of(db, item)
    if carrier is not None:
        report["carrier"] = carrier
    return report


def _carrier_of(db, item: dict) -> dict | None:
    """#3611 : la publication d'un porteur `integration/**` gelé, lue au
    journal (`packet_automerge.publication`) : sans demande `ci-full`, le run
    push de son workflow de paquet ne produit pas le check requis de la cible
    — et ce veilleur ne suit pas ce workflow (`WORKFLOWS`). None hors porteur
    ou sans gel."""
    from . import packet_automerge
    if not packet_automerge.carrier(item.get("branch")):
        return None
    frozen = frozen_of(db, int(item["id"]))
    sha = str((frozen or {}).get("sha") or "").strip().lower()
    if not sha:
        return None
    return dict(packet_automerge.publication(db, int(item["id"]), sha), sha=sha)


_STATUS_FR = {"running": "running", "success": "green", "failure": "red",
              "cancelled": "cancelled", "superseded": "superseded"}


def render_status(report: dict) -> list[str]:
    from .notify import local_time
    now = time.time()
    lines = ["lot #%d \"%s\" (branch %s)" % (report["work_item"], report.get("title") or "",
                                          report.get("branch") or "—")]
    if not report["runs"]:
        lines.append("  no CI run seen (seen by `ameesh notify` on the host "
                     "named by github.host)")
    carrier = report.get("carrier")
    if carrier:
        sha, lot = carrier["sha"][:12], report["work_item"]
        if carrier.get("armed"):
            lines.append("  packet carrier: auto-merge armed on %s → %s (PR #%s): ameesh "
                         "merges it once the required checks are green"
                         % (sha, carrier.get("base") or "?", carrier.get("pr")))
        elif carrier.get("requested"):
            lines.append("  packet carrier: ci-full requested on %s but the auto-merge is not "
                         "armed (see the lot's `auto-merge not-armed` note): rerun `ameesh ci "
                         "run %d --full --reason …`" % (sha, lot))
        else:
            # #3614 : le journal ne dit pas si la forge impose des checks à la
            # cible ; sans check imposé (Nexlink), le feu atterrit le porteur
            # (`fire._carrier_gap`) — la ligne reste conditionnelle.
            lines.append("  packet carrier: no ci-full request on the frozen head %s — if the "
                         "target's forge requires checks (ameesh: `tests` on main), a push run "
                         "of the packet workflow does not produce them and nothing merges it: "
                         "open its PR if none, then `ameesh ci run %d --full --reason …` "
                         "(#2813); without required checks (Nexlink), the fire lands it. The "
                         "journal alone does not tell which: the fire reads the protection and "
                         "answers `not-ready` with this cause when it applies (#3611)"
                         % (sha, lot))
    from . import packet_red
    for run in report["runs"]:
        state = _STATUS_FR.get(run["status"], run["status"])
        attempt = int(run.get("run_attempt") or 1)
        if attempt > 1:
            state += " (attempt %d)" % attempt
        if run["status"] == "superseded" and run.get("superseded_by"):
            state += " by %s" % _short(run["superseded_by"])
        own = [f for f in run["failures"] if not f.get("inherited")]
        inherited = len(run["failures"]) - len(own)
        wait = wait_text(run, now)
        lines.append("  %s  %s  %s  %s  %s%s%s%s" % (
            local_time(run["first_seen_ts"]), _short(run["head_sha"]),
            run.get("workflow") or "—", state,
            "%d failure(s)" % len(own), ", %d inherited" % inherited if inherited else "",
            "  (%s)" % wait if wait else "",
            "  %s" % run["url"] if run.get("url") else ""))
        attribution = run.get("attribution") or {}
        by_test = packet_red.tests_by_name(attribution)
        for f in own[:10]:
            lines.append("      %s (%s)%s" % (f["test"], f.get("part") or "?",
                                              "  → %s" % f["replay"] if f.get("replay") else ""))
            entry = by_test.get(f["test"])
            if entry:
                lines.append("        unités probables : %s"
                             % packet_red.evidence_line(entry))
        if len(own) > 10:
            lines.append("      … and %d more" % (len(own) - 10))
        if attribution and attribution.get("status") in ("no-manifest", "no-files") \
                and attribution.get("why"):
            lines.append("        unités probables : non attribuable (%s)"
                         % attribution["why"])
        note = packet_red.note_of(attribution)
        if note:
            lines.append("      %s" % note)
    return lines


def quota_line(state_dir, *, now=None) -> str | None:
    """Une ligne lisible sur les lectures GitHub partagées de l'hôte (#848),
    ou None tant qu'aucune lecture n'a été comptée.

    Elle dit l'économie (requêtes, 304, cache servi) et le quota restant par
    ressource d'API (`core`, `graphql`…) : de quoi lire la CI sans ajouter
    d'appel, et voir venir l'épuisement — sans confondre les buckets."""
    from . import plan_github

    stats = plan_github.quota_stats(state_dir)
    counters, quotas = stats["counters"], stats["quota"]
    if not counters and not quotas:
        return None
    parts = []
    requests = int(counters.get("requests") or 0)
    hits = int(counters.get("hits") or 0)
    if requests or hits:
        parts.append("%d request(s) (%d not modified), %d served from the shared cache"
                     % (requests, int(counters.get("not_modified") or 0), hits))
    now = time.time() if now is None else float(now)
    for resource, quota in sorted(quotas.items()):
        if not isinstance(quota, dict):
            continue
        remaining = quota.get("remaining")
        if remaining is not None:
            label = "quota" if resource == plan_github.DEFAULT_RESOURCE \
                else "quota %s" % resource
            parts.append("%s %s/%s" % (label, remaining, quota.get("limit", "?")))
        blocked = float(quota.get("blocked_until") or 0.0)
        if blocked > now:
            suffix = "" if resource == plan_github.DEFAULT_RESOURCE else " (%s)" % resource
            parts.append("exhausted until %s%s"
                         % (time.strftime("%H:%M:%SZ", time.gmtime(blocked)), suffix))
    return "GitHub reads (shared): " + ", ".join(parts) if parts else None
# --------------------------------------------------------------------------
# ameesh ci run / ameesh ci cancel — la CI d'un lot part au gel (#1343)
# --------------------------------------------------------------------------
#
# canon §3 (manaty/home#66, owner 14/10 21:45) : sur ameesh, la relecture passe
# avant la CI. Une poussée sur une PR de lot ne lance plus rien ; la CI du lot
# (impactés + contrat) ne tourne qu'une fois, sur la tête gelée, quand les
# verdicts requis sont acquis. `ameesh ci run <lot>` porte ce déclencheur :
#
#   * le lot porte un gel (`ameesh work milestone <lot> frozen --sha S`), sa
#     branche distante ET la tête de sa PR ouverte sont encore sur S — une
#     tête changée est refusée, jamais un vert sur une autre tête ;
#   * `--avis N` verdicts `ok` sur S, d'acteurs distincts et hors de l'assigné
#     (0 pour un lot léger, 1 pour un lot sensible, 2 pour SQL/sécurité/prod) ;
#   * la PR doit exister : le déclencheur est l'étiquette `ci-lot-<S>` (impactés
#     + contrat) ou `ci-full-<S>` (suite complète), posée par la commande sur
#     la PR (`pull_request` `types: [labeled]`). Le job `plan` compare
#     exactement ce SHA à `pull_request.head.sha`, vérifie que le checkout est
#     le merge-ref de cette tête composé de la pointe cible figée dans la
#     description de l'étiquette (ou d'une pointe qui n'en diffère que par des
#     atterrissages de coupure, #4533), puis sélectionne contre sa base réelle
#     (#2328 : `pull_request.base.sha` peut être périmé) ;
#   * le jeton `gh` doit produire l'événement `pull_request: labeled` : un
#     `GITHUB_TOKEN` d'Actions (acteur `github-actions[bot]`) ne déclenche
#     aucun workflow et est refusé ;
#   * idempotence bornée au parcours PR et à la composition courante (numéro
#     de PR, tête gelée S, pointe cible demandée, mode) : un vert ou un run en vol de
#     cette composition rend la main sans rien toucher ; un push vert de la
#     même tête ne remplace pas le check de la PR, pas plus qu'un ancien
#     dispatch ; une base qui a bougé demande une nouvelle demande ;
#   * un terminal rouge ou annulé de la composition, ou un check requis rouge
#     laissé par une demande non applicable (étiquette étrangère), demande
#     `--force` (reprise explicite) : la commande retire alors l'étiquette et
#     la repose pour produire un nouvel événement. Jamais de retrait/repose sur
#     un vert applicable ou un run en vol, ni pour rejouer automatiquement.
#
# La commande est seule à retirer/reposer ses étiquettes ; une pose à la main
# (propriétaire, pilote) demande de vrais tests, elle ne prouve ni gel ni avis
# et ne dispense pas des gardes de fusion. Une étiquette étrangère (ni
# `ci-lot-*` ni `ci-full-*`) fait échouer le plan : le check requis rougit sans
# exécuter de tests, et sa concurrency est distincte pour ne pas annuler un run
# gelé valide.
#
# Avant de reposer, les runs non conclues de la branche qui ne portent pas la
# composition courante sont annulés (canon §3 : un run lancé sur un lot qui
# attend encore un verdict ou une correction est annulé) ; un échec
# d'annulation est dit, jamais fatal.
#
# Exceptions (canon §3) : un lot qui débloque la CI ou corrige un incident
# demande sa CI en parallèle de sa relecture, `--exception incident|unblock
# --reason …` ; le motif et la marque restent dans la trace du lot. `--full
# --reason …` demande la suite complète sur une PR ordinaire ; une source ou
# une cible `release/**` ou `integration/**` l'impose et refuse `ci-lot`. Le
# mode `full` ne dispense jamais des avis requis (#2214) : seul `--exception`
# les suspend, avec son motif.
#
# `ameesh ci cancel <lot>` fait la même annulation seule (`--all` : tout, y
# compris le run de la composition gelée).
#
# #2364 : une demande dont le run-name est illisible (ancien workflow/CLI : la
# base du titre n'est pas un SHA, mais la description de l'étiquette) reste
# corrélable par sa tête et son libellé `ci-lot|full-<sha>`. Un tel run
# matérialise la réservation — `--force` reprend au lieu de répondre « already
# reserved » — sans jamais verdir la composition, dont la base reste inconnue.

GEL_SCHEMA = "ameesh-ci-gel/1"
CANCEL_SCHEMA = "ameesh-ci-cancel/1"
#: exceptions du canon §3 : la CI part en parallèle de la relecture, marquée
EXCEPTIONS = ("incident", "unblock")
#: états GitHub d'un run qui n'a pas conclu (LIVE_STATUSES en est un sous-ensemble)
OPEN_STATUSES = ("queued", "in_progress", "requested", "waiting", "pending")
#: conclusions d'un check requis considéré vert (règles GitHub)
CHECK_GREEN = ("success", "neutral", "skipped")
#: nombre d'avis que `--avis` accepte (léger, sensible, SQL/sécurité/prod)
MAX_AVIS = 2
#: durée pendant laquelle une étiquette posée mais pas encore visible dans
#: l'API GitHub tient la clé : au-delà, un nouvel essai est permis (le run ne
#: viendra plus, ou GitHub l'a perdu).
RESERVE_TTL_S = 15 * 60
#: l'étiquette qui demande la CI d'un gel : mode et SHA complet (#1343)
LABEL_RE = re.compile(r"^ci-(?P<mode>lot|full)-(?P<sha>[0-9a-f]{40})$")
#: le titre du run (run-name de tests.yml) : PR, étiquette, pointe cible demandée
PR_RUN_RE = re.compile(
    r"\[PR #(?P<number>\d+) (?P<label>ci-(?:lot|full)-[0-9a-f]{40}) "
    r"base (?P<base>[0-9a-f]{40})\]")
#: le libellé d'une demande dans un titre dont la base est illisible (#2364) :
#: un ancien workflow y mettait la description de l'étiquette au lieu de la base.
CARRIED_RE = re.compile(r"\bci-(?P<mode>lot|full)-(?P<sha>[0-9a-f]{40})\b")
#: le numéro de PR d'un titre, même quand sa base n'est pas un SHA
CARRIED_PR_RE = re.compile(r"\[PR #(?P<number>\d+)\b")
#: la marque de demande dans le journal du lot : `reserve` prend le verrou de
#: la ligne, `applied` la referme sur une étiquette posée, `released` sur un
#: refus. Le motif ne dépend pas du texte qui suit. Une marque `applied` porte
#: `seen=<id>` (P2-3) : le plus grand identifiant d'exécution connu avant sa
#: pose, seule corrélation fiable avec le run qu'elle a produit (les horloges
#: de la base et de GitHub ne se comparent pas : la seconde contre la fraction).
#: `avis=<N>` (N2 #2240) garde le nombre d'avis déclaré par l'appelant, y
#: compris 0 : `--avis 0 --full` reste lisible dans la trace du lot.
REQUEST_RE = re.compile(
    r"^ci-label (?P<state>reserve|applied|released) pr=(?P<pr>\d+) "
    r"label=(?P<label>ci-(?:lot|full)-[0-9a-f]{40}) sha=(?P<sha>[0-9a-f]{40}) "
    r"base=(?P<base>[0-9a-f]{40}) mode=(?P<mode>lot|full) token=(?P<token>[0-9a-f]{16})"
    r"(?: avis=(?P<avis>\d+))?(?: seen=(?P<seen>\d+))?\b")
#: l'acteur du `GITHUB_TOKEN` d'Actions : ses étiquettes ne déclenchent rien
ACTIONS_BOT = "github-actions[bot]"


class GelError(CiError):
    """Demande de gel : refus actionnable, jamais un run silencieux."""


def _q(value: str) -> str:
    """Une valeur de chemin ou de requête GitHub (une branche `lot/x` incluse)."""
    from urllib.parse import quote
    return quote(value or "", safe="")


def _sha_ok(sha: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", sha or ""))


def _run_id(row: dict) -> int:
    """L'identifiant entier d'une exécution, 0 s'il est illisible (P2-3)."""
    try:
        return int(row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def label_name(mode: str, sha: str) -> str:
    """L'étiquette d'une demande : `ci-lot-<40hex>` ou `ci-full-<40hex>`."""
    if mode not in ("lot", "full"):
        raise GelError("label mode: %r (lot or full)" % mode)
    if not _sha_ok(sha):
        raise GelError("label sha: full 40-hex expected, not %r" % sha)
    return "ci-%s-%s" % (mode, sha)


def repo_of(cfg, item: dict, *, repo: str = "") -> str:
    """Explicit repository, lot metadata, then origin as a last resort.

    Only an explicit dot selects the caller's clone over the lot metadata;
    it must still agree with the known project to avoid testing another repo.
    """
    from . import plan_github
    value = repo.strip()
    if value and value != ".":
        return plan_github.check_repo(value)
    parts = plan_github._ref_parts(item.get("issue_ref"))
    expected = parts[0] if parts and parts[0] else None
    if not expected:
        try:
            expected = plan_github.settings_of(cfg).repo_for(item.get("app"))
        except plan_github.SettingsError as exc:
            raise GelError(str(exc)) from exc
    if not value and expected:
        return expected
    local = _local_repo()
    if expected and local.casefold() != expected.casefold():
        raise GelError("origin repository %s differs from lot #%s repository %s; "
                       "use the project's clone or --repo %s"
                       % (local, item.get("id"), expected, expected))
    return local


def _local_repo() -> str:
    """Resolve origin before constructing an API path; never send repos/./… ."""
    import subprocess
    from . import plan_github
    try:
        result = subprocess.run(["git", "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=10)
        match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$",
                          result.stdout.strip()) if result.returncode == 0 else None
        if match:
            return plan_github.check_repo(match.group(1))
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise GelError("cannot resolve origin to a GitHub owner/name; pass --repo owner/name")


def frozen_of(db, item_id: int) -> dict | None:
    """Le dernier jalon `frozen` du lot, ou None (le lot n'est pas gelé)."""
    from . import work
    return next((m for m in work.milestones(db, int(item_id))
                 if m["kind"] == "frozen"), None)


def latest_verdicts(db, item_id: int, sha: str, assignee: str) -> list[dict]:
    """Le dernier verdict de chaque acteur sur `sha`, du plus récent au plus ancien.

    Un verdict `ok` ancien ne survit jamais au `blocked` plus récent du même
    acteur sur la même tête (P1 R1) : c'est le dernier mot de chaque relecteur
    qui vaut, pas le meilleur. L'assigné ne compte pas : son verdict n'est pas
    un avis indépendant.
    """
    from . import work
    who = (assignee or "").strip().casefold()
    out, seen = [], set()
    for m in work.milestones(db, int(item_id)):      # du plus récent au plus ancien
        if m["kind"] != "verdict":
            continue
        if (m.get("sha") or "").strip().casefold() != (sha or "").casefold():
            continue
        actor = (m.get("actor") or "").strip()
        key = actor.casefold()
        if not actor or key == who or key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def independent_verdicts(db, item_id: int, sha: str, assignee: str) -> list[dict]:
    """Les verdicts `ok` retenus : le dernier de chaque acteur, hors assigné."""
    return [m for m in latest_verdicts(db, item_id, sha, assignee)
            if (m.get("verdict") or "") == "ok"]


def blocked_verdicts(db, item_id: int, sha: str, assignee: str) -> list[dict]:
    """Les têtes refusées : le dernier verdict de l'acteur est `blocked`."""
    return [m for m in latest_verdicts(db, item_id, sha, assignee)
            if (m.get("verdict") or "") == "blocked"]


def release_refs(head_ref: str, base_ref: str) -> bool:
    """Source ou cible `release/**`/`integration/**` : la suite complète."""
    return any((ref or "").strip().startswith(("release/", "integration/"))
               for ref in (head_ref, base_ref))


def _head_of(api, repo: str, branch: str) -> str:
    data = api("repos/%s/branches/%s" % (repo, _q(branch))) or {}
    sha = ((data.get("commit") or {}).get("sha") or "").strip().lower()
    if not _sha_ok(sha):
        raise GelError("branch %s not found in %s" % (branch, repo))
    return sha


def _runs_of(api, repo: str, *, sha: str = "", branch: str = "",
             event: str = "", workflows=WORKFLOWS) -> list[dict]:
    query = "per_page=50"
    if sha:
        query += "&head_sha=%s" % _q(sha)
    if branch:
        query += "&branch=%s" % _q(branch)
    if event:
        query += "&event=%s" % _q(event)
    rows = []
    for workflow in workflows:
        data = api("repos/%s/actions/workflows/%s/runs?%s"
                   % (repo, _q(workflow), query)) or {}
        rows.extend(dict(row, path=row.get("path") or ".github/workflows/" + workflow)
                    for row in data.get("workflow_runs") or ())
    return rows


def _dispatch_plan(db, cfg, item, api, repo, branch, sha) -> dict:
    """Use declared/discovered suites and the repository's own due-parts plan.

    No project-specific workflow list lives here. A repository that provides
    dispatch-due-parts.mjs owns its selection and additional dispatch inputs;
    otherwise every declared/discovered suite is due.
    """
    import base64
    from . import canon, plan_github
    declared = plan_github.settings_of(cfg).workflows_for(repo)
    watcher = Watcher(cfg, api=api)
    found = watcher.discovered_workflows(repo)
    if found is None:
        raise GelError("cannot discover registered CI workflows in %s" % repo)
    selected = _repository_dispatch_plan(db, cfg, item, api, repo, branch, sha)
    if selected is None:
        workflows = declared if declared is not None else tuple(w for w in found if SUITE_RE.search(w))
        if not workflows:
            raise GelError("no suite workflow declared or discovered in %s" % repo)
        selected = {workflow: {"expected_sha": sha} for workflow in workflows}
    if not selected:
        raise GelError("the repository CI planner selected no suite workflows")
    missing = set(selected) - set(found)
    if missing:
        raise GelError("CI workflow(s) not registered: " + ", ".join(sorted(missing)))
    for workflow, inputs in selected.items():
        data = api("repos/%s/contents/.github/workflows/%s?ref=%s"
                   % (repo, _q(workflow), sha)) or {}
        try:
            doc = canon.load_yaml(base64.b64decode(data.get("content") or "").decode())
            triggers = doc.get("on", doc.get(True, {}))
            dispatch = triggers["workflow_dispatch"] or {}
            spec = dispatch.get("inputs") or {}
            if "expected_sha" not in spec or inputs.get("expected_sha") != sha:
                raise ValueError("expected_sha input missing or differs from frozen head")
            for name, entry in spec.items():
                if entry.get("required") and name not in inputs and "default" not in entry:
                    raise ValueError("required input %s missing from repository plan" % name)
            if set(inputs) - set(spec):
                raise ValueError("repository planner supplied undeclared inputs")
        except (ValueError, TypeError, KeyError, AttributeError, canon.YamlError) as exc:
            raise GelError("workflow %s cannot dispatch the frozen head: %s" % (workflow, exc)) from exc
    return selected


def _repository_dispatch_plan(db, cfg, item, api, repo, branch, sha):
    """Read the planner at the frozen tree, then run only its dry-run path.

    Mirrors nexlink_release.ci_plan: a detached worktree prevents the caller's
    checkout from substituting a planner. Absence permits generic suites;
    presence with an unreadable/invalid plan fails before any dispatch.
    """
    import json
    import os
    import subprocess
    import tempfile
    from . import plan_github, prevalidation_calls, release_cutoff as rc
    script = "scripts/ci/dispatch-due-parts.mjs"
    try:
        data = api("repos/%s/contents/%s?ref=%s" % (repo, script, sha)) or {}
    except plan_github.GithubError as exc:
        if "404" not in str(exc):
            raise
        return None
    if not data.get("content"):
        return None
    local = prevalidation_calls.repository(cfg, db, item, sha)
    if not local:
        raise GelError("repository CI planner needs a local clone containing %s" % sha)
    folder = path = None
    try:
        folder = tempfile.mkdtemp(prefix="ameesh-ci-dispatch-")
        path = os.path.join(folder, "tree")
        rc._git(local, "worktree", "add", "--detach", path, sha, check=True)
        # The script fetches origin/develop and verifies the integration tip;
        # its JSON argv is generated by dispatchArgv, including SQL anchors.
        proc = subprocess.run(["node", script, branch, sha, "--dry-run"], cwd=path,
                              capture_output=True, text=True, timeout=60)
        if proc.returncode:
            raise GelError("repository CI planner refused: " + proc.stderr[-500:])
        report = json.loads(proc.stdout)
        if (report.get("schema") != "nexlink-dispatch-due-parts/1"
                or report.get("ref") != branch or report.get("head") != sha
                or report.get("dryRun") is not True or report.get("failures")):
            raise GelError("repository CI planner returned an invalid dispatch plan")
        selected = {}
        for entry in report.get("argv") or []:
            argv = entry["argv"]
            if (len(argv) < 7 or argv[:2] != ["workflow", "run"]
                    or argv[3:5] != ["--ref", branch]
                    or not re.fullmatch(r"[A-Za-z0-9_-]+\.ya?ml", argv[2])):
                raise GelError("repository planner returned invalid workflow arguments")
            inputs = {}
            for index in range(5, len(argv), 2):
                if argv[index] != "-f" or index + 1 >= len(argv):
                    raise GelError("repository planner returned invalid dispatch inputs")
                name, value = argv[index + 1].split("=", 1)
                if name in inputs:
                    raise GelError("repository planner repeated an input")
                inputs[name] = value
            if argv[2] in selected:
                raise GelError("repository planner repeated a workflow")
            selected[argv[2]] = inputs
        if set(selected) != {part["workflow"] for part in report["parts"]}:
            raise GelError("repository planner's parts and dispatch arguments differ")
        return selected
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, rc.ReleaseError) as exc:
        raise GelError("repository CI dispatch plan unavailable: %s" % exc) from exc
    finally:
        if folder is not None:
            rc._drop_worktree(local, folder, path)


def _dispatch_request_runs(db, item_id, api, decision):
    """A full dispatch is green only when every selected workflow succeeded.

    Dispatch titles don't encode the PR base. Only an applied request of this
    composition correlates runs after its watermark; foreign completed runs
    never prove this request green. Live foreign runs still prevent duplicates.
    """
    rows = _runs_of(api, decision["repo"], sha=decision["sha"],
                    branch=decision["branch"], event="workflow_dispatch",
                    workflows=tuple(decision["dispatch"]))
    marks = [m for m in request_events(db, item_id)
             if m["state"] == "applied" and m["sha"] == decision["sha"]
             and m["base"] == decision["base"] and m["mode"] == "full"]
    mark = marks[-1] if marks else None
    correlated = [r for r in rows if mark and _run_id(r) > (mark.get("seen") or 0)]
    latest = {}
    for row in correlated:
        workflow = workflow_of(row.get("path"))
        if workflow not in latest or _run_id(row) > _run_id(latest[workflow]):
            latest[workflow] = row
    live = [r for r in rows if r.get("status") in OPEN_STATUSES]
    if not latest:
        carried = [{"row": row, "id": _run_id(row), "mode": "full",
                    "status": row.get("status") or "", "conclusion": row.get("conclusion") or ""}
                   for row in rows]
        return [], carried
    complete = set(latest) == set(decision["dispatch"])
    green = complete and all(r.get("status") == "completed" and r.get("conclusion") == "success"
                             for r in latest.values())
    status = "in_progress" if live else "completed"
    conclusion = "success" if green else ("" if live else "failure")
    row = dict(max(rows, key=_run_id), status=status, conclusion=conclusion)
    return [{"row": row, "id": _run_id(row), "number": decision["pr"],
             "sha": decision["sha"], "base": decision["base"], "mode": "full",
             "status": status, "conclusion": conclusion}], []


def _pose_dispatch(gh, decision, *, api):
    """Same client/token as labels; stop on ambiguous failure, keep the intent."""
    from . import plan_github
    accepted = []
    for workflow, inputs in decision["dispatch"].items():
        payload = {"ref": decision["branch"], "inputs": inputs}
        try:
            if _head_of(api, decision["repo"], decision["branch"]) != decision["sha"]:
                return {"requested": False, "action": "refuse",
                        "why": "branch moved before dispatch; accepted workflows: " + ", ".join(accepted)}
            code, out, err = gh.run(["api", "-X", "POST",
                                    "repos/%s/actions/workflows/%s/dispatches"
                                    % (decision["repo"], _q(workflow)), "--input", "-"],
                                   json.dumps(payload))
        except (plan_github.GithubError, OSError) as exc:
            code, out, err = 1, "", str(exc)
        if code:
            return {"requested": False, "action": "refuse",
                    "why": "dispatch %s failed or is ambiguous (%s); accepted: %s; "
                           "intent retained, inspect runs before --force"
                           % (workflow, _gh_error(out, err), ", ".join(accepted) or "none")}
        accepted.append(workflow)
    return {"requested": True, "action": "request", "run": {}, "warnings": []}


def _packet_runs(api, repo: str, branch: str, sha: str) -> list[dict]:
    """#2876 : portiers push vivants de cette branche et cette tête seulement.

    La liste générale évite un 404 sur les dépôts sans paquet.yml. Paginer
    garde les portiers visibles même après de nombreuses relances.
    """
    found, page = [], 1
    while True:
        data = api("repos/%s/actions/runs?per_page=100&page=%d&head_sha=%s&branch=%s&event=push"
                   % (repo, page, _q(sha), _q(branch))) or {}
        rows = list(data.get("workflow_runs") or ())
        found.extend(row for row in rows
                     if row.get("path") == ".github/workflows/paquet.yml"
                     and row.get("event") == "push"
                     and row.get("head_branch") == branch
                     and row.get("head_sha") == sha
                     and row.get("status") in OPEN_STATUSES)
        if len(rows) < 100:
            return found
        page += 1


def pr_run(row: dict) -> dict | None:
    """Une exécution de PR étiquetée #1343 : PR, mode, tête gelée, base.

    Le titre (run-name de tests.yml) porte la demande ; `head_sha` doit nommer
    la même tête (à défaut, le payload de la PR listé par l'API). Tout autre
    run de PR — d'avant #1343, sans étiquette lisible, ou dont la tête ne
    correspond pas — ne prouve rien : le doute ne verdit pas un check requis.
    """
    if (row.get("event") or "").strip() != "pull_request":
        return None
    m = PR_RUN_RE.search(str(row.get("display_title") or ""))
    if not m:
        return None
    label = m.group("label")
    lm = LABEL_RE.match(label)
    if not lm:
        return None
    sha = lm.group("sha")
    if (row.get("head_sha") or "").strip().lower() != sha:
        heads = [((pr.get("head") or {}).get("sha") or "").strip().lower()
                 for pr in row.get("pull_requests") or ()]
        if sha not in heads:
            return None
    return {"row": row, "number": int(m.group("number")), "label": label,
            "mode": lm.group("mode"), "sha": sha, "base": m.group("base"),
            "status": (row.get("status") or "").strip().lower(),
            "conclusion": (row.get("conclusion") or "").strip().lower()}


def carried_request(row: dict, number: int, sha: str) -> str:
    """Le mode d'une demande visible dont le run-name est illisible, ou "" (#2364).

    Un ancien workflow composait le run-name avec la description de l'étiquette
    (« base CI #1343 full sur <sha court> ») au lieu de la base du payload :
    `pr_run` refuse ce run, faute de base lisible. Il porte pourtant la même
    tête et le même libellé `ci-lot|full-<sha>` que la demande ; il matérialise
    donc sa réservation, sans jamais prouver sa composition ni verdir un check.
    """
    if (row.get("event") or "").strip() != "pull_request":
        return ""
    title = str(row.get("display_title") or "")
    found = CARRIED_RE.search(title)
    if not found or found.group("sha") != sha:
        return ""
    number_m = CARRIED_PR_RE.search(title)
    if number_m and int(number_m.group("number")) != int(number):
        return ""
    head = (row.get("head_sha") or "").strip().lower()
    heads = [((pr.get("head") or {}).get("sha") or "").strip().lower()
             for pr in row.get("pull_requests") or ()]
    if head != sha and sha not in heads:
        return ""
    return found.group("mode")


def _pr_request_runs(api, repo: str, number: int, sha: str,
                     branch: str) -> tuple[list[dict], list[dict]]:
    """Les demandes de gel de cette PR et cette tête, lues en un seul appel.

    Rend `(lisibles, illisibles)` : les premières prouvent leur composition
    (PR, tête, base, mode) ; les secondes ne portent que la tête et le libellé
    d'un run-name illisible (#2364). Une demande illisible matérialise la
    réservation de la même composition, sans verdir ni comparer aucune base.
    """
    readable, carried = [], []
    for row in _runs_of(api, repo, branch=branch, event="pull_request"):
        info = pr_run(row)
        if info and info["number"] == number and info["sha"] == sha:
            readable.append(info)
            continue
        mode = carried_request(row, number, sha)
        if mode:
            carried.append({"row": row, "id": _run_id(row), "mode": mode,
                            "status": (row.get("status") or "").strip().lower(),
                            "conclusion": (row.get("conclusion") or "").strip().lower()})
    return readable, carried


def _current_run(row: dict, frozen: str, number: int, base: str, covered=()) -> bool:
    """Un run de la composition courante : ni périmé, ni à annuler.

    Avec `number`/`base` (une demande de gel), un run de PR de la même tête
    mais d'une autre base est périmé : la composition a bougé, sauf pour une
    base de `covered` (atterrissages de coupure seuls, #5192). Sans eux
    (`ameesh ci cancel` seul), tout run qui porte le SHA gelé est conservé,
    comme avant (#1343).
    """
    info = pr_run(row)
    if number and info is not None:
        return (info["number"] == number and info["sha"] == frozen
                and (not base or info["base"] == base or info["base"] in covered))
    return (row.get("head_sha") or "").strip().lower() == frozen


def _pr_of(api, repo: str, branch: str) -> dict:
    """La PR ouverte : numéro, tête, pointe cible actuelle, refs, étiquettes."""
    owner = repo.split("/", 1)[0]
    rows = api("repos/%s/pulls?state=open&head=%s:%s&per_page=10"
               % (repo, _q(owner), _q(branch)))
    if not isinstance(rows, list):
        raise GelError("pull requests of %s: unreadable response" % repo)
    if not rows:
        raise GelError("branch %s has no open pull request in %s: open the PR before "
                       "requesting the frozen CI (#1343)" % (branch, repo))
    if len(rows) > 1:
        raise GelError("branch %s has %d open pull requests in %s: one only"
                       % (branch, len(rows), repo))
    pr = rows[0]
    base_ref = ((pr.get("base") or {}).get("ref") or "").strip()
    if not base_ref:
        raise GelError("pull request #%s: missing target branch, the composition "
                       "cannot be checked" % pr.get("number"))
    # #2328 : pulls.base.sha peut encore nommer la base du dernier push de
    # tête, alors que le merge-ref utilise déjà la nouvelle pointe cible.
    base = _head_of(api, repo, base_ref)
    labels = [(lab.get("name") if isinstance(lab, dict) else str(lab))
              for lab in pr.get("labels") or ()]
    return {"number": int(pr.get("number") or 0),
            "head": ((pr.get("head") or {}).get("sha") or "").strip().lower(),
            "base": base,
            "head_ref": ((pr.get("head") or {}).get("ref") or ""),
            "base_ref": base_ref,
            "labels": [name for name in labels if name]}


def _carries_freeze_request(api, repo: str, sha: str) -> bool:
    """Le workflow du SHA gelé porte-t-il le déclencheur #1343 ?

    Sans ce contrôle, un lot dont la branche précède la bascule serait refusé
    par GitHub sans dire quoi faire (aucun run ne partirait) ; ici le refus est
    explicite : refusionner la branche sur main avant de demander la CI.
    """
    import base64
    from . import plan_github
    try:
        data = api("repos/%s/contents/.github/workflows/tests.yml?ref=%s"
                   % (repo, _q(sha))) or {}
    except plan_github.GithubError as exc:
        if "404" not in str(exc):
            raise
        return False
    content = (data.get("content") or "").replace("\n", "")
    if not content:
        return False
    try:
        text = base64.b64decode(content).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return False
    return ("types: [labeled]" in text and "ci-(lot|full)-" in text
            and "github.event.label.description" in text)


def _reviews_at(api, repo: str, ref: str) -> set[str] | None:
    """Les fichiers du dossier `reviews/` à `ref`, ou None s'il est illisible."""
    from . import plan_github
    try:
        data = api("repos/%s/contents/reviews?ref=%s" % (repo, _q(ref)))
    except plan_github.GithubError:
        return None
    if not isinstance(data, list):
        return None
    return {str(item.get("name") or "") for item in data
            if isinstance(item, dict) and item.get("type") == "file"}


def _manifest_name(head_ref: str) -> str:
    """`integration/<nom>` → `integration-<nom>.md`, le nom que publient les porteurs."""
    return "integration-%s.md" % head_ref[len("integration/"):].replace("/", "-")


def _carrier_manifest(api, repo: str, head_ref: str, sha: str, base: str) -> str:
    """Le manifeste de composition que publie le porteur à `sha`, ou "" (N1 #2240).

    Un porteur `integration/<nom>` publie `reviews/integration-<nom>.md` :
    c'est la revue unitaire du paquet qui tient lieu d'avis sur la tête
    composée, pour la seule demande de CI (`--avis 0 --full`). Le manifeste
    doit être le sien et absent de la base de la PR : `main` porte déjà ceux
    des paquets fusionnés, qu'une branche `integration/**` hériterait sans rien
    publier (R1 ameesh-dev1). Toute réponse illisible vaut « pas de manifeste » :
    le refus est nommé, jamais un zéro avis supposé.
    """
    name = _manifest_name(head_ref)
    at_head = _reviews_at(api, repo, sha)
    at_base = _reviews_at(api, repo, base)
    if at_head is None or at_base is None or name not in at_head or name in at_base:
        return ""
    return name


def _short_run(row: dict) -> dict:
    run = {"id": row.get("id"), "status": row.get("status"),
           "conclusion": row.get("conclusion"), "event": row.get("event"),
           "url": row.get("html_url")}
    info = pr_run(row) if row else None
    if info:
        run.update({"mode": info["mode"], "pr": info["number"], "base": info["base"]})
    elif (row.get("event") or "") == "push":
        run["mode"] = "full"
    return run


def _covers(have: str, want: str) -> bool:
    """`have` couvre-t-il la CI demandée ? La suite complète couvre le lot."""
    return have == want or (want == "lot" and have == "full")


def _alive_or_green(run: dict) -> bool:
    return run["status"] in OPEN_STATUSES or (run["status"] == "completed"
                                              and run["conclusion"] == "success")


def _cutoff_why(run: dict, base: str) -> str:
    """La trace d'un run qui couvre la composition depuis une base de coupure."""
    if run["base"] == base:
        return ""
    return ("; run on base %s, the target advanced only by cutoff landings (canon §12)"
            % run["base"][:12])


def _cutoff_bases(cfg, db, item, slug: str, target: str, base: str, runs,
                  mode: str, *, checkout: str = "", proven=()) -> list[str]:
    """#5192 (canon §12) : les bases d'autres runs de la tête qui couvrent `base`.

    Une cible avancée par les seuls atterrissages d'une coupure (version et
    notes) ne fait pas une nouvelle composition : un run vivant ou vert de la
    même tête et d'un mode qui couvre `mode` (tous si `mode` est vide), sur une
    base dont `base` ne diffère que par ces commits, la couvre déjà ; reposer
    l'étiquette annulerait ce run (groupe de concurrence de tests.yml). Même
    règle que la fusion (`packet_automerge._cutoff_landing_only`) et même clone
    (`_gate_repo`). `proven` : des bases déjà prouvées pour ce `base` (la
    relation ne dépend que des deux SHA), reprises sans relire git. Un clone
    absent ou illisible, ou un commit de code dans l'intervalle, ne couvre
    rien : la demande part comme avant."""
    candidates = sorted({r["base"] for r in runs if r["base"] != base
                         and (not mode or _covers(r["mode"], mode)) and _alive_or_green(r)})
    proven = set(proven)
    covered = [b for b in candidates if b in proven]
    fresh = [b for b in candidates if b not in proven]
    if not fresh:
        return covered
    from . import packet_automerge
    repo = packet_automerge._gate_repo(cfg, db, item, slug, checkout)
    if not repo:
        return covered
    # la pointe de la cible peut manquer au clone : sans elle, rien ne couvre
    packet_automerge._fetch_into(repo, (target,))
    return sorted(covered + [b for b in fresh
                             if packet_automerge._cutoff_landing_only(repo, b, base)])


def _latest_check(api, repo: str, sha: str) -> dict | None:
    """Le dernier check run `tests` de la tête : ce que la fusion regarderait.

    `filter=latest` rend le dernier état par nom ; à défaut on garde le plus
    grand identifiant. None quand aucun check n'existe encore.
    """
    data = api("repos/%s/commits/%s/check-runs?filter=latest&per_page=100"
               % (repo, _q(sha))) or {}
    rows = [c for c in data.get("check_runs") or ()
            if str(c.get("name") or "") == "tests"]
    if not rows:
        return None
    row = max(rows, key=lambda c: int(c.get("id") or 0))
    return {"id": row.get("id"), "status": (row.get("status") or "").strip().lower(),
            "conclusion": (row.get("conclusion") or "").strip().lower(),
            "url": row.get("details_url") or row.get("html_url") or ""}


def _request_note(state: str, number: int, label: str, sha: str, base: str,
                  mode: str, token: str, text: str = "",
                  avis: int | None = None, seen: int | None = None) -> str:
    marker = ("ci-label %s pr=%d label=%s sha=%s base=%s mode=%s token=%s"
              % (state, number, label, sha, base, mode, token))
    if avis is not None:
        #: N2 #2240 : le nombre d'avis déclaré par l'appelant reste lisible
        #: dans la marque, `--avis 0 --full` compris (aucun passage muet).
        marker += " avis=%d" % avis
    if seen is not None:
        marker += " seen=%d" % seen
    return "%s: %s" % (marker, text.strip()) if text.strip() else marker


def request_events(db, item_id: int, *, limit: int = 200) -> list[dict]:
    """Les marques de demande du lot, de la plus ancienne à la plus récente."""
    from . import work
    out = []
    for ev in work.events(db, int(item_id), limit=limit):
        m = REQUEST_RE.match((ev.get("note") or "").strip())
        if not m:
            continue
        out.append({"id": int(ev.get("id") or 0), "state": m.group("state"),
                    "pr": int(m.group("pr")), "label": m.group("label"),
                    "sha": m.group("sha"), "base": m.group("base"),
                    "mode": m.group("mode"), "token": m.group("token"),
                    "avis": int(m.group("avis")) if m.group("avis") is not None else None,
                    "seen": int(m.group("seen")) if m.group("seen") is not None else None,
                    "ts": float(ev.get("created_ts") or 0.0)})
    return sorted(out, key=lambda e: e["id"])


def _materialized(mark: dict, runs) -> bool:
    """Une exécution visible matérialise-t-elle cette marque (P2-3) ?

    Chaque marque `applied` enregistre `seen=` : le plus grand identifiant
    d'exécution connu avant sa pose. Les identifiants des exécutions GitHub
    croissent avec le temps : un run d'identifiant supérieur n'a pu naître
    qu'après elle, donc la demande a produit son exécution et ne tient plus la
    clé. `runs` porte des couples (identifiant, mode) : la couverture des modes
    suit celle des exécutions (§ `_covers`) — un run de lot ne matérialise pas
    une demande de suite complète, seul un `full` le fait (P2-3, cx2). Un run
    au run-name illisible y entre aussi, avec le mode lu dans son libellé
    (#2364) : la marque qu'il a produite ne tient plus la clé, même si sa base
    n'est pas comparable. Une marque sans `seen=` (écrite avant #1343) n'est
    pas corrélable : elle tient la clé jusqu'au `ttl`, jamais libérée par une
    comparaison entre l'horloge de la base (marque, fractionnaire) et celle de
    GitHub (run, à la seconde) — c'est cette comparaison qui bloquait la
    reprise d'un run rouge né dans la même seconde que sa marque.
    """
    seen = mark.get("seen")
    if seen is None:
        return False
    return any(ident > seen and _covers(run_mode, mark["mode"])
               for ident, run_mode in runs)


def reservation_holder(events: list[dict], sha: str, base: str, mode: str, token: str, *,
                       now: float, ttl: float = RESERVE_TTL_S,
                       runs: tuple = ()) -> dict | None:
    """La marque `applied` fraîche d'un autre porteur, ou None (P2 R1/P2-3).

    Une étiquette posée mais pas encore visible comme exécution dans l'API
    GitHub tient la clé le temps du `ttl` ; `released` (demande refusée), les
    marques périmées et celles que matérialise une exécution visible
    (`_materialized`) ne tiennent pas. `runs` porte les couples (identifiant,
    mode) des exécutions visibles de la composition : la marque est libérée par
    corrélation d'identifiants et de mode, jamais par une comparaison
    d'horloges. La couverture des modes suit celle des exécutions (§ `_covers`) :
    un `full` réservé couvre une demande `lot` de la même composition, l'inverse
    non — et seul un run `full` matérialise une marque `full`.
    Appelé sous le verrou de la ligne du lot, il lit les marques commitées des
    autres processus ; le porteur courant est exclu (`token`).
    """
    key = [e for e in events
           if e["sha"] == sha and e["base"] == base and _covers(e["mode"], mode)
           and e["state"] == "applied" and e["token"] != token]
    held = [e for e in key
            if now - e["ts"] < ttl and not _materialized(e, runs)]
    return held[-1] if held else None


def gel_plan(db, cfg, item_id: int, *, api, sha: str = "", avis: int = 1,
             exception: str = "", reason: str = "", full: bool = False,
             force: bool = False, repo: str = "", checkout: str = "") -> dict:
    """La décision de `ameesh ci run <lot>`, sans aucune écriture.

    Rend `{schema, action: request|green|running|refuse, …}` : `request` est le
    seul cas qui écrit (`cmd_ci_run` pose l'étiquette). Un refus est une
    `GelError` dont le message dit le geste à faire (geler, refusionner,
    relire, refaire la demande).
    """
    from . import work
    item = work.get(db, int(item_id))
    if item is None:
        raise GelError("lot #%s unknown" % item_id)
    if exception and exception not in EXCEPTIONS:
        raise GelError("--exception: %s (one of %s)" % (exception, ", ".join(EXCEPTIONS)))
    if exception and not reason.strip():
        raise GelError("--exception %s requires --reason: the exception is marked "
                       "(canon §3)" % exception)
    if full and not reason.strip():
        raise GelError("--full requires --reason: the full suite is an explicit "
                       "gesture (release, package, hand request)")
    if avis < 0 or avis > MAX_AVIS:
        raise GelError("--avis: 0 (lot léger), 1 (sensible) or 2 (SQL/sécurité/prod)")
    name = repo_of(cfg, item, repo=repo)
    branch = (item.get("branch") or "").strip()
    if not branch:
        raise GelError("lot #%s has no branch: push the lot first" % item_id)
    target = (item.get("branch_target") or "main").strip() or "main"
    freeze = frozen_of(db, item_id)
    frozen = ((freeze or {}).get("sha") or "").strip().lower()
    asked = (sha or "").strip().lower()
    exempt = bool(exception or full)
    if asked and not _sha_ok(asked):
        raise GelError("--sha: the frozen commit (40 hex), not %r" % sha)
    if asked and frozen and asked != frozen:
        raise GelError("--sha %s is not the freeze of lot #%s (%s): refreeze first, "
                       "a correction needs a new review" % (asked[:12], item_id, frozen[:12]))
    if not frozen and not exempt:
        # le gel est un jalon, pas un drapeau : `--sha` seul ne le remplace pas,
        # sinon un lot jamais gelé obtiendrait sa CI sans trace de gel.
        raise GelError("lot #%s is not frozen: ameesh work milestone %s frozen --sha <sha>"
                       % (item_id, item_id))
    head = _head_of(api, name, branch)
    #: la tête demandée : le gel pour une demande normale ; pour une exception
    #: ou la suite complète, le `--sha` nommé ou la tête de la branche (le
    #: correctif d'un incident arrive après le gel et n'attend pas un nouveau gel).
    tested = (asked or head) if exempt else frozen
    if not _sha_ok(tested):
        raise GelError("lot #%s: the frozen milestone carries no usable sha (%r): "
                       "refreeze with --sha" % (item_id, tested))
    if head != tested:
        raise GelError("branch %s is on %s, %s is %s: the ref moved — refreeze and "
                       "re-review, nothing requested"
                       % (branch, head[:12], "--sha" if exempt else "the freeze",
                          tested[:12]))
    # #2546 : GitHub ne démarre aucun workflow sur une tête dont le message
    # porte une instruction de saut, `pull_request: labeled` compris. Refuser
    # ici, avant toute étiquette, évite une demande silencieuse et un faux
    # diagnostic (paquet h-2000, ~30 min de CI perdues).
    marker = skip_ci_marker(_commit_message(api, name, head))
    if marker:
        raise GelError(
            "commit %s carries the GitHub skip instruction %s: GitHub starts no "
            "workflow on this head, the pull_request `labeled` event of this request "
            "included — rewrite the commit message, push a new head (refreeze it if "
            "this is not an exception), then request the CI again; nothing requested"
            % (head[:12], marker))
    frozen_sha = tested
    mode = "full" if full else "lot"
    verdicts = latest_verdicts(db, item_id, frozen_sha, item.get("assignee") or "")
    reviews = [m for m in verdicts if (m.get("verdict") or "") == "ok"]
    blocked = [m for m in verdicts if (m.get("verdict") or "") == "blocked"]
    if not exception:
        # #2214 : `--full` ne dispense pas des avis. La suite complète est un
        # mode d'exécution, pas une relecture ; seules les exceptions motivées
        # du canon §3 (incident|unblock) partent en parallèle de la relecture.
        # un `blocked` est le dernier mot d'un relecteur sur cette tête : la
        # corriger demande une nouvelle tête, donc un nouveau gel (P1 R1).
        if blocked:
            raise GelError("lot #%s: %s blocked %s (last verdict of that reviewer): "
                           "fix the head, refreeze and re-review; nothing requested"
                           % (item_id, blocked[0].get("actor") or "a reviewer",
                              frozen_sha[:12]))
        if len(reviews) < avis:
            raise GelError("lot #%s: %d independent verdict(s) `ok` on %s, %d required "
                           "(ameesh work milestone %s verdict ok --sha %s --actor <relecteur>); "
                           "an incident or a CI unblock uses --exception incident|unblock "
                           "--reason …" % (item_id, len(reviews), frozen_sha[:12], avis,
                                           item_id, frozen_sha[:12]))
    pr = _pr_of(api, name, branch)
    if pr["head"] != frozen_sha:
        raise GelError("pull request #%d head is %s, the freeze is %s: the PR moved — "
                       "refreeze and re-review, nothing requested"
                       % (pr["number"], pr["head"][:12], frozen_sha[:12]))
    if not _sha_ok(pr["base"]):
        raise GelError("pull request #%d: unusable base commit (%r): the composition "
                       "cannot be checked" % (pr["number"], pr["base"]))
    if full and avis == 0 and not exception:
        # (b) §12 #2240 (pilote mesh-design, 17/10 13:22) : le zéro avis d'une
        # suite complète est réservé à un porteur `integration/**` qui publie
        # son manifeste ; hors de ce cas la relecture reste la règle (#2214).
        # Cette garde ne touche que la demande de CI : checks requis,
        # review_gate et porte de fusion du porteur restent inchangés.
        head_ref = (pr["head_ref"] or "").strip()
        if not head_ref.startswith("integration/"):
            raise GelError("pull request #%d: --avis 0 --full is reserved to an "
                           "integration/** carrier head (source %r); review the head "
                           "and pass its required verdicts"
                           % (pr["number"], head_ref or "—"))
        manifest = _carrier_manifest(api, name, head_ref, frozen_sha, pr["base"])
        if not manifest:
            raise GelError("pull request #%d: --avis 0 --full needs the carrier's own "
                           "manifest (reviews/%s) at %s, absent from the base; without "
                           "it, review the head and pass its required verdicts"
                           % (pr["number"], _manifest_name(head_ref), frozen_sha[:12]))
    if release_refs(pr["head_ref"], pr["base_ref"]) and mode == "lot":
        raise GelError("pull request #%d: source %s or target %s is release/** or "
                       "integration/**: request the full suite (--full --reason …); a lot "
                       "request never covers a release"
                       % (pr["number"], pr["head_ref"] or "—", pr["base_ref"] or "—"))
    base = pr["base"]
    label = label_name(mode, frozen_sha)
    dispatch = None
    if full:
        from . import plan_github
        declared = plan_github.settings_of(cfg).workflows_for(name)
        if (declared is not None and WORKFLOWS[0] not in declared
                or not _carries_freeze_request(api, name, frozen_sha)):
            dispatch = _dispatch_plan(db, cfg, item, api, name, branch, frozen_sha)
    dispatch_decision = {"repo": name, "sha": frozen_sha, "base": base,
                         "branch": branch, "pr": pr["number"], "mode": mode,
                         "dispatch": dispatch}
    runs, carried = (_dispatch_request_runs(db, item_id, api, dispatch_decision)
                     if dispatch is not None else
                     _pr_request_runs(api, name, pr["number"], frozen_sha, branch))
    #: P1 R1 : la composition compte — même tête mais autre base, ou mode
    #: insuffisant (`lot` ne prouve pas le `full`), ne verdit pas. #5192 : sauf
    #: une base que seuls des atterrissages de coupure séparent de `base`, pour
    #: un run vivant ou vert (canon §12).
    cutoff = _cutoff_bases(cfg, db, item, name, pr["base_ref"] or target, base, runs, mode,
                           checkout=checkout)
    applicable = [r for r in runs if _covers(r["mode"], mode)
                  and (r["base"] == base or r["base"] in cutoff and _alive_or_green(r))]
    live = [r for r in applicable if r["status"] in OPEN_STATUSES]
    green = [r for r in applicable if r["status"] == "completed"
             and r["conclusion"] == "success"]
    decision = {"schema": GEL_SCHEMA, "work_item": int(item_id), "repo": name,
                "branch": branch, "target": target, "sha": frozen_sha, "base": base,
                "head_ref": pr["head_ref"], "base_ref": pr["base_ref"],
                "mode": mode, "label": label,
                "path": "workflow-dispatch" if dispatch is not None else "pr-label",
                **({"dispatch": dispatch} if dispatch is not None else {}),
                "pr": pr["number"],
                "exception": exception, "reason": reason.strip(), "avis": avis,
                "verdicts": sorted((m.get("actor") or "") for m in reviews),
                "frozen": bool(freeze), "runs": len(runs),
                **({"cutoff_bases": cutoff} if cutoff else {}),
                "unreadable": [{"id": r["id"], "mode": r["mode"],
                                "conclusion": r["conclusion"] or r["status"] or "?"}
                               for r in carried],
                "requested": [{"id": r["row"].get("id"), "mode": r["mode"],
                               "base": r["base"],
                               "conclusion": r["conclusion"] or r["status"]}
                              for r in applicable]}
    if live:
        return {**decision, "action": "running",
                "why": "a %s request of this composition (PR #%d, %s, base %s) is already "
                       "in flight%s: nothing posed"
                       % (live[0]["mode"], pr["number"], frozen_sha[:12], base[:12],
                          _cutoff_why(live[0], base)),
                "run": _short_run(live[0]["row"])}
    if green:
        #: le vert applicable ne suffit pas si le dernier check requis de la
        #: tête est rouge (une étiquette étrangère a pu le laisser ainsi) : la
        #: reprise est alors explicite (`--force`), jamais automatique.
        check = None if dispatch is not None else _latest_check(api, name, frozen_sha)
        if check is None or (check["status"] == "completed"
                             and check["conclusion"] in CHECK_GREEN):
            return {**decision, "action": "green",
                    "why": "already green on this composition (PR #%d, %s, base %s, "
                           "mode %s%s): nothing posed"
                           % (pr["number"], frozen_sha[:12], base[:12], mode,
                              _cutoff_why(green[-1], base)),
                    "run": _short_run(green[-1]["row"])}
        if check["status"] != "completed":
            return {**decision, "action": "running",
                    "why": "the required `tests` check of %s is %s (run %s): nothing posed"
                           % (frozen_sha[:12], check["status"] or "?",
                              check.get("id") or "?"),
                    "check": check}
        if not force:
            raise GelError("lot #%s: the required `tests` check on %s is %s (run %s), not "
                           "green although a %s request of this composition succeeded: "
                           "--force to request the CI again on this composition (explicit "
                           "resumption of a red or cancelled check)"
                           % (item_id, frozen_sha[:12], check["conclusion"] or "?",
                              check.get("id") or "?", mode))
    if applicable and not green and not force:
        raise GelError("lot #%s: %d request(s) already ran on PR #%d for %s (base %s), "
                       "none green (%s): --force to request the CI again (explicit "
                       "resumption of a red or cancelled run)"
                       % (item_id, len(applicable), pr["number"], frozen_sha[:12],
                          base[:12],
                          ", ".join(sorted({r["conclusion"] or r["status"] or "?"
                                            for r in applicable}))))
    if not applicable:
        #: P2 R1 : GitHub ne montre pas un run aussitôt ; une étiquette déjà
        #: posée par un autre processus est dite, jamais reposée. Un run
        #: illisible, lui, matérialise la marque qui l'a produit (#2364).
        held = reservation_holder(request_events(db, item_id), frozen_sha, base, mode, "",
                                  now=time.time(),
                                  runs=tuple((r["id"], r["mode"]) for r in carried))
        if held:
            return {**decision, "action": "running",
                    "why": "a request of this composition was already reserved (token %s): "
                           "nothing posed" % held["token"]}
        if carried:
            #: #2364 : un run dont le run-name est illisible (ancien
            #: workflow/CLI) porte la tête et le libellé de la demande, jamais
            #: sa base. Vivant, il se surveille ; terminé, il ne vaut ni vert
            #: ni rouge applicable : seule une reprise explicite le remplace.
            live_carried = [r for r in carried if r["status"] in OPEN_STATUSES]
            if live_carried:
                return {**decision, "action": "running",
                        "why": "a request of this composition (PR #%d, %s) is visible "
                               "but its run-name is unreadable (run %s, mode %s): its "
                               "state cannot be checked; nothing posed"
                               % (pr["number"], frozen_sha[:12], live_carried[0]["id"],
                                  live_carried[0]["mode"]),
                        "run": _short_run(live_carried[0]["row"])}
            if not force:
                raise GelError("lot #%s: a request already ran on PR #%d for %s (run %s, "
                               "mode %s), but its run-name is unreadable (base is not a "
                               "SHA: old workflow/CLI), so its composition cannot be "
                               "checked: --force to request the CI again (explicit "
                               "resumption)"
                               % (item_id, pr["number"], frozen_sha[:12],
                                  carried[0]["id"], carried[0]["mode"]))
    if dispatch is None and not _carries_freeze_request(api, name, frozen_sha):
        raise GelError("the workflow at %s does not carry the freeze request trigger "
                       "with a pinned target base (#1343/#2328, ci-lot/ci-full label): "
                       "rebase the lot on main before "
                       "requesting the CI" % frozen_sha[:12])
    return {**decision, "action": "request",
            "why": ("%s verdict(s) on %s" % (len(reviews), frozen_sha[:12])
                    if not (exception or full) else
                    ("exception %s: %s" % (exception, reason.strip()) if exception
                     else "full suite: %s" % reason.strip()))}


def cancel_plan(db, cfg, item_id: int, *, api, all_runs: bool = False,
                repo: str = "", number: int = 0, base: str = "", workflows=WORKFLOWS,
                target: str = "", checkout: str = "", proven=()) -> dict:
    """Les runs d'un lot à annuler : non conclues, hors de la composition gardée.

    `all_runs` annule aussi le run du SHA gelé (geste explicite). Un lot non
    gelé n'a pas de SHA de référence : tous ses runs ouverts sont périmés.
    Avec `number`/`base` (une demande de gel), un run de PR de la même tête
    mais d'une autre base est périmé lui aussi : sa composition a bougé —
    sauf une base que seuls des atterrissages de coupure séparent de `base`
    (#5192, canon §12 ; `proven` : bases déjà prouvées par le plan).
    """
    from . import work
    item = work.get(db, int(item_id))
    if item is None:
        raise GelError("lot #%s unknown" % item_id)
    name = repo_of(cfg, item, repo=repo)
    branch = (item.get("branch") or "").strip()
    if not branch:
        raise GelError("lot #%s has no branch: nothing to cancel" % item_id)
    freeze = frozen_of(db, item_id)
    frozen = ((freeze or {}).get("sha") or "").strip().lower()
    rows = [r for r in _runs_of(api, name, branch=branch, workflows=workflows)
            if (r.get("status") or "").strip().lower() in OPEN_STATUSES]
    covered: list[str] = []
    if frozen and not all_runs and number and base:
        same = []
        for row in rows:
            info = pr_run(row)
            if (info is not None and info["number"] == number and info["sha"] == frozen
                    and info["base"] != base):
                same.append({"base": info["base"], "mode": info["mode"],
                             "status": (row.get("status") or "").strip().lower(),
                             "conclusion": row.get("conclusion")})
        covered = _cutoff_bases(cfg, db, item, name,
                                target or (item.get("branch_target") or "main"), base,
                                same, "", checkout=checkout, proven=proven)
    if frozen and not all_runs:
        rows = [r for r in rows if not _current_run(r, frozen, number, base, covered)]
    return {"schema": CANCEL_SCHEMA, "work_item": int(item_id), "repo": name,
            "branch": branch, "frozen": frozen, "all": bool(all_runs),
            "runs": [_short_run(r) for r in rows],
            "why": ("every open run of the branch" if all_runs or not frozen else
                    "open runs that do not carry the frozen composition %s" % frozen[:12])}


def _cancel_runs(gh, repo: str, runs: list[dict], *, say=print) -> int:
    """Annule les runs (POST …/cancel) ; rend le nombre d'échecs, jamais fatal."""
    failed = 0
    for row in runs:
        path = "repos/%s/actions/runs/%s/cancel" % (repo, row.get("id"))
        code, out, err = gh.run(["api", "-X", "POST", path])
        if code != 0:
            failed += 1
            say("cancel run %s: %s" % (row.get("id"),
                                       " ".join((err or out or "failed").split())[:200]))
        else:
            say("cancel run %s (%s) requested" % (row.get("id"),
                                                  (row.get("url") or "")[:80]))
    return failed


def _gh_error(out: str, err: str) -> str:
    return " ".join((err or out or "failed").split())[:300]


def _check_label_actor(gh) -> str:
    """L'acteur du jeton `gh` : refuse le `GITHUB_TOKEN` d'Actions (#1343).

    L'événement `pull_request: labeled` produit par le `GITHUB_TOKEN` d'une
    Action ne déclenche aucun workflow : l'étiquette serait posée et rien ne
    partirait. Un jeton d'utilisateur ou d'application GitHub, lui, déclenche.
    """
    code, out, err = gh.run(["api", "user", "--jq", ".login"])
    if code != 0:
        raise GelError("cannot read the gh token identity (%s): the label event of a "
                       "GITHUB_TOKEN starts no workflow — use a user or GitHub App token"
                       % _gh_error(out, err))
    actor = out.strip()
    if not actor or actor.casefold() == ACTIONS_BOT:
        raise GelError("the gh token authenticates as %s: its label events start no "
                       "workflow — use a user or GitHub App token" % (actor or ACTIONS_BOT))
    return actor


def _label_exists(gh, repo: str, name: str) -> bool:
    code, _out, _err = gh.run(["api", "repos/%s/labels/%s" % (repo, _q(name))])
    return code == 0


def _ensure_label(gh, repo: str, name: str, mode: str, base: str) -> None:
    """Fige la pointe cible dans la description avant l'événement labeled.

    L'événement conserve cette description : le titre du run, le plan et les
    parents du merge-ref portent ainsi la même base, même si pulls.base.sha
    est périmé. Une reprise actualise aussi une étiquette existante (#2328).
    """
    exists = _label_exists(gh, repo, name)
    payload = {"description": base}
    path = "repos/%s/labels" % repo
    if exists:
        path += "/%s" % _q(name)
    else:
        payload.update({"name": name, "color": "0e8a16" if mode == "lot" else "1d76db"})
    code, out, err = gh.run(["api", "-X", "PATCH" if exists else "POST", path,
                             "--input", "-"], json.dumps(payload))
    if code != 0:
        raise GelError("cannot %s the label %s in %s: %s"
                       % ("update" if exists else "create", name, repo, _gh_error(out, err)))


def _drop_label(gh, repo: str, number: int, name: str, *, delete_label: bool) -> list[str]:
    """Retire l'étiquette de la PR ; `delete_label` la supprime du dépôt."""
    failed = []
    code, out, err = gh.run(["api", "-X", "DELETE",
                             "repos/%s/issues/%d/labels/%s" % (repo, number, _q(name))])
    if code != 0:
        failed.append("remove %s: %s" % (name, _gh_error(out, err)))
    if delete_label:
        code, out, err = gh.run(["api", "-X", "DELETE", "repos/%s/labels/%s"
                                 % (repo, _q(name))])
        if code != 0:
            failed.append("delete %s: %s" % (name, _gh_error(out, err)))
    return failed


def _pose_request(gh, repo: str, pr: dict, label: str, mode: str) -> dict:
    """Retire les étiquettes de demande périmées de la PR, puis pose la nôtre.

    Rend `{ok, dropped, failed, error}` ; ne lève pas : un refus est dit. Une
    étiquette étrangère n'est jamais touchée ; la nôtre est retirée avant
    d'être reposée, seul moyen de produire un nouvel événement `labeled`.
    """
    number = int(pr["number"])
    dropped, failed = [], []
    for old in pr["labels"]:
        if not LABEL_RE.match(old) or old == label:
            continue                      # périmée (autre SHA/mode) : retirée
        failed += _drop_label(gh, repo, number, old, delete_label=True)
        dropped.append(old)
    if label in pr["labels"]:
        #: reprise explicite : sans retrait, reposer la même étiquette ne
        #: produirait aucun événement.
        failed += _drop_label(gh, repo, number, label, delete_label=False)
        if failed:
            return {"ok": False, "dropped": dropped, "failed": failed,
                    "error": "cannot remove the label %s before posing it again" % label}
    try:
        _ensure_label(gh, repo, label, mode, pr["base"])
    except GelError as exc:
        return {"ok": False, "dropped": dropped, "failed": failed, "error": str(exc)}
    code, out, err = gh.run(["api", "-X", "POST",
                             "repos/%s/issues/%d/labels" % (repo, number),
                             "--input", "-"], json.dumps({"labels": [label]}))
    if code != 0:
        return {"ok": False, "dropped": dropped, "failed": failed,
                "error": "cannot pose the label %s on PR #%d: %s"
                         % (label, number, _gh_error(out, err))}
    return {"ok": True, "dropped": dropped, "failed": failed, "error": ""}


def _revalidate(db, item_id: int, decision: dict) -> None:
    """Relit le gel et les verdicts sous le verrou, avant la pose (avis delta R1).

    `gel_plan` décide hors transaction : un relecteur peut retirer son `ok`
    (dernier verdict `blocked`) ou un nouveau gel être enregistré avant que la
    réservation n'obtienne le verrou de la ligne du lot. Cette relecture refait
    les gardes de `gel_plan` dans la transaction qui tient cette ligne, donc
    après toute écriture concurrente ; seule l'exception explicite
    (`--exception … --reason …`), geste marqué et motivé, ne dépend pas des
    verdicts et garde son cours. `--full` reste soumis aux avis (#2214).
    """
    from . import work
    item = work.get(db, int(item_id))
    if item is None:
        raise GelError("lot #%s unknown" % item_id)
    if decision.get("exception"):
        return
    sha = decision["sha"]
    if decision.get("mode") != "full":
        #: un `--full` nomme sa tête lui-même (gel facultatif, cf. `gel_plan`) ;
        #: il ne relit donc pas le gel, mais bien les verdicts ci-dessous.
        freeze = frozen_of(db, item_id)
        frozen = ((freeze or {}).get("sha") or "").strip().lower()
        if frozen != sha:
            if not frozen:
                raise GelError("lot #%s is no longer frozen (the freeze milestone moved "
                               "while the request was prepared): nothing posed" % item_id)
            raise GelError("the freeze of lot #%s moved to %s while the request was "
                           "prepared (%s requested): refreeze and re-review, nothing posed"
                           % (item_id, frozen[:12], sha[:12]))
    assignee = (item.get("assignee") or "")
    verdicts = latest_verdicts(db, item_id, sha, assignee)
    blocked = [m for m in verdicts if (m.get("verdict") or "") == "blocked"]
    if blocked:
        raise GelError("lot #%s: %s blocked %s (last verdict of that reviewer, read under "
                       "the lock): fix the head, refreeze and re-review; nothing posed"
                       % (item_id, blocked[0].get("actor") or "a reviewer", sha[:12]))
    reviews = [m for m in verdicts if (m.get("verdict") or "") == "ok"]
    avis = int(decision.get("avis") or 0)
    if len(reviews) < avis:
        raise GelError("lot #%s: %d independent verdict(s) `ok` on %s, %d required (a "
                       "verdict changed while the request was prepared): nothing posed"
                       % (item_id, len(reviews), sha[:12], avis))


def _request_once(db, item_id: int, decision: dict, *, api, gh, force: bool,
                  cfg=None, checkout: str = "") -> dict:
    """La demande, sous le verrou de la ligne du lot (#1343, P2 R1).

    `work.note` (marque `reserve`) prend le verrou `FOR NO KEY UPDATE` de
    `work_items` dans la transaction : sur une connexion directe, deux `ci run`
    concurrents, même d'hôtes différents, se sérialisent ; le second relit les
    exécutions, la composition
    de la PR, les marques, puis le gel et les derniers verdicts (`_revalidate`,
    avis delta R1), et recule si un run est visible, si la PR a bougé, si un
    autre a déjà posé l'étiquette ou si l'autorisation a changé. Le verrou ne
    survit pas au processus tué (ROLLBACK) : le suivant reprend, sans attente
    ni marque périmée. La marque `applied` enregistre `seen=` (plus grand
    identifiant d'exécution connu avant la pose) : c'est cette corrélation, et
    non une comparaison d'horloges, qui libère la reprise d'un run rouge dès
    que son exécution est visible (P2-3).

    La sérialisation par verrou de ligne vaut pour une connexion directe. Sur
    une connexion d'exécuteur médiée (`storage.is_remote`, L109), la phase
    critique s'exécute sans transaction : la marque `applied` ne détecte
    qu'une demande déjà commitée avant sa lecture, et deux poses simultanées
    peuvent se croiser — aucune garantie inter-hôtes n'est annoncée sur ce
    chemin.

    Rend `{requested, action, why?, run?}` ; `action` vaut `request` quand
    l'étiquette a été posée, sinon `green`, `running` ou `refuse`.
    """
    from . import storage, work
    token = secrets.token_hex(8)
    sha, base, mode = decision["sha"], decision["base"], decision["mode"]
    number, label = decision["pr"], decision["label"]

    def critical(tx) -> dict:
        def note(state: str, text: str = "", seen: int | None = None) -> None:
            #: N2 #2240 : chaque marque porte le nombre d'avis déclaré, y
            #: compris 0 (`--avis 0 --full` ne passe plus sans trace).
            work.note(tx, int(item_id),
                      _request_note(state, number, label, sha, base, mode, token, text,
                                    avis=int(decision.get("avis") or 0), seen=seen))

        note("reserve", "freeze CI requested")
        known, carried = (_dispatch_request_runs(tx, item_id, api, decision)
                          if decision.get("dispatch") is not None else
                          _pr_request_runs(api, decision["repo"], number, sha,
                                           decision["branch"]))
        #: P2-3 : le plus grand identifiant d'exécution déjà connu avant la pose
        #: est la marque de l'instant ; le run de cette demande, créé après,
        #: portera un identifiant supérieur. La marque `applied` l'enregistre
        #: (`seen=`) et la reprise d'un run rouge se libère par cette
        #: corrélation, sans jamais comparer l'horodatage (base, fractionnaire)
        #: au `created_at` du run (GitHub, à la seconde). Les runs au run-name
        #: illisible entrent aussi dans le `seen=` (#2364) : la marque posée
        #: ensuite ne sera pas libérée par eux.
        watermark = max([_run_id(r["row"]) for r in known]
                        + [r["id"] for r in carried], default=0)
        #: #5192 (canon §12) : la couverture par une base de coupure se refait
        #: sur les runs relus ici — un run apparu après `gel_plan` compte aussi ;
        #: les bases déjà prouvées par le plan ne relisent pas git.
        cutoff = set(_cutoff_bases(cfg, db, work.get(db, int(item_id)) or {},
                                   decision["repo"],
                                   decision.get("base_ref") or decision.get("target") or "main",
                                   base, known, mode, checkout=checkout,
                                   proven=decision.get("cutoff_bases") or ()))
        runs = [r for r in known if _covers(r["mode"], mode)
                and (r["base"] == base or r["base"] in cutoff and _alive_or_green(r))]
        live = [r for r in runs if r["status"] in OPEN_STATUSES]
        green = [r for r in runs if r["status"] == "completed" and r["conclusion"] == "success"]
        if live:
            return {"requested": False, "action": "running",
                    "why": "a %s request is already in flight: nothing posed"
                           % live[0]["mode"], "run": _short_run(live[0]["row"])}
        if green:
            #: avis delta R1 : la phase critique refait la même décision que `gel_plan`
            #: — un vert applicable ne suffit pas si le dernier check requis
            #: est rouge ; `--force` répare, sans lui rien ne part.
            check = (None if decision.get("dispatch") is not None else
                     _latest_check(api, decision["repo"], sha))
            if check is None or (check["status"] == "completed"
                                 and check["conclusion"] in CHECK_GREEN):
                return {"requested": False, "action": "green",
                        "why": "a green %s request of this composition is visible: nothing "
                               "posed" % green[-1]["mode"],
                        "run": _short_run(green[-1]["row"])}
            if check["status"] != "completed":
                return {"requested": False, "action": "running",
                        "why": "the required `tests` check of %s is %s (run %s): nothing "
                               "posed" % (sha[:12], check["status"] or "?",
                                          check.get("id") or "?"),
                        "check": check}
            if not force:
                return {"requested": False, "action": "refuse",
                        "why": "the required `tests` check of this composition is %s "
                               "(run %s), not green although a %s request succeeded: "
                               "--force to request the CI again"
                               % (check["conclusion"] or "?", check.get("id") or "?",
                                  green[-1]["mode"])}
        if runs and not force:
            return {"requested": False, "action": "refuse",
                    "why": "a %s request of this composition already ran, none green: "
                           "--force to request the CI again" % runs[-1]["mode"]}
        #: P2 R1/P2-3 : une marque que matérialise une exécution visible (run
        #: rouge court) ne tient plus la clé ; seule une demande encore
        #: invisible tient, et la corrélation est le couple (identifiant, mode)
        #: du run, pas l'horloge — un run de lot ne matérialise pas un `full`.
        #: Un run au run-name illisible matérialise la marque de sa composition
        #: (#2364), avec le mode lu dans son libellé.
        held = reservation_holder(request_events(tx, item_id), sha, base, mode, token,
                                  now=time.time(),
                                  runs=tuple([(_run_id(r["row"]), r["mode"]) for r in runs]
                                             + [(r["id"], r["mode"]) for r in carried]))
        if held:
            note("released", "another request (token %s) already holds this composition"
                 % held["token"])
            return {"requested": False, "action": "running",
                    "why": "a request of this composition was already reserved (token %s): "
                           "nothing posed" % held["token"]}
        #: #2364 : le run illisible porte la tête et le libellé, pas la base ;
        #: vivant, il se surveille ; terminé sans `--force`, il demande la
        #: reprise explicite au lieu de laisser la marque tenue pour toujours.
        live_carried = [r for r in carried if r["status"] in OPEN_STATUSES]
        if live_carried:
            return {"requested": False, "action": "running",
                    "why": "a request is visible but its run-name is unreadable "
                           "(run %s, mode %s): its state cannot be checked; nothing posed"
                           % (live_carried[0]["id"], live_carried[0]["mode"]),
                    "run": _short_run(live_carried[0]["row"])}
        if carried and not runs and not force:
            return {"requested": False, "action": "refuse",
                    "why": "a request already ran on PR #%d (run %s, mode %s), but its "
                           "run-name is unreadable (base is not a SHA: old workflow/CLI), "
                           "so its composition cannot be checked: --force to request the "
                           "CI again (explicit resumption)"
                           % (number, carried[0]["id"], carried[0]["mode"])}
        try:
            _revalidate(tx, int(item_id), decision)
        except GelError as exc:
            note("released", str(exc))
            return {"requested": False, "action": "refuse", "why": str(exc)}
        try:
            _check_label_actor(gh)
        except GelError as exc:
            note("released", str(exc))
            return {"requested": False, "action": "refuse", "why": str(exc)}
        pr = _pr_of(api, decision["repo"], decision["branch"])
        if pr["head"] != sha:
            why = ("the pull request head moved to %s (freeze %s): nothing posed"
                   % (pr["head"][:12] or "?", sha[:12]))
            note("released", why)
            return {"requested": False, "action": "refuse", "why": why}
        if pr["base"] != base:
            why = ("the pull request base moved from %s to %s: the composition changed, "
                   "run `ameesh ci run %d` again" % (base[:12], pr["base"][:12], item_id))
            note("released", why)
            return {"requested": False, "action": "refuse", "why": why}
        # #2876 : le push peut avoir lancé le portier avant la création de
        # la PR. Son workflow_call ne porte pas le check requis de cette PR.
        # Le remplacer avant labeled, après toutes les gardes et sous verrou.
        if (decision.get("dispatch") is None and mode == "full"
                and pr["head_ref"].startswith("integration/")):
            for row in _packet_runs(api, decision["repo"], decision["branch"], sha):
                messages = []
                failed = _cancel_runs(gh, decision["repo"], [_short_run(row)],
                                      say=messages.append)
                text = "packet portier: " + "; ".join(messages)
                work.note(tx, int(item_id), text)
                if failed:
                    note("released", text)
                    return {"requested": False, "action": "refuse", "why": text}
        if decision.get("dispatch") is not None:
            # Keep the request mark on an ambiguous API outcome; do not resend.
            note("applied", "workflow dispatch intent: " +
                 ", ".join(decision["dispatch"]), seen=watermark)
            result = _pose_dispatch(gh, decision, api=api)
            work.note(tx, int(item_id), "CI workflow dispatch outcome: " +
                      (result.get("why") or "all selected workflows accepted"))
            return result
        posed = _pose_request(gh, decision["repo"], pr, label, mode)
        if not posed["ok"]:
            note("released", posed["error"])
            return {"requested": False, "action": "refuse", "why": posed["error"]}
        text = "CI requested on %s (PR #%d, base %s, mode %s)%s" % (
            sha, number, base, mode,
            ", exception %s: %s" % (decision["exception"], decision["reason"])
            if decision["exception"] else
            (", full suite: %s" % decision["reason"] if mode == "full" else ""))
        if posed["dropped"]:
            text += " — replaced %s" % ", ".join(posed["dropped"])
        if posed["failed"]:
            text += " — warnings: %s" % "; ".join(posed["failed"])
        note("applied", text, seen=watermark)
        return {"requested": True, "action": "request", "run": {},
                "dropped": posed["dropped"], "warnings": posed["failed"]}

    if storage.is_remote(db):
        # connexion d'exécuteur médiée : ni transaction ni verrou de ligne ; la
        # marque reste et le second recule devant elle, sans atomicité d'hôte —
        # la garantie de sérialisation est bornée aux connexions directes.
        return critical(db)
    with db.transaction() as tx:
        return critical(tx)


def _render_gel(decision: dict) -> list[str]:
    lines = ["lot #%d  %s  %s  %s" % (decision["work_item"], decision["repo"],
                                    decision["branch"], decision["sha"][:12])]
    lines.append("  %s: %s" % (decision["action"], decision.get("why") or ""))
    if decision.get("base"):
        lines.append("  PR #%s  base %s  mode %s  label %s"
                     % (decision.get("pr"), decision["base"][:12], decision["mode"],
                        decision.get("label") or "—"))
    if decision.get("exception"):
        lines.append("  exception %s (canon §3): %s" % (decision["exception"],
                                                        decision["reason"]))
    if decision.get("verdicts"):
        lines.append("  verdict(s): %s" % ", ".join(decision["verdicts"]))
    if decision.get("requested"):
        lines.append("  request(s) seen: %s" % ", ".join(
            "%s %s" % (r["mode"], r["conclusion"] or "?") for r in decision["requested"]))
    if decision.get("unreadable"):
        lines.append("  unreadable request(s): %s" % ", ".join(
            "run %s %s %s" % (r["id"], r["mode"], r["conclusion"])
            for r in decision["unreadable"]))
    run = decision.get("run") or {}
    if run.get("id"):
        lines.append("  run %s %s %s" % (run.get("id"), run.get("status"),
                                         run.get("url") or ""))
    armed = decision.get("auto_merge")
    if armed:
        lines.append(("  auto-merge: %s — %s" if armed["state"] in
                      ("armed", "already-armed") else
                      "  auto-merge NOT armed (%s): %s") % (armed["state"], armed["why"]))
        merge = armed.get("merge") or {}
        if merge.get("result") in ("merged", "error", "busy"):
            lines.append("  auto-merge now: %s%s" % (
                merge["result"], (" — %s" % merge["error"]) if merge.get("error") else ""))
    return lines


def _packet_prevalidation(cfg, db, item, decision, args) -> int:
    """#2644 : la prévalidation d'un porteur `integration/**`, avant l'étiquette.

    Rend 0 quand la demande peut être posée (verte, hors porteur, exception
    déclarée, ou prévalidation indisponible), 1 seulement sur un rouge : aucun
    appelant ne pose d'étiquette sur un retour non nul. Un incident déclaré
    (`--exception` ou journal `packet-exception` de cette tête) ne bloque pas :
    la porte de fusion reste la CI complète.
    """
    from . import packet_guard, prevalidation_calls, work
    head_ref = str(decision.get("head_ref") or "")
    if not head_ref.startswith("integration/"):
        return 0
    declared = str(decision.get("exception") or "")
    if not declared:
        found = packet_guard.declared(db, args.id, decision.get("sha") or "")
        declared = str((found or {}).get("exception") or "")
    if declared:
        decision["prevalidation"] = {"state": "skipped", "exception": declared}
        if not getattr(args, "json", False):
            print("  prévalidation non bloquante : exception %s déclarée pour %s"
                  % (declared, str(decision.get("sha") or "")[:12]))
        return 0
    result = prevalidation_calls.gate_packet(
        cfg, db, item, decision,
        repo=getattr(args, "prevalidation_repo", "") or "",
        map_path=getattr(args, "prevalidation_map", "") or "",
        deadline=prevalidation_calls.caller_deadline(
            getattr(args, "prevalidation_budget", None)))
    if result.get("state") == "skipped":
        #: #4136 : véhicule testé par paquet-auto, ni rouge ni non concluant.
        try:
            work.note(db, args.id, result["why"], actor="ameesh", once=True)
        except Exception:  # noqa: BLE001 - une note ne bloque pas la demande
            pass
        decision["prevalidation"] = {"state": "skipped", "manifest": result.get("manifest"),
                                     "sha": result.get("sha")}
        if not getattr(args, "json", False):
            print("  " + result["why"])
        return 0
    if result.get("state") == "red":
        #: décision pilote 19/10 : seul un rouge bloque la demande de CI.
        why = result.get("why") or "run-red"
        text = "prévalidation refusée (run-red) — %s ; aucune étiquette posée" % why
        if getattr(args, "json", False):
            print(json.dumps({"schema": GEL_SCHEMA, "ok": False, "work_item": args.id,
                              "action": "refuse", "why": text},
                             ensure_ascii=False, indent=1))
        else:
            print("ameesh ci run: " + text)
        return 1
    if result.get("state") == "unavailable":
        #: une prévalidation indisponible ne bloque jamais : le motif est
        #: journalisé pour être cité au manifeste du porteur.
        cause = str(result.get("cause") or "unavailable")
        why = str(result.get("why") or "")
        text = "prevalidation-unavailable: %s%s" % (
            cause, (" — " + why) if why else "")
        try:
            work.note(db, args.id, text, actor="ameesh", once=True)
        except Exception:  # noqa: BLE001 - une note ne bloque pas la demande
            pass
        decision["prevalidation"] = {"state": "unavailable", "cause": cause,
                                     "why": why}
        if not getattr(args, "json", False):
            print("  prévalidation indisponible (%s)%s : ci run continue, motif "
                  "journalisé (à citer au manifeste)" % (cause, (" — " + why) if why else ""))
        return 0
    report = result.get("report") or {}
    decision["prevalidation"] = {
        "state": "green", "sha": report.get("sha"),
        "host": report.get("host"), "seconds": report.get("seconds"),
        "modules": len(report.get("modules") or ())}
    if not getattr(args, "json", False):
        print("  prévalidation verte : %s, %s module(s), %s s sur %s"
              % (str(report.get("sha") or "")[:12], len(report.get("modules") or ()),
                 report.get("seconds"), report.get("host")))
    return 0


def _arm_packet(cfg, db, decision: dict, gh, args) -> dict | None:
    """#2813 (canon §12) : publier un porteur `integration/**` — sa suite
    complète demandée, en vol ou verte sur la tête gelée — arme sa fusion
    automatique sur cette tête et cette cible (`packet_automerge.arm`) :
    ameesh fusionne lui-même cette tête dans cette cible, portes vertes.

    L'issue est rangée dans `decision["auto_merge"]` et rendue avec la
    décision ; elle ne change jamais le code de retour : l'étiquette est
    posée, la CI part, et un armement manqué laisse seulement le paquet à
    fusionner à la main (le motif est au journal du lot)."""
    from . import packet_automerge, work
    if decision.get("mode") != "full" or not packet_automerge.carrier(decision.get("head_ref")) \
            or decision.get("action") not in ("request", "green", "running"):
        return None
    try:
        out = packet_automerge.arm(
            cfg, db, args.id, slug=decision["repo"], number=decision["pr"], sha=decision["sha"],
            target=decision.get("base_ref") or "", gh=packet_automerge.gh_of(gh),
            actor=getattr(args, "actor", "") or work.SYSTEM_SENDER,
            checkout=getattr(args, "prevalidation_repo", "") or "")
    except Exception as exc:  # noqa: BLE001 - l'étiquette est posée : jamais fatal
        out = {"state": "error", "why": "%s: %s" % (type(exc).__name__, exc)}
    decision["auto_merge"] = out
    return out


def cmd_ci_run(cfg, args) -> int:
    """`ameesh ci run <lot>` : vérifie le gel et pose l'étiquette, une fois.

    #2333 (canon §11 bis) : aucune étiquette `ci-lot-<sha>` entre deux paquets.
    La garde passe avant le plan — un `--dry-run` la montre aussi — et ne
    laisse partir que `--full` ou une exception `incident|unblock` motivée,
    journalisée dans la note du lot à la tête réellement testée."""
    from . import plan_github, work
    from .db import open_db
    db = open_db(cfg)
    gh = plan_github.Gh()

    def api(path: str):
        return plan_github._json(gh, ["api", path])

    item = work.get(db, args.id) if args.id else None
    if item is not None:
        freeze = frozen_of(db, args.id)
        declared_sha = ((args.sha or "") or ((freeze or {}).get("sha") or "")).strip().lower()
        try:
            packet_guard.check_ci(db, args.id, item.get("branch") or "",
                                  mode="full" if args.full else "lot",
                                  sha=declared_sha,
                                  exception=args.exception or "", reason=args.reason or "")
        except packet_guard.PacketGuardError as exc:
            if getattr(args, "json", False):
                print(json.dumps({"schema": GEL_SCHEMA, "ok": False, "work_item": args.id,
                                  "action": "refuse", "why": str(exc)},
                                 ensure_ascii=False, indent=1))
            else:
                print("ameesh ci run: %s" % exc)
            return 1

    try:
        decision = gel_plan(db, cfg, args.id, api=api, sha=args.sha or "",
                            avis=args.avis, exception=args.exception or "",
                            reason=args.reason or "", full=args.full, force=args.force,
                            repo=args.repo or "",
                            checkout=getattr(args, "prevalidation_repo", "") or "")
    except (GelError, plan_github.GithubError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"schema": GEL_SCHEMA, "ok": False, "work_item": args.id,
                              "action": "refuse", "why": str(exc)}, ensure_ascii=False,
                             indent=1))
        else:
            print("ameesh ci run: %s" % exc)
        return 1
    if args.dry_run or decision["action"] != "request":
        if not args.dry_run:
            _arm_packet(cfg, db, decision, gh, args)
        if args.json:
            print(json.dumps({**decision, "ok": True, "requested": False},
                             ensure_ascii=False, indent=1))
        else:
            print("\n".join(_render_gel(decision)))
        return 0
    # #2644 : la prévalidation #1770 passe avant l'étiquette d'un porteur
    # `integration/**` ; un refus nommé ne pose rien (aucune étiquette).
    if _packet_prevalidation(cfg, db, item, decision, args):
        return 1
    # canon §3 : un run de la branche hors de la composition courante (autre
    # SHA, autre base) est périmé ; jamais celui de la composition demandée.
    # #2333 : l'exception est journalisée à la tête que le plan a retenue
    # (celle que la demande va tester), avant la pose de l'étiquette.
    if decision.get("exception"):
        packet_guard.declare(db, args.id, decision["exception"], decision["reason"],
                             decision["sha"], actor=getattr(args, "actor", "") or "")
    stale = cancel_plan(db, cfg, args.id, api=api, repo=args.repo or "",
                        number=decision["pr"], base=decision["base"],
                        target=decision.get("base_ref") or "",
                        checkout=getattr(args, "prevalidation_repo", "") or "",
                        proven=decision.get("cutoff_bases") or (),
                        workflows=tuple(decision["dispatch"]) if decision.get("dispatch")
                        is not None else WORKFLOWS)["runs"]
    if stale:
        _cancel_runs(gh, decision["repo"], stale,
                     say=lambda text: print("  %s" % text))
    try:
        result = _request_once(db, args.id, decision, api=api, gh=gh, force=args.force,
                               cfg=cfg,
                               checkout=getattr(args, "prevalidation_repo", "") or "")
    except (GelError, plan_github.GithubError) as exc:
        print("ameesh ci run: %s" % exc)
        return 1
    if not result["requested"]:
        if result["action"] == "refuse":
            if args.json:
                print(json.dumps({"schema": GEL_SCHEMA, "ok": False, "work_item": args.id,
                                  "action": "refuse", "why": result["why"]},
                                 ensure_ascii=False, indent=1))
            else:
                print("ameesh ci run: %s" % result["why"])
            return 1
        decision = {**decision, "action": result["action"], "why": result["why"],
                    "run": result.get("run")}
        _arm_packet(cfg, db, decision, gh, args)
        if args.json:
            print(json.dumps({**decision, "ok": True, "requested": False},
                             ensure_ascii=False, indent=1))
        else:
            print("\n".join(_render_gel(decision)))
        return 0
    if result.get("warnings"):
        print("ameesh ci run: posed, with warnings: %s" % "; ".join(result["warnings"]))
    _arm_packet(cfg, db, decision, gh, args)
    if args.json:
        print(json.dumps({**decision, "ok": True, "requested": True,
                          "run": result.get("run") or {}}, ensure_ascii=False, indent=1))
    else:
        print("\n".join(_render_gel(decision)))
        print("  requested: the run on PR #%d will appear shortly (`ameesh ci status %d`)"
              % (decision["pr"], args.id))
    return 0


def cmd_ci_cancel(cfg, args) -> int:
    """`ameesh ci cancel <lot>` : annule les runs périmés de la branche."""
    from . import plan_github, work
    from .db import open_db
    db = open_db(cfg)
    gh = plan_github.Gh()

    def api(path: str):
        return plan_github._json(gh, ["api", path])

    try:
        item = work.get(db, args.id)
        if item is None:
            raise GelError("lot #%s unknown" % args.id)
        # #5154 : lire les workflows que le relevé suit pour ce dépôt
        # (#1110 : liste déclarée ; #1483 : découverte des suites quand
        # `tests.yml` n'est pas publié — nexlink). Sans elle, `_runs_of`
        # interrogeait `tests.yml` et rendait un 404 sur un dépôt qui ne le
        # publie pas.
        repo = repo_of(cfg, item, repo=args.repo or "")
        workflows = Watcher(cfg, api=api).followed_workflows(repo)
        report = cancel_plan(db, cfg, args.id, api=api, all_runs=args.all,
                             repo=args.repo or "", workflows=workflows)
    except (GelError, plan_github.GithubError) as exc:
        print("ameesh ci cancel: %s" % exc)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("lot #%d  %s  %s" % (report["work_item"], report["repo"], report["branch"]))
        print("  %s: %d run(s)" % (report["why"], len(report["runs"])))
    if not report["runs"]:
        return 0
    if args.dry_run:
        for row in report["runs"]:
            print("  would cancel run %s (%s)" % (row["id"], row["url"] or ""))
        return 0
    failed = _cancel_runs(gh, report["repo"], report["runs"])
    return 1 if failed else 0


def cmd_ci(cfg, args, out=None) -> int:
    """`ameesh ci ...` ; `out` reçoit la vue de statut (défaut : stdout).

    #4736 : la vue est écrite sur le flux qu'on lui donne, jamais sur le
    `sys.stdout` global. `redirect_stdout` est global au processus : un fil
    laissé vivant par un autre test du même processus peut y écrire pendant la
    commande et polluer une capture JSON (« Extra data »). Les commandes
    `run`/`cancel` gardent stdout (elles ne servent pas de vue capturable)."""
    from . import plan_github
    from .db import open_db
    command = getattr(args, "ci_command", "status")
    if command == "run":
        return cmd_ci_run(cfg, args)
    if command == "cancel":
        return cmd_ci_cancel(cfg, args)
    out = sys.stdout if out is None else out
    db = open_db(cfg)
    try:
        report = status_of(db, args.id, limit=args.limit)
    except CiError as exc:
        print("ameesh ci: %s" % exc, file=out)
        return 1
    # N2 #848 : la vue JSON dit les lectures GitHub partagées de l'hôte, comme
    # la vue texte (`quota_line`) ; sans état, la projection est vide.
    report["github_reads"] = plan_github.quota_stats(getattr(cfg, "state_dir", None))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1), file=out)
        return 0
    lines = render_status(report)
    line = quota_line(getattr(cfg, "state_dir", None))
    if line:
        lines.append(line)
    print("\n".join(lines), file=out)
    return 0
