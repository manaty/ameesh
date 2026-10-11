# SPDX-License-Identifier: AGPL-3.0-only
"""Échecs de tests remontés en temps réel au courrier de l'agent du lot (L161c).

    ameesh ci status <lot> [--limit N] [--json]

Étude L161 (docs/design/etudes/l161-tests-par-impact.md, section 3), voie A :
pendant les tests, chaque part de la CI publie un check run « tests (flux) »
(`scripts/ci-flux.py publier`) dont la sortie porte, en JSON lignes, chaque
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
#: préfixe du nom des check runs publiés par `scripts/ci-flux.py publier`
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
        lines = lines[:EXCERPT_LINES] + ["… (%d ligne(s) de plus)" % (len(lines) - EXCERPT_LINES)]
    return "\n".join(lines)


def _short(sha: str) -> str:
    return (sha or "")[:10]


def _head(run: dict, lot: dict | None) -> str:
    where = "lot #%d « %s »" % (lot["id"], lot.get("title") or "") if lot else "sans lot"
    return "CI de %s, tête %s sur %s (%s)" % (where, _short(run["head_sha"]), run["branch"],
                                              run.get("url") or "exécution %s" % run["run_id"])


def _failure_lines(failures: list[dict], *, detail: bool) -> list[str]:
    out = []
    for i, f in enumerate(failures):
        out.append("• %s%s (%s, %s)" % (f["test"], " — hérité" if f.get("inherited") else "",
                                        f.get("part") or "?", f.get("status") or "echec"))
        if f.get("replay"):
            out.append("  rejouer : %s" % f["replay"])
        if detail and i < DETAILED and f.get("excerpt"):
            out.append("")
            out.extend("    " + line for line in _excerpt(f["excerpt"]).splitlines())
            out.append("")
    if detail and len(failures) > DETAILED:
        out.append("(extraits des %d premiers seulement)" % DETAILED)
    return out


def failure_text(run: dict, lot: dict | None, failures: list[dict], *, first: bool,
                 detail: bool = True, unknown_base: bool = False) -> str:
    """Le message d'un ou plusieurs échecs nouveaux d'une exécution."""
    head = _head(run, lot)
    what = ("premier échec" if len(failures) == 1 else "%d premiers échecs" % len(failures)) \
        if first else ("échec suivant" if len(failures) == 1
                       else "%d échecs suivants" % len(failures))
    lines = ["%s : %s." % (head, what), ""]
    if unknown_base:
        lines += ["Aucune exécution conclue de %s n'a encore été relevée : ces échecs sont "
                  "peut-être hérités, d'où ce message sans réveil." % (run.get("base") or "main"),
                  ""]
    lines += _failure_lines(failures, detail=detail)
    lines += ["", "La suite continue, verdict à la fin. Une nouvelle poussée remplace ce "
                  "passage : les messages que tu n'as pas encore lus seront retirés. Inutile "
                  "d'attendre la CI dans ton tour (gh pr checks --watch) : ameesh te prévient."]
    return "\n".join(lines)


def verdict_text(run: dict, lot: dict | None, status: str, failures: list[dict]) -> str:
    """Le message de fin d'une exécution."""
    head = _head(run, lot)
    own = [f for f in failures if not f.get("inherited")]
    inherited = [f for f in failures if f.get("inherited")]
    if status == "success" and not failures:
        return "%s : verte." % head
    lines = []
    if status == "success":
        lines.append("%s : verte, malgré des échecs relevés en cours de route "
                     "(réessayés ou tolérés)." % head)
    elif not own and inherited:
        lines.append("%s : rouge, mais seulement des échecs hérités de %s (déjà rouges "
                     "avant ta poussée) : rien ne t'est imputé."
                     % (head, run.get("base") or "main"))
    elif not failures:
        lines.append("%s : rouge, sans test en échec relevé dans le flux (part interrompue, "
                     "délai dépassé ou autre job) : voir l'exécution." % head)
    else:
        lines.append("%s : rouge, %d échec(s)%s." % (
            head, len(own), ", et %d hérité(s) de la base" % len(inherited) if inherited else ""))
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
                self.log("CI %s : relevé en échec (%s)" % (repo, " ".join(str(exc).split())[:200]))
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
        for row in st.ci.running(repo):
            out += self._failures(db, repo, row)
            if int(row["run_id"]) not in live:
                out += self._conclude(db, repo, row)
        return out

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
                       url=run.get("html_url"))
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
    where = "CI %s exécution %s" % (record.get("repo"), record.get("run_id"))
    if record["action"] == "superseded":
        return "%s remplacée par %s ; %d message(s) non lu(s) retiré(s)" % (
            where, _short(record.get("by") or ""), len(record.get("withdrawn") or ()))
    to = "→ %s%s" % (record["to"], " (urgent)" if record.get("urgent") else "") \
        if record.get("to") else "(sans destinataire)"
    if record["action"] == "failures":
        return "%s : %d échec(s) %s" % (where, len(record.get("tests") or ()), to)
    return "%s : verdict %s %s" % (where, record.get("status"), to)


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


# --------------------------------------------------------------------------
# ameesh ci status
# --------------------------------------------------------------------------

def status_of(db, item_id: int, limit: int = 5) -> dict:
    from . import work
    item = work.get(db, int(item_id))
    if item is None:
        raise CiError("lot #%s inconnu" % item_id)
    st = storage.of(db)
    runs = []
    for row in st.ci.for_lot(int(item_id), limit):
        failures = st.ci.failures(row["repo"], int(row["run_id"]))
        runs.append(dict(row, failures=[{k: f.get(k) for k in (
            "part", "test", "status", "inherited", "replay", "failed_ts")} for f in failures]))
    return {"schema": STATUS_SCHEMA, "work_item": int(item_id), "title": item.get("title"),
            "branch": item.get("branch"), "runs": runs}


_STATUS_FR = {"running": "en cours", "success": "verte", "failure": "rouge",
              "cancelled": "annulée", "superseded": "remplacée"}


def render_status(report: dict) -> list[str]:
    from .notify import local_time
    lines = ["lot #%d « %s » (branche %s)" % (report["work_item"], report.get("title") or "",
                                             report.get("branch") or "—")]
    if not report["runs"]:
        lines.append("  aucune exécution de CI relevée (relevé de `ameesh notify` sur l'hôte "
                     "désigné par github.host)")
    for run in report["runs"]:
        state = _STATUS_FR.get(run["status"], run["status"])
        if run["status"] == "superseded" and run.get("superseded_by"):
            state += " par %s" % _short(run["superseded_by"])
        own = [f for f in run["failures"] if not f.get("inherited")]
        inherited = len(run["failures"]) - len(own)
        lines.append("  %s  %s  %s  %s%s%s" % (
            local_time(run["first_seen_ts"]), _short(run["head_sha"]), state,
            "%d échec(s)" % len(own), ", %d hérité(s)" % inherited if inherited else "",
            "  %s" % run["url"] if run.get("url") else ""))
        for f in own[:10]:
            lines.append("      %s (%s)%s" % (f["test"], f.get("part") or "?",
                                              "  → %s" % f["replay"] if f.get("replay") else ""))
        if len(own) > 10:
            lines.append("      … et %d autre(s)" % (len(own) - 10))
    return lines


def cmd_ci(cfg, args) -> int:
    from .db import open_db
    db = open_db(cfg)
    try:
        report = status_of(db, args.id, limit=args.limit)
    except CiError as exc:
        print("ameesh ci : %s" % exc)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("\n".join(render_status(report)))
    return 0
