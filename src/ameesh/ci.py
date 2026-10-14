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
runs périmés d'un lot.

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

import datetime
import json
import re
import secrets
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
        for row in st.ci.running(repo):
            out += self._failures(db, repo, row)
            if int(row["run_id"]) not in live:
                out += self._conclude(db, repo, row)
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
        lines.append("  %s  %s  %s  %s%s%s" % (
            local_time(run["first_seen_ts"]), _short(run["head_sha"]), state,
            "%d failure(s)" % len(own), ", %d inherited" % inherited if inherited else "",
            "  %s" % run["url"] if run.get("url") else ""))
        for f in own[:10]:
            lines.append("      %s (%s)%s" % (f["test"], f.get("part") or "?",
                                              "  → %s" % f["replay"] if f.get("replay") else ""))
        if len(own) > 10:
            lines.append("      … and %d more" % (len(own) - 10))
    return lines


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
#     le merge-ref composé de la base du payload et de cette tête (ses deux
#     parents), et sélectionne contre la base du payload ;
#   * le jeton `gh` doit produire l'événement `pull_request: labeled` : un
#     `GITHUB_TOKEN` d'Actions (acteur `github-actions[bot]`) ne déclenche
#     aucun workflow et est refusé ;
#   * idempotence bornée au parcours PR et à la composition courante (numéro
#     de PR, tête gelée S, base du payload, mode) : un vert ou un run en vol de
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
# une cible `release/**` ou `integration/**` l'impose et refuse `ci-lot`.
#
# `ameesh ci cancel <lot>` fait la même annulation seule (`--all` : tout, y
# compris le run de la composition gelée).

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
#: le titre du run (run-name de tests.yml) : PR, étiquette, base du payload
PR_RUN_RE = re.compile(
    r"\[PR #(?P<number>\d+) (?P<label>ci-(?:lot|full)-[0-9a-f]{40}) "
    r"base (?P<base>[0-9a-f]{40})\]")
#: la marque de demande dans le journal du lot : `reserve` prend le verrou de
#: la ligne, `applied` la referme sur une étiquette posée, `released` sur un
#: refus. Le motif ne dépend pas du texte qui suit.
REQUEST_RE = re.compile(
    r"^ci-label (?P<state>reserve|applied|released) pr=(?P<pr>\d+) "
    r"label=(?P<label>ci-(?:lot|full)-[0-9a-f]{40}) sha=(?P<sha>[0-9a-f]{40}) "
    r"base=(?P<base>[0-9a-f]{40}) mode=(?P<mode>lot|full) token=(?P<token>[0-9a-f]{16})\b")
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


def label_name(mode: str, sha: str) -> str:
    """L'étiquette d'une demande : `ci-lot-<40hex>` ou `ci-full-<40hex>`."""
    if mode not in ("lot", "full"):
        raise GelError("label mode: %r (lot or full)" % mode)
    if not _sha_ok(sha):
        raise GelError("label sha: full 40-hex expected, not %r" % sha)
    return "ci-%s-%s" % (mode, sha)


def repo_of(cfg, item: dict, *, repo: str = "") -> str:
    """`owner/repo` du lot : `--repo`, son `issue_ref`, sinon `github.projects`."""
    from . import plan_github
    if repo.strip():
        return repo.strip()
    parts = plan_github._ref_parts(item.get("issue_ref"))
    if parts and parts[0]:
        return parts[0]
    try:
        found = plan_github.settings_of(cfg).repo_for(item.get("app"))
    except plan_github.SettingsError as exc:
        raise GelError(str(exc)) from exc
    if found:
        return found
    raise GelError("lot #%s: no issue_ref and no github.projects entry for %r: "
                   "pass --repo owner/name" % (item.get("id"), item.get("app")))


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
             event: str = "") -> list[dict]:
    query = "per_page=50"
    if sha:
        query += "&head_sha=%s" % _q(sha)
    if branch:
        query += "&branch=%s" % _q(branch)
    if event:
        query += "&event=%s" % _q(event)
    data = api("repos/%s/actions/workflows/tests.yml/runs?%s" % (repo, query)) or {}
    return list(data.get("workflow_runs") or ())


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


