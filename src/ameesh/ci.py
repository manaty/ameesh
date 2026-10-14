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
   (`actions/workflows`) : un dépôt dont les suites portent d'autres noms
   (nexlink : `backend-suite.yml`, `sql-invariant.yml`…) n'est plus ignoré en
   silence (#1110). Chaque exécution porte son fichier de workflow, pour que
   le verdict nomme la suite concernée ; chacune est rattachée à un lot : par
   sa branche (`work_items.branch`, L118), sinon par la ligne
   `ameesh-work: <id>` de son commit de tête (une fusion dans main) ; une
   exécution courte peut naître et finir entre deux passages sans jamais être
   vue « en cours » : un rattrapage borné des exécutions récemment terminées
   les découvre, rend leur verdict et réveille l'agent — une seule fois, et
   jamais pour une tête déjà remplacée (#1110) ;
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
* verdict (vert, ou rouge avec la liste) : un événement ordinaire ; rien pour
  une exécution annulée ou remplacée ; le verdict nomme le workflow et, quand
  d'autres workflows de la même tête tournent encore, le dit — un vert de
  `backend-suite.yml` n'est pas « la CI est verte » ;
* **rouge hérité** : un test qui échouait déjà à la dernière exécution conclue
  de la base (la cible de la PR, sinon main ; pour main, l'exécution
  précédente de main) n'est jamais imputé à l'auteur : pas d'urgent, une ligne
  dans le verdict.

Une exécution rouge sur main lève aussi l'alerte `ci_main_red`
(`exploitation`, poussée par `ameesh notify` au responsable humain).

Le réessai d'un échec (étude : « premier échec confirmé ») n'est pas encore
émis par le flux : un échec compte dès sa première occurrence.
"""
from __future__ import annotations

import json
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
#: branches de base : une exécution n'y est jamais remplacée, et ses échecs
#: servent de référence au rouge hérité
BASE_BRANCHES = ("main",)
ECHECS = ("echec", "erreur")
#: conclusion GitHub → état de `ci_runs`
_CONCLUSIONS = {"success": "success", "neutral": "success", "skipped": "success",
                "cancelled": "cancelled", "stale": "cancelled"}
#: lignes d'extrait au plus dans un message
EXCERPT_LINES = 40
#: échecs détaillés au plus dans un message (les autres : leur nom)
DETAILED = 3
STATUS_SCHEMA = "ameesh-ci-status/1"


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


def lot_of_run(run: dict, lots: list[dict]) -> int | None:
    """Le lot d'une exécution : par sa branche (lots ouverts), sinon par la
    ligne `ameesh-work:` de son commit de tête."""
    from .plan_git import WORK_TRAILER_RE
    branch = (run.get("head_branch") or "").casefold()
    if branch and branch not in BASE_BRANCHES:
        for lot in lots:
            if (lot.get("branch") or "").casefold() == branch:
                return int(lot["id"])
    message = ((run.get("head_commit") or {}).get("message")) or ""
    match = WORK_TRAILER_RE.search(message)
    return int(match.group(1)) if match else None


def base_of(run: dict) -> str:
    """La branche de référence du rouge hérité : la cible de la PR, sinon main."""
    for pr in run.get("pull_requests") or ():
        ref = ((pr.get("base") or {}).get("ref")) or ""
        if ref:
            return ref
    return BASE_BRANCHES[0]


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
          urgent: bool) -> int:
    """Envoie ; un texte refusé comme illisible (extrait binaire ou encodé)
    part sans ses extraits."""
    from . import mail, work
    from .fil import UnreadableBody
    body = dict(payload)
    if urgent:
        body["urgent"] = True
    try:
        return mail.send(db, work.SYSTEM_SENDER, to, text, kind="event",
                         work_item_id=str(lot) if lot else None, payload=body)
    except UnreadableBody:
        return mail.send(db, work.SYSTEM_SENDER, to, short, kind="event",
                         work_item_id=str(lot) if lot else None, payload=body)


def recipient_of(lot: dict | None) -> str | None:
    from . import work
    to = (lot or {}).get("assignee") or ""
    if not to or to.startswith(work.HUMAN_PREFIX):
        return None
    return to


# --------------------------------------------------------------------------
# relevé
# --------------------------------------------------------------------------

def gh_api(gh=None):
    """La fonction `chemin -> JSON` de l'API GitHub, par `gh api`."""
    from . import plan_github

    def api(path: str):
        nonlocal gh
        gh = gh or plan_github.Gh()
        return plan_github._json(gh, ["api", path])
    return api


class Watcher:
    """Le relevé de la CI, mené par `ameesh notify` à chaque passage, sur
    l'hôte désigné (`github.host`). Une erreur (GitHub, base) d'un dépôt est
    journalisée une fois par passage et n'arrête ni les autres dépôts, ni
    les alertes."""

    def __init__(self, cfg, *, api=None, log=None, clock=None, dry_run: bool = False):
        self.cfg = cfg
        self.api = api
        self.log = log or (lambda _text: None)
        self.clock = clock or time.time
        self.dry_run = bool(dry_run)
        #: découverte des workflows par dépôt : (horodatage, ttl, noms)
        self._workflows_cache: dict[str, tuple] = {}
        #: avertissements déjà journalisés par ce passage de service
        self._warned_workflows: set = set()

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

    def discovered_workflows(self, repo: str) -> tuple[str, ...] | None:
        """Les fichiers de workflow actifs du dépôt (API GitHub), ou None si
        l'API ne répond pas. Le résultat est gardé : une découverte par heure,
        une nouvelle tentative après un échec."""
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
        découverts — un dépôt dont les suites portent d'autres noms n'est
        jamais ignoré en silence (#1110)."""
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
        return found

    def tick(self, db) -> list[dict]:
        """Un relevé de chaque dépôt ; rend les envois et retraits."""
        repos = self.repos()
        if not repos or self.dry_run:
            return []
        if self.api is None:
            self.api = gh_api()
        out: list[dict] = []
        for repo in repos:
            try:
                out += self.watch(db, repo)
            except Exception as exc:  # noqa: BLE001 - jamais fatal au passage
                self.log("CI %s: check failed (%s)" % (repo, " ".join(str(exc).split())[:200]))
        return out

    # -- un dépôt --------------------------------------------------------------
    def watch(self, db, repo: str) -> list[dict]:
        st = storage.of(db)
        followed = set(self.followed_workflows(repo))
        live = {}
        for status in LIVE_STATUSES:
            data = self.api("repos/%s/actions/runs?status=%s&per_page=50" % (repo, status)) or {}
            for r in data.get("workflow_runs") or ():
                if workflow_of(r.get("path")) in followed:
                    live.setdefault(int(r["id"]), r)
        lots = [r for r in st.work.open_for_sweep(500) if r.get("branch")]
        out: list[dict] = []
        for run_id in sorted(live):
            out += self._follow(db, repo, live[run_id], lots)
        # les exécutions courtes, nées et finies entre deux passages, n'ont
        # jamais été vues « en cours » : rattrapage borné (#1110)
        out += self._catch_up(db, repo, lots, followed)
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
            out += self._failures(db, repo, row)
            run = finishing.get(int(row["run_id"]))
            if run is None:
                continue
            others = [r for r in running if int(r["run_id"]) not in finishing
                      and int(r["run_id"]) != int(row["run_id"])
                      and r.get("head_sha") == row.get("head_sha")
                      and r.get("branch") == row.get("branch")]
            out += self._conclude(db, repo, row, run=run, pending=others)
        return out

    def _catch_up(self, db, repo: str, lots: list[dict], followed: set[str]) -> list[dict]:
        """Les exécutions terminées entre deux passages (#1110) : une exécution
        courte n'est jamais vue « en cours », donc jamais conclue sans ce
        rattrapage. Borné aux exécutions récentes ; une ligne déjà connue est
        laissée au relevé normal (pas de doublon) ; une tête déjà remplacée sur
        la même branche est ignorée (pas de verdict périmé)."""
        st = storage.of(db)
        data = self.api("repos/%s/actions/runs?status=completed&per_page=50" % repo) or {}
        runs = list(data.get("workflow_runs") or ())
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
            if st.ci.head_replaced(repo, branch, head, run_id):
                continue
            out += self._follow(db, repo, run, lots)
            row = st.ci.run(repo, run_id)
            if row is None or (run.get("status") or "") != "completed":
                continue
            out += self._failures(db, repo, row)
            pending = [r for r in st.ci.running(repo) if r.get("head_sha") == head
                       and r.get("branch") == branch
                       and int(r["run_id"]) not in settled]
            out += self._conclude(db, repo, row, run=run, pending=pending)
        return out

    def _follow(self, db, repo: str, run: dict, lots: list[dict]) -> list[dict]:
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
                lot = lot_of_run(run, lots)
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
                if lot is not None and (not known_head or head == known_head):
                    st.ci.open_run(repo, run_id, head_sha=head,
                                   branch=run.get("head_branch") or "",
                                   event=run.get("event") or "", base=base_of(run),
                                   work_item_id=lot, url=run.get("html_url"))
                    self.log("CI %s run %s: lot #%s linked (branch set later)"
                             % (repo, run_id, lot))
                elif lot is not None:
                    self.log("CI %s run %s: obsolete head %s (branch head %s), not linked"
                             % (repo, run_id, _short(head), _short(known_head)))
            return []
        st.ci.open_run(repo, run_id, head_sha=run.get("head_sha") or "",
                       branch=run.get("head_branch") or "", event=run.get("event") or "",
                       base=base_of(run), work_item_id=lot_of_run(run, lots),
                       url=run.get("html_url"), workflow=workflow_of(run.get("path")))
        branch = run.get("head_branch") or ""
        if not branch or branch in BASE_BRANCHES:
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

    def _failures(self, db, repo: str, row: dict) -> list[dict]:
        """Les échecs nouveaux d'une exécution suivie, et leur message."""
        from . import work
        st = storage.of(db)
        checks = self.api("repos/%s/commits/%s/check-runs?per_page=100"
                          % (repo, row["head_sha"])) or {}
        events = events_of(checks.get("check_runs"), int(row["run_id"]))
        if not events:
            return []
        reference = st.ci.base_failures(repo, row.get("base") or BASE_BRANCHES[0],
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
        message_id = _send(db, to, text, short, lot=lot["id"], payload=payload, urgent=urgent)
        if urgent:
            st.ci.set_message(repo, int(row["run_id"]), "urgent", message_id)
            row["urgent_message_id"] = message_id
        st.ci.set_failure_message(repo, int(row["run_id"]),
                                  [(f["part"], f["test"]) for f in own], message_id)
        return [{"action": "failures", "repo": repo, "run_id": row["run_id"],
                 "tests": [f["test"] for f in own], "to": to, "urgent": urgent,
                 "message_id": message_id}]

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
        message_id = _send(db, to, text, text, lot=lot["id"], payload=payload, urgent=False)
        st.ci.set_message(repo, int(row["run_id"]), "verdict", message_id)
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

def main_red(db) -> list[dict]:
    """Les dernières exécutions conclues des branches de base qui sont rouges,
    **par workflow** : sur la dernière tête conclue de la branche, un succès
    d'une suite ne masque pas l'échec d'une autre (#1110).
    `{repo, branch, run_id, head_sha, url, work_item_id, concluded_ts, tests}`."""
    st = storage.of(db)
    out = []
    for row in st.ci.latest_concluded(BASE_BRANCHES):
        if row.get("status") != "failure":
            continue
        tests = [f["test"] for f in st.ci.failures(row["repo"], int(row["run_id"]))]
        out.append(dict(row, tests=tests))
    return out


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
        lines.append("  %s  %s  %s  %s  %s%s%s" % (
            local_time(run["first_seen_ts"]), _short(run["head_sha"]),
            run.get("workflow") or "—", state,
            "%d failure(s)" % len(own), ", %d inherited" % inherited if inherited else "",
            "  %s" % run["url"] if run.get("url") else ""))
        for f in own[:10]:
            lines.append("      %s (%s)%s" % (f["test"], f.get("part") or "?",
                                              "  → %s" % f["replay"] if f.get("replay") else ""))
        if len(own) > 10:
            lines.append("      … and %d more" % (len(own) - 10))
    return lines


def cmd_ci(cfg, args) -> int:
    from .db import open_db
    db = open_db(cfg)
    try:
        report = status_of(db, args.id, limit=args.limit)
    except CiError as exc:
        print("ameesh ci: %s" % exc)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("\n".join(render_status(report)))
    return 0
