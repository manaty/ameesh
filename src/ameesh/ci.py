# SPDX-License-Identifier: AGPL-3.0-only
"""Échecs de tests remontés en temps réel au courrier de l'agent du lot (L161c).

    ameesh ci status <lot> [--limit N] [--json]

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
   chaque tête porte un lot différent : #1110, #1483) ;
2. une nouvelle tête sur la branche d'un lot remplace l'exécution précédente
   (`superseded`), en cours ou déjà annulée : les messages de CI de l'ancienne tête que l'agent n'a
   pas encore reçus sont retirés sans être montrés ; un message déjà remis
   reste, car l'agent a pu agir ;
3. pour chaque exécution suivie, les check runs « tests (flux) » de sa tête :
   chaque échec nouveau est enregistré (`ci_test_failures`, 0055) ;
4. une exécution qui n'est plus en cours est conclue : verdict.

Courrier, de `ameesh` à l'assigné du lot, rangé dans le fil du lot, selon
L125 :

* premier échec de la tête : **urgent**, un seul par exécution (la CI seule ne
  peut pas déclencher `turn_churn`) ; passif (`ci_passif`) tant qu'aucune
  exécution conclue de la base n'a été relevée, car l'échec est peut-être
  hérité ;
* échecs suivants : un événement ordinaire par relevé (regroupé par la
  fenêtre de 90 s du courrier) ;
* verdict (vert, ou rouge avec la liste) : un événement ordinaire (#514 : la
  fin de CI réveille l'agent, elle ne perce pas le tour en cours ; le rouge
  perce déjà par la première alerte d'échec) ; le courrier et la référence qui
  le rend vérifiable (`ci_runs.verdict_message_id`) sont publiés dans la même
  transaction (#795 N2) ; rien pour une exécution annulée ou remplacée ; le
  verdict nomme le workflow et, quand d'autres workflows de la même tête
  tournent encore, le dit — un vert de `backend-suite.yml` n'est pas « la CI
  est verte » ;
* **rouge hérité** : un test qui échouait déjà à la dernière exécution conclue
  de la base (la cible de la PR, sinon la base du dépôt —
  `github.projects.<projet>.base`, `main` par défaut, `develop` pour Nexlink :
  #1483 ; pour une base, l'exécution précédente de la même suite) n'est jamais
  imputé à l'auteur : pas d'urgent, une ligne dans le verdict.

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
import time
from datetime import datetime

from . import storage

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
#: l'exécution qu'elle remplace soit annulée
LIVE_STATUSES = ("in_progress", "queued")
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
#: #1483 : taille des pages de listes d'exécutions du veilleur (l'API GitHub
#: plafonne à 100). À 50, une file de plus de 50 exécutions — tous workflows
#: confondus — pouvait cacher une exécution suivie, que seul le rattrapage
#: rendait, une heure plus tard.
LIST_PER_PAGE = 100
ECHECS = ("echec", "erreur")
#: conclusion GitHub → état de `ci_runs`
_CONCLUSIONS = {"success": "success", "neutral": "success", "skipped": "success",
                "cancelled": "cancelled", "stale": "cancelled"}
#: #846 : conclusions d'un check run de job qui disent un main rouge, même
#: sans événement de test publié (panne d'installation, délai dépassé)
FAILED_JOB_CONCLUSIONS = ("failure", "timed_out")
#: #846 : part des échecs de job relevés ainsi (`ci_test_failures.part`)
JOB_PART = "job"
#: lignes d'extrait au plus dans un message
EXCERPT_LINES = 40
#: échecs détaillés au plus dans un message (les autres : leur nom)
DETAILED = 3
STATUS_SCHEMA = "ameesh-ci-status/1"
#: #845 (arbitrage A du pilote, 14/10) : le relevé « runner busy sans job »
#: coûte un appel par exécution non conclue (en plus des deux listes et de
#: celle des runners) : il est mené au plus toutes les `GUARD_SCAN_S` secondes,
#: jamais à chaque passage. La fenêtre est écrite dans la base avant le premier
#: appel HTTP (`ci_guard_scan`) : deux `notify --once` concurrents, ou deux
#: hôtes, ne la mènent pas deux fois. À 600 s et 63 appels au plus par relevé,
#: cette garde seule consomme au plus 378 appels/h et par dépôt (hors appels du
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
#: la garde — les deux listes d'exécutions, un appel par exécution non conclue
#: (chacune est relue : un run resté `queued` peut porter un job déjà
#: `in_progress`, R1 cx3 B1), et la liste des runners. Le plafond couvre
#: exactement `GUARD_SCAN_MAX_RUNS` exécutions : 2 + 60 + 1 = 63. Au-delà, le
#: relevé est déclaré incomplet et RIEN n'est publié : un relevé partiel ne vaut
#: pas un relevé complet, et les relevés précédents vieillissent jusqu'à ce que
#: la fraîcheur (`exploitation.CI_GUARD_FRESH_S`, 1800 s) les clôt. Le nombre
#: d'appels réellement consommés est journalisé dans `ci_guard_scan`.
GUARD_SCAN_MAX_CALLS = 63


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


def updated_ts(run: dict) -> float | None:
    """L'horodatage de dernière mise à jour d'une exécution (ISO 8601), ou
    `None` s'il est absent ou illisible."""
    text = str(run.get("updated_at") or run.get("created_at") or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def events_of(check_runs, run_id: int) -> list[dict]:
    """Les événements `test` en échec, pour l'exécution `run_id`, portés par
    la sortie des check runs « tests (flux) » d'une tête."""
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
            key = (str(ev.get("part") or ""), ev["test"])
            if key not in seen:
                seen.add(key)
                out.append(ev)
    return out


def failed_jobs_of(check_runs, run_id: int) -> list[str]:
    """#846 : les jobs de l'exécution `run_id` déjà en échec, d'après les check
    runs natifs de GitHub — les seuls publiés même quand aucun événement de
    test ne l'est (panne d'installation, délai dépassé). Un check run d'un
    autre workflow, ou d'une autre exécution du même commit, est écarté par le
    lien vers son job (`/actions/runs/<run_id>/job/<job_id>`)."""
    out = set()
    for check in check_runs or ():
        name = str(check.get("name") or "").strip()
        if not name or name.startswith(CHECK_PREFIX):
            continue
        if (check.get("conclusion") or "") not in FAILED_JOB_CONCLUSIONS:
            continue
        link = str(check.get("details_url") or check.get("url") or "")
        if "/actions/runs/%d/job/" % int(run_id) not in link:
            continue
        out.add(name)
    return sorted(out)


def _base_set(branches) -> frozenset:
    """Les branches de base, en minuscules : la comparaison d'une branche
    GitHub ne dépend pas de la casse (#1483)."""
    return frozenset(str(b).casefold() for b in (branches or BASE_BRANCHES))


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


def _failure_lines(failures: list[dict], *, detail: bool) -> list[str]:
    out = []
    for i, f in enumerate(failures):
        out.append("• %s%s (%s, %s)" % (f["test"], " — inherited" if f.get("inherited") else "",
                                        f.get("part") or "?", f.get("status") or "echec"))
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
                 detail: bool = True, unknown_base: bool = False) -> str:
    """Le message d'un ou plusieurs échecs nouveaux d'une exécution."""
    head = _head(run, lot)
    what = ("first failure" if len(failures) == 1 else "first %d failures" % len(failures)) \
        if first else ("next failure" if len(failures) == 1
                       else "next %d failures" % len(failures))
    lines = ["%s: %s." % (head, what), ""]
    if unknown_base:
        lines += ["No finished run of %s has been seen yet: these failures may be "
                  "inherited, hence this message without a wake-up." % (run.get("base") or "main"),
                  ""]
    lines += _failure_lines(failures, detail=detail)
    lines += ["", "The suite goes on, verdict at the end. A new push replaces this "
                  "run: the messages you have not read yet will be withdrawn. No need "
                  "to wait for the CI in your turn (gh pr checks --watch): ameesh tells you."]
    return "\n".join(lines)