def _pr_runs(api, repo: str, number: int, sha: str, branch: str) -> list[dict]:
    """Les demandes de gel déjà vues pour cette PR et cette tête."""
    out = []
    for row in _runs_of(api, repo, branch=branch, event="pull_request"):
        info = pr_run(row)
        if info and info["number"] == number and info["sha"] == sha:
            out.append(info)
    return out


def _current_run(row: dict, frozen: str, number: int, base: str) -> bool:
    """Un run de la composition courante : ni périmé, ni à annuler.

    Avec `number`/`base` (une demande de gel), un run de PR de la même tête
    mais d'une autre base est périmé : la composition a bougé. Sans eux
    (`ameesh ci cancel` seul), tout run qui porte le SHA gelé est conservé,
    comme avant (#1343).
    """
    info = pr_run(row)
    if number and info is not None:
        return (info["number"] == number and info["sha"] == frozen
                and (not base or info["base"] == base))
    return (row.get("head_sha") or "").strip().lower() == frozen


def _pr_of(api, repo: str, branch: str) -> dict:
    """La PR ouverte de la branche : numéro, tête, base, refs, étiquettes."""
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
    labels = [(lab.get("name") if isinstance(lab, dict) else str(lab))
              for lab in pr.get("labels") or ()]
    return {"number": int(pr.get("number") or 0),
            "head": ((pr.get("head") or {}).get("sha") or "").strip().lower(),
            "base": ((pr.get("base") or {}).get("sha") or "").strip().lower(),
            "head_ref": ((pr.get("head") or {}).get("ref") or ""),
            "base_ref": ((pr.get("base") or {}).get("ref") or ""),
            "labels": [name for name in labels if name]}


def _carries_freeze_request(api, repo: str, sha: str) -> bool:
    """Le workflow du SHA gelé porte-t-il le déclencheur #1343 ?

    Sans ce contrôle, un lot dont la branche précède la bascule serait refusé
    par GitHub sans dire quoi faire (aucun run ne partirait) ; ici le refus est
    explicite : refusionner la branche sur main avant de demander la CI.
    """
    import base64
    data = api("repos/%s/contents/.github/workflows/tests.yml?ref=%s"
               % (repo, _q(sha))) or {}
    content = (data.get("content") or "").replace("\n", "")
    if not content:
        return False
    try:
        text = base64.b64decode(content).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return False
    return "types: [labeled]" in text and "ci-(lot|full)-" in text


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
                  mode: str, token: str, text: str = "") -> str:
    marker = ("ci-label %s pr=%d label=%s sha=%s base=%s mode=%s token=%s"
              % (state, number, label, sha, base, mode, token))
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
                    "ts": float(ev.get("created_ts") or 0.0)})
    return sorted(out, key=lambda e: e["id"])


def _run_ts(row: dict) -> float:
    """L'instant de création d'une exécution (epoch), 0.0 s'il est illisible."""
    raw = str(row.get("created_at") or row.get("run_started_at") or "")
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def reservation_holder(events: list[dict], sha: str, base: str, mode: str, token: str, *,
                       now: float, ttl: float = RESERVE_TTL_S,
                       after: float = 0.0) -> dict | None:
    """La marque `applied` fraîche d'un autre porteur, ou None (P2 R1).

    Une étiquette posée mais pas encore visible comme exécution dans l'API
    GitHub tient la clé le temps du `ttl` ; `released` (demande refusée), les
    marques périmées et celles qui précèdent l'exécution visible (`after`) ne
    tiennent pas — une exécution visible matérialise la demande. Appelé sous le
    verrou de la ligne du lot, il lit les marques commitées des autres
    processus ; le porteur courant est exclu (`token`).
    """
    key = [e for e in events
           if e["sha"] == sha and e["base"] == base and e["mode"] == mode
           and e["state"] == "applied" and e["token"] != token]
    if not key:
        return None
    last = key[-1]
    if now - last["ts"] >= ttl or last["ts"] < after:
        return None
    return last


