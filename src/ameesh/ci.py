# SPDX-License-Identifier: AGPL-3.0-only
"""Échecs de tests remontés en temps réel au courrier de l'agent du lot (L161c).

    ameesh ci status <lot> [--limit N] [--json]

Étude L161 (docs/design/etudes/l161-tests-par-impact.md, section 3), voie A :
pendant les tests, chaque part de la CI publie un check run « tests (flux) »
(`scripts/ci-flow.py publish`) dont la sortie porte, en JSON lignes, chaque
test en échec au schéma `ameesh-test-event/1` (L161b). `ameesh notify`, sur
l'hôte que désigne `github.host` (comme les issues des lots, L126), fait à
chaque passage un relevé (`Watcher.tick`) :

1. les exécutions en cours ou en attente du workflow des tests (`WORKFLOWS`) de chaque dépôt
   de `github.projects` (une requête par dépôt) ; chacune est rattachée à un
   lot : par sa branche (`work_items.branch`, L118), sinon par la ligne
   `ameesh-work: <id>` de son commit de tête (une fusion dans main) ;
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
  une exécution annulée ou remplacée ;
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

from . import storage

#: workflows dont les exécutions sont suivies (nom du fichier)
WORKFLOWS = ("tests.yml",)
#: états GitHub d'une exécution pas encore conclue que le relevé lit : une
#: exécution en attente d'un runner est vue dès la poussée, avant que
#: l'exécution qu'elle remplace soit annulée
LIVE_STATUSES = ("in_progress", "queued")
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
    return "CI of %s, head %s on %s (%s)" % (where, _short(run["head_sha"]), run["branch"],
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


def verdict_text(run: dict, lot: dict | None, status: str, failures: list[dict]) -> str:
    """Le message de fin d'une exécution."""
    head = _head(run, lot)
    own = [f for f in failures if not f.get("inherited")]
    inherited = [f for f in failures if f.get("inherited")]
    if status == "success" and not failures:
        return "%s: green." % head
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
        lines.append("%s: red, %d failure(s)%s." % (
            head, len(own), ", and %d inherited from the base" % len(inherited) if inherited else ""))
    lines.append("")
    lines += _failure_lines(own + inherited, detail=False)
    return "\n".join(lines).rstrip()


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
    les alertes."""

    def __init__(self, cfg, *, api=None, log=None, clock=None, dry_run: bool = False):
        self.cfg = cfg
        self.api = api
        self.log = log or (lambda _text: None)
        self.clock = clock or time.time
        self.dry_run = bool(dry_run)

    def repos(self) -> list[str]:
        from . import plan_github
        try:
            settings = plan_github.settings_of(self.cfg)
        except plan_github.SettingsError:
            return []  # déjà journalisé par la projection des issues
        if not settings.projects or settings.host != getattr(self.cfg, "host", None):
            return []
        return sorted(set(settings.projects.values()))

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
        live = {}
        for status in LIVE_STATUSES:
            data = self.api("repos/%s/actions/runs?status=%s&per_page=50" % (repo, status)) or {}
            for r in data.get("workflow_runs") or ():
                if str(r.get("path") or "").rsplit("/", 1)[-1] in WORKFLOWS:
                    live.setdefault(int(r["id"]), r)
        lots = [r for r in st.work.open_for_sweep(500) if r.get("branch")]
        out: list[dict] = []
        for run_id in sorted(live):
            out += self._follow(db, repo, live[run_id], lots)
        # #747 : fin de l'attente d'un runner, distincte de la durée des tests
        st.ci.mark_started(repo, [i for i, r in live.items()
                                  if r.get("status") == "in_progress"])
        for row in st.ci.running(repo):
            out += self._failures(db, repo, row)
            if int(row["run_id"]) not in live:
                out += self._conclude(db, repo, row)
        # #845 : la garde passe après les échecs et les verdicts — un relevé de
        # runners coûte une requête par exécution non conclue (quelques
        # dizaines de secondes en saturation) et ne doit pas les retarder
        self._guard(db, repo)
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
        try:
            self._guard_state(db, repo)
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

    def _follow(self, db, repo: str, run: dict, lots: list[dict]) -> list[dict]:
        """Une exécution en cours : enregistrée si nouvelle ; elle remplace
        les exécutions d'une autre tête sur la même branche (hors base)."""
        st = storage.of(db)
        run_id = int(run["id"])
        known = st.ci.run(repo, run_id)
        if known is not None:
            return []
        st.ci.open_run(repo, run_id, head_sha=run.get("head_sha") or "",
                       branch=run.get("head_branch") or "", event=run.get("event") or "",
                       base=base_of(run), work_item_id=lot_of_run(run, lots),
                       url=run.get("html_url"), created_at=run.get("created_at"))
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
        reference = st.ci.base_failures(repo, row.get("base") or BASE_BRANCHES[0])
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

    def _conclude(self, db, repo: str, row: dict) -> list[dict]:
        """Une exécution qui n'est plus en cours : son verdict."""
        from . import work
        st = storage.of(db)
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
        text = verdict_text(row, lot, status, failures)
        payload = {"ci": {"repo": repo, "run_id": row["run_id"], "head_sha": row["head_sha"],
                          "branch": row["branch"], "kind": "verdict", "status": status,
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
    """Les dernières exécutions conclues des branches de base qui sont rouges :
    `{repo, branch, run_id, head_sha, url, work_item_id, concluded_ts, tests}`."""
    st = storage.of(db)
    out = []
    for row in st.ci.latest_concluded(BASE_BRANCHES):
        if row.get("status") != "failure":
            continue
        tests = [f["test"] for f in st.ci.failures(row["repo"], int(row["run_id"]))]
        out.append(dict(row, tests=tests))
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
        lines.append("  %s  %s  %s  %s%s%s%s" % (
            local_time(run["first_seen_ts"]), _short(run["head_sha"]), state,
            "%d failure(s)" % len(own), ", %d inherited" % inherited if inherited else "",
            "  (%s)" % wait if wait else "",
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