def verdict_text(run: dict, lot: dict | None, status: str, failures: list[dict],
                 pending: list[dict] | None = None) -> str:
    """Le message de fin d'une exécution. `pending` : les autres workflows de
    la même tête encore en cours — le verdict les nomme, pour qu'un vert d'une
    suite ne soit pas pris pour un vert de toute la CI (#1110)."""
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
        lines += _failure_lines(own + inherited, detail=False)
        first = "\n".join(lines).rstrip()
    if others:
        first += ("\n\nOther workflow(s) of this head are still running: %s — their "
                  "verdicts will follow." % ", ".join(others))
    return first


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


def recipient_of(lot: dict | None) -> str | None:
    from . import work
    to = (lot or {}).get("assignee") or ""
    if not to or to.startswith(work.HUMAN_PREFIX):
        return None
    return to


# --------------------------------------------------------------------------
# relevé
# --------------------------------------------------------------------------

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

    def __init__(self, cfg, *, api=None, log=None, clock=None, dry_run: bool = False,
                 ttl=None):
        self.cfg = cfg
        self.api = api
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
        if not settings.projects or settings.host != getattr(self.cfg, "host", None):
            return []
        return sorted(set(settings.projects.values()))

    def bases(self, repo: str) -> tuple:
        """Les branches de base d'un dépôt (#1483) : celles que déclare
        `github.projects.<projet>.base`, en tête (la première est la référence
        d'un push hors PR), puis `BASE_BRANCHES` — `main` reste une base même
        quand le dépôt déclare `develop` : traitée en branche de lot, chaque
        tête de main y remplacerait la précédente et retirerait son verdict,
        comme `ci_main_red` la lit déjà."""
        settings = self.settings()
        declared = settings.bases_for(repo) if settings else None
        return tuple(dict.fromkeys(tuple(declared or ()) + BASE_BRANCHES))

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
        soit perdue."""
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
        # #747 : fin de l'attente d'un runner, distincte de la durée des tests
        st.ci.mark_started(repo, [i for i, r in live.items()
                                  if r.get("status") == "in_progress"])
        # les exécutions courtes, nées et finies entre deux passages, n'ont
        # jamais été vues « en cours » : rattrapage borné (#1110)
        out += self._catch_up(db, repo, lots, followed, bases)
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
            out += self._failures(db, repo, row, bases)
            run = finishing.get(int(row["run_id"]))
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
        return out

    def _catch_up(self, db, repo: str, lots: list[dict], followed: set[str],
                  bases=BASE_BRANCHES) -> list[dict]:
        """Les exécutions terminées entre deux passages (#1110) : une exécution
        courte n'est jamais vue « en cours », donc jamais conclue sans ce
        rattrapage. Borné aux exécutions récentes ; une ligne déjà connue est
        laissée au relevé normal (pas de doublon) ; une tête déjà remplacée est
        ignorée (pas de verdict périmé) — **sauf sur une branche de base**
        (#1483) : chaque tête de `main` porte un lot différent, le relevé normal
        n'y remplace jamais rien, et écarter la tête M1 quand M2 est déjà
        relevée perdait le verdict et le réveil du lot fusionné avec M1."""
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
            if st.ci.run(repo, run_id) is not None:
                continue
            updated = updated_ts(run)
            if updated is None or not 0 <= now - updated <= CATCHUP_WINDOW:
                continue
            head = run.get("head_sha") or ""
            branch = run.get("head_branch") or ""
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
            out += self._failures(db, repo, row, bases)
            pending = [r for r in st.ci.running(repo) if r.get("head_sha") == head
                       and r.get("branch") == branch
                       and int(r["run_id"]) not in settled]
            out += self._conclude(db, repo, row, run=run, pending=pending)
        return out

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
        relue, et le relevé s'arrête au-delà de 60 exécutions ou de 63 appels
        (2 listes + 60 jobs + 1 runners). Chaque exécution non conclue est
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
        (#1321), tant que sa tête est encore celle de la branche ; elle
        remplace les exécutions d'une autre tête sur la même branche (hors
        base)."""
        st = storage.of(db)
        run_id = int(run["id"])
        known = st.ci.run(repo, run_id)
        if known is not None:
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
            return []
        st.ci.open_run(repo, run_id, head_sha=run.get("head_sha") or "",
                       branch=run.get("head_branch") or "", event=run.get("event") or "",
                       base=base_of(run, default=bases[0]),
                       work_item_id=lot_of_run(run, lots, bases),
                       url=run.get("html_url"), workflow=workflow_of(run.get("path")),
                       created_at=run.get("created_at"))
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

    def _failures(self, db, repo: str, row: dict, bases=BASE_BRANCHES) -> list[dict]:
        """Les échecs nouveaux d'une exécution suivie, et leur message."""
        from . import work
        st = storage.of(db)
        checks = self.api("repos/%s/commits/%s/check-runs?per_page=100"
                          % (repo, row["head_sha"])) or {}
        events = events_of(checks.get("check_runs"), int(row["run_id"]))
        self._record_failed_jobs(st, repo, row, checks.get("check_runs"), bases)
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
        new = []
        for ev in events:
            failure = {"part": str(ev.get("part") or ""), "test": ev["test"],
                       "module": ev.get("module") or "", "status": ev.get("statut"),
                       "inherited": ev["test"] in before,
                       "excerpt": ev.get("extrait") or "", "replay": ev.get("rejouer") or "",
                       "duration_s": ev.get("duree_s"), "failed_at": ev.get("ts")}
            if st.ci.add_failure(repo, int(row["run_id"]), **failure):
                new.append(failure)
        own = [f for f in new if not f["inherited"]]
        if not own:
            return []
        lot = work.get(db, int(row["work_item_id"])) if row.get("work_item_id") else None
        to = recipient_of(lot)
        if not to:
            return [{"action": "failures", "repo": repo, "run_id": row["run_id"],
                     "tests": [f["test"] for f in own], "to": None}]
        # premier message de l'exécution (un passif sans base compte aussi)
        first = not row.get("urgent_message_id") and not any(
            f.get("message_id") for f in st.ci.failures(repo, int(row["run_id"])))
        urgent = first and not unknown_base
        text = failure_text(row, lot, own, first=first, unknown_base=unknown_base)
        short = failure_text(row, lot, own, first=first, unknown_base=unknown_base,
                             detail=False)
        payload = {"ci": {"repo": repo, "run_id": row["run_id"], "head_sha": row["head_sha"],
                          "branch": row["branch"], "kind": "failure",
                          "workflow": row.get("workflow") or workflow_of(row.get("path")),
                          "tests": [f["test"] for f in own]}}
        if unknown_base:
            payload["ci_passif"] = True
        def pose(st, message_id: int) -> None:
            if urgent:
                st.ci.set_message(repo, int(row["run_id"]), "urgent", message_id)
                row["urgent_message_id"] = message_id
            st.ci.set_failure_message(repo, int(row["run_id"]),
                                      [(f["part"], f["test"]) for f in own], message_id)

        # #795 N2 : le courrier et sa preuve (`urgent_message_id`, `message_id`
        # des échecs) dans la même transaction : le NOTIFY du courrier n'est
        # délivré qu'au commit, quand l'exécuteur peut déjà le vérifier.
        message_id = _send_posed(db, to, text, short, lot=lot["id"], payload=payload,
                                 urgent=urgent, pose=pose)
        return [{"action": "failures", "repo": repo, "run_id": row["run_id"],
                 "tests": [f["test"] for f in own], "to": to, "urgent": urgent,
                 "message_id": message_id}]

    def _record_failed_jobs(self, st, repo: str, row: dict, check_runs,
                            bases=BASE_BRANCHES) -> None:
        """#846 : sur une branche de base, un job déjà en échec est un rouge de
        la base, même sans événement de test publié (panne d'installation, délai
        dépassé). Relevé ici pour que `ci.main_red` alerte sans attendre la
        conclusion de l'exécution. Aucun courrier n'en part : `_failures` ne
        signale que les événements de test (part `job` mise à part). `bases` :
        les branches de base du dépôt (#1483)."""
        if (row.get("branch") or "").casefold() not in _base_set(bases):
            return
        for job in failed_jobs_of(check_runs, int(row["run_id"])):
            st.ci.add_failure(repo, int(row["run_id"]), part=JOB_PART, test=job,
                              module="", status="echec", inherited=False,
                              excerpt="", replay="", duration_s=None, failed_at=None)

    def _conclude(self, db, repo: str, row: dict, *, run: dict | None = None,
                  pending: list[dict] | None = None) -> list[dict]:
        """Une exécution qui n'est plus en cours : son verdict. `run` : son
        dernier état GitHub, déjà relevé ; `pending` : les autres workflows de
        la même tête encore en cours, que le verdict nomme."""
        from . import work
        st = storage.of(db)
        if run is None:
            run = self.api("repos/%s/actions/runs/%s" % (repo, row["run_id"])) or {}
        if (run.get("status") or "") != "completed":
            return []  # en attente d'un runner, ou relancée : au prochain relevé
        status = _CONCLUSIONS.get(run.get("conclusion") or "", "failure")
        if not st.ci.conclude(repo, int(row["run_id"]), status):
            return []
        if status == "cancelled":
            return [{"action": "verdict", "repo": repo, "run_id": row["run_id"],
                     "status": status, "to": None}]
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
        text = verdict_text(view, lot, status, failures, pending=pending)
        payload = {"ci": {"repo": repo, "run_id": row["run_id"], "head_sha": row["head_sha"],
                          "branch": row["branch"], "kind": "verdict", "status": status,
                          "workflow": view.get("workflow") or workflow_of(view.get("path")),
                          "attempt": int(view.get("run_attempt") or 1),
                          "failures": len([f for f in failures if not f.get("inherited")]),
                          "inherited": len([f for f in failures if f.get("inherited")])}}
        # #514 : la fin de CI réveille l'agent (événement ordinaire), elle ne
        # perce pas le tour en cours ; le rouge perce déjà par la première
        # alerte d'échec, urgente et vérifiable en base (`interrupt_allowed`).
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
    to = "→ %s%s" % (record["to"], " (urgent)" if record.get("urgent") else "") \
        if record.get("to") else "(no recipient)"
    if record["action"] == "failures":
        return "%s: %d failure(s) %s" % (where, len(record.get("tests") or ()), to)
    return "%s: verdict %s %s" % (where, record.get("status"), to)


# --------------------------------------------------------------------------
# alerte ci_main_red
# --------------------------------------------------------------------------

def main_red(db, bases=BASE_BRANCHES) -> list[dict]:
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
    `{repo, branch, run_id, head_sha, url, work_item_id, concluded_ts, tests}`."""
    st = storage.of(db)
    out, newest = [], {}
    for row in st.ci.latest_concluded(bases):
        key = (row["repo"], row["branch"], row.get("workflow"))
        newest[key] = max(newest.get(key, 0), int(row["run_id"]))
        if row.get("status") != "failure":
            continue
        tests = [f["test"] for f in st.ci.failures(row["repo"], int(row["run_id"]))]
        out.append(dict(row, tests=tests))
    for row in st.ci.running_on(bases):
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

def status_of(db, item_id: int, limit: int = 5) -> dict:
    from . import work
    item = work.get(db, int(item_id))
    if item is None:
        raise CiError("lot #%s unknown" % item_id)
    st = storage.of(db)
    runs = []
    for row in st.ci.for_lot(int(item_id), limit):
        failures = st.ci.failures(row["repo"], int(row["run_id"]))
        runs.append(dict(row, failures=[{k: f.get(k) for k in (
            "part", "test", "status", "inherited", "replay", "failed_ts")} for f in failures]))
    return {"schema": STATUS_SCHEMA, "work_item": int(item_id), "title": item.get("title"),
            "branch": item.get("branch"), "runs": runs}


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
    for run in report["runs"]:
        state = _STATUS_FR.get(run["status"], run["status"])
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
        for f in own[:10]:
            lines.append("      %s (%s)%s" % (f["test"], f.get("part") or "?",
                                              "  → %s" % f["replay"] if f.get("replay") else ""))
        if len(own) > 10:
            lines.append("      … and %d more" % (len(own) - 10))
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


def cmd_ci(cfg, args) -> int:
    from . import plan_github
    from .db import open_db
    db = open_db(cfg)
    try:
        report = status_of(db, args.id, limit=args.limit)
    except CiError as exc:
        print("ameesh ci: %s" % exc)
        return 1
    # N2 #848 : la vue JSON dit les lectures GitHub partagées de l'hôte, comme
    # la vue texte (`quota_line`) ; sans état, la projection est vide.
    report["github_reads"] = plan_github.quota_stats(getattr(cfg, "state_dir", None))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0
    lines = render_status(report)
    line = quota_line(getattr(cfg, "state_dir", None))
    if line:
        lines.append(line)
    print("\n".join(lines))
    return 0