def gel_plan(db, cfg, item_id: int, *, api, sha: str = "", avis: int = 1,
             exception: str = "", reason: str = "", full: bool = False,
             force: bool = False, repo: str = "") -> dict:
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
    frozen_sha = tested
    mode = "full" if full else "lot"
    verdicts = latest_verdicts(db, item_id, frozen_sha, item.get("assignee") or "")
    reviews = [m for m in verdicts if (m.get("verdict") or "") == "ok"]
    blocked = [m for m in verdicts if (m.get("verdict") or "") == "blocked"]
    if not (exception or full):
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
    if release_refs(pr["head_ref"], pr["base_ref"]) and mode == "lot":
        raise GelError("pull request #%d: source %s or target %s is release/** or "
                       "integration/**: request the full suite (--full --reason …); a lot "
                       "request never covers a release"
                       % (pr["number"], pr["head_ref"] or "—", pr["base_ref"] or "—"))
    base = pr["base"]
    label = label_name(mode, frozen_sha)
    runs = _pr_runs(api, name, pr["number"], frozen_sha, branch)
    #: P1 R1 : la composition compte — même tête mais autre base, ou mode
    #: insuffisant (`lot` ne prouve pas le `full`), ne verdit pas.
    applicable = [r for r in runs if r["base"] == base and _covers(r["mode"], mode)]
    live = [r for r in applicable if r["status"] in OPEN_STATUSES]
    green = [r for r in applicable if r["status"] == "completed"
             and r["conclusion"] == "success"]
    decision = {"schema": GEL_SCHEMA, "work_item": int(item_id), "repo": name,
                "branch": branch, "target": target, "sha": frozen_sha, "base": base,
                "mode": mode, "label": label, "path": "pr-label", "pr": pr["number"],
                "exception": exception, "reason": reason.strip(), "avis": avis,
                "verdicts": sorted((m.get("actor") or "") for m in reviews),
                "frozen": bool(freeze), "runs": len(runs),
                "requested": [{"id": r["row"].get("id"), "mode": r["mode"],
                               "base": r["base"],
                               "conclusion": r["conclusion"] or r["status"]}
                              for r in applicable]}
    if live:
        return {**decision, "action": "running",
                "why": "a %s request of this composition (PR #%d, %s, base %s) is already "
                       "in flight: nothing posed"
                       % (live[0]["mode"], pr["number"], frozen_sha[:12], base[:12]),
                "run": _short_run(live[0]["row"])}
    if green:
        #: le vert applicable ne suffit pas si le dernier check requis de la
        #: tête est rouge (une étiquette étrangère a pu le laisser ainsi) : la
        #: reprise est alors explicite (`--force`), jamais automatique.
        check = _latest_check(api, name, frozen_sha)
        if check is None or (check["status"] == "completed"
                             and check["conclusion"] in CHECK_GREEN):
            return {**decision, "action": "green",
                    "why": "already green on this composition (PR #%d, %s, base %s, "
                           "mode %s): nothing posed"
                           % (pr["number"], frozen_sha[:12], base[:12], mode),
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
        #: posée par un autre processus est dite, jamais reposée.
        held = reservation_holder(request_events(db, item_id), frozen_sha, base, mode, "",
                                  now=time.time())
        if held:
            return {**decision, "action": "running",
                    "why": "a request of this composition was already reserved (token %s): "
                           "nothing posed" % held["token"]}
    if not _carries_freeze_request(api, name, frozen_sha):
        raise GelError("the workflow at %s does not carry the freeze request trigger "
                       "(#1343, ci-lot/ci-full label): rebase the lot on main before "
                       "requesting the CI" % frozen_sha[:12])
    return {**decision, "action": "request",
            "why": ("%s verdict(s) on %s" % (len(reviews), frozen_sha[:12])
                    if not (exception or full) else
                    ("exception %s: %s" % (exception, reason.strip()) if exception
                     else "full suite: %s" % reason.strip()))}


def cancel_plan(db, cfg, item_id: int, *, api, all_runs: bool = False,
                repo: str = "", number: int = 0, base: str = "") -> dict:
    """Les runs d'un lot à annuler : non conclues, hors de la composition gardée.

    `all_runs` annule aussi le run du SHA gelé (geste explicite). Un lot non
    gelé n'a pas de SHA de référence : tous ses runs ouverts sont périmés.
    Avec `number`/`base` (une demande de gel), un run de PR de la même tête
    mais d'une autre base est périmé lui aussi : sa composition a bougé.
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
    rows = [r for r in _runs_of(api, name, branch=branch)
            if (r.get("status") or "").strip().lower() in OPEN_STATUSES]
    if frozen and not all_runs:
        rows = [r for r in rows if not _current_run(r, frozen, number, base)]
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


def _ensure_label(gh, repo: str, name: str, mode: str, sha: str) -> None:
    """Crée l'étiquette du dépôt si elle manque (une par demande, #1343)."""
    if _label_exists(gh, repo, name):
        return
    payload = json.dumps({"name": name,
                          "color": "0e8a16" if mode == "lot" else "1d76db",
                          "description": "CI #1343 %s sur %s" % (mode, sha[:12])})
    code, out, err = gh.run(["api", "-X", "POST", "repos/%s/labels" % repo,
                             "--input", "-"], payload)
    if code != 0:
        raise GelError("cannot create the label %s in %s: %s"
                       % (name, repo, _gh_error(out, err)))


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


def _pose_request(gh, repo: str, pr: dict, label: str, mode: str, sha: str) -> dict:
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
        _ensure_label(gh, repo, label, mode, sha)
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


def _request_once(db, item_id: int, decision: dict, *, api, gh, force: bool) -> dict:
    """La demande, sous le verrou de la ligne du lot (#1343, P2 R1).

    `work.note` (marque `reserve`) prend le verrou `FOR NO KEY UPDATE` de
    `work_items` dans la transaction : deux `ci run` concurrents, même d'hôtes
    différents, se sérialisent ; le second relit les exécutions, la composition
    de la PR et les marques, et recule si un run est visible, si la PR a bougé
    ou si un autre a déjà posé l'étiquette. Le verrou ne survit pas au
    processus tué (ROLLBACK) : le suivant reprend, sans attente ni marque
    périmée.

    Rend `{requested, action, why?, run?}` ; `action` vaut `request` quand
    l'étiquette a été posée, sinon `green`, `running` ou `refuse`.
    """
    from . import storage, work
    token = secrets.token_hex(8)
    sha, base, mode = decision["sha"], decision["base"], decision["mode"]
    number, label = decision["pr"], decision["label"]

    def critical(tx) -> dict:
        def note(state: str, text: str = "") -> None:
            work.note(tx, int(item_id),
                      _request_note(state, number, label, sha, base, mode, token, text))

        note("reserve", "freeze CI requested")
        runs = [r for r in _pr_runs(api, decision["repo"], number, sha, decision["branch"])
                if r["base"] == base and _covers(r["mode"], mode)]
        live = [r for r in runs if r["status"] in OPEN_STATUSES]
        green = [r for r in runs if r["status"] == "completed" and r["conclusion"] == "success"]
        if live:
            return {"requested": False, "action": "running",
                    "why": "a %s request is already in flight: nothing posed"
                           % live[0]["mode"], "run": _short_run(live[0]["row"])}
        if green:
            return {"requested": False, "action": "green",
                    "why": "a green %s request of this composition is visible: nothing "
                           "posed" % green[-1]["mode"], "run": _short_run(green[-1]["row"])}
        if runs and not force:
            return {"requested": False, "action": "refuse",
                    "why": "a %s request of this composition already ran, none green: "
                           "--force to request the CI again" % runs[-1]["mode"]}
        held = reservation_holder(request_events(tx, item_id), sha, base, mode, token,
                                  now=time.time())
        if held:
            note("released", "another request (token %s) already holds this composition"
                 % held["token"])
            return {"requested": False, "action": "running",
                    "why": "a request of this composition was already reserved (token %s): "
                           "nothing posed" % held["token"]}
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
        posed = _pose_request(gh, decision["repo"], pr, label, mode, sha)
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
        note("applied", text)
        return {"requested": True, "action": "request", "run": {},
                "dropped": posed["dropped"], "warnings": posed["failed"]}

    if storage.is_remote(db):
        # connexion d'exécuteur médiée : ni transaction ni verrou de ligne ; la
        # marque reste et le second recule devant elle, sans atomicité d'hôte.
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
    run = decision.get("run") or {}
    if run.get("id"):
        lines.append("  run %s %s %s" % (run.get("id"), run.get("status"),
                                         run.get("url") or ""))
    return lines


def cmd_ci_run(cfg, args) -> int:
    """`ameesh ci run <lot>` : vérifie le gel et pose l'étiquette, une fois."""
    from . import plan_github
    from .db import open_db
    db = open_db(cfg)
    gh = plan_github.Gh()

    def api(path: str):
        return plan_github._json(gh, ["api", path])

    try:
        decision = gel_plan(db, cfg, args.id, api=api, sha=args.sha or "",
                            avis=args.avis, exception=args.exception or "",
                            reason=args.reason or "", full=args.full, force=args.force,
                            repo=args.repo or "")
    except (GelError, plan_github.GithubError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"schema": GEL_SCHEMA, "ok": False, "work_item": args.id,
                              "action": "refuse", "why": str(exc)}, ensure_ascii=False,
                             indent=1))
        else:
            print("ameesh ci run: %s" % exc)
        return 1
    if args.dry_run or decision["action"] != "request":
        if args.json:
            print(json.dumps({**decision, "ok": True, "requested": False},
                             ensure_ascii=False, indent=1))
        else:
            print("\n".join(_render_gel(decision)))
        return 0
    # canon §3 : un run de la branche hors de la composition courante (autre
    # SHA, autre base) est périmé ; jamais celui de la composition demandée.
    stale = cancel_plan(db, cfg, args.id, api=api, repo=args.repo or "",
                        number=decision["pr"], base=decision["base"])["runs"]
    if stale:
        _cancel_runs(gh, decision["repo"], stale,
                     say=lambda text: print("  %s" % text))
    try:
        result = _request_once(db, args.id, decision, api=api, gh=gh, force=args.force)
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
        if args.json:
            print(json.dumps({**decision, "ok": True, "requested": False},
                             ensure_ascii=False, indent=1))
        else:
            print("\n".join(_render_gel(decision)))
        return 0
    if result.get("warnings"):
        print("ameesh ci run: posed, with warnings: %s" % "; ".join(result["warnings"]))
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
    from . import plan_github
    from .db import open_db
    db = open_db(cfg)
    gh = plan_github.Gh()

    def api(path: str):
        return plan_github._json(gh, ["api", path])

    try:
        report = cancel_plan(db, cfg, args.id, api=api, all_runs=args.all,
                             repo=args.repo or "")
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


def cmd_ci(cfg, args) -> int:
    from .db import open_db
    command = getattr(args, "ci_command", "status")
    if command == "run":
        return cmd_ci_run(cfg, args)
    if command == "cancel":
        return cmd_ci_cancel(cfg, args)
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
